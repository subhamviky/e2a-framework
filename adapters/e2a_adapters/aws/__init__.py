"""AWS adapters for E2A's ports (design notes D-02, MP-05). Needs boto3: install e2a-adapters[aws].

    DynamoDB*            claims, rows, leases, checkpoints
    SQSBroker            messaging (SQS queues, SNS topics), with dead letters
    SQSLambdaBatch       one Lambda SQS batch as a broker (v1.7.2)
    DynamoDBOutboxStore  the transactional outbox (v1.7.1)
    SecretsManagerCredentialProvider   tool credentials from Secrets Manager
"""
from .dynamodb import (MAX_VALUE_BYTES, DynamoDBCheckpointStore, DynamoDBIdempotencyStore, DynamoDBLease,
                       DynamoDBTransactionalStore, is_condition_failure)
from .outbox import DynamoDBOutboxStore
from .secrets import SecretsManagerCredentialProvider
from .sqs import SQSBroker, SQSLambdaBatch

__all__ = ['DynamoDBIdempotencyStore', 'DynamoDBTransactionalStore', 'DynamoDBLease', 'DynamoDBCheckpointStore',
           'MAX_VALUE_BYTES', 'is_condition_failure', 'SQSBroker', 'SQSLambdaBatch', 'SecretsManagerCredentialProvider', 'DynamoDBOutboxStore']
