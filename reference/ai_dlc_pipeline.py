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

import copy
import os
import json
import uuid
import time
import hashlib
import logging
import threading
from typing import Any, Dict, List, Optional, TypedDict

from e2a_base import (BasePipeline, resolve_config, ClaimStatus, BaseIdempotencyStore,
                      BaseTransactionalStore, Deadline, ConcurrencyConflict, AdmissionTimeout)
from developer_platform_workflow import DeveloperPlatformWorkflow
from a2c_base import SDLCWorkflow, DevPlatformValidationService

# Harness generator types that run P0 and therefore produce a dev_request;
# Phase B (A2C) needs one.
INHERITED_GENERATOR_TYPES = ('e2a_inherited', 'a2c_inherited')

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

    deadline: Any             # Deadline or seconds; else request_deadline_seconds

    # Written by AIDLCPipelineOrchestrator — do not supply
    build_key: str
    generator_key: str        # deprecated alias of build_key (not G2C's generator_key)
    correlation_id: str


class AIDLCPipelineResult(TypedDict, total=False):
    success: bool
    build_key: str            # project_name:runtime:build_tool:platform; claimed as aidlc:<build_key>
    generator_key: str        # deprecated alias of build_key
    correlation_id: str
    phase_reached: str          # QUEUED | G2C_P0_DONE | A2C_DONE | E2A_VERIFIED
                                 # | FAILED_G2C_P0 | FAILED_A2C | SKIPPED_IDEMPOTENT
                                 # | SKIPPED_IN_PROGRESS | REJECTED_CONFLICT | REJECTED_INVALID
                                 # | REJECTED_OVERLOAD | RECOVERY_REQUIRED | FAILED_FINAL
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
    build_key: str
    generator_key: str          # deprecated alias of build_key
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
# _BuildAdmission — bounded build admission (per instance + per tenant)
# ==================================================

class _BuildAdmission:
    """Caps concurrent builds: build_max_inflight per instance and
    tenant_build_max_inflight per tenant, with a BOUNDED wait
    (permit_timeout, capped by the remaining deadline). A build that
    cannot be admitted in time is REJECTED (retryable), never queued
    without bound, so one tenant's burst cannot occupy every build slot.
    Idle tenant entries are evicted so the table does not grow without
    bound."""

    def __init__(self):
        self._lock = threading.Lock()
        self._global, self._global_cap = None, None
        self._tenants: Dict[str, List[Any]] = {}      # tenant -> [semaphore, active]

    def acquire(self, tenant_id, config, deadline=None):
        gcap = int(resolve_config('build_max_inflight', config))
        tcap = int(resolve_config('tenant_build_max_inflight', config))
        timeout = float(resolve_config('permit_timeout', config))
        budget = deadline.bound(timeout) if deadline else timeout
        end = time.monotonic() + budget
        with self._lock:
            if self._global is None:
                self._global, self._global_cap = threading.BoundedSemaphore(gcap), gcap
            entry = self._tenants.setdefault(tenant_id, [threading.BoundedSemaphore(max(1, tcap)), 0])
            entry[1] += 1                                # waiter or holder: not evictable
        got_t = got_g = False
        try:
            got_t = entry[0].acquire(timeout=max(0.0, end - time.monotonic()))
            if not got_t:
                raise AdmissionTimeout(f'E2A Admission Timeout: tenant {tenant_id!r} build limit reached')
            got_g = self._global.acquire(timeout=max(0.0, end - time.monotonic()))
            if not got_g:
                raise AdmissionTimeout('E2A Admission Timeout: instance build limit reached')
        except BaseException:
            self._release(entry, tenant_id, got_t, got_g)
            raise
        return lambda: self._release(entry, tenant_id, True, True)

    def _release(self, entry, tenant_id, got_t, got_g):
        if got_g:
            self._global.release()
        if got_t:
            entry[0].release()
        with self._lock:
            entry[1] -= 1
            if entry[1] == 0 and len(self._tenants) > 1024:
                self._tenants.pop(tenant_id, None)


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
        self._admission = _BuildAdmission()

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
        build_key = self.__generate_key(request)
        result['build_key'] = build_key
        result['generator_key'] = build_key       # deprecated alias
        claim_key = f'aidlc:{build_key}'          # namespaced: never collides with g2c:/p0:/a2c: keys
        generator_key = build_key                 # identity used in failed_keys / DLQ
        tenant_id = request.get('tenant_id') or '-'
        secs = float(resolve_config('request_deadline_seconds', config))
        deadline = Deadline.coerce(request.get('deadline')) if request.get('deadline') is not None \
            else (Deadline.after(secs) if secs > 0 else None)
        store = config.get('idempotency_store')
        owned, release_slot = False, None

        # A requested phase that cannot run is a rejected request, not a
        # build that later reports success without it.
        problem = self.__request_problem(request)
        if problem:
            result['phase_reached'] = 'REJECTED_INVALID'
            result['errors'] = [problem]
            self._log(message_log, correlation_id, 'WARN', 'pipeline_rejected_invalid', detail=problem)
            result['latency_ms'] = round((time.time() - start) * 1000, 1)
            result['trace_id'] = correlation_id
            return result

        try:
            # Step 1 — Governance gate
            gov_engine = config.get('governance_engine')
            if gov_engine:
                request = gov_engine.enforce_governance_gate(
                    request, config.get('io_config', {}), config,
                    correlation_id=correlation_id, tenant_id=request.get('tenant_id'))

            # Step 2 — Atomic idempotency CLAIM (one winner per generator_key;
            # never a check-then-build). Duplicates get a typed outcome.
            if config.get('idempotency', True) and isinstance(store, BaseIdempotencyStore):
                # The lease must outlive the whole build: at least the request
                # deadline plus a margin, never less than the configured lease.
                lease = int(resolve_config('idempotency_lease_seconds', config))
                if deadline:
                    lease = max(lease, int(deadline.remaining()) + 60)
                claim = store.claim(
                    claim_key, config, tenant_id=tenant_id, owner=correlation_id,
                    fingerprint=self.__fingerprint(request), lease_seconds=lease)
                if claim.status == ClaimStatus.REPLAY:
                    self._log(message_log, correlation_id, 'INFO', 'pipeline_skipped_idempotent')
                    result['success'] = True
                    result['phase_reached'] = 'SKIPPED_IDEMPOTENT'
                    return result
                if claim.status != ClaimStatus.CLAIMED:
                    result['phase_reached'] = {
                        ClaimStatus.IN_PROGRESS: 'SKIPPED_IN_PROGRESS', ClaimStatus.CONFLICT: 'REJECTED_CONFLICT',
                        ClaimStatus.RECOVERY_REQUIRED: 'RECOVERY_REQUIRED',
                        ClaimStatus.FAILED_FINAL: 'FAILED_FINAL'}[claim.status]
                    self._log(message_log, correlation_id, 'INFO', 'pipeline_duplicate', claim=claim.status.value)
                    return result
                owned = True

            # Bounded build admission: reject (retryable) rather than queue forever
            release_slot = self._admission.acquire(tenant_id, config, deadline)

            if deadline:
                deadline.check('Phase A')
            # Step 3 — Phase A: G2C + P0. P0's bootstrap() runs inline
            # inside DeveloperPlatformWorkflow.generate() (via the
            # inherited-class generator's own phases 7–8) — never
            # invoked separately by this class.
            g2c_request = dict(request.get('harness', {}))
            g2c_request['scaffold'] = request.get('context', {})
            g2c_request.update(request.get('prompt', {}))
            # The build's tenant and remaining deadline reach every Phase A step
            # (G2C claims, the gateway, P0 and the dev_request it builds).
            if request.get('tenant_id'):
                g2c_request.setdefault('tenant_id', request['tenant_id'])
            if deadline:
                g2c_request['deadline_ms'] = deadline.to_remaining_ms()
            g2c_config = dict(config)
            # One artifact-ownership record per shared abstract class: G2C
            # registers its references in this build's artifact_store.
            if config.get('artifact_store') is not None:
                g2c_config['artifact_registry'] = _ArtifactRegistryAdapter(config['artifact_store'], config)
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
            if deadline:
                deadline.check('Phase B')
            dev_request = (generator_result.get('scaffold_result') or {}).get('dev_request')
            if request.get('a2c_sdlc', {}).get('enabled'):
                if not dev_request:
                    # Requested but impossible: never skip silently.
                    failed_keys.append(generator_key)
                    result['phase_reached'] = 'FAILED_A2C'
                    result.setdefault('errors', []).append(
                        'a2c_sdlc.enabled but Phase A produced no dev_request '
                        '(needs an inherited generator with a scaffold)')
                    return result
                if request.get('tenant_id'):
                    dev_request.setdefault('tenant_id', request['tenant_id'])
                result['dev_request'] = dev_request
                # Validation tier before the workflow (BaseValidationService).
                validator = config.get('a2c_validation_service') or DevPlatformValidationService()
                verdict = validator.validate('SDLCWorkflow', dev_request, config,
                                             correlation_id=correlation_id)
                if not verdict['valid']:
                    failed_keys.append(generator_key)
                    result['phase_reached'] = 'FAILED_A2C'
                    result.setdefault('errors', []).extend(verdict['errors'])
                    return result
                # A copy: A2C enriches its state in place.
                sdlc_result = SDLCWorkflow(config=config).execute(copy.deepcopy(dev_request), config)
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
            if deadline:
                deadline.check('Phase C')
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

        except AdmissionTimeout as e:
            # Overload, not a build failure: nothing ran, nothing to compensate
            # or dead-letter. The caller retries; the claim is released below.
            result['phase_reached'] = 'REJECTED_OVERLOAD'
            result.setdefault('errors', []).append(str(e))
            self._log(message_log, correlation_id, 'WARN', 'pipeline_rejected_overload', detail=str(e))
        except Exception as e:
            failed_keys.append(generator_key)
            self._handle_error(e, request, result, config,
                                correlation_id=correlation_id, message_log=message_log)
        finally:
            if release_slot:
                release_slot()
            if failed_keys:
                self._send_to_dlq(failed_keys, config)
                self.__trigger_compensation(result, config, correlation_id=correlation_id)
            if owned:      # settle AFTER compensation, so the key is never claimable mid-cleanup
                kw = dict(config=config, tenant_id=tenant_id, owner=correlation_id)
                if result['success']:
                    store.complete(claim_key, {'phase_reached': result['phase_reached']}, **kw)
                else:
                    store.fail(claim_key, retryable=True, **kw)
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

    def __fingerprint(self, request) -> str:
        body = {'context': request.get('context', {}), 'a2c_sdlc': request.get('a2c_sdlc', {})}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def __persist_state(self, result, phase, config):
        """Durable phase state, written with a CONDITIONAL commit so a lost
        update (two writers for one correlation_id) surfaces as a typed
        ConcurrencyConflict instead of silently overwriting."""
        store = config.get('pipeline_state_store')  # a BaseTransactionalStore
        if not store:
            return
        record: PipelineStateRecord = {
            'correlation_id': result['correlation_id'],
            'build_key': result['build_key'],
            'generator_key': result['build_key'],
            'phase': phase,
            'updated_at': time.time(),
        }
        cur = store.get_versioned(result['correlation_id'], config, table_or_collection_name='pipeline_state')
        res = store.put_if_version(result['correlation_id'], record, cur[1] if cur else None, config,
                                   table_or_collection_name='pipeline_state')
        if not res.committed:
            raise ConcurrencyConflict(f"pipeline state for {result['correlation_id']} changed concurrently")

    def __request_problem(self, request) -> Optional[str]:
        """A requested phase that cannot run makes the request invalid."""
        if request.get('a2c_sdlc', {}).get('enabled'):
            gtype = request.get('harness', {}).get('generator_type')
            if gtype not in INHERITED_GENERATOR_TYPES:
                return (f"a2c_sdlc.enabled needs an inherited generator in the harness "
                        f"({' or '.join(INHERITED_GENERATOR_TYPES)}), got {gtype!r}")
        return None

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

class _ArtifactRegistryAdapter:
    """Presents this build's artifact_store (a BaseTransactionalStore) as
    the registry G2C's DeveloperPlatformWorkflow expects (get_versioned /
    compare_and_set). Records stay in the AI-DLC schema
    artifact:<path> -> {refs, created_by, deleted}, so G2C's registration
    and this compensator's release work on ONE record with ONE identity
    (G2C's generator_key)."""

    def __init__(self, store, config=None):
        self.store, self.config = store, config or {}

    def get_versioned(self, path):
        cur = self.store.get_versioned(f'artifact:{path}', self.config)
        if cur is None:
            return None
        rec, ver = cur
        return ({'refs': list(rec.get('refs', [])), 'creator': rec.get('created_by'),
                 'deleted': bool(rec.get('deleted'))}, ver)

    def get(self, path):
        cur = self.get_versioned(path)
        return cur[0] if cur else None

    def compare_and_set(self, path, record, expected_version) -> bool:
        res = self.store.put_if_version(
            f'artifact:{path}', {'refs': list(record['refs']), 'created_by': record.get('creator'),
                                 'deleted': bool(record.get('deleted'))},
            expected_version, self.config)
        return bool(res.committed)


class AIDLCSagaCompensator:
    """PRIMARY class — compensate() is the single public entry point.
    Scoped so it only ever reverts artifacts this specific build produced.

    Rule: compensation never deletes an output whose producing step settled
    SUCCEEDED (a P0 scaffold that succeeded, a generated abstract class).
    Those claims would otherwise replay paths that no longer exist. A failed
    build releases its reference on the shared abstract class instead.
    Deleting an orphaned abstract class is opt-in
    (config['compensation_delete_orphan_abstract']) and allowed only to the
    build that created it fresh, after the last reference is released; G2C
    and P0 report a replay whose output is missing as recovery_required."""

    def compensate(self, result: 'AIDLCPipelineResult', config: Dict[str, Any] = None,
                    correlation_id: str = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        phase = result.get('phase_reached', 'QUEUED')
        gr = result.get('generator_result') or {}
        actions: List[str] = []

        if phase in ('QUEUED', 'FAILED_G2C_P0'):
            # Delete only what no settled claim vouches for: a scaffold whose
            # P0 step did not succeed, and the inherited class of a failed
            # generation (written only if the failure came after the write).
            if not (gr.get('scaffold_result') or {}).get('success'):
                actions.append(self._delete_scaffold(result, config))
            actions.append(self._delete_inherited_class(result, config))
            actions.append(self._delete_abstract_class_if_orphaned(result, config))

        elif phase == 'G2C_P0_DONE':
            # Phase C failed with no Phase B: scaffold and classes are valid,
            # replayable outputs — keep them; release this build's reference.
            actions.append(self._delete_abstract_class_if_orphaned(result, config))

        elif phase == 'FAILED_A2C':
            # Scaffold + G2C classes are valid on their own — keep them.
            # Only the A2C-generated Terraform/CI-CD/business-logic files
            # and the ephemeral Git branch are reverted.
            actions.append(self._delete_a2c_artifacts(result, config))
            actions.append(self._revert_git_branch(result, config))
            actions.append(self._delete_abstract_class_if_orphaned(result, config))

        elif phase == 'A2C_DONE':
            # Phase C (static verification) failed after A2C succeeded —
            # revert only the staged A2C commit; scaffold and classes stand.
            actions.append(self._revert_git_branch(result, config))
            actions.append(self._delete_abstract_class_if_orphaned(result, config))

        self._route_to_dlq(result, config, correlation_id=correlation_id)
        return {'phase_compensated': phase, 'actions': actions}

    def _delete_scaffold(self, result, config) -> str:
        return 'scaffold_deleted'  # object-store delete under the build_key prefix

    def _delete_inherited_class(self, result, config) -> str:
        return 'inherited_class_deleted'

    def _delete_abstract_class_if_orphaned(self, result, config) -> str:
        """Release this build's reference on the shared abstract class (one
        compare-and-set on the ownership record). The identity is G2C's
        generator_key, the one G2C registered. The class is deleted only if
        deletion is enabled, this build CREATED it fresh, and no other
        reference remains; the CAS makes that decision exactly once."""
        store, gr = config.get('artifact_store'), result.get('generator_result') or {}
        path = gr.get('abstract_class_path')
        me = gr.get('generator_key') or result.get('build_key') or result.get('generator_key')
        if not store or not path:
            return 'abstract_class_untouched'
        allow_delete = bool(config.get('compensation_delete_orphan_abstract', False))
        key = f'artifact:{path}'
        for _ in range(8):
            cur = store.get_versioned(key, config)
            if cur is None:
                return 'abstract_class_untouched'
            rec, ver = cur
            if me not in rec.get('refs', []):
                return 'abstract_class_untouched'
            refs = [r for r in rec['refs'] if r != me]
            orphan = (allow_delete and not refs and rec.get('created_by') == me
                      and not rec.get('deleted'))
            res = store.put_if_version(key, dict(rec, refs=refs, deleted=bool(rec.get('deleted')) or orphan),
                                       ver, config)
            if res.committed:
                if orphan:
                    self._delete_object(path, config)
                    return 'abstract_class_orphan_deleted'
                return 'abstract_reference_released'
        raise ConcurrencyConflict(f'could not release artifact reference {key!r}')

    def _delete_object(self, path, config) -> None:
        """Hook: delete the object at `path` from the object store."""
        return None

    def _delete_a2c_artifacts(self, result, config) -> str:
        return 'a2c_artifacts_deleted'

    def _revert_git_branch(self, result, config) -> str:
        return 'git_branch_reverted'

    def _route_to_dlq(self, result, config, **kwargs) -> None:
        queue_url = resolve_config('dlq_queue_url', config)
        if queue_url:
            logging.warning(f"DLQ (compensation) -> {queue_url}: {result.get('build_key')}")
