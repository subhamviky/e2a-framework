# Multimodal & Multi-Agent Orchestration

How E2A resolves a request to one of four peer agents — RAG-grounded, MCP tool call, API tool call, or LLM-only (including speech, document, and image input) — with a fallback agent covering everything else, and how that's wired into a LangGraph `StateGraph`.

## The four peers, plus fallback

`BaseWorkflow._get_agent()` already resolves intent through a name-keyed `agent_registry` lookup (Section 2.4 of the Implementation Playbook) — not an `if`/`elif` chain. Extending that registry from two shapes to four required no change to `BaseWorkflow` or `BaseAgent` at all:

| Intent | Agent | Base class | What it does |
|---|---|---|---|
| `RAG_GROUNDED` | `RAGAgent` | `BaseAgent` | Composes `BaseRAGPipeline` |
| `MCP_TOOL_CALL` | `MCPToolAgent` | `BaseAgent` | `_get_tool_service(..., transport='mcp')` |
| `API_TOOL_CALL` | `APIToolAgent` | `BaseAgent` | `_get_tool_service(..., transport='http')` |
| `LLM_ONLY` | `GenericLLMOnlyAgent` | `LLMOnlyAgent` | Model only — no tool call, no retrieval |
| *(no match / low confidence)* | `FallbackAgent` | `BaseAgent` | `_evaluate_output()` always `0.0` |

`FallbackAgent` needs no special-casing anywhere in `execute()`: its `_evaluate_output()` always returns `0.0`, which trips `BaseAgent.run()`'s existing confidence check — the same mechanism every agent already uses when its own output scores low. `_get_agent()` just returns `FallbackAgent` when the semantic router matches no registered intent, or scores the match below `min_confidence`.

## `LLMOnlyAgent` is a peer to `BaseAgent`, not a subclass

`LLMOnlyAgent` ships in `e2a_base.py` as `class LLMOnlyAgent(ABC, _PropagationMixin)` — an independent abstract line beside `BaseAgent`, `BaseRAGPipeline`, and `BaseToolService`, not a subclass of `BaseAgent`. That mirrors a decision the framework already made: `BaseRAGPipeline` is its own abstract class beside `BaseAgent` rather than a subclass of it, because RAG grounding needed a different public contract than "build a prompt, call the model."

`LLMOnlyAgent` needed the same kind of independence, for the same reason: its public contract dispatches on **modality** before a single prompt is ever built.

```python
class LLMOnlyAgent(ABC, _PropagationMixin):
    def run(self, state, config=None, ...) -> Dict[str, Any]:
        # same propagation-field / confidence-check / fallback shape as
        # BaseAgent.run() — a concrete subclass slots into agent_registry
        # and is returned by _get_agent() exactly like a BaseAgent
        # subclass would be, without inheriting from it.
        modality = self._detect_modality(state, config, **kwargs)
        handler = {"TEXT": self._handle_text, "AUDIO": self._handle_audio,
                   "IMAGE": self._handle_image, "DOCUMENT": self._handle_document}[modality]
        ...

    @abstractmethod
    def _detect_modality(self, state, config=None, **kwargs) -> str: ...
    @abstractmethod
    def _handle_text(self, state, config=None, **kwargs) -> Dict[str, Any]: ...
    @abstractmethod
    def _handle_audio(self, state, config=None, **kwargs) -> Dict[str, Any]: ...
    @abstractmethod
    def _handle_image(self, state, config=None, **kwargs) -> Dict[str, Any]: ...
    @abstractmethod
    def _handle_document(self, state, config=None, **kwargs) -> Dict[str, Any]: ...

    def _select_model(self, modality, config=None, **kwargs) -> str:
        """Concrete, framework-owned — same registry-resolution pattern
        as _get_tool_service(): a config-driven lookup, resolved by
        modality, never an if/elif chain."""

    def _llm_call(self, messages, modality, config=None, **kwargs) -> Dict[str, Any]:
        """Concrete, single-underscore (not name-mangled) so a
        subclass's _handle_* methods can call it directly."""

    @abstractmethod
    def _evaluate_output(self, response, state, config=None, **kwargs) -> float: ...
    @abstractmethod
    def _fallback(self, state, config=None, **kwargs): ...
    @abstractmethod
    def _handle_error(self, error, state, config=None, **kwargs): ...
```

Because `LLMOnlyAgent.run()` has the same public shape as `BaseAgent.run()`, a concrete subclass slots into `agent_registry` and gets returned by `_get_agent()` exactly like a `BaseAgent` subclass would — `BaseWorkflow.execute()` just calls `.run()` on whatever it gets back. No shared inheritance is needed for that to work.

## File placement

| File | Contains | Modify? |
|---|---|---|
| `src/e2a_base.py` | `BaseAgent`, `BaseWorkflow`, `BaseRAGPipeline`, `BaseToolService`, `RestToolService`, `MCPToolService`, `BaseMCPServer`, **`LLMOnlyAgent` (abstract)** | Never — drop in, subclass, never edit |
| `src/workflows/multimodal_orchestrator.py` | `GenericLLMOnlyAgent`, `RAGAgent`, `MCPToolAgent`, `APIToolAgent`, `FallbackAgent`, `MultiAgentOrchestratorWorkflow` | Yes — same placement convention as `src/agents/refund_agent.py` |

The abstract class is scaffold; the reference implementation and the orchestrator that wires it together are usage-level, downstream of the scaffold — same relationship `RefundAgent`/`OrderOpsAgent` already have to `e2a_base.py`.

## The LangGraph orchestrator

`_build_workflow()` constructs the `StateGraph` that `_validate_workflow()` checks for `required_nodes` before `execute()` ever calls `_get_agent()` — one node per peer agent, plus the fallback node, wired with the same routing rule `_get_agent()` applies at runtime, so the two can't drift apart:

```python
def _build_workflow(self, config=None, **kwargs):
    graph = StateGraph(OrchestratorState)
    graph.add_node("classify_intent", self._classify_intent_node)
    graph.add_node("rag_agent", self._agent_node("RAG_GROUNDED"))
    graph.add_node("mcp_tool_agent", self._agent_node("MCP_TOOL_CALL"))
    graph.add_node("api_tool_agent", self._agent_node("API_TOOL_CALL"))
    graph.add_node("llm_only_agent", self._agent_node("LLM_ONLY"))
    graph.add_node("fallback", self._fallback_node)
    graph.set_entry_point("classify_intent")
    graph.add_conditional_edges("classify_intent", self._route_intent, {
        "RAG_GROUNDED": "rag_agent", "MCP_TOOL_CALL": "mcp_tool_agent",
        "API_TOOL_CALL": "api_tool_agent", "LLM_ONLY": "llm_only_agent",
        "NONE": "fallback",  # no match, or score < min_confidence
    })
    for node in ("rag_agent", "mcp_tool_agent", "api_tool_agent", "llm_only_agent"):
        graph.add_edge(node, END)
    graph.add_edge("fallback", END)
    return graph.compile()
```

## Multimodal input and model selection

`GenericLLMOnlyAgent` detects modality from whichever of `audio_uri` / `image_uri` / `document_uri` / `text` is present in the request, and resolves the model per modality through the same registry-resolution pattern used everywhere else in the framework:

```python
# config['model_capability_registry'] — modality -> foundation model
{
    "TEXT":     "anthropic.claude-sonnet-4-6",
    "IMAGE":    "anthropic.claude-sonnet-4-6",   # native vision input
    "AUDIO":    "amazon.titan-text-express-v1",  # after transcription
    "DOCUMENT": "anthropic.claude-sonnet-4-6",   # after extraction
    "default":  "anthropic.claude-sonnet-4-6",
}
```

Audio and document input go through a composed `ModalityPreprocessor` (transcription / extraction) before ever reaching `_llm_call()`. Per the framework's Logical-vs-Physical rule (Section 2.6), an empty or low-confidence transcription/extraction is not treated as ground truth — it routes straight to `_fallback()` rather than reach the model as if it were complete.

## Infrastructure placement

- **RAG / MCP tool call / API tool call** — Protected tier out to the Knowledge Tier or Integration Tier and back (unchanged).
- **LLM-only** — resolves entirely inside the Protected Business Logic Tier; no Integration Tier egress, no Knowledge Tier round-trip.
- **Modality ingestion** (audio/document/image) — a Tier 2 preprocessing step in the same private subnet as `BaseAgent`, never public-facing, for the same reason the Semantic Router and Semantic Firewall are Tier 2. The intake bucket itself sits in the Data & Messaging Perimeter (Tier 3), same isolation as tool payloads.
- **Fallback** — no infrastructure of its own; runs in-process, in whichever tier the workflow was already executing in.

Full detail: `docs/e2a-framework-cloud-landing-zone.md`, Sections 3.1.4 and 8.3.

## New config keys

| Key | Env var | Default | Used in |
|---|---|---|---|
| `model_capability_registry` | `MODEL_CAPABILITY_REGISTRY_PATH` | `{}` | `LLMOnlyAgent._select_model()` |
| `stt_provider` | `STT_PROVIDER` | `None` | `ModalityPreprocessor.transcribe()` |
| `document_extractor` | `DOCUMENT_EXTRACTOR_PROVIDER` | `None` | `ModalityPreprocessor.extract()` |
| `max_audio_duration_seconds` | `MAX_AUDIO_DURATION_SECONDS` | `600` | Checked before `transcribe()` |
| `max_document_pages` | `MAX_DOCUMENT_PAGES` | `50` | Checked before `extract()` |

## Verification

`MultiAgentOrchestratorWorkflow` was run end-to-end against the real, unmodified `BaseWorkflow.execute()` / `BaseAgent.run()` — not a stub — for all four intents plus an unmatched intent and a below-threshold match. Both the runtime `_get_agent()` dispatch path and the declarative `graph.invoke()` path agree on routing in every case. `issubclass(LLMOnlyAgent, BaseAgent)` is `False`; `issubclass(GenericLLMOnlyAgent, LLMOnlyAgent)` is `True`.

```
$ python3 src/workflows/multimodal_orchestrator.py
RAG_GROUNDED  (score=0.9)  -> {'text': '...', 'metadata': {'model_id': 'default'}}
MCP_TOOL_CALL (score=0.9)  -> {'text': '...', 'metadata': {'model_id': 'default'}}
API_TOOL_CALL (score=0.9)  -> {'text': '...', 'metadata': {'model_id': 'default'}}
LLM_ONLY      (score=0.9)  -> {'text': '...', 'metadata': {'model_id': 'default'}}
UNKNOWN_INTENT(score=0.9)  -> {'text': '', 'metadata': {'degraded': True, 'reason': 'no_intent_match'}}
RAG_GROUNDED  (score=0.5)  -> {'text': '', 'metadata': {'degraded': True, 'reason': 'no_intent_match'}}
```
