"""Firestore adapters for E2A's foundation ports (design note D-03).

    FirestoreIdempotencyStore   FirestoreTransactionalStore   FirestoreLease   FirestoreCheckpointStore

One document per key in one collection (default ``e2a``). Every change is ``create()``
(refused with AlreadyExists if the document exists) or ``update()`` with a
``last_update_time`` precondition (refused with FailedPrecondition if it changed since it was
read). Those refusals, NotFound on a precondition write, and ABORTED (Firestore's answer to
lock contention: the write was not applied) are the only errors read as a lost race, after
which the port reads again and re-decides; everything else is raised. The ``ttl`` field is a timestamp for a Firestore TTL
policy: garbage collection only, never correctness.

    from google.cloud import firestore
    from e2a_adapters.gcp import FirestoreIdempotencyStore
    store = FirestoreIdempotencyStore(firestore.Client())
"""
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from google.api_core import exceptions as gx
from google.cloud import firestore

from ..cas import CasBackend, CasCheckpointStore, CasIdempotencyStore, CasLease, CasTransactionalStore

__all__ = ['FirestoreBackend', 'FirestoreIdempotencyStore', 'FirestoreTransactionalStore', 'FirestoreLease',
           'FirestoreCheckpointStore']


def _doc_id(key: str) -> str:
    """Document ids cannot contain '/'; '%' is escaped first so the mapping stays one-to-one."""
    return 'e2a-' + key.replace('%', '%25').replace('/', '%2F')


class FirestoreBackend(CasBackend):
    max_value_bytes = 900_000          # a Firestore document is at most 1 MiB

    def __init__(self, client: 'firestore.Client', collection: str = 'e2a', ttl_field: str = 'ttl'):
        self.client, self.collection, self.ttl_field = client, collection, ttl_field

    def _ref(self, key: str):
        return self.client.collection(self.collection).document(_doc_id(key))

    def _with_ttl(self, doc, ttl_at):
        data = dict(doc)
        if ttl_at is not None:
            data[self.ttl_field] = datetime.fromtimestamp(ttl_at, timezone.utc)
        return data

    def get(self, key):
        snap = self._ref(key).get()
        if not snap.exists:
            return None
        doc = snap.to_dict()
        doc.pop(self.ttl_field, None)
        return doc, snap.update_time

    def create(self, key, doc, ttl_at=None):
        try:
            self._ref(key).create(self._with_ttl(doc, ttl_at))
            return True
        except (gx.AlreadyExists, gx.Aborted):
            return False

    def replace(self, key, doc, token, ttl_at=None):
        ref = self._ref(key)
        snap = ref.get()
        if not snap.exists or snap.update_time != token:
            return False
        updates = self._with_ttl(doc, ttl_at)
        for field in snap.to_dict():                     # whole-document replace: drop what is not in doc
            if field not in updates:
                updates[field] = firestore.DELETE_FIELD
        try:
            ref.update(updates, option=self.client.write_option(last_update_time=token))
            return True
        except (gx.FailedPrecondition, gx.NotFound, gx.Aborted):
            return False

    def delete(self, key):
        for _ in range(8):
            try:
                self._ref(key).delete(option=self.client.write_option(exists=True))
                return True
            except gx.NotFound:
                return False
            except gx.Aborted:                     # contention: not applied, try again
                continue
        raise RuntimeError(f'delete of {key!r} kept aborting under contention')


def _backend(client, collection, ttl_field):
    return FirestoreBackend(client, collection, ttl_field)


def _client_from(config):
    """v1.7.0: a Firestore client from configuration, created on first use. The emulator is
    selected by FIRESTORE_EMULATOR_HOST, as the SDK does by itself."""
    from e2a import resolve_config
    from .. import config_keys  # noqa: F401
    from .._lazy import Lazy
    project, database = resolve_config('gcp_project', config), resolve_config('firestore_database', config)
    return Lazy(lambda: firestore.Client(project=project, database=database)), \
        resolve_config('firestore_collection', config)


class _FromConfig:
    @classmethod
    def from_config(cls, config, **kwargs):
        client, collection = _client_from(config)
        return cls(client, collection, **kwargs)


class FirestoreIdempotencyStore(_FromConfig, CasIdempotencyStore):
    def __init__(self, client, collection: str = 'e2a', ttl_field: str = 'ttl', clock: Callable[[], float] = time.time):
        super().__init__(_backend(client, collection, ttl_field), clock)


class FirestoreTransactionalStore(_FromConfig, CasTransactionalStore):
    def __init__(self, client, collection: str = 'e2a', ttl_field: str = 'ttl', clock: Callable[[], float] = time.time):
        super().__init__(_backend(client, collection, ttl_field), clock)


class FirestoreLease(_FromConfig, CasLease):
    def __init__(self, client, collection: str = 'e2a', ttl_field: str = 'ttl', clock: Callable[[], float] = time.time):
        super().__init__(_backend(client, collection, ttl_field), clock)


class FirestoreCheckpointStore(_FromConfig, CasCheckpointStore):
    def __init__(self, client, collection: str = 'e2a', ttl_field: str = 'ttl', clock: Callable[[], float] = time.time,
                 ttl_seconds: Optional[int] = None):
        super().__init__(_backend(client, collection, ttl_field), clock, ttl_seconds)
