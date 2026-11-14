"""The brokers pass E2A's messaging suite (e2a.port_contract.check_message_broker, v1.7.0): SQS
on moto always; Pub/Sub and Service Bus on their emulators when CI runs them."""
import itertools
import json
import os
import uuid

import boto3
import pytest
from moto import mock_aws

from e2a.port_contract import check_message_broker

SUITE = 25


def test_sqs_meets_the_messaging_contract():
    from e2a_adapters.aws import SQSBroker
    with mock_aws():
        sqs = boto3.client('sqs', region_name='us-east-1')
        broker = SQSBroker(sqs)

        def make():
            q = f'q-{uuid.uuid4().hex[:8]}'
            sqs.create_queue(QueueName=q)
            sqs.create_queue(QueueName=q + '-dlq')
            return broker, q, q
        assert len(check_message_broker(make, timeout=5)) == SUITE


def test_sns_fans_out_to_queues_with_the_attributes():
    from e2a_adapters.aws import SQSBroker
    with mock_aws():
        sqs, sns = boto3.client('sqs', region_name='us-east-1'), boto3.client('sns', region_name='us-east-1')
        topic = sns.create_topic(Name='orders')['TopicArn']
        url = sqs.create_queue(QueueName='billing')['QueueUrl']
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=['QueueArn'])['Attributes']['QueueArn']
        sns.subscribe(TopicArn=topic, Protocol='sqs', Endpoint=arn, Attributes={'RawMessageDelivery': 'true'})
        broker = SQSBroker(sqs, sns)
        broker.publish(topic, {'order': 'A-1', 'amount': '10.50'}, {'tenant_id': 't1', 'idempotency_key': 'k1'})
        [m] = broker.receive('billing')
        assert m.body == {'order': 'A-1', 'amount': '10.50'} and (m.tenant_id, m.idempotency_key) == ('t1', 'k1')


def test_sqs_from_config_reads_region_and_endpoint():
    from e2a_adapters.aws import SQSBroker
    with mock_aws():
        boto3.client('sqs', region_name='eu-west-1').create_queue(QueueName='jobs')
        broker = SQSBroker.from_config({'aws_region': 'eu-west-1'})
        broker.publish('jobs', {'n': 1}, {'tenant_id': 't', 'idempotency_key': 'k'})
        assert broker.receive('jobs')[0].body == {'n': 1}


@pytest.mark.skipif(not os.getenv('PUBSUB_EMULATOR_HOST'), reason='PUBSUB_EMULATOR_HOST not set (Pub/Sub emulator not running)')
def test_pubsub_meets_the_messaging_contract():
    from google.cloud import pubsub_v1

    from e2a_adapters.gcp import PubSubBroker
    project = 'demo-e2a-adapters'
    pub, sub = pubsub_v1.PublisherClient(), pubsub_v1.SubscriberClient()
    broker = PubSubBroker(project, pub, sub)

    def make():
        name = f'm{uuid.uuid4().hex[:8]}'
        for n in (name, name + '-dlq'):
            pub.create_topic(request={'name': f'projects/{project}/topics/{n}'})
            sub.create_subscription(request={'name': f'projects/{project}/subscriptions/{n}',
                                             'topic': f'projects/{project}/topics/{n}', 'ack_deadline_seconds': 30})
        return broker, name, name
    # Pub/Sub counts deliveries only under a dead-letter policy, so the count check is not run
    assert len(check_message_broker(make, timeout=15)) == SUITE - 1


@pytest.mark.skipif(not os.getenv('SERVICEBUS_CONNECTION_STRING'),
                    reason='SERVICEBUS_CONNECTION_STRING not set (Service Bus emulator not running)')
def test_service_bus_meets_the_messaging_contract():
    from e2a_adapters.azure import ServiceBusBroker
    broker = ServiceBusBroker(connection_string=os.environ['SERVICEBUS_CONNECTION_STRING'])
    # the emulator declares its queues up front (adapters/tests/servicebus-emulator.json): q0 .. q29
    names = (f'q{i}' for i in itertools.count())
    try:
        def make():
            q = next(names)
            return broker, q, q
        assert len(check_message_broker(make, timeout=15)) == SUITE
    finally:
        broker.close()
