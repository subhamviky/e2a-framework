"""Azure Service Bus as E2A's BaseMessageBroker (v1.7.0, design note MP-05, N1).

    destination   a queue name, or "topic/<topic>"
    source        a queue name, or "topic/<topic>/subscriptions/<subscription>"
    dead letters  the entity's own dead-letter subqueue (native): dead_letter() records the reason
                  there, and Service Bus moves a message there by itself after MaxDeliveryCount.

Peek-lock is the lock: a received message is settled (complete, abandon, dead-letter) on the
receiver that received it, so receivers are kept per source. nack() abandons: Service Bus
redelivers at once (it has no per-message delay; a delay needs a scheduled re-send).
delivery_count is Service Bus's own count.

Each sender and receiver is its own AMQP connection: a sender is opened per publish and
closed, and at most `max_receivers` receivers stay open (the least recently used is closed;
its unsettled messages are released and redelivered, which at-least-once delivery allows).

Credentials: a connection string when one is configured (the emulator), else Entra ID through
the platform identity (servicebus_namespace).
"""
import json
import threading
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Tuple

from e2a import BrokerMessage, resolve_config
from e2a.messaging import BaseMessageBroker

from .. import config_keys  # noqa: F401


def _text(value: Any) -> str:
    return value.decode('utf-8') if isinstance(value, (bytes, bytearray)) else str(value)


class ServiceBusBroker(BaseMessageBroker):
    capabilities = frozenset({'messaging', 'dead_letter'})

    def __init__(self, client=None, connection_string: str = None, namespace: str = None,
                 max_receivers: int = 4) -> None:
        self._client, self._conn, self._namespace = client, connection_string, namespace
        self._receivers: 'OrderedDict[Tuple[str, bool], Any]' = OrderedDict()
        self.max_receivers = max(1, int(max_receivers))
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'ServiceBusBroker':
        return cls(connection_string=resolve_config('servicebus_connection_string', config),
                   namespace=resolve_config('servicebus_namespace', config))

    @property
    def client(self):
        if self._client is None:
            if not (self._conn or self._namespace):
                raise ValueError('ServiceBusBroker needs servicebus_connection_string or servicebus_namespace')
            from azure.servicebus import ServiceBusClient
            if self._conn:
                self._client = ServiceBusClient.from_connection_string(self._conn)
            else:
                from azure.identity import DefaultAzureCredential
                self._client = ServiceBusClient(self._namespace, credential=DefaultAzureCredential())
        return self._client

    def _sender(self, destination: str):
        if destination.startswith('topic/'):
            return self.client.get_topic_sender(topic_name=destination[len('topic/'):])
        return self.client.get_queue_sender(queue_name=destination)

    def _receiver(self, source: str, dead: bool = False):
        from azure.servicebus import ServiceBusSubQueue
        key = (source, dead)
        with self._lock:
            if key in self._receivers:
                self._receivers.move_to_end(key)
            else:
                while len(self._receivers) >= self.max_receivers:
                    _, oldest = self._receivers.popitem(last=False)
                    oldest.close()
                sub_queue = ServiceBusSubQueue.DEAD_LETTER if dead else None
                if source.startswith('topic/'):
                    _, topic, _, subscription = source.split('/', 3)
                    self._receivers[key] = self.client.get_subscription_receiver(
                        topic_name=topic, subscription_name=subscription, sub_queue=sub_queue)
                else:
                    self._receivers[key] = self.client.get_queue_receiver(queue_name=source, sub_queue=sub_queue)
            return self._receivers[key]

    def publish(self, destination, body, attributes=None, config=None):
        from azure.servicebus import ServiceBusMessage
        mid = str(uuid.uuid4())
        message = ServiceBusMessage(json.dumps(body, ensure_ascii=False, separators=(',', ':')), message_id=mid,
                                    content_type='application/json',
                                    application_properties={str(k): str(v) for k, v in (attributes or {}).items()})
        with self._sender(destination) as sender:
            sender.send_messages(message)
        return mid

    def _convert(self, m, source: str) -> BrokerMessage:
        body = b''.join(bytes(part) for part in m.body) if not isinstance(m.body, (bytes, str)) else m.body
        attrs = {_text(k): _text(v) for k, v in (m.application_properties or {}).items()}
        if m.dead_letter_reason and 'dead_letter_reason' not in attrs:
            attrs['dead_letter_reason'] = m.dead_letter_reason
        return BrokerMessage(str(m.message_id), json.loads(_text(body)), attrs, m.delivery_count, source, m)

    def _receive(self, source: str, dead: bool, max_messages: int, wait_seconds: float) -> List[BrokerMessage]:
        with self._lock:
            got = self._receiver(source, dead).receive_messages(max_message_count=max(1, int(max_messages)),
                                                                max_wait_time=max(0.5, float(wait_seconds)))
        return [self._convert(m, source) for m in got]

    def receive(self, source, max_messages=10, wait_seconds=0.0, config=None):
        return self._receive(source, False, max_messages, wait_seconds)

    def ack(self, source, message, config=None):
        with self._lock:
            self._receiver(source).complete_message(message.receipt)

    def nack(self, source, message, delay_seconds=0.0, config=None):
        with self._lock:
            self._receiver(source).abandon_message(message.receipt)

    def dead_letter(self, source, message, reason, config=None):
        with self._lock:
            self._receiver(source).dead_letter_message(message.receipt, reason=str(reason)[:1000],
                                                       error_description=str(reason)[:1000])

    def receive_dead_letters(self, source, max_messages=10, config=None):
        return self._receive(source, True, max_messages, 0.5)

    def ack_dead_letter(self, source, message, config=None):
        with self._lock:
            self._receiver(source, True).complete_message(message.receipt)

    def close(self) -> None:
        with self._lock:
            for handle in self._receivers.values():
                handle.close()
            self._receivers.clear()
            if self._client is not None:
                self._client.close()
