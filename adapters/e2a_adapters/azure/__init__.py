"""Azure adapters for E2A's foundation ports (design note D-03). Needs azure-cosmos:
install e2a-adapters[azure]."""
from .cosmos import (CosmosBackend, CosmosCheckpointStore, CosmosIdempotencyStore, CosmosLease,
                     CosmosTransactionalStore)

__all__ = ['CosmosBackend', 'CosmosIdempotencyStore', 'CosmosTransactionalStore', 'CosmosLease',
           'CosmosCheckpointStore']
