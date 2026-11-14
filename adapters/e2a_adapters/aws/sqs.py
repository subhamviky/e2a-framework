"""Amazon SQS (and SNS) as E2A's BaseMessageBroker (v1.7.0, design note MP-05, N1).

    destination   a queue name, or an SNS topic ARN (fan-out to queues subscribed with raw
                  message delivery, so attributes arrive as SQS message attributes)
    source        a queue name
    dead letters  the queue <source><sqs_dead_letter_suffix> (default "-dlq"). A redrive policy on
                  the source moves a message there by itself after maxReceiveCount; dead_letter()
                  moves it at once, with the reason, when E2A knows it can never succeed.

Visibility is the lock: a received message is hidden for the queue's visibility timeout;
nack() makes it visible again after the delay. delivery_count is ApproximateReceiveCount.
"""
import json
from typing import Any, Dict, List, Optional

from e2a import BrokerMessage, resolve_config
from e2a.messaging import BaseMessageBroker

from .. import config_keys  # noqa: F401
from .._lazy import Lazy


class SQSBroker(BaseMessageBroker):
    capabilities = frozenset({'messaging', 'dead_letter'})

    def __init__(self, client=None, sns_client=None, dead_letter_suffix: str = '-dlq') -> None:
        if client is None:
            raise ValueError('SQSBroker needs a boto3 SQS client (or use SQSBroker.from_config)')
        self.client, self.sns, self.suffix = client, sns_client, dead_letter_suffix
        self._urls: Dict[str, str] = {}

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'SQSBroker':
        import boto3
        region, endpoint = resolve_config('aws_region', config), resolve_config('aws_endpoint_url', config)
        return cls(Lazy(lambda: boto3.client('sqs', region_name=region, endpoint_url=endpoint)),
                   Lazy(lambda: boto3.client('sns', region_name=region, endpoint_url=endpoint)),
                   resolve_config('sqs_dead_letter_suffix', config))

    def _url(self, name: str) -> str:
        if name not in self._urls:
            self._urls[name] = self.client.get_queue_url(QueueName=name)['QueueUrl']
        return self._urls[name]

    @staticmethod
    def _attrs(attributes: Optional[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
        return {str(k): {'DataType': 'String', 'StringValue': str(v)} for k, v in (attributes or {}).items()}

    def publish(self, destination, body, attributes=None, config=None):
        text = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
        if destination.startswith('arn:aws:sns:'):
            if self.sns is None:
                raise ValueError('publishing to an SNS topic needs sns_client')
            return self.sns.publish(TopicArn=destination, Message=text,
                                    MessageAttributes=self._attrs(attributes))['MessageId']
        return self.client.send_message(QueueUrl=self._url(destination), MessageBody=text,
                                        MessageAttributes=self._attrs(attributes))['MessageId']

    def _receive(self, queue: str, max_messages: int, wait_seconds: float) -> List[BrokerMessage]:
        resp = self.client.receive_message(QueueUrl=self._url(queue), MaxNumberOfMessages=max(1, min(int(max_messages), 10)),
                                           WaitTimeSeconds=max(0, min(int(wait_seconds), 20)),
                                           MessageAttributeNames=['All'], AttributeNames=['ApproximateReceiveCount'])
        out = []
        for m in resp.get('Messages', []):
            attrs = {k: v.get('StringValue') for k, v in (m.get('MessageAttributes') or {}).items()}
            out.append(BrokerMessage(m['MessageId'], json.loads(m['Body']), attrs,
                                     int((m.get('Attributes') or {}).get('ApproximateReceiveCount', 0)) or None,
                                     queue, m['ReceiptHandle']))
        return out

    def receive(self, source, max_messages=10, wait_seconds=0.0, config=None):
        return self._receive(source, max_messages, wait_seconds)

    def ack(self, source, message, config=None):
        self.client.delete_message(QueueUrl=self._url(source), ReceiptHandle=message.receipt)

    def nack(self, source, message, delay_seconds=0.0, config=None):
        self.client.change_message_visibility(QueueUrl=self._url(source), ReceiptHandle=message.receipt,
                                              VisibilityTimeout=max(0, min(int(delay_seconds), 43200)))

    def dead_letter(self, source, message, reason, config=None):
        attrs = {**message.attributes, 'dead_letter_reason': str(reason)[:1000], 'dead_letter_source': source}
        self.client.send_message(QueueUrl=self._url(source + self.suffix),
                                 MessageBody=json.dumps(message.body, ensure_ascii=False, separators=(',', ':')),
                                 MessageAttributes=self._attrs(attrs))
        self.ack(source, message)            # only after the dead letter is safely stored

    def receive_dead_letters(self, source, max_messages=10, config=None):
        return self._receive(source + self.suffix, max_messages, 0)

    def ack_dead_letter(self, source, message, config=None):
        self.client.delete_message(QueueUrl=self._url(source + self.suffix), ReceiptHandle=message.receipt)
