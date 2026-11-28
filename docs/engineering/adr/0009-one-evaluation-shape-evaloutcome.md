# ADR 0009: One evaluation shape: `EvalOutcome`

- **Status:** Accepted
- **Date:** v1.9.0
- **Deciders:** repository owner

## Context

Scores, thresholds and reasons were returned in different shapes and lost in logs.

## Options considered

A) Floats. B) `EvalOutcome(score, passed, threshold, reason, grader, evidence)`; every evaluation is recorded in `state['eval_outcome']` and as an `evaluation` message; `_evaluate_output` may return either.

## Decision

B (`e2a.evals`).

## Consequences

Evaluations are comparable and traceable; the release gate and live scorers reuse them.
