"""E2A: BaseLLMAgentService: the only way any class reaches a model.

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

from .core import (AdmissionTimeout, Deadline, DeadlineExceeded, DehydrationInterrupt, ErrorClass,
                   ModelRefusal, NFRViolationError, classify_error)
from .llm_types import ModelResult
from .capabilities import capabilities_of
from .schema_check import schema_errors
from .config import resolve_config
from .resilience import ResiliencePolicy
from ._propagation import _PropagationMixin
 
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


def _iban_valid(value: str) -> bool:
    """ISO 13616 mod-97 check for an IBAN candidate."""
    iban = re.sub(r'\s+', '', value).upper()
    if not re.fullmatch(r'[A-Z]{2}\d{2}[A-Z0-9]{11,30}', iban):
        return False
    digits = ''.join(str(int(c, 36)) for c in iban[4:] + iban[:4])
    return int(digits) % 97 == 1


# v1.6.0: validators a configured DLP pattern may name (no code in configuration)
DLP_VALIDATORS = {'luhn': _luhn_valid, 'iban_mod97': _iban_valid}


def _compile_patterns(entries, kind: str, with_validator: bool):
    out = []
    for i, entry in enumerate(entries or []):
        if not isinstance(entry, dict) or not entry.get('name') or not entry.get('regex'):
            raise ValueError(f'{kind}[{i}]: each pattern needs a name and a regex')
        try:
            pattern = re.compile(entry['regex'], re.IGNORECASE if entry.get('ignore_case') else 0)
        except re.error as e:
            raise ValueError(f"{kind} '{entry['name']}': invalid regex: {e}") from e
        if not with_validator:
            out.append(pattern)
            continue
        name = entry.get('validator')
        if name and name not in DLP_VALIDATORS:
            raise ValueError(f"{kind} '{entry['name']}': unknown validator {name!r} "
                             f"(available: {', '.join(sorted(DLP_VALIDATORS))})")
        out.append((entry['name'], pattern, DLP_VALIDATORS.get(name)))
    return out


REASONING_EFFORTS = ('minimal', 'low', 'medium', 'high', 'max')


class StructuredOutputUnsupported(ValueError):
    """A response schema was requested from a model that declares no structured output."""


class StructuredOutputInvalid(ValueError):
    """structured_output_strict: the model's structured result does not match the schema."""


class _TenantPermit:
    """Per-tenant concurrency cap entry. `active` counts holders AND
    waiters so an entry in use is never evicted."""
    __slots__ = ('sem', 'cap', 'active', 'last_used')

    def __init__(self, cap: int):
        self.sem, self.cap, self.active, self.last_used = asyncio.Semaphore(cap), cap, 0, time.monotonic()


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
        # v1.6.0: patterns from configuration, compiled now: a bad one fails at startup
        extra_dlp = _compile_patterns(self._resolve_param('dlp_patterns', self._config, kwargs), 'dlp_patterns', True)
        replace = bool(self._resolve_param('dlp_patterns_replace', self._config, kwargs))
        self._DLP_PATTERNS = extra_dlp if replace else list(type(self)._DLP_PATTERNS) + extra_dlp
        self._INJECTION_PATTERNS = list(type(self)._INJECTION_PATTERNS) + _compile_patterns(
            [dict(e, ignore_case=e.get('ignore_case', True)) if isinstance(e, dict) else e
             for e in self._resolve_param('injection_patterns', self._config, kwargs) or []],
            'injection_patterns', False)
        replicas = max(1, int(self._resolve_param('llm_replica_count', self._config, kwargs)))
        self._pod_semaphore = asyncio.Semaphore(
            max(1, int(self._resolve_param('pod_concurrency', self._config, kwargs))))
        self._tenant_permits: Dict[str, _TenantPermit] = {}
        self._inflight = 0
        self._draining = False
        self.stats: Dict[str, float] = {
            'permit_wait_total': 0.0, 'quota_wait_total': 0.0, 'permit_timeouts': 0,
            'quota_timeouts': 0, 'quota_rejections': 0, 'cancelled_cleanups': 0, 'peak_inflight': 0}
        # circuit breaker around the upstream call; permits already bound
        # concurrency, so the policy runs without its own bulkhead
        self._resilience = ResiliencePolicy('llm-upstream', self._config, kwargs, bulkhead=False)
        # Per-pod share of the fleet quota: configured limit / replica count.
        self._max_rpm = float(self._resolve_param('max_rpm', self._config, kwargs)) / replicas
        self._max_tpm = float(self._resolve_param('max_tpm', self._config, kwargs)) / replicas
        self._rpm_tokens = self._max_rpm
        self._tpm_tokens = self._max_tpm
        self._last_bucket_update = time.monotonic()
        self._throttle_factor = 1.0                     # v1.6.1: < 1 for a while after a throttle
        self._bucket_lock = asyncio.Lock()

    # ----------------------------------------------------------------
    # PUBLIC 1 — unary
    # ----------------------------------------------------------------
    async def process_inference(
            self, messages: List[Dict[str, Any]], config: Dict[str, Any] = None,
            correlation_id: str = None, io_config: Dict[str, Any] = None,
            idempotency_key: str = None, tenant_id: str = None,
            message_log: List[dict] = None, failed_keys: List[str] = None,
            deadline: Any = None, **kwargs) -> Dict[str, Any]:
        """The one unary entry point. Runs the governed call (__infer, below), then, v1.6.1:
        * a failure or refusal whose kind matches a `model_routes` entry is answered by the
          route's model (`to`) or bound service (`llm_service`), once per route, recorded as
          `fallback_from`; a service that declares `refusal_fallback_server` falls back on its
          own side, so E2A takes no refusal route for it;
        * the result is enriched for telemetry (`_record_telemetry`), with `cost_usd` from
          `model_price_table` when the model is priced."""
        route_depth = kwargs.pop('_e2a_route_depth', 0)
        telemetry_ctx = {'agent': kwargs.pop('agent_name', None), 'step': kwargs.pop('workflow_step', None)}
        cfg = {**self._config, **(config or {})}
        failed_keys = failed_keys if failed_keys is not None else []
        failed_before = len(failed_keys)
        payload = await self.__infer(messages, config, correlation_id, io_config, idempotency_key, tenant_id,
                                     message_log, failed_keys, deadline, **kwargs)
        if route_depth == 0 and payload.get('status') in ('FAILED', 'REFUSED'):
            routed = await self.__route(payload, messages, config, cfg, correlation_id, io_config, idempotency_key,
                                        tenant_id, message_log, failed_keys, deadline, kwargs)
            if routed is not None:
                del failed_keys[failed_before:]          # the routed answer stands: no failed key
                payload = routed
        self.__telemetry(payload, cfg, kwargs, tenant_id, telemetry_ctx)
        return payload

    async def __route(self, payload, messages, config, cfg, correlation_id, io_config, idempotency_key,
                      tenant_id, message_log, failed_keys, deadline, kwargs):
        kind = 'refusal' if payload.get('status') == 'REFUSED' else payload.get('error_kind')
        if kind == 'refusal' and 'refusal_fallback_server' in capabilities_of(self, cfg):
            return None                                     # the provider already fell back
        for route in self._resolve_param('model_routes', cfg, kwargs) or []:
            if kind not in (route.get('on') or []):
                continue
            target = route.get('llm_service') or self
            if not isinstance(target, BaseLLMAgentService):
                raise ValueError(f"model_routes: llm_service {target!r} is not a bound BaseLLMAgentService "
                                 f"(name a seam in the profile; e2a.bindings resolves it)")
            call = {k: v for k, v in kwargs.items() if k != 'model_id'}
            if route.get('to'):
                call['model_id'] = route['to']
            for _ in range(max(1, int(route.get('max_attempts', 1)))):
                routed = await target.process_inference(
                    messages, config, correlation_id, io_config, idempotency_key, tenant_id, message_log,
                    failed_keys, deadline, _e2a_route_depth=1, **call)
                if routed.get('status') == 'SUCCESS':
                    routed['fallback_from'] = {'model_id': self._resolve_param('model_id', cfg, kwargs),
                                               'reason': kind, 'service': type(self).__name__}
                    self._log(message_log, correlation_id, 'WARN', 'llm_fallback_route', reason=kind,
                              to=route.get('to') or type(target).__name__)
                    return routed
            return None                                     # first matching route only
        return None

    def __telemetry(self, payload, cfg, kwargs, tenant_id, telemetry_ctx):
        usage = payload.get('usage') or {}
        model = self._resolve_param('model_id', cfg, kwargs)
        attributes = {
            'e2a.tenant_id': tenant_id, 'e2a.correlation_id': payload.get('correlation_id'),
            'e2a.agent': telemetry_ctx.get('agent'), 'e2a.workflow_step': telemetry_ctx.get('step'),
            'e2a.status': payload.get('status'), 'e2a.fallback_from': (payload.get('fallback_from') or {}).get('model_id'),
            'e2a.refusal_category': (payload.get('refusal') or {}).get('category') or None,
            'gen_ai.request.model': (payload.get('fallback_from') and kwargs.get('model_id')) or model,
            'gen_ai.response.finish_reasons': payload.get('stop_reason'),
            'gen_ai.usage.input_tokens': usage.get('input_tokens'),
            'gen_ai.usage.output_tokens': usage.get('output_tokens'),
        }
        prices = (self._resolve_param('model_price_table', cfg, kwargs) or {}).get(model)
        if prices and payload.get('status') == 'SUCCESS':
            inp = usage.get('input_tokens') if usage else payload.get('tokens_used', 0)
            out = usage.get('output_tokens', 0) if usage else 0
            cost = (float(inp or 0) * float(prices.get('input_per_mtok', 0))
                    + float(out or 0) * float(prices.get('output_per_mtok', 0))
                    + float(usage.get('cache_read_tokens', 0) or 0) * float(prices.get('cache_read_per_mtok', 0))) / 1e6
            payload['cost_usd'] = round(cost, 8)
            attributes['e2a.cost_usd'] = payload['cost_usd']
        self._record_telemetry({k: v for k, v in attributes.items() if v is not None}, cfg)

    def _record_telemetry(self, attributes: Dict[str, Any], config: Dict[str, Any]) -> None:
        """Hook (v1.6.1): enrich the active OpenTelemetry span (when the OpenTelemetry API is
        installed and a span is recording; the vendors' SDK instrumentation creates it) and
        pass the attributes to a bound observability engine's record_inference(). E2A adds
        tenant, correlation, agent, step, refusal or fallback and cost; it emits no spans."""
        engine = (config or {}).get('observability_engine')
        if engine is not None and callable(getattr(engine, 'record_inference', None)):
            engine.record_inference(dict(attributes))
        try:
            from opentelemetry import trace
        except ImportError:
            return
        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attributes(attributes)

    async def __infer(
            self, messages: List[Dict[str, Any]], config: Dict[str, Any] = None,
            correlation_id: str = None, io_config: Dict[str, Any] = None,
            idempotency_key: str = None, tenant_id: str = None,
            message_log: List[dict] = None, failed_keys: List[str] = None,
            deadline: Any = None, **kwargs) -> Dict[str, Any]:
        """Template method. Fixed lifecycle: sanitise -> semantic cache ->
        context budget -> KV prefix cache -> tool schemas -> (tenant + pod)
        concurrency permit [bounded wait] -> RPM/TPM quota admission
        [bounded wait] -> upstream call under min(llm_max_latency, remaining
        deadline) behind a circuit breaker -> output framing -> egress DLP
        -> usage accounting. `deadline` is the additive propagation field
        (a Deadline or remaining seconds): every wait is bounded by it, and
        permits and quota are released in finally-style cleanup on failure,
        deadline AND cancellation. Never raises a request failure past this
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
        deadline = Deadline.coerce(deadline)
        quota, dispatched = None, False
        note = lambda event, **f: self._log(message_log, correlation_id, 'WARN', event, **f)
        payload: Dict[str, Any] = {}
        try:
            if self._draining:
                raise AdmissionTimeout('E2A service draining: not accepting new inference')
            if deadline:
                deadline.check('inference')
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
            # v1.6.1 (P9): the bound service's declared features decide, not its vendor
            features = self._features_for(cfg)
            if kwargs.get('response_schema') is not None and features.get('structured_output') == 'none':
                raise StructuredOutputUnsupported(
                    f'{type(self).__name__} declares no structured output: bind a service that does, '
                    f'or list "structured_output" in the profile features so verify() refuses it at startup')
            if temperature is not None and features.get('sampling') == 'unsupported':
                note('param_dropped', param='temperature', reason='sampling unsupported by the bound model')
                temperature = None
            effort = self._resolve_param('reasoning_effort', cfg, kwargs)
            if effort is not None:
                if effort not in REASONING_EFFORTS:
                    raise ValueError(f'reasoning_effort {effort!r}: use one of {", ".join(REASONING_EFFORTS)}')
                if features.get('reasoning') == 'none':
                    note('param_dropped', param='reasoning_effort', reason='reasoning unsupported by the bound model')
                    kwargs = {k: v for k, v in kwargs.items() if k != 'reasoning_effort'}

            estimate = sum(self._estimate_tokens(m) for m in prepared) + max_out
            async with self.__concurrency_permit(tenant_id, cfg, kwargs, deadline, note):
                quota = (None if self._resolve_param('quota_authority', cfg, kwargs) == 'gateway'
                         else await self.__admit_quota(estimate, tenant_id, cfg, kwargs, deadline))
                up_timeout = float(self._resolve_param('llm_max_latency', cfg, kwargs))
                if deadline:
                    up_timeout = deadline.bound(up_timeout)

                async def _upstream():
                    nonlocal dispatched
                    dispatched = True
                    try:
                        return await asyncio.wait_for(
                            self._dispatch_upstream_call(
                                messages=prepared, model_id=model_id, temperature=temperature,
                                max_tokens=max_out, tools=compiled_tools,
                                stop_sequences=stop_sequences, cache_handle=cache_handle,
                                config=cfg, **common,
                                **self.__extras(kwargs, 'model_id', 'temperature', 'max_tokens',
                                                'tools', 'stop_sequences', 'cache_handle', 'config')),
                            timeout=up_timeout)
                    except asyncio.TimeoutError:
                        if deadline and deadline.expired():
                            raise DeadlineExceeded('request deadline exceeded during the upstream call')
                        raise NFRViolationError(
                            f'LLM inference latency SLO breached: > {up_timeout:.2f}s')

                # v1.5.1: model calls are retried (they have no side effect) when the
                # failure is transient, throttled or overloaded, honouring Retry-After
                # within the deadline; throttling does not count toward the circuit.
                retry_on = set(self._resolve_param('llm_retry_on', cfg, kwargs))
                circuit_counts = set(self._resolve_param('circuit_counts', cfg, kwargs))
                retry_after_cap = float(self._resolve_param('llm_retry_after_cap', cfg, kwargs))

                def _retryable(e):
                    c = self._classify_error(e)
                    return c.kind in retry_on and (c.retry_after is None or c.retry_after <= retry_after_cap)

                def _counts(e):
                    kind = self._classify_error(e).kind
                    if kind == 'throttled':
                        self.__throttled(cfg)              # v1.6.1 (P7 b): admit less, for a while
                    return kind in circuit_counts

                raw = await self._resilience.call(
                    _upstream, deadline=deadline,
                    max_retries=int(self._resolve_param('llm_max_retries', cfg, kwargs)),
                    retryable=_retryable,
                    counts_for_circuit=_counts,
                    delay_for=lambda e: self._classify_error(e).retry_after)
                if isinstance(raw, ModelResult):          # v1.6.0: canonical result or legacy dict
                    raw = raw.to_raw()
                if isinstance(raw, dict) and raw.get('stop_reason') == 'refusal':
                    raise ModelRefusal(getattr(self, 'provider', 'model'), str(model_id), str(raw.get('text') or ''),
                                       str((raw.get('metadata') or {}).get('refusal_category') or ''))

            payload = self._format_and_validate_output(
                raw, kwargs.get('response_schema'), model_id, cache_handle is not None,
                correlation_id, tenant_id, cfg)
            payload['tokens_used'] = self.__track_usage(prepared, raw)
            # v1.5.1: the provider's metadata is kept (stop reason, request id, token split);
            # the base's own fields win on a clash.
            provider_meta = dict(raw.get('metadata') or {}) if isinstance(raw, dict) else {}
            payload['metadata'] = {**provider_meta, 'model_id': model_id, 'tokens_used': payload['tokens_used']}
            for field in ('stop_reason', 'usage', 'provider_request_id'):
                value = raw.get(field) if isinstance(raw, dict) and field in raw else provider_meta.get(field)
                if value is not None:
                    payload[field] = value
            if payload.get('stop_reason') == 'max_tokens':
                payload['truncated'] = True          # v1.6.0: the answer was cut by the token limit
            problems = self.__check_structured(payload, kwargs.get('response_schema'))
            if self._resolve_param('dlp_enabled', cfg, kwargs) and 'text' in payload:
                payload['text'], payload['dlp_redactions'] = self._redact_sensitive(payload['text'])
            self.__settle_quota(quota, dispatched, used=payload['tokens_used'])
            quota = None
            if problems and self._resolve_param('structured_output_strict', cfg, kwargs):
                raise StructuredOutputInvalid('structured output does not match the schema: ' + '; '.join(problems[:3]))
            if use_cache and payload.get('physical_success') and 'tool_calls' not in payload:
                await self._store_semantic_cache(
                    messages, payload, cfg, tenant_id=tenant_id, **self.__extras(kwargs))
        except DehydrationInterrupt:
            self.__settle_quota(quota, dispatched, used=None)
            raise
        except asyncio.CancelledError:
            # BaseException: the `except Exception` below never sees it, so
            # the admitted quota MUST be returned here (cancellation cleanup)
            self.__settle_quota(quota, dispatched, used=None)
            self.stats['cancelled_cleanups'] += 1
            self._emit_metric('counter', 'e2a.cancellation.cleanup_total', 1, {'owner': 'llm_service'})
            raise
        except ModelRefusal as refusal:
            # v1.6.0: a policy outcome, not a fault: no failed key, no _handle_error
            self.__settle_quota(quota, dispatched, used=None)
            payload = {'status': 'REFUSED', 'correlation_id': correlation_id, 'tenant_id': tenant_id,
                       'refusal': refusal.as_dict(), 'text': '', 'physical_success': False, 'tokens_used': 0}
        except Exception as e:
            self.__settle_quota(quota, dispatched, used=None)
            key = idempotency_key or correlation_id
            if key not in failed_keys:
                failed_keys.append(key)
            self._handle_error(e, messages, cfg, **common, failed_keys=failed_keys)
            payload = {'status': 'FAILED', 'correlation_id': correlation_id,
                       'tenant_id': tenant_id, 'error': str(e),
                       'error_type': type(e).__name__, 'error_kind': self._classify_error(e).kind,
                       'physical_success': False, 'tokens_used': 0}
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
            deadline: Any = None, **kwargs) -> AsyncGenerator[str, None]:
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
        start, emitted = time.time(), 0
        deadline = Deadline.coerce(deadline)
        quota, dispatched, completed = None, False, False
        note = lambda event, **f: self._log(message_log, correlation_id, 'WARN', event, **f)
        try:
            if self._draining:
                raise AdmissionTimeout('E2A service draining: not accepting new inference')
            if deadline:
                deadline.check('stream inference')
            self.__sanitize_prompts(messages)
            prepared = self._manage_context_budget(
                messages, self._resolve_param('context_budget', cfg, kwargs),
                kwargs.get('reasoning_budget'), cfg, **common)
            max_out = int(self._resolve_param('max_output_tokens', cfg, kwargs))
            estimate = sum(self._estimate_tokens(m) for m in prepared) + max_out
            holdback = int(self._resolve_param('dlp_holdback_chars', cfg, kwargs))
            dlp_on = self._resolve_param('dlp_enabled', cfg, kwargs)
            async with self.__concurrency_permit(tenant_id, cfg, kwargs, deadline, note):
                quota = (None if self._resolve_param('quota_authority', cfg, kwargs) == 'gateway'
                         else await self.__admit_quota(estimate, tenant_id, cfg, kwargs, deadline))
                dispatched = True
                raw_stream = self._dispatch_upstream_stream(
                    messages=prepared,
                    model_id=self._resolve_param('model_id', cfg, kwargs),
                    temperature=self._resolve_param('temperature', cfg, kwargs),
                    max_tokens=max_out, config=cfg, **common,
                    **self.__extras(kwargs, 'model_id', 'temperature', 'max_tokens', 'config'))
                async for chunk in self.__stream_with_dlp(raw_stream, holdback, dlp_on):
                    if deadline and deadline.expired():
                        raise DeadlineExceeded('request deadline exceeded mid-stream')
                    emitted += len(chunk)
                    yield chunk
                completed = True
        except DehydrationInterrupt:
            raise
        except asyncio.CancelledError:
            self.stats['cancelled_cleanups'] += 1
            self._emit_metric('counter', 'e2a.cancellation.cleanup_total', 1, {'owner': 'llm_service'})
            raise
        except Exception as e:
            key = idempotency_key or correlation_id
            if key not in failed_keys:
                failed_keys.append(key)
            self._handle_error(e, messages, cfg, **common, failed_keys=failed_keys)
            raise
        finally:
            # runs on completion, failure, cancellation and aclose()
            self.__settle_quota(quota, dispatched, used=(emitted // 4 + 1) if completed else None)
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

    async def _release_shared_quota(self, tokens, tenant_id, config, **kwargs) -> None:
        """Hook: return fleet-wide quota taken by _acquire_shared_quota()
        when local admission then failed. Default no-op."""
        return None

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

    def _classify_error(self, error: BaseException) -> ErrorClass:
        """Hook (v1.5.1): what kind of failure the upstream call raised, which decides
        whether it is retried (llm_retry_on) and whether it counts toward the circuit
        (circuit_counts). The default (e2a.core.classify_error) reads HTTP statuses,
        Retry-After and connection/timeout errors the way the common SDKs expose them;
        override it only when a provider SDK signals failures differently."""
        return classify_error(error)

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

    def _emit_metric(self, kind: str, name: str, value: float, dimensions: Dict[str, Any] = None) -> None:
        """Hook (default no-op): forward a metric from the E2A_METRICS
        catalogue to BaseObservability's typed emitters / the metrics
        backend. kind: counter | histogram | gauge."""

    def __observe_wait(self, kind: str, waited: float, tenant_id: str, note=None) -> None:
        self.stats[f'{kind}_wait_total'] += waited
        self._emit_metric('histogram', f'e2a.llm.{kind}_wait_seconds', waited, {'tenant_id': tenant_id})
        slow = float(self._resolve_param('slow_wait_threshold_seconds', self._config, {}))
        if note and waited >= slow:
            note('llm_slow_wait', kind=kind, waited=round(waited, 3))

    @property
    def ready(self) -> bool:
        """Readiness signal: False once drain() has started, so the
        platform stops routing new traffic before in-flight work ends."""
        return not self._draining

    async def drain(self, timeout: float = None, config: Dict[str, Any] = None) -> bool:
        """Graceful shutdown: flip readiness, reject new inference, wait up
        to drain_timeout_seconds (which must be shorter than the platform's
        SIGTERM-to-SIGKILL window — see validate_capacity()) for in-flight
        work. Returns True if everything finished; the caller cancels the
        remainder otherwise, and cancellation cleanup releases their
        permits and quota."""
        self._draining = True
        limit = float(timeout if timeout is not None
                      else self._resolve_param('drain_timeout_seconds', {**self._config, **(config or {})}, {}))
        end = time.monotonic() + limit
        while self._inflight > 0 and time.monotonic() < end:
            await asyncio.sleep(0.01)
        return self._inflight == 0

    @property
    def inflight(self) -> int:
        """Requests currently holding a pod permit (gauge e2a.llm.inflight)."""
        return self._inflight

    async def __bounded_acquire(self, sem, timeout, deadline, what) -> None:
        if not sem.locked():
            await sem.acquire()
            return
        try:
            await asyncio.wait_for(sem.acquire(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            self.stats['permit_timeouts'] += 1
            self._emit_metric('counter', 'e2a.admission.rejected_total', 1, {'reason': 'permit_timeout'})
            if deadline and deadline.expired():
                raise DeadlineExceeded(f'request deadline exceeded waiting for the {what}')
            raise AdmissionTimeout(f'E2A Admission Timeout: no {what} within {max(0.0, timeout):.2f}s')

    def __tenant_entry(self, tenant_id, cap, config, kwargs) -> _TenantPermit:
        now = time.monotonic()
        evict = float(self._resolve_param('tenant_idle_evict_seconds', config, kwargs))
        for k in [k for k, e in self._tenant_permits.items() if e.active == 0 and now - e.last_used > evict]:
            del self._tenant_permits[k]          # idle tenants do not accumulate
        entry = self._tenant_permits.get(tenant_id)
        if entry is None or (entry.cap != cap and entry.active == 0):
            entry = self._tenant_permits[tenant_id] = _TenantPermit(cap)   # picks up a changed cap once idle
        return entry

    @contextlib.asynccontextmanager
    async def __concurrency_permit(self, tenant_id, config, kwargs, deadline=None, note=None):
        """Optional tenant-fairness cap, then the pod semaphore. The WAIT is
        bounded by min(permit_timeout, remaining deadline); nothing is
        queued indefinitely. Always released, including on cancellation."""
        timeout = float(self._resolve_param('permit_timeout', config, kwargs))
        budget = deadline.bound(timeout) if deadline else timeout
        t0 = time.monotonic()
        entry, tenant_held = None, False
        try:
            cap = int(self._resolve_param('tenant_concurrency', config, kwargs))
            if cap > 0:
                entry = self.__tenant_entry(tenant_id, cap, config, kwargs)
                entry.active += 1
                await self.__bounded_acquire(entry.sem, budget - (time.monotonic() - t0), deadline, 'tenant permit')
                tenant_held = True
            await self.__bounded_acquire(self._pod_semaphore, budget - (time.monotonic() - t0), deadline, 'pod permit')
        except BaseException:
            if tenant_held:
                entry.sem.release()
            if entry:
                entry.active -= 1
                entry.last_used = time.monotonic()
            raise
        self.__observe_wait('permit', time.monotonic() - t0, tenant_id, note)
        self._inflight += 1
        self.stats['peak_inflight'] = max(self.stats['peak_inflight'], self._inflight)
        self._emit_metric('gauge', 'e2a.llm.inflight', self._inflight, {})
        try:
            yield
        finally:
            self._inflight -= 1
            self._pod_semaphore.release()
            if tenant_held:
                entry.sem.release()
            if entry:
                entry.active -= 1
                entry.last_used = time.monotonic()

    async def __admit_quota(self, tokens, tenant_id, config, kwargs, deadline=None):
        """Dual token bucket (requests/min and tokens/min). Fails fast with
        a typed AdmissionTimeout after min(quota_timeout, remaining
        deadline) rather than queueing a caller indefinitely. The wait is
        computed from the refill rate, not busy-polled. Returns the
        (rpm_units, tpm_tokens) actually consumed, for settlement."""
        timeout = float(self._resolve_param('quota_timeout', config, kwargs))
        budget = deadline.bound(timeout) if deadline else timeout
        start = time.monotonic()
        if not await self._acquire_shared_quota(tokens, tenant_id, config):
            self.stats['quota_rejections'] += 1
            self._emit_metric('counter', 'e2a.admission.rejected_total', 1, {'reason': 'shared_quota'})
            raise AdmissionTimeout('E2A Quota Exhaustion: shared fleet quota denied.')
        need_tpm = min(tokens, self._max_tpm)
        try:
            while True:
                async with self._bucket_lock:
                    now = time.monotonic()
                    elapsed, self._last_bucket_update = now - self._last_bucket_update, now
                    if self._throttle_factor < 1.0:      # v1.6.1: recover linearly after a throttle
                        recovery = max(1e-6, float(self._resolve_param('admission_recovery_seconds', config, kwargs)))
                        self._throttle_factor = min(1.0, self._throttle_factor + elapsed / recovery)
                    rpm_cap, tpm_cap = self._max_rpm * self._throttle_factor, self._max_tpm * self._throttle_factor
                    self._rpm_tokens = min(rpm_cap, self._rpm_tokens + elapsed * rpm_cap / 60.0)
                    self._tpm_tokens = min(tpm_cap, self._tpm_tokens + elapsed * tpm_cap / 60.0)
                    if self._rpm_tokens >= 1.0 and self._tpm_tokens >= need_tpm:
                        self._rpm_tokens -= 1.0
                        self._tpm_tokens -= need_tpm
                        self.__observe_wait('quota', time.monotonic() - start, tenant_id)
                        return (1.0, need_tpm)
                    rpm_rate, tpm_rate = rpm_cap / 60.0, tpm_cap / 60.0
                    wait = max((1.0 - self._rpm_tokens) / rpm_rate if (self._rpm_tokens < 1.0 and rpm_rate > 0) else 0.0,
                               (need_tpm - self._tpm_tokens) / tpm_rate if (self._tpm_tokens < need_tpm and tpm_rate > 0) else 0.0)
                remaining = budget - (time.monotonic() - start)
                if remaining <= 0 or wait == float('inf'):
                    self.stats['quota_timeouts'] += 1
                    self._emit_metric('counter', 'e2a.admission.rejected_total', 1, {'reason': 'quota_timeout'})
                    if deadline and deadline.expired():
                        raise DeadlineExceeded('request deadline exceeded waiting for RPM/TPM quota')
                    raise AdmissionTimeout('E2A Quota Exhaustion: timed out waiting for RPM/TPM quota.')
                await asyncio.sleep(max(0.005, min(wait, remaining, 1.0)))
        except BaseException:
            await self._release_shared_quota(tokens, tenant_id, config)   # local admission failed: give the fleet quota back
            raise

    @staticmethod
    def __check_structured(payload, schema) -> List[str]:
        """v1.6.1 (P9): every structured result is checked against the schema that was asked
        for, whichever model produced it; marks structured_output_valid and schema_errors."""
        if schema is None or 'structured_output' not in payload:
            return []
        value = payload['structured_output']
        if isinstance(value, dict) and 'parse_error' in value and 'raw' in value:
            problems = [f'$: not valid JSON ({value["parse_error"]})']
        else:
            problems = schema_errors(schema, value)
        payload['structured_output_valid'] = not problems
        if problems:
            payload['schema_errors'] = problems
            payload['physical_success'] = False
        return problems

    def __throttled(self, config) -> None:
        """v1.6.1 (P7 b): the provider throttled us. Lower this pod's admission share by
        admission_throttle_decay (down to admission_throttle_floor) and cap what is available
        now, so callers get E2A's typed AdmissionTimeout instead of more provider 429s; the
        share recovers linearly over admission_recovery_seconds."""
        decay = float(self._resolve_param('admission_throttle_decay', config, {}))
        floor = float(self._resolve_param('admission_throttle_floor', config, {}))
        self._throttle_factor = max(floor, self._throttle_factor * decay)
        self._rpm_tokens = min(self._rpm_tokens, self._max_rpm * self._throttle_factor)
        self._tpm_tokens = min(self._tpm_tokens, self._max_tpm * self._throttle_factor)
        self.stats['throttled'] = self.stats.get('throttled', 0) + 1
        self._emit_metric('counter', 'e2a.llm.throttled_total', 1, {'factor': round(self._throttle_factor, 3)})

    def _features_for(self, config: Dict[str, Any]) -> Dict[str, str]:
        """Hook (v1.6.1): what the bound model supports, as a map the base reads: e.g.
        {'structured_output': 'native' | 'none', 'sampling': 'supported' | 'unsupported',
        'reasoning': '<style>' | 'none'}. Default: the class attribute `features` (empty: the
        base changes nothing, as before). A provider overrides it when it depends on config."""
        return dict(getattr(self, 'features', None) or {})

    def __settle_quota(self, quota, dispatched, used) -> None:
        """Return admitted-but-unused quota. Synchronous on purpose: it must
        run on the cancellation path too. RPM is returned only if the call
        never reached the upstream; tokens are returned for the unused part
        (all of it when the outcome is a failure/cancellation)."""
        if not quota:
            return
        rpm, tpm = quota
        if not dispatched:
            self._rpm_tokens = min(self._max_rpm, self._rpm_tokens + rpm)
        refund = tpm if used is None else max(0, tpm - used)
        if refund > 0:
            self._tpm_tokens = min(self._max_tpm, self._tpm_tokens + refund)

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
