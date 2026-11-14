"""E2A: The central configuration registry: E2A_CONFIG, resolve_config(), register_config_key().

Split from e2a_base.py in v1.2.0 without code changes; e2a_base re-exports every name."""
import os
import re
import json
import uuid
import time
import asyncio
import inspect
import hashlib
import logging
import threading
import contextlib
import random
import concurrent.futures
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

# ==================================================
# CENTRAL CONFIGURATION REGISTRY — the single source of truth for every
# config key and environment variable used by every E2A class (agents,
# workflows, RAG, tools, MCP, LLM service, pipelines) and by the concrete
# subclasses in a project built on this scaffold.
#
# Resolution order, identical for every key and every class:
#     call-time kwargs  >  config dict  >  environment variable  >  default
#
# Rules:
#   1. No class reads os.getenv() or hardcodes a default itself — it calls
#      resolve_config(key, config, kwargs). Defaults live here only.
#   2. A project adds its own keys with register_config_key() at start-up
#      (e.g. in src/settings.py) — never by editing this file.
#   3. Section 5 of the Implementation Playbook and .env.example are
#      generated from this registry (config_reference_markdown(),
#      env_example()), so documentation cannot drift from the code.
# ==================================================

def _to_bool(v):
    return v if isinstance(v, bool) else str(v).strip().lower() in ('true', '1', 'yes', 'on')


def _to_json_map(v):
    """Env value is inline JSON or a path to a JSON file; {} on failure."""
    if isinstance(v, (dict, list)):
        return v
    try:
        text = str(v).strip()
        if text[:1] in '{[':
            return json.loads(text)
        with open(text, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        logging.warning('E2A config: could not load JSON map from %r', v)
        return {}


def _to_json_list(v):
    """Env value is a JSON array or a comma-separated list; [] when empty."""
    if isinstance(v, (list, tuple, set)):
        return list(v)
    text = str(v).strip()
    if not text:
        return []
    if text[:1] == '[':
        try:
            return json.loads(text)
        except Exception:
            logging.warning('E2A config: could not parse JSON list from %r', v)
            return []
    return [x.strip() for x in text.split(',') if x.strip()]


@dataclass(frozen=True)
class ConfigKey:
    key: str                        # name in the config dict / kwargs
    env: Optional[str]              # environment variable (None = not env-resolvable)
    default: Any                    # scaffold default
    cast: Callable[[Any], Any]      # applied to the env string only
    group: str                      # section of the reference table
    used_by: str                    # who reads it
    scoped_defaults: Dict[str, Any] = field(default_factory=dict)  # per-class default overrides
    secret: bool = False            # never written to .env.example with a value


def _k(key, env, default, cast, group, used_by, scoped=None, secret=False):
    return ConfigKey(key, env, default, cast, group, used_by, scoped or {}, secret)


E2A_CONFIG: Dict[str, ConfigKey] = {c.key: c for c in [
    # -- Propagation, logging, idempotency (all classes) --
    _k('io_config_prefix', 'IO_CONFIG_PREFIX', 'io_', str, 'Common', '_resolve_io_config()'),
    # -- Bindings and profiles (v1.6.0, design note MP-02) --
    _k('provider_profile', 'E2A_PROVIDER_PROFILE', None, str, 'Bindings', 'e2a.bindings.bindings_from_profile() — the profile name or path'),
    _k('profile_settings', None, None, dict, 'Bindings', 'resolve_config() — the profile layer, below env (filled by e2a.bindings)'),
    _k('required_capabilities', 'E2A_REQUIRED_CAPABILITIES', [], _to_json_list, 'Bindings', 'Bindings.verify() — capabilities the deployment relies on'),
    _k('provider_options', 'E2A_PROVIDER_OPTIONS', {}, _to_json_map, 'Bindings', 'provider transports only — {provider: {vendor parameters}}, never read by base classes'),
    _k('credential_provider', None, None, str, 'Bindings', 'BaseToolService — a BaseCredentialProvider (object reference); default: auth_token'),
    _k('dlp_patterns', 'E2A_DLP_PATTERNS', [], _to_json_list, 'LLM service', 'v1.6.0: extra egress DLP patterns [{name, regex, validator?}], compiled at construction'),
    _k('injection_patterns', 'E2A_INJECTION_PATTERNS', [], _to_json_list, 'LLM service', 'v1.6.0: extra prompt-injection patterns [{name, regex}], compiled at construction'),
    _k('dlp_patterns_replace', 'E2A_DLP_PATTERNS_REPLACE', False, _to_bool, 'LLM service', 'v1.6.0: true replaces the built-in DLP patterns instead of adding to them'),
    # -- v1.6.1 (design note MP-04): routes, features, telemetry, admission, scopes --
    _k('model_routes', 'E2A_MODEL_ROUTES', [], _to_json_list, 'LLM service', 'v1.6.1: [{on: [error kinds | "refusal"], to: model_id, llm_service: seam, max_attempts}] — answer a failure or refusal with another model, once'),
    _k('structured_output_strict', 'E2A_STRUCTURED_OUTPUT_STRICT', False, _to_bool, 'LLM service', 'v1.6.1: true makes a structured result that fails its schema FAILED (StructuredOutputInvalid); false only marks it'),
    _k('reasoning_effort', 'LLM_REASONING_EFFORT', None, str, 'LLM service', 'v1.6.1: minimal | low | medium | high | max; providers map it to their own control'),
    _k('model_price_table', 'E2A_MODEL_PRICE_TABLE', {}, _to_json_map, 'LLM service', 'v1.6.1: {model_id: {input_per_mtok, output_per_mtok, cache_read_per_mtok}} USD — sets cost_usd on results'),
    _k('quota_authority', 'E2A_QUOTA_AUTHORITY', 'e2a', str, 'LLM service', 'v1.6.1: e2a (the pod token bucket admits) | gateway (a gateway enforces quota; E2A skips its bucket, keeps concurrency)'),
    _k('admission_throttle_decay', 'E2A_ADMISSION_THROTTLE_DECAY', 0.5, float, 'LLM service', 'v1.6.1: a provider throttle multiplies the pod admission share by this'),
    _k('admission_throttle_floor', 'E2A_ADMISSION_THROTTLE_FLOOR', 0.1, float, 'LLM service', 'v1.6.1: the lowest admission share after throttles'),
    _k('admission_recovery_seconds', 'E2A_ADMISSION_RECOVERY_SECONDS', 60.0, float, 'LLM service', 'v1.6.1: seconds for the share to recover linearly to 1 after a throttle'),
    _k('guardrail_fields', 'GUARDRAIL_FIELDS', ['query'], _to_json_list, 'Governance', 'v1.6.1: ManagedGuardrailStage — state fields the classifier screens'),
    _k('guardrail_fail_open', 'GUARDRAIL_FAIL_OPEN', False, _to_bool, 'Governance', 'v1.6.1: true lets a request through when the classifier cannot answer (default: refuse)'),
    _k('delegated_max_turns', 'DELEGATED_MAX_TURNS', 8, int, 'LLM service', 'v1.6.1: DelegatedRuntime — most tool round-trips in one managed session'),
    _k('artifact_matrix', 'E2A_ARTIFACT_MATRIX', [], _to_json_list, 'Pipeline', 'v1.6.1: BasePipeline — the images to build (empty: the default matrix)'),
    _k('secret_scan_paths', 'E2A_SECRET_SCAN_PATHS', ['src'], _to_json_list, 'Pipeline', 'v1.6.1: BasePipeline built-in secret scan roots ([] turns it off)'),
    # -- v1.7.0 (design note MP-05): messaging, identity --
    _k('consumer_batch_size', 'E2A_CONSUMER_BATCH_SIZE', 10, int, 'Messaging', 'v1.7.0: IdempotentConsumer / BrokerOutboxSweeper — messages or records per batch'),
    _k('consumer_wait_seconds', 'E2A_CONSUMER_WAIT_SECONDS', 0.0, float, 'Messaging', 'v1.7.0: IdempotentConsumer.consume() — long-poll wait for a batch'),
    _k('consumer_max_deliveries', 'E2A_CONSUMER_MAX_DELIVERIES', 5, int, 'Messaging', 'v1.7.0: a message delivered more often is dead-lettered (0 = no limit; brokers without a count are not limited)'),
    _k('consumer_retry_delay_seconds', 'E2A_CONSUMER_RETRY_DELAY_SECONDS', 5.0, float, 'Messaging', 'v1.7.0: a retried or deferred message becomes visible again after this'),
    _k('identity_claim_map', 'E2A_IDENTITY_CLAIM_MAP', {}, _to_json_map, 'Identity', 'v1.7.0: {tenant_id, subject, roles, issuer: claim name or dotted path} — the identity provider\'s claim names'),
    _k('identity_trusted_issuers', 'E2A_IDENTITY_TRUSTED_ISSUERS', [], _to_json_list, 'Identity', 'v1.7.0: issuers accepted by BaseIdentityResolver (empty: any issuer the gateway verified)'),
    _k('identity_tenant_map', 'E2A_IDENTITY_TENANT_MAP', {}, _to_json_map, 'Identity', 'v1.7.0: external tenant id (an Entra directory id) -> E2A tenant_id'),
    _k('scopes', 'E2A_SCOPES', {}, _to_json_map, 'Bindings', 'v1.6.1: {"tenant:<id>": {key: value}, "agent:<name>": {...}} — overlays applied per call (tenant, then agent)'),
    _k('log_sink', 'LOG_SINK', 'stdout', str, 'Common', 'BaseObservability._ship_logs() / _flush_logs()'),
    _k('dlq_queue_url', 'DLQ_QUEUE_URL', None, str, 'Common', '_send_to_dlq()'),
    _k('commit_gate_enabled', 'COMMIT_GATE_ENABLED', True, _to_bool, 'Common', '_commit_gate()'),
    _k('expected_version', None, None, str, 'Common', '_commit_gate() — optional OCC version, caller-supplied per call'),
    _k('idempotency_store_url', 'IDEMPOTENCY_STORE_URL', None, str, 'Common', 'BaseWorkflow.__resolve_idempotency()'),
    _k('idempotency_ttl_seconds', 'IDEMPOTENCY_TTL_SECONDS', 86400, int, 'Common', '__lookup_idempotency_store()'),
    _k('tenant_routing_map', 'TENANT_ROUTING_MAP', {}, _to_json_map, 'Common', 'get_endpoint() — per-tenant overrides'),
    _k('observability_engine', None, None, str, 'Engines (object references)', 'BaseWorkflow.execute() — BaseObservability instance'),
    _k('governance_engine', None, None, str, 'Engines (object references)', 'BaseAgent.run() — BaseGovernanceFramework instance'),
    _k('llm_service', None, None, str, 'Engines (object references)', 'BaseAgent / LLMOnlyAgent _infer() — the bound BaseLLMAgentService (required)'),
    _k('max_token_budget', None, float('inf'), float, 'Engines (object references)', 'BaseGovernanceFramework.__enforce_token_budget() — io_config key'),
    # -- Validation --
    _k('schema_registry', 'VALIDATION_SCHEMA_REGISTRY_PATH', {}, _to_json_map, 'Validation', 'BaseValidationService validators'),
    _k('strict_mode', 'VALIDATION_STRICT_MODE', True, _to_bool, 'Validation', 'BaseValidationService.validate()'),
    # -- Agents --
    _k('min_confidence', 'MIN_CONFIDENCE', 0.85, float, 'Agents', 'BaseAgent.run(), LLMOnlyAgent.run() — fallback threshold'),
    _k('max_latency', 'MAX_LATENCY', 2.0, float, 'Agents', 'BaseAgent.run(), BaseRAGPipeline.retrieve(), BaseToolService.execute() — latency SLO, NFRViolationError on breach',
       scoped={'BaseToolService': 5.0, 'CQRS': 1.0}),
    _k('max_tokens', 'MAX_TOKENS', 5000, int, 'Agents', 'BaseAgent.run() — token budget per call/workflow'),
    _k('approved_agents', 'APPROVED_AGENTS', [], _to_json_list, 'Governance', 'BaseAgent.run() — agent_name must be listed'),
    _k('approved_models', 'APPROVED_MODELS', [], _to_json_list, 'Governance', 'BaseRAGPipeline — embedding / reranker model allow-list'),
    _k('approved_tools', 'APPROVED_TOOLS', [], _to_json_list, 'Governance', 'BaseToolService — tool_name must be listed'),
    _k('prompt_template', 'PROMPT_TEMPLATE', None, str, 'Agents', 'BaseAgent._build_messages() — default prompt template when no registry is used'),
    _k('cache_refresh_token', None, 'v1', str, 'Agents', 'BaseAgent._build_messages() — mutate to force a context-cache miss'),
    _k('model_capability_registry', 'MODEL_CAPABILITY_REGISTRY_PATH', {}, _to_json_map, 'Multimodal', 'LLMOnlyAgent._select_model()'),
    _k('stt_provider', 'STT_PROVIDER', None, str, 'Multimodal', 'ModalityPreprocessor.transcribe()'),
    _k('document_extractor', 'DOCUMENT_EXTRACTOR_PROVIDER', None, str, 'Multimodal', 'ModalityPreprocessor.extract()'),
    _k('max_audio_duration_seconds', 'MAX_AUDIO_DURATION_SECONDS', 600, int, 'Multimodal', 'checked before transcribe() — NFRViolationError on breach'),
    _k('max_document_pages', 'MAX_DOCUMENT_PAGES', 50, int, 'Multimodal', 'checked before extract() — NFRViolationError on breach'),
    # -- LLM inference service (BaseLLMAgentService) --
    _k('model_id', 'LLM_MODEL_ID', 'default', str, 'LLM service', '_dispatch_upstream_call() — provider routing; LLMOnlyAgent._select_model() default'),
    _k('temperature', 'LLM_TEMPERATURE', 0.0, float, 'LLM service', '_dispatch_upstream_call()'),
    _k('context_budget', 'LLM_CONTEXT_BUDGET', 8192, int, 'LLM service', '_manage_context_budget() — input token budget'),
    _k('max_output_tokens', 'LLM_MAX_OUTPUT_TOKENS', 2048, int, 'LLM service', 'quota estimate and upstream limit'),
    _k('llm_max_latency', 'LLM_MAX_LATENCY', 3.0, float, 'LLM service', 'upstream call deadline — NFRViolationError on breach'),
    _k('llm_max_retries', 'LLM_MAX_RETRIES', 2, int, 'LLM service', 'v1.5.1: retries of a model call whose failure is in llm_retry_on'),
    _k('llm_retry_on', 'LLM_RETRY_ON', ['transient', 'throttled', 'overloaded'], _to_json_list, 'LLM service', 'v1.5.1: error kinds (ErrorClass) that are retried'),
    _k('circuit_counts', 'LLM_CIRCUIT_COUNTS', ['transient', 'overloaded', 'unknown'], _to_json_list, 'LLM service', 'v1.5.1: error kinds that count toward the circuit breaker (throttling does not)'),
    _k('llm_retry_after_cap', 'LLM_RETRY_AFTER_CAP', 30.0, float, 'LLM service', 'v1.5.1: a Retry-After longer than this (seconds) is not waited for: the call fails'),
    _k('pod_concurrency', 'LLM_POD_CONCURRENCY', 32, int, 'LLM service', 'pod semaphore — in-flight upstream calls'),
    _k('tenant_concurrency', 'LLM_TENANT_CONCURRENCY', 0, int, 'LLM service', 'per-tenant cap within the pod (0 = off)'),
    _k('llm_replica_count', 'LLM_REPLICA_COUNT', 1, int, 'LLM service', 'divides max_rpm / max_tpm into a per-pod share'),
    _k('max_rpm', 'LLM_MAX_RPM', 600, int, 'LLM service', 'fleet requests-per-minute limit'),
    _k('max_tpm', 'LLM_MAX_TPM', 300000, int, 'LLM service', 'fleet tokens-per-minute limit'),
    _k('quota_timeout', 'LLM_QUOTA_TIMEOUT', 5.0, float, 'LLM service', 'max wait for quota before NFRViolationError'),
    _k('enable_context_caching', 'LLM_ENABLE_CONTEXT_CACHING', True, _to_bool, 'LLM service', '_apply_kv_caching()'),
    _k('min_cache_threshold', 'LLM_MIN_CACHE_THRESHOLD', 1024, int, 'LLM service', '_apply_kv_caching() — minimum static prefix tokens'),
    _k('enable_semantic_cache', 'LLM_ENABLE_SEMANTIC_CACHE', True, _to_bool, 'LLM service', '_check_semantic_cache() / _store_semantic_cache()'),
    _k('dlp_enabled', 'LLM_DLP_ENABLED', True, _to_bool, 'LLM service', 'egress and stream redaction'),
    _k('dlp_holdback_chars', 'LLM_DLP_HOLDBACK_CHARS', 128, int, 'LLM service', 'stream holdback window'),
    _k('speculative_model', 'SPECULATIVE_MODEL', None, str, 'LLM service', '_dispatch_upstream_call() io_config — speculative-decoding draft model'),
    _k('max_speculative_tokens', 'MAX_SPECULATIVE_TOKENS', None, int, 'LLM service', '_dispatch_upstream_call() io_config — draft length'),
    # -- RAG --
    _k('faithfulness_threshold', 'FAITHFULNESS_THRESHOLD', 0.85, float, 'RAG', 'BaseRAGPipeline._evaluate_answer(); BasePipeline RAG eval gate'),
    _k('rag_top_k', 'RAG_TOP_K', 5, int, 'RAG', 'BaseRAGPipeline.retrieve() — result count'),
    _k('embedding_model', 'EMBEDDING_MODEL', 'amazon.titan-embed-text-v1', str, 'RAG', 'BaseRAGPipeline.__embed()'),
    _k('hybrid_alpha', 'HYBRID_ALPHA', 0.5, float, 'RAG', 'BaseRAGPipeline._search_index() — vector vs keyword weight'),
    _k('similarity_metric', 'SIMILARITY_METRIC', 'COSINE', str, 'RAG', 'BaseRAGPipeline._search_index() io_config'),
    _k('index_name', 'INDEX_NAME', None, str, 'RAG', 'BaseRAGPipeline._search_index() — index / collection'),
    _k('tenant_index_prefix', 'TENANT_INDEX_PREFIX', None, str, 'RAG', 'BaseRAGPipeline._search_index() — f"{prefix}_{tenant_id}" namespace'),
    _k('rerank_top_n', 'RERANK_TOP_N', None, int, 'RAG', 'BaseRAGPipeline._rerank()'),
    # -- Tools and MCP --
    _k('timeout', 'TOOL_TIMEOUT', 5.0, float, 'Tools / MCP', 'BaseToolService.__http_post()'),
    _k('retries', 'TOOL_RETRIES', 3, int, 'Tools / MCP', 'BaseToolService.execute()'),
    _k('tool_base_url', 'TOOL_BASE_URL', 'http://localhost', str, 'Tools / MCP', 'RestToolService.get_endpoint()'),
    _k('auth_token', 'TOOL_AUTH_TOKEN', None, str, 'Tools / MCP', 'BaseToolService.__http_post()', secret=True),
    _k('mcp_server_url', 'MCP_SERVER_URL', None, str, 'Tools / MCP', 'MCPToolService.get_endpoint()'),
    _k('mcp_protocol_version', 'MCP_PROTOCOL_VERSION', '2025-06-18', str, 'Tools / MCP', 'BaseMCPServer.serve()'),
    _k('mcp_server_name', 'MCP_SERVER_NAME', 'e2a-mcp-server', str, 'Tools / MCP', 'BaseMCPServer — logging identity'),
    _k('tool_filter', 'MCP_TOOL_FILTER', None, str, 'Tools / MCP', 'BaseMCPServer — exposed-tool allow-list'),
    _k('mcp_server_registry', 'MCP_SERVER_REGISTRY_PATH', {}, _to_json_map, 'Tools / MCP', '_resolve_tool_call() — checked first'),
    _k('tool_registry', 'TOOL_REGISTRY_PATH', {}, _to_json_map, 'Tools / MCP', '_get_tool_service() — fallback path'),
    _k('default_transport', 'DEFAULT_TOOL_TRANSPORT', 'http', str, 'Tools / MCP', '_get_tool_service() — final fallback'),
    _k('max_retries', 'WEBHOOK_MAX_RETRIES', 3, int, 'Tools / MCP', 'BaseWebhookDispatcher.dispatch()'),
    _k('signing_secret', 'WEBHOOK_SIGNING_SECRET', None, str, 'Tools / MCP', 'BaseWebhookDispatcher._sign_payload()', secret=True),
    # -- Workflow, prompts, pipeline --
    _k('shadow_mode_enabled', 'SHADOW_MODE_ENABLED', False, _to_bool, 'Workflow / Pipeline', 'BaseWorkflow.execute() — Blue-Green / shadow trials'),
    _k('shadow_target_pipeline', 'SHADOW_TARGET_PIPELINE', None, str, 'Workflow / Pipeline', 'BaseWorkflow.execute() — shadow target'),
    _k('prompt_store_url', 'PROMPT_STORE_URL', None, str, 'Workflow / Pipeline', 'BasePromptRegistry.get_prompt()'),
    _k('default_version', 'PROMPT_DEFAULT_VERSION', 'latest', str, 'Workflow / Pipeline', 'BasePromptRegistry.get_prompt()'),
    _k('deployment_strategy', 'DEPLOYMENT_STRATEGY', 'BLUE_GREEN', str, 'Workflow / Pipeline', 'BasePipeline._execute_deployment_rollout()'),
    # -- Object references added by the concurrency extension --
    _k('idempotency_store', None, None, str, 'Engines (object references)', 'BaseWorkflow.execute() / BaseOrchestrator.execute() / AIDLCPipelineOrchestrator — the bound BaseIdempotencyStore (atomic claim)'),
    _k('cache_store', None, None, str, 'Engines (object references)', 'BaseQueryService.fetch() — the bound BaseCacheStore (single-flight get_or_load)'),
    _k('transactional_store', None, None, str, 'Engines (object references)', 'BaseCommandService._commit_versioned() — the bound BaseTransactionalStore (conditional commit)'),
    _k('pipeline_state_store', None, None, str, 'Engines (object references)', 'AIDLCPipelineOrchestrator — the bound BaseTransactionalStore (durable phase state)'),
    # -- Deadlines --
    _k('request_deadline_seconds', 'REQUEST_DEADLINE_SECONDS', 0.0, float, 'Deadlines', 'BaseWorkflow.execute() / BaseOrchestrator.execute() — origin of the additive deadline field (0 = none)'),
    # -- LLM service admission (extends the LLM service group) --
    _k('permit_timeout', 'LLM_PERMIT_TIMEOUT', 5.0, float, 'LLM service', 'bounded wait for the tenant + pod permit — AdmissionTimeout on expiry (also capped by the remaining deadline)'),
    _k('tenant_idle_evict_seconds', 'LLM_TENANT_IDLE_EVICT_SECONDS', 300.0, float, 'LLM service', 'idle per-tenant permit entries are evicted after this long'),
    # -- Resilience (retry budget, circuit breaker, bulkhead) — owned by the adapter that calls the dependency --
    _k('retry_base_backoff', 'RETRY_BASE_BACKOFF', 0.1, float, 'Resilience', 'ResiliencePolicy — full-jitter exponential backoff base (seconds)'),
    _k('retry_max_backoff', 'RETRY_MAX_BACKOFF', 2.0, float, 'Resilience', 'ResiliencePolicy — backoff ceiling (seconds)'),
    _k('retry_budget_ratio', 'RETRY_BUDGET_RATIO', 0.2, float, 'Resilience', 'ResiliencePolicy — retry tokens earned per first attempt (caps retry amplification)'),
    _k('retry_budget_cap', 'RETRY_BUDGET_CAP', 10.0, float, 'Resilience', 'ResiliencePolicy — maximum retry-token balance'),
    _k('circuit_failure_threshold', 'CIRCUIT_FAILURE_THRESHOLD', 5, int, 'Resilience', 'ResiliencePolicy — consecutive failures that open the circuit'),
    _k('circuit_reset_seconds', 'CIRCUIT_RESET_SECONDS', 30.0, float, 'Resilience', 'ResiliencePolicy — open duration before a half-open probe'),
    _k('bulkhead_max_concurrent', 'BULKHEAD_MAX_CONCURRENT', 16, int, 'Resilience', 'ResiliencePolicy — per-dependency in-flight cap (0 = off)'),
    _k('bulkhead_acquire_timeout', 'BULKHEAD_ACQUIRE_TIMEOUT', 1.0, float, 'Resilience', 'ResiliencePolicy — bounded wait for a bulkhead slot'),
    # -- Pools and fleet capacity (inputs to validate_capacity()) --
    _k('db_pool_size', 'DB_POOL_SIZE', 20, int, 'Pools / capacity', 'store adapters — per-instance database connection pool'),
    _k('http_pool_size', 'HTTP_POOL_SIZE', 50, int, 'Pools / capacity', 'tool / upstream adapters — per-instance HTTP connection pool'),
    _k('retrieval_pool_size', 'RETRIEVAL_POOL_SIZE', 20, int, 'Pools / capacity', 'BaseRAGPipeline._search_index() adapters — per-instance index connection pool'),
    _k('fleet_replica_count', 'FLEET_REPLICA_COUNT', 1, int, 'Pools / capacity', 'validate_capacity() — replicas of this service (multiplies per-instance concurrency)'),
    _k('db_max_connections', 'DB_MAX_CONNECTIONS', 0, int, 'Pools / capacity', 'validate_capacity() — database connection ceiling for the whole fleet (0 = unchecked)'),
    _k('model_max_inflight', 'MODEL_MAX_INFLIGHT', 0, int, 'Pools / capacity', 'validate_capacity() — provider-side concurrent request ceiling for the whole fleet (0 = unchecked)'),
    _k('termination_grace_seconds', 'TERMINATION_GRACE_SECONDS', 30.0, float, 'Pools / capacity', 'validate_capacity() — platform SIGTERM-to-SIGKILL window; drain_timeout_seconds must be shorter'),
    # -- Async workers --
    _k('worker_concurrency', 'WORKER_CONCURRENCY', 8, int, 'Async workers', 'consumer runtime — in-flight messages per instance'),
    _k('worker_prefetch', 'WORKER_PREFETCH', 16, int, 'Async workers', 'consumer runtime — broker prefetch / max in-flight window per instance'),
    _k('drain_timeout_seconds', 'DRAIN_TIMEOUT_SECONDS', 20.0, float, 'Async workers', 'consumer runtime / services — graceful-shutdown drain bound'),
    # -- Leases --
    _k('lease_ttl_seconds', 'LEASE_TTL_SECONDS', 60, int, 'Leases', 'BaseOutboxSweeper / BaseReconciliationService — BaseDistributedLock lease TTL'),
    _k('lease_renew_interval_seconds', 'LEASE_RENEW_INTERVAL_SECONDS', 20, int, 'Leases', 'BaseOutboxSweeper / BaseReconciliationService — renew cadence (must be < lease_ttl_seconds)'),
    _k('fencing_enabled', 'FENCING_ENABLED', True, _to_bool, 'Leases', 'lease holders pass LeaseHandle.fencing_token to downstream commits'),
    _k('idempotency_lease_seconds', 'IDEMPOTENCY_LEASE_SECONDS', 300, int, 'Leases', 'BaseIdempotencyStore.claim() — IN_PROGRESS claim expiry; an expired claim needs reconciliation, never blind re-execution'),
    # -- Cache single-flight --
    _k('cache_fill_timeout_seconds', 'CACHE_FILL_TIMEOUT_SECONDS', 5.0, float, 'Cache', 'BaseCacheStore.get_or_load() — follower wait for the in-flight fill'),
    _k('cache_fill_on_timeout', 'CACHE_FILL_ON_TIMEOUT', 'load', str, 'Cache', 'BaseCacheStore.get_or_load() — follower policy on fill timeout: load | stale | raise'),
    # -- Build admission (AI-DLC) --
    _k('build_max_inflight', 'BUILD_MAX_INFLIGHT', 4, int, 'Build (AI-DLC)', 'AIDLCPipelineOrchestrator — concurrent builds per instance'),
    _k('tenant_build_max_inflight', 'TENANT_BUILD_MAX_INFLIGHT', 2, int, 'Build (AI-DLC)', 'AIDLCPipelineOrchestrator — concurrent builds per tenant'),
    # -- Concurrency observability --
    _k('slow_wait_threshold_seconds', 'SLOW_WAIT_THRESHOLD_SECONDS', 0.5, float, 'Concurrency observability', 'permit / pool / bulkhead waits above this are logged at WARN'),
    _k('queue_age_slo_seconds', 'QUEUE_AGE_SLO_SECONDS', 60.0, float, 'Concurrency observability', 'oldest-message age above this is an SLO breach signal'),
]}


def _same_cast(a, b, default) -> bool:
    """v1.6.1: two casts mean the same when they are the same function or read the default's
    environment spelling alike (a provider package's own list parser and E2A's JSON list
    parser both read '["query"]' as ['query']), so a key E2A takes over from a provider
    package keeps working with the package's earlier registration."""
    if a is b:
        return True
    probe = json.dumps(default) if isinstance(default, (list, dict)) else ('' if default is None else str(default))
    try:
        return a(probe) == b(probe)
    except Exception:
        return False


def register_config_key(spec: ConfigKey) -> ConfigKey:
    """Add a project-specific key to the central registry (call once at
    start-up, e.g. from src/settings.py). Re-registering an identical spec
    is a no-op; a conflicting duplicate raises."""
    existing = E2A_CONFIG.get(spec.key)
    if existing is not None and existing != spec:
        # v1.6.0: the same key with the same meaning (env, default, type, secrecy) is
        # compatible even if its description differs, e.g. a key a provider package registered
        # before E2A took it over. The registered spec is kept; only a real conflict raises.
        same = (existing.env, existing.default, existing.scoped_defaults, existing.secret) == \
               (spec.env, spec.default, spec.scoped_defaults, spec.secret) and _same_cast(existing.cast, spec.cast, spec.default)
        if not same:
            raise ValueError(f"config key '{spec.key}' is already registered differently")
        return existing
    E2A_CONFIG[spec.key] = spec
    return spec


def resolve_config(key: str, config: Optional[Dict[str, Any]] = None,
                   kwargs: Optional[Dict[str, Any]] = None, scope: Optional[str] = None):
    """kwargs > config > environment variable > profile > default, for any
    registered key. `scope` selects a per-class default (e.g. 'BaseToolService').
    The profile layer (v1.6.0) is config['profile_settings'], which e2a.bindings
    fills from a profile file: an environment variable overrides a profile value."""
    spec = E2A_CONFIG.get(key)
    if spec is None:
        raise KeyError(f"'{key}' is not in the central E2A_CONFIG registry — "
                       f"add it with register_config_key()")
    if kwargs and kwargs.get(key) is not None:
        return kwargs[key]
    if config and config.get(key) is not None:
        return config[key]
    if spec.env:
        raw = os.getenv(spec.env)
        if raw is not None:
            return spec.cast(raw)
    profile = config.get('profile_settings') if config else None
    if isinstance(profile, dict) and profile.get(key) is not None:
        return profile[key]
    return spec.scoped_defaults.get(scope, spec.default) if scope else spec.default


# v1.6.1 (P3): keys a scope may not change. They identify or guard the request itself.
UNSCOPABLE_KEYS = frozenset({'scopes', 'tenant_id', 'correlation_id', 'profile_settings', 'expected_version',
                             'checkpoint_store', 'idempotency_store', 'idempotency_lease_seconds'})


def scoped_config(config: Optional[Dict[str, Any]], agent_name: Optional[str] = None,
                  tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """v1.6.1 (design note MP-04, P3): config with the matching `scopes` overlays applied,
    tenant first, then agent (the agent's own settings are the more specific).

        scopes = {"tenant:acme": {"max_output_tokens": 1024},
                  "agent:triage": {"model_id": "small-model", "temperature": 0.0}}

    An overlay sets config values, so it sits at the config layer: a per-call keyword still
    wins, and an overlay beats environment variables and the profile. A scope key that is not
    registered, or one that identifies the request (UNSCOPABLE_KEYS), raises ValueError, so a
    typo fails at the first call rather than being silently ignored. No scopes: config as given."""
    config = config or {}
    scopes = resolve_config('scopes', config) or {}
    if not scopes:
        return config
    out = dict(config)
    for label in ([f'tenant:{tenant_id}'] if tenant_id else []) + ([f'agent:{agent_name}'] if agent_name else []):
        overlay = scopes.get(label)
        if not overlay:
            continue
        if not isinstance(overlay, dict):
            raise ValueError(f'scopes[{label!r}] must be a mapping of config keys')
        bad = sorted(k for k in overlay if k in UNSCOPABLE_KEYS)
        if bad:
            raise ValueError(f'scopes[{label!r}] may not set {", ".join(bad)}')
        unknown = [k for k in unknown_config_keys(overlay)]
        if unknown:
            raise ValueError(f'scopes[{label!r}]: unregistered config keys {", ".join(unknown)}')
        out.update(overlay)
    for label in scopes:
        if not (isinstance(label, str) and label.partition(':')[0] in ('tenant', 'agent') and label.partition(':')[2]):
            raise ValueError(f'scopes: {label!r} must be "tenant:<id>" or "agent:<name>"')
    return out


def unknown_config_keys(config: Optional[Dict[str, Any]]) -> List[str]:
    """Keys in a config dict that are neither registered nor io_ prefixed —
    a typo guard to call at start-up."""
    prefix = resolve_config('io_config_prefix', config)
    return sorted(k for k in (config or {}) if k not in E2A_CONFIG and not k.startswith(prefix))


def config_reference_markdown() -> str:
    """The Section 5 reference table, grouped, generated from the registry."""
    out, groups = [], []
    for c in E2A_CONFIG.values():
        if c.group not in groups:
            groups.append(c.group)
    for g in groups:
        out += [f'### {g}', '', '| Config key | Environment variable | Default | Used by |', '|---|---|---|---|']
        for c in (x for x in E2A_CONFIG.values() if x.group == g):
            env = c.env or '— (not env-resolvable)'
            d = '(unset)' if c.default is None else repr(c.default)
            if c.scoped_defaults:
                d += ' (' + ', '.join(f'{v!r} for {k}' for k, v in c.scoped_defaults.items()) + ')'
            out.append(f'| {c.key} | {env} | {d} | {c.used_by} |')
        out.append('')
    return '\n'.join(out)


def env_example() -> str:
    """A .env.example listing every env-resolvable key with its default.
    Secrets are emitted empty."""
    lines = ['# Generated from E2A_CONFIG — edit values, never add keys here;',
             '# register project keys with register_config_key().']
    group = None
    for c in E2A_CONFIG.values():
        if not c.env:
            continue
        if c.group != group:
            group = c.group
            lines += ['', f'# --- {group} ---']
        val = '' if (c.secret or c.default is None) else (
            json.dumps(c.default) if isinstance(c.default, (dict, list)) else str(c.default))
        lines.append(f'{c.env}={val}')
    return '\n'.join(lines) + '\n'
