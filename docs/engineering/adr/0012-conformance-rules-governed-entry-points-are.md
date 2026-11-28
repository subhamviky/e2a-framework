# ADR 0012: Conformance rules (governed entry points) are strict by default

- **Status:** Accepted
- **Date:** v1.2.2 introduced, v1.3.0 strict
- **Deciders:** repository owner

## Context

Projects subclassed framework classes in ways that bypassed the safety order (overriding public methods, calling protected hooks from outside).

## Options considered

A) Guidelines. B) `python -m e2a.conformance` checks R1 (no overriding or adding public methods on framework subclasses), R2, R3 (no calling protected hooks externally) and D1; R2 and D1 are errors from v1.3.0.

## Decision

B, run in every project's CI (`--strict`).

## Consequences

Design shortcuts fail the build: the assistant's per-tenant governance and public `respond()` were redesigned rather than waived.
