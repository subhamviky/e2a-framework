"""E2A: BaseGovernanceFramework.

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

from .core import DehydrationInterrupt, NFRViolationError
from .config import resolve_config
 
 
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
        self.__enforce_token_budget(state, io_config, config)
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
 
    def __enforce_token_budget(self, state: Dict[str, Any], io_config: Dict[str, Any],
                               config: Dict[str, Any] = None) -> None:
        # Base-owned. Stops the request before another LLM call is made,
        # not after. v1.1.2: io_config only carries io_-prefixed keys, so the
        # budget is resolved from io_config (max_token_budget or
        # io_max_token_budget), then from config, then the registry default.
        io_config = io_config or {}
        max_budget = io_config.get('max_token_budget', io_config.get('io_max_token_budget'))
        if max_budget is None:
            max_budget = resolve_config('max_token_budget', config)
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
