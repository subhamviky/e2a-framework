# Requirements and user stories

| # | As a... | I want... | So that... | Acceptance |
|---|---|---|---|---|
| E1 | service developer | a base agent whose model step cannot act without authorisation | a model never moves money | `_act` only after an accepted evaluation; stages first |
| E2 | service developer | governance (capacity, budget, firewall, approval, redaction) as configuration | I do not rewrite it per project | stages built from policy, per request |
| E3 | service developer | exactly-once execution with an idempotency claim | retries are safe | claim states (duplicate, deferred, resumed); a `BaseException` leaves the claim recoverable |
| E4 | service developer | the same code on AWS, Google Cloud and Azure | I choose the cloud by configuration | the port contract kit passes on every adapter |
| E5 | service developer | identity at ingress and between services | tenants and roles are verified | per-cloud sources, grants by configuration |
| E6 | project owner | a release gate and evaluation shape every project reuses | quality is comparable | `ProjectReleaseGate`, `EvalOutcome` |
| E7 | project owner | rules that stop subclasses bypassing the safety order | shortcuts fail the build | strict conformance in every project's CI |
| E8 | consumer | to pin a version and upgrade deliberately | nothing changes under me | tags, CHANGELOG, a deprecation release before a removal |

Out of scope: a hosted service, a UI, a model of its own.
