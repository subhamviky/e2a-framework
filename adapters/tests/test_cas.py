"""The CAS engine (design note D-03) passes the port contract kit on its in-memory backend:
the algorithms Firestore and Cosmos DB share are proven independently of either database."""
import pytest

from e2a.port_contract import check_checkpoint_store, check_idempotency_store, check_lease, check_transactional_store
from e2a_adapters.cas import (CasCheckpointStore, CasIdempotencyStore, CasLease, CasTransactionalStore,
                              InMemoryCasBackend)


def test_idempotency():
    assert len(check_idempotency_store(lambda c: CasIdempotencyStore(InMemoryCasBackend(), c))) == 24


def test_transactional():
    assert len(check_transactional_store(lambda c: CasTransactionalStore(InMemoryCasBackend(), c))) == 13


def test_lease():
    assert len(check_lease(lambda c: CasLease(InMemoryCasBackend(), c))) == 11


def test_checkpoint():
    assert len(check_checkpoint_store(lambda c: CasCheckpointStore(InMemoryCasBackend(), c))) == 8


def test_a_value_over_the_backend_limit_is_refused():
    backend = InMemoryCasBackend()
    backend.max_value_bytes = 100
    with pytest.raises(ValueError, match='exceeds 100'):
        CasTransactionalStore(backend).put_if_version('k', {'x': 'y' * 200}, None)


def test_a_backend_that_always_loses_gives_up_loudly():
    class AlwaysLoses(InMemoryCasBackend):
        def create(self, key, doc, ttl_at=None):
            return False
    with pytest.raises(RuntimeError, match='kept losing'):
        CasIdempotencyStore(AlwaysLoses()).claim('k')
