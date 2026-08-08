# Enterprise-to-Agentic (E2A) Architecture Framework
### Implementation Playbook — Scaffold File, Class Contracts & Usage Guide

> **Framework reference** — Repository: [github.com/subhamviky/e2a-framework](https://github.com/subhamviky/e2a-framework) · Reference implementation: [github.com/subhamviky/order-to-cash-agentic-ai](https://github.com/subhamviky/order-to-cash-agentic-ai) · Scaffold file: `e2a_base.py` — drop into any repo, inherit, override, run. · Document role: single, consolidated playbook. The Master Abstraction Reference defines WHAT the framework enforces; this Playbook defines HOW to implement it — including MCP server hosting and cross-class propagation as core, first-class content rather than addenda.

# 1. Purpose & Scope

This playbook provides everything a developer needs to implement the E2A Architecture Framework in a new or existing repository: the complete, runnable scaffold file (e2a_base.py), method signature specifications for all primary abstract classes, configuration and environment variable resolution patterns, and a full worked example from inheritance through execution.

This edition folds two sets of content that earlier revisions carried as separate addenda directly into the main body, so there is a single set of class contracts to read rather than a base version plus patches:

- MCP is now native to the Tool Services layer. Alongside BaseToolService (the existing client-side tool caller, which can speak REST or MCP transport via RestToolService / MCPToolService), the framework defines BaseMCPServer — a class whose job is to turn existing wrapped APIs into a spec-compliant MCP server exposing tools/list and tools/call — plus a single tool-call routing function that checks whether a wrapped MCP server exists for a given tool before falling back to the normal REST tool call.

- The cross-class propagation contract (correlation_id, io_config, idempotency_key, tenant_id, message_log, failed_keys) is written directly into the constructor and method tables of BaseWorkflow, BaseAgent, BaseRAGPipeline, BaseToolService, and BaseMCPServer. There is no separate reconciliation step — the tables below are the current, single source of truth.

- State validation is no longer a method each BaseAgent subclass implements on itself. It is now a separate class, BaseValidationService (Section 2.2), invoked before BaseWorkflow or BaseAgent is instantiated at all — so an invalid request never pays for agent-service compute. BaseAgent's contract no longer includes _validate_state.

The document is structured in four parts:

- Part A — Class Contract Specifications (Section 2): complete method signatures, config/kwargs, and the propagation contract for every class — BaseValidationService, BaseAgent, BaseWorkflow, BaseRAGPipeline, BaseToolService, BaseMCPServer, tool-call routing, and guidance on progressively splitting a class into multiple microservices as it grows.

- Part B — Scaffold File (Section 3): the ready-to-drop e2a_base.py with all abstract classes, fully wired.

- Part C — Usage Guide (Section 4): repo structure, inheritance, invocation, config/env integration, a worked example that includes validation, wrapping an API as an MCP server, and tool-call routing.

- Part D — Global Config / Environment Reference (Section 5): one consolidated table for every config key across every class.

> **Single Rule**
>
> Every class has exactly one public entry point: validate() for BaseValidationService, run() for BaseAgent, execute() for BaseWorkflow and BaseToolService, retrieve() for BaseRAGPipeline, serve() for BaseMCPServer. Application code calls only this public method. All protected methods are called internally in a fixed sequence. Subclasses override only the protected methods they need.

*Source Note: Implementation examples and the categorical breakdown of cloud-native primitives in this playbook align with the AWS AI Ecosystem architectural map by Prashant Rathi.*

# 2. Class Contract Specifications

This section specifies the complete method contracts for each abstract class — access modifier, signature, accepted config/kwargs keys, and purpose. The config parameter and **kwargs together provide full flexibility: pass explicit values at call time, or rely on config dict defaults, or fall back to environment variables. The resolution order is always: kwargs override → config dict → environment variable → hardcoded default.

## 2.1 Cross-Class Propagation Fields

Six fields flow through every PUBLIC, PROTECTED, and PRIVATE method across all five classes (BaseWorkflow, BaseAgent, BaseRAGPipeline, BaseToolService, BaseMCPServer). Four are inputs, resolved once and passed down unchanged; two are outputs, mutated in place by every method that touches them and read once at the end of the chain. This is core to the framework, not an extension of it — every constructor and method table in this section already reflects these fields.

> **Why Not Class-Level (self.) Attributes**
>
> A field stored as self.correlation_id would be shared instance state. If a BaseWorkflow or BaseAgent instance is reused across concurrent requests — the normal case behind FastAPI/Lambda/ECS — two in-flight requests writing to the same self.message_log would corrupt each other's logs and failure data. Every field below is created fresh at the top of BaseWorkflow.execute() for a single logical request, passed as an explicit parameter to every method it touches, and discarded when that request completes.

| **Direction** | **Field**                 | **Resolved / Mutated**                                                                                                  | **Purpose**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
|---------------|---------------------------|-------------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| INPUT         | correlation_id: str       | Generated once in BaseWorkflow.execute() (uuid4, or client-supplied). Read-only below that point.                       | Ties every log line and DLQ entry across all classes back to one logical request.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| INPUT         | io_config: dict           | Resolved once in BaseWorkflow.execute() from config['io'] (or IO_CONFIG env namespace); a validated subset of config. | Separates connection/endpoint concerns (DB, S3, queue, vector store, MCP server URLs) from behavioral NFR config. Also carries max_token_budget, read by BaseGovernanceFramework.enforce_governance_gate() (Section 2.11) to cap cumulative LLM spend per request.                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| INPUT         | idempotency_key: str      | Resolved once via __resolve_idempotency() — client-supplied and validated, or generated.                              | Lets BaseToolService, BaseMCPServer, and any commit/save path detect and skip a request that already completed.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| INPUT         | tenant_id: str            | Read from state['tenant_id']; validated present in BaseWorkflow.execute() before routing.                             | Scopes RAG index search, tool endpoint resolution, and MCP server/tool registry lookups to a tenant namespace.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| OUTPUT        | message_log: list[dict] | Empty list created in BaseWorkflow.execute(); every method appends structured entries via the shared _log() helper.    | One ordered, structured log for the entire request. BaseWorkflow.execute() no longer flushes it to stdout directly: its finally block hands message_log to a configured BaseObservability.record_telemetry() implementation (Section 2.10), falling back to per-entry logging only if no observability_engine is configured. Individual entries carry whatever fields the writer attached — BaseAgent.run(), BaseRAGPipeline.retrieve(), and BaseToolService.execute() each attach latency (seconds) to their own completion entry, and BaseAgent.run() additionally attaches tokens_used and confidence, which is what lets BaseObservability.__extract_metrics() (Section 2.10) sum total_tokens without any class-specific parsing. |
| OUTPUT        | failed_keys: list[str]  | Empty list created in BaseWorkflow.execute(); any _handle_error() across any class appends the failing item's key.     | Drives the commit gate and end-of-request DLQ dispatch — failed items are isolated, successful items still commit.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |

### 2.1.1 AgentState Schema — Additions

state (the dict threaded through every BaseAgent method) carries three required keys, using the same mechanism trace_id already used before correlation_id replaced it.

| **Kind** | **Key**              | **Resolution**                                                                    | **Purpose**                                                                                             |
|----------|----------------------|-----------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------|
| STATE    | tenant_id: str       | Required. Set by the API boundary before BaseWorkflow.execute() is called.        | Read by BaseRAGPipeline._search_index() and BaseToolService/BaseMCPServer for tenant-scoped filtering. |
| STATE    | correlation_id: str  | Required after BaseWorkflow.execute() runs. Mirrors the correlation_id parameter. | Convenience accessor for any code that only has state, not the full call signature.                     |
| STATE    | idempotency_key: str | Required after BaseWorkflow.execute() runs.                                       | Same rationale — available on state for handlers that don't thread the full parameter list.             |

## 2.2 BaseValidationService

*validation/ — validate() is the single public entry point. Runs before any agent, workflow, RAG, tool, or MCP class is invoked — and before any of them are even instantiated.*

Every prior revision of this playbook had each BaseAgent subclass validate its own state via _validate_state(), as the first step inside run(). That means an invalid request still pays for a full agent-service invocation (container already warm or cold-started, full class instantiated) before being rejected. BaseValidationService moves that check in front of the agent entirely: one small, cheap function validates the request and either forwards it or rejects it before the compute that runs BaseWorkflow/BaseAgent is ever triggered.

> **Fail-Fast Placement**
>
> BaseValidationService is designed to run in a separate, smaller compute tier from BaseWorkflow/BaseAgent — typically a Lambda or Cloud Function in front of a Fargate/Cloud Run service. On a validation failure, the caller returns immediately; the downstream agent container is never invoked. This is a cost and latency control, not just a code-organization change — see Section 3 of the companion Cloud Landing Zone HLD/LLD document for the deployment topology this enables.

Class Variables

| **Variable**       | **Type**              | **Default** | **Description**                                                                                                                          |
|--------------------|-----------------------|-------------|------------------------------------------------------------------------------------------------------------------------------------------|
| validator_registry | Dict[str, Callable] | {}          | Populated by the concrete subclass. Maps agent_name -> the bound _validate_<agent_name> method that validates state for that agent. |

Method Contract Table

| **Access**           | **Signature**                                                                                  | **Config / kwargs keys**     | **Purpose**                                                                                                                                                                                                                                                                                                                                                         |
|----------------------|------------------------------------------------------------------------------------------------|------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PUBLIC               | validate(agent_name, state, config=None, correlation_id=None, **kwargs) -> ValidationResult | schema_registry, strict_mode | Template method — the single entry point. Originates correlation_id if not supplied (this is now the earliest point in the request chain, ahead of BaseWorkflow). Resolves the correct validator via _resolve_validator(), runs it, and returns a structured result. Raises nothing — a failed validation is a normal, well-formed return value, not an exception. |
| PROTECTED            | _resolve_validator(agent_name, config=None, **kwargs) -> Callable                          | validator_registry           | Looks up agent_name in validator_registry and returns the bound _validate_<agent_name> method. Same resolution pattern as BaseWorkflow._get_agent() and _get_tool_service() — a name-keyed registry, not a chain of if/elif.                                                                                                                                  |
| ABSTRACT (per agent) | _validate_<agent_name>(state, config=None, **kwargs) -> ValidationResult                | schema, required_fields      | One method per agent, implemented on the concrete subclass. Same signature and shape as the old per-agent _validate_state(), just relocated. Returns ValidationResult(valid, errors) rather than a bare bool, so a failure can carry field-level detail back to the caller.                                                                                        |
| PRIVATE              | __build_result(valid, errors, agent_name, correlation_id, **kwargs) -> ValidationResult   | —                            | Framework-owned. Shapes a consistent {valid, errors, agent_name, correlation_id} object regardless of which _validate_<agent_name> produced it.                                                                                                                                                                                                                 |

Config / Environment Resolution — BaseValidationService

| **config key**  | **Environment variable**        | **Default** | **Used in step**                                                                                         |
|-----------------|---------------------------------|-------------|----------------------------------------------------------------------------------------------------------|
| schema_registry | VALIDATION_SCHEMA_REGISTRY_PATH | {}          | _resolve_validator() / per-agent validators — JSON-schema source per agent_name                         |
| strict_mode     | VALIDATION_STRICT_MODE          | True        | validate() — if False, unknown agent_name falls through to a permissive default instead of a hard reject |

What Changes Downstream

- BaseAgent no longer declares _validate_state — it is removed from the contract entirely (Section 2.3). BaseAgent.run() assumes it is only ever called with state that already passed validate().

- correlation_id now originates in BaseValidationService.validate(), not BaseWorkflow.execute(). BaseWorkflow.execute() still accepts correlation_id as a parameter and still falls back to generating one if called standalone (e.g. in a unit test that skips the validation gate), but in the normal request path it receives one already minted.

- A validation failure is a terminal, cheap response (HTTP 400 with the ValidationResult body) returned from the validation tier. BaseWorkflow, BaseAgent, and everything behind them are never instantiated for that request.

## 2.3 BaseAgent

*agents/ — run() is the single public entry point*

Constructor

| **Parameter**                                                                   | **Type**         | **Default** | **Required** | **Description**                                                                                                                                                               |
|---------------------------------------------------------------------------------|------------------|-------------|--------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| state                                                                           | Dict[str, Any] | —           | Yes          | Agent state object. Must contain at minimum: query (str), intent (str), tenant_id (str).                                                                                      |
| config                                                                          | Dict[str, Any] | None        | No           | Runtime configuration dict. Falls back to environment variables then hardcoded defaults.                                                                                      |
| correlation_id, io_config, idempotency_key, tenant_id, message_log, failed_keys | see 2.1          | None / [] | No           | Propagation fields. Received from BaseWorkflow when run() is called through the normal chain; self-originated as a fallback so BaseAgent remains independently unit-testable. |
| **kwargs                                                                      | Any              | —           | No           | Optional per-call overrides. Highest priority in the resolution chain.                                                                                                        |

Method Contract Table

| **Access** | **Signature**                                                                                                                                               | **Config / kwargs keys**               | **Purpose**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
|------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PUBLIC     | run(state, config=None, correlation_id=None, io_config=None, idempotency_key=None, tenant_id=None, message_log=None, failed_keys=None, **kwargs) -> Dict | any config key; any kwarg              | Template method. Calls all protected methods in fixed sequence. Receives and forwards propagation fields; does not re-generate correlation_id if one is supplied. Assumes state has already passed BaseValidationService.validate() (Section 2.2) — run() no longer validates its own input. Immediately before this step, run() checks config['governance_engine']: if a BaseGovernanceFramework instance is configured, run() calls its enforce_governance_gate() (Section 2.11) instead of _apply_policy(); a DehydrationInterrupt raised from that call is re-raised unchanged, for BaseWorkflow to catch. After the LLM call, run() also tracks this call's token usage via the private __track_usage() helper below (feeding state['cumulative_tokens_used'], which the governance gate's token-budget check reads on the next call) and, once a response is set, measures total elapsed latency against max_latency, logging a single agent_run_complete entry to message_log either way and raising NFRViolationError only on a breach. |
| PROTECTED  | _build_messages(state, config, **common, **kwargs) -> list                                                                                            | prompt_template, role                  | Enforce governance: retries, circuit breakers, approval gates, redaction. Used only as the fallback when no governance_engine is configured — the preferred, centrally-enforced path is BaseGovernanceFramework.enforce_governance_gate() (Section 2.11). This is also where a tool call is triggered via _resolve_tool_call() (Section 2.8) when this fallback path runs.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| PROTECTED  | _apply_policy(state, config, **common, **kwargs) -> None                                                                                              | retry_policy, circuit_breaker_config   | Enforce governance: retries, circuit breakers, approval gates, redaction. This is also where a tool call is triggered via _resolve_tool_call() (Section 2.8).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| PRIVATE    | __llm_call(messages, config, **kwargs) -> dict                                                                                                         | model_id, max_tokens, temperature      | Invoke LLM provider. Returns {text, metadata}. Provider-agnostic, routed internally.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| PRIVATE    | __track_usage(messages, response, config=None, **kwargs) -> int                                                                                        | —                                      | Base-owned, never overridden. Prefers an actual token count from the LLM provider's response metadata (response['metadata']['tokens_used']); falls back to a ~4-chars/token estimate over the outbound messages when a provider doesn't report usage. run() adds the returned value to state['cumulative_tokens_used'] and to the agent_run_complete log entry's tokens_used field — the same field BaseObservability.__extract_metrics() (Section 2.10) already sums into total_tokens.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| PROTECTED  | _evaluate_output(response, state, config, **common, **kwargs) -> float                                                                                | min_confidence, groundedness_threshold | Evaluate output quality, return [0.0–1.0].                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| PROTECTED  | _fallback(state, config, **common, **kwargs) -> dict                                                                                                  | fallback_model, cache_key              | Fallback strategy when confidence is below threshold.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| PRIVATE    | (inline in run(), not a separate method) — latency SLO check                                                                                                | max_latency                            | Base-owned, not overridden — there is no separate hook because there is nothing for a subclass to customize: run() measures its own wall-clock time from entry to the point state['response'] is set, compares it to max_latency (kwargs > config > MAX_LATENCY env > 2.0s default), and raises NFRViolationError on breach. The measurement always happens and is always logged, whether or not it breaches — see the run() row above.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| PROTECTED  | _handle_error(error, state, config, correlation_id, message_log, failed_keys, **kwargs) -> None                                                         | alert_channel, retry_policy            | Error handling. Appends the failing key to failed_keys instead of raising past the request boundary.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |

> **NFRViolationError — a Measured Breach, Not a Control-Flow Signal**
>
> class NFRViolationError(Exception): raised whenever a measured non-functional requirement is breached — a latency SLO in BaseAgent.run(), BaseRAGPipeline.retrieve(), or BaseToolService.execute() (Section 2.6), or the token budget in BaseGovernanceFramework.__enforce_token_budget() (Section 2.11). Unlike DehydrationInterrupt above, it is not caught separately — it falls through the normal except Exception path in whichever method raised it, so a breach is a regular failure: added to failed_keys, handed to _handle_error(), and routed to the DLQ like any other exception.

Config / Environment Resolution — BaseAgent

| **config key** | **Environment variable** | **Default** | **Used in step**                                                                   |
|----------------|--------------------------|-------------|------------------------------------------------------------------------------------|
| min_confidence | MIN_CONFIDENCE           | 0.85        | run() — fallback threshold                                                         |
| model_id       | LLM_MODEL_ID             | 'default'   | __llm_call() — provider routing                                                  |
| max_latency    | MAX_LATENCY              | 2.0         | run() — latency SLO check, measured end-to-end, raises NFRViolationError on breach |

## 2.4 BaseWorkflow

*orchestration/ — execute() is the single public entry point; origin point for io_config, idempotency_key, message_log, and failed_keys. correlation_id is now typically received from BaseValidationService (Section 2.2) rather than originated here.*

Method Contract Table

| **Access** | **Signature**                                                          | **Config / kwargs keys**                       | **Purpose**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
|------------|------------------------------------------------------------------------|------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PUBLIC     | execute(state, config=None, correlation_id=None, **kwargs) -> Dict  | workflow_id, routing_strategy                  | Public entry point. Validates tenant_id, accepts correlation_id from the caller (BaseValidationService, normally) or originates one as a standalone fallback, resolves io_config/idempotency_key, builds and validates the workflow graph, resolves and invokes the target agent, dispatches message_log to the configured observability engine, and dispatches failed_keys to the DLQ. A DehydrationInterrupt raised from within the agent chain (Section 2.11) is caught separately: execute() logs a workflow_dehydrated event and sets state['status'] = 'AWAITING_WEBHOOK' rather than treating the pause as a failure. |
| PROTECTED  | _build_workflow(config, **common, **kwargs) -> Any               | workflow_definition                            | Construct the LangGraph graph (or equivalent) of agent nodes.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| PROTECTED  | _validate_workflow(workflow, config, **common, **kwargs) -> bool | required_nodes                                 | Validate the graph is well-formed before execution.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| PROTECTED  | _get_agent(intent, config, **common, **kwargs) -> BaseAgent      | agent_registry                                 | Resolve which BaseAgent subclass handles a given intent.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| PROTECTED  | _resolve_io_config(config, **kwargs) -> dict                       | io_config namespace                            | Extract the IO_CONFIG-prefixed subset of config, separate from behavioral NFR config.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| PROTECTED  | _generate_idempotency_key(state, config, **kwargs) -> str          | idempotency_strategy, business_key_fields      | Abstract — subclass supplies domain logic, e.g. hash(tenant_id + REF_ELEM_KEY). Called only when no valid client-supplied key exists.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| PRIVATE    | __resolve_idempotency(state, config, **kwargs) -> str             | idempotency_ttl_seconds, idempotency_store_url | Base-owned. Checks for a client-supplied key, validates and looks it up in the idempotency store; returns the existing key if a matching non-expired entry exists (replay), otherwise mints and persists a new one via _generate_idempotency_key().                                                                                                                                                                                                                                                                                                                                                                           |
| PRIVATE    | (retired — see BaseObservability.record_telemetry(), Section 2.10)     | —                                              | The private __flush_logs() helper from the prior revision is retired. BaseWorkflow.execute()'s finally block now reads config['observability_engine'] directly: if a BaseObservability instance is configured it calls record_telemetry(message_log, correlation_id, config, tenant_id=tenant_id); otherwise it falls back to logging each message_log entry individually.                                                                                                                                                                                                                                                 |
| PRIVATE    | _send_to_dlq(failed_keys, config)                                     | dlq_queue_url                                  | Base-owned. Dispatches failed_keys to SQS/Pub-Sub/Service Bus at end of request.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |

## 2.5 BaseRAGPipeline

*rag/ — retrieve() is the single public entry point*

Method Contract Table

| **Access** | **Signature**                                                                     | **Config / kwargs keys**            | **Purpose**                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
|------------|-----------------------------------------------------------------------------------|-------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PUBLIC     | retrieve(query, config=None, tenant_id=None, **kwargs) -> list                 | top_k, embedding_model, max_latency | Template method. Embeds query, searches the tenant-scoped index, evaluates groundedness, returns ranked context. Measures wall-clock latency across the embed → search → rerank sequence, logs a rag_retrieve_complete entry to message_log (when the caller passes one), and raises NFRViolationError if latency exceeds max_latency (kwargs > config > MAX_LATENCY env > 2.0s default) — same pattern as BaseAgent.run() (Section 2.3) and BaseToolService.execute() (Section 2.6). |
| PROTECTED  | _search_index(query_vector, config, tenant_id=None, **kwargs) -> list[dict] | index_name, tenant_index_prefix     | Namespaces the vector search to f'{tenant_index_prefix}_{tenant_id}' or an equivalent metadata filter — prevents one tenant's retrieve() from ever surfacing another tenant's documents.                                                                                                                                                                                                                                                                                                |
| PROTECTED  | _rerank(results, config, **kwargs) -> list                                    | rerank_top_n                        | Re-rank candidate documents before returning.                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| PROTECTED  | _evaluate_answer(answer, config, **kwargs) -> float                           | groundedness_threshold              | Evaluate answer quality (RAGAS faithfulness gate).                                                                                                                                                                                                                                                                                                                                                                                                                                       |

Config / Environment Resolution — BaseRAGPipeline

| **config key**         | **Environment variable** | **Default**                  | **Used in step**                                                                            |
|------------------------|--------------------------|------------------------------|---------------------------------------------------------------------------------------------|
| faithfulness_threshold | FAITHFULNESS_THRESHOLD   | 0.85                         | _evaluate_answer() — RAGAS gate                                                            |
| rag_top_k              | RAG_TOP_K                | 5                            | retrieve() — result count                                                                   |
| embedding_model        | EMBEDDING_MODEL          | 'amazon.titan-embed-text-v1' | retrieve() — embedding provider                                                             |
| max_latency            | MAX_LATENCY              | 2.0                          | retrieve() — latency SLO across embed → search → rerank, raises NFRViolationError on breach |

## 2.6 BaseToolService

*tools/ — execute() is the single public entry point (async). Client-side tool caller: REST or MCP transport via RestToolService / MCPToolService.*

Method Contract Table

| **Access** | **Signature**                                                            | **Config / kwargs keys**                   | **Purpose**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
|------------|--------------------------------------------------------------------------|--------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PUBLIC     | async execute(payload, config=None, tenant_id=None, **kwargs) -> dict | timeout, retries, auth_token, max_latency  | Template method. Validates → resolves endpoint → builds payload → checks the commit gate → HTTP POST. Returns result dict. Measures wall-clock latency across the full call (validation through __http_post's return), logs a tool_execute_complete entry to message_log (when the caller passes one), and raises NFRViolationError if latency exceeds max_latency (kwargs > config > MAX_LATENCY env > 5.0s default here, a looser bound than BaseAgent/BaseRAGPipeline's 2.0s since this measures a network round trip). |
| PROTECTED  | get_endpoint(config, tenant_id=None, **kwargs) -> str                 | base_url, service_name, tenant_routing_map | Return the endpoint URL for this tool. Optionally resolves a tenant-specific base URL via config['tenant_routing_map'][tenant_id], falling back to the shared base_url.                                                                                                                                                                                                                                                                                                                                                     |
| PROTECTED  | _build_payload(payload, config, **kwargs) -> dict                    | schema, enrichments                        | Transform input dict to the tool's request schema.                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| PROTECTED  | _validate_input(payload, config, **kwargs) -> bool                   | schema, required_fields                    | Domain-specific validation. Return False to raise ValueError in execute().                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| PRIVATE    | async __http_post(endpoint, body, config, **kwargs) -> dict         | timeout, retries, auth_token               | Perform HTTP POST. Handles retries and timeout internally. Never overridden.                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| PRIVATE    | _commit_gate(key, failed_keys, config, **kwargs) -> bool             | commit_gate_enabled                        | Base-owned. Called immediately before any save/commit/write. Returns False if key is already present in failed_keys — the item is excluded from commit and left for DLQ investigation while sibling items proceed.                                                                                                                                                                                                                                                                                                              |

### 2.6.1 RestToolService and MCPToolService — Transport Implementations

MCP's Streamable HTTP transport is JSON-RPC framed over HTTP POST. An MCP-backed tool call therefore fits the existing BaseToolService.execute() template — validate → get_endpoint → build_payload → __http_post — without touching the private, framework-owned __http_post method. Only get_endpoint() and _build_payload() differ from a REST tool.

| **Class**       | **Access** | **Signature**                                         | **Purpose**                                                                                                               |
|-----------------|------------|-------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------|
| RestToolService | PROTECTED  | get_endpoint(config, **kwargs) -> str              | Existing pattern, unchanged: return config['tool_base_url'].                                                            |
| RestToolService | PROTECTED  | _build_payload(payload, config, **kwargs) -> dict | Existing pattern, unchanged: schema-mapped passthrough.                                                                   |
| MCPToolService  | PUBLIC     | execute(...) -> dict                                 | Inherited unchanged from BaseToolService. No override.                                                                    |
| MCPToolService  | PROTECTED  | get_endpoint(config, **kwargs) -> str              | Return the MCP server's Streamable HTTP URL from config['mcp_server_url'].                                              |
| MCPToolService  | PROTECTED  | _build_payload(payload, config, **kwargs) -> dict | Wrap payload in the MCP JSON-RPC envelope: {"jsonrpc":"2.0","method":"tools/call","params":{"name":...,"arguments":...}}. |

Config / Environment Resolution — BaseToolService

| **config key**      | **Environment variable** | **Default**        | **Used in step**                                                                 |
|---------------------|--------------------------|--------------------|----------------------------------------------------------------------------------|
| timeout             | TOOL_TIMEOUT             | 5.0                | __http_post() — session timeout                                                |
| retries             | TOOL_RETRIES             | 3                  | execute() — retry loop count                                                     |
| tool_base_url       | TOOL_BASE_URL            | 'http://localhost' | RestToolService.get_endpoint()                                                   |
| mcp_server_url      | MCP_SERVER_URL           | None               | MCPToolService.get_endpoint()                                                    |
| auth_token          | TOOL_AUTH_TOKEN          | None               | __http_post() — Authorization header                                           |
| tenant_routing_map  | TENANT_ROUTING_MAP       | {}                 | get_endpoint() — per-tenant endpoint overrides                                   |
| commit_gate_enabled | COMMIT_GATE_ENABLED      | True               | _commit_gate() — set False only for single-item, non-batch flows                |
| max_latency         | MAX_LATENCY              | 5.0                | execute() — latency SLO across the full call, raises NFRViolationError on breach |

## 2.7 BaseMCPServer

*mcp/ — serve() is the single public entry point. Server-side counterpart to BaseToolService: turns wrapped APIs into a spec-compliant MCP server.*

BaseToolService (2.5) is a client — it calls an MCP server that already exists somewhere. BaseMCPServer is the other half: it lets a repo expose its own existing tool implementations (typically BaseToolService subclasses) as an MCP server, so external agents can discover them via tools/list and invoke them via tools/call, without hand-writing MCP protocol code per tool.

Class Variables

| **Variable**     | **Type**          | **Default**      | **Description**                                                                                                     |
|------------------|-------------------|------------------|---------------------------------------------------------------------------------------------------------------------|
| server_name      | str               | 'e2a-mcp-server' | Governance key identifying this server instance in logs and the tool registry.                                      |
| registered_tools | Dict[str, dict] | {}               | Populated by wrap_api_as_tool(). Maps tool_name -> {handler: BaseToolService instance, description, input_schema}. |

Method Contract Table

| **Access** | **Signature**                                                                                     | **Config / kwargs keys** | **Purpose**                                                                                                                                                                                                                                                                                               |
|------------|---------------------------------------------------------------------------------------------------|--------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PUBLIC     | serve(request, config=None, **kwargs) -> dict                                                  | mcp_protocol_version     | Template method — the single entry point, mirroring run()/execute()/retrieve(). Delegates to the framework-owned __dispatch_jsonrpc() and returns a JSON-RPC-shaped response.                                                                                                                           |
| PROTECTED  | _list_tools(config=None, **kwargs) -> list[dict]                                            | tool_filter              | Handles the tools/list method. Default implementation derives {name, description, inputSchema} for every entry in registered_tools. Override only to filter or augment what is advertised (e.g. per-tenant tool visibility).                                                                              |
| PROTECTED  | _call_tool(tool_name, arguments, config=None, tenant_id=None, **kwargs) -> dict               | —                        | Handles the tools/call method. Default implementation resolves tool_name in registered_tools and awaits the wrapped handler's execute(arguments, config, tenant_id=tenant_id). Returns an MCP-shaped {content, isError} result.                                                                           |
| PROTECTED  | wrap_api_as_tool(handler, tool_name, description, input_schema, config=None, **kwargs) -> None | —                        | Factory method — 'create an MCP server by wrapping an API'. Registers an existing BaseToolService instance (REST or otherwise) under tool_name with an MCP tool definition generated from input_schema. No new transport code is written; the existing tool's execute() is reused as-is.                  |
| PRIVATE    | __dispatch_jsonrpc(request, config, **kwargs) -> dict                                        | —                        | Framework-owned. Parses the JSON-RPC envelope, routes request['method'] ('tools/list' -> _list_tools, 'tools/call' -> _call_tool), and wraps the result or error in the correct JSON-RPC response shape. Never overridden — same pattern as __http_post being framework-owned in BaseToolService. |

Config / Environment Resolution — BaseMCPServer

| **config key**       | **Environment variable** | **Default**      | **Used in step**                                       |
|----------------------|--------------------------|------------------|--------------------------------------------------------|
| mcp_protocol_version | MCP_PROTOCOL_VERSION     | '2025-06-18'     | serve() — advertised in tools/list responses           |
| mcp_server_name      | MCP_SERVER_NAME          | 'e2a-mcp-server' | serve() — identifies this server in logs               |
| tool_filter          | —                        | None             | _list_tools() — optional per-tenant visibility filter |

## 2.8 Tool Call Routing — MCP-First With REST Fallback

*workflow/ or a ToolRouter helper — lives beside _get_agent() on BaseWorkflow, called from the orchestration layer immediately before a tool is invoked, never from inside BaseAgent.*

This is the single function that answers: for this tool call, does a wrapped MCP server exist? If yes, call it as an MCP tool call through MCPToolService. If no, execute the normal (REST) tool call through RestToolService — the existing behavior is preserved unchanged as the fallback path.

| **Access** | **Signature**                                                                             | **Config / kwargs keys**                              | **Purpose**                                                                                                                                                                                                                                     |
|------------|-------------------------------------------------------------------------------------------|-------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PROTECTED  | _resolve_tool_call(tool_name, payload, config=None, tenant_id=None, **kwargs) -> dict | mcp_server_registry, tool_registry, default_transport | Checks config['mcp_server_registry'] for tool_name. If an entry exists, executes via MCPToolService against that server's endpoint. Otherwise falls back to _get_tool_service()'s existing REST/MCP-client resolution and executes normally. |
| PROTECTED  | _get_tool_service(tool_name, config=None, **kwargs) -> BaseToolService                | tool_registry, default_transport                      | Secondary resolver, used only inside the fallback path. Resolves which BaseToolService subclass (Rest or MCP client) to instantiate for a tool that is not backed by a locally wrapped MCP server.                                              |

Design Rule

Tool-transport selection is resolved outside BaseAgent, using the same resolution pattern BaseWorkflow already uses for agent routing (_get_agent) and BaseAgent already uses for LLM provider routing (__llm_call). BaseAgent's contract does not change: it still only reasons and returns state, calling _resolve_tool_call() from inside _apply_policy() rather than doing tool-execution work itself. This preserves the retry, timeout, auth, and tracing guarantees that BaseToolService.execute() centrally enforces, whichever path is taken.

Config / Environment Resolution — Tool Routing

| **config key**      | **Environment variable** | **Default** | **Used in step**                                                                                                |
|---------------------|--------------------------|-------------|-----------------------------------------------------------------------------------------------------------------|
| mcp_server_registry | MCP_SERVER_REGISTRY_PATH | {}          | _resolve_tool_call() — maps tool_name to a wrapped MCP server's {mcp_server_url, mcp_tool_name}, checked first |
| tool_registry       | TOOL_REGISTRY_PATH       | {}          | _get_tool_service() — maps tool_name to {transport, endpoint} for the fallback path                            |
| default_transport   | DEFAULT_TOOL_TRANSPORT   | 'http'      | _get_tool_service() — fallback when tool_name is in neither registry                                           |

What Does Not Change

- BaseAgent — no new methods, no tool-execution code. It still only produces state via run(); it calls _resolve_tool_call() from _apply_policy(), the same place it always called tools from.

- BaseToolService.execute() and __http_post() — both inherited unchanged by MCPToolService, whether the endpoint is an external MCP server or one hosted locally via BaseMCPServer.

- Governance — retries, timeout, and auth_token injection in __http_post() apply identically whether the downstream call is a legacy REST tool or an MCP server, local or external.

## 2.9 Foundation Classes

*Interface pattern — BasePromptRegistry*

> **Interface Pattern — No Shared Implementation**
>
> Foundation classes are Interfaces (Python Protocol or pure ABC with no __init__ state). They carry no shared implementations. The abstract class enforces the contract; the project-specific subclass owns the complete implementation. Every method accepts config=None and **kwargs so the same signature pattern is consistent across all E2A classes. BaseInfraProvisioner, BasePipeline, BaseObservability, and BaseGovernanceFramework are no longer listed here — each now has a public template method and at least one framework-provided step, which makes each a primary class like BaseAgent or BaseWorkflow, not a bare interface. BaseObservability and BaseGovernanceFramework moved first, in Sections 2.10 and 2.11, since request-path classes (BaseAgent, BaseWorkflow) call into them directly; BasePipeline and BaseInfraProvisioner follow in Sections 2.12 and 2.13.

| **Access** | **Class / Method**                                                                    | **Config / kwargs keys**          | **Purpose**                                                                                                                                          |
|------------|---------------------------------------------------------------------------------------|-----------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------|
| INTERFACE  | BasePromptRegistry                                                                    |                                   | New in this revision — see rationale below.                                                                                                          |
| INTERFACE  | get_prompt(prompt_id, version, tenant_id, config=None, **kwargs) -> PromptTemplate | prompt_store_url, default_version | Fetch a version-controlled, tenant-aware prompt template from an external, GitOps-managed store instead of a hardcoded string in _build_messages(). |

> **Why BasePromptRegistry Is the One New Interface**
>
> Every other reference pattern reviewed for this revision fit an existing hook — _build_messages(), _rerank(), _search_index(), _get_agent(), _evaluate_output(), _commit_gate. Prompt Registry is the one exception: nothing in the prior contract represented 'an external, versioned source of prompt templates,' so BaseAgent._build_messages() had no composable collaborator to call. BaseObservability and BaseGovernanceFramework used to sit in this same thin-interface bucket, but both have since been promoted to primary classes (Sections 2.10-2.11) once each gained a real template method — record_telemetry() and enforce_governance_gate() respectively. Prompt Registry is the one that stays here, because it never had, and still doesn't have, one.

## 2.10 BaseObservability — Enterprise Telemetry Engine

*telemetry/ — record_telemetry() is the single public entry point. Promoted from a thin Foundation interface (which had only _collect_metrics, _collect_traces, and _collect_logs) to a primary class for the same reason as BasePipeline and BaseInfraProvisioner: it now has a real template method.*

record_telemetry() replaces the private __flush_logs() helper described in Section 2.1: BaseWorkflow.execute() no longer writes message_log to stdout directly in its finally block. Instead it reads config['observability_engine']; if a BaseObservability instance is configured, execute() calls record_telemetry(message_log, correlation_id, config, tenant_id=tenant_id) and lets this class own dispatch. If no observability_engine is configured, BaseWorkflow falls back to logging each message_log entry individually, so the framework degrades gracefully rather than failing when observability isn't wired up yet.

| **Access** | **Signature**                                                                   | **Config / kwargs keys** | **Purpose**                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
|------------|---------------------------------------------------------------------------------|--------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PUBLIC     | record_telemetry(message_log, correlation_id, config=None, **kwargs) -> None | tenant_id                | Template method. Enriches every message_log entry with correlation_id, tenant_id, a host_epoch_ms timestamp, and the running framework_version, then derives request-level metrics and calls the hooks below in sequence: _ship_logs(), _emit_metrics() (only if metrics were extracted), _export_traces(). The whole dispatch is wrapped in a try/except — a telemetry backend outage falls back to logging.info() per entry rather than losing the request's logs. |
| PROTECTED  | _ship_logs(enriched_logs, config, **kwargs) -> None                         | log_sink                 | Ship the enriched log entries to the configured sink (CloudWatch Logs / Datadog / Cloud Logging).                                                                                                                                                                                                                                                                                                                                                                       |
| PROTECTED  | _emit_metrics(metrics, config, **kwargs) -> None                            | namespace, dimensions    | Emit the metrics record_telemetry() derived (total_tokens, error_count) to a metrics backend.                                                                                                                                                                                                                                                                                                                                                                           |
| PROTECTED  | _export_traces(correlation_id, logs, config, **kwargs) -> None              | trace_id, service_name   | Export distributed traces for this request, keyed by correlation_id.                                                                                                                                                                                                                                                                                                                                                                                                    |
| PRIVATE    | __extract_metrics(enriched_logs) -> Dict[str, float]                       | —                        | Base-owned, never overridden. Single pass over enriched_logs: counts entries at level == 'ERROR' into error_count, and sums any tokens_used field present on an entry into total_tokens.                                                                                                                                                                                                                                                                                |

## 2.11 BaseGovernanceFramework — AI Safety, Economics & Lifecycle Governance

*governance/ — enforce_governance_gate() is the single public entry point. Promoted from a thin Foundation interface (which had only _apply_policy and _circuit_breaker) to a primary class for the same reason as BaseObservability: it now has a real template method, invoked from inside BaseAgent.run() rather than delegated to from a subclass's own _apply_policy() override.*

BaseAgent.run() branches on config['governance_engine'] immediately before what used to be an unconditional call to _apply_policy(): if a BaseGovernanceFramework instance is configured, run() calls gov_engine.enforce_governance_gate(state, io_config, config, **common, **kwargs) and uses its returned state; otherwise it falls back to the agent subclass's own _apply_policy(), unchanged (Section 2.3). This replaces the composition pattern from the prior revision of this table — a concrete agent's _apply_policy() delegating internally to an injected governance instance — with an explicit either/or branch in the base class itself, so the governance path is enforced by the framework rather than by convention in each agent.

A new DehydrationInterrupt signal exception, raised only by the base-owned __trigger_dehydration() helper below, is how this class pauses a request pending human approval without holding Tier 2 compute open. BaseAgent.run() re-raises it unchanged rather than converting it into a failed_keys entry — a dehydrated request is not a failure. BaseWorkflow.execute() catches it separately from other exceptions, logs a workflow_dehydrated event, and sets state['status'] = 'AWAITING_WEBHOOK', resuming later through the same asynchronous webhook path described in Landing Zone Section 10.1 rather than a held-open connection.

> **DehydrationInterrupt — a Signal Exception, Not an Error**
>
> class DehydrationInterrupt(Exception): a lightweight signal exception used only to communicate between BaseGovernanceFramework and BaseWorkflow. It is raised exclusively by __trigger_dehydration() and caught exclusively by BaseWorkflow.execute(); it must never reach _handle_error() or the DLQ, since a paused-for-approval request has not failed.

| **Access** | **Signature**                                                                     | **Config / kwargs keys**         | **Purpose**                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
|------------|-----------------------------------------------------------------------------------|----------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PUBLIC     | enforce_governance_gate(state, io_config, config=None, **kwargs) -> dict       | —                                | Template method, called from BaseAgent.run() in place of _apply_policy() whenever a governance_engine is configured. Runs, in fixed order: (1) the base-owned token budget check; (2) if state['requires_human_approval'] is set and not yet approved, the base-owned dehydration trigger; (3) if state['pending_mcp_tool_execution'] is set, the abstract sandbox-verification hook; (4) the abstract semantic firewall hook, whose returned state is this method's return value. |
| PRIVATE    | __enforce_token_budget(state, io_config) -> None                               | max_token_budget (io_config key) | Base-owned. Compares state['cumulative_tokens_used'] against io_config['max_token_budget'] (default unlimited) and raises a Governance Violation ValueError once the budget is exhausted — stops the request before another LLM call is made, not after.                                                                                                                                                                                                                            |
| PRIVATE    | __trigger_dehydration(state, config, **kwargs) -> None                       | —                                | Base-owned. Calls the abstract _dehydrate_state_to_perimeter() hook to persist state to the Data & Messaging Perimeter, then raises DehydrationInterrupt so BaseWorkflow can halt the request and scale Tier 2 compute to zero while awaiting the human-approval webhook, instead of holding a paid compute tier open on a blocking wait.                                                                                                                                              |
| PROTECTED  | _execute_semantic_firewall(state, config, **kwargs) -> dict                   | policy_file, redaction_rules     | Run local guard models for PII redaction and prompt-injection blocking; returns the (possibly redacted) state. Same Tier 2 placement as the Semantic Firewall reference in Section 2.15.                                                                                                                                                                                                                                                                                                |
| PROTECTED  | _verify_sandbox_profile(config, **kwargs) -> None                             | —                                | Assert the current execution context is an isolated microVM (gVisor/Firecracker) before a pending MCP tool call is allowed to proceed.                                                                                                                                                                                                                                                                                                                                                  |
| PROTECTED  | _circuit_breaker(failures, config, **kwargs) -> bool                          | fail_max, reset_timeout          | Circuit breaker state transitions + metric emission — unchanged contract from the prior revision of this table.                                                                                                                                                                                                                                                                                                                                                                         |
| PROTECTED  | _dehydrate_state_to_perimeter(state, correlation_id, tenant_id, config) -> None | —                                | Persist state to the Data & Messaging Perimeter ahead of a human-in-the-loop pause; called only by the base-owned __trigger_dehydration().                                                                                                                                                                                                                                                                                                                                            |

## 2.12 BasePipeline — CI/CD Lifecycle Engine

*cicd/ — run_pipeline() is the single public entry point. Promoted from a thin Foundation interface (which had only _run_tests and _run_rag_eval) to a primary class for the same reason as BaseInfraProvisioner: it now has a real template method.*

This class is the code-level counterpart to Landing Zone Section 9 (CI/CD & Deployment Topology). Where that document narrates build → test → gate → deploy in prose, BasePipeline.run_pipeline() is the template method that runs it, in a fixed seven-stage order, fail-fast on the cheapest checks first.

Constructor

| **Attribute**            | **Type**    | **Description**                                                                                                                                                                                                                                                                                                                                                                                                                                        |
|--------------------------|-------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| required_artifact_matrix | List[str] | The container images this pipeline must build and push — one per E2A class family that deploys as its own service: ingress-validator (Tier 1), orchestration-engine (Tier 2), rag-retrieval-worker / rest-tool-worker / mcp-server-worker (Tier 4). Tier 0 (API Gateway) is a managed service, not a custom image; Tier 5 (Saga) is an IaC-defined state machine via BaseInfraProvisioner, not a container — both are correctly absent from this list. |

Method Contract Table

| **Access**           | **Signature**                                                                                           | **Purpose**                                                                                                                                                                                                                                                                                                                                                                                                         |
|----------------------|---------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PUBLIC               | run_pipeline(config=None, correlation_id=None, message_log=None, failed_keys=None, **kwargs) -> dict | Template method. Runs seven stages in fixed order; any stage failing raises internally and is caught, appending correlation_id to failed_keys and returning status='FAILED' rather than propagating the exception.                                                                                                                                                                                                  |
| PROTECTED (abstract) | _run_static_security_scans(config, **kwargs) -> bool                                                | SAST linting and secret-detection scans (e.g. gitleaks) for exposed LLM API tokens/keys. Runs first — cheapest, fastest check, so a bad commit fails before any compute-heavy stage runs.                                                                                                                                                                                                                           |
| PROTECTED (abstract) | _execute_test_suite(config, **kwargs) -> bool                                                       | Runs the tests/ directory — unit and integration tests against the Playbook's scaffold contracts. Supersedes the old bare _run_tests() stub with a richer, execution-oriented name.                                                                                                                                                                                                                                |
| PROTECTED (abstract) | _verify_scaffold_contracts(config, **kwargs) -> bool                                                | Statically parses source classes and asserts the Single Public Entry Point Rule (Section 1) still holds: exactly one public method per core class (run/execute/retrieve/serve/validate/provision_landing_zone/run_pipeline), and no framework-owned private method (the double-underscore ones, e.g. __http_post) has been overridden in a subclass. This turns a documented convention into an enforced CI gate. |
| PROTECTED (abstract) | _run_rag_eval(config, **kwargs) -> float                                                            | Same method name as the retired Foundation-interface stub — RAGAS faithfulness/groundedness score against a golden dataset. Canonical reference implementation: Needle-In-A-Haystack recall testing (Section 2.15). Runs before artifact compilation deliberately: no point building and pushing five container images if the eval already failed.                                                                  |
| PROTECTED (abstract) | _compile_artifact(artifact_name, config, **kwargs) -> bool                                          | Builds one OCI/Docker image from required_artifact_matrix and pushes it to the versioned artifact registry (Landing Zone Section 9.1).                                                                                                                                                                                                                                                                              |
| PROTECTED (abstract) | _run_dynamic_security_checks(config, **kwargs) -> bool                                              | DAST scans against a running instance in an ephemeral staging sandbox — the same sandbox infrastructure the NIAH validation pattern uses (Section 2.15), since both need a live, disposable deployment rather than static source.                                                                                                                                                                                   |
| PROTECTED (abstract) | _execute_deployment_rollout(strategy, config, **kwargs) -> dict                                     | Canary weight-shifting or Blue-Green environment cutover. Deployment-level Blue-Green — swapping whole environments/infra — is a different layer from Section 2.15's Shadow Mode pattern, which mirrors live request traffic at the BaseWorkflow.execute() level. Same vocabulary, two different altitudes in the stack; don't conflate them.                                                                       |

Config / Environment Resolution — BasePipeline

| **config key**         | **Environment variable** | **Default**  | **Used in step**                                                         |
|------------------------|--------------------------|--------------|--------------------------------------------------------------------------|
| faithfulness_threshold | FAITHFULNESS_THRESHOLD   | 0.85         | run_pipeline() — RAGAS gate, shared with BaseRAGPipeline's own threshold |
| deployment_strategy    | DEPLOYMENT_STRATEGY      | 'BLUE_GREEN' | _execute_deployment_rollout() — 'BLUE_GREEN' or 'CANARY'                |

## 2.13 BaseInfraProvisioner — Landing Zone Provisioning

*infra/ — provision_landing_zone() is the single public entry point. Promoted from a Foundation interface to a primary class: it now has a real template method and one framework-provided step, the same shape as every other core E2A class.*

This class is the code-level counterpart to the entire Cloud Landing Zone HLD/LLD document — where that document narrates the three zones, six tiers, and Saga engine in prose, BaseInfraProvisioner.provision_landing_zone() is the template method that actually stands them up. This is Structural Isomorphism applied one level deeper than the rest of the framework: not just 'class boundaries map to network boundaries,' but 'the act of provisioning those network boundaries is itself an E2A class following E2A's own rules.'

Method Contract Table

| **Access**                                | **Signature**                                                                                           | **Config / kwargs keys**       | **Purpose**                                                                                                                                                                                                                                                                                                                                                                                                                                       |
|-------------------------------------------|---------------------------------------------------------------------------------------------------------|--------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PUBLIC                                    | provision_landing_zone(tenant_id, tenancy_model, config=None, correlation_id=None, **kwargs) -> dict | tenancy_model ('POOL'|'SILO') | Template method. Runs all seven provisioning steps in fixed order, returns a manifest with topology_maps and status ('PROVISIONED' | 'FAILED'). Mints its own correlation_id if the caller doesn't supply one — this is a control-plane operation, invoked from CI/CD, not a per-request data-plane call, so it does not carry the full six-field runtime propagation contract from Section 2.1.                                                 |
| PROTECTED (abstract)                      | _define_network_topologies(tenant_id, config, **kwargs) -> dict                                     | cidr_block, region             | Provisions the Public VPC/DMZ, Private VPC application space, and the Data & Messaging Perimeter's service boundary (VPC Service Controls / PrivateLink / Private Link).                                                                                                                                                                                                                                                                          |
| PROTECTED (abstract)                      | _define_edge_security(networks, config, **kwargs) -> dict                                           | waf_policy, rate_limit         | Deploys Tier 0: API Gateway plus WAF/Cloud Armor rules — including prompt-injection filter rules, which is where the Semantic Firewall pattern's edge-layer half lives (Section 2.15).                                                                                                                                                                                                                                                            |
| PROTECTED (abstract)                      | _define_data_perimeter_substrate(networks, tenant_id, tenancy_model, config, **kwargs) -> dict      | pool_or_silo_routing           | Provisions Tier 3: Intake Topic (push), Task Queue (pull, with a poison-pill redrive policy at max_receives=3), and the State/Outbox table, routed per tenancy_model per Section 10.2.                                                                                                                                                                                                                                                            |
| PROTECTED (abstract)                      | _define_compute_tiers(networks, data_meta, config, **kwargs) -> dict                                | instance_type, desired_count   | Provisions Tier 1 (validation function), Tier 2 (BaseWorkflow + BaseAgent, co-located), and Tier 4 (decoupled workers, queue-depth autoscaled).                                                                                                                                                                                                                                                                                                   |
| PROTECTED (abstract)                      | _define_iam_governance_framework(compute, data_meta, config, **kwargs) -> dict                      | role_bindings                  | Generates per-tier IAM roles/service accounts: Tier 1 gets publish-only access to the Intake Topic; Tier 2/4 get broader intra-perimeter read/write; Tier 2's database credential toward read-replica connections is read-only where the Read-Only Data pattern applies (Section 2.15). One shared service account for every tier is an under-specification of this hook, not a valid implementation of it.                                       |
| PROTECTED (concrete, override per vendor) | _generate_saga_orchestrator(compute_ctx, config, **kwargs) -> dict                                  | —                              | Returns the Tier 5 state-machine definition. Ships with an AWS Step Functions (ASL) default. This is NOT framework-locked the way __http_post or _commit_gate are — a GCP or Azure subclass MUST override it, because Step Functions ASL JSON is not valid GCP Workflows YAML or Azure Logic Apps JSON. The logical shape (Choice on failed_keys → Parallel with three tracks → webhook dispatch) is what's vendor-neutral; the syntax is not. |
| PROTECTED (abstract)                      | _apply_infrastructure_graph(final_manifest, config, **kwargs) -> bool                               | dry_run, auto_approve          | Executes the actual apply — Terraform, Pulumi, CDK, or a raw cloud SDK client. The only step that touches real infrastructure; every step before it only builds the manifest in memory.                                                                                                                                                                                                                                                           |

Correcting the Reference Implementation This Was Built From

- _generate_saga_orchestrator was originally proposed as a single framework-owned method returning hardcoded ASL JSON, used unchanged by every vendor subclass including a GCP one. That's a real bug, not a simplification: GCP Workflows and Azure Logic Apps do not read ASL. The corrected contract makes this method vendor-overridable with an AWS-flavored default, and the scaffold in Section 3 includes a corrected GCPInfraProvisioner that overrides it with GCP Workflows YAML.

- _define_iam_governance_framework's reference implementation returned one generic service account for the whole tenant. That collapses exactly the per-tier privilege separation the rest of this framework insists on — Tier 1's publish-only grant into the Data & Messaging Perimeter (Landing Zone Section 3.2) has no meaning if Tier 2 and Tier 4 share the same credential. The corrected scaffold returns one role binding per tier.

> **What This Class Deliberately Does Not Do**
>
> provision_landing_zone() stands up the substrate — networks, edge security, data perimeter, compute shells, IAM, and the Saga state machine definition. It does not deploy application code (that's the CI/CD pipeline in Landing Zone Section 9, consuming the artifact registry this class's compute tier definitions point at) and it does not run per-request business logic. Keeping that line clean is what keeps this a control-plane class instead of turning it into a second, competing workflow engine.

## 2.14 Progressive Decomposition — Splitting a Class Into Multiple Microservices

Every class in this playbook is deliberately coarse-grained at first: one BaseRAGPipeline, one BaseToolService per tool, one BaseMCPServer per wrapped API surface. That is a starting shape, not a ceiling. As traffic, team size, or NFRs diverge across the responsibilities inside a single class, the same class can be split along its natural method boundaries into two or more narrower classes — and each of those can then be deployed as its own microservice. BaseValidationService (Section 2.2) is itself an example of this pattern already applied once: state validation was split out of BaseAgent for exactly this reason.

How to Recognize a Split Point

- Different scaling profiles: one group of methods is CPU/GPU-bound and bursty (e.g. embedding generation), another is I/O-bound and steady (e.g. vector search) — forcing them to scale together wastes cost in one direction or starves the other.

- Different update cadence: one group of methods changes weekly (retrieval ranking tuned per tenant), another is nearly static (the ingestion/chunking pipeline) — coupling them in one deployable means low-risk changes wait on high-risk release cycles, and vice versa.

- Different failure blast radius: a failure in one group should not take down the other. A chunking/indexing failure during a batch ingestion job has no reason to affect live query-time retrieval.

- Different callers: if one group of methods is called synchronously in the request path and another is called from a scheduled batch job or a separate producer, they already have different runtime lifecycles even before the code is split.

Worked Example: BaseRAGPipeline

BaseRAGPipeline (Section 2.5) currently owns the full lifecycle implicitly: an implementation's _search_index() assumes documents are already chunked, embedded, and indexed by some other process. Made explicit, that upstream lifecycle splits cleanly into a second class:

| **Class**                                | **Owns**                                                                                                                                                             | **Public entry point** | **Typical deployment**                                                                                                |
|------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------------------|-----------------------------------------------------------------------------------------------------------------------|
| BaseRAGPipeline (existing)               | retrieve() — embed query, _search_index(), _rerank(), _evaluate_answer(). Query-time, synchronous, called on every agent request.                                 | retrieve()             | Request-path service, scales with query QPS (Section 3.2-style compute).                                              |
| BaseIndexingPipeline (new, same pattern) | ingest() — _chunk_documents(), _embed_documents(), _write_index(). Write-time, usually batch or event-triggered, called on document upload/update, not per query. | ingest()               | Event-driven worker, scales with ingestion volume (Section 3.4-style decoupled worker), independent of query traffic. |

BaseIndexingPipeline follows the same shape as every other class in this playbook: one public entry point (ingest()), protected hooks a subclass overrides (_chunk_documents(), _embed_documents(), _write_index()), and the same six propagation fields threaded through — tenant_id in particular is just as critical here as in _search_index(), since a mis-scoped write is worse than a mis-scoped read.

Applying the Same Pattern Elsewhere

| **Existing class** | **Candidate split**                                                                                                                                                                                                                                                                              | **Why**                                                                                                                                      |
|--------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------|
| BaseToolService    | Split _build_payload()/schema-mapping concerns from __http_post()/transport concerns only if a single tool's payload construction becomes independently complex (e.g. a multi-step enrichment pipeline before the call) — otherwise leave as one class.                                       | Most tools don't warrant this; only split when payload-building has its own failure modes and release cadence distinct from the call itself. |
| BaseAgent          | Split _build_messages() (prompt construction, often content/PM-owned and iterated frequently) from _apply_policy()/_evaluate_output() (governance, owned by platform/risk teams, changed rarely) if the two are maintained by different teams with different review and release requirements. | Different owners and different change risk are a stronger signal than raw traffic volume.                                                    |
| BaseMCPServer      | Split wrap_api_as_tool() registration/config concerns (rarely called, admin-time) from serve()/_call_tool() (every request, hot path) only once the number of wrapped tools is large enough that registration logic itself needs independent versioning.                                        | Registration is admin-plane; serving is data-plane — usually fine to colocate until scale forces the split.                                  |

> **Rule of Thumb**
>
> Don't split preemptively. Splitting a class into two microservices adds a network hop, a second deployment pipeline, and a second place propagation fields must be threaded through correctly. Split only once one of the four recognition signals above is concretely true for your workload — not because the class 'feels big'. Every split still follows the same E2A shape: one public entry point, protected override hooks, and the same propagation contract, so the framework's guarantees don't weaken as it decomposes.

## 2.15 Advanced Pattern Integration — Reference Implementations

The patterns below are performance, cost, reliability, and safety techniques that a production E2A deployment accumulates over time. Sixteen of the seventeen fit an existing hook — no new class required. Only Prompt Registry needed a new Foundation interface (Section 2.9). Two contract refinements are formalized rather than left as informal advice: BaseToolService.execute()'s return contract, and _commit_gate's optional version check.

### 2.15.1 Integration Map

| **Pattern**                            | **Hook / Extension Point**                                                                         | **Integration Type**                                                                                                                                                                                                                                                                                                                                           |
|----------------------------------------|----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Ephemeral Context Caching              | BaseAgent._build_messages()                                                                       | Existing hook — place the static block first, flag it cache_control:{'type':'ephemeral'}                                                                                                                                                                                                                                                                       |
| Speculative Decoding                   | BaseAgent.__llm_call() (private, framework-owned)                                                | Config key on io_config — speculative_model, max_speculative_tokens. Zero-touch for managed model endpoints; self-hosted requires the draft and target model co-located on the same Tier 2 GPU node.                                                                                                                                                           |
| Lost-in-the-Middle (U-shape reorder)   | BaseRAGPipeline._rerank()                                                                         | Existing hook — reference implementation only, no contract change                                                                                                                                                                                                                                                                                              |
| Hybrid Long-Context Architecture       | BaseAgent._build_messages()                                                                       | Existing hook — the agent decides per-call whether to load a cached full-context prefix or call self.rag_pipeline.retrieve() first; not a new abstract method, since forcing every agent through a shared decision hook would be a preemptive split (Section 2.14)                                                                                             |
| Semantic Firewall                      | BaseGovernanceFramework.enforce_governance_gate() -> _execute_semantic_firewall() (Section 2.11) | Primary-class template method, not a composed hook — corrected placement: Tier 2 (Private VPC), not a public-edge proxy. See note below.                                                                                                                                                                                                                       |
| Semantic Routing                       | BaseWorkflow._get_agent()                                                                         | Existing hook — corrected placement: classifier runs as Tier 2 compute, not inside the Tier 3 data perimeter, which holds no compute in this framework; the same lookup resolves a RAG-grounded, MCP tool-call, API tool-call, or LLM-only agent (Section 4.8), or the fallback agent directly when no intent matches — a fifth registry entry, not a new hook |
| Chain of Verification (CoVe)           | BaseAgent._evaluate_output() / _fallback()                                                       | Existing hooks — runs synchronously inside BaseAgent.run() (Tier 2) as coded; offloading it to Tier 4 would require an explicit async redesign, not assume one                                                                                                                                                                                                 |
| Extraction Backstop                    | BaseAgent.run() exception handling → _fallback()                                                  | Existing pattern — already how run() converts exceptions into failed_keys entries; formalized as the canonical use of _fallback() for schema repair                                                                                                                                                                                                           |
| Optimistic Concurrency Control         | _commit_gate (extended)                                                                           | Contract extension — _commit_gate gains an optional expected_version/actual_version check, not a second, colliding method name                                                                                                                                                                                                                                |
| Ordered / Causal Delivery              | Intake Topic + Task Queue (FIFO), correlation_id + timestamped message_log                         | Infrastructure-level, already present — see naming note below                                                                                                                                                                                                                                                                                                  |
| Logical vs. Physical State Resolution  | BaseToolService.execute() return contract                                                          | Contract extension — physical_success: bool becomes a required key in every tool result; _resolve_tool_call() and BaseWorkflow must check it before treating a tool's result as ground truth                                                                                                                                                                  |
| Blue-Green / Shadow Mode Trials        | BaseWorkflow.execute()                                                                             | Config-driven — shadow_mode_enabled, shadow_target_pipeline; existing entry point, no new method                                                                                                                                                                                                                                                               |
| Prompt Registry (IaC for Prompts)      | BaseAgent._build_messages(), composing with BasePromptRegistry                                    | New Foundation interface (Section 2.9) — the one genuinely new abstraction in this revision                                                                                                                                                                                                                                                                    |
| Base + Delta (Tenant Merging)          | BaseRAGPipeline._search_index()                                                                   | Existing hook — reference implementation only                                                                                                                                                                                                                                                                                                                  |
| Read-Only Data (Access Locked)         | BaseToolService discovery-tool subclasses + IAM/network boundary                                   | Not a code abstraction — enforced by database credentials and read replicas between Tier 2 and Tier 3, documented in the Landing Zone Section 10.5                                                                                                                                                                                                             |
| Semantic Distance (Similarity Metrics) | BaseRAGPipeline._search_index() io_config                                                         | Config key — similarity_metric, added to the Global Config Reference (Section 5)                                                                                                                                                                                                                                                                               |
| Needle-In-A-Haystack (NIAH) Validation | BasePipeline._run_rag_eval()                                                                      | Existing Foundation interface — canonical reference implementation of the CI/CD RAG evaluation gate                                                                                                                                                                                                                                                            |

### 2.15.2 Two Corrections Worth Flagging

- Semantic Firewall and Semantic Router both run model inference over raw tenant query content — a guard model for the firewall, an embedding classifier for the router. Placing either in a public-facing proxy, or in the Data & Messaging Perimeter (which holds no compute in this framework — see Landing Zone Section 3.3), contradicts the rule that governs every other class in this framework: business logic and tenant-data processing live in the Private VPC, never the public edge or the data-only perimeter. Both patterns' own code samples already agree with this — Semantic Firewall wires into BaseGovernanceFramework.enforce_governance_gate() (Section 2.11) and Semantic Router wires into _get_agent(), both Tier 2 methods. Keep them there.

- 'External Consistency' is a specific, stronger distributed-systems guarantee (Spanner's TrueTime commit-wait, linearizable global ordering across synchronized clocks) than what FIFO partition keys plus timestamped logs actually provide, which is ordered, causal delivery per partition key — a real and useful guarantee, just a weaker one than the name implies. This framework's Intake Topic and Task Queue already give you the weaker, honest guarantee; nothing needs to change in the infrastructure, only in what you call it when explaining the system to someone else.

# 3. Scaffold File — e2a_base.py

Drop this file into any repository as the foundation layer. All E2A abstract classes are defined here with full docstrings, @abstractmethod decorators, config/env resolution, and the cross-class propagation wiring baked in from the start — not layered on after. Developers import and subclass these without modifying this file.

> **File placement**
>
> Place e2a_base.py at src/e2a_base.py or framework/e2a_base.py. All concrete agent, workflow, RAG, tool, and MCP server classes import from it. The file must never be modified once placed — create subclasses instead.

e2a_base.py — Complete Source
```python
# e2a_base.py — E2A Architecture Framework Scaffold
# Drop into src/ or framework/. Import and subclass. Never modify.
# github.com/subhamviky/e2a-framework
 
import os
import uuid
import time
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
 
logging.basicConfig(level=logging.INFO)
 
# ==================================================
# Shared propagation helpers (mixed into every class below)
# ==================================================
 
class _PropagationMixin:
    """Shared, base-owned helpers for correlation/log/DLQ handling.
    Never overridden by subclasses."""
 
    def _resolve_io_config(self, config=None, **kwargs):
        config = config or {}
        prefix = kwargs.get('io_config_prefix', config.get(
            'io_config_prefix', os.getenv('IO_CONFIG_PREFIX', 'io_')))
        return {k: v for k, v in config.items() if k.startswith(prefix)}
 
    def _log(self, message_log, correlation_id, level, event, **fields):
        if message_log is None:
            return
        message_log.append({
            'timestamp': time.time(), 'correlation_id': correlation_id,
            'level': level, 'event': event,
            'class': type(self).__name__, **fields,
        })
 
    def _flush_logs(self, message_log, config=None, **kwargs):
        config = config or {}
        sink = kwargs.get('log_sink', config.get(
            'log_sink', os.getenv('LOG_SINK', 'stdout')))
        for entry in (message_log or []):
            logging.info(entry) if sink == 'stdout' else None
            # cloudwatch / datadog / stackdriver dispatch goes here
 
    def _send_to_dlq(self, failed_keys, config=None, **kwargs):
        config = config or {}
        queue_url = kwargs.get('dlq_queue_url', config.get(
            'dlq_queue_url', os.getenv('DLQ_QUEUE_URL')))
        if queue_url and failed_keys:
            logging.warning(f'DLQ dispatch -> {queue_url}: {failed_keys}')
 
    def _commit_gate(self, key, failed_keys, config=None, **kwargs):
        """Base-owned, called immediately before any save/commit/write.
        Two independent checks, both must pass:
        1. Batch isolation: key must not already be in failed_keys.
        2. Optimistic Concurrency Control (optional): if the caller passes
           expected_version, it must match the store's actual_version —
           a mismatch means a sibling process already wrote a newer
           version, so this write is rejected rather than clobbering it.
        OCC and the failed_keys check are deliberately one gate, not two:
        both answer 'is it still safe to write this item right now?'"""
        config = config or {}
        enabled = kwargs.get('commit_gate_enabled', config.get(
            'commit_gate_enabled', True))
        if not enabled:
            return True
        if key in (failed_keys or []):
            return False
        expected_version = kwargs.get('expected_version')
        if expected_version is not None:
            actual_version = kwargs.get('actual_version_lookup', lambda k, c: expected_version)(key, config)
            if expected_version != actual_version:
                return False  # OCC collision — a sibling process wrote a newer version
        return True
 
# ==================================================
# BaseValidationService — runs before BaseWorkflow/BaseAgent exist
# ==================================================
 
class ValidationResult(dict):
    """Thin dict subclass: {'valid': bool, 'errors': list,
    'agent_name': str, 'correlation_id': str}. Kept as a dict so it
    serializes directly as an HTTP 400 body with no extra mapping."""
 
class BaseValidationService(ABC, _PropagationMixin):
 
    validator_registry: Dict[str, Any] = {}  # populated by the subclass
 
    def validate(self, agent_name: str, state: Dict[str, Any],
                 config: Dict[str, Any] = None, correlation_id: str = None,
                 **kwargs) -> 'ValidationResult':
        """Public entry point. Earliest point in the request chain —
        originates correlation_id if the caller (API Gateway / WAF)
        hasn't already minted one."""
        config = config or {}
        correlation_id = correlation_id or state.get(
            'correlation_id', str(uuid.uuid4()))
        validator = self._resolve_validator(agent_name, config, **kwargs)
        if validator is None:
            strict = kwargs.get('strict_mode', config.get(
                'strict_mode', os.getenv('VALIDATION_STRICT_MODE', 'True') == 'True'))
            if strict:
                return self.__build_result(
                    False, [f'No validator registered for {agent_name}'],
                    agent_name, correlation_id)
            return self.__build_result(True, [], agent_name, correlation_id)
        valid, errors = validator(state, config, **kwargs)
        return self.__build_result(valid, errors, agent_name, correlation_id)
 
    def _resolve_validator(self, agent_name, config=None, **kwargs):
        """Same registry-resolution pattern as BaseWorkflow._get_agent()
        and _get_tool_service() — name-keyed lookup, not if/elif chains."""
        return self.validator_registry.get(agent_name)
 
    def __build_result(self, valid, errors, agent_name, correlation_id) -> 'ValidationResult':
        """PRIVATE, framework-owned. Never overridden."""
        return ValidationResult(valid=valid, errors=errors,
                                 agent_name=agent_name, correlation_id=correlation_id)
 
 
class OrderOpsValidationService(BaseValidationService):
    """Example concrete subclass — one _validate_<agent_name> per agent,
    registered in validator_registry. This class is what actually
    deploys to the Lambda / Cloud Function edge tier."""
 
    def __init__(self):
        self.validator_registry = {
            'RefundAgent': self._validate_refund_agent,
            'OrderOpsAgent': self._validate_order_ops_agent,
        }
 
    def _validate_refund_agent(self, state, config=None, **kwargs):
        errors = []
        if 'order_id' not in state:
            errors.append('order_id is required')
        if 'tenant_id' not in state:
            errors.append('tenant_id is required')
        return (len(errors) == 0, errors)
 
    def _validate_order_ops_agent(self, state, config=None, **kwargs):
        errors = [f'{f} is required' for f in ('query', 'user_id', 'intent', 'tenant_id')
                  if f not in state]
        return (len(errors) == 0, errors)
 
# ==================================================
# BaseAgent
# ==================================================
 
class BaseAgent(ABC, _PropagationMixin):
 
    def run(self, state: Dict[str, Any], config: Dict[str, Any] = None,
            correlation_id: str = None, io_config: Dict[str, Any] = None,
            idempotency_key: str = None, tenant_id: str = None,
            message_log: List[dict] = None, failed_keys: List[str] = None,
            **kwargs) -> Dict[str, Any]:
        """Public entry point. Receives propagation fields from
        BaseWorkflow; self-originates them only if run() is invoked
        standalone (e.g. in a unit test), so the class remains usable
        outside the full chain. Assumes `state` already passed
        BaseValidationService.validate() — run() does not validate
        its own input; there is no _validate_state hook here."""
        config = config or {}
        correlation_id = correlation_id or state.get(
            'correlation_id', str(uuid.uuid4()))
        io_config = io_config or self._resolve_io_config(config, **kwargs)
        message_log = message_log if message_log is not None else []
        failed_keys = failed_keys if failed_keys is not None else []
        tenant_id = tenant_id or state.get('tenant_id')
        common = dict(correlation_id=correlation_id, io_config=io_config,
                      tenant_id=tenant_id, message_log=message_log)
        try:
            messages = self._build_messages(state, config, **common, **kwargs)
            self._apply_policy(state, config, **common, **kwargs)
            response = self.__llm_call(messages, config, **kwargs)
            confidence = self._evaluate_output(
                response, state, config, **common, **kwargs)
            min_conf = kwargs.get('min_confidence', config.get(
                'min_confidence', float(os.getenv('MIN_CONFIDENCE', 0.85))))
            if confidence < min_conf:
                response = self._fallback(state, config, **common, **kwargs)
            state['response'] = response
        except Exception as e:
            failed_keys.append(idempotency_key or correlation_id)
            self._handle_error(e, state, config, correlation_id=correlation_id,
                                message_log=message_log,
                                failed_keys=failed_keys, **kwargs)
        state['tenant_id'] = tenant_id
        state['correlation_id'] = correlation_id
        return state
 
    @abstractmethod
    def _build_messages(self, state, config=None, **kwargs):
        """kwargs: prompt_template, role"""
 
    @abstractmethod
    def _apply_policy(self, state, config=None, **kwargs):
        """kwargs: retry_policy, circuit_breaker_config.
        This is where _resolve_tool_call() is invoked if the agent
        decides a tool needs to run — see the Tool Routing section."""
 
    # PRIVATE — framework owns LLM dispatch entirely
    def __llm_call(self, messages, config=None, **kwargs):
        model_id = kwargs.get('model_id', config.get(
            'model_id', os.getenv('LLM_MODEL_ID', 'default')))
        # routed to Bedrock / Vertex / OpenAI by prefix — unchanged
        return {'text': '...', 'metadata': {'model_id': model_id}}
 
    @abstractmethod
    def _evaluate_output(self, response, state, config=None, **kwargs):
        """Return float [0.0-1.0]. kwargs: min_confidence"""
 
    @abstractmethod
    def _fallback(self, state, config=None, **kwargs):
        """kwargs: fallback_model, cache_key"""
 
    @abstractmethod
    def _handle_error(self, error, state, config=None, **kwargs):
        """Appends to failed_keys via kwargs['failed_keys'].append(...)"""
 
# ==================================================
# BaseWorkflow
# ==================================================
 
class BaseWorkflow(ABC, _PropagationMixin):
 
    def execute(self, state: Dict[str, Any], config: Dict[str, Any] = None,
                **kwargs) -> Dict[str, Any]:
        """Public entry point. Origin of every propagation field — the
        only place correlation_id, io_config, and idempotency_key are
        generated from scratch."""
        config = config or {}
        tenant_id = state.get('tenant_id')
        if not tenant_id:
            raise ValueError('state["tenant_id"] is required')
        correlation_id = kwargs.get(
            'correlation_id', state.get('correlation_id', str(uuid.uuid4())))
        io_config = self._resolve_io_config(config, **kwargs)
        idempotency_key = self.__resolve_idempotency(state, config, **kwargs)
        message_log: List[dict] = []
        failed_keys: List[str] = []
        state['tenant_id'] = tenant_id
        state['correlation_id'] = correlation_id
        state['idempotency_key'] = idempotency_key
        try:
            workflow = self._build_workflow(
                config, correlation_id=correlation_id, io_config=io_config,
                tenant_id=tenant_id, message_log=message_log, **kwargs)
            if not self._validate_workflow(
                    workflow, config, correlation_id=correlation_id,
                    io_config=io_config, tenant_id=tenant_id,
                    message_log=message_log, **kwargs):
                raise ValueError('Workflow validation failed')
            agent = self._get_agent(
                state.get('intent'), config, correlation_id=correlation_id,
                io_config=io_config, tenant_id=tenant_id,
                message_log=message_log, **kwargs)
            state = agent.run(
                state, config, correlation_id=correlation_id,
                io_config=io_config, idempotency_key=idempotency_key,
                tenant_id=tenant_id, message_log=message_log,
                failed_keys=failed_keys, **kwargs)
        except Exception as e:
            failed_keys.append(idempotency_key)
            self._handle_error(e, state, config, correlation_id=correlation_id,
                                message_log=message_log,
                                failed_keys=failed_keys, **kwargs)
        finally:
            self._flush_logs(message_log, config)
            if failed_keys:
                self._send_to_dlq(failed_keys, config)
            state['message_log'] = message_log
            state['failed_keys'] = failed_keys
        return state
 
    @abstractmethod
    def _build_workflow(self, config=None, **kwargs):
        """kwargs: workflow_definition"""
 
    @abstractmethod
    def _validate_workflow(self, workflow, config=None, **kwargs):
        """kwargs: required_nodes"""
 
    @abstractmethod
    def _get_agent(self, intent, config=None, **kwargs) -> BaseAgent:
        """kwargs: agent_registry"""
 
    @abstractmethod
    def _generate_idempotency_key(self, state, config=None, **kwargs) -> str:
        """Domain logic, e.g. hash(tenant_id + REF_ELEM_KEY).
        Called only when no valid client-supplied key exists."""
 
    @abstractmethod
    def _handle_error(self, error, state, config=None, **kwargs):
        pass
 
    # PRIVATE — base-owned, never overridden
    def __resolve_idempotency(self, state, config=None, **kwargs) -> str:
        """1. Client-supplied key wins if present and well-formed.
        2. A matching, non-expired store entry means 'retain existing'
           (this is a replay). 3. Otherwise mint a new key via the
        subclass's domain logic and persist it before returning."""
        config = config or {}
        client_key = kwargs.get('idempotency_key', state.get('idempotency_key'))
        if client_key:
            existing = self.__lookup_idempotency_store(client_key, config)
            if existing and not existing.get('expired'):
                return existing['idempotency_key']
        new_key = self._generate_idempotency_key(state, config, **kwargs)
        self.__persist_idempotency_store(new_key, config)
        return new_key
 
    def __lookup_idempotency_store(self, key, config=None):
        return None  # DynamoDB/Redis lookup via config['idempotency_store_url']
 
    def __persist_idempotency_store(self, key, config=None):
        pass  # persisted with TTL = config['idempotency_ttl_seconds']
 
# ==================================================
# BaseRAGPipeline
# ==================================================
 
class BaseRAGPipeline(ABC, _PropagationMixin):
 
    def retrieve(self, query: str, config: Dict[str, Any] = None,
                 tenant_id: str = None, **kwargs) -> List[dict]:
        config = config or {}
        vector = self.__embed(query, config, **kwargs)
        results = self._search_index(vector, config, tenant_id=tenant_id, **kwargs)
        reranked = self._rerank(results, config, **kwargs)
        return reranked
 
    @abstractmethod
    def _search_index(self, query_vector, config=None, tenant_id=None, **kwargs):
        """Namespace the search to f'{tenant_index_prefix}_{tenant_id}'"""
 
    @abstractmethod
    def _rerank(self, results, config=None, **kwargs):
        """kwargs: rerank_top_n"""
 
    @abstractmethod
    def _evaluate_answer(self, answer, config=None, **kwargs):
        """kwargs: groundedness_threshold"""
 
    def __embed(self, query, config=None, **kwargs):
        model = kwargs.get('embedding_model', config.get(
            'embedding_model', os.getenv(
                'EMBEDDING_MODEL', 'amazon.titan-embed-text-v1')))
        return [0.0]  # provider call
 
# ==================================================
# BaseToolService  (client — REST or MCP transport)
# ==================================================
 
class BaseToolService(ABC, _PropagationMixin):
 
    async def execute(self, payload: Dict[str, Any],
                       config: Dict[str, Any] = None, tenant_id: str = None,
                       failed_keys: List[str] = None, **kwargs) -> Dict[str, Any]:
        """Return contract: every result MUST include physical_success: bool.
        This is the framework's Logical-vs-Physical rule — an agent's
        textual narrative ('I processed the transfer') is never treated
        as ground truth. Only this typed field is. Callers (BaseWorkflow,
        _resolve_tool_call()) check physical_success before proceeding,
        regardless of what the agent's response text claims."""
        config = config or {}
        if not self._validate_input(payload, config, **kwargs):
            raise ValueError('Invalid input')
        key = kwargs.get('idempotency_key', payload.get('idempotency_key'))
        if key and not self._commit_gate(key, failed_keys, config, **kwargs):
            return {'status': 'blocked', 'reason': 'commit_gate'}
        endpoint = self.get_endpoint(config, tenant_id=tenant_id, **kwargs)
        body = self._build_payload(payload, config, **kwargs)
        return await self.__http_post(endpoint, body, config, **kwargs)
 
    @abstractmethod
    def get_endpoint(self, config=None, tenant_id=None, **kwargs):
        """kwargs: base_url, service_name, tenant_routing_map"""
 
    @abstractmethod
    def _build_payload(self, payload, config=None, **kwargs):
        """kwargs: schema, enrichments"""
 
    @abstractmethod
    def _validate_input(self, payload, config=None, **kwargs):
        """kwargs: schema, required_fields"""
 
    # PRIVATE — framework owns HTTP mechanics
    async def __http_post(self, endpoint, body, config=None, **kwargs):
        timeout = kwargs.get('timeout', config.get(
            'timeout', float(os.getenv('TOOL_TIMEOUT', 5.0))))
        retries = kwargs.get('retries', config.get(
            'retries', int(os.getenv('TOOL_RETRIES', 3))))
        token = kwargs.get('auth_token', config.get(
            'auth_token', os.getenv('TOOL_AUTH_TOKEN')))
        # physical_success reflects the real network/API outcome —
        # callers must check this, never an agent's narrative about it.
        return {'status': 'ok', 'endpoint': endpoint, 'physical_success': True}  # aiohttp/httpx call
 
class RestToolService(BaseToolService):
    """Classic REST/HTTP tool."""
 
    def get_endpoint(self, config=None, tenant_id=None, **kwargs):
        config = config or {}
        routing = config.get('tenant_routing_map', {})
        if tenant_id and tenant_id in routing:
            return routing[tenant_id]
        return kwargs.get('base_url', config.get(
            'tool_base_url', os.getenv('TOOL_BASE_URL', 'http://localhost')))
 
    def _build_payload(self, payload, config=None, **kwargs):
        return payload
 
    def _validate_input(self, payload, config=None, **kwargs):
        required = kwargs.get('required_fields', [])
        return all(f in payload for f in required)
 
class MCPToolService(BaseToolService):
    """MCP tool over Streamable HTTP. Reuses BaseToolService.__http_post
    unchanged — MCP Streamable HTTP is JSON-RPC over HTTP POST, so only
    get_endpoint() and _build_payload() differ from RestToolService.
    Points at either an external MCP server or one hosted locally via
    a BaseMCPServer instance."""
 
    def get_endpoint(self, config=None, tenant_id=None, **kwargs):
        config = config or {}
        return kwargs.get('mcp_server_url', config.get(
            'mcp_server_url', os.getenv('MCP_SERVER_URL')))
 
    def _build_payload(self, payload, config=None, **kwargs):
        tool_name = kwargs.get('mcp_tool_name', payload.get('_tool_name'))
        return {
            'jsonrpc': '2.0', 'method': 'tools/call',
            'params': {'name': tool_name, 'arguments': payload},
        }
 
    def _validate_input(self, payload, config=None, **kwargs):
        required = kwargs.get('required_fields', [])
        return all(f in payload for f in required)
 
# ==================================================
# BaseMCPServer  (server — wraps existing APIs as MCP tools)
# ==================================================
 
class BaseMCPServer(ABC, _PropagationMixin):
    """Turns wrapped BaseToolService instances into a spec-compliant
    MCP server: tools/list + tools/call over JSON-RPC."""
 
    server_name: str = 'e2a-mcp-server'
 
    def __init__(self):
        self.registered_tools: Dict[str, dict] = {}
 
    def serve(self, request: Dict[str, Any],
              config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        """Public entry point. Single JSON-RPC dispatch surface for the
        server, mirroring run()/execute()/retrieve()."""
        return self.__dispatch_jsonrpc(request, config, **kwargs)
 
    def wrap_api_as_tool(self, handler, tool_name: str, description: str,
                          input_schema: dict, config: Dict[str, Any] = None,
                          **kwargs) -> None:
        """Factory method — 'create an MCP server by wrapping an API'.
        handler is any existing BaseToolService instance; its execute()
        is reused as-is. No new transport code is written per tool."""
        self.registered_tools[tool_name] = {
            'handler': handler, 'description': description,
            'input_schema': input_schema,
        }
 
    def _list_tools(self, config: Dict[str, Any] = None, **kwargs) -> List[dict]:
        """Default: derive tools/list output from registered_tools.
        Override only to filter/augment (e.g. per-tenant visibility)."""
        return [
            {'name': name, 'description': entry['description'],
             'inputSchema': entry['input_schema']}
            for name, entry in self.registered_tools.items()
        ]
 
    async def _call_tool(self, tool_name: str, arguments: dict,
                          config: Dict[str, Any] = None, tenant_id: str = None,
                          **kwargs) -> Dict[str, Any]:
        """Default: resolve tool_name and delegate to the wrapped
        handler's execute(). Returns an MCP-shaped result."""
        entry = self.registered_tools.get(tool_name)
        if not entry:
            return {'content': [{'type': 'text',
                    'text': f'Unknown tool: {tool_name}'}], 'isError': True}
        result = await entry['handler'].execute(
            arguments, config, tenant_id=tenant_id, **kwargs)
        return {'content': [{'type': 'text', 'text': str(result)}],
                'isError': False}
 
    # PRIVATE — framework-owned JSON-RPC envelope handling
    def __dispatch_jsonrpc(self, request, config=None, **kwargs):
        method = request.get('method')
        req_id = request.get('id')
        if method == 'tools/list':
            result = self._list_tools(config, **kwargs)
        elif method == 'tools/call':
            params = request.get('params', {})
            result = self._call_tool(
                params.get('name'), params.get('arguments', {}),
                config, **kwargs)
        else:
            return {'jsonrpc': '2.0', 'id': req_id,
                    'error': {'code': -32601, 'message': 'Method not found'}}
        return {'jsonrpc': '2.0', 'id': req_id, 'result': result}
 
# ==================================================
# Tool call routing — MCP-first, REST fallback
# lives beside _get_agent(); called from the orchestration layer
# ==================================================
 
async def _resolve_tool_call(tool_name: str, payload: dict,
                              config: Dict[str, Any] = None,
                              tenant_id: str = None, **kwargs) -> dict:
    """1. Does a wrapped MCP server exist for this tool? -> call it as
    an MCP tool call. 2. Otherwise -> execute the normal tool call
    (REST, via _get_tool_service's existing resolution)."""
    config = config or {}
    mcp_registry = config.get('mcp_server_registry', {})
    entry = mcp_registry.get(tool_name)
    if entry:
        service = MCPToolService()
        return await service.execute(
            payload, config, tenant_id=tenant_id,
            mcp_server_url=entry['mcp_server_url'],
            mcp_tool_name=entry.get('mcp_tool_name', tool_name),
            **kwargs)
    service = _get_tool_service(tool_name, config, **kwargs)
    return await service.execute(payload, config, tenant_id=tenant_id, **kwargs)
 
def _get_tool_service(tool_name: str, config: Dict[str, Any] = None,
                       **kwargs) -> BaseToolService:
    """Fallback resolver, used only when no local MCP server wraps this
    tool. Resolution: kwargs > config['tool_registry'][tool_name] >
    default_transport."""
    config = config or {}
    registry = config.get('tool_registry', {})
    entry = registry.get(tool_name, {})
    transport = kwargs.get('transport', entry.get(
        'transport', os.getenv('DEFAULT_TOOL_TRANSPORT', 'http')))
    if transport == 'mcp':
        return MCPToolService()
    return RestToolService()

# ======================================================================
# LLMOnlyAgent — abstract peer to BaseAgent, BaseRAGPipeline, and
# BaseToolService above, for intents that need only the model itself.
# Subclass this directly (see multimodal_orchestrator.py for a
# reference implementation, GenericLLMOnlyAgent).
# ======================================================================

class LLMOnlyAgent(ABC, _PropagationMixin):
    """Abstract peer to BaseAgent, BaseRAGPipeline, and BaseToolService —
    for intents that need only the model itself: no tool call, no
    retrieval. Kept as its own abstract line rather than a BaseAgent
    subclass because its public contract dispatches on modality (text,
    audio, image, document) to a distinct abstract handler per
    modality before a single prompt is ever built — the same reason
    BaseRAGPipeline is its own abstract class beside BaseAgent instead
    of a subclass of it. run() has the same public shape as
    BaseAgent.run() — propagation fields in, confidence-checked
    response out — so a concrete subclass slots into an agent_registry
    and is returned by _get_agent() exactly like a BaseAgent subclass
    would be, without inheriting from it. Subclass this directly for
    any concrete multimodal agent; never modify this class itself."""

    def run(self, state, config=None, correlation_id=None, io_config=None,
            idempotency_key=None, tenant_id=None, message_log=None,
            failed_keys=None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        correlation_id = correlation_id or state.get(
            "correlation_id", str(uuid.uuid4()))
        io_config = io_config or self._resolve_io_config(config, **kwargs)
        message_log = message_log if message_log is not None else []
        failed_keys = failed_keys if failed_keys is not None else []
        tenant_id = tenant_id or state.get("tenant_id")
        common = dict(correlation_id=correlation_id, io_config=io_config,
                      tenant_id=tenant_id, message_log=message_log)
        try:
            modality = self._detect_modality(state, config, **kwargs)
            handler = {
                "TEXT": self._handle_text,
                "AUDIO": self._handle_audio,
                "IMAGE": self._handle_image,
                "DOCUMENT": self._handle_document,
            }[modality]
            response = handler(state, config, **common, **kwargs)
            confidence = self._evaluate_output(
                response, state, config, **common, **kwargs)
            min_conf = kwargs.get("min_confidence", config.get(
                "min_confidence", float(os.getenv("MIN_CONFIDENCE", 0.85))))
            if confidence < min_conf:
                response = self._fallback(state, config, **common, **kwargs)
            state["response"] = response
        except Exception as e:
            failed_keys.append(idempotency_key or correlation_id)
            self._handle_error(e, state, config, correlation_id=correlation_id,
                                message_log=message_log,
                                failed_keys=failed_keys, **kwargs)
        state["tenant_id"] = tenant_id
        state["correlation_id"] = correlation_id
        return state

    @abstractmethod
    def _detect_modality(self, state, config=None, **kwargs) -> str:
        """Return one of 'TEXT', 'AUDIO', 'IMAGE', 'DOCUMENT'."""

    @abstractmethod
    def _handle_text(self, state, config=None, **kwargs) -> Dict[str, Any]:
        """Compose the prompt from state directly and call the model."""

    @abstractmethod
    def _handle_audio(self, state, config=None, **kwargs) -> Dict[str, Any]:
        """Transcribe state['audio_uri'], then call the model. kwargs:
        stt_provider, max_audio_duration_seconds."""

    @abstractmethod
    def _handle_image(self, state, config=None, **kwargs) -> Dict[str, Any]:
        """Call a vision-capable model with state['image_uri'] directly —
        no transcription/extraction step, unlike audio and document."""

    @abstractmethod
    def _handle_document(self, state, config=None, **kwargs) -> Dict[str, Any]:
        """Extract state['document_uri'], then call the model. kwargs:
        document_extractor, max_document_pages."""

    def _select_model(self, modality, config=None, **kwargs) -> str:
        """PROTECTED, concrete — same registry-resolution pattern as
        _get_tool_service(): a config-driven lookup, resolved by
        modality, never an if/elif chain, so adding a model is a
        config change, not code. Available to every _handle_*
        implementation a subclass writes:
        {'TEXT': 'anthropic.claude-sonnet-4-6', 'IMAGE': 'anthropic.
         claude-sonnet-4-6', 'AUDIO': 'amazon.titan-text-express-v1',
         'DOCUMENT': 'anthropic.claude-sonnet-4-6', 'default': '...'}"""
        registry = kwargs.get("model_capability_registry",
                               (config or {}).get("model_capability_registry", {}))
        return registry.get(modality, registry.get(
            "default", os.getenv("LLM_MODEL_ID", "default")))

    def _llm_call(self, messages, modality, config=None, **kwargs) -> Dict[str, Any]:
        """PROTECTED, concrete, single underscore — not name-mangled,
        so a subclass's _handle_* methods can call it directly (unlike
        BaseAgent.__llm_call(), which only BaseAgent.run() itself ever
        calls). Resolves the model via _select_model() and dispatches;
        routed to Bedrock / Vertex / OpenAI by prefix — unchanged."""
        model_id = self._select_model(modality, config, **kwargs)
        return {"text": "...", "metadata": {"model_id": model_id}}

    @abstractmethod
    def _evaluate_output(self, response, state, config=None, **kwargs) -> float:
        """Return float [0.0-1.0]. kwargs: min_confidence"""

    @abstractmethod
    def _fallback(self, state, config=None, **kwargs):
        """kwargs: fallback_model, cache_key"""

    @abstractmethod
    def _handle_error(self, error, state, config=None, **kwargs):
        """Appends to failed_keys via kwargs['failed_keys'].append(...)"""


# ==================================================
# BasePromptRegistry — Foundation interface, composed
# into BaseAgent._build_messages(), not called directly
# ==================================================
 
class BasePromptRegistry(ABC):
    """Interface — no shared implementation. Decouples prompt text
    from application code so business teams can update a prompt via
    GitOps without a container rebuild."""
 
    @abstractmethod
    def get_prompt(self, prompt_id: str, version: str = 'latest',
                    tenant_id: str = None, config: Dict[str, Any] = None,
                    **kwargs) -> 'PromptTemplate':
        """kwargs: prompt_store_url, default_version. Returns an object
        with a .render(**variables) -> str method."""
 
 
# Example usage inside a concrete BaseAgent — not part of the scaffold,
# shown here for reference:
#
# def _build_messages(self, state, config=None, **kwargs):
#     template = self.prompt_registry.get_prompt(
#         prompt_id=self.prompt_id,
#         version=kwargs.get('io_config', {}).get('prompt_version', 'latest'),
#         tenant_id=kwargs.get('tenant_id'))
#     return [{'role': 'user', 'content': template.render(query=state['query'])}]

# ==================================================
# BasePipeline — control-plane class. CI/CD lifecycle engine,
# the code-level counterpart to Landing Zone Section 9.
# Run once per commit/deploy, not per request.
# ==================================================
 
class BasePipeline(ABC, _PropagationMixin):
 
    def __init__(self):
        # One image per E2A class family that deploys as its own
        # service. Tier 0 (API Gateway) is a managed service, not a
        # custom image; Tier 5 (Saga) is IaC-defined via
        # BaseInfraProvisioner, not a container — both correctly absent.
        self.required_artifact_matrix = [
            'e2a-ingress-validator',      # Tier 1
            'e2a-orchestration-engine',   # Tier 2 (Workflow + Agent)
            'e2a-rag-retrieval-worker',   # Tier 4
            'e2a-rest-tool-worker',       # Tier 4
            'e2a-mcp-server-worker',      # Tier 4
        ]
 
    def run_pipeline(self, config: Dict[str, Any] = None,
                      correlation_id: str = None,
                      message_log: List[dict] = None,
                      failed_keys: List[str] = None, **kwargs) -> Dict[str, Any]:
        """Public entry point. Seven fixed stages, cheapest/fastest
        checks first. Any stage failing is caught here, not raised
        past this boundary — same philosophy as every other run()/
        execute() in this framework."""
        config = config or {}
        correlation_id = correlation_id or f'pipeline-{int(time.time())}'
        message_log = message_log if message_log is not None else []
        failed_keys = failed_keys if failed_keys is not None else []
        report = {'correlation_id': correlation_id, 'stages': {}, 'status': 'IN_PROGRESS'}
        try:
            if not self._run_static_security_scans(config, **kwargs):
                raise ValueError('Static security check failed: vulnerabilities or exposed secrets.')
            report['stages']['static_security'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'static_security_passed')
 
            if not self._execute_test_suite(config, **kwargs):
                raise ValueError('Test suite failed: regression detected in tests/.')
            report['stages']['test_execution'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'test_suite_passed')
 
            if not self._verify_scaffold_contracts(config, **kwargs):
                raise ValueError('Scaffold verification failed: Single Public Entry Point Rule violated.')
            report['stages']['scaffold_compliance'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'scaffold_compliance_passed')
 
            min_faithfulness = float(kwargs.get('faithfulness_threshold', config.get(
                'faithfulness_threshold', 0.85)))
            rag_score = self._run_rag_eval(config, **kwargs)
            report['rag_eval_score'] = rag_score
            if rag_score < min_faithfulness:
                raise ValueError(f'RAGAS gate failed: {rag_score} below threshold {min_faithfulness}.')
            report['stages']['ragas_gate'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'ragas_gate_passed', score=rag_score)
 
            for artifact in self.required_artifact_matrix:
                if not self._compile_artifact(artifact, config, **kwargs):
                    raise RuntimeError(f'Failed to compile/push artifact: {artifact}')
            report['stages']['artifact_compilation'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'artifacts_compiled',
                       count=len(self.required_artifact_matrix))
 
            if not self._run_dynamic_security_checks(config, **kwargs):
                raise ValueError('DAST failed in the ephemeral staging sandbox.')
            report['stages']['dynamic_security'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'dynamic_security_passed')
 
            strategy = kwargs.get('deployment_strategy', config.get(
                'deployment_strategy', os.getenv('DEPLOYMENT_STRATEGY', 'BLUE_GREEN'))).upper()
            deploy_meta = self._execute_deployment_rollout(strategy, config, **kwargs)
            report['stages']['deployment_rollout'] = f'PASSED_{strategy}'
            report['deployment_metadata'] = deploy_meta
            report['status'] = 'SUCCESS'
        except Exception as e:
            failed_keys.append(correlation_id)
            report['status'] = 'FAILED'
            report['error'] = str(e)
            self._log(message_log, correlation_id, 'ERROR', 'pipeline_failed', error=str(e))
        finally:
            self._flush_logs(message_log, config)
            report['message_log'] = message_log
        return report
 
    @abstractmethod
    def _run_static_security_scans(self, config, **kwargs) -> bool:
        """SAST + secret detection (e.g. gitleaks) for exposed LLM API keys."""
 
    @abstractmethod
    def _execute_test_suite(self, config, **kwargs) -> bool:
        """Run tests/ — unit + integration against the scaffold contracts."""
 
    @abstractmethod
    def _verify_scaffold_contracts(self, config, **kwargs) -> bool:
        """Statically assert the Single Public Entry Point Rule: exactly
        one public method per core class, no framework-owned private
        method (__http_post, __llm_call, etc.) overridden downstream."""
 
    @abstractmethod
    def _run_rag_eval(self, config, **kwargs) -> float:
        """RAGAS faithfulness score against a golden dataset. See the
        NIAH reference implementation for a concrete example."""
 
    @abstractmethod
    def _compile_artifact(self, artifact_name, config, **kwargs) -> bool:
        """Build + push one OCI/Docker image to the artifact registry."""
 
    @abstractmethod
    def _run_dynamic_security_checks(self, config, **kwargs) -> bool:
        """DAST against a running instance in an ephemeral sandbox —
        shares infrastructure with the NIAH testing pattern."""
 
    @abstractmethod
    def _execute_deployment_rollout(self, strategy, config, **kwargs) -> dict:
        """Canary weight-shift or Blue-Green cutover. Deployment-level —
        not to be confused with Section 2.15's application-level Shadow
        Mode, which mirrors live request traffic instead of environments."""

# ==================================================
# BaseInfraProvisioner — control-plane class. Stands up the
# substrate the Cloud Landing Zone HLD/LLD document describes.
# Not part of the request-time propagation contract (Section 2.1) —
# this runs from CI/CD, once per environment/tenant, not per request.
# ==================================================
 
class BaseInfraProvisioner(ABC):
 
    def provision_landing_zone(self, tenant_id: str, tenancy_model: str,
                                config: Dict[str, Any] = None,
                                correlation_id: str = None, **kwargs) -> Dict[str, Any]:
        """Public entry point. tenancy_model is 'POOL' or 'SILO' (Section 10.2
        of the Landing Zone doc). Runs all seven steps in fixed order and
        returns a manifest; never raises past this boundary."""
        config = config or {}
        correlation_id = correlation_id or f'iac-{tenant_id}-{int(time.time())}'
        manifest = {
            'tenant_id': tenant_id, 'tenancy_model': tenancy_model,
            'correlation_id': correlation_id, 'topology_maps': {}, 'status': 'INITIATED',
        }
        try:
            networks = self._define_network_topologies(tenant_id, config, **kwargs)
            manifest['topology_maps']['networks'] = networks
            edge_security = self._define_edge_security(networks, config, **kwargs)
            manifest['topology_maps']['edge_security'] = edge_security
            data_perimeter = self._define_data_perimeter_substrate(
                networks, tenant_id, tenancy_model, config, **kwargs)
            manifest['topology_maps']['data_perimeter'] = data_perimeter
            compute = self._define_compute_tiers(networks, data_perimeter, config, **kwargs)
            manifest['topology_maps']['compute'] = compute
            iam = self._define_iam_governance_framework(compute, data_perimeter, config, **kwargs)
            manifest['topology_maps']['iam_governance'] = iam
            saga = self._generate_saga_orchestrator(compute, config, **kwargs)
            manifest['topology_maps']['saga_orchestration'] = saga
            if self._apply_infrastructure_graph(manifest, config, **kwargs):
                manifest['status'] = 'PROVISIONED'
                manifest['timestamp'] = time.time()
            else:
                raise RuntimeError('Infra apply returned a structural error.')
        except Exception as e:
            manifest['status'] = 'FAILED'
            manifest['error_trace'] = str(e)
        return manifest
 
    @abstractmethod
    def _define_network_topologies(self, tenant_id, config=None, **kwargs) -> dict:
        """Public VPC/DMZ, Private VPC application space, and the Data &
        Messaging Perimeter's service boundary (VPC-SC / PrivateLink)."""
 
    @abstractmethod
    def _define_edge_security(self, networks, config=None, **kwargs) -> dict:
        """Tier 0: API Gateway + WAF/Cloud Armor, including prompt-injection
        filter rules (Semantic Firewall's edge-layer half)."""
 
    @abstractmethod
    def _define_data_perimeter_substrate(self, networks, tenant_id,
                                          tenancy_model, config=None, **kwargs) -> dict:
        """Tier 3: Intake Topic (push), Task Queue (pull, redrive at
        max_receives=3 to a DLQ), State/Outbox table routed per
        tenancy_model (POOL: shared table + partition key; SILO: separate
        instance/account)."""
 
    @abstractmethod
    def _define_compute_tiers(self, networks, data_meta, config=None, **kwargs) -> dict:
        """Tier 1 (validation function), Tier 2 (BaseWorkflow + BaseAgent,
        co-located), Tier 4 (decoupled workers, queue-depth autoscaled)."""
 
    @abstractmethod
    def _define_iam_governance_framework(self, compute, data_meta,
                                          config=None, **kwargs) -> dict:
        """One role binding PER TIER, not one shared credential:
        Tier 1 -> publish-only on the Intake Topic.
        Tier 2/4 -> broader intra-perimeter read/write.
        Tier 2 -> tool/database credentials read-only where the
        Read-Only Data pattern applies."""
 
    def _generate_saga_orchestrator(self, compute_ctx, config=None, **kwargs) -> dict:
        """Concrete AWS-flavored DEFAULT — override this for GCP/Azure.
        Unlike __http_post or _commit_gate, this is NOT framework-locked;
        Step Functions ASL is not valid GCP Workflows YAML or Azure Logic
        Apps JSON, so a non-AWS subclass must override the syntax while
        keeping the same logical shape (Choice on failed_keys -> Parallel
        with 3 tracks -> webhook dispatch)."""
        return {
            'engine_type': 'aws_states_language',
            'structural_skeleton': {
                'Comment': 'E2A Distributed Saga Orchestrator & Save Prevention Engine',
                'StartAt': 'EvaluateFailedKeys',
                'States': {
                    'EvaluateFailedKeys': {
                        'Type': 'Choice',
                        'Choices': [{'Variable': '$.failed_keys[0]', 'IsPresent': True,
                                     'Next': 'IsolateAndSplitBatch'}],
                        'Default': 'StandardDatabaseCommit',
                    },
                    'StandardDatabaseCommit': {'Type': 'Task', 'Next': 'DispatchFinalWebhook'},
                    'IsolateAndSplitBatch': {
                        'Type': 'Parallel',
                        'Branches': [
                            {'StartAt': 'CommitSuccessfulItems', 'States': {'...': '...'}},
                            {'StartAt': 'RunCompensatingRollback', 'States': {'...': '...'}},
                            {'StartAt': 'RouteToDLQ', 'States': {'...': '...'}},
                        ],
                        'Next': 'DispatchFinalWebhook',
                    },
                    'DispatchFinalWebhook': {'Type': 'Task', 'End': True},
                },
            },
        }
 
    @abstractmethod
    def _apply_infrastructure_graph(self, final_manifest, config=None, **kwargs) -> bool:
        """Terraform apply / Pulumi up / CDK deploy / raw SDK client. The
        only step that touches real infrastructure."""
 
 
class GCPInfraProvisioner(BaseInfraProvisioner):
    """Corrected reference implementation — overrides
    _generate_saga_orchestrator (GCP Workflows YAML, not ASL) and
    provisions one IAM role per tier instead of one shared account."""
 
    def _define_network_topologies(self, tenant_id, config=None, **kwargs):
        return {
            'vpc_network_name': f'e2a-vpc-{tenant_id}',
            'vpc_service_perimeter': 'accessPolicies/e2a_policy/servicePerimeters/prod_perimeter',
        }
 
    def _define_edge_security(self, networks, config=None, **kwargs):
        return {
            'api_gateway_id': 'e2a-cloud-endpoints-gw',
            'cloud_armor_policy': 'securityPolicies/block-prompt-injection-rules',
        }
 
    def _define_data_perimeter_substrate(self, networks, tenant_id, tenancy_model, config=None, **kwargs):
        table_id = (f'projects/e2a-prod/databases/{tenant_id}-state-db' if tenancy_model == 'SILO'
                    else 'projects/e2a-prod/databases/shared-pool-state-db')
        return {
            'pubsub_intake_topic': 'projects/e2a-prod/topics/intake-topic',
            'pubsub_task_queue': 'projects/e2a-prod/topics/task-queue',
            'firestore_nosql_table': table_id,
        }
 
    def _define_compute_tiers(self, networks, data_meta, config=None, **kwargs):
        return {
            'tier1_validation_function': 'gcr.io/e2a-prod/ingress-validator:latest',
            'tier2_orchestrator_cloudrun': 'gcr.io/e2a-prod/orchestration-engine:latest',
            'tier4_worker_cloudrun': 'gcr.io/e2a-prod/async-workers:latest',
        }
 
    def _define_iam_governance_framework(self, compute, data_meta, config=None, **kwargs):
        # One binding per tier — a single shared service account would
        # collapse the Tier 1 publish-only / Tier 2+4 read-write
        # separation the Landing Zone doc's Section 3.2 depends on.
        return {
            'tier1_sa': 'e2a-validator-sa@e2a-prod.iam.gserviceaccount.com',
            'tier1_role': 'roles/pubsub.publisher',  # Intake Topic only
            'tier2_sa': 'e2a-orchestrator-sa@e2a-prod.iam.gserviceaccount.com',
            'tier2_role': 'roles/pubsub.editor,roles/datastore.user',
            'tier2_db_role': 'roles/datastore.viewer',  # read-only replica, if Read-Only Data pattern applies
            'tier4_sa': 'e2a-worker-sa@e2a-prod.iam.gserviceaccount.com',
            'tier4_role': 'roles/pubsub.subscriber,roles/datastore.user',
        }
 
    def _generate_saga_orchestrator(self, compute_ctx, config=None, **kwargs):
        # Overridden: GCP Workflows uses YAML, not Amazon States
        # Language — same logical shape as the AWS default, different syntax.
        return {
            'engine_type': 'gcp_workflows_yaml',
            'structural_skeleton': {
                'main': {
                    'steps': [
                        {'evaluate_failed_keys': {'switch': [
                            {'condition': '${len(failed_keys) > 0}', 'next': 'isolate_and_split_batch'},
                        ], 'next': 'standard_database_commit'}},
                        {'standard_database_commit': {'call': 'commit_all', 'next': 'dispatch_final_webhook'}},
                        {'isolate_and_split_batch': {'parallel': {'branches': [
                            {'commit_successful_items': {'call': 'commit_partial'}},
                            {'run_compensating_rollback': {'call': 'compensate'}},
                            {'route_to_dlq': {'call': 'dlq_publish'}},
                        ]}, 'next': 'dispatch_final_webhook'}},
                        {'dispatch_final_webhook': {'call': 'http.post', 'end': True}},
                    ],
                },
            },
        }
 
    def _apply_infrastructure_graph(self, final_manifest, config=None, **kwargs):
        return True  # gcloud SDK client or Terraform google provider apply
```

# 4. Usage Guide

## 4.1 Repo Structure

After placing e2a_base.py, the recommended repo structure is:

```python
repo-root/
├── src/
│   ├── e2a_base.py        # E2A scaffold — never modify
│   ├── validation/        # BaseValidationService subclasses — deploys
│   │                      # separately, ahead of agents/ and workflows/
│   ├── agents/            # Concrete agent classes
│   ├── workflows/         # Workflow implementations
│   ├── rag/               # RAG pipeline implementations
│   ├── tools/             # Tool service implementations (REST + MCP client)
│   │   ├── create_order_tool.py
│   │   └── check_stock_tool.py
│   └── mcp/               # BaseMCPServer subclasses — wrap tools/ as MCP
│       └── order_ops_mcp_server.py
├── tests/
├── .env
└── requirements.txt
```
└── requirements.txt

## 4.2 Creating an Inherited Class

Subclass the abstract base class and override only the protected methods relevant to your domain. The public method (run, execute, retrieve, serve) is never overridden — it is inherited and used directly.

```python
# src/agents/refund_agent.py
from src.e2a_base import BaseAgent
 
class RefundAgent(BaseAgent):
    agent_name = 'RefundAgent'
    # No _validate_state here — see OrderOpsValidationService
    # ._validate_refund_agent() in Section 3, which runs before
    # this class is ever instantiated.
 
    def _build_messages(self, state, config=None, **kwargs):
        template = kwargs.get('prompt_template', config.get(
            'prompt_template', 'Process refund for order {order_id}'))
        content = template.format(order_id=state['order_id'])
        return [{'role': 'system', 'content': 'You are a refund specialist.'},
                {'role': 'user', 'content': content}]
 
    def _apply_policy(self, state, config=None, **kwargs):
        limit = kwargs.get('refund_limit', config.get(
            'refund_limit', float(os.getenv('REFUND_LIMIT', 500.0))))
        if state.get('amount', 0) > limit:
            raise ValueError(f'Refund exceeds limit: {limit}')
 
    def _evaluate_output(self, response, state, config=None, **kwargs):
        return 1.0
```
return 1.0

## 4.3 Invoking the Public Entry Point

Every class has a single public method. Application code calls only this method. Protected methods are never called directly.

```python
# Pattern 1: Direct invocation
agent = RefundAgent()
state = {'order_id': 'ORD-123', 'amount': 150.0, 'tenant_id': 'acme'}
result = agent.run(state)
 
# Pattern 2: LangGraph node — same call, no wrapper needed
def refund_node(state: dict) -> dict:
    return RefundAgent().run(state, config=AGENT_CONFIG)
 
# Pattern 3: FastAPI endpoint — public method maps directly to endpoint
@router.post('/api/v1/refund', response_model=AgentState)
async def process_refund(request: RefundRequest, settings=Depends(get_settings)):
    return RefundAgent().run(state=request.to_state(), config=settings.agent_config)
 
# Pattern 4: MCP server, hosted behind a FastAPI route
mcp_server = OrderOpsMCPServer()
@router.post('/mcp')
async def mcp_endpoint(request: dict):
    return mcp_server.serve(request, config=settings.agent_config)
```
return mcp_server.serve(request, config=settings.agent_config)

## 4.4 Config and Environment Variable Integration

The resolution chain is consistent across all E2A classes, including BaseMCPServer and tool-call routing:

```python
# Resolution order (highest to lowest priority):
# 1. kwargs passed at call time
# 2. config dict passed at call time
# 3. Environment variable
# 4. Hardcoded default in scaffold
 
value = kwargs.get('my_param', config.get(
    'my_param', type_cast(os.getenv('MY_PARAM', 'default'))))
```
'my_param', type_cast(os.getenv('MY_PARAM', 'default'))))

## 4.5 Guidance: Which Methods to Override

| **Protected Method**                                  | **Override?**       | **When and why**                                                                                     | **Default**                     |
|-------------------------------------------------------|---------------------|------------------------------------------------------------------------------------------------------|---------------------------------|
| _validate_<agent_name> (on BaseValidationService) | Required, per agent | One per agent, on the validation subclass — not on BaseAgent. Runs before the agent is instantiated. | no default — must be registered |
| _build_messages                                      | Yes                 | Always: every agent needs a domain-specific prompt.                                                  | pass — no messages              |
| _apply_policy                                        | If governed         | Domain-specific approval gates or a tool call via _resolve_tool_call().                             | pass — no policy                |
| _evaluate_output                                     | Yes                 | Always: define what 'quality' means for this agent.                                                  | pass — returns None             |
| wrap_api_as_tool calls                                | As needed           | Once per existing API you want discoverable/callable over MCP.                                       | no tools registered             |
| _list_tools / _call_tool                            | Rarely              | Only to filter tool visibility per tenant or add pre/post-processing.                                | derives from registered_tools   |

## 4.6 Multi-Class End-to-End Example

This example shows the full request path: BaseValidationService gates the request first — the agent is never instantiated on a validation failure — then an OrderOpsAgent grounds its response via a RAG pipeline and calls a tool through _resolve_tool_call(). One tool (check_stock) has been wrapped and hosted as an MCP server; the other (legacy_pricing_lookup) has not — so the same call site transparently uses MCP for the first and REST for the second, with no branching in the agent code.

```python
# src/validation/order_ops_validation_service.py — runs first,
# typically as its own Lambda/Cloud Function ahead of the agent tier
from src.e2a_base import BaseValidationService
 
class OrderOpsValidationService(BaseValidationService):
    def __init__(self):
        self.validator_registry = {'OrderOpsAgent': self._validate_order_ops_agent}
 
    def _validate_order_ops_agent(self, state, config=None, **kwargs):
        errors = [f'{f} is required' for f in ('query', 'user_id', 'intent')
                  if f not in state]
        return (len(errors) == 0, errors)
 
# --- edge handler (Lambda/Cloud Function) ---
def validation_handler(event, context):
    result = OrderOpsValidationService().validate('OrderOpsAgent', event['state'])
    if not result['valid']:
        return {'statusCode': 400, 'body': result}   # BaseWorkflow/BaseAgent never invoked
    return invoke_agent_service(event['state'], correlation_id=result['correlation_id'])
 
# src/mcp/order_ops_mcp_server.py — wrap an existing API as MCP
from src.e2a_base import BaseMCPServer
from src.tools.check_stock_tool import CheckStockTool
 
class OrderOpsMCPServer(BaseMCPServer):
    server_name = 'order-ops-mcp'
 
    def __init__(self):
        super().__init__()
        self.wrap_api_as_tool(
            handler=CheckStockTool(),
            tool_name='check_stock',
            description='Check on-hand inventory for a SKU',
            input_schema={'type': 'object',
                           'properties': {'sku': {'type': 'string'}},
                           'required': ['sku']},
        )
 
# src/agents/order_ops_agent.py
from src.e2a_base import BaseAgent, _resolve_tool_call
import asyncio
 
class OrderOpsAgent(BaseAgent):
    agent_name = 'OrderOpsAgent'
    # State is already validated by OrderOpsValidationService above —
    # this class assumes a well-formed state and gets straight to work.
 
    def _build_messages(self, state, config=None, **kwargs):
        return [{'role': 'user', 'content': state['query']}]
 
    def _apply_policy(self, state, config=None, tenant_id=None, **kwargs):
        if state.get('action') == 'check_stock':
            state['tool_result'] = asyncio.run(_resolve_tool_call(
                'check_stock', {'sku': state['sku']}, config,
                tenant_id=tenant_id))
        elif state.get('action') == 'legacy_pricing_lookup':
            state['tool_result'] = asyncio.run(_resolve_tool_call(
                'legacy_pricing_lookup', {'sku': state['sku']}, config,
                tenant_id=tenant_id))
 
    def _evaluate_output(self, response, state, config=None, **kwargs):
        return 1.0 if response.get('text') else 0.0
 
# config['mcp_server_registry'] — the only thing that determines
# which tools route through MCP; everything else falls back to REST:
# {
#     'check_stock': {
#         'mcp_server_url': 'https://order-ops-mcp.internal/mcp',
#         'mcp_tool_name': 'check_stock',
#     },
#     # 'legacy_pricing_lookup' is absent -> _resolve_tool_call falls
#     # back to _get_tool_service() -> RestToolService, unchanged.
# }
```
# }

## 4.7 Composing BasePromptRegistry, BaseInfraProvisioner, and BasePipeline

Three more collaborators an agent or a deploy pipeline typically composes with:

```python
# src/agents/order_ops_agent.py — same class as Section 4.6, with the
# hardcoded prompt swapped for a registry-backed one
class OrderOpsAgent(BaseAgent):
    agent_name = 'OrderOpsAgent'
    prompt_id = 'order-ops-system-prompt'
 
    def __init__(self, prompt_registry: BasePromptRegistry):
        self.prompt_registry = prompt_registry  # composed in, not inherited
 
    def _build_messages(self, state, config=None, **kwargs):
        template = self.prompt_registry.get_prompt(
            prompt_id=self.prompt_id,
            version=kwargs.get('io_config', {}).get('prompt_version', 'latest'),
            tenant_id=kwargs.get('tenant_id'), config=config)
        return [{'role': 'user', 'content': template.render(query=state['query'])}]
```
return [{'role': 'user', 'content': template.render(query=state['query'])}]

```python
# deploy/provision.py — CI/CD entry point, one call per tenant/environment
from src.infra.gcp_provisioner import GCPInfraProvisioner
 
result = GCPInfraProvisioner().provision_landing_zone(
    tenant_id='acme', tenancy_model='POOL',
    config={'region': 'us-central1'})
 
if result['status'] != 'PROVISIONED':
    raise SystemExit(f"Provisioning failed: {result.get('error_trace')}")
 
# deploy/ci_pipeline.py — CI/CD entry point, one call per commit
from src.cicd.gcp_pipeline import GCPPipeline
 
report = GCPPipeline().run_pipeline(config={'deployment_strategy': 'CANARY'})
if report['status'] != 'SUCCESS':
    raise SystemExit(f"Pipeline failed at: {report['stages']}")
```
raise SystemExit(f"Pipeline failed at: {report['stages']}")

Both provision_landing_zone() and run_pipeline() are control-plane calls, run once per environment or per commit from CI/CD (Landing Zone Section 9) — neither ever appears inside a BaseWorkflow.execute() or BaseAgent.run() call chain.

## 4.8 Four-Way Agent Orchestration — RAG, MCP Tool Call, API Tool Call, and LLM-Only

LLMOnlyAgent ships in the scaffold (Section 3) as an abstract peer to BaseAgent, BaseRAGPipeline, and BaseToolService — for intents that need only the model itself: no tool call, no retrieval. It is not a BaseAgent subclass, the same way BaseRAGPipeline isn't one either. Its run() has the same public shape as BaseAgent.run() — propagation fields in, confidence-checked response out — but dispatches by modality to four separate abstract handlers (_handle_text, _handle_audio, _handle_image, _handle_document) instead of one _build_messages(); _select_model() and _llm_call() are concrete, framework-owned helpers on the class itself, following the same registry-resolution pattern as _get_tool_service().

A reference implementation (GenericLLMOnlyAgent) and the other three peer agents — RAGAgent, MCPToolAgent, APIToolAgent, plus FallbackAgent — live in src/workflows/multimodal_orchestrator.py, the same file-placement convention RefundAgent uses (src/agents/refund_agent.py, Section 4.1): generic and reusable, but downstream of the never-modify scaffold, not part of it. _get_agent() resolves every intent to one of these four peers through the same agent_registry lookup used in Section 4.6 — no new abstract method, no new hook. When the semantic router (Section 2.15) matches none of the four, or scores the match below min_confidence, _get_agent() returns a fifth agent, FallbackAgent, whose _evaluate_output() always returns 0.0 — which forces BaseAgent.run()'s own confidence check (Section 2.3) to call _fallback() immediately. No change to run() or execute() was needed to add this path.

```python
# src/workflows/multimodal_orchestrator.py — imports from src.e2a_base
self.agent_registry = {
    'RAG_GROUNDED':  RAGAgent(rag_pipeline),
    'MCP_TOOL_CALL': MCPToolAgent(),
    'API_TOOL_CALL': APIToolAgent(),
    'LLM_ONLY':      GenericLLMOnlyAgent(),   # concrete subclass of
}                                          # LLMOnlyAgent (Section 3),
self.fallback_agent = FallbackAgent()     # not of BaseAgent

def _get_agent(self, intent, config=None, **kwargs):
    agent_registry = kwargs.get('agent_registry', self.agent_registry)
    score = kwargs.get('intent_score', 1.0)
    if intent not in agent_registry or score < self.min_confidence:
        return self.fallback_agent
    return agent_registry[intent]
```
return agent_registry[intent]

GenericLLMOnlyAgent implements the four abstract handlers by composing a ModalityPreprocessor (audio transcription, document extraction) and calling the inherited _llm_call() with the modality-resolved model:

```python
class GenericLLMOnlyAgent(LLMOnlyAgent):
    def __init__(self, modality_preprocessor=None):
        self.modality_preprocessor = modality_preprocessor or ModalityPreprocessor()

    def _detect_modality(self, state, config=None, **kwargs) -> str:
        for key, modality in (('audio_uri', 'AUDIO'), ('image_uri', 'IMAGE'),
                              ('document_uri', 'DOCUMENT')):
            if state.get(key):
                return modality
        return 'TEXT'

    def _handle_audio(self, state, config=None, **kwargs):
        transcript = self.modality_preprocessor.transcribe(state['audio_uri'], config, **kwargs)
        return self._llm_call([{'role': 'user', 'content': transcript}], 'AUDIO', config, **kwargs)

    def _handle_image(self, state, config=None, **kwargs):
        # vision-capable model takes the reference directly
        return self._llm_call([{'role': 'user', 'content': state['image_uri']}], 'IMAGE', config, **kwargs)

    # _handle_text() and _handle_document() follow the same shape —
    # full source in src/workflows/multimodal_orchestrator.py
```
# full source in src/workflows/multimodal_orchestrator.py

_build_workflow() (Section 2.4) constructs the LangGraph StateGraph that _validate_workflow() checks for required_nodes before execute() ever calls _get_agent() — one node per peer agent, plus the fallback node, wired with the same routing rule _get_agent() applies at runtime, so the two never drift apart:

```python
def _build_workflow(self, config=None, **kwargs):
    graph = StateGraph(OrchestratorState)
    graph.add_node('classify_intent', self._classify_intent_node)
    graph.add_node('rag_agent', self._agent_node('RAG_GROUNDED'))
    graph.add_node('mcp_tool_agent', self._agent_node('MCP_TOOL_CALL'))
    graph.add_node('api_tool_agent', self._agent_node('API_TOOL_CALL'))
    graph.add_node('llm_only_agent', self._agent_node('LLM_ONLY'))
    graph.add_node('fallback', self._fallback_node)
    graph.set_entry_point('classify_intent')
    graph.add_conditional_edges('classify_intent', self._route_intent, {
        'RAG_GROUNDED':  'rag_agent',
        'MCP_TOOL_CALL': 'mcp_tool_agent',
        'API_TOOL_CALL': 'api_tool_agent',
        'LLM_ONLY':      'llm_only_agent',
        'NONE':          'fallback',  # no match, or score < min_confidence
    })
    for node in ('rag_agent', 'mcp_tool_agent', 'api_tool_agent', 'llm_only_agent'):
        graph.add_edge(node, END)
    graph.add_edge('fallback', END)
    return graph.compile()
```
return graph.compile()

Transcription and extraction — like tool calls (Section 2.6) — run through the Logical-vs-Physical rule: an empty or low-confidence result from modality_preprocessor is not treated as ground truth, and should route straight to _fallback() rather than reach the model as if it were complete. On the Landing Zone side (Landing Zone Section 3.1.4), the LLM-only path skips the Integration Tier and Knowledge Tier hops entirely, and its preprocessing step runs as Tier 2 compute alongside BaseAgent (Landing Zone Section 8.3) — never as a public-facing service, for the same reason the Semantic Router and Semantic Firewall do (Section 2.15.2). The fallback path needs no infrastructure of its own — FallbackAgent runs in-process, in whichever tier the workflow was already executing in.

New config keys, added to the Global Config / Environment Reference below: model_capability_registry, stt_provider, document_extractor, max_audio_duration_seconds, and max_document_pages — the last two enforced before a transcription or extraction job starts, the same NFR-violation pattern max_latency already uses.

# 5. Global Config / Environment Reference

One consolidated table for every config key used anywhere in the framework, replacing the separate per-addendum tables carried in earlier revisions.

| **config key**             | **Environment variable**                    | **Default**                   | **Used in**                                                                                                                        |
|----------------------------|---------------------------------------------|-------------------------------|------------------------------------------------------------------------------------------------------------------------------------|
| schema_registry            | VALIDATION_SCHEMA_REGISTRY_PATH             | {}                            | BaseValidationService validators                                                                                                   |
| strict_mode                | VALIDATION_STRICT_MODE                      | True                          | BaseValidationService.validate()                                                                                                   |
| min_confidence             | MIN_CONFIDENCE                              | 0.85                          | BaseAgent.run()                                                                                                                    |
| max_latency                | MAX_LATENCY                                 | 2.0 (5.0 for BaseToolService) | BaseAgent.run(), BaseRAGPipeline.retrieve(), BaseToolService.execute() — latency SLO check, raises NFRViolationError on breach     |
| model_id                   | LLM_MODEL_ID                                | 'default'                     | BaseAgent.__llm_call()                                                                                                           |
| io_config_prefix           | IO_CONFIG_PREFIX                            | 'io_'                        | _resolve_io_config()                                                                                                              |
| idempotency_store_url      | IDEMPOTENCY_STORE_URL                       | None                          | BaseWorkflow.__resolve_idempotency()                                                                                             |
| idempotency_ttl_seconds    | IDEMPOTENCY_TTL_SECONDS                     | 86400                         | __lookup_idempotency_store()                                                                                                     |
| dlq_queue_url              | DLQ_QUEUE_URL                               | None                          | _send_to_dlq()                                                                                                                    |
| log_sink                   | LOG_SINK                                    | 'stdout'                      | BaseObservability._ship_logs() (subclass-defined sink; Section 2.10) — retired from the old framework-owned _flush_logs() helper |
| commit_gate_enabled        | COMMIT_GATE_ENABLED                         | True                          | BaseToolService._commit_gate()                                                                                                    |
| tenant_routing_map         | TENANT_ROUTING_MAP                          | {}                            | get_endpoint() — per-tenant overrides                                                                                              |
| faithfulness_threshold     | FAITHFULNESS_THRESHOLD                      | 0.85                          | BaseRAGPipeline._evaluate_answer()                                                                                                |
| rag_top_k                  | RAG_TOP_K                                   | 5                             | BaseRAGPipeline.retrieve()                                                                                                         |
| embedding_model            | EMBEDDING_MODEL                             | 'amazon.titan-embed-text-v1'  | BaseRAGPipeline.__embed()                                                                                                        |
| timeout                    | TOOL_TIMEOUT                                | 5.0                           | BaseToolService.__http_post()                                                                                                    |
| retries                    | TOOL_RETRIES                                | 3                             | BaseToolService.execute()                                                                                                          |
| tool_base_url              | TOOL_BASE_URL                               | 'http://localhost'            | RestToolService.get_endpoint()                                                                                                     |
| auth_token                 | TOOL_AUTH_TOKEN                             | None                          | BaseToolService.__http_post()                                                                                                    |
| mcp_server_url             | MCP_SERVER_URL                              | None                          | MCPToolService.get_endpoint()                                                                                                      |
| mcp_protocol_version       | MCP_PROTOCOL_VERSION                        | '2025-06-18'                  | BaseMCPServer.serve()                                                                                                              |
| mcp_server_name            | MCP_SERVER_NAME                             | 'e2a-mcp-server'              | BaseMCPServer — logging identity                                                                                                   |
| expected_version           | —                                           | None                          | _commit_gate() — optional OCC version check, caller-supplied per call                                                             |
| cache_refresh_token        | —                                           | 'v1'                          | BaseAgent._build_messages() — ephemeral context caching, mutate to force a cache miss                                             |
| speculative_model          | SPECULATIVE_MODEL                           | None                          | BaseAgent.__llm_call() io_config — speculative decoding draft model                                                              |
| similarity_metric          | SIMILARITY_METRIC                           | 'COSINE'                      | BaseRAGPipeline._search_index() io_config                                                                                         |
| shadow_mode_enabled        | SHADOW_MODE_ENABLED                         | False                         | BaseWorkflow.execute() — Blue-Green/shadow traffic trials                                                                          |
| shadow_target_pipeline     | SHADOW_TARGET_PIPELINE                      | None                          | BaseWorkflow.execute() — which pipeline the shadow copy runs against                                                               |
| prompt_store_url           | PROMPT_STORE_URL                            | None                          | BasePromptRegistry.get_prompt()                                                                                                    |
| default_version            | PROMPT_DEFAULT_VERSION                      | 'latest'                      | BasePromptRegistry.get_prompt()                                                                                                    |
| deployment_strategy        | DEPLOYMENT_STRATEGY                         | 'BLUE_GREEN'                  | BasePipeline._execute_deployment_rollout()                                                                                        |
| mcp_server_registry        | MCP_SERVER_REGISTRY_PATH                    | {}                            | _resolve_tool_call() — checked first                                                                                              |
| tool_registry              | TOOL_REGISTRY_PATH                          | {}                            | _get_tool_service() — fallback path                                                                                               |
| default_transport          | DEFAULT_TOOL_TRANSPORT                      | 'http'                        | _get_tool_service() — final fallback                                                                                              |
| observability_engine       | — (object reference, not env-resolvable)    | None                          | BaseWorkflow.execute() — selects the BaseObservability instance for record_telemetry() (Section 2.10)                              |
| governance_engine          | — (object reference, not env-resolvable)    | None                          | BaseAgent.run() — selects the BaseGovernanceFramework instance for enforce_governance_gate() (Section 2.11)                        |
| max_token_budget           | — (io_config key, not top-level config/env) | inf (unlimited)               | BaseGovernanceFramework.__enforce_token_budget() (Section 2.11)                                                                  |
| model_capability_registry  | MODEL_CAPABILITY_REGISTRY_PATH              | {}                            | LLMOnlyAgent._select_model()                                                                                                      |
| stt_provider               | STT_PROVIDER                                | None                          | ModalityPreprocessor.transcribe()                                                                                                  |
| document_extractor         | DOCUMENT_EXTRACTOR_PROVIDER                 | None                          | ModalityPreprocessor.extract()                                                                                                     |
| max_audio_duration_seconds | MAX_AUDIO_DURATION_SECONDS                  | 600                           | checked before transcribe() — NFRViolationError on breach                                                                          |
| max_document_pages         | MAX_DOCUMENT_PAGES                          | 50                            | checked before extract() — NFRViolationError on breach                                                                             |

*E2A Architecture Framework — Implementation Playbook · github.com/subhamviky/e2a-framework · Subham Gupta*

*Authorship: Framework implementation, abstract class contracts, and multi-cloud mapping logic are the original work of Subham Gupta.*

*Trademark: All vendor trademarks (AWS, GCP, Azure, Meta Llama) are the property of their respective owners and are used here for architectural reference only.*

*Managed Service Disclaimer: Certain ecosystem components referenced (e.g., Pinecone, LangGraph, Lakera, FAISS) are third-party partner technologies and are not native managed services of AWS, Google Cloud, or Microsoft Azure. The E2A Framework provides a standardized way to integrate these third-party tools alongside native cloud services.*
