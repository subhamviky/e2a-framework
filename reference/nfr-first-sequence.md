
# NFR-First Development Sequence

> Build everything the business logic depends on before writing the business logic.

## The Six Phases

| Phase | What Gets Built | Enterprise Equivalent | Why First |
|---|---|---|---|
| **1. Scaffold** | Folder structure, Poetry env, pre-commit hooks | ABAP package + transport setup | Forces architecture decisions before any code exists |
| **2. IaC** | Terraform: ECS, ALB, API Gateway, SQS/DLQ, OpenSearch, Secrets Manager | SAP system landscape (DEV → QA → PROD) | Infrastructure reproducible and auditable from day one |
| **3. Observability** | structlog, OTel/X-Ray, CloudWatch metrics, SLO checker, error budget | CDS analytical views + ATC runtime monitoring | Every feature deployed into a monitored environment |
| **4. CI/CD** | GitHub Actions: OIDC auth, RAGAS eval gate, Trivy scan, auto-rollback | SAP CTS+ transport with ATC approval gates | Quality gates enforced before any logic ships |
| **5. NFRs** | Circuit breakers, retry/backoff, FinOps token routing, policy-as-code, LLM cache | ABAP Unit test harness + authorisation framework | Resilience and governance baked in, not bolted on |
| **6. Business Logic** | Agents, RAG pipeline, tool microservices | BIMP classes, determinations, actions | Built on a production-ready foundation from day one |

## Why This Order Matters

Most agentic AI projects are built in reverse — agents first, observability never.
The consequences surface in production: groundedness drift with no metrics to detect
it, token cost explosion with no budget enforcement, governance gaps discovered
during a compliance review rather than prevented by design.

NFR-First means every feature is born into a production-ready environment.
Observability, resilience, and governance are never retrofitted — they are the
foundation everything else is built on.

## The Terraform Lifecycle as a Test Pattern

Terraform follows the same four-phase lifecycle as a unit test framework.
Both exist to produce reproducible, auditable, controlled environments
from parameterised inputs.

| Phase | Terraform | Unit Test Framework (ABAP AUnit) |
|---|---|---|
| **Setup** | `terraform init` — downloads providers, initialises state backend | `SETUP` method — initialises test harness |
| **Controlled Inputs** | `variables.tf` — parameterised config (region, CPU, model IDs) | Test doubles / mock objects — controlled inputs |
| **Execution** | `terraform apply` — provisions all cloud resources | `TEST` method — runs business logic under test |
| **Assertion** | `outputs.tf` + smoke tests — exposes endpoints, verifies health | `ASSERT` statements — verifies expected results |

> **Important:** Terraform provisions real cloud infrastructure, not mocks.
> The analogy holds at the lifecycle pattern level — both enforce reproducible,
> parameterised, auditable environments through the same four phases.
> Terraform does for infrastructure what unit tests do for business logic.

## CI/CD Quality Gates

| Pipeline Stage | Implementation | Gate Condition |
|---|---|---|
| Lint + pre-commit | Black, Ruff, YAML validation, Terraform fmt | Blocks commit on any violation |
| Unit tests | pytest + coverage >= 70% | Blocks PR merge on failure or coverage drop |
| Policy compliance | Governance YAML validation + Trivy IaC scan | Blocks PR merge on policy violation |
| RAG evaluation | RAGAS: faithfulness, relevancy, precision, recall | Blocks deploy if any score < 0.85 |
| Build + scan | Docker build → Trivy vulnerability scan → ECR push | Blocks deploy on HIGH/CRITICAL CVE |
| Deploy + rollback | ECS update → smoke test → auto-rollback on failure | Auto-rollback if health check fails within 5 minutes |

---

→ Back to [README](../README.md)
