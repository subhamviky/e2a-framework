"""E2A: Storage and integration ports: idempotency, transactional, cache, lock, webhook, outbox, reconciliation.

Split from e2a_base.py in v1.2.0 without code changes; e2a_base re-exports every name."""
import os
import re
import json
import uuid
import time
import asyncio
import inspect
import hashlib
import logging
import threading
import contextlib
import random
import concurrent.futures
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from .core import AdmissionTimeout, ClaimStatus, Deadline, PhysicalOutcome
from .config import resolve_config
from ._propagation import _PropagationMixin
 
 
# ==================================================
# BasePromptRegistry — Foundation interface, composed
# into BaseAgent._build_messages(), not called directly
# ==================================================
 
class BasePromptRegistry(ABC):
    """Interface — no shared implementation. Decouples prompt text
    from application code so business teams can update a prompt via
    GitOps without a container rebuild."""
 
    @abstractmethod
    def get_prompt(self, prompt_id: str, version: str = 'latest',
                    tenant_id: str = None, config: Dict[str, Any] = None,
                    **kwargs) -> 'PromptTemplate':
        """kwargs: prompt_store_url, default_version. Returns an object
        with a .render(**variables) -> str method."""
 
 
# Example usage inside a concrete BaseAgent — not part of the scaffold,
# shown here for reference:
#
# def _build_messages(self, state, config=None, **kwargs):
#     template = self.prompt_registry.get_prompt(
#         prompt_id=self.prompt_id,
#         version=kwargs.get('io_config', {}).get('prompt_version', 'latest'),
#         tenant_id=kwargs.get('tenant_id'))
#     return [{'role': 'user', 'content': template.render(query=state['query'])}]
 
# ==================================================
# New Foundation Ports — Section 2.16. Interfaces only,
# no shared implementation, no domain knowledge. A concrete
# subclass binds each one to a real backend.
# ==================================================
 
class BaseTransactionalStore(ABC):
    """Vendor-neutral durable read/write contract. put_if_version() is the
    AUTHORITY for competing writers (optimistic concurrency): a version
    check done earlier (e.g. _commit_gate's expected_version fast-fail) is
    advisory, because the state can change between check and write; only a
    conditional commit inside the store is atomic."""

    @abstractmethod
    def get(self, key: str, config: Dict[str, Any] = None, **kwargs) -> Optional[Dict[str, Any]]:
        """kwargs: table_or_collection_name."""

    @abstractmethod
    def put(self, key: str, value: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> bool:
        """Unconditional write — for records with a single writer by
        construction. Competing writers use put_if_version().
        kwargs: table_or_collection_name."""

    @abstractmethod
    def delete(self, key: str, config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: table_or_collection_name."""

    @abstractmethod
    def get_versioned(self, key: str, config: Dict[str, Any] = None, **kwargs):
        """Return (value, version) or None. kwargs: table_or_collection_name."""

    @abstractmethod
    def put_if_version(self, key: str, value: Dict[str, Any], expected_version: Optional[int],
                       config: Dict[str, Any] = None, **kwargs) -> 'CommitResult':
        """Atomic conditional commit. expected_version None means
        'create only if absent'. Returns CommitResult COMMITTED (new
        version) or CONFLICT (current_version) — never silently
        overwrites. kwargs: table_or_collection_name, fencing_token (when
        given, a token older than the highest one the store has accepted
        for this key must return status FENCED). On CONFLICT the caller
        reloads and RE-EVALUATES its preconditions; it never blindly
        retries the same decision against new state."""


class BaseCacheStore(ABC):
    """Vendor-neutral cache contract. Separate from
    BaseTransactionalStore — TTL/eviction semantics differ from
    durable-store semantics. A subclass implementing two-tier
    (L1 in-process / L2 shared) caching does so entirely inside
    get()/set(); no separate abstract method for that.

    get_or_load()/aget_or_load() are base-owned template methods adding
    per-key SINGLE-FLIGHT inside one process: concurrent misses for the
    same key share one loader call. This is deduplication of expensive
    work, not mutual exclusion — no lock is held around cache reads. A
    short distributed fill lease is only justified when a cross-replica
    stampede materially matters, and is then a subclass concern."""

    @abstractmethod
    def get(self, key: str, config: Dict[str, Any] = None, **kwargs) -> Optional[Any]:
        """kwargs: default_ttl_seconds."""

    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: int = None,
            config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: default_ttl_seconds."""

    @abstractmethod
    def delete(self, key: str, config: Dict[str, Any] = None, **kwargs) -> bool:
        pass

    # ---- single-flight (base-owned) ----
    def __sf(self) -> Dict[str, Any]:
        with _SINGLE_FLIGHT_INIT_LOCK:
            st = self.__dict__.get('_e2a_single_flight')
            if st is None:
                st = {'lock': threading.Lock(), 'flights': {}, 'followers': 0, 'loads': 0}
                self.__dict__['_e2a_single_flight'] = st
            return st

    @property
    def single_flight_stats(self) -> Dict[str, int]:
        st = self.__sf()
        return {'loads': st['loads'], 'followers': st['followers']}

    def get_or_load(self, key: str, loader: Callable[[], Any], ttl_seconds: int = None,
                    config: Dict[str, Any] = None, **kwargs) -> Any:
        """Cache-aside read with per-key single-flight (threads). Followers
        wait up to cache_fill_timeout_seconds (bounded by kwargs['deadline']),
        then follow cache_fill_on_timeout: load | stale | raise."""
        value = self.get(key, config, **kwargs)
        if value is not None:
            return value
        st = self.__sf()
        with st['lock']:
            flight = st['flights'].get(key)
            leader = flight is None
            if leader:
                flight = st['flights'][key] = _Flight()
                st['loads'] += 1
            else:
                st['followers'] += 1
        if leader:
            try:
                value = loader()
                if value is not None:
                    self.set(key, value, ttl_seconds, config, **kwargs)
                flight.finish(value=value)
                return value
            except BaseException as e:
                flight.finish(error=e)
                raise
            finally:
                with st['lock']:
                    st['flights'].pop(key, None)
        deadline = Deadline.coerce(kwargs.get('deadline'))
        timeout = float(resolve_config('cache_fill_timeout_seconds', config, kwargs))
        timeout = deadline.bound(timeout) if deadline else timeout
        if flight.event.wait(timeout):
            if flight.error is not None and not isinstance(flight.error, _FlightAborted):
                raise flight.error
            if flight.error is None:
                return flight.value
        return self.__follower_fallback(key, loader, config, kwargs)

    def __follower_fallback(self, key, loader, config, kwargs):
        policy = str(resolve_config('cache_fill_on_timeout', config, kwargs)).lower()
        if policy == 'raise':
            raise AdmissionTimeout(f'cache fill for {key!r} did not complete in time')
        if policy == 'stale':
            return self.get(key, config, **kwargs)
        return loader()

    async def aget_or_load(self, key: str, loader: Callable[[], Any], ttl_seconds: int = None,
                           config: Dict[str, Any] = None, **kwargs) -> Any:
        """asyncio variant of get_or_load(); `loader` is an async callable.
        get()/set() are the store's own (synchronous) calls."""
        value = self.get(key, config, **kwargs)
        if value is not None:
            return value
        st = self.__sf()
        loop = asyncio.get_running_loop()
        fkey = (id(loop), key)
        fut = st['flights'].get(fkey)
        if fut is None:
            fut = loop.create_future()
            st['flights'][fkey] = fut
            st['loads'] += 1
            try:
                value = await loader()
                if value is not None:
                    self.set(key, value, ttl_seconds, config, **kwargs)
                fut.set_result(value)
                return value
            except asyncio.CancelledError:
                fut.set_exception(_FlightAborted())   # followers fall back; they are not cancelled
                raise
            except BaseException as e:
                fut.set_exception(e)
                raise
            finally:
                st['flights'].pop(fkey, None)
                if fut.done() and not fut.cancelled():
                    fut.exception()      # mark retrieved: no 'never retrieved' warning when nobody followed
        st['followers'] += 1
        deadline = Deadline.coerce(kwargs.get('deadline'))
        timeout = float(resolve_config('cache_fill_timeout_seconds', config, kwargs))
        timeout = deadline.bound(timeout) if deadline else timeout
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except _FlightAborted:
            pass
        except asyncio.TimeoutError:
            pass
        policy = str(resolve_config('cache_fill_on_timeout', config, kwargs)).lower()
        if policy == 'raise':
            raise AdmissionTimeout(f'cache fill for {key!r} did not complete in time')
        if policy == 'stale':
            return self.get(key, config, **kwargs)
        return await loader()


_SINGLE_FLIGHT_INIT_LOCK = threading.Lock()


class _FlightAborted(Exception):
    """Leader was cancelled; followers degrade instead of inheriting the cancellation."""


class _Flight:
    def __init__(self):
        self.event, self.value, self.error = threading.Event(), None, None

    def finish(self, value=None, error=None):
        self.value = value
        self.error = _FlightAborted() if isinstance(error, (KeyboardInterrupt, SystemExit)) else error
        self.event.set()


class BaseIdempotencyStore(ABC):
    """Atomic idempotency CLAIM with a small state machine:
        ABSENT -> IN_PROGRESS(owner, lease) -> SUCCEEDED(result)
                                            -> FAILED_RETRYABLE | FAILED_FINAL
                                            -> UNKNOWN (outcome needs reconciliation)
    The ABSENT -> IN_PROGRESS transition MUST be one atomic conditional
    write: a duplicate caller never executes the action merely because it
    saw no cached result before another claimant committed ownership.
    Keys are scoped by tenant_id; a request fingerprint makes the same
    key with a different payload a typed CONFLICT. This is NOT a lock:
    it answers 'has this request already been (or is it being) done?'."""

    @abstractmethod
    def claim(self, idempotency_key: str, config: Dict[str, Any] = None, **kwargs) -> 'ClaimResult':
        """kwargs: tenant_id, fingerprint, owner, lease_seconds,
        ttl_seconds, recover (True lets a reconciler take over a
        RECOVERY_REQUIRED record). CLAIMED -> caller owns the work;
        IN_PROGRESS -> another unexpired owner; REPLAY -> stored result;
        CONFLICT -> fingerprint mismatch; RECOVERY_REQUIRED -> expired
        lease or UNKNOWN outcome; FAILED_FINAL -> never re-executed.
        TTL is garbage collection only — correctness uses an explicit
        expiry field, never a store's TTL deletion timing."""

    @abstractmethod
    def complete(self, idempotency_key: str, result: Dict[str, Any],
                 config: Dict[str, Any] = None, **kwargs) -> bool:
        """IN_PROGRESS -> SUCCEEDED, only for the current owner (a stale
        owner whose lease was taken over returns False). Store the
        post-redaction result. kwargs: tenant_id, owner."""

    @abstractmethod
    def fail(self, idempotency_key: str, retryable: bool = True,
             config: Dict[str, Any] = None, **kwargs) -> bool:
        """IN_PROGRESS -> FAILED_RETRYABLE (claimable again) or
        FAILED_FINAL (deterministic failure). kwargs: tenant_id, owner,
        outcome — PhysicalOutcome.UNKNOWN parks the record as
        RECOVERY_REQUIRED for the reconciler."""

    def check_and_set(self, idempotency_key: str, config: Dict[str, Any] = None,
                      **kwargs) -> bool:
        """Compatibility shim: True only for the caller that wins the
        atomic claim. Callers that need replay or in-progress
        distinction use claim()."""
        return self.claim(idempotency_key, config, **kwargs).status == ClaimStatus.CLAIMED


class BaseCheckpointStore(ABC):
    """Where a paused workflow waits for a human decision (v1.2, design note 01, E-17).
    A checkpoint is {'checkpoint_id', 'tenant_id', 'status': 'PENDING', 'step', 'state',
    'idempotency_key', 'correlation_id', 'requested_by', 'reason', 'created_at'}.
    BaseWorkflow saves it when a step pauses and resume() decides it exactly once."""

    @abstractmethod
    def save(self, checkpoint: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> None:
        """Store a new PENDING checkpoint under checkpoint['checkpoint_id']."""

    @abstractmethod
    def load(self, checkpoint_id: str, config: Dict[str, Any] = None, **kwargs) -> Optional[Dict[str, Any]]:
        """The checkpoint, or None. kwargs: tenant_id (a checkpoint of another tenant is None)."""

    @abstractmethod
    def decide(self, checkpoint_id: str, status: str, decided_by: str,
               config: Dict[str, Any] = None, **kwargs) -> bool:
        """PENDING -> status ('APPROVED' | 'REJECTED') as ONE atomic conditional write.
        False when it was no longer PENDING: a second or concurrent decision never
        resumes the workflow twice."""


class BaseDistributedLock(ABC):
    """Temporary EXCLUSIVE OWNERSHIP as a lease: time-bound, renewable,
    owner-checked. Used by scheduled/reconciliation work that needs one
    active worker (BaseOutboxSweeper, BaseReconciliationService). It is
    not used for ordinary writes (use put_if_version), de-duplication
    (use BaseIdempotencyStore) or cache fills (use get_or_load).

    A lease alone does not stop an expired-but-still-running owner from
    reaching a downstream store. Where that matters the holder passes
    LeaseHandle.fencing_token with each commit and the store rejects a
    token older than one it has already accepted."""

    @abstractmethod
    def acquire(self, lock_key: str, ttl_seconds: int,
                config: Dict[str, Any] = None, **kwargs) -> Optional['LeaseHandle']:
        """Return a LeaseHandle (truthy) or None if held elsewhere.
        fencing_token is strictly increasing per key. kwargs: lock_backend."""

    @abstractmethod
    def renew(self, handle: 'LeaseHandle', ttl_seconds: int = None,
              config: Dict[str, Any] = None, **kwargs) -> bool:
        """Extend the lease; False if it was lost (expired and taken)."""

    @abstractmethod
    def release(self, handle: 'LeaseHandle', config: Dict[str, Any] = None, **kwargs) -> None:
        """Owner-checked: releasing a lease that was lost/taken over is a no-op."""


class BaseWebhookDispatcher(ABC, _PropagationMixin):
    """PRIMARY class — dispatch() is the single public entry point.
    Outbound counterpart to BaseToolService: pushes a notification
    and does not wait for a business-logic response."""
 
    def dispatch(self, webhook_url: str, payload: Dict[str, Any],
                 config: Dict[str, Any] = None, correlation_id: str = None,
                 **kwargs) -> Dict[str, Any]:
        """Template method. kwargs: signing_secret, max_retries."""
        config = config or {}
        correlation_id = correlation_id or str(uuid.uuid4())
        signed = self._sign_payload(payload, resolve_config('signing_secret', config, kwargs), config, **kwargs)
        max_retries = resolve_config('max_retries', config, kwargs)
        for attempt in range(max_retries):
            if self._deliver(webhook_url, signed, config, **kwargs):
                return {'status': 'DELIVERED', 'correlation_id': correlation_id,
                        'attempts': attempt + 1}
        return {'status': 'FAILED', 'correlation_id': correlation_id,
                'attempts': max_retries}
 
    @abstractmethod
    def _sign_payload(self, payload: Dict[str, Any], signing_secret: str,
                       config: Dict[str, Any] = None, **kwargs) -> str:
        """kwargs: signing_algorithm. HMAC-SHA256 by default convention;
        subclass owns the actual implementation."""
 
    @abstractmethod
    def _deliver(self, webhook_url: str, signed_payload: str,
                 config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: timeout_seconds. The transport call — raw HTTP POST
        or a managed delivery service."""
 
 
class BaseSchemaTransformer(ABC):
    """Upcast/downcast contract for a versioned event/payload, applied
    inline at deserialization. No compute tier of its own, no business
    meaning of its own — a structural transform only."""
 
    @abstractmethod
    def upcast(self, payload: Dict[str, Any], from_version: str, to_version: str,
               config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        """kwargs: schema_registry_url."""
 
 
class BaseMemoryManager(ABC):
    """Agentic profile only — composed into BaseAgent._build_messages()
    the same way BasePromptRegistry is, not called directly by
    BaseWorkflow. No CQRS equivalent: CQRS carries no conversation
    state to prune."""
 
    @abstractmethod
    def prune(self, message_history: list, max_tokens: int,
              config: Dict[str, Any] = None, **kwargs) -> list:
        """kwargs: pruning_strategy. Strategy (sliding window,
        summarization, importance scoring) is entirely subclass-owned."""
 
 
# Example usage inside a concrete BaseAgent — not part of the scaffold,
# shown here for reference:
#
# def _build_messages(self, state, config=None, **kwargs):
#     history = self.memory_manager.prune(
#         state.get('message_history', []),
#         max_tokens=kwargs.get('io_config', {}).get('max_context_tokens', 8000))
#     ...
 
# ==================================================
# BaseOutboxSweeper and BaseReconciliationService —
# Section 2.17. Control-plane workers, scheduled, not
# called from BaseWorkflow.execute() or BaseAgent.run().
# ==================================================
 
class BaseOutboxSweeper(ABC, _PropagationMixin):
    """PRIMARY class — sweep() is the single public entry point.
    Invoked on a fixed cadence (EventBridge Scheduler / cron),
    not per request. Exclusivity is a LEASE (BaseDistributedLock): the
    sweeper renews it between records and stops with LEASE_LOST if it can
    no longer prove ownership. Each publish carries the lease's
    fencing_token so a downstream store can reject a stale sweeper."""

    def __init__(self, lock: 'BaseDistributedLock' = None):
        self.lock = lock  # injected BaseDistributedLock (2.16)

    def sweep(self, config: Dict[str, Any] = None, correlation_id: str = None,
              **kwargs) -> Dict[str, Any]:
        """Template method. kwargs: batch_size, max_age_seconds."""
        config = config or {}
        correlation_id = correlation_id or str(uuid.uuid4())
        handle = None
        if self.lock:
            handle = self.lock.acquire('outbox_sweeper', ttl_seconds=int(resolve_config('lease_ttl_seconds', config, kwargs)),
                                        config=config)
            if not handle:
                return {'status': 'SKIPPED_LOCKED', 'correlation_id': correlation_id}
        try:
            fencing = (handle.fencing_token if handle and resolve_config('fencing_enabled', config, kwargs) else None)
            renew_every = float(resolve_config('lease_renew_interval_seconds', config, kwargs))
            last_renew = time.monotonic()
            pending = self._fetch_pending_records(config, **kwargs)
            published, failed = 0, []
            for record in pending:
                if handle and time.monotonic() - last_renew >= renew_every:
                    if not self.lock.renew(handle, config=config):
                        handle = None   # lease lost: stop, do not release someone else's
                        return {'status': 'LEASE_LOST', 'correlation_id': correlation_id,
                                'published': published, 'failed_keys': failed}
                    last_renew = time.monotonic()
                if self._publish_pending_record(record, config, fencing_token=fencing, **kwargs):
                    published += 1
                else:
                    failed.append(record.get('id'))
            return {'status': 'SWEPT', 'correlation_id': correlation_id,
                    'published': published, 'failed_keys': failed}
        finally:
            if self.lock and handle:
                self.lock.release(handle, config=config)

    @abstractmethod
    def _fetch_pending_records(self, config: Dict[str, Any] = None, **kwargs) -> list:
        """kwargs: batch_size, max_age_seconds. Reads pending records
        from the Outbox table (Landing Zone Section 3.1.3 / 8.1)."""

    @abstractmethod
    def _publish_pending_record(self, record: Dict[str, Any],
                                 config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: destination_topic, fencing_token (the lease's token, or
        None when fencing is off — pass it on to any store write a stale
        sweeper could otherwise land)."""


class BaseReconciliationService(ABC, _PropagationMixin):
    """PRIMARY class — reconcile() is the single public entry point.
    Same scheduled-cadence, same lease pattern as BaseOutboxSweeper.

    For each entry it takes over the idempotency record (claim with
    recover=True), CONFIRMS PHYSICAL TRUTH through
    _confirm_physical_outcome() before any replay, and only then replays
    a NOT_COMMITTED entry. An UNKNOWN outcome stays parked for the next
    run — it is never replayed blindly (that is how a duplicate material
    effect happens)."""

    def __init__(self, lock: 'BaseDistributedLock' = None,
                 idempotency_store: 'BaseIdempotencyStore' = None):
        self.lock = lock
        self.idempotency_store = idempotency_store  # (2.16)

    def reconcile(self, config: Dict[str, Any] = None, correlation_id: str = None,
                  **kwargs) -> Dict[str, Any]:
        """Template method. kwargs: dlq_source, max_replay_count."""
        config = config or {}
        correlation_id = correlation_id or str(uuid.uuid4())
        handle = None
        if self.lock:
            handle = self.lock.acquire('reconciliation_service',
                                        ttl_seconds=int(resolve_config('lease_ttl_seconds', config, kwargs)),
                                        config=config)
            if not handle:
                return {'status': 'SKIPPED_LOCKED', 'correlation_id': correlation_id}
        store = self.idempotency_store
        try:
            fencing = (handle.fencing_token if handle and resolve_config('fencing_enabled', config, kwargs) else None)
            renew_every = float(resolve_config('lease_renew_interval_seconds', config, kwargs))
            last_renew = time.monotonic()
            lease_s = int(resolve_config('idempotency_lease_seconds', config, kwargs))
            entries = self._fetch_dlq_entries(config, **kwargs)
            replayed, exhausted, unresolved = 0, [], []
            for entry in entries:
                if handle and time.monotonic() - last_renew >= renew_every:
                    if not self.lock.renew(handle, config=config):
                        handle = None
                        return {'status': 'LEASE_LOST', 'correlation_id': correlation_id,
                                'replayed': replayed, 'failed_keys': exhausted, 'unresolved': unresolved}
                    last_renew = time.monotonic()
                key, tenant = entry.get('idempotency_key'), entry.get('tenant_id')
                owned = False
                if store and key:
                    cr = store.claim(key, config, tenant_id=tenant, owner=correlation_id,
                                     fingerprint=entry.get('fingerprint'), lease_seconds=lease_s, recover=True)
                    if cr.status != ClaimStatus.CLAIMED:
                        continue  # already reconciled, being worked, or finally failed
                    owned = True
                    outcome = self._confirm_physical_outcome(entry, config, fencing_token=fencing, **kwargs)
                    if outcome == PhysicalOutcome.COMMITTED:
                        store.complete(key, entry.get('result') or {'outcome': 'COMMITTED'}, config,
                                       tenant_id=tenant, owner=correlation_id)
                        continue    # the effect already happened — nothing to replay
                    if outcome == PhysicalOutcome.UNKNOWN:
                        store.fail(key, retryable=False, config=config, tenant_id=tenant,
                                   owner=correlation_id, outcome=PhysicalOutcome.UNKNOWN)
                        unresolved.append(entry.get('id'))
                        continue
                if self._replay_dlq_entry(entry, config, fencing_token=fencing, **kwargs):
                    replayed += 1
                    if owned:
                        store.complete(key, entry.get('result') or {'outcome': 'COMMITTED'}, config,
                                       tenant_id=tenant, owner=correlation_id)
                else:
                    exhausted.append(entry.get('id'))
                    if owned:
                        store.fail(key, retryable=True, config=config, tenant_id=tenant, owner=correlation_id)
            return {'status': 'RECONCILED', 'correlation_id': correlation_id,
                    'replayed': replayed, 'failed_keys': exhausted, 'unresolved': unresolved}
        finally:
            if self.lock and handle:
                self.lock.release(handle, config=config)

    def _confirm_physical_outcome(self, entry: Dict[str, Any], config: Dict[str, Any] = None,
                                  **kwargs) -> 'PhysicalOutcome':
        """Hook: ask the system of record whether the entry's material
        effect actually committed (by idempotency key / business key).
        Default UNKNOWN — a safe, never-replay default for a subclass that
        has not bound a confirmation query. kwargs: fencing_token."""
        return PhysicalOutcome.UNKNOWN

    @abstractmethod
    def _fetch_dlq_entries(self, config: Dict[str, Any] = None, **kwargs) -> list:
        """kwargs: dlq_source, max_replay_count."""

    @abstractmethod
    def _replay_dlq_entry(self, entry: Dict[str, Any],
                           config: Dict[str, Any] = None, **kwargs) -> bool:
        """kwargs: target_idempotency_store, fencing_token. Re-submits
        through the same BaseIdempotencyStore claim every live request
        uses, and through the same governance/policy path."""
