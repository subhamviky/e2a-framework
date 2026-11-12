"""In-memory REFERENCE adapters for the E2A foundation ports.

These are test doubles / reference implementations of the port contracts
(atomic claim, conditional commit, fenced writes, leases, cache). They are
single-process and NOT for production: a real adapter binds the same
contracts to a managed store whose conditional-write / lease primitive
provides the atomicity (see the Multi-Cloud Reference).
"""
import threading
import time
import uuid
from typing import Any, Dict, Optional

from e2a import BaseCheckpointStore
from e2a_base import (BaseCacheStore, BaseDistributedLock, BaseIdempotencyStore,
                      BaseTransactionalStore, ClaimResult, ClaimStatus, CommitResult,
                      LeaseHandle, PhysicalOutcome)


class InMemoryIdempotencyStore(BaseIdempotencyStore):
    """State machine per (tenant, key): IN_PROGRESS -> SUCCEEDED |
    FAILED_RETRYABLE | FAILED_FINAL | UNKNOWN. claim() is one atomic step
    (a lock stands in for the store's conditional write)."""

    def __init__(self, clock=time.monotonic):
        self._lock, self._rec, self._clock = threading.Lock(), {}, clock

    @staticmethod
    def _k(key, kw):
        return f"{kw.get('tenant_id') or '-'}:{key}"

    def claim(self, idempotency_key, config=None, **kw) -> ClaimResult:
        k, now = self._k(idempotency_key, kw), self._clock()
        fp, owner = kw.get('fingerprint'), kw.get('owner') or str(uuid.uuid4())
        lease = float(kw.get('lease_seconds') or 300)
        with self._lock:
            r = self._rec.get(k)
            ttl = kw.get('ttl_seconds')
            if r and r.get('expires_at') and now >= r['expires_at']:
                r = None                      # explicit expiry field, not store-TTL timing
            if r is None or r['state'] == 'FAILED_RETRYABLE':
                self._rec[k] = {'state': 'IN_PROGRESS', 'owner': owner, 'lease_until': now + lease,
                                'fp': fp, 'result': None, 'expires_at': (now + ttl) if ttl else None}
                return ClaimResult(ClaimStatus.CLAIMED, owner=owner)
            if fp is not None and r['fp'] is not None and fp != r['fp']:
                return ClaimResult(ClaimStatus.CONFLICT)
            if r['state'] == 'SUCCEEDED':
                return ClaimResult(ClaimStatus.REPLAY, result=r['result'])
            if r['state'] == 'FAILED_FINAL':
                return ClaimResult(ClaimStatus.FAILED_FINAL)
            expired = r['state'] == 'IN_PROGRESS' and now >= r['lease_until']
            if r['state'] == 'UNKNOWN' or expired:
                if kw.get('recover'):         # a reconciler takes over, after which it confirms truth
                    r.update(state='IN_PROGRESS', owner=owner, lease_until=now + lease)
                    return ClaimResult(ClaimStatus.CLAIMED, owner=owner)
                return ClaimResult(ClaimStatus.RECOVERY_REQUIRED)
            return ClaimResult(ClaimStatus.IN_PROGRESS, owner=r['owner'])

    def _owned(self, k, kw):
        r = self._rec.get(k)
        return r if (r and r['state'] == 'IN_PROGRESS' and r['owner'] == kw.get('owner')) else None

    def complete(self, idempotency_key, result, config=None, **kw) -> bool:
        k = self._k(idempotency_key, kw)
        with self._lock:
            r = self._owned(k, kw)
            if not r:
                return False                  # stale owner: its lease was taken over
            r.update(state='SUCCEEDED', result=result)
            return True

    def fail(self, idempotency_key, retryable=True, config=None, **kw) -> bool:
        k = self._k(idempotency_key, kw)
        with self._lock:
            r = self._owned(k, kw)
            if not r:
                return False
            if kw.get('outcome') == PhysicalOutcome.UNKNOWN:
                r['state'] = 'UNKNOWN'
            else:
                r['state'] = 'FAILED_RETRYABLE' if retryable else 'FAILED_FINAL'
            return True

    def state_of(self, key, tenant_id=None) -> Optional[str]:
        r = self._rec.get(self._k(key, {'tenant_id': tenant_id}))
        return r['state'] if r else None


class InMemoryTransactionalStore(BaseTransactionalStore):
    """Versioned rows; put_if_version() is an atomic compare-and-set and
    rejects fenced (stale-lease) writes."""

    def __init__(self):
        self._lock, self._rows, self._fence = threading.Lock(), {}, {}
        self.conflicts = self.fenced = 0

    def get(self, key, config=None, **kw):
        r = self._rows.get(key)
        return r[0] if r else None

    def get_versioned(self, key, config=None, **kw):
        return self._rows.get(key)

    def put(self, key, value, config=None, **kw) -> bool:
        with self._lock:
            ver = (self._rows[key][1] + 1) if key in self._rows else 1
            self._rows[key] = (value, ver)
            return True

    def delete(self, key, config=None, **kw) -> bool:
        with self._lock:
            return self._rows.pop(key, None) is not None

    def put_if_version(self, key, value, expected_version, config=None, **kw) -> CommitResult:
        token = kw.get('fencing_token')
        with self._lock:
            if token is not None:
                if token < self._fence.get(key, 0):
                    self.fenced += 1
                    return CommitResult('FENCED', current_version=(self._rows.get(key) or (None, None))[1])
                self._fence[key] = token
            cur = self._rows.get(key)
            cur_ver = cur[1] if cur else None
            if cur_ver != expected_version:
                self.conflicts += 1
                return CommitResult('CONFLICT', current_version=cur_ver)
            ver = (cur_ver or 0) + 1
            self._rows[key] = (value, ver)
            return CommitResult('COMMITTED', version=ver)


class InMemoryCacheStore(BaseCacheStore):
    def __init__(self):
        self._d, self.gets = {}, 0

    def get(self, key, config=None, **kw):
        self.gets += 1
        return self._d.get(key)

    def set(self, key, value, ttl_seconds=None, config=None, **kw) -> bool:
        self._d[key] = value
        return True

    def delete(self, key, config=None, **kw) -> bool:
        return self._d.pop(key, None) is not None


class InMemoryLease(BaseDistributedLock):
    """Lease with owner token and a strictly increasing fencing token per key."""

    def __init__(self, clock=time.monotonic):
        self._lock, self._held, self._fence, self._clock = threading.Lock(), {}, {}, clock

    def acquire(self, lock_key, ttl_seconds, config=None, **kw) -> Optional[LeaseHandle]:
        now = self._clock()
        with self._lock:
            cur = self._held.get(lock_key)
            if cur and now < cur.expires_at:
                return None
            self._fence[lock_key] = self._fence.get(lock_key, 0) + 1
            h = LeaseHandle(lock_key, str(uuid.uuid4()), self._fence[lock_key], now + ttl_seconds)
            self._held[lock_key] = h
            return h

    def renew(self, handle, ttl_seconds=None, config=None, **kw) -> bool:
        now = self._clock()
        with self._lock:
            cur = self._held.get(handle.key)
            if not cur or cur.owner_token != handle.owner_token or now >= cur.expires_at:
                return False
            cur.expires_at = now + (ttl_seconds or 60)
            handle.expires_at = cur.expires_at
            return True

    def release(self, handle, config=None, **kw) -> None:
        with self._lock:
            cur = self._held.get(handle.key)
            if cur and cur.owner_token == handle.owner_token:
                del self._held[handle.key]


class InMemoryCheckpointStore(BaseCheckpointStore):
    """Paused workflows waiting for a decision; decide() is an atomic PENDING -> X."""

    def __init__(self):
        self._lock, self._items = threading.Lock(), {}

    def save(self, checkpoint, config=None, **kw):
        with self._lock:
            self._items[checkpoint['checkpoint_id']] = dict(checkpoint)

    def load(self, checkpoint_id, config=None, **kw):
        with self._lock:
            cp = self._items.get(checkpoint_id)
            if cp is None or (kw.get('tenant_id') and cp['tenant_id'] != kw['tenant_id']):
                return None
            return dict(cp)

    def decide(self, checkpoint_id, status, decided_by, config=None, **kw) -> bool:
        with self._lock:
            cp = self._items.get(checkpoint_id)
            if cp is None or cp['status'] != 'PENDING':
                return False
            cp.update(status=status, decided_by=decided_by, decided_at=time.time())
            return True
