"""PubSubPushDelivery (v1.10.0, hub design note GCP-01): an IdempotentConsumer settles one
Pub/Sub push request; the HTTP status tells Pub/Sub whether to deliver again."""
import base64
import json

import pytest

from e2a import IdempotentConsumer, PhysicalOutcome
from e2a_adapters.gcp import PubSubBroker, PubSubPushDelivery
from e2a_inmemory_adapters import InMemoryIdempotencyStore


class _Future:
    def __init__(self, value):
        self.value = value

    def result(self, timeout=None):
        return self.value


class FakePublisher:
    def __init__(self):
        self.published = []

    def publish(self, topic, data, **attributes):
        self.published.append((topic, json.loads(data), attributes))
        return _Future(f"id-{len(self.published)}")


def envelope(body, mid="m1", attempt=1, sub="projects/p/subscriptions/bank-lines", raw=None, **attrs):
    data = raw if raw is not None else base64.b64encode(json.dumps(body).encode()).decode()
    return {"message": {"data": data, "messageId": mid, "attributes": attrs}, "subscription": sub,
            "deliveryAttempt": attempt}


class Lines(IdempotentConsumer):
    def __init__(self, broker, store):
        super().__init__(broker, store)
        self.done = []

    def _tenant_of(self, message):
        return "acme"

    def _idempotency_key(self, message):
        return message.body.get("line_id")

    def _handle(self, message, config, tenant_id, idempotency_key):
        if message.body.get("flaky"):
            raise ConnectionError("ledger down")
        if message.body.get("amount") is None:
            raise ValueError("no amount")
        self.done.append(idempotency_key)
        return {"physical_success": True, "outcome": PhysicalOutcome.COMMITTED}


def settle(env, store, publisher=None):
    delivery = PubSubPushDelivery(env, PubSubBroker("p", publisher=publisher or FakePublisher()))
    report = Lines(delivery, store).consume(delivery.source, {"consumer_retry_delay_seconds": 0})
    return delivery, report


def test_a_processed_message_answers_204_and_a_duplicate_is_not_processed_again():
    store = InMemoryIdempotencyStore()
    first, report = settle(envelope({"line_id": "L1", "amount": 10}), store)
    assert first.source == "bank-lines" and report["messages"][0]["decision"] == "processed"
    assert first.response_status() == 204 and first.settlement == "ack"
    again, report = settle(envelope({"line_id": "L1", "amount": 10}, mid="m2", attempt=2), store)
    assert report["messages"][0]["decision"] == "duplicate" and again.response_status() == 204


def test_a_transient_failure_answers_500_so_pubsub_delivers_again():
    delivery, report = settle(envelope({"line_id": "L2", "flaky": True}), InMemoryIdempotencyStore())
    assert report["messages"][0]["decision"] == "retried" and delivery.response_status() == 500


def test_a_poison_message_is_dead_lettered_with_the_reason_then_answered_204():
    publisher = FakePublisher()
    delivery, report = settle(envelope({"line_id": "L3"}), InMemoryIdempotencyStore(), publisher)
    assert report["messages"][0]["decision"] == "dead_lettered" and delivery.response_status() == 204
    topic, body, attrs = publisher.published[-1]
    assert topic == "projects/p/topics/bank-lines-dlq" and body == {"line_id": "L3"}
    assert attrs["dead_letter_reason"].startswith("ValueError") and attrs["dead_letter_source"] == "bank-lines"


def test_a_body_that_is_not_json_is_dead_lettered_not_retried_forever():
    publisher = FakePublisher()
    raw = base64.b64encode(b"{not json").decode()
    delivery, report = settle(envelope(None, raw=raw), InMemoryIdempotencyStore(), publisher)
    assert report["messages"][0]["decision"] == "dead_lettered" and delivery.response_status() == 204
    assert publisher.published[-1][1] == {"_raw": "{not json"}


def test_one_message_per_request_and_unsettled_means_500():
    delivery = PubSubPushDelivery(envelope({"line_id": "L4", "amount": 1}), PubSubBroker("p", publisher=FakePublisher()))
    [message] = delivery.receive(delivery.source)
    assert message.body == {"line_id": "L4", "amount": 1} and message.delivery_count == 1
    assert delivery.receive(delivery.source) == [] and delivery.response_status() == 500


def test_an_envelope_that_is_not_pubsub_is_refused():
    with pytest.raises(ValueError, match="push envelope"):
        PubSubPushDelivery({"data": "x"})
