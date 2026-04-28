# E2A — Enterprise-to-Agentic Architecture Framework

> **The runtime changes. The architecture does not.**

A structured methodology for engineers with enterprise architecture backgrounds
(SAP, Oracle, Mainframe, Java EE) transitioning to production-grade agentic AI systems.

**Core thesis:** The structural layers in SAP RAP, Spring Boot, and LangGraph agents
are the same Clean Architecture pattern — Behavior Definitions are BaseAgent abstract
classes; Determinations are CriticAgents; CDS Entities are AgentState TypedDicts.
Enterprise architects already know how to design these systems. They have not seen
the mapping made explicit.

---

## Who This Is For

- SAP / ABAP architects building or evaluating agentic AI systems
- Java EE / Spring engineers moving to Python-based AI pipelines  
- Oracle, Mainframe, MuleSoft architects assessing agentic AI architecture
- Engineering managers evaluating whether enterprise patterns apply to AI

## Who This Is Not For

- Researchers building experimental agents (this framework is production-oriented)
- Teams building simple chatbots (the NFR-First methodology is overkill for low-complexity flows)

---

## The 10-Layer Mapping

| Architectural Layer | SAP RAP | Spring Boot | FastAPI/Python | LangGraph Agent |
|---|---|---|---|---|
| Data model | CDS View Entity | `@Entity` JPA | Pydantic `BaseModel` | `AgentState TypedDict` |
| Interface contract | Behavior Definition (BDEF) | Java `interface` | `Protocol` / ABC | `BaseAgent` abstract class |
| Business logic | Behavior Implementation (BIMP) | `@Service` | Concrete service class | Agent class + LLM calls |
| Callable operation | RAP Action | `@Transactional` method | Route handler | `AgentTool` / MCP Tool |
| Auto quality gate | Determination | Spring `@Aspect` / AOP | Middleware / decorator | `CriticAgent` |
| Exposure layer | OData Service Binding | `@RestController` | `FastAPI APIRouter` | `/workflow` endpoint |
| Async events | BOPF Event Framework | Spring Kafka `@KafkaListener` | SQS worker | LangGraph conditional edge |
| Observability | CDS Analytical Views + ATC | Micrometer + Actuator | structlog + OTel | `observability/` module |
| Governance | Authorization Objects | Spring Security | `policies/` YAML + OPA | CriticAgent SLO gate |
| Deployment | SAP CTS+ transport | Docker + Helm + kubectl | ECS + GitHub Actions | GitHub Actions → ECR → ECS |

→ Full reference: [reference/mapping-tables.md](reference/mapping-tables.md)

---

## NFR-First Development Sequence

| Phase | What Gets Built | Why Before Business Logic |
|---|---|---|
| 1. Scaffold | Folder structure, Poetry, pre-commit hooks | Forces architecture decisions before code |
| 2. IaC | Terraform: ECS, ALB, SQS, OpenSearch, Secrets | Infrastructure reproducible from day one |
| 3. Observability | structlog, OTel, CloudWatch, SLO checker | Every feature deployed into a monitored environment |
| 4. CI/CD | GitHub Actions: OIDC, RAGAS gate, Trivy, auto-rollback | Quality gates enforced before logic ships |
| 5. NFRs | Circuit breakers, FinOps routing, policy-as-code | Resilience and governance baked in, not bolted on |
| 6. Business Logic | Agents, RAG pipeline, tool microservices | Built on a production-ready foundation |

→ Full methodology: [reference/nfr-first-sequence.md](reference/nfr-first-sequence.md)

---

## Financial Integrity — Correct by Design

The E2A calibration principle: **financial integrity controls belong at the business
logic layer when domain complexity justifies it.** Three factors determine the appropriate layer:

1. **Input mutability** — can amounts or quantities change legitimately mid-flight?
2. **Output ambiguity** — can two records with the same value carry different business meaning?
3. **Audit exposure** — what is the cost of a wrong posting?

Score all three HIGH → business-layer controls (Saga, @Idempotent AOP, double-entry invariant).  
Score all three LOW → infrastructure-layer controls (API idempotency key, SQS dedup ID).

→ Decision table: [reference/calibration-principle.md](reference/calibration-principle.md)

---

## Full Framework Document

📄 [E2A_Framework.pdf](docs/Subham_Gupta_E2A_Enterprise_Agentic_Framework.pdf)

Nine sections covering: core thesis, Clean Architecture mapping, NFR-First methodology,
financial integrity patterns, domain mapping reference (agents, RAG, tools, observability,
NFR triad), CI/CD pipeline, cross-platform applicability, and quick reference card.

[E2A_Master_Abstraction_Reference](docs/E2A_Master_Abstraction_Reference.pdf)

Eight abstract classes across four layers: Agentic Orchestration, RAG based Retrieval, MCP based Tool Services, Foundation (environment-specific)

---

## Validated In Production

The E2A Framework is validated through three active AWS portfolio projects:

| Project | Stack | E2A Patterns Applied |
|---|---|---|
| [Financial Settlement Platform](https://github.com/subhamviky/financial-settlement-platform) | Java 21 · Spring Boot · Kafka · Spring AI | Saga · @Idempotent AOP · double-entry ledger · RAG audit |
| [Order-to-Cash Agentic AI](https://github.com/subhamviky/order-to-cash-agentic-ai) | Python · LangGraph · Bedrock · Terraform IaC | Full 6-phase NFR-First · CriticAgent · RAGAS CI/CD gate |
| [Payment Reconciliation Engine](https://github.com/subhamviky/aws-reconciliation-engine) | Python · Lambda · DynamoDB · Bedrock | Two-layer idempotency · DLQ · LangGraph multi-agent |

---

**Author:** Subham Gupta · Staff Architect, SAP Labs India  
[![LinkedIn](https://img.shields.io/badge/LinkedIn-Connect-0077B5?logo=linkedin)](https://linkedin.com/in/subham-gupta-0a05a058)
