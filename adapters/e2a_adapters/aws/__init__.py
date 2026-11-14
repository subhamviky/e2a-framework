"""AWS adapters for E2A's ports (design notes D-02, MP-05). Needs boto3: install e2a-adapters[aws].

    DynamoDB*            claims, rows, leases, checkpoints
    SQSBroker            messaging (SQS queues, SNS topics), with dead letters
    SecretsManagerCredentialProvider   tool credentials from Secrets Manager
"""
from .dynamodb import (MAX_VALUE_BYTES, DynamoDBCheckpointStore, DynamoDBIdempotencyStore, DynamoDBLease,
                       DynamoDBTransactionalStore, is_condition_failure)
from .secrets import SecretsManagerCredentialProvider
from .sqs import SQSBroker

__all__ = ['DynamoDBIdempotencyStore', 'DynamoDBTransactionalStore', 'DynamoDBLease', 'DynamoDBCheckpointStore',
           'MAX_VALUE_BYTES', 'is_condition_failure', 'SQSBroker', 'SecretsManagerCredentialProvider']
