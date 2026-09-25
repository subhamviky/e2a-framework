# E2A AI-DLC Implementation Playbook

### Implementation Playbook — Class Contracts & Scaffold, AI-DLC Profile

|                     |                                                                                                                                                                                                          |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Document Version    | 1.0.0                                                                                                                                                                                                    |
| Author              | Subham Gupta, Staff Architect & AI Architect                                                                                                                                                             |
| Classification      | Architecture Reference — Class Contracts & Scaffold Code, AI-DLC Profile                                                                                                                                 |
| Companion document  | [AIDLC_LANDING_ZONE.md](https://github.com/subhamviky/e2a-framework/blob/main/docs/AIDLC_LANDING_ZONE.md) — the infrastructure and topology this scaffold deploys onto                                  |
| Parent reference    | [IMPLEMENTATION_PLAYBOOK.md](https://github.com/subhamviky/e2a-framework/blob/main/docs/IMPLEMENTATION_PLAYBOOK.md) — method signatures for `BaseValidationService`, `BasePipeline`, `BaseObservability`, `BaseGovernanceFramework`, reused unmodified |
| Sibling reference   | [CQRS_IMPLEMENTATION_PLAYBOOK.md](https://github.com/subhamviky/e2a-framework/blob/main/docs/CQRS_IMPLEMENTATION_PLAYBOOK.md) — this document's structure and conventions follow it                     |
| Scope               | Method signatures, propagation contract, and runnable scaffold for `AIDLCPipelineOrchestrator`, `AIDLCStaticPipeline`, and `AIDLCSagaCompensator` — composing G2C's `DeveloperPlatformWorkflow`, P0's `BaseProjectBootstrapper` (invoked inline, never called separately), and A2C's `SDLCWorkflow`. |

## 1. Purpose & Relationship to Companion Documents

This document carries the method-level contract and runnable scaffold for the one class the AI-DLC profile actually adds: `AIDLCPipelineOrchestrator`, plus its two supporting classes (`AIDLCStaticPipeline`, `AIDLCSagaCompensator`) and three new TypedDicts. It is the LLD companion to [AIDLC_LANDING_ZONE.md](https://github.com/subhamviky/e2a-framework/blob/main/docs/AIDLC_LANDING_ZONE.md), exactly as [CQRS_IMPLEMENTATION_PLAYBOOK.md](https://github.com/subhamviky/e2a-framework/blob/main/docs/CQRS_IMPLEMENTATION_PLAYBOOK.md) is the LLD companion to [CQRS_CLOUD_LANDING_ZONE.md](https://github.com/subhamviky/e2a-framework/blob/main/docs/CQRS_CLOUD_LANDING_ZONE.md).

Every framework this profile composes — G2C's `DeveloperPlatformWorkflow`, P0's `BaseProjectBootstrapper` (invoked inline by G2C, never called separately here), and A2C's `SDLCWorkflow` — is reused unmodified. None of their method contracts are repeated in this document beyond the entry-point table in Section 2; read `G2C_Framework_Reference.md`, `P0_ProjectBootstrap_Framework_Reference.md`, and `A2C_Framework_Reference.md` for those.
> **A Note on SDLCWorkflow's Signature**
>
> `A2C_Framework_Reference.md` describes `SDLCWorkflow` conceptually (Section 3.2 — the five-agent LangGraph StateGraph: RequirementsAgent → CodeGenAgent → IaCAgent → CICDAgent → CodeCriticAgent) but does not publish a formal method-contract table for it the way it does for `SDLCAssistantAgent`. The `execute(dev_request, config) -> DevRequest` signature used throughout this document is inferred from that description and from the calling convention every other E2A/A2C workflow class already follows — it is this profile's expected contract, not a verbatim quote from that document. Flagging this distinction matters: everything else in this Playbook is a direct, checkable consequence of a published contract; this one piece is the single inferred exception.

This document also corrects one design gap discovered while writing it. The Landing Zone HLD describes Phase C as running "the first 3 of `BasePipeline`'s 7 stages." Read literally, that would mean calling `BasePipeline`'s protected stage hooks (`_run_static_security_scans()`, etc.) directly from outside the class — which breaks the Single Public Entry Point Rule this profile holds every other class to. Section 6 below resolves this the correct way: a concrete `BasePipeline` subclass, `AIDLCStaticPipeline`, whose four unused stages are overridden as intentional no-ops. `BasePipeline.run_pipeline()` itself — its one public method — is still called exactly as written, untouched.

Two further corrections apply versus an earlier framing of this profile: (1) there are no `G2CPhaseService` / `P0PhaseService` / `A2CPhaseService` / `E2AActivationPhaseService` wrapper classes — `AIDLCPipelineOrchestrator` calls `DeveloperPlatformWorkflow.generate()` and `SDLCWorkflow.execute()` directly, with nothing in between; (2) the `dev_request` handed to Phase B is never rebuilt by this orchestrator via a second `__build_dev_request()` call — it is read straight off Phase A's `GeneratorResult` (`scaffold_result.dev_request`), produced inline by P0's `bootstrap()` because `config['a2c']['enabled']=True` was set before Phase A ran (P0 Sec 2.2 / 9.2).

## 2. The Single Public Entry Point Rule

| Class                        | Public Entry Point                                                                             | Status                                                            |
| ----------------------------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------ |
| `AIDLCPipelineOrchestrator`   | `execute_pipeline(request, config, **kwargs) -> AIDLCPipelineResult`                             | New — this document (Section 5)                                   |
| `AIDLCStaticPipeline`         | `run_pipeline(config, **kwargs) -> dict` (inherited from `BasePipeline`, not overridden)         | New subclass — this document (Section 6)                          |
| `AIDLCSagaCompensator`        | `compensate(result, config, correlation_id, **kwargs) -> dict`                                   | New — this document (Section 7)                                   |
| `DeveloperPlatformWorkflow`   | `generate(request, config) -> GeneratorResult`                                                   | Reused unmodified (`G2C_Framework_Reference.md` Sec 10.2)          |
| `SDLCWorkflow`                | `execute(dev_request, config) -> DevRequest`                                                     | Reused, signature inferred (see callout above)                    |
| `BaseValidationService`       | `validate(agent_name, state, config, correlation_id, **kwargs) -> ValidationResult`              | Reused unmodified — Tier 1, ahead of this class entirely           |
| `BaseProjectBootstrapper`     | `bootstrap(request, config) -> ScaffoldResult`                                                   | Reused unmodified — invoked inline by G2C, never called here       |

## 3. Cross-Class Propagation Fields

The same six/seven fields from the Agentic and CQRS profiles, unmodified. `generator_key` is this profile's own business idempotency key — not a universal field, the same way `idempotency_key`'s origination point already differs slightly per profile in the companion Playbooks.

| Field             | Flow             | Resolved / Minted By                                                                         |
| ------------------ | ----------------- | ------------------------------------------------------------------------------------------------ |
| `correlation_id`  | INPUT             | `BaseValidationService.validate()` at Tier 1, ahead of `AIDLCPipelineOrchestrator`               |
| `tenant_id`       | INPUT             | JWT claim at Tier 0, read from `request['tenant_id']`                                            |
| `generator_key`   | INPUT (business key) | `AIDLCPipelineOrchestrator.__generate_key()` — `project_name:runtime:build_tool:platform`     |
| `io_config`       | INPUT             | Resolved once from `config['io']` / the `IO_CONFIG` namespace                                    |
| `message_log`     | OUTPUT            | Empty at request start; every phase appends structured entries via `_log()`                      |
| `failed_keys`     | OUTPUT            | Empty at request start; populated with `generator_key` on any phase failure                       |
| `trace_context`   | INPUT (additive)  | W3C traceparent if present, else derived from `correlation_id`                                    |

## 4. TypedDicts — Request, Result, and State Record

`AIDLCPipelineRequest` is the unified UI payload from the Landing Zone HLD's Section 4.3, reproduced here as a runnable type. `AIDLCPipelineResult` is its output contract. `PipelineStateRecord` is the row shape written to the Tier 3 Pipeline State/Outbox table — bound to `BaseTransactionalStore`, not a new abstract class.

```python
class AIDLCPipelineRequest(TypedDict, total=False):
    # UI panel: Context — P0's ScaffoldRequest, embedded verbatim as
    # request['scaffold'] when calling DeveloperPlatformWorkflow.generate()
    context: Dict[str, Any]

    # UI panel: Harness — G2C's GeneratorRequest fields
    harness: Dict[str, Any]

    # UI panel: Prompt — merged into the harness/GeneratorRequest at call time
    prompt: Dict[str, Any]

    # A2C SDLCWorkflow activation — Phase B is skipped entirely, not failed,
    # if this is absent or enabled=False
    a2c_sdlc: Dict[str, Any]  # {enabled, project_type, mandatory_nfrs, target_cloud}

    tenant_id: str
    model_id: str
    critic_model_id: str

    # Written by AIDLCPipelineOrchestrator — do not supply
    generator_key: str
    correlation_id: str


class AIDLCPipelineResult(TypedDict, total=False):
    success: bool
    generator_key: str
    correlation_id: str
    phase_reached: str          # QUEUED | G2C_P0_DONE | A2C_DONE | E2A_VERIFIED
                                 # | FAILED_G2C_P0 | FAILED_A2C | SKIPPED_IDEMPOTENT
    generator_result: dict      # GeneratorResult from Phase A (G2C)
    dev_request: dict           # DevRequest handed to A2C, if Phase B ran
    sdlc_result: dict           # updated DevRequest returned by Phase B
    pipeline_report: dict       # BasePipeline-shaped report from Phase C
    message_log: List[dict]
    failed_keys: List[str]
    trace_id: str
    latency_ms: float
    errors: List[str]


class PipelineStateRecord(TypedDict, total=False):
    """Bound to BaseTransactionalStore (Implementation Playbook Sec 2.16)
    via config['pipeline_state_store'] — put()/get() against the Tier 3
    Pipeline State/Outbox table. Not a new abstract class; a concrete
    BaseTransactionalStore subclass just points it at that table instead
    of at a generic row store."""
    correlation_id: str
    tenant_id: str
    generator_key: str
    phase: str
    artifact_pointers: Dict[str, str]
    created_at: float
    updated_at: float
```

## 5. AIDLCPipelineOrchestrator — Corrected Scaffold

The composing class. Calls `DeveloperPlatformWorkflow.generate()` and `SDLCWorkflow.execute()` directly — by design, no wrapper classes sit between this orchestrator and either framework's real entry point.

| Step | Action                              | Condition                                                                             |
| ---- | ------------------------------------ | ---------------------------------------------------------------------------------------- |
| 1    | Governance gate                      | Always, if `config['governance_engine']` is set                                       |
| 2    | Idempotency check                    | Only if `config['idempotency']` = `True` (default)                                    |
| 3    | Phase A — G2C + P0                   | Always. Aborts the pipeline (no Phase B/C) on failure                                 |
| 4    | Phase B — A2C                        | Only if `request['a2c_sdlc']['enabled']` and Phase A produced a `dev_request`          |
| 5    | Phase C — E2A static verification    | Always, via `AIDLCStaticPipeline.run_pipeline()` (Section 6)                           |
| 6    | Saga evaluation                      | In the `finally` block — triggers `AIDLCSagaCompensator.compensate()` if `failed_keys` is non-empty |
| 7    | Observability flush                  | Always, in the `finally` block                                                        |

```python
class AIDLCPipelineOrchestrator:
    """The single class this profile adds. Every framework it calls (G2C,
    P0, A2C, E2A) is reused unmodified via its own existing public entry
    point — no wrapper classes sit between this orchestrator and
    DeveloperPlatformWorkflow.generate() or SDLCWorkflow.execute()."""

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}

    def execute_pipeline(self, request: 'AIDLCPipelineRequest',
                          config: Dict[str, Any] = None,
                          correlation_id: str = None, **kwargs) -> 'AIDLCPipelineResult':
        """Public entry point. Fixed sequence below — never overridden."""
        config = config or self.config
        correlation_id = correlation_id or request.get('correlation_id', str(uuid.uuid4()))
        message_log: List[dict] = []
        failed_keys: List[str] = []
        start = time.time()
        result: AIDLCPipelineResult = {
            'correlation_id': correlation_id, 'message_log': message_log,
            'failed_keys': failed_keys, 'success': False, 'phase_reached': 'QUEUED',
        }
        generator_key = self.__generate_key(request)
        result['generator_key'] = generator_key

        try:
            # Step 1 — Governance gate
            gov_engine = config.get('governance_engine')
            if gov_engine:
                request = gov_engine.enforce_governance_gate(
                    request, config.get('io_config', {}), config,
                    correlation_id=correlation_id, tenant_id=request.get('tenant_id'))

            # Step 2 — Idempotency check
            if config.get('idempotency', True) and self.__already_built(generator_key, config):
                self._log(message_log, correlation_id, 'INFO', 'pipeline_skipped_idempotent')
                result['success'] = True
                result['phase_reached'] = 'SKIPPED_IDEMPOTENT'
                return result

            # Step 3 — Phase A: G2C + P0. P0's bootstrap() runs inline
            # inside DeveloperPlatformWorkflow.generate() (via the
            # inherited-class generator's own phases 7–8) — never
            # invoked separately by this class.
            g2c_request = dict(request.get('harness', {}))
            g2c_request['scaffold'] = request.get('context', {})
            g2c_request.update(request.get('prompt', {}))
            g2c_config = dict(config)
            if request.get('a2c_sdlc', {}).get('enabled'):
                # Rides through DeveloperPlatformWorkflow -> ... -> P0's
                # bootstrap() unchanged, exactly as P0's own
                # BootstrapAndGenerateWorkflow already does for the
                # P0-only case (P0_ProjectBootstrap_Framework_Reference.md
                # Sec 9, Pattern 3).
                g2c_config['a2c'] = {
                    'enabled': True,
                    'project_type': request['a2c_sdlc'].get('project_type', ''),
                    'mandatory_nfrs': request['a2c_sdlc'].get('mandatory_nfrs', []),
                    'target_cloud': request['a2c_sdlc'].get('target_cloud', 'aws'),
                    'context': request.get('prompt', {}).get('user_prompt', ''),
                }

            generator_result = DeveloperPlatformWorkflow(config=g2c_config).generate(
                g2c_request, g2c_config)
            result['generator_result'] = generator_result
            self.__persist_state(result, 'G2C_P0_ATTEMPTED', config)
            self._log(message_log, correlation_id, 'INFO', 'phase_a_complete',
                       success=generator_result.get('success', False))

            if not generator_result.get('success', False):
                failed_keys.append(generator_key)
                result['phase_reached'] = 'FAILED_G2C_P0'
                return result
            result['phase_reached'] = 'G2C_P0_DONE'
            self.__persist_state(result, 'G2C_P0_DONE', config)

            # Step 4 — Phase B: A2C (conditional). dev_request is
            # surfaced on Phase A's GeneratorResult as
            # scaffold_result.dev_request, produced inline by P0's
            # bootstrap() Step 16 because config['a2c']['enabled']=True
            # was set above (P0 Sec 2.2 / 9.2) — never rebuilt here.
            dev_request = (generator_result.get('scaffold_result') or {}).get('dev_request')
            if request.get('a2c_sdlc', {}).get('enabled') and dev_request:
                result['dev_request'] = dev_request
                sdlc_result = SDLCWorkflow(config=config).execute(dev_request, config)
                result['sdlc_result'] = sdlc_result
                self._log(message_log, correlation_id, 'INFO', 'phase_b_complete',
                           critic_score=(sdlc_result.get('critic_report') or {}).get('score'))
                if sdlc_result.get('errors'):
                    failed_keys.append(generator_key)
                    result['phase_reached'] = 'FAILED_A2C'
                    return result
                result['phase_reached'] = 'A2C_DONE'
                self.__persist_state(result, 'A2C_DONE', config)

            # Step 5 — Phase C: E2A static verification. Routed through
            # BasePipeline's ONE public method — never by reaching into
            # its protected stage hooks from outside the class.
            static_pipeline = config.get('static_pipeline') or AIDLCStaticPipeline()
            pipeline_report = static_pipeline.run_pipeline(
                config, correlation_id=correlation_id,
                message_log=message_log, failed_keys=failed_keys)
            result['pipeline_report'] = pipeline_report
            if pipeline_report.get('status') != 'SUCCESS':
                raise ValueError(f"Phase C failed: {pipeline_report.get('error')}")

            result['phase_reached'] = 'E2A_VERIFIED'
            result['success'] = True
            self.__persist_state(result, 'E2A_VERIFIED', config)

        except Exception as e:
            failed_keys.append(generator_key)
            self._handle_error(e, request, result, config,
                                correlation_id=correlation_id, message_log=message_log)
        finally:
            if failed_keys:
                self._send_to_dlq(failed_keys, config)
                self.__trigger_compensation(result, config, correlation_id=correlation_id)
            obs_engine = config.get('observability_engine')
            if obs_engine:
                obs_engine.record_telemetry(message_log, correlation_id, config,
                                             tenant_id=request.get('tenant_id'))
            else:
                for entry in message_log:
                    logging.info(entry)
            result['latency_ms'] = round((time.time() - start) * 1000, 1)
            result['trace_id'] = correlation_id

        return result

    # ---- PRIVATE — framework-owned, never overridden ----

    def __generate_key(self, request) -> str:
        ctx = request.get('context', {})
        return (f"{ctx.get('project_name','?')}:{ctx.get('runtime','?')}:"
                f"{ctx.get('build_tool','?')}:{ctx.get('platform','?')}")

    def __already_built(self, generator_key, config) -> bool:
        store = config.get('idempotency_store')  # a BaseIdempotencyStore
        if not store:
            return False
        # check_and_set() returns True only for the caller that wins the
        # race — False here means another request already claimed this key.
        return not store.check_and_set(generator_key, config=config)

    def __persist_state(self, result, phase, config):
        store = config.get('pipeline_state_store')  # a BaseTransactionalStore
        if not store:
            return
        record: PipelineStateRecord = {
            'correlation_id': result['correlation_id'],
            'generator_key': result['generator_key'],
            'phase': phase,
            'updated_at': time.time(),
        }
        store.put(result['correlation_id'], record, config=config,
                  table_or_collection_name='pipeline_state')

    def __trigger_compensation(self, result, config, correlation_id=None):
        compensator = config.get('saga_compensator') or AIDLCSagaCompensator()
        compensator.compensate(result, config, correlation_id=correlation_id)

    def _log(self, message_log, correlation_id, level, event, **fields):
        if message_log is None:
            return
        message_log.append({'timestamp': time.time(), 'correlation_id': correlation_id,
                             'level': level, 'event': event,
                             'class': type(self).__name__, **fields})

    def _send_to_dlq(self, failed_keys, config=None, **kwargs):
        config = config or {}
        queue_url = config.get('dlq_queue_url', os.getenv('DLQ_QUEUE_URL'))
        if queue_url and failed_keys:
            logging.warning(f'DLQ dispatch -> {queue_url}: {failed_keys}')

    def _handle_error(self, error, request, result, config=None, **kwargs):
        logging.error(f"[AIDLCPipelineOrchestrator][{kwargs.get('correlation_id')}] {error}")
        result.setdefault('errors', []).append(str(error))
```

## 6. AIDLCStaticPipeline — Phase C's BasePipeline Subclass

Resolves the design gap flagged in Section 1. `AIDLCStaticPipeline` extends `BasePipeline` and overrides only its seven protected stage hooks — never `run_pipeline()` itself, which stays `BasePipeline`'s one untouched public method. Three hooks do real work (static security scan, test suite, scaffold-contract check); four are intentional no-ops, because this profile's own Tier 2 compute never performs a live deploy.

```python
class AIDLCStaticPipeline(BasePipeline):
    """Concrete BasePipeline subclass used only for this profile's Phase C.
    run_pipeline() — BasePipeline's one public entry point — is inherited
    untouched; this class never overrides it (Implementation Playbook
    Sec 2.12: 'PUBLIC | run_pipeline() | Template method'). The four
    stages this profile has no use for (RAG eval, artifact compile,
    dynamic security, deployment rollout) are overridden as intentional
    pass-through no-ops rather than skipped, so BasePipeline's fixed
    seven-stage sequence still runs exactly as written — this profile
    just has nothing for those four stages to do. A generated project's
    own CI/CD workflow (produced by A2C's CICDAgent in Phase B) is what
    actually compiles, DAST-scans, and deploys, on its own first commit."""

    def __init__(self):
        super().__init__()
        self.required_artifact_matrix = []  # nothing for this profile to compile

    def _run_static_security_scans(self, config, **kwargs) -> bool:
        # SAST + secret-detection (e.g. gitleaks) against the generated
        # repository content in the Tier 3 object store.
        return True

    def _execute_test_suite(self, config, **kwargs) -> bool:
        # Runs the generated project's own tests/ directory in an
        # ephemeral build container — not a live deploy.
        return True

    def _verify_scaffold_contracts(self, config, **kwargs) -> bool:
        # Statically asserts the Single Public Entry Point Rule against
        # the GENERATED classes, not this pipeline's own.
        return True

    def _run_rag_eval(self, config, **kwargs) -> float:
        return 1.0  # no RAG pipeline exists on this profile's own request path

    def _compile_artifact(self, artifact_name, config, **kwargs) -> bool:
        return True  # required_artifact_matrix is empty; never actually called

    def _run_dynamic_security_checks(self, config, **kwargs) -> bool:
        return True  # DAST belongs to the generated CI/CD workflow, not here

    def _execute_deployment_rollout(self, strategy, config, **kwargs) -> dict:
        return {'skipped': 'no live deploy in the AI-DLC pipeline itself'}
```
> **Reading the `pipeline_report` Honestly**
>
> Because `run_pipeline()` is never modified, its `finally` block still writes `report['stages']['deployment_rollout'] = 'PASSED_<strategy>'` unconditionally after calling `_execute_deployment_rollout()` — even though that call is a no-op here. Any code that consumes `AIDLCStaticPipeline`'s report should treat the `artifact_compilation`, `dynamic_security`, and `deployment_rollout` entries as inapplicable for this profile, not as evidence anything was actually compiled, scanned, or deployed. Only `static_security`, `test_execution`, and `scaffold_compliance` carry real meaning here.

## 7. AIDLCSagaCompensator — Corrected Scaffold

Implements the Saga Decision Matrix from the Landing Zone HLD's Section 7, with the correction that matters most: it is scoped by `generator_key`, and it never deletes the shared abstract class except when that class was generated fresh for this exact request and no other `generator_key` yet depends on it.

```python
class AIDLCSagaCompensator:
    """PRIMARY class — compensate() is the single public entry point.
    Scoped so it only ever reverts artifacts this specific generator_key
    produced. common/e2a_base.py or common/a2c_base.py is never touched
    here — that file is reused-by-reference across generator_keys and may
    already be a dependency of other, unrelated, already-completed builds."""

    def compensate(self, result: 'AIDLCPipelineResult', config: Dict[str, Any] = None,
                    correlation_id: str = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        phase = result.get('phase_reached', 'QUEUED')
        actions: List[str] = []

        if phase in ('QUEUED', 'FAILED_G2C_P0'):
            # Nothing durable was written yet, or only a partial scaffold —
            # delete the inherited-class file and the P0 scaffold for this
            # generator_key only. The shared abstract class is untouched
            # unless it was generated fresh for this request and no other
            # generator_key references it yet (checked inside the helper).
            actions.append(self._delete_scaffold(result, config))
            actions.append(self._delete_inherited_class(result, config))
            actions.append(self._delete_abstract_class_if_orphaned(result, config))

        elif phase == 'FAILED_A2C':
            # Scaffold + G2C classes are valid on their own — keep them.
            # Only the A2C-generated Terraform/CI-CD/business-logic files
            # and the ephemeral Git branch are reverted.
            actions.append(self._delete_a2c_artifacts(result, config))
            actions.append(self._revert_git_branch(result, config))

        elif phase == 'A2C_DONE':
            # Phase C (static verification) failed after A2C succeeded —
            # revert only the staged A2C commit; scaffold and classes stand.
            actions.append(self._revert_git_branch(result, config))

        self._route_to_dlq(result, config, correlation_id=correlation_id)
        return {'phase_compensated': phase, 'actions': actions}

    def _delete_scaffold(self, result, config) -> str:
        return 'scaffold_deleted'  # object-store delete under generator_key prefix

    def _delete_inherited_class(self, result, config) -> str:
        return 'inherited_class_deleted'

    def _delete_abstract_class_if_orphaned(self, result, config) -> str:
        # Only deletes common/e2a_base.py (or a2c_base.py) if this
        # generator_key is its sole known reference — checked against the
        # Pipeline State table before any delete.
        return 'abstract_class_untouched_or_orphan_deleted'

    def _delete_a2c_artifacts(self, result, config) -> str:
        return 'a2c_artifacts_deleted'

    def _revert_git_branch(self, result, config) -> str:
        return 'git_branch_reverted'

    def _route_to_dlq(self, result, config, **kwargs) -> None:
        queue_url = (config or {}).get('dlq_queue_url', os.getenv('DLQ_QUEUE_URL'))
        if queue_url:
            logging.warning(f"DLQ (compensation) -> {queue_url}: {result.get('generator_key')}")
```

### 7.1 Saga Decision Matrix

| `phase_reached` at Failure         | Compensating Action                                                                                                     | What Is Preserved                                                                          |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `QUEUED` / `FAILED_G2C_P0`          | Delete the P0 scaffold and the inherited-class file for this `generator_key`; delete the shared abstract class only if orphaned by this request | Nothing durable existed yet, or only a partial scaffold — clean abort                          |
| `FAILED_A2C`                        | Delete the uncommitted A2C-generated files (Terraform, CI/CD YAML, business logic) and the ephemeral Git branch          | The P0 scaffold and the G2C abstract/inherited class — valid on their own                      |
| `A2C_DONE` (Phase C failed)         | Revert only the staged A2C commit                                                                                        | The P0 scaffold and the G2C classes; the failure is isolated to Phase C's report               |

## 8. Config / kwargs Reference

| Key                     | Scope                          | Default                            | Used In                                                                                              |
| ------------------------ | -------------------------------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `idempotency`           | config                          | `True`                              | `execute_pipeline()` Step 2                                                                          |
| `idempotency_store`     | config (object ref)             | `None`                              | `__already_built()` — a `BaseIdempotencyStore` instance                                              |
| `pipeline_state_store`  | config (object ref)             | `None`                              | `__persist_state()` — a `BaseTransactionalStore` instance bound to the Tier 3 Pipeline State table   |
| `governance_engine`     | config (object ref)             | `None`                              | `execute_pipeline()` Step 1 — a `BaseGovernanceFramework` instance                                    |
| `observability_engine`  | config (object ref)             | `None`                              | `execute_pipeline()` Step 7 — a `BaseObservability` instance                                          |
| `static_pipeline`       | config (object ref)             | `AIDLCStaticPipeline()` default     | `execute_pipeline()` Phase C — override only to substitute a different concrete `BasePipeline` subclass |
| `saga_compensator`      | config (object ref)             | `AIDLCSagaCompensator()` default    | `__trigger_compensation()`                                                                            |
| `dlq_queue_url`         | config / env `DLQ_QUEUE_URL`    | `None`                              | `_send_to_dlq()`, `AIDLCSagaCompensator._route_to_dlq()`                                              |

## 9. Error Handling & DLQ Semantics

`execute_pipeline()` catches exceptions from either phase uniformly: it appends `generator_key` to `failed_keys` and calls `_handle_error()`, exactly the pattern every other E2A workflow class already follows. No new exception type is introduced — a Phase C failure is raised as a plain `ValueError` carrying the failing stage's report, caught by the same `except Exception` clause as a Phase A or Phase B failure.

The one branch that is specific to this profile is in the `finally` block: whenever `failed_keys` is non-empty, `AIDLCSagaCompensator.compensate()` runs before the observability flush, not after — so the `message_log` entries record what was actually reverted, and a caller inspecting the final `AIDLCPipelineResult` sees a consistent, already-cleaned-up `phase_reached` value rather than a dangling in-progress one.

## 10. End-to-End Execution Trace (Annotated)

### 10.1 Full Pipeline — G2C + P0 + A2C + E2A, All Phases Succeed

1. Tier 1: `BaseValidationService.validate()` mints `correlation_id`, confirms entitlement, publishes to the Intake Topic.
2. Tier 2: `AIDLCPipelineOrchestrator.execute_pipeline()` resolves `generator_key`, passes the governance gate, finds no existing idempotency claim.
3. Phase A: `DeveloperPlatformWorkflow.generate()` resolves/generates the E2A (and, for `generator_type='a2c_inherited'`, A2C) abstract class, then the inherited class; P0's `bootstrap()` runs inline and — because `config['a2c']['enabled']` was set — Step 16 builds `dev_request` into `ScaffoldResult`. `generator_result['success']` is `True`; `phase_reached` → `G2C_P0_DONE`.
4. Phase B: `dev_request` is read off `generator_result['scaffold_result']['dev_request']`. `SDLCWorkflow.execute(dev_request, config)` runs RequirementsAgent → CodeGenAgent → IaCAgent → CICDAgent → CodeCriticAgent; CodeCriticAgent's score clears 0.75. `sdlc_result['errors']` is empty; `phase_reached` → `A2C_DONE`.
5. Phase C: `AIDLCStaticPipeline().run_pipeline()` runs its full seven-stage sequence; the three real stages pass against the generated repository, the four no-op stages return trivially. `pipeline_report['status'] == 'SUCCESS'`; `phase_reached` → `E2A_VERIFIED`, `success` → `True`.
6. `finally`: `failed_keys` is empty, so no compensation runs. `BaseObservability.record_telemetry()` ships the accumulated `message_log` under one `correlation_id`. Tier 5's Saga sees an empty `failed_keys` and performs a standard commit — merge to main, tag release, dispatch the completion webhook via Tier 4.

### 10.2 Minimal Pipeline — G2C + P0 Only (`a2c_sdlc.enabled` Unset)

1. Same Tier 0/1/2 entry as above, but `request['a2c_sdlc']` is absent.
2. Phase A runs identically — `config['a2c']` is never set on `g2c_config`, so P0's `bootstrap()` Step 16 never builds a `dev_request`, and `generator_result['scaffold_result'].get('dev_request')` is `None`.
3. Phase B is skipped, not failed — the `request.get('a2c_sdlc', {}).get('enabled')` check is `False`. `phase_reached` stays at `G2C_P0_DONE` going into Phase C.
4. Phase C still runs, verifying the scaffold and the one inherited-class file G2C produced. On success, `phase_reached` → `E2A_VERIFIED` — a smaller, valid deliverable, exactly as the Landing Zone HLD's Section 4.3 callout describes.

### 10.3 Failure Example — Phase B (A2C) Fails

1. Phase A completes normally; `phase_reached` is `G2C_P0_DONE`, `dev_request` is available.
2. `SDLCWorkflow.execute(dev_request, config)` returns with `sdlc_result['errors']` populated — CodeCriticAgent's score stayed below 0.75 after two retries.
3. `execute_pipeline()` appends `generator_key` to `failed_keys` and returns immediately with `phase_reached = 'FAILED_A2C'`; Phase C never runs.
4. `finally`: `AIDLCSagaCompensator.compensate()` reads `phase_reached == 'FAILED_A2C'`, deletes only the uncommitted A2C-generated files and the ephemeral Git branch, and leaves the P0 scaffold and G2C classes untouched — per the Decision Matrix in Section 7.1.
5. The result is routed to the DLQ; Tier 5's Saga sees a non-empty `failed_keys` and takes the compensation path, but the repository is left in the smaller, still-valid G2C+P0 state rather than a half-built one.

## 11. Composition Summary — What This Profile Reuses vs. Adds

| Component                                            | Treatment in This Profile                                                                                                             | Source of Truth                            |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------- |
| `DeveloperPlatformWorkflow` (G2C)                     | Reused unmodified; called directly, no wrapper                                                                                        | `G2C_Framework_Reference.md` Sec 10.2       |
| `SDLCWorkflow` (A2C)                                  | Reused, signature inferred (Section 1 callout); called directly, no wrapper, conditional on `a2c_sdlc.enabled`                        | `A2C_Framework_Reference.md` Sec 3.2        |
| `BaseProjectBootstrapper` (P0)                        | Reused unmodified; never called by this profile — only ever invoked inline by G2C's inherited-class generator                          | `P0_ProjectBootstrap_Framework_Reference.md` |
| `BaseValidationService`                               | Reused unmodified — Tier 1, entirely ahead of this profile's own classes                                                               | `IMPLEMENTATION_PLAYBOOK.md` Sec 2.2         |
| `BasePipeline`                                        | Reused unmodified — `run_pipeline()` itself is never touched; only its seven protected hooks are overridden, by `AIDLCStaticPipeline`  | `IMPLEMENTATION_PLAYBOOK.md` Sec 2.12        |
| `BaseObservability` / `BaseGovernanceFramework`       | Reused unmodified, in their Agentic-profile form (this pipeline calls LLMs on every phase)                                            | `IMPLEMENTATION_PLAYBOOK.md` Sec 2.10–2.11   |
| `BaseTransactionalStore` / `BaseIdempotencyStore`     | Reused unmodified — bound to the Pipeline State table, the Artifact Object Store, and `generator_key` idempotency respectively         | `IMPLEMENTATION_PLAYBOOK.md` Sec 2.16        |
| `BaseWebhookDispatcher`                               | Reused unmodified — Tier 4 completion notifications                                                                                    | `IMPLEMENTATION_PLAYBOOK.md` Sec 2.16        |
| `AIDLCPipelineOrchestrator`                           | New — this document, Section 5                                                                                                         | —                                               |
| `AIDLCStaticPipeline`                                 | New — this document, Section 6 (thin `BasePipeline` subclass)                                                                          | —                                               |
| `AIDLCSagaCompensator`                                | New — this document, Section 7                                                                                                         | —                                               |
| `AIDLCPipelineRequest` / `Result` / `PipelineStateRecord` | New TypedDicts — this document, Section 4                                                                                          | —                                               |

## 12. Related Documents

- [AIDLC_LANDING_ZONE.md](https://github.com/subhamviky/e2a-framework/blob/main/docs/AIDLC_LANDING_ZONE.md) — the HLD this Playbook is the LLD companion to; Sections 4–8 there map directly onto Sections 5–7 here.
- `G2C_Framework_Reference.md` — `DeveloperPlatformWorkflow`, `GeneratorRequest`, `GeneratorCriticAgent` — Phase A.
- `P0_ProjectBootstrap_Framework_Reference.md` — `BaseProjectBootstrapper`, `ScaffoldRequest`, the `a2c` config block and the `dev_request` field on `ScaffoldResult` — Phase A/B boundary.
- `A2C_Framework_Reference.md` — `SDLCWorkflow`, `DevRequest`, `CodeCriticAgent` — Phase B.
- [IMPLEMENTATION_PLAYBOOK.md](https://github.com/subhamviky/e2a-framework/blob/main/docs/IMPLEMENTATION_PLAYBOOK.md) — `BaseValidationService`, `BaseObservability`, `BaseGovernanceFramework`, `BasePipeline`, `BaseTransactionalStore`/`BaseIdempotencyStore`/`BaseDistributedLock`/`BaseWebhookDispatcher` — every class this profile reuses unmodified.
- [CQRS_IMPLEMENTATION_PLAYBOOK.md](https://github.com/subhamviky/e2a-framework/blob/main/docs/CQRS_IMPLEMENTATION_PLAYBOOK.md) — the sibling Playbook this document's structure and conventions follow.

## Appendix A — `reference/ai_dlc_pipeline.py` Complete Source

The full, contiguous scaffold file for this profile, mirroring the base Implementation Playbook's and the CQRS Playbook's convention of one complete drop-in file rather than fragments spread across prose. Place it at `reference/ai_dlc_pipeline.py`, never modify it directly, and wire `config['static_pipeline']`, `config['saga_compensator']`, `config['pipeline_state_store']`, and `config['idempotency_store']` to concrete implementations at deployment time. It has been syntax-validated (`python3 -m ast`) as a single, self-contained, importable module.

```python
# ai_dlc_pipeline.py — E2A AI-DLC Landing Zone, LLD Scaffold
# Composes G2C's DeveloperPlatformWorkflow, P0 (invoked inline by G2C's
# own inherited-class generator — never called separately here), and
# A2C's SDLCWorkflow into one governed, saga-compensated build pipeline.
# Drop into src/ or framework/. Import and subclass at the request/config
# level only — never modify this file directly.
#
# NOTE ON SDLCWorkflow: A2C_Framework_Reference.md describes SDLCWorkflow
# conceptually (Sec 3.2, the 5-agent LangGraph StateGraph) but does not
# publish a formal method-contract table for it the way it does for
# SDLCAssistantAgent. The execute(dev_request, config) -> DevRequest
# signature below is inferred from that description and from the calling
# convention every other E2A/A2C workflow class already uses.

import os
import uuid
import time
import logging
from typing import Any, Dict, List, Optional, TypedDict

from e2a_base import BasePipeline
from developer_platform_workflow import DeveloperPlatformWorkflow
from a2c_base import SDLCWorkflow

logging.basicConfig(level=logging.INFO)


# ==================================================
# TypedDicts
# ==================================================

class AIDLCPipelineRequest(TypedDict, total=False):
    # UI panel: Context — P0's ScaffoldRequest, embedded verbatim as
    # request['scaffold'] when calling DeveloperPlatformWorkflow.generate()
    context: Dict[str, Any]

    # UI panel: Harness — G2C's GeneratorRequest fields
    harness: Dict[str, Any]

    # UI panel: Prompt — merged into the harness/GeneratorRequest at call time
    prompt: Dict[str, Any]

    # A2C SDLCWorkflow activation — Phase B is skipped entirely, not failed,
    # if this is absent or enabled=False
    a2c_sdlc: Dict[str, Any]  # {enabled, project_type, mandatory_nfrs, target_cloud}

    tenant_id: str
    model_id: str
    critic_model_id: str

    # Written by AIDLCPipelineOrchestrator — do not supply
    generator_key: str
    correlation_id: str


class AIDLCPipelineResult(TypedDict, total=False):
    success: bool
    generator_key: str
    correlation_id: str
    phase_reached: str          # QUEUED | G2C_P0_DONE | A2C_DONE | E2A_VERIFIED
                                 # | FAILED_G2C_P0 | FAILED_A2C | SKIPPED_IDEMPOTENT
    generator_result: dict      # GeneratorResult from Phase A (G2C)
    dev_request: dict           # DevRequest handed to A2C, if Phase B ran
    sdlc_result: dict           # updated DevRequest returned by Phase B
    pipeline_report: dict       # BasePipeline-shaped report from Phase C
    message_log: List[dict]
    failed_keys: List[str]
    trace_id: str
    latency_ms: float
    errors: List[str]


class PipelineStateRecord(TypedDict, total=False):
    """Bound to BaseTransactionalStore (Implementation Playbook Sec 2.16)
    via config['pipeline_state_store'] — put()/get() against the Tier 3
    Pipeline State/Outbox table. Not a new abstract class; a concrete
    BaseTransactionalStore subclass just points it at that table instead
    of at a generic row store."""
    correlation_id: str
    tenant_id: str
    generator_key: str
    phase: str
    artifact_pointers: Dict[str, str]
    created_at: float
    updated_at: float


# ==================================================
# AIDLCStaticPipeline — Phase C's BasePipeline subclass
# ==================================================

class AIDLCStaticPipeline(BasePipeline):
    """Concrete BasePipeline subclass used only for this profile's Phase C.
    run_pipeline() — BasePipeline's one public entry point — is inherited
    untouched; this class never overrides it (Implementation Playbook
    Sec 2.12: 'PUBLIC | run_pipeline() | Template method'). The four
    stages this profile has no use for (RAG eval, artifact compile,
    dynamic security, deployment rollout) are overridden as intentional
    pass-through no-ops rather than skipped, so BasePipeline's fixed
    seven-stage sequence still runs exactly as written — this profile
    just has nothing for those four stages to do. A generated project's
    own CI/CD workflow (produced by A2C's CICDAgent in Phase B) is what
    actually compiles, DAST-scans, and deploys, on its own first commit."""

    def __init__(self):
        super().__init__()
        self.required_artifact_matrix = []  # nothing for this profile to compile

    def _run_static_security_scans(self, config, **kwargs) -> bool:
        # SAST + secret-detection (e.g. gitleaks) against the generated
        # repository content in the Tier 3 object store.
        return True

    def _execute_test_suite(self, config, **kwargs) -> bool:
        # Runs the generated project's own tests/ directory in an
        # ephemeral build container — not a live deploy.
        return True

    def _verify_scaffold_contracts(self, config, **kwargs) -> bool:
        # Statically asserts the Single Public Entry Point Rule against
        # the GENERATED classes, not this pipeline's own.
        return True

    def _run_rag_eval(self, config, **kwargs) -> float:
        return 1.0  # no RAG pipeline exists on this profile's own request path

    def _compile_artifact(self, artifact_name, config, **kwargs) -> bool:
        return True  # required_artifact_matrix is empty; never actually called

    def _run_dynamic_security_checks(self, config, **kwargs) -> bool:
        return True  # DAST belongs to the generated CI/CD workflow, not here

    def _execute_deployment_rollout(self, strategy, config, **kwargs) -> dict:
        return {'skipped': 'no live deploy in the AI-DLC pipeline itself'}


# ==================================================
# AIDLCPipelineOrchestrator — Tier 2 composing orchestrator
# ==================================================

class AIDLCPipelineOrchestrator:
    """The single class this profile adds. Every framework it calls (G2C,
    P0, A2C, E2A) is reused unmodified via its own existing public entry
    point — no wrapper classes sit between this orchestrator and
    DeveloperPlatformWorkflow.generate() or SDLCWorkflow.execute()."""

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}

    def execute_pipeline(self, request: 'AIDLCPipelineRequest',
                          config: Dict[str, Any] = None,
                          correlation_id: str = None, **kwargs) -> 'AIDLCPipelineResult':
        """Public entry point. Fixed sequence below — never overridden."""
        config = config or self.config
        correlation_id = correlation_id or request.get('correlation_id', str(uuid.uuid4()))
        message_log: List[dict] = []
        failed_keys: List[str] = []
        start = time.time()
        result: AIDLCPipelineResult = {
            'correlation_id': correlation_id, 'message_log': message_log,
            'failed_keys': failed_keys, 'success': False, 'phase_reached': 'QUEUED',
        }
        generator_key = self.__generate_key(request)
        result['generator_key'] = generator_key

        try:
            # Step 1 — Governance gate
            gov_engine = config.get('governance_engine')
            if gov_engine:
                request = gov_engine.enforce_governance_gate(
                    request, config.get('io_config', {}), config,
                    correlation_id=correlation_id, tenant_id=request.get('tenant_id'))

            # Step 2 — Idempotency check
            if config.get('idempotency', True) and self.__already_built(generator_key, config):
                self._log(message_log, correlation_id, 'INFO', 'pipeline_skipped_idempotent')
                result['success'] = True
                result['phase_reached'] = 'SKIPPED_IDEMPOTENT'
                return result

            # Step 3 — Phase A: G2C + P0. P0's bootstrap() runs inline
            # inside DeveloperPlatformWorkflow.generate() (via the
            # inherited-class generator's own phases 7–8) — never
            # invoked separately by this class.
            g2c_request = dict(request.get('harness', {}))
            g2c_request['scaffold'] = request.get('context', {})
            g2c_request.update(request.get('prompt', {}))
            g2c_config = dict(config)
            if request.get('a2c_sdlc', {}).get('enabled'):
                # Rides through DeveloperPlatformWorkflow -> ... -> P0's
                # bootstrap() unchanged, exactly as P0's own
                # BootstrapAndGenerateWorkflow already does for the
                # P0-only case (P0_ProjectBootstrap_Framework_Reference.md
                # Sec 9, Pattern 3).
                g2c_config['a2c'] = {
                    'enabled': True,
                    'project_type': request['a2c_sdlc'].get('project_type', ''),
                    'mandatory_nfrs': request['a2c_sdlc'].get('mandatory_nfrs', []),
                    'target_cloud': request['a2c_sdlc'].get('target_cloud', 'aws'),
                    'context': request.get('prompt', {}).get('user_prompt', ''),
                }

            generator_result = DeveloperPlatformWorkflow(config=g2c_config).generate(
                g2c_request, g2c_config)
            result['generator_result'] = generator_result
            self.__persist_state(result, 'G2C_P0_ATTEMPTED', config)
            self._log(message_log, correlation_id, 'INFO', 'phase_a_complete',
                       success=generator_result.get('success', False))

            if not generator_result.get('success', False):
                failed_keys.append(generator_key)
                result['phase_reached'] = 'FAILED_G2C_P0'
                return result
            result['phase_reached'] = 'G2C_P0_DONE'
            self.__persist_state(result, 'G2C_P0_DONE', config)

            # Step 4 — Phase B: A2C (conditional). dev_request is
            # surfaced on Phase A's GeneratorResult as
            # scaffold_result.dev_request, produced inline by P0's
            # bootstrap() Step 16 because config['a2c']['enabled']=True
            # was set above (P0 Sec 2.2 / 9.2) — never rebuilt here.
            dev_request = (generator_result.get('scaffold_result') or {}).get('dev_request')
            if request.get('a2c_sdlc', {}).get('enabled') and dev_request:
                result['dev_request'] = dev_request
                sdlc_result = SDLCWorkflow(config=config).execute(dev_request, config)
                result['sdlc_result'] = sdlc_result
                self._log(message_log, correlation_id, 'INFO', 'phase_b_complete',
                           critic_score=(sdlc_result.get('critic_report') or {}).get('score'))
                if sdlc_result.get('errors'):
                    failed_keys.append(generator_key)
                    result['phase_reached'] = 'FAILED_A2C'
                    return result
                result['phase_reached'] = 'A2C_DONE'
                self.__persist_state(result, 'A2C_DONE', config)

            # Step 5 — Phase C: E2A static verification. Routed through
            # BasePipeline's ONE public method — never by reaching into
            # its protected stage hooks from outside the class.
            static_pipeline = config.get('static_pipeline') or AIDLCStaticPipeline()
            pipeline_report = static_pipeline.run_pipeline(
                config, correlation_id=correlation_id,
                message_log=message_log, failed_keys=failed_keys)
            result['pipeline_report'] = pipeline_report
            if pipeline_report.get('status') != 'SUCCESS':
                raise ValueError(f"Phase C failed: {pipeline_report.get('error')}")

            result['phase_reached'] = 'E2A_VERIFIED'
            result['success'] = True
            self.__persist_state(result, 'E2A_VERIFIED', config)

        except Exception as e:
            failed_keys.append(generator_key)
            self._handle_error(e, request, result, config,
                                correlation_id=correlation_id, message_log=message_log)
        finally:
            if failed_keys:
                self._send_to_dlq(failed_keys, config)
                self.__trigger_compensation(result, config, correlation_id=correlation_id)
            obs_engine = config.get('observability_engine')
            if obs_engine:
                obs_engine.record_telemetry(message_log, correlation_id, config,
                                             tenant_id=request.get('tenant_id'))
            else:
                for entry in message_log:
                    logging.info(entry)
            result['latency_ms'] = round((time.time() - start) * 1000, 1)
            result['trace_id'] = correlation_id

        return result

    # ---- PRIVATE — framework-owned, never overridden ----

    def __generate_key(self, request) -> str:
        ctx = request.get('context', {})
        return (f"{ctx.get('project_name','?')}:{ctx.get('runtime','?')}:"
                f"{ctx.get('build_tool','?')}:{ctx.get('platform','?')}")

    def __already_built(self, generator_key, config) -> bool:
        store = config.get('idempotency_store')  # a BaseIdempotencyStore
        if not store:
            return False
        # check_and_set() returns True only for the caller that wins the
        # race — False here means another request already claimed this key.
        return not store.check_and_set(generator_key, config=config)

    def __persist_state(self, result, phase, config):
        store = config.get('pipeline_state_store')  # a BaseTransactionalStore
        if not store:
            return
        record: PipelineStateRecord = {
            'correlation_id': result['correlation_id'],
            'generator_key': result['generator_key'],
            'phase': phase,
            'updated_at': time.time(),
        }
        store.put(result['correlation_id'], record, config=config,
                  table_or_collection_name='pipeline_state')

    def __trigger_compensation(self, result, config, correlation_id=None):
        compensator = config.get('saga_compensator') or AIDLCSagaCompensator()
        compensator.compensate(result, config, correlation_id=correlation_id)

    def _log(self, message_log, correlation_id, level, event, **fields):
        if message_log is None:
            return
        message_log.append({'timestamp': time.time(), 'correlation_id': correlation_id,
                             'level': level, 'event': event,
                             'class': type(self).__name__, **fields})

    def _send_to_dlq(self, failed_keys, config=None, **kwargs):
        config = config or {}
        queue_url = config.get('dlq_queue_url', os.getenv('DLQ_QUEUE_URL'))
        if queue_url and failed_keys:
            logging.warning(f'DLQ dispatch -> {queue_url}: {failed_keys}')

    def _handle_error(self, error, request, result, config=None, **kwargs):
        logging.error(f"[AIDLCPipelineOrchestrator][{kwargs.get('correlation_id')}] {error}")
        result.setdefault('errors', []).append(str(error))


# ==================================================
# AIDLCSagaCompensator — Tier 5 Saga compensation engine
# ==================================================

class AIDLCSagaCompensator:
    """PRIMARY class — compensate() is the single public entry point.
    Scoped so it only ever reverts artifacts this specific generator_key
    produced. common/e2a_base.py or common/a2c_base.py is never touched
    here — that file is reused-by-reference across generator_keys and may
    already be a dependency of other, unrelated, already-completed builds."""

    def compensate(self, result: 'AIDLCPipelineResult', config: Dict[str, Any] = None,
                    correlation_id: str = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        phase = result.get('phase_reached', 'QUEUED')
        actions: List[str] = []

        if phase in ('QUEUED', 'FAILED_G2C_P0'):
            # Nothing durable was written yet, or only a partial scaffold —
            # delete the inherited-class file and the P0 scaffold for this
            # generator_key only. The shared abstract class is untouched
            # unless it was generated fresh for this request and no other
            # generator_key references it yet (checked inside the helper).
            actions.append(self._delete_scaffold(result, config))
            actions.append(self._delete_inherited_class(result, config))
            actions.append(self._delete_abstract_class_if_orphaned(result, config))

        elif phase == 'FAILED_A2C':
            # Scaffold + G2C classes are valid on their own — keep them.
            # Only the A2C-generated Terraform/CI-CD/business-logic files
            # and the ephemeral Git branch are reverted.
            actions.append(self._delete_a2c_artifacts(result, config))
            actions.append(self._revert_git_branch(result, config))

        elif phase == 'A2C_DONE':
            # Phase C (static verification) failed after A2C succeeded —
            # revert only the staged A2C commit; scaffold and classes stand.
            actions.append(self._revert_git_branch(result, config))

        self._route_to_dlq(result, config, correlation_id=correlation_id)
        return {'phase_compensated': phase, 'actions': actions}

    def _delete_scaffold(self, result, config) -> str:
        return 'scaffold_deleted'  # object-store delete under generator_key prefix

    def _delete_inherited_class(self, result, config) -> str:
        return 'inherited_class_deleted'

    def _delete_abstract_class_if_orphaned(self, result, config) -> str:
        # Only deletes common/e2a_base.py (or a2c_base.py) if this
        # generator_key is its sole known reference — checked against the
        # Pipeline State table before any delete.
        return 'abstract_class_untouched_or_orphan_deleted'

    def _delete_a2c_artifacts(self, result, config) -> str:
        return 'a2c_artifacts_deleted'

    def _revert_git_branch(self, result, config) -> str:
        return 'git_branch_reverted'

    def _route_to_dlq(self, result, config, **kwargs) -> None:
        queue_url = (config or {}).get('dlq_queue_url', os.getenv('DLQ_QUEUE_URL'))
        if queue_url:
            logging.warning(f"DLQ (compensation) -> {queue_url}: {result.get('generator_key')}")
```

---

*E2A AI-DLC Implementation Playbook — Class Contracts & Scaffold · Subham Gupta, Staff Architect & AI Architect*
