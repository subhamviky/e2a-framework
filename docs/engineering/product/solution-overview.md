# Solution overview

*One page.*

**The problem.** Teams adding agents to enterprise systems rebuild identity, tenancy, policy, state, retries and confirmation each time, and get the order wrong: a model that can act before it is authorised.

**What it is.** A vendor-neutral framework (Python package `e2a`, adapters for AWS, Google Cloud and Azure, and Java libraries) in which the model only proposes and deterministic stages authorise, execute and confirm. It supplies base agents and workflows, governance stages, idempotent claims, resumable approvals, ports for state and messaging, identity at ingress, an evaluation shape and a release gate, and a conformance checker that fails the build when a project bypasses the safety order.

**Evidence.** Four projects (settlement, reconciliation, order-to-cash, the GenAI assistant) are built on it and run on a real cloud; every adapter passes one contract kit; the CHANGELOG records what each cloud run verified and what it found (for example the claim defect fixed in v1.8.0 and the identity findings in v1.11.0).

**Limits.** One maintainer; adapters verified on emulators and mocks; no benchmark; managed providers are in a separate repository.
