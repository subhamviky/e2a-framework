# e2a_base.py — E2A Architecture Framework Scaffold
# Drop into src/ or framework/. Import and subclass. Never modify.
# All config keys / env vars live in E2A_CONFIG (central registry below).
# github.com/subhamviky/e2a-framework
 
import os
import re
import json
import uuid
import time
import asyncio
import hashlib
import logging
import threading
import contextlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional
 
logging.basicConfig(level=logging.INFO)
 
 
class DehydrationInterrupt(Exception):
    """Signal exception, not an error. Raised exclusively by
    BaseGovernanceFramework.__trigger_dehydration() and caught exclusively
    by BaseWorkflow.execute() / BaseAgent.run(). Must never reach
    _handle_error() or the DLQ — a paused-for-approval request has not
    failed."""
    pass
 
 
class NFRViolationError(Exception):
    """Raised whenever a measured non-functional requirement is breached —
    a latency SLO in BaseAgent.run() / BaseRAGPipeline.retrieve() /
    BaseToolService.execute(), a capacity rejection or token-budget
    exhaustion in BaseGovernanceFramework. Unlike DehydrationInterrupt, not
    caught separately — falls through the normal except Exception path, so
    a breach is a regular failure: added to failed_keys, handed to
    _handle_error(), routed to the DLQ like any other exception."""
    pass
 
 

_BRIDGE = {'loop': None}
_BRIDGE_LOCK = threading.Lock()


def _run_on_bridge_loop(coro):
    """Run a coroutine to completion on one shared background event
    loop (created lazily, daemon thread) and return its result."""
    with _BRIDGE_LOCK:
        if _BRIDGE['loop'] is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, daemon=True,
                             name='e2a-llm-bridge').start()
            _BRIDGE['loop'] = loop
    return asyncio.run_coroutine_threadsafe(coro, _BRIDGE['loop']).result()


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
]}


def register_config_key(spec: ConfigKey) -> ConfigKey:
    """Add a project-specific key to the central registry (call once at
    start-up, e.g. from src/settings.py). Re-registering an identical spec
    is a no-op; a conflicting duplicate raises."""
    existing = E2A_CONFIG.get(spec.key)
    if existing is not None and existing != spec:
        raise ValueError(f"config key '{spec.key}' is already registered differently")
    E2A_CONFIG[spec.key] = spec
    return spec


def resolve_config(key: str, config: Optional[Dict[str, Any]] = None,
                   kwargs: Optional[Dict[str, Any]] = None, scope: Optional[str] = None):
    """kwargs > config > environment variable > default, for any registered
    key. `scope` selects a per-class default (e.g. 'BaseToolService')."""
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
    return spec.scoped_defaults.get(scope, spec.default) if scope else spec.default


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

# ==================================================
# Shared propagation helpers (mixed into every class below)
# ==================================================
 
class _PropagationMixin:
    """Shared, base-owned helpers for correlation/log/DLQ handling.
    Never overridden by subclasses."""
 
    def _resolve_io_config(self, config=None, **kwargs):
        config = config or {}
        prefix = resolve_config('io_config_prefix', config, kwargs)
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
        sink = resolve_config('log_sink', config, kwargs)
        for entry in (message_log or []):
            logging.info(entry) if sink == 'stdout' else None
            # cloudwatch / datadog / stackdriver dispatch goes here
 
    def _send_to_dlq(self, failed_keys, config=None, **kwargs):
        config = config or {}
        queue_url = resolve_config('dlq_queue_url', config, kwargs)
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
        enabled = resolve_config('commit_gate_enabled', config, kwargs)
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

    # ---- LLM service bridge (the ONLY way any agent reaches a model) ----
    def _resolve_llm_service(self, config=None, **kwargs):
        config = config or {}
        svc = kwargs.get('llm_service') or config.get('llm_service')
        if not isinstance(svc, BaseLLMAgentService):
            raise ValueError("config['llm_service'] must be a BaseLLMAgentService")
        return svc

    async def _infer_async(self, messages, config=None, **kwargs) -> Dict[str, Any]:
        """Awaitable bridge for async callers (orchestrators, tests)."""
        svc = self._resolve_llm_service(config, **kwargs)
        kwargs.pop('llm_service', None)
        result = await svc.process_inference(messages, config, **kwargs)
        if result.get('status') == 'FAILED':
            raise RuntimeError(f"LLM inference failed: {result.get('error')}")
        return result

    def _infer(self, messages, config=None, **kwargs) -> Dict[str, Any]:
        """Sync bridge for BaseAgent.run() / LLMOnlyAgent._handle_*().
        Runs the coroutine on one process-wide background loop so the
        service's semaphores and token bucket are always bound to the
        same event loop, whatever thread the agent runs on. A FAILED
        inference raises, so the caller's normal except path
        (failed_keys + _handle_error) applies."""
        return _run_on_bridge_loop(self._infer_async(messages, config, **kwargs))
 
# ==================================================
# BaseObservability & BaseGovernanceFramework — Foundation classes
# referenced by BaseAgent.run() / BaseWorkflow.execute() below.
# Defined ahead of BaseValidationService since both are wired in via
# config['observability_engine'] / config['governance_engine'] before
# any agent-specific class is reached.
# ==================================================
 
class BaseObservability(ABC):
    """Template method abstract class for Enterprise Telemetry.
    record_telemetry() is the single public entry point (Section 2.10).
    A subclass that omits any @abstractmethod hook below fails at
    instantiation rather than shipping a no-op integration to production."""
 
    def record_telemetry(self, message_log: List[dict], correlation_id: str,
                          config: Dict[str, Any] = None, **kwargs) -> None:
        config = config or {}
        enriched_logs = []
        for entry in (message_log or []):
            enriched_logs.append({
                **entry,
                'correlation_id': correlation_id,
                'tenant_id': kwargs.get('tenant_id', 'UNKNOWN_TENANT'),
                'host_epoch_ms': int(time.time() * 1000),
                'framework_version': 'e2a-v1.0',
            })
        metrics = self.__extract_metrics(enriched_logs)
        try:
            self._ship_logs(enriched_logs, config, **kwargs)
            if metrics:
                self._emit_metrics(metrics, config, **kwargs)
            self._export_traces(correlation_id, enriched_logs, config, **kwargs)
        except Exception as e:
            logging.error(f'Telemetry dispatch failed for {correlation_id}: {e}')
            for log in enriched_logs:
                logging.info(log)
 
    def __extract_metrics(self, enriched_logs: List[dict]) -> Dict[str, float]:
        # Base-owned, never overridden. Single pass: counts ERROR-level
        # entries into error_count, sums any tokens_used field into
        # total_tokens.
        metrics = {'total_tokens': 0.0, 'error_count': 0}
        for log in enriched_logs:
            if log.get('level') == 'ERROR':
                metrics['error_count'] += 1
            if 'tokens_used' in log:
                metrics['total_tokens'] += log['tokens_used']
        return metrics
 
    @abstractmethod
    def _ship_logs(self, enriched_logs: List[dict], config: Dict[str, Any], **kwargs) -> None:
        """kwargs: log_sink. Ship enriched log entries to the configured
        sink (CloudWatch Logs / Datadog / Cloud Logging)."""
 
    @abstractmethod
    def _emit_metrics(self, metrics: Dict[str, float], config: Dict[str, Any], **kwargs) -> None:
        """kwargs: namespace, dimensions. Emit the aggregate metrics
        record_telemetry() derived (total_tokens, error_count)."""
 
    @abstractmethod
    def _export_traces(self, correlation_id: str, logs: List[dict],
                        config: Dict[str, Any], **kwargs) -> None:
        """kwargs: trace_id, service_name. If trace_context (Section 2.1)
        is present, this is where it is forwarded as the W3C traceparent
        header to the next hop — correlation_id remains the internal
        join key; trace_context is what crosses the wire."""
 
    # Optional typed emitters — a subclass overrides these instead of, or
    # alongside, _emit_metrics() if the metrics backend distinguishes
    # metric types (OTel Counter/Histogram/Gauge). Default no-op: a
    # subclass that only implements _emit_metrics() above still works.
    def _emit_counter(self, name: str, value: float, dimensions: Dict[str, Any] = None,
                       config: Dict[str, Any] = None, **kwargs) -> None:
        pass
 
    def _emit_histogram(self, name: str, value: float, dimensions: Dict[str, Any] = None,
                         config: Dict[str, Any] = None, **kwargs) -> None:
        pass
 
    def _emit_gauge(self, name: str, value: float, dimensions: Dict[str, Any] = None,
                     config: Dict[str, Any] = None, **kwargs) -> None:
        pass
 
 
class BaseGovernanceFramework(ABC):
    """Template method abstract class for AI Safety, Economics &
    Lifecycle Governance (Section 2.11). Two public methods:
    enforce_governance_gate() runs once, pre-execution; redact_response()
    runs once, after a response exists. Kept separate rather than one
    method, because a response to redact does not exist at the point
    enforce_governance_gate() runs."""
 
    def enforce_governance_gate(self, state: Dict[str, Any], io_config: Dict[str, Any],
                                 config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        if not self._check_execution_capacity(state, io_config, config, **kwargs):
            raise NFRViolationError('Execution capacity exceeded; admission rejected.')
        self.__enforce_token_budget(state, io_config)
        if state.get('requires_human_approval') and not state.get('approval_granted'):
            self.__trigger_dehydration(state, config, **kwargs)
        if state.get('pending_mcp_tool_execution'):
            self._verify_sandbox_profile(config, **kwargs)
        state = self._execute_semantic_firewall(state, config, **kwargs)
        return state
 
    def redact_response(self, state: Dict[str, Any], response: Dict[str, Any],
                         config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        """Second public method on this class. Called once from
        BaseAgent.run(), after _evaluate_output() produces a response —
        never from enforce_governance_gate()."""
        config = config or {}
        return self._redact_output_fields(state, response, config, **kwargs)
 
    def __enforce_token_budget(self, state: Dict[str, Any], io_config: Dict[str, Any]) -> None:
        # Base-owned. Stops the request before another LLM call is made,
        # not after.
        max_budget = resolve_config('max_token_budget', io_config)
        used = state.get('cumulative_tokens_used', 0)
        if used >= max_budget:
            raise ValueError(
                f'Governance Violation: token budget exhausted. '
                f'Used {used}, Limit {max_budget}')
 
    def __trigger_dehydration(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> None:
        correlation_id = kwargs.get('correlation_id', state.get('correlation_id'))
        tenant_id = kwargs.get('tenant_id', state.get('tenant_id'))
        self._dehydrate_state_to_perimeter(state, correlation_id, tenant_id, config)
        raise DehydrationInterrupt(f'State {correlation_id} dehydrated. Awaiting webhook.')
 
    @abstractmethod
    def _check_execution_capacity(self, state: Dict[str, Any], io_config: Dict[str, Any],
                                   config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: max_concurrency, queue_depth_threshold. Consolidated
        capacity-aware admission check — edge load-shedding and worker
        backpressure in one contract, not two. Return False to reject
        admission before any LLM call or downstream dependency is
        touched."""
 
    @abstractmethod
    def _evaluate_dependency_health(self, config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: health_check_targets. Checks downstream dependency
        health (data store, cache, external API). Called by a concrete
        _check_execution_capacity() implementation as one of its inputs —
        a separate hook so health-check behavior and admission-threshold
        behavior can be overridden independently."""
 
    @abstractmethod
    def _execute_semantic_firewall(self, state: Dict[str, Any], config: Dict[str, Any],
                                    **kwargs) -> Dict[str, Any]:
        """kwargs: policy_file, redaction_rules. Run local guard models
        for PII redaction and prompt-injection blocking on the inbound
        payload; returns the (possibly redacted) state. Ingress
        direction — governs what reaches the model."""
 
    @abstractmethod
    def _redact_output_fields(self, state: Dict[str, Any], response: Dict[str, Any],
                               config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        """kwargs: masking_policy_registry. Egress-direction counterpart
        to _execute_semantic_firewall() — field-level masking on the
        response. Called only from redact_response()."""
 
    @abstractmethod
    def _verify_sandbox_profile(self, config: Dict[str, Any], **kwargs) -> None:
        """Assert the current execution context is an isolated microVM
        (gVisor/Firecracker) before a pending MCP tool call proceeds."""
 
    @abstractmethod
    def _circuit_breaker(self, failures: int, config: Dict[str, Any], **kwargs) -> bool:
        """kwargs: fail_max, reset_timeout. Circuit breaker state
        transitions + metric emission."""
 
    @abstractmethod
    def _dehydrate_state_to_perimeter(self, state: Dict[str, Any], correlation_id: str,
                                       tenant_id: str, config: Dict[str, Any]) -> None:
        """Persist state to the Data & Messaging Perimeter ahead of a
        human-in-the-loop pause; called only by __trigger_dehydration()."""
 
 
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
            strict = resolve_config('strict_mode', config, kwargs)
            if strict:
                return self.__build_result(
                    False, [f'No validator registered for {agent_name}'],
                    agent_name, correlation_id)
            return self.__build_result(True, [], agent_name, correlation_id)
        valid, errors = validator(state, config, **kwargs)
        if not valid:
            return self.__build_result(valid, errors, agent_name, correlation_id)
        return self._enforce_entitlement_gate(agent_name, state, config, **kwargs)
 
    def _resolve_validator(self, agent_name, config=None, **kwargs):
        """Same registry-resolution pattern as BaseWorkflow._get_agent()
        and _get_tool_service() — name-keyed lookup, not if/elif chains."""
        return self.validator_registry.get(agent_name)
 
    @abstractmethod
    def _enforce_entitlement_gate(self, agent_name: str, state: Dict[str, Any],
                                   config: Dict[str, Any] = None,
                                   **kwargs) -> 'ValidationResult':
        """ABAC check — evaluates the caller's resolved claims against
        a policy for the agent_name/resource combination. kwargs:
        entitlement_policy_registry. Runs only after schema validation
        already succeeded."""
 
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
# BaseLLMAgentService — LLM inference microservice
# Peer of BaseAgent / BaseRAGPipeline / BaseToolService / BaseMCPServer.
# Owns every model-facing concern so no agent class does: capacity
# (pod semaphore + RPM/TPM token bucket), context budgeting, KV-prefix
# and semantic caching hooks, tool-schema compilation, ingress
# prompt-injection sanitising, egress/stream DLP, token accounting.
# Subclasses implement ONLY vendor wire transport (the two
# _dispatch_upstream_* hooks) — nothing else is vendor-specific.
# ==================================================

DLP_MARKER = '[REDACTED_BY_E2A_DLP]'


def _luhn_valid(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class BaseLLMAgentService(ABC, _PropagationMixin):
    """E2A LLM Inference Microservice — the single, capacity-governed
    boundary between every agent archetype (BaseAgent, LLMOnlyAgent,
    RAG-grounded, tool-calling, MCP, batch evals) and a foundation model.

    Two public entry points, one per interaction mode, sharing one
    pipeline: process_inference() (unary) and process_inference_stream()
    (server-streaming). Callers never touch a model SDK; only a concrete
    subclass does, inside _dispatch_upstream_call / _dispatch_upstream_stream.

    Capacity state (pod semaphore, RPM/TPM bucket) is deliberately
    per-instance and in-memory; fleet-wide quota is an optional hook
    (_acquire_shared_quota) a subclass may bind to a BaseCacheStore."""

    # Ingress screens. Best-effort pattern gate; a model-based guard
    # belongs in BaseGovernanceFramework._execute_semantic_firewall.
    _INJECTION_PATTERNS = [
        re.compile(r'ignore\s+(all\s+)?(prior|previous)\s+instructions', re.IGNORECASE),
        re.compile(r'disregard\s+(the\s+)?system\s+prompt', re.IGNORECASE),
        re.compile(r'bypass\s+(the\s+)?security\s+boundary', re.IGNORECASE),
    ]
    # (name, pattern, optional validator). Card numbers must pass Luhn.
    _DLP_PATTERNS = [
        ('ssn', re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), None),
        ('card', re.compile(r'\b(?:\d[ -]?){13,19}\b'), _luhn_valid),
        ('bearer', re.compile(r'Bearer\s+[A-Za-z0-9\-_\.=]{16,}'), None),
    ]

    def __init__(self, config: Dict[str, Any] = None, **kwargs):
        self._config: Dict[str, Any] = dict(config or {})
        replicas = max(1, int(self._resolve_param('llm_replica_count', self._config, kwargs)))
        self._pod_semaphore = asyncio.Semaphore(
            max(1, int(self._resolve_param('pod_concurrency', self._config, kwargs))))
        self._tenant_semaphores: Dict[str, asyncio.Semaphore] = {}
        # Per-pod share of the fleet quota: configured limit / replica count.
        self._max_rpm = float(self._resolve_param('max_rpm', self._config, kwargs)) / replicas
        self._max_tpm = float(self._resolve_param('max_tpm', self._config, kwargs)) / replicas
        self._rpm_tokens = self._max_rpm
        self._tpm_tokens = self._max_tpm
        self._last_bucket_update = time.monotonic()
        self._bucket_lock = asyncio.Lock()

    # ----------------------------------------------------------------
    # PUBLIC 1 — unary
    # ----------------------------------------------------------------
    async def process_inference(
            self, messages: List[Dict[str, Any]], config: Dict[str, Any] = None,
            correlation_id: str = None, io_config: Dict[str, Any] = None,
            idempotency_key: str = None, tenant_id: str = None,
            message_log: List[dict] = None, failed_keys: List[str] = None,
            **kwargs) -> Dict[str, Any]:
        """Template method. Fixed lifecycle: sanitise -> semantic cache ->
        context budget -> KV prefix cache -> tool schemas -> quota
        admission -> (tenant + pod) concurrency permit -> upstream call
        under the latency deadline -> output framing -> egress DLP ->
        usage accounting. Never raises a request failure past this
        boundary: errors become {'status': 'FAILED'} plus a failed_keys
        entry, like every other E2A class. Always returns the E2A
        physical_success contract. kwargs (model_id, temperature, tools,
        stop_sequences, response_schema, reasoning_budget, bypass_cache,
        max_output_tokens ...) take highest precedence."""
        cfg = {**self._config, **(config or {})}
        correlation_id = correlation_id or str(uuid.uuid4())
        io_config = io_config or self._resolve_io_config(cfg, **kwargs)
        message_log = message_log if message_log is not None else []
        failed_keys = failed_keys if failed_keys is not None else []
        if not tenant_id:
            raise ValueError('tenant_id is required for LLM inference')
        common = dict(correlation_id=correlation_id, io_config=io_config,
                      tenant_id=tenant_id, message_log=message_log)
        start = time.time()
        admitted = 0
        payload: Dict[str, Any] = {}
        try:
            self.__sanitize_prompts(messages)

            tools = kwargs.get('tools')
            use_cache = (self._resolve_param('enable_semantic_cache', cfg, kwargs)
                         and not kwargs.get('bypass_cache', False) and not tools)
            if use_cache:
                cached = await self._check_semantic_cache(
                    messages, cfg, tenant_id=tenant_id, **self.__extras(kwargs))
                if cached:
                    hit = dict(cached, correlation_id=correlation_id, tenant_id=tenant_id,
                               cached_semantic_hit=True, tokens_used=0)
                    self._log(message_log, correlation_id, 'INFO', 'llm_semantic_cache_hit',
                              latency=round(time.time() - start, 3))
                    return hit

            prepared = self._manage_context_budget(
                messages, self._resolve_param('context_budget', cfg, kwargs),
                kwargs.get('reasoning_budget'), cfg, **common)
            cache_handle = None
            if self._resolve_param('enable_context_caching', cfg, kwargs):
                cache_handle = self._apply_kv_caching(prepared, tenant_id, cfg)
            compiled_tools = self._compile_tool_schemas(tools, cfg) if tools else None
            stop_sequences = kwargs.get('stop_sequences') or []
            max_out = int(self._resolve_param('max_output_tokens', cfg, kwargs))
            model_id = self._resolve_param('model_id', cfg, kwargs)
            temperature = self._resolve_param('temperature', cfg, kwargs)

            admitted = sum(self._estimate_tokens(m) for m in prepared) + max_out
            await self.__admit_quota(admitted, tenant_id, cfg, kwargs)

            deadline = float(self._resolve_param('llm_max_latency', cfg, kwargs))
            async with self.__concurrency_permit(tenant_id, cfg, kwargs):
                try:
                    raw = await asyncio.wait_for(
                        self._dispatch_upstream_call(
                            messages=prepared, model_id=model_id, temperature=temperature,
                            max_tokens=max_out, tools=compiled_tools,
                            stop_sequences=stop_sequences, cache_handle=cache_handle,
                            config=cfg, **common,
                            **self.__extras(kwargs, 'model_id', 'temperature', 'max_tokens',
                                            'tools', 'stop_sequences', 'cache_handle', 'config')),
                        timeout=deadline)
                except asyncio.TimeoutError:
                    raise NFRViolationError(
                        f'LLM inference latency SLO breached: > {deadline:.2f}s')

            payload = self._format_and_validate_output(
                raw, kwargs.get('response_schema'), model_id, cache_handle is not None,
                correlation_id, tenant_id, cfg)
            payload['tokens_used'] = self.__track_usage(prepared, raw)
            payload['metadata'] = {'model_id': model_id, 'tokens_used': payload['tokens_used']}
            if self._resolve_param('dlp_enabled', cfg, kwargs) and 'text' in payload:
                payload['text'], payload['dlp_redactions'] = self._redact_sensitive(payload['text'])
            await self.__refund_quota(admitted - payload['tokens_used'])
            admitted = 0
            if use_cache and payload.get('physical_success') and 'tool_calls' not in payload:
                await self._store_semantic_cache(
                    messages, payload, cfg, tenant_id=tenant_id, **self.__extras(kwargs))
        except DehydrationInterrupt:
            raise
        except Exception as e:
            if admitted:
                await self.__refund_quota(admitted)
            key = idempotency_key or correlation_id
            if key not in failed_keys:
                failed_keys.append(key)
            self._handle_error(e, messages, cfg, **common, failed_keys=failed_keys)
            payload = {'status': 'FAILED', 'correlation_id': correlation_id,
                       'tenant_id': tenant_id, 'error': str(e),
                       'error_type': type(e).__name__, 'physical_success': False,
                       'tokens_used': 0}
        self._log(message_log, correlation_id, 'INFO', 'llm_inference_complete',
                  latency=round(time.time() - start, 3),
                  llm_tokens_used=payload.get('tokens_used', 0),
                  kv_cache_hit=payload.get('cached_kv_hit', False),
                  status=payload.get('status'))
        return payload

    # ----------------------------------------------------------------
    # PUBLIC 2 — server-streaming
    # ----------------------------------------------------------------
    async def process_inference_stream(
            self, messages: List[Dict[str, Any]], config: Dict[str, Any] = None,
            correlation_id: str = None, io_config: Dict[str, Any] = None,
            idempotency_key: str = None, tenant_id: str = None,
            message_log: List[dict] = None, failed_keys: List[str] = None,
            **kwargs) -> AsyncGenerator[str, None]:
        """Streaming sibling of process_inference(). Same admission
        (sanitise, context budget, quota, tenant + pod permit) and the
        same failure bookkeeping, then yields DLP-screened text chunks.
        There is no result payload to carry a failure, so a failure is
        recorded (failed_keys + _handle_error) and re-raised to the
        consumer. Total-latency SLO is not enforced mid-stream; the
        permit is held for the life of the stream, so consumers must
        iterate it to completion or aclose() it."""
        cfg = {**self._config, **(config or {})}
        correlation_id = correlation_id or str(uuid.uuid4())
        io_config = io_config or self._resolve_io_config(cfg, **kwargs)
        message_log = message_log if message_log is not None else []
        failed_keys = failed_keys if failed_keys is not None else []
        if not tenant_id:
            raise ValueError('tenant_id is required for LLM inference')
        common = dict(correlation_id=correlation_id, io_config=io_config,
                      tenant_id=tenant_id, message_log=message_log)
        start, emitted, admitted = time.time(), 0, 0
        try:
            self.__sanitize_prompts(messages)
            prepared = self._manage_context_budget(
                messages, self._resolve_param('context_budget', cfg, kwargs),
                kwargs.get('reasoning_budget'), cfg, **common)
            max_out = int(self._resolve_param('max_output_tokens', cfg, kwargs))
            admitted = sum(self._estimate_tokens(m) for m in prepared) + max_out
            await self.__admit_quota(admitted, tenant_id, cfg, kwargs)
            holdback = int(self._resolve_param('dlp_holdback_chars', cfg, kwargs))
            dlp_on = self._resolve_param('dlp_enabled', cfg, kwargs)
            async with self.__concurrency_permit(tenant_id, cfg, kwargs):
                raw_stream = self._dispatch_upstream_stream(
                    messages=prepared,
                    model_id=self._resolve_param('model_id', cfg, kwargs),
                    temperature=self._resolve_param('temperature', cfg, kwargs),
                    max_tokens=max_out, config=cfg, **common,
                    **self.__extras(kwargs, 'model_id', 'temperature', 'max_tokens', 'config'))
                async for chunk in self.__stream_with_dlp(raw_stream, holdback, dlp_on):
                    emitted += len(chunk)
                    yield chunk
        except DehydrationInterrupt:
            raise
        except Exception as e:
            key = idempotency_key or correlation_id
            if key not in failed_keys:
                failed_keys.append(key)
            self._handle_error(e, messages, cfg, **common, failed_keys=failed_keys)
            raise
        finally:
            self._log(message_log, correlation_id, 'INFO', 'llm_stream_complete',
                      latency=round(time.time() - start, 3), chars_emitted=emitted)

    # ----------------------------------------------------------------
    # PROTECTED hooks — fully implemented; override only to specialise
    # ----------------------------------------------------------------
    def _estimate_tokens(self, message: Dict[str, Any]) -> int:
        return len(str(message.get('content', ''))) // 4 + 4

    def _manage_context_budget(self, messages, budget, reasoning_budget, config, **kwargs):
        """Working-set management. System / priority-0 messages are
        pinned; the final turn and priority-1 messages are active; the
        rest is compressible history, kept newest-first as one contiguous
        recent window. Conversation order is preserved. Pinned + active
        alone exceeding the budget is an NFR violation, never silently
        truncated."""
        if not messages:
            raise ValueError('messages must be a non-empty list')
        effective = budget - (reasoning_budget or 0)
        pinned = {i for i, m in enumerate(messages)
                  if m.get('priority') == 0 or m.get('role') == 'system'}
        active = ({i for i, m in enumerate(messages) if m.get('priority') == 1}
                  | {len(messages) - 1}) - pinned
        invariant = sum(self._estimate_tokens(messages[i]) for i in pinned | active)
        if invariant > effective:
            raise NFRViolationError(
                f'Context overflow: pinned+active ({invariant}) exceed budget ({effective}).')
        remaining, keep = effective - invariant, set(pinned | active)
        for i in reversed([j for j in range(len(messages)) if j not in keep]):
            cost = self._estimate_tokens(messages[i])
            if cost > remaining:
                break
            keep.add(i)
            remaining -= cost
        return [messages[i] for i in sorted(keep)]

    def _apply_kv_caching(self, messages, tenant_id, config, **kwargs) -> Optional[str]:
        """Large static prefixes (>= min_cache_threshold tokens) get a
        deterministic, tenant-scoped cache-handle name. The handle is a
        request to the vendor transport, which creates/reuses the real
        provider-side cache inside _dispatch_upstream_call."""
        threshold = int(self._resolve_param('min_cache_threshold', config, kwargs))
        pinned = [m for m in messages if m.get('priority') == 0 or m.get('role') == 'system']
        if sum(self._estimate_tokens(m) for m in pinned) < threshold:
            return None
        digest = hashlib.sha256(
            '\x1f'.join(str(m.get('content', '')) for m in pinned).encode()).hexdigest()[:32]
        return f'kvcache/{tenant_id}/{digest}'

    def _compile_tool_schemas(self, tools, config, **kwargs) -> List[Dict[str, Any]]:
        """Normalise caller tool specs to the canonical, vendor-neutral
        E2A declaration {name, description, parameters}. Mapping to a
        provider wire format is the vendor subclass's job."""
        compiled = []
        for t in tools:
            if not t.get('name'):
                raise ValueError('tool declaration requires a name')
            compiled.append({
                'name': t['name'], 'description': t.get('description', ''),
                'parameters': t.get('parameters', t.get('input_schema',
                                                        {'type': 'object', 'properties': {}}))})
        return compiled

    async def _check_semantic_cache(self, messages, config, tenant_id=None, **kwargs):
        """Hook: tenant-scoped similarity lookup (cosine >= tau, e.g. 0.95)
        against a BaseCacheStore. Default: no cache."""
        return None

    async def _store_semantic_cache(self, messages, payload, config, tenant_id=None, **kwargs):
        """Hook: write-through counterpart of _check_semantic_cache."""
        return None

    async def _acquire_shared_quota(self, tokens, tenant_id, config, **kwargs) -> bool:
        """Optional fleet-wide quota hook (e.g. a BaseCacheStore-backed
        counter). Default admits; the in-memory per-pod bucket always
        applies in addition."""
        return True

    def _format_and_validate_output(self, raw, response_schema, model_id, cached_hit,
                                    correlation_id, tenant_id, config, **kwargs):
        out: Dict[str, Any] = {
            'status': 'SUCCESS', 'correlation_id': correlation_id, 'tenant_id': tenant_id,
            'model_executed': model_id, 'cached_kv_hit': cached_hit,
            'physical_success': True}
        if raw.get('tool_calls'):
            out['tool_calls'] = raw['tool_calls']
            out['action'] = 'TOOL_CALL_DETECTED'
            out['text'] = raw.get('text', '')
            return out
        if response_schema:
            try:
                out['structured_output'] = json.loads(raw.get('text', '{}'))
            except Exception as e:
                out['structured_output'] = {'parse_error': str(e), 'raw': raw.get('text')}
                out['physical_success'] = False
        out['text'] = raw.get('text', '')
        return out

    def _redact_sensitive(self, text: str):
        """Egress DLP. Returns (redacted_text, redaction_count)."""
        count = 0
        for _name, pattern, validator in self._DLP_PATTERNS:
            def _sub(m, validator=validator):
                nonlocal count
                if validator and not validator(m.group(0)):
                    return m.group(0)
                count += 1
                return DLP_MARKER
            text = pattern.sub(_sub, text)
        return text, count

    def _handle_error(self, error, messages, config=None, **kwargs) -> None:
        self._log(kwargs.get('message_log'), kwargs.get('correlation_id', 'UNKNOWN'),
                  'ERROR', 'llm_inference_failed', error=str(error),
                  error_type=type(error).__name__)

    # ----------------------------------------------------------------
    # PRIVATE — framework-owned, never overridden
    # ----------------------------------------------------------------
    def _resolve_param(self, key, config, kwargs):
        """Thin alias of the central resolve_config() — kwargs > config >
        env var > default. Single underscore so a subclass's transport can
        resolve its own (registered) keys."""
        return resolve_config(key, config, kwargs, scope='BaseLLMAgentService')

    @staticmethod
    def __extras(kwargs, *explicit):
        """kwargs minus the names this class already passes explicitly
        (and minus the propagation fields), so no keyword is ever
        supplied twice to a hook."""
        skip = set(explicit) | {'correlation_id', 'io_config', 'tenant_id', 'message_log',
                                'failed_keys', 'idempotency_key'}
        return {k: v for k, v in kwargs.items() if k not in skip}

    def __sanitize_prompts(self, messages) -> None:
        for m in messages:
            if m.get('role') in ('user', 'prompt', 'tool'):
                content = str(m.get('content', ''))
                if any(p.search(content) for p in self._INJECTION_PATTERNS):
                    raise ValueError('E2A Security Gate: prompt-injection pattern detected.')

    def __track_usage(self, prepared, raw) -> int:
        reported = (raw or {}).get('metadata', {}).get('tokens_used')
        if reported is not None:
            return int(reported)
        return max(1, sum(self._estimate_tokens(m) for m in prepared)
                   + len(str((raw or {}).get('text', ''))) // 4)

    def __concurrency_permit(self, tenant_id, config, kwargs):
        """Tenant-fairness cap (optional) then the pod semaphore."""
        stack = contextlib.AsyncExitStack()
        service = self

        class _Permit:
            async def __aenter__(_self):
                cap = int(service._resolve_param('tenant_concurrency', config, kwargs))
                if cap > 0:
                    sem = service._tenant_semaphores.setdefault(tenant_id, asyncio.Semaphore(cap))
                    await stack.enter_async_context(sem)
                await stack.enter_async_context(service._pod_semaphore)

            async def __aexit__(_self, *exc):
                return await stack.__aexit__(*exc)
        return _Permit()

    async def __admit_quota(self, tokens, tenant_id, config, kwargs) -> None:
        """Dual token bucket (requests/min and tokens/min). Fails fast
        with NFRViolationError after quota_timeout rather than queueing
        a caller indefinitely."""
        timeout = float(self._resolve_param('quota_timeout', config, kwargs))
        start = time.monotonic()
        if not await self._acquire_shared_quota(tokens, tenant_id, config):
            raise NFRViolationError('E2A Quota Exhaustion: shared fleet quota denied.')
        while True:
            async with self._bucket_lock:
                now = time.monotonic()
                elapsed, self._last_bucket_update = now - self._last_bucket_update, now
                self._rpm_tokens = min(self._max_rpm, self._rpm_tokens + elapsed * self._max_rpm / 60.0)
                self._tpm_tokens = min(self._max_tpm, self._tpm_tokens + elapsed * self._max_tpm / 60.0)
                if self._rpm_tokens >= 1.0 and self._tpm_tokens >= min(tokens, self._max_tpm):
                    self._rpm_tokens -= 1.0
                    self._tpm_tokens -= min(tokens, self._max_tpm)
                    return
            if time.monotonic() - start > timeout:
                raise NFRViolationError(
                    'E2A Quota Exhaustion: timed out waiting for RPM/TPM quota.')
            await asyncio.sleep(0.02)

    async def __refund_quota(self, tokens) -> None:
        if tokens > 0:
            async with self._bucket_lock:
                self._tpm_tokens = min(self._max_tpm, self._tpm_tokens + tokens)

    async def __stream_with_dlp(self, raw_stream, holdback, enabled):
        """Hold back the trailing `holdback` characters so a sensitive
        value split across chunks is complete in the buffer before any
        part of it is released. Best effort: a value longer than
        holdback can still leak its head — pair with
        BaseGovernanceFramework._redact_output_fields for strict egress."""
        if not enabled:
            async for chunk in raw_stream:
                yield chunk
            return
        buf = ''
        async for chunk in raw_stream:
            buf, _ = self._redact_sensitive(buf + chunk)
            if len(buf) > holdback:
                out, buf = buf[:-holdback], buf[-holdback:]
                yield out
        buf, _ = self._redact_sensitive(buf)
        if buf:
            yield buf

    # ----------------------------------------------------------------
    # ABSTRACT — vendor wire transport only
    # ----------------------------------------------------------------
    @abstractmethod
    async def _dispatch_upstream_call(self, messages, model_id, temperature, max_tokens,
                                      tools, stop_sequences, cache_handle, config,
                                      **kwargs) -> Dict[str, Any]:
        """Execute one model call on the concrete provider (managed API,
        self-hosted server, ...), including any provider-side cache
        creation for cache_handle and any model fallback cascade. `tools`
        are canonical E2A declarations; map them to the provider's wire
        format here. Return {'text': str, 'metadata': {'tokens_used': int}}
        and/or {'tool_calls': [...]}."""

    @abstractmethod
    def _dispatch_upstream_stream(self, messages, model_id, temperature, max_tokens,
                                  config, **kwargs) -> AsyncGenerator[str, None]:
        """Async generator yielding raw text chunks from the provider.
        Implement as `async def ...` containing `yield`."""


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
        gov_engine = config.get('governance_engine')
        start = time.time()
        try:
            messages = self._build_messages(state, config, **common, **kwargs)
            if gov_engine and isinstance(gov_engine, BaseGovernanceFramework):
                state = gov_engine.enforce_governance_gate(state, io_config, config, **common, **kwargs)
            else:
                self._apply_policy(state, config, **common, **kwargs)
            response = self._infer(messages, config, **common, **kwargs)
            tokens_used = self.__track_usage(messages, response, config, **kwargs)
            state['cumulative_tokens_used'] = state.get('cumulative_tokens_used', 0) + tokens_used
            confidence = self._evaluate_output(
                response, state, config, **common, **kwargs)
            min_conf = resolve_config('min_confidence', config, kwargs)
            if confidence < min_conf:
                response = self._fallback(state, config, **common, **kwargs)
            if gov_engine and isinstance(gov_engine, BaseGovernanceFramework):
                response = gov_engine.redact_response(state, response, config, **common, **kwargs)
            state['response'] = response
        except DehydrationInterrupt:
            raise  # re-raised unchanged; BaseWorkflow.execute() catches it
        except Exception as e:
            failed_keys.append(idempotency_key or correlation_id)
            self._handle_error(e, state, config, correlation_id=correlation_id,
                                message_log=message_log,
                                failed_keys=failed_keys, **kwargs)
        latency = time.time() - start
        max_latency = resolve_config('max_latency', config, kwargs)
        self._log(message_log, correlation_id, 'INFO', 'agent_run_complete',
                   latency=round(latency, 3), tokens_used=state.get('cumulative_tokens_used', 0))
        state['tenant_id'] = tenant_id
        state['correlation_id'] = correlation_id
        if latency > max_latency:
            raise NFRViolationError(f'Agent run latency SLO breached: {latency:.2f}s > {max_latency}s')
        return state
 
    @abstractmethod
    def _build_messages(self, state, config=None, **kwargs):
        """kwargs: prompt_template, role"""
 
    @abstractmethod
    def _apply_policy(self, state, config=None, **kwargs):
        """kwargs: retry_policy, circuit_breaker_config. Fallback path,
        used only when no governance_engine is configured — the
        preferred, centrally-enforced path is
        BaseGovernanceFramework.enforce_governance_gate(). This is also
        where _resolve_tool_call() is invoked if the agent decides a
        tool needs to run — see the Tool Routing section."""
 
    # No LLM dispatch lives here: the model call is delegated to
    # config['llm_service'] (a BaseLLMAgentService) through the
    # mixin's _infer().
 
    def __track_usage(self, messages, response, config=None, **kwargs) -> int:
        """Base-owned, never overridden. Prefers an actual token count
        from the LLM provider's response metadata; falls back to a
        ~4-chars/token estimate over the outbound messages when a
        provider doesn't report usage."""
        reported = (response or {}).get('metadata', {}).get('tokens_used')
        if reported is not None:
            return reported
        char_count = sum(len(str(m)) for m in (messages or []))
        return max(1, char_count // 4)
 
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
            self._log(message_log, correlation_id, 'INFO', 'workflow_dehydrated', reason=str(d))
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
                self._flush_logs(message_log, config)  # fallback when no engine configured
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
        # hybrid: the index gets BOTH the raw text (keyword/BM25 leg)
        # and the vector (semantic leg) and fuses them itself.
        results = self._search_index(query_text=query, query_vector=vector,
                                     config=config, tenant_id=tenant_id, **kwargs)
        reranked = self._rerank(results, config, **kwargs)
        return reranked
 
    @abstractmethod
    def _search_index(self, query_text, query_vector, config=None, tenant_id=None, **kwargs):
        """Hybrid keyword + vector search. Namespace the search to
        f'{tenant_index_prefix}_{tenant_id}'. kwargs: top_k, hybrid_alpha"""
 
    @abstractmethod
    def _rerank(self, results, config=None, **kwargs):
        """kwargs: rerank_top_n"""
 
    @abstractmethod
    def _evaluate_answer(self, answer, config=None, **kwargs):
        """kwargs: groundedness_threshold"""
 
    def __embed(self, query, config=None, **kwargs):
        model = resolve_config('embedding_model', config, kwargs)
        return [0.0]  # provider call
 
# ==================================================
# BaseIndexingPipeline — write side of BaseRAGPipeline
# Fed by a change stream (CDC) or a bulk load; keeps the vector /
# keyword index consistent with the system of record.
# ==================================================
 
class BaseIndexingPipeline(ABC, _PropagationMixin):
    """Single public entry point: ingest(). Each document dict carries
    {'id', 'op': 'upsert'|'delete', 'text'/fields}. Per-document
    isolation: one failing document lands in failed_keys and never
    blocks the batch. The embedding model MUST match the one
    BaseRAGPipeline.retrieve() embeds queries with (same
    'embedding_model' key)."""
 
    def ingest(self, documents: List[Dict[str, Any]], config: Dict[str, Any] = None,
              correlation_id: str = None, io_config: Dict[str, Any] = None,
              idempotency_key: str = None, tenant_id: str = None,
              message_log: List[dict] = None, failed_keys: List[str] = None,
              **kwargs) -> Dict[str, Any]:
        config = config or {}
        if not tenant_id:
            raise ValueError('tenant_id is required for indexing')
        correlation_id = correlation_id or str(uuid.uuid4())
        io_config = io_config or self._resolve_io_config(config, **kwargs)
        message_log = message_log if message_log is not None else []
        failed_keys = failed_keys if failed_keys is not None else []
        common = dict(correlation_id=correlation_id, io_config=io_config,
                      tenant_id=tenant_id, message_log=message_log)
        indexed = deleted = 0
        for doc in documents:
            doc_id = doc.get('id')
            try:
                if not self._commit_gate(doc_id, failed_keys, config, **kwargs):
                    continue
                if doc.get('op', 'upsert') == 'delete':
                    self._delete_from_index(doc_id, config, **common, **kwargs)
                    deleted += 1
                else:
                    chunks = self._chunk_documents(doc, config, **common, **kwargs)
                    vectors = self._embed_documents([c['text'] for c in chunks], config,
                                                **common, **kwargs)
                    if len(vectors) != len(chunks):
                        raise ValueError('embedding count != chunk count')
                    self._write_index(chunks, vectors, config, **common, **kwargs)
                    indexed += 1
            except Exception as e:
                failed_keys.append(doc_id)
                self._handle_error(e, doc, config, correlation_id=correlation_id,
                                   message_log=message_log, failed_keys=failed_keys, **kwargs)
        self._log(message_log, correlation_id, 'INFO', 'indexing_complete',
                  indexed=indexed, deleted=deleted, failed=len(failed_keys))
        return {'correlation_id': correlation_id, 'tenant_id': tenant_id,
                'indexed': indexed, 'deleted': deleted, 'failed_keys': failed_keys}
 
    @abstractmethod
    def _chunk_documents(self, document, config=None, **kwargs) -> List[Dict[str, Any]]:
        """Return [{'text': ..., 'metadata': {...}}]. kwargs: chunk_size, chunk_overlap"""
 
    @abstractmethod
    def _embed_documents(self, texts, config=None, **kwargs) -> List[List[float]]:
        """kwargs: embedding_model — must equal the retrieval-side model."""
 
    @abstractmethod
    def _write_index(self, chunks, vectors, config=None, tenant_id=None, **kwargs):
        """Write to f'{tenant_index_prefix}_{tenant_id}'. Must be idempotent
        on chunk id so CDC at-least-once delivery is safe."""
 
    @abstractmethod
    def _delete_from_index(self, doc_id, config=None, tenant_id=None, **kwargs):
        """Remove every chunk of doc_id from the tenant namespace."""
 
    @abstractmethod
    def _handle_error(self, error, document, config=None, **kwargs):
        """Appends to failed_keys via kwargs['failed_keys'].append(...)"""
 
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
        timeout = resolve_config('timeout', config, kwargs)
        retries = resolve_config('retries', config, kwargs)
        token = resolve_config('auth_token', config, kwargs)
        # physical_success reflects the real network/API outcome —
        # callers must check this, never an agent's narrative about it.
        return {'status': 'ok', 'endpoint': endpoint, 'physical_success': True}  # aiohttp/httpx call
 
class RestToolService(BaseToolService):
    """Classic REST/HTTP tool."""
 
    def get_endpoint(self, config=None, tenant_id=None, **kwargs):
        config = config or {}
        routing = resolve_config('tenant_routing_map', config, kwargs)
        if tenant_id and tenant_id in routing:
            return routing[tenant_id]
        return kwargs.get('base_url') or resolve_config('tool_base_url', config, kwargs)
 
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
        return resolve_config('mcp_server_url', config, kwargs)
 
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
    mcp_registry = resolve_config('mcp_server_registry', config, kwargs)
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
    registry = resolve_config('tool_registry', config, kwargs)
    entry = registry.get(tool_name, {})
    transport = kwargs.get('transport') or entry.get(
        'transport', resolve_config('default_transport', config, kwargs))
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
            min_conf = resolve_config('min_confidence', config, kwargs)
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
        registry = resolve_config('model_capability_registry', config, kwargs)
        return registry.get(modality, registry.get(
            'default', resolve_config('model_id', config, kwargs)))
 
    # No _llm_call here: every _handle_* implementation reaches the
    # model with self._infer(messages, config, model_id=self._select_model(
    # modality, config, **kwargs), **common) — delegated to
    # config['llm_service'] (BaseLLMAgentService).
 
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
# New Foundation Ports — Section 2.16. Interfaces only,
# no shared implementation, no domain knowledge. A concrete
# subclass binds each one to a real backend.
# ==================================================
 
class BaseTransactionalStore(ABC):
    """Vendor-neutral durable read/write contract."""
 
    @abstractmethod
    def get(self, key: str, config: Dict[str, Any] = None, **kwargs) -> Optional[Dict[str, Any]]:
        """kwargs: table_or_collection_name."""
 
    @abstractmethod
    def put(self, key: str, value: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: table_or_collection_name."""
 
    @abstractmethod
    def delete(self, key: str, config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: table_or_collection_name."""
 
 
class BaseCacheStore(ABC):
    """Vendor-neutral cache contract. Separate from
    BaseTransactionalStore — TTL/eviction semantics differ from
    durable-store semantics. A subclass implementing two-tier
    (L1 in-process / L2 shared) caching does so entirely inside
    get()/set(); no separate abstract method for that."""
 
    @abstractmethod
    def get(self, key: str, config: Dict[str, Any] = None, **kwargs) -> Optional[Any]:
        """kwargs: default_ttl_seconds."""
 
    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: int = None,
            config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: default_ttl_seconds."""
 
    @abstractmethod
    def delete(self, key: str, config: Dict[str, Any] = None, **kwargs) -> bool:
        pass
 
 
class BaseIdempotencyStore(ABC):
    """Formalizes the conditional-write pattern already described
    informally in Section 2.1 (__ensure_idempotency) as a reusable,
    injectable port."""
 
    @abstractmethod
    def check_and_set(self, idempotency_key: str, config: Dict[str, Any] = None,
                       **kwargs) -> bool:
        """kwargs: ttl_seconds. Atomic check-and-claim — returns True
        only for the caller that wins the race."""
 
 
class BaseDistributedLock(ABC):
    """Extracted from the inline cache-stampede mutex (Section 2.15)
    into a standalone coordination primitive. Deliberately minimal —
    short-lived, TTL-bound, no reentrancy, no distributed-transaction
    semantics."""
 
    @abstractmethod
    def acquire(self, lock_key: str, ttl_seconds: int,
                config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: lock_backend."""
 
    @abstractmethod
    def release(self, lock_key: str, config: Dict[str, Any] = None, **kwargs) -> None:
        pass
 
 
class BaseWebhookDispatcher(ABC, _PropagationMixin):
    """PRIMARY class — dispatch() is the single public entry point.
    Outbound counterpart to BaseToolService: pushes a notification
    and does not wait for a business-logic response."""
 
    def dispatch(self, webhook_url: str, payload: Dict[str, Any],
                 config: Dict[str, Any] = None, correlation_id: str = None,
                 **kwargs) -> Dict[str, Any]:
        """Template method. kwargs: signing_secret, max_retries."""
        config = config or {}
        correlation_id = correlation_id or str(uuid.uuid4())
        signed = self._sign_payload(payload, resolve_config('signing_secret', config, kwargs), config, **kwargs)
        max_retries = resolve_config('max_retries', config, kwargs)
        for attempt in range(max_retries):
            if self._deliver(webhook_url, signed, config, **kwargs):
                return {'status': 'DELIVERED', 'correlation_id': correlation_id,
                        'attempts': attempt + 1}
        return {'status': 'FAILED', 'correlation_id': correlation_id,
                'attempts': max_retries}
 
    @abstractmethod
    def _sign_payload(self, payload: Dict[str, Any], signing_secret: str,
                       config: Dict[str, Any] = None, **kwargs) -> str:
        """kwargs: signing_algorithm. HMAC-SHA256 by default convention;
        subclass owns the actual implementation."""
 
    @abstractmethod
    def _deliver(self, webhook_url: str, signed_payload: str,
                 config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: timeout_seconds. The transport call — raw HTTP POST
        or a managed delivery service."""
 
 
class BaseSchemaTransformer(ABC):
    """Upcast/downcast contract for a versioned event/payload, applied
    inline at deserialization. No compute tier of its own, no business
    meaning of its own — a structural transform only."""
 
    @abstractmethod
    def upcast(self, payload: Dict[str, Any], from_version: str, to_version: str,
               config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        """kwargs: schema_registry_url."""
 
 
class BaseMemoryManager(ABC):
    """Agentic profile only — composed into BaseAgent._build_messages()
    the same way BasePromptRegistry is, not called directly by
    BaseWorkflow. No CQRS equivalent: CQRS carries no conversation
    state to prune."""
 
    @abstractmethod
    def prune(self, message_history: list, max_tokens: int,
              config: Dict[str, Any] = None, **kwargs) -> list:
        """kwargs: pruning_strategy. Strategy (sliding window,
        summarization, importance scoring) is entirely subclass-owned."""
 
 
# Example usage inside a concrete BaseAgent — not part of the scaffold,
# shown here for reference:
#
# def _build_messages(self, state, config=None, **kwargs):
#     history = self.memory_manager.prune(
#         state.get('message_history', []),
#         max_tokens=kwargs.get('io_config', {}).get('max_context_tokens', 8000))
#     ...
 
# ==================================================
# BaseOutboxSweeper and BaseReconciliationService —
# Section 2.17. Control-plane workers, scheduled, not
# called from BaseWorkflow.execute() or BaseAgent.run().
# ==================================================
 
class BaseOutboxSweeper(ABC, _PropagationMixin):
    """PRIMARY class — sweep() is the single public entry point.
    Invoked on a fixed cadence (EventBridge Scheduler / cron),
    not per request."""
 
    def __init__(self, lock: 'BaseDistributedLock' = None):
        self.lock = lock  # injected BaseDistributedLock (2.16)
 
    def sweep(self, config: Dict[str, Any] = None, correlation_id: str = None,
              **kwargs) -> Dict[str, Any]:
        """Template method. kwargs: batch_size, max_age_seconds."""
        config = config or {}
        correlation_id = correlation_id or str(uuid.uuid4())
        if self.lock and not self.lock.acquire('outbox_sweeper', ttl_seconds=60, config=config):
            return {'status': 'SKIPPED_LOCKED', 'correlation_id': correlation_id}
        try:
            pending = self._fetch_pending_records(config, **kwargs)
            published, failed = 0, []
            for record in pending:
                if self._publish_pending_record(record, config, **kwargs):
                    published += 1
                else:
                    failed.append(record.get('id'))
            return {'status': 'SWEPT', 'correlation_id': correlation_id,
                    'published': published, 'failed_keys': failed}
        finally:
            if self.lock:
                self.lock.release('outbox_sweeper', config=config)
 
    @abstractmethod
    def _fetch_pending_records(self, config: Dict[str, Any] = None, **kwargs) -> list:
        """kwargs: batch_size, max_age_seconds. Reads pending records
        from the Outbox table (Landing Zone Section 3.1.3 / 8.1)."""
 
    @abstractmethod
    def _publish_pending_record(self, record: Dict[str, Any],
                                 config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: destination_topic."""
 
 
class BaseReconciliationService(ABC, _PropagationMixin):
    """PRIMARY class — reconcile() is the single public entry point.
    Same scheduled-cadence, same lock-acquisition pattern as
    BaseOutboxSweeper."""
 
    def __init__(self, lock: 'BaseDistributedLock' = None,
                 idempotency_store: 'BaseIdempotencyStore' = None):
        self.lock = lock
        self.idempotency_store = idempotency_store  # (2.16)
 
    def reconcile(self, config: Dict[str, Any] = None, correlation_id: str = None,
                  **kwargs) -> Dict[str, Any]:
        """Template method. kwargs: dlq_source, max_replay_count."""
        config = config or {}
        correlation_id = correlation_id or str(uuid.uuid4())
        if self.lock and not self.lock.acquire('reconciliation_service', ttl_seconds=60, config=config):
            return {'status': 'SKIPPED_LOCKED', 'correlation_id': correlation_id}
        try:
            entries = self._fetch_dlq_entries(config, **kwargs)
            replayed, exhausted = 0, []
            for entry in entries:
                if self.idempotency_store and not self.idempotency_store.check_and_set(
                        entry.get('idempotency_key'), config=config):
                    continue  # already reconciled by a prior run
                if self._replay_dlq_entry(entry, config, **kwargs):
                    replayed += 1
                else:
                    exhausted.append(entry.get('id'))
            return {'status': 'RECONCILED', 'correlation_id': correlation_id,
                    'replayed': replayed, 'failed_keys': exhausted}
        finally:
            if self.lock:
                self.lock.release('reconciliation_service', config=config)
 
    @abstractmethod
    def _fetch_dlq_entries(self, config: Dict[str, Any] = None, **kwargs) -> list:
        """kwargs: dlq_source, max_replay_count."""
 
    @abstractmethod
    def _replay_dlq_entry(self, entry: Dict[str, Any],
                           config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: target_idempotency_store. Re-submits through the
        same BaseIdempotencyStore check every live request uses."""
 
 
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
 
            min_faithfulness = float(resolve_config('faithfulness_threshold', config, kwargs))
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
 
            strategy = str(resolve_config('deployment_strategy', config, kwargs)).upper()
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
        method (__http_post, __admit_quota, etc.) overridden downstream."""
 
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
                                availability_tier: str = 'STANDARD',
                                config: Dict[str, Any] = None,
                                correlation_id: str = None, **kwargs) -> Dict[str, Any]:
        """Public entry point. tenancy_model is 'POOL' or 'SILO' (Section 10.2
        of the Landing Zone doc). availability_tier is 'STANDARD', 'HIGH', or
        'CRITICAL' (Landing Zone Section 8.4) — selects Multi-AZ/Multi-Region
        topology; carries no domain meaning of its own. Runs all seven steps
        in fixed order and returns a manifest; never raises past this boundary."""
        config = config or {}
        correlation_id = correlation_id or f'iac-{tenant_id}-{int(time.time())}'
        manifest = {
            'tenant_id': tenant_id, 'tenancy_model': tenancy_model,
            'availability_tier': availability_tier,
            'correlation_id': correlation_id, 'topology_maps': {}, 'status': 'INITIATED',
        }
        try:
            networks = self._define_network_topologies(
                tenant_id, availability_tier, config, **kwargs)
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
    def _define_network_topologies(self, tenant_id, availability_tier,
                                    config=None, **kwargs) -> dict:
        """Public VPC/DMZ, Private VPC application space, and the Data &
        Messaging Perimeter's service boundary (VPC-SC / PrivateLink).
        availability_tier determines AZ/region count (STANDARD: 1, HIGH:
        >=2 AZs, CRITICAL: >=2 AZs across >=2 regions) — this contract only
        guarantees the count is honored; the subclass owns the concrete list."""
 
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
 
    def _define_network_topologies(self, tenant_id, availability_tier='STANDARD',
                                    config=None, **kwargs):
        region_count = {'STANDARD': 1, 'HIGH': 1, 'CRITICAL': 2}.get(availability_tier, 1)
        return {
            'vpc_network_name': f'e2a-vpc-{tenant_id}',
            'vpc_service_perimeter': 'accessPolicies/e2a_policy/servicePerimeters/prod_perimeter',
            'availability_tier': availability_tier,
            'region_count': region_count,
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
