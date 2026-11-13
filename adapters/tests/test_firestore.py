"""The Firestore adapters pass the port contract kit on the Firestore emulator.
Run the emulator and set FIRESTORE_EMULATOR_HOST (CI does both); otherwise these are skipped."""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.getenv('FIRESTORE_EMULATOR_HOST'),
                                reason='FIRESTORE_EMULATOR_HOST not set (Firestore emulator not running)')

from e2a.port_contract import check_checkpoint_store, check_idempotency_store, check_lease, check_transactional_store


@pytest.fixture
def client():
    from google.cloud import firestore
    return firestore.Client(project='demo-e2a-adapters')


@pytest.fixture
def collection():
    return f'e2a-test-{uuid.uuid4().hex[:8]}'


def test_idempotency(client, collection):
    from e2a_adapters.gcp import FirestoreIdempotencyStore
    assert len(check_idempotency_store(lambda c: FirestoreIdempotencyStore(client, collection, clock=c))) == 24


def test_transactional(client, collection):
    from e2a_adapters.gcp import FirestoreTransactionalStore
    assert len(check_transactional_store(lambda c: FirestoreTransactionalStore(client, collection, clock=c))) == 13


def test_lease(client, collection):
    from e2a_adapters.gcp import FirestoreLease
    assert len(check_lease(lambda c: FirestoreLease(client, collection, clock=c))) == 11


def test_checkpoint(client, collection):
    from e2a_adapters.gcp import FirestoreCheckpointStore
    assert len(check_checkpoint_store(lambda c: FirestoreCheckpointStore(client, collection, clock=c))) == 8


def test_layout_and_ttl_field(client, collection):
    from e2a_adapters.gcp import FirestoreIdempotencyStore
    store = FirestoreIdempotencyStore(client, collection)
    c = store.claim('orders/42', tenant_id='acme', ttl_seconds=3600)
    store.complete('orders/42', {'ok': True}, tenant_id='acme', owner=c.owner)
    [doc] = list(client.collection(collection).stream())
    assert doc.id == 'e2a-IDEM#acme#orders%2F42'
    data = doc.to_dict()
    assert data['state'] == 'SUCCEEDED' and data['ttl'].timestamp() > 0, 'ttl is a timestamp for the TTL policy'
    again = store.claim('orders/42', tenant_id='acme')
    assert again.result == {'ok': True}
