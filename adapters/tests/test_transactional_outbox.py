"""v1.7.3: put_if_version(..., outbox=[records]) commits the row and its records in one atomic
write on each backend (E2A's check_transactional_outbox): the CAS engine in memory, DynamoDB on
moto (and DynamoDB Local for the race), Firestore and Cosmos DB on their emulators."""
import os
import uuid

import boto3
import pytest
from moto import mock_aws

from e2a import InMemoryOutboxStore
from e2a.port_contract import check_transactional_outbox
from e2a_adapters.cas import CasTransactionalStore, InMemoryCasBackend

SUITE = 9
KEY = 'C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw=='  # emulator's public key


def _table(client, name):
    client.create_table(TableName=name, BillingMode='PAY_PER_REQUEST',
                        KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
                        AttributeDefinitions=[{'AttributeName': 'pk', 'AttributeType': 'S'},
                                              {'AttributeName': 'sk', 'AttributeType': 'S'}])


def test_the_cas_engine_commits_records_with_the_row():
    def make():
        outbox = InMemoryOutboxStore()
        return CasTransactionalStore(InMemoryCasBackend(), outbox=outbox), outbox
    assert len(check_transactional_outbox(make)) == SUITE
    with pytest.raises(ValueError, match='no outbox'):
        CasTransactionalStore(InMemoryCasBackend()).put_if_version('k', {}, None, outbox=[{'destination': 'd'}])


def test_dynamodb_commits_records_with_the_row():
    from e2a_adapters.aws import DynamoDBOutboxStore, DynamoDBTransactionalStore
    with mock_aws():
        client = boto3.client('dynamodb', region_name='us-east-1')
        _table(client, 'state')

        def make():
            outbox = DynamoDBOutboxStore(client, 'state', uuid.uuid4().hex[:8])
            return DynamoDBTransactionalStore(client=client, table_name='state', outbox=outbox), outbox
        # moto is not atomic under threads: the race runs on DynamoDB Local below
        assert len(check_transactional_outbox(make, race=1)) == SUITE - 1


def test_from_config_builds_the_matching_outbox():
    from e2a_adapters.aws import DynamoDBTransactionalStore
    store = DynamoDBTransactionalStore.from_config({'aws_region': 'us-east-1', 'dynamodb_state_table': 'state',
                                                    'outbox_name': 'orders'})
    assert store.outbox.table_name == 'state' and store.outbox.name == 'orders'
    assert 'transactional_outbox' in store.capabilities


@pytest.mark.skipif(not os.getenv('DYNAMODB_LOCAL_ENDPOINT'), reason='DYNAMODB_LOCAL_ENDPOINT not set')
def test_dynamodb_race_on_dynamodb_local(monkeypatch):
    from e2a_adapters.aws import DynamoDBOutboxStore, DynamoDBTransactionalStore
    for k, v in (('AWS_ACCESS_KEY_ID', 'local'), ('AWS_SECRET_ACCESS_KEY', 'local')):
        monkeypatch.setenv(k, v)
    client = boto3.client('dynamodb', region_name='us-east-1', endpoint_url=os.environ['DYNAMODB_LOCAL_ENDPOINT'])
    name = f'txo-{uuid.uuid4().hex[:8]}'
    _table(client, name)
    try:
        def make():
            outbox = DynamoDBOutboxStore(client, name, uuid.uuid4().hex[:8])
            return DynamoDBTransactionalStore(client=client, table_name=name, outbox=outbox), outbox
        assert len(check_transactional_outbox(make)) == SUITE
    finally:
        client.delete_table(TableName=name)


@pytest.mark.skipif(not os.getenv('FIRESTORE_EMULATOR_HOST'), reason='FIRESTORE_EMULATOR_HOST not set')
def test_firestore_commits_records_with_the_row():
    from google.cloud import firestore

    from e2a_adapters.gcp import FirestoreOutboxStore, FirestoreTransactionalStore
    client = firestore.Client(project='demo-e2a-adapters')

    def make():
        collection = f'txo-{uuid.uuid4().hex[:8]}'
        outbox = FirestoreOutboxStore(client, collection, 'orders')
        return FirestoreTransactionalStore(client, collection, outbox=outbox), outbox
    assert len(check_transactional_outbox(make)) == SUITE


@pytest.mark.skipif(not os.getenv('COSMOS_EMULATOR_ENDPOINT'), reason='COSMOS_EMULATOR_ENDPOINT not set')
def test_cosmos_commits_records_with_the_row(drop_cosmos_container):
    from azure.cosmos import CosmosClient, PartitionKey

    from e2a_adapters.azure import CosmosOutboxStore, CosmosTransactionalStore
    db = CosmosClient(os.environ['COSMOS_EMULATOR_ENDPOINT'], KEY).create_database_if_not_exists('e2a-adapters-test')
    c = db.create_container(f'txo-{uuid.uuid4().hex[:8]}', PartitionKey('/pk'), default_ttl=-1)
    try:
        def make():
            outbox = CosmosOutboxStore(c, uuid.uuid4().hex[:8])
            return CosmosTransactionalStore(c, outbox=outbox), outbox
        assert len(check_transactional_outbox(make)) == SUITE
    finally:
        drop_cosmos_container(db, c.id)
