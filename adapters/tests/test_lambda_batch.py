"""SQSLambdaBatch (v1.7.2): an IdempotentConsumer settles a Lambda-pushed SQS batch, on moto."""
import json

import boto3
from moto import mock_aws

from e2a import IdempotentConsumer, PhysicalOutcome
from e2a_adapters.aws import SQSBroker, SQSLambdaBatch
from e2a_inmemory_adapters import InMemoryIdempotencyStore

ARN = 'arn:aws:sqs:us-east-1:123456789012:lines'


def record(mid, body, **attrs):
    return {'messageId': mid, 'receiptHandle': f'rh-{mid}', 'body': body, 'eventSource': 'aws:sqs',
            'eventSourceARN': ARN, 'attributes': {'ApproximateReceiveCount': '1'},
            'messageAttributes': {k: {'stringValue': v, 'dataType': 'String'} for k, v in attrs.items()}}


class Lines(IdempotentConsumer):
    def __init__(self, broker, store):
        super().__init__(broker, store)
        self.done = []

    def _tenant_of(self, message):
        return 'acme'

    def _idempotency_key(self, message):
        return message.body.get('line_id')

    def _handle(self, message, config, tenant_id, idempotency_key):
        if message.body.get('flaky'):
            raise ConnectionError('ledger down')
        if message.body.get('amount') is None:
            raise ValueError('no amount')
        self.done.append(idempotency_key)
        return {'physical_success': True, 'outcome': PhysicalOutcome.COMMITTED}


def test_a_lambda_batch_is_settled_record_by_record():
    with mock_aws():
        sqs = boto3.client('sqs', region_name='us-east-1')
        sqs.create_queue(QueueName='lines')
        sqs.create_queue(QueueName='lines-dlq')
        store = InMemoryIdempotencyStore()
        event = {'Records': [record('m0', json.dumps({'line_id': 'L1', 'amount': 1})),
                             record('m1', json.dumps({'line_id': 'L1', 'amount': 1})),   # duplicate
                             record('m2', json.dumps({'line_id': 'L2', 'flaky': True})),  # transient
                             record('m3', json.dumps({'line_id': 'L3'})),                 # poison
                             record('m4', '{not json')]}                                   # not JSON
        batch = SQSLambdaBatch(event, sqs)
        assert batch.source == 'lines'
        report = Lines(batch, store).consume(batch.source, {'consumer_retry_delay_seconds': 0})
        assert [m['decision'] for m in report['messages']] == \
            ['processed', 'duplicate', 'retried', 'dead_lettered', 'dead_lettered']
        assert batch.batch_item_failures() == {'batchItemFailures': [{'itemIdentifier': 'm2'}]}
        assert batch.receive(batch.source) == []                         # one batch per invocation
        dead = SQSBroker(sqs).receive_dead_letters('lines', 10)
        reasons = sorted(m.attributes['dead_letter_reason'] for m in dead)
        assert len(dead) == 2 and reasons[0].startswith('ValueError') and 'missing' in reasons[1]
        assert {'_raw': '{not json'} in [m.body for m in dead]
