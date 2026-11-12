"""E2A: Internal: the propagation helpers mixed into every lifecycle class.

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

from .core import Deadline
from .config import resolve_config
from .resilience import _run_on_bridge_loop
# ==================================================
# Shared propagation helpers (mixed into every class below)
# ==================================================
 
class _PropagationMixin:
    """Shared, base-owned helpers for correlation/log/DLQ handling.
    Never overridden by subclasses."""
 
    def _resolve_io_config(self, config=None, **kwargs):
        config = config or {}
        prefix = resolve_config('io_config_prefix', config, kwargs)
        return {k: v for k, v in config.items() if k.startswith(prefix)}
 
    def _log(self, message_log, correlation_id, level, event, **fields):
        if message_log is None:
            return
        message_log.append({
            'timestamp': time.time(), 'correlation_id': correlation_id,
            'level': level, 'event': event,
            'class': type(self).__name__, **fields,
        })
 
    def _flush_logs(self, message_log, config=None, **kwargs):
        config = config or {}
        sink = resolve_config('log_sink', config, kwargs)
        for entry in (message_log or []):
            logging.info(entry) if sink == 'stdout' else None
            # cloudwatch / datadog / stackdriver dispatch goes here
 
    def _send_to_dlq(self, failed_keys, config=None, **kwargs):
        config = config or {}
        queue_url = resolve_config('dlq_queue_url', config, kwargs)
        if queue_url and failed_keys:
            logging.warning(f'DLQ dispatch -> {queue_url}: {failed_keys}')
 
    def _commit_gate(self, key, failed_keys, config=None, **kwargs):
        """Base-owned, called immediately before any save/commit/write.
        Two independent checks, both must pass:
        1. Batch isolation: key must not already be in failed_keys.
        2. Optimistic Concurrency Control (optional): if the caller passes
           expected_version, it must match the store's actual_version —
           a mismatch means a sibling process already wrote a newer
           version, so this write is rejected rather than clobbering it.
        OCC and the failed_keys check are deliberately one gate, not two:
        both answer 'is it still safe to write this item right now?'

        ADVISORY ONLY for competing writers: a version compared here can
        change before the write lands (check-then-write). The AUTHORITY is
        BaseTransactionalStore.put_if_version(), an atomic conditional
        commit returning a typed CommitResult. This gate is the cheap
        local fast-fail in front of it."""
        config = config or {}
        enabled = resolve_config('commit_gate_enabled', config, kwargs)
        if not enabled:
            return True
        if key in (failed_keys or []):
            return False
        expected_version = kwargs.get('expected_version')
        if expected_version is not None:
            actual_version = kwargs.get('actual_version_lookup', lambda k, c: expected_version)(key, config)
            if expected_version != actual_version:
                return False  # OCC collision — a sibling process wrote a newer version
        return True

    # ---- LLM service bridge (the ONLY way any agent reaches a model) ----
    def _resolve_llm_service(self, config=None, **kwargs):
        config = config or {}
        from .llm import BaseLLMAgentService   # deferred: llm itself imports this mixin
        svc = kwargs.get('llm_service') or config.get('llm_service')
        if not isinstance(svc, BaseLLMAgentService):
            raise ValueError("config['llm_service'] must be a BaseLLMAgentService")
        return svc

    async def _infer_async(self, messages, config=None, **kwargs) -> Dict[str, Any]:
        """Awaitable bridge for async callers (orchestrators, tests)."""
        svc = self._resolve_llm_service(config, **kwargs)
        kwargs.pop('llm_service', None)
        result = await svc.process_inference(messages, config, **kwargs)
        if result.get('status') == 'FAILED':
            raise RuntimeError(f"LLM inference failed: {result.get('error')}")
        return result

    def _infer(self, messages, config=None, **kwargs) -> Dict[str, Any]:
        """Sync bridge for BaseAgent.run() / LLMOnlyAgent._handle_*().
        Runs the coroutine on one process-wide background loop so the
        service's semaphores and token bucket are always bound to the
        same event loop, whatever thread the agent runs on. A FAILED
        inference raises, so the caller's normal except path
        (failed_keys + _handle_error) applies."""
        c = config or {}
        deadline = Deadline.coerce(kwargs.get('deadline'))
        bound = (float(resolve_config('llm_max_latency', c, kwargs)) + float(resolve_config('permit_timeout', c, kwargs))
                 + float(resolve_config('quota_timeout', c, kwargs)) + 1.0)
        # slack so the service raises its own typed DeadlineExceeded first
        timeout = min(bound, deadline.remaining() + 0.5) if deadline else bound
        return _run_on_bridge_loop(self._infer_async(messages, config, **kwargs), timeout=timeout)
