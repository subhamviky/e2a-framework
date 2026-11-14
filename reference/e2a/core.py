"""E2A: Outcome types, claim results, deadlines and the framework's exceptions.

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

 
class DehydrationInterrupt(Exception):
    """Signal exception, not an error. Raised exclusively by
    BaseGovernanceFramework.__trigger_dehydration() and caught exclusively
    by BaseWorkflow.execute() / BaseAgent.run(). Must never reach
    _handle_error() or the DLQ — a paused-for-approval request has not
    failed."""
    pass
 
 
class NFRViolationError(Exception):
    """Raised whenever a measured non-functional requirement is breached —
    a latency SLO in BaseAgent.run() / BaseRAGPipeline.retrieve() /
    BaseToolService.execute(), a capacity rejection or token-budget
    exhaustion in BaseGovernanceFramework. Unlike DehydrationInterrupt, not
    caught separately — falls through the normal except Exception path, so
    a breach is a regular failure: added to failed_keys, handed to
    _handle_error(), routed to the DLQ like any other exception."""
    pass



# ==================================================
# CONCURRENCY PRIMITIVES — typed outcomes and small, owner-local helpers.
# Nothing here is a global concurrency manager: each primitive is used by
# the class that owns the scarce resource or correctness invariant
# (LLM service -> permits/quota, stores -> conditional commit / claim,
# adapters -> retry budget / circuit / bulkhead, workflows -> deadline).
# ==================================================

class ConcurrencyConflict(Exception):
    """Typed conflict: an OCC version mismatch, a fenced (stale-owner)
    write, or an idempotency key reused with a different request
    fingerprint. Never auto-retried against new state without
    re-evaluating the caller's preconditions."""


class AdmissionTimeout(NFRViolationError):
    """A bounded wait for a permit or quota expired before admission —
    reject-early backpressure, not an unbounded queue."""


class DeadlineExceeded(NFRViolationError):
    """The request's remaining deadline reached zero."""


class CircuitOpenError(NFRViolationError):
    """A dependency's circuit is open; the call was never attempted."""


class BulkheadFullError(NFRViolationError):
    """A dependency's bulkhead had no free slot within its bounded wait."""


# Deterministic failures (validation / policy) are never retried.
_DETERMINISTIC_ERRORS = (ValueError, TypeError, PermissionError)


class PhysicalOutcome(str, Enum):
    """Typed commit evidence for a material side effect. Logical/model
    success is not proof of a system-of-record commit. UNKNOWN (e.g. a
    timeout after the request may have been committed) is reconciled,
    never blindly retried or dead-lettered."""
    COMMITTED = 'COMMITTED'
    NOT_COMMITTED = 'NOT_COMMITTED'
    UNKNOWN = 'UNKNOWN'


def outcome_of(result: Optional[Dict[str, Any]]) -> 'PhysicalOutcome':
    """Typed outcome of a tool/command result. physical_success stays the
    boolean view (True only for COMMITTED)."""
    result = result or {}
    if result.get('outcome') is not None:
        return PhysicalOutcome(result['outcome'])
    return PhysicalOutcome.COMMITTED if result.get('physical_success') else PhysicalOutcome.NOT_COMMITTED


class Deadline:
    """Additive propagation field `deadline`. Absolute on the local
    monotonic clock; across a process boundary it travels as REMAINING
    milliseconds (to_remaining_ms / from_remaining_ms), never as a
    wall-clock time, so clock skew cannot corrupt it. Cancellation is not
    a field: it propagates through the runtime (asyncio task
    cancellation / thread interruption)."""
    HEADER = 'x-e2a-deadline-ms'
    __slots__ = ('_at',)

    def __init__(self, at_monotonic: float):
        self._at = at_monotonic

    @classmethod
    def after(cls, seconds: float) -> 'Deadline':
        return cls(time.monotonic() + float(seconds))

    @classmethod
    def from_remaining_ms(cls, ms) -> 'Deadline':
        return cls.after(float(ms) / 1000.0)

    @classmethod
    def coerce(cls, value) -> Optional['Deadline']:
        """None -> None; Deadline -> itself; number -> remaining seconds."""
        if value is None or isinstance(value, Deadline):
            return value
        return cls.after(float(value))

    def remaining(self) -> float:
        return max(0.0, self._at - time.monotonic())

    def expired(self) -> bool:
        return time.monotonic() >= self._at

    def bound(self, timeout: Optional[float]) -> float:
        """min(timeout, remaining) — every bounded wait uses this."""
        rem = self.remaining()
        return rem if timeout is None else min(float(timeout), rem)

    def check(self, what: str = 'operation') -> None:
        if self.expired():
            raise DeadlineExceeded(f'request deadline exceeded before {what}')

    def to_remaining_ms(self) -> int:
        return int(self.remaining() * 1000)


class ClaimStatus(str, Enum):
    CLAIMED = 'CLAIMED'                      # caller owns the work; execute it
    IN_PROGRESS = 'IN_PROGRESS'              # another owner holds an unexpired claim
    REPLAY = 'REPLAY'                        # already SUCCEEDED; result is the stored one
    CONFLICT = 'CONFLICT'                    # same key, different request fingerprint
    RECOVERY_REQUIRED = 'RECOVERY_REQUIRED'  # owner lease expired / outcome UNKNOWN -> reconcile
    FAILED_FINAL = 'FAILED_FINAL'            # deterministic failure; never re-executed


class ModelRefusal(Exception):
    """The model declined the request on policy grounds (v1.6.0). A policy outcome, not a
    fault: process_inference returns status 'REFUSED' (never retried, never counted toward the
    circuit) and BaseAgent routes it to _fallback. A transport raises it, or returns
    stop_reason 'refusal'."""

    def __init__(self, provider: str = 'model', model: str = '', detail: str = '', category: str = ''):
        super().__init__(f"{provider} model {model} declined the request" + (f': {detail}' if detail else ''))
        self.provider, self.model, self.detail, self.category = provider, model, detail, category

    def as_dict(self) -> Dict[str, str]:
        return {'provider': self.provider, 'model': self.model, 'detail': self.detail, 'category': self.category}


@dataclass(frozen=True)
class ErrorClass:
    """What kind of failure an upstream call raised (v1.5.1). kind is one of:
    transient | throttled | overloaded | auth | deterministic | context_overflow | unknown.
    retry_after: the server's minimum wait in seconds, when it gave one."""
    kind: str
    retry_after: Optional[float] = None


_CONTEXT_MARKERS = ('context length', 'context_length', 'context window', 'maximum context',
                    'too many tokens', 'prompt is too long', 'input is too long')


def _http_status(error: BaseException) -> Optional[int]:
    for attr in ('status_code', 'status', 'http_status', 'code'):
        value = getattr(error, attr, None)
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value < 600:
            return value
    response = getattr(error, 'response', None)
    if isinstance(response, dict):                       # botocore ClientError
        value = (response.get('ResponseMetadata') or {}).get('HTTPStatusCode')
        return value if isinstance(value, int) else None
    value = getattr(response, 'status_code', None) or getattr(response, 'status', None)
    return value if isinstance(value, int) else None


def _retry_after(error: BaseException) -> Optional[float]:
    response = getattr(error, 'response', None)
    headers = getattr(response, 'headers', None) or getattr(error, 'headers', None)
    if headers is None and isinstance(response, dict):
        headers = (response.get('ResponseMetadata') or {}).get('HTTPHeaders')
    if not headers:
        return None
    try:
        lower = {str(k).lower(): v for k, v in dict(headers).items()}
    except (TypeError, ValueError):
        return None
    for name, scale in (('retry-after-ms', 0.001), ('retry-after', 1.0)):
        if name in lower:
            try:
                return max(0.0, float(lower[name]) * scale)
            except (TypeError, ValueError):              # an HTTP-date: not honoured, backoff applies
                return None
    return None


def classify_error(error: BaseException) -> ErrorClass:
    """Default classification of an upstream failure, from what SDKs commonly expose: an HTTP
    status (status_code / status / code, or botocore's response), Retry-After headers, and
    connection or timeout exception types. Anything else is 'unknown' (not retried, and it
    counts toward the circuit, as before v1.5.1)."""
    if isinstance(error, ModelRefusal):
        return ErrorClass('refusal')
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return ErrorClass('transient')
    if isinstance(error, _DETERMINISTIC_ERRORS):
        return ErrorClass('deterministic')
    status = _http_status(error)
    if status is not None:
        if status == 429:
            return ErrorClass('throttled', _retry_after(error))
        if status in (503, 529):
            return ErrorClass('overloaded', _retry_after(error))
        if status >= 500:
            return ErrorClass('transient', _retry_after(error))
        if status in (401, 403):
            return ErrorClass('auth')
        if status in (400, 413) and any(m in str(error).lower() for m in _CONTEXT_MARKERS):
            return ErrorClass('context_overflow')
        if 400 <= status < 500:
            return ErrorClass('deterministic')
    name = type(error).__name__
    if isinstance(error, ConnectionError) or 'Connect' in name or 'Timeout' in name:
        return ErrorClass('transient')
    return ErrorClass('unknown')


@dataclass
class ClaimResult:
    status: 'ClaimStatus'
    result: Optional[Dict[str, Any]] = None
    owner: Optional[str] = None


@dataclass
class CommitResult:
    """Outcome of BaseTransactionalStore.put_if_version()."""
    status: str                    # COMMITTED | CONFLICT | FENCED
    version: Optional[int] = None  # new version when COMMITTED
    current_version: Optional[int] = None  # store's version when CONFLICT

    @property
    def committed(self) -> bool:
        return self.status == 'COMMITTED'


@dataclass
class LeaseHandle:
    """Returned by BaseDistributedLock.acquire(). owner_token proves
    ownership for renew()/release(); fencing_token is monotonically
    increasing per key and must accompany any commit a stale (expired)
    owner could otherwise still land."""
    key: str
    owner_token: str
    fencing_token: int
    expires_at: float              # monotonic clock


# Metric catalogue — names, types and labels defined once. Emitted through
# BaseObservability's typed emitters (_emit_counter/_emit_histogram/
# _emit_gauge) or a class's own _emit_metric() hook. tenant_id is a
# high-cardinality label: aggregate or sample it in the metrics backend.
E2A_METRICS: Dict[str, Dict[str, Any]] = {
    'e2a.llm.inflight': {'type': 'gauge', 'unit': 'requests', 'labels': ['pod']},
    'e2a.llm.permit_wait_seconds': {'type': 'histogram', 'unit': 's', 'labels': ['tenant_id']},
    'e2a.llm.quota_wait_seconds': {'type': 'histogram', 'unit': 's', 'labels': ['tenant_id']},
    'e2a.admission.rejected_total': {'type': 'counter', 'unit': '1', 'labels': ['reason']},
    'e2a.retry.attempts_total': {'type': 'counter', 'unit': '1', 'labels': ['dependency']},
    'e2a.retry.budget_exhausted_total': {'type': 'counter', 'unit': '1', 'labels': ['dependency']},
    'e2a.circuit.state': {'type': 'gauge', 'unit': '0=closed,1=half,2=open', 'labels': ['dependency']},
    'e2a.bulkhead.wait_seconds': {'type': 'histogram', 'unit': 's', 'labels': ['dependency']},
    'e2a.occ.conflicts_total': {'type': 'counter', 'unit': '1', 'labels': ['store']},
    'e2a.idempotency.replays_total': {'type': 'counter', 'unit': '1', 'labels': ['status']},
    'e2a.lease.lost_total': {'type': 'counter', 'unit': '1', 'labels': ['lease']},
    'e2a.fencing.rejections_total': {'type': 'counter', 'unit': '1', 'labels': ['store']},
    'e2a.cache.single_flight_followers_total': {'type': 'counter', 'unit': '1', 'labels': ['cache']},
    'e2a.queue.depth': {'type': 'gauge', 'unit': 'messages', 'labels': ['queue']},
    'e2a.queue.oldest_age_seconds': {'type': 'gauge', 'unit': 's', 'labels': ['queue']},
    'e2a.cancellation.cleanup_total': {'type': 'counter', 'unit': '1', 'labels': ['owner']},
}
