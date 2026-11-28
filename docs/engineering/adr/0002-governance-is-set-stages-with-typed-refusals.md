# ADR 0002: Governance is a set of stages, with typed refusals

- **Status:** Accepted
- **Date:** v1.2.0
- **Deciders:** repository owner

## Context

Capacity, token budget, sandbox, firewall, entitlement, approval and redaction were reimplemented per project.

## Options considered

A) Per-project code. B) `BaseGovernanceFramework` stages (capacity, budget, sandbox, firewall before anything reads the request; deciding stages such as approval after `_prepare`; redaction last) with `GovernanceRefusal` carrying `code`, `stage`, `reason`, `retryable`.

## Decision

B.

## Consequences

A caller can tell a refusal from a bug and whether it can be resent. Stages are rebuilt from policy on every request, which is what lets the assistant make governance per tenant.
