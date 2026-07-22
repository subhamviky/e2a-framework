# e2a_cqrs_base.py — E2A Architecture Framework Scaffold, Deterministic CQRS Profile
# Drop into src/ or framework/. Import and subclass. Never modify directly.
# Companion to e2a_base.py (agentic profile) — reuses the same propagation
# contract and single-entry-point rule; replaces BaseWorkflow/BaseAgent with
# BaseOrchestrator/BaseCommandService/BaseQueryService for deterministic,
# non-LLM request paths.

import os
import uuid
import time
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, ValidationError

logging.basicConfig(level=logging.INFO)


class DehydrationInterrupt(Exception):
    """Signal exception used to cleanly halt execution and scale compute to
    zero pending manual approval (e.g. a Command above a policy threshold)."""
    pass


class NFRViolationError(Exception):
    """Raised when a measured non-functional requirement — the tenant request
    quota in BaseGovernanceFramework.__enforce_tenant_quota(), or the latency
    SLO in BaseCommandService.mutate() / BaseQueryService.fetch() — is
    breached. Falls through the normal exception path like any other
    failure; there is no token-budget variant on this profile since neither
    path calls an LLM."""
    pass


# ==================================================
# 1. Shared Propagation Helpers
# ==================================================
class _PropagationMixin:
    """Shared, base-owned helpers for cross-class state threading. Identical
    to the agentic profile's mixin — observability is not handled here, it is
    migrated to BaseObservability below."""

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

    def _send_to_dlq(self, failed_keys, config=None, **kwargs):
        config = config or {}
        queue_url = kwargs.get('dlq_queue_url', config.get(
            'dlq_queue_url', os.getenv('DLQ_QUEUE_URL')))
        if queue_url and failed_keys:
            logging.warning(f'DLQ dispatch -> {queue_url}: {failed_keys}')

    def _commit_gate(self, key, failed_keys, config=None, **kwargs):
        """Base-owned commit gate. Enforces batch isolation and Optimistic
        Concurrency Control (OCC) via expected_version checks."""
        config = config or {}
        enabled = kwargs.get('commit_gate_enabled', config.get('commit_gate_enabled', True))
        if not enabled:
            return True
        if key in (failed_keys or []):
            return False
        expected_version = kwargs.get('expected_version')
        if expected_version is not None:
            actual_version = kwargs.get('actual_version_lookup', lambda k, c: expected_version)(key, config)
            if expected_version != actual_version:
                return False  # OCC collision
        return True


# ==================================================
# 2. Foundation Classes (Observability, Governance) — CQRS profile
# ==================================================
class BaseObservability(ABC):
    """Template method abstract class for CQRS Service Telemetry. Same
    abstract-class contract as the agentic profile's BaseObservability
    (_ship_logs / _emit_metrics / _export_traces remain the three hooks a
    subclass must implement) — only the derived metrics change, since token
    usage has no meaning on a request path with no LLM call."""

    def record_telemetry(self, message_log: List[dict], correlation_id: str,
                          config: Dict[str, Any] = None, **kwargs) -> None:
        config = config or {}
        enriched_logs = []
        for entry in (message_log or []):
            enriched_entry = {
                **entry,
                'correlation_id': correlation_id,
                'tenant_id': kwargs.get('tenant_id', 'UNKNOWN_TENANT'),
                'host_epoch_ms': int(time.time() * 1000),
                'framework_version': 'e2a-cqrs-v1.0',
            }
            enriched_logs.append(enriched_entry)

        extracted_metrics = self.__extract_metrics(enriched_logs)
        try:
            self._ship_logs(enriched_logs, config, **kwargs)
            if extracted_metrics:
                self._emit_metrics(extracted_metrics, config, **kwargs)
            self._export_traces(correlation_id, enriched_logs, config, **kwargs)
        except Exception as e:
            logging.error(f"Telemetry dispatch failed for {correlation_id}: {str(e)}")
            for log in enriched_logs:
                logging.info(log)

    def __extract_metrics(self, enriched_logs: List[dict]) -> Dict[str, float]:
        # CQRS-relevant counters, derived from BaseOrchestrator/BaseCommandService/
        # BaseQueryService's _log() events — no token/cost metrics on this profile.
        metrics = {
            'command_count': 0, 'query_count': 0,
            'cache_hit_count': 0, 'cache_miss_count': 0,
            'error_count': 0,
        }
        for log in enriched_logs:
            event = log.get('event')
            if log.get('level') == 'ERROR':
                metrics['error_count'] += 1
            if event == 'routing_to_command_service':
                metrics['command_count'] += 1
            elif event == 'routing_to_query_service':
                metrics['query_count'] += 1
            elif event == 'query_cache_hit':
                metrics['cache_hit_count'] += 1
            elif event == 'query_cache_miss':
                metrics['cache_miss_count'] += 1
        return metrics

    @abstractmethod
    def _ship_logs(self, enriched_logs: List[dict], config: Dict[str, Any], **kwargs):
        pass

    @abstractmethod
    def _emit_metrics(self, metrics: Dict[str, float], config: Dict[str, Any], **kwargs):
        pass

    @abstractmethod
    def _export_traces(self, correlation_id: str, logs: List[dict], config: Dict[str, Any], **kwargs):
        pass


class BaseGovernanceFramework(ABC):
    """Template method abstract class for Deterministic Service Governance:
    tenant quota enforcement, an injection/PII semantic firewall on inbound
    Command/Query payloads, isolation for Command handlers that call external
    APIs, and circuit breaking on downstream failures. This replaces the
    agentic profile's AI Safety/Economics framing (LLM token budgets, MCP
    tool sandboxing) with the equivalent deterministic-service concerns."""

    def enforce_governance_gate(self, state: Dict[str, Any], io_config: Dict[str, Any],
                                config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        self.__enforce_tenant_quota(state, io_config)

        if state.get('requires_manual_approval') and not state.get('approval_granted'):
            self.__trigger_dehydration(state, config, **kwargs)

        if state.get('pending_external_call'):
            self._verify_sandbox_profile(config, **kwargs)

        state = self._execute_semantic_firewall(state, config, **kwargs)
        return state

    def __enforce_tenant_quota(self, state: Dict[str, Any], io_config: Dict[str, Any]) -> None:
        # Deterministic-service equivalent of the agentic profile's token
        # budget: a per-tenant request quota over a rolling window, protecting
        # shared Tier 2/Tier 3 capacity rather than LLM spend.
        max_quota = io_config.get('max_requests_per_window', float('inf'))
        current_usage = state.get('cumulative_requests_in_window', 0)
        if current_usage >= max_quota:
            raise NFRViolationError(
                f"Tenant request quota exhausted. "
                f"Used {current_usage}, Limit {max_quota}")

    def __trigger_dehydration(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> None:
        correlation_id = kwargs.get('correlation_id', state.get('correlation_id'))
        tenant_id = kwargs.get('tenant_id', state.get('tenant_id'))
        self._dehydrate_state_to_perimeter(state, correlation_id, tenant_id, config)
        raise DehydrationInterrupt(f"State {correlation_id} dehydrated. Awaiting webhook.")

    @abstractmethod
    def _execute_semantic_firewall(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """Inspect the raw Command/Query payload for injection patterns and
        redact PII before it reaches business logic."""
        pass

    @abstractmethod
    def _verify_sandbox_profile(self, config: Dict[str, Any], **kwargs) -> None:
        """Assert a Command handler's downstream external-API call executes
        inside an isolated network/IAM boundary (dedicated egress security
        group, scoped credential) before that call is made."""
        pass

    @abstractmethod
    def _circuit_breaker(self, failures: int, config: Dict[str, Any], **kwargs) -> bool:
        """Trip on repeated downstream failures (outbox commit, read-replica
        connection, external API call) and short-circuit further attempts."""
        pass

    @abstractmethod
    def _dehydrate_state_to_perimeter(self, state: Dict[str, Any], correlation_id: str,
                                      tenant_id: str, config: Dict[str, Any]) -> None:
        pass


# ==================================================
# 3. Validation (Tier 1 Edge) — reused unmodified from the agentic profile
# ==================================================
class ValidationResult(dict):
    """Thin dict subclass for HTTP 400 serializable validation returns."""
    pass


class BaseValidationService(ABC, _PropagationMixin):
    validator_registry: Dict[str, Any] = {}

    def validate(self, agent_name: str, state: Dict[str, Any],
                 config: Dict[str, Any] = None, correlation_id: str = None,
                 **kwargs) -> 'ValidationResult':
        config = config or {}
        correlation_id = correlation_id or state.get('correlation_id', str(uuid.uuid4()))
        validator = self._resolve_validator(agent_name, config, **kwargs)

        if validator is None:
            strict = kwargs.get('strict_mode', config.get('strict_mode', os.getenv('VALIDATION_STRICT_MODE', 'True') == 'True'))
            if strict:
                return self.__build_result(False, [f'No validator registered for {agent_name}'], agent_name, correlation_id)
            return self.__build_result(True, [], agent_name, correlation_id)

        valid, errors = validator(state, config, **kwargs)
        return self.__build_result(valid, errors, agent_name, correlation_id)

    def _resolve_validator(self, agent_name, config=None, **kwargs):
        return self.validator_registry.get(agent_name)

    def __build_result(self, valid, errors, agent_name, correlation_id) -> 'ValidationResult':
        return ValidationResult(valid=valid, errors=errors, agent_name=agent_name, correlation_id=correlation_id)


# ==================================================
# 4. Orchestration (Tier 2 Compute) — BaseOrchestrator
# ==================================================
class BaseOrchestrator(ABC, _PropagationMixin):
    """Tier 2 lifecycle manager. Replaces BaseWorkflow: routes to
    BaseCommandService or BaseQueryService instead of a BaseAgent, and never
    issues an LLM call."""

    def execute(self, state: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        tenant_id = state.get('tenant_id')
        if not tenant_id:
            raise ValueError('state["tenant_id"] is required')

        correlation_id = kwargs.get('correlation_id', state.get('correlation_id', str(uuid.uuid4())))
        io_config = self._resolve_io_config(config, **kwargs)
        idempotency_key = self.__resolve_idempotency(state, config, **kwargs)

        message_log: List[dict] = []
        failed_keys: List[str] = []
        is_read = self._is_read_scenario(state, config)

        common = dict(correlation_id=correlation_id, tenant_id=tenant_id,
                      io_config=io_config, idempotency_key=idempotency_key,
                      message_log=message_log, failed_keys=failed_keys)

        try:
            gov_engine = config.get('governance_engine')
            if gov_engine and isinstance(gov_engine, BaseGovernanceFramework):
                state = gov_engine.enforce_governance_gate(state, io_config, config, **common)

            if is_read:
                self._log(message_log, correlation_id, 'INFO', 'routing_to_query_service')
                service = self._get_query_service(state.get('resource'), config)
                state = service.fetch(state, config, **common)
            else:
                self._log(message_log, correlation_id, 'INFO', 'routing_to_command_service')
                service = self._get_command_service(state.get('action'), config)
                state = service.mutate(state, config, **common)

        except DehydrationInterrupt as d:
            self._log(message_log, correlation_id, 'INFO', 'orchestrator_dehydrated', reason=str(d))
            state['status'] = 'AWAITING_WEBHOOK'
        except Exception as e:
            failed_keys.append(idempotency_key)
            self._handle_error(e, state, config, **common)
        finally:
            obs_engine = config.get('observability_engine')
            if obs_engine and isinstance(obs_engine, BaseObservability):
                obs_engine.record_telemetry(message_log, correlation_id, config, tenant_id=tenant_id)
            else:
                for entry in message_log:
                    logging.info(entry)  # fallback
            if failed_keys and not is_read:
                self._send_to_dlq(failed_keys, config)
            state['message_log'] = message_log
            state['failed_keys'] = failed_keys

        return state

    @abstractmethod
    def _is_read_scenario(self, state: Dict[str, Any], config: Dict[str, Any]) -> bool: pass
    @abstractmethod
    def _get_query_service(self, resource: str, config: Dict[str, Any]) -> 'BaseQueryService': pass
    @abstractmethod
    def _get_command_service(self, action: str, config: Dict[str, Any]) -> 'BaseCommandService': pass
    @abstractmethod
    def _generate_idempotency_key(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> str: pass
    @abstractmethod
    def _handle_error(self, error: Exception, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> None: pass

    def __resolve_idempotency(self, state, config=None, **kwargs) -> str:
        client_key = kwargs.get('idempotency_key', state.get('idempotency_key'))
        if client_key:
            existing = self.__lookup_idempotency_store(client_key, config)
            if existing and not existing.get('expired'):
                return existing['idempotency_key']
        new_key = self._generate_idempotency_key(state, config, **kwargs)
        self.__persist_idempotency_store(new_key, config)
        return new_key

    def __lookup_idempotency_store(self, key, config=None): return None
    def __persist_idempotency_store(self, key, config=None): pass


# ==================================================
# 5. BaseCommandService — Write Path (Tier 2)
# ==================================================
class BaseCommandService(ABC, _PropagationMixin):
    input_dto_model: Type[BaseModel]
    output_dto_model: Type[BaseModel]

    def mutate(self, payload: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> dict:
        config = config or {}
        key = kwargs.get('idempotency_key')
        failed_keys = kwargs.get('failed_keys', [])
        message_log = kwargs.get('message_log', [])
        correlation_id = kwargs.get('correlation_id')
        start = time.time()

        try:
            validated_input = self.input_dto_model(**payload)
        except ValidationError as e:
            self._log(message_log, correlation_id, 'ERROR', 'dto_validation_failed', error=str(e))
            raise ValueError(f'Schema validation failed: {e}')

        if key and not self._commit_gate(key, failed_keys, config, **kwargs):
            self._log(message_log, correlation_id, 'WARN', 'commit_gate_rejected')
            return self.output_dto_model(status='blocked', reason='commit_gate_rejection').dict()

        processed_dto = self._execute_business_logic(validated_input, config, **kwargs)
        self._execute_outbox_commit(processed_dto, config, **kwargs)
        self._synchronize_cache(processed_dto, config, **kwargs)

        latency = time.time() - start
        max_latency = kwargs.get('max_latency', config.get('max_latency', float(os.getenv('MAX_LATENCY', 1.0))))
        self._log(message_log, correlation_id, 'INFO', 'command_mutation_complete', latency=round(latency, 3))
        if latency > max_latency:
            raise NFRViolationError(f'Command latency SLO breached: {latency:.2f}s > {max_latency}s')
        return processed_dto.dict()

    @abstractmethod
    def _execute_business_logic(self, validated_input: BaseModel, config: dict, **kwargs) -> BaseModel: pass
    @abstractmethod
    def _execute_outbox_commit(self, processed_dto: BaseModel, config: dict, **kwargs) -> None: pass
    @abstractmethod
    def _synchronize_cache(self, processed_dto: BaseModel, config: dict, **kwargs) -> None: pass


# ==================================================
# 6. BaseQueryService — Read Path (Tier 2 -> Tier 3 Cache-Aside)
# ==================================================
class BaseQueryService(ABC, _PropagationMixin):
    output_dto_model: Type[BaseModel]

    def fetch(self, query_params: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> dict:
        config = config or {}
        tenant_id = kwargs.get('tenant_id')
        correlation_id = kwargs.get('correlation_id')
        message_log = kwargs.get('message_log', [])
        io_config = kwargs.get('io_config', {})
        start = time.time()

        cache_key = self._generate_cache_key(query_params, tenant_id)
        cached_data = self._read_from_cache(cache_key, io_config)
        if cached_data:
            self._log(message_log, correlation_id, 'INFO', 'query_cache_hit', latency=round(time.time() - start, 3))
            return self.output_dto_model(**cached_data).dict()

        self._log(message_log, correlation_id, 'INFO', 'query_cache_miss')
        db_data = self._execute_db_read(query_params, config, **kwargs)
        if db_data:
            self._write_to_cache(cache_key, db_data, io_config)

        latency = time.time() - start
        max_latency = kwargs.get('max_latency', config.get('max_latency', float(os.getenv('MAX_LATENCY', 1.0))))
        self._log(message_log, correlation_id, 'INFO', 'query_fetch_complete', latency=round(latency, 3))
        if latency > max_latency:
            raise NFRViolationError(f'Query latency SLO breached: {latency:.2f}s > {max_latency}s')
        return self.output_dto_model(**db_data).dict()

    @abstractmethod
    def _generate_cache_key(self, query_params: dict, tenant_id: str) -> str: pass
    @abstractmethod
    def _read_from_cache(self, cache_key: str, io_config: dict) -> Optional[dict]: pass
    @abstractmethod
    def _execute_db_read(self, query_params: dict, config: dict, **kwargs) -> dict: pass
    @abstractmethod
    def _write_to_cache(self, cache_key: str, data: dict, io_config: dict) -> None: pass
