"""Smoke tests for the AI-DLC pipeline concurrency contracts (T-13, T-14).
developer_platform_workflow and a2c_base are stubbed (not part of this repo copy).
Run: python smoke_test_aidlc_concurrency.py"""
import sys, threading, time, types
sys.path.insert(0, '.')

calls = {'g2c': 0}
GEN = {'success': True, 'abstract_class_path': 'common/e2a_base.py', 'abstract_class_generated_fresh': True,
       'scaffold_result': {}}
gate = threading.Event()
class _DPW:
    def __init__(self, config=None): pass
    def generate(self, req, cfg):
        calls['g2c'] += 1; gate.wait(2); time.sleep(0.05); return dict(GEN)
class _SDLC:
    def __init__(self, config=None): pass
m = types.ModuleType('developer_platform_workflow'); m.DeveloperPlatformWorkflow = _DPW
n = types.ModuleType('a2c_base'); n.SDLCWorkflow = _SDLC
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
check('T-14 sibling build keeps shared artifact referenced', 'B' in str(rec['refs']) or any('B:' in x for x in rec['refs']))
check('T-14 artifact not deleted while referenced', deleted == [])
# direct race: two compensations of two keys on one artifact, only creator+orphan may delete, never twice
store = InMemoryTransactionalStore(); cfg2 = {'artifact_store': store}
store.put_if_version('artifact:x', {'refs': ['k1', 'k2'], 'created_by': 'k1', 'deleted': False}, None)
comp = Comp(); deleted.clear()
def go(k): comp._delete_abstract_class_if_orphaned({'generator_key': k, 'generator_result': {'abstract_class_path': 'x'}}, cfg2)
ths = [threading.Thread(target=go, args=(k,)) for k in ('k1', 'k2')]
[t.start() for t in ths]; [t.join() for t in ths]
check('T-14 racing compensations of k1,k2: exactly one deletes, exactly once', deleted == ['x'])
store.put_if_version('artifact:y', {'refs': ['k1', 'k2'], 'created_by': 'k1', 'deleted': False}, None)
deleted.clear(); comp._delete_abstract_class_if_orphaned({'generator_key': 'k1', 'generator_result': {'abstract_class_path': 'y'}}, cfg2)
check('T-14 one referrer leaving while another remains: not deleted', deleted == [])
comp._delete_abstract_class_if_orphaned({'generator_key': 'k2', 'generator_result': {'abstract_class_path': 'y'}}, cfg2)
check('T-14 last referrer leaving deletes it', deleted == ['y'])

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
print(f'{ok} checks passed')
