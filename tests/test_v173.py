"""v1.7.3 (design note MP-05): a row and its outbox records in one commit."""
import pytest

from e2a import BrokerOutboxSweeper, InMemoryBroker, InMemoryOutboxStore
from e2a.port_contract import check_transactional_outbox
from e2a_inmemory_adapters import InMemoryTransactionalStore


def make():
    outbox = InMemoryOutboxStore()
    return InMemoryTransactionalStore(outbox=outbox), outbox


def test_the_reference_store_meets_the_transactional_outbox_contract():
    passed = check_transactional_outbox(make)
    assert len(passed) == 9 and 'a stale version adds no record and keeps the row' in passed


def test_records_without_an_outbox_are_refused_before_anything_is_written():
    store = InMemoryTransactionalStore()
    with pytest.raises(ValueError, match='no outbox'):
        store.put_if_version('k', {'a': 1}, None, outbox=[{'destination': 'd', 'body': {}}])
    assert store.get('k') is None
    assert store.put_if_version('k', {'a': 1}, None).status == 'COMMITTED'      # without records, as before


def test_the_sweeper_publishes_each_committed_record_once():
    store, outbox = make()
    broker = InMemoryBroker()
    store.put_if_version('order:1', {'state': 'PLACED'}, None,
                         outbox=[{'destination': 'orders', 'body': {'order': 1}, 'attributes': {'kind': 'order.placed'},
                                  'tenant_id': 't1', 'idempotency_key': 'order:1:placed'}])
    store.put_if_version('order:1', {'state': 'X'}, None,
                         outbox=[{'destination': 'orders', 'body': {'order': 1}, 'tenant_id': 't1',
                                  'idempotency_key': 'order:1:lost'}])                       # CONFLICT: no event
    sweeper = BrokerOutboxSweeper(outbox, broker)
    sweeper.sweep()
    sweeper.sweep()
    got = broker.receive('orders', max_messages=10)
    assert [m.idempotency_key for m in got] == ['order:1:placed'] and got[0].tenant_id == 't1'


def test_the_capability_is_registered():
    from e2a.capabilities import CAPABILITIES
    assert 'transactional_outbox' in CAPABILITIES and 'transactional_outbox' in InMemoryTransactionalStore.capabilities
