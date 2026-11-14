"""DynamoDB adapters for E2A's foundation ports (design note D-02).

    DynamoDBIdempotencyStore   BaseIdempotencyStore   atomic claim, replay, lease, recovery
    DynamoDBTransactionalStore BaseTransactionalStore compare-and-set with fencing
    DynamoDBLease              BaseDistributedLock    exclusive lease, strictly increasing fence
    DynamoDBCheckpointStore    BaseCheckpointStore    decide exactly once

Every state change is ONE DynamoDB write with a condition expression; there is no read
followed by a write. A lost condition (``ConditionalCheckFailedException``) is the only error
read as "another writer got there first"; any other error (throttling, validation, access) is
raised, never turned into IN_PROGRESS or CONFLICT. After a lost condition the adapter reads the
item with a strongly consistent read to say WHY (replay, conflict, fenced, ...).

The adapters share the application's table: any table whose partition and sort keys are
strings. Their items live under their own partitions (``E2A#IDEM#...``, ``E2A#ROW#...``,
``E2A#LEASE#...``, ``E2A#CKPT#...``), one partition per key, so E2A's traffic never forms a hot
partition. Times are wall-clock epoch seconds (several hosts compare them); correctness uses
explicit fields, and the ``ttl`` attribute is only DynamoDB's garbage collection.

    import boto3
    from e2a_adapters.aws import DynamoDBIdempotencyStore
    store = DynamoDBIdempotencyStore(boto3.resource('dynamodb').Table('my-table'))
"""
import json
import time
import uuid
from typing import Any, Callable, Dict, Optional

from botocore.exceptions import ClientError

from e2a import (BaseCheckpointStore, BaseDistributedLock, BaseIdempotencyStore, BaseTransactionalStore,
                 ClaimResult, ClaimStatus, CommitResult, LeaseHandle, PhysicalOutcome)

__all__ = ['DynamoDBIdempotencyStore', 'DynamoDBTransactionalStore', 'DynamoDBLease', 'DynamoDBCheckpointStore',
           'MAX_VALUE_BYTES', 'is_condition_failure']

MAX_VALUE_BYTES = 350_000          # DynamoDB's item limit is 400 KB; keep room for the other attributes
_MAX_ATTEMPTS = 8                  # re-evaluations after a lost condition before giving up (a live-lock guard)


def is_condition_failure(error: ClientError) -> bool:
    """True only for a lost condition: another writer got there first."""
    return error.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException'


def _s(value: str) -> Dict[str, str]:
    return {'S': str(value)}


def _n(value) -> Dict[str, str]:
    return {'N': repr(float(value)) if isinstance(value, float) else str(value)}


def _num(item: Dict, name: str) -> Optional[float]:
    return float(item[name]['N']) if name in item else None


def _str(item: Dict, name: str) -> Optional[str]:
    return item[name]['S'] if name in item else None


def _dump(value: Any) -> str:
    text = json.dumps(value, separators=(',', ':'), ensure_ascii=False)
    if len(text.encode('utf-8')) > MAX_VALUE_BYTES:
        raise ValueError(f'value of {len(text.encode("utf-8"))} bytes exceeds {MAX_VALUE_BYTES} '
                         '(DynamoDB items are at most 400 KB); store it elsewhere and keep a reference')
    return text


class _DynamoDB:
    """Shared plumbing: the client, the table and the key layout."""

    def __init__(self, table=None, *, client=None, table_name: str = None, prefix: str = 'E2A#',
                 partition_key: str = 'pk', sort_key: str = 'sk', ttl_attribute: str = 'ttl',
                 clock: Callable[[], float] = time.time):
        if table is not None:
            # a boto3 Table resource. Its meta.client converts Python values itself, so the adapter
            # builds its own low-level client (thread-safe) for the same region and endpoint.
            import boto3
            meta = table.meta.client.meta
            client = boto3.client('dynamodb', region_name=meta.region_name, endpoint_url=meta.endpoint_url)
            table_name = table.name
        if client is None or not table_name:
            raise ValueError('give a boto3 Table, or client= and table_name=')
        self.client, self.table_name, self.prefix = client, table_name, prefix
        self.pk, self.sk, self.ttl_attribute, self.clock = partition_key, sort_key, ttl_attribute, clock

    _table_key = 'dynamodb_state_table'      # the config key naming this adapter's table (v1.7.0)

    @classmethod
    def from_config(cls, config: Dict[str, Any], **kwargs):
        """v1.7.0: the adapter from configuration (a cloud profile): region, endpoint and table
        name are registered keys. The client is created on first use, so binding and verifying
        a profile needs no credentials."""
        import boto3
        from e2a import resolve_config
        from .. import config_keys  # noqa: F401
        from .._lazy import Lazy
        region, endpoint = resolve_config('aws_region', config), resolve_config('aws_endpoint_url', config)
        client = Lazy(lambda: boto3.client('dynamodb', region_name=region, endpoint_url=endpoint))
        return cls(client=client, table_name=resolve_config(cls._table_key, config), **kwargs)

    def _key(self, kind: str, *parts: str) -> Dict[str, Dict[str, str]]:
        return {self.pk: _s(self.prefix + kind + '#' + '#'.join(str(p) for p in parts)), self.sk: _s(kind.lower())}

    def _read(self, key) -> Optional[Dict]:
        return self.client.get_item(TableName=self.table_name, Key=key, ConsistentRead=True).get('Item')

    def _update(self, key, update: str, condition: str, names: Dict, values: Dict, **extra) -> Optional[Dict]:
        """One conditional update. Returns the response, or None when the condition was lost."""
        try:
            return self.client.update_item(TableName=self.table_name, Key=key, UpdateExpression=update,
                                           ConditionExpression=condition, ExpressionAttributeNames=names,
                                           ExpressionAttributeValues=values, **extra)
        except ClientError as e:
            if is_condition_failure(e):
                return None
            raise


# ------------------------------------------------------------ idempotency
class DynamoDBIdempotencyStore(_DynamoDB, BaseIdempotencyStore):
    """The claim state machine of BaseIdempotencyStore on one item per (tenant, key):
    ABSENT -> IN_PROGRESS(owner, lease_until) -> SUCCEEDED | FAILED_RETRYABLE | FAILED_FINAL | UNKNOWN."""
    capabilities = frozenset({'idempotency_store'})
    _table_key = 'dynamodb_idempotency_table'

    def _k(self, idempotency_key: str, kw: Dict):
        return self._key('IDEM', kw.get('tenant_id') or '-', idempotency_key)

    def claim(self, idempotency_key, config=None, **kw) -> ClaimResult:
        key = self._k(idempotency_key, kw)
        owner = kw.get('owner') or str(uuid.uuid4())
        lease, ttl, fp = float(kw.get('lease_seconds') or 300), kw.get('ttl_seconds'), kw.get('fingerprint')
        for _ in range(_MAX_ATTEMPTS):
            now = self.clock()
            sets = ['#st = :inprog', '#ow = :owner', 'lease_until = :lease']
            values = {':inprog': _s('IN_PROGRESS'), ':owner': _s(owner), ':lease': _n(now + lease),
                      ':retry': _s('FAILED_RETRYABLE'), ':now': _n(now)}
            removes = ['#res']
            if fp is not None:
                sets.append('fp = :fp')
                values[':fp'] = _s(fp)
            else:
                removes.append('fp')
            if ttl:
                sets += ['expires_at = :exp', '#ttl = :ttl']
                values[':exp'], values[':ttl'] = _n(now + float(ttl)), _n(int(now + float(ttl)) + 1)
            else:
                removes += ['expires_at', '#ttl']           # never leave an old TTL on a live claim
            # ABSENT, FAILED_RETRYABLE, or past its explicit expiry -> IN_PROGRESS, in one write
            done = self._update(
                key, 'SET ' + ', '.join(sets) + ' REMOVE ' + ', '.join(removes),
                'attribute_not_exists(#pk) OR #st = :retry OR (attribute_exists(expires_at) AND expires_at <= :now)',
                {'#pk': self.pk, '#st': 'state', '#ow': 'owner', '#res': 'result', '#ttl': self.ttl_attribute},
                values)
            if done is not None:
                return ClaimResult(ClaimStatus.CLAIMED, owner=owner)

            item = self._read(key)
            if item is None:
                continue                                   # deleted in between: claim again
            state, now = _str(item, 'state'), self.clock()
            expires = _num(item, 'expires_at')
            if state == 'FAILED_RETRYABLE' or (expires is not None and now >= expires):
                continue                                   # claimable after all: the write decides
            if fp is not None and _str(item, 'fp') is not None and fp != _str(item, 'fp'):
                return ClaimResult(ClaimStatus.CONFLICT)
            if state == 'SUCCEEDED':
                return ClaimResult(ClaimStatus.REPLAY, result=json.loads(_str(item, 'result') or 'null'))
            if state == 'FAILED_FINAL':
                return ClaimResult(ClaimStatus.FAILED_FINAL)
            expired = state == 'IN_PROGRESS' and now >= (_num(item, 'lease_until') or 0)
            if state == 'UNKNOWN' or expired:
                if not kw.get('recover'):
                    return ClaimResult(ClaimStatus.RECOVERY_REQUIRED)
                # a reconciler takes over, only from exactly the record it saw
                taken = self._update(key, 'SET #ow = :owner, #st = :inprog, lease_until = :lease',
                                     '#st = :seen AND #ow = :seen_owner',
                                     {'#st': 'state', '#ow': 'owner'},
                                     {':owner': _s(owner), ':inprog': _s('IN_PROGRESS'), ':lease': _n(now + lease),
                                      ':seen': _s(state), ':seen_owner': item['owner']})
                if taken is not None:
                    return ClaimResult(ClaimStatus.CLAIMED, owner=owner)
                continue                                   # someone else moved it: look again
            return ClaimResult(ClaimStatus.IN_PROGRESS, owner=_str(item, 'owner'))
        raise RuntimeError(f'claim of {idempotency_key!r} kept losing to concurrent writers')

    def _settle(self, idempotency_key, kw, state: str, result=None) -> bool:
        sets, values = ['#st = :new'], {':new': _s(state), ':inprog': _s('IN_PROGRESS'),
                                         ':owner': _s(kw.get('owner') or '')}
        if result is not None:
            sets.append('#res = :res')
            values[':res'] = _s(_dump(result))
        names = {'#st': 'state', '#ow': 'owner', **({'#res': 'result'} if result is not None else {})}
        return self._update(self._k(idempotency_key, kw), 'SET ' + ', '.join(sets),
                            '#st = :inprog AND #ow = :owner', names, values) is not None

    def complete(self, idempotency_key, result, config=None, **kw) -> bool:
        """IN_PROGRESS -> SUCCEEDED, only for the current owner."""
        return self._settle(idempotency_key, kw, 'SUCCEEDED', result if result is not None else {})

    def fail(self, idempotency_key, retryable=True, config=None, **kw) -> bool:
        outcome = kw.get('outcome')
        unknown = outcome == PhysicalOutcome.UNKNOWN or outcome == 'UNKNOWN'
        return self._settle(idempotency_key, kw,
                            'UNKNOWN' if unknown else ('FAILED_RETRYABLE' if retryable else 'FAILED_FINAL'))

    def state_of(self, key, tenant_id=None) -> Optional[str]:
        item = self._read(self._k(key, {'tenant_id': tenant_id}))
        return _str(item, 'state') if item else None


# ----------------------------------------------------------- transactional
class DynamoDBTransactionalStore(_DynamoDB, BaseTransactionalStore):
    """Versioned JSON rows; put_if_version() is one conditional update. A fencing token is
    recorded per row and a write carrying an older one is FENCED."""
    capabilities = frozenset({'transactional_store'})

    def _k(self, key: str, kw: Dict):
        return self._key('ROW', kw.get('table_or_collection_name') or 'default', key)

    def get(self, key, config=None, **kw):
        found = self.get_versioned(key, config, **kw)
        return found[0] if found else None

    def get_versioned(self, key, config=None, **kw):
        item = self._read(self._k(key, kw))
        if not item or 'version' not in item:
            return None
        return json.loads(_str(item, 'value')), int(_num(item, 'version'))

    def put(self, key, value, config=None, **kw) -> bool:
        """Unconditional write (a single writer by construction); the version still advances."""
        self.client.update_item(TableName=self.table_name, Key=self._k(key, kw),
                                UpdateExpression='SET #v = :v ADD version :one',
                                ExpressionAttributeNames={'#v': 'value'},
                                ExpressionAttributeValues={':v': _s(_dump(value)), ':one': _n(1)})
        return True

    def delete(self, key, config=None, **kw) -> bool:
        old = self.client.delete_item(TableName=self.table_name, Key=self._k(key, kw), ReturnValues='ALL_OLD')
        return bool(old.get('Attributes'))

    def put_if_version(self, key, value, expected_version, config=None, **kw) -> CommitResult:
        token = kw.get('fencing_token')
        new_version = (expected_version or 0) + 1
        sets, values = ['#v = :v', 'version = :new'], {':v': _s(_dump(value)), ':new': _n(new_version)}
        names = {'#v': 'value'}
        if expected_version is None:
            condition = 'attribute_not_exists(version)'
        else:
            condition, values[':exp'] = 'version = :exp', _n(expected_version)
        if token is not None:
            condition = f'({condition}) AND (attribute_not_exists(fence) OR fence <= :tok)'
            sets.append('fence = :tok')
            values[':tok'] = _n(int(token))
        k = self._k(key, kw)
        if self._update(k, 'SET ' + ', '.join(sets), condition, names, values) is not None:
            return CommitResult('COMMITTED', version=new_version)
        item = self._read(k) or {}
        current = int(_num(item, 'version')) if 'version' in item else None
        fence = _num(item, 'fence')
        if token is not None and fence is not None and int(token) < fence:
            return CommitResult('FENCED', current_version=current)
        return CommitResult('CONFLICT', current_version=current)


# ------------------------------------------------------------------- lease
class DynamoDBLease(_DynamoDB, BaseDistributedLock):
    """One item per lock key. Release clears the owner but keeps the item, so the fencing token
    (ADD fence 1 on every acquire) is strictly increasing for the key's whole life."""
    capabilities = frozenset({'lease'})

    def _k(self, lock_key: str):
        return self._key('LEASE', lock_key)

    def acquire(self, lock_key, ttl_seconds, config=None, **kw) -> Optional[LeaseHandle]:
        owner, now = str(uuid.uuid4()), self.clock()
        expires = now + float(ttl_seconds)
        done = self._update(self._k(lock_key), 'SET #ow = :owner, expires_at = :exp ADD fence :one',
                            'attribute_not_exists(#ow) OR expires_at <= :now', {'#ow': 'owner'},
                            {':owner': _s(owner), ':exp': _n(expires), ':one': _n(1), ':now': _n(now)},
                            ReturnValues='ALL_NEW')
        if done is None:
            return None
        return LeaseHandle(lock_key, owner, int(_num(done['Attributes'], 'fence')), expires)

    def renew(self, handle, ttl_seconds=None, config=None, **kw) -> bool:
        now = self.clock()
        expires = now + float(ttl_seconds or 60)
        done = self._update(self._k(handle.key), 'SET expires_at = :exp', '#ow = :owner AND expires_at > :now',
                            {'#ow': 'owner'}, {':owner': _s(handle.owner_token), ':exp': _n(expires), ':now': _n(now)})
        if done is None:
            return False
        handle.expires_at = expires
        return True

    def release(self, handle, config=None, **kw) -> None:
        self._update(self._k(handle.key), 'REMOVE #ow, expires_at', '#ow = :owner', {'#ow': 'owner'},
                     {':owner': _s(handle.owner_token)})


# -------------------------------------------------------------- checkpoint
class DynamoDBCheckpointStore(_DynamoDB, BaseCheckpointStore):
    """A paused workflow's checkpoint as JSON, with its status as a separate attribute so that
    decide() is one conditional update (PENDING -> APPROVED | REJECTED)."""
    capabilities = frozenset({'checkpoint_store'})
    _table_key = 'dynamodb_checkpoint_table'

    def __init__(self, *args, ttl_seconds: Optional[int] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ttl_seconds = ttl_seconds          # garbage collection of decided checkpoints, if wanted

    def _k(self, checkpoint_id: str):
        return self._key('CKPT', checkpoint_id)

    def save(self, checkpoint, config=None, **kw):
        item = {**self._k(checkpoint['checkpoint_id']), 'data': _s(_dump(checkpoint)),
                'status': _s(checkpoint.get('status', 'PENDING')), 'tenant_id': _s(checkpoint.get('tenant_id') or '-')}
        if self.ttl_seconds:
            item[self.ttl_attribute] = _n(int(self.clock() + self.ttl_seconds))
        self.client.put_item(TableName=self.table_name, Item=item)

    def load(self, checkpoint_id, config=None, **kw):
        item = self._read(self._k(checkpoint_id))
        if not item:
            return None
        cp = json.loads(_str(item, 'data'))
        if kw.get('tenant_id') and cp.get('tenant_id') != kw['tenant_id']:
            return None
        cp['status'] = _str(item, 'status')
        if 'decided_by' in item:
            cp['decided_by'], cp['decided_at'] = _str(item, 'decided_by'), _num(item, 'decided_at')
        return cp

    def decide(self, checkpoint_id, status, decided_by, config=None, **kw) -> bool:
        return self._update(self._k(checkpoint_id), 'SET #st = :new, decided_by = :by, decided_at = :at',
                            '#st = :pending', {'#st': 'status'},
                            {':new': _s(status), ':by': _s(decided_by), ':at': _n(self.clock()),
                             ':pending': _s('PENDING')}) is not None
