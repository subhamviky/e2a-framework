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
