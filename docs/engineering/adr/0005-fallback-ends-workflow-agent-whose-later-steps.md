# ADR 0005: A fallback ends a workflow; an agent whose later steps must run repairs its own state

- **Status:** Accepted
- **Date:** v1.2.0, documented in v1.11.0
- **Deciders:** repository owner

## Context

After a model reply fails evaluation, `_fallback` runs and the workflow stops with `evaluation = FALLBACK`.

## Options considered

A) Continue after a fallback. B) Stop: a degraded step must not feed later steps unnoticed; an agent that needs to continue repairs its own state in the hook.

## Decision

B (noted from the GCP cloud run, where the intent agent had to accept a rules-based reading to let the evidence step run).

## Consequences

Predictable failure semantics. Agents must be explicit when a degraded result is good enough to continue.
