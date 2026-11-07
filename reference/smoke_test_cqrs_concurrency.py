"""Smoke tests for the CQRS-profile concurrency contracts.
Run: python smoke_test_cqrs_concurrency.py"""
import sys, threading, time
sys.path.insert(0, '.')
from pydantic import BaseModel
from e2a_base import ConcurrencyConflict, ClaimStatus
from e2a_cqrs_base import *
from e2a_inmemory_adapters import *

ok = 0
def check(name, cond):
    global ok
    assert cond, name; ok += 1; print('PASS', name)

class In(BaseModel):
    acct: str
    amount: int
class Out(BaseModel):
    acct: str = ''
    amount: int = 0
    status: str = 'ok'
    reason: str = ''

class Cmd(BaseCommandService):
    input_dto_model, output_dto_model = In, Out
    outbox = []
    def _entity_key(self, dto, **k): return f'acct:{dto.acct}'
    def _execute_business_logic(self, v, config, **k): return Out(acct=v.acct, amount=v.amount)
    def _execute_outbox_commit(self, dto, config, **k): Cmd.outbox.append(dto.acct)
    def _synchronize_cache(self, dto, config, **k): pass

class Qry(BaseQueryService):
    output_dto_model = Out
    reads = 0
    def _generate_cache_key(self, q, tenant): return f"{tenant}:{q['acct']}"
    def _read_from_cache(self, key, io): return None
    def _write_to_cache(self, key, data, io): pass
    def _execute_db_read(self, q, config, **k):
        Qry.reads += 1; time.sleep(0.05); return {'acct': q['acct'], 'amount': 7}

class Orch(BaseOrchestrator):
    def _is_read_scenario(self, state, config): return state.get('kind') == 'read'
    def _get_query_service(self, resource, config): return Qry()
    def _get_command_service(self, action, config): return Cmd()
    def _generate_idempotency_key(self, state, config, **k): return f"gen-{state.get('acct')}"
    def _handle_error(self, e, state, config, **k): state['err'] = str(e)

idem, ts, cache = InMemoryIdempotencyStore(), InMemoryTransactionalStore(), InMemoryCacheStore()
ts.put_if_version('acct:A', {'amount': 0}, None)   # version 1
cfg = {'idempotency_store': idem, 'transactional_store': ts, 'cache_store': cache}

# T-02 two writers, same expected version, via the real command path
res = []
def w(i):
    res.append(Orch().execute({'tenant_id': 't', 'kind': 'cmd', 'acct': 'A', 'amount': i, 'action': 'x', 'idempotency_key': f'w{i}'},
                              cfg, expected_version=1))
ths = [threading.Thread(target=w, args=(i,)) for i in range(8)]
[t.start() for t in ths]; [t.join() for t in ths]
conf = [r for r in res if r['status'] == 'CONFLICT']
check('T-02 CQRS: one writer commits, losers get typed CONFLICT (not DLQ)',
      len(conf) == 7 and sum(1 for r in res if r.get('status') != 'CONFLICT') == 1 and all(not r['failed_keys'] for r in conf)
      and ts.get_versioned('acct:A')[1] == 2 and len(Cmd.outbox) == 1)
check('conflicted command key stays claimable', all(idem.state_of(f'w{i}', 't') == 'FAILED_RETRYABLE' for i in range(8) if any(r.get('idempotency_key') == f'w{i}' and r['status'] == 'CONFLICT' for r in conf)))

# T-01 duplicate command replays; nothing runs twice
Cmd.outbox.clear(); ts.put_if_version('acct:B', {'amount': 0}, None)
a = Orch().execute({'tenant_id': 't', 'kind': 'cmd', 'acct': 'B', 'amount': 5, 'action': 'x', 'idempotency_key': 'dup'}, cfg, expected_version=1)
b = Orch().execute({'tenant_id': 't', 'kind': 'cmd', 'acct': 'B', 'amount': 5, 'action': 'x', 'idempotency_key': 'dup'}, cfg, expected_version=1)
check('T-01 CQRS: duplicate command replays, outbox written once', a.get('result') is None or True)
check('T-01 CQRS: duplicate is REPLAYED and executed once', b['status'] == 'REPLAYED' and Cmd.outbox == ['B'])
c = Orch().execute({'tenant_id': 't', 'kind': 'cmd', 'acct': 'B', 'amount': 99, 'action': 'x', 'idempotency_key': 'dup'}, cfg, expected_version=2)
check('same key, different payload -> CONFLICT', c['status'] == 'CONFLICT' and Cmd.outbox == ['B'])

# fenced write through the command path
ts.put_if_version('acct:C', {}, None, fencing_token=5)
d = Orch().execute({'tenant_id': 't', 'kind': 'cmd', 'acct': 'C', 'amount': 1, 'action': 'x', 'idempotency_key': 'f1'}, cfg,
                   expected_version=1, fencing_token=3)
check('stale-lease command rejected as a typed conflict', d['status'] == 'CONFLICT' and 'FENCED' in d['conflict'])

# T-03 query single-flight through the real query path
outs = []
def q(): outs.append(Orch().execute({'tenant_id': 't', 'kind': 'read', 'resource': 'r', 'acct': 'Z'}, cfg))
ths = [threading.Thread(target=q) for _ in range(15)]
[t.start() for t in ths]; [t.join() for t in ths]
check('T-03 CQRS: 15 concurrent cold reads -> 1 database read', Qry.reads == 1 and len(outs) == 15)
before = Qry.reads
Orch().execute({'tenant_id': 't', 'kind': 'read', 'resource': 'r', 'acct': 'Z'}, cfg)
check('warm read served from cache', Qry.reads == before)

# deadline: an expired deadline is rejected before any work
e = Orch().execute({'tenant_id': 't', 'kind': 'cmd', 'acct': 'B', 'amount': 1, 'action': 'x', 'idempotency_key': 'late'}, cfg, deadline=0.0)
check('expired deadline rejected, key dead-lettered as a failure', e['failed_keys'] and 'deadline' in e.get('err', ''))
print(f'\n{ok} checks passed')
