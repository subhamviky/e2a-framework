# E2A / A2C / P0 / G2C — Framework-to-Cloud Landing Zone

> **Translating Abstract Class Hierarchies into Cloud-Native Architecture Patterns**
>
> Subham Gupta · Staff Architect & AI Architect · [github.com/subhamviky](https://github.com/subhamviky) · June 2026

| Framework | Role |
|-----------|------|
| **E2A** | Abstract Agent Orchestration |
| **A2C** | NFR-First Code Generation |
| **P0** | Project Bootstrap |
| **G2C** | Self-Generating Framework |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [The Isomorphism Thesis — Class Hierarchy as Landing Zone](#2-the-isomorphism-thesis--class-hierarchy-as-landing-zone)
3. [E2A Framework — Class-to-Zone Architecture](#3-e2a-framework--class-to-zone-architecture)
4. [A2C Framework — NFR-First as Landing Zone Contract](#4-a2c-framework--nfr-first-as-landing-zone-contract)
5. [P0 Framework — Project Bootstrap as Account Vending Machine](#5-p0-framework--project-bootstrap-as-account-vending-machine)
6. [G2C Framework — Self-Generating Platform as Internal Developer Platform](#6-g2c-framework--self-generating-platform-as-internal-developer-platform)
7. [The 7-Phase End-to-End Flow as Landing Zone Provisioning](#7-the-7-phase-end-to-end-flow-as-landing-zone-provisioning)
8. [The Complete Cloud Landing Zone Architecture](#8-the-complete-cloud-landing-zone-architecture)
9. [Architectural Principles — The Mapping Codified](#9-architectural-principles--the-mapping-codified)
10. [Portfolio Application](#10-portfolio-application--three-projects-on-this-architecture)

---

## 1. Executive Summary

The E2A, A2C, P0, and G2C frameworks define a four-layer abstract class hierarchy governing how enterprise-grade agentic AI systems are structured, generated, and deployed. Each framework applies the **Template Method Pattern**: a single public entry point orchestrates a fixed lifecycle, protected abstract methods encapsulate domain variability, and private methods enforce infrastructure invariants that subclasses never touch.

This document establishes a formal architectural thesis: **the same structural discipline that governs these framework class hierarchies maps directly and completely onto a cloud-native landing zone.**

> **Core Thesis**
>
> The runtime changes; the architecture does not. The clean separation of public orchestration, protected domain logic, and private infrastructure helpers in E2A/A2C/P0/G2C maps one-to-one onto the layers of a cloud landing zone:
> - **Public method** → API Gateway (single ingress, governance at boundary)
> - **Protected method** → Business Logic Tier (domain-specific, replaceable)
> - **Private method** → Data & Infrastructure Tier (shared, never directly exposed)
> - **Abstract class enforcement** → Service Control Policy
> - **Inheritance** → Environment promotion (DEV → QA → PROD)

This is not metaphor. The structural relationships are isomorphic: every architectural constraint in the framework classes has a direct cloud-native counterpart enforced at the infrastructure level.

---

## 2. The Isomorphism Thesis — Class Hierarchy as Landing Zone

### 2.1 The Three-Layer Method Contract

Every E2A framework class follows an identical three-tier method access pattern. This pattern is not stylistic convention — it is a structural governance contract that determines what is changeable, what is overridable, and what is fixed by infrastructure.

| Layer | Framework Contract | Cloud Landing Zone Equivalent | Governance Rule |
|-------|-------------------|-------------------------------|-----------------|
| **PUBLIC** | Single entry point — `run()` / `execute()` / `bootstrap()`. Orchestrates full lifecycle. Never overridden. | API Gateway / ALB — single ingress, traffic governance, auth enforcement, rate limiting. | One door in. Governance enforced at the boundary. |
| **PROTECTED** | Abstract domain hooks — `_validate_state()`, `_build_messages()`, `_apply_policy()`. Subclass overrides for domain logic. | Business Logic Tier — ECS/EKS microservices, Lambda functions, LangGraph agents. Domain-specific; replaceable per environment. | Domain changes here. Infrastructure constraints enforced by abstract contract. |
| **PRIVATE** | Infrastructure helpers — `__llm_call()`, `__write_file()`, `__ensure_idempotency()`. Fixed mechanics. Never touched by subclasses. | Data & Infrastructure Tier — RDS/DynamoDB, S3, SQS, Secrets Manager, VPC internals. Shared; never directly exposed. | No direct access. Infrastructure as immutable contract. |

### 2.2 The Complete Class-to-Zone Mapping

| Framework Concept | Cloud-Native Equivalent | Enforcement Mechanism |
|-------------------|------------------------|-----------------------|
| **Abstract Class** | Landing Zone Account / VPC | AWS Control Tower Account Factory — every account derives from the same base blueprint |
| **Inherited Class** | Environment-specific Deployment (DEV / QA / PROD) | Terraform module inheritance — child modules extend root with environment overrides |
| **Public Entry Point** | API Gateway + ALB (single ingress per domain) | SCP: deny direct VPC access; all traffic via Gateway |
| **Protected Method** | ECS Task / Lambda Function / EKS Pod | IAM Role boundary: execute domain logic, cannot access raw DB |
| **Private Method** | RDS / DynamoDB / SQS / Secrets Manager | Security Group: no ingress from public tier; app tier only |
| **`@abstractmethod`** | Required NFR contract (Health Check, Observability endpoint) | ECS task definition enforces `/health` endpoint; deploy fails without it |
| **`_apply_policy()`** | Service Control Policy (SCP) + OPA Policy Gate | Runs before business logic; blocks non-compliant resource creation |
| **`_validate_state()`** | WAF + Input Schema Validation at API Gateway | Rejects malformed requests before compute is invoked |
| **CriticAgent** | RAGAS CI/CD Gate + CloudWatch SLO Alarm | Deployment blocked if quality score < 0.85; auto-rollback triggered |
| **`_fallback()`** | Circuit Breaker + Dead Letter Queue (DLQ) | pybreaker / AWS SQS DLQ — degraded path activated on failure |
| **`_handle_error()`** | Centralized Error Bus — EventBridge + SNS | Structured error events routed to ops topic; PagerDuty integration |
| **BaseWorkflow** | Step Functions / LangGraph StateGraph Orchestrator | State machine enforces execution order; same as `build_workflow()` DAG definition |
| **BaseRAGPipeline** | OpenSearch Serverless + Bedrock Knowledge Base | Chunk → Embed → Index → Search → Rerank pipeline; S3 source bucket |
| **BaseToolService** | Internal Microservice Mesh (App Mesh / ALB internal) | Private ALB; tool services not publicly addressable |
| **ScaffoldRequest (P0)** | Account Vending Machine Input Contract | Control Tower: `project_type` → account OU; `platform` → region config |
| **GeneratorRequest (G2C)** | Service Catalog Template / Platform Engineering Request | Self-service portal input → generates governed Terraform + app scaffold |

---

## 3. E2A Framework — Class-to-Zone Architecture

### 3.1 BaseAgent as a Three-Tier Cloud Application

E2A's `BaseAgent` defines the canonical template: one public `run()` method orchestrates six protected abstract hooks across four governance phases. This maps to a complete three-tier application zone with governance enforced at each layer boundary.

#### 3.1.1 The `run()` Method — API Gateway Pattern

`BaseAgent.run()` is the sole public entry point. It cannot be overridden. It enforces idempotency, validates state, applies policy, builds messages, calls the LLM, evaluates output, and handles errors — in that fixed sequence. This is architecturally identical to an API Gateway: one public ingress, fixed request lifecycle, governance before compute.

| `BaseAgent.run()` Step | API Gateway Equivalent | AWS Implementation |
|------------------------|----------------------|--------------------|
| `__ensure_idempotency()` | Idempotency Key validation on request header | API GW + DynamoDB conditional write (SETNX pattern) |
| `_validate_state()` | WAF + Request Schema Validation | API GW model validation + WAF WebACL rule group |
| `_apply_policy()` | SCP + OPA Policy Gate (pre-compute) | Lambda Authorizer + OPA sidecar evaluated before routing |
| `_build_messages()` + `__llm_call()` | Route to Compute (ECS / Lambda) | ALB target group → ECS task; Bedrock API call |
| `_evaluate_output()` | Response Validation + SLO Check | CloudWatch metric → SLO alarm → auto-rollback on breach |
| `_fallback()` / `_handle_error()` | Circuit Breaker + Error Response standardization | pybreaker / ALB fixed response + SNS error topic |

#### 3.1.2 Protected Abstract Methods — The Business Logic Tier

The six protected abstract methods in `BaseAgent` define the contract for domain variability. Each subclass (`RefundAgent`, `SettlementWorkflow`, etc.) implements these with domain-specific logic. In cloud-native terms, these map to the compute tier — ECS tasks or Lambda functions that can be swapped per domain without altering the surrounding infrastructure.

> **Architecture Rule:** Protected methods run inside the compute tier. They can read from the data tier (DynamoDB, S3) and call internal services (BaseToolService microservices), but they cannot directly access Secrets Manager, VPC internals, or other infrastructure primitives — those are private infrastructure methods enforced by IAM Role boundaries.

#### 3.1.3 Private Methods — The Data and Infrastructure Tier

Private methods (`__llm_call`, `__ensure_idempotency`, `__write_file`) encapsulate infrastructure mechanics. They are never overridden. In cloud-native terms, this tier contains RDS/DynamoDB, S3, SQS, Secrets Manager, and Bedrock API endpoints — shared infrastructure with no direct public access, governed by Security Groups and VPC subnet rules.

### 3.2 E2A Class Hierarchy → Environment Promotion Pipeline

| E2A Class Hierarchy | Landing Zone Environment | Terraform Module Pattern | What Changes |
|--------------------|--------------------------|--------------------------|--------------|
| **BaseAgent (Abstract)** | Root Account / Baseline VPC | Root Terraform module — shared SG, IAM, VPC layout | Nothing — fixed contract |
| **Domain Class (Concrete)** | DEV Environment Account | Child module: extends root, relaxed SLOs, debug logging | Log verbosity, SLO thresholds, model routing |
| Same class, QA config | QA Environment Account | Child module: RAGAS gate enforced, integration test fixtures | Quality gates stricter, test data isolation |
| Same class, PROD config | PROD Environment Account | Child module: full SCP, WAF on, FinOps routing active | Full governance, production model, cost controls |

### 3.3 E2A Multi-Class Architecture — Full Landing Zone Topology

| E2A Class | Cloud Zone / Tier | AWS Services | Network Boundary |
|-----------|------------------|--------------|-----------------|
| **BaseWorkflow** | Orchestration Tier | Step Functions / LangGraph on ECS | Private subnet; receives from API GW, routes to agents |
| **BaseAgent** | Compute Tier | ECS Fargate tasks / Lambda | Private subnet; calls Bedrock (VPC endpoint), tool services |
| **BaseRAGPipeline** | Knowledge Tier | OpenSearch Serverless, S3, Bedrock Titan Embeddings | Isolated VPC endpoint; no public ingress |
| **BaseToolService** | Integration Tier | Internal ALB + ECS microservices | Private ALB; accessible only from Compute Tier SG |
| **CriticAgent** | Governance / Evaluation Tier | Lambda + RAGAS + CloudWatch | Invoked by Workflow post-agent; blocks delivery on SLO breach |

---

## 4. A2C Framework — NFR-First as Landing Zone Contract

### 4.1 `_apply_policy()` as Service Control Policy

A2C's central architectural innovation is the `_apply_policy()` hook: it runs before the LLM call and programmatically injects mandatory NFRs (Idempotency, CircuitBreaker, Observability, RetryWithBackoff, HealthCheck, GracefulShutdown) into the generation contract. The developer cannot produce a service without these patterns — not because they remembered to ask, but because the abstract contract enforces it.

This mechanism is structurally identical to AWS Service Control Policy:

```
A2C:  DevRequest → _validate_state() → _apply_policy() [inject NFRs] → LLM call → generated code
SCP:  API call   → IAM eval          → SCP eval        [inject deny]  → resource provisioned

Both: governance runs BEFORE the action. Neither can be bypassed by the consumer.
```

### 4.2 SDLCWorkflow — The Multi-Agent Pipeline as Cloud Pipeline

| SDLC Agent | Cloud Pipeline Stage | Cloud Equivalent | Gate / Output |
|------------|---------------------|-----------------|---------------|
| **RequirementsAgent** | Requirements Validation | ADR lint check in CI | Validated DevRequest — mandatory fields confirmed |
| CodeGenAgent | Code Generation / Build | GitHub Actions: compile + unit test + coverage gate | Generated microservice code (Clean Architecture layers) |
| IaCAgent | Infrastructure Provisioning | Terraform plan + tfsec security scan | Generated Terraform modules for ECS/RDS/SQS |
| CICDAgent | Pipeline Generation | GitHub Actions YAML with OIDC, Trivy scan, rollback | Generated CI/CD workflow with RAGAS gate enforced |
| **CodeCriticAgent** | Quality Gate / Deployment Decision | RAGAS eval → CloudWatch SLO check → deploy or block | Score ≥ 0.85: deploy. Score < 0.75: regenerate. |

### 4.3 A2C NFR Injection → Cloud NFR Enforcement

| A2C Mandatory NFR | Generated Code Pattern | Cloud Infrastructure Equivalent | Landing Zone Control |
|-------------------|----------------------|--------------------------------|----------------------|
| **Idempotency** | DynamoDB conditional write + X-Idempotency-Key | SQS exactly-once + API GW idempotency header | SCP: deny SQS queues without redrive policy |
| Observability | structlog JSON + OTel + CloudWatch PutMetricData | CloudWatch Log Group + X-Ray tracing + Dashboards | SCP: deny ECS task defs without log configuration |
| CircuitBreaker | pybreaker per downstream service | ALB target group health checks + connection draining | ECS: required health check in task definition |
| RetryWithBackoff | tenacity decorator with jitter | SQS message visibility timeout + exponential backoff | DLQ required on all SQS queues (SCP enforced) |
| HealthCheck | `/health` endpoint (FastAPI/Spring Boot) | ECS readiness probe → ALB health check path | ALB: `/health` target group required for all services |
| GracefulShutdown | SIGTERM handler — drain in-flight requests | ECS: stopTimeout + ALB deregistration delay | ECS task def: stopTimeout ≥ 30s enforced via SCP |

---

## 5. P0 Framework — Project Bootstrap as Account Vending Machine

### 5.1 BaseProjectBootstrapper as Cloud Account Factory

> **Architectural Equivalence**
>
> P0 is an Account Vending Machine in abstract class form.
> - `ScaffoldRequest` = account provisioning contract
> - `bootstrap()` = Account Factory workflow
> - The 16-step bootstrap sequence = AWS Control Tower Account Vending Machine lifecycle

### 5.2 P0 16-Step Bootstrap → Control Tower Account Lifecycle

| # | P0 Bootstrap Step | Cloud AVM Equivalent | Cloud-Native Implementation |
|---|------------------|---------------------|----------------------------|
| 1 | `_check_idempotency()` | Account existence check | Control Tower: skip if account already exists in OU |
| 2 | `_resolve_config()` | Account config template load | Service Catalog: load account blueprint YAML |
| 3 | `_validate_request()` | SCP pre-flight check | Validate runtime ∈ APPROVED_RUNTIMES; platform ∈ APPROVED_PLATFORMS |
| 4 | `_plan_scaffold()` | Account provisioning plan | Terraform plan — list of resources to create |
| 5 | `_scaffold_structure()` | VPC + Subnet creation | `terraform apply`: VPC, public/private subnets, IGW, NAT GW |
| 6 | `_generate_build_manifest()` | IaC manifest | Terraform root module: `variables.tf`, `main.tf`, `outputs.tf` |
| 7 | `_generate_git_config()` | SCM baseline + branch policies | GitHub repo + branch protection rules + CODEOWNERS |
| 8 | `_generate_env_templates()` | Environment config injection | `.env.dev` / `.env.qa` / `.env.prod` with Secrets Manager refs |
| 9 | `_generate_docker_config()` | Container baseline | Dockerfile (non-root user, HEALTHCHECK), `.dockerignore`, ECR repo |
| 10 | `_generate_makefile()` | Runbook / Operations baseline | Makefile: build / test / deploy / destroy targets |
| 11 | `_generate_readme_scaffold()` | Service catalog entry | README.md: architecture, NFRs, runbook, SLO table |
| 12 | `_generate_license()` | IP governance tagging | LICENSE file + SBOM generation |
| 13-15 | `__write_file()` loop | Resource tagging + Cost allocation | AWS tags: Project, Team, Environment, CostCenter on all resources |
| 16 | `_validate_output()` | Post-provisioning smoke test | Smoke test: ALB health check, ECR push test, Secrets Manager read |

### 5.3 P0 Runtime Subclasses → Cloud Platform Templates

| P0 Concrete Subclass | Cloud Platform Template | Container Base Image | Default Compute Target |
|---------------------|------------------------|---------------------|----------------------|
| PythonPoetryBootstrapper | FastAPI / LangGraph template | `python:3.12-slim` (non-root) | ECS Fargate / Lambda |
| JavaMavenBootstrapper | Spring Boot microservice template | `eclipse-temurin:21-jre-alpine` | ECS Fargate / EKS |
| JavaGradleBootstrapper | Spring Boot + Gradle template | `eclipse-temurin:21-jre-alpine` | ECS Fargate / EKS |
| NodeNpmBootstrapper | Express / Next.js template | `node:20-alpine` (non-root) | Lambda / ECS Fargate |
| GoModulesBootstrapper | Go microservice template | `scratch` / distroless | Lambda / ECS Fargate |

---

## 6. G2C Framework — Self-Generating Platform as Internal Developer Platform

### 6.1 G2C as an Internal Developer Platform (IDP)

G2C is the top layer of the framework stack. It accepts a `GeneratorRequest` and produces a complete, governed, deployable project: E2A abstract classes + inherited domain classes + P0 project scaffold + A2C code/IaC/CI/CD — all in one call.

| G2C Concept | IDP / Platform Engineering Equivalent | AWS / Cloud Implementation |
|-------------|--------------------------------------|---------------------------|
| **GeneratorRequest** | Service onboarding form / golden path template | Service Catalog product + Parameter Store defaults |
| GeneratorFactory | Platform routing layer | Step Functions choice state → correct account factory path |
| E2AAbstractClassGenerator | Shared framework library publishing pipeline | CodeArtifact publishing + pip/Maven package release |
| E2AInheritedClassGenerator | New microservice onboarding (full stack) | Account + VPC + ECS cluster + app scaffold + CI/CD pipeline |
| A2CInheritedClassGenerator | SDLC developer platform instance | Code generation environment: Bedrock + S3 artifacts + CodeCriticAgent |
| **GeneratorCriticAgent** | Platform quality gate | RAGAS eval Lambda → score < 0.75 → regenerate with stricter prompt |
| BootstrapAndGenerateWorkflow | End-to-end platform vending workflow | Step Functions: P0 scaffold → A2C generate → G2C critic → deliver |

### 6.2 G2C Runtime Agnosticism → Multi-Cloud Platform

```python
# G2C runtime agnosticism → Cloud platform engineering pattern
request['runtime'] = 'python'  # generates e2a_base.py   → targets ECS Fargate / Lambda
request['runtime'] = 'java'    # generates E2ABase.java   → targets ECS Fargate / EKS
request['runtime'] = 'node'    # generates e2aBase.ts     → targets Lambda / ECS
request['runtime'] = 'go'      # generates e2a_base.go    → targets Lambda (scratch image)

# Generator layer: always Python, always AWS (Control Tower account)
# Generated workload: any approved runtime, any approved platform
# Equivalent to: Platform team operates in us-east-1 Control Tower account.
#                Generated services deploy to any approved region / account OU.
```

### 6.3 The Self-Referential Design — Framework Generates Itself

> **Self-Referential Governance Principle**
>
> Cloud Landing Zone equivalent: The platform account (Control Tower management account) must itself be provisioned by Terraform managed in the same CodePipeline it uses to provision workload accounts. The SCP that governs all accounts must itself be version-controlled, peer-reviewed, and deployed through the same CI/CD gate it enforces on others. **Governance applies recursively — to the governance layer itself.**

---

## 7. The 7-Phase End-to-End Flow as Landing Zone Provisioning

| Phase | Framework Activity | Cloud Zone Activity | AWS Services | Governance Gate |
|-------|--------------------|--------------------|--------------|-----------------| 
| **1** | E2A Abstract (G2C generates BaseAgent, BaseWorkflow) | Root Module / Shared VPC Blueprint | Control Tower, VPC, SG, IAM baseline | SCP: no public S3, no root account use, enforce CloudTrail |
| **2** | A2C Abstract (G2C generates SDLCAssistantAgent) | Platform Engineering Account + Service Catalog | CodeArtifact, Service Catalog, Step Functions | NFR injection contract validated before platform deployed |
| **3** | P0 Scaffold (project structure + manifests) | Account Vending + Baseline Infra | Account Factory, VPC, Route53, ECR, Secrets Manager | ScaffoldRequest: runtime ∈ approved list, platform ∈ approved list |
| **4** | E2A Inherited (domain agents: RefundAgent, SettlementWorkflow) | Workload Deployment — Business Logic Tier | ECS Fargate, ALB, CloudWatch, X-Ray | IAM Role boundary: compute tier cannot access raw data tier |
| **5** | A2C Inherited (IaCAgent, CICDAgent, CodeCriticAgent) | CI/CD Pipeline + IaC + Quality Gate | GitHub Actions, Terraform Cloud, RAGAS Lambda, Trivy | CodeCriticAgent score ≥ 0.85; Trivy scan: 0 critical CVEs |
| **6** | A2C Complete (execute(), deploy(), validate()) | Production Deployment + SLO Enforcement | ECS deployment, ALB routing, CloudWatch alarms | SLO: p95 latency < 2.5s; groundedness ≥ 0.85 |
| **7** | BootstrapAndGenerateWorkflow (all phases) | End-to-End Platform Vending Workflow | Step Functions Express, EventBridge | Full idempotency: re-run safe; full X-Ray trace across all phases |

---

## 8. The Complete Cloud Landing Zone Architecture

### 8.1 Landing Zone Network Topology

| Zone Tier | Framework Layer | Subnet Type | Resources + Security Controls |
|-----------|----------------|-------------|-------------------------------|
| **Public Ingress** | Public method (`run`/`execute`/`bootstrap`) | Public subnet | API Gateway + ALB. WAF WebACL. Route53. SSL termination. Rate limiting. No direct EC2/ECS access. |
| **Orchestration** | `BaseWorkflow` / `BootstrapAndGenerateWorkflow` | Private subnet | Step Functions / LangGraph ECS task. Receives from Public tier ALB only. |
| **Compute (Agent)** | Protected methods (`_validate`, `_apply_policy`, `_build_messages`) | Private subnet | ECS Fargate tasks / Lambda. IAM Role: read DynamoDB, call Bedrock via VPC endpoint. No public ingress. |
| **Knowledge (RAG)** | `BaseRAGPipeline` | Private subnet (isolated) | OpenSearch Serverless. S3 document store. Bedrock Titan Embeddings (VPC endpoint). Compute tier SG only. |
| **Integration (Tools)** | `BaseToolService` | Private subnet | Internal ALB + ECS microservices. Compute tier SG only. No public ALB. |
| **Data** | Private methods (`__ensure_idempotency`, `__llm_call`) | Isolated subnet | DynamoDB. RDS (private subnet). SQS (VPC endpoint). Secrets Manager (VPC endpoint). ElastiCache. |
| **Governance** | `_apply_policy` / `CriticAgent` / `_validate_state` | Cross-cutting (SCP + Lambda) | SCP (org-level). OPA sidecar. RAGAS eval Lambda. CloudWatch SLO alarms. Auto-rollback on breach. |

### 8.2 Governance Matrix — SCP Rules Derived from Abstract Contracts

| Abstract Contract | SCP Rule Derived | Effect on Non-Compliant Resource |
|-------------------|-----------------|----------------------------------|
| `_validate_state()` — required abstract | Deny ECS task creation without `/health` endpoint in task definition | ECS task definition rejected at registration time |
| `_apply_policy()` — governance before business logic | Deny Lambda creation without resource-based policy tagging owner + costcenter | Lambda function creation blocked; deploy pipeline fails |
| `__ensure_idempotency()` — fixed private | Deny SQS queue creation without redrive policy (DLQ required) | SQS queue creation rejected in all workload accounts |
| `_evaluate_output()` — quality gate | Deny ECS service update if CloudWatch SLO alarm is in ALARM state | Blue/green deployment blocked until SLO recovers |
| `_fallback()` — degraded path required | Deny ALB creation without a fixed-response action on 5xx | ALB listener rule must include fallback response |
| P0: APPROVED_RUNTIMES list | Deny ECR image pull if image not in approved base image list | CodePipeline: Trivy base image scan blocks non-approved images |
| G2C: GeneratorCriticAgent score ≥ 0.75 | Deny CodePipeline approval stage bypass; critic score must be in artifact | Manual approval stage requires `critic_report.json` with score ≥ 0.75 |

---

## 9. Architectural Principles — The Mapping Codified

### Principle 1 — Single Entry Point = Single Ingress
Every framework class exposes one public method. Every cloud zone exposes one ingress point. Multiple public methods = multiple attack surfaces = architecture violation in both contexts.

### Principle 2 — Abstract Contract = Service Control Policy
Every `@abstractmethod` in the framework is a required NFR. Every required NFR has a corresponding SCP rule that blocks non-compliant resource creation. Framework enforcement at code time + SCP enforcement at infrastructure time = **double-lock governance**.

### Principle 3 — Inheritance = Environment Promotion
Abstract base class → DEV. First concrete override → QA (adds quality gates). Second override → PROD (adds full SCPs, FinOps routing, production model). Environment differences are expressed as protected method overrides, not as environment-specific infrastructure forks.

### Principle 4 — Private Methods = Private Subnets
Private framework methods are inaccessible from outside the class. Private cloud subnets are inaccessible from outside the VPC/tier. Both encode the same invariant: infrastructure primitives are never directly accessible from the public interface.

### Principle 5 — CriticAgent = Deployment Gate
The CriticAgent runs after every agent/workflow execution and blocks delivery if score < threshold. This is architecturally identical to the CloudWatch SLO alarm blocking ECS deployments, the RAGAS gate blocking CI/CD pipeline progression, and the tfsec scan blocking Terraform apply.

### Principle 6 — NFR-First = Infrastructure-First
A2C's NFR-First methodology mandates: scaffold → IaC → observability → CI/CD → NFRs → business logic. Cloud landing zone provisioning follows the same sequence: account → network → monitoring → pipeline → security → workload. **Business logic is built last in both paradigms, on a production-ready foundation.**

### Principle 7 — G2C Self-Reference = Platform Eats Itself
G2C generators are E2A-governed agents. The platform account is provisioned by the same Terraform modules it uses to provision workload accounts. The governance layer must itself be governed. **Recursive self-governance — all the way down.**

### 9.2 What This Architecture Enables

- A developer submits a `GeneratorRequest`. G2C generates the abstract class, scaffolds the project (P0), generates the microservice code + IaC + CI/CD (A2C), and validates output quality (GeneratorCriticAgent). **Total time: minutes. Manual equivalent: days to weeks.**
- Every generated project is production-ready by construction: `/health` endpoint, structured logging, idempotency, circuit breaker, Dockerfile (non-root), Makefile — all enforced by the abstract contract, not by developer memory.
- Every cloud resource is SCP-governed from day one: no public S3, no root account access, DLQ required on SQS, `/health` required on ECS tasks — enforced at the AWS Organizations level, derived directly from abstract class contracts.
- Environment promotion is expressed as a configuration change on the same abstract class, not as a fork of infrastructure code.
- The complete platform is auditable from a single source of truth: full lineage from `GeneratorRequest` to deployed infrastructure is traceable via `trace_id` and CloudTrail.

---

## 10. Portfolio Application — Three Projects on This Architecture

| Project | Framework Stack Applied | Landing Zone Pattern | Key Architectural Demonstration |
|---------|------------------------|---------------------|---------------------------------|
| **[aws-reconciliation-engine](https://github.com/subhamviky/aws-reconciliation-engine)** | G2C → E2A → A2C → P0 | Lambda + DynamoDB + SQS + Bedrock RAG + LangGraph | BaseRAGPipeline as Knowledge Tier; DynamoDB idempotency as private method pattern; RAGAS CI/CD gate as CriticAgent equivalent. |
| **[order-to-cash-agentic-ai](https://github.com/subhamviky/order-to-cash-agentic-ai)** | G2C → E2A → A2C → P0 | ECS Fargate + LangGraph StateGraph + OpenSearch + Bedrock | BaseWorkflow as orchestration tier; multi-agent pattern as protected method tier; CriticAgent SLO gate as deployment gate. |
| **[financial-settlement-platform](https://github.com/subhamviky/financial-settlement-platform)** | G2C → E2A → A2C → P0 + Java 21 Spring Boot | EKS + RDS + ElastiCache + Python G2C generators | Java abstract class as landing zone blueprint; REF_ELEM_KEY idempotency as private method pattern; SAP TM financial integrity controls mapped to DynamoDB conditional write + UNIQUE INDEX invariant. |

> **Portfolio Efficiency Claim:** Each project is rebuilt on `main` using the G2C framework stack, with the original implementation preserved in a `legacy` branch. This enables concrete before/after efficiency measurement: manual hours vs. framework-accelerated hours per project. The `legacy` branch is the counterfactual for the framework thesis.

---

*Subham Gupta · Staff Architect & AI Architect · [github.com/subhamviky](https://github.com/subhamviky) · June 2026*
