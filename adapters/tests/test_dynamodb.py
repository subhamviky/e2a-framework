"""The DynamoDB adapters pass E2A's port contract kit:

- on moto (no AWS account, always runs), without the concurrent races: moto's in-process
  backend does not evaluate condition expressions atomically across threads;
- on DynamoDB Local (Amazon's emulator, which does), the whole kit including the races. Run
  it and set DYNAMODB_LOCAL_ENDPOINT (CI does both); otherwise those tests are skipped.

Plus the DynamoDB-specific checks: shared table layout, errors that are not a lost condition, value size."""
import uuid
import json
import os

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from e2a import ClaimStatus
from e2a.port_contract import check_checkpoint_store, check_idempotency_store, check_lease, check_transactional_store
from e2a_adapters.aws import (MAX_VALUE_BYTES, DynamoDBCheckpointStore, DynamoDBIdempotencyStore, DynamoDBLease,
                              DynamoDBTransactionalStore)


@pytest.fixture
def table():
    os.environ.setdefault('AWS_DEFAULT_REGION', 'eu-west-1')
    for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
        os.environ[k] = 'testing'
    with mock_aws():
        ddb = boto3.resource('dynamodb')
        t = ddb.create_table(TableName='app', BillingMode='PAY_PER_REQUEST',
                             KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'},
                                        {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
                             AttributeDefinitions=[{'AttributeName': 'pk', 'AttributeType': 'S'},
                                                   {'AttributeName': 'sk', 'AttributeType': 'S'}])
        yield t


def test_idempotency_store_meets_the_contract(table):
    assert len(check_idempotency_store(lambda clock: DynamoDBIdempotencyStore(table, clock=clock), race=1)) == 23


def test_transactional_store_meets_the_contract(table):
    assert len(check_transactional_store(lambda clock: DynamoDBTransactionalStore(table, clock=clock), race=1)) == 12


def test_lease_meets_the_contract(table):
    assert len(check_lease(lambda clock: DynamoDBLease(table, clock=clock), race=1)) == 10


def test_checkpoint_store_meets_the_contract(table):
    assert len(check_checkpoint_store(lambda clock: DynamoDBCheckpointStore(table, clock=clock), race=1)) == 7


def test_it_shares_the_application_table_under_its_own_partitions(table):
    table.put_item(Item={'pk': 'ORDER#1', 'sk': 'order', 'amount': 10})
    store = DynamoDBIdempotencyStore(table)
    c = store.claim('req-1', tenant_id='acme', ttl_seconds=3600)
    store.complete('req-1', {'ok': True}, tenant_id='acme', owner=c.owner)
    items = table.scan()['Items']
    e2a = [i for i in items if i['pk'].startswith('E2A#')]
    assert [i['pk'] for i in e2a] == ['E2A#IDEM#acme#req-1'] and e2a[0]['sk'] == 'idem'
    assert e2a[0]['state'] == 'SUCCEEDED' and json.loads(e2a[0]['result']) == {'ok': True}
    assert int(e2a[0]['ttl']) > 0, 'the ttl attribute is set for DynamoDB garbage collection'
    assert table.get_item(Key={'pk': 'ORDER#1', 'sk': 'order'})['Item']['amount'] == 10


def test_custom_key_names_and_client_form():
    os.environ.setdefault('AWS_DEFAULT_REGION', 'eu-west-1')
    with mock_aws():
        client = boto3.client('dynamodb')
        client.create_table(TableName='other', BillingMode='PAY_PER_REQUEST',
                            KeySchema=[{'AttributeName': 'PK', 'KeyType': 'HASH'},
                                       {'AttributeName': 'SK', 'KeyType': 'RANGE'}],
                            AttributeDefinitions=[{'AttributeName': 'PK', 'AttributeType': 'S'},
                                                  {'AttributeName': 'SK', 'AttributeType': 'S'}])
        store = DynamoDBIdempotencyStore(client=client, table_name='other', partition_key='PK', sort_key='SK',
                                         prefix='SVC#')
        assert store.claim('k').status == ClaimStatus.CLAIMED
        assert client.scan(TableName='other')['Items'][0]['PK']['S'] == 'SVC#IDEM#-#k'


def test_an_error_that_is_not_a_lost_condition_is_raised(table):
    store = DynamoDBIdempotencyStore(client=table.meta.client, table_name='missing-table')
    with pytest.raises(ClientError) as e:
        store.claim('k')
    assert e.value.response['Error']['Code'] == 'ResourceNotFoundException'


def test_a_value_too_large_for_an_item_is_refused_up_front(table):
    rows = DynamoDBTransactionalStore(table)
    with pytest.raises(ValueError, match='exceeds'):
        rows.put_if_version('big', {'blob': 'x' * (MAX_VALUE_BYTES + 1)}, None)
    assert rows.get('big') is None


def test_a_lease_keeps_its_fencing_counter_across_release(table):
    lease = DynamoDBLease(table)
    tokens = []
    for _ in range(3):
        h = lease.acquire('job', 30)
        tokens.append(h.fencing_token)
        lease.release(h)
    assert tokens == [1, 2, 3]


# ------------------------------------------------ DynamoDB Local: the whole kit, races included
local = pytest.mark.skipif(not os.getenv('DYNAMODB_LOCAL_ENDPOINT'),
                           reason='DYNAMODB_LOCAL_ENDPOINT not set (DynamoDB Local not running)')


@pytest.fixture
def local_table():
    for k, v in (('AWS_ACCESS_KEY_ID', 'local'), ('AWS_SECRET_ACCESS_KEY', 'local'), ('AWS_DEFAULT_REGION', 'eu-west-1')):
        os.environ[k] = v
    ddb = boto3.resource('dynamodb', endpoint_url=os.environ['DYNAMODB_LOCAL_ENDPOINT'])
    t = ddb.create_table(TableName=f'e2a-{uuid.uuid4().hex[:8]}', BillingMode='PAY_PER_REQUEST',
                         KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'},
                                    {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
                         AttributeDefinitions=[{'AttributeName': 'pk', 'AttributeType': 'S'},
                                               {'AttributeName': 'sk', 'AttributeType': 'S'}])
    t.wait_until_exists()
    yield t
    t.delete()


@local
def test_on_dynamodb_local_idempotency_store_meets_the_whole_contract(local_table):
    assert len(check_idempotency_store(lambda clock: DynamoDBIdempotencyStore(local_table, clock=clock))) == 24


@local
def test_on_dynamodb_local_transactional_store_meets_the_whole_contract(local_table):
    assert len(check_transactional_store(lambda clock: DynamoDBTransactionalStore(local_table, clock=clock))) == 13


@local
def test_on_dynamodb_local_lease_meets_the_whole_contract(local_table):
    assert len(check_lease(lambda clock: DynamoDBLease(local_table, clock=clock))) == 11


@local
def test_on_dynamodb_local_checkpoint_store_meets_the_whole_contract(local_table):
    assert len(check_checkpoint_store(lambda clock: DynamoDBCheckpointStore(local_table, clock=clock))) == 8
