"""The transactional outbox on Cosmos DB (v1.7.1, design note MP-05, N1).

Cosmos DB commits atomically only within one logical partition, so a record lives in the
partition of the business document it belongs to. Pass that partition key and put
batch_operation(record, partition_key) into the application's transactional batch
(container.execute_item_batch). The record is then committed if and only if the business change
is. add(record, partition_key=...) writes it alone when there is no other write.

Layout, in e2a-adapters' container (partition key /pk):
    {id: <record id>, pk: <business partition, or OUTBOX#<outbox_name>>, e2a_outbox: <outbox_name>, ...}
    {id: <outbox_name>, pk: "OUTBOX-FENCE", fence: <newest fencing token seen>}

pending() is one cross-partition query ordered by created_at. mark_published() checks and
advances the fence with an ETag condition, then deletes the record with its ETag: two
conditional writes, not one transaction, because the fence and the record are in different
partitions. A stale sweeper that passes the fence check just before a newer one advances it can
still mark a record the newer one also published. That is harmless: consumers deduplicate on
the record's idempotency key.
"""
import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from azure.core import MatchConditions
from azure.cosmos import exceptions as cx
from e2a import resolve_config
from e2a.messaging import BaseOutboxStore

from .. import config_keys  # noqa: F401
from .cosmos import _container_from

_FENCE_PK = 'OUTBOX-FENCE'


class CosmosOutboxStore(BaseOutboxStore):
    capabilities = frozenset({'outbox_store'})

    def __init__(self, container, name: str = 'default', partition_key_field: str = 'pk',
                 clock: Callable[[], float] = time.time) -> None:
        self.container, self.name, self.pk, self.clock = container, name, partition_key_field, clock

    @classmethod
    def from_config(cls, config: Dict[str, Any], **kwargs) -> 'CosmosOutboxStore':
        return cls(_container_from(config), resolve_config('outbox_name', config), **kwargs)

    def _new_id(self) -> str:
        return f'{int(self.clock() * 1_000_000):017d}-{uuid.uuid4().hex[:12]}'

    def _item(self, record: Dict[str, Any], partition_key: Optional[str]) -> Dict[str, Any]:
        record.setdefault('id', self._new_id())
        return {'id': record['id'], self.pk: partition_key or f'OUTBOX#{self.name}', 'e2a_outbox': self.name,
                'created_at': self.clock(),
                'record': json.dumps({k: record.get(k) for k in ('destination', 'body', 'attributes', 'tenant_id',
                                                                'idempotency_key')},
                                     separators=(',', ':'), ensure_ascii=False)}

    def batch_operation(self, record: Dict[str, Any], partition_key: str) -> Tuple[str, tuple]:
        """The create operation for the application's transactional batch on `partition_key`."""
        return ('create', (self._item(record, partition_key),))

    def add(self, record, config=None, **kwargs):
        item = self._item(dict(record), kwargs.get('partition_key'))
        self.container.create_item(item)
        return item['id']

    def pending(self, limit=100, config=None, **kwargs):
        query = ('SELECT TOP @n c.id, c.record FROM c WHERE c.e2a_outbox = @name '
                 'ORDER BY c.created_at ASC')
        items = self.container.query_items(query, parameters=[{'name': '@n', 'value': max(1, int(limit))},
                                                              {'name': '@name', 'value': self.name}],
                                           enable_cross_partition_query=True)
        return [{'id': it['id'], **json.loads(it['record'])} for it in items]

    def _find(self, record_id: str) -> Optional[Dict[str, Any]]:
        items = list(self.container.query_items(
            'SELECT * FROM c WHERE c.e2a_outbox = @name AND c.id = @id',
            parameters=[{'name': '@name', 'value': self.name}, {'name': '@id', 'value': record_id}],
            enable_cross_partition_query=True))
        return items[0] if items else None

    def _advance_fence(self, token: int) -> bool:
        while True:
            try:
                fence = self.container.read_item(self.name, partition_key=_FENCE_PK)
            except cx.CosmosResourceNotFoundError:
                try:
                    self.container.create_item({'id': self.name, self.pk: _FENCE_PK, 'fence': int(token)})
                    return True
                except cx.CosmosResourceExistsError:
                    continue                                 # another sweeper created it: read again
            if int(fence['fence']) > int(token):
                return False
            if int(fence['fence']) == int(token):
                return True
            try:
                self.container.replace_item(self.name, {**{k: v for k, v in fence.items() if not k.startswith('_')},
                                                        'fence': int(token)},
                                            etag=fence['_etag'], match_condition=MatchConditions.IfNotModified)
                return True
            except cx.CosmosAccessConditionFailedError:
                continue                                     # moved meanwhile: read again

    def mark_published(self, record_id, config=None, **kwargs):
        token = kwargs.get('fencing_token')
        if token is not None and not self._advance_fence(int(token)):
            return False
        item = self._find(record_id)
        if item is None:
            return False
        try:
            self.container.delete_item(item['id'], partition_key=item[self.pk], etag=item['_etag'],
                                       match_condition=MatchConditions.IfNotModified)
            return True
        except (cx.CosmosResourceNotFoundError, cx.CosmosAccessConditionFailedError):
            return False
