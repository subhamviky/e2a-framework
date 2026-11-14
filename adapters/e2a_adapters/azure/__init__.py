"""Azure adapters for E2A's ports (design notes D-03, MP-05). Install e2a-adapters[azure].

    Cosmos*                      claims, rows, leases, checkpoints
    ServiceBusBroker             messaging, with native dead letters
    CosmosOutboxStore            the transactional outbox (v1.7.1)
    KeyVaultCredentialProvider   tool credentials from Key Vault
"""
from .cosmos import (CosmosBackend, CosmosCheckpointStore, CosmosIdempotencyStore, CosmosLease,
                     CosmosTransactionalStore)
from .keyvault import KeyVaultCredentialProvider
from .outbox import CosmosOutboxStore
from .servicebus import ServiceBusBroker

__all__ = ['CosmosBackend', 'CosmosIdempotencyStore', 'CosmosTransactionalStore', 'CosmosLease',
           'CosmosCheckpointStore', 'ServiceBusBroker', 'KeyVaultCredentialProvider', 'CosmosOutboxStore']
