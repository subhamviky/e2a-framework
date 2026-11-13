# E2A Framework — Agentic Core

### High-Level and Low-Level Design

*This document describes the agentic core of the E2A framework from problem statement to method-level contract — the scaffold every E2A service, the CQRS profile and the AI-DLC profile build on. It is written so that a reader can form a complete mental model of the framework: what it is for, how it is deployed, how every class collaborates and what every method guarantees, without opening the Python source. The source files remain the reference for line-level implementation.*

| | |
|---|---|
| Framework | E2A — Enterprise-to-Agentic Architecture Framework |
| Document type | Design Document (HLD + LLD) |
| Author | Subham Gupta, Principal Platform Architect |
| Source baseline | `subhamviky/e2a-framework` `v1.2.0` — the `e2a` package (`reference/e2a/`, 19 modules, 5,159 lines), `reference/e2a_base.py` (compatibility module), `reference/multimodal_orchestrator.py`, `reference/e2a_inmemory_adapters.py`; baseline of the v1.1 sections: `main` 32c99c0 |
| Date | 13 November 2026 (v1.2.0; baseline 7 November 2026) |
| Status | Aligned to E2A v1.2.0 and its test suite (design notes 01–05) |

## 1. Introduction

### 1.1 Purpose

E2A (Enterprise-to-Agentic) is an architecture framework for building multi-tenant, governed, AI-enabled enterprise services. It ships as the **`e2a` package** (installed as `e2a-framework`; until v1.1 a single scaffold file, `reference/e2a_base.py`, which remains as a compatibility module) of base classes that a project imports and subclasses, and never modifies. Each base class owns a fixed lifecycle behind governed entry points, one public method per genuine capability; projects fill in protected hooks with domain or vendor logic; private, base-owned methods enforce the invariants that must not vary between teams: idempotency, tenant isolation, bounded waits, typed outcomes, governance and telemetry.

This document is the design reference for that agentic core. It covers:

- the problem E2A solves and the motivation for its shape (Section 2);
- the core use cases and non-goals (Section 3);
- the design principles and invariants (Section 4);
- the high-level design: context, the three-layer contract, zones and tiers, the inference gateway, capacity boundaries, deployment and vendor mapping (Section 5);
- the propagation, payload and configuration contracts (Section 6);
- the low-level design: module structure, class model and a contract card for every class (Sections 7 and 8);
- dynamic behaviour as sequence, state and activity models (Section 9);
- the concurrency, consistency and failure model (Section 10);
- non-functional design: capacity, security, observability (Section 11);
- how to extend the framework (Section 12), how it is verified (Section 13) and known gaps (Section 14);
- what v1.2 changes, gathered in one place (Section 15).

The deterministic CQRS profile and the AI-DLC profile have their own design documents; both import the shared contracts described here.

> **v1.2.** E2A v1.2 keeps every v1.1 contract and adds a second, declarative style for new code: a frozen request context (`ctx`) and two accessors (`io_read`, `io_modify`) that a hook receives only if it declares them; governance as an ordered list of stages with typed refusals; workflows declared as steps, with `resume()` for a paused step; ports for RAG; and a conformance kit. A v1.1 subclass runs unchanged, with one `DeprecationWarning` per class. Section 15 summarises the changes; the sections they touch carry a **v1.2** note.

### 1.2 Audience

| Reader | Read first | Use the document to |
|---|---|---|
| Platform / solution architect | Sections 2–5, 10, 11 | Understand the tiers, isolation boundaries and capacity model; size a landing zone. |
| Service developer | Sections 6–9, 12 | Implement workflows, agents, RAG pipelines, tool services and an inference binding correctly. |
| SRE / operations | Sections 5.6–5.8, 10, 11, 13 | Understand saturation behaviour, failure semantics, metrics, repair jobs and graceful shutdown. |
| Security / governance | Sections 4, 5.9, 8.9–8.10, 11.3 | Check where admission, firewall, DLP, entitlements and egress controls live. |
| Reviewer | Sections 4, 13, 14, Appendix C | Verify invariants, traceability and the implemented-versus-runtime boundary. |

### 1.3 Status labels

**Table 1 — Status labels**

| Label | Meaning |
|---|---|
| **Implemented** | Present in the reference files and exercised by the smoke tests. |
| **Abstract hook** | `@abstractmethod`: the scaffold defines the contract, a subclass supplies the body; a missing hook fails at instantiation with `TypeError`. |
| **Default (v1.2)** | A protected hook that was abstract in v1.1 and now has a safe default (for example `BaseAgent._apply_policy`, `BaseWorkflow._get_agent`); a subclass overrides it only to specialise. |
| **Deprecated (v1.2)** | Kept for v1.1 code with a `DeprecationWarning`; removed in v1.3 (the v1.1 hook signatures, `BaseToolService.get_endpoint`). |
| **Reference** | Concrete, generic implementation shipped for reuse or as an example (e.g. `RestToolService`, `GCPInfraProvisioner`, the multimodal orchestrator). |
| **Test double** | In-memory adapter used by the smoke tests; not for production. |
| **Runtime obligation** | Behaviour the platform around the scaffold must provide (consumer loops, schedulers, the HTTP/gRPC server hosting a class). |

### 1.4 Source of truth and related documents

Where this document and the code disagree, the code is authoritative and Section 14 records the difference. Configuration names, defaults and environment variables in Appendix A are generated directly from the registry (`E2A_CONFIG` in `e2a/config.py`).

**Table 2 — Related documents**

| Document | Relationship |
|---|---|
| E2A Implementation Playbook | Narrative method listings and the full scaffold source. |
| E2A Cloud Landing Zone (HLD/LLD) | Infrastructure view; Sections 5 and 11 here summarise it. |
| E2A Deterministic CQRS Profile — Design Document | Replaces the Tier 2 workflow/agent layer with command/query services; imports the contracts in Section 8.6. |
| E2A AI-DLC — Design Document | Pipeline profile that composes G2C and A2C; uses the same idempotency, lease and capacity contracts. |
| E2A Master Abstraction Reference | Framework-wide taxonomy and naming rules. |
| `reference/smoke_test_*.py`, `tests/` | Executable evidence for Section 13. |
| E2A v1.2 design notes 01–05 (o2c-platform-hub, `roadmap/design/`) | Rationale for the v1.2 contracts in Section 15: entry points, request context, agent lifecycle, governance stages, workflows and structure. |

## 2. Problem Statement and Motivation

### 2.1 The problem

Enterprises moving from deterministic services to AI-enabled ones hit a recurring set of failures that are not about model quality:

1. **Every team wires the model differently.** Prompt handling, retries, rate limits, credentials and logging are reinvented per service, so platform-wide controls — cost caps, DLP, egress policy — cannot be applied.
2. **Narrative is mistaken for fact.** An agent says "I processed the refund"; nobody checks whether the downstream API committed.
3. **Duplicate and ambiguous side effects.** At-least-once delivery and timeouts cause tool calls to run twice, or to be dead-lettered after they actually succeeded.
4. **Unbounded waits and noisy neighbours.** One tenant's burst, one slow tool or a throttled model provider exhausts shared threads, pools and quota for everyone.
5. **Tenant leakage.** Caches, vector indexes and prompt caches are keyed globally rather than per tenant.
6. **Governance as an afterthought.** Prompt-injection screening, PII redaction, human approval and sandboxing are bolted on after the fact, inconsistently.
7. **Architecture drift.** Code structure stops matching the deployed topology, so security zones, scaling units and ownership become guesswork.

### 2.2 The E2A answer

E2A addresses these structurally rather than by guidance:

- **One capacity-governed boundary to the model.** No agent calls a model SDK. Every inference goes through `BaseLLMAgentService`, deployed as its own Tier 2b inference gateway — the only tier with provider credentials and model egress.
- **Template methods with fixed lifecycles.** Each class's public method runs the same steps in the same order for every subclass; projects only fill hooks.
- **An explicit propagation contract.** `correlation_id`, `tenant_id`, `idempotency_key`, `io_config`, `message_log`, `failed_keys` (plus the additive `deadline`, `trace_context`, `expected_version`, `fencing_token`) travel as arguments across every class boundary — never as instance state.
- **Typed physical outcomes.** Tools return `physical_success` and `outcome` (`COMMITTED` / `NOT_COMMITTED` / `UNKNOWN`); workflows settle idempotency claims on that evidence, not on model text.
- **Concurrency owned by invariant.** Atomic claims, conditional commits, single-flight caches, leases with fencing, bulkheads, circuits and retry budgets — each owned by exactly one class.
- **Structural isomorphism.** Class boundaries map one-to-one onto cloud tiers, so the class diagram *is* the deployment diagram.

### 2.3 Design goals

**Table 3 — Design goals**

| ID | Goal | Mechanism |
|---|---|---|
| G-1 | Single governed model boundary | `BaseLLMAgentService`; agents reach it only through `_infer()` with `config['llm_service']`. |
| G-2 | Effectively-once material effects | Atomic tenant-scoped claim in `BaseWorkflow.execute()`; settlement from typed `PhysicalOutcome`. |
| G-3 | Bounded everything | `Deadline` propagated everywhere; bounded permit, quota, bulkhead and cache-fill waits; typed `AdmissionTimeout`. |
| G-4 | Tenant fairness and isolation | Per-tenant permits, tenant-scoped claims, cache keys, KV handles and index namespaces. |
| G-5 | Governance before compute, redaction after | `enforce_governance_gate()` pre-execution; `redact_response()` and egress DLP post-execution. |
| G-6 | Honest outcomes | `physical_success` / `outcome` are ground truth; `UNKNOWN` is reconciled, never blindly retried. |
| G-7 | Vendor neutrality | Model, store, cache, lock, telemetry and IaC are ports; vendors live in subclasses. |
| G-8 | Code-to-cloud isomorphism | Each class has exactly one tier; tier numbers are shared across all E2A profiles. |
| G-9 | Configuration without drift | One registry (`E2A_CONFIG`); docs and `.env.example` generated from it. |

## 3. Core Use Cases

### 3.1 Actors

**Table 4 — Actors**

| Actor | Description |
|---|---|
| Tenant user / client system | Calls the platform through the gateway with a JWT. |
| Edge (T0/T1) | Gateway plus `BaseValidationService`: schema, rules, entitlements; mints `correlation_id`. |
| Workflow (T2) | Concrete `BaseWorkflow` that routes to an agent. |
| Agent (T2) | Concrete `BaseAgent` or `LLMOnlyAgent`. |
| Inference gateway (T2b) | Concrete `BaseLLMAgentService` binding for one provider. |
| Knowledge / integration workers (T4) | RAG retrieval, indexing, REST and MCP tools. |
| Approver | Human who approves a dehydrated request. |
| Platform jobs | Outbox sweeper, reconciler, CI/CD pipeline, landing-zone provisioner. |

### 3.2 Use-case catalogue

**Table 5 — Use cases**

| ID | Use case | Main flow | Guarantee |
|---|---|---|---|
| UC-1 | Answer a grounded question | Edge → workflow → `RAGAgent` → `retrieve()` (hybrid, tenant namespace) → `_infer()` → faithfulness check → response | Tenant-isolated retrieval; low-faithfulness answers degrade to fallback |
| UC-2 | Perform an action through an API tool | Workflow claims key → `APIToolAgent._apply_policy()` → `BaseToolService.execute()` → typed outcome → settle claim | At most one effect per key; outcome is physical truth |
| UC-3 | Call a tool over MCP | `_resolve_tool_call()` finds the tool in `mcp_server_registry` → `MCPToolService` JSON-RPC `tools/call` | Same resilience and outcome typing as REST |
| UC-4 | Expose an existing API as an MCP tool | `BaseMCPServer.wrap_api_as_tool(handler, …)`; clients call `serve()` | No per-tool transport code |
| UC-5 | Model-only request (text, audio, image, document) | `GenericLLMOnlyAgent`: detect modality → transcribe / extract / pass image → `_infer()` | Size limits checked before paid preprocessing |
| UC-6 | Unknown or low-confidence intent | Router score < `min_confidence` → `FallbackAgent` → degraded answer | No model or tool spend on unroutable input |
| UC-7 | Stream a response | `process_inference_stream()` with hold-back DLP | Sensitive values split across chunks are redacted |
| UC-8 | Human approval | Governance raises `DehydrationInterrupt` after persisting state; resume via webhook | `AWAITING_WEBHOOK`, no DLQ; key reclaimable on resume |
| UC-9 | Duplicate or retried request | Claim returns `REPLAY` / `IN_PROGRESS` / `CONFLICT` | No second execution; stored, redacted result replayed |
| UC-10 | Keep the index in sync | CDC → `BaseIndexingPipeline.ingest()` | Per-document isolation; idempotent writes |
| UC-11 | Repair parked or stuck work | `BaseOutboxSweeper.sweep()`, `BaseReconciliationService.reconcile()` under a lease | Confirm physical truth before any replay |
| UC-12 | Ship a change safely | `BasePipeline.run_pipeline()`: security, tests, capacity + contract gate, RAGAS, images, DAST, rollout | No deploy that violates capacity equations |
| UC-13 | Stand up a tenant landing zone | `BaseInfraProvisioner.provision_landing_zone(tenant, POOL\|SILO, tier)` | Fixed step order; per-tier IAM |
| UC-14 | Run several agents as one request (v1.2) | `_build_workflow(ctx)` returns `Steps(Step(...), Route(...))`; one context, one deadline, one claim before the first material step | Each step recorded in `state['steps']`; a fallback or refusal ends the workflow; a repeat of a material request is `REPLAYED` as a whole |
| UC-15 | Approve a paused step days later (v1.2) | `ApprovalStage` pauses; the checkpoint is stored; `resume(checkpoint_id, decision)` | Decided once, by an allowed decider; an approval continues at the paused step under the original key; a rejection executes nothing |
| UC-16 | Check a codebase against the contract (v1.2) | `python -m e2a.conformance src/ --strict`; `AgentContract` tests | Framework-owned methods are not overridden; hooks are not called from outside |

### 3.3 Non-goals

- **Model quality.** E2A governs how models are called, not which prompt or model is best.
- **Exactly-once delivery.** The guarantee is effectively-once material effects under at-least-once delivery.
- **A global concurrency manager or service mesh.** Each primitive is owned by the class guarding the resource.
- **Vendor SDK wrappers.** Bindings are thin subclasses written per project.
- **Hosting.** The scaffold defines classes; HTTP/gRPC servers, consumer loops and schedulers that host them are runtime obligations.

## 4. Design Principles and Invariants

### 4.1 Principles

1. **Governed entry points** (v1.2; was "single public entry point per class"): a public method per genuine capability, checked by `e2a.conformance` (R1–R3); v1.2 adds `BaseWorkflow.resume`, `BaseMCPServer.serve_async` and `BaseRAGPipeline.evaluate_answer`. The entry points: `execute`, `run`, `retrieve`, `ingest`, `serve`, `process_inference` (plus its streaming sibling), `record_telemetry`, `enforce_governance_gate` (plus `redact_response`), `validate`, `sweep`, `reconcile`, `run_pipeline`, `provision_landing_zone`, `dispatch`.
2. **Three-layer method contract** — public (fixed lifecycle), protected (domain / vendor hooks), private (name-mangled base-owned invariants).
3. **Propagate, never store** — request-scoped data are arguments, so one instance safely serves concurrent requests.
4. **Errors become data at class boundaries** — public methods catch failures, append to `failed_keys` and call `_handle_error()`; only `DehydrationInterrupt` (and, by design, the latency SLO in `BaseAgent.run()`) crosses upward.
5. **Logical versus physical** — `physical_success` and `outcome` are ground truth; model narrative never is.
6. **Owned concurrency** — each risk is owned by one class, contract or config key.
7. **Central configuration** — `resolve_config()`: kwargs › config › environment › default.
8. **Isomorphism** — one class, one tier; tier numbers never change.

### 4.2 Invariants

**Table 6 — Invariants**

| ID | Invariant | Enforced by |
|---|---|---|
| INV-01 | No request runs without `tenant_id`. | `BaseWorkflow.execute()`, `process_inference()`, `ingest()` raise `ValueError`. |
| INV-02 | No agent holds model credentials or calls a model directly. | `_infer()` → `config['llm_service']`; Tier 2b is the only model egress. |
| INV-03 | One owner per tenant-scoped idempotency key; same key with a different payload is `CONFLICT`. | `BaseIdempotencyStore.claim()` + `_request_fingerprint()`. |
| INV-04 | A claim is settled once, by its owner, from typed evidence. | `__settle_claim()` + `_physical_outcome()`. |
| INV-05 | An `UNKNOWN` outcome is never retried or replayed blindly. | `fail(retryable=False, outcome=UNKNOWN)`; reconciler confirms first. |
| INV-06 | `DehydrationInterrupt` never reaches `_handle_error()` or the DLQ. | Dedicated branches in `run()` and `execute()`. |
| INV-07 | Every wait is bounded by `min(own timeout, remaining deadline)`. | `Deadline.bound()` in permits, quota, bulkhead, cache fill, upstream call, bridge. |
| INV-08 | Permits and quota are always released, including on cancellation. | `__concurrency_permit` context manager; synchronous `__settle_quota`. |
| INV-09 | A timed-out non-idempotent write is never re-sent. | `ResiliencePolicy` retries timeouts only when `idempotent`; tool returns `UNKNOWN`. |
| INV-10 | Prompts are screened on ingress and outputs on egress. | `__sanitize_prompts`, semantic firewall, `_redact_sensitive`, `redact_response`. |
| INV-11 | Cache, KV handle, index and claim keys are tenant-scoped. | Hook contracts and `_apply_kv_caching`. |
| INV-12 | No deploy violates the capacity equations. | `validate_capacity()` in the scaffold-compliance gate. |
| INV-13 | Scheduled repair runs with one leader and fenced writes. | `BaseDistributedLock` lease + `fencing_token`. |
| INV-14 | A v1.2 hook receives exactly the parameters it declares, from those its step offers. | `BaseAgent.run` / `BaseWorkflow` hook dispatch; declaring `io_modify` in `_prepare` is an error. |
| INV-15 | A material effect goes only through `io_modify`, always with an idempotency key; `io_read` refuses material tools. | `ReadAccess` / `ModifyAccess`; tool `material = True`; `allowed_tools`, `tool_policy`. |
| INV-16 | A request is screened before anything is read, and nothing material runs after the deadline or before an accepted evaluation. | `phase='screen'` stages before `_prepare`; `_act` only after evaluation, never after the deadline. |
| INV-17 | A declared workflow takes one claim, before its first material step; a paused step is decided once. | `Steps.claim_at`; `BaseCheckpointStore.decide`; `ResumeRefused`. |

## 5. High-Level Design

### 5.1 System context

![Figure 1 — System context of a platform built on the E2A framework](images/01_context.png)

*Figure 1 — System context of a platform built on the E2A framework*

### 5.2 The three-layer contract

Every E2A class has the same three layers, and each layer has a landing-zone counterpart. This is the basis of the isomorphism thesis: the runtime changes, the architecture does not.

![Figure 2 — The three-layer method contract and its landing-zone counterpart](images/02_three_layer.png)

*Figure 2 — The three-layer method contract and its landing-zone counterpart*

### 5.3 Zones and tiers

![Figure 3 — Agentic landing zone: zones, tiers 0–5 and the Tier 2b inference gateway](images/03_topology.png)

*Figure 3 — Agentic landing zone: zones, tiers 0–5 and the Tier 2b inference gateway*

**Table 7 — Tier responsibilities**

| Tier | Component | Responsibility |
|---|---|---|
| T0 | API gateway + WAF | JWT verification, tenant extraction, rate limiting, edge prompt-injection rules. |
| T1 | Validation function | `BaseValidationService.validate()` — schema, business rules, ABAC entitlements; mints `correlation_id`; async work to the intake topic, sync work to T2. |
| T2 | Orchestration compute | `BaseWorkflow` and agents; semantic router; governance gate; idempotency claim; telemetry hand-off. |
| T2b | Inference gateway | `BaseLLMAgentService` — permits, RPM/TPM quota, context budget, caching, DLP; dual ingress (gRPC from T2, queue for batch); only model egress. |
| T3 | Data & messaging perimeter | State/outbox and idempotency records, cache keyspaces (session, semantic, shared quota), vector index, topics, task queue, DLQ, multimodal intake bucket. |
| T4 | Workers | RAG retrieval, REST and MCP tools, CDC indexing; pull-based, autoscaled on depth; one bulkhead per dependency. |
| T5 | Saga edge | Commit or isolate / compensate / dead-letter on `failed_keys`. |
| — | Control plane | Leased sweeper and reconciler; CI/CD (`BasePipeline`); IaC (`BaseInfraProvisioner`). |

### 5.4 Structural isomorphism

![Figure 4 — Class boundary to tier mapping](images/04_class_to_tier.png)

*Figure 4 — Class boundary to tier mapping*

### 5.5 The inference gateway (Tier 2b)

`BaseLLMAgentService` is deployed as its own tier between orchestration and the model provider. It is vendor-neutral: only the two `_dispatch_upstream_*` hooks in a binding subclass know the provider. Its admission order is fixed and every wait is bounded: tenant permit → pod permit (`permit_timeout`) → RPM/TPM quota (`quota_timeout`) → upstream call under `min(llm_max_latency, remaining deadline)` behind a circuit breaker. Quota is an estimate reserved before the call and refunded to actual usage on success, failure and cancellation. Rate state is per pod and in memory (fleet limit ÷ `llm_replica_count`); a fleet-wide hard cap is an optional hook (`_acquire_shared_quota`) a subclass binds to a shared cache keyspace. The tier scales on in-flight requests and queue backlog, not CPU.

### 5.6 Capacity and backpressure at tier boundaries

Every boundary is either a **synchronous bounded wait** (timeout, deadline-capped, typed rejection) or a **durable backlog** (redelivery, DLQ). Mixing the two is what turns a slow dependency into an outage.

![Figure 5 — Capacity and backpressure at tier boundaries](images/28_capacity.png)

*Figure 5 — Capacity and backpressure at tier boundaries*

**Table 8 — Boundary bounds and saturation behaviour**

| Boundary | Wait type | Bound (config) | When saturated |
|---|---|---|---|
| T0 → T1 | Sync | Per-tenant gateway rate limit | HTTP 429; nothing reaches T2 |
| T1 → intake topic | Durable | Topic retention | 202 accepted; latency moves to backlog |
| Intake → T2 | Durable | `worker_concurrency` ≤ `db_pool_size`, `worker_prefetch` | In-flight bounded; poison → DLQ |
| T2 → T3 state/cache | Sync | `db_pool_size`, `deadline` | Bounded pool wait; claim/commit conflicts are typed |
| T2 → T2b | Sync | `permit_timeout`, `quota_timeout`; `tenant_concurrency` ≤ `pod_concurrency` | `AdmissionTimeout`; one tenant cannot hold all permits |
| T2b → provider | Sync | `min(llm_max_latency, deadline)`; circuit; refund | Typed failure; retries from a shared budget |
| T2 → T4 tools/RAG | Sync | `bulkhead_max_concurrent`, `bulkhead_acquire_timeout`, circuit, retry budget | `BulkheadFullError` / `CircuitOpenError`; timed-out write → `UNKNOWN` |
| Task queue → T4/T5 | Durable | Depth-driven autoscale; saga concurrency | Backlog grows; consumers stay within pools |

### 5.7 Saga edge (Tier 5)

T5 consumes the batch result and `failed_keys`. An empty list leads to a standard commit; otherwise the batch is isolated and split into parallel tracks — commit successful items, compensate, route failed items to the DLQ — before a final webhook. Inference failures arrive as data (`FAILED` payloads and keys in `failed_keys`), not exceptions. `BaseInfraProvisioner._generate_saga_orchestrator()` emits the AWS States Language skeleton by default; `GCPInfraProvisioner` overrides it with GCP Workflows YAML of the same logical shape.

### 5.8 Multimodal ingestion

Clients upload audio, documents and images by presigned URL to an isolated intake bucket in T3; only the object reference reaches T2. Transcription and extraction run as T2 preprocessing on a worker pool sized apart from inference permits, with duration and page limits (`max_audio_duration_seconds`, `max_document_pages`) checked before any paid call. Vision-capable models take the image reference directly.

### 5.9 Vendor mapping by neutral contract

**Table 9 — Neutral contract to provider example**

| Neutral contract | AWS | GCP | Azure | Semantics that differ |
|---|---|---|---|---|
| Atomic claim (`claim`) | DynamoDB conditional put | Firestore create-if-absent | Cosmos DB create | TTL is GC only; expiry is an explicit field |
| Conditional commit (`put_if_version`) | Condition expression on version | Update-time precondition | ETag `If-Match` | Single-item / single-partition scope |
| Lease + fencing (`LeaseHandle`) | Versioned item + expiry | Versioned document | Blob lease or versioned item | No native fencing token; adapter supplies it |
| Durable queue + DLQ | SQS + redrive | Pub/Sub + dead-letter topic | Service Bus | At-least-once everywhere |
| Ordered partition | SQS FIFO group | Ordering key | Session | Poison handling differs |
| Model endpoint (T2b) | Bedrock via VPC endpoint | Vertex AI via PSC | Azure OpenAI / Foundry via Private Link | Quota accounting differs; not assumed |
| Vector index | OpenSearch Serverless | Vector Search / AlloyDB AI | AI Search | Hybrid scoring options differ |
| Compute | ECS Fargate / EKS | Cloud Run / GKE | Container Apps / AKS | Termination grace: ~30 s / ~10 s / 30 s |
| Saga engine | Step Functions | Workflows | Logic Apps | Syntax only; same logical shape |
| Telemetry | CloudWatch + X-Ray | Cloud Monitoring + Trace | Azure Monitor + App Insights | — |

### 5.10 Deployment and delivery

`BasePipeline.required_artifact_matrix` defines five images — `e2a-ingress-validator` (T1), `e2a-orchestration-engine` (T2), `e2a-rag-retrieval-worker`, `e2a-rest-tool-worker` and `e2a-mcp-server-worker` (T4). T0 is a managed gateway and T5 is IaC-defined, so neither has an image. The inference gateway binding is deployed as its own service per provider. Environments are promoted by configuration (DEV → QA → PROD), never by subclass changes.

## 6. Data and Propagation Contracts

### 6.1 Propagation fields

**Table 10 — Propagation fields**

| Field | Dir. | Origin | Purpose |
|---|---|---|---|
| `correlation_id` | In | `validate()` at T1; else `execute()` mints one | Joins logs, metrics and traces; also the claim `owner`. |
| `tenant_id` | In | Verified JWT at T0; required in `state` | Scopes claims, permits, caches, KV handles, indexes, routing. |
| `idempotency_key` | In | Client key, else `_generate_idempotency_key()` | Claim key; appended to `failed_keys` on failure. |
| `io_config` | In | `_resolve_io_config()` — config keys with the `io_` prefix | Endpoint / quota configuration separate from behaviour. |
| `message_log` | Out | Empty list per request | Ordered structured events; shipped once to observability. |
| `failed_keys` | Out | Empty list per request | Failures for the DLQ and the Saga edge. |
| `deadline` | In | Inbound `Deadline` or `x-e2a-deadline-ms`, else `request_deadline_seconds` | Bounds every wait; crosses processes as remaining milliseconds. |
| `trace_context` | In | W3C `traceparent` from the edge | Forwarded by `_export_traces()`; excluded from fingerprints. |
| `expected_version` | In | Caller | Advisory OCC in `_commit_gate()`; authority is `put_if_version`. |
| `fencing_token` | In | Lease holder | Lets a store reject a stale writer. |

`BaseWorkflow.execute()` is the origin of the first six; it writes `tenant_id`, `correlation_id` and `idempotency_key` back into `state` and passes everything to `agent.run()` as keyword arguments.

> **v1.2.** The inbound fields travel together as one frozen `RequestContext` (Section 6.6); the outbound ones as `MessageLog` and `FailedKeys`. A v1.2 agent receives the context as `ctx=`; a v1.1 agent still receives the keywords above.

### 6.2 Request state keys used by the base classes

**Table 11 — State keys**

| Key | R/W | Meaning |
|---|---|---|
| `tenant_id`, `correlation_id`, `idempotency_key` | R/W | Propagation fields (written back by `execute()` and `run()`). |
| `intent` | R | Passed to `_get_agent()`. |
| `requires_human_approval`, `approval_granted` | R | Trigger dehydration in the governance gate. |
| `pending_mcp_tool_execution` | R | Triggers `_verify_sandbox_profile()`. |
| `cumulative_tokens_used` | R/W | Accumulated by `run()`; compared with `max_token_budget` by governance. |
| `response` | W | The (fallback-replaced, redacted) model response. |
| `outcome` | R | `PhysicalOutcome` from a tool result; read by `_physical_outcome()`. |
| `status` | W | `REPLAYED`, `IN_PROGRESS`, `CONFLICT`, `RECOVERY_REQUIRED`, `FAILED_FINAL`, `AWAITING_WEBHOOK`. |
| `idempotency_status`, `replayed` | W | Raw claim status and replay marker. |
| `message_log`, `failed_keys` | W | Always written on exit. |
| `steps` | W | v1.2: one record per declared step (`name`, `agent`, status `DONE` / `SKIPPED` / `FALLBACK` / `REFUSED` / `PAUSED`, latency). |
| `refusal` | W | v1.2: a typed governance refusal (`code`, `stage`, `reason`, `retryable`). |
| `evaluation`, `errors` | W | v1.2: `ACCEPTED` / `FALLBACK`; errors recorded by the default `_handle_error`. |
| `slo_breached` | W | v1.1.2: latency breach recorded, not raised. |
| `checkpoint_id`, `approved_by` | R/W | v1.2: set on a paused step and by `resume()`. |

### 6.3 Inference payload contract

`process_inference()` never raises a request failure (except `ValueError` for a missing tenant and the re-raised `DehydrationInterrupt` / `CancelledError`); it always returns a payload.

**Table 12 — Inference payload fields**

| Field | Meaning |
|---|---|
| `status` | `SUCCESS` or `FAILED`. |
| `physical_success` | `True` on success; `False` on failure or unparsable structured output. |
| `text` | Model text after egress DLP. |
| `tool_calls`, `action` | Present when the model requested tools (`action = TOOL_CALL_DETECTED`). |
| `structured_output` | Parsed JSON when `response_schema` was requested. |
| `tokens_used`, `metadata` | Provider-reported or estimated usage; `metadata.model_id`. |
| `model_executed`, `cached_kv_hit`, `cached_semantic_hit` | Routing and cache evidence. |
| `dlp_redactions` | Number of values redacted. |
| `error`, `error_type` | On failure. |
| `correlation_id`, `tenant_id` | Echoed propagation fields. |

### 6.4 Tool result contract

Every `BaseToolService.execute()` result carries `physical_success: bool` and `outcome: PhysicalOutcome`. `COMMITTED` when the call succeeded, `NOT_COMMITTED` when it definitively did not, and `UNKNOWN` (`status = 'UNKNOWN'`) when a timeout occurred after the request may have been committed. A commit-gate rejection returns `{'status': 'blocked', 'reason': 'commit_gate'}`. Callers act only on these fields.

### 6.5 Configuration

All keys live in the `E2A_CONFIG` registry (106 keys in 18 groups) and resolve through `resolve_config(key, config, kwargs, scope)` in the order kwargs › config › environment › default. `scope` selects per-class defaults (for example `max_latency` is 2.0 s by default, 5.0 s for `BaseToolService` and 1.0 s for `CQRS`). Projects add keys with `register_config_key()` at start-up; `unknown_config_keys()` guards typos; `config_reference_markdown()` and `env_example()` generate documentation and `.env.example` from the registry. The full generated reference is Appendix A.

Object references are also passed in `config`: `llm_service` (required for any agent that infers), `governance_engine`, `observability_engine`, `idempotency_store`, `cache_store`, `transactional_store`, `pipeline_state_store`.

### 6.6 Request context and accessors (v1.2)

A v1.2 hook declares only what it uses, from this set. The framework passes exactly the declared parameters; declaring one the step does not offer is an error.

**Table 13 — v1.2 hook parameters**

| Parameter | Type | Contents |
|---|---|---|
| `state` | `dict` | The business request, as in v1.1. |
| `ctx` | `RequestContext` (frozen) | `tenant_id`, `correlation_id`, `idempotency_key`, `deadline`, `settings` (every registry key and application key, resolved once through `resolve_config`), `trace`; `ctx.setting(key)`, `ctx.child(...)`, `ctx.expired()`. Created once by `RequestContext.begin()` at the outermost entry point. |
| `io_read` | `ReadAccess` | `call(tool, payload)` for replay-safe tools; `resource(name)` for named resources (retrievers, stores). Refuses a material tool. |
| `io_modify` | `ModifyAccess` | `call(tool, payload, idempotency_key=None)` for material tools; always sends a key (derived from the request when none is given); a transport failure is `UNKNOWN`. Refuses a replay-safe tool. |
| `messages` | `MessageLog` | `info` / `warn` / `error(event, **fields)`. |
| `failed_keys` | `FailedKeys` | `add(key)`. |
| `outcomes` | `list` | The typed results `_act` returned, offered to `_confirm`. |

**Table 14 — What each step offers**

| Hook | Offered |
|---|---|
| `_prepare` | `state`, `ctx`, `io_read`, `messages` |
| `_build_messages` | `state`, `ctx`, `messages` (return `None` for no model step) |
| `_evaluate_output`, `_fallback` | `response` (evaluate only), `state`, `ctx`, `messages` |
| `_act` | `state`, `ctx`, `io_modify`, `messages`, `failed_keys` |
| `_confirm` | `state`, `ctx`, `io_read`, `outcomes`, `messages` |
| `_handle_error` | `error`, `state`, `ctx`, `messages`, `failed_keys` |

An agent's `allowed_tools`, and the governance class's `tool_policy` ({agent: [tools]}), limit both accessors.

## 7. Low-Level Design — Class Model

### 7.1 Module structure

**Table 15 — Modules**

| Module | Contents |
|---|---|
| `reference/e2a/` (v1.2) | The `e2a` package, one module per contract area: `core` (exceptions, `Deadline`, outcomes), `config` (`E2A_CONFIG`, `resolve_config`), `resilience`, `observability`, `governance`, `stages`, `validation`, `llm`, `context`, `agents`, `workflows`, `rag`, `tools`, `ports` (stores, locks, `BaseCheckpointStore`, sweeper, reconciler), `pipeline`, `infra`, `conformance`. Imports between modules are explicit; each public name has one home. |
| `reference/e2a_base.py` | Compatibility module since v1.2: re-exports every name it defined in v1.1.x (the same objects). Until v1.1: the scaffold. Exceptions and concurrency value types; configuration registry; resilience primitives; `_PropagationMixin`; foundation classes (observability, governance, validation); `BaseLLMAgentService`; `BaseAgent`, `BaseWorkflow`, `LLMOnlyAgent`; `BaseRAGPipeline`, `BaseIndexingPipeline`; `BaseToolService`, `RestToolService`, `MCPToolService`, `BaseMCPServer`, tool routing; `BasePromptRegistry`, `BaseMemoryManager`, `BaseSchemaTransformer`, `BaseWebhookDispatcher`; store and lock ports; `BaseOutboxSweeper`, `BaseReconciliationService`; `BasePipeline`; `BaseInfraProvisioner`, `GCPInfraProvisioner`. "Import and subclass. Never modify." |
| `reference/multimodal_orchestrator.py` | Reference `MultiAgentOrchestratorWorkflow` (LangGraph) with `RAGAgent`, `APIToolAgent`, `MCPToolAgent`, `GenericLLMOnlyAgent`, `FallbackAgent`, `ModalityPreprocessor`, `OrchestratorState`. Lives in `src/workflows/`. |
| `reference/e2a_inmemory_adapters.py` | Test doubles: `InMemoryIdempotencyStore`, `InMemoryTransactionalStore`, `InMemoryCacheStore`, `InMemoryLease`. |
| `src/…` (project) | Concrete workflows, agents, pipelines, tool services, inference binding, engines, store adapters, `settings.py` (project config keys). |

### 7.2 Class catalogue

**Table 16 — Classes by layer**

| Class | Status | Role |
|---|---|---|
| **Request path** | | |
| `BaseWorkflow` | Abstract | T2 entry point; origin of propagation; claim, routing, settlement. |
| `BaseAgent` | Abstract | Governance → inference → evaluation → fallback → redaction. |
| `LLMOnlyAgent` | Abstract | Modality-dispatching peer of `BaseAgent` (no tools, no retrieval). |
| `BaseLLMAgentService` | Abstract | T2b inference gateway; only model boundary. |
| `_TenantPermit` | Internal | Per-tenant semaphore entry in the gateway. |
| **Knowledge and integration** | | |
| `BaseRAGPipeline` | Abstract | Hybrid retrieval and rerank (read side). |
| `BaseIndexingPipeline` | Abstract | CDC-fed chunk/embed/write (write side). |
| `BaseToolService` | Abstract | Tool client with resilience and typed outcome. |
| `RestToolService`, `MCPToolService` | Reference | REST and MCP-over-HTTP transports. |
| `BaseMCPServer` | Abstract | Wraps tool services as an MCP server. |
| `_resolve_tool_call()`, `_get_tool_service()` | Function | MCP-first routing; cached tool-service resolution. |
| **Foundation** | | |
| `BaseGovernanceFramework` | Abstract | Admission, budget, dehydration, sandbox, firewall, egress redaction. |
| `BaseObservability` | Abstract | Enrich, derive metrics, ship logs/metrics/traces. |
| `BaseValidationService`, `ValidationResult` | Abstract / value | T1 validation and ABAC entitlement. |
| `OrderOpsValidationService` | Reference | Example validator registry. |
| `BasePromptRegistry`, `BaseMemoryManager`, `BaseSchemaTransformer` | Interface | Prompt store, history pruning, payload upcasting. |
| `BaseWebhookDispatcher` | Abstract | Signed outbound notifications. |
| **Concurrency and resilience** | | |
| `Deadline`, `ClaimStatus`, `ClaimResult`, `CommitResult`, `LeaseHandle`, `PhysicalOutcome` | Value | Shared concurrency vocabulary. |
| `BaseIdempotencyStore`, `BaseTransactionalStore`, `BaseCacheStore`, `BaseDistributedLock` | Abstract port | Claim, conditional commit, single-flight cache, lease. |
| `_Flight`, `_FlightAborted` | Internal | Single-flight bookkeeping. |
| `RetryBudget`, `CircuitBreaker`, `Bulkhead`, `ResiliencePolicy` | Implemented | Per-dependency resilience. |
| `ConcurrencyConflict`, `AdmissionTimeout`, `DeadlineExceeded`, `CircuitOpenError`, `BulkheadFullError` | Exception | Typed outcomes. |
| `DehydrationInterrupt`, `NFRViolationError` | Exception | HITL signal; NFR breach. |
| **Control plane** | | |
| `BaseOutboxSweeper`, `BaseReconciliationService` | Abstract | Leased, scheduled repair. |
| `BasePipeline` | Abstract | CI/CD gates and rollout. |
| `BaseInfraProvisioner`, `GCPInfraProvisioner` | Abstract / reference | Landing-zone provisioning. |
| **Configuration** | | |
| `ConfigKey`, `E2A_CONFIG`, `resolve_config()`, `register_config_key()`, `unknown_config_keys()`, `config_reference_markdown()`, `env_example()`, `validate_capacity()` | Implemented | Central registry and capacity rule. |
| **Shared helpers** | | |
| `_PropagationMixin` | Implemented | io_config, logging, DLQ, commit gate, LLM bridge. |
| `_run_on_bridge_loop()` | Function | One process-wide event loop for sync → async calls. |
| **Reference orchestrator** (`multimodal_orchestrator.py`) | | |
| `MultiAgentOrchestratorWorkflow` | Reference | LangGraph router over four peer agents + fallback. |
| `RAGAgent`, `APIToolAgent`, `MCPToolAgent`, `FallbackAgent` | Reference | `BaseAgent` subclasses. |
| `GenericLLMOnlyAgent`, `ModalityPreprocessor` | Reference | `LLMOnlyAgent` subclass; STT / extraction stubs. |
| `OrchestratorState` | TypedDict | Graph state schema. |
| **Test doubles** | | |
| `InMemoryIdempotencyStore`, `InMemoryTransactionalStore`, `InMemoryCacheStore`, `InMemoryLease` | Test double | Lock-based stand-ins for atomic store operations. |

### 7.3 Request path

UML notation: `+` public, `#` protected, `-` private (name-mangled); *italic* members are abstract.

![Figure 6 — Request path: workflow, agent archetypes and the inference service](images/05a_class_request_path.png)

*Figure 6 — Request path: workflow, agent archetypes and the inference service*

![Figure 7 — BaseLLMAgentService member view](images/06_class_llm_service.png)

*Figure 7 — BaseLLMAgentService member view*

### 7.4 Knowledge and integration tiers

![Figure 8 — RAG, indexing, tools and MCP](images/05b_class_knowledge_tools.png)

*Figure 8 — RAG, indexing, tools and MCP*

### 7.5 Foundation and cross-cutting classes

![Figure 9 — Foundation and cross-cutting classes](images/07_class_foundation.png)

*Figure 9 — Foundation and cross-cutting classes*

### 7.6 Stores, leases and value types

![Figure 10 — Store, lease and value contracts with test doubles](images/08_class_stores.png)

*Figure 10 — Store, lease and value contracts with test doubles*

The value types (`Deadline`, `ClaimStatus`, `ClaimResult`, `CommitResult`, `PhysicalOutcome`) are shared with the CQRS profile and are drawn in full in its design document; their contracts are in Section 8.6.

### 7.7 Resilience primitives

![Figure 11 — Resilience primitives](images/09_class_resilience.png)

*Figure 11 — Resilience primitives*

### 7.8 Control plane

![Figure 12 — Scheduled repair, CI/CD and provisioning](images/10_class_control_plane.png)

*Figure 12 — Scheduled repair, CI/CD and provisioning*

### 7.9 Reference multimodal orchestrator

![Figure 13 — multimodal_orchestrator.py](images/11_class_multimodal.png)

*Figure 13 — multimodal_orchestrator.py*

### 7.10 Configuration and exceptions

![Figure 14 — Central configuration registry](images/12a_class_config.png)

*Figure 14 — Central configuration registry*

![Figure 15 — Exception taxonomy](images/12b_exceptions.png)

*Figure 15 — Exception taxonomy*

## 8. Low-Level Design — Class Contracts

*Kind* in the method tables: **entry** (public entry point), **public** (other public member), **template** (base-owned private step), **hook** (abstract), **default** (protected, overridable), **helper** (inherited utility).

### 8.1 BaseWorkflow

| Aspect | Detail |
|---|---|
| Stereotype | Abstract; Tier 2 lifecycle manager; origin of every propagation field |
| Inherits | `ABC`, `_PropagationMixin` |
| Collaborators | Agent from `_get_agent()`; `idempotency_store`, `observability_engine` via `config`; `Deadline` |
| Invariants | INV-01, INV-03 to INV-06 |

**Table 17 — BaseWorkflow methods**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `execute` | entry | `(state, config=None, **kwargs) → dict` | `tenant_id` required. Mints `correlation_id` (kwargs › state › uuid4), `io_config`, `deadline`, `idempotency_key`; writes the three IDs into `state`. If an idempotency store is bound and `_requires_idempotency_claim()`: `claim()`; non-`CLAIMED` short-circuits. Then `_build_workflow()` → `_validate_workflow()` (else `ValueError`) → `_get_agent(state['intent'])` → `agent.run(...)` with all propagation fields and `deadline`. `DehydrationInterrupt` → `AWAITING_WEBHOOK`; other exceptions → `failed_keys`, `_handle_error()`. `finally`: telemetry (or `_flush_logs`), settle claim, DLQ, write back logs. |
| `_build_workflow` | hook | `(config, **kw)` | Build the orchestration graph (e.g. LangGraph). |
| `_validate_workflow` | hook | `(workflow, config, **kw) → bool` | Check required nodes; `False` aborts with `ValueError`. |
| `_get_agent` | hook | `(intent, config, **kw) → BaseAgent` | Registry lookup by intent; may return any object with the `run()` contract (e.g. `LLMOnlyAgent`). |
| `_generate_idempotency_key` | hook | `(state, config, **kw) → str` | Deterministic key from business content when the client sent none. |
| `_handle_error` | hook | `(error, state, config, **kw)` | Map a failure to the caller's error shape; must not raise. |
| `_requires_idempotency_claim` | default | `(state, config, **kw) → bool` | `True`; override to skip claims for read-only / generative intents. |
| `_request_fingerprint` | default | `(state, config, **kw) → str` | SHA-256 of `state` minus volatile keys (`correlation_id`, `idempotency_key`, `message_log`, `failed_keys`, `deadline`, `status`, `response`, `outcome`, `cumulative_tokens_used`, `trace_context`). |
| `_replayable_result` | default | `(state, config, **kw) → dict` | `response`, `outcome`, `cumulative_tokens_used` — taken after egress redaction. |
| `_physical_outcome` | default | `(state, config, **kw) → PhysicalOutcome \| None` | `state['outcome']`, else `state['response']['outcome']`. |
| `__resolve_deadline` | template | `(config, kwargs) → Deadline \| None` | Pops `deadline`; else `request_deadline_seconds` > 0. |
| `__resolve_idempotency` | template | `(state, config, **kw) → str` | Client key wins; derives only. |
| `__short_circuit` | template | `(state, claim, log, cid, config) → dict` | Map claim status; copy replayable keys on `REPLAY`; flush logs locally; empty `failed_keys`. |
| `__settle_claim` | template | `(store, key, tenant, owner, state, failed_keys, dehydrated, error, config)` | Dehydrated → `fail(retryable=True)`; `UNKNOWN` → park; failures → `fail(retryable = not deterministic)`; else `complete()`. |

**v1.2 — declared steps and resume.** A v1.2 workflow implements `_build_workflow(self, ctx)` and returns `Steps(...)`; `_validate_workflow` and `_get_agent` then have defaults and are not needed.

**Table 18 — BaseWorkflow v1.2 members**

| Member | Kind | Signature → returns | Contract |
|---|---|---|---|
| `Step` | value | `(name, agent, when=None, stop_if=None, material=False)` | One agent run. `when(state)` false → `SKIPPED`; `stop_if(state)` true after the run → the workflow ends. |
| `Route` | value | `(name, by, agents, default=None, when=None, stop_if=None, material=False)` | One agent chosen by `state[by]`; no match and no default → `ValueError`. |
| `Steps` | value | `(*steps)` | Ordered, unique names; `claim_at` = the first material step (else 0). No loops, parallel branches or graphs. |
| `execute` | entry | `(state, config=None, ctx=None, **kw) → dict` | With `Steps`: one context, one deadline, one log; the claim is taken before step `claim_at`, so a read-only request takes none and a repeat of a material one is `REPLAYED` as a whole; a fallback or refusal ends the workflow; `state['steps']` records each step. |
| `resume` | entry | `(checkpoint_id, decision, config=None, tenant_id=None, **kw) → dict` | Needs `config['checkpoint_store']`. Refuses an unknown checkpoint or another tenant's (`UNKNOWN_CHECKPOINT`), a second or concurrent decision (`ALREADY_DECIDED`), a decider `_authorize_decision` rejects (`DECIDER_REFUSED`), each as `ResumeRefused`. Approved → continues at the paused step under the original key; rejected → `status REJECTED`, nothing executed. |
| `_authorize_decision` | default | `(checkpoint, decision, ctx) → bool` | Four eyes: a named decider who is not the requester. |
| `_rehydrate_state` | default | `(checkpoint, decision, ctx) → dict` | The stored state plus `approval_granted`, `approved_by`. |
| `__execute_steps` | template | `(state, config, kwargs, ctx, start_at, fingerprint)` | Runs the steps; on `ApprovalRequired` stores the checkpoint (the state at the pause, after the step's `_prepare`). |

### 8.2 BaseAgent

| Aspect | Detail |
|---|---|
| Stereotype | Abstract; Tier 2 agent |
| Inherits | `ABC`, `_PropagationMixin` |
| Collaborators | `BaseLLMAgentService` via `_infer()`; `BaseGovernanceFramework` via `config['governance_engine']`; tools via `_resolve_tool_call()` from `_apply_policy()`; optionally `BasePromptRegistry` and `BaseMemoryManager` composed into `_build_messages()` |
| Input assumption | `state` already passed `BaseValidationService.validate()`; there is no `_validate_state` hook |

**Table 19 — BaseAgent methods**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `run` | entry | `(state, config=None, correlation_id=None, io_config=None, idempotency_key=None, tenant_id=None, message_log=None, failed_keys=None, **kw) → dict` | Self-originates missing propagation fields (usable standalone). Steps: `_build_messages()`; governance gate if bound else `_apply_policy()`; `_infer()`; accumulate `cumulative_tokens_used`; `_evaluate_output()` < `min_confidence` → `_fallback()`; `redact_response()` if bound; `state['response']`. `DehydrationInterrupt` re-raised; other exceptions → `failed_keys`, `_handle_error()` (not raised). Logs `agent_run_complete`; latency > `max_latency` → **raises** `NFRViolationError`. |
| `_build_messages` | hook | `(state, config, **kw) → list` | Compose model messages (prompt registry, memory pruning, grounding). |
| `_apply_policy` | hook | `(state, config, **kw)` | Fallback policy path when no governance engine is bound; also where a tool call runs (set `state['outcome']`). |
| `_evaluate_output` | hook | `(response, state, config, **kw) → float` | Confidence in [0, 1]. |
| `_fallback` | hook | `(state, config, **kw) → dict` | Degraded response (fallback model, cache). |
| `_handle_error` | hook | `(error, state, config, **kw)` | Record the failure; `failed_keys` is in kwargs. |
| `__track_usage` | template | `(messages, response, config, **kw) → int` | Provider-reported tokens, else ≈ chars / 4. |

**v1.2 — the agent lifecycle (Figure 20).** An agent whose hooks declare `ctx` runs the v1.2 order; one that does not runs exactly as in v1.1, with one `DeprecationWarning` per class.

**Table 20 — BaseAgent v1.2 hooks**

| Hook | Kind | Declares (subset of) | Contract |
|---|---|---|---|
| `_prepare` | default | `state, ctx, io_read, messages` | Read facts or retrieve before the prompt; no side effects. Runs after the `screen` governance stages. |
| `_apply_policy` | default | `state, ctx` | Agent-specific policy (no-op by default); runs after the `decide` stages. |
| `_build_messages` | hook | `state, ctx, messages` | The prompt; `None` means no model step (a deterministic agent). |
| `_evaluate_output` | hook (model step only) | `response, state, ctx, messages` | Confidence in [0, 1]; below `min_confidence` → `_fallback`; records `state['evaluation']`. |
| `_fallback` | hook (model step only) | `state, ctx, messages` | Degraded response; nothing material follows. |
| `_act` | default | `state, ctx, io_modify, messages, failed_keys` | Material effects; only after an accepted evaluation and never after the deadline; returns the typed results. |
| `_confirm` | default | `state, ctx, io_read, outcomes, messages` | Sets `state['outcome']`: `UNKNOWN` › `NOT_COMMITTED` › `COMMITTED`; no outcomes, no verdict. |
| `_handle_error` | default | `error, state, ctx, messages, failed_keys` | Records `state['errors']`; the run does not raise. |

A refused run records `state['refusal']`; a deterministic agent's response is what its steps put in `state['response']`.

### 8.3 LLMOnlyAgent

| Aspect | Detail |
|---|---|
| Stereotype | Abstract; peer of `BaseAgent` (not a subclass) for model-only intents |
| Why separate | Its contract dispatches on modality to a distinct handler before any prompt is built; it still exposes the same `run()` shape so `_get_agent()` can return it. |

**Table 21 — LLMOnlyAgent methods**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `run` | entry | same shape as `BaseAgent.run` | `_detect_modality()` → handler (`TEXT`, `AUDIO`, `IMAGE`, `DOCUMENT`) → `_evaluate_output()` → fallback below `min_confidence` → `state['response']`. Exceptions → `failed_keys`, `_handle_error()`. No governance gate, redaction or latency SLO in the base (Section 14). |
| `_detect_modality` | hook | `(state, config, **kw) → str` | One of the four modality names. |
| `_handle_text` / `_handle_audio` / `_handle_image` / `_handle_document` | hook | `(state, config, **kw) → dict` | Build messages and call `self._infer(messages, config, model_id=self._select_model(m), …)`. Audio/document handlers transcribe/extract first, checking size limits. |
| `_select_model` | default | `(modality, config, **kw) → str` | `model_capability_registry[modality]` › `['default']` › `model_id`. |
| `_evaluate_output`, `_fallback`, `_handle_error` | hook | as `BaseAgent` | — |

### 8.4 BaseLLMAgentService

| Aspect | Detail |
|---|---|
| Stereotype | Abstract; Tier 2b inference microservice; the single model boundary |
| Inherits | `ABC`, `_PropagationMixin` |
| State | Per-instance, in memory: pod semaphore, per-tenant permits (idle-evicted), RPM/TPM buckets (fleet ÷ replicas), `ResiliencePolicy` (circuit, no bulkhead), `stats`, draining flag |
| Subclass duty | Implement the two `_dispatch_upstream_*` hooks for one provider — nothing else is vendor-specific |
| Concurrency | All waits bounded by `min(timeout, deadline)`; permits always released; quota always settled |

**Table 22 — BaseLLMAgentService public and protected members**

| Member | Kind | Signature → returns | Contract |
|---|---|---|---|
| `process_inference` | entry (async) | `(messages, config=None, correlation_id=None, io_config=None, idempotency_key=None, tenant_id=None, message_log=None, failed_keys=None, deadline=None, **kw) → dict` | Fixed lifecycle: draining check → deadline check → sanitise → semantic cache → context budget → KV handle → tool schemas → permits → quota → upstream (circuit, `min(llm_max_latency, deadline)`) → format → usage → DLP → settle quota → store cache. Returns the payload in Table 12. Kwargs `model_id`, `temperature`, `tools`, `stop_sequences`, `response_schema`, `reasoning_budget`, `bypass_cache`, `max_output_tokens` take precedence. |
| `process_inference_stream` | entry (async gen) | same signature → `AsyncGenerator[str]` | Same admission; permit held for the stream; yields DLP-screened chunks; deadline checked per chunk; failure recorded then re-raised; quota settled in `finally`. Consumers must iterate to completion or `aclose()`. |
| `drain` | public (async) | `(timeout=None, config=None) → bool` | Flip readiness, reject new work, wait ≤ `drain_timeout_seconds` for in-flight. |
| `ready`, `inflight` | property | `→ bool`, `→ int` | Readiness signal; current permit holders. |
| `_estimate_tokens` | default | `(message) → int` | ≈ chars / 4 + 4. |
| `_manage_context_budget` | default | `(messages, budget, reasoning_budget, config, **kw) → list` | Pinned (system / priority 0) + active (last turn, priority 1) must fit, else `NFRViolationError`; then newest history first; order preserved. |
| `_apply_kv_caching` | default | `(messages, tenant_id, config, **kw) → str \| None` | Pinned prefix ≥ `min_cache_threshold` → `kvcache/{tenant}/{sha256[:32]}`. |
| `_compile_tool_schemas` | default | `(tools, config, **kw) → list` | Canonical `{name, description, parameters}`; name required. |
| `_check_semantic_cache` / `_store_semantic_cache` | default (async) | `(messages, …, tenant_id, **kw)` | Hooks; default no cache. |
| `_acquire_shared_quota` / `_release_shared_quota` | default (async) | `(tokens, tenant_id, config, **kw)` | Optional fleet-wide quota; default admits. |
| `_format_and_validate_output` | default | `(raw, response_schema, model_id, cached_hit, cid, tenant, config) → dict` | Build payload; tool calls; JSON parse when a schema was requested. |
| `_redact_sensitive` | default | `(text) → (str, int)` | SSN, Luhn-valid card numbers, bearer tokens → `[REDACTED_BY_E2A_DLP]`. |
| `_handle_error` | default | `(error, messages, config, **kw)` | Log `llm_inference_failed`. |
| `_resolve_param` | default | `(key, config, kwargs)` | `resolve_config(…, scope='BaseLLMAgentService')`. |
| `_emit_metric` | default | `(kind, name, value, dimensions)` | No-op hook to forward catalogue metrics. |
| `_dispatch_upstream_call` | hook (async) | `(messages, model_id, temperature, max_tokens, tools, stop_sequences, cache_handle, config, **kw) → dict` | One provider call incl. provider-side cache and fallback cascade; return `{text, metadata.tokens_used}` and/or `{tool_calls}`. |
| `_dispatch_upstream_stream` | hook | `(messages, model_id, temperature, max_tokens, config, **kw) → AsyncGenerator[str]` | Raw provider chunks. |

**Table 23 — BaseLLMAgentService private (framework-owned) steps**

| Step | Contract |
|---|---|
| `__sanitize_prompts` | `user` / `prompt` / `tool` messages matching injection patterns → `ValueError`. |
| `__concurrency_permit` | Async context: tenant permit (if `tenant_concurrency` > 0) then pod permit, total wait ≤ `min(permit_timeout, deadline)`; `AdmissionTimeout` or `DeadlineExceeded`; always released; counts in-flight and peak. |
| `__admit_quota` | Shared-quota hook, then dual token bucket refilled continuously; wait computed from refill rate ≤ `min(quota_timeout, deadline)`; returns `(rpm, tpm)` reserved; returns shared quota on failure. |
| `__settle_quota` | Synchronous (runs on cancellation): RPM returned only if never dispatched; unused TPM refunded (all of it on failure). |
| `__stream_with_dlp` | Hold back `dlp_holdback_chars` so a split value is complete before release. |
| `__track_usage` | Reported tokens, else estimate over prompt + output. |
| `__bounded_acquire`, `__tenant_entry`, `__observe_wait`, `__extras` | Bounded semaphore acquire; tenant entry with idle eviction; wait histograms and slow-wait warnings; kwargs de-duplication for hooks. |

### 8.5 Knowledge tier

#### BaseRAGPipeline

**Table 24 — BaseRAGPipeline methods**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `retrieve` | entry | `(query, config=None, tenant_id=None, **kw) → List[dict]` | Embed → `_search_index(query_text, query_vector, …)` (hybrid) → `_rerank()`. |
| `_search_index` | hook | `(query_text, query_vector, config, tenant_id, **kw)` | Hybrid BM25 + vector in namespace `{tenant_index_prefix}_{tenant_id}`; kwargs `top_k`, `hybrid_alpha`. |
| `_rerank` | hook | `(results, config, **kw)` | kwargs `rerank_top_n`. |
| `_evaluate_answer` | hook | `(answer, config, **kw) → float` | Groundedness / faithfulness score; used by `RAGAgent._evaluate_output()`. |
| `__embed` | template | `(query, config, **kw)` | Uses `embedding_model`; reference stub returns a placeholder vector. |

#### BaseIndexingPipeline

**Table 25 — BaseIndexingPipeline methods**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `ingest` | entry | `(documents, config, correlation_id, io_config, idempotency_key, tenant_id, message_log, failed_keys, **kw) → dict` | `tenant_id` required. Per document: commit gate → `delete` or chunk → embed (count must match) → write. One failure appends the doc id and never blocks the batch. Returns `{indexed, deleted, failed_keys, …}`. |
| `_chunk_documents` | hook | `(document, config, **kw) → [{text, metadata}]` | kwargs `chunk_size`, `chunk_overlap`. |
| `_embed_documents` | hook | `(texts, config, **kw) → List[List[float]]` | Must use the retrieval-side `embedding_model`. |
| `_write_index` | hook | `(chunks, vectors, config, tenant_id, **kw)` | Idempotent on chunk id (CDC is at-least-once). |
| `_delete_from_index` | hook | `(doc_id, config, tenant_id, **kw)` | Remove all chunks of the doc in the tenant namespace. |
| `_handle_error` | hook | `(error, document, config, **kw)` | — |

**v1.2 — RAG ports and a façade.** `BaseEmbeddingModel` (`model_id`, `embed_documents`, `embed_query`) and `BaseVectorIndex` (`upsert`, `delete`, `search`). With `embedding_model` / `vector_index` set, `_embed_query`, `_search_index`, `_embed_documents`, `_write_index` and `_delete_from_index` have working defaults (namespace `f'{namespace_prefix}_{tenant}'`) and are no longer abstract. `BaseRAGPipeline.evaluate_answer(answer, retrieved)` is public and delegates to `_evaluate_answer`. `RAGPipeline(indexing, retrieval)` offers `ingest` / `retrieve` / `evaluate_answer` over the two pipelines and refuses different embedding models. `retrieve(…, ctx=…)` and `ingest(…, ctx=…)` take the tenant from the context and refuse a different `tenant_id`.

### 8.6 Integration tier

#### BaseToolService, RestToolService, MCPToolService

**Table 26 — BaseToolService methods**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `execute` | entry (async) | `(payload, config=None, tenant_id=None, failed_keys=None, **kw) → dict` | `_validate_input()` else `ValueError`; commit gate on `idempotency_key` else `{status: blocked}`; endpoint + payload; `__http_post()`. Result always has `physical_success` and `outcome`. |
| `get_endpoint` | hook (public) | `(config, tenant_id, **kw)` | Rest: `tenant_routing_map[tenant]` › `base_url` › `tool_base_url`. MCP: `mcp_server_url`. |
| `_build_payload` | hook | `(payload, config, **kw)` | Rest: as-is. MCP: JSON-RPC `tools/call` with `mcp_tool_name`. |
| `_validate_input` | hook | `(payload, config, **kw) → bool` | Reference: `required_fields` present. |
| `_resilience_policy` | default | `(config, kwargs) → ResiliencePolicy` | One policy per instance = per dependency. |
| `_send_request` | default (async) | `(endpoint, body, token, timeout, config, **kw) → dict` | Transport hook (aiohttp/httpx in a subclass); reference stub returns success. |
| `__http_post` | template (async) | `(endpoint, body, config, **kw)` | Timeout and retries from config; replay-safe only with an idempotency key or `safe_to_retry`; non-replay-safe calls get zero retries; timeout → `{status: UNKNOWN, outcome: UNKNOWN}`; else sets `outcome` from `physical_success`. |

#### Tool routing functions

| Function | Contract |
|---|---|
| `_resolve_tool_call(tool_name, payload, config, tenant_id, **kw)` | MCP-first: if `mcp_server_registry[tool_name]` exists, call `MCPToolService.execute()` with its URL and tool name; otherwise `_get_tool_service()` and execute. |
| `_get_tool_service(tool_name, config, **kw)` | Transport from kwargs › `tool_registry[tool].transport` › `default_transport`; returns one cached instance per `(transport, tool)` so its resilience state is shared by all calls to that dependency. |

#### BaseMCPServer

**Table 27 — BaseMCPServer methods**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `serve` | entry | `(request, config=None, **kw) → dict` | JSON-RPC dispatch: `tools/list`, `tools/call`, else error −32601. |
| `wrap_api_as_tool` | public | `(handler, tool_name, description, input_schema, config, **kw)` | Register any `BaseToolService` as an MCP tool; no per-tool transport code. |
| `_list_tools` | default | `(config, **kw) → List[dict]` | `{name, description, inputSchema}` per tool; override for per-tenant visibility. |
| `_call_tool` | default (async) | `(tool_name, arguments, config, tenant_id, **kw) → dict` | Delegate to `handler.execute()`; MCP content shape; unknown tool → `isError`. |
| `__dispatch_jsonrpc` | template | `(request, config, **kw)` | Envelope handling. |

**v1.2.** A tool declares `material = True` for an effect (reached only through `io_modify`). `BaseToolService._get_endpoint` is the protected hook; `get_endpoint` stays as a deprecated alias, and a v1.1 subclass that overrides it keeps precedence with one `DeprecationWarning` (conformance rule D1). `execute(…, ctx=…)` takes the tenant and the deadline from the context. `BaseMCPServer.serve_async` exposes the same JSON-RPC surface to async hosts.

### 8.7 BaseGovernanceFramework

| Aspect | Detail |
|---|---|
| Stereotype | Abstract; template method; injected as `config['governance_engine']` |
| Two public methods | `enforce_governance_gate()` before execution; `redact_response()` after a response exists — kept separate because there is nothing to redact pre-execution |

**Table 28 — BaseGovernanceFramework methods**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `enforce_governance_gate` | entry | `(state, io_config, config=None, **kw) → dict` | `_check_execution_capacity()` false → `NFRViolationError`; token budget; dehydration if `requires_human_approval` and not `approval_granted`; sandbox check if `pending_mcp_tool_execution`; semantic firewall → sanitised state. |
| `redact_response` | public | `(state, response, config=None, **kw) → dict` | Egress field masking via `_redact_output_fields()`. |
| `__enforce_token_budget` | template | `(state, io_config)` | `cumulative_tokens_used ≥ max_token_budget` → `ValueError` (see Section 14). |
| `__trigger_dehydration` | template | `(state, config, **kw)` | Persist via `_dehydrate_state_to_perimeter()`, raise `DehydrationInterrupt`. |
| `_check_execution_capacity` | hook | `(state, io_config, config, **kw) → bool` | Consolidated load-shedding / backpressure admission. |
| `_evaluate_dependency_health` | hook | `(config, **kw) → bool` | Health input to capacity checks. |
| `_execute_semantic_firewall` | hook | `(state, config, **kw) → dict` | Ingress PII redaction and injection blocking with guard models. |
| `_redact_output_fields` | hook | `(state, response, config, **kw) → dict` | Egress masking. |
| `_verify_sandbox_profile` | hook | `(config, **kw)` | Assert isolated microVM before an MCP tool call. |
| `_circuit_breaker` | hook | `(failures, config, **kw) → bool` | Circuit transitions and metrics (not invoked by the base). |
| `_dehydrate_state_to_perimeter` | hook | `(state, correlation_id, tenant_id, config)` | Persist resumable state to T3. |

**v1.2 — governance as stages (`e2a.stages`).** A subclass declares `stages`, an ordered list, and optionally `tool_policy`; the v1.1 hooks above are then not used, and are no longer abstract.

**Table 29 — Built-in stages**

| Stage | Phase | Refusal (also the v1.1 exception type) |
|---|---|---|
| `CapacityStage(limiter=InflightLimiter(n), breakers=…)` | screen | `CapacityRefusal` (`NFRViolationError`) |
| `TokenBudgetStage()` — reads `ctx.setting('max_token_budget')` | screen | `TokenBudgetRefusal` (`ValueError`) |
| `SandboxStage()` | screen | `SandboxRefusal` (`PermissionError`) |
| `FirewallStage(injection_patterns, redactions, max_length, fields)` | screen | `FirewallRefusal` (`PermissionError`) |
| `ApprovalStage(required=…)` | decide | `ApprovalRequired` (a `DehydrationInterrupt`): the step pauses |
| `RedactionStage(redactions, fields)` | egress | — (masks the response) |

Every refusal is a `GovernanceRefusal` with `code`, `stage`, `reason` and `retryable`; tool limits raise `ToolRefusal`. `enforce_governance_gate(…, phase='screen' | 'decide')` runs the stages of one phase: `BaseAgent` runs `screen` before `_prepare` (nothing unscreened reaches a retriever) and `decide` after it (approval needs the facts `_prepare` read). Custom stages subclass `IngressStage.check(state, ctx, messages)` or `EgressStage.redact(response, state, ctx, messages)`; `compile_redactions` / `redact_text` load redaction rules from policy files.

### 8.8 BaseObservability

**Table 30 — BaseObservability methods**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `record_telemetry` | entry | `(message_log, correlation_id, config=None, **kw)` | Enrich entries (`correlation_id`, `tenant_id`, `host_epoch_ms`, `framework_version`); derive `total_tokens`, `error_count`; call the three hooks; on failure log locally. Never raises. |
| `__extract_metrics` | template | `(enriched_logs) → dict` | Sum `tokens_used`, count `ERROR`s. |
| `_ship_logs` / `_emit_metrics` / `_export_traces` | hook | `(…, config, **kw)` | Sink, metrics backend, trace export (forward W3C `traceparent`). |
| `_emit_counter` / `_emit_histogram` / `_emit_gauge` | default | `(name, value, dimensions, config, **kw)` | Typed OTel-style emitters; no-op by default. |

### 8.9 BaseValidationService

**Table 31 — BaseValidationService methods**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `validate` | entry | `(agent_name, state, config=None, correlation_id=None, **kw) → ValidationResult` | Earliest point; mints `correlation_id`. No validator → invalid when `strict_mode`. Validator `(state, config, **kw) → (valid, errors)`; invalid → result; valid → `_enforce_entitlement_gate()`. |
| `_resolve_validator` | default | `(agent_name, config, **kw)` | `validator_registry[agent_name]` — one `_validate_<agent_name>` per agent. |
| `_enforce_entitlement_gate` | hook | `(agent_name, state, config, **kw) → ValidationResult` | ABAC on resolved claims; runs only after schema validation succeeded. |
| `__build_result` | template | `(valid, errors, agent_name, cid)` | `ValidationResult` dict (HTTP 400 body). |

`OrderOpsValidationService` is the reference subclass: it registers `_validate_refund_agent` and `_validate_order_ops_agent`.

### 8.10 Other foundation classes

**Table 32 — Interfaces and outbound dispatcher**

| Class | Method | Contract |
|---|---|---|
| `BasePromptRegistry` | `get_prompt(prompt_id, version='latest', tenant_id=None, config=None, **kw)` | Returns an object with `render(**variables) → str`; prompts change by GitOps without a rebuild. |
| `BaseMemoryManager` | `prune(message_history, max_tokens, config=None, **kw) → list` | Strategy is subclass-owned; composed into `_build_messages()`; agentic profile only. |
| `BaseSchemaTransformer` | `upcast(payload, from_version, to_version, config=None, **kw) → dict` | Structural version transform at deserialisation. |
| `BaseWebhookDispatcher` | `dispatch(webhook_url, payload, config=None, correlation_id=None, **kw) → dict` | Sign once, try `_deliver()` up to `max_retries`; returns `DELIVERED` / `FAILED` with attempt count. Hooks: `_sign_payload()`, `_deliver()`. |

### 8.11 Stores, lease and value types

**Table 33 — Store and lease ports**

| Port | Methods | Contract |
|---|---|---|
| `BaseIdempotencyStore` | `claim`, `complete`, `fail`, `check_and_set` | `ABSENT → IN_PROGRESS` is one atomic conditional write, tenant-scoped, with fingerprint. `claim` returns `CLAIMED`, `IN_PROGRESS`, `REPLAY` (stored result), `CONFLICT`, `RECOVERY_REQUIRED` (expired lease or `UNKNOWN`; `recover=True` lets a reconciler take over), `FAILED_FINAL`. `complete`/`fail` are owner-checked. TTL is GC only. |
| `BaseTransactionalStore` | `get`, `put`, `delete`, `get_versioned`, `put_if_version` | `put_if_version` is the authority for competing writers: `COMMITTED` (new version), `CONFLICT` (current version) or `FENCED` (older `fencing_token`). `expected_version=None` = create-if-absent. |
| `BaseCacheStore` | `get`, `set`, `delete`, `get_or_load`, `aget_or_load`, `single_flight_stats` | Per-key single-flight inside a process; followers wait ≤ `min(cache_fill_timeout_seconds, deadline)` then `load` / `stale` / `raise`; loader failure propagates, not cached; cancelled leader degrades followers. |
| `BaseDistributedLock` | `acquire`, `renew`, `release` | Lease (not lock): time-bound, renewable, owner-checked; strictly increasing `fencing_token` per key; release of a lost lease is a no-op. |

**Table 34 — Value types**

| Type | Contract |
|---|---|
| `Deadline` | Absolute monotonic instant. `after(s)`, `from_remaining_ms(ms)`, `coerce(v)` (None, Deadline or seconds), `remaining()`, `expired()`, `bound(timeout)` = min(timeout, remaining), `check(what)` → `DeadlineExceeded`, `to_remaining_ms()`; header `x-e2a-deadline-ms`. Cancellation is not a field — it propagates through the runtime. |
| `ClaimStatus` / `ClaimResult` | Enum above; result carries `status`, stored `result`, `owner`. |
| `CommitResult` | `status`, `version`, `current_version`, `committed`. |
| `LeaseHandle` | `key`, `owner_token`, `fencing_token`, `expires_at` (monotonic). |
| `PhysicalOutcome` | `COMMITTED`, `NOT_COMMITTED`, `UNKNOWN`; `outcome_of(result)` derives it from `outcome` or `physical_success`. |

### 8.12 Resilience primitives

**Table 35 — Resilience classes**

| Class | Members | Contract |
|---|---|---|
| `RetryBudget` | `deposit()`, `withdraw() → bool`, `balance` | Each first attempt earns `ratio` tokens (capped at `cap`, starts at half); each retry spends one — retry amplification ≈ `ratio` of traffic. |
| `CircuitBreaker` | `state`, `allow()`, `record_success()`, `record_failure()` | Closed → open after `failure_threshold` consecutive failures → half-open after `reset_seconds` with exactly one probe. |
| `Bulkhead` | `slot(timeout)` (async ctx), `waiting` | In-flight cap with bounded wait; `BulkheadFullError` on timeout. |
| `ResiliencePolicy` | `call(fn, *, deadline, max_retries=0, idempotent=False, retryable=None)`, `stats` | Deadline check → bulkhead → budget deposit → loop: circuit `allow()` else `CircuitOpenError`; call; success resets circuit; failure (non-deterministic, non-structural) counts; retry only if allowed (Figure 27) and the budget pays; full-jitter backoff never beyond the deadline. |

### 8.13 _PropagationMixin and bridge

**Table 36 — Shared helpers**

| Helper | Signature | Contract |
|---|---|---|
| `_resolve_io_config` | `(config, **kw) → dict` | Config entries with the `io_config_prefix`. |
| `_log` | `(message_log, cid, level, event, **fields)` | Append a structured entry with timestamp and class. |
| `_flush_logs` | `(message_log, config, **kw)` | Local fallback sink. |
| `_send_to_dlq` | `(failed_keys, config, **kw)` | Dispatch to `dlq_queue_url` (scaffold logs). |
| `_commit_gate` | `(key, failed_keys, config, **kw) → bool` | Batch isolation + advisory OCC; disabled by `commit_gate_enabled`. |
| `_resolve_llm_service` | `(config, **kw)` | `llm_service` must be a `BaseLLMAgentService`, else `ValueError`. |
| `_infer_async` | `(messages, config, **kw) → dict` (async) | Await `process_inference`; `FAILED` → `RuntimeError`. |
| `_infer` | `(messages, config, **kw) → dict` | Sync bridge on the shared loop; bounded by `min(llm_max_latency + permit_timeout + quota_timeout + 1, deadline + 0.5)`. |
| `_run_on_bridge_loop` | `(coro, timeout)` | One daemon event loop per process; bounded wait cancels the coroutine (`DeadlineExceeded`), so permits and quota are cleaned up. |

### 8.14 Control plane

**Table 37 — Scheduled repair**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `BaseOutboxSweeper.sweep` | entry | `(config=None, correlation_id=None, **kw) → dict` | Acquire lease `outbox_sweeper` (else `SKIPPED_LOCKED`); renew every `lease_renew_interval_seconds` (lost → `LEASE_LOST`, stop); publish each pending record with the fencing token; `SWEPT` with counts; release in `finally`. |
| `_fetch_pending_records` / `_publish_pending_record` | hook | `(config, **kw)` / `(record, config, fencing_token, **kw) → bool` | Read stuck outbox rows; publish (pass the fencing token to any store write). |
| `BaseReconciliationService.reconcile` | entry | `(config=None, correlation_id=None, **kw) → dict` | Lease `reconciliation_service`; for each DLQ entry: `claim(recover=True)`; `_confirm_physical_outcome()`: `COMMITTED` → complete, `UNKNOWN` → stay parked, else replay once and complete (or `fail(retryable=True)`). |
| `_confirm_physical_outcome` | default | `(entry, config, **kw) → PhysicalOutcome` | Default `UNKNOWN` — safe never-replay default. |
| `_fetch_dlq_entries` / `_replay_dlq_entry` | hook | — | Replay through the same claim and governance path as live traffic. |

**Table 38 — BasePipeline and BaseInfraProvisioner**

| Method | Kind | Signature → returns | Contract |
|---|---|---|---|
| `BasePipeline.run_pipeline` | entry | `(config, correlation_id, message_log, failed_keys, **kw) → dict` | Seven gates in order (Figure 33); first failure → `status FAILED`, never raised; report includes stages, `capacity_violations`, `rag_eval_score`, deployment metadata. |
| `_validate_capacity_budget` | default | `(config, **kw) → List[str]` | `validate_capacity()`; override to add rules. |
| `_run_static_security_scans`, `_execute_test_suite`, `_verify_scaffold_contracts`, `_run_rag_eval`, `_compile_artifact`, `_run_dynamic_security_checks`, `_execute_deployment_rollout` | hook | — | SAST/secrets; tests; single-entry-point and no private-override check; RAGAS score; build and push one image; DAST; blue-green or canary. |
| `BaseInfraProvisioner.provision_landing_zone` | entry | `(tenant_id, tenancy_model, availability_tier='STANDARD', config, correlation_id, **kw) → dict` | `POOL` or `SILO`; `STANDARD` / `HIGH` / `CRITICAL`. Seven steps (Figure 34) into a manifest; any failure → `status FAILED`, `error_trace`; never raises. |
| `_define_*` (5), `_apply_infrastructure_graph` | hook | — | Networks (AZ/region count honoured), edge, data perimeter (per tenancy), compute, IAM per tier; apply via Terraform / Pulumi / CDK. |
| `_generate_saga_orchestrator` | default | `(compute_ctx, config, **kw) → dict` | AWS States Language skeleton; override syntax per cloud. |

`GCPInfraProvisioner` is the reference implementation: per-tier service accounts (T1 publish-only), POOL vs SILO Firestore databases, GCP Workflows saga.

### 8.15 Configuration functions

**Table 39 — Configuration API**

| Function | Contract |
|---|---|
| `resolve_config(key, config=None, kwargs=None, scope=None)` | Unregistered key → `KeyError`. kwargs › config › env (cast) › scoped default › default. |
| `register_config_key(spec)` | Idempotent for an identical spec; conflicting duplicate → `ValueError`. |
| `unknown_config_keys(config)` | Keys neither registered nor `io_`-prefixed. |
| `config_reference_markdown()`, `env_example()` | Generated docs and `.env.example` (secrets emitted empty). |
| `validate_capacity(config, **kw) → List[str]` | Violations of: `worker_concurrency ≤ db_pool_size`; `pod_concurrency ≤ http_pool_size`; `tenant_concurrency ≤ pod_concurrency`; `worker_prefetch ≥ worker_concurrency`; `replicas × db_pool_size ≤ db_max_connections`; `replicas × pod_concurrency × (1 + retry_budget_ratio) ≤ model_max_inflight`; lease renew < TTL; drain < termination grace. |

### 8.16 Reference orchestrator (multimodal_orchestrator.py)

**Table 40 — Reference orchestrator classes**

| Class | Behaviour |
|---|---|
| `MultiAgentOrchestratorWorkflow` | `BaseWorkflow` subclass. `agent_registry`: `RAG_GROUNDED` → `RAGAgent`, `MCP_TOOL_CALL` → `MCPToolAgent`, `API_TOOL_CALL` → `APIToolAgent`, `LLM_ONLY` → `GenericLLMOnlyAgent`; unknown intent or score below `min_confidence` (0.85) → `FallbackAgent`. `_build_workflow()` compiles a LangGraph `StateGraph` (classify → route → agent → END); `_validate_workflow()` checks six required nodes. Claims only tool intents above the threshold; key = SHA-256 of tenant and query. |
| `RAGAgent` | Grounds the prompt with `rag_pipeline.retrieve()`; confidence = `rag_pipeline._evaluate_answer()`; fallback reason `faithfulness_threshold`. |
| `APIToolAgent` / `MCPToolAgent` | `_apply_policy()` resolves the tool with transport pinned (`http` / `mcp`), runs it on the bridge loop (`_run_tool`, bounded by deadline or `timeout × (retries + 1)`), sets `state['outcome']`; confidence = `physical_success`. |
| `GenericLLMOnlyAgent` | Modality from `audio_uri` / `image_uri` / `document_uri`, else text; audio and documents through `ModalityPreprocessor` (stubs in the reference); image reference passed directly. |
| `FallbackAgent` | Confidence always 0 → degraded answer with reason `no_intent_match`. |
| `ModalityPreprocessor` | `transcribe()` and `extract()` with pre-flight duration/page checks; reference raises `NotImplementedError`. |

### 8.17 Test doubles

`InMemoryIdempotencyStore` implements the full claim state machine per `(tenant, key)` with a lock standing in for the conditional write, plus `state_of()` for tests. `InMemoryTransactionalStore` keeps versioned rows and rejects stale fencing tokens. `InMemoryCacheStore` is a dict. `InMemoryLease` issues owner tokens and strictly increasing fencing tokens with expiry on an injectable clock.

### 8.18 Conformance kit (v1.2)

`e2a.conformance` checks the Governed Entry Point principle statically. The rule sets are read from the e2a classes, and subclasses are followed across files.

**Table 41 — Conformance rules**

| Rule | Level | Finding |
|---|---|---|
| R1 | error | A subclass overrides a framework-owned public method or a framework-private (`__`) method. |
| R2 | warning (error with `--strict`; the default from v1.3) | Code outside a class calls that class's protected hook. |
| R3 | info | An extra public method: allowed if it is a capability (its own caller or trigger, its own guarantees). Framework data attributes (`stages`, `tool_policy`) are not findings. |
| D1 | warning | A subclass overrides a public method that became a hook in v1.2 (`get_endpoint`). |

`python -m e2a.conformance src/ [--strict] [--quiet]` exits 1 on an error. `AgentContract` is a pytest kit: `run()` never raises on a hook error; a model step below `min_confidence` falls back and commits nothing; nothing material after the deadline; the request tenant reaches the result. `BasePipeline._verify_scaffold_contracts` runs the rules over `conformance_paths` (findings in `self.conformance_findings`) and no longer requires one public method per class.

## 9. Dynamic Behaviour

### 9.1 Workflow execution

![Figure 16 — BaseWorkflow.execute(): propagation, claim, routing, settlement](images/13_seq_workflow.png)

*Figure 16 — BaseWorkflow.execute(): propagation, claim, routing, settlement*

The claim precedes everything else, so a duplicate costs one store round trip and no model, tool or governance spend. A short-circuit returns before the `try` block: it flushes logs locally but does not call the observability engine or the DLQ.

![Figure 17 — Claim settlement in the workflow finally block](images/25_flow_settlement.png)

*Figure 17 — Claim settlement in the workflow finally block*

![Figure 18 — Idempotency record life cycle](images/29_state_claim.png)

*Figure 18 — Idempotency record life cycle*

### 9.2 Agent run

![Figure 19 — BaseAgent.run(): governance, inference, evaluation, egress redaction](images/14_seq_agent_run.png)

*Figure 19 — BaseAgent.run(): governance, inference, evaluation, egress redaction*

Ordering matters: messages are built before the governance gate (so the firewall sees the request state the prompt is built from), redaction happens after the fallback decision (so a fallback answer is also masked), and the stored replay result is taken from the redacted state.

**v1.2.** Figure 20 shows the v1.2 order: screening before reading, the decision stages after it, effects only after an accepted evaluation.

![Figure 20 — BaseAgent.run() in v1.2: screen, prepare, decide, model step, act, confirm](images/31_v12_agent_run.png)

*Figure 20 — BaseAgent.run() in v1.2: screen, prepare, decide, model step, act, confirm*

### 9.3 Human-in-the-loop dehydration

![Figure 21 — Dehydration and resume](images/21_seq_dehydration.png)

*Figure 21 — Dehydration and resume*

**v1.2.** With declared steps, `ApprovalStage` pauses the step and the workflow stores a checkpoint holding the state at the pause, so the approver sees what the decision is about; `resume()` continues at that step (Figure 22). A v1.1 workflow that pauses also gets a checkpoint and is resumed by running it again, approved.

![Figure 22 — Declared steps, one claim, a pause and resume()](images/32_v12_workflow_steps.png)

*Figure 22 — Declared steps, one claim, a pause and resume()*

### 9.4 Inference gateway

![Figure 23 — process_inference(): the admission pipeline](images/15_seq_inference.png)

*Figure 23 — process_inference(): the admission pipeline*

![Figure 24 — process_inference_stream(): hold-back DLP](images/16_seq_stream.png)

*Figure 24 — process_inference_stream(): hold-back DLP*

![Figure 25 — Inference service life cycle and graceful shutdown](images/23_state_llm_lifecycle.png)

*Figure 25 — Inference service life cycle and graceful shutdown*

### 9.5 Tool calls and resilience

![Figure 26 — Tool call: MCP-first routing, resilience and typed outcome](images/17_seq_tool_call.png)

*Figure 26 — Tool call: MCP-first routing, resilience and typed outcome*

![Figure 27 — ResiliencePolicy retry decision](images/24_flow_retry.png)

*Figure 27 — ResiliencePolicy retry decision*

![Figure 28 — CircuitBreaker state machine](images/22_state_circuit.png)

*Figure 28 — CircuitBreaker state machine*

### 9.6 MCP server

![Figure 29 — Wrapping an existing API as an MCP tool](images/18_seq_mcp_server.png)

*Figure 29 — Wrapping an existing API as an MCP tool*

### 9.7 Knowledge tier

![Figure 30 — CDC-fed indexing and hybrid retrieval](images/19_seq_rag_indexing.png)

*Figure 30 — CDC-fed indexing and hybrid retrieval*

### 9.8 Reference multimodal orchestration

![Figure 31 — Intent routing to four peer agents](images/20_seq_multimodal.png)

*Figure 31 — Intent routing to four peer agents*

Two routing paths exist in the reference: the compiled LangGraph (`classify_intent` → `_route_intent` → agent node) and `BaseWorkflow.execute()`'s own `_get_agent()` call. Both read the same registry and threshold so they cannot diverge; `execute()` builds and validates the graph but routes through `_get_agent()`, passing full propagation fields to `run()`.

### 9.9 Scheduled repair

![Figure 32 — Leased reconciliation](images/30_seq_reconcile.png)

*Figure 32 — Leased reconciliation*

### 9.10 Delivery and provisioning

![Figure 33 — BasePipeline.run_pipeline() gates](images/26_flow_pipeline.png)

*Figure 33 — BasePipeline.run_pipeline() gates*

![Figure 34 — provision_landing_zone() steps](images/27_flow_provision.png)

*Figure 34 — provision_landing_zone() steps*

## 10. Concurrency, Consistency and Failure Model

### 10.1 Ownership of concurrency risks

**Table 42 — Risk ownership**

| Risk | Owner | Mechanism |
|---|---|---|
| Duplicate material request | `BaseWorkflow` + `BaseIdempotencyStore` | Atomic tenant-scoped claim, fingerprint, replay of redacted result. |
| Lost update | `BaseTransactionalStore.put_if_version` | Compare-and-set; typed `CONFLICT` / `FENCED`. |
| Stale writer after lease loss | `LeaseHandle.fencing_token` + store | Store rejects older tokens. |
| Model overload / noisy tenant | `BaseLLMAgentService` | Tenant permit, pod semaphore, RPM/TPM bucket, bounded waits. |
| Slow dependency | `ResiliencePolicy` per dependency | Bulkhead, circuit, retry budget. |
| Retry storms | `RetryBudget` | Retries ≈ `retry_budget_ratio` of traffic. |
| Ambiguous write | `__http_post` + `PhysicalOutcome.UNKNOWN` | Never re-sent; parked; reconciler confirms. |
| Cache stampede | `BaseCacheStore.get_or_load` | Per-key single-flight. |
| Orphaned work after timeout | `_run_on_bridge_loop`, `__concurrency_permit`, `__settle_quota` | Cancellation releases permits and refunds quota. |
| Pool and connection exhaustion | `validate_capacity()` | Checked in the scaffold-compliance gate. |
| Duplicate scheduled jobs | `BaseDistributedLock` | One leader per lease; renew < TTL. |
| Deploy-time kill of in-flight work | `drain()` + `drain_timeout_seconds` | Drain shorter than termination grace. |

### 10.2 Error handling at each boundary

**Table 43 — How each public method treats failures**

| Boundary | Behaviour |
|---|---|
| `BaseValidationService.validate` | Returns a `ValidationResult`; never raises for invalid input. |
| `BaseWorkflow.execute` | Raises only `ValueError` for a missing tenant. `DehydrationInterrupt` → status; other exceptions → `failed_keys`, `_handle_error`, DLQ; claim settled. |
| `BaseAgent.run` | Re-raises `DehydrationInterrupt`; swallows other exceptions into `failed_keys`; raises `NFRViolationError` on latency breach. |
| `LLMOnlyAgent.run` | Swallows all exceptions into `failed_keys`. |
| `process_inference` | Returns `FAILED` payloads; re-raises `DehydrationInterrupt` and `CancelledError` after refunding quota; raises `ValueError` for a missing tenant. |
| `process_inference_stream` | Records then re-raises every failure. |
| `BaseToolService.execute` | Raises `ValueError` on invalid input; returns `blocked`, `UNKNOWN` or a typed result; resilience errors propagate. |
| `BaseIndexingPipeline.ingest` | Per-document isolation; never raises for a document failure. |
| Sweeper, reconciler, pipeline, provisioner | Return a status report; never raise past the boundary (sweeper/reconciler propagate hook exceptions after releasing the lease). |

### 10.3 Consistency model

- **Material effects:** effectively-once per tenant-scoped key, settled from physical evidence.
- **Generative reads** (RAG, LLM-only, fallback in the reference orchestrator): not claimed, never replayed from a stored answer.
- **Index:** eventually consistent with the system of record through CDC; writes idempotent per chunk.
- **Caches:** advisory; freshness by explicit expiry field; tenant-scoped keys.
- **Quota:** per-pod and approximate (estimate reserved, actual refunded); fleet-wide accuracy only with the shared-quota hook.

## 11. Non-Functional Design

### 11.1 Capacity sizing

Little's Law sizes every pool: in-flight = arrival rate × mean service time. For the inference tier, `pod_concurrency` per pod times replicas must stay below the provider's concurrent ceiling including retry amplification — exactly the equation in `validate_capacity()`. Autoscalers raise capacity only up to what the next tier can absorb (`maxReplicaCount × per-pod cap` ≤ downstream pool, quota or connection limit). Scale signals are in-flight requests and queue backlog. Baseline planning load from the landing zone: 20,000 daily active tenants × 5,000 operations ≈ 1,157 RPS average, ≈ 3,472 RPS design peak.

### 11.2 Latency budgets

**Table 44 — In-code latency and wait bounds**

| Bound | Default | Where |
|---|---|---|
| `max_latency` | 2.0 s (tools 5.0 s, CQRS 1.0 s) | End of `BaseAgent.run()` — `NFRViolationError`. |
| `llm_max_latency` | 3.0 s | Upstream model call, capped by the deadline. |
| `permit_timeout` / `quota_timeout` | 5.0 s / 5.0 s | Inference admission waits. |
| `timeout` / `retries` (tools) | 5.0 s / 3 | Per attempt; retries only when replay-safe. |
| `bulkhead_acquire_timeout` | 1.0 s | Dependency slot wait. |
| `cache_fill_timeout_seconds` | 5.0 s | Single-flight follower wait. |
| `request_deadline_seconds` | 0 (none) | Origin of the request deadline. |
| `drain_timeout_seconds` | 20 s | Must be below `termination_grace_seconds` (30 s). |

### 11.3 Security and governance controls

**Table 45 — Controls by construction**

| Control | Where |
|---|---|
| Tenant identity | Verified JWT at T0; `tenant_id` mandatory at `execute()`, `process_inference()`, `ingest()`. |
| Entitlements (ABAC) | `_enforce_entitlement_gate()` at T1 after schema validation. |
| Admission and load shedding | `_check_execution_capacity()` in the governance gate; gateway rate limits; inference permits. |
| Prompt injection | Edge WAF rules; `__sanitize_prompts` in Tier 2b; `_execute_semantic_firewall()` guard models. |
| Data loss prevention | `_redact_sensitive` on output and streams (hold-back); `redact_response()` field masking. |
| Model egress | Only Tier 2b has provider credentials and a route to model endpoints. |
| Tool sandboxing | `_verify_sandbox_profile()` before MCP tool execution. |
| Human approval | Dehydration with state persisted in the perimeter. |
| Tenant scoping | Claims, permits, KV handles, cache keyspaces and `{prefix}_{tenant}` index namespaces. |
| Least privilege | One IAM role per tier (`_define_iam_governance_framework`); T1 publish-only. |
| Supply chain | `BasePipeline` static scans, DAST, contract verification. |

Policy-as-code derived from the abstract contracts (landing zone governance matrix): deny inference pods without autoscaler and semaphore settings; deny direct egress from compute pods to public model APIs; require tenant-tagged cache and KV handles; deny model credentials to any role except the inference gateway; deny promotion of a deployment that violates the capacity equations.

### 11.4 Observability

Every class logs structured events to the request's `message_log`; `BaseWorkflow` hands it once to `record_telemetry()`. The metric catalogue (`E2A_METRICS`) defines names, types and labels once; `tenant_id` is a high-cardinality label to aggregate or sample.

**Table 46 — Key log events**

| Event | Emitter | Meaning |
|---|---|---|
| `idempotent_short_circuit` | `BaseWorkflow` | Duplicate, in-progress or conflicting request. |
| `workflow_dehydrated` | `BaseWorkflow` | Awaiting approval. |
| `agent_run_complete` | `BaseAgent` | Latency and cumulative tokens. |
| `llm_semantic_cache_hit` | `BaseLLMAgentService` | Served from semantic cache. |
| `llm_inference_complete` / `llm_stream_complete` | `BaseLLMAgentService` | Latency, tokens, KV hit, status / characters. |
| `llm_inference_failed` | `BaseLLMAgentService` | Error type. |
| `llm_slow_wait` (WARN) | `BaseLLMAgentService` | Permit/quota wait above `slow_wait_threshold_seconds`. |
| `indexing_complete` | `BaseIndexingPipeline` | Indexed, deleted, failed counts. |
| `*_passed`, `pipeline_failed` | `BasePipeline` | Gate results. |

The full metric catalogue is Appendix B.

## 12. Extension Guide

### 12.1 Building a service

1. **Bind an inference provider.** Subclass `BaseLLMAgentService`; implement `_dispatch_upstream_call` and `_dispatch_upstream_stream` (map canonical tool declarations to the provider format, create provider-side caches for `cache_handle`). Optionally implement the semantic-cache and shared-quota hooks and `_emit_metric`. Deploy as its own Tier 2b service.
2. **Implement validators** on a `BaseValidationService` subclass (`_validate_<agent_name>` per agent, `_enforce_entitlement_gate`).
3. **Implement agents.** `BaseAgent` subclasses for prompt + tool flows; `LLMOnlyAgent` subclasses for model-only, modality-aware flows; reuse the reference agents where generic.
4. **Implement knowledge and tools.** `BaseRAGPipeline` + `BaseIndexingPipeline` with the same embedding model and tenant namespace; tools through `RestToolService` / `MCPToolService` or a custom `_send_request`; wrap internal APIs with `BaseMCPServer`.
5. **Implement the workflow.** Graph, validation, agent registry, deterministic idempotency key, error mapping; override `_requires_idempotency_claim` for read-only intents. v1.2: declare `Steps(...)` instead, mark the material steps, and set `checkpoint_store` if a step can pause.
9. **(v1.2) Check conformance** in CI: `python -m e2a.conformance src/ --strict`, and an `AgentContract` test per agent.
6. **Bind engines and ports** in `config`: `llm_service`, `governance_engine`, `observability_engine`, `idempotency_store`, and store adapters.
7. **Register project config keys** with `register_config_key()` in `src/settings.py`; run `unknown_config_keys()` at start-up.
8. **Wire the control plane:** sweeper and reconciler with a lease adapter, a `BasePipeline` subclass, and a `BaseInfraProvisioner` per cloud.

### 12.2 Skeletons

```python
class ProviderLLMService(BaseLLMAgentService):
    async def _dispatch_upstream_call(self, messages, model_id, temperature, max_tokens,
                                      tools, stop_sequences, cache_handle, config, **kw):
        ...  # provider SDK call -> {'text': ..., 'metadata': {'tokens_used': n}} or {'tool_calls': [...]}
    async def _dispatch_upstream_stream(self, messages, model_id, temperature, max_tokens, config, **kw):
        async for chunk in ...:  # provider stream
            yield chunk

class RefundAgent(BaseAgent):
    def _build_messages(self, state, config=None, **kw):  ...  # prompt registry + memory pruning
    def _apply_policy(self, state, config=None, **kw):    ...  # optional tool call; set state['outcome']
    def _evaluate_output(self, response, state, config=None, **kw): return 1.0 if response.get('physical_success') else 0.0
    def _fallback(self, state, config=None, **kw):        return {'text': '', 'metadata': {'degraded': True}}
    def _handle_error(self, error, state, config=None, **kw): ...

class RefundWorkflow(BaseWorkflow):
    def _build_workflow(self, config=None, **kw):           ...
    def _validate_workflow(self, wf, config=None, **kw):    return True
    def _get_agent(self, intent, config=None, **kw):        return RefundAgent()
    def _generate_idempotency_key(self, state, config=None, **kw): return f"{state['tenant_id']}:{state['order_id']}"
    def _handle_error(self, error, state, config=None, **kw): ...

result = RefundWorkflow().execute(state, {'llm_service': ProviderLLMService(), 'idempotency_store': store,
                                          'governance_engine': gov, 'observability_engine': obs})
```

The same service in the v1.2 style (`from e2a import ...`): each hook declares only what it uses.

```python
class RefundAgent(BaseAgent):
    def _prepare(self, state, ctx, io_read):
        state['order'] = io_read.call('get_order', {'order_id': state['order_id']})
    def _build_messages(self, state, ctx):
        return None                                   # deterministic: no model step
    def _act(self, state, ctx, io_modify):
        return [io_modify.call('issue_refund', {'order_id': state['order_id']})]

class RefundGovernance(BaseGovernanceFramework):
    stages = [CapacityStage(InflightLimiter(50)), FirewallStage(), ApprovalStage()]
    tool_policy = {'RefundAgent': ['get_order', 'issue_refund']}

class RefundWorkflow(BaseWorkflow):
    def _build_workflow(self, ctx):
        return Steps(Step('refund', RefundAgent(), material=True))
    def _generate_idempotency_key(self, state, ctx):
        return f"{ctx.tenant_id}:{state['order_id']}"
    def _handle_error(self, error, state, ctx):
        state['error_code'] = type(error).__name__
```

### 12.3 Rules and anti-patterns

**Table 47 — Do and do not**

| Do | Do not |
|---|---|
| Reach the model only through `_infer()` / `config['llm_service']`. | Import a provider SDK in an agent or workflow. |
| Override protected hooks only. | Override `execute()`, `run()`, `process_inference()` or any `__private` step. |
| Pass request data as arguments. | Store `correlation_id`, `tenant_id` or keys on `self`. |
| Set `state['outcome']` from the tool's typed result. | Infer success from model text. |
| Give writes an idempotency key so retries are safe. | Retry a timed-out write without a key. |
| Raise `ValueError` / `TypeError` / `PermissionError` for permanent errors. | Use generic exceptions for validation failures. |
| Keep one tool-service instance per dependency (`_get_tool_service`). | Create a new tool service (and resilience policy) per call. |
| Tenant-scope every cache, index and handle key. | Share a global namespace across tenants. |
| Register config keys centrally. | Read `os.getenv()` in subclasses or edit the scaffold. |
| (v1.2) Declare only the hook parameters you use; read settings with `ctx.setting()`. | Use `**kwargs` in a v1.2 hook, or call another class's protected hook (R2). |
| (v1.2) Mark material tools and steps. | Perform an effect in `_prepare` or through `io_read`. |

## 13. Verification

The scaffold ships executable smoke tests against the in-memory doubles. At baseline 32c99c0 all pass: `smoke_test_concurrency.py` (48 checks), `smoke_test_llm_service.py` (37), `smoke_test_multimodal_concurrency.py` (6), and the `multimodal_orchestrator.py` self-test (six routing cases through the unmodified `BaseWorkflow.execute()`).

**Table 48 — Selected evidence**

| ID | Scenario | Evidence |
|---|---|---|
| T-01 | 20 concurrent claimers; replay; conflict; tenant scoping | `smoke_test_concurrency.py` |
| — | Workflow: duplicate replays, agent ran once; changed payload → `CONFLICT` | `smoke_test_concurrency.py` |
| T-12 | `UNKNOWN` parks; duplicate not re-executed; reconciler confirms then replays once | `smoke_test_concurrency.py` |
| T-02 / T-09 | One same-version writer commits; stale owner rejected as `FENCED` | `smoke_test_concurrency.py` |
| T-03 | 30 async / 20 threaded concurrent misses → one load; loader failure not cached | `smoke_test_concurrency.py` |
| T-04 | Permit wait bounded; noisy tenant cannot starve a quiet one; idle tenant entries evicted | `smoke_test_concurrency.py` |
| T-10 | Deadline bounds upstream call; cancellation releases permits and refunds TPM | `smoke_test_concurrency.py` |
| T-11 | Drain: in-flight finishes, new work rejected, readiness flips | `smoke_test_concurrency.py` |
| T-06 / T-05 | Retry amplification bounded by budget; circuit opens; bulkhead rejects within bound | `smoke_test_concurrency.py` |
| T-12 | Timed-out non-idempotent write → `UNKNOWN`, sent once | `smoke_test_concurrency.py` |
| — | `validate_capacity()` and pipeline capacity gate | `smoke_test_concurrency.py` |
| — | Inference: tenant required, injection blocked, quota refund, latency SLO, pod and tenant caps, RPM exhaustion, context budget, KV handle, DLP incl. split-card streaming, semantic cache, tool schema | `smoke_test_llm_service.py` |
| — | Multimodal: request deadline bounds a tool call below the tool timeout | `smoke_test_multimodal_concurrency.py` |

**v1.2.0:** `pytest tests` runs every smoke script above against the installed package, plus 51 tests of the v1.2 contracts: `test_contract.py` (14, context and accessors, the agent order), `test_governance_stages.py` (9), `test_entry_points.py` (9), `test_workflow_steps.py` (9), `test_conformance.py` (5) and `test_package.py` (5, every v1.1 name importable, one home per public name). All 67 pass (the multimodal orchestrator self-test needs `langgraph`; without it 66 pass and that one is skipped). CI also runs `python -m e2a.conformance --strict` over the framework and reference code. The O2C migration (39 tests and the three-service landscape demo) is the integration proof.

Recommended additions from Section 14: MCP `tools/call` returns a resolved result; token budget enforcement through `io_config`; agent-internal deterministic error settles `FAILED_FINAL`; latency breach after a committed side effect does not re-execute.

## 14. Implementation Reality, Gaps and Design Observations

These were found by reading the reference against this design. O-1 to O-4 were reproduced against the in-memory adapters at baseline 32c99c0. None changes a public signature. The status of the ones fixed since is given in the last column; O-14 to O-17 come from v1.2.

**Table 49 — Design observations**

| ID | Observation | Effect | Recommendation |
|---|---|---|---|
| O-1 | `BaseMCPServer.__dispatch_jsonrpc()` calls the async `_call_tool()` without awaiting it. | `tools/call` returns a coroutine object in `result` (reproduced); the wrapped tool never runs. | Make `serve()` async and await, or run `_call_tool()` on the bridge loop.  **Status:** Resolved in v1.1.2 (E-2): `serve()` completes the call; v1.2 adds `serve_async`. |
| O-2 | `__enforce_token_budget` reads `max_token_budget` from `io_config`, but `_resolve_io_config()` keeps only `io_`-prefixed keys. | The budget resolves to its default (infinity); token budgets are never enforced (reproduced). When the key is present it raises `ValueError`, not the documented `NFRViolationError`. | Read `io_max_token_budget` (or resolve from `config`) and raise `NFRViolationError`.  **Status:** Resolved in v1.1.2 (E-1); v1.2 `TokenBudgetStage` reads the budget from the request context. |
| O-3 | `BaseAgent.run()` swallows exceptions into `failed_keys`; `BaseWorkflow.__settle_claim()` then sees no workflow-level error. | Deterministic errors inside the agent (validation, permission) settle as `FAILED_RETRYABLE`, so redeliveries retry them (reproduced). | Carry the agent's error type in `state` (e.g. `state['error_type']`) and use it in settlement. |
| O-4 | `BaseAgent.run()` raises the latency SLO after the response and any tool side effect. | A slow run whose tool committed is dead-lettered and settled retryable; a redelivery executes the side effect again (reproduced). | Record the breach without raising once `outcome == COMMITTED`, or settle by outcome before failures.  **Status:** Resolved in v1.1.2 (E-3): the breach is recorded in `state['slo_breached']`, not raised. |
| O-5 | `LLMOnlyAgent.run()` has no governance gate, no `redact_response()` and no latency SLO. | Model-only requests bypass the semantic firewall and egress masking (the gateway's own sanitiser and DLP still apply). | Mirror the `BaseAgent.run()` governance wiring. |
| O-6 | `_resolve_tool_call()` creates a new `MCPToolService()` per call on the MCP path. | Each MCP call gets a fresh, empty circuit and retry budget, defeating per-dependency resilience. | Use the cached `_get_tool_service(tool, transport='mcp')`. |
| O-7 | `BaseRAGPipeline.retrieve()` enforces no latency SLO, does not require `tenant_id`, and `__embed` is a placeholder. | Docs reference a retrieval SLO; an absent tenant reaches `_search_index`. | Add the SLO and tenant check; make embedding a hook.  **Status:** Partly resolved: `_embed_query` is a hook (v1.1.2); v1.2 adds the embedding and index ports and takes the tenant from `ctx`. The retrieval SLO is still open. |
| O-8 | `BaseToolService.execute()` does not apply `max_latency` (scoped 5.0 s). | Only per-attempt timeouts bound tool calls. | Apply the scoped SLO or document it as per-attempt only. |
| O-9 | Several registry keys are not read by base classes: `approved_agents`, `approved_tools`, `approved_models`, `max_tokens`, `shadow_mode_enabled`, `shadow_target_pipeline`, `idempotency_store_url`, `rag_top_k`, `hybrid_alpha`, `tool_filter`, `mcp_protocol_version`; some `used_by` texts name retired methods (`__lookup_idempotency_store`). | Allow-lists and shadow mode look enforced but are subclass responsibilities. | Mark them "for subclass use" in the registry or wire them in. |
| O-10 | `BaseWorkflow` short-circuit flushes logs locally but does not call `observability_engine`. | Replays and conflicts are invisible in the telemetry backend. | Ship telemetry on the short-circuit path. |
| O-11 | `_circuit_breaker()` is abstract on governance but never called; `ResiliencePolicy` is the active circuit. | Two circuit concepts. | Document `_circuit_breaker` as a metrics/policy hook or remove it. |
| O-12 | `BaseWebhookDispatcher.dispatch()` retries immediately without backoff or deadline. | Hot retry loop against a failing receiver. | Route through `ResiliencePolicy`. |
| O-13 | In the reference orchestrator `_route_intent`/graph nodes call `agent.run(state)` without propagation fields; `execute()` routes via `_get_agent()` instead. | The compiled graph is validated but not executed by `execute()`. | Document the graph as the routing specification, or invoke it with propagation fields. |
| O-14 | v1.2 keeps the v1.1 hook signatures and `get_endpoint` as deprecated adapters. | Two styles coexist; each v1.1 class warns once. | Migrate (the O2C migration is the worked example); v1.3 removes the adapters. |
| O-15 | Conformance rule R2 is a warning by default. | A hook called from outside its class is reported but not blocking. | Run `--strict` in CI now; it becomes the default in v1.3. |
| O-16 | The v1.2 contracts exist for Python only. | Java services (A2C generates them) stay on the v1.1 shape. | `e2a-java` (deferred wave D). |
| O-17 | `Steps` has no loops, parallel branches or graphs. | A graph-shaped workflow keeps a v1.1 `_build_workflow`, or composes several workflows. | Intended: declared steps cover the projects so far; revisit on evidence. |

## 15. What v1.2 Changes

**Table 50 — E2A v1.2.0 at a glance**

| Area | v1.1 | v1.2 | Where |
|---|---|---|---|
| Packaging | One scaffold file, `e2a_base.py` | The `e2a` package, one module per contract area; `e2a_base` re-exports every v1.1 name | 7.1 |
| Entry points | "One public method per class" | Governed entry points: a public method per genuine capability; adds `resume`, `serve_async`, `evaluate_answer` | 4.1, 8.18 |
| Request data | Keyword arguments on every hook, `**kwargs` | One frozen `RequestContext`; hooks declare only what they use (`ctx`, `io_read`, `io_modify`, …) | 6.6 |
| Agent order | build messages → gate → model → evaluate → redact | screen → `_prepare` → decide → `_apply_policy` → model step (optional) → evaluate → `_act` → `_confirm` → redact | 8.2, 9.2 |
| Effects | A tool call anywhere, usually in `_apply_policy` | Only `_act` through `io_modify`, with a key, after an accepted evaluation and before the deadline | 6.6, 8.6 |
| Governance | Abstract hooks on one class | Ordered stages, typed refusals, `tool_policy` | 8.7 |
| Workflows | `_build_workflow` + `_get_agent(intent)` | `Steps` / `Step` / `Route`, one claim, `state['steps']`, `resume()` | 8.1, 9.3 |
| Knowledge | Abstract embedding and index hooks | `BaseEmbeddingModel`, `BaseVectorIndex`, `RAGPipeline` façade | 8.5 |
| Contract checks | None (one-public-method check in the pipeline) | `e2a.conformance` (R1–R3, D1), `AgentContract` | 8.18, 13 |
| Defaults | Most hooks abstract | Safe defaults where a hook is optional | 1.3 |

**Compatibility.** Every v1.1 name imports from `e2a_base`; a v1.1 subclass runs unchanged with one `DeprecationWarning` per class; a v1.1 workflow that pauses gets a checkpoint. v1.3 removes the v1.1 adapters and makes R2 an error. The unmigrated O2C code (v1.1.2 style) passes its 39 tests on v1.2.

### 15.1 v1.2.2: a release gate every project can reuse, and e2a-java

**Table 51 — E2A v1.2.2 additions**

| Addition | What it is | Design |
|---|---|---|
| `e2a.release.ProjectReleaseGate` | A `BasePipeline` whose generic stages work as they are (secret scan, `pytest`, the capacity equation and strict `e2a.conformance`, `compileall`); the domain stages are one hook per case: `_score_case` over `evals/golden.json`, held to `faithfulness_threshold`, and `_probe` over `evals/redteam.json`. A missing set is `NOT_CONFIGURED`, never a silent pass. `python -m e2a.release`. | Note C-04 (D-1) |
| e2a-java (`java/`) | The same contracts for Java 21, released with E2A's tag: `e2a-core` (request context, idempotency, accessors, stages, the v1.2 agent lifecycle, declared workflows with one claim), `e2a-cqrs` (the Deterministic CQRS profile), `e2a-spring-jpa` (a JPA idempotency store), `e2a-test` (`AgentContract`). In Java the compiler enforces R1 and R2: entry points are `final`, hooks `protected`. | Note D-01 (G-4) |

The proofs:
- O2C's release gate shrank from 134 to 70 lines on `ProjectReleaseGate`.
- settlement's audit service gained a gate. Its seven evaluation cases must each be right, so its threshold is 1.0.
- settlement-service moved onto e2a-java: its own `cqrs/` package and `E2ABase.java` were deleted, 611 lines net.

### 15.2 v1.3.0: the v1.1 agent hooks and get_endpoint are removed

As announced in v1.2.0 and prepared in v1.2.3 (design note V13-01):
- **A v1.1 agent hook is refused when its class is defined:** a hook with the keyword signature `config=None, **kwargs` and no `ctx` raises a `TypeError` naming the hook. The same holds for a v1.1 workflow hook in a workflow of declared steps.
- **`get_endpoint` is gone.** A tool implements `_get_endpoint`, and a subclass that still defines `get_endpoint` is refused.
- **`e2a.conformance` is strict by default:** R2 and D1 are errors; `--lenient` reports them while migrating.
- **What stays:** `e2a_base` (a re-export), the v1.1 workflow form (a graph and `_get_agent`, never deprecated), the v1.1 governance hooks of an engine without stages, and the mirrored `state['tenant_id']`.
- **What changes for this document:** where it describes a v1.1 hook signature (Sections 8 and 12), read the v1.2 signature of Table 13 instead.

## Appendix A. Configuration Reference (generated from E2A_CONFIG)

Resolution order for every key: call-time kwargs › config dict › environment variable › scaffold default (scoped defaults in parentheses). Object references (`llm_service`, engines, stores) are passed in `config` only.

**Table C-1 — Common**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `io_config_prefix` | `IO_CONFIG_PREFIX` | 'io_' | _resolve_io_config() |
| `log_sink` | `LOG_SINK` | 'stdout' | BaseObservability._ship_logs() / _flush_logs() |
| `dlq_queue_url` | `DLQ_QUEUE_URL` | (unset) | _send_to_dlq() |
| `commit_gate_enabled` | `COMMIT_GATE_ENABLED` | True | _commit_gate() |
| `expected_version` | — | (unset) | _commit_gate() — optional OCC version, caller-supplied per call |
| `idempotency_store_url` | `IDEMPOTENCY_STORE_URL` | (unset) | BaseWorkflow.__resolve_idempotency() |
| `idempotency_ttl_seconds` | `IDEMPOTENCY_TTL_SECONDS` | 86400 | __lookup_idempotency_store() |
| `tenant_routing_map` | `TENANT_ROUTING_MAP` | {} | get_endpoint() — per-tenant overrides |

**Table C-2 — Engines (object references)**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `observability_engine` | — | (unset) | BaseWorkflow.execute() — BaseObservability instance |
| `governance_engine` | — | (unset) | BaseAgent.run() — BaseGovernanceFramework instance |
| `llm_service` | — | (unset) | BaseAgent / LLMOnlyAgent _infer() — the bound BaseLLMAgentService (required) |
| `max_token_budget` | — | inf | BaseGovernanceFramework.__enforce_token_budget() — io_config key |
| `idempotency_store` | — | (unset) | BaseWorkflow.execute() / BaseOrchestrator.execute() / AIDLCPipelineOrchestrator — the bound BaseIdempotencyStore (atomic claim) |
| `cache_store` | — | (unset) | BaseQueryService.fetch() — the bound BaseCacheStore (single-flight get_or_load) |
| `transactional_store` | — | (unset) | BaseCommandService._commit_versioned() — the bound BaseTransactionalStore (conditional commit) |
| `pipeline_state_store` | — | (unset) | AIDLCPipelineOrchestrator — the bound BaseTransactionalStore (durable phase state) |

**Table C-3 — Validation**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `schema_registry` | `VALIDATION_SCHEMA_REGISTRY_PATH` | {} | BaseValidationService validators |
| `strict_mode` | `VALIDATION_STRICT_MODE` | True | BaseValidationService.validate() |

**Table C-4 — Agents**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `min_confidence` | `MIN_CONFIDENCE` | 0.85 | BaseAgent.run(), LLMOnlyAgent.run() — fallback threshold |
| `max_latency` | `MAX_LATENCY` | 2.0 (5.0 for BaseToolService, 1.0 for CQRS) | BaseAgent.run(), BaseRAGPipeline.retrieve(), BaseToolService.execute() — latency SLO, NFRViolationError on breach |
| `max_tokens` | `MAX_TOKENS` | 5000 | BaseAgent.run() — token budget per call/workflow |
| `prompt_template` | `PROMPT_TEMPLATE` | (unset) | BaseAgent._build_messages() — default prompt template when no registry is used |
| `cache_refresh_token` | — | 'v1' | BaseAgent._build_messages() — mutate to force a context-cache miss |

**Table C-5 — Governance**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `approved_agents` | `APPROVED_AGENTS` | [] | BaseAgent.run() — agent_name must be listed |
| `approved_models` | `APPROVED_MODELS` | [] | BaseRAGPipeline — embedding / reranker model allow-list |
| `approved_tools` | `APPROVED_TOOLS` | [] | BaseToolService — tool_name must be listed |

**Table C-6 — Multimodal**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `model_capability_registry` | `MODEL_CAPABILITY_REGISTRY_PATH` | {} | LLMOnlyAgent._select_model() |
| `stt_provider` | `STT_PROVIDER` | (unset) | ModalityPreprocessor.transcribe() |
| `document_extractor` | `DOCUMENT_EXTRACTOR_PROVIDER` | (unset) | ModalityPreprocessor.extract() |
| `max_audio_duration_seconds` | `MAX_AUDIO_DURATION_SECONDS` | 600 | checked before transcribe() — NFRViolationError on breach |
| `max_document_pages` | `MAX_DOCUMENT_PAGES` | 50 | checked before extract() — NFRViolationError on breach |

**Table C-7 — LLM service**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `model_id` | `LLM_MODEL_ID` | 'default' | _dispatch_upstream_call() — provider routing; LLMOnlyAgent._select_model() default |
| `temperature` | `LLM_TEMPERATURE` | 0.0 | _dispatch_upstream_call() |
| `context_budget` | `LLM_CONTEXT_BUDGET` | 8192 | _manage_context_budget() — input token budget |
| `max_output_tokens` | `LLM_MAX_OUTPUT_TOKENS` | 2048 | quota estimate and upstream limit |
| `llm_max_latency` | `LLM_MAX_LATENCY` | 3.0 | upstream call deadline — NFRViolationError on breach |
| `pod_concurrency` | `LLM_POD_CONCURRENCY` | 32 | pod semaphore — in-flight upstream calls |
| `tenant_concurrency` | `LLM_TENANT_CONCURRENCY` | 0 | per-tenant cap within the pod (0 = off) |
| `llm_replica_count` | `LLM_REPLICA_COUNT` | 1 | divides max_rpm / max_tpm into a per-pod share |
| `max_rpm` | `LLM_MAX_RPM` | 600 | fleet requests-per-minute limit |
| `max_tpm` | `LLM_MAX_TPM` | 300000 | fleet tokens-per-minute limit |
| `quota_timeout` | `LLM_QUOTA_TIMEOUT` | 5.0 | max wait for quota before NFRViolationError |
| `enable_context_caching` | `LLM_ENABLE_CONTEXT_CACHING` | True | _apply_kv_caching() |
| `min_cache_threshold` | `LLM_MIN_CACHE_THRESHOLD` | 1024 | _apply_kv_caching() — minimum static prefix tokens |
| `enable_semantic_cache` | `LLM_ENABLE_SEMANTIC_CACHE` | True | _check_semantic_cache() / _store_semantic_cache() |
| `dlp_enabled` | `LLM_DLP_ENABLED` | True | egress and stream redaction |
| `dlp_holdback_chars` | `LLM_DLP_HOLDBACK_CHARS` | 128 | stream holdback window |
| `speculative_model` | `SPECULATIVE_MODEL` | (unset) | _dispatch_upstream_call() io_config — speculative-decoding draft model |
| `max_speculative_tokens` | `MAX_SPECULATIVE_TOKENS` | (unset) | _dispatch_upstream_call() io_config — draft length |
| `permit_timeout` | `LLM_PERMIT_TIMEOUT` | 5.0 | bounded wait for the tenant + pod permit — AdmissionTimeout on expiry (also capped by the remaining deadline) |
| `tenant_idle_evict_seconds` | `LLM_TENANT_IDLE_EVICT_SECONDS` | 300.0 | idle per-tenant permit entries are evicted after this long |

**Table C-8 — RAG**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `faithfulness_threshold` | `FAITHFULNESS_THRESHOLD` | 0.85 | BaseRAGPipeline._evaluate_answer(); BasePipeline RAG eval gate |
| `rag_top_k` | `RAG_TOP_K` | 5 | BaseRAGPipeline.retrieve() — result count |
| `embedding_model` | `EMBEDDING_MODEL` | 'amazon.titan-embed-text-v1' | BaseRAGPipeline.__embed() |
| `hybrid_alpha` | `HYBRID_ALPHA` | 0.5 | BaseRAGPipeline._search_index() — vector vs keyword weight |
| `similarity_metric` | `SIMILARITY_METRIC` | 'COSINE' | BaseRAGPipeline._search_index() io_config |
| `index_name` | `INDEX_NAME` | (unset) | BaseRAGPipeline._search_index() — index / collection |
| `tenant_index_prefix` | `TENANT_INDEX_PREFIX` | (unset) | BaseRAGPipeline._search_index() — f"{prefix}_{tenant_id}" namespace |
| `rerank_top_n` | `RERANK_TOP_N` | (unset) | BaseRAGPipeline._rerank() |

**Table C-9 — Tools / MCP**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `timeout` | `TOOL_TIMEOUT` | 5.0 | BaseToolService.__http_post() |
| `retries` | `TOOL_RETRIES` | 3 | BaseToolService.execute() |
| `tool_base_url` | `TOOL_BASE_URL` | 'http://localhost' | RestToolService.get_endpoint() |
| `auth_token` | `TOOL_AUTH_TOKEN` | (unset) | BaseToolService.__http_post() |
| `mcp_server_url` | `MCP_SERVER_URL` | (unset) | MCPToolService.get_endpoint() |
| `mcp_protocol_version` | `MCP_PROTOCOL_VERSION` | '2025-06-18' | BaseMCPServer.serve() |
| `mcp_server_name` | `MCP_SERVER_NAME` | 'e2a-mcp-server' | BaseMCPServer — logging identity |
| `tool_filter` | `MCP_TOOL_FILTER` | (unset) | BaseMCPServer — exposed-tool allow-list |
| `mcp_server_registry` | `MCP_SERVER_REGISTRY_PATH` | {} | _resolve_tool_call() — checked first |
| `tool_registry` | `TOOL_REGISTRY_PATH` | {} | _get_tool_service() — fallback path |
| `default_transport` | `DEFAULT_TOOL_TRANSPORT` | 'http' | _get_tool_service() — final fallback |
| `max_retries` | `WEBHOOK_MAX_RETRIES` | 3 | BaseWebhookDispatcher.dispatch() |
| `signing_secret` | `WEBHOOK_SIGNING_SECRET` | (unset) | BaseWebhookDispatcher._sign_payload() |

**Table C-10 — Workflow / Pipeline**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `shadow_mode_enabled` | `SHADOW_MODE_ENABLED` | False | BaseWorkflow.execute() — Blue-Green / shadow trials |
| `shadow_target_pipeline` | `SHADOW_TARGET_PIPELINE` | (unset) | BaseWorkflow.execute() — shadow target |
| `prompt_store_url` | `PROMPT_STORE_URL` | (unset) | BasePromptRegistry.get_prompt() |
| `default_version` | `PROMPT_DEFAULT_VERSION` | 'latest' | BasePromptRegistry.get_prompt() |
| `deployment_strategy` | `DEPLOYMENT_STRATEGY` | 'BLUE_GREEN' | BasePipeline._execute_deployment_rollout() |

**Table C-11 — Deadlines**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `request_deadline_seconds` | `REQUEST_DEADLINE_SECONDS` | 0.0 | BaseWorkflow.execute() / BaseOrchestrator.execute() — origin of the additive deadline field (0 = none) |

**Table C-12 — Resilience**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `retry_base_backoff` | `RETRY_BASE_BACKOFF` | 0.1 | ResiliencePolicy — full-jitter exponential backoff base (seconds) |
| `retry_max_backoff` | `RETRY_MAX_BACKOFF` | 2.0 | ResiliencePolicy — backoff ceiling (seconds) |
| `retry_budget_ratio` | `RETRY_BUDGET_RATIO` | 0.2 | ResiliencePolicy — retry tokens earned per first attempt (caps retry amplification) |
| `retry_budget_cap` | `RETRY_BUDGET_CAP` | 10.0 | ResiliencePolicy — maximum retry-token balance |
| `circuit_failure_threshold` | `CIRCUIT_FAILURE_THRESHOLD` | 5 | ResiliencePolicy — consecutive failures that open the circuit |
| `circuit_reset_seconds` | `CIRCUIT_RESET_SECONDS` | 30.0 | ResiliencePolicy — open duration before a half-open probe |
| `bulkhead_max_concurrent` | `BULKHEAD_MAX_CONCURRENT` | 16 | ResiliencePolicy — per-dependency in-flight cap (0 = off) |
| `bulkhead_acquire_timeout` | `BULKHEAD_ACQUIRE_TIMEOUT` | 1.0 | ResiliencePolicy — bounded wait for a bulkhead slot |

**Table C-13 — Pools / capacity**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `db_pool_size` | `DB_POOL_SIZE` | 20 | store adapters — per-instance database connection pool |
| `http_pool_size` | `HTTP_POOL_SIZE` | 50 | tool / upstream adapters — per-instance HTTP connection pool |
| `retrieval_pool_size` | `RETRIEVAL_POOL_SIZE` | 20 | BaseRAGPipeline._search_index() adapters — per-instance index connection pool |
| `fleet_replica_count` | `FLEET_REPLICA_COUNT` | 1 | validate_capacity() — replicas of this service (multiplies per-instance concurrency) |
| `db_max_connections` | `DB_MAX_CONNECTIONS` | 0 | validate_capacity() — database connection ceiling for the whole fleet (0 = unchecked) |
| `model_max_inflight` | `MODEL_MAX_INFLIGHT` | 0 | validate_capacity() — provider-side concurrent request ceiling for the whole fleet (0 = unchecked) |
| `termination_grace_seconds` | `TERMINATION_GRACE_SECONDS` | 30.0 | validate_capacity() — platform SIGTERM-to-SIGKILL window; drain_timeout_seconds must be shorter |

**Table C-14 — Async workers**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `worker_concurrency` | `WORKER_CONCURRENCY` | 8 | consumer runtime — in-flight messages per instance |
| `worker_prefetch` | `WORKER_PREFETCH` | 16 | consumer runtime — broker prefetch / max in-flight window per instance |
| `drain_timeout_seconds` | `DRAIN_TIMEOUT_SECONDS` | 20.0 | consumer runtime / services — graceful-shutdown drain bound |

**Table C-15 — Leases**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `lease_ttl_seconds` | `LEASE_TTL_SECONDS` | 60 | BaseOutboxSweeper / BaseReconciliationService — BaseDistributedLock lease TTL |
| `lease_renew_interval_seconds` | `LEASE_RENEW_INTERVAL_SECONDS` | 20 | BaseOutboxSweeper / BaseReconciliationService — renew cadence (must be < lease_ttl_seconds) |
| `fencing_enabled` | `FENCING_ENABLED` | True | lease holders pass LeaseHandle.fencing_token to downstream commits |
| `idempotency_lease_seconds` | `IDEMPOTENCY_LEASE_SECONDS` | 300 | BaseIdempotencyStore.claim() — IN_PROGRESS claim expiry; an expired claim needs reconciliation, never blind re-execution |

**Table C-16 — Cache**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `cache_fill_timeout_seconds` | `CACHE_FILL_TIMEOUT_SECONDS` | 5.0 | BaseCacheStore.get_or_load() — follower wait for the in-flight fill |
| `cache_fill_on_timeout` | `CACHE_FILL_ON_TIMEOUT` | 'load' | BaseCacheStore.get_or_load() — follower policy on fill timeout: load \| stale \| raise |

**Table C-17 — Build (AI-DLC)**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `build_max_inflight` | `BUILD_MAX_INFLIGHT` | 4 | AIDLCPipelineOrchestrator — concurrent builds per instance |
| `tenant_build_max_inflight` | `TENANT_BUILD_MAX_INFLIGHT` | 2 | AIDLCPipelineOrchestrator — concurrent builds per tenant |

**Table C-18 — Concurrency observability**

| Key | Environment variable | Default | Used by |
|---|---|---|---|
| `slow_wait_threshold_seconds` | `SLOW_WAIT_THRESHOLD_SECONDS` | 0.5 | permit / pool / bulkhead waits above this are logged at WARN |
| `queue_age_slo_seconds` | `QUEUE_AGE_SLO_SECONDS` | 60.0 | oldest-message age above this is an SLO breach signal |

## Appendix B. Metric Catalogue (generated from E2A_METRICS)

**Table M-1 — Metric catalogue (E2A_METRICS)**

| Metric | Type | Unit | Labels |
|---|---|---|---|
| `e2a.llm.inflight` | gauge | requests | pod |
| `e2a.llm.permit_wait_seconds` | histogram | s | tenant_id |
| `e2a.llm.quota_wait_seconds` | histogram | s | tenant_id |
| `e2a.admission.rejected_total` | counter | 1 | reason |
| `e2a.retry.attempts_total` | counter | 1 | dependency |
| `e2a.retry.budget_exhausted_total` | counter | 1 | dependency |
| `e2a.circuit.state` | gauge | 0=closed,1=half,2=open | dependency |
| `e2a.bulkhead.wait_seconds` | histogram | s | dependency |
| `e2a.occ.conflicts_total` | counter | 1 | store |
| `e2a.idempotency.replays_total` | counter | 1 | status |
| `e2a.lease.lost_total` | counter | 1 | lease |
| `e2a.fencing.rejections_total` | counter | 1 | store |
| `e2a.cache.single_flight_followers_total` | counter | 1 | cache |
| `e2a.queue.depth` | gauge | messages | queue |
| `e2a.queue.oldest_age_seconds` | gauge | s | queue |
| `e2a.cancellation.cleanup_total` | counter | 1 | owner |

## Appendix C. Glossary and Traceability

**Table G-1 — Glossary**

| Term | Definition |
|---|---|
| Scaffold | The base classes a project subclasses and never modifies: the `e2a` package since v1.2 (`e2a_base` is its compatibility module); `e2a_base.py` until v1.1. |
| Request context (`ctx`) | v1.2: the frozen identity and settings of one request (`RequestContext`), created once at the outermost entry point. |
| `io_read` / `io_modify` | v1.2: the accessors for replay-safe and material tools; a hook receives one only if it declares it. |
| Stage | v1.2: one governance check in an ordered list (`screen`, `decide` or egress); a failure is a typed `GovernanceRefusal`. |
| Steps / Step / Route | v1.2: a workflow's declared agent runs, under one context and one claim. |
| Checkpoint | v1.2: the stored state of a paused step, decided once through `resume()`. |
| Governed entry point | v1.2 principle: a public method per genuine capability, delegating to hooks; checked by `e2a.conformance`. |
| Propagation fields | Request-scoped arguments threaded through every class: `correlation_id`, `tenant_id`, `idempotency_key`, `io_config`, `message_log`, `failed_keys`, plus `deadline`, `trace_context`, `expected_version`, `fencing_token`. |
| Inference gateway (Tier 2b) | The deployed `BaseLLMAgentService` binding; the only model egress. |
| Physical outcome | Whether a material effect committed in the system of record: `COMMITTED`, `NOT_COMMITTED`, `UNKNOWN`. |
| Logical vs physical rule | Model narrative is never ground truth; only `physical_success` / `outcome` are. |
| Claim | Atomic tenant-scoped ownership of an idempotency key. |
| Dehydration | Persisting state and pausing (compute can scale to zero) until an approval webhook. |
| Single-flight | One loader call per cache key for concurrent misses. |
| Lease / fencing token | Time-bound renewable ownership; a monotonic token that lets a store reject a stale owner. |
| Retry budget | Token bucket that caps retries to a fraction of first attempts. |
| Bulkhead | Per-dependency in-flight cap with a bounded wait. |
| Structural isomorphism | One-to-one mapping between class boundaries and landing-zone tiers. |

**Table G-2 — Traceability: goal or invariant → class → code anchor (`reference/e2a/` at v1.2.0)**

| Goal / invariant | Class · method | Anchor |
|---|---|---|
| G-1, INV-02 | `_PropagationMixin._infer` → `BaseLLMAgentService.process_inference` | `_propagation.py` L98, `llm.py` L117 |
| G-2, INV-03/04 | `BaseWorkflow.execute`, `__settle_claim`, `_physical_outcome` | `workflows.py` L131, L535, L336 |
| INV-05 | `__settle_claim` UNKNOWN branch; `BaseReconciliationService.reconcile` | `workflows.py` L535, `ports.py` L496 |
| INV-06 | `BaseAgent.run` / `BaseWorkflow.execute` dehydration branches | `agents.py` L35, `workflows.py` L131 |
| G-3, INV-07 | `Deadline`; `__concurrency_permit`; `__admit_quota`; `ResiliencePolicy.call`; `_run_on_bridge_loop` | `core.py` L64, `llm.py` L538, L576, `resilience.py` L192, L38 |
| INV-08 | `__concurrency_permit` finally; `__settle_quota` | `llm.py` L538, L617 |
| INV-09, G-6 | `BaseToolService.__http_post`; `ResiliencePolicy.__attempts` | `tools.py` L98, `resilience.py` L203 |
| G-4, INV-11 | `__tenant_entry`; `_apply_kv_caching`; `_search_index` contract | `llm.py` L527, L359, `rag.py` L89 |
| G-5, INV-10 | `enforce_governance_gate`, `redact_response`; `__sanitize_prompts`; `_redact_sensitive` | `governance.py` L45, L71, `llm.py` L462, L426 |
| G-9 | `E2A_CONFIG`, `resolve_config`, `register_config_key` | `config.py` L92, L231, L220 |
| INV-12 | `validate_capacity`; `BasePipeline._validate_capacity_budget` | `resilience.py` L242, `pipeline.py` L116 |
| INV-13 | `BaseDistributedLock`; `BaseOutboxSweeper.sweep` | `ports.py` L322, L433 |
| INV-14, INV-15 | `RequestContext`; `ReadAccess`; `ModifyAccess` | `context.py` L37, L190, L198 |
| INV-16 | `IngressStage`, `FirewallStage`; `enforce_governance_gate(phase=…)` | `stages.py` L61, L181, `governance.py` L45 |
| INV-17, UC-14/15 | `Steps`; `resume`; `__execute_steps`; `BaseCheckpointStore` | `workflows.py` L91, L256, L376, `ports.py` L300 |
| UC-16 | `check_paths`; `AgentContract`; `_verify_scaffold_contracts` | `conformance.py` L109, L231, `pipeline.py` L133 |
| T1 edge | `BaseValidationService.validate` | `validation.py` L39 |
| Knowledge tier | `BaseRAGPipeline.retrieve`; `BaseIndexingPipeline.ingest`; `RAGPipeline` | `rag.py` L67, L132, L204 |
| Integration tier | `BaseToolService.execute`, `_get_endpoint`; `_resolve_tool_call`; `BaseMCPServer.serve`, `serve_async` | `tools.py` L32, L70, L261, L185, L197 |
| Landing zone | `BaseInfraProvisioner.provision_landing_zone` | `infra.py` L32 |
| Reference orchestrator | `MultiAgentOrchestratorWorkflow` | `multimodal_orchestrator.py` L274 |
