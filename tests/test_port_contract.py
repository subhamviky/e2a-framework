"""v1.4 (D-02): the in-memory reference adapters pass the port contract kit that every
production adapter (e2a-adapters) must pass too."""
from e2a.port_contract import (FakeClock, check_checkpoint_store, check_idempotency_store, check_lease,
                               check_transactional_store)
from e2a_inmemory_adapters import (InMemoryCheckpointStore, InMemoryIdempotencyStore, InMemoryLease,
                                   InMemoryTransactionalStore)


def test_in_memory_idempotency_store_meets_the_contract():
    assert len(check_idempotency_store(lambda clock: InMemoryIdempotencyStore(clock=clock))) == 24


def test_in_memory_transactional_store_meets_the_contract():
    assert len(check_transactional_store(lambda clock: InMemoryTransactionalStore())) == 13


def test_in_memory_lease_meets_the_contract():
    assert len(check_lease(lambda clock: InMemoryLease(clock=clock))) == 11


def test_in_memory_checkpoint_store_meets_the_contract():
    assert len(check_checkpoint_store(lambda clock: InMemoryCheckpointStore())) == 8


def test_a_broken_adapter_fails_naming_the_scenario():
    class NoAtomicity(InMemoryIdempotencyStore):
        def claim(self, key, config=None, **kw):
            from e2a import ClaimResult, ClaimStatus
            return ClaimResult(ClaimStatus.CLAIMED, owner='x')
    try:
        check_idempotency_store(lambda clock: NoAtomicity(clock=clock))
    except AssertionError as e:
        assert 'a second claim while the first owns it is IN_PROGRESS' in str(e)
    else:
        raise AssertionError('a broken adapter passed')


def test_fake_clock():
    c = FakeClock(10)
    c.advance(5)
    assert c() == 15
