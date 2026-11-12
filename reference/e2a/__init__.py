"""E2A: Enterprise-to-Agentic runtime contracts.

Modules: core, config, resilience, observability, governance, validation, llm, agents, workflows, rag, tools, ports, pipeline, infra.
"""
from .core import DehydrationInterrupt, NFRViolationError, ConcurrencyConflict, AdmissionTimeout, DeadlineExceeded, CircuitOpenError, BulkheadFullError, PhysicalOutcome, outcome_of, Deadline, ClaimStatus, ClaimResult, CommitResult, LeaseHandle, E2A_METRICS
from .config import ConfigKey, E2A_CONFIG, register_config_key, resolve_config, unknown_config_keys, config_reference_markdown, env_example
from .resilience import run_sync, RetryBudget, CircuitBreaker, Bulkhead, ResiliencePolicy, validate_capacity
from .observability import BaseObservability
from .governance import BaseGovernanceFramework
from .validation import ValidationResult, BaseValidationService, OrderOpsValidationService
from .llm import DLP_MARKER, BaseLLMAgentService
from .agents import BaseAgent, LLMOnlyAgent
from .workflows import BaseWorkflow
from .rag import BaseRAGPipeline, BaseIndexingPipeline
from .tools import BaseToolService, RestToolService, MCPToolService, BaseMCPServer
from .ports import BasePromptRegistry, BaseTransactionalStore, BaseCacheStore, BaseIdempotencyStore, BaseDistributedLock, BaseWebhookDispatcher, BaseSchemaTransformer, BaseMemoryManager, BaseOutboxSweeper, BaseReconciliationService
from .pipeline import BasePipeline
from .infra import BaseInfraProvisioner, GCPInfraProvisioner

__version__ = "1.2.0"
