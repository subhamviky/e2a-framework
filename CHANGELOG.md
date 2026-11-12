# Changelog

All notable changes to the E2A framework documents and reference code.

## v1.2.0 — in progress (release branch `release/v1.2.0`)

Design: the v1.2 design notes in the o2c-platform-hub repository (roadmap/design/).

### Step 1 — Package structure and test suite (no behaviour change)
- **`e2a` package (E-12):** the framework is split from the 3,300-line `e2a_base.py` into one module per contract area: `e2a.core`, `config`, `resilience`, `observability`, `governance`, `validation`, `llm`, `agents`, `workflows`, `rag`, `tools`, `ports`, `pipeline`, `infra`. The code moved without changes; imports between modules are explicit.
- **`e2a_base` is a compatibility module:** it re-exports every name it defined in v1.1.x (the same objects), and the standard-library names that `from e2a_base import *` used to provide.
- **pytest suite (E-14):** `pytest tests` runs every reference smoke script against the installed package, plus package-layout tests (all v1.1 names importable, one home per public name, every module importable on its own). CI runs it.
- `smoke_test_llm_service.py`: the "no `os.getenv` outside the registry" check now covers every module of the package.

### Step 5 — Workflow steps and resume (design note 05 §1, note 01 E-17)
- **`Steps`, `Step`, `Route`:** a v1.2 workflow (`_build_workflow(self, ctx)`) declares its agents in order, with `when` (skipped and recorded), `stop_if` and `material`. All steps run under one context, one deadline, one log and **one idempotency claim**, taken before the first material step (before the first step when none is marked), so a read-only request takes no claim and a repeat of a material one is REPLAYED as a whole. Each step is recorded in `state['steps']` (status DONE / SKIPPED / FALLBACK / REFUSED / PAUSED, latency); a fallback or a refusal ends the workflow. No loops, parallel branches or graphs.
- **`BaseWorkflow.resume(checkpoint_id, decision, config, tenant_id=…)`:** a paused step stores a checkpoint (`config['checkpoint_store']`, new port `BaseCheckpointStore`, `InMemoryCheckpointStore`). `resume()` refuses an unknown checkpoint or another tenant's, a second or concurrent decision, and a decider `_authorize_decision` rejects (default: four eyes), each as `ResumeRefused` with its `code`. An approval continues **at the paused step** under the original idempotency key; a rejection executes nothing. New protected hooks: `_authorize_decision`, `_rehydrate_state`. A v1.1 workflow that pauses gets a checkpoint too and is resumed by running it again, approved.
- **Defaults instead of abstract hooks:** `BaseWorkflow._validate_workflow` / `_get_agent` (not needed with Steps); `BaseAgent._apply_policy` (no-op), `_handle_error` (records `state['errors']`), `_evaluate_output` / `_fallback` (required only when the agent has a model step). A v2 agent records `state['evaluation']` (ACCEPTED / FALLBACK).
- The default request fingerprint ignores workflow bookkeeping and the approval decision, so an approved resume is the same request, not a CONFLICT.

### Step 4 — Entry points (design note 01, principle E-15: one governed pathway, several public capabilities where they are real)
- **RAG ports:** `BaseEmbeddingModel` (`model_id`, `embed_documents`, `embed_query`) and `BaseVectorIndex` (`upsert`, `delete`, `search`). With `embedding_model` / `vector_index` set, `BaseRAGPipeline` and `BaseIndexingPipeline` have working defaults for `_embed_query`, `_search_index`, `_embed_documents`, `_write_index` and `_delete_from_index` (namespace `f'{namespace_prefix}_{tenant}'`); those hooks are no longer abstract.
- **`BaseRAGPipeline.evaluate_answer(answer, retrieved)`:** public, delegates to `_evaluate_answer`; agents and release gates no longer call the protected hook.
- **`RAGPipeline` façade:** `ingest` / `retrieve` / `evaluate_answer` over the two pipelines, which stay separate classes; a different embedding model on either side is refused at construction.
- **`retrieve(…, ctx=…)` and `ingest(…, ctx=…)`:** the tenant (and correlation) come from the request context; a different `tenant_id` keyword is refused.
- **`BaseMCPServer.serve_async`:** the same JSON-RPC surface for async hosts; awaits the tool on the running loop.
- **`BaseToolService._get_endpoint`:** the endpoint is a protected hook; `get_endpoint` stays as a deprecated alias. A v1.1 subclass that overrides `get_endpoint` keeps precedence (also over `RestToolService`'s own `_get_endpoint`), with one `DeprecationWarning`.

### Step 3 — Governance as stages (design note 04)
- **`e2a.stages`:** `IngressStage` / `EgressStage` and the built-in stages `CapacityStage` (with `InflightLimiter` and dependency breakers), `TokenBudgetStage` (reads `ctx.setting('max_token_budget')`), `ApprovalStage` (raises `ApprovalRequired`, a `DehydrationInterrupt`), `SandboxStage`, `FirewallStage` (injection patterns, maximum length, redaction of state fields) and `RedactionStage` (egress); `compile_redactions` / `redact_text` for policy files.
- **Typed refusals:** `GovernanceRefusal` (`code`, `stage`, `reason`, `retryable`) with `CapacityRefusal`, `TokenBudgetRefusal`, `FirewallRefusal`, `SandboxRefusal`, `ToolRefusal`, each also the exception type v1.1 raised (`NFRViolationError`, `ValueError`, `PermissionError`). A refused v1.2 agent run records `state['refusal']`.
- **`BaseGovernanceFramework.stages` and `tool_policy`:** a subclass that declares stages runs them in order (ingress at the gate, egress at redaction); `tool_policy` ({agent: [tools]}) limits each agent's `io_read`/`io_modify`. The v1.1 hooks are no longer abstract (safe defaults); a v1.1 subclass runs exactly as before, now with typed refusals.
- **v1.2 agents run the gate *and* their own `_apply_policy`.**

### Fixed
- **`BaseAgent.run` with a `governance_engine` failed since v1.1** (`got multiple values for argument 'io_config'`): `io_config` was passed both as the gate's argument and inside the shared keywords. Regression test added.

### Step 2 — The BOPF-style propagation contract (design note 02) and the act/confirm steps (note 03)
- **`e2a.context`:** `RequestContext` (frozen: tenant, correlation, idempotency key, deadline, `settings` resolved once for every registry key plus application keys, trace), `MessageLog` and `FailedKeys` (outputs), `ReadAccess` (`io_read`: replay-safe tools, named resources) and `ModifyAccess` (`io_modify`: material tools, always with an idempotency key, typed outcomes; a transport failure is UNKNOWN). Tools declare `material = True`; each accessor refuses the other kind, and an agent's `allowed_tools` limits both.
- **Declared hook parameters:** a hook that declares `ctx` receives exactly the parameters it declares, from those its step offers; declaring one the step does not offer (for example `io_modify` in `_prepare`) is an error. No `**kwargs`.
- **`BaseAgent.run(…, ctx=…)`** for v1.2 agents: `_prepare` → governance gate **and** `_apply_policy` → `_build_messages` (`None` = no model step) → infer → evaluate/fallback → `_act` (only after an accepted evaluation, never after the deadline) → `_confirm` (sets `state['outcome']`; default: UNKNOWN > NOT_COMMITTED > COMMITTED) → redaction. A v1.1 agent runs exactly as before, with one `DeprecationWarning` per class.
- **`BaseWorkflow.execute`** creates the context once (or takes `ctx=`) and hands it to a `BaseAgent` as one object; agent-like objects without the v1.2 signature (A2C's agent chain) still receive the v1.1 keywords.
- **`BaseToolService.execute(…, ctx=…)`:** the tenant and the deadline come from the context.
- `tests/test_contract.py`: 14 tests.

## v1.1.2 — Fixes from the Order-to-Cash projects

### Fixed
- **Token budget (E-1):** `max_token_budget` given in `config` is now enforced by the governance gate. `io_config` only carries `io_`-prefixed keys, so in v1.1.1 the budget never reached the gate.
- **MCP `tools/call` (E-2):** `BaseMCPServer.serve()` completes the async tool call and returns a finished JSON-RPC result instead of an un-awaited coroutine.
- **Latency SLO (E-3):** a breach detected at the end of `BaseAgent.run()` is recorded in `state['slo_breached']` and logged, no longer raised. Raising after the run reported committed effects as failures; enforcement before effects is the request deadline's job.

### Added
- `BaseRAGPipeline._embed_query()` (E-4): a protected hook for query embedding (previously a private method returning a placeholder).
- `run_sync(coro, timeout)` (E-5): the public bridge from synchronous code to async tools, on the framework's shared loop.
- `BaseValidationService._result(valid, errors, agent_name, correlation_id, **extra)` (E-6): build results, with extra fields, from a subclass.
- `reference/smoke_test_v112.py` (11 checks; fails on v1.1.1).

### Compatibility
- No public signature changes. Callers that worked around these issues (an own `io_config` with the budget, awaiting MCP results, embedding inside `_search_index`, `_run_on_bridge_loop`, building `ValidationResult` by hand) keep working.

## v1.1.1 — Propagation fixes

### Fixed
- `BaseWorkflow.execute()`: propagation fields passed as keywords (`idempotency_key`, `correlation_id`, `io_config`, `tenant_id`, `message_log`, `failed_keys`) are resolved once and no longer forwarded a second time to `agent.run()` and the hooks. In v1.1.0 a client `idempotency_key=` or `correlation_id=` keyword raised `TypeError: got multiple values for keyword argument`.
- `BaseToolService.execute()`: `tenant_id` now reaches `_build_payload()` and `_send_request()` (it already reached `get_endpoint()`), so a transport can scope the call to the tenant.

### Added
- `reference/smoke_test_propagation.py` (7 checks), run by CI on Python 3.11 and 3.12.

### Changed
- CI uses `actions/checkout@v6` and `actions/setup-python@v6` (Node 24).

### Compatibility
- No public signature changes. Code that worked around v1.1.0 (client key in `state['idempotency_key']`, tenant in the tool payload) keeps working unchanged.

## v1.1.0 — Consumable framework

### Added
- `pyproject.toml`: E2A installs as the `e2a-framework` package from a git tag; the reference modules keep their flat import names.
- `reference/e2a_simulated_llm.py`: `SimulatedLLMService`, a deterministic rule-based `BaseLLMAgentService` for offline development, CI and reproducible builds, with token and cost accounting.
- `reference/smoke_test_simulated_llm.py` (10 checks) and a GitHub Actions workflow running the smoke tests on Python 3.11 and 3.12.

## Concurrency and idempotency

### Added
- **Concurrency model across the framework docs:** ownership by invariant, effectively-once effects under at-least-once delivery, atomic claims, leases and fencing, deadlines, compare-and-set (CAS) artifact ownership, and gateway-only model calls.
- **Claim statuses** `CLAIMED`, `REPLAY`, `IN_PROGRESS`, `CONFLICT`, `RECOVERY_REQUIRED`, `FAILED_FINAL`, and settlement as retryable, final or `UNKNOWN`.
- **Claim namespaces:** `g2c:<generator_key>`, `p0:<scaffold_key>`, `a2c:<project_name>:<project_type>:<target_cloud>`, `aidlc:<build_key>`.
- **Implementation Playbook, Section 2.19:** concurrency for the Agentic profile.
- **Cloud Landing Zone** and **MultiCloud Reference:** concurrency, admission and capacity sections, with a new capacity and admission figure.
- **AI-DLC pipeline** (`reference/ai_dlc_pipeline.py`):
  - `build_key` is the pipeline identity (`project_name:runtime:build_tool:platform`); `generator_key` remains as a deprecated alias and is not G2C's `generator_key`.
  - New `phase_reached` values: `REJECTED_INVALID`, `SKIPPED_IN_PROGRESS`, `REJECTED_CONFLICT`, `REJECTED_OVERLOAD`, `RECOVERY_REQUIRED`, `FAILED_FINAL`.
  - The build lease is max(`idempotency_lease_seconds`, remaining deadline + 60 s).
  - `deadline` and tenant propagate to G2C.
  - A validation tier (`a2c_validation_service`, default `DevPlatformValidationService`) runs before `SDLCWorkflow`, which receives a deep copy of the `dev_request`.
  - `a2c_sdlc.enabled` without an inherited generator is `REJECTED_INVALID`; without a `dev_request` it is `FAILED_A2C`.
  - `_ArtifactRegistryAdapter` presents `artifact_store` as G2C's registry (`artifact:<path>` → `{refs, created_by, deleted}`).
- `reference/smoke_test_aidlc_concurrency.py`: 26 checks using in-memory doubles. `smoke_test_aidlc_integration.py` (a2c-framework, 15 checks) runs AI-DLC over the real G2C, P0 and A2C.
- **Design documents** under `docs/design/` in both repositories (E2A core, CQRS, AI-DLC and family system in e2a-framework; A2C, G2C and P0 in a2c-framework), each with diagrams and a Word copy.

### Changed
- **Saga compensation:** never deletes outputs vouched for by a settled claim. Deleting an orphaned abstract class is opt-in (`config['compensation_delete_orphan_abstract']`) and only the creator may do it, after the last reference is released. A `G2C_P0_DONE` (Phase C) failure keeps the outputs and releases the reference.
- **Validation** moved from a per-agent `_validate_state` to `BaseValidationService.validate()` with per-agent `_validate_<agent_name>` validators and an ABAC entitlement gate.
- **A2C, G2C and P0 Framework References:** aligned with the reference implementation (`a2c_base.py`, `base_generator_agent.py`, `inherited_generator.py`), including the code-aligned `CodeCriticAgent` (score = min of model score and deterministic NFR-pattern score), G2C template mode and claim namespaces.
- **AI-DLC Implementation Playbook and Landing Zone:** `SDLCWorkflow` is now described as implemented (`execute` inherited from `BaseWorkflow`); embedded code regenerated from the repository file; saga compensation figure redrawn for the rules above.

### Notes
- With `compensation_delete_orphan_abstract` set, deleting a shared base makes later replays return `recovery_required` until the claim record is reconciled; it is off by default.
- All tests use in-memory doubles and scripted model responses. Nothing here has been validated against live services, and template-mode tests prove the sequence, not model quality.
- `docs/images/aidlc_capacity_admission.png` is referenced by `docs/AIDLC_LANDING_ZONE.md` but missing from the repository.
