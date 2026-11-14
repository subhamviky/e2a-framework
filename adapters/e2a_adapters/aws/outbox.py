"""The transactional outbox on DynamoDB (v1.7.1, design note MP-05, N1).

A record is written in the SAME transaction as the business change: put transact_item(record) in
the application's TransactWriteItems (or call add() when there is no other write). The
sweeper (e2a.BrokerOutboxSweeper) reads pending() and publishes, and mark_published() deletes
the record in one transaction with the fencing check, so a stale sweeper cannot mark.

Layout, in any table with string keys pk / sk (e2a-adapters' state table by default):

    pk = E2A#OUTBOX#<outbox_name>   sk = <record id>        a pending record
    pk = E2A#OUTBOX-FENCE           sk = <outbox_name>      the newest fencing token seen

Record ids sort by creation time (microseconds, then a random suffix), so pending() is one
Query in key order. All pending records share a partition: one outbox handles what one
partition writes (about 1,000 records a second); use several outbox names to go beyond that.
"""
import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from botocore.exceptions import ClientError
from e2a import resolve_config
from e2a.messaging import BaseOutboxStore

from .. import config_keys  # noqa: F401
from .._lazy import Lazy


def _s(value: str) -> Dict[str, str]:
    return {'S': str(value)}


class DynamoDBOutboxStore(BaseOutboxStore):
    capabilities = frozenset({'outbox_store'})

    def __init__(self, client=None, table_name: str = None, name: str = 'default',
                 clock: Callable[[], float] = time.time) -> None:
        if client is None or not table_name:
            raise ValueError('DynamoDBOutboxStore needs client= and table_name= (or use from_config)')
        self.client, self.table_name, self.name, self.clock = client, table_name, name, clock
        self.pk = f'E2A#OUTBOX#{name}'

    @classmethod
    def from_config(cls, config: Dict[str, Any], **kwargs) -> 'DynamoDBOutboxStore':
        import boto3
        region, endpoint = resolve_config('aws_region', config), resolve_config('aws_endpoint_url', config)
        return cls(Lazy(lambda: boto3.client('dynamodb', region_name=region, endpoint_url=endpoint)),
                   resolve_config('dynamodb_state_table', config), resolve_config('outbox_name', config), **kwargs)

    def _new_id(self) -> str:
        return f'{int(self.clock() * 1_000_000):017d}-{uuid.uuid4().hex[:12]}'

    def transact_item(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """The Put for the application's own TransactWriteItems: the record is committed if and
        only if the business change is. record['id'] is set when absent."""
        record.setdefault('id', self._new_id())
        payload = {k: record.get(k) for k in ('destination', 'body', 'attributes', 'tenant_id', 'idempotency_key')}
        return {'Put': {'TableName': self.table_name,
                        'Item': {'pk': _s(self.pk), 'sk': _s(record['id']), 'created_at': {'N': repr(self.clock())},
                                 'record': _s(json.dumps(payload, separators=(',', ':'), ensure_ascii=False))},
                        'ConditionExpression': 'attribute_not_exists(pk)'}}

    def add(self, record, config=None, **kwargs):
        item = self.transact_item(dict(record))
        self.client.put_item(**item['Put'])
        return item['Put']['Item']['sk']['S']

    def pending(self, limit=100, config=None, **kwargs):
        resp = self.client.query(TableName=self.table_name, KeyConditionExpression='pk = :pk',
                                 ExpressionAttributeValues={':pk': _s(self.pk)}, ConsistentRead=True,
                                 Limit=max(1, int(limit)))
        out: List[Dict[str, Any]] = []
        for item in resp.get('Items', []):
            out.append({'id': item['sk']['S'], **json.loads(item['record']['S'])})
        return out

    def mark_published(self, record_id, config=None, **kwargs):
        delete = {'Delete': {'TableName': self.table_name, 'Key': {'pk': _s(self.pk), 'sk': _s(record_id)},
                             'ConditionExpression': 'attribute_exists(pk)'}}
        token: Optional[int] = kwargs.get('fencing_token')
        try:
            if token is None:
                self.client.delete_item(**delete['Delete'])
            else:
                fence = {'Update': {'TableName': self.table_name,
                                    'Key': {'pk': _s('E2A#OUTBOX-FENCE'), 'sk': _s(self.name)},
                                    'UpdateExpression': 'SET fence = :t',
                                    'ConditionExpression': 'attribute_not_exists(fence) OR fence <= :t',
                                    'ExpressionAttributeValues': {':t': {'N': str(int(token))}}}}
                self.client.transact_write_items(TransactItems=[fence, delete])
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code')
            if code == 'ConditionalCheckFailedException':
                return False
            if code == 'TransactionCanceledException':
                # a lost condition (stale fence, already published) or a concurrent transaction
                # on the same items is a lost race; throttling or validation is an error
                reasons = [r.get('Code') for r in e.response.get('CancellationReasons') or []]
                if all(r in (None, 'None', 'ConditionalCheckFailed', 'TransactionConflict') for r in reasons):
                    return False
            raise
        return True
