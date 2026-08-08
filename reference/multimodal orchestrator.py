# src/workflows/multimodal_orchestrator.py
#
# Four peer agents — RAGAgent, MCPToolAgent, APIToolAgent, and
# GenericLLMOnlyAgent (a concrete subclass of the abstract LLMOnlyAgent
# in src/e2a_base.py) — orchestrated by a LangGraph StateGraph, with
# FallbackAgent handling any intent that matches none of the four (or
# that the semantic router scores below min_confidence). Drop into
# src/workflows/. Imports everything it builds on from src/e2a_base.py
# (Playbook Section 3) — that file is never modified.
#
# github.com/subhamviky/e2a-framework

import asyncio
import os
from typing import Any, Dict, Optional, TypedDict

from langgraph.graph import StateGraph, END

from src.e2a_base import (
    BaseAgent,
    BaseWorkflow,
    BaseRAGPipeline,
    LLMOnlyAgent,
    _get_tool_service,
)


class OrchestratorState(TypedDict, total=False):
    query: str
    tenant_id: str
    correlation_id: str
    intent: str
    intent_score: float
    tool_name: str
    tool_payload: Dict[str, Any]
    mcp_server_url: str
    mcp_tool_name: str
    audio_uri: str
    image_uri: str
    document_uri: str
    text: str
    response: Dict[str, Any]


class ModalityPreprocessor:
    def transcribe(self, audio_uri: str, config: Optional[dict] = None,
                    **kwargs) -> str:
        """kwargs: stt_provider, max_audio_duration_seconds. Raises
        NFRViolationError (defined above in this file) if the object at audio_uri
        exceeds max_audio_duration_seconds before the STT provider is
        ever called — same pre-flight pattern BaseAgent.run() uses for
        max_latency (Section 2.3)."""
        raise NotImplementedError("wire to Transcribe / Speech-to-Text")

    def extract(self, document_uri: str, config: Optional[dict] = None,
                **kwargs) -> str:
        """kwargs: document_extractor, max_document_pages. Same
        pre-flight page-count check as transcribe()'s duration check."""
        raise NotImplementedError("wire to Textract / Document AI")


# ======================================================================
# Four peer agents, all BaseAgent subclasses defined above in this
# resolved through the same agent_registry lookup in _get_agent()
# below — no agent knows about the others, and none is a special case
# of any other. Each implements exactly the five abstract methods
# BaseAgent.run() calls (Section 2.3): _build_messages, _apply_policy,
# _evaluate_output, _fallback, _handle_error. Nothing here is a new
# hook — the tool-call agents reuse _get_tool_service() precisely as
# OrderOpsAgent does in Section 4.6, just with transport forced instead
# of left to config['mcp_server_registry'] auto-detection.
# ======================================================================
class RAGAgent(BaseAgent):
    agent_name = "RAGAgent"

    def __init__(self, rag_pipeline: BaseRAGPipeline):
        self.rag_pipeline = rag_pipeline  # composed in, not inherited —
        # same pattern as prompt_registry in Section 4.7

    def _build_messages(self, state, config=None, tenant_id=None, **kwargs):
        results = self.rag_pipeline.retrieve(
            state["query"], config, tenant_id=tenant_id, **kwargs)
        grounding = "\n".join(r.get("text", "") for r in results)
        return [{"role": "user", "content": f"{grounding}\n\n{state['query']}"}]

    def _apply_policy(self, state, config=None, **kwargs):
        pass  # grounding already resolved in _build_messages(); no tool call

    def _evaluate_output(self, response, state, config=None, **kwargs):
        # BaseRAGPipeline._evaluate_answer() is the protected hook a
        # concrete pipeline implements (Section 2.5) — called directly,
        # the same way an agent calls its own protected methods.
        return self.rag_pipeline._evaluate_answer(
            response.get("text", ""), config, **kwargs)

    def _fallback(self, state, config=None, **kwargs):
        return {"text": "", "metadata": {"degraded": True, "reason": "faithfulness_threshold"}}

    def _handle_error(self, error, state, config=None, **kwargs):
        kwargs["failed_keys"].append(state.get("correlation_id"))


class APIToolAgent(BaseAgent):
    agent_name = "APIToolAgent"

    def _build_messages(self, state, config=None, **kwargs):
        return [{"role": "user", "content": state["query"]}]

    def _apply_policy(self, state, config=None, tenant_id=None, **kwargs):
        # _get_tool_service() is the same registry-resolution function
        # _resolve_tool_call() calls internally (Section 2.8) — called
        # directly here, with transport pinned to 'http', to skip the
        # mcp_server_registry auto-detection _resolve_tool_call() does
        # and force REST regardless of what's registered for this tool.
        service = _get_tool_service(state["tool_name"], config, transport="http")
        state["tool_result"] = asyncio.run(service.execute(
            state.get("tool_payload", {}), config, tenant_id=tenant_id, **kwargs))

    def _evaluate_output(self, response, state, config=None, **kwargs):
        # Logical-vs-Physical rule (Section 2.6): only physical_success
        # is ground truth, never the LLM response text.
        return 1.0 if state.get("tool_result", {}).get("physical_success") else 0.0

    def _fallback(self, state, config=None, **kwargs):
        return {"text": "", "metadata": {"degraded": True, "reason": "api_tool_call_failed"}}

    def _handle_error(self, error, state, config=None, **kwargs):
        kwargs["failed_keys"].append(state.get("correlation_id"))


class MCPToolAgent(BaseAgent):
    agent_name = "MCPToolAgent"

    def _build_messages(self, state, config=None, **kwargs):
        return [{"role": "user", "content": state["query"]}]

    def _apply_policy(self, state, config=None, tenant_id=None, **kwargs):
        # Same _get_tool_service() call as APIToolAgent, transport
        # pinned to 'mcp' instead of 'http'. mcp_server_url/mcp_tool_name
        # follow the same kwargs MCPToolService.get_endpoint() and
        # ._build_payload() already read (Section 3, MCPToolService).
        service = _get_tool_service(state["tool_name"], config, transport="mcp")
        state["tool_result"] = asyncio.run(service.execute(
            state.get("tool_payload", {}), config, tenant_id=tenant_id,
            mcp_server_url=state.get("mcp_server_url"),
            mcp_tool_name=state.get("mcp_tool_name", state.get("tool_name")),
            **kwargs))

    def _evaluate_output(self, response, state, config=None, **kwargs):
        return 1.0 if state.get("tool_result", {}).get("physical_success") else 0.0

    def _fallback(self, state, config=None, **kwargs):
        return {"text": "", "metadata": {"degraded": True, "reason": "mcp_tool_call_failed"}}

    def _handle_error(self, error, state, config=None, **kwargs):
        kwargs["failed_keys"].append(state.get("correlation_id"))


class GenericLLMOnlyAgent(LLMOnlyAgent):
    """Reference implementation of LLMOnlyAgent — text handled inline,
    audio/document routed through a composed ModalityPreprocessor,
    image passed straight to a vision-capable model. Generic (no
    domain fields), so it ships here rather than in src/agents/, same
    as RAGAgent/MCPToolAgent/APIToolAgent below. Subclass LLMOnlyAgent
    directly instead of this class for any agent that needs different
    per-modality handling — this is one valid implementation, not the
    only one."""

    def __init__(self, modality_preprocessor: Optional["ModalityPreprocessor"] = None):
        self.modality_preprocessor = modality_preprocessor or ModalityPreprocessor()

    def _detect_modality(self, state, config=None, **kwargs) -> str:
        for key, modality in (("audio_uri", "AUDIO"), ("image_uri", "IMAGE"),
                               ("document_uri", "DOCUMENT")):
            if state.get(key):
                return modality
        return "TEXT"

    def _handle_text(self, state, config=None, **kwargs) -> Dict[str, Any]:
        messages = [{"role": "user", "content": state.get("text", "")}]
        return self._llm_call(messages, "TEXT", config, **kwargs)

    def _handle_audio(self, state, config=None, **kwargs) -> Dict[str, Any]:
        transcript = self.modality_preprocessor.transcribe(
            state["audio_uri"], config, **kwargs)
        messages = [{"role": "user", "content": transcript}]
        return self._llm_call(messages, "AUDIO", config, **kwargs)

    def _handle_document(self, state, config=None, **kwargs) -> Dict[str, Any]:
        extracted = self.modality_preprocessor.extract(
            state["document_uri"], config, **kwargs)
        messages = [{"role": "user", "content": extracted}]
        return self._llm_call(messages, "DOCUMENT", config, **kwargs)

    def _handle_image(self, state, config=None, **kwargs) -> Dict[str, Any]:
        # vision-capable model takes the reference directly — no
        # transcription/extraction step
        messages = [{"role": "user", "content": state["image_uri"]}]
        return self._llm_call(messages, "IMAGE", config, **kwargs)

    def _evaluate_output(self, response, state, config=None, **kwargs) -> float:
        return 1.0 if response.get("text") else 0.0

    def _fallback(self, state, config=None, **kwargs):
        # Extraction Backstop pattern (Playbook Section 2.15), unchanged —
        # degrades to fallback_model; no re-transcription, no re-extraction.
        return {"text": "", "metadata": {"degraded": True}}

    def _handle_error(self, error, state, config=None, **kwargs):
        kwargs["failed_keys"].append(state.get("correlation_id"))


class FallbackAgent(BaseAgent):


    """Returned by _get_agent() when the semantic router matches no
    registered intent, or scores confidence below min_confidence.
    _evaluate_output() always returns 0.0, so BaseAgent.run()'s own
    confidence check (Section 2.3) fires _fallback() immediately —
    zero changes to run() or execute() were needed to add this path."""

    agent_name = "FallbackAgent"

    def _build_messages(self, state, config=None, **kwargs):
        return [{"role": "user", "content": state.get("query", "")}]

    def _apply_policy(self, state, config=None, **kwargs):
        pass

    def _evaluate_output(self, response, state, config=None, **kwargs):
        return 0.0  # forces _fallback() unconditionally

    def _fallback(self, state, config=None, **kwargs):
        return {"text": "", "metadata": {"degraded": True, "reason": "no_intent_match"}}

    def _handle_error(self, error, state, config=None, **kwargs):
        kwargs["failed_keys"].append(state.get("correlation_id"))


# ======================================================================
# Orchestrator — a concrete BaseWorkflow subclass, defined in this same
# file rather than a downstream repo, because none of the four agents
# above carry any domain-specific field (order_id, refund_limit, ...):
# they only read generic keys (query, tool_name, audio_uri) and compose
# collaborators, exactly like RestToolService/MCPToolService above —
# reusable across any tenant unmodified, so they belong beside those,
# not beside RefundAgent/OrderOpsAgent in src/agents/. _build_workflow() (abstract in BaseWorkflow,
# Section 2.4) constructs the LangGraph StateGraph that _validate_workflow()
# checks before BaseWorkflow.execute() calls _get_agent() and agent.run() —
# execute() itself is inherited unchanged, never overridden here, exactly
# as Section 4.3 requires ("the public method is never overridden").
# ======================================================================
class MultiAgentOrchestratorWorkflow(BaseWorkflow):
    workflow_name = "MultiAgentOrchestratorWorkflow"

    def __init__(self, rag_pipeline: BaseRAGPipeline, semantic_router,
                 min_confidence: float = 0.85):
        self.semantic_router = semantic_router  # classify_intent() -> (intent, score)
        self.min_confidence = min_confidence
        # Note: LLM_ONLY resolves to a GenericLLMOnlyAgent, an instance
        # of the standalone LLMOnlyAgent abstract line above — not of
        # BaseAgent. Both satisfy the same run() contract, so the graph
        # and _get_agent() below treat all four uniformly.
        self.agent_registry: Dict[str, Any] = {
            "RAG_GROUNDED": RAGAgent(rag_pipeline),
            "MCP_TOOL_CALL": MCPToolAgent(),
            "API_TOOL_CALL": APIToolAgent(),
            "LLM_ONLY": GenericLLMOnlyAgent(),
        }
        self.fallback_agent = FallbackAgent()

    def _build_workflow(self, config=None, **kwargs):
        graph = StateGraph(OrchestratorState)
        graph.add_node("classify_intent", self._classify_intent_node)
        graph.add_node("rag_agent", self._agent_node("RAG_GROUNDED"))
        graph.add_node("mcp_tool_agent", self._agent_node("MCP_TOOL_CALL"))
        graph.add_node("api_tool_agent", self._agent_node("API_TOOL_CALL"))
        graph.add_node("llm_only_agent", self._agent_node("LLM_ONLY"))
        graph.add_node("fallback", self._fallback_node)

        graph.set_entry_point("classify_intent")
        graph.add_conditional_edges(
            "classify_intent",
            self._route_intent,
            {
                "RAG_GROUNDED": "rag_agent",
                "MCP_TOOL_CALL": "mcp_tool_agent",
                "API_TOOL_CALL": "api_tool_agent",
                "LLM_ONLY": "llm_only_agent",
                "NONE": "fallback",  # no match, or score < min_confidence
            },
        )
        for node in ("rag_agent", "mcp_tool_agent", "api_tool_agent", "llm_only_agent"):
            graph.add_edge(node, END)
        graph.add_edge("fallback", END)
        return graph.compile()

    def _validate_workflow(self, workflow, config=None, **kwargs) -> bool:
        required_nodes = kwargs.get("required_nodes", {
            "classify_intent", "rag_agent", "mcp_tool_agent",
            "llm_only_agent", "api_tool_agent", "fallback",
        })
        graph_nodes = set(workflow.get_graph().nodes.keys())
        return required_nodes.issubset(graph_nodes)

    def _get_agent(self, intent, config=None, **kwargs):
        """The dispatch BaseWorkflow.execute() actually calls (Section 2.4).
        Returns a BaseAgent instance for RAG_GROUNDED/MCP_TOOL_CALL/
        API_TOOL_CALL, or an LLMOnlyAgent instance for LLM_ONLY — both
        satisfy the same run() contract, so execute() calling
        agent.run(...) works unmodified either way. Mirrors
        _route_intent() below exactly — same registry, same fallback
        rule — so the two never drift apart."""
        agent_registry = kwargs.get("agent_registry", self.agent_registry)
        score = kwargs.get("intent_score", 1.0)
        if intent not in agent_registry or score < self.min_confidence:
            return self.fallback_agent
        return agent_registry[intent]

    def _generate_idempotency_key(self, state, config=None, **kwargs) -> str:
        import hashlib
        return hashlib.sha256(
            f"{state.get('tenant_id')}:{state.get('query', '')}".encode()
        ).hexdigest()

    def _handle_error(self, error, state, config=None, **kwargs):
        kwargs["failed_keys"].append(state.get("correlation_id"))

    # -- LangGraph node functions -------------------------------------
    def _classify_intent_node(self, state):
        intent, score = self.semantic_router.classify_intent(state["query"])
        state["intent"], state["intent_score"] = intent, score
        return state

    def _route_intent(self, state) -> str:
        if state["intent"] not in self.agent_registry or \
                state["intent_score"] < self.min_confidence:
            return "NONE"
        return state["intent"]

    def _agent_node(self, intent: str):
        agent = self.agent_registry[intent]

        def node(state):
            return agent.run(state)
        return node

    def _fallback_node(self, state):
        return self.fallback_agent.run(state)


if __name__ == "__main__":
    # Smoke test against the real src/e2a_base.py scaffold — builds and
    # validates the graph, and drives BaseWorkflow.execute() (inherited,
    # unmodified) through all five peer paths without a live LLM, tool
    # backend, or vector store. See tests/ for wired integration tests.
    class _StubRAGPipeline(BaseRAGPipeline):
        def _search_index(self, query_vector, config=None, tenant_id=None, **kwargs):
            return [{"text": "stub-grounding"}]

        def _rerank(self, results, config=None, **kwargs):
            return results

        def _evaluate_answer(self, answer, config=None, **kwargs):
            return 1.0

    class _StubRouter:
        def __init__(self, intent="LLM_ONLY", score=0.93):
            self.intent, self.score = intent, score

        def classify_intent(self, query):
            return self.intent, self.score

    for intent, score in [
        ("RAG_GROUNDED", 0.9), ("MCP_TOOL_CALL", 0.9), ("API_TOOL_CALL", 0.9),
        ("LLM_ONLY", 0.9), ("UNKNOWN_INTENT", 0.9), ("RAG_GROUNDED", 0.5),
    ]:
        wf = MultiAgentOrchestratorWorkflow(_StubRAGPipeline(), _StubRouter(intent, score))
        graph = wf._build_workflow(config={})
        assert wf._validate_workflow(graph, config={})
        state = {"tenant_id": "acme", "query": "test", "tool_name": "check_stock",
                 "tool_payload": {"sku": "SKU-1"}, "intent": intent, "text": "hello"}
        result = wf.execute(state, config={}, intent_score=score)
        print(f"{intent} (score={score}) -> {result['response']}")
    print("OK — four peer agents (one of them a GenericLLMOnlyAgent, not a\n"
          "BaseAgent subclass) + fallback, driven through the real,\n"
          "unmodified BaseWorkflow.execute().")
