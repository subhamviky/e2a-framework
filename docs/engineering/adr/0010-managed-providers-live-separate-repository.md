# ADR 0010: Managed providers live in a separate repository behind vendor-neutral contracts

- **Status:** Accepted
- **Date:** v1.6.0
- **Deciders:** repository owner

## Context

Provider SDKs and model catalogues change faster than the framework.

## Options considered

A) Bundle providers. B) The core defines contracts (types, refusals, bindings, profiles, capabilities); `e2a-managed-providers` implements them for Anthropic, OpenAI, Google and Meta, chosen by profile.

## Decision

B, with a neutrality test that the core names no vendor.

## Consequences

A provider change is a release of another repository; a project selects a provider by configuration.
