"""GCP adapters for E2A's ports (design notes D-03, MP-05). Install e2a-adapters[gcp].

    Firestore*                         claims, rows, leases, checkpoints
    PubSubBroker                       messaging, with dead letters
    PubSubPushDelivery                 one Pub/Sub push request as a broker (v1.10.0)
    FirestoreOutboxStore               the transactional outbox (v1.7.1)
    SecretManagerCredentialProvider    tool credentials from Secret Manager
"""
from .firestore import (FirestoreBackend, FirestoreCheckpointStore, FirestoreIdempotencyStore, FirestoreLease,
                        FirestoreTransactionalStore)
from .outbox import FirestoreOutboxStore
from .pubsub import PubSubBroker, PubSubPushDelivery
from .secrets import SecretManagerCredentialProvider

__all__ = ['FirestoreBackend', 'FirestoreIdempotencyStore', 'FirestoreTransactionalStore', 'FirestoreLease',
           'FirestoreCheckpointStore', 'PubSubBroker', 'PubSubPushDelivery', 'SecretManagerCredentialProvider', 'FirestoreOutboxStore']
