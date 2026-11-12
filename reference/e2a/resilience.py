"""E2A: Retry budget, circuit breaker, bulkhead, ResiliencePolicy, the sync bridge (run_sync) and validate_capacity().

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

from .core import AdmissionTimeout, BulkheadFullError, CircuitOpenError, Deadline, DeadlineExceeded, NFRViolationError, _DETERMINISTIC_ERRORS
from .config import resolve_config
 
 

_BRIDGE = {'loop': None}
_BRIDGE_LOCK = threading.Lock()


def run_sync(coro, timeout: Optional[float] = None):
    """Public bridge (v1.1.2): run a coroutine, for example a tool's execute(), from
    synchronous code such as an agent hook, on the shared background loop that the
    framework's semaphores and resilience policies are bound to."""
    return _run_on_bridge_loop(coro, timeout=timeout)


def _run_on_bridge_loop(coro, timeout: Optional[float] = None):
    """Run a coroutine to completion on one shared background event
    loop (created lazily, daemon thread) and return its result. The wait
    is BOUNDED when `timeout` is given: on expiry (or if the calling
    thread is interrupted) the coroutine is cancelled, so its permits and
    quota are released by the coroutine's own cleanup — a parent deadline
    or cancellation is never left orphaned on the bridge loop."""
    with _BRIDGE_LOCK:
        if _BRIDGE['loop'] is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, daemon=True,
                             name='e2a-llm-bridge').start()
            _BRIDGE['loop'] = loop
    fut = asyncio.run_coroutine_threadsafe(coro, _BRIDGE['loop'])
    try:
        return fut.result(timeout)
    except concurrent.futures.TimeoutError:
        if fut.done():
            raise                      # the coroutine's own TimeoutError, not ours
        fut.cancel()
        raise DeadlineExceeded(f'bridge call exceeded its {timeout:.2f}s bound; coroutine cancelled')
    except BaseException:
        fut.cancel()
        raise


# ==================================================
# ResiliencePolicy — retry budget + circuit breaker + bulkhead, as ONE small
# composition helper owned by the ADAPTER that calls a dependency
# (BaseToolService, BaseLLMAgentService upstream, retrieval adapters).
# Not a manager: each adapter instance owns one policy per dependency, so
# a saturated dependency cannot starve another.
# ==================================================

class RetryBudget:
    """Token-deposit retry budget: every first attempt earns `ratio`
    tokens (capped); every retry spends one. Bounds retry amplification
    to roughly `ratio` of traffic regardless of failure rate."""

    def __init__(self, ratio: float = 0.2, cap: float = 10.0):
        self._ratio, self._cap = float(ratio), float(cap)
        self._tokens = self._cap / 2.0
        self._lock = threading.Lock()

    def deposit(self) -> None:
        with self._lock:
            self._tokens = min(self._cap, self._tokens + self._ratio)

    def withdraw(self) -> bool:
        with self._lock:
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    @property
    def balance(self) -> float:
        return self._tokens


class CircuitBreaker:
    """closed -> (threshold consecutive failures) -> open -> (reset window)
    -> half-open (one probe) -> closed on success / open on failure."""

    def __init__(self, failure_threshold: int = 5, reset_seconds: float = 30.0, clock=time.monotonic):
        self._threshold, self._reset, self._clock = int(failure_threshold), float(reset_seconds), clock
        self._failures, self._opened_at, self._probing = 0, None, False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return 'closed'
            return 'half_open' if self._clock() - self._opened_at >= self._reset else 'open'

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at >= self._reset and not self._probing:
                self._probing = True      # exactly one half-open probe
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures, self._opened_at, self._probing = 0, None, False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._probing or self._failures >= self._threshold:
                self._opened_at, self._probing = self._clock(), False


class Bulkhead:
    """Per-dependency in-flight cap with a BOUNDED wait."""

    def __init__(self, max_concurrent: int, name: str = 'dependency'):
        self.name, self.max_concurrent = name, int(max_concurrent)
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self.waiting = 0

    @contextlib.asynccontextmanager
    async def slot(self, timeout: float):
        self.waiting += 1
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            raise BulkheadFullError(f"bulkhead '{self.name}' full ({self.max_concurrent} in flight)")
        finally:
            self.waiting -= 1
        try:
            yield
        finally:
            self._sem.release()


class ResiliencePolicy:
    """One dependency's retry / circuit / bulkhead state.

    call(fn, deadline=, max_retries=, idempotent=) runs the zero-argument
    coroutine factory `fn`:
      * circuit open            -> CircuitOpenError, fn never called
      * bulkhead full (bounded) -> BulkheadFullError
      * deterministic failures (ValueError/TypeError/PermissionError) and
        NFR/deadline errors are never retried
      * a timeout is retried only when the call is idempotent (a timeout
        after a possible commit is an UNKNOWN outcome for a material
        write — never blindly re-sent)
      * each retry spends one retry-budget token, uses full-jitter
        exponential backoff and never sleeps past the remaining deadline
    `emit(name, **fields)` is an optional metrics callback."""

    def __init__(self, name: str, config: Dict[str, Any] = None, kwargs: Dict[str, Any] = None,
                 bulkhead: bool = True, emit: Callable[..., None] = None):
        c, k = config or {}, kwargs or {}
        self.name, self._emit = name, emit
        self.base = float(resolve_config('retry_base_backoff', c, k))
        self.max_backoff = float(resolve_config('retry_max_backoff', c, k))
        self.budget = RetryBudget(resolve_config('retry_budget_ratio', c, k),
                                  resolve_config('retry_budget_cap', c, k))
        self.breaker = CircuitBreaker(resolve_config('circuit_failure_threshold', c, k),
                                      resolve_config('circuit_reset_seconds', c, k))
        n = int(resolve_config('bulkhead_max_concurrent', c, k))
        self.bulkhead = Bulkhead(n, name) if (bulkhead and n > 0) else None
        self.bulkhead_timeout = float(resolve_config('bulkhead_acquire_timeout', c, k))
        self.stats = {'attempts': 0, 'retries': 0, 'budget_exhausted': 0, 'circuit_rejected': 0}

    def _note(self, event: str, **fields) -> None:
        if self._emit:
            self._emit(event, dependency=self.name, **fields)

    async def call(self, fn: Callable[[], Any], *, deadline: Optional[Deadline] = None,
                   max_retries: int = 0, idempotent: bool = False,
                   retryable: Callable[[BaseException], bool] = None):
        if deadline:
            deadline.check(f'calling {self.name}')
        if self.bulkhead is None:
            return await self.__attempts(fn, deadline, max_retries, idempotent, retryable)
        wait = deadline.bound(self.bulkhead_timeout) if deadline else self.bulkhead_timeout
        async with self.bulkhead.slot(wait):
            return await self.__attempts(fn, deadline, max_retries, idempotent, retryable)

    async def __attempts(self, fn, deadline, max_retries, idempotent, retryable):
        self.budget.deposit()
        attempt = 0
        while True:
            if not self.breaker.allow():
                self.stats['circuit_rejected'] += 1
                self._note('circuit_open')
                raise CircuitOpenError(f"circuit open for '{self.name}'")
            attempt += 1
            self.stats['attempts'] += 1
            try:
                result = await fn()
                self.breaker.record_success()
                return result
            except asyncio.CancelledError:
                raise
            except Exception as e:
                is_timeout = isinstance(e, (asyncio.TimeoutError, TimeoutError))
                structural = isinstance(e, (CircuitOpenError, BulkheadFullError, DeadlineExceeded, AdmissionTimeout))
                if not isinstance(e, _DETERMINISTIC_ERRORS) and not structural:
                    self.breaker.record_failure()
                can_retry = (attempt <= max_retries and not isinstance(e, _DETERMINISTIC_ERRORS)
                             and not structural and not isinstance(e, NFRViolationError)
                             and (idempotent or not is_timeout)
                             and (retryable(e) if retryable else True))
                if can_retry and not self.budget.withdraw():
                    self.stats['budget_exhausted'] += 1
                    self._note('retry_budget_exhausted')
                    can_retry = False
                if not can_retry:
                    raise
                delay = random.uniform(0.0, min(self.max_backoff, self.base * (2 ** (attempt - 1))))
                if deadline and deadline.remaining() <= delay:
                    raise
                self.stats['retries'] += 1
                self._note('retry', attempt=attempt, delay=round(delay, 4))
                await asyncio.sleep(delay)


def validate_capacity(config: Dict[str, Any] = None, **kwargs) -> List[str]:
    """Capacity equation as ONE pure, testable rule over the central
    registry: replicas x per-instance concurrency must not silently exceed
    pools, ceilings or the shutdown window. Returns violation strings
    (empty = consistent). Runs as part of BasePipeline's scaffold-compliance
    gate and may be called at start-up."""
    r = lambda key: resolve_config(key, config, kwargs)
    v: List[str] = []
    replicas = max(1, int(r('fleet_replica_count')))
    pod, tenant = int(r('pod_concurrency')), int(r('tenant_concurrency'))
    workers, prefetch = int(r('worker_concurrency')), int(r('worker_prefetch'))
    db_pool, http_pool = int(r('db_pool_size')), int(r('http_pool_size'))
    if workers > db_pool:
        v.append(f'worker_concurrency ({workers}) exceeds db_pool_size ({db_pool}) per instance')
    if pod > http_pool:
        v.append(f'pod_concurrency ({pod}) exceeds http_pool_size ({http_pool}) per instance')
    if tenant > pod:
        v.append(f'tenant_concurrency ({tenant}) exceeds pod_concurrency ({pod})')
    if prefetch < workers:
        v.append(f'worker_prefetch ({prefetch}) is below worker_concurrency ({workers}); workers would starve')
    db_max = int(r('db_max_connections'))
    if db_max and replicas * db_pool > db_max:
        v.append(f'fleet DB connections {replicas} x {db_pool} = {replicas * db_pool} exceed db_max_connections ({db_max})')
    model_max = int(r('model_max_inflight'))
    amplified = replicas * pod * (1.0 + float(r('retry_budget_ratio')))
    if model_max and amplified > model_max:
        v.append(f'fleet model concurrency {replicas} x {pod} x (1+retry_budget_ratio) = {amplified:.0f} exceeds model_max_inflight ({model_max})')
    if int(r('lease_renew_interval_seconds')) >= int(r('lease_ttl_seconds')):
        v.append('lease_renew_interval_seconds must be shorter than lease_ttl_seconds')
    if float(r('drain_timeout_seconds')) >= float(r('termination_grace_seconds')):
        v.append('drain_timeout_seconds must be shorter than termination_grace_seconds')
    return v
