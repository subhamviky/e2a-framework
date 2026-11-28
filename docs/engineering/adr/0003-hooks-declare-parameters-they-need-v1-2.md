# ADR 0003: Hooks declare the parameters they need (the v1.2 signature); the v1.1 hooks are removed in v1.3.0

- **Status:** Accepted
- **Date:** v1.2.0 introduced, v1.3.0 removed
- **Deciders:** repository owner

## Context

Hooks took fixed argument lists; the request context (`ctx`) could not be added without breaking them.

## Options considered

A) Keep both forever. B) v1.2 hooks declare `state, ctx, io_read, ...`, receiving exactly the parameters they declare; v1.1 hooks keep working until v1.3.0, which removes them.

## Decision

B, with a deprecation release in between (v1.2.3 prepared for v1.3).

## Consequences

A breaking change with a documented migration; projects upgraded one at a time.
