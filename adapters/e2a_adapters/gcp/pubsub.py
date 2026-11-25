"""Google Cloud Pub/Sub as E2A's BaseMessageBroker (v1.7.0, design note MP-05, N1).

    destination   a topic id (in gcp_project)
    source        a subscription id
    dead letters  the topic and subscription <source><pubsub_dead_letter_suffix> (default "-dlq").
                  A dead-letter policy on the subscription forwards a message there by itself after
                  its max delivery attempts; dead_letter() forwards it at once, with the reason.

The acknowledgement deadline is the lock; nack() sets it to the delay (0: redeliver now).
delivery_count is Pub/Sub's delivery_attempt, which it sets only for a subscription with a
dead-letter policy (None otherwise: consumer_max_deliveries does not apply, the policy does).
"""
import json
from typing import Any, Dict, List

from e2a import BrokerMessage, resolve_config
from e2a.messaging import BaseMessageBroker

from .. import config_keys  # noqa: F401
from .._lazy import Lazy


class PubSubBroker(BaseMessageBroker):
    capabilities = frozenset({'messaging', 'dead_letter'})

    def __init__(self, project: str, publisher=None, subscriber=None, dead_letter_suffix: str = '-dlq') -> None:
        # v1.7.1: the SDK is imported on first use, so a profile binds without it installed
        self.project, self.suffix = project, dead_letter_suffix
        self.publisher = publisher or Lazy(lambda: __import__('google.cloud.pubsub_v1', fromlist=['x']).PublisherClient())
        self.subscriber = subscriber or Lazy(lambda: __import__('google.cloud.pubsub_v1', fromlist=['x']).SubscriberClient())

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'PubSubBroker':
        return cls(resolve_config('gcp_project', config), dead_letter_suffix=resolve_config('pubsub_dead_letter_suffix', config))

    def _project(self) -> str:
        if not self.project:
            raise ValueError('PubSubBroker needs a project (gcp_project / GOOGLE_CLOUD_PROJECT)')
        return self.project

    def _topic(self, topic: str) -> str:
        return topic if topic.startswith('projects/') else f'projects/{self._project()}/topics/{topic}'

    def _sub(self, sub: str) -> str:
        return sub if sub.startswith('projects/') else f'projects/{self._project()}/subscriptions/{sub}'

    def publish(self, destination, body, attributes=None, config=None):
        data = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        return self.publisher.publish(self._topic(destination), data,
                                      **{str(k): str(v) for k, v in (attributes or {}).items()}).result(timeout=30)

    def _pull(self, sub: str, max_messages: int, wait_seconds: float) -> List[BrokerMessage]:
        from google.api_core import exceptions as gx
        try:
            resp = self.subscriber.pull(request={'subscription': self._sub(sub), 'max_messages': max(1, int(max_messages))},
                                        timeout=max(1.0, float(wait_seconds) + 1.0))
        except (gx.DeadlineExceeded, gx.RetryError):
            return []
        out = []
        for r in resp.received_messages:
            m = r.message
            out.append(BrokerMessage(m.message_id, json.loads(m.data.decode('utf-8')), dict(m.attributes),
                                     r.delivery_attempt or None, sub, r.ack_id))
        return out

    def receive(self, source, max_messages=10, wait_seconds=0.0, config=None):
        return self._pull(source, max_messages, wait_seconds)

    def ack(self, source, message, config=None):
        self.subscriber.acknowledge(request={'subscription': self._sub(source), 'ack_ids': [message.receipt]})

    def nack(self, source, message, delay_seconds=0.0, config=None):
        self.subscriber.modify_ack_deadline(request={'subscription': self._sub(source), 'ack_ids': [message.receipt],
                                                     'ack_deadline_seconds': max(0, min(int(delay_seconds), 600))})

    def dead_letter(self, source, message, reason, config=None):
        attrs = {**message.attributes, 'dead_letter_reason': str(reason)[:1000], 'dead_letter_source': source}
        self.publish(source + self.suffix, message.body, attrs)
        self.ack(source, message)            # only after the dead letter is safely published

    def receive_dead_letters(self, source, max_messages=10, config=None):
        return self._pull(source + self.suffix, max_messages, 0)

    def ack_dead_letter(self, source, message, config=None):
        self.subscriber.acknowledge(request={'subscription': self._sub(source + self.suffix),
                                             'ack_ids': [message.receipt]})


class PubSubPushDelivery(BaseMessageBroker):
    """v1.10.0 (hub design note GCP-01): one Pub/Sub PUSH request as a BaseMessageBroker, so
    IdempotentConsumer settles a message Pub/Sub pushed to an HTTP endpoint (Cloud Run, no
    always-on worker) as it does pulled ones. The pattern of SQSLambdaBatch, for one message:

        delivery = PubSubPushDelivery(envelope, broker=PubSubBroker(project))
        consumer.consume(delivery.source)
        return Response(status_code=delivery.response_status())   # 204 done, 500 deliver again

    ack: the request answers 2xx. nack: it answers 500 and Pub/Sub delivers again by the
    subscription's retry policy (a delay is not possible). dead_letter: the message is published
    to <subscription><suffix> with the reason (through `broker`), then answered 2xx. Without an
    explicit settlement the request answers 500 (never lose a message). The endpoint must check
    the push request's OIDC token (Cloud Run IAM does). A body that is not a JSON object arrives
    as {"_raw": <text>} with the attribute `e2a_body_error`, so a consumer can dead-letter it."""
    capabilities = frozenset({'messaging', 'dead_letter'})

    def __init__(self, envelope: Dict[str, Any], broker: 'PubSubBroker' = None, dead_letter_suffix: str = '-dlq') -> None:
        if not isinstance(envelope, dict) or not isinstance(envelope.get('message'), dict):
            raise ValueError('not a Pub/Sub push envelope: {"message": {...}, "subscription": "..."} expected')
        self._envelope, self._broker, self.suffix = envelope, broker, dead_letter_suffix
        self._taken, self._settled = False, None

    @property
    def source(self) -> str:
        """The subscription the push came from (its short name)."""
        sub = str(self._envelope.get('subscription') or '')
        if not sub:
            raise ValueError('the push envelope names no subscription')
        return sub.rsplit('/', 1)[-1]

    def _pubsub(self) -> 'PubSubBroker':
        if self._broker is None:
            raise ValueError('PubSubPushDelivery needs a PubSubBroker for dead letters and publishing')
        return self._broker

    def publish(self, destination, body, attributes=None, config=None):
        return self._pubsub().publish(destination, body, attributes, config)

    def receive(self, source, max_messages=10, wait_seconds=0.0, config=None):
        if self._taken or source != self.source:
            return []                                   # one message per request
        self._taken = True
        import base64
        m = self._envelope['message']
        attrs = {str(k): str(v) for k, v in (m.get('attributes') or {}).items()}
        raw = m.get('data') or ''
        try:
            text = base64.b64decode(raw).decode('utf-8')
        except (ValueError, UnicodeDecodeError) as e:
            text, attrs = raw, {**attrs, 'e2a_body_error': f'not base64 UTF-8: {e}'[:200]}
        body: Dict[str, Any]
        if 'e2a_body_error' in attrs:
            body = {'_raw': text}
        else:
            try:
                body = json.loads(text)
                if not isinstance(body, dict):
                    raise ValueError('the body is JSON but not an object')
            except ValueError as e:
                body, attrs = {'_raw': text}, {**attrs, 'e2a_body_error': str(e)[:200]}
        count = self._envelope.get('deliveryAttempt')
        msg_id = str(m.get('messageId') or m.get('message_id') or '')
        return [BrokerMessage(msg_id, body, attrs, int(count) if count else None, source, msg_id)]

    def ack(self, source, message, config=None):
        self._settled = 'ack'

    def nack(self, source, message, delay_seconds=0.0, config=None):
        self._settled = 'nack'

    def dead_letter(self, source, message, reason, config=None):
        attrs = {**{k: v for k, v in message.attributes.items() if v is not None},
                 'dead_letter_reason': str(reason)[:1000], 'dead_letter_source': source}
        self._pubsub().publish(source + self.suffix, message.body, attrs)
        self._settled = 'dead_lettered'                  # answered 2xx only after it is published

    def receive_dead_letters(self, source, max_messages=10, config=None):
        return self._pubsub().receive_dead_letters(source, max_messages, config)

    def ack_dead_letter(self, source, message, config=None):
        return self._pubsub().ack_dead_letter(source, message, config)

    @property
    def settlement(self):
        """'ack', 'nack', 'dead_lettered' or None (not settled)."""
        return self._settled

    def response_status(self) -> int:
        """The HTTP status to answer the push with: 204 when the message is done (processed or
        dead-lettered), 500 otherwise, so Pub/Sub delivers it again."""
        return 204 if self._settled in ('ack', 'dead_lettered') else 500
