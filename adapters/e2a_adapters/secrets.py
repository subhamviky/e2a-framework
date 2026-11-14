"""Secrets as tool credentials (v1.7.0, design note MP-05, N2).

E2A's BaseCredentialProvider (v1.6.0) gives a tool call its auth headers. The providers here read
them from the cloud's secret store, through the platform identity (a task or pod role, a
managed identity, a service account), so no secret is in configuration:

    secret_headers = [{"target": "https://api.partner.example", "secret": "partner-api-token",
                       "header": "Authorization", "prefix": "Bearer "}]

The longest matching `target` prefix wins; `json_key` reads one field of a JSON secret. A value
is cached for `secret_cache_seconds`, so a rotated secret is picked up without a restart.
"""
import json
import threading
import time
from abc import abstractmethod
from typing import Any, Dict, Optional

from e2a import BaseCredentialProvider, resolve_config

from . import config_keys  # noqa: F401


class SecretHeaderProvider(BaseCredentialProvider):
    capabilities = frozenset({'secret_store'})

    def __init__(self, config: Optional[Dict[str, Any]] = None, client: Any = None, clock=time.monotonic) -> None:
        self.config, self._client, self._clock = dict(config or {}), client, clock
        self._cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'SecretHeaderProvider':
        return cls(config)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client(self.config)
        return self._client

    @abstractmethod
    def _build_client(self, config: Dict[str, Any]) -> Any:
        """The secret store's SDK client, through the platform identity."""

    @abstractmethod
    def _fetch(self, secret: str) -> str:
        """The current value of one secret."""

    def secret(self, name: str) -> str:
        """Public: one secret's value, cached for secret_cache_seconds."""
        ttl = float(resolve_config('secret_cache_seconds', self.config))
        with self._lock:
            hit = self._cache.get(name)
            if hit and self._clock() - hit[1] < ttl:
                return hit[0]
        value = self._fetch(name)
        with self._lock:
            self._cache[name] = (value, self._clock())
        return value

    def headers(self, target, scope=None, config=None, **kwargs):
        specs = resolve_config('secret_headers', {**self.config, **(config or {})}) or []
        match = max((s for s in specs if str(target).startswith(str(s.get('target', '')))),
                    key=lambda s: len(str(s.get('target', ''))), default=None)
        if match is None:
            return {}
        value = self.secret(match['secret'])
        if match.get('json_key'):
            value = json.loads(value)[match['json_key']]
        return {match.get('header', 'Authorization'): f"{match.get('prefix', 'Bearer ')}{value}"}
