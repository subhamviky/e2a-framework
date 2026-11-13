"""GCP adapters for E2A's foundation ports (design note D-03). Needs google-cloud-firestore:
install e2a-adapters[gcp]."""
from .firestore import (FirestoreBackend, FirestoreCheckpointStore, FirestoreIdempotencyStore, FirestoreLease,
                        FirestoreTransactionalStore)

__all__ = ['FirestoreBackend', 'FirestoreIdempotencyStore', 'FirestoreTransactionalStore', 'FirestoreLease',
           'FirestoreCheckpointStore']
