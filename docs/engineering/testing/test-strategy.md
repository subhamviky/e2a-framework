# Test strategy: e2a-framework

| Layer | What it proves | Where |
|---|---|---|
| Unit and contract | agents, workflow steps and `resume()`, governance stages, entry points, the release gate, v1.5.1 to v1.9.0 features (one test module per version), neutrality (the core names no vendor) | `tests/` |
| Smoke scripts against the installed package | the packaged framework works, not just the checkout | CI step |
| Conformance (strict) | R1, R2, R3, D1 over the framework, the reference code and the adapters | `python -m e2a.conformance` |
| Port contract kit | every adapter (DynamoDB, Firestore, Cosmos DB) honours idempotency, transactions, leases, checkpoints, tenant isolation and races | `e2a.port_contract`, `adapters/tests` on the emulators |
| Messaging suite | brokers (SQS/SNS, Pub/Sub, Service Bus) and consumers (push delivery, resumable handlers) | `adapters/tests` |
| Java | core, cqrs, identity, spring-jpa and test modules | `mvn verify` |
| Real runs | each cloud-facing feature is "verified in the cloud" in the CHANGELOG (for example Pub/Sub push and Cloud Run identity on 25 November 2026) | consuming projects' cloud scenarios |

Gaps: no performance benchmarks; no mutation testing; Azure and AWS adapters are verified on emulators and mocks, not on real accounts.
