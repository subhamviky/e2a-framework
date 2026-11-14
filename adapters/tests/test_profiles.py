"""The cloud profiles (v1.7.0): each binds and verifies without credentials or network, its
identity claim map is the preset of its cloud's identity provider, and its stores, built
from configuration, keep the port contracts (DynamoDB on moto; Firestore and Cosmos DB on
their emulators when CI runs them)."""
import json
import os
import uuid
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

import e2a_adapters
from e2a import bindings_from_profile
from e2a.bindings import available_profiles
from e2a.port_contract import check_idempotency_store, check_lease
from e2a_adapters.identity import PRESETS, preset

CLOUDS = {'aws': 'cognito', 'gcp': 'identity_platform', 'azure': 'entra_id'}


@pytest.mark.parametrize('cloud', sorted(CLOUDS))
def test_each_cloud_profile_binds_and_verifies_offline(cloud):
    assert cloud in available_profiles()
    b = bindings_from_profile(cloud, verify=True)
    config = b.config()
    for seam in ('idempotency_store', 'checkpoint_store', 'transactional_store', 'lease', 'message_broker',
                 'credential_provider', 'identity_resolver'):
        assert config.get(seam) is not None, seam
    assert config['profile_settings']['identity_claim_map'] == PRESETS[CLOUDS[cloud]]
    assert b.warnings == [] and b.profile.literal_secrets == []


def test_the_profiles_are_shipped_in_the_package():
    names = sorted(p.stem for p in (Path(e2a_adapters.__file__).parent / 'profiles').glob('*.json'))
    assert names == ['aws', 'azure', 'gcp']


def test_an_environment_variable_overrides_a_profile_value(monkeypatch):
    monkeypatch.setenv('DYNAMODB_IDEMPOTENCY_TABLE', 'orders-claims')
    assert bindings_from_profile('aws').idempotency_store.table_name == 'orders-claims'


def test_presets_rename_a_claim():
    assert preset('cognito', tenant_id='custom:org')['tenant_id'] == 'custom:org'
    with pytest.raises(ValueError):
        preset('okta')


def test_the_aws_profile_resolves_a_cognito_identity():
    resolver = bindings_from_profile('aws').identity_resolver
    ident = resolver.resolve({'custom:tenant_id': 'acme', 'sub': 'u1', 'cognito:groups': ['ops']}, verified=True)
    assert (ident.tenant_id, ident.roles) == ('acme', ['ops'])


def _table(ddb, name):
    ddb.create_table(TableName=name, BillingMode='PAY_PER_REQUEST',
                     KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
                     AttributeDefinitions=[{'AttributeName': 'pk', 'AttributeType': 'S'},
                                           {'AttributeName': 'sk', 'AttributeType': 'S'}])


def test_dynamodb_stores_from_config_keep_the_contracts():
    from e2a_adapters.aws import DynamoDBIdempotencyStore, DynamoDBLease
    with mock_aws():
        ddb = boto3.client('dynamodb', region_name='us-east-1')
        _table(ddb, 'claims')
        _table(ddb, 'state')
        cfg = {'aws_region': 'us-east-1', 'dynamodb_idempotency_table': 'claims', 'dynamodb_state_table': 'state'}
        # moto is not atomic under threads: the races run on DynamoDB Local (test_dynamodb.py)
        assert check_idempotency_store(lambda c: DynamoDBIdempotencyStore.from_config(cfg, clock=c), race=1)
        assert check_lease(lambda c: DynamoDBLease.from_config(cfg, clock=c), race=1)


@pytest.mark.skipif(not os.getenv('FIRESTORE_EMULATOR_HOST'), reason='FIRESTORE_EMULATOR_HOST not set')
def test_firestore_stores_from_config_keep_the_contracts():
    from e2a_adapters.gcp import FirestoreIdempotencyStore
    cfg = {'gcp_project': 'demo-e2a-adapters', 'firestore_collection': f'cfg-{uuid.uuid4().hex[:8]}'}
    assert check_idempotency_store(lambda c: FirestoreIdempotencyStore.from_config(cfg, clock=c))


@pytest.mark.skipif(not os.getenv('COSMOS_EMULATOR_ENDPOINT'), reason='COSMOS_EMULATOR_ENDPOINT not set')
def test_cosmos_stores_from_config_keep_the_contracts():
    from azure.cosmos import CosmosClient, PartitionKey

    from e2a_adapters.azure import CosmosIdempotencyStore
    key = 'C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw=='  # the emulator's public key
    db = CosmosClient(os.environ['COSMOS_EMULATOR_ENDPOINT'], key).create_database_if_not_exists('e2a-adapters-test')
    name = f'cfg-{uuid.uuid4().hex[:8]}'
    db.create_container(name, PartitionKey('/pk'), default_ttl=-1)
    try:
        cfg = {'cosmos_endpoint': os.environ['COSMOS_EMULATOR_ENDPOINT'], 'cosmos_key': key,
               'cosmos_database': 'e2a-adapters-test', 'cosmos_container': name}
        assert check_idempotency_store(lambda c: CosmosIdempotencyStore.from_config(cfg, clock=c))
    finally:
        db.delete_container(name)
