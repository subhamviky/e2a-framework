"""E2A: BaseValidationService and ValidationResult.

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

from .config import resolve_config
from ._propagation import _PropagationMixin
 
 
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
 
    def _result(self, valid, errors, agent_name, correlation_id, **extra) -> 'ValidationResult':
        """Protected (v1.1.2): lets a subclass, typically _enforce_entitlement_gate(),
        build a result in the framework's shape, with extra fields such as forbidden=True."""
        return ValidationResult(valid=valid, errors=errors, agent_name=agent_name,
                                correlation_id=correlation_id, **extra)

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
