"""E2A: the BOPF-style propagation contract (v1.2).

Every lifecycle hook receives role-separated, declared parameters, as a BOPF
determination receives is_ctx, it_key, io_read, io_modify, eo_message and
et_failed_key:

    ctx          RequestContext  input, frozen: tenant, correlation, idempotency key,
                                 deadline, settings (the resolved configuration), trace
    state        dict            the business data
    io_read      ReadAccess      reads: replay-safe tools and named resources
    io_modify    ModifyAccess    material effects: tools called with an idempotency key
                                 and typed outcomes (given only to the step that acts)
    messages     MessageLog      output: messages and evidence
    failed_keys  FailedKeys      output: items that failed, without stopping the batch

The entry point creates these once per request and passes the same objects down.
The context is never written into the state.
"""
import uuid
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .config import E2A_CONFIG, resolve_config
from .core import Deadline, PhysicalOutcome
from .resilience import run_sync

_UNSET = object()

# Keys that are object references or per-call values, not settings.
_NOT_SETTINGS = {'llm_service', 'governance_engine', 'observability_engine', 'idempotency_store',
                 'tools', 'resources'}


@dataclass(frozen=True)
class RequestContext:
    """The request's identity and settings: the `is_ctx` of E2A. Frozen."""
    tenant_id: Optional[str]
    correlation_id: str
    idempotency_key: Optional[str] = None
    deadline: Optional[Deadline] = None
    settings: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    trace: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def begin(cls, *, tenant_id: str, config: Optional[Mapping[str, Any]] = None,
              correlation_id: Optional[str] = None, idempotency_key: Optional[str] = None,
              deadline: Any = None, trace: Optional[Mapping[str, str]] = None,
              overrides: Optional[Mapping[str, Any]] = None, require_tenant: bool = True) -> 'RequestContext':
        """Create the context once, at the outermost entry point. Settings are resolved a
        single time through the central registry (kwargs > config > env > default), for
        every registered key, plus any application keys present in `config`."""
        if not tenant_id and require_tenant:
            raise ValueError('a tenant_id is required to start a request')
        config = dict(config or {})
        overrides = {k: v for k, v in dict(overrides or {}).items() if k in E2A_CONFIG}
        settings: Dict[str, Any] = {k: v for k, v in config.items() if k not in _NOT_SETTINGS}
        for key in E2A_CONFIG:
            if key not in _NOT_SETTINGS:
                settings[key] = resolve_config(key, config, overrides)
        if deadline is None:
            seconds = float(settings.get('request_deadline_seconds') or 0)
            deadline = Deadline.after(seconds) if seconds > 0 else None
        return cls(tenant_id=tenant_id, correlation_id=correlation_id or str(uuid.uuid4()),
                   idempotency_key=idempotency_key, deadline=Deadline.coerce(deadline),
                   settings=MappingProxyType(settings), trace=MappingProxyType(dict(trace or {})))

    def child(self, **changes) -> 'RequestContext':
        """The same request with a narrower scope, for example a tool call with its own key."""
        return replace(self, **changes)

    def setting(self, key: str, default: Any = _UNSET) -> Any:
        if key in self.settings:
            return self.settings[key]
        if default is not _UNSET:
            return default
        raise KeyError(f"'{key}' is not a setting of this request")

    def expired(self) -> bool:
        return bool(self.deadline and self.deadline.expired())


class MessageLog:
    """Output: messages and evidence (the `eo_message` of E2A). Appends to the request's
    log list, the same list v1.1 code knows as message_log."""

    def __init__(self, entries: Optional[List[dict]] = None, correlation_id: Optional[str] = None,
                 source: str = '') -> None:
        self.entries: List[dict] = entries if entries is not None else []
        self.correlation_id, self.source = correlation_id, source

    def add(self, level: str, event: str, **fields) -> None:
        import time
        self.entries.append({'timestamp': time.time(), 'correlation_id': self.correlation_id,
                             'level': level, 'event': event, 'class': self.source, **fields})

    def info(self, event: str, **fields) -> None:
        self.add('INFO', event, **fields)

    def warn(self, event: str, **fields) -> None:
        self.add('WARN', event, **fields)

    def error(self, event: str, **fields) -> None:
        self.add('ERROR', event, **fields)

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


class FailedKeys:
    """Output: keys of items that failed (the `et_failed_key` of E2A). Appends to the
    request's list, the same list v1.1 code knows as failed_keys."""

    def __init__(self, keys: Optional[List[str]] = None) -> None:
        self.keys: List[str] = keys if keys is not None else []

    def add(self, key: str) -> None:
        if key not in self.keys:
            self.keys.append(key)

    def __contains__(self, key) -> bool:
        return key in self.keys

    def __iter__(self):
        return iter(self.keys)

    def __len__(self) -> int:
        return len(self.keys)


class ToolDenied(PermissionError):
    """The calling class is not allowed to use this tool."""


class _Access:
    material = False

    def __init__(self, ctx: RequestContext, config: Optional[Mapping[str, Any]] = None,
                 tools: Optional[Mapping[str, Any]] = None, resources: Optional[Mapping[str, Any]] = None,
                 allowed: Optional[Iterable[str]] = None, messages: Optional[MessageLog] = None) -> None:
        config = config or {}
        self._ctx, self._config, self._messages = ctx, dict(config), messages
        self._tools = dict(tools if tools is not None else config.get('tools') or {})
        self._resources = dict(resources if resources is not None else config.get('resources') or {})
        self._allowed = set(allowed) if allowed is not None else None

    def resource(self, name: str) -> Any:
        """A named object the application registered under config['resources'] (a store, a
        RAG pipeline, a client)."""
        if name not in self._resources:
            raise KeyError(f"no resource named '{name}'")
        return self._resources[name]

    def _call(self, name: str, payload: Mapping[str, Any], **call_kwargs) -> Dict[str, Any]:
        if name not in self._tools:
            raise KeyError(f"no tool named '{name}'")
        if self._allowed is not None and name not in self._allowed:
            raise ToolDenied(f"tool '{name}' is not allowed here")
        tool = self._tools[name]
        if self.material != bool(getattr(tool, 'material', False)):
            kind = 'material' if getattr(tool, 'material', False) else 'read-only'
            where = 'io_modify' if self.material else 'io_read'
            raise ToolDenied(f"tool '{name}' is {kind}; it cannot be called through {where}")
        timeout = float(self._ctx.setting('timeout', 5.0)) * (1 + int(self._ctx.setting('retries', 0))) + 1.0
        if self._ctx.deadline:
            timeout = max(0.01, min(timeout, self._ctx.deadline.remaining()))
        try:
            result = run_sync(tool.execute(dict(payload), self._config, ctx=self._ctx, **call_kwargs),
                              timeout=timeout)
        except (PermissionError, ValueError, KeyError):
            raise
        except Exception as error:      # transport failure after the tool's own retries
            outcome = PhysicalOutcome.UNKNOWN if self.material else PhysicalOutcome.NOT_COMMITTED
            result = {'status': 'ERROR', 'physical_success': False, 'outcome': outcome, 'error': str(error)}
        if self.material:
            result['outcome'] = PhysicalOutcome(result.get('outcome') or (
                PhysicalOutcome.COMMITTED if result.get('physical_success') else PhysicalOutcome.NOT_COMMITTED))
        else:   # a read commits nothing; physical_success still says whether the read worked
            result['outcome'] = PhysicalOutcome.NOT_COMMITTED
        if self._messages is not None:
            self._messages.info('tool_call', tool=name, outcome=result['outcome'].value)
        return result


class ReadAccess(_Access):
    """`io_read`: replay-safe tool calls (retried freely) and named resources."""
    material = False

    def call(self, name: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return self._call(name, payload, safe_to_retry=True)


class ModifyAccess(_Access):
    """`io_modify`: tools with a material effect. Every call carries an idempotency key
    (the request's, unless a narrower one is given) and returns a typed outcome:
    COMMITTED, NOT_COMMITTED or UNKNOWN (a transport failure after the request may
    have been committed: reconcile, never re-send blindly under a new key)."""
    material = True

    def call(self, name: str, payload: Mapping[str, Any], idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        key = idempotency_key or self._ctx.idempotency_key
        if not key:
            raise ValueError(f"material tool '{name}' needs an idempotency key")
        if self._ctx.expired():
            return {'status': 'DEADLINE', 'physical_success': False, 'outcome': PhysicalOutcome.NOT_COMMITTED,
                    'error': 'request deadline passed before the call'}
        return self._call(name, payload, idempotency_key=key)
