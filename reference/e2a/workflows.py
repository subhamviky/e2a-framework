"""E2A: BaseWorkflow, and (v1.2, design note 05) declared workflow steps.

A v1.2 workflow (its _build_workflow declares `ctx`) returns Steps: Step / Route items
run in order under one context, one deadline, one log and ONE idempotency claim, taken
when the first material step is reached. A step that pauses for approval stores a
checkpoint; resume() continues at that step. Deliberately not included: loops,
parallel branches, arbitrary graphs. A v1.1 workflow runs exactly as before."""
import copy
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

from typing import Mapping, Sequence

from .core import ClaimStatus, Deadline, DehydrationInterrupt, PhysicalOutcome, _DETERMINISTIC_ERRORS
from .config import resolve_config
from ._propagation import _PropagationMixin
from ._contract import call_hook, declares_ctx
from .context import MessageLog, RequestContext
from .observability import BaseObservability
from .agents import BaseAgent
from .ports import BaseCheckpointStore, BaseIdempotencyStore
from .stages import GovernanceRefusal


# ==================================================
# Declared steps (v1.2, design note 05)
# ==================================================

@dataclass(frozen=True)
class Step:
    """One agent run. `when(state)` False -> recorded as SKIPPED. `stop_if(state)` True
    after the run -> the workflow ends here. `material=True`: the idempotency claim is
    taken before this step (when no step is material, before the first step)."""
    name: str
    agent: Any
    when: Optional[Callable[[Dict[str, Any]], bool]] = None
    stop_if: Optional[Callable[[Dict[str, Any]], bool]] = None
    material: bool = False

    def agent_for(self, state: Dict[str, Any]) -> Any:
        return self.agent


@dataclass(frozen=True)
class Route:
    """One agent run, chosen by a state field: agents[state[by]] (or `default`)."""
    name: str
    by: str
    agents: Mapping[str, Any]
    default: Any = None
    when: Optional[Callable[[Dict[str, Any]], bool]] = None
    stop_if: Optional[Callable[[Dict[str, Any]], bool]] = None
    material: bool = False

    def agent_for(self, state: Dict[str, Any]) -> Any:
        agent = self.agents.get(state.get(self.by), self.default)
        if agent is None:
            raise ValueError(f"step '{self.name}': no agent for {self.by}={state.get(self.by)!r}")
        return agent


@dataclass(frozen=True)
class _Resolved:
    """A v1.2 workflow that returns a definition instead of Steps: one step whose agent
    comes from _get_agent(), as in v1.1."""
    name: str
    resolve: Callable[[Dict[str, Any]], Any]
    when = None
    stop_if = None
    material = False

    def agent_for(self, state):
        return self.resolve(state)


class Steps:
    """The ordered steps of a workflow."""

    def __init__(self, *steps: Any) -> None:
        if not steps:
            raise ValueError('a workflow needs at least one step')
        names = [s.name for s in steps]
        if len(set(names)) != len(names):
            raise ValueError(f'step names must be unique: {names}')
        self.steps: Sequence[Any] = tuple(steps)

    def index(self, name: str) -> int:
        for i, step in enumerate(self.steps):
            if step.name == name:
                return i
        raise ValueError(f"no step named '{name}'")

    @property
    def claim_at(self) -> int:
        return next((i for i, s in enumerate(self.steps) if s.material), 0)


class ResumeRefused(GovernanceRefusal, PermissionError):
    """resume() refused: code UNKNOWN_CHECKPOINT, ALREADY_DECIDED or DECIDER_REFUSED."""
    code = 'RESUME_REFUSED'


def _snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    body = {k: v for k, v in state.items() if k not in ('message_log', 'failed_keys')}
    try:
        return copy.deepcopy(body)
    except Exception:           # an object that cannot be copied travels by reference
        return dict(body)
 
# ==================================================
# BaseWorkflow
# ==================================================
 
class BaseWorkflow(ABC, _PropagationMixin):
 
    def execute(self, state: Dict[str, Any], config: Dict[str, Any] = None,
                **kwargs) -> Dict[str, Any]:
        """Public entry point. Origin of every propagation field — the
        only place correlation_id, io_config, and idempotency_key are
        generated from scratch.

        v1.2: when _build_workflow declares `ctx`, the workflow runs as declared Steps
        (see Steps); otherwise exactly as in v1.1."""
        config = config or {}
        if declares_ctx(self._build_workflow):
            return self.__execute_steps(state, config, kwargs)
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
            self.__checkpoint(_snapshot(state), state, ctx, None, d, config)
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
 
    def _validate_workflow(self, workflow, config=None, **kwargs):
        """kwargs: required_nodes. Default (v1.2): valid; Steps validate themselves."""
        return True

    def _get_agent(self, intent, config=None, **kwargs) -> BaseAgent:
        """kwargs: agent_registry. Not needed when _build_workflow returns Steps (v1.2)."""
        raise NotImplementedError('implement _get_agent(), or return Steps from _build_workflow(ctx)')
 
    @abstractmethod
    def _generate_idempotency_key(self, state, config=None, **kwargs) -> str:
        """Domain logic, e.g. hash(tenant_id + REF_ELEM_KEY).
        Called only when no valid client-supplied key exists."""
 
    @abstractmethod
    def _handle_error(self, error, state, config=None, **kwargs):
        pass
 
    # ------------------------------------------------------------------ resume (v1.2, E-17)
    def resume(self, checkpoint_id: str, decision: Dict[str, Any], config: Dict[str, Any] = None,
               tenant_id: str = None, **kwargs) -> Dict[str, Any]:
        """Public entry point (v1.2): continue a paused workflow with a human decision,
        often days after it paused. decision = {'approved': bool, 'decided_by': str, ...}.
        Guarantees: the checkpoint exists for this tenant and is still pending; the decider
        is allowed (_authorize_decision, four eyes by default); it is decided exactly once
        (a second or concurrent resume is refused); an approved resume continues AT the
        paused step, under the original idempotency key. A rejection executes nothing.
        Raises ResumeRefused (code UNKNOWN_CHECKPOINT | ALREADY_DECIDED | DECIDER_REFUSED)."""
        config = config or {}
        given = kwargs.pop('ctx', None)
        tenant_id = tenant_id or (given.tenant_id if given else None)
        if not tenant_id:
            raise ValueError('resume(): a tenant_id is required')
        store = config.get('checkpoint_store')
        if not isinstance(store, BaseCheckpointStore):
            raise ValueError('resume() needs config["checkpoint_store"] (a BaseCheckpointStore)')
        checkpoint = store.load(checkpoint_id, config, tenant_id=tenant_id)
        if checkpoint is None:
            raise ResumeRefused(f'no checkpoint {checkpoint_id} for this tenant', 'resume', code='UNKNOWN_CHECKPOINT')
        if checkpoint['status'] != 'PENDING':
            raise ResumeRefused(f'checkpoint {checkpoint_id} was already decided', 'resume', code='ALREADY_DECIDED')
        ctx = RequestContext.begin(
            tenant_id=tenant_id, config=config, idempotency_key=checkpoint['idempotency_key'],
            correlation_id=kwargs.pop('correlation_id', None) or (given.correlation_id if given else None),
            trace={'resumed_from': checkpoint['correlation_id'], 'checkpoint_id': checkpoint_id},
            overrides=kwargs)
        if not self._authorize_decision(checkpoint, decision, ctx):
            raise ResumeRefused('the decider may not decide this checkpoint', 'resume', code='DECIDER_REFUSED')
        approved = bool(decision.get('approved'))
        if not store.decide(checkpoint_id, 'APPROVED' if approved else 'REJECTED', decision.get('decided_by'),
                            config, tenant_id=tenant_id):
            raise ResumeRefused(f'checkpoint {checkpoint_id} was decided concurrently', 'resume',
                                code='ALREADY_DECIDED')
        state = self._rehydrate_state(checkpoint, decision, ctx)
        state['checkpoint_id'] = checkpoint_id
        if not approved:
            state.update(status='REJECTED', message_log=[], failed_keys=[])
            return state
        if checkpoint.get('step') is None:          # paused by a v1.1 workflow: run it again, approved
            return self.execute(state, config, ctx=ctx, **kwargs)
        return self.__execute_steps(state, config, kwargs, ctx=ctx, start_at=checkpoint['step'],
                                    fingerprint=checkpoint.get('fingerprint'))

    def _authorize_decision(self, checkpoint: Dict[str, Any], decision: Dict[str, Any], ctx) -> bool:
        """Protected hook (v1.2): may this decider decide? Default: four eyes, a named
        decider who is not the requester. Override for roles, limits, delegation."""
        decider = decision.get('decided_by')
        return bool(decider) and decider != checkpoint.get('requested_by')

    def _rehydrate_state(self, checkpoint: Dict[str, Any], decision: Dict[str, Any], ctx) -> Dict[str, Any]:
        """Protected hook (v1.2): the state the paused step continues with. Default: the
        stored state plus the decision (approval_granted lets the approval stage pass)."""
        return dict(checkpoint['state'], approval_granted=bool(decision.get('approved')),
                    approved_by=decision.get('decided_by'))

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
                    'status', 'response', 'outcome', 'cumulative_tokens_used', 'trace_context',
                    # v1.2: workflow bookkeeping and the approval decision are not request content
                    'steps', 'evaluation', 'refusal', 'checkpoint_id', 'approval_granted', 'approved_by'}
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
    def __hook(self, name, step, legacy_args, legacy_kwargs, **available):
        fn = getattr(self, name)
        if declares_ctx(fn):
            return call_hook(fn, step, available)
        if getattr(type(self), name) is not getattr(BaseWorkflow, name, None):
            # v1.3: a declared-steps workflow (its _build_workflow declares ctx) uses v1.2 hooks only
            raise TypeError(f"{type(self).__qualname__}.{name} uses the v1.1 keyword signature, removed in E2A v1.3 "
                            f"for a workflow of declared steps: declare the v1.2 parameters ('state, ctx, ...')")
        return fn(*legacy_args, **legacy_kwargs)

    def __checkpoint(self, snapshot, state, ctx, step_name, interrupt, config, fingerprint=None):
        store = config.get('checkpoint_store')
        if not isinstance(store, BaseCheckpointStore):
            return
        checkpoint_id = str(uuid.uuid4())
        store.save({'checkpoint_id': checkpoint_id, 'tenant_id': ctx.tenant_id, 'status': 'PENDING',
                    'step': step_name, 'state': snapshot, 'idempotency_key': ctx.idempotency_key,
                    'correlation_id': ctx.correlation_id, 'fingerprint': fingerprint,
                    'requested_by': state.get('requested_by', state.get('user_id')),
                    'reason': getattr(interrupt, 'reason', str(interrupt)), 'created_at': time.time()},
                   config, tenant_id=ctx.tenant_id)
        state['checkpoint_id'] = checkpoint_id

    @staticmethod
    def __run_agent(agent, state, config, ctx, io_config, message_log, failed_keys, kwargs):
        if isinstance(agent, BaseAgent):
            return agent.run(state, config, ctx=ctx, io_config=io_config,
                             message_log=message_log, failed_keys=failed_keys, **kwargs)
        return agent.run(state, config, correlation_id=ctx.correlation_id, io_config=io_config,
                         idempotency_key=ctx.idempotency_key, tenant_id=ctx.tenant_id,
                         message_log=message_log, failed_keys=failed_keys, deadline=ctx.deadline, **kwargs)

    def __execute_steps(self, state, config, kwargs, ctx=None, start_at=None, fingerprint=None):
        """The v1.2 engine: declared steps under one context, deadline, log and claim."""
        given = ctx or kwargs.pop('ctx', None)
        tenant_id = given.tenant_id if given else state.get('tenant_id')
        if not tenant_id:
            raise ValueError('state["tenant_id"] is required')
        correlation_id = (given.correlation_id if given else None) or kwargs.get('correlation_id') \
            or state.get('correlation_id') or str(uuid.uuid4())
        client_key = (given.idempotency_key if given else None) or kwargs.get('idempotency_key') \
            or state.get('idempotency_key')
        deadline = given.deadline if given and given.deadline is not None else self.__resolve_deadline(config, kwargs)
        for name in ('correlation_id', 'idempotency_key', 'io_config', 'tenant_id', 'message_log',
                     'failed_keys', 'deadline'):
            kwargs.pop(name, None)
        ctx = RequestContext.begin(tenant_id=tenant_id, config=config, correlation_id=correlation_id,
                                   idempotency_key=client_key, deadline=deadline, overrides=kwargs,
                                   trace=given.trace if given else None)
        io_config = self._resolve_io_config(config, **kwargs)
        message_log: List[dict] = []
        failed_keys: List[str] = []
        messages = MessageLog(message_log, correlation_id, type(self).__name__)
        state['tenant_id'], state['correlation_id'] = tenant_id, correlation_id
        if client_key:
            state['idempotency_key'] = client_key
        store = config.get('idempotency_store')
        store = store if isinstance(store, BaseIdempotencyStore) else None
        run = {'ctx': ctx, 'decided': False, 'owned': False, 'short': None, 'fingerprint': fingerprint}
        legacy = dict(correlation_id=correlation_id, io_config=io_config, tenant_id=tenant_id,
                      message_log=message_log, **kwargs)
        dehydrated, last_error = False, None
        try:
            workflow = call_hook(self._build_workflow, 'build_workflow', dict(ctx=ctx, config=config, state=state))
            if not self.__hook('_validate_workflow', 'validate_workflow', (workflow, config), legacy,
                               workflow=workflow, ctx=ctx, config=config):
                raise ValueError('Workflow validation failed')
            if not isinstance(workflow, Steps):
                workflow = Steps(_Resolved('agent', lambda s: self.__hook(
                    '_get_agent', 'get_agent', (s.get('intent'), config), legacy,
                    intent=s.get('intent'), state=s, ctx=run['ctx'], config=config)))
            records = state.setdefault('steps', [])
            for i in range(workflow.index(start_at) if start_at else 0, len(workflow.steps)):
                step = workflow.steps[i]
                if not run['decided'] and i >= workflow.claim_at:
                    self.__claim(state, config, run, store, legacy)
                    if run['short'] is not None:
                        break
                if step.when is not None and not step.when(state):
                    records.append({'step': step.name, 'status': 'SKIPPED'})
                    continue
                agent = step.agent_for(state)
                started = time.time()
                state.pop('evaluation', None)
                try:
                    state = self.__run_agent(agent, state, config, run['ctx'], io_config,
                                             message_log, failed_keys, kwargs)
                except DehydrationInterrupt as d:
                    # the checkpoint holds the state at the pause: after the step's _prepare
                    # (reads only), so the approver sees the facts the decision is about; on
                    # resume the step runs again from its start
                    records.append({'step': step.name, 'agent': type(agent).__name__, 'status': 'PAUSED'})
                    self.__checkpoint(_snapshot(state), state, run['ctx'], step.name, d, config, run['fingerprint'])
                    raise
                status = ('REFUSED' if state.get('refusal') else
                          'FALLBACK' if state.get('evaluation') == 'FALLBACK' else 'DONE')
                stopped = status != 'DONE' or bool(step.stop_if and step.stop_if(state))
                records.append({'step': step.name, 'agent': type(agent).__name__, 'status': status,
                                'stopped': stopped, 'latency_ms': round((time.time() - started) * 1000, 1)})
                if stopped:
                    break
            if run['short'] is not None:
                self.__apply_short_circuit(state, run['short'], messages)
        except DehydrationInterrupt as d:
            messages.info('workflow_dehydrated', reason=str(d))
            state['status'] = 'AWAITING_WEBHOOK'
            dehydrated = True
        except Exception as e:
            last_error = e
            failed_keys.append(run['ctx'].idempotency_key or correlation_id)
            self.__hook('_handle_error', 'handle_error', (e, state, config),
                        dict(legacy, failed_keys=failed_keys), error=e, state=state, ctx=run['ctx'],
                        messages=messages, failed_keys=failed_keys)
        finally:
            obs_engine = config.get('observability_engine')
            if obs_engine and isinstance(obs_engine, BaseObservability):
                obs_engine.record_telemetry(message_log, correlation_id, config, tenant_id=tenant_id)
            else:
                self._flush_logs(message_log, config)
            if run['owned']:
                self.__settle_claim(store, run['ctx'].idempotency_key, tenant_id, correlation_id, state,
                                    failed_keys, dehydrated, last_error, config)
            if failed_keys:
                self._send_to_dlq(failed_keys, config)
            state['message_log'] = message_log
            state['failed_keys'] = failed_keys
        return state

    def __claim(self, state, config, run, store, legacy):
        """At the first material step: resolve the key (client's, else the domain's) and,
        for a material request, take the claim for the whole remaining workflow."""
        run['decided'] = True
        ctx = run['ctx']
        key = ctx.idempotency_key or self.__hook('_generate_idempotency_key', 'generate_idempotency_key',
                                                 (state, config), legacy, state=state, ctx=ctx, config=config)
        if key != ctx.idempotency_key:
            run['ctx'] = ctx = ctx.child(idempotency_key=key)
        state['idempotency_key'] = key
        if store is None or not self.__hook('_requires_idempotency_claim', 'requires_idempotency_claim',
                                            (state, config), legacy, state=state, ctx=ctx, config=config):
            return
        run['fingerprint'] = run['fingerprint'] or self.__hook(
            '_request_fingerprint', 'request_fingerprint', (state, config), legacy, state=state, ctx=ctx, config=config)
        claim = store.claim(key, config, tenant_id=ctx.tenant_id, owner=ctx.correlation_id,
                            fingerprint=run['fingerprint'],
                            lease_seconds=int(ctx.setting('idempotency_lease_seconds')),
                            ttl_seconds=int(ctx.setting('idempotency_ttl_seconds')))
        if claim.status == ClaimStatus.CLAIMED:
            run['owned'] = True
        else:
            run['short'] = claim

    @staticmethod
    def __apply_short_circuit(state, claim, messages):
        st = {ClaimStatus.REPLAY: 'REPLAYED', ClaimStatus.IN_PROGRESS: 'IN_PROGRESS',
              ClaimStatus.CONFLICT: 'CONFLICT', ClaimStatus.RECOVERY_REQUIRED: 'RECOVERY_REQUIRED',
              ClaimStatus.FAILED_FINAL: 'FAILED_FINAL'}[claim.status]
        state['status'], state['idempotency_status'] = st, claim.status.value
        if claim.status == ClaimStatus.REPLAY and claim.result:
            state.update({k: v for k, v in claim.result.items() if k in ('response', 'outcome', 'cumulative_tokens_used')})
            state['replayed'] = True
        messages.info('idempotent_short_circuit', claim_status=claim.status.value)

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
