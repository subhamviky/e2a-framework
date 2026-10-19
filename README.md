# E2A: Enterprise-to-Agentic Architecture Framework

> **A Cross-Cloud Architectural Meta-Standard Translating Enterprise Distributed Systems Invariants into Production-Grade Agentic AI Runtimes**

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Java: 21 LTS](https://img.shields.io/badge/Java-21%20LTS-orange.svg)](https://openjdk.org/projects/jdk/21/)
[![Protocol: Model Context Protocol](https://img.shields.io/badge/Protocol-MCP%20JSON--RPC%202.0-purple.svg)](https://modelcontextprotocol.io/)
[![Cloud: AWS | GCP | Azure](https://img.shields.io/badge/Cloud-AWS%20%7C%20GCP%20%7C%20Azure-yellowgreen.svg)](#)
[![Article: Context, Harness & Evals](https://img.shields.io/badge/Article-Context%2C%20Harness%20%26%20Evals-0A66C2.svg)](https://www.linkedin.com/pulse/context-harness-evals-what-ai-assisted-sdlc-can-borrow-subham-gupta-fplfc/)

---

## 🏛️ Executive Summary & The Enterprise Bridge

Modern enterprise adoption of frontier artificial intelligence introduces powerful capabilities: autonomous tool orchestration, prefix prompt caching, agent memory, and reflection loops. However, when deploying these capabilities to interact with systems of record (financial ledgers, core ERPs, and regulated databases), relying solely on prompt engineering or unconstrained agent frameworks introduces severe operational risk: non-deterministic retries, duplicate financial writes, and unverified state mutations.

The **E2A Framework** acts as an **Enterprise Bridge** connecting frontier AI models directly to mission-critical ledgers:

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. PLUGGABLE COGNITIVE LAYER (Claude, GPT-4o, Gemini, Open-Weights)     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Cognitive Intent & Tool Call Payloads
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. ENTERPRISE CONTROL PLANE & GOVERNANCE BRIDGE (E2A / A2C / P0 / G2C) │
│    • AST Workspace Scaffolding (P0)    • Typed Contract Synthesis (G2C)│
│    • Pre-Commit Eval Gates (A2C)       • Transactional Outbox & Saga   │
│    • Single Public Entry Orchestrator  • Distributed Idempotency Locks │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Deterministic, Idempotent Operations
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. HYPERSCALER RUNTIMES & SYSTEMS OF RECORD (AWS, GCP, Azure, SAP Core)│
└────────────────────────────────────────────────────────────────────────┘
```

Rather than replacing cloud-native platforms, E2A encloses model tool executions within proven cloud-native distributed systems patterns: **Hexagonal Architecture (Ports and Adapters)**, **the Template Method Pattern**, **Saga Orchestration**, and **Transactional Outbox CDC**.

---

## 🎯 Multi-Stakeholder Value Realization

| Stakeholder Lens | Strategic Enterprise Priority | How E2A Realizes Business Value |
| :--- | :--- | :--- |
| **VP of Engineering & Chief Architect** | Architectural Agility & Maintainability | Decouples business logic from proprietary model SDKs. Core services run identically across AWS Bedrock, Google Cloud Vertex AI, Azure AI Foundry, or open-weight models without modifying domain code. |
| **CISO & Enterprise Risk Officer (CRO)** | Privilege Defense & Audit Verification | Guarantees non-amplifiable credentials per transaction. Writes state mutations, tool payloads, and reasoning traces atomically to a local outbox before event publication. |
| **FinOps Director & Cloud Economics** | Cost Predictability & Token Control | Pairs with native prompt caching checkpoints to reduce recurring input token costs by up to $90\%$. Dynamic model routing directs tasks to the most cost-effective tier. |
| **SRE & Operations Leadership** | Ledger Integrity & High Availability | Eliminates duplicate transactions during network timeouts via `HMAC-SHA256` distributed locks. Coordinates multi-step operations with automated Saga compensating rollbacks. |
| **Product Leadership & Delivery Leads** | Time-to-Market & Customer Trust | Converts fragile experimental agent scripts into enterprise-ready production microservices in days by injecting harvested metadata into pre-built governance hooks. |

---

## 💡 Value at a Glance

**The problem.** Frontier models are probabilistic; enterprise systems of record are deterministic. E2A is the governed bridge between them — it never lets an agent's narrative claim count as a completed transaction.

| If you need… | E2A gives you |
| :--- | :--- |
| **Agents that can write to a system of record safely** | Single public entry point per class, validated and idempotent writes, OCC commit gate, saga compensation, typed `physical_success` confirmation |
| **To keep the core untouched** | A sidecar pattern: existing APIs wrapped as MCP/tool services, agents reason outside the core |
| **No vendor or cloud lock-in** | Model, storage, lock, cache and queue are ports; switching is a binding plus a config value |
| **One standard for four project types** | Agentic apps, deterministic CQRS services, hybrids of the two, and AI-assisted SDLC — same classes and propagation contract |
| **Predictable operations and cost** | One inference gateway tier with RPM/TPM quota, pod concurrency and context budgets; one central configuration registry |
| **A path from prototype to production** | Harvest a playground prototype's schemas, generate governed classes and landing-zone code, gate merges in CI |
| **Start small, scale later** | Run every class in one process on day one; extract any class into its own service later without changing business logic |

➡️ **Read the two-page summary:** [docs/E2A_STRATEGIC_OVERVIEW.md](docs/E2A_STRATEGIC_OVERVIEW.md) — capabilities, the four archetypes, use cases (implemented vs directional), and role-by-role value, each linked to the document that explains *how*.

---

## 🏛️ Core Thesis: The Runtime Changes; The Architecture Does Not

The structural layers underpinning enterprise architectures (such as SAP ABAP OOP, SAP RAP, Oracle SOA, and high-throughput transaction monitors) are expressions of pure Clean Architecture. Proven across $\$350\text{M}+$ financial settlement workloads, the E2A Framework maps these exact enterprise patterns directly to modern AI-native topologies:

| Enterprise System Paradigm (SAP RAP / OOP) | What It Enforces / Structurally Solves | E2A AI Framework Equivalent |
| :--- | :--- | :--- |
| **OData Service Exposure** | Governed, decoupled interface boundary. | FastAPI REST Endpoints |
| **Business Defense (BDEF Contracts)** | Invariant protection & state-transition rules. | `BaseAgent` Abstract Class Contracts |
| **CDS Entities & Transactional State** | Structured data definitions and transactional buffer. | `AgentState` Orchestration TypedDicts |
| **Abstract Peer Classes** | Independent contracts for orchestration paths with distinct dispatch shapes. | `BaseRAGPipeline`, `LLMOnlyAgent` |
| **Gateway / Anti-Corruption Layer** | One governed boundary between business logic and a volatile external system. | `BaseLLMAgentService` — the single place the model is called |

---

## 🛠️ Framework Architecture & Core Specifications

E2A enforces structural non-functional requirements (NFRs)—such as idempotency, latency SLOs, token tracking, and groundedness limits—directly at the compilation and class lifecycle layer rather than relying on loose application-level exceptions or defensive prompts.

### 1. The Single Public Entry Point Pattern
To safeguard cross-cutting NFR internals from domain-level leakages, all communication across agent nodes follows a strict interface protocol. Application loops and external workflow nodes **never** invoke internal helpers directly. Execution is securely encapsulated via a singular, typed interface contract:
* `run(state, config, **kwargs)`
* `execute(payload, config, **kwargs)`
* `retrieve(query, config, **kwargs)`
* `ingest(documents, config, **kwargs)` — the write side of retrieval (`BaseIndexingPipeline`)
* `process_inference(messages, config, **kwargs)` and `process_inference_stream(...)` — `BaseLLMAgentService`: one public entry per interaction mode (unary, server-streaming)

### 2. Encapsulated Access Modifier System
The framework segregates execution governance from custom implementation details through explicit object-oriented boundaries:
* **`PUBLIC` Interface Hooks:** The only exposed entry points for pipeline execution (e.g., `agent.run()`).
* **`PROTECTED` Lifecycle Steps:** Internal hooks that subclasses must override to inject business logic (e.g., `_build_messages()`, `_evaluate_output()`).
* **`PRIVATE` Governance Engines:** Immutable framework routines that handle logging telemetry, error-budget calculation, and token cost tracking. These cannot be overridden.
* **Compile-Time Enforced Foundation Classes:** `BaseObservability` and `BaseGovernanceFramework` are `ABC` subclasses with `@abstractmethod`-decorated hooks — a subclass missing a required hook (e.g. `_export_traces()`, `_verify_sandbox_profile()`) fails at instantiation, not at first call in production.
* **Compile-Time Harness Engineering:** The Template Method pattern provides execution sandboxing for autonomous agents. Subclasses inherit runtime boundaries, distributed idempotency (Redis `SETNX` / DynamoDB conditional writes), and transactional outboxes by construction—preventing unconstrained tool calls from mutating enterprise state.

### 3. Stateless Explicit Context Propagation
To eliminate thread-state corruption in elastic container environments (Kubernetes, AWS ECS, Cloud Run, Azure Container Apps), E2A requires a **Six-Field Explicit Propagation Contract**:

```python
@dataclass(frozen=True)
class ExecutionContext:
    correlation_id: str      # W3C standard traceparent (cross-cloud trace lineage)
    tenant_id: str           # Multi-tenant data partition isolation
    idempotency_key: str     # Deterministic HMAC-SHA256 replay-prevention key
    io_config: dict          # Ephemeral caching directives, timeouts, regional endpoints
    message_log: list        # Isolated, append-only transaction audit trail
    failed_keys: set         # Granular error identification and circuit-breaker states
```

---

## 🔌 Decoupled LLM Inference Service

The model call no longer lives inside agent classes. `BaseLLMAgentService` is an abstract agent-microservice class, in `e2a_base.py` beside the others, that owns every cross-cutting concern of calling a model — and is reusable by **any** agent type (`BaseAgent`, `LLMOnlyAgent`, generated A2C agents). Agents reach it through `config['llm_service']`; they never import a provider SDK.

* **Vendor-neutral by construction:** the only vendor-specific code is two abstract hooks, `_dispatch_upstream_call` and `_dispatch_upstream_stream`. Bedrock, Vertex AI, Azure AI Foundry, a self-hosted Llama endpoint or any other provider is one small subclass; nothing else changes.
* **Governance inherited, not re-implemented:** pod-level concurrency semaphore, in-memory RPM/TPM token bucket (per pod = fleet limit ÷ replicas, unused tokens refunded), context-budget management, tenant-scoped KV-cache handles, tool-schema compilation, DLP redaction (Luhn-checked cards) on unary and streamed output, and a latency SLO.
* **Typed failure semantics:** a failed inference returns a `FAILED` payload and populates `failed_keys` instead of raising (only `DehydrationInterrupt` propagates), so the existing Saga/DLQ path applies unchanged.
* **Config precedence:** per-call kwargs › config › environment › registered default (all declared in the central `E2A_CONFIG` registry), so a cheaper model for a critic or evaluator is just `model_id=...` on the call.
* **Landing zone:** deployed as the Tier 2b Inference Gateway — dual ingress (gRPC and queue), event-driven autoscaling on in-flight requests and backlog, and an optional Redis-compatible semantic cache. See [docs/CLOUD_LANDING_ZONE.md](docs/CLOUD_LANDING_ZONE.md).

Retrieval follows the same discipline: `BaseRAGPipeline.retrieve()` now passes both `query_text` and `query_vector` to `_search_index(query_text, query_vector, config, tenant_id, **kwargs)`, making hybrid (BM25 + vector) search the default contract, weighted by `hybrid_alpha`. `BaseIndexingPipeline.ingest()` is the CDC-fed write side.

---

## ⚙️ Central Configuration — One Registry for the Whole Framework

Every configuration key and environment variable used by every agent, workflow, RAG, tool, MCP, LLM-service and pipeline class is declared **once**, in the `E2A_CONFIG` registry at the top of [`reference/e2a_base.py`](reference/e2a_base.py), and resolved by a single function:

```python
value = resolve_config('max_latency', config, kwargs)   # kwargs > config > environment variable > registered default
```

* **No class reads `os.getenv()` or hardcodes a default.** Per-class behaviour (for example, a 5.0 s tool-call latency SLO versus 2.0 s for agents and 1.0 s on the CQRS profile) is a scoped default inside the registry.
* **Projects extend, never edit.** Add your own keys with `register_config_key(ConfigKey(...))` in `src/settings.py`; `unknown_config_keys(config)` catches misspelled keys at start-up.
* **Generated, never hand-written.** `.env.example` ([`reference/env.example`](reference/env.example) (copy to `.env`)) and the configuration table ([`docs/config_reference.md`](docs/config_reference.md), also Section 5 of the Implementation Playbook) are produced from the registry by `env_example()` and `config_reference_markdown()`, so documentation cannot drift from code.
* **Secrets and objects stay out of files.** `auth_token` and `signing_secret` are emitted empty; `llm_service`, `governance_engine` and `observability_engine` are object references passed in the config dict.

## 🎭 Multi-Agent Orchestration: RAG, Tool Call, and LLM-Only

Every request resolves to one of four peer agents through a single `agent_registry` lookup — RAG-grounded, MCP tool call, API tool call, or LLM-only (text, speech, image, or document) — with an automatic fallback agent for anything that doesn't classify. `LLMOnlyAgent` ships as an independent abstract class beside `BaseAgent`, the same architectural move already made for `BaseRAGPipeline`, not a subclass of it. [Full writeup →](docs/multimodal-agent-orchestration.md)

---

## 🌐 Cross-Cloud Portability & FinOps Arbitrage

Because E2A cleanly decouples agent orchestration from proprietary vendor packages, it provides complete model and provider portability. By abstracting the core orchestration lifecycle, the identical agent subclass can execute seamlessly across **AWS Bedrock, GCP Vertex AI, Azure AI Foundry, or standalone Meta Llama** topologies.

### Programmatic Workload Arbitrage
The base configuration engine supports dynamic, time-of-day cost routing directly inside the runtime loop. Workloads can be programmatically shifted from premium frontier models to highly optimized open-source models based on real-time margin thresholds without modifying single lines of subclass code:

Because the model call is a single abstract service, switching provider is a binding change (one `_dispatch_upstream_*` subclass), not an agent change:

```python
# Real-time FinOps Arbitrage pattern executed via configuration adjustments
hour = datetime.datetime.utcnow().hour
model_id = 'meta.llama4-scout' if hour < 8 or hour > 20 else 'anthropic.claude-3-5-sonnet'

agent.run(state, {'model_id': model_id, **base_config})
```

---

## 📋 The Nine Abstract Classes

| Layer | Class | Public Entry Point | NFRs Enforced |
| :--- | :--- | :--- | :--- |
| **Agentic Orchestration** | `BaseWorkflow` | `execute()` | Governance approval, graph validation, intent routing |
| **Agentic Orchestration** | `BaseAgent` | `run()` | Idempotency, token budget, observability, fallback (model call delegated to `BaseLLMAgentService`) |
| **Inference** | `BaseLLMAgentService` | `process_inference()` / `process_inference_stream()` | Concurrency, RPM/TPM limits, context budget, KV cache, DLP, latency SLO, `failed_keys` on failure |
| **Retrieval** | `BaseRAGPipeline` | `retrieve()` | Hybrid (BM25 + vector) search, rerank, faithfulness gate $\ge 0.85$ |
| **Retrieval (write side)** | `BaseIndexingPipeline` | `ingest()` | CDC-fed chunking, embedding, index write/delete |
| **Tool Services** | `BaseToolService` | `execute()` | Exactly-once write, auth, retry, timeout, governance |
| **Foundation** | `BaseInfraProvisioner` | Interface | VPC, compute, storage, secrets contract |
| **Foundation** | `BaseObservability` | Interface | Metrics, traces, logs, SLO contract |
| **Foundation** | `BasePipeline` | Interface | Tests, RAG eval gate, build, deploy contract |
| **Foundation** | `BaseGovernanceFramework` | Interface | Policy, FinOps, SLO, circuit breaker contract |

---

## 🚀 The AI-SDLC Stack Family (P0, G2C, A2C, E2A)

The framework stack operationalizes four specialized disciplines across the modern AI software development life cycle, mapping directly to distributed systems foundations:

* **P0 Framework (Context Engineering as CQRS Read-Model Projection):** Zero-day developer workspace bootstrap generating structured context manifests (`scaffold-config.json`) and repo directory trees in $<10\text{s}$ to eliminate LLM context setup friction and token waste.
* **G2C Framework (Context Compilation as IDL / Contract Synthesis):** Spec-driven meta-generation substrate compiling declarative OpenAPI/OData schemas into type-safe microservice classes (FastAPI / Spring Boot), eliminating boilerplate and context drift.
* **E2A Framework (Harness Engineering as Command Pipeline):** Model Context Protocol (MCP) execution harness providing Template Method base classes (`BaseWorkflow`, `BaseAgent`, `BaseLLMAgentService`, `BaseMCPServer`) that enforce execution sandboxing, idempotency, and transactional outbox commits.
* **A2C Framework (Continuous Evals as Shift-Left AST Verification):** Enforces non-functional requirements (NFRs) at generation time. Embedded `CodeCriticAgent` harnesses validate AST structures and assert automated RAGAS faithfulness thresholds ($\ge 0.85$) before code reaches review.

The composed, cloud-native version of this pipeline — deployed as a Saga-governed service a developer triggers from one UI action — is documented in [docs/AIDLC_LANDING_ZONE.md](https://github.com/subhamviky/e2a-framework/blob/main/docs/AIDLC_LANDING_ZONE.md).

---

## 📐 Reference Documentation

| Document | Architectural Profile | Core Coverage |
| :--- | :--- | :--- |
| [docs/E2A_STRATEGIC_OVERVIEW.md](docs/E2A_STRATEGIC_OVERVIEW.md) | Strategic Overview | Two-page summary of value, capabilities, the four project archetypes, use cases and role-by-role benefits |
| [docs/config_reference.md](docs/config_reference.md) | Central Configuration | Generated table of every config key, environment variable, default and consumer, from `E2A_CONFIG` |
| [docs/CLOUD_LANDING_ZONE.md](docs/CLOUD_LANDING_ZONE.md) | Agentic Landing Zone | Combined HLD/LLD — network zones, compute tiers (including Tier 2b Inference Gateway), Saga orchestration, vendor mapping (AWS/GCP/Azure) for `BaseWorkflow`/`BaseAgent`/`BaseLLMAgentService`. |
| [docs/IMPLEMENTATION_PLAYBOOK.md](docs/IMPLEMENTATION_PLAYBOOK.md) | Agentic Playbook | Class contracts, design rationale and full scaffold source (`reference/e2a_base.py`), including `BaseLLMAgentService` (§2.18) and its binding examples. |
| [docs/multimodal-agent-orchestration.md](docs/multimodal-agent-orchestration.md) | Multimodal Orchestration | RAG / MCP tool / API tool / LLM-only agent routing through one `agent_registry`. |
| [docs/AIDLC_IMPLEMENTATION_PLAYBOOK.md](docs/AIDLC_IMPLEMENTATION_PLAYBOOK.md) | AI-DLC Playbook | Implementation contracts for the composed G2C → P0 → A2C → E2A pipeline. |
| [docs/PLATFORM_USE_CASES.md](docs/PLATFORM_USE_CASES.md) | Use Cases | Five enterprise use cases across hyperscalers, marked implemented or directional. |
| [docs/CQRS_CLOUD_LANDING_ZONE.md](docs/CQRS_CLOUD_LANDING_ZONE.md) | Deterministic CQRS | Same topology scope for `BaseOrchestrator`/`BaseCommandService`/`BaseQueryService` execution path — zero LLM on the transactional write path, so no inference tier is provisioned. |
| [docs/CQRS_IMPLEMENTATION_PLAYBOOK.md](docs/CQRS_IMPLEMENTATION_PLAYBOOK.md) | CQRS Playbook | Class contracts and full scaffold source (`reference/e2a_cqrs_base.py`), including CQRS-adapted `BaseObservability`/`BaseGovernanceFramework`. |
| [docs/AIDLC_LANDING_ZONE.md](docs/AIDLC_LANDING_ZONE.md) | AI-DLC Factory | Composed, Saga-governed pipeline (G2C → P0 → A2C → E2A) generating compliant services from a single developer-triggered action. |
| [docs/e2a_architecture_framework_whitepaper.md](docs/e2a_architecture_framework_whitepaper.md) | Executive Whitepaper | Complete theoretical foundation: structural isomorphism, Template Method pattern, and multi-cloud industrialization. |
| [docs/enterprise_architecture_ai_pattern_mapping_matrix.md](docs/enterprise_architecture_ai_pattern_mapping_matrix.md) | Architectural Matrix | 12-pattern master taxonomic mapping pairing emerging AI concepts with distributed systems invariants and cloud managed tiers. |

---

## 📦 Reference Implementations & Production Validation Spikes

The practical specifications of this meta-standard are actively verified across production-ready cloud ecosystems:

* **[order-to-cash-agentic-ai](https://github.com/subhamviky/order-to-cash-agentic-ai):** Multi-agent coordination (Python, LangGraph, Bedrock, OpenSearch) featuring 5-agent bounded context isolation, hybrid RAG retrieval, and critic faithfulness verification.
* **[financial-settlement-platform](https://github.com/subhamviky/financial-settlement-platform):** High-throughput distributed settlement engine (Java 21, Spring Boot 3, Kafka, PostgreSQL) validating Saga orchestration with reverse compensation, transactional outbox CDC, and double-entry immutable ledgers.
* **[aws-reconciliation-engine](https://github.com/subhamviky/aws-reconciliation-engine):** Serverless reconciliation system (Python, Lambda, DynamoDB, SQS) enforcing two-layer idempotency gates, atomic state updates, and dead-letter queue escalation.

---

## 👤 Author & Architecture Advisory

**Subham Gupta** — Staff Architect · Distributed Systems & Enterprise Agentic Governance  
13+ years building ledger-grade distributed systems at SAP scale.

[![LinkedIn](https://img.shields.io/badge/LinkedIn-Connect-0077B5?logo=linkedin)](https://linkedin.com/in/subham-gupta-0a05a058)
[![Email](https://img.shields.io/badge/Email-subhamviky@gmail.com-D14836?logo=gmail)](mailto:subhamviky@gmail.com)

---
*Trademarks: AWS, GCP, Azure, Anthropic, Claude, OpenAI, Meta Llama, and SAP belong to their respective owners and are used purely for architectural reference and nominative identification.*
