# E2A — Strategic Overview: Value, Capabilities & Where It Applies

*A two-page summary for architects, platform leaders and security reviewers. Each section ends with a pointer to the document that explains **how**.*

**Reference implementations:** [`e2a-framework`](https://github.com/subhamviky/e2a-framework) · [`financial-settlement-platform`](https://github.com/subhamviky/financial-settlement-platform) · [`order-to-cash-agentic-ai`](https://github.com/subhamviky/order-to-cash-agentic-ai) · [`aws-reconciliation-engine`](https://github.com/subhamviky/aws-reconciliation-engine)

---

## 1. The problem: the Enterprise Wall

Frontier models are probabilistic: they reason and generate through stochastic sampling. Enterprise systems of record — ERP backends, double-entry ledgers, core banking, regulated operational systems — are deterministic: ACID transactions, immutable ledgers, audited state changes. A schema drift, an unhandled exception or an unauthorized mutation there is a balance-sheet or compliance event, not a bug.

E2A is the governed bridge between the two. It treats a model call as a rate-limited network API, an agent as a microservice with one public entry point, retrieval as the CQRS read path, and tool calls as the transactional write path — validated, idempotent, and committed only on a machine-typed confirmation (`physical_success`), never on an agent's narrative claim.

**Go deeper:** [Whitepaper — Harmonizing the Frontier](e2a_architecture_framework_whitepaper.md) · [Pattern map](enterprise_architecture_ai_pattern_mapping_matrix.md)

---

## 2. What you get — eight capabilities

| Capability | In one line | Where it is specified |
| :--- | :--- | :--- |
| **Single public entry point** | One public method per class (`validate`, `run`, `execute`, `retrieve`, `serve`, `ingest`, `process_inference`); everything else is protected or private. | [Implementation Playbook §2](IMPLEMENTATION_PLAYBOOK.md) |
| **Layered (fractal) abstraction** | Level 0 immutable scaffold → Level 1 domain/industry rules → Level 2 vendor and cloud adapters → Level 3 business-logic leaf classes that override only protected hooks. | [Master Abstraction Reference](E2A_Master_Abstraction_Reference.docx) |
| **Decoupled inference service** | `BaseLLMAgentService` owns concurrency, RPM/TPM quota, context budget, caching hooks and stream DLP; subclasses implement only vendor wire transport. | [Playbook §2.18](IMPLEMENTATION_PLAYBOOK.md) |
| **One central configuration registry** | Every key and environment variable declared once (`E2A_CONFIG`) and resolved by one function: kwargs › config › environment › default. | [Playbook §5](IMPLEMENTATION_PLAYBOOK.md) · [config-reference](config_reference.md) |
| **Vendor and cloud portability** | Model, storage, lock, cache and queue are ports; switching provider is a binding plus a config value, not a rewrite. | [MultiCloud Reference](E2A_MultiCloud_Reference.docx) |
| **Explicit context propagation** | Six fields on every call: `correlation_id`, `trace_context`, `tenant_id`, `idempotency_key`, `message_log`, `failed_keys`. | [Playbook §2.1](IMPLEMENTATION_PLAYBOOK.md) |
| **Enforced non-functional requirements** | Latency, token and confidence limits raise `NFRViolationError` at run time; CI-time AST checks (`BasePipeline._verify_scaffold_contracts`) block merges that override framework-owned methods; an evaluation gate (RAGAS faithfulness ≥ 0.85) blocks deployment. | [Playbook §2.12](IMPLEMENTATION_PLAYBOOK.md) |
| **Human-approval hibernation** | `DehydrationInterrupt` persists workflow state and releases compute while waiting for an approval callback. | [Playbook §2.11](IMPLEMENTATION_PLAYBOOK.md) |

---

## 3. Four ways to use it

The same classes, propagation contract and governance model serve four kinds of project, so one engineering team can work from one standard.

| Archetype | What it is | E2A shape | Deep dive |
| :--- | :--- | :--- | :--- |
| **Agentic AI application** | Multi-step reasoning, tool use, human approval | `BaseWorkflow` + `BaseAgent` / `LLMOnlyAgent` + `BaseLLMAgentService`, `BaseMemoryManager`, `DehydrationInterrupt` | [Agentic Landing Zone](CLOUD_LANDING_ZONE.md) |
| **Deterministic CQRS application** | High-throughput command/query split, no LLM | `BaseOrchestrator` + `BaseCommandService` (OCC commit gate, idempotency, outbox) + `BaseQueryService` (cache-aside) | [CQRS Playbook](CQRS_IMPLEMENTATION_PLAYBOOK.md) |
| **Hybrid application** | Transaction engine and agent sidecars under one governance boundary | Both families share `_PropagationMixin`; an agent failure isolates its key in `failed_keys` and triggers saga compensation (`BaseReconciliationService`) | [CQRS Landing Zone](CQRS_CLOUD_LANDING_ZONE.md) |
| **AI-assisted SDLC (AI-DLC)** | Prototypes and specs compiled into governed code | G2C → P0 → A2C → E2A pipeline with AST and evaluation gates | [AI-DLC Playbook](AIDLC_IMPLEMENTATION_PLAYBOOK.md) · [a2c-framework](https://github.com/subhamviky/a2c-framework) |

---

## 4. Where it applies

Status uses the same key as [Platform Use Cases](PLATFORM_USE_CASES.md): 🟢 implemented in reference docs/runtimes · 🟡 directional.

| Use case | Problem | How E2A addresses it | Status |
| :--- | :--- | :--- | :---: |
| **Sidecar over a system of record** | Changing core transaction code is slow and risky. | Wrap existing APIs behind `BaseMCPServer` / `BaseToolService`; agents reason outside the core and commit only after validation and an OCC version check. The core stays unmodified. | 🟢 |
| **Prototype to production** | Playground prototypes stall on security and governance review. | Harvest tool schemas and signatures, generate E2A classes and landing-zone code, gate merges in CI. | 🟢 |
| **Progressive decomposition** | Day-1 microservices are costly; a monolith is hard to evolve. | Run every class in one process first; extract any class behind HTTP/gRPC later with no change to business logic, because each has one entry point. | 🟢 |
| **Multi-cloud portability** | Provider-SDK coupling limits leverage and residency options. | Port-and-adapter design for model, storage, lock, cache and queue. | 🟢 |
| **SRE and cost governance** | Silent failures, latency spikes, runaway token spend. | Dual RPM/TPM buckets, pod semaphores, context budgets, per-tenant telemetry, DLQ with scheduled reconciliation. | 🟢 |
| **Industry verticals** | Each vertical re-invents distributed-systems plumbing. | Level-1 domain classes carry the schemas and compliance rules. Financial settlement and order-to-cash are implemented in the reference repos; energy/asset maintenance and regulated healthcare are directional. | 🟢 / 🟡 |

**Go deeper:** [Platform Use Cases](PLATFORM_USE_CASES.md)

---

## 5. Who benefits and why

| Role | Concern | What E2A provides |
| :--- | :--- | :--- |
| **CTO / Chief Architect** | Lock-in, drift, technical debt | Ports and adapters; one scaffold that is never modified, only subclassed. |
| **CIO / Programme owner** | Time and risk of core change | Sidecar pattern leaves the system of record untouched. |
| **CISO / Risk** | Leakage, non-deterministic writes, injection, tenant mixing | Edge validation, tenant-scoped indexes and keys, ingress sanitising, stream DLP, outbox-backed audit trail, typed write confirmation. |
| **Engineering leadership** | Parallel delivery, review standards | Repository layout mirrors the tiers, so teams own separate classes (LLM platform, agent, retrieval, core integration, edge/security) with explicit contracts — an inverse-Conway arrangement. |
| **SRE / FinOps** | SLOs, spend, traceability | Capacity control at one gateway tier, uniform telemetry, central configuration changeable without code edits. |
| **Delivery / FDE practice leads** | Repeatable customer onboarding | Each engagement yields a running service and a reusable component back into the shared bench. |

---

## 6. Start here

1. Read the [README](../README.md) for the class set and architecture.
2. Read the [Whitepaper](e2a_architecture_framework_whitepaper.md) for the principles.
3. Open the [Implementation Playbook](IMPLEMENTATION_PLAYBOOK.md) and `reference/e2a_base.py` to see the code.
4. Run `reference/smoke_test_llm_service.py` for a self-contained demonstration.
