# ADR 0011: e2a-java is versioned equal to the E2A tag

- **Status:** Accepted
- **Date:** v1.9.1
- **Deciders:** repository owner

## Context

A generated Java service builds against `e2a-java` from the tag of the same number; v1.9.0 had left it at 1.8.0.

## Options considered

A) Independent versions. B) The Java library's version equals the E2A tag.

## Decision

B.

## Consequences

A consumer pins one number. Java is built and tested in CI (`mvn verify` for core, cqrs, identity, spring-jpa and test modules).
