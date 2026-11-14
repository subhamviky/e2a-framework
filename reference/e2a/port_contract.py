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

``race`` is how many threads each suite's last scenario races on one key (default 8). Pass a
value below 2 to leave that scenario out, and only where the BACKEND is not atomic under
threads (moto's in-process DynamoDB is not): the race then has to be proven on a real service
or a faithful emulator (DynamoDB Local, the Firestore and Cosmos DB emulators).
"""
import threading
import uuid
from typing import Callable, List

from .core import ClaimStatus, PhysicalOutcome

__all__ = ['FakeClock', 'check_idempotency_store', 'check_transactional_store', 'check_lease',
           'check_checkpoint_store', 'check_llm_service', 'check_message_broker', 'check_all']

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

    if race >= 2:
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

    if race >= 2:
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

    if race >= 2:
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

    if race >= 2:
        cid, cp = checkpoint()
        s.save(cp)
        results = _race(race, lambda i: s.decide(cid, 'APPROVED', f'user-{i}'))
        r.check(f'{race} concurrent decisions: exactly one wins', sum(1 for x in results if x) == 1)
    return r.passed


# -------------------------------------------------------------- llm service
def check_llm_service(make: Callable[[Callable], object]) -> List[str]:
    """A model adapter (BaseLLMAgentService subclass) meets E2A's model contract (v1.6.0).

    ``make(responder)`` returns the adapter with its provider SDK pointed at a test transport
    that, for each wire request, decodes the conversation, calls ``responder(messages)`` and
    encodes the answer in the provider's wire format. The responder receives the canonical
    conversation (a list of {'role', 'content', ...} dicts) and returns:

        "text"                                   a plain answer
        {"text", "tool_calls", "stop_reason", "metadata", "provider_request_id"}
        {"error_status": 429, "retry_after": 0}  answer the HTTP request with that status
                                                 (and a Retry-After header)
    """
    import asyncio
    r = _Run('llm service')
    system, user = {'role': 'system', 'content': 'You are terse.'}, {'role': 'user', 'content': 'Say hello'}

    def scripted(*answers):
        seen = {'calls': 0, 'messages': None}

        def responder(messages):
            seen['calls'] += 1
            seen['messages'] = messages
            answer = answers[min(seen['calls'], len(answers)) - 1]
            return answer
        return responder, seen

    def infer(svc, **kw):
        return asyncio.run(svc.process_inference([system, user], tenant_id='t1', **kw))

    responder, seen = scripted('hello')
    out = infer(make(responder))
    r.check('a plain answer is SUCCESS with its text', out.get('status') == 'SUCCESS' and out.get('text') == 'hello',
            str(out))
    msgs = seen['messages'] or []
    r.check('the conversation reaches the provider in the canonical shape',
            any(m.get('role') == 'user' and 'Say hello' in str(m.get('content')) for m in msgs), str(msgs))

    responder, _ = scripted({'text': '', 'tool_calls': [{'id': 'c1', 'name': 'lookup', 'arguments': {'q': 'x'}}],
                             'stop_reason': 'tool_use'})
    out = infer(make(responder), tools=[{'name': 'lookup', 'description': 'find', 'parameters': {
        'type': 'object', 'properties': {'q': {'type': 'string'}}}}])
    calls = out.get('tool_calls') or []
    r.check('a tool call comes back with parsed arguments',
            len(calls) == 1 and calls[0].get('name') == 'lookup' and calls[0].get('arguments') == {'q': 'x'}, str(out))

    responder, _ = scripted('{"amount": 12}')
    out = infer(make(responder), response_schema={'type': 'object', 'properties': {'amount': {'type': 'integer'}}})
    r.check('structured output is parsed', out.get('structured_output') == {'amount': 12}, str(out))

    responder, _ = scripted('hello streaming world')

    async def stream(svc):
        return ''.join([c async for c in svc.process_inference_stream([system, user], tenant_id='t1')])
    r.check('a stream yields the answer', asyncio.run(stream(make(responder))).replace(' ', '') == 'hellostreamingworld')

    responder, _ = scripted({'text': 'I cannot help with that.', 'stop_reason': 'refusal'})
    out = infer(make(responder))
    r.check('a provider refusal is REFUSED, not an answer', out.get('status') == 'REFUSED', str(out))

    responder, _ = scripted('The SSN is 123-45-6789.')
    out = infer(make(responder))
    r.check("E2A's egress DLP still applies", '123-45-6789' not in str(out.get('text')), str(out))

    responder, seen = scripted({'error_status': 429, 'retry_after': 0}, 'after the throttle')
    out = infer(make(responder))
    r.check('a throttled call is retried by E2A', out.get('status') == 'SUCCESS' and seen['calls'] == 2
            and out.get('text') == 'after the throttle', f'{out} calls={seen["calls"]}')

    responder, _ = scripted({'text': 'ok', 'stop_reason': 'end_turn', 'provider_request_id': 'req-1'})
    out = infer(make(responder))
    r.check('the stop reason reaches the payload', out.get('stop_reason') == 'end_turn', str(out))

    responder, _ = scripted({'text': 'cut', 'stop_reason': 'max_tokens'})
    out = infer(make(responder))
    r.check('an answer cut by the token limit is marked truncated', out.get('truncated') is True, str(out))
    return r.passed


# ------------------------------------------------------------ messaging (v1.7.0)
def _poll(fn, timeout: float, until=lambda got: bool(got), step: float = 0.2):
    """Call fn() until until(result) or the timeout; a broker delivers asynchronously."""
    import time as _t
    end, got = _t.monotonic() + timeout, fn()
    while not until(got) and _t.monotonic() < end:
        _t.sleep(step)
        got = fn()
    return got


def check_message_broker(make: Callable[[], tuple], timeout: float = 15.0, store_factory: Callable = None) -> List[str]:
    """The behaviour every BaseMessageBroker adapter keeps, and the exactly-once guarantees
    IdempotentConsumer builds on it. ``make()`` returns ``(broker, destination, source)`` for a
    FRESH, EMPTY destination whose messages arrive at ``source`` (a queue: the same name; a
    topic: its subscription). ``store_factory()`` returns an empty BaseIdempotencyStore
    (default: the in-memory reference store). Waits are bounded by ``timeout`` seconds."""
    from .messaging import BrokerOutboxSweeper, IdempotentConsumer, InMemoryOutboxStore
    if store_factory is None:
        from e2a_inmemory_adapters import InMemoryIdempotencyStore as store_factory
    r = _Run('message broker')
    quiet = min(timeout, 2.0)

    def take(broker, source, n=1, dead=False):
        def once():
            got = (broker.receive_dead_letters(source, max_messages=n) if dead
                   else broker.receive(source, max_messages=n, wait_seconds=1.0))
            return got
        return _poll(once, timeout)

    # 1. a message arrives intact, and an acknowledged one never comes back
    broker, dest, source = make()
    attrs = {'tenant_id': 't1', 'idempotency_key': _key('k'), 'kind': 'order.created'}
    mid = broker.publish(dest, VALUE, attrs)
    got = take(broker, source)
    r.check('a published message is received', len(got) == 1, str(got))
    m = got[0]
    r.check('the body round-trips exactly', m.body == VALUE, str(m.body))
    r.check('the attributes round-trip', all(m.attributes.get(k) == v for k, v in attrs.items()), str(m.attributes))
    r.check('the message has an id, and the publish returned one', bool(m.id) and bool(mid))
    broker.ack(source, m)
    r.check('an acknowledged message is not delivered again',
            not _poll(lambda: broker.receive(source, max_messages=1, wait_seconds=0.5), quiet))

    # 2. a nacked message is delivered again, with a higher count where the broker counts
    broker, dest, source = make()
    broker.publish(dest, {'n': 1}, {'tenant_id': 't1', 'idempotency_key': _key('k')})
    first = take(broker, source)[0]
    broker.nack(source, first, 0)
    again = take(broker, source)
    r.check('a nacked message is delivered again', len(again) == 1 and again[0].body == {'n': 1}, str(again))
    if first.delivery_count is not None:
        r.check('the delivery count grows', (again[0].delivery_count or 0) > first.delivery_count,
                f'{first.delivery_count} -> {again[0].delivery_count}')
    broker.ack(source, again[0])

    # 3. dead letters: out of the source, into its dead letters with the reason, then settled
    broker, dest, source = make()
    broker.publish(dest, {'bad': True}, {'tenant_id': 't1', 'idempotency_key': _key('k')})
    m = take(broker, source)[0]
    broker.dead_letter(source, m, 'poison: schema')
    r.check('a dead-lettered message leaves the source',
            not _poll(lambda: broker.receive(source, max_messages=1, wait_seconds=0.5), quiet))
    dl = take(broker, source, dead=True)
    r.check('it is among the dead letters, with the reason',
            len(dl) == 1 and dl[0].body == {'bad': True} and 'poison' in dl[0].attributes.get('dead_letter_reason', ''),
            str(dl))
    broker.ack_dead_letter(source, dl[0])
    r.check('a settled dead letter is gone',
            not _poll(lambda: broker.receive_dead_letters(source, max_messages=1), quiet))

    # 4-7. the consumer's guarantees, on this broker
    class Effects(IdempotentConsumer):
        def __init__(self, broker, store, source, behaviour, truth=PhysicalOutcome.UNKNOWN):
            super().__init__(broker, store, source)
            self.behaviour, self.truth, self.effects = behaviour, truth, []

        def _handle(self, message, config, tenant_id, idempotency_key):
            return self.behaviour(self, message)

        def _confirm(self, message, config):
            return self.truth

    def commit(c, message):
        c.effects.append(message.body)
        return {'physical_success': True}

    def drain(consumer):
        reports = []
        for _ in range(4):
            got = _poll(lambda: consumer.consume(config={'consumer_retry_delay_seconds': 0}), quiet,
                        until=lambda rep: rep['received'] > 0)
            reports.append(got)
            if not got['received']:
                break
        return reports

    broker, dest, source = make()
    key = _key('dup')
    for _ in range(2):                                        # the same message, delivered twice
        broker.publish(dest, {'order': 'A'}, {'tenant_id': 't1', 'idempotency_key': key})
    consumer = Effects(broker, store_factory(), source, commit)
    reports = drain(consumer)
    r.check('a duplicate delivery has its effect once', consumer.effects == [{'order': 'A'}], str(consumer.effects))
    r.check('the duplicate is acknowledged as a duplicate', sum(x['duplicates'] for x in reports) == 1, str(reports))

    class LostAck:                                             # the ack never reaches the broker
        def __init__(self, inner):
            self.inner, self.lost = inner, 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def ack(self, source, message, config=None):
            if not self.lost:
                self.lost += 1
                return self.inner.nack(source, message, 0, config)
            return self.inner.ack(source, message, config)

    inner, dest, source = make()
    inner.publish(dest, {'order': 'B'}, {'tenant_id': 't1', 'idempotency_key': _key('ack')})
    consumer = Effects(LostAck(inner), store_factory(), source, commit)
    reports = drain(consumer)
    r.check('a lost acknowledgement does not repeat the effect', consumer.effects == [{'order': 'B'}],
            f'{consumer.effects} {reports}')

    def poison(c, message):
        raise ValueError('amount is not a number')

    class Settles:                                             # counts the dead letters settled
        def __init__(self, inner):
            self.inner, self.settled = inner, 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def ack_dead_letter(self, source, message, config=None):
            self.settled += 1
            return self.inner.ack_dead_letter(source, message, config)

    inner, dest, source = make()
    broker = Settles(inner)
    broker.publish(dest, {'order': 'C'}, {'tenant_id': 't1', 'idempotency_key': _key('poison')})
    consumer = Effects(broker, store_factory(), source, poison)
    reports = drain(consumer)
    r.check('a poison message is dead-lettered with its reason, not retried',
            sum(x['dead_lettered'] for x in reports) == 1 and sum(x['retried'] for x in reports) == 0
            and 'ValueError' in ' '.join(sum((x['dead_letter_reasons'] for x in reports), [])), str(reports))
    rec = _poll(lambda: consumer.reconcile({}), timeout, until=lambda rep: bool(consumer.settlements))
    r.check('reconcile sees the poison message and never replays it',
            rec['replayed'] == 0 and consumer.settlements[-1][1] == 'SKIPPED_FAILED_FINAL', f'{rec} {consumer.settlements}')
    r.check('the poison message is not settled: it stays among the dead letters for a person',
            broker.settled == 0, str(rec))

    def unknown(c, message):
        if c.behaviour_state.get('first', True):
            c.behaviour_state['first'] = False
            return {'physical_success': False, 'outcome': PhysicalOutcome.UNKNOWN}
        c.effects.append(message.body)
        return {'physical_success': True}

    for truth, expect_effects, label in ((PhysicalOutcome.NOT_COMMITTED, 1, 'replayed once'),
                                         (PhysicalOutcome.COMMITTED, 0, 'settled without a replay')):
        inner, dest, source = make()
        broker = Settles(inner)
        broker.publish(dest, {'order': 'D'}, {'tenant_id': 't1', 'idempotency_key': _key('unknown')})
        consumer = Effects(broker, store_factory(), source, unknown, truth)
        consumer.behaviour_state = {}
        drain(consumer)
        r.check('an UNKNOWN outcome is dead-lettered, never retried blindly', consumer.effects == [],
                str(consumer.effects))
        rec = _poll(lambda: consumer.reconcile({}), timeout, until=lambda rep: bool(consumer.settlements))
        r.check(f'reconcile confirms first: {truth.value} is {label}', len(consumer.effects) == expect_effects,
                f'{consumer.effects} {rec} {consumer.settlements}')
        r.check(f'the dead letter is settled ({truth.value})', broker.settled == 1,
                f'{consumer.settlements}')

    # 8. the outbox: published once, with the attributes a consumer deduplicates on
    broker, dest, source = make()
    outbox = InMemoryOutboxStore()
    outbox.add({'destination': dest, 'body': {'order': 'E'}, 'tenant_id': 't1', 'idempotency_key': _key('outbox')})
    outbox.add({'destination': dest, 'body': {'order': 'F'}, 'tenant_id': None, 'idempotency_key': None})
    sweep = BrokerOutboxSweeper(outbox, broker).sweep({})
    r.check('the outbox publishes a record a consumer can deduplicate, and refuses one it could not',
            sweep['published'] == 1 and len(sweep['failed_keys']) == 1, str(sweep))
    r.check('a second sweep publishes nothing new', BrokerOutboxSweeper(outbox, broker).sweep({})['published'] == 0)
    consumer = Effects(broker, store_factory(), source, commit)
    drain(consumer)
    r.check('the published record is consumed once', consumer.effects == [{'order': 'E'}], str(consumer.effects))
    return r.passed


def check_all(idempotency=None, transactional=None, lease=None, checkpoint=None, llm_service=None,
              message_broker=None) -> dict:
    """Run the suites for the factories given; returns {port: [passed scenario names]}."""
    out = {}
    for name, fn, make in (('idempotency', check_idempotency_store, idempotency),
                           ('transactional', check_transactional_store, transactional),
                           ('lease', check_lease, lease), ('checkpoint', check_checkpoint_store, checkpoint),
                           ('llm_service', check_llm_service, llm_service),
                           ('message_broker', check_message_broker, message_broker)):
        if make is not None:
            out[name] = fn(make)
    return out
