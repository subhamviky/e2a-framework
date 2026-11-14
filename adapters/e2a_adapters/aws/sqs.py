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


class SQSLambdaBatch(BaseMessageBroker):
    """v1.7.2: one Lambda invocation's SQS batch as a BaseMessageBroker, so IdempotentConsumer
    settles messages that Lambda pushed (an event source mapping) as it does pulled ones.

        broker = SQSLambdaBatch(event, sqs_client)
        consumer.consume(broker.source)                 # every record of the batch
        return broker.batch_item_failures()             # the Lambda response

    ack: nothing to do (a record not reported as failed is deleted by Lambda). nack: the record
    is reported in batchItemFailures, and SQS delivers it again after its visibility timeout (a
    delay is not possible). dead_letter: the record is sent to <queue><suffix> with the reason,
    then counts as done. The mapping must enable ReportBatchItemFailures. Dead letters are read
    and settled through SQS itself (the scheduled reconcile() uses SQSBroker or this class).
    A record whose body is not JSON arrives with body {"_raw": <text>} and the attribute
    `e2a_body_error`, so a consumer can dead-letter it with the reason instead of failing the batch."""
    capabilities = frozenset({'messaging', 'dead_letter'})

    def __init__(self, event: Dict[str, Any], client=None, dead_letter_suffix: str = '-dlq') -> None:
        self._records = [r for r in (event or {}).get('Records') or [] if r.get('eventSource') == 'aws:sqs']
        self._queue = SQSBroker(client, dead_letter_suffix=dead_letter_suffix) if client is not None else None
        self._failed: List[str] = []
        self._taken = False

    @staticmethod
    def _queue_name(record: Dict[str, Any]) -> str:
        return str(record.get('eventSourceARN') or '').rsplit(':', 1)[-1]

    @property
    def source(self) -> str:
        """The queue the batch came from (one event source mapping, one queue)."""
        names = {self._queue_name(r) for r in self._records}
        if len(names) != 1:
            raise ValueError(f'an SQS batch comes from one queue, not {sorted(names) or "none"}')
        return names.pop()

    def _sqs(self) -> SQSBroker:
        if self._queue is None:
            raise ValueError('SQSLambdaBatch needs an SQS client for dead letters and publishing')
        return self._queue

    def publish(self, destination, body, attributes=None, config=None):
        return self._sqs().publish(destination, body, attributes, config)

    def receive(self, source, max_messages=10, wait_seconds=0.0, config=None):
        if self._taken:
            return []                               # one batch per invocation
        self._taken = True
        out = []
        for r in self._records:
            if self._queue_name(r) != source:
                continue
            attrs = {k: v.get('stringValue') for k, v in (r.get('messageAttributes') or {}).items()
                     if v.get('stringValue') is not None}
            try:
                body = json.loads(r.get('body') or '')
                if not isinstance(body, dict):
                    raise ValueError('the body is JSON but not an object')
            except ValueError as e:
                body, attrs = {'_raw': r.get('body')}, {**attrs, 'e2a_body_error': str(e)[:200]}
            count = int((r.get('attributes') or {}).get('ApproximateReceiveCount', 0)) or None
            out.append(BrokerMessage(r['messageId'], body, attrs, count, source, r.get('receiptHandle')))
        return out

    def ack(self, source, message, config=None):
        return None

    def nack(self, source, message, delay_seconds=0.0, config=None):
        if message.id not in self._failed:
            self._failed.append(message.id)

    def dead_letter(self, source, message, reason, config=None):
        attrs = {**{k: v for k, v in message.attributes.items() if v is not None},
                 'dead_letter_reason': str(reason)[:1000], 'dead_letter_source': source}
        body = message.body if '_raw' not in message.body else {'_raw': message.body['_raw']}
        self._sqs().client.send_message(QueueUrl=self._sqs()._url(source + self._sqs().suffix),
                                        MessageBody=json.dumps(body, ensure_ascii=False, separators=(',', ':')),
                                        MessageAttributes=self._sqs()._attrs(attrs))
        # not reported as failed: Lambda deletes it from the source

    def receive_dead_letters(self, source, max_messages=10, config=None):
        return self._sqs().receive_dead_letters(source, max_messages, config)

    def ack_dead_letter(self, source, message, config=None):
        return self._sqs().ack_dead_letter(source, message, config)

    def batch_item_failures(self) -> Dict[str, List[Dict[str, str]]]:
        """The Lambda response: the records to deliver again."""
        return {'batchItemFailures': [{'itemIdentifier': i} for i in self._failed]}
