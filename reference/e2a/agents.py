"""E2A: BaseAgent and LLMOnlyAgent.

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

from .core import DehydrationInterrupt, PhysicalOutcome
from .context import FailedKeys, MessageLog, ModifyAccess, ReadAccess, RequestContext
from ._contract import call_hook, declares_ctx, warn_legacy
from .stages import GovernanceRefusal
from .config import resolve_config
from ._propagation import _PropagationMixin
from .governance import BaseGovernanceFramework
# ==================================================
# BaseAgent
# ==================================================
 
class BaseAgent(ABC, _PropagationMixin):
 
    def run(self, state: Dict[str, Any], config: Dict[str, Any] = None,
            correlation_id: str = None, io_config: Dict[str, Any] = None,
            idempotency_key: str = None, tenant_id: str = None,
            message_log: List[dict] = None, failed_keys: List[str] = None,
            ctx: 'RequestContext' = None, **kwargs) -> Dict[str, Any]:
        """Public entry point. Receives the request context from BaseWorkflow, or
        creates it when run() is called on its own (e.g. in a unit test). Assumes `state`
        already passed BaseValidationService.validate().

        v1.2 (BOPF-style contract): a hook that declares `ctx` receives exactly the
        parameters it declares, from those its step offers:
          _prepare(state, ctx, io_read, messages)                  read facts; no side effects
          _apply_policy(state, ctx, io_read, messages, failed_keys) the agent's own authorization
          _build_messages(state, ctx)                               None = no model step
          _evaluate_output(response, state, ctx, messages)          confidence
          _fallback(state, ctx, messages)                           response below min_confidence
          _act(state, ctx, io_modify, messages, failed_keys)        material effects -> outcomes
          _confirm(state, ctx, io_read, outcomes, messages)         physical truth -> outcome
          _handle_error(error, state, ctx, messages, failed_keys)
        Order for a v1.2 agent: prepare -> governance gate -> _apply_policy (both) ->
        build -> infer -> evaluate/fallback -> act (only after an accepted evaluation, and
        not after the deadline) -> confirm -> redaction. A v1.1 agent (hooks without
        `ctx`) runs exactly as in v1.1."""
        config = config or {}
        given = {'tenant_id': tenant_id, 'correlation_id': correlation_id, 'idempotency_key': idempotency_key}
        if ctx is None:
            ctx = RequestContext.begin(
                tenant_id=tenant_id or state.get('tenant_id'), config=config, require_tenant=False,
                correlation_id=correlation_id or state.get('correlation_id'),
                idempotency_key=idempotency_key or state.get('idempotency_key'),
                deadline=kwargs.get('deadline'), overrides=kwargs)
        else:
            clash = [k for k, v in given.items() if v is not None and v != getattr(ctx, k)]
            if clash:
                raise ValueError(f'run(): {", ".join(clash)} given both in ctx and as a keyword, with different values')
        if ctx.deadline is not None:
            kwargs.setdefault('deadline', ctx.deadline)
        messages = MessageLog(message_log, ctx.correlation_id, type(self).__name__)
        failed = FailedKeys(failed_keys)
        io_config = io_config or self._resolve_io_config(config, **kwargs)
        common = dict(correlation_id=ctx.correlation_id, io_config=io_config,
                      tenant_id=ctx.tenant_id, message_log=messages.entries)
        # io_config travels as the gate's own argument; passing it again inside the
        # keywords raised "multiple values for argument 'io_config'" (a defect since v1.1)
        gate_kw = {k: v for k, v in common.items() if k != 'io_config'}
        gov_engine = config.get('governance_engine')
        gov_engine = gov_engine if isinstance(gov_engine, BaseGovernanceFramework) else None
        allowed = getattr(self, 'allowed_tools', None)
        name = getattr(self, 'agent_name', type(self).__name__)
        if allowed is None and gov_engine is not None and name in (gov_engine.tool_policy or {}):
            allowed = gov_engine.tool_policy[name]          # v1.2: the engine's per-agent tool policy
        io_read = ReadAccess(ctx, config, allowed=allowed, messages=messages)
        io_modify = ModifyAccess(ctx, config, allowed=allowed, messages=messages)
        v2 = declares_ctx(self._build_messages) or (
            type(self)._apply_policy is not BaseAgent._apply_policy and declares_ctx(self._apply_policy))

        def hook(name, step, legacy_args, legacy_kwargs, **available):
            fn = getattr(self, name)
            if declares_ctx(fn):
                return call_hook(fn, step, available)
            warn_legacy(type(self), name)
            return fn(*legacy_args, **legacy_kwargs)

        start = time.time()
        try:
            if v2:
                # screening stages (capacity, budget, sandbox, firewall) before anything reads the
                # request; deciding stages (approval) after _prepare has read the facts they need
                if gov_engine:
                    state = gov_engine.enforce_governance_gate(state, io_config, config, ctx=ctx, messages=messages,
                                                               phase='screen', **gate_kw, **kwargs)
                call_hook(self._prepare, 'prepare', dict(state=state, ctx=ctx, io_read=io_read, messages=messages))
                if gov_engine:
                    state = gov_engine.enforce_governance_gate(state, io_config, config, ctx=ctx, messages=messages,
                                                               phase='decide', **gate_kw, **kwargs)
                hook('_apply_policy', 'apply_policy', (state, config), dict(common, **kwargs),
                     state=state, ctx=ctx, io_read=io_read, messages=messages, failed_keys=failed)
                prompt = hook('_build_messages', 'build_messages', (state, config), dict(common, **kwargs),
                              state=state, ctx=ctx)
            else:   # the v1.1 order, unchanged
                prompt = self._build_messages(state, config, **common, **kwargs)
                if gov_engine:
                    state = gov_engine.enforce_governance_gate(state, io_config, config, **gate_kw, **kwargs)
                else:
                    hook('_apply_policy', 'apply_policy', (state, config), dict(common, **kwargs))
            accepted, response = True, None
            if prompt is not None or not v2:
                response = self._infer(prompt, config, **common, **kwargs)
                tokens_used = self.__track_usage(prompt, response, config, **kwargs)
                state['cumulative_tokens_used'] = state.get('cumulative_tokens_used', 0) + tokens_used
                confidence = hook('_evaluate_output', 'evaluate_output', (response, state, config),
                                  dict(common, **kwargs), response=response, state=state, ctx=ctx, messages=messages)
                if confidence < resolve_config('min_confidence', config, kwargs):
                    accepted = False
                    response = hook('_fallback', 'fallback', (state, config), dict(common, **kwargs),
                                    state=state, ctx=ctx, messages=messages)
                if v2:      # a workflow step ends the workflow on a fallback (note 05)
                    state['evaluation'] = 'ACCEPTED' if accepted else 'FALLBACK'
            if accepted:
                if ctx.expired():
                    outcomes = []
                    messages.warn('act_skipped_deadline')
                    if declares_ctx(self._act) and type(self)._act is not BaseAgent._act:
                        state['outcome'] = PhysicalOutcome.NOT_COMMITTED.value
                else:
                    outcomes = call_hook(self._act, 'act', dict(state=state, ctx=ctx, io_modify=io_modify,
                                                                messages=messages, failed_keys=failed)) or []
                outcome = call_hook(self._confirm, 'confirm', dict(state=state, ctx=ctx, io_read=io_read,
                                                                   outcomes=list(outcomes), messages=messages))
                if outcome is not None:
                    state['outcome'] = PhysicalOutcome(outcome).value
            if v2 and prompt is None:   # a deterministic agent: its steps may set state['response']
                response = state.get('response')
            if gov_engine:
                extra = dict(ctx=ctx, messages=messages) if v2 else {}
                response = gov_engine.redact_response(state, response, config, **extra, **common, **kwargs)
            state['response'] = response
        except DehydrationInterrupt:
            raise  # re-raised unchanged; BaseWorkflow.execute() catches it
        except Exception as e:
            if isinstance(e, GovernanceRefusal):        # v1.2: typed, so callers can tell a refusal from a bug
                state['refusal'] = {'code': e.code, 'stage': e.stage, 'reason': e.reason, 'retryable': e.retryable}
                messages.warn('governance_refusal', code=e.code, stage=e.stage)
            failed.add(ctx.idempotency_key or ctx.correlation_id)
            hook('_handle_error', 'handle_error', (e, state, config),
                 dict(correlation_id=ctx.correlation_id, message_log=messages.entries,
                      failed_keys=failed.keys, **kwargs),
                 error=e, state=state, ctx=ctx, messages=messages, failed_keys=failed)
        latency = time.time() - start
        max_latency = resolve_config('max_latency', config, kwargs)
        self._log(messages.entries, ctx.correlation_id, 'INFO', 'agent_run_complete',
                   latency=round(latency, 3), tokens_used=state.get('cumulative_tokens_used', 0))
        state['tenant_id'] = ctx.tenant_id            # mirrored for v1.1 code; removed in v1.3
        state['correlation_id'] = ctx.correlation_id
        if latency > max_latency:
            # v1.1.2: recorded, not raised. The run has finished and may have committed
            # a material effect; raising now would report a committed outcome as a
            # failure. Enforcement before effects is the job of the request deadline.
            state['slo_breached'] = {'latency': round(latency, 3), 'max_latency': max_latency}
            self._log(messages.entries, ctx.correlation_id, 'WARN', 'agent_latency_slo_breached',
                      latency=round(latency, 3), max_latency=max_latency)
        return state

    # ---- v1.2 steps with defaults (a v1.1 agent never notices them)
    def _prepare(self, state, ctx, io_read, messages):
        """Read facts or retrieve before the prompt is built. No side effects."""
        return None

    def _act(self, state, ctx, io_modify, messages, failed_keys):
        """Execute material effects through io_modify; return the tools' typed results."""
        return []

    def _confirm(self, state, ctx, io_read, outcomes, messages):
        """Confirm physical truth. Default: the tools' typed outcomes decide; UNKNOWN wins
        over NOT_COMMITTED, which wins over COMMITTED; no outcomes, no verdict."""
        if not outcomes:
            return None
        kinds = {PhysicalOutcome(o.get('outcome') if isinstance(o, dict) else o) for o in outcomes}
        for kind in (PhysicalOutcome.UNKNOWN, PhysicalOutcome.NOT_COMMITTED):
            if kind in kinds:
                return kind
        return PhysicalOutcome.COMMITTED

    @abstractmethod
    def _build_messages(self, state, config=None, **kwargs):
        """kwargs: prompt_template, role"""
 
    def _apply_policy(self, state=None, ctx=None, **kwargs):
        """Default (v1.2): no agent-specific policy; the governance stages still run.
        v1.1 kwargs: retry_policy, circuit_breaker_config. Fallback path,
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
 
    def _evaluate_output(self, response=None, state=None, ctx=None, **kwargs):
        """Return float [0.0-1.0]. kwargs: min_confidence. Required only for an agent
        with a model step (v1.2: a deterministic agent's _build_messages returns None)."""
        raise NotImplementedError(f'{type(self).__name__} has a model step: implement _evaluate_output')

    def _fallback(self, state=None, ctx=None, **kwargs):
        """kwargs: fallback_model, cache_key. Required only for an agent with a model step."""
        raise NotImplementedError(f'{type(self).__name__} has a model step: implement _fallback')

    def _handle_error(self, error=None, state=None, ctx=None, messages=None, failed_keys=None, **kwargs):
        """Default (v1.2): record the error in state['errors'] and in messages; the run
        does not raise. v1.1: appends to failed_keys via kwargs['failed_keys']."""
        if state is not None:
            state.setdefault('errors', []).append(str(error))
        if isinstance(messages, MessageLog):
            messages.error('agent_error', error=str(error), error_type=type(error).__name__)
 
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
