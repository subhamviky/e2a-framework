"""E2A: Enterprise-to-Agentic runtime contracts.

Modules: core, config, resilience, observability, governance, validation, llm, agents, workflows, rag, tools, ports, pipeline, infra; context, stages, conformance (v1.2); release (v1.2.2: ProjectReleaseGate, import it from e2a.release); port_contract (v1.4.0: the behavioural suite every port adapter passes, import it from e2a.port_contract); llm_types, capabilities, bindings (v1.6.0: canonical model types, capabilities, profiles and bindings); delegated, schema_check (v1.6.1: the delegated-runtime base, the structured-output check); messaging, identity (v1.7.0: exactly-once effects from at-least-once brokers, identity at ingress).
"""
from .core import ErrorClass, classify_error, ModelRefusal, DehydrationInterrupt, NFRViolationError, ConcurrencyConflict, AdmissionTimeout, DeadlineExceeded, CircuitOpenError, BulkheadFullError, PhysicalOutcome, outcome_of, Deadline, ClaimStatus, ClaimResult, CommitResult, LeaseHandle, E2A_METRICS
from .config import ConfigKey, E2A_CONFIG, register_config_key, resolve_config, unknown_config_keys, scoped_config, config_reference_markdown, env_example
from .resilience import run_sync, RetryBudget, CircuitBreaker, Bulkhead, ResiliencePolicy, validate_capacity
from .observability import BaseObservability
from .governance import BaseGovernanceFramework
from .validation import ValidationResult, BaseValidationService, OrderOpsValidationService
from .llm import DLP_MARKER, REASONING_EFFORTS, BaseLLMAgentService, StructuredOutputInvalid, StructuredOutputUnsupported
from .agents import BaseAgent, LLMOnlyAgent
from .workflows import BaseWorkflow, ResumeRefused, Route, Step, Steps
from .rag import BaseRAGPipeline, BaseIndexingPipeline, BaseEmbeddingModel, BaseVectorIndex, BaseReranker, RAGPipeline, check_dimensions
from .tools import BaseToolService, RestToolService, MCPToolService, BaseMCPServer
from .ports import BaseCredentialProvider, TokenCredentialProvider, BasePromptRegistry, BaseTransactionalStore, BaseCacheStore, BaseIdempotencyStore, BaseCheckpointStore, BaseDistributedLock, BaseWebhookDispatcher, BaseSchemaTransformer, BaseMemoryManager, BaseOutboxSweeper, BaseReconciliationService
from .pipeline import BasePipeline
from .llm_types import Message, ModelResult, StopReason, ToolCall, ToolDeclaration, Usage
from .capabilities import CapabilityError, capabilities_of, register_capability
from .bindings import Bindings, Profile, bindings_from_profile, register_provider_package
from .delegated import DelegatedRuntime, DelegatedTurnLimit, MaterialToolRefused
from .schema_check import schema_errors
from .messaging import (BaseMessageBroker, BaseOutboxStore, BrokerMessage, BrokerOutboxSweeper, IdempotentConsumer,
                        InMemoryBroker, InMemoryOutboxStore)
from .identity import BaseIdentityResolver, ClaimMapIdentityResolver, Identity, IdentityRefused
from .infra import BaseInfraProvisioner, GCPInfraProvisioner
from .context import FailedKeys, MessageLog, ModifyAccess, ReadAccess, RequestContext, ToolDenied
from .stages import (ApprovalRequired, ApprovalStage, CapacityRefusal, CapacityStage, EgressStage,
                     FirewallRefusal, FirewallStage, GovernanceRefusal, GuardrailVerdict, IngressStage,
                     InflightLimiter, ManagedGuardrailEgressStage, ManagedGuardrailStage,
                     RedactionStage, SandboxRefusal, SandboxStage, TokenBudgetRefusal, TokenBudgetStage,
                     ToolRefusal, compile_redactions, redact_text)

__version__ = "1.7.0"
