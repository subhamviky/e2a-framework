# Architecture Decision Records

One record per significant decision (context, options, decision, consequences). Records are not edited to change history: a reversed decision gets a new record that supersedes the old one. These are the decisions of the E2A framework and its adapters and Java libraries. Platform-wide decisions (a real cloud rather than emulators only, synthetic data, Workload Identity Federation, promotion, the documentation layout) are in the [assistant repository's ADR index](https://github.com/subhamviky/enterprise-finance-genai-assistant/blob/main/docs/engineering/adr/README.md).

Template: [`0000-template.md`](0000-template.md).

| # | Decision | Status |
|---|---|---|
| [0001](0001-probabilistic-intelligence-may-propose.md) | Probabilistic intelligence may propose; deterministic controls authorise, execute and confirm | Accepted |
| [0002](0002-governance-is-set-stages-with-typed-refusals.md) | Governance is a set of stages, with typed refusals | Accepted |
| [0003](0003-hooks-declare-parameters-they-need-v1-2.md) | Hooks declare the parameters they need (the v1.2 signature); the v1.1 hooks are removed in v1.3.0 | Accepted |
| [0004](0004-ports-adapters-every-adapter-passes-port.md) | Ports and adapters, and every adapter passes the port contract kit | Accepted |
| [0005](0005-fallback-ends-workflow-agent-whose-later-steps.md) | A fallback ends a workflow; an agent whose later steps must run repairs its own state | Accepted |
| [0006](0006-claim-is-left-progress-when-baseexception.md) | A claim is left IN_PROGRESS when a BaseException escapes a step | Accepted |
| [0007](0007-identity-is-read-at-ingress-per-cloud-with.md) | Identity is read at ingress per cloud, with grants by configuration | Accepted |
| [0008](0008-one-retry-rule-is-retryable.md) | One retry rule: `is_retryable` | Accepted |
| [0009](0009-one-evaluation-shape-evaloutcome.md) | One evaluation shape: `EvalOutcome` | Accepted |
| [0010](0010-managed-providers-live-separate-repository.md) | Managed providers live in a separate repository behind vendor-neutral contracts | Accepted |
| [0011](0011-e2a-java-is-versioned-equal-e2a-tag.md) | e2a-java is versioned equal to the E2A tag | Accepted |
| [0012](0012-conformance-rules-governed-entry-points-are.md) | Conformance rules (governed entry points) are strict by default | Accepted |
