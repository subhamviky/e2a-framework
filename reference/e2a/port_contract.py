"""Port contract kit (v1.4, design note D-02): one behavioural suite per foundation port.

Every adapter of a port, whatever its backend, must pass the same checks: the in-memory
reference adapters run them in E2A's own tests, the DynamoDB adapters (e2a-adapters) run them
on moto, and the Firestore, Cosmos DB and PostgreSQL adapters will run them too. So "an
adapter" has one definition.

Each check_* function takes a factory ``make(clock)`` that returns a FRESH, EMPTY adapter whose
time source is ``clock`` (a callable returning seconds), and runs every scenario against it.
It raises AssertionError naming the scenario that failed, and returns the names of the
scenarios that passed.

    from e2a.port_contract import FakeClock, check_idempotency_store
    passed = check_idempotency_store(lambda clock: MyStore(table, clock=clock))

Values given to the stores are JSON-compatible dicts: that is all a durable adapter can promise
to round-trip exactly.
"""
import threading
import uuid
from typing import Callable, List

from .core import ClaimStatus, PhysicalOutcome

__all__ = ['FakeClock', 'check_idempotency_store', 'check_transactional_store', 'check_lease',
           'check_checkpoint_store', 'check_all']

VALUE = {'amount': '100.25', 'count': 3, 'ratio': 0.5, 'ok': True, 'none': None,
         'nested': {'items': ['a', 'b'], 'unicode': 'Zürich €'}}


class FakeClock:
    """A controllable clock: seconds since an arbitrary start; advance() moves it forward."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Run:
    def __init__(self, port: str):
        self.port, self.passed = port, []

    def check(self, name: str, ok: bool, detail: str = '') -> None:
        if not ok:
            raise AssertionError(f'{self.port} contract: {name}' + (f' ({detail})' if detail else ''))
        self.passed.append(name)


def _race(n: int, fn: Callable[[int], object]) -> list:
    """Run fn(i) in n threads released together; return the results."""
    barrier, results, errors = threading.Barrier(n), [None] * n, []

    def worker(i):
        try:
            barrier.wait()
            results[i] = fn(i)
        except Exception as e:                    # pragma: no cover - surfaced below
            errors.append(e)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    return results


def _key(label: str) -> str:
    return f'{label}-{uuid.uuid4().hex[:8]}'


# ------------------------------------------------------------ idempotency
def check_idempotency_store(make: Callable[[FakeClock], object], race: int = 8) -> List[str]:
    """BaseIdempotencyStore: atomic claim, replay, conflict, lease expiry and recovery,
    stale owner, retryable and final failure, UNKNOWN outcome, explicit expiry."""
    r, clock = _Run('idempotency store'), FakeClock()
    s = make(clock)

    k = _key('claim')
    first = s.claim(k, tenant_id='t1', lease_seconds=30)
    r.check('the first claim is CLAIMED with an owner', first.status == ClaimStatus.CLAIMED and bool(first.owner))
    second = s.claim(k, tenant_id='t1', lease_seconds=30)
    r.check('a second claim while the first owns it is IN_PROGRESS',
            second.status == ClaimStatus.IN_PROGRESS and second.owner == first.owner, str(second))
    r.check('keys are scoped by tenant', s.claim(k, tenant_id='t2').status == ClaimStatus.CLAIMED)
    r.check('a caller that is not the owner cannot complete',
            s.complete(k, {'x': 1}, tenant_id='t1', owner='someone-else') is False)
    r.check('the owner completes', s.complete(k, VALUE, tenant_id='t1', owner=first.owner) is True)
    replay = s.claim(k, tenant_id='t1')
    r.check('a claim after success is REPLAY with the stored result, unchanged',
            replay.status == ClaimStatus.REPLAY and replay.result == VALUE, str(replay))
    r.check('complete() twice is refused (no longer IN_PROGRESS)',
            s.complete(k, {'y': 2}, tenant_id='t1', owner=first.owner) is False)

    k = _key('fp')
    a = s.claim(k, tenant_id='t1', fingerprint='A')
    r.check('the same key with another fingerprint is CONFLICT',
            s.claim(k, tenant_id='t1', fingerprint='B').status == ClaimStatus.CONFLICT)
    s.complete(k, {'done': True}, tenant_id='t1', owner=a.owner)
    r.check('the same key and fingerprint after success is REPLAY',
            s.claim(k, tenant_id='t1', fingerprint='A').status == ClaimStatus.REPLAY)

    k = _key('retry')
    a = s.claim(k, tenant_id='t1')
    r.check('fail(retryable=True) by the owner', s.fail(k, retryable=True, tenant_id='t1', owner=a.owner) is True)
    b = s.claim(k, tenant_id='t1')
    r.check('a FAILED_RETRYABLE key is claimable again, by a new owner',
            b.status == ClaimStatus.CLAIMED and b.owner != a.owner)
    r.check('the previous owner can no longer complete', s.complete(k, {}, tenant_id='t1', owner=a.owner) is False)

    k = _key('final')
    a = s.claim(k, tenant_id='t1')
    s.fail(k, retryable=False, tenant_id='t1', owner=a.owner)
    r.check('a FAILED_FINAL key is never re-executed', s.claim(k, tenant_id='t1').status == ClaimStatus.FAILED_FINAL)

    k = _key('lease')
    a = s.claim(k, tenant_id='t1', lease_seconds=10)
    clock.advance(5)
    r.check('within the lease: IN_PROGRESS', s.claim(k, tenant_id='t1').status == ClaimStatus.IN_PROGRESS)
    clock.advance(6)
    r.check('after the lease expired: RECOVERY_REQUIRED, never a silent second execution',
            s.claim(k, tenant_id='t1').status == ClaimStatus.RECOVERY_REQUIRED)
    b = s.claim(k, tenant_id='t1', recover=True, lease_seconds=10)
    r.check('recover=True takes over with a new owner', b.status == ClaimStatus.CLAIMED and b.owner != a.owner)
    r.check('the stale owner cannot complete after the takeover',
            s.complete(k, {'stale': True}, tenant_id='t1', owner=a.owner) is False)
    r.check('the new owner completes', s.complete(k, {'fresh': True}, tenant_id='t1', owner=b.owner) is True)
    r.check('the result is the new owner\'s', s.claim(k, tenant_id='t1').result == {'fresh': True})

    k = _key('unknown')
    a = s.claim(k, tenant_id='t1')
    s.fail(k, tenant_id='t1', owner=a.owner, outcome=PhysicalOutcome.UNKNOWN)
    r.check('an UNKNOWN outcome is RECOVERY_REQUIRED', s.claim(k, tenant_id='t1').status == ClaimStatus.RECOVERY_REQUIRED)
    r.check('a reconciler takes an UNKNOWN record over',
            s.claim(k, tenant_id='t1', recover=True).status == ClaimStatus.CLAIMED)

    k = _key('ttl')
    a = s.claim(k, tenant_id='t1', ttl_seconds=60)
    s.complete(k, {'old': True}, tenant_id='t1', owner=a.owner)
    clock.advance(30)
    r.check('before its explicit expiry the record replays', s.claim(k, tenant_id='t1').status == ClaimStatus.REPLAY)
    clock.advance(31)
    r.check('after its explicit expiry the key is claimable again (expiry is a field, not TTL timing)',
            s.claim(k, tenant_id='t1').status == ClaimStatus.CLAIMED)

    k = _key('race')
    results = _race(race, lambda i: s.claim(k, tenant_id='t1', lease_seconds=60))
    r.check(f'{race} concurrent claims of one key: exactly one CLAIMED',
            sum(1 for x in results if x.status == ClaimStatus.CLAIMED) == 1,
            str([x.status.value for x in results]))
    return r.passed


# ----------------------------------------------------------- transactional
def check_transactional_store(make: Callable[[FakeClock], object], race: int = 8) -> List[str]:
    """BaseTransactionalStore: create-if-absent, compare-and-set, conflict with the current
    version, fencing, unconditional put, delete, exact round-trip."""
    r = _Run('transactional store')
    s = make(FakeClock())
    k = _key('row')
    r.check('a missing key reads as None', s.get(k) is None and s.get_versioned(k) is None)
    c = s.put_if_version(k, VALUE, None)
    r.check('create-if-absent commits version 1', c.status == 'COMMITTED' and c.version == 1, str(c))
    c = s.put_if_version(k, {'other': 1}, None)
    r.check('create-if-absent on an existing key is CONFLICT with the current version',
            c.status == 'CONFLICT' and c.current_version == 1, str(c))
    r.check('the value round-trips exactly', s.get(k) == VALUE and s.get_versioned(k) == (VALUE, 1))
    c = s.put_if_version(k, {'v': 2}, 1)
    r.check('compare-and-set at the current version commits the next one', c.status == 'COMMITTED' and c.version == 2)
    c = s.put_if_version(k, {'v': 'stale'}, 1)
    r.check('a stale expected version is CONFLICT, never an overwrite',
            c.status == 'CONFLICT' and c.current_version == 2 and s.get(k) == {'v': 2}, str(c))

    k = _key('fence')
    s.put_if_version(k, {'v': 1}, None, fencing_token=5)
    c = s.put_if_version(k, {'v': 'late'}, 1, fencing_token=4)
    r.check('a write with an older fencing token is FENCED', c.status == 'FENCED' and s.get(k) == {'v': 1}, str(c))
    c = s.put_if_version(k, {'v': 2}, 1, fencing_token=6)
    r.check('a newer fencing token commits', c.status == 'COMMITTED' and c.version == 2)
    c = s.put_if_version(k, {'v': 3}, 2)
    r.check('a write without a token keeps the recorded fence', c.status == 'COMMITTED'
            and s.put_if_version(k, {'v': 'late'}, 3, fencing_token=5).status == 'FENCED')

    k = _key('put')
    r.check('put() writes unconditionally', s.put(k, {'a': 1}) is True and s.get_versioned(k) == ({'a': 1}, 1))
    s.put(k, {'a': 2})
    r.check('put() advances the version', s.get_versioned(k) == ({'a': 2}, 2))
    r.check('delete() removes, then reports nothing to delete', s.delete(k) is True and s.delete(k) is False
            and s.get(k) is None)

    k = _key('race')
    s.put_if_version(k, {'n': 0}, None)
    results = _race(race, lambda i: s.put_if_version(k, {'n': i}, 1))
    r.check(f'{race} concurrent compare-and-sets at one version: exactly one COMMITTED',
            sum(1 for x in results if x.status == 'COMMITTED') == 1, str([x.status for x in results]))
    return r.passed


# ------------------------------------------------------------------- lease
def check_lease(make: Callable[[FakeClock], object], race: int = 8) -> List[str]:
    """BaseDistributedLock: exclusive, owner-checked renew and release, expiry, strictly
    increasing fencing tokens (also across release and expiry)."""
    r, clock = _Run('lease'), FakeClock()
    s = make(clock)
    k = _key('lock')
    h = s.acquire(k, 30)
    r.check('acquire returns a handle', bool(h) and h.key == k and bool(h.owner_token))
    r.check('a held lease is not acquired again', s.acquire(k, 30) is None)
    r.check('another key is independent', bool(s.acquire(_key('other'), 30)))
    clock.advance(20)
    r.check('the owner renews', s.renew(h, 30) is True)
    clock.advance(20)
    r.check('a renewed lease is still held', s.acquire(k, 30) is None)
    forged = type(h)(h.key, 'not-the-owner', h.fencing_token, h.expires_at)
    r.check('a non-owner cannot renew', s.renew(forged, 30) is False)
    s.release(forged)
    r.check('a non-owner release changes nothing', s.acquire(k, 30) is None)
    s.release(h)
    h2 = s.acquire(k, 30)
    r.check('after release the lease is acquired again, with a greater fencing token',
            bool(h2) and h2.fencing_token > h.fencing_token)
    clock.advance(31)
    h3 = s.acquire(k, 30)
    r.check('after expiry another owner acquires it, with a greater fencing token',
            bool(h3) and h3.fencing_token > h2.fencing_token)
    r.check('the expired owner cannot renew', s.renew(h2, 30) is False)

    k = _key('race')
    results = _race(race, lambda i: s.acquire(k, 60))
    r.check(f'{race} concurrent acquires: exactly one handle', sum(1 for x in results if x) == 1)
    return r.passed


# -------------------------------------------------------------- checkpoint
def check_checkpoint_store(make: Callable[[FakeClock], object], race: int = 8) -> List[str]:
    """BaseCheckpointStore: save and load, tenant isolation, decide exactly once."""
    r = _Run('checkpoint store')
    s = make(FakeClock())

    def checkpoint():
        cid = _key('cp')
        return cid, {'checkpoint_id': cid, 'tenant_id': 't1', 'status': 'PENDING', 'step': 'approve',
                     'state': VALUE, 'idempotency_key': 'req-1', 'correlation_id': 'c-1',
                     'requested_by': 'agent', 'reason': 'amount above limit', 'created_at': 1.5}
    cid, cp = checkpoint()
    s.save(cp)
    loaded = s.load(cid, tenant_id='t1')
    r.check('a saved checkpoint loads unchanged', loaded == cp, str(loaded))
    r.check('another tenant never sees it', s.load(cid, tenant_id='t2') is None)
    r.check('an unknown id is None', s.load(_key('missing')) is None)
    r.check('the first decision wins', s.decide(cid, 'APPROVED', 'alice') is True)
    r.check('a second decision is refused', s.decide(cid, 'REJECTED', 'bob') is False)
    after = s.load(cid, tenant_id='t1')
    r.check('the decision is recorded', after['status'] == 'APPROVED' and after['decided_by'] == 'alice', str(after))
    r.check('an unknown checkpoint cannot be decided', s.decide(_key('missing'), 'APPROVED', 'x') is False)

    cid, cp = checkpoint()
    s.save(cp)
    results = _race(race, lambda i: s.decide(cid, 'APPROVED', f'user-{i}'))
    r.check(f'{race} concurrent decisions: exactly one wins', sum(1 for x in results if x) == 1)
    return r.passed


def check_all(idempotency=None, transactional=None, lease=None, checkpoint=None) -> dict:
    """Run the suites for the factories given; returns {port: [passed scenario names]}."""
    out = {}
    for name, fn, make in (('idempotency', check_idempotency_store, idempotency),
                           ('transactional', check_transactional_store, transactional),
                           ('lease', check_lease, lease), ('checkpoint', check_checkpoint_store, checkpoint)):
        if make is not None:
            out[name] = fn(make)
    return out
