# ADR 0008: One retry rule: `is_retryable`

- **Status:** Accepted
- **Date:** v1.9.1
- **Deciders:** repository owner

## Context

Each layer decided differently whether a failure could be resent.

## Options considered

A) Per-layer rules. B) `e2a.is_retryable(error)`: an exception that says so (a bool `retryable` attribute) is believed; otherwise a default by type.

## Decision

B; AI-DLC builds that cannot pass are no longer reported as retryable.

## Consequences

Callers and workflows agree on what may be retried.
