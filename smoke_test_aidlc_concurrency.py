"""Smoke tests for the AI-DLC pipeline concurrency contracts (T-13, T-14).
developer_platform_workflow and a2c_base are stubbed (not part of this repo copy).
Run: python smoke_test_aidlc_concurrency.py"""
import sys, threading, time, types
sys.path.insert(0, '.')

calls = {'g2c': 0, 'last_req': None, 'sdlc_in': None}
GEN = {'success': True, 'abstract_class_path': 'common/e2a_base.py', 'abstract_class_generated_fresh': True,
       'scaffold_result': {}}
gate = threading.Event()
class _DPW:
    """Stands in for G2C: like the real DeveloperPlatformWorkflow it registers
    its own generator_key on the shared abstract class through
    config['artifact_registry'] (compare-and-set) when generation succeeds."""
    def __init__(self, config=None): pass
    def generate(self, req, cfg):
        calls['g2c'] += 1; calls['last_req'] = dict(req); gate.wait(2); time.sleep(0.05)
        out = dict(GEN, generator_key='g2c-' + req['scaffold']['project_name'])
        if req.get('generator_type') in ('e2a_inherited', 'a2c_inherited'):
            out['scaffold_result'] = {'success': True, 'dev_request': {
                'project_name': req['scaffold']['project_name'], 'project_type': 'svc',
                'mandatory_nfrs': [], 'target_cloud': 'aws'}}
        reg = cfg.get('artifact_registry')
        if reg is not None:
            while True:
                cur = reg.get_versioned(out['abstract_class_path'])
                rec, ver = (cur if cur else ({'refs': [], 'creator': None, 'deleted': False}, None))
                if out['generator_key'] in rec['refs']: break
                creator = rec['creator'] or (out['generator_key'] if out['abstract_class_generated_fresh'] and not rec['refs'] else None)
                if reg.compare_and_set(out['abstract_class_path'], dict(rec, refs=rec['refs'] + [out['generator_key']], creator=creator), ver): break
        return out
class _SDLC:
    def __init__(self, config=None): pass
    def execute(self, dev_request, config):
        calls['sdlc_in'] = dev_request; dev_request['generated_code'] = 'x'; return dev_request
class _Validator:
    def validate(self, agent_name, state, config=None, correlation_id=None):
        missing = [k for k in ('project_name', 'project_type', 'mandatory_nfrs', 'target_cloud', 'tenant_id') if k not in state]
        return {'valid': not missing, 'errors': [f'missing field: {k}' for k in missing]}
m = types.ModuleType('developer_platform_workflow'); m.DeveloperPlatformWorkflow = _DPW
n = types.ModuleType('a2c_base'); n.SDLCWorkflow = _SDLC; n.DevPlatformValidationService = _Validator
sys.modules['developer_platform_workflow'], sys.modules['a2c_base'] = m, n

from e2a_base import ConcurrencyConflict, Deadline
from ai_dlc_pipeline import *
from ai_dlc_pipeline import _BuildAdmission
from e2a_inmemory_adapters import InMemoryIdempotencyStore, InMemoryTransactionalStore

ok = 0
def check(name, cond):
    global ok
    assert cond, name; ok += 1; print('PASS', name)

class StaticOK:
    def run_pipeline(self, config, **k): return {'status': 'SUCCESS'}
class StaticBad:
    def run_pipeline(self, config, **k): return {'status': 'FAILED', 'error': 'scan'}

def req(project='p1', tenant='t1'):
    return {'tenant_id': tenant, 'context': {'project_name': project, 'runtime': 'py', 'build_tool': 'pip', 'platform': 'aws'}}

def mk(**extra):
    c = {'idempotency_store': InMemoryIdempotencyStore(), 'pipeline_state_store': InMemoryTransactionalStore(),
         'artifact_store': InMemoryTransactionalStore(), 'static_pipeline': StaticOK()}
    c.update(extra); return c

# T-13 duplicate concurrent builds of one generator_key: exactly one runs
cfg = mk(); orch = AIDLCPipelineOrchestrator(cfg); out = []
gate.clear(); calls['g2c'] = 0
ths = [threading.Thread(target=lambda: out.append(orch.execute_pipeline(req(), cfg))) for _ in range(6)]
[t.start() for t in ths]; time.sleep(0.3); gate.set(); [t.join() for t in ths]
check('T-13 six concurrent duplicates -> one build', calls['g2c'] == 1)
check('T-13 losers get typed SKIPPED_IN_PROGRESS', sum(r['phase_reached'] == 'SKIPPED_IN_PROGRESS' for r in out) == 5)
check('T-13 winner verified', sum(r['phase_reached'] == 'E2A_VERIFIED' for r in out) == 1)
r = orch.execute_pipeline(req(), cfg)
check('T-13 later duplicate replays as SKIPPED_IDEMPOTENT', r['phase_reached'] == 'SKIPPED_IDEMPOTENT' and calls['g2c'] == 1)

# failed build releases the key (retryable) and can be rebuilt
gate.set(); cfg = mk(static_pipeline=StaticBad()); orch = AIDLCPipelineOrchestrator(cfg)
r1 = orch.execute_pipeline(req('p2'), cfg)
cfg['static_pipeline'] = StaticOK()
r2 = orch.execute_pipeline(req('p2'), cfg)
check('failed build -> retry allowed and succeeds', not r1['success'] and r2['success'])

# T-14 shared abstract class: B references it, A fails -> NOT deleted; sole creator failing -> deleted
deleted = []
class Comp(AIDLCSagaCompensator):
    def _delete_object(self, path, config): deleted.append(path)
cfg = mk(saga_compensator=Comp(), static_pipeline=StaticBad()); orch = AIDLCPipelineOrchestrator(cfg)
gate.set()
rA = orch.execute_pipeline(req('A'), cfg)            # fails Phase C after G2C -> phase E2A? compensation by phase
cfgB = dict(cfg, static_pipeline=StaticOK()); rB = orch.execute_pipeline(req('B'), cfgB)
rec = cfg['artifact_store'].get_versioned('artifact:common/e2a_base.py')[0]
check('T-14 one ownership record, G2C identity: failed A released, B registered', rec['refs'] == ['g2c-B'])
check('T-14 artifact not deleted while referenced', deleted == [])
# direct: releases by G2C key; deletion opt-in, creator only, last reference only, at most once
store = InMemoryTransactionalStore(); comp = Comp()
def rel(k, path, cfg_): return comp._delete_abstract_class_if_orphaned(
    {'generator_result': {'generator_key': k, 'abstract_class_path': path}}, cfg_)
off, on = {'artifact_store': store}, {'artifact_store': store, 'compensation_delete_orphan_abstract': True}
store.put_if_version('artifact:w', {'refs': ['k1'], 'created_by': 'k1', 'deleted': False}, None)
deleted.clear(); r = rel('k1', 'w', off)
check('T-14 default: creator releasing the last reference keeps the file', r == 'abstract_reference_released' and deleted == [])
store.put_if_version('artifact:y', {'refs': ['k1', 'k2'], 'created_by': 'k1', 'deleted': False}, None)
deleted.clear(); rel('k2', 'y', on); rel('k1', 'y', on)
check('T-14 opt-in: creator leaving last deletes it', deleted == ['y'])
store.put_if_version('artifact:z', {'refs': ['k1', 'k2'], 'created_by': 'k1', 'deleted': False}, None)
deleted.clear(); rel('k1', 'z', on); rel('k2', 'z', on)
check('T-14 opt-in: a non-creator leaving last never deletes', deleted == [])
store.put_if_version('artifact:x', {'refs': ['k1', 'k2'], 'created_by': 'k1', 'deleted': False}, None)
deleted.clear()
ths = [threading.Thread(target=rel, args=(k, 'x', on)) for k in ('k1', 'k2')]
[t.start() for t in ths]; [t.join() for t in ths]
check('T-14 racing releases: at most one delete, never twice', deleted in ([], ['x']))

# admission: bounded, typed rejection, no leak
cfg = mk(); a = _BuildAdmission()
c = {'tenant_build_max_inflight': 1, 'build_max_inflight': 4, 'permit_timeout': 0.1}
rel = a.acquire('t', c)
t0 = time.monotonic()
try: a.acquire('t', c); got = False
except AdmissionTimeout: got = True
check('admission: second build for same tenant rejected within bound', got and time.monotonic() - t0 < 1)
rel(); a.acquire('t', c)()
check('admission: slot released and reusable', True)
r = a.acquire('other', c); r()
check('admission: other tenant unaffected', True)

# overload surfaces as REJECTED_OVERLOAD, releases the claim, no DLQ/compensation
cfg = mk(tenant_build_max_inflight=1, permit_timeout=0.1); orch = AIDLCPipelineOrchestrator(cfg)
hold = orch._admission.acquire('t1', cfg)
r = orch.execute_pipeline(req('q'), cfg)
hold()
r2 = orch.execute_pipeline(req('q'), cfg)
check('overload -> REJECTED_OVERLOAD (no failed_keys)', r['phase_reached'] == 'REJECTED_OVERLOAD' and not r['failed_keys'])
check('overload releases claim; retry then succeeds', r2['success'])

# expired deadline stops before Phase A work
cfg = mk(); orch = AIDLCPipelineOrchestrator(cfg); calls['g2c'] = 0
time.sleep(0.01); r = orch.execute_pipeline(dict(req('d'), deadline=Deadline.after(-1)), cfg)
check('expired deadline: no build started', calls['g2c'] == 0 and not r['success'])
# ---- cross-framework fixes (E2A family system design X-1..X-8, X-11)
gate.set()
cfg = mk(); orch = AIDLCPipelineOrchestrator(cfg); calls['last_req'] = None
r = orch.execute_pipeline(dict(req('ix'), a2c_sdlc={'enabled': True}, harness={'generator_type': 'e2a_abstract'}), cfg)
check('X-1 A2C requested with an abstract-only harness -> REJECTED_INVALID, nothing runs',
      r['phase_reached'] == 'REJECTED_INVALID' and calls['last_req'] is None
      and cfg['idempotency_store'].state_of('aidlc:ix:py:pip:aws', 't1') is None)
calls['last_req'] = None
r = orch.execute_pipeline(dict(req('tx'), deadline=Deadline.after(120), harness={'generator_type': 'e2a_inherited'}), cfg)
check('X-2 tenant_id and deadline_ms reach the G2C request',
      calls['last_req']['tenant_id'] == 't1' and 0 < calls['last_req']['deadline_ms'] <= 120000)
check('X-4 build claim is namespaced aidlc:', cfg['idempotency_store'].state_of('aidlc:tx:py:pip:aws', 't1') == 'SUCCEEDED'
      and r['build_key'] == 'tx:py:pip:aws')
r = orch.execute_pipeline(dict(req('a2'), harness={'generator_type': 'e2a_inherited'},
                               a2c_sdlc={'enabled': True, 'project_type': 'svc'}), cfg)
check('X-3 dev_request validated, then A2C runs', r['phase_reached'] == 'E2A_VERIFIED' and calls['sdlc_in'] is not None)
check('X-8 A2C gets a copy: the build result is not enriched', 'generated_code' not in r['dev_request'])
check('X-3 validated dev_request carries the build tenant', r['dev_request']['tenant_id'] == 't1')
class LeaseSpy(InMemoryIdempotencyStore):
    def claim(self, key, config=None, **kw):
        self.lease = kw.get('lease_seconds'); return super().claim(key, config, **kw)
cfg = mk(idempotency_store=LeaseSpy()); orch = AIDLCPipelineOrchestrator(cfg)
orch.execute_pipeline(dict(req('ls'), deadline=Deadline.after(900)), cfg)
check('X-7 build lease outlives the deadline', cfg['idempotency_store'].lease >= 900)
deleted.clear()
cfg = mk(saga_compensator=Comp(), static_pipeline=StaticBad(), compensation_delete_orphan_abstract=True)
orch = AIDLCPipelineOrchestrator(cfg)
r = orch.execute_pipeline(dict(req('solo'), harness={'generator_type': 'e2a_inherited'}), cfg)
rec = cfg['artifact_store'].get_versioned('artifact:common/e2a_base.py')[0]
check('X-11 Phase C failure after Phase A releases the reference', r['phase_reached'] == 'G2C_P0_DONE' and rec['refs'] == [])
check('X-11 sole creator, opt-in: orphaned base deleted once', deleted == ['common/e2a_base.py'])
print(f'{ok} checks passed')
