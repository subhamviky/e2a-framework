"""The transactional outbox on Firestore (v1.7.1, design note MP-05, N1).

A record is written in the SAME transaction as the business change: add(record,
transaction=tx) inside the application's Firestore transaction (or a WriteBatch, batch=b).
mark_published() deletes the record and advances the fence in one atomic write with
preconditions (v1.7.3: not a lock-based transaction), so a stale sweeper cannot mark.

Layout:
    <firestore_collection>-outbox-<outbox_name>/<record id>    a pending record
    <firestore_collection>-outbox-fence/<outbox_name>          the newest fencing token seen

pending() orders by `created_at` (a single-field index Firestore keeps by itself) and then by
document id, so no composite index is needed.
"""
import json
import random
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from e2a import resolve_config
from e2a.messaging import BaseOutboxStore
from google.api_core import exceptions as gx
from google.cloud import firestore

from .. import config_keys  # noqa: F401
from .._lazy import Lazy


_CONTENTION_ROUNDS = 32


class FirestoreOutboxStore(BaseOutboxStore):
    capabilities = frozenset({'outbox_store'})

    def __init__(self, client, collection: str = 'e2a', name: str = 'default',
                 clock: Callable[[], float] = time.time) -> None:
        self.client, self.name, self.clock = client, name, clock
        self.records, self.fences = f'{collection}-outbox-{name}', f'{collection}-outbox-fence'

    @classmethod
    def from_config(cls, config: Dict[str, Any], **kwargs) -> 'FirestoreOutboxStore':
        project, database = resolve_config('gcp_project', config), resolve_config('firestore_database', config)
        return cls(Lazy(lambda: firestore.Client(project=project, database=database)),
                   resolve_config('firestore_collection', config), resolve_config('outbox_name', config), **kwargs)

    def _new_id(self) -> str:
        return f'{int(self.clock() * 1_000_000):017d}-{uuid.uuid4().hex[:12]}'

    def add(self, record, config=None, **kwargs):
        """kwargs: transaction= (a firestore.Transaction) or batch= (a WriteBatch) to commit the
        record with the business change; without either, the record is written on its own."""
        rid = record.get('id') or self._new_id()
        doc = {'created_at': self.clock(),
               'record': json.dumps({k: record.get(k) for k in ('destination', 'body', 'attributes', 'tenant_id',
                                                               'idempotency_key')},
                                    separators=(',', ':'), ensure_ascii=False)}
        ref = self.client.collection(self.records).document(rid)
        writer = kwargs.get('transaction') or kwargs.get('batch')
        if writer is not None:
            writer.create(ref, doc)
        else:
            ref.create(doc)
        return rid

    def pending(self, limit=100, config=None, **kwargs):
        query = self.client.collection(self.records).order_by('created_at').order_by('__name__').limit(max(1, int(limit)))
        out: List[Dict[str, Any]] = []
        for snap in query.stream():
            out.append({'id': snap.id, **json.loads(snap.get('record'))})
        return out

    def mark_published(self, record_id, config=None, **kwargs):
        """Delete the record, and with a fencing token advance the fence, in ONE atomic write
        with preconditions (no lock-based transaction: under many concurrent sweepers those
        abort each other). The record's delete requires it to exist, so exactly one sweeper
        wins; the fence's write requires the fence to be unchanged since it was read."""
        token: Optional[int] = kwargs.get('fencing_token')
        ref = self.client.collection(self.records).document(record_id)
        fence_ref = self.client.collection(self.fences).document(self.name)
        for _ in range(_CONTENTION_ROUNDS):
            batch = self.client.batch()
            if token is not None:
                fence = fence_ref.get()
                if fence.exists and int(fence.get('fence')) > int(token):
                    return False                         # a newer sweeper exists: this one is stale
                if fence.exists:
                    batch.update(fence_ref, {'fence': int(token)},
                                 option=self.client.write_option(last_update_time=fence.update_time))
                else:
                    batch.create(fence_ref, {'fence': int(token)})
            batch.delete(ref, option=self.client.write_option(exists=True))
            try:
                batch.commit()
                return True
            except gx.NotFound:
                return False                             # already published (the fence is never deleted)
            except (gx.FailedPrecondition, gx.AlreadyExists, gx.Aborted):
                time.sleep(0.01 * (1 + random.random()))  # the fence moved, or contention: read again
        raise RuntimeError(f'mark_published({record_id!r}) kept losing to concurrent sweepers')
