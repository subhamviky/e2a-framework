# ADR 0001: Probabilistic intelligence may propose; deterministic controls authorise, execute and confirm

- **Status:** Accepted
- **Date:** v1.0 (core thesis)
- **Deciders:** repository owner

## Context

Agentic systems put a model where enterprise systems used identity, policy, state and confirmation.

## Options considered

A) Let the agent act within a prompt's rules. B) Make the framework enforce the boundary: the agent proposes; stages authorise; tools execute with typed outcomes; a physical outcome is confirmed.

## Decision

B. `BaseAgent` runs screening stages, `_prepare`, deciding stages, `_apply_policy`, the model step, `_evaluate_output` or `_fallback`, then `_act` (only after an accepted evaluation) and `_confirm`.

## Consequences

Every project built on E2A inherits the safety order. Agents that need to do more than propose must go through `_act` and typed tool outcomes.
