# E2A: Enterprise-to-Agentic Architecture Framework

A formal, cross-cloud architectural meta-standard designed to translate enterprise distributed systems discipline—proven at $350M+ financial settlement scale—directly into production-grade agentic AI systems.

---

## 🏛️ Core Thesis: The Runtime Changes; The Architecture Does Not

The structural layers underpinning enterprise architectures (such as SAP ABAP OOP, SAP RAP, Oracle SOA, and high-throughput transaction monitors) are expressions of pure Clean Architecture. The E2A Framework maps these exact enterprise patterns directly to modern AI-native topologies:

| Enterprise System Paradigm (SAP RAP / OOP) | What It Enforces / Structurally Solves | E2A AI Framework Equivalent |
| :--- | :--- | :--- |
| **OData Service Exposure** | Governed, decoupled interface boundary. | FastAPI REST Endpoints |
| **Business Defense (BDEF Contracts)** | Invariant protection & state-transition rules. | `BaseAgent` Abstract Class Contracts |
| **CDS Entities & Transactional State** | Structured data definitions and transactional buffer. | `AgentState` Orchestration TypedDicts |

---

## 🛠️ Framework Architecture & Core Specifications

E2A enforces structural non-functional requirements (NFRs)—such as idempotency, latency SLOs, token tracking, and groundedness limits—directly at the compilation layer rather than relying on loose application-level exceptions or prompt engineering.

### The Single Public Entry Point Pattern
To safeguard cross-cutting NFR internals from domain-level leakages, all communication across agent nodes follows a strict interface protocol. Application loops and external workflow nodes **never** invoke internal helpers directly. Execution is securely encapsulated via a singular, typed interface contract:
* `run(state, config, **kwargs)`
* `execute(payload, config, **kwargs)`
* `retrieve(query, config, **kwargs)`

### Encapsulated Access Modifier System
The framework segregates execution governance from custom implementation details through explicit object-oriented boundaries:
* **`PUBLIC` Interface Hooks:** The only exposed entry points for pipeline execution (e.g., `agent.run()`).
* **`PROTECTED` Lifecycle Steps:** Internal hooks that subclasses must override to inject business logic (e.g., `_build_messages()`, `_evaluate_output()`).
* **`PRIVATE` Governance Engines:** Immutable framework routines that handle logging telemetry, error-budget calculation, and token cost tracking. These cannot be overridden.

---

## 🌐 Cross-Cloud Portability & FinOps Arbitrage

Because E2A cleanly decouples agent orchestration from proprietary vendor packages, it provides complete model and provider portability. By abstracting the core orchestration lifecycle, the identical agent subclass can execute seamlessly across **AWS Bedrock, GCP Vertex AI, Azure AI Foundry, or standalone Meta Llama** topologies.

### Programmatic Workload Arbitrage
The base configuration engine supports dynamic, time-of-day cost routing directly inside the runtime loop. Workloads can be programmatically shifted from premium frontier models to highly optimized open-source models based on real-time margin thresholds without modifying single lines of subclass code:

```python
# Real-time FinOps Arbitrage pattern executed via configuration adjustments
hour = datetime.datetime.utcnow().hour
model_id = 'meta.llama4-scout' if hour < 8 or hour > 20 else 'anthropic.claude-3-5-sonnet'

agent.run(state, {'model_id': model_id, **base_config})
```
---

## The Eight Abstract Classes

| Layer | Class | Public entry point | NFRs enforced |
|---|---|---|---|
| Agentic Orchestration | `BaseWorkflow` | `execute()` | Governance approval, graph validation, intent routing |
| Agentic Orchestration | `BaseAgent` | `run()` | Idempotency, latency SLO, token budget, observability, fallback |
| Retrieval | `BaseRAGPipeline` | `retrieve()` | Chunking, embedding, search, rerank, faithfulness gate >= 0.85 |
| Tool Services | `BaseToolService` | `execute()` | Exactly-once write, auth, retry, timeout, governance |
| Foundation | `BaseInfraProvisioner` | Interface | VPC, compute, storage, secrets contract |
| Foundation | `BaseObservability` | Interface | Metrics, traces, logs, SLO contract |
| Foundation | `BasePipeline` | Interface | Tests, RAG eval gate, build, deploy contract |
| Foundation | `BaseGovernanceFramework` | Interface | Policy, FinOps, SLO, circuit breaker contract |

## A2C Extension

The [A2C Framework](https://github.com/subhamviky/a2c-framework) extends E2A in one
precise direction: it uses E2A-governed agents to *generate* enterprise-grade microservice
code, Terraform IaC, and GitHub Actions pipelines with mandatory NFRs injected
structurally at generation time via `_apply_policy()`.

The generator agent is governed by E2A. The output is governed by E2A.

## 📦 Reference Implementations & Validation Spikes
The practical specifications of this meta-standard are actively verified across production-ready cloud ecosystems:

* **Python Reference Spike:** Order-to-Cash Agentic AI Platform — A 5-agent LangGraph orchestration platform on AWS.
* **Java Reference Spike:** Cloud-Native Financial Settlement Platform — Validating cross-runtime transactional saga patterns.
