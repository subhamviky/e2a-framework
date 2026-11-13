"""AWS adapters for E2A's foundation ports (design note D-02). Needs boto3: install e2a-adapters[aws]."""
from .dynamodb import (MAX_VALUE_BYTES, DynamoDBCheckpointStore, DynamoDBIdempotencyStore, DynamoDBLease,
                       DynamoDBTransactionalStore, is_condition_failure)

__all__ = ['DynamoDBIdempotencyStore', 'DynamoDBTransactionalStore', 'DynamoDBLease', 'DynamoDBCheckpointStore',
           'MAX_VALUE_BYTES', 'is_condition_failure']
