# e2a-java — the E2A runtime for Java

Design: design note D-01 (o2c-platform-hub, `roadmap/design/`). Released with E2A's tag, at E2A's version (`1.2.2` first).

| Module | What it gives you |
|---|---|
| `e2a-core` | `RequestContext`, `Deadline`, `PhysicalOutcome`; `IdempotencyStore` (+ `InMemoryIdempotencyStore`); `ToolService` with `ReadAccess` / `ModifyAccess`; governance `Stage`s (`CapacityStage`, `FirewallStage`, `ApprovalStage`) and `Governance`; `LlmService` (+ `SimulatedLlmService`); `BaseAgent` with the v1.2 lifecycle; `BaseWorkflow` with `Steps` / `Step` / `Route` and one claim per request |
| `e2a-cqrs` | The Deterministic CQRS profile: `BaseOrchestrator`, `BaseCommandService`, `BaseQueryService` |
| `e2a-spring-jpa` | `JpaIdempotencyStore` on the `idempotency_record` table (`com/subhamviky/e2a/jpa/schema.sql`), auto-configured for Spring Boot 3.5 |
| `e2a-test` | `AgentContract`, a JUnit 5 kit for the guarantees every agent keeps |

## The agent lifecycle

`BaseAgent.run(state, ctx, config)` is `final`:

```text
screen stages → prepare(io_read) → decide stages → applyPolicy → buildMessages (null = no model step)
→ model → evaluateOutput ≥ min_confidence, else fallback → act(io_modify) → confirm → state.outcome
```

Effects happen only in `act`, through `io_modify` (material tools, always with a key), after an
accepted evaluation and before the deadline. `run` never throws on a hook error: a refusal goes to
`state.refusal`, an error to `state.errors`. Entry points are `final` and hooks `protected`, so the
compiler enforces what `e2a.conformance` checks in Python (R1, R2).

## Using it

There is no package registry: build it from E2A's tag in your CI, before your own build.

```yaml
- uses: actions/checkout@v6
  with:
    repository: subhamviky/e2a-framework
    ref: v1.2.2
    path: .e2a
- run: mvn -B -q -f .e2a/java/pom.xml install -DskipTests
```

```xml
<dependency>
    <groupId>com.subhamviky.e2a</groupId>
    <artifactId>e2a-core</artifactId>   <!-- or e2a-cqrs, e2a-spring-jpa; e2a-test with <scope>test</scope> -->
    <version>1.2.2</version>
</dependency>
```

Not in this release: `resume()` and checkpoints, RAG ports, the inference gateway's own concerns
(quota, caching, DLP). A Java service calls a gateway through `LlmService`.
