# e2a-adapters

Production adapters for the E2A foundation ports (design note D-02). Each one passes E2A's
port contract kit (`e2a.port_contract`), the same suite as the in-memory reference adapters.

| Backend | Install | Adapters |
|---|---|---|
| AWS DynamoDB | `e2a-adapters[aws]` | `DynamoDBIdempotencyStore`, `DynamoDBTransactionalStore`, `DynamoDBLease`, `DynamoDBCheckpointStore` |

Firestore, Cosmos DB and PostgreSQL follow in later releases.

## Install

The version follows E2A. Install e2a-framework from its tag first, as every project already does:

```bash
pip install "e2a-framework @ git+https://github.com/subhamviky/e2a-framework.git@v1.4.0" \
            "e2a-adapters[aws] @ git+https://github.com/subhamviky/e2a-framework.git@v1.4.0#subdirectory=adapters"
```

Poetry:

```toml
"e2a-framework" = { git = "https://github.com/subhamviky/e2a-framework.git", tag = "v1.4.0" }
"e2a-adapters" = { git = "https://github.com/subhamviky/e2a-framework.git", tag = "v1.4.0", subdirectory = "adapters", extras = ["aws"] }
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

## Tests

```bash
pip install -e ".[aws,test]" && pytest -q tests
```

The tests run on moto, which evaluates condition expressions, so no AWS account is needed.
