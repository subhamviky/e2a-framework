"""E2A: Enterprise-to-Agentic runtime contracts.

Modules: core, config, resilience, observability, governance, validation, llm, agents, workflows, rag, tools, ports, pipeline, infra; context, stages, conformance (v1.2); release (v1.2.2: ProjectReleaseGate, import it from e2a.release).
"""
from .core import DehydrationInterrupt, NFRViolationError, ConcurrencyConflict, AdmissionTimeout, DeadlineExceeded, CircuitOpenError, BulkheadFullError, PhysicalOutcome, outcome_of, Deadline, ClaimStatus, ClaimResult, CommitResult, LeaseHandle, E2A_METRICS
from .config import ConfigKey, E2A_CONFIG, register_config_key, resolve_config, unknown_config_keys, config_reference_markdown, env_example
from .resilience import run_sync, RetryBudget, CircuitBreaker, Bulkhead, ResiliencePolicy, validate_capacity
from .observability import BaseObservability
from .governance import BaseGovernanceFramework
from .validation import ValidationResult, BaseValidationService, OrderOpsValidationService
from .llm import DLP_MARKER, BaseLLMAgentService
from .agents import BaseAgent, LLMOnlyAgent
from .workflows import BaseWorkflow, ResumeRefused, Route, Step, Steps
from .rag import BaseRAGPipeline, BaseIndexingPipeline, BaseEmbeddingModel, BaseVectorIndex, RAGPipeline
from .tools import BaseToolService, RestToolService, MCPToolService, BaseMCPServer
from .ports import BasePromptRegistry, BaseTransactionalStore, BaseCacheStore, BaseIdempotencyStore, BaseCheckpointStore, BaseDistributedLock, BaseWebhookDispatcher, BaseSchemaTransformer, BaseMemoryManager, BaseOutboxSweeper, BaseReconciliationService
from .pipeline import BasePipeline
from .infra import BaseInfraProvisioner, GCPInfraProvisioner
from .context import FailedKeys, MessageLog, ModifyAccess, ReadAccess, RequestContext, ToolDenied
from .stages import (ApprovalRequired, ApprovalStage, CapacityRefusal, CapacityStage, EgressStage,
                     FirewallRefusal, FirewallStage, GovernanceRefusal, IngressStage, InflightLimiter,
                     RedactionStage, SandboxRefusal, SandboxStage, TokenBudgetRefusal, TokenBudgetStage,
                     ToolRefusal, compile_redactions, redact_text)

__version__ = "1.3.0"
