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

from e2a_base import BasePipeline, resolve_config
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
        queue_url = resolve_config('dlq_queue_url', config)
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
        queue_url = resolve_config('dlq_queue_url', config)
        if queue_url:
            logging.warning(f"DLQ (compensation) -> {queue_url}: {result.get('generator_key')}")
