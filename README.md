# E2A — Enterprise-to-Agentic Architecture Framework

> **The runtime changes. The architecture does not disappear.**

E2A is a vendor-neutral architecture framework for applying enterprise engineering discipline to agentic AI systems.

It focuses on the boundary where probabilistic reasoning meets deterministic enterprise execution: identity, tenancy, policy, state, tools, systems of record, reliability, observability, cost and confirmed physical outcomes.

**Core thesis:**

> **Probabilistic intelligence may propose an action. Deterministic enterprise controls must authorize, execute and confirm material outcomes.**

The framework grew from a recurring observation: SAP RAP, Java/Spring, Python services and agentic runtimes use different technologies, but many architectural responsibilities remain recognizable across them.

E2A makes those responsibilities explicit for production-oriented agentic systems.

---

## Who This Is For

- Enterprise architects evaluating or building agentic AI systems
- SAP / ABAP architects translating established transaction patterns into AI-native architectures
- Java EE / Spring engineers moving toward Python and agentic runtimes
- Cloud and platform engineers responsible for governed AI foundations
- Engineering leaders evaluating how traditional NFRs change when part of the execution path becomes probabilistic

## Who This Is Not For

- Low-risk prototypes that do not interact with enterprise data or tools
- Simple chatbots where a bounded model or retrieval workflow is sufficient
- Teams that need only a model SDK abstraction rather than an enterprise execution architecture

E2A deliberately introduces stronger controls than many prototypes need.

---

# 1. The Enterprise-to-Agentic Problem

Traditional systems of record operate under a deterministic correctness model:

- Explicit authorization
- Known state transitions
- Idempotency
- Concurrency control
- Transactional persistence
- Reliable event publication
- Auditability
- Recovery and reconciliation

Agentic systems add another correctness model:

- Probabilistic reasoning
- Retrieved context
- Model and prompt variability
- Tool selection
- Confidence and groundedness
- Iterative execution
- Token and model cost

The objective is not to force probabilistic components to behave like deterministic transaction processors.

The objective is to establish a clean boundary between them.

```text
Deterministic Enterprise World
        |
        | context, policy, data
        v
+-------------------------------+
|          E2A CONTROL          |
| identity | tenancy | policy   |
| context  | orchestration      |
| evaluation | observability    |
+-------------------------------+
        |
        | governed context
        v
Probabilistic AI World
agents | LLMs | RAG | planning
        |
        | proposed actions
        v
+-------------------------------+
| deterministic side-effect     |
| authorization + confirmation  |
+-------------------------------+
        |
        v
Systems of Record
```

**A tool call is not automatically a successful business transaction.**

A material action should be declared successful only after the downstream system has provided the evidence required by the business contract.

---

# 2. Position in the Framework Family

E2A is the runtime and governance core of a broader framework family.

| Framework / profile | Responsibility |
|---|---|
| **P0** | Project, platform, configuration and engineering scaffolding |
| **G2C** | Governed contract synthesis; Generate-to-Class is the initial concrete realization |
| **E2A** | Runtime orchestration, agents, RAG, tools, policy, enterprise integration and execution context |
| **A2C** | Policy-aware generation of code, tests, configuration, IaC and delivery artifacts |
| **AI-DLC** | Static and runtime evaluation and assurance across the lifecycle |
| **Deterministic CQRS profile** | Transactional commands, scalable queries, outbox, idempotency and compensation |

Conceptually:

```text
P0  ->  G2C  ->  E2A  ->  A2C
foundation   contracts   runtime   artifacts
                  \          /
                   \        /
                    AI-DLC
              evaluation / assurance
```

The layers are deliberately separated so that scaffolding, contract synthesis, runtime behavior, artifact generation and evaluation do not collapse into one opaque automation pipeline.

---

# 3. Architectural Principles

## 3.1 Correct by Design

Important controls should be encoded into architectural contracts rather than repeatedly rediscovered in application code.

Examples include:

- Lifecycle ordering
- Required request context
- Authorization boundaries
- Idempotency
- Evaluation gates
- Tool contracts
- Observability
- Failure classification
- Deterministic confirmation of material effects

## 3.2 NFR First

E2A treats non-functional requirements as architecture inputs rather than post-development checklists.

Availability, latency, security, cost, reliability, auditability and AI quality should map to explicit enforcement points and measurable evidence.

## 3.3 One Public Lifecycle Entry Point

Core framework capabilities expose a stable public entry point that owns lifecycle ordering and cross-cutting controls.

Domain implementations specialize behavior through bounded hooks rather than replacing the lifecycle itself.

## 3.4 Explicit Execution Context

Request state is propagated explicitly rather than hidden inside long-lived agent instances.

Typical context dimensions include:

- Correlation / trace identity
- Tenant
- User or workload identity
- Idempotency context
- Policy context
- Runtime configuration
- Business context
- Failure and compensation metadata

## 3.5 Vendor-Neutral Ports, Provider Adapters

Domain and execution contracts should not unnecessarily expose provider SDK semantics.

Cloud services implement those contracts through adapters.

Portability therefore means **stable contracts with replaceable realization**, not an assumption that AWS, Google Cloud and Microsoft Azure services are identical.

## 3.6 Deterministic Confirmation of Physical Side Effects

Agent reasoning, tool selection and even transport-level success are not sufficient evidence that a material business effect occurred.

For high-impact actions, E2A expects a deterministic confirmation appropriate to the system of record and business contract.

---

# 4. Structural Isomorphism

One of the ideas behind E2A is **structural isomorphism**: recurring architectural responsibilities can remain aligned across code contracts, runtime behavior and deployment topology even when technologies change.

It does **not** mean every class maps one-to-one to a cloud service.

It means similar responsibility boundaries should remain recognizable across layers.

| Code-level construct | Runtime responsibility | Typical physical realization |
|---|---|---|
| Public template method | Controlled lifecycle entry | API/service endpoint |
| Protected extension hook | Domain specialization | Private application compute |
| Private framework mechanic | Governed platform behavior | Adapter or managed platform capability |
| Execution context | Causality, identity and tenancy | Trace / identity / policy propagation |
| Evaluation gate | Release-quality decision | Runtime evaluator / pipeline gate |
| Compensator | Scoped recovery | Workflow/state-machine tier |

---

# 5. The 10-Layer Enterprise Mapping

The concrete comparison that originally motivated the framework remains useful:

| Architectural Layer | SAP RAP | Spring Boot | FastAPI/Python | Agentic Runtime |
|---|---|---|---|---|
| Data model | CDS View Entity | `@Entity` / DTO | Pydantic model | Agent / workflow state |
| Interface contract | Behavior Definition | Java interface | Protocol / ABC | Base agent/workflow contract |
| Business logic | Behavior Implementation | `@Service` | Concrete service | Agent + deterministic services |
| Callable operation | RAP Action | Transactional method | Route / service operation | Tool / command |
| Quality gate | Determination / validation | Aspect / validator | Middleware / decorator | Evaluator / critic gate |
| Exposure | Service Binding / OData | Controller | FastAPI route | Workflow endpoint |
| Async integration | Event framework | Kafka listener | Queue worker | Async workflow / event edge |
| Observability | Platform/runtime telemetry | Micrometer / Actuator | OTel / structured logging | Agent/tool/evaluation telemetry |
| Governance | Authorization / business rules | Security / policy | Policy layer | Model/tool/policy gate |
| Deployment | Platform transport | Container / Kubernetes | Cloud/container deployment | Governed cloud runtime |

See [`reference/mapping-tables.md`](reference/mapping-tables.md) for the repository reference material.

> The point of this table is architectural continuity, not technology equivalence.

---

# 6. NFR-First Development Sequence

A representative E2A engineering sequence is:

| Phase | What gets established | Why it comes early |
|---|---|---|
| **1. Scaffold** | Repository structure, dependencies, hooks and conventions | Forces foundational decisions before feature code expands |
| **2. Infrastructure** | Reproducible compute, networking, data, messaging and secrets | Makes the execution substrate explicit |
| **3. Observability** | Structured logs, traces, metrics and SLO instrumentation | Makes later behavior measurable |
| **4. CI/CD** | Build, test, security and evaluation gates | Prevents quality from depending only on review |
| **5. NFR controls** | Reliability, policy, security, cost and recovery mechanisms | Makes production controls architectural |
| **6. Business logic** | Agents, RAG, tools and domain flows | Builds capability on top of governed foundations |

Full repository reference: [`reference/nfr-first-sequence.md`](reference/nfr-first-sequence.md).

This ordering is a methodology, not a universal mandate. Simpler systems should remain simple.

---

# 7. Core Runtime Contracts

A normalized E2A runtime centers around a small number of controlled abstractions.

## BaseAgent

Representative lifecycle:

```text
run(...)
  |
  +-> validate state
  +-> enforce policy
  +-> build messages / context
  +-> invoke model through governed routing
  +-> evaluate output
  +-> execute authorized tools when appropriate
  +-> confirm material side effects
  +-> record telemetry
```

The public lifecycle remains stable. Domain-specific behavior is introduced through controlled extension points.

## BaseWorkflow

Coordinates stateful or multi-agent execution while maintaining explicit context, workflow ownership, terminal evaluation and failure boundaries.

## BaseRAGPipeline

Separates ingestion, indexing, retrieval, generation and evaluation responsibilities.

Enterprise controls include tenant scope, authorization, source lineage, freshness and groundedness evaluation.

## BaseToolService

Provides a transport-neutral contract for governed actions.

REST and MCP are transports. They do not replace authorization, schema validation, retries, idempotency or confirmation.

## BaseObservability

Standardizes traces, metrics, structured logs, SLO evaluation and business/agent execution evidence.

## BaseGovernanceFramework

Provides policy enforcement around model, tool, security, cost and execution constraints.

---

# 8. Deterministic vs Probabilistic Responsibilities

| Activity | Default character | Control required |
|---|---|---|
| Intent classification | Probabilistic | Confidence, schema and fallback |
| RAG retrieval | Mixed | Authorization, tenant scope, lineage and freshness |
| Plan generation | Probabilistic | Policy and contract validation |
| Tool recommendation | Probabilistic | Allowlist and authorization |
| System-of-record mutation | Deterministic | Idempotency, concurrency and transactional control |
| Material success declaration | Deterministic | Verified physical outcome |
| Natural-language explanation | Probabilistic | Evidence and policy constraints |

This separation is central to E2A.

---

# 9. Deterministic CQRS Profile

Agentic systems frequently still depend on conventional distributed-systems patterns for the parts of the workflow that change business state.

## Command path

Optimizes for correctness and controlled mutation:

- Validation and authorization
- Idempotency
- Optimistic concurrency where appropriate
- Atomic state mutation
- Transactional outbox
- Event publication
- Compensation or reconciliation

## Query path

Optimizes for scalable, isolated reads:

- Read models / replicas
- Tenant-scoped cache-aside
- Distributed mutex where cache stampede protection is justified
- Explicit staleness policy

## Event path

Uses durable publication and consumer-side idempotency rather than assuming exactly-once network delivery.

## Compensation path

Tracks failure scope and executes bounded compensating actions rather than globally rolling back unrelated work.

---

# 10. Financial Integrity — Correct by Design

The E2A calibration principle is:

> **Financial integrity controls belong at the business logic layer when domain complexity justifies them.**

Three questions help choose the correct layer:

1. **Input mutability** — can amounts or quantities legitimately change during the process?
2. **Output ambiguity** — can two records with the same value represent different business meaning?
3. **Audit exposure** — what is the impact of an incorrect posting?

When ambiguity and exposure are high, domain-level mechanisms such as sagas, explicit idempotency, reconciliation and ledger invariants become justified.

When they are low, simpler platform-level deduplication may be sufficient.

See [`reference/calibration-principle.md`](reference/calibration-principle.md).

---

# 11. RAG and Knowledge Boundaries

RAG is treated as a controlled subsystem, not merely a vector-search call.

A complete lifecycle considers:

1. Source authority and classification
2. Chunk lineage
3. Tenant-scoped indexing
4. Retrieval authorization
5. Metadata and freshness
6. Grounded generation
7. Evaluation
8. Release / fallback / escalation

The architecture should preserve enough evidence to understand what knowledge influenced an answer or action.

---

# 12. Tool Execution and MCP

E2A treats tools as enterprise integration boundaries.

Every material tool path should consider:

- Input schema validation
- Identity and authorization
- Tenant context
- Endpoint/tool allowlisting
- Credential isolation
- Timeout and retry policy
- Circuit breaking
- Idempotency where relevant
- Structured result contracts
- Physical-outcome confirmation
- Trace and audit evidence

MCP can standardize tool discovery and invocation. It does not remove the need for these enterprise controls.

---

# 13. Failure and Recovery Model

E2A distinguishes different failure classes rather than applying retries universally.

| Failure class | Representative treatment |
|---|---|
| Invalid input | Fail fast |
| Policy violation | Fail closed |
| Transient model/tool failure | Bounded retry with backoff |
| Repeated dependency failure | Circuit break |
| Low-quality model output | Repair, fallback or escalation |
| Retrieval insufficiency | Clarify, fallback or human review |
| Partial physical effect | Compensate or reconcile |
| Poison asynchronous work | DLQ and operational investigation |
| Cost-budget violation | Stop, downgrade or route |

Failure handling is policy-driven and should preserve causal evidence.

---

# 14. Evaluation, Observability and Cost

Agentic production systems require conventional telemetry plus AI-native evidence.

Representative dimensions include:

### Operational

- Request latency
- Tool latency and success
- Retry / circuit-breaker activity
- Queue depth
- Error rate
- Availability

### AI quality

- Groundedness / faithfulness
- Relevance
- Structured-output validity
- Policy compliance
- Task success

### Cost

- Input/output tokens
- Model selection
- Cost per request/workflow
- Cache hit rate
- Loop / delegation count

### Business

- Completed business outcome
- Escalation / exception rate
- Human approval or override
- Adoption and workflow completion

The exact metrics should be derived from the business use case rather than copied mechanically between systems.

---

# 15. Multi-Cloud Architecture

E2A separates stable architecture contracts from provider-specific realizations.

| Capability | Google Cloud | Microsoft Azure | AWS |
|---|---|---|---|
| Compute | Cloud Run / GKE | Container Apps / AKS | ECS / EKS / Lambda |
| Foundation models | Vertex AI | Azure AI Foundry / Azure OpenAI | Amazon Bedrock |
| Vector retrieval | Vertex AI Vector Search | Azure AI Search | OpenSearch Service |
| Relational state | AlloyDB / Cloud SQL | Azure Database for PostgreSQL | Aurora / RDS |
| Messaging | Pub/Sub | Service Bus / Event Grid | SQS / SNS / EventBridge |
| Cache | Memorystore | Azure Managed Redis | ElastiCache |
| Secrets | Secret Manager | Key Vault | Secrets Manager |
| Observability | Cloud Operations | Azure Monitor / Application Insights | CloudWatch / X-Ray |
| Durable workflow | Workflows | Durable Functions / Logic Apps | Step Functions |

These are reference mappings, not claims of semantic equivalence between provider services.

### Portability claim

Agent subclasses that depend only on supported E2A contracts may remain unchanged across provider adapters. Cloud-specific configuration, capabilities, infrastructure and adapter implementation may still differ.

---

# 16. Reference Implementations

E2A patterns are exercised through multiple architecture spikes and reference runtimes.

| Project | Stack | Patterns exercised |
|---|---|---|
| [Financial Settlement Platform](https://github.com/subhamviky/financial-settlement-platform) | Java / Spring Boot / Kafka / Redis / PostgreSQL | Saga, idempotency, financial-integrity patterns |
| [Order-to-Cash Agentic AI](https://github.com/subhamviky/order-to-cash-agentic-ai) | Python / LangGraph / Bedrock / OpenSearch / Terraform | Multi-agent orchestration, RAG, NFR-first controls, IaC |
| [Payment Reconciliation Engine](https://github.com/subhamviky/aws-reconciliation-engine) | Python / FastAPI / Lambda / SQS / DynamoDB / Bedrock | Serverless idempotency, async processing, DLQ and agent routing |

> **Evidence discipline:** these projects are reference implementations used to exercise architectural patterns. They should not be described as production validation unless the specific project provides explicit production evidence.

---

# 17. Framework Claim Classification

Repository documentation uses the following vocabulary deliberately:

| Classification | Meaning |
|---|---|
| **Framework invariant** | A rule E2A intends to enforce across implementations |
| **Implemented capability** | Present in concrete code |
| **Reference implementation** | An implementation used to exercise a pattern |
| **Recommended production pattern** | Guidance requiring contextual validation |
| **Directional design** | An extension or hypothesis under architectural exploration |

This keeps reference architecture, implementation evidence and future direction separate.

---

# 18. Why Not Simpler?

E2A is intentionally not the answer to every AI problem.

Before introducing framework machinery, ask:

1. Does the use case actually require probabilistic reasoning?
2. Does the workflow interact with enterprise data, tools or systems of record?
3. Is the action material, regulated or difficult to reverse?
4. Will multiple domains reuse the same controls?
5. Can a managed cloud service satisfy the requirement directly?

If the answer to these questions does not justify additional architecture, prefer the simpler system.

---

# 19. Repository Reference Material

- [`reference/mapping-tables.md`](reference/mapping-tables.md) — cross-runtime architecture mapping
- [`reference/nfr-first-sequence.md`](reference/nfr-first-sequence.md) — NFR-first engineering sequence
- [`reference/calibration-principle.md`](reference/calibration-principle.md) — financial-integrity calibration
- [`docs/Subham_Gupta_E2A_Enterprise_Agentic_Framework.pdf`](docs/Subham_Gupta_E2A_Enterprise_Agentic_Framework.pdf) — framework document currently stored in the repository

---

# 20. Design Summary

E2A can be summarized as a set of boundaries:

```text
ONE GOVERNED ENTRY POINT
          |
          v
VALIDATE IDENTITY + STATE + POLICY
          |
          v
PROPAGATE EXPLICIT EXECUTION CONTEXT
          |
          v
REASON / RETRIEVE / PLAN
          |
          v
EVALUATE BEFORE TRUST
          |
          v
AUTHORIZE TOOL OR COMMAND
          |
          v
EXECUTE THROUGH CONTROLLED ADAPTER
          |
          v
CONFIRM PHYSICAL OUTCOME
          |
          v
TRACE + AUDIT + MEASURE BUSINESS RESULT
```

> **Same enterprise principles. New runtime. Explicit boundaries. Measurable outcomes.**

---

## Author

**Subham Gupta**

Architecture R&D focused on enterprise distributed systems, cloud-native modernization, governed agentic AI, developer platforms and production AI engineering.

---

## Trademarks and ecosystem note

All trademarks, service marks and logos referenced in architecture examples are the property of their respective owners. References to SAP, AWS, Google Cloud, Microsoft Azure, Meta, LangGraph and other technologies are for educational and architectural purposes and do not imply endorsement.
