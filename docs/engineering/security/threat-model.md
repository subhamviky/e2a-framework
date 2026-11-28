# Threat model: e2a-framework

- **Status:** Reviewed 2026-11-28 · STRIDE for a framework: the risks are in how it is *used* and in what it lets a consumer get wrong.

| # | Threat | Control | Evidence |
|---|---|---|---|
| F1 | **Tampering**: a model-driven action bypasses authorisation | the safety order is built into `BaseAgent.run`; `_act` runs only after an accepted evaluation and not after the deadline; conformance rules R1 to R3 stop subclasses from bypassing it | `reference/`, `e2a.conformance`, `tests/test_conformance.py` |
| F2 | **Tampering**: a step reported done without its effects | the claim is left IN_PROGRESS when a `BaseException` escapes (v1.8.0) | ADR 0006 |
| F3 | **Spoofing**: a caller's tenant or role taken from headers | identity at ingress per cloud; `gcp_cloud_run` reads the verified token; grants by configuration; fail-closed | v1.7.4, v1.11.0 |
| F4 | **Information disclosure**: personal data or card numbers in model output | egress DLP and redaction stages (card numbers, not identifiers: v1.7.5) | `tests/` |
| F5 | **Information disclosure**: cross-tenant state | tenant is part of every store key in the contract; the port contract kit includes tenant isolation | `e2a.port_contract` |
| F6 | **Denial of service**: unbounded in-flight work or tokens | capacity and token-budget stages; model-call retries are bounded | v1.5.1, v1.2.0 |
| F7 | **Repudiation**: no record of what the agent did | `state['trajectory']`, `eval_outcome`, a message log with the correlation id on every step | v1.9.0 |
| F8 | **Elevation**: an adapter that does not honour the port contract (a lost update, a double claim) | every adapter must pass the port contract kit, including races, on the real emulators | CI `adapters` job |

Residual: dependency and supply-chain risk is managed by pinning tags and running CI, not by signing or an SBOM policy; a consuming project must run the conformance kit to benefit from the safety order; there is no penetration test.
