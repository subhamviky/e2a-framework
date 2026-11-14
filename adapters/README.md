# e2a-adapters

E2A's ports on the clouds' infrastructure (design notes D-02, D-03, MP-05). Each adapter passes
E2A's port contract kit (`e2a.port_contract`), the same suite as the in-memory reference adapters;
each broker passes its messaging suite. No AI vendor SDK is imported here: AI services live in
the provider layer (`e2a-managed-providers`).

## Cloud profiles (since 1.7.0)

An application chooses its cloud by naming a profile; every adapter is built from configuration
(`from_config`), so nothing is constructed by hand:

```python
from e2a import bindings_from_profile
bindings = bindings_from_profile("aws", verify=True)      # or "gcp", "azure"; or E2A_PROVIDER_PROFILE
config = bindings.config()     # idempotency_store, checkpoint_store, transactional_store, lease,
                               # message_broker, outbox_store (1.7.1), credential_provider, identity_resolver
```

| Profile | Stores | Messaging | Tool credentials | Identity claims |
|---|---|---|---|---|
| `aws` | DynamoDB | SQS / SNS (`SQSBroker`) | Secrets Manager | Cognito |
| `gcp` | Firestore | Pub/Sub (`PubSubBroker`) | Secret Manager | Identity Platform |
| `azure` | Cosmos DB | Service Bus (`ServiceBusBroker`) | Key Vault | Entra ID |

Every setting is a registered key with an environment variable (`AWS_REGION`, `DYNAMODB_IDEMPOTENCY_TABLE`,
`GOOGLE_CLOUD_PROJECT`, `COSMOS_ENDPOINT`, `SERVICEBUS_NAMESPACE`, `KEYVAULT_URL`, ...; `python -c
"from e2a import config_reference_markdown; import e2a_adapters; print(config_reference_markdown())"`).
Credentials come from the platform identity; the connection string and account key keys exist for
the emulators. A provider profile in `e2a-managed-providers` extends the profile of its cloud.

Clients are created on first use, so a profile binds and verifies without credentials or network.

## Adapters

| Backend | Install | Adapters |
|---|---|---|
| AWS DynamoDB | `e2a-adapters[aws]` | `DynamoDBIdempotencyStore`, `DynamoDBTransactionalStore`, `DynamoDBLease`, `DynamoDBCheckpointStore` |
| GCP Firestore (since 1.5.0) | `e2a-adapters[gcp]` | `FirestoreIdempotencyStore`, `FirestoreTransactionalStore`, `FirestoreLease`, `FirestoreCheckpointStore` |
| Azure Cosmos DB, NoSQL API (since 1.5.0) | `e2a-adapters[azure]` | `CosmosIdempotencyStore`, `CosmosTransactionalStore`, `CosmosLease`, `CosmosCheckpointStore` |
| Amazon SQS / SNS (since 1.7.0) | `e2a-adapters[aws]` | `SQSBroker`: queues, SNS topics, a `<queue>-dlq` dead-letter queue |
| Google Pub/Sub (since 1.7.0) | `e2a-adapters[gcp]` | `PubSubBroker`: topics and subscriptions, a `<subscription>-dlq` topic and subscription |
| Azure Service Bus (since 1.7.0) | `e2a-adapters[azure]` | `ServiceBusBroker`: queues, topic subscriptions, the native dead-letter subqueue |
| Secret stores (since 1.7.0) | per cloud | `SecretsManagerCredentialProvider`, `SecretManagerCredentialProvider`, `KeyVaultCredentialProvider`: tool auth headers from `secret_headers` |
| Lambda SQS batches (since 1.7.2) | `e2a-adapters[aws]` | `SQSLambdaBatch`: one Lambda-pushed SQS batch as a broker; `batch_item_failures()` is the Lambda response |
| Outbox stores (since 1.7.1) | per cloud | `DynamoDBOutboxStore` (`transact_item` for your TransactWriteItems), `FirestoreOutboxStore` (`add(..., transaction=)`), `CosmosOutboxStore` (`batch_operation` for your transactional batch, in the business document's partition) |
| Identity presets (since 1.7.0) | – | `e2a_adapters.identity.PRESETS`: the claim maps of Cognito, Entra ID and Identity Platform |

Firestore and Cosmos DB share one implementation of the ports, `e2a_adapters.cas`. It works over any document store that can create-if-absent and replace-if-unchanged (an update time, an ETag), and each database supplies only a small `CasBackend`. PostgreSQL would be a third backend.

## Install

The version follows E2A. Install e2a-framework from its tag first, as every project already does:

```bash
pip install "e2a-framework @ git+https://github.com/subhamviky/e2a-framework.git@v1.7.2" \
            "e2a-adapters[aws] @ git+https://github.com/subhamviky/e2a-framework.git@v1.7.2#subdirectory=adapters"
```

Poetry:

```toml
"e2a-framework" = { git = "https://github.com/subhamviky/e2a-framework.git", tag = "v1.7.2" }
"e2a-adapters" = { git = "https://github.com/subhamviky/e2a-framework.git", tag = "v1.7.2", subdirectory = "adapters", extras = ["aws"] }
```

## DynamoDB

```python
import boto3
from e2a_adapters.aws import DynamoDBIdempotencyStore, DynamoDBLease

table = boto3.resource('dynamodb').Table('my-service')       # the application's own table
store = DynamoDBIdempotencyStore(table)
lease = DynamoDBLease(table)
config = {'idempotency_store': store}                         # what BaseWorkflow claims with
```

- **The table:** any table whose partition and sort keys are strings, by default `pk` / `sk` (`partition_key=`, `sort_key=`).
  - E2A's items live under their own partitions: `E2A#IDEM#<tenant>#<key>`, `E2A#ROW#<collection>#<key>`, `E2A#LEASE#<key>` and `E2A#CKPT#<id>`. The prefix is `prefix=`.
  - There is one partition per key, so E2A never makes a hot partition.
- **Atomicity:** every state change is one write with a condition expression.
  - Only `ConditionalCheckFailedException` means "another writer got there first".
  - Any other error (throttling, access, a missing table) is raised.
- **Time:** wall-clock epoch seconds (`clock=`, injectable for tests).
  - Correctness uses explicit fields (`lease_until`, `expires_at`).
  - Enable DynamoDB TTL on the `ttl` attribute (`ttl_attribute=`) for garbage collection only.
- **Values** are stored as JSON, so they round-trip exactly. Over 350 KB they are refused with a `ValueError`.
- **IAM:** `dynamodb:GetItem`, `dynamodb:PutItem` (checkpoints), `dynamodb:UpdateItem`, `dynamodb:DeleteItem` (`DynamoDBTransactionalStore.delete` only) on the table.

Terraform for the TTL:

```hcl
resource "aws_dynamodb_table" "app" {
  # ...
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}
```

## Firestore

```python
from google.cloud import firestore
from e2a_adapters.gcp import FirestoreIdempotencyStore, FirestoreTransactionalStore

client = firestore.Client()                                  # Firestore in Native mode
store = FirestoreIdempotencyStore(client)                    # documents in the collection "e2a"
rows = FirestoreTransactionalStore(client, collection="e2a")
```

- **Writes:** every change is `create()`, or `update()` with a `last_update_time` precondition.
  - Lost races are AlreadyExists, FailedPrecondition, NotFound on a precondition write, and ABORTED (contention: nothing was written). After one, the port reads again and re-decides.
  - Every other error is raised.
- **Documents:**
  - document ids are the key with `/` escaped, prefixed `e2a-`;
  - values are JSON strings;
  - the limit is 900 KB.
- **TTL:** the `ttl` field is a timestamp. A TTL policy on it is garbage collection only (Terraform: `google_firestore_field` with `ttl_config {}` on collection `e2a`, field `ttl`).
- **IAM:** `roles/datastore.user` for the service account.

## Cosmos DB

```python
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
from e2a_adapters.azure import CosmosIdempotencyStore

container = CosmosClient(url, DefaultAzureCredential()).get_database_client("app").get_container_client("e2a")
store = CosmosIdempotencyStore(container)
```

- **The container:** its partition key path is `/pk`, and each item's partition key is its id, so one key is one logical partition. Enable TTL with `default_ttl = -1`: items without a `ttl` never expire, and claims set one.
- **Writes:** every change is `create_item()`, or `replace_item()` with the ETag that was read and `IfNotModified`.
  - Lost races are 409, 412, and NotFound on a replace.
  - Every other error is raised.
- **Item ids:** the key with `/ \ ? # %` percent-escaped (ids over 200 characters are hashed). The original key is kept in `e2a_key`.
- **Values:** JSON strings, up to 1.8 MB.
- **Consistency:** the default Session consistency gives a writer read-your-writes. Several instances need Strong consistency (single region) or Bounded Staleness, because the claim's decision reads what another instance wrote. A replace-if-unchanged still refuses a stale ETag either way.
- **Access:** the data-plane role "Cosmos DB Built-in Data Contributor".

## Tests

```bash
pip install -e ".[aws,gcp,azure,test]" && pytest -q -rs tests
```

| Backend | Where the tests run | Variable that enables them |
|---|---|---|
| CAS engine | in memory, always | — |
| DynamoDB | moto, always, without the concurrent races (moto is not atomic under threads) | — |
| DynamoDB, whole kit | DynamoDB Local: `docker run -p 8000:8000 amazon/dynamodb-local` | `DYNAMODB_LOCAL_ENDPOINT=http://127.0.0.1:8000` |
| Firestore | `docker run -p 8085:8085 gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators gcloud emulators firestore start --host-port=0.0.0.0:8085` | `FIRESTORE_EMULATOR_HOST=127.0.0.1:8085` |
| Cosmos DB | `docker run -p 8081:8081 mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator:vnext-preview` | `COSMOS_EMULATOR_ENDPOINT=http://127.0.0.1:8081` |

CI starts all three emulators.
