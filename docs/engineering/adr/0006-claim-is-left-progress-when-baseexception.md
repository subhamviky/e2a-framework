# ADR 0006: A claim is left IN_PROGRESS when a BaseException escapes a step

- **Status:** Accepted
- **Date:** v1.8.0
- **Deciders:** repository owner

## Context

The workflow settled the claim in a `finally` block, so a `BaseException` (such as a cancellation) completed the claim as SUCCEEDED.

## Options considered

A) Keep. B) Leave the claim IN_PROGRESS so it can be recovered.

## Decision

B.

## Consequences

Recovery can finish what a cancelled step did not; no step is reported done without its effects.
