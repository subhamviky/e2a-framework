# Release process: e2a-framework

1. **Branch and pull request.** CI (`ci`): the test suite (pytest runs every smoke script against the *installed* package); the Governed Entry Point rules over the framework and reference code (`python -m e2a.conformance --strict`); the smoke tests against the installed package; the adapter tests (the port contract kit and the messaging suite) on every backend with the emulators started (DynamoDB Local, Firestore, Cosmos DB, Pub/Sub, Service Bus); the conformance rules over the adapters; and `mvn verify` for e2a-core, e2a-cqrs, e2a-identity, e2a-spring-jpa and e2a-test.
2. **Merge to `main`.**
3. **Tag `vX.Y.Z`.** Consumers pin the tag (`pip install "e2a-framework @ git+https://github.com/subhamviky/e2a-framework.git@vX.Y.Z"`, Poetry `tag = "vX.Y.Z"`); `e2a-adapters` is a separate distribution at the same tag; **e2a-java's version equals the tag** (ADR 0011). A tag is never moved.
4. **CHANGELOG entry** per version: what was added (additive by default), what changed, what was fixed, and what was verified in a real run ("Verified in the cloud").
5. **Documents after the tag** are updated (design documents, playbooks) in a docs-only follow-up when needed (v1.2.1, v1.9.1 did this).

**Versioning.** Additive changes in a minor version; a breaking change is announced one release earlier and removed in a later one (v1.2.3 prepared v1.3.0). Patch versions fix defects found by consumers (v1.1.1, v1.1.2 came from the order-to-cash projects).

**Rollback.** Consumers repin the previous tag.
