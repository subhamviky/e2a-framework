"""The configuration keys of the cloud adapters (v1.7.0, design note MP-05), registered in E2A's
central registry when e2a_adapters is imported. They are what the cloud profiles (aws, gcp,
azure) set and what every adapter's from_config(config) reads, so an application chooses its
cloud by naming a profile and overrides any value with an environment variable.

Keys the provider layer registered before (aws_region, gcp_project, the table and database
names) are registered here with the same meaning, so both packages import together."""
import json
from typing import Any, Dict, List

from e2a import ConfigKey, register_config_key

_GROUP = 'Cloud infrastructure (e2a-adapters)'


def _list(v: Any) -> List[Any]:
    if isinstance(v, (list, tuple)):
        return list(v)
    text = str(v).strip()
    if not text:
        return []
    return json.loads(text) if text.startswith('[') else [x.strip() for x in text.split(',') if x.strip()]


def _map(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    text = str(v).strip()
    return json.loads(text) if text else {}


KEYS = [
    # AWS
    ConfigKey('aws_region', 'AWS_REGION', 'us-east-1', str, _GROUP, 'every AWS client'),
    ConfigKey('aws_endpoint_url', 'AWS_ENDPOINT_URL', None, str, _GROUP, 'every AWS client — a local endpoint (moto server, LocalStack); unset in the cloud'),
    ConfigKey('dynamodb_idempotency_table', 'DYNAMODB_IDEMPOTENCY_TABLE', 'e2a-idempotency', str, _GROUP, 'DynamoDBIdempotencyStore.from_config'),
    ConfigKey('dynamodb_checkpoint_table', 'DYNAMODB_CHECKPOINT_TABLE', 'e2a-checkpoints', str, _GROUP, 'DynamoDBCheckpointStore.from_config'),
    ConfigKey('dynamodb_state_table', 'DYNAMODB_STATE_TABLE', 'e2a-state', str, _GROUP, 'DynamoDBTransactionalStore / DynamoDBLease.from_config'),
    ConfigKey('sqs_dead_letter_suffix', 'SQS_DEAD_LETTER_SUFFIX', '-dlq', str, _GROUP, 'SQSBroker — the dead-letter queue of <queue> is <queue><suffix>'),
    # GCP
    ConfigKey('gcp_project', 'GOOGLE_CLOUD_PROJECT', None, str, _GROUP, 'every Google client'),
    ConfigKey('firestore_database', 'FIRESTORE_DATABASE', '(default)', str, _GROUP, 'Firestore*.from_config'),
    ConfigKey('firestore_collection', 'FIRESTORE_COLLECTION', 'e2a', str, _GROUP, 'Firestore*.from_config — the collection of E2A records'),
    ConfigKey('pubsub_dead_letter_suffix', 'PUBSUB_DEAD_LETTER_SUFFIX', '-dlq', str, _GROUP, 'PubSubBroker — the dead-letter topic and subscription of <subscription> are <subscription><suffix>'),
    # Azure
    ConfigKey('cosmos_endpoint', 'COSMOS_ENDPOINT', None, str, _GROUP, 'Cosmos*.from_config'),
    ConfigKey('cosmos_database', 'COSMOS_DATABASE', 'e2a', str, _GROUP, 'Cosmos*.from_config'),
    ConfigKey('cosmos_container', 'COSMOS_CONTAINER', 'e2a', str, _GROUP, 'Cosmos*.from_config — the container of E2A records'),
    ConfigKey('cosmos_key', 'COSMOS_KEY', None, str, _GROUP, 'Cosmos*.from_config — an account key (the emulator); unset in the cloud: Entra ID', secret=True),
    ConfigKey('servicebus_namespace', 'SERVICEBUS_NAMESPACE', None, str, _GROUP, 'ServiceBusBroker — <namespace>.servicebus.windows.net, with Entra ID'),
    ConfigKey('servicebus_connection_string', 'SERVICEBUS_CONNECTION_STRING', None, str, _GROUP, 'ServiceBusBroker — a connection string (the emulator); unset in the cloud', secret=True),
    ConfigKey('keyvault_url', 'KEYVAULT_URL', None, str, _GROUP, 'KeyVaultCredentialProvider — https://<vault>.vault.azure.net'),
    # the outbox (v1.7.1), every cloud
    ConfigKey('outbox_name', 'E2A_OUTBOX_NAME', 'default', str, _GROUP, 'the outbox stores — one service\'s outbox; several names spread the load'),
    # secrets as tool credentials (N2), every cloud
    ConfigKey('secret_headers', 'E2A_SECRET_HEADERS', [], _list, _GROUP, '[{target, secret, header, prefix, json_key}] — the header a tool call to <target> carries, read from a secret store'),
    ConfigKey('secret_cache_seconds', 'E2A_SECRET_CACHE_SECONDS', 300, int, _GROUP, 'secret-backed credential providers — how long a secret value is reused'),
]

for _spec in KEYS:
    register_config_key(_spec)
