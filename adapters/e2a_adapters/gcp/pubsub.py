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
        from google.cloud import pubsub_v1
        self.project, self.suffix = project, dead_letter_suffix
        self.publisher = publisher or Lazy(pubsub_v1.PublisherClient)
        self.subscriber = subscriber or Lazy(pubsub_v1.SubscriberClient)

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
