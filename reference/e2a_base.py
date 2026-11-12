"""e2a_base: compatibility module (v1.2.0).

The framework now lives in the e2a package (e2a.agents, e2a.workflows, e2a.rag, ...).
Every name that e2a_base defined in v1.1.x is re-exported here, so `from e2a_base import BaseAgent`
keeps working.
"""
# Standard-library names kept: `from e2a_base import *` exported them in v1.1.x.
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

logging.basicConfig(level=logging.INFO)

from e2a.core import DehydrationInterrupt, NFRViolationError, ConcurrencyConflict, AdmissionTimeout, DeadlineExceeded, CircuitOpenError, BulkheadFullError, _DETERMINISTIC_ERRORS, PhysicalOutcome, outcome_of, Deadline, ClaimStatus, ClaimResult, CommitResult, LeaseHandle, E2A_METRICS  # noqa: F401
from e2a.config import _to_bool, _to_json_map, _to_json_list, ConfigKey, _k, E2A_CONFIG, register_config_key, resolve_config, unknown_config_keys, config_reference_markdown, env_example  # noqa: F401
from e2a.resilience import _BRIDGE, _BRIDGE_LOCK, run_sync, _run_on_bridge_loop, RetryBudget, CircuitBreaker, Bulkhead, ResiliencePolicy, validate_capacity  # noqa: F401
from e2a._propagation import _PropagationMixin  # noqa: F401
from e2a.observability import BaseObservability  # noqa: F401
from e2a.governance import BaseGovernanceFramework  # noqa: F401
from e2a.validation import ValidationResult, BaseValidationService, OrderOpsValidationService  # noqa: F401
from e2a.llm import DLP_MARKER, _luhn_valid, _TenantPermit, BaseLLMAgentService  # noqa: F401
from e2a.agents import BaseAgent, LLMOnlyAgent  # noqa: F401
from e2a.workflows import BaseWorkflow  # noqa: F401
from e2a.rag import BaseRAGPipeline, BaseIndexingPipeline  # noqa: F401
from e2a.tools import BaseToolService, RestToolService, MCPToolService, BaseMCPServer, _resolve_tool_call, _get_tool_service, _TOOL_SERVICE_CACHE, _TOOL_SERVICE_LOCK  # noqa: F401
from e2a.ports import BasePromptRegistry, BaseTransactionalStore, BaseCacheStore, _SINGLE_FLIGHT_INIT_LOCK, _FlightAborted, _Flight, BaseIdempotencyStore, BaseDistributedLock, BaseWebhookDispatcher, BaseSchemaTransformer, BaseMemoryManager, BaseOutboxSweeper, BaseReconciliationService  # noqa: F401
from e2a.pipeline import BasePipeline  # noqa: F401
from e2a.infra import BaseInfraProvisioner, GCPInfraProvisioner  # noqa: F401

