# ADR 0004: Ports and adapters, and every adapter passes the port contract kit

- **Status:** Accepted
- **Date:** v1.4.0
- **Deciders:** repository owner

## Context

State, leases, claims, checkpoints, brokers and secrets must run on AWS, Google Cloud and Azure without the core knowing.

## Options considered

A) Cloud-specific code in the core. B) Ports in the core (`BaseTransactionalStore`, idempotency, lease, checkpoint, `BaseMessageBroker`, secret stores); adapters in a separate distribution `e2a-adapters` (DynamoDB v1.4.0; Firestore and Cosmos DB v1.5.0; brokers and secrets v1.7.0), each passing `e2a.port_contract`.

## Decision

B.

## Consequences

A new backend is a contract away from supported. The adapters need the emulators in CI (DynamoDB Local, Firestore, Cosmos DB, Pub/Sub, Service Bus).
