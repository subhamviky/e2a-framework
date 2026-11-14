"""The outbox stores (v1.7.1) pass E2A's outbox contract (e2a.port_contract.check_outbox_store), and a
record commits with the business change or not at all, in each database's own transaction:
DynamoDB on moto (and DynamoDB Local for the race), Firestore and Cosmos DB on their emulators."""
import os
import sys
import uuid

import boto3
import pytest
from moto import mock_aws

from e2a import BrokerOutboxSweeper, InMemoryBroker
from e2a.port_contract import check_outbox_store

SUITE = 11
KEY = 'C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw=='  # emulator's public key


def _table(client, name):
    client.create_table(TableName=name, BillingMode='PAY_PER_REQUEST',
                        KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
                        AttributeDefinitions=[{'AttributeName': 'pk', 'AttributeType': 'S'},
                                              {'AttributeName': 'sk', 'AttributeType': 'S'}])


def test_dynamodb_outbox_meets_the_contract_and_commits_with_the_business_change():
    from e2a_adapters.aws import DynamoDBOutboxStore
    with mock_aws():
        client = boto3.client('dynamodb', region_name='us-east-1')
        _table(client, 'state')
        # moto is not atomic under threads: the race runs on DynamoDB Local below
        assert len(check_outbox_store(lambda: DynamoDBOutboxStore(client, 'state', uuid.uuid4().hex[:8]), race=1)) == SUITE - 1
        outbox = DynamoDBOutboxStore(client, 'state', 'orders')
        order = {'Put': {'TableName': 'state', 'Item': {'pk': {'S': 'ORDER#1'}, 'sk': {'S': 'order'}},
                         'ConditionExpression': 'attribute_not_exists(pk)'}}
        record = {'destination': 'orders', 'body': {'o': 1}, 'tenant_id': 't', 'idempotency_key': 'k1'}
        client.transact_write_items(TransactItems=[order, outbox.transact_item(dict(record))])
        with pytest.raises(client.exceptions.TransactionCanceledException):          # the order exists: rolled back
            client.transact_write_items(TransactItems=[order, outbox.transact_item({**record, 'idempotency_key': 'k2'})])
        assert [r['idempotency_key'] for r in outbox.pending()] == ['k1']
        broker = InMemoryBroker()
        assert BrokerOutboxSweeper(outbox, broker).sweep({})['published'] == 1 and outbox.pending() == []


@pytest.mark.skipif(not os.getenv('DYNAMODB_LOCAL_ENDPOINT'), reason='DYNAMODB_LOCAL_ENDPOINT not set')
def test_dynamodb_outbox_race_on_dynamodb_local(monkeypatch):
    from e2a_adapters.aws import DynamoDBOutboxStore
    for k, v in (('AWS_ACCESS_KEY_ID', 'local'), ('AWS_SECRET_ACCESS_KEY', 'local')):
        monkeypatch.setenv(k, v)
    client = boto3.client('dynamodb', region_name='us-east-1', endpoint_url=os.environ['DYNAMODB_LOCAL_ENDPOINT'])
    name = f'outbox-{uuid.uuid4().hex[:8]}'
    _table(client, name)
    try:
        assert len(check_outbox_store(lambda: DynamoDBOutboxStore(client, name, uuid.uuid4().hex[:8]))) == SUITE
    finally:
        client.delete_table(TableName=name)


@pytest.mark.skipif(not os.getenv('FIRESTORE_EMULATOR_HOST'), reason='FIRESTORE_EMULATOR_HOST not set')
def test_firestore_outbox_meets_the_contract_and_commits_with_the_business_change():
    from google.cloud import firestore

    from e2a_adapters.gcp import FirestoreOutboxStore
    client = firestore.Client(project='demo-e2a-adapters')
    assert len(check_outbox_store(lambda: FirestoreOutboxStore(client, 'e2a', uuid.uuid4().hex[:8]))) == SUITE
    outbox = FirestoreOutboxStore(client, 'e2a', f'tx-{uuid.uuid4().hex[:6]}')

    @firestore.transactional
    def place(tx, fail):
        tx.set(client.collection('orders').document(uuid.uuid4().hex), {'state': 'PLACED'})
        outbox.add({'destination': 'orders', 'body': {'o': 1}, 'tenant_id': 't', 'idempotency_key': str(fail)},
                   transaction=tx)
        if fail:
            raise RuntimeError('a business rule failed')
    with pytest.raises(RuntimeError):
        place(client.transaction(), True)
    place(client.transaction(), False)
    assert [r['idempotency_key'] for r in outbox.pending()] == ['False']


@pytest.mark.skipif(not os.getenv('COSMOS_EMULATOR_ENDPOINT'), reason='COSMOS_EMULATOR_ENDPOINT not set')
def test_cosmos_outbox_meets_the_contract_and_commits_with_the_business_change():
    from azure.cosmos import CosmosClient, PartitionKey
    from azure.cosmos.exceptions import CosmosBatchOperationError

    from e2a_adapters.azure import CosmosOutboxStore
    db = CosmosClient(os.environ['COSMOS_EMULATOR_ENDPOINT'], KEY).create_database_if_not_exists('e2a-adapters-test')
    c = db.create_container(f'outbox-{uuid.uuid4().hex[:8]}', PartitionKey('/pk'), default_ttl=-1)
    try:
        assert len(check_outbox_store(lambda: CosmosOutboxStore(c, uuid.uuid4().hex[:8]))) == SUITE
        outbox = CosmosOutboxStore(c, 'orders')
        record = {'destination': 'orders', 'body': {'o': 1}, 'tenant_id': 'acme', 'idempotency_key': 'k1'}
        order = ('create', ({'id': 'order-1', 'pk': 'tenant-acme'},))
        c.execute_item_batch([order, outbox.batch_operation(dict(record), 'tenant-acme')], partition_key='tenant-acme')
        with pytest.raises(CosmosBatchOperationError):                                # the order exists: rolled back
            c.execute_item_batch([order, outbox.batch_operation({**record, 'idempotency_key': 'k2'}, 'tenant-acme')],
                                 partition_key='tenant-acme')
        assert [r['idempotency_key'] for r in outbox.pending()] == ['k1']
    finally:
        db.delete_container(c.id)


def test_the_pubsub_broker_binds_without_its_sdk(monkeypatch):
    # v1.7.1: a profile binds and verifies offline even where google-cloud-pubsub is not installed
    from e2a_adapters.gcp import PubSubBroker
    monkeypatch.setitem(sys.modules, 'google.cloud.pubsub_v1', None)        # importing it now fails
    broker = PubSubBroker.from_config({'gcp_project': 'p'})
    with pytest.raises(ImportError):
        broker.publisher.publish
