# Changelog

All notable changes to the E2A framework documents and reference code.

## v1.6.0 — Contracts for managed providers (design note MP-02)

The vendor-neutral contracts lifted from the managed-providers prototype (assessment MP-00).
Additive: nothing is removed, and no existing signature changes. Vendor knowledge (wire
codecs, provider SDK adapters) stays out of E2A.

### Added
- **`e2a.llm_types` (P5):** canonical `Message`, `ToolCall`, `ToolDeclaration`, `Usage`, `StopReason` and `ModelResult`.
  - They read and write the dicts E2A already passes around.
  - Tool arguments are always a parsed dict.
  - `_dispatch_upstream_call` may return a `ModelResult` or the dict, as before.
- **Typed refusals (P6):** `ModelRefusal`.
  - A transport raises it, or returns `stop_reason: refusal`.
  - `process_inference` then returns status **`REFUSED`**: not `FAILED`, never retried, never counted toward the circuit, not a failed key.
  - **`BaseAgent` routes a refusal to `_fallback`** (`state['model_refusal']` records it), so no subclass has to know the new status.
  - `stop_reason: max_tokens` marks the payload `truncated`.
- **`e2a.capabilities` (P4):** capabilities a bound object declares (`capabilities`, or the hook `_capabilities_for(config)`).
  - Names come from a registry; a provider package adds its own with `register_capability`, and an unknown name is an error.
- **`e2a.bindings` (P1):** `bindings_from_profile()`, `Bindings`, `Profile`.
  - Seams are bound from a profile file and built lazily; `verify()` checks capabilities at startup.
  - **Only registered provider packages may be bound** (entry-point group `e2a.providers`, or `register_provider_package`): a profile cannot import arbitrary code.
  - JSON profiles need nothing; YAML needs the new extra `e2a-framework[profiles]`.
  - `extends`, `${VAR:-default}` placeholders, and unknown fields and seams are errors.
  - E2A ships the profile **`simulated`** (`SimulatedLLMService`).
- **The profile configuration layer (P2):** `resolve_config` reads `kwargs > config > env > profile > default`.
  - The profile's values travel as `config['profile_settings']`, so an environment variable overrides a profile value, and `RequestContext`'s per-request snapshot is unchanged.
  - `provider_options` (`{provider: {...}}`) is the one untyped channel; only transports read it.
- **`BaseCredentialProvider` (P20):** with `TokenCredentialProvider` as the default (it reads `auth_token`).
  - A bound `credential_provider` gives `BaseToolService` the headers it passes to `_send_request` as `auth_headers`.
- **DLP and injection patterns from configuration (P13):** `dlp_patterns`, `injection_patterns`, `dlp_patterns_replace`.
  - Validators are named (`luhn`, `iban_mod97`); there is no code in configuration.
  - Patterns are compiled when the service is constructed, so a bad one fails at startup.
- **`e2a.port_contract.check_llm_service` (P25):** ten scenarios a model adapter must meet, driven through its own wire by a responder:
  - the canonical conversation;
  - text, tool call, structured output, stream;
  - refusal, DLP, a throttle then a retry;
  - stop reason, truncation.
- **Keys:** `provider_profile`, `profile_settings`, `required_capabilities`, `provider_options`, `credential_provider`, `dlp_patterns`, `injection_patterns`, `dlp_patterns_replace`.

### Changed
- **`register_config_key`** accepts a key registered again with the same meaning (environment variable, default, type, secrecy) even if its description differs: the first registration is kept. Before, a package that had registered a key E2A later adopts (the prototype's `provider_profile`) failed at import.
- e2a-adapters and e2a-java follow the version: `1.6.0` (no code change).

## v1.5.1 — Model-call retries and three smaller fixes (design note MP-01)

Found while assessing the managed-providers proposal (hub MP-00). Nothing is removed, and no
existing signature changes.

### Fixed
- **Model calls are retried (P7).** `process_inference` called the upstream with `max_retries=0`, so a model call was never retried, and every 429 counted toward the circuit breaker like an outage.
  - Now a failure is classified (`ErrorClass`, through a new protected hook `_classify_error` with a default, `e2a.classify_error`). Only transient, throttled and overloaded failures are retried, up to `llm_max_retries` (default 2).
  - `Retry-After` is honoured, and never past the request deadline; a `Retry-After` beyond `llm_retry_after_cap` (default 30 s) is not waited for.
  - **Throttling no longer counts toward the circuit breaker** (`circuit_counts`).
  - Deterministic (4xx), auth and unknown failures are not retried. An unknown failure still counts toward the circuit, as before.
  - Streaming is unchanged: a stream is not retried.
- **Provider metadata is kept (P6).** `payload['metadata']` was overwritten with `{model_id, tokens_used}`. Now the transport's `metadata` is kept (the base fields win), and `stop_reason`, `usage` and `provider_request_id` are copied to the payload when the transport returns them.
- **The reranker receives the query (P17).** `retrieve()` now calls `_rerank(results, config, query=..., **kwargs)`, so a cross-encoder can score query-passage pairs. A `_rerank` override that does not accept `**kwargs` would need it: every override in the E2A family does.
- **`framework_version` in log records (P12)** is the installed version (it said `e2a-v1.0` in every release).

### Added
- `ErrorClass`, `classify_error` in `e2a` (and `e2a.core`).
- `ResiliencePolicy.call(..., counts_for_circuit=, delay_for=)`. Both are optional; every existing caller behaves as before.
- Keys: `llm_max_retries` (2), `llm_retry_on` (`["transient", "throttled", "overloaded"]`), `circuit_counts` (`["transient", "overloaded", "unknown"]`), `llm_retry_after_cap` (30).
- `tests/test_v151.py`: 28 tests.

### Changed
- e2a-adapters and e2a-java follow the version: `1.5.1` (no code change).

## v1.5.0 — Firestore and Cosmos DB adapters (design note D-03)

Additive: nothing changes for code on v1.4.0.

### Added
- **`e2a_adapters.cas`:** the four ports written once, over any document store that can create-if-absent and replace-if-unchanged.
  - `CasIdempotencyStore`, `CasTransactionalStore`, `CasLease` and `CasCheckpointStore`, over a small `CasBackend`.
  - After a lost race the port reads again and re-decides; it never overwrites.
  - `InMemoryCasBackend` proves the engine on the port contract kit without any database.
- **`e2a_adapters.gcp`** (`e2a-adapters[gcp]`): Firestore.
  - `create()`, and `update()` with a `last_update_time` precondition.
  - ABORTED (lock contention) counts as a lost race: nothing was written.
  - The `ttl` timestamp field is for a TTL policy.
- **`e2a_adapters.azure`** (`e2a-adapters[azure]`): Cosmos DB, NoSQL API.
  - `create_item()`, and `replace_item()` with the ETag and `IfNotModified`.
  - The item id is the partition key; escaped ids keep the original key in `e2a_key`.
  - The `ttl` field is in seconds.
- **Tests:** every backend passes the whole contract kit, races included, on its emulator:
  - Firestore emulator and Cosmos DB emulator (vNext);
  - DynamoDB Local for DynamoDB;
  - the CAS engine in memory.
- **CI:** the adapters job starts the three emulators and installs every extra; a skipped backend is listed (`-rs`).

### Changed
- e2a-java follows the version: `1.5.0` (no code change).

## v1.4.0 — Production adapters, AWS first (design note D-02)

Additive: nothing changes for code on v1.3.0.

### Added
- **`e2a.port_contract`, the port contract kit.** One behavioural suite per foundation port, for any adapter whatever its backend:
  - `check_idempotency_store`: 24 scenarios (claim, replay, fingerprint conflict, retryable and final failure, lease expiry and recovery, stale owner, UNKNOWN outcome, explicit expiry, a concurrent race);
  - `check_transactional_store`: 13 (create-if-absent, compare-and-set, conflict with the current version, fencing, put, delete, exact round-trip, a race);
  - `check_lease`: 11 (exclusive, owner-checked renew and release, expiry, strictly increasing fencing tokens, a race);
  - `check_checkpoint_store`: 8 (save and load, tenant isolation, decide exactly once, a race).

  Each takes a factory `make(clock)` and a `FakeClock`, and fails naming the scenario. The in-memory reference adapters pass all of them (`tests/test_port_contract.py`).
- **`e2a-adapters` 1.4.0** in [`adapters/`](adapters/README.md), a separate distribution so boto3 stays out of the core package:
  - `e2a_adapters.aws`: `DynamoDBIdempotencyStore`, `DynamoDBTransactionalStore`, `DynamoDBLease`, `DynamoDBCheckpointStore`;
  - one condition expression per state change; only `ConditionalCheckFailedException` counts as a lost race, and every other error is raised;
  - they share the application's table under `E2A#…` partitions (one per key);
  - wall-clock leases with an injectable clock; the `ttl` attribute for garbage collection only;
  - values stored as JSON, refused over 350 KB;
  - the lease keeps its fencing counter across release, so tokens never go back.

  Tested on moto: the four contract suites, plus the table layout, a non-condition error and the size limit.
- **CI:** an `adapters` job installs the core and the adapters and runs their tests and strict conformance.
- **The concurrent races run on DynamoDB Local** (Amazon's emulator, which evaluates condition expressions atomically), in CI and wherever `DYNAMODB_LOCAL_ENDPOINT` is set.
  - On moto the kit runs without them (`race=1`): moto's in-process backend is not atomic under threads, so those scenarios failed about one run in eight without proving anything about the adapter.
  - The kit's `race` argument documents this: a value below 2 leaves the race scenario out, and is only for backends that are not atomic under threads.

### Changed
- e2a-java follows the version: `1.4.0` (no code change).

## v1.3.0 — The v1.1 agent hooks and get_endpoint are removed

Announced in v1.2.0 and prepared in v1.2.3 (design note V13-01): every codebase in the family already runs without them.

### Removed
- **The v1.1 agent hook adapters.**
  - A `BaseAgent` subclass hook with the v1.1 keyword signature (`config=None, **kwargs`, no `ctx`) is now refused when the class is defined: a `TypeError` naming the hook. It was deprecated with a warning in v1.2.
  - `BaseAgent.run` has one order, the v1.2 one.
  - In a workflow of declared steps (`_build_workflow(self, ctx)`), a v1.1 workflow hook is a `TypeError`.
- **`BaseToolService.get_endpoint()`,** the public alias deprecated in v1.2. A subclass that defines `get_endpoint` is refused when the class is defined; implement `_get_endpoint`.
- `_contract.warn_legacy`, which is no longer needed.

### Changed
- **`e2a.conformance` is strict by default.**
  - R2 (a protected hook called from outside its class) and D1 (a removed public method) are errors.
  - `--lenient` reports them without failing, while migrating; `--strict` is accepted and does nothing.
  - `failed(findings, strict=True)` is the default.
  - D1 now reports `get_endpoint` on any subclass of a class with `_get_endpoint`.
- **`BasePipeline._verify_scaffold_contracts`** is strict by default (`conformance_strict=True`).
- **Tests:** the tests that exercised the adapters now check that v1.3 refuses them. 79 tests pass.
- **e2a-java:** version `1.3.0`, unchanged.

### Kept, on purpose
- **`e2a_base`:** the re-export of the `e2a` package.
- **The v1.1 workflow form:** a graph plus `_get_agent`, with no `ctx`. It was never deprecated, and A2C's SDLC workflow and the reference orchestrator use it.
- **The v1.1 governance hooks** of an engine without stages.
- **The mirrored `state['tenant_id']` and `state['correlation_id']`.**

## v1.2.3 — Preparing for v1.3 (design note V13-01)

Nothing is removed and no contract changed; this release makes E2A's own code ready for v1.3.

### Changed
- **The reference orchestrator** (`multimodal_orchestrator.py`) uses the v1.2 hooks:
  - `RAGAgent` retrieves in `_prepare` and scores with the pipeline's public `evaluate_answer`;
  - `APIToolAgent` and `MCPToolAgent` are deterministic agents whose tool call is the material `_act`, with the typed result deciding `state['outcome']`;
  - `FallbackAgent` is on the v1.2 hooks.
  - Its workflow keeps the v1.1 workflow form (a graph and `_get_agent`), which v1.2 never deprecated.
- **The reference smoke scripts:** their test agents and tools use the v1.2 hooks and `_get_endpoint`. The propagation script now checks that the propagated fields arrive in `ctx`.
- **e2a-java:** version `1.2.3`, unchanged.

### Added
- **A guard** in `tests/conftest.py`: a test that uses the v1.1 adapters fails, unless it is marked `@pytest.mark.v11_adapters`. The six tests marked this way exercise the adapters on purpose and become the tests that v1.3 refuses them.
- **`tests/test_smoke_scripts.py`:** runs the scripts with warnings shown and fails on any v1.1 adapter use.
- **Result:** 79 tests pass.

## v1.2.2 — A release gate every project can reuse, and e2a-java

### Added — `e2a.release` (design note C-04, D-1)
- **`ProjectReleaseGate`**, a `BasePipeline` whose generic stages work as they are: a secret scan (`scan_paths`, `SECRET_PATTERNS`), `pytest`, the capacity equation plus `e2a.conformance` (strict by default), and `compileall`. The domain stages are one hook per case: `_score_case(case)` over `evals/golden.json` (the average is held to `faithfulness_threshold`) and `_probe(case)` over `evals/redteam.json`. A missing set is reported as `NOT_CONFIGURED`, not as a pass.
- `python -m e2a.release [--root] [--skip-tests] [--report] [--config]`; a project gate calls `run_release_gate(gate_class=...)`. `gate_report()` adds the evidence (secret hits, the test summary, conformance findings, one row per case, red-team failures).
- `load_cases()` reads an evaluation or red-team set (a list, or an object of lists with `kind`) and names what is wrong with it. `scan_secrets()` is the one list of secret patterns A2C's Phase C also uses.
- `tests/test_release_gate.py`: 12 tests (79 in all).

### Added — e2a-java (design note D-01, plan item G-4)
- **`java/`**: the E2A runtime for Java 21, released with this tag at version `1.2.2`:
  - `e2a-core`: `RequestContext`, `IdempotencyStore`, `ReadAccess` / `ModifyAccess`, governance stages, `LlmService`, `BaseAgent` with the v1.2 lifecycle, `BaseWorkflow` with declared steps and one claim;
  - `e2a-cqrs`: the Deterministic CQRS profile, extracted from financial-settlement-platform;
  - `e2a-spring-jpa`: `JpaIdempotencyStore`, auto-configured for Spring Boot 3.5;
  - `e2a-test`: the `AgentContract` JUnit kit.
- **Tests:** 27 tests, plus a `java` CI job.
- **How services consume it:** there is no package registry. They build it from this tag in their CI (`java/README.md`).

### Changed
- Version `1.2.2`. No Python contract changed.

## v1.2.1 — Documents for v1.2 (docs only)

No code changes; the package version moves to 1.2.1 so the documents have a tag. Everything below describes v1.2.0.

### Design documents
- **Agentic core** (`docs/design/core/`): v1.2 folded in — the `e2a` package (7.1), request context and accessors (new 6.6), the v1.2 agent lifecycle and hooks (8.2), governance stages (8.7), declared steps and `resume()` (8.1, 9.3), RAG ports and the façade (8.5), `_get_endpoint` and `serve_async` (8.6), the conformance kit (new 8.18), a v1.2 extension skeleton (12.2), verification (67 tests), use cases UC-14 to UC-16, invariants INV-14 to INV-17, observations O-14 to O-17 with the status of the ones fixed since, a new Section 15 "What v1.2 changes", and traceability anchors pointing at the package modules. Two new diagrams: `31_v12_agent_run`, `32_v12_workflow_steps`.
- **CQRS profile** (new Section 16), **AI-DLC profile** (new Section 15) and **family system design** (new Section 15): what v1.2 (and A2C v1.2.0) means for each; shared-contract homes and anchors updated.
- The four Word documents are regenerated from the Markdown in the same house style; `docs/design/common/style.puml` (the shared PlantUML style) is restored.

### Playbooks, landing zones and references
- **Implementation Playbook:** new Section 2.20, "E2A v1.2 — Contracts and Migration Guide" (package layout; migrating agents, governance and workflows, with examples run against v1.2; conformance); the Governed Entry Point principle replaces the "Single Rule"; the `e2a_base.py` listing is marked as the v1.1 reference.
- **CQRS and AI-DLC playbooks:** v1.2 notes.
- **Cloud landing zone:** Principle 1 is now "Governed Entry Points = Governed Ingress"; new 9.2 maps the v1.2 contracts (stages, accessors, context, steps and checkpoints, conformance) to landing-zone components. **AI-DLC landing zone:** Phase A output for v1.2 services.
- **Strategic overview, whitepaper, multimodal note:** v1.2 wording (governed entry points, request context, stages, conformance).
- **Configuration reference:** source moved to `e2a/config.py`; how v1.2 resolves settings once per request (`ctx.setting`). The registry itself did not change.
- **Master Abstraction Reference (.docx):** section 2.2 is the Governed Entry Point rule, with the v1.2 capabilities and conformance rules. **MultiCloud Reference (.docx):** examples import from `e2a` and raise `GovernanceRefusal` (they imported `GovernanceViolationError`, which is A2C's, not E2A's).

## v1.2.0 — Governed entry points, request context, stages, declared steps

Design: the v1.2 design notes in the o2c-platform-hub repository (roadmap/design/).

### Step 1 — Package structure and test suite (no behaviour change)
- **`e2a` package (E-12):** the framework is split from the 3,300-line `e2a_base.py` into one module per contract area: `e2a.core`, `config`, `resilience`, `observability`, `governance`, `validation`, `llm`, `agents`, `workflows`, `rag`, `tools`, `ports`, `pipeline`, `infra`. The code moved without changes; imports between modules are explicit.
- **`e2a_base` is a compatibility module:** it re-exports every name it defined in v1.1.x (the same objects), and the standard-library names that `from e2a_base import *` used to provide.
- **pytest suite (E-14):** `pytest tests` runs every reference smoke script against the installed package, plus package-layout tests (all v1.1 names importable, one home per public name, every module importable on its own). CI runs it.
- `smoke_test_llm_service.py`: the "no `os.getenv` outside the registry" check now covers every module of the package.

### Step 7 — Proof: O2C migrated (findings fixed in the framework)
Migrating the O2C assistant (branch `feature/e2a-v1.2` of order-to-cash-agentic-ai) found four gaps, fixed here:
- **Screening before reading.** A v1.2 agent ran `_prepare` (reads, retrieval) before the firewall, so an unscreened request (PII, an injection) could reach a retriever. Ingress stages now have a `phase`: `'screen'` stages (capacity, token budget, sandbox, firewall, custom ones by default) run **before** `_prepare`; `'decide'` stages (`ApprovalStage`) run after it, because they need the facts it read. `enforce_governance_gate(…, phase=…)`; no phase runs all, as before.
- **The checkpoint holds the state at the pause** (after the step's `_prepare`), so the approver sees what the decision is about (the priced order); the step runs again from its start on resume.
- **A deterministic agent's response** is what its steps put in `state['response']` (it was always `None` without a model step, so a replay returned no result).
- **Conformance:** framework data attributes a subclass sets (`stages`, `tool_policy`, …) are no R3 finding; `checkpoint_store` is not a setting of the request context.
Result: O2C's 39 tests and the three-service landscape demo pass on the migrated code; the unmigrated v1.1.2 O2C code also passes its 39 tests on v1.2.

### Step 6 — Conformance kit (design note 05 §3; rules from note 01)
- **`e2a.conformance`** checks the Governed Entry Point principle (a public method per genuine capability, delegating to protected hooks and framework-owned private methods): **R1** a framework-owned public or private method overridden (error); **R2** a protected hook called from outside its class (warning; an error with `--strict`, the v1.3 default); **D1** a deprecated public hook overridden (`get_endpoint`); **R3** an extra public method, a review note. The rule sets are read from the e2a classes, and subclasses are followed across files. `python -m e2a.conformance src/ [--strict] [--quiet]` exits 1 on an error; CI runs it strictly over the framework and reference code.
- **`AgentContract`** pytest kit: run() never raises on a hook error; a model step below `min_confidence` falls back and commits nothing; nothing material after the deadline; the request tenant reaches the result.
- **`BasePipeline._verify_scaffold_contracts`** is no longer abstract: by default it runs the rules over `conformance_paths` (findings in `self.conformance_findings`). It no longer requires exactly one public method per class.
- The reference multi-agent orchestrator calls `evaluate_answer()` instead of the protected `_evaluate_answer()` (an R2 finding in E2A's own code). Docs: the principle is restated in the overview, design and playbooks.

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
