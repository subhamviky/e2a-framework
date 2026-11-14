"""GCP adapters for E2A's ports (design notes D-03, MP-05). Install e2a-adapters[gcp].

    Firestore*                         claims, rows, leases, checkpoints
    PubSubBroker                       messaging, with dead letters
    SecretManagerCredentialProvider    tool credentials from Secret Manager
"""
from .firestore import (FirestoreBackend, FirestoreCheckpointStore, FirestoreIdempotencyStore, FirestoreLease,
                        FirestoreTransactionalStore)
from .pubsub import PubSubBroker
from .secrets import SecretManagerCredentialProvider

__all__ = ['FirestoreBackend', 'FirestoreIdempotencyStore', 'FirestoreTransactionalStore', 'FirestoreLease',
           'FirestoreCheckpointStore', 'PubSubBroker', 'SecretManagerCredentialProvider']
