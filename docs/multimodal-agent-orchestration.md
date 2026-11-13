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

`LLMOnlyAgent` ships in `e2a_base.py` (since v1.2 in `e2a/agents.py`, re-exported unchanged by `e2a_base`; its contract did not change in v1.2) as `class LLMOnlyAgent(ABC, _PropagationMixin)` — an independent abstract line beside `BaseAgent`, `BaseRAGPipeline`, and `BaseToolService`, not a subclass of `BaseAgent`. That mirrors a decision the framework already made: `BaseRAGPipeline` is its own abstract class beside `BaseAgent` rather than a subclass of it, because RAG grounding needed a different public contract than "build a prompt, call the model."

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

    # No model call lives here. _handle_* methods call the inherited
    # self._infer(messages, config, model_id=self._select_model(...)),
    # which delegates to config['llm_service'] (BaseLLMAgentService).

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
| `src/e2a_base.py` | `BaseAgent`, `BaseWorkflow`, `BaseRAGPipeline`, `BaseToolService`, `RestToolService`, `MCPToolService`, `BaseMCPServer`, `BaseLLMAgentService` (abstract), **`LLMOnlyAgent` (abstract)** | Never — drop in, subclass, never edit |
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

## Concurrency: Isolation, Deadlines and Bounded Fan-Out

Concurrency here is owned by invariant, not by a manager: each risk is assigned to a class, a contract or a config key that already exists (Implementation Playbook Section 2.19). The guarantee is effectively-once material effects under at-least-once delivery, never exactly-once.

**Request-local state.** The compiled `StateGraph` is built once and invoked by many requests at once, so nothing request-specific may live on `self` or in module globals. Every `execute()` call starts from its own `OrchestratorState`; the propagation fields (`correlation_id`, `tenant_id`, `message_log`, `failed_keys` and the additive `deadline`) are passed as arguments, never stored as attributes — the same rule `BaseWorkflow` already follows. Agents, the `ModalityPreprocessor` and the tool and LLM services are shared, long-lived and stateless per request; the only mutable state they hold is bounded by design (permits, bulkhead slots, circuit counters, cache entries).

**Routing is exclusive, so there is no fan-out by default.** The conditional edge sends a request to exactly one peer. Fan-out is added only for genuinely independent branches — for example a RAG retrieval and a read-only lookup whose results do not depend on each other — and then only with a hard bound (a semaphore no larger than the pod's share of `pod_concurrency`) and one shared deadline for the whole fan-out. A branch that can change state (an MCP or API tool call with a side effect) is never one of several speculative parallel branches. The scaffold's graph has no fan-out edge today; this is the rule for adding one, not an implemented feature.

**Deadlines and cancellation.** `BaseWorkflow.execute()` resolves one `deadline` for the request (from an inbound deadline or `request_deadline_seconds`; none when neither is set) and passes it to every agent. The synchronous agent path reaches the asynchronous tool services through `_run_tool()`, which runs the coroutine on the shared bridge loop — never `asyncio.run()`, because a fresh loop per call cannot be cancelled by its parent and breaks per-loop primitives. The wait is bounded by `min(timeout × (retries + 1) + 1, deadline.remaining() + 0.5)`; when it expires the coroutine is **cancelled**, not orphaned, so a timed-out tool call does not keep consuming a bulkhead slot or a connection.

**Side effects and the claim.** Only tool-call intents (`MCP_TOOL_CALL`, `API_TOOL_CALL`) with a score at or above `min_confidence` can have a material effect, so only they take an atomic idempotency claim (`_requires_idempotency_claim()`). RAG, LLM-only and fallback answers are generative reads: they are never claimed and never replayed from a stored answer. After a tool call the agent records the typed outcome in `state['outcome']` (`COMMITTED`, `NOT_COMMITTED` or `UNKNOWN`). A timeout on a non-idempotent write yields `UNKNOWN`: the request was sent exactly once, the key is parked for reconciliation, and a duplicate gets `RECOVERY_REQUIRED` — it is never re-sent blindly.

**Capacity, one owner per resource.**

| Resource | Bound | Owner |
|---|---|---|
| Inference (all four peers that call a model) | Tenant + pod permit (`permit_timeout`), then token quota (`quota_timeout`), then the upstream call under `min(llm_max_latency, remaining deadline)`; quota refunded on failure and cancellation | `BaseLLMAgentService` — shared by every agent through `config['llm_service']`; each agent adds no limit of its own |
| Modality preprocessing (transcription, extraction) | Input size limits `max_audio_duration_seconds` and `max_document_pages`, plus (a deployment obligation, not scaffold code) a preprocessing worker pool sized apart from the inference permits | `ModalityPreprocessor` — independent capacity, so a burst of large audio or documents cannot consume the permits that text requests need |
| RAG retrieval and index | Index and vector-store connection pool (`db_pool_size`, `http_pool_size`) | `BaseRAGPipeline` adapters |
| MCP and API tool calls | Per-dependency bulkhead (`bulkhead_max_concurrent`, `bulkhead_acquire_timeout`), circuit breaker (`circuit_failure_threshold`, `circuit_reset_seconds`) and retry budget (`retry_budget_ratio`); a timeout is retried only if the call is idempotent | `BaseToolService` through `ResiliencePolicy` |
| LLM upstream | Circuit breaker only; admission is the permit and quota above | `BaseLLMAgentService` |

`validate_capacity()` checks these together at startup, so a sizing mistake fails the scaffold-compliance gate instead of surfacing under load: the pod's inference concurrency must fit the HTTP pool, the tenant cap must not exceed the pod cap, and fleet model concurrency including the retry budget must fit the model's limit.

**Optional single-flight.** Duplicate work that is expensive and safe to share can be collapsed with `BaseCacheStore.get_or_load`: concurrent identical retrievals for one RAG query, or repeated transcription or extraction of the same content hash. Followers wait up to `cache_fill_timeout_seconds` (bounded by the deadline) and then follow `cache_fill_on_timeout` (`load`, `stale` or `raise`). It is optional and applies only to deterministic, side-effect-free work. It is never applied to LLM generation or to tool calls, whose duplicates are governed by the claim instead.

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

Audio and document input go through a composed `ModalityPreprocessor` (transcription / extraction) before ever reaching the LLM service (`_infer()`). Per the framework's Logical-vs-Physical rule (Section 2.6), an empty or low-confidence transcription/extraction is not treated as ground truth — it routes straight to `_fallback()` rather than reach the model as if it were complete.

## Infrastructure placement

- **RAG / MCP tool call / API tool call** — Protected tier out to the Knowledge Tier or Integration Tier and back (unchanged).
- **LLM-only** — resolves entirely inside the Protected Business Logic Tier; no Integration Tier egress, no Knowledge Tier round-trip.
- **Modality ingestion** (audio/document/image) — a Tier 2 preprocessing step in the same private subnet as `BaseAgent`, never public-facing, for the same reason the Semantic Router and Semantic Firewall are Tier 2. The intake bucket itself sits in the Data & Messaging Perimeter (Tier 3), same isolation as tool payloads.
- **Fallback** — no infrastructure of its own; runs in-process, in whichever tier the workflow was already executing in.
- **Capacity placement** — preprocessing runs on its own worker pool in the Tier 2 subnet, scaled independently of the inference gateway; the shared `BaseLLMAgentService` (Tier 2b) is the single place model concurrency and quota are enforced, whichever peer calls it; each tool dependency has its own bulkhead and circuit at the Integration Tier boundary.

Full detail: `docs/e2a-framework-cloud-landing-zone.md`, Sections 3.1.4 and 8.3.

## New config keys

| Key | Env var | Default | Used in |
|---|---|---|---|
| `model_capability_registry` | `MODEL_CAPABILITY_REGISTRY_PATH` | `{}` | `LLMOnlyAgent._select_model()` |
| `stt_provider` | `STT_PROVIDER` | `None` | `ModalityPreprocessor.transcribe()` |
| `document_extractor` | `DOCUMENT_EXTRACTOR_PROVIDER` | `None` | `ModalityPreprocessor.extract()` |
| `max_audio_duration_seconds` | `MAX_AUDIO_DURATION_SECONDS` | `600` | Checked before `transcribe()` |
| `max_document_pages` | `MAX_DOCUMENT_PAGES` | `50` | Checked before `extract()` |
| `request_deadline_seconds` | `REQUEST_DEADLINE_SECONDS` | `0` (none) | `BaseWorkflow.execute()` — origin of the request `deadline`, passed to `_run_tool()` |
| `idempotency_lease_seconds` | `IDEMPOTENCY_LEASE_SECONDS` | `300` | Atomic claim for tool-call intents; an expired claim needs reconciliation |
| `bulkhead_max_concurrent`, `bulkhead_acquire_timeout` | `BULKHEAD_MAX_CONCURRENT`, `BULKHEAD_ACQUIRE_TIMEOUT` | `16`, `1.0` | Tool-service bulkhead (per dependency) |
| `permit_timeout`, `quota_timeout` | `LLM_PERMIT_TIMEOUT`, `LLM_QUOTA_TIMEOUT` | `5.0`, `5.0` | `BaseLLMAgentService` — bounded permit and quota waits shared by all agents |
| `cache_fill_timeout_seconds`, `cache_fill_on_timeout` | `CACHE_FILL_TIMEOUT_SECONDS`, `CACHE_FILL_ON_TIMEOUT` | `5.0`, `load` | Optional single-flight for retrieval and preprocessing |

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

### Concurrency verification

`smoke_test_multimodal_concurrency.py` runs the same unmodified `BaseWorkflow` against an in-memory idempotency store and a probe tool service. Its checks: `no asyncio.run left in the orchestrator`; `T-01 duplicate tool-call request replays; the tool was sent once`; `generative intents are not claimed or replayed`; `T-12 slow tool: typed UNKNOWN, sent exactly once, bounded wait`; `T-12 UNKNOWN parks the key; a duplicate is not re-sent`; `request deadline bounds a tool call far below the tool timeout`. Inference admission, quota and cancellation are proven once at the gateway (`smoke_test_concurrency.py`: T-04, T-10, T-11) and capacity-equation checking by `validate_capacity()` in the same suite, rather than re-tested per agent.

Not covered by the scaffold's smoke tests, and to be proven in a load environment before release:

| Test | Expected result |
|---|---|
| Mixed-modal fairness: a sustained burst of long audio and large documents alongside a steady stream of text requests | Text p95 latency stays within its objective; preprocessing saturation rejects or queues within its bound and consumes no inference permits |
| Single-flight under identical concurrent retrievals (when enabled) | One index query; followers return the shared result or follow `cache_fill_on_timeout` |
| Independent-branch fan-out (when added) | In-flight branches never exceed the bound; one shared deadline cancels the rest; no side-effecting branch is run speculatively |
| Tool dependency down | Circuit opens, calls fail fast, other dependencies and other modalities unaffected |
