"""E2A: BaseObservability.

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

 
# ==================================================
# BaseObservability & BaseGovernanceFramework — Foundation classes
# referenced by BaseAgent.run() / BaseWorkflow.execute() below.
# Defined ahead of BaseValidationService since both are wired in via
# config['observability_engine'] / config['governance_engine'] before
# any agent-specific class is reached.
# ==================================================
 

def _framework_version() -> str:
    """v1.5.1: the installed E2A version (it said 'e2a-v1.0' for every release)."""
    from . import __version__
    return f'e2a-v{__version__}'

class BaseObservability(ABC):
    """Template method abstract class for Enterprise Telemetry.
    record_telemetry() is the single public entry point (Section 2.10).
    A subclass that omits any @abstractmethod hook below fails at
    instantiation rather than shipping a no-op integration to production."""
 
    def record_telemetry(self, message_log: List[dict], correlation_id: str,
                          config: Dict[str, Any] = None, **kwargs) -> None:
        config = config or {}
        enriched_logs = []
        for entry in (message_log or []):
            enriched_logs.append({
                **entry,
                'correlation_id': correlation_id,
                'tenant_id': kwargs.get('tenant_id', 'UNKNOWN_TENANT'),
                'host_epoch_ms': int(time.time() * 1000),
                'framework_version': _framework_version(),
            })
        metrics = self.__extract_metrics(enriched_logs)
        try:
            self._ship_logs(enriched_logs, config, **kwargs)
            if metrics:
                self._emit_metrics(metrics, config, **kwargs)
            self._export_traces(correlation_id, enriched_logs, config, **kwargs)
        except Exception as e:
            logging.error(f'Telemetry dispatch failed for {correlation_id}: {e}')
            for log in enriched_logs:
                logging.info(log)
 
    def __extract_metrics(self, enriched_logs: List[dict]) -> Dict[str, float]:
        # Base-owned, never overridden. Single pass: counts ERROR-level
        # entries into error_count, sums any tokens_used field into
        # total_tokens.
        metrics = {'total_tokens': 0.0, 'error_count': 0}
        for log in enriched_logs:
            if log.get('level') == 'ERROR':
                metrics['error_count'] += 1
            if 'tokens_used' in log:
                metrics['total_tokens'] += log['tokens_used']
        return metrics
 
    @abstractmethod
    def _ship_logs(self, enriched_logs: List[dict], config: Dict[str, Any], **kwargs) -> None:
        """kwargs: log_sink. Ship enriched log entries to the configured
        sink (CloudWatch Logs / Datadog / Cloud Logging)."""
 
    @abstractmethod
    def _emit_metrics(self, metrics: Dict[str, float], config: Dict[str, Any], **kwargs) -> None:
        """kwargs: namespace, dimensions. Emit the aggregate metrics
        record_telemetry() derived (total_tokens, error_count)."""
 
    @abstractmethod
    def _export_traces(self, correlation_id: str, logs: List[dict],
                        config: Dict[str, Any], **kwargs) -> None:
        """kwargs: trace_id, service_name. If trace_context (Section 2.1)
        is present, this is where it is forwarded as the W3C traceparent
        header to the next hop — correlation_id remains the internal
        join key; trace_context is what crosses the wire."""
 
    # Optional typed emitters — a subclass overrides these instead of, or
    # alongside, _emit_metrics() if the metrics backend distinguishes
    # metric types (OTel Counter/Histogram/Gauge). Default no-op: a
    # subclass that only implements _emit_metrics() above still works.
    def _emit_counter(self, name: str, value: float, dimensions: Dict[str, Any] = None,
                       config: Dict[str, Any] = None, **kwargs) -> None:
        pass
 
    def _emit_histogram(self, name: str, value: float, dimensions: Dict[str, Any] = None,
                         config: Dict[str, Any] = None, **kwargs) -> None:
        pass
 
    def _emit_gauge(self, name: str, value: float, dimensions: Dict[str, Any] = None,
                     config: Dict[str, Any] = None, **kwargs) -> None:
        pass
