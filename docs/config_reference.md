# E2A Configuration Reference

Every config key and environment variable used by the E2A framework, grouped by owner. The tables are generated from the E2A_CONFIG registry at the top of `e2a_base.py` (`config_reference_markdown()`), so they cannot drift from the code. `env.example` is generated from the same registry (`env_example()`).

**Resolution order** — identical for every key: call-time kwargs, then the config dict, then the environment variable, then the default. A provider-specific value is supplied through the same path by the concrete subclass; it is never a second key for the same resource. Object references (`llm_service`, `idempotency_store`, `cache_store`, `transactional_store`, `pipeline_state_store`, the engines) are not environment-resolvable and are passed in the config dict from the composition root. Secrets are env-resolvable but emitted empty in `env.example`.

### Common

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| io_config_prefix | IO_CONFIG_PREFIX | 'io_' | _resolve_io_config() |
| log_sink | LOG_SINK | 'stdout' | BaseObservability._ship_logs() / _flush_logs() |
| dlq_queue_url | DLQ_QUEUE_URL | (unset) | _send_to_dlq() |
| commit_gate_enabled | COMMIT_GATE_ENABLED | True | _commit_gate() |
| expected_version | — (not env-resolvable) | (unset) | _commit_gate() — optional OCC version, caller-supplied per call |
| idempotency_store_url | IDEMPOTENCY_STORE_URL | (unset) | BaseWorkflow.__resolve_idempotency() |
| idempotency_ttl_seconds | IDEMPOTENCY_TTL_SECONDS | 86400 | __lookup_idempotency_store() |
| tenant_routing_map | TENANT_ROUTING_MAP | {} | get_endpoint() — per-tenant overrides |

### Engines (object references)

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| observability_engine | — (not env-resolvable) | (unset) | BaseWorkflow.execute() — BaseObservability instance |
| governance_engine | — (not env-resolvable) | (unset) | BaseAgent.run() — BaseGovernanceFramework instance |
| llm_service | — (not env-resolvable) | (unset) | BaseAgent / LLMOnlyAgent _infer() — the bound BaseLLMAgentService (required) |
| max_token_budget | — (not env-resolvable) | inf | BaseGovernanceFramework.__enforce_token_budget() — io_config key |
| idempotency_store | — (not env-resolvable) | (unset) | BaseWorkflow.execute() / BaseOrchestrator.execute() / AIDLCPipelineOrchestrator — the bound BaseIdempotencyStore (atomic claim) |
| cache_store | — (not env-resolvable) | (unset) | BaseQueryService.fetch() — the bound BaseCacheStore (single-flight get_or_load) |
| transactional_store | — (not env-resolvable) | (unset) | BaseCommandService._commit_versioned() — the bound BaseTransactionalStore (conditional commit) |
| pipeline_state_store | — (not env-resolvable) | (unset) | AIDLCPipelineOrchestrator — the bound BaseTransactionalStore (durable phase state) |

### Validation

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| schema_registry | VALIDATION_SCHEMA_REGISTRY_PATH | {} | BaseValidationService validators |
| strict_mode | VALIDATION_STRICT_MODE | True | BaseValidationService.validate() |

### Agents

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| min_confidence | MIN_CONFIDENCE | 0.85 | BaseAgent.run(), LLMOnlyAgent.run() — fallback threshold |
| max_latency | MAX_LATENCY | 2.0 (5.0 for BaseToolService, 1.0 for CQRS) | BaseAgent.run(), BaseRAGPipeline.retrieve(), BaseToolService.execute() — latency SLO, NFRViolationError on breach |
| max_tokens | MAX_TOKENS | 5000 | BaseAgent.run() — token budget per call/workflow |
| prompt_template | PROMPT_TEMPLATE | (unset) | BaseAgent._build_messages() — default prompt template when no registry is used |
| cache_refresh_token | — (not env-resolvable) | 'v1' | BaseAgent._build_messages() — mutate to force a context-cache miss |

### Governance

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| approved_agents | APPROVED_AGENTS | [] | BaseAgent.run() — agent_name must be listed |
| approved_models | APPROVED_MODELS | [] | BaseRAGPipeline — embedding / reranker model allow-list |
| approved_tools | APPROVED_TOOLS | [] | BaseToolService — tool_name must be listed |

### Multimodal

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| model_capability_registry | MODEL_CAPABILITY_REGISTRY_PATH | {} | LLMOnlyAgent._select_model() |
| stt_provider | STT_PROVIDER | (unset) | ModalityPreprocessor.transcribe() |
| document_extractor | DOCUMENT_EXTRACTOR_PROVIDER | (unset) | ModalityPreprocessor.extract() |
| max_audio_duration_seconds | MAX_AUDIO_DURATION_SECONDS | 600 | checked before transcribe() — NFRViolationError on breach |
| max_document_pages | MAX_DOCUMENT_PAGES | 50 | checked before extract() — NFRViolationError on breach |

### LLM service

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| model_id | LLM_MODEL_ID | 'default' | _dispatch_upstream_call() — provider routing; LLMOnlyAgent._select_model() default |
| temperature | LLM_TEMPERATURE | 0.0 | _dispatch_upstream_call() |
| context_budget | LLM_CONTEXT_BUDGET | 8192 | _manage_context_budget() — input token budget |
| max_output_tokens | LLM_MAX_OUTPUT_TOKENS | 2048 | quota estimate and upstream limit |
| llm_max_latency | LLM_MAX_LATENCY | 3.0 | upstream call deadline — NFRViolationError on breach |
| pod_concurrency | LLM_POD_CONCURRENCY | 32 | pod semaphore — in-flight upstream calls |
| tenant_concurrency | LLM_TENANT_CONCURRENCY | 0 | per-tenant cap within the pod (0 = off) |
| llm_replica_count | LLM_REPLICA_COUNT | 1 | divides max_rpm / max_tpm into a per-pod share |
| max_rpm | LLM_MAX_RPM | 600 | fleet requests-per-minute limit |
| max_tpm | LLM_MAX_TPM | 300000 | fleet tokens-per-minute limit |
| quota_timeout | LLM_QUOTA_TIMEOUT | 5.0 | max wait for quota before NFRViolationError |
| enable_context_caching | LLM_ENABLE_CONTEXT_CACHING | True | _apply_kv_caching() |
| min_cache_threshold | LLM_MIN_CACHE_THRESHOLD | 1024 | _apply_kv_caching() — minimum static prefix tokens |
| enable_semantic_cache | LLM_ENABLE_SEMANTIC_CACHE | True | _check_semantic_cache() / _store_semantic_cache() |
| dlp_enabled | LLM_DLP_ENABLED | True | egress and stream redaction |
| dlp_holdback_chars | LLM_DLP_HOLDBACK_CHARS | 128 | stream holdback window |
| speculative_model | SPECULATIVE_MODEL | (unset) | _dispatch_upstream_call() io_config — speculative-decoding draft model |
| max_speculative_tokens | MAX_SPECULATIVE_TOKENS | (unset) | _dispatch_upstream_call() io_config — draft length |
| permit_timeout | LLM_PERMIT_TIMEOUT | 5.0 | bounded wait for the tenant + pod permit — AdmissionTimeout on expiry (also capped by the remaining deadline) |
| tenant_idle_evict_seconds | LLM_TENANT_IDLE_EVICT_SECONDS | 300.0 | idle per-tenant permit entries are evicted after this long |

### RAG

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| faithfulness_threshold | FAITHFULNESS_THRESHOLD | 0.85 | BaseRAGPipeline._evaluate_answer(); BasePipeline RAG eval gate |
| rag_top_k | RAG_TOP_K | 5 | BaseRAGPipeline.retrieve() — result count |
| embedding_model | EMBEDDING_MODEL | 'amazon.titan-embed-text-v1' | BaseRAGPipeline.__embed() |
| hybrid_alpha | HYBRID_ALPHA | 0.5 | BaseRAGPipeline._search_index() — vector vs keyword weight |
| similarity_metric | SIMILARITY_METRIC | 'COSINE' | BaseRAGPipeline._search_index() io_config |
| index_name | INDEX_NAME | (unset) | BaseRAGPipeline._search_index() — index / collection |
| tenant_index_prefix | TENANT_INDEX_PREFIX | (unset) | BaseRAGPipeline._search_index() — f"{prefix}_{tenant_id}" namespace |
| rerank_top_n | RERANK_TOP_N | (unset) | BaseRAGPipeline._rerank() |

### Tools / MCP

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| timeout | TOOL_TIMEOUT | 5.0 | BaseToolService.__http_post() |
| retries | TOOL_RETRIES | 3 | BaseToolService.execute() |
| tool_base_url | TOOL_BASE_URL | 'http://localhost' | RestToolService.get_endpoint() |
| auth_token | TOOL_AUTH_TOKEN | (unset) | BaseToolService.__http_post() |
| mcp_server_url | MCP_SERVER_URL | (unset) | MCPToolService.get_endpoint() |
| mcp_protocol_version | MCP_PROTOCOL_VERSION | '2025-06-18' | BaseMCPServer.serve() |
| mcp_server_name | MCP_SERVER_NAME | 'e2a-mcp-server' | BaseMCPServer — logging identity |
| tool_filter | MCP_TOOL_FILTER | (unset) | BaseMCPServer — exposed-tool allow-list |
| mcp_server_registry | MCP_SERVER_REGISTRY_PATH | {} | _resolve_tool_call() — checked first |
| tool_registry | TOOL_REGISTRY_PATH | {} | _get_tool_service() — fallback path |
| default_transport | DEFAULT_TOOL_TRANSPORT | 'http' | _get_tool_service() — final fallback |
| max_retries | WEBHOOK_MAX_RETRIES | 3 | BaseWebhookDispatcher.dispatch() |
| signing_secret | WEBHOOK_SIGNING_SECRET | (unset) | BaseWebhookDispatcher._sign_payload() |

### Workflow / Pipeline

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| shadow_mode_enabled | SHADOW_MODE_ENABLED | False | BaseWorkflow.execute() — Blue-Green / shadow trials |
| shadow_target_pipeline | SHADOW_TARGET_PIPELINE | (unset) | BaseWorkflow.execute() — shadow target |
| prompt_store_url | PROMPT_STORE_URL | (unset) | BasePromptRegistry.get_prompt() |
| default_version | PROMPT_DEFAULT_VERSION | 'latest' | BasePromptRegistry.get_prompt() |
| deployment_strategy | DEPLOYMENT_STRATEGY | 'BLUE_GREEN' | BasePipeline._execute_deployment_rollout() |

### Deadlines

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| request_deadline_seconds | REQUEST_DEADLINE_SECONDS | 0.0 | BaseWorkflow.execute() / BaseOrchestrator.execute() — origin of the additive deadline field (0 = none) |

### Resilience

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| retry_base_backoff | RETRY_BASE_BACKOFF | 0.1 | ResiliencePolicy — full-jitter exponential backoff base (seconds) |
| retry_max_backoff | RETRY_MAX_BACKOFF | 2.0 | ResiliencePolicy — backoff ceiling (seconds) |
| retry_budget_ratio | RETRY_BUDGET_RATIO | 0.2 | ResiliencePolicy — retry tokens earned per first attempt (caps retry amplification) |
| retry_budget_cap | RETRY_BUDGET_CAP | 10.0 | ResiliencePolicy — maximum retry-token balance |
| circuit_failure_threshold | CIRCUIT_FAILURE_THRESHOLD | 5 | ResiliencePolicy — consecutive failures that open the circuit |
| circuit_reset_seconds | CIRCUIT_RESET_SECONDS | 30.0 | ResiliencePolicy — open duration before a half-open probe |
| bulkhead_max_concurrent | BULKHEAD_MAX_CONCURRENT | 16 | ResiliencePolicy — per-dependency in-flight cap (0 = off) |
| bulkhead_acquire_timeout | BULKHEAD_ACQUIRE_TIMEOUT | 1.0 | ResiliencePolicy — bounded wait for a bulkhead slot |

### Pools / capacity

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| db_pool_size | DB_POOL_SIZE | 20 | store adapters — per-instance database connection pool |
| http_pool_size | HTTP_POOL_SIZE | 50 | tool / upstream adapters — per-instance HTTP connection pool |
| retrieval_pool_size | RETRIEVAL_POOL_SIZE | 20 | BaseRAGPipeline._search_index() adapters — per-instance index connection pool |
| fleet_replica_count | FLEET_REPLICA_COUNT | 1 | validate_capacity() — replicas of this service (multiplies per-instance concurrency) |
| db_max_connections | DB_MAX_CONNECTIONS | 0 | validate_capacity() — database connection ceiling for the whole fleet (0 = unchecked) |
| model_max_inflight | MODEL_MAX_INFLIGHT | 0 | validate_capacity() — provider-side concurrent request ceiling for the whole fleet (0 = unchecked) |
| termination_grace_seconds | TERMINATION_GRACE_SECONDS | 30.0 | validate_capacity() — platform SIGTERM-to-SIGKILL window; drain_timeout_seconds must be shorter |

### Async workers

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| worker_concurrency | WORKER_CONCURRENCY | 8 | consumer runtime — in-flight messages per instance |
| worker_prefetch | WORKER_PREFETCH | 16 | consumer runtime — broker prefetch / max in-flight window per instance |
| drain_timeout_seconds | DRAIN_TIMEOUT_SECONDS | 20.0 | consumer runtime / services — graceful-shutdown drain bound |

### Leases

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| lease_ttl_seconds | LEASE_TTL_SECONDS | 60 | BaseOutboxSweeper / BaseReconciliationService — BaseDistributedLock lease TTL |
| lease_renew_interval_seconds | LEASE_RENEW_INTERVAL_SECONDS | 20 | BaseOutboxSweeper / BaseReconciliationService — renew cadence (must be < lease_ttl_seconds) |
| fencing_enabled | FENCING_ENABLED | True | lease holders pass LeaseHandle.fencing_token to downstream commits |
| idempotency_lease_seconds | IDEMPOTENCY_LEASE_SECONDS | 300 | BaseIdempotencyStore.claim() — IN_PROGRESS claim expiry; an expired claim needs reconciliation, never blind re-execution |

### Cache

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| cache_fill_timeout_seconds | CACHE_FILL_TIMEOUT_SECONDS | 5.0 | BaseCacheStore.get_or_load() — follower wait for the in-flight fill |
| cache_fill_on_timeout | CACHE_FILL_ON_TIMEOUT | 'load' | BaseCacheStore.get_or_load() — follower policy on fill timeout: load | stale | raise |

### Build (AI-DLC)

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| build_max_inflight | BUILD_MAX_INFLIGHT | 4 | AIDLCPipelineOrchestrator — concurrent builds per instance |
| tenant_build_max_inflight | TENANT_BUILD_MAX_INFLIGHT | 2 | AIDLCPipelineOrchestrator — concurrent builds per tenant |

### Concurrency observability

| Config key | Environment variable | Default | Used by |
|---|---|---|---|
| slow_wait_threshold_seconds | SLOW_WAIT_THRESHOLD_SECONDS | 0.5 | permit / pool / bulkhead waits above this are logged at WARN |
| queue_age_slo_seconds | QUEUE_AGE_SLO_SECONDS | 60.0 | oldest-message age above this is an SLO breach signal |

## Existing keys that own a concurrency resource

One resource is controlled by one key. The keys below existed before the concurrency contracts and remain the owners; no parallel knob was added for them.

| Resource | Owning key (environment variable) | Notes |
|---|---|---|
| In-flight model calls per pod | `pod_concurrency` (`LLM_POD_CONCURRENCY`) | Fleet total = `fleet_replica_count` x this value |
| In-flight model calls per tenant per pod | `tenant_concurrency` (`LLM_TENANT_CONCURRENCY`) | 0 = off; must not exceed `pod_concurrency` |
| Wait for quota | `quota_timeout` (`LLM_QUOTA_TIMEOUT`) | Seconds; capped by the remaining request deadline |
| Requests / tokens per minute | `max_rpm` / `max_tpm` | Fleet figures; each pod holds a share (divided by `llm_replica_count`) |
| Upstream model call limit | `llm_max_latency` (`LLM_MAX_LATENCY`) | Seconds; the call runs under min(this, remaining deadline) |
| Tool call timeout | `timeout` (`TOOL_TIMEOUT`) | Seconds |
| Tool call retries | `retries` (`TOOL_RETRIES`) | Retries also require an idempotent call and a retry-budget token |
| Wait for a permit | `permit_timeout` (`LLM_PERMIT_TIMEOUT`) | Added for the permit wait; shared by build admission |

## Effective fleet capacity

Per-instance limits multiply by the replica count, so the keys must be read together. `validate_capacity(config)` checks the following and runs in `BasePipeline`'s scaffold-compliance stage.

| Check | Equation |
|---|---|
| Worker concurrency within the DB pool | `worker_concurrency` <= `db_pool_size` (per instance) |
| LLM concurrency within the HTTP pool | `pod_concurrency` <= `http_pool_size` (per instance) |
| Tenant share within the pod | `tenant_concurrency` <= `pod_concurrency` |
| Prefetch does not starve workers | `worker_prefetch` >= `worker_concurrency` |
| Fleet DB connections | `fleet_replica_count` x `db_pool_size` <= `db_max_connections` (when set) |
| Fleet model concurrency incl. retries | `fleet_replica_count` x `pod_concurrency` x (1 + `retry_budget_ratio`) <= `model_max_inflight` (when set) |
| Lease renewal | `lease_renew_interval_seconds` < `lease_ttl_seconds` |
| Drain window | `drain_timeout_seconds` < `termination_grace_seconds` |

Worked example: `pod_concurrency` 32, `db_pool_size` 20, `retry_budget_ratio` 0.2, a database allowing 200 connections and a model quota of 400 concurrent requests. At 10 replicas: 10 x 20 = 200 connections and 10 x 32 x 1.2 = 384 concurrent model requests — both fit. At 12 replicas the check reports `fleet DB connections 12 x 20 = 240 exceed db_max_connections (200)` and `fleet model concurrency 12 x 32 x (1+retry_budget_ratio) = 461 exceeds model_max_inflight (400)`. The per-instance values did not change; replica multiplication did.
