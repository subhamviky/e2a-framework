"""The E2A ports over any document store with optimistic concurrency (design note D-03).

Firestore (a document's ``update_time``) and Cosmos DB (an item's ETag) offer the same
primitive: read a document with a version token, then write it only if the token is unchanged.
So the four ports are written ONCE here, over a ``CasBackend``, and each database supplies a
thin backend. Every state change is a create-if-absent or a replace-if-unchanged; a lost race
makes the port read again and re-decide, never overwrite.

    CasIdempotencyStore   BaseIdempotencyStore
    CasTransactionalStore BaseTransactionalStore
    CasLease              BaseDistributedLock
    CasCheckpointStore    BaseCheckpointStore

A backend stores flat documents of JSON scalars (str, int, float) under a string key; values
and results are JSON strings inside them, so they round-trip exactly.
"""
import json
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional, Tuple

from e2a import (BaseCheckpointStore, BaseDistributedLock, BaseIdempotencyStore, BaseTransactionalStore,
                 ClaimResult, ClaimStatus, CommitResult, LeaseHandle, PhysicalOutcome)

__all__ = ['CasBackend', 'InMemoryCasBackend', 'CasIdempotencyStore', 'CasTransactionalStore', 'CasLease',
           'CasCheckpointStore']

_MAX_ATTEMPTS = 16                 # re-reads after lost races before giving up (a live-lock guard)


class CasBackend(ABC):
    """A key -> document store with optimistic concurrency. Only a LOST RACE returns False;
    every other error (permissions, quota, a missing database) is raised."""

    max_value_bytes = 900_000

    @abstractmethod
    def get(self, key: str) -> Optional[Tuple[Dict[str, Any], Any]]:
        """(document, version token), or None. A strongly consistent read."""

    @abstractmethod
    def create(self, key: str, doc: Dict[str, Any], ttl_at: Optional[float] = None) -> bool:
        """Write only if absent; False if the key exists."""

    @abstractmethod
    def replace(self, key: str, doc: Dict[str, Any], token: Any, ttl_at: Optional[float] = None) -> bool:
        """Replace the whole document only if it is still at ``token``; False otherwise (changed
        or deleted). Fields not in ``doc`` are removed; ``ttl_at`` None removes the TTL."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete; False if there was nothing to delete."""

    def dump(self, value: Any) -> str:
        text = json.dumps(value, separators=(',', ':'), ensure_ascii=False)
        size = len(text.encode('utf-8'))
        if size > self.max_value_bytes:
            raise ValueError(f'value of {size} bytes exceeds {self.max_value_bytes} for {type(self).__name__}; '
                             'store it elsewhere and keep a reference')
        return text


class InMemoryCasBackend(CasBackend):
    """The CasBackend contract in memory: for the engine's own tests and as a reference."""

    def __init__(self):
        self._lock, self._docs = threading.Lock(), {}

    def get(self, key):
        with self._lock:
            found = self._docs.get(key)
            return (dict(found[0]), found[1]) if found else None

    def create(self, key, doc, ttl_at=None):
        with self._lock:
            if key in self._docs:
                return False
            self._docs[key] = (dict(doc), uuid.uuid4().hex)
            return True

    def replace(self, key, doc, token, ttl_at=None):
        with self._lock:
            if key not in self._docs or self._docs[key][1] != token:
                return False
            self._docs[key] = (dict(doc), uuid.uuid4().hex)
            return True

    def delete(self, key):
        with self._lock:
            return self._docs.pop(key, None) is not None


def _write(backend: CasBackend, key: str, current, doc: Dict, ttl_at: Optional[float] = None) -> bool:
    """Create when there was no document, else replace at the token that was read."""
    return backend.create(key, doc, ttl_at) if current is None else backend.replace(key, doc, current[1], ttl_at)


def _exhausted(what: str):
    return RuntimeError(f'{what} kept losing to concurrent writers')


# ------------------------------------------------------------ idempotency
class CasIdempotencyStore(BaseIdempotencyStore):
    """BaseIdempotencyStore's state machine on one document per (tenant, key)."""

    def __init__(self, backend: CasBackend, clock: Callable[[], float] = time.time):
        self.backend, self.clock = backend, clock

    @staticmethod
    def _k(key: str, kw: Dict) -> str:
        return f"IDEM#{kw.get('tenant_id') or '-'}#{key}"

    def claim(self, idempotency_key, config=None, **kw) -> ClaimResult:
        k, b = self._k(idempotency_key, kw), self.backend
        owner = kw.get('owner') or str(uuid.uuid4())
        lease, ttl, fp = float(kw.get('lease_seconds') or 300), kw.get('ttl_seconds'), kw.get('fingerprint')
        for _ in range(_MAX_ATTEMPTS):
            now = self.clock()
            fresh = {'state': 'IN_PROGRESS', 'owner': owner, 'lease_until': now + lease}
            if fp is not None:
                fresh['fp'] = fp
            ttl_at = (now + float(ttl)) if ttl else None
            if ttl_at is not None:
                fresh['expires_at'] = ttl_at
            current = b.get(k)
            r = current[0] if current else None
            if r is None or r.get('state') == 'FAILED_RETRYABLE' or (
                    r.get('expires_at') is not None and now >= r['expires_at']):
                if _write(b, k, current, fresh, ttl_at):
                    return ClaimResult(ClaimStatus.CLAIMED, owner=owner)
                continue
            if fp is not None and r.get('fp') is not None and fp != r['fp']:
                return ClaimResult(ClaimStatus.CONFLICT)
            state = r.get('state')
            if state == 'SUCCEEDED':
                return ClaimResult(ClaimStatus.REPLAY, result=json.loads(r.get('result') or 'null'))
            if state == 'FAILED_FINAL':
                return ClaimResult(ClaimStatus.FAILED_FINAL)
            expired = state == 'IN_PROGRESS' and now >= float(r.get('lease_until') or 0)
            if state == 'UNKNOWN' or expired:
                if not kw.get('recover'):
                    return ClaimResult(ClaimStatus.RECOVERY_REQUIRED)
                taken = dict(r, state='IN_PROGRESS', owner=owner, lease_until=now + lease)
                if b.replace(k, taken, current[1], r.get('expires_at')):
                    return ClaimResult(ClaimStatus.CLAIMED, owner=owner)
                continue
            return ClaimResult(ClaimStatus.IN_PROGRESS, owner=r.get('owner'))
        raise _exhausted(f'claim of {idempotency_key!r}')

    def _settle(self, idempotency_key, kw, state: str, result=None) -> bool:
        k, b = self._k(idempotency_key, kw), self.backend
        stored = b.dump(result) if result is not None else None
        for _ in range(_MAX_ATTEMPTS):
            current = b.get(k)
            r = current[0] if current else None
            if r is None or r.get('state') != 'IN_PROGRESS' or r.get('owner') != kw.get('owner'):
                return False                      # a stale owner: its lease was taken over
            new = dict(r, state=state)
            if stored is not None:
                new['result'] = stored
            if b.replace(k, new, current[1], r.get('expires_at')):
                return True
        raise _exhausted(f'settling {idempotency_key!r}')

    def complete(self, idempotency_key, result, config=None, **kw) -> bool:
        return self._settle(idempotency_key, kw, 'SUCCEEDED', result if result is not None else {})

    def fail(self, idempotency_key, retryable=True, config=None, **kw) -> bool:
        outcome = kw.get('outcome')
        unknown = outcome == PhysicalOutcome.UNKNOWN or outcome == 'UNKNOWN'
        return self._settle(idempotency_key, kw,
                            'UNKNOWN' if unknown else ('FAILED_RETRYABLE' if retryable else 'FAILED_FINAL'))

    def state_of(self, key, tenant_id=None) -> Optional[str]:
        current = self.backend.get(self._k(key, {'tenant_id': tenant_id}))
        return current[0].get('state') if current else None


# ----------------------------------------------------------- transactional
class CasTransactionalStore(BaseTransactionalStore):
    """Versioned JSON rows; put_if_version() is a replace-if-unchanged after checking the
    version and the fence that were read."""

    def __init__(self, backend: CasBackend, clock: Callable[[], float] = time.time):
        self.backend, self.clock = backend, clock

    @staticmethod
    def _k(key: str, kw: Dict) -> str:
        return f"ROW#{kw.get('table_or_collection_name') or 'default'}#{key}"

    def get(self, key, config=None, **kw):
        found = self.get_versioned(key, config, **kw)
        return found[0] if found else None

    def get_versioned(self, key, config=None, **kw):
        current = self.backend.get(self._k(key, kw))
        if not current:
            return None
        return json.loads(current[0]['value']), int(current[0]['version'])

    def put(self, key, value, config=None, **kw) -> bool:
        k, b, text = self._k(key, kw), self.backend, self.backend.dump(value)
        for _ in range(_MAX_ATTEMPTS):
            current = b.get(k)
            doc = dict(current[0]) if current else {}
            doc.update(value=text, version=int(doc.get('version') or 0) + 1)
            if _write(b, k, current, doc):
                return True
        raise _exhausted(f'put of {key!r}')

    def delete(self, key, config=None, **kw) -> bool:
        return self.backend.delete(self._k(key, kw))

    def put_if_version(self, key, value, expected_version, config=None, **kw) -> CommitResult:
        k, b, text = self._k(key, kw), self.backend, self.backend.dump(value)
        token = kw.get('fencing_token')
        for _ in range(_MAX_ATTEMPTS):
            current = b.get(k)
            r = current[0] if current else None
            version = int(r['version']) if r else None
            fence = r.get('fence') if r else None
            if token is not None and fence is not None and int(token) < int(fence):
                return CommitResult('FENCED', current_version=version)
            if version != expected_version:
                return CommitResult('CONFLICT', current_version=version)
            doc = {'value': text, 'version': (expected_version or 0) + 1}
            if token is not None or fence is not None:
                doc['fence'] = int(token) if token is not None else int(fence)
            if _write(b, k, current, doc):
                return CommitResult('COMMITTED', version=doc['version'])
        raise _exhausted(f'commit of {key!r}')


# ------------------------------------------------------------------- lease
class CasLease(BaseDistributedLock):
    """One document per lock key. Release clears the owner and keeps the document, so the
    fencing token is strictly increasing for the key's whole life."""

    def __init__(self, backend: CasBackend, clock: Callable[[], float] = time.time):
        self.backend, self.clock = backend, clock

    @staticmethod
    def _k(key: str) -> str:
        return f'LEASE#{key}'

    def acquire(self, lock_key, ttl_seconds, config=None, **kw) -> Optional[LeaseHandle]:
        k, b = self._k(lock_key), self.backend
        for _ in range(_MAX_ATTEMPTS):
            now = self.clock()
            current = b.get(k)
            r = current[0] if current else {}
            if r.get('owner') and float(r.get('expires_at') or 0) > now:
                return None
            owner, expires = str(uuid.uuid4()), now + float(ttl_seconds)
            fence = int(r.get('fence') or 0) + 1
            if _write(b, k, current, {'owner': owner, 'expires_at': expires, 'fence': fence}):
                return LeaseHandle(lock_key, owner, fence, expires)
        raise _exhausted(f'acquire of {lock_key!r}')

    def renew(self, handle, ttl_seconds=None, config=None, **kw) -> bool:
        k, b = self._k(handle.key), self.backend
        for _ in range(_MAX_ATTEMPTS):
            now = self.clock()
            current = b.get(k)
            r = current[0] if current else {}
            if r.get('owner') != handle.owner_token or float(r.get('expires_at') or 0) <= now:
                return False
            expires = now + float(ttl_seconds or 60)
            if b.replace(k, dict(r, expires_at=expires), current[1]):
                handle.expires_at = expires
                return True
        raise _exhausted(f'renew of {handle.key!r}')

    def release(self, handle, config=None, **kw) -> None:
        k, b = self._k(handle.key), self.backend
        for _ in range(_MAX_ATTEMPTS):
            current = b.get(k)
            if not current or current[0].get('owner') != handle.owner_token:
                return
            if b.replace(k, {'fence': int(current[0]['fence'])}, current[1]):
                return


# -------------------------------------------------------------- checkpoint
class CasCheckpointStore(BaseCheckpointStore):
    """A paused workflow's checkpoint as JSON, with its status beside it, so that decide() is
    one replace-if-unchanged (PENDING -> APPROVED | REJECTED)."""

    def __init__(self, backend: CasBackend, clock: Callable[[], float] = time.time,
                 ttl_seconds: Optional[int] = None):
        self.backend, self.clock, self.ttl_seconds = backend, clock, ttl_seconds

    @staticmethod
    def _k(checkpoint_id: str) -> str:
        return f'CKPT#{checkpoint_id}'

    def _ttl_at(self):
        return (self.clock() + self.ttl_seconds) if self.ttl_seconds else None

    def save(self, checkpoint, config=None, **kw):
        k, b = self._k(checkpoint['checkpoint_id']), self.backend
        doc = {'data': b.dump(checkpoint), 'status': checkpoint.get('status', 'PENDING'),
               'tenant_id': checkpoint.get('tenant_id') or '-'}
        for _ in range(_MAX_ATTEMPTS):
            if _write(b, k, b.get(k), doc, self._ttl_at()):
                return
        raise _exhausted(f"save of checkpoint {checkpoint['checkpoint_id']!r}")

    def load(self, checkpoint_id, config=None, **kw):
        current = self.backend.get(self._k(checkpoint_id))
        if not current:
            return None
        r = current[0]
        cp = json.loads(r['data'])
        if kw.get('tenant_id') and cp.get('tenant_id') != kw['tenant_id']:
            return None
        cp['status'] = r['status']
        if 'decided_by' in r:
            cp['decided_by'], cp['decided_at'] = r['decided_by'], r.get('decided_at')
        return cp

    def decide(self, checkpoint_id, status, decided_by, config=None, **kw) -> bool:
        k, b = self._k(checkpoint_id), self.backend
        for _ in range(_MAX_ATTEMPTS):
            current = b.get(k)
            if not current or current[0].get('status') != 'PENDING':
                return False
            new = dict(current[0], status=status, decided_by=decided_by, decided_at=self.clock())
            if b.replace(k, new, current[1], self._ttl_at()):
                return True
        raise _exhausted(f'decision on checkpoint {checkpoint_id!r}')
