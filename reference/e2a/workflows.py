"""E2A: BaseWorkflow.

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

from .core import ClaimStatus, Deadline, DehydrationInterrupt, PhysicalOutcome, _DETERMINISTIC_ERRORS
from .config import resolve_config
from ._propagation import _PropagationMixin
from .context import RequestContext
from .observability import BaseObservability
from .agents import BaseAgent
from .ports import BaseIdempotencyStore
 
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
        given_ctx = kwargs.pop('ctx', None)       # v1.2: a caller may pass the request context
        tenant_id = given_ctx.tenant_id if given_ctx else state.get('tenant_id')
        if not tenant_id:
            raise ValueError('state["tenant_id"] is required')
        if given_ctx:
            kwargs.setdefault('correlation_id', given_ctx.correlation_id)
            if given_ctx.idempotency_key:
                kwargs.setdefault('idempotency_key', given_ctx.idempotency_key)
            if given_ctx.deadline is not None:
                kwargs.setdefault('deadline', given_ctx.deadline)
        correlation_id = kwargs.get(
            'correlation_id', state.get('correlation_id', str(uuid.uuid4())))
        io_config = self._resolve_io_config(config, **kwargs)
        deadline = self.__resolve_deadline(config, kwargs)
        idempotency_key = self.__resolve_idempotency(state, config, **kwargs)
        # The propagation fields are resolved above and passed on explicitly below;
        # removing them from kwargs keeps a caller-supplied value from reaching a hook
        # twice ("got multiple values for keyword argument").
        for field in ('correlation_id', 'idempotency_key', 'io_config', 'tenant_id',
                      'message_log', 'failed_keys'):
            kwargs.pop(field, None)
        message_log: List[dict] = []
        failed_keys: List[str] = []
        # v1.2: the request context, created once here and handed to the agent as one object
        ctx = RequestContext.begin(tenant_id=tenant_id, config=config, correlation_id=correlation_id,
                                   idempotency_key=idempotency_key, deadline=deadline, overrides=kwargs,
                                   trace=given_ctx.trace if given_ctx else None)
        state['tenant_id'] = tenant_id
        state['correlation_id'] = correlation_id
        state['idempotency_key'] = idempotency_key
        # Atomic claim: exactly one caller owns a material request. A
        # duplicate gets a typed outcome (REPLAYED / IN_PROGRESS / CONFLICT /
        # RECOVERY_REQUIRED / FAILED_FINAL), never a second execution.
        store = config.get('idempotency_store')
        owned, dehydrated, last_error = False, False, None
        if isinstance(store, BaseIdempotencyStore) and self._requires_idempotency_claim(state, config, **kwargs):
            claim = store.claim(
                idempotency_key, config, tenant_id=tenant_id, owner=correlation_id,
                fingerprint=self._request_fingerprint(state, config, **kwargs),
                lease_seconds=int(resolve_config('idempotency_lease_seconds', config, kwargs)),
                ttl_seconds=int(resolve_config('idempotency_ttl_seconds', config, kwargs)))
            if claim.status != ClaimStatus.CLAIMED:
                return self.__short_circuit(state, claim, message_log, correlation_id, config)
            owned = True
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
            if isinstance(agent, BaseAgent):    # v1.2: one context object
                state = agent.run(state, config, ctx=ctx, io_config=io_config,
                                  message_log=message_log, failed_keys=failed_keys, **kwargs)
            else:                               # an agent-like object (e.g. A2C's agent chain): v1.1 keywords
                state = agent.run(
                    state, config, correlation_id=correlation_id,
                    io_config=io_config, idempotency_key=idempotency_key,
                    tenant_id=tenant_id, message_log=message_log,
                    failed_keys=failed_keys, deadline=deadline, **kwargs)
        except DehydrationInterrupt as d:
            self._log(message_log, correlation_id, 'INFO', 'workflow_dehydrated', reason=str(d))
            state['status'] = 'AWAITING_WEBHOOK'
            dehydrated = True
        except Exception as e:
            last_error = e
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
            if owned:
                self.__settle_claim(store, idempotency_key, tenant_id, correlation_id, state,
                                    failed_keys, dehydrated, last_error, config)
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
 
    # PROTECTED hooks with defaults — a subclass overrides only to specialise
    def _requires_idempotency_claim(self, state, config=None, **kwargs) -> bool:
        """Hook: does this request carry a MATERIAL effect (a write /
        side effect that must happen at most once)? Default True. A
        workflow with read-only or purely generative intents overrides it
        so those are not replayed from a stored answer."""
        return True

    def _request_fingerprint(self, state, config=None, **kwargs) -> str:
        """Stable hash of the request's business content. The same
        idempotency key with a different fingerprint is a CONFLICT, never a
        replay. Volatile/propagation fields are excluded."""
        volatile = {'correlation_id', 'idempotency_key', 'message_log', 'failed_keys', 'deadline',
                    'status', 'response', 'outcome', 'cumulative_tokens_used', 'trace_context'}
        body = {k: v for k, v in state.items() if k not in volatile}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def _replayable_result(self, state, config=None, **kwargs) -> Dict[str, Any]:
        """What a duplicate caller gets back on REPLAY. Taken AFTER egress
        redaction, so a stored result never re-exposes redacted fields."""
        return {k: state[k] for k in ('response', 'outcome', 'cumulative_tokens_used') if k in state}

    def _physical_outcome(self, state, config=None, **kwargs) -> Optional['PhysicalOutcome']:
        """Typed commit evidence for the request, or None when the request
        had no material side effect. Default reads state['outcome'] (set by
        the agent from a tool's typed result)."""
        o = state.get('outcome')
        if o is None and isinstance(state.get('response'), dict):
            o = state['response'].get('outcome')
        return PhysicalOutcome(o) if o else None

    # PRIVATE — base-owned, never overridden
    def __resolve_deadline(self, config, kwargs) -> Optional['Deadline']:
        d = kwargs.pop('deadline', None)
        if d is not None:
            return Deadline.coerce(d)
        secs = float(resolve_config('request_deadline_seconds', config, kwargs))
        return Deadline.after(secs) if secs > 0 else None

    def __resolve_idempotency(self, state, config=None, **kwargs) -> str:
        """Client-supplied key wins; otherwise the subclass's domain logic
        mints one. Resolution only DERIVES the key — ownership is the
        atomic claim in execute(), never a lookup-then-persist."""
        client_key = kwargs.get('idempotency_key', state.get('idempotency_key'))
        return client_key if client_key else self._generate_idempotency_key(state, config, **kwargs)

    def __short_circuit(self, state, claim, message_log, correlation_id, config):
        st = {ClaimStatus.REPLAY: 'REPLAYED', ClaimStatus.IN_PROGRESS: 'IN_PROGRESS',
              ClaimStatus.CONFLICT: 'CONFLICT', ClaimStatus.RECOVERY_REQUIRED: 'RECOVERY_REQUIRED',
              ClaimStatus.FAILED_FINAL: 'FAILED_FINAL'}[claim.status]
        state['status'] = st
        state['idempotency_status'] = claim.status.value
        if claim.status == ClaimStatus.REPLAY and claim.result:
            state.update({k: v for k, v in claim.result.items() if k in ('response', 'outcome', 'cumulative_tokens_used')})
            state['replayed'] = True
        self._log(message_log, correlation_id, 'INFO', 'idempotent_short_circuit', claim_status=claim.status.value)
        self._flush_logs(message_log, config)
        state['message_log'], state['failed_keys'] = message_log, []
        return state

    def __settle_claim(self, store, key, tenant_id, owner, state, failed_keys, dehydrated, error, config):
        kw = dict(config=config, tenant_id=tenant_id, owner=owner)
        outcome = self._physical_outcome(state, config)
        if dehydrated:
            store.fail(key, retryable=True, **kw)            # paused, not failed: claimable again on resume
        elif outcome == PhysicalOutcome.UNKNOWN:
            store.fail(key, retryable=False, outcome=PhysicalOutcome.UNKNOWN, **kw)   # parked for reconciliation
        elif failed_keys:
            store.fail(key, retryable=not isinstance(error, _DETERMINISTIC_ERRORS), **kw)
        else:
            store.complete(key, self._replayable_result(state, config), **kw)
