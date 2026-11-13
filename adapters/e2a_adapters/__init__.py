"""e2a-adapters: production adapters for E2A's foundation ports (design note D-02).

Each backend is an optional extra and a subpackage, so a project installs only what it uses:

    e2a_adapters.aws     DynamoDB   (pip install "e2a-adapters[aws]")
    e2a_adapters.gcp     Firestore  (pip install "e2a-adapters[gcp]")
    e2a_adapters.azure   Cosmos DB  (pip install "e2a-adapters[azure]")
    e2a_adapters.cas     the ports over any document store with optimistic concurrency, which
                         Firestore and Cosmos DB use (and a new backend can too)

Every adapter passes E2A's port contract kit (e2a.port_contract), the same suite the in-memory
reference adapters pass.
"""
__version__ = '1.5.0'
