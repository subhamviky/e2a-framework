"""The Cosmos DB adapters pass the port contract kit on the Cosmos DB emulator.
Run the emulator and set COSMOS_EMULATOR_ENDPOINT (CI does both); otherwise these are skipped."""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.getenv('COSMOS_EMULATOR_ENDPOINT'),
                                reason='COSMOS_EMULATOR_ENDPOINT not set (Cosmos DB emulator not running)')

from e2a.port_contract import check_checkpoint_store, check_idempotency_store, check_lease, check_transactional_store

# The emulator's published, well-known key (not a secret): https://learn.microsoft.com/azure/cosmos-db/emulator
EMULATOR_KEY = 'C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw=='


@pytest.fixture(scope='module')
def database():
    from azure.cosmos import CosmosClient
    client = CosmosClient(os.environ['COSMOS_EMULATOR_ENDPOINT'], EMULATOR_KEY)
    return client.create_database_if_not_exists('e2a-adapters-test')


@pytest.fixture
def container(database):
    from azure.cosmos import PartitionKey
    c = database.create_container(f'ports-{uuid.uuid4().hex[:8]}', PartitionKey('/pk'), default_ttl=-1)
    yield c
    database.delete_container(c.id)


def test_idempotency(container):
    from e2a_adapters.azure import CosmosIdempotencyStore
    assert len(check_idempotency_store(lambda c: CosmosIdempotencyStore(container, clock=c))) == 24


def test_transactional(container):
    from e2a_adapters.azure import CosmosTransactionalStore
    assert len(check_transactional_store(lambda c: CosmosTransactionalStore(container, clock=c))) == 13


def test_lease(container):
    from e2a_adapters.azure import CosmosLease
    assert len(check_lease(lambda c: CosmosLease(container, clock=c))) == 11


def test_checkpoint(container):
    from e2a_adapters.azure import CosmosCheckpointStore
    assert len(check_checkpoint_store(lambda c: CosmosCheckpointStore(container, clock=c))) == 8


def test_layout_and_ttl(container):
    from e2a_adapters.azure import CosmosIdempotencyStore
    store = CosmosIdempotencyStore(container)
    c = store.claim('evt/1?x#y', tenant_id='acme', ttl_seconds=3600)
    store.complete('evt/1?x#y', {'ok': True}, tenant_id='acme', owner=c.owner)
    [item] = list(container.read_all_items())
    assert item['id'] == item['pk'] == 'IDEM%23acme%23evt%2F1%3Fx%23y' and item['e2a_key'] == 'IDEM#acme#evt/1?x#y'
    assert item['state'] == 'SUCCEEDED' and 3000 < item['ttl'] <= 3601
    assert store.claim('evt/1?x#y', tenant_id='acme').result == {'ok': True}
