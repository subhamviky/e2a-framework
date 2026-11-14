"""Messaging: exactly-once effects under at-least-once delivery (v1.7.0, design note MP-05, N1).

Every managed broker (SQS/SNS, Pub/Sub, Service Bus) delivers at least once: a message can
arrive twice, arrive after its effect already happened (a lost acknowledgement), or never be
processable at all (a poison message). E2A turns that into exactly-once *effects* with the
claim it already uses for requests:

    broker ──receive──▶ IdempotentConsumer.consume()
                         claim(idempotency_key, tenant)        BaseIdempotencyStore
                           CLAIMED      -> _handle(message)  -> COMMITTED: complete, ack
                                                             -> NOT_COMMITTED / transient error: fail(retryable), nack
                                                             -> deterministic error: fail(final), dead-letter
                                                             -> UNKNOWN: fail(UNKNOWN), dead-letter
                           REPLAY       -> ack (a duplicate: the effect already happened)
                           IN_PROGRESS  -> nack with a delay (another consumer holds it)
                           CONFLICT / FAILED_FINAL / RECOVERY_REQUIRED -> dead-letter
    dead letters ──▶ IdempotentConsumer.reconcile()   (BaseReconciliationService)
                         confirm physical truth first (_confirm), replay only NOT_COMMITTED

Ports here are vendor-neutral. The adapters (e2a-adapters: SQS/SNS, Pub/Sub, Service Bus) and
the outbox stores implement them; `e2a.port_contract.check_message_broker` is the suite every
adapter passes, on emulators.
"""
import hashlib
import json
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import resolve_config
from .core import _DETERMINISTIC_ERRORS, ClaimStatus, PhysicalOutcome, outcome_of
from .ports import BaseIdempotencyStore, BaseOutboxSweeper, BaseReconciliationService

__all__ = ['BrokerMessage', 'BaseMessageBroker', 'InMemoryBroker', 'IdempotentConsumer', 'BaseOutboxStore',
           'InMemoryOutboxStore', 'BrokerOutboxSweeper', 'body_fingerprint']


@dataclass
class BrokerMessage:
    """One delivered message, the same shape whatever the broker.

    `attributes` are strings (every broker carries string attributes); `tenant_id` and
    `idempotency_key` travel there. `delivery_count` is the broker's own count when it has
    one (None when it does not, as Pub/Sub without a dead-letter policy). `receipt` is the
    adapter's handle for ack / nack / dead-letter, opaque to everything else."""
    id: str
    body: Dict[str, Any]
    attributes: Dict[str, str] = field(default_factory=dict)
    delivery_count: Optional[int] = None
    destination: str = ''
    receipt: Any = None

    @property
    def tenant_id(self) -> Optional[str]:
        return self.attributes.get('tenant_id')

    @property
    def idempotency_key(self) -> Optional[str]:
        return self.attributes.get('idempotency_key')


def body_fingerprint(body: Dict[str, Any]) -> str:
    """The request fingerprint of a message body: the same key with a different body is a CONFLICT."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


class BaseMessageBroker(ABC):
    """Port (v1.7.0): one broker, publish and consume. `destination` is where a message is
    published (a queue or topic); `source` is where it is received from (a queue or a
    subscription). Every source has a dead-letter source of its own, which the adapter
    resolves (native where the broker has one, a configured queue or topic where not)."""
    capabilities = frozenset({'messaging'})

    @abstractmethod
    def publish(self, destination: str, body: Dict[str, Any], attributes: Optional[Dict[str, str]] = None,
                config: Dict[str, Any] = None) -> str:
        """Publish one JSON body; return the broker's message id."""

    @abstractmethod
    def receive(self, source: str, max_messages: int = 10, wait_seconds: float = 0.0,
                config: Dict[str, Any] = None) -> List[BrokerMessage]:
        """Up to max_messages, each hidden from other consumers until acked, nacked or its
        lock / visibility expires."""

    @abstractmethod
    def ack(self, source: str, message: BrokerMessage, config: Dict[str, Any] = None) -> None:
        """Done: the broker never delivers this message again."""

    @abstractmethod
    def nack(self, source: str, message: BrokerMessage, delay_seconds: float = 0.0,
             config: Dict[str, Any] = None) -> None:
        """Not done: deliver it again (after delay_seconds where the broker supports a delay)."""

    @abstractmethod
    def dead_letter(self, source: str, message: BrokerMessage, reason: str, config: Dict[str, Any] = None) -> None:
        """Move the message to the source's dead letters, with the reason, and remove it from the source."""

    @abstractmethod
    def receive_dead_letters(self, source: str, max_messages: int = 10,
                             config: Dict[str, Any] = None) -> List[BrokerMessage]:
        """Dead letters of a source; `attributes['dead_letter_reason']` carries the reason."""

    @abstractmethod
    def ack_dead_letter(self, source: str, message: BrokerMessage, config: Dict[str, Any] = None) -> None:
        """A dead letter is settled (its effect confirmed, or replayed): remove it."""


# ------------------------------------------------------------------ the reference broker
class InMemoryBroker(BaseMessageBroker):
    """The reference broker: one process, at-least-once like a real one (a nacked or
    unacked message is delivered again, with its delivery count). Destinations and sources
    are the same names (queues). Used by tests and by the contract suite's own self-check."""

    def __init__(self, visibility_seconds: float = 30.0, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._queues: Dict[str, List[dict]] = {}
        self._dead: Dict[str, List[dict]] = {}
        self.visibility_seconds, self._clock = visibility_seconds, clock

    def publish(self, destination, body, attributes=None, config=None):
        mid = str(uuid.uuid4())
        with self._lock:
            self._queues.setdefault(destination, []).append(
                {'id': mid, 'body': json.loads(json.dumps(body)), 'attributes': dict(attributes or {}),
                 'count': 0, 'visible_at': 0.0, 'destination': destination})
        return mid

    def receive(self, source, max_messages=10, wait_seconds=0.0, config=None):
        now, out = self._clock(), []
        with self._lock:
            for item in self._queues.get(source, []):
                if len(out) >= max_messages:
                    break
                if item['visible_at'] <= now:
                    item['count'] += 1
                    item['visible_at'] = now + self.visibility_seconds
                    item['receipt'] = str(uuid.uuid4())
                    out.append(BrokerMessage(item['id'], json.loads(json.dumps(item['body'])), dict(item['attributes']),
                                             item['count'], item['destination'], item['receipt']))
        return out

    def _take(self, queue, message):
        for item in queue:
            if item['id'] == message.id and item.get('receipt') == message.receipt:
                queue.remove(item)
                return item
        return None

    def ack(self, source, message, config=None):
        with self._lock:
            self._take(self._queues.get(source, []), message)

    def nack(self, source, message, delay_seconds=0.0, config=None):
        with self._lock:
            for item in self._queues.get(source, []):
                if item['id'] == message.id and item.get('receipt') == message.receipt:
                    item['visible_at'] = self._clock() + float(delay_seconds)

    def dead_letter(self, source, message, reason, config=None):
        with self._lock:
            item = self._take(self._queues.get(source, []), message)
            if item is not None:
                item['attributes'] = {**item['attributes'], 'dead_letter_reason': str(reason)}
                item['visible_at'] = 0.0
                self._dead.setdefault(source, []).append(item)

    def receive_dead_letters(self, source, max_messages=10, config=None):
        now, out = self._clock(), []
        with self._lock:
            for item in self._dead.get(source, []):
                if len(out) >= max_messages:
                    break
                if item['visible_at'] <= now:
                    item['visible_at'] = now + self.visibility_seconds
                    item['receipt'] = str(uuid.uuid4())
                    out.append(BrokerMessage(item['id'], json.loads(json.dumps(item['body'])), dict(item['attributes']),
                                             item['count'], item['destination'], item['receipt']))
        return out

    def ack_dead_letter(self, source, message, config=None):
        with self._lock:
            self._take(self._dead.get(source, []), message)


# ------------------------------------------------------------------ the consumer
class IdempotentConsumer(BaseReconciliationService):
    """Exactly-once effects from an at-least-once broker. Public: consume() (the live path) and
    the inherited reconcile() (the dead letters, confirming physical truth before any replay).

    A subclass implements _handle(message, config, tenant_id, idempotency_key) and returns
    the physical_success contract ({'physical_success': bool, 'outcome': ...}). It may
    override _idempotency_key / _tenant_of (default: the message attributes) and _confirm
    (default UNKNOWN, so nothing is replayed until the system of record can be asked)."""

    def __init__(self, broker: BaseMessageBroker, idempotency_store: BaseIdempotencyStore,
                 source: Optional[str] = None, lock=None) -> None:
        super().__init__(lock=lock, idempotency_store=idempotency_store)
        self.broker, self.source = broker, source
        self.settlements: List[tuple] = []     # (dead letter id, decision) from reconcile(), newest last

    # ------------------------------------------------------------ public: the live path
    def consume(self, source: Optional[str] = None, config: Dict[str, Any] = None,
                correlation_id: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """Receive one batch from `source` and settle every message. Never raises for one
        message: each is acked, nacked or dead-lettered, counted in the report and listed in
        report['messages'] (v1.7.2) with its decision."""
        config = config or {}
        source = source or self.source
        if not source:
            raise ValueError('consume(): no source (pass one, or construct the consumer with source=)')
        correlation_id = correlation_id or str(uuid.uuid4())
        batch = int(resolve_config('consumer_batch_size', config, kwargs))
        wait = float(resolve_config('consumer_wait_seconds', config, kwargs))
        report = {'correlation_id': correlation_id, 'received': 0, 'processed': 0, 'duplicates': 0,
                  'deferred': 0, 'retried': 0, 'dead_lettered': 0, 'dead_letter_reasons': [], 'messages': []}
        counter = {'processed': 'processed', 'duplicate': 'duplicates', 'deferred': 'deferred',
                   'retried': 'retried', 'dead_lettered': 'dead_lettered'}
        for message in self.broker.receive(source, max_messages=batch, wait_seconds=wait, config=config):
            report['received'] += 1
            settled = self.settle(message, source, config, correlation_id, **kwargs)
            report[counter[settled['decision']]] += 1
            if settled['decision'] == 'dead_lettered':
                report['dead_letter_reasons'].append(settled['reason'])
            report['messages'].append({k: v for k, v in settled.items() if k != 'error'})
        return report

    def settle(self, message: BrokerMessage, source: Optional[str] = None, config: Dict[str, Any] = None,
               correlation_id: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """Public (v1.7.2): settle ONE received message under its claim, through the broker, and
        say what happened: {message_id, idempotency_key, decision, reason, result, error}.
        decision: processed (result: the handler's), duplicate (result: the stored one),
        deferred, retried (error: the exception, if one), dead_lettered (reason; error, if one).
        consume() calls it for each message of a batch; a synchronous entry point can call it
        for one message it publishes and receives itself, so both paths share one claim."""
        config = config or {}
        source = source or self.source
        owner = correlation_id or str(uuid.uuid4())
        out: Dict[str, Any] = {'message_id': message.id, 'idempotency_key': None, 'decision': None,
                               'reason': None, 'result': None, 'error': None}
        delay = float(resolve_config('consumer_retry_delay_seconds', config, kwargs))

        def decided(decision, **fields):
            out.update(decision=decision, **fields)
            return out

        def dead(reason, error=None):
            self.broker.dead_letter(source, message, reason, config)
            return decided('dead_lettered', reason=reason, error=error)

        tenant, key = self._tenant_of(message), self._idempotency_key(message)
        out['idempotency_key'] = key
        if not tenant or not key:
            return dead('missing tenant_id or idempotency_key')
        limit = int(resolve_config('consumer_max_deliveries', config, kwargs))
        if message.delivery_count is not None and limit and message.delivery_count > limit:
            return dead(f'delivered {message.delivery_count} times (consumer_max_deliveries={limit})')
        claim = self.idempotency_store.claim(
            key, config, tenant_id=tenant, owner=owner, fingerprint=body_fingerprint(message.body),
            lease_seconds=int(resolve_config('idempotency_lease_seconds', config, kwargs)),
            # v1.7.2: a resumable handler may take over a claim whose owner stopped (expired lease)
            recover=bool(resolve_config('consumer_resume_expired', config, kwargs)))
        if claim.status == ClaimStatus.REPLAY:
            self.broker.ack(source, message, config)            # a duplicate, or a lost acknowledgement
            return decided('duplicate', result=claim.result)
        if claim.status == ClaimStatus.IN_PROGRESS:
            self.broker.nack(source, message, delay, config)
            return decided('deferred', reason='another consumer holds the claim')
        if claim.status != ClaimStatus.CLAIMED:                 # CONFLICT, FAILED_FINAL, RECOVERY_REQUIRED
            return dead(f'claim {claim.status.value}')
        try:
            result = self._handle(message, config, tenant_id=tenant, idempotency_key=key)
        except _DETERMINISTIC_ERRORS as e:
            self.idempotency_store.fail(key, retryable=False, config=config, tenant_id=tenant, owner=owner)
            return dead(f'{type(e).__name__}: {e}', error=e)
        except Exception as e:
            self.idempotency_store.fail(key, retryable=True, config=config, tenant_id=tenant, owner=owner)
            self.broker.nack(source, message, delay, config)
            return decided('retried', reason=f'{type(e).__name__}: {e}', error=e)
        outcome = outcome_of(result)
        if outcome == PhysicalOutcome.COMMITTED:
            self.idempotency_store.complete(key, dict(result or {}), config, tenant_id=tenant, owner=owner)
            self.broker.ack(source, message, config)
            return decided('processed', result=result)
        if outcome == PhysicalOutcome.UNKNOWN:
            # the effect may have happened: never retried blindly; reconcile() confirms it first
            self.idempotency_store.fail(key, retryable=False, config=config, tenant_id=tenant, owner=owner,
                                        outcome=PhysicalOutcome.UNKNOWN)
            out['result'] = result
            return dead('outcome UNKNOWN')
        self.idempotency_store.fail(key, retryable=True, config=config, tenant_id=tenant, owner=owner)
        self.broker.nack(source, message, delay, config)
        return decided('retried', reason='not committed', result=result)

    # ------------------------------------------------------------ hooks
    @abstractmethod
    def _handle(self, message: BrokerMessage, config: Dict[str, Any], tenant_id: str,
                idempotency_key: str) -> Dict[str, Any]:
        """The effect. Return {'physical_success': bool, 'outcome': PhysicalOutcome (optional)}.
        Raise a ValueError / TypeError / PermissionError for a message that can never succeed."""

    def _idempotency_key(self, message: BrokerMessage) -> Optional[str]:
        return message.idempotency_key

    def _tenant_of(self, message: BrokerMessage) -> Optional[str]:
        return message.tenant_id

    def _confirm(self, message: BrokerMessage, config: Dict[str, Any]) -> PhysicalOutcome:
        """Did this message's effect commit? Ask the system of record. Default UNKNOWN: an
        UNKNOWN dead letter stays parked until a subclass can answer."""
        return PhysicalOutcome.UNKNOWN

    # ------------------------------------------------------------ reconcile(): BaseReconciliationService hooks
    def _fetch_dlq_entries(self, config=None, **kwargs):
        source = kwargs.get('dlq_source') or self.source
        batch = int(resolve_config('consumer_batch_size', config, kwargs))
        return [{'id': m.id, 'idempotency_key': self._idempotency_key(m), 'tenant_id': self._tenant_of(m),
                 'fingerprint': body_fingerprint(m.body), 'message': m, 'source': source}
                for m in self.broker.receive_dead_letters(source, max_messages=batch, config=config)]

    def _confirm_physical_outcome(self, entry, config=None, **kwargs):
        return self._confirm(entry['message'], config or {})

    def _replay_dlq_entry(self, entry, config=None, **kwargs):
        message = entry['message']
        try:
            result = self._handle(message, config or {}, tenant_id=entry['tenant_id'],
                                  idempotency_key=entry['idempotency_key'])
        except Exception:
            return False
        if outcome_of(result) == PhysicalOutcome.COMMITTED:
            entry['result'] = dict(result or {})
            return True
        return False

    def _settle_dlq_entry(self, entry, outcome, config=None, **kwargs):
        # settled dead letters leave the DLQ; UNRESOLVED and FAILED ones stay for the next run or a person
        self.settlements.append((entry['id'], outcome))
        if outcome in ('COMMITTED', 'REPLAYED', 'SKIPPED_REPLAY'):
            self.broker.ack_dead_letter(entry['source'], entry['message'], config)


# ------------------------------------------------------------------ the outbox
class BaseOutboxStore(ABC):
    """Port (v1.7.0): the transactional outbox. A record is written in the same transaction
    as the business change (add()), and published later by a sweeper, so a message is
    published if and only if the change committed. Record: {id, destination, body,
    attributes, tenant_id, idempotency_key}."""

    @abstractmethod
    def add(self, record: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> str:
        """Store a pending record; return its id. kwargs may carry the business transaction."""

    @abstractmethod
    def pending(self, limit: int = 100, config: Dict[str, Any] = None, **kwargs) -> List[Dict[str, Any]]:
        """The oldest pending records first."""

    @abstractmethod
    def mark_published(self, record_id: str, config: Dict[str, Any] = None, **kwargs) -> bool:
        """Pending -> published. kwargs: fencing_token (a stale sweeper's write is refused)."""


class InMemoryOutboxStore(BaseOutboxStore):
    def __init__(self) -> None:
        self._lock, self._records, self._fence = threading.Lock(), {}, -1

    def add(self, record, config=None, **kwargs):
        rid = record.get('id') or str(uuid.uuid4())
        with self._lock:
            self._records[rid] = {**record, 'id': rid, 'status': 'PENDING', 'created_at': time.time()}
        return rid

    def pending(self, limit=100, config=None, **kwargs):
        with self._lock:
            rows = sorted((r for r in self._records.values() if r['status'] == 'PENDING'), key=lambda r: r['created_at'])
            return [dict(r) for r in rows[:limit]]

    def mark_published(self, record_id, config=None, **kwargs):
        token = kwargs.get('fencing_token')
        with self._lock:
            if token is not None:
                if token < self._fence:
                    return False
                self._fence = token
            record = self._records.get(record_id)
            if record is None or record['status'] != 'PENDING':
                return False
            record['status'] = 'PUBLISHED'
            return True


class BrokerOutboxSweeper(BaseOutboxSweeper):
    """BaseOutboxSweeper over an outbox store and a broker. A record is published with its
    tenant_id and idempotency_key as attributes, so the consumer's claim makes a second
    publication (a sweeper that died after publish, before mark_published) harmless."""

    def __init__(self, outbox: BaseOutboxStore, broker: BaseMessageBroker, lock=None) -> None:
        super().__init__(lock=lock)
        self.outbox, self.broker = outbox, broker

    def _fetch_pending_records(self, config=None, **kwargs):
        return self.outbox.pending(int(kwargs.get('batch_size') or resolve_config('consumer_batch_size', config, kwargs)),
                                   config)

    def _publish_pending_record(self, record, config=None, **kwargs):
        if not record.get('tenant_id') or not record.get('idempotency_key'):
            return False                        # never publish what a consumer could not deduplicate
        attributes = {**(record.get('attributes') or {}), 'tenant_id': str(record['tenant_id']),
                      'idempotency_key': str(record['idempotency_key'])}
        self.broker.publish(record['destination'], record['body'], attributes, config)
        return self.outbox.mark_published(record['id'], config, fencing_token=kwargs.get('fencing_token'))
