"""Smoke tests for the concurrency contracts (T-01..T-12 subset).
Run: python smoke_test_concurrency.py"""
import asyncio, sys, threading, time
sys.path.insert(0, '.')
from e2a_base import *
from e2a_inmemory_adapters import *

ok = 0
def check(name, cond):
    global ok
    assert cond, name; ok += 1; print('PASS', name)
def run(c): return asyncio.run(c)

M = [{'role': 'system', 'content': 'be brief'}, {'role': 'user', 'content': 'hi'}]

class FakeLLM(BaseLLMAgentService):
    def __init__(self, *a, text='ok', delay=0.0, **k):
        super().__init__(*a, **k); self.text, self.delay, self.calls = text, delay, 0
        self.active = self.peak = 0
    async def _dispatch_upstream_call(self, messages, model_id, temperature, max_tokens,
                                      tools, stop_sequences, cache_handle, config, **kw):
        self.calls += 1; self.active += 1; self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.text == 'BOOM': raise RuntimeError('upstream down')
            return {'text': self.text, 'metadata': {'tokens_used': 10}}
        finally:
            self.active -= 1
    async def _dispatch_upstream_stream(self, *a, **k):
        yield 'x'

# ---- Deadline ----
d = Deadline.after(0.05)
check('Deadline remaining/expired/remaining_ms', 0 < d.remaining() <= 0.05 and not d.expired() and d.to_remaining_ms() <= 50)
time.sleep(0.06)
def _raises(fn, exc):
    try: fn()
    except exc: return True
    return False
check('Deadline.check raises when expired', _raises(lambda: d.check('x'), DeadlineExceeded))
check('Deadline from remaining ms crosses a boundary', 0.9 < Deadline.from_remaining_ms(1000).remaining() <= 1.0)

# ---- T-01 duplicate material requests: atomic claim ----
store = InMemoryIdempotencyStore()
results = []
def claimer(i): results.append(store.claim('K1', tenant_id='t1', owner=f'o{i}', fingerprint='f'))
ths = [threading.Thread(target=claimer, args=(i,)) for i in range(20)]
[t.start() for t in ths]; [t.join() for t in ths]
check('T-01 exactly one of 20 concurrent claimers wins', sum(r.status == ClaimStatus.CLAIMED for r in results) == 1
      and sum(r.status == ClaimStatus.IN_PROGRESS for r in results) == 19)
winner = next(r.owner for r in results if r.status == ClaimStatus.CLAIMED)
check('T-01 complete -> duplicates replay the stored result', store.complete('K1', {'response': 'R'}, tenant_id='t1', owner=winner)
      and store.claim('K1', tenant_id='t1', owner='x', fingerprint='f').status == ClaimStatus.REPLAY
      and store.claim('K1', tenant_id='t1', owner='x', fingerprint='f').result == {'response': 'R'})
check('T-01 same key + different payload -> CONFLICT', store.claim('K1', tenant_id='t1', fingerprint='OTHER').status == ClaimStatus.CONFLICT)
check('T-01 keys are tenant-scoped', store.claim('K1', tenant_id='t2', owner='z', fingerprint='f').status == ClaimStatus.CLAIMED)
check('check_and_set compat shim', InMemoryIdempotencyStore().check_and_set('A', tenant_id='t') is True)
# stale owner cannot complete after takeover
clk = [0.0]; s2 = InMemoryIdempotencyStore(clock=lambda: clk[0])
s2.claim('J', tenant_id='t', owner='old', lease_seconds=10); clk[0] = 11
check('expired claim -> RECOVERY_REQUIRED, never blind re-execution', s2.claim('J', tenant_id='t', owner='new').status == ClaimStatus.RECOVERY_REQUIRED)
check('reconciler takes over with recover=True; stale owner cannot complete',
      s2.claim('J', tenant_id='t', owner='rec', recover=True).status == ClaimStatus.CLAIMED
      and s2.complete('J', {}, tenant_id='t', owner='old') is False)

# ---- workflow integration: claim -> run -> complete / replay / UNKNOWN ----
class Ag(BaseAgent):
    runs = 0
    def _build_messages(self, state, ctx): return list(M)          # E2A v1.2 hooks (V13-01)
    def _evaluate_output(self, response, state, ctx): return 0.99
    def _fallback(self, state, ctx): return {'text': 'fb'}
    def _handle_error(self, error, state, ctx): state['err'] = str(error)
    def run(self, state, config=None, **k):
        Ag.runs += 1
        if state.get('unknown'): state['outcome'] = 'UNKNOWN'
        return super().run(state, config, **k)
class WF(BaseWorkflow):
    def _build_workflow(self, config=None, **k): return object()
    def _validate_workflow(self, w, config=None, **k): return True
    def _get_agent(self, intent, config=None, **k): return Ag()
    def _generate_idempotency_key(self, state, config=None, **k): return 'gen-' + state['order']
    def _handle_error(self, e, state, config=None, **k): state['err'] = str(e)
st = InMemoryIdempotencyStore()
cfg = {'llm_service': FakeLLM(text='hello'), 'idempotency_store': st, 'max_latency': 5.0}
Ag.runs = 0
a = WF().execute({'tenant_id': 't1', 'order': '1'}, cfg)
b = WF().execute({'tenant_id': 't1', 'order': '1'}, cfg)
check('workflow: duplicate request replays, agent ran once', Ag.runs == 1 and b['status'] == 'REPLAYED' and b['response'] == a['response'])
c = WF().execute({'tenant_id': 't1', 'order': '1', 'amount': 5, 'idempotency_key': 'gen-1'}, cfg)
check('workflow: same key, changed payload -> CONFLICT, not executed', c['status'] == 'CONFLICT' and Ag.runs == 1)
u = WF().execute({'tenant_id': 't1', 'order': '2', 'unknown': True}, cfg)
check('T-12 UNKNOWN outcome parks the key for reconciliation', st.state_of('gen-2', 't1') == 'UNKNOWN')
u2 = WF().execute({'tenant_id': 't1', 'order': '2', 'unknown': True}, cfg)
check('T-12 duplicate of an UNKNOWN request is not re-executed', u2['status'] == 'RECOVERY_REQUIRED' and Ag.runs == 2)
cfg_bad = {'llm_service': FakeLLM(text='BOOM'), 'idempotency_store': InMemoryIdempotencyStore(), 'max_latency': 5.0}
WF().execute({'tenant_id': 't1', 'order': '3'}, cfg_bad)
check('failed run is retryable (claimable again)', cfg_bad['idempotency_store'].state_of('gen-3', 't1') == 'FAILED_RETRYABLE')

# ---- reconciliation: confirms physical truth before replay ----
class Rec(BaseReconciliationService):
    def __init__(self, *a, truth, **k): super().__init__(*a, **k); self.truth, self.replays = truth, 0
    def _fetch_dlq_entries(self, config=None, **k): return [{'id': 1, 'idempotency_key': 'gen-2', 'tenant_id': 't1'}]
    def _confirm_physical_outcome(self, entry, config=None, **k): return self.truth
    def _replay_dlq_entry(self, entry, config=None, **k): self.replays += 1; return True
r = Rec(InMemoryLease(), st, truth=PhysicalOutcome.UNKNOWN); out = r.reconcile({})
check('T-12 UNKNOWN stays parked: no replay', r.replays == 0 and out['unresolved'] == [1] and st.state_of('gen-2', 't1') == 'UNKNOWN')
r = Rec(InMemoryLease(), st, truth=PhysicalOutcome.COMMITTED); out = r.reconcile({})
check('T-12 COMMITTED truth: marked done, never replayed', r.replays == 0 and st.state_of('gen-2', 't1') == 'SUCCEEDED')
st3 = InMemoryIdempotencyStore(); st3.claim('gen-9', tenant_id='t1', owner='x'); st3.fail('gen-9', False, tenant_id='t1', owner='x', outcome=PhysicalOutcome.UNKNOWN)
class Rec2(Rec):
    def _fetch_dlq_entries(self, config=None, **k): return [{'id': 9, 'idempotency_key': 'gen-9', 'tenant_id': 't1'}]
r = Rec2(InMemoryLease(), st3, truth=PhysicalOutcome.NOT_COMMITTED); out = r.reconcile({})
check('T-12 NOT_COMMITTED truth: replayed once, then SUCCEEDED', r.replays == 1 and st3.state_of('gen-9', 't1') == 'SUCCEEDED')

# ---- T-02 OCC: conditional commit ----
ts = InMemoryTransactionalStore()
ts.put_if_version('acct', {'v': 0}, None)
outs = []
def writer(i): outs.append(ts.put_if_version('acct', {'v': i}, 1))
ths = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
[t.start() for t in ths]; [t.join() for t in ths]
check('T-02 exactly one same-version writer commits; rest get typed CONFLICT',
      sum(o.committed for o in outs) == 1 and sum(o.status == 'CONFLICT' for o in outs) == 9 and ts.get_versioned('acct')[1] == 2)

# ---- T-09 lease + fencing ----
clk = [0.0]; lease = InMemoryLease(clock=lambda: clk[0])
h1 = lease.acquire('sweeper', 10)
check('lease: second acquire refused while held', lease.acquire('sweeper', 10) is None)
clk[0] = 11
h2 = lease.acquire('sweeper', 10)
check('lease: fencing token strictly increases', h2 and h2.fencing_token > h1.fencing_token)
check('lease: stale owner cannot renew; its release is a no-op', lease.renew(h1) is False and (lease.release(h1) or True) and lease.acquire('sweeper', 10) is None)
ts2 = InMemoryTransactionalStore()
ts2.put_if_version('rec', {'by': 'new'}, None, fencing_token=h2.fencing_token)
late = ts2.put_if_version('rec', {'by': 'old'}, 1, fencing_token=h1.fencing_token)
check('T-09 stale owner commit rejected as FENCED', late.status == 'FENCED' and ts2.get('rec') == {'by': 'new'})
class Sw(BaseOutboxSweeper):
    seen = []
    def _fetch_pending_records(self, config=None, **k): return [{'id': 1}]
    def _publish_pending_record(self, rec, config=None, **k): Sw.seen.append(k.get('fencing_token')); return True
o = Sw(InMemoryLease()).sweep({})
check('sweeper passes the lease fencing token to publish', o['status'] == 'SWEPT' and Sw.seen == [1])
check('sweeper skips when lease is held', (lambda L: (L.acquire('outbox_sweeper', 60), Sw(L).sweep({}))[1]['status'])(InMemoryLease()) == 'SKIPPED_LOCKED')

# ---- T-03 single-flight ----
cs = InMemoryCacheStore(); loads = []
async def hot():
    async def loader():
        loads.append(1); await asyncio.sleep(0.05); return 'v'
    return await asyncio.gather(*[cs.aget_or_load('k', loader, 60) for _ in range(30)])
vals = run(hot())
check('T-03 async: 30 concurrent misses -> 1 load', len(loads) == 1 and set(vals) == {'v'} and cs.single_flight_stats['followers'] == 29)
cs2 = InMemoryCacheStore(); n = []
def slow():
    n.append(1); time.sleep(0.05); return 'w'
ths = [threading.Thread(target=lambda: cs2.get_or_load('k', slow, 60)) for _ in range(20)]
[t.start() for t in ths]; [t.join() for t in ths]
check('T-03 threads: 20 concurrent misses -> 1 load', len(n) == 1)
def bad(): raise RuntimeError('db down')
cs3 = InMemoryCacheStore()
check('T-03 loader failure propagates and is not cached', _raises(lambda: cs3.get_or_load('z', bad), RuntimeError) and cs3.get('z') is None)

# ---- T-04 saturation: bounded waits + tenant fairness ----
s = FakeLLM(delay=0.3, config={'pod_concurrency': 2, 'permit_timeout': 0.05})
async def burst(): return await asyncio.gather(*[s.process_inference(M, tenant_id='t1') for _ in range(6)])
rs = run(burst())
ok_n = sum(r['status'] == 'SUCCESS' for r in rs); rej = [r for r in rs if r['status'] == 'FAILED']
check('T-04 permit wait is bounded: excess rejected early, not queued', ok_n == 2 and len(rej) == 4 and all('Admission Timeout' in r['error'] for r in rej) and s.peak == 2)
check('T-04 permits fully released afterwards', s.inflight == 0 and s._pod_semaphore._value == 2)
s = FakeLLM(delay=0.15, config={'pod_concurrency': 4, 'tenant_concurrency': 2, 'permit_timeout': 0.02})
async def mixed():
    a = [s.process_inference(M, tenant_id='noisy') for _ in range(6)]
    b = [s.process_inference(M, tenant_id='quiet') for _ in range(2)]
    return await asyncio.gather(*a, *b)
rs = run(mixed()); quiet = rs[6:]
check('T-04 noisy tenant cannot starve a quiet tenant', all(r['status'] == 'SUCCESS' for r in quiet) and s.peak <= 4)
s = FakeLLM(config={'tenant_concurrency': 1, 'tenant_idle_evict_seconds': 0.01})
for i in range(5): run(s.process_inference(M, tenant_id=f't{i}'))
time.sleep(0.03); run(s.process_inference(M, tenant_id='last'))
check('idle tenant permit entries are evicted', len(s._tenant_permits) <= 2)

# ---- deadline bounds the LLM service ----
s = FakeLLM(delay=0.5)
t0 = time.monotonic(); r = run(s.process_inference(M, tenant_id='t', deadline=0.05))
check('deadline bounds the upstream call', time.monotonic() - t0 < 0.3 and r['status'] == 'FAILED' and r['error_type'] == 'DeadlineExceeded')
r = run(s.process_inference(M, tenant_id='t', deadline=Deadline.after(0)))
check('expired deadline rejected before any work', r['status'] == 'FAILED' and s.calls == 1)

# ---- T-10 cancellation releases permits and quota ----
s = FakeLLM(delay=1.0, config={'pod_concurrency': 1})
async def cancel_case():
    before = (s._rpm_tokens, s._tpm_tokens)
    t = asyncio.create_task(s.process_inference(M, tenant_id='t'))
    await asyncio.sleep(0.05)
    inflight_during = s.inflight
    t.cancel()
    try: await t
    except asyncio.CancelledError: pass
    return before, inflight_during
before, during = run(cancel_case())
check('T-10 cancel mid-call: permit released', during == 1 and s.inflight == 0 and s._pod_semaphore._value == 1)
check('T-10 cancel mid-call: TPM refunded, cleanup counted', s._tpm_tokens >= before[1] - 1 and s.stats['cancelled_cleanups'] == 1)
s = FakeLLM(delay=1.0, config={'pod_concurrency': 1, 'permit_timeout': 5.0})
async def cancel_waiter():
    holder = asyncio.create_task(s.process_inference(M, tenant_id='t'))
    await asyncio.sleep(0.05)
    rpm_before = s._rpm_tokens
    waiter = asyncio.create_task(s.process_inference(M, tenant_id='t'))
    await asyncio.sleep(0.05); waiter.cancel()
    try: await waiter
    except asyncio.CancelledError: pass
    rpm_after = s._rpm_tokens
    holder.cancel()
    try: await holder
    except asyncio.CancelledError: pass
    return rpm_before, rpm_after
rb, ra = run(cancel_waiter())
check('T-10 cancel while waiting for a permit consumes no quota', ra >= rb - 0.01 and s.inflight == 0 and s._pod_semaphore._value == 1)

# ---- T-11 graceful shutdown ----
s = FakeLLM(delay=0.2)
async def shutdown():
    t = asyncio.create_task(s.process_inference(M, tenant_id='t'))
    await asyncio.sleep(0.05)
    drained = asyncio.create_task(s.drain(timeout=2.0))
    await asyncio.sleep(0.01)
    late = await s.process_inference(M, tenant_id='t')
    return (await t), late, await drained, s.ready
inflight_r, late_r, drained, ready = run(shutdown())
check('T-11 drain: in-flight finishes, new work rejected, readiness flips',
      inflight_r['status'] == 'SUCCESS' and late_r['status'] == 'FAILED' and 'draining' in late_r['error'] and drained and not ready)

# ---- T-06 retry storm bounded by the budget; circuit opens ----
class Tool(BaseToolService):
    sent = 0
    def _get_endpoint(self, config=None, tenant_id=None, **k): return 'http://x'
    def _build_payload(self, payload, config=None, **k): return payload
    def _validate_input(self, payload, config=None, **k): return True
    async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **k):
        Tool.sent += 1; raise ConnectionError('down')
async def storm():
    t = Tool()
    cfg = {'retries': 3, 'retry_base_backoff': 0.001, 'retry_max_backoff': 0.002, 'circuit_failure_threshold': 10_000,
           'bulkhead_max_concurrent': 0}
    res = await asyncio.gather(*[t.execute({'a': 1}, cfg, idempotency_key=f'k{i}', commit_gate_enabled=False) for i in range(100)], return_exceptions=True)
    return t, res
t, res = run(storm())
check('T-06 retry amplification bounded by the budget (not 4x)', Tool.sent <= 100 + 10 + 20 + 1 and all(isinstance(r, ConnectionError) for r in res))
Tool.sent = 0
async def circuit():
    t = Tool(); cfg = {'retries': 0, 'circuit_failure_threshold': 3, 'circuit_reset_seconds': 60, 'bulkhead_max_concurrent': 0}
    out = []
    for i in range(6):
        try: await t.execute({'a': 1}, cfg, commit_gate_enabled=False)
        except Exception as e: out.append(type(e).__name__)
    return out
out = run(circuit())
check('circuit opens after the threshold; later calls fail fast', out == ['ConnectionError'] * 3 + ['CircuitOpenError'] * 3 and Tool.sent == 3)
class SlowTool(Tool):
    async def _send_request(self, *a, **k): SlowTool.sent += 1; await asyncio.sleep(1)
SlowTool.sent = 0
r = run(SlowTool().execute({'a': 1}, {'timeout': 0.05, 'retries': 3}, commit_gate_enabled=False))
check('T-12 timeout on a non-idempotent write -> UNKNOWN, sent once, never retried',
      r['outcome'] == PhysicalOutcome.UNKNOWN and r['physical_success'] is False and SlowTool.sent == 1)
class BHTool(Tool):
    async def _send_request(self, *a, **k): await asyncio.sleep(0.2); return {'status': 'ok', 'physical_success': True}
async def bulkhead():
    t = BHTool(); cfg = {'bulkhead_max_concurrent': 2, 'bulkhead_acquire_timeout': 0.02, 'timeout': 2.0}
    return await asyncio.gather(*[t.execute({'a': 1}, cfg, commit_gate_enabled=False) for _ in range(5)], return_exceptions=True)
res = run(bulkhead())
check('T-05 bulkhead: 2 admitted, the rest rejected within the bound',
      sum(isinstance(r, dict) for r in res) == 2 and sum(isinstance(r, BulkheadFullError) for r in res) == 3)

# ---- bridge cancels on deadline ----
class Slow(FakeLLM): pass
sv = Slow(delay=2.0)
t0 = time.monotonic()
try:
    sv_agent = type('A2', (Ag,), {})()
    sv_agent._infer(M, {'llm_service': sv, 'permit_timeout': 0.1, 'quota_timeout': 0.1, 'llm_max_latency': 0.2}, tenant_id='t', deadline=Deadline.after(0.1))
    raised = None
except Exception as e:
    raised = e
check('bridge: sync caller bounded by the deadline; coroutine cancelled', raised is not None and time.monotonic() - t0 < 1.0)
time.sleep(0.2)
check('bridge: nothing orphaned after the deadline', sv.inflight == 0)

# ---- capacity validation ----
check('validate_capacity: defaults are consistent', validate_capacity({}) == [])
v = validate_capacity({'fleet_replica_count': 10, 'db_pool_size': 20, 'db_max_connections': 100, 'pod_concurrency': 40, 'http_pool_size': 10,
                       'drain_timeout_seconds': 40, 'tenant_concurrency': 50, 'model_max_inflight': 200})
check('validate_capacity: reports every violated equation', len(v) == 5 and any('db_max_connections' in x for x in v) and any('drain' in x for x in v))
class P(BasePipeline):
    def _run_static_security_scans(self, c, **k): return True
    def _execute_test_suite(self, c, **k): return True
    def _verify_scaffold_contracts(self, c, **k): return True
    def _run_rag_eval(self, c, **k): return 1.0
    def _compile_artifact(self, a, c, **k): return True
    def _run_dynamic_security_checks(self, c, **k): return True
    def _execute_deployment_rollout(self, s_, c, **k): return {}
rep = P().run_pipeline({'drain_timeout_seconds': 99})
check('BasePipeline scaffold-compliance gate fails on a capacity violation', rep['status'] == 'FAILED' and 'Capacity' in rep['error'])
check('metric catalogue is defined once', 'e2a.llm.permit_wait_seconds' in E2A_METRICS and all('type' in m for m in E2A_METRICS.values()))
print(f'\n{ok} checks passed')
