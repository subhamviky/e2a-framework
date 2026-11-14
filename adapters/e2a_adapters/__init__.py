"""e2a-adapters: E2A's ports on the clouds' infrastructure (design notes D-02, D-03, MP-05).

Each cloud is an optional extra and a subpackage, so a project installs only what it uses:

    e2a_adapters.aws     DynamoDB, SQS/SNS, Secrets Manager   (pip install "e2a-adapters[aws]")
    e2a_adapters.gcp     Firestore, Pub/Sub, Secret Manager   (pip install "e2a-adapters[gcp]")
    e2a_adapters.azure   Cosmos DB, Service Bus, Key Vault    (pip install "e2a-adapters[azure]")
    e2a_adapters.cas     the ports over any document store with optimistic concurrency
    e2a_adapters.identity  claim maps of Cognito, Entra ID and Identity Platform

v1.7.0: every adapter has from_config(config), and the cloud profiles `aws`, `gcp` and `azure`
bind them (bindings_from_profile("aws")); a provider profile extends one. Every adapter passes
E2A's port contract kit (e2a.port_contract), the brokers its messaging suite, on emulators.
No AI vendor SDK is imported here (tests/test_boundary.py).
"""
from . import config_keys  # noqa: F401  (registers the cloud keys)

__version__ = '1.7.1'
