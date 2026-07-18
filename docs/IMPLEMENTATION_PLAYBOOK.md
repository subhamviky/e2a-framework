# Enterprise-to-Agentic (E2A)

### Architecture Framework

*Implementation Playbook — Scaffold File, Class Contracts & Usage Guide*

> **Framework reference**
>
> Repository: github.com/subhamviky/e2a-framework  ·  Reference implementation: github.com/subhamviky/order-to-cash-agentic-ai  ·  Scaffold file: e2a_base.py — drop into any repo, inherit, override, run.  ·  Document role: single, consolidated playbook. The Master Abstraction Reference defines WHAT the framework enforces; this Playbook defines HOW to implement it — including MCP server hosting and cross-class propagation as core, first-class content rather than addenda.


# 1. Purpose & Scope


This playbook provides everything a developer needs to implement the E2A Architecture Framework in a new or existing repository: the complete, runnable scaffold file (e2a_base.py), method signature specifications for all primary abstract classes, configuration and environment variable resolution patterns, and a full worked example from inheritance through execution.

This edition folds two sets of content that earlier revisions carried as separate addenda directly into the main body, so there is a single set of class contracts to read rather than a base version plus patches:

- MCP is now native to the Tool Services layer. Alongside BaseToolService (the existing client-side tool caller, which can speak REST or MCP transport via RestToolService / MCPToolService), the framework defines BaseMCPServer — a class whose job is to turn existing wrapped APIs into a spec-compliant MCP server exposing tools/list and tools/call — plus a single tool-call routing function that checks whether a wrapped MCP server exists for a given tool before falling back to the normal REST tool call.

- The cross-class propagation contract (correlation_id, io_config, idempotency_key, tenant_id, message_log, failed_keys) is written directly into the constructor and method tables of BaseWorkflow, BaseAgent, BaseRAGPipeline, BaseToolService, and BaseMCPServer. There is no separate reconciliation step — the tables below are the current, single source of truth.

- State validation is no longer a method each BaseAgent subclass implements on itself. It is now a separate class, BaseValidationService (Section 2.2), invoked before BaseWorkflow or BaseAgent is instantiated at all — so an invalid request never pays for agent-service compute. BaseAgent's contract no longer includes _validate_state.

- BaseObservability and BaseGovernanceFramework (Section 2.9) are fully-specified template classes wired directly into the request path, not disconnected interface stubs to implement separately. BaseWorkflow.execute() hands its message_log to BaseObservability.record_telemetry() on every request; BaseAgent.run() calls BaseGovernanceFramework.enforce_governance_gate() ahead of _apply_policy() whenever a governance engine is configured. Both fall back to the framework's original default behavior if the corresponding engine isn't set in config, so existing subclasses that don't configure either continue to work unchanged.

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

| Direction | Field | Resolved / Mutated | Purpose |
| --- | --- | --- | --- |
| INPUT | correlation_id: str | Generated once in BaseWorkflow.execute() (uuid4, or client-supplied). Read-only below that point. | Ties every log line and DLQ entry across all classes back to one logical request. |
| INPUT | io_config: dict | Resolved once in BaseWorkflow.execute() from config['io'] (or IO_CONFIG env namespace); a validated subset of config. | Separates connection/endpoint concerns (DB, S3, queue, vector store, MCP server URLs) from behavioral NFR config. |
| INPUT | idempotency_key: str | Resolved once via __resolve_idempotency() — client-supplied and validated, or generated. | Lets BaseToolService, BaseMCPServer, and any commit/save path detect and skip a request that already completed. |
| INPUT | tenant_id: str | Read from state['tenant_id']; validated present in BaseWorkflow.execute() before routing. | Scopes RAG index search, tool endpoint resolution, and MCP server/tool registry lookups to a tenant namespace. |
| OUTPUT | message_log: list[dict] | Empty list created in BaseWorkflow.execute(); every method appends structured entries via the shared __log() helper. | One ordered, structured log for the entire request, flushed once at the end instead of scattered per-class log lines. |
| OUTPUT | failed_keys: list[str] | Empty list created in BaseWorkflow.execute(); any _handle_error() across any class appends the failing item's key. | Drives the commit gate and end-of-request DLQ dispatch — failed items are isolated, successful items still commit. |


### 2.1.1 AgentState Schema — Additions


state (the dict threaded through every BaseAgent method) carries three required keys, using the same mechanism trace_id already used before correlation_id replaced it.

| Kind | Key | Resolution | Purpose |
| --- | --- | --- | --- |
| STATE | tenant_id: str | Required. Set by the API boundary before BaseWorkflow.execute() is called. | Read by BaseRAGPipeline._search_index() and BaseToolService/BaseMCPServer for tenant-scoped filtering. |
| STATE | correlation_id: str | Required after BaseWorkflow.execute() runs. Mirrors the correlation_id parameter. | Convenience accessor for any code that only has state, not the full call signature. |
| STATE | idempotency_key: str | Required after BaseWorkflow.execute() runs. | Same rationale — available on state for handlers that don't thread the full parameter list. |


## 2.2 BaseValidationService


*validation/ — validate() is the single public entry point. Runs before any agent, workflow, RAG, tool, or MCP class is invoked — and before any of them are even instantiated.*

Every prior revision of this playbook had each BaseAgent subclass validate its own state via _validate_state(), as the first step inside run(). That means an invalid request still pays for a full agent-service invocation (container already warm or cold-started, full class instantiated) before being rejected. BaseValidationService moves that check in front of the agent entirely: one small, cheap function validates the request and either forwards it or rejects it before the compute that runs BaseWorkflow/BaseAgent is ever triggered.

> **Fail-Fast Placement**
>
> BaseValidationService is designed to run in a separate, smaller compute tier from BaseWorkflow/BaseAgent — typically a Lambda or Cloud Function in front of a Fargate/Cloud Run service. On a validation failure, the caller returns immediately; the downstream agent container is never invoked. This is a cost and latency control, not just a code-organization change — see Section 3 of the companion Cloud Landing Zone HLD/LLD document for the deployment topology this enables.


### Class Variables


| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| validator_registry | Dict[str, Callable] | {} | Populated by the concrete subclass. Maps agent_name -> the bound _validate_<agent_name> method that validates state for that agent. |


### Method Contract Table


| Access | Signature | Config / kwargs keys | Purpose |
| --- | --- | --- | --- |
| PUBLIC | validate(agent_name, state, config=None, correlation_id=None, **kwargs) -> ValidationResult | schema_registry, strict_mode | Template method — the single entry point. Originates correlation_id if not supplied (this is now the earliest point in the request chain, ahead of BaseWorkflow). Resolves the correct validator via _resolve_validator(), runs it, and returns a structured result. Raises nothing — a failed validation is a normal, well-formed return value, not an exception. |
| PROTECTED | _resolve_validator(agent_name, config=None, **kwargs) -> Callable | validator_registry | Looks up agent_name in validator_registry and returns the bound _validate_<agent_name> method. Same resolution pattern as BaseWorkflow._get_agent() and _get_tool_service() — a name-keyed registry, not a chain of if/elif. |
| ABSTRACT (per agent) | _validate_<agent_name>(state, config=None, **kwargs) -> ValidationResult | schema, required_fields | One method per agent, implemented on the concrete subclass. Same signature and shape as the old per-agent _validate_state(), just relocated. Returns ValidationResult(valid, errors) rather than a bare bool, so a failure can carry field-level detail back to the caller. |
| PRIVATE | __build_result(valid, errors, agent_name, correlation_id, **kwargs) -> ValidationResult | — | Framework-owned. Shapes a consistent {valid, errors, agent_name, correlation_id} object regardless of which _validate_<agent_name> produced it. |


### Config / Environment Resolution — BaseValidationService


| config key | Environment variable | Default | Used in step |
| --- | --- | --- | --- |
| schema_registry | VALIDATION_SCHEMA_REGISTRY_PATH | {} | _resolve_validator() / per-agent validators — JSON-schema source per agent_name |
| strict_mode | VALIDATION_STRICT_MODE | True | validate() — if False, unknown agent_name falls through to a permissive default instead of a hard reject |


### What Changes Downstream


- BaseAgent no longer declares _validate_state — it is removed from the contract entirely (Section 2.3). BaseAgent.run() assumes it is only ever called with state that already passed validate().

- correlation_id now originates in BaseValidationService.validate(), not BaseWorkflow.execute(). BaseWorkflow.execute() still accepts correlation_id as a parameter and still falls back to generating one if called standalone (e.g. in a unit test that skips the validation gate), but in the normal request path it receives one already minted.

- A validation failure is a terminal, cheap response (HTTP 400 with the ValidationResult body) returned from the validation tier. BaseWorkflow, BaseAgent, and everything behind them are never instantiated for that request.


## 2.3 BaseAgent


*agents/ — run() is the single public entry point*


### Constructor


| Parameter | Type | Default | Required | Description |
| --- | --- | --- | --- | --- |
| state | Dict[str, Any] | — | Yes | Agent state object. Must contain at minimum: query (str), intent (str), tenant_id (str). |
| config | Dict[str, Any] | None | No | Runtime configuration dict. Falls back to environment variables then hardcoded defaults. |
| correlation_id, io_config, idempotency_key, tenant_id, message_log, failed_keys | see 2.1 | None / [] | No | Propagation fields. Received from BaseWorkflow when run() is called through the normal chain; self-originated as a fallback so BaseAgent remains independently unit-testable. |
| **kwargs | Any | — | No | Optional per-call overrides. Highest priority in the resolution chain. |


### Method Contract Table


| Access | Signature | Config / kwargs keys | Purpose |
| --- | --- | --- | --- |
| PUBLIC | run(state, config=None, correlation_id=None, io_config=None, idempotency_key=None, tenant_id=None, message_log=None, failed_keys=None, **kwargs) -> Dict | any config key; any kwarg | Template method. Calls all protected methods in fixed sequence. Receives and forwards propagation fields; does not re-generate correlation_id if one is supplied. Assumes state has already passed BaseValidationService.validate() (Section 2.2) — run() no longer validates its own input. If config['governance_engine'] is set, calls BaseGovernanceFramework.enforce_governance_gate() (Section 2.9.2) in place of _apply_policy(); otherwise calls _apply_policy() directly, unchanged. |
| PROTECTED | _build_messages(state, config, **common, **kwargs) -> list | prompt_template, role | Construct [{role, content}] message list for LLM call. |
| PROTECTED | _apply_policy(state, config, **common, **kwargs) -> None | retry_policy, circuit_breaker_config | Enforce governance: retries, circuit breakers, approval gates, redaction. This is also where a tool call is triggered via _resolve_tool_call() (Section 2.8). Only called when no config['governance_engine'] is configured — see 2.9.2 for the alternate path. |
| PRIVATE | __llm_call(messages, config, **kwargs) -> dict | model_id, max_tokens, temperature | Invoke LLM provider. Returns {text, metadata}. Provider-agnostic, routed internally. |
| PROTECTED | _evaluate_output(response, state, config, **common, **kwargs) -> float | min_confidence, groundedness_threshold | Evaluate output quality, return [0.0–1.0]. |
| PROTECTED | _fallback(state, config, **common, **kwargs) -> dict | fallback_model, cache_key | Fallback strategy when confidence is below threshold. |
| PROTECTED | _handle_error(error, state, config, correlation_id, message_log, failed_keys, **kwargs) -> None | alert_channel, retry_policy | Error handling. Appends the failing key to failed_keys instead of raising past the request boundary. |


### Config / Environment Resolution — BaseAgent


| config key | Environment variable | Default | Used in step |
| --- | --- | --- | --- |
| min_confidence | MIN_CONFIDENCE | 0.85 | run() — fallback threshold |
| model_id | LLM_MODEL_ID | 'default' | __llm_call() — provider routing |
| max_latency | MAX_LATENCY | 2.0 | _apply_policy() — SLO enforcement |


## 2.4 BaseWorkflow


*orchestration/ — execute() is the single public entry point; origin point for io_config, idempotency_key, message_log, and failed_keys. correlation_id is now typically received from BaseValidationService (Section 2.2) rather than originated here.*


### Method Contract Table


| Access | Signature | Config / kwargs keys | Purpose |
| --- | --- | --- | --- |
| PUBLIC | execute(state, config=None, correlation_id=None, **kwargs) -> Dict | workflow_id, routing_strategy | Public entry point. Validates tenant_id, accepts correlation_id from the caller (BaseValidationService, normally) or originates one as a standalone fallback, resolves io_config/idempotency_key, builds and validates the workflow graph, resolves and invokes the target agent, hands message_log to config['observability_engine'].record_telemetry() if one is configured (falls back to the base log flush otherwise — see Section 2.9.1), and dispatches failed_keys to the DLQ. A DehydrationInterrupt raised anywhere downstream is caught separately from a generic exception: it sets state['status'] = 'AWAITING_WEBHOOK' and skips the failed_keys/DLQ path entirely, since it's a scheduled pause, not a failure. |
| PROTECTED | _build_workflow(config, **common, **kwargs) -> Any | workflow_definition | Construct the LangGraph graph (or equivalent) of agent nodes. |
| PROTECTED | _validate_workflow(workflow, config, **common, **kwargs) -> bool | required_nodes | Validate the graph is well-formed before execution. |
| PROTECTED | _get_agent(intent, config, **common, **kwargs) -> BaseAgent | agent_registry | Resolve which BaseAgent subclass handles a given intent. |
| PROTECTED | _resolve_io_config(config, **kwargs) -> dict | io_config namespace | Extract the IO_CONFIG-prefixed subset of config, separate from behavioral NFR config. |
| PROTECTED | _generate_idempotency_key(state, config, **kwargs) -> str | idempotency_strategy, business_key_fields | Abstract — subclass supplies domain logic, e.g. hash(tenant_id + REF_ELEM_KEY). Called only when no valid client-supplied key exists. |
| PRIVATE | __resolve_idempotency(state, config, **kwargs) -> str | idempotency_ttl_seconds, idempotency_store_url | Base-owned. Checks for a client-supplied key, validates and looks it up in the idempotency store; returns the existing key if a matching non-expired entry exists (replay), otherwise mints and persists a new one via _generate_idempotency_key(). |
| PRIVATE | __log / __flush_logs(message_log, config) | log_sink | Base-owned structured logging, shared by every class. See Section 3 for the shared implementation. |
| PRIVATE | __send_to_dlq(failed_keys, config) | dlq_queue_url | Base-owned. Dispatches failed_keys to SQS/Pub-Sub/Service Bus at end of request. |


## 2.5 BaseRAGPipeline


*rag/ — retrieve() is the single public entry point*


### Method Contract Table


| Access | Signature | Config / kwargs keys | Purpose |
| --- | --- | --- | --- |
| PUBLIC | retrieve(query, config=None, tenant_id=None, **kwargs) -> list | top_k, embedding_model | Template method. Embeds query, searches the tenant-scoped index, evaluates groundedness, returns ranked context. |
| PROTECTED | _search_index(query_vector, config, tenant_id=None, **kwargs) -> list[dict] | index_name, tenant_index_prefix | Namespaces the vector search to f'{tenant_index_prefix}_{tenant_id}' or an equivalent metadata filter — prevents one tenant's retrieve() from ever surfacing another tenant's documents. |
| PROTECTED | _rerank(results, config, **kwargs) -> list | rerank_top_n | Re-rank candidate documents before returning. |
| PROTECTED | _evaluate_answer(answer, config, **kwargs) -> float | groundedness_threshold | Evaluate answer quality (RAGAS faithfulness gate). |


### Config / Environment Resolution — BaseRAGPipeline


| config key | Environment variable | Default | Used in step |
| --- | --- | --- | --- |
| faithfulness_threshold | FAITHFULNESS_THRESHOLD | 0.85 | _evaluate_answer() — RAGAS gate |
| rag_top_k | RAG_TOP_K | 5 | retrieve() — result count |
| embedding_model | EMBEDDING_MODEL | 'amazon.titan-embed-text-v1' | retrieve() — embedding provider |


## 2.6 BaseToolService


*tools/ — execute() is the single public entry point (async). Client-side tool caller: REST or MCP transport via RestToolService / MCPToolService.*


### Method Contract Table


| Access | Signature | Config / kwargs keys | Purpose |
| --- | --- | --- | --- |
| PUBLIC | async execute(payload, config=None, tenant_id=None, **kwargs) -> dict | timeout, retries, auth_token | Template method. Validates → resolves endpoint → builds payload → checks the commit gate → HTTP POST. Returns result dict. |
| PROTECTED | get_endpoint(config, tenant_id=None, **kwargs) -> str | base_url, service_name, tenant_routing_map | Return the endpoint URL for this tool. Optionally resolves a tenant-specific base URL via config['tenant_routing_map'][tenant_id], falling back to the shared base_url. |
| PROTECTED | _build_payload(payload, config, **kwargs) -> dict | schema, enrichments | Transform input dict to the tool's request schema. |
| PROTECTED | _validate_input(payload, config, **kwargs) -> bool | schema, required_fields | Domain-specific validation. Return False to raise ValueError in execute(). |
| PRIVATE | async __http_post(endpoint, body, config, **kwargs) -> dict | timeout, retries, auth_token | Perform HTTP POST. Handles retries and timeout internally. Never overridden. |
| PRIVATE | __commit_gate(key, failed_keys, config, **kwargs) -> bool | commit_gate_enabled | Base-owned. Called immediately before any save/commit/write. Returns False if key is already present in failed_keys — the item is excluded from commit and left for DLQ investigation while sibling items proceed. |


### 2.6.1 RestToolService and MCPToolService — Transport Implementations


MCP's Streamable HTTP transport is JSON-RPC framed over HTTP POST. An MCP-backed tool call therefore fits the existing BaseToolService.execute() template — validate → get_endpoint → build_payload → __http_post — without touching the private, framework-owned __http_post method. Only get_endpoint() and _build_payload() differ from a REST tool.

| Class | Access | Signature | Purpose |
| --- | --- | --- | --- |
| RestToolService | PROTECTED | get_endpoint(config, **kwargs) -> str | Existing pattern, unchanged: return config['tool_base_url']. |
| RestToolService | PROTECTED | _build_payload(payload, config, **kwargs) -> dict | Existing pattern, unchanged: schema-mapped passthrough. |
| MCPToolService | PUBLIC | execute(...) -> dict | Inherited unchanged from BaseToolService. No override. |
| MCPToolService | PROTECTED | get_endpoint(config, **kwargs) -> str | Return the MCP server's Streamable HTTP URL from config['mcp_server_url']. |
| MCPToolService | PROTECTED | _build_payload(payload, config, **kwargs) -> dict | Wrap payload in the MCP JSON-RPC envelope: {"jsonrpc":"2.0","method":"tools/call","params":{"name":...,"arguments":...}}. |


### Config / Environment Resolution — BaseToolService


| config key | Environment variable | Default | Used in step |
| --- | --- | --- | --- |
| timeout | TOOL_TIMEOUT | 5.0 | __http_post() — session timeout |
| retries | TOOL_RETRIES | 3 | execute() — retry loop count |
| tool_base_url | TOOL_BASE_URL | 'http://localhost' | RestToolService.get_endpoint() |
| mcp_server_url | MCP_SERVER_URL | None | MCPToolService.get_endpoint() |
| auth_token | TOOL_AUTH_TOKEN | None | __http_post() — Authorization header |
| tenant_routing_map | TENANT_ROUTING_MAP | {} | get_endpoint() — per-tenant endpoint overrides |
| commit_gate_enabled | COMMIT_GATE_ENABLED | True | __commit_gate() — set False only for single-item, non-batch flows |


## 2.7 BaseMCPServer


*mcp/ — serve() is the single public entry point. Server-side counterpart to BaseToolService: turns wrapped APIs into a spec-compliant MCP server.*

BaseToolService (2.5) is a client — it calls an MCP server that already exists somewhere. BaseMCPServer is the other half: it lets a repo expose its own existing tool implementations (typically BaseToolService subclasses) as an MCP server, so external agents can discover them via tools/list and invoke them via tools/call, without hand-writing MCP protocol code per tool.


### Class Variables


| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| server_name | str | 'e2a-mcp-server' | Governance key identifying this server instance in logs and the tool registry. |
| registered_tools | Dict[str, dict] | {} | Populated by wrap_api_as_tool(). Maps tool_name -> {handler: BaseToolService instance, description, input_schema}. |


### Method Contract Table


| Access | Signature | Config / kwargs keys | Purpose |
| --- | --- | --- | --- |
| PUBLIC | serve(request, config=None, **kwargs) -> dict | mcp_protocol_version | Template method — the single entry point, mirroring run()/execute()/retrieve(). Delegates to the framework-owned __dispatch_jsonrpc() and returns a JSON-RPC-shaped response. |
| PROTECTED | _list_tools(config=None, **kwargs) -> list[dict] | tool_filter | Handles the tools/list method. Default implementation derives {name, description, inputSchema} for every entry in registered_tools. Override only to filter or augment what is advertised (e.g. per-tenant tool visibility). |
| PROTECTED | _call_tool(tool_name, arguments, config=None, tenant_id=None, **kwargs) -> dict | — | Handles the tools/call method. Default implementation resolves tool_name in registered_tools and awaits the wrapped handler's execute(arguments, config, tenant_id=tenant_id). Returns an MCP-shaped {content, isError} result. |
| PROTECTED | wrap_api_as_tool(handler, tool_name, description, input_schema, config=None, **kwargs) -> None | — | Factory method — 'create an MCP server by wrapping an API'. Registers an existing BaseToolService instance (REST or otherwise) under tool_name with an MCP tool definition generated from input_schema. No new transport code is written; the existing tool's execute() is reused as-is. |
| PRIVATE | __dispatch_jsonrpc(request, config, **kwargs) -> dict | — | Framework-owned. Parses the JSON-RPC envelope, routes request['method'] ('tools/list' -> _list_tools, 'tools/call' -> _call_tool), and wraps the result or error in the correct JSON-RPC response shape. Never overridden — same pattern as __http_post being framework-owned in BaseToolService. |


### Config / Environment Resolution — BaseMCPServer


| config key | Environment variable | Default | Used in step |
| --- | --- | --- | --- |
| mcp_protocol_version | MCP_PROTOCOL_VERSION | '2025-06-18' | serve() — advertised in tools/list responses |
| mcp_server_name | MCP_SERVER_NAME | 'e2a-mcp-server' | serve() — identifies this server in logs |
| tool_filter | — | None | _list_tools() — optional per-tenant visibility filter |


## 2.8 Tool Call Routing — MCP-First With REST Fallback


*workflow/ or a ToolRouter helper — lives beside _get_agent() on BaseWorkflow, called from the orchestration layer immediately before a tool is invoked, never from inside BaseAgent.*

This is the single function that answers: for this tool call, does a wrapped MCP server exist? If yes, call it as an MCP tool call through MCPToolService. If no, execute the normal (REST) tool call through RestToolService — the existing behavior is preserved unchanged as the fallback path.

| Access | Signature | Config / kwargs keys | Purpose |
| --- | --- | --- | --- |
| PROTECTED | _resolve_tool_call(tool_name, payload, config=None, tenant_id=None, **kwargs) -> dict | mcp_server_registry, tool_registry, default_transport | Checks config['mcp_server_registry'] for tool_name. If an entry exists, executes via MCPToolService against that server's endpoint. Otherwise falls back to _get_tool_service()'s existing REST/MCP-client resolution and executes normally. |
| PROTECTED | _get_tool_service(tool_name, config=None, **kwargs) -> BaseToolService | tool_registry, default_transport | Secondary resolver, used only inside the fallback path. Resolves which BaseToolService subclass (Rest or MCP client) to instantiate for a tool that is not backed by a locally wrapped MCP server. |


### Design Rule


Tool-transport selection is resolved outside BaseAgent, using the same resolution pattern BaseWorkflow already uses for agent routing (_get_agent) and BaseAgent already uses for LLM provider routing (__llm_call). BaseAgent's contract does not change: it still only reasons and returns state, calling _resolve_tool_call() from inside _apply_policy() rather than doing tool-execution work itself. This preserves the retry, timeout, auth, and tracing guarantees that BaseToolService.execute() centrally enforces, whichever path is taken.


### Config / Environment Resolution — Tool Routing


| config key | Environment variable | Default | Used in step |
| --- | --- | --- | --- |
| mcp_server_registry | MCP_SERVER_REGISTRY_PATH | {} | _resolve_tool_call() — maps tool_name to a wrapped MCP server's {mcp_server_url, mcp_tool_name}, checked first |
| tool_registry | TOOL_REGISTRY_PATH | {} | _get_tool_service() — maps tool_name to {transport, endpoint} for the fallback path |
| default_transport | DEFAULT_TOOL_TRANSPORT | 'http' | _get_tool_service() — fallback when tool_name is in neither registry |


### What Does Not Change


- BaseAgent — no new methods, no tool-execution code. It still only produces state via run(); it calls _resolve_tool_call() from _apply_policy(), the same place it always called tools from.

- BaseToolService.execute() and __http_post() — both inherited unchanged by MCPToolService, whether the endpoint is an external MCP server or one hosted locally via BaseMCPServer.

- Governance — retries, timeout, and auth_token injection in __http_post() apply identically whether the downstream call is a legacy REST tool or an MCP server, local or external.


## 2.9 Foundation Classes


*Interface pattern — BaseInfraProvisioner · BasePipeline. Fully-specified, wired-in template classes — BaseObservability · BaseGovernanceFramework.*

BaseObservability and BaseGovernanceFramework are concrete `ABC` classes with a public template method each — `record_telemetry()` and `enforce_governance_gate()` — not disconnected interface stubs. Both are called directly from the scaffold: `BaseWorkflow.execute()` hands `message_log` to `observability_engine.record_telemetry()` in its `finally` block, and `BaseAgent.run()` calls `governance_engine.enforce_governance_gate()` ahead of `_apply_policy()` whenever a governance engine is configured. If neither engine is present in `config`, both classes fall back to the framework's existing default behavior (`__flush_logs()` and a direct `_apply_policy()` call, respectively) — so the integration is additive, not a breaking requirement.


### 2.9.1 BaseObservability


*monitoring/ — record_telemetry() is the single public entry point, invoked from BaseWorkflow.execute()'s finally block for every request, success or failure.*

| Access | Signature | Config / kwargs keys | Purpose |
| --- | --- | --- | --- |
| PUBLIC | record_telemetry(message_log, correlation_id, config=None, **kwargs) -> None | observability_engine | Template method. Enriches every message_log entry with correlation_id, tenant_id, a host timestamp, and the framework version; derives basic metrics (total_tokens summed from any tokens_used field, error_count); then dispatches to the three hooks below inside a try/except that falls back to plain logging.info() per entry if any hook raises. |
| PROTECTED (abstract) | _ship_logs(enriched_logs, config, **kwargs) | log_sink | Ship the enriched, structured logs to their sink (stdout / CloudWatch / Datadog / Cloud Logging). |
| PROTECTED (abstract) | _emit_metrics(metrics, config, **kwargs) | namespace, dimensions | Emit the derived metrics (total_tokens, error_count) to CloudWatch / Datadog / GCP Monitoring. |
| PROTECTED (abstract) | _export_traces(correlation_id, logs, config, **kwargs) | trace_id, service_name | Emit distributed traces to X-Ray / Jaeger / Cloud Trace, keyed by correlation_id. |

> **Compile-Time Enforced, Not a Thin Interface**
>
> Unlike BaseInfraProvisioner/BasePipeline below, BaseObservability carries a real template method with actual logic (enrichment, then metric derivation) ahead of the three abstract hooks. Because it is an ABC with `@abstractmethod`-decorated hooks, a subclass that leaves any one of `_ship_logs`, `_emit_metrics`, or `_export_traces` unimplemented fails at instantiation — `TypeError: Can't instantiate abstract class ... with abstract method ...` — not at the first call, in whichever environment happens to exercise that code path first.


### 2.9.2 BaseGovernanceFramework


*governance/ — enforce_governance_gate() is the single public entry point, invoked from BaseAgent.run() in place of a direct _apply_policy() call whenever config['governance_engine'] is set.*

| Access | Signature | Config / kwargs keys | Purpose |
| --- | --- | --- | --- |
| PUBLIC | enforce_governance_gate(state, io_config, config=None, **kwargs) -> dict | governance_engine, max_token_budget | Template method. Runs three checks in fixed order — (1) token budget: raises ValueError if state['cumulative_tokens_used'] >= io_config['max_token_budget']; (2) human-approval dehydration: raises DehydrationInterrupt if state['requires_human_approval'] is set and state['approval_granted'] is not; (3) MCP sandbox verification, only if state['pending_mcp_tool_execution'] is set — then passes state through the semantic firewall hook for a final pass before returning it. |
| PROTECTED (abstract) | _execute_semantic_firewall(state, config, **kwargs) -> dict | policy_file, redaction_rules | Run local guard models for PII redaction and prompt-injection blocking against the agent state. |
| PROTECTED (abstract) | _verify_sandbox_profile(config, **kwargs) -> None | — | Assert the current execution is running inside an isolated microVM (gVisor/Firecracker) before an MCP tool call is allowed to proceed. |
| PROTECTED (abstract) | _circuit_breaker(failures, config, **kwargs) -> bool | fail_max, reset_timeout | Circuit breaker state transitions and metric emission. No call site inside enforce_governance_gate() itself — invoked by wiring code that tracks failure counts across calls, typically from inside a concrete _handle_error(). |
| PROTECTED (abstract) | _dehydrate_state_to_perimeter(state, correlation_id, tenant_id, config) -> None | — | Persist state to the Tier 3 data perimeter immediately before DehydrationInterrupt is raised, so a later webhook callback can rehydrate and resume the request. |

> **DehydrationInterrupt — a Clean Pause, Not a Failure**
>
> `enforce_governance_gate()` raises `DehydrationInterrupt` (not a generic exception) when a human-approval gate isn't yet satisfied. `BaseAgent.run()` catches it separately from `Exception` and re-raises it unchanged, so it bubbles past `BaseAgent` untouched. `BaseWorkflow.execute()` is the only class positioned to safely handle it: it sets `state['status'] = 'AWAITING_WEBHOOK'` and returns without appending to `failed_keys` or dispatching to the DLQ — a dehydration is a scheduled pause awaiting external input, not an error.

> **Token Budget Is Config-Driven, Not Hardcoded**
>
> The token-budget check reads `io_config['max_token_budget']`, defaulting to unlimited if absent — so agents that don't set a budget are unaffected. This lets cost governance be opted into per-tenant or per-workflow via `io_config` rather than being a global framework constant.


### 2.9.3 BaseInfraProvisioner & BasePipeline — Interface Pattern, No Shared Implementation


> **Interface Pattern — No Shared Implementation**
>
> These two remain pure Interfaces (Python Protocol or pure ABC with no __init__ state) — they carry no shared implementation of their own. The abstract class enforces the contract; the project-specific subclass owns the complete implementation. Every method accepts config=None and **kwargs so the same signature pattern is consistent across all E2A classes.

| Access | Class / Method | Config / kwargs keys | Purpose |
| --- | --- | --- | --- |
| INTERFACE | BaseInfraProvisioner |  |  |
| INTERFACE | _define_network(config, **kwargs) | cidr_block, region, az_count | Define VPC, subnets, security groups. |
| INTERFACE | _define_compute(config, **kwargs) | instance_type, desired_count | Define ECS/GKE/Cloud Run compute resources. |
| INTERFACE | _apply_infra(config, **kwargs) | dry_run, auto_approve | Execute terraform apply / CDK deploy / Pulumi up. |
| INTERFACE | BasePipeline |  |  |
| INTERFACE | _run_tests(config, **kwargs) | coverage_threshold, test_markers | Unit + integration test gate. |
| INTERFACE | _run_rag_eval(config, **kwargs) | faithfulness_threshold, eval_dataset | RAGAS evaluation gate. Blocks deploy if below threshold. |


## 2.10 Progressive Decomposition — Splitting a Class Into Multiple Microservices


Every class in this playbook is deliberately coarse-grained at first: one BaseRAGPipeline, one BaseToolService per tool, one BaseMCPServer per wrapped API surface. That is a starting shape, not a ceiling. As traffic, team size, or NFRs diverge across the responsibilities inside a single class, the same class can be split along its natural method boundaries into two or more narrower classes — and each of those can then be deployed as its own microservice. BaseValidationService (Section 2.2) is itself an example of this pattern already applied once: state validation was split out of BaseAgent for exactly this reason.


### How to Recognize a Split Point


- Different scaling profiles: one group of methods is CPU/GPU-bound and bursty (e.g. embedding generation), another is I/O-bound and steady (e.g. vector search) — forcing them to scale together wastes cost in one direction or starves the other.

- Different update cadence: one group of methods changes weekly (retrieval ranking tuned per tenant), another is nearly static (the ingestion/chunking pipeline) — coupling them in one deployable means low-risk changes wait on high-risk release cycles, and vice versa.

- Different failure blast radius: a failure in one group should not take down the other. A chunking/indexing failure during a batch ingestion job has no reason to affect live query-time retrieval.

- Different callers: if one group of methods is called synchronously in the request path and another is called from a scheduled batch job or a separate producer, they already have different runtime lifecycles even before the code is split.


### Worked Example: BaseRAGPipeline


BaseRAGPipeline (Section 2.5) currently owns the full lifecycle implicitly: an implementation's _search_index() assumes documents are already chunked, embedded, and indexed by some other process. Made explicit, that upstream lifecycle splits cleanly into a second class:

| Class | Owns | Public entry point | Typical deployment |
| --- | --- | --- | --- |
| BaseRAGPipeline (existing) | retrieve() — embed query, _search_index(), _rerank(), _evaluate_answer(). Query-time, synchronous, called on every agent request. | retrieve() | Request-path service, scales with query QPS (Section 3.2-style compute). |
| BaseIndexingPipeline (new, same pattern) | ingest() — _chunk_documents(), _embed_documents(), _write_index(). Write-time, usually batch or event-triggered, called on document upload/update, not per query. | ingest() | Event-driven worker, scales with ingestion volume (Section 3.4-style decoupled worker), independent of query traffic. |

BaseIndexingPipeline follows the same shape as every other class in this playbook: one public entry point (ingest()), protected hooks a subclass overrides (_chunk_documents(), _embed_documents(), _write_index()), and the same six propagation fields threaded through — tenant_id in particular is just as critical here as in _search_index(), since a mis-scoped write is worse than a mis-scoped read.


### Applying the Same Pattern Elsewhere


| Existing class | Candidate split | Why |
| --- | --- | --- |
| BaseToolService | Split _build_payload()/schema-mapping concerns from __http_post()/transport concerns only if a single tool's payload construction becomes independently complex (e.g. a multi-step enrichment pipeline before the call) — otherwise leave as one class. | Most tools don't warrant this; only split when payload-building has its own failure modes and release cadence distinct from the call itself. |
| BaseAgent | Split _build_messages() (prompt construction, often content/PM-owned and iterated frequently) from _apply_policy()/_evaluate_output() (governance, owned by platform/risk teams, changed rarely) if the two are maintained by different teams with different review and release requirements. | Different owners and different change risk are a stronger signal than raw traffic volume. |
| BaseMCPServer | Split wrap_api_as_tool() registration/config concerns (rarely called, admin-time) from serve()/_call_tool() (every request, hot path) only once the number of wrapped tools is large enough that registration logic itself needs independent versioning. | Registration is admin-plane; serving is data-plane — usually fine to colocate until scale forces the split. |

> **Rule of Thumb**
>
> Don't split preemptively. Splitting a class into two microservices adds a network hop, a second deployment pipeline, and a second place propagation fields must be threaded through correctly. Split only once one of the four recognition signals above is concretely true for your workload — not because the class 'feels big'. Every split still follows the same E2A shape: one public entry point, protected override hooks, and the same propagation contract, so the framework's guarantees don't weaken as it decomposes.


---



# 3. Scaffold File — e2a_base.py


Drop this file into any repository as the foundation layer. All E2A abstract classes are defined here with full docstrings, @abstractmethod decorators, config/env resolution, and the cross-class propagation wiring baked in from the start — not layered on after. Developers import and subclass these without modifying this file.

> **File placement**
>
> Place e2a_base.py at src/e2a_base.py or framework/e2a_base.py. All concrete agent, workflow, RAG, tool, and MCP server classes import from it. The file must never be modified once placed — create subclasses instead.


### e2a_base.py — Complete Source


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

    def __log(self, message_log, correlation_id, level, event, **fields):
        if message_log is None:
            return
        message_log.append({
            'timestamp': time.time(), 'correlation_id': correlation_id,
            'level': level, 'event': event,
            'class': type(self).__name__, **fields,
        })

    def __flush_logs(self, message_log, config=None, **kwargs):
        config = config or {}
        sink = kwargs.get('log_sink', config.get(
            'log_sink', os.getenv('LOG_SINK', 'stdout')))
        for entry in (message_log or []):
            logging.info(entry) if sink == 'stdout' else None
            # cloudwatch / datadog / stackdriver dispatch goes here

    def __send_to_dlq(self, failed_keys, config=None, **kwargs):
        config = config or {}
        queue_url = kwargs.get('dlq_queue_url', config.get(
            'dlq_queue_url', os.getenv('DLQ_QUEUE_URL')))
        if queue_url and failed_keys:
            logging.warning(f'DLQ dispatch -> {queue_url}: {failed_keys}')

    def __commit_gate(self, key, failed_keys, config=None, **kwargs):
        config = config or {}
        enabled = kwargs.get('commit_gate_enabled', config.get(
            'commit_gate_enabled', True))
        if not enabled:
            return True
        return key not in (failed_keys or [])

# ==================================================
# Foundation classes — Observability & Governance
# Concrete ABC classes with a public template method each.
# Wired into BaseWorkflow.execute() / BaseAgent.run() below via
# config['observability_engine'] / config['governance_engine'].
# ==================================================

class DehydrationInterrupt(Exception):
    """Raised by BaseGovernanceFramework.enforce_governance_gate() when a
    human-approval gate isn't yet satisfied. BaseAgent.run() re-raises it
    unchanged; BaseWorkflow.execute() is the only class that catches it
    to safely halt — a dehydration is a scheduled pause, not a failure."""

class BaseObservability(ABC):
    """Template method abstract class for Enterprise Telemetry."""

    def record_telemetry(self, message_log: List[dict], correlation_id: str,
                          config: Dict[str, Any] = None, **kwargs) -> None:
        """Public entry point. Called from BaseWorkflow.execute()'s finally
        block for every request, success or failure, if config['observability_engine']
        is set. Enriches every entry, derives basic metrics, then dispatches
        to the three hooks below."""
        config = config or {}
        enriched_logs = []
        for entry in (message_log or []):
            enriched_logs.append({
                **entry,
                'correlation_id': correlation_id,
                'tenant_id': kwargs.get('tenant_id', 'UNKNOWN_TENANT'),
                'host_epoch_ms': int(time.time() * 1000),
                'framework_version': 'e2a-v2.0',
            })
        metrics = self.__extract_metrics(enriched_logs)
        try:
            self._ship_logs(enriched_logs, config, **kwargs)
            if metrics:
                self._emit_metrics(metrics, config, **kwargs)
            self._export_traces(correlation_id, enriched_logs, config, **kwargs)
        except Exception as e:
            logging.error(f"Telemetry dispatch failed for {correlation_id}: {str(e)}")
            for log in enriched_logs:
                logging.info(log)

    def __extract_metrics(self, enriched_logs: List[dict]) -> Dict[str, float]:
        metrics = {'total_tokens': 0, 'error_count': 0}
        for log in enriched_logs:
            if log.get('level') == 'ERROR':
                metrics['error_count'] += 1
            if 'tokens_used' in log:
                metrics['total_tokens'] += log['tokens_used']
        return metrics

    @abstractmethod
    def _ship_logs(self, enriched_logs: List[dict], config: Dict[str, Any], **kwargs):
        """kwargs: log_sink"""

    @abstractmethod
    def _emit_metrics(self, metrics: Dict[str, float], config: Dict[str, Any], **kwargs):
        """kwargs: namespace, dimensions"""

    @abstractmethod
    def _export_traces(self, correlation_id: str, logs: List[dict], config: Dict[str, Any], **kwargs):
        """kwargs: trace_id, service_name"""

class BaseGovernanceFramework(ABC):
    """Template method abstract class for AI Safety, Economics, and Lifecycle Governance."""

    def enforce_governance_gate(self, state: Dict[str, Any], io_config: Dict[str, Any],
                                 config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        """Public entry point. Called from BaseAgent.run() in place of a
        direct _apply_policy() call, if config['governance_engine'] is set.
        Runs token budget, human-approval dehydration, and MCP sandbox
        checks in fixed order, then the semantic firewall."""
        config = config or {}
        self.__enforce_token_budget(state, io_config)

        if state.get('requires_human_approval') and not state.get('approval_granted'):
            self.__trigger_dehydration(state, config, **kwargs)

        if state.get('pending_mcp_tool_execution'):
            self._verify_sandbox_profile(config, **kwargs)

        state = self._execute_semantic_firewall(state, config, **kwargs)
        return state

    def __enforce_token_budget(self, state: Dict[str, Any], io_config: Dict[str, Any]) -> None:
        max_budget = io_config.get('max_token_budget', float('inf'))
        current_usage = state.get('cumulative_tokens_used', 0)
        if current_usage >= max_budget:
            raise ValueError(f"Governance Violation: Token budget exhausted. "
                             f"Used {current_usage}, Limit {max_budget}")

    def __trigger_dehydration(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> None:
        correlation_id = kwargs.get('correlation_id', state.get('correlation_id'))
        tenant_id = kwargs.get('tenant_id', state.get('tenant_id'))
        self._dehydrate_state_to_perimeter(state, correlation_id, tenant_id, config)
        raise DehydrationInterrupt(f"State {correlation_id} dehydrated. Awaiting webhook.")

    @abstractmethod
    def _execute_semantic_firewall(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """Execute local Guard Models for PII redaction and injection blocking."""

    @abstractmethod
    def _verify_sandbox_profile(self, config: Dict[str, Any], **kwargs) -> None:
        """Assert execution is within a gVisor/Firecracker microVM for MCP Isolation."""

    @abstractmethod
    def _circuit_breaker(self, failures: int, config: Dict[str, Any], **kwargs) -> bool:
        """kwargs: fail_max, reset_timeout. No call site inside
        enforce_governance_gate() itself — invoked by wiring code that
        tracks failure counts across calls, typically from _handle_error()."""

    @abstractmethod
    def _dehydrate_state_to_perimeter(self, state: Dict[str, Any], correlation_id: str,
                                       tenant_id: str, config: Dict[str, Any]) -> None:
        """Persist state to the Tier 3 data perimeter before
        DehydrationInterrupt is raised, so a webhook can later resume it."""

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
            gov_engine = config.get('governance_engine')
            if gov_engine and isinstance(gov_engine, BaseGovernanceFramework):
                state = gov_engine.enforce_governance_gate(state, io_config, config, **common, **kwargs)
            else:
                self._apply_policy(state, config, **common, **kwargs)
            messages = self._build_messages(state, config, **common, **kwargs)
            response = self.__llm_call(messages, config, **kwargs)
            confidence = self._evaluate_output(
                response, state, config, **common, **kwargs)
            min_conf = kwargs.get('min_confidence', config.get(
                'min_confidence', float(os.getenv('MIN_CONFIDENCE', 0.85))))
            if confidence < min_conf:
                response = self._fallback(state, config, **common, **kwargs)
            state['response'] = response
        except DehydrationInterrupt as d:
            raise d  # Bubble up to BaseWorkflow to safely halt
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
        except DehydrationInterrupt as d:
            self.__flush_logs([{'timestamp': time.time(), 'level': 'INFO',
                               'event': 'workflow_dehydrated', 'reason': str(d)}], config)
            state['status'] = 'AWAITING_WEBHOOK'
        except Exception as e:
            failed_keys.append(idempotency_key)
            self._handle_error(e, state, config, correlation_id=correlation_id,
                                message_log=message_log,
                                failed_keys=failed_keys, **kwargs)
        finally:
            obs_engine = config.get('observability_engine')
            if obs_engine and isinstance(obs_engine, BaseObservability):
                obs_engine.record_telemetry(message_log, correlation_id, config, tenant_id=tenant_id)
            else:
                self.__flush_logs(message_log, config)
            if failed_keys:
                self.__send_to_dlq(failed_keys, config)
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
        config = config or {}
        if not self._validate_input(payload, config, **kwargs):
            raise ValueError('Invalid input')
        key = kwargs.get('idempotency_key', payload.get('idempotency_key'))
        if key and not self.__commit_gate(key, failed_keys, config, **kwargs):
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
        return {'status': 'ok', 'endpoint': endpoint}  # aiohttp/httpx call

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
```


---



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


## 4.5 Guidance: Which Methods to Override


| Protected Method | Override? | When and why | Default |
| --- | --- | --- | --- |
| _validate_<agent_name> (on BaseValidationService) | Required, per agent | One per agent, on the validation subclass — not on BaseAgent. Runs before the agent is instantiated. | no default — must be registered |
| _build_messages | Yes | Always: every agent needs a domain-specific prompt. | pass — no messages |
| _apply_policy | If governed | Domain-specific approval gates or a tool call via _resolve_tool_call(). | pass — no policy |
| _evaluate_output | Yes | Always: define what 'quality' means for this agent. | pass — returns None |
| wrap_api_as_tool calls | As needed | Once per existing API you want discoverable/callable over MCP. | no tools registered |
| _list_tools / _call_tool | Rarely | Only to filter tool visibility per tenant or add pre/post-processing. | derives from registered_tools |


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


---



# 5. Global Config / Environment Reference


One consolidated table for every config key used anywhere in the framework, replacing the separate per-addendum tables carried in earlier revisions.

| config key | Environment variable | Default | Used in |
| --- | --- | --- | --- |
| schema_registry | VALIDATION_SCHEMA_REGISTRY_PATH | {} | BaseValidationService validators |
| strict_mode | VALIDATION_STRICT_MODE | True | BaseValidationService.validate() |
| min_confidence | MIN_CONFIDENCE | 0.85 | BaseAgent.run() |
| model_id | LLM_MODEL_ID | 'default' | BaseAgent.__llm_call() |
| io_config_prefix | IO_CONFIG_PREFIX | 'io_' | _resolve_io_config() |
| idempotency_store_url | IDEMPOTENCY_STORE_URL | None | BaseWorkflow.__resolve_idempotency() |
| idempotency_ttl_seconds | IDEMPOTENCY_TTL_SECONDS | 86400 | __lookup_idempotency_store() |
| dlq_queue_url | DLQ_QUEUE_URL | None | __send_to_dlq() |
| log_sink | LOG_SINK | 'stdout' | __flush_logs() |
| observability_engine | — (object reference, not env-resolvable) | None | BaseWorkflow.execute() — record_telemetry() sink; falls back to __flush_logs() if unset (Section 2.9.1) |
| governance_engine | — (object reference, not env-resolvable) | None | BaseAgent.run() — enforce_governance_gate() gate; falls back to a direct _apply_policy() call if unset (Section 2.9.2) |
| max_token_budget | MAX_TOKEN_BUDGET | inf | BaseGovernanceFramework.enforce_governance_gate() — per-request ceiling checked against state['cumulative_tokens_used'] |
| commit_gate_enabled | COMMIT_GATE_ENABLED | True | BaseToolService.__commit_gate() |
| tenant_routing_map | TENANT_ROUTING_MAP | {} | get_endpoint() — per-tenant overrides |
| faithfulness_threshold | FAITHFULNESS_THRESHOLD | 0.85 | BaseRAGPipeline._evaluate_answer() |
| rag_top_k | RAG_TOP_K | 5 | BaseRAGPipeline.retrieve() |
| embedding_model | EMBEDDING_MODEL | 'amazon.titan-embed-text-v1' | BaseRAGPipeline.__embed() |
| timeout | TOOL_TIMEOUT | 5.0 | BaseToolService.__http_post() |
| retries | TOOL_RETRIES | 3 | BaseToolService.execute() |
| tool_base_url | TOOL_BASE_URL | 'http://localhost' | RestToolService.get_endpoint() |
| auth_token | TOOL_AUTH_TOKEN | None | BaseToolService.__http_post() |
| mcp_server_url | MCP_SERVER_URL | None | MCPToolService.get_endpoint() |
| mcp_protocol_version | MCP_PROTOCOL_VERSION | '2025-06-18' | BaseMCPServer.serve() |
| mcp_server_name | MCP_SERVER_NAME | 'e2a-mcp-server' | BaseMCPServer — logging identity |
| mcp_server_registry | MCP_SERVER_REGISTRY_PATH | {} | _resolve_tool_call() — checked first |
| tool_registry | TOOL_REGISTRY_PATH | {} | _get_tool_service() — fallback path |
| default_transport | DEFAULT_TOOL_TRANSPORT | 'http' | _get_tool_service() — final fallback |

*E2A Architecture Framework — Implementation Playbook · github.com/subhamviky/e2a-framework · Subham Gupta*

*Authorship: Framework implementation, abstract class contracts, and multi-cloud mapping logic are the original work of Subham Gupta.*

*Trademark: All vendor trademarks (AWS, GCP, Azure, Meta Llama) are the property of their respective owners and are used here for architectural reference only.*

*Managed Service Disclaimer: Certain ecosystem components referenced (e.g., Pinecone, LangGraph, Lakera, FAISS) are third-party partner technologies and are not native managed services of AWS, Google Cloud, or Microsoft Azure. The E2A Framework provides a standardized way to integrate these third-party tools alongside native cloud services.*
