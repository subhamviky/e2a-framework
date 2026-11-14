"""Cosmos DB (NoSQL API) adapters for E2A's foundation ports (design note D-03).

    CosmosIdempotencyStore   CosmosTransactionalStore   CosmosLease   CosmosCheckpointStore

One item per key in one container, whose partition key is the item id (``/pk`` by default), so
each key is its own logical partition. Every change is ``create_item()`` (refused with
CosmosResourceExistsError, HTTP 409) or ``replace_item()`` with the ETag that was read and
``IfNotModified`` (refused with CosmosAccessConditionFailedError, HTTP 412). Those refusals, and
a NotFound on replace, are the only errors read as a lost race; everything else is raised.
Reads go to the item directly (strong for a single region; for multi-region accounts use
Strong or Bounded Staleness consistency). The ``ttl`` field is Cosmos DB's per-item TTL in
seconds: garbage collection only, and only when the container has TTL enabled (default_ttl -1).

    from azure.cosmos import CosmosClient
    from e2a_adapters.azure import CosmosIdempotencyStore
    container = CosmosClient(url, credential).get_database_client('db').get_container_client('e2a')
    store = CosmosIdempotencyStore(container)
"""
import hashlib
import time
from typing import Callable, Optional

from azure.core import MatchConditions
from azure.cosmos import exceptions as cx

from ..cas import CasBackend, CasCheckpointStore, CasIdempotencyStore, CasLease, CasTransactionalStore

__all__ = ['CosmosBackend', 'CosmosIdempotencyStore', 'CosmosTransactionalStore', 'CosmosLease',
           'CosmosCheckpointStore']

_SYSTEM = ('id', '_rid', '_self', '_etag', '_attachments', '_ts', 'ttl', 'e2a_key')


def _item_id(key: str) -> str:
    """Cosmos ids cannot contain '/', '\\\\', '?' or '#' and are at most 255 characters."""
    safe = key.replace('%', '%25').replace('/', '%2F').replace('\\', '%5C').replace('?', '%3F').replace('#', '%23')
    return safe if len(safe) <= 200 else 'h-' + hashlib.sha256(key.encode()).hexdigest()


class CosmosBackend(CasBackend):
    max_value_bytes = 1_800_000        # a Cosmos DB item is at most 2 MB

    def __init__(self, container, partition_key_field: str = 'pk', wall_clock: Callable[[], float] = time.time):
        self.container, self.pk = container, partition_key_field
        self.wall_clock = wall_clock   # Cosmos TTL is relative to the write: real time, GC only

    def _body(self, key, doc, ttl_at):
        item_id = _item_id(key)
        body = {**doc, 'id': item_id, self.pk: item_id, 'e2a_key': key}
        if ttl_at is not None:
            body['ttl'] = max(1, int(ttl_at - self.wall_clock()) + 1)
        return item_id, body

    def get(self, key):
        item_id = _item_id(key)
        try:
            item = self.container.read_item(item_id, partition_key=item_id)
        except cx.CosmosResourceNotFoundError:
            return None
        etag = item['_etag']
        return {k: v for k, v in item.items() if k not in _SYSTEM and k != self.pk}, etag

    def create(self, key, doc, ttl_at=None):
        _, body = self._body(key, doc, ttl_at)
        try:
            self.container.create_item(body)
            return True
        except cx.CosmosResourceExistsError:
            return False

    def replace(self, key, doc, token, ttl_at=None):
        item_id, body = self._body(key, doc, ttl_at)
        try:
            self.container.replace_item(item_id, body, etag=token, match_condition=MatchConditions.IfNotModified)
            return True
        except (cx.CosmosAccessConditionFailedError, cx.CosmosResourceNotFoundError):
            return False

    def delete(self, key):
        item_id = _item_id(key)
        try:
            self.container.delete_item(item_id, partition_key=item_id)
            return True
        except cx.CosmosResourceNotFoundError:
            return False


def _container_from(config):
    """v1.7.0: the Cosmos DB container from configuration, created on first use: an account key
    when one is set (the emulator), else Entra ID through the platform identity."""
    from e2a import resolve_config
    from .. import config_keys  # noqa: F401
    from .._lazy import Lazy
    endpoint, key = resolve_config('cosmos_endpoint', config), resolve_config('cosmos_key', config)
    database, container = resolve_config('cosmos_database', config), resolve_config('cosmos_container', config)

    def build():
        from azure.cosmos import CosmosClient
        if key:
            credential = key
        else:
            from azure.identity import DefaultAzureCredential
            credential = DefaultAzureCredential()
        return CosmosClient(endpoint, credential=credential).get_database_client(database) \
            .get_container_client(container)
    return Lazy(build)


class _FromConfig:
    @classmethod
    def from_config(cls, config, **kwargs):
        return cls(_container_from(config), **kwargs)


class CosmosIdempotencyStore(_FromConfig, CasIdempotencyStore):
    def __init__(self, container, partition_key_field: str = 'pk', clock: Callable[[], float] = time.time):
        super().__init__(CosmosBackend(container, partition_key_field), clock)


class CosmosTransactionalStore(_FromConfig, CasTransactionalStore):
    def __init__(self, container, partition_key_field: str = 'pk', clock: Callable[[], float] = time.time):
        super().__init__(CosmosBackend(container, partition_key_field), clock)


class CosmosLease(_FromConfig, CasLease):
    def __init__(self, container, partition_key_field: str = 'pk', clock: Callable[[], float] = time.time):
        super().__init__(CosmosBackend(container, partition_key_field), clock)


class CosmosCheckpointStore(_FromConfig, CasCheckpointStore):
    def __init__(self, container, partition_key_field: str = 'pk', clock: Callable[[], float] = time.time,
                 ttl_seconds: Optional[int] = None):
        super().__init__(CosmosBackend(container, partition_key_field), clock, ttl_seconds)
