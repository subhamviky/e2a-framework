# E2A CQRS Implementation Playbook

### Class Contracts & Scaffold Source — Deterministic CQRS Profile

| | |
|---|---|
| Document Version | 1.0.0 |
| Author | Subham Gupta, Principal Platform Architect |
| Classification | Architecture Reference — Class Contracts & Scaffold Code, Deterministic CQRS Profile |
| Companion document | [CQRS_CLOUD_LANDING_ZONE.md](CQRS_CLOUD_LANDING_ZONE.md) — the infrastructure and topology this scaffold deploys onto |
| Parent reference | Implementation Playbook (agentic) — method signatures for `BaseValidationService`, reused unmodified |
| Scope | Method signatures, propagation contract, and runnable scaffold for `BaseOrchestrator`, `BaseCommandService`, and `BaseQueryService`, plus the abstract-class Observability/Governance contract this profile requires. |

## 1. Purpose & Relationship to the Base Playbook

> **E2A v1.2.** The CQRS profile's classes and this playbook's contracts are unchanged in v1.2.0. The shared contracts it imports (`resolve_config`, `Deadline`, claims, stores, outcomes) now live in the `e2a` package and reach `e2a_cqrs_base.py` through `e2a_base`, which re-exports them unchanged. The agentic-core additions (request context, governance stages, declared steps, `resume()`) do not apply here; `python -m e2a.conformance` can still check a CQRS project. See the E2A CQRS Design Document, Section 16.


This document carries the method-level contract and runnable scaffold for the three classes that are new in the CQRS profile — `BaseOrchestrator`, `BaseCommandService`, `BaseQueryService` — plus the abstract-class Observability/Governance contract this profile depends on. `BaseValidationService` is reused from the base Implementation Playbook without modification and is not repeated here beyond the entry-point table in Section 2.

> **Note:** `BaseObservability` and `BaseGovernanceFramework` below are CQRS-profile classes, not the agentic profile's classes reused verbatim. The abstract-class contract (`ABC`, the same `@abstractmethod` hook names) is identical to `e2a_base.py` — that part is intentionally unchanged so both profiles are pluggable behind the same `config['observability_engine']` / `config['governance_engine']` interface. What changes is the concern each hook enforces: LLM token budgets and MCP tool sandboxing are replaced with tenant request quotas and external-API-call isolation, since no LLM call and no MCP tool call ever occurs on this profile's request path.

> **Configuration is central.** This profile adds no config keys of its own: it imports resolve_config() from e2a_base.py and reads every key from the single registry, E2A_CONFIG (Implementation Playbook Section 5), in the same order as the agentic profile — kwargs, then config, then environment variable, then default. The one CQRS-specific default is max_latency = 1.0 s (registry scope 'CQRS'), tighter than the agentic 2.0 s.

> **Concurrency is owned by invariant.** Each shared-resource risk on the command and query paths — duplicate commands, lost updates, stale writers, cache stampedes, overload, work orphaned after a timeout — is assigned to exactly one existing class, contract or config key; there is no global concurrency manager. The guarantee is effectively-once material effects under at-least-once delivery, never exactly-once. The shared contracts (`BaseIdempotencyStore`, `BaseTransactionalStore`, `BaseCacheStore`, `Deadline`, `ConcurrencyConflict`, `CommitResult`, `PhysicalOutcome`) live in `e2a_base.py` and are imported by `e2a_cqrs_base.py`; Implementation Playbook Section 2.19 defines them. This document shows how the three CQRS classes use them (Sections 5–7), the consumer model that sits behind the outbox (Section 12), the scheduled repair path (Section 13) and the race and saturation tests (Section 14).

## 2. The Single Public Entry Point Rule

> **v1.2: the Governed Entry Point principle.** Callers reach a class only through its public methods; each public method is a distinct capability (its own caller or trigger, its own guarantees, not a shortcut to a hook) that delegates to protected hooks and framework-owned private methods. Most classes have one public method; some have two or three (`BaseWorkflow.execute` / `resume`, `BaseMCPServer.serve` / `serve_async`, `BaseRAGPipeline.retrieve` / `evaluate_answer`). `python -m e2a.conformance` enforces it: R1 (no override of a framework-owned public or private method) fails; R2 (no call to another class's protected hook) warns in v1.2 and fails from v1.3; R3 (an extra public capability) is a review note.

| Class | Public Entry Point |
|---|---|
| `BaseOrchestrator` | `execute(state, config=None, **kwargs) -> dict` |
| `BaseCommandService` | `mutate(payload, config=None, **kwargs) -> dict` |
| `BaseQueryService` | `fetch(query_params, config=None, **kwargs) -> dict` |
| `BaseObservability` | `record_telemetry(message_log, correlation_id, config=None, **kwargs) -> None` |
| `BaseGovernanceFramework` | `enforce_governance_gate(state, io_config, config=None, **kwargs) -> dict` |
| `BaseValidationService` | `validate(agent_name, state, config=None, **kwargs) -> ValidationResult` (reused unmodified) |

## 3. Cross-Class Propagation Fields

The same six fields from the base framework, none stored as `self.` attributes for the same concurrency-safety reason. Origins shift slightly because `BaseOrchestrator` replaces `BaseWorkflow` as the field-minting point.

| Field | Direction | Origin | Purpose |
|---|---|---|---|
| `correlation_id` | INPUT | `BaseValidationService.validate()` — unchanged | Ties every log line, metric, and trace across both the Command and Query paths back to one logical transaction. |
| `tenant_id` | INPUT | Extracted from the JWT claim at the API Gateway — unchanged | Enforces the cache-key namespace and the database partition/silo routing. |
| `idempotency_key` | INPUT | Intercepted or minted in `BaseOrchestrator.execute()` via `_generate_idempotency_key()` | Drives the commit gate on the Command path only — the Query path never mints or checks one. |
| `io_config` | INPUT | Resolved in `BaseOrchestrator.execute()` from the `IO_CONFIG` namespace — unchanged | Separates cache/connection endpoint configuration from behavioral NFR configuration. |
| `message_log` | OUTPUT | Empty list at request start; both `BaseCommandService` and `BaseQueryService` append structured entries | One ordered log per request, flushed once to `BaseObservability`. |
| `failed_keys` | OUTPUT | Empty list at request start; populated on the Command path only | Drives the commit gate and the Saga engine's DLQ split — never forwarded from the Query path. |

Three further fields are additive. None changes the six above and none is stored as a `self.` attribute.

| Field | Direction | Origin | Purpose |
|---|---|---|---|
| `deadline` | INPUT | `BaseOrchestrator.execute()` — from an inbound `Deadline` (or remaining milliseconds on the `x-e2a-deadline-ms` header), else from `request_deadline_seconds`; absent when neither is set | Bounds every wait on both paths. Locally an absolute monotonic instant; across a process boundary it travels as remaining milliseconds. `BaseCommandService.mutate()` and `BaseQueryService.fetch()` check it before starting work; the cache single-flight wait is bounded by it. |
| `expected_version` | INPUT | Caller-supplied (kwargs or `state`) | The version the caller read. The advisory fast-fail in `_commit_gate()` compares it; the authority is the store's atomic `put_if_version` in `_commit_versioned()`. |
| `fencing_token` | INPUT | Caller-supplied when it holds a lease (kwargs or `state`) | Passed through to `put_if_version` so the store can reject a writer whose lease has been superseded. Needed only where a stale writer could otherwise commit. |

## 4. Abstract-Class Foundation: BaseObservability & BaseGovernanceFramework

Both classes are imported from `reference/e2a_cqrs_base.py`. They are reproduced here in full because they are the specific change this profile standardizes on: abstract base classes (`ABC`) with `@abstractmethod` hooks. A subclass that omits any hook below fails at instantiation time (`TypeError: Can't instantiate abstract class ... with abstract method ...`) rather than shipping a no-op integration to production.

### 4.1 BaseObservability(ABC) — CQRS Profile

```python
class BaseObservability(ABC):
    """Template method abstract class for CQRS Service Telemetry."""

    def record_telemetry(self, message_log, correlation_id,
                          config=None, **kwargs) -> None:
        config = config or {}
        enriched_logs = []
        for entry in (message_log or []):
            enriched_logs.append({
                **entry,
                'correlation_id': correlation_id,
                'tenant_id': kwargs.get('tenant_id', 'UNKNOWN_TENANT'),
                'host_epoch_ms': int(time.time() * 1000),
                'framework_version': 'e2a-cqrs-v1.0',
            })
        metrics = self.__extract_metrics(enriched_logs)
        try:
            self._ship_logs(enriched_logs, config, **kwargs)
            if metrics:
                self._emit_metrics(metrics, config, **kwargs)
            self._export_traces(correlation_id, enriched_logs, config, **kwargs)
        except Exception as e:
            logging.error(f'Telemetry dispatch failed for {correlation_id}: {e}')
            for log in enriched_logs:
                logging.info(log)

    def __extract_metrics(self, enriched_logs) -> dict:
        # CQRS-relevant counters, derived from the _log() events emitted
        # by BaseOrchestrator / BaseCommandService / BaseQueryService.
        # No token/cost metrics on this profile — there is no LLM call.
        metrics = {
            'command_count': 0, 'query_count': 0,
            'cache_hit_count': 0, 'cache_miss_count': 0,
            'error_count': 0,
        }
        for log in enriched_logs:
            event = log.get('event')
            if log.get('level') == 'ERROR':
                metrics['error_count'] += 1
            if event == 'routing_to_command_service':
                metrics['command_count'] += 1
            elif event == 'routing_to_query_service':
                metrics['query_count'] += 1
            elif event == 'query_cache_hit':
                metrics['cache_hit_count'] += 1
            elif event == 'query_cache_miss':
                metrics['cache_miss_count'] += 1
        return metrics

    @abstractmethod
    def _ship_logs(self, enriched_logs, config, **kwargs): pass
    @abstractmethod
    def _emit_metrics(self, metrics, config, **kwargs): pass
    @abstractmethod
    def _export_traces(self, correlation_id, logs, config, **kwargs): pass
```

**CQRS-specific subclassing note:** a single `BaseObservability` subclass instance is shared across both `BaseCommandService` and `BaseQueryService` invocations of `BaseOrchestrator` — see Section 6.1. `cache_hit_count`/`cache_miss_count` give the read-path cache effectiveness a dedicated metric; `command_count`/`query_count` let one dashboard split traffic by CQRS side without a separate engine per path.

### 4.2 BaseGovernanceFramework(ABC) — CQRS Profile

```python
class BaseGovernanceFramework(ABC):
    """Template method abstract class for Deterministic Service
    Governance: tenant quota enforcement, an injection/PII semantic
    firewall on inbound Command/Query payloads, isolation for Command
    handlers that call external APIs, and circuit breaking on
    downstream failures."""

    def enforce_governance_gate(self, state, io_config,
                                 config=None, **kwargs) -> dict:
        config = config or {}
        self.__enforce_tenant_quota(state, io_config)
        if state.get('requires_manual_approval') and not state.get('approval_granted'):
            self.__trigger_dehydration(state, config, **kwargs)
        if state.get('pending_external_call'):
            self._verify_sandbox_profile(config, **kwargs)
        state = self._execute_semantic_firewall(state, config, **kwargs)
        return state

    def __enforce_tenant_quota(self, state, io_config) -> None:
        # Deterministic-service equivalent of an LLM token budget: a
        # per-tenant request quota over a rolling window, protecting
        # shared Tier 2 / Tier 3 capacity rather than LLM spend.
        max_quota = io_config.get('max_requests_per_window', float('inf'))
        used = state.get('cumulative_requests_in_window', 0)
        if used >= max_quota:
            raise NFRViolationError(
                f'Tenant request quota exhausted. '
                f'Used {used}, Limit {max_quota}')

    def __trigger_dehydration(self, state, config, **kwargs) -> None:
        correlation_id = kwargs.get('correlation_id', state.get('correlation_id'))
        tenant_id = kwargs.get('tenant_id', state.get('tenant_id'))
        self._dehydrate_state_to_perimeter(state, correlation_id, tenant_id, config)
        raise DehydrationInterrupt(f'State {correlation_id} dehydrated.')

    @abstractmethod
    def _execute_semantic_firewall(self, state, config, **kwargs) -> dict:
        """Inspect the raw Command/Query payload for injection patterns
        and redact PII before it reaches business logic."""
        pass

    @abstractmethod
    def _verify_sandbox_profile(self, config, **kwargs) -> None:
        """Assert a Command handler's downstream external-API call
        executes inside an isolated network/IAM boundary before that
        call is made."""
        pass

    @abstractmethod
    def _circuit_breaker(self, failures, config, **kwargs) -> bool:
        """Trip on repeated downstream failures (outbox commit,
        read-replica connection, external API call)."""
        pass

    @abstractmethod
    def _dehydrate_state_to_perimeter(self, state, correlation_id,
                                       tenant_id, config) -> None: pass
```

**CQRS-specific subclassing note:** `requires_manual_approval` / `pending_external_call` replace the agentic profile's `requires_human_approval` / `pending_mcp_tool_execution` trigger fields — the dehydration mechanism itself (halt, persist state, resume on webhook) is unchanged, since it is equally valid for a high-value Command awaiting manual sign-off as it was for an LLM step awaiting human approval. `_execute_semantic_firewall` and `_circuit_breaker` carry the real weight on this profile. The tenant-quota check and the latency SLO added to `mutate()`/`fetch()` (Sections 6, 7) now share one exception type, `NFRViolationError` — both are measured resource-consumption breaches, not payload-level governance failures, so they're kept out of the semantic-firewall/circuit-breaker vocabulary.

## 5. BaseOrchestrator — Corrected Scaffold

Replaces `BaseWorkflow`. Same responsibilities — mint/resolve the propagation fields, run the Governance gate, hand off to Observability on exit — routed to a Command or Query service instead of a `BaseAgent`.

```python
class BaseOrchestrator(ABC, _PropagationMixin):
    """Tier 2 lifecycle manager. Replaces BaseWorkflow: routes to
    BaseCommandService or BaseQueryService instead of a BaseAgent, and never
    issues an LLM call."""

    def execute(self, state: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        tenant_id = state.get('tenant_id')
        if not tenant_id:
            raise ValueError('state["tenant_id"] is required')

        correlation_id = kwargs.get('correlation_id', state.get('correlation_id', str(uuid.uuid4())))
        io_config = self._resolve_io_config(config, **kwargs)
        deadline = self.__resolve_deadline(config, kwargs)
        idempotency_key = self.__resolve_idempotency(state, config, **kwargs)

        message_log: List[dict] = []
        failed_keys: List[str] = []
        is_read = self._is_read_scenario(state, config)

        common = dict(correlation_id=correlation_id, tenant_id=tenant_id,
                      io_config=io_config, idempotency_key=idempotency_key,
                      message_log=message_log, failed_keys=failed_keys, deadline=deadline)
        for _k in ('expected_version', 'fencing_token'):     # caller-supplied OCC / lease evidence
            _v = kwargs.get(_k, state.get(_k))
            if _v is not None:
                common[_k] = _v

        # Commands claim their idempotency key atomically; reads need no claim.
        store = config.get('idempotency_store')
        owned, dehydrated, last_error, conflicted = False, False, None, False
        if not is_read and isinstance(store, BaseIdempotencyStore):
            claim = store.claim(
                idempotency_key, config, tenant_id=tenant_id, owner=correlation_id,
                fingerprint=self._request_fingerprint(state, config, **kwargs),
                lease_seconds=int(resolve_config('idempotency_lease_seconds', config, kwargs)),
                ttl_seconds=int(resolve_config('idempotency_ttl_seconds', config, kwargs)))
            if claim.status != ClaimStatus.CLAIMED:
                return self.__short_circuit(state, claim, message_log, correlation_id)
            owned = True

        try:
            gov_engine = config.get('governance_engine')
            if gov_engine and isinstance(gov_engine, BaseGovernanceFramework):
                state = gov_engine.enforce_governance_gate(state, io_config, config, **common)

            if is_read:
                self._log(message_log, correlation_id, 'INFO', 'routing_to_query_service')
                service = self._get_query_service(state.get('resource'), config)
                state = service.fetch(state, config, **common)
            else:
                self._log(message_log, correlation_id, 'INFO', 'routing_to_command_service')
                service = self._get_command_service(state.get('action'), config)
                state = service.mutate(state, config, **common)

        except DehydrationInterrupt as d:
            self._log(message_log, correlation_id, 'INFO', 'orchestrator_dehydrated', reason=str(d))
            state['status'] = 'AWAITING_WEBHOOK'
            dehydrated = True
        except ConcurrencyConflict as c:
            # A typed outcome, not a failure: nothing is dead-lettered. The
            # caller reloads and re-evaluates its preconditions.
            conflicted = True
            state['status'] = 'CONFLICT'
            state['conflict'] = str(c)
            self._log(message_log, correlation_id, 'WARN', 'concurrency_conflict', detail=str(c))
        except Exception as e:
            last_error = e
            failed_keys.append(idempotency_key)
            self._handle_error(e, state, config, **common)
        finally:
            obs_engine = config.get('observability_engine')
            if obs_engine and isinstance(obs_engine, BaseObservability):
                obs_engine.record_telemetry(message_log, correlation_id, config, tenant_id=tenant_id)
            else:
                for entry in message_log:
                    logging.info(entry)  # fallback
            if owned:
                self.__settle_claim(store, idempotency_key, tenant_id, correlation_id, state,
                                    failed_keys, dehydrated or conflicted, last_error, config)
            if failed_keys and not is_read:
                self._send_to_dlq(failed_keys, config)
            state['message_log'] = message_log
            state['failed_keys'] = failed_keys

        return state

    @abstractmethod
    def _is_read_scenario(self, state: Dict[str, Any], config: Dict[str, Any]) -> bool: pass
    @abstractmethod
    def _get_query_service(self, resource: str, config: Dict[str, Any]) -> 'BaseQueryService': pass
    @abstractmethod
    def _get_command_service(self, action: str, config: Dict[str, Any]) -> 'BaseCommandService': pass
    @abstractmethod
    def _generate_idempotency_key(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> str: pass
    @abstractmethod
    def _handle_error(self, error: Exception, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> None: pass

    def _request_fingerprint(self, state, config=None, **kwargs) -> str:
        """Stable hash of the command's business content; the same key with
        a different fingerprint is a CONFLICT, never a replay."""
        volatile = {'correlation_id', 'idempotency_key', 'message_log', 'failed_keys', 'deadline',
                    'status', 'result', 'outcome', 'conflict'}
        body = {k: v for k, v in state.items() if k not in volatile}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def _replayable_result(self, state, config=None, **kwargs) -> Dict[str, Any]:
        """What a duplicate caller receives on REPLAY (post-redaction)."""
        return {k: state[k] for k in ('result', 'outcome') if k in state}

    def __resolve_deadline(self, config, kwargs):
        d = kwargs.pop('deadline', None)
        if d is not None:
            return Deadline.coerce(d)
        secs = float(resolve_config('request_deadline_seconds', config, kwargs))
        return Deadline.after(secs) if secs > 0 else None

    def __resolve_idempotency(self, state, config=None, **kwargs) -> str:
        """Client key wins, else the subclass mints one. Only DERIVES the
        key; ownership is the atomic claim in execute()."""
        client_key = kwargs.get('idempotency_key', state.get('idempotency_key'))
        return client_key if client_key else self._generate_idempotency_key(state, config, **kwargs)

    def __short_circuit(self, state, claim, message_log, correlation_id):
        state['status'] = {ClaimStatus.REPLAY: 'REPLAYED', ClaimStatus.IN_PROGRESS: 'IN_PROGRESS',
                           ClaimStatus.CONFLICT: 'CONFLICT', ClaimStatus.RECOVERY_REQUIRED: 'RECOVERY_REQUIRED',
                           ClaimStatus.FAILED_FINAL: 'FAILED_FINAL'}[claim.status]
        state['idempotency_status'] = claim.status.value
        if claim.status == ClaimStatus.REPLAY and claim.result:
            state.update({k: v for k, v in claim.result.items() if k in ('result', 'outcome')})
            state['replayed'] = True
        self._log(message_log, correlation_id, 'INFO', 'idempotent_short_circuit', claim_status=claim.status.value)
        state['message_log'], state['failed_keys'] = message_log, []
        return state

    def __settle_claim(self, store, key, tenant_id, owner, state, failed_keys, released, error, config):
        kw = dict(config=config, tenant_id=tenant_id, owner=owner)
        outcome = state.get('outcome')
        if released:
            store.fail(key, retryable=True, **kw)      # paused or conflicted: claimable again
        elif outcome == PhysicalOutcome.UNKNOWN:
            store.fail(key, retryable=False, outcome=PhysicalOutcome.UNKNOWN, **kw)
        elif failed_keys:
            store.fail(key, retryable=not isinstance(error, _DETERMINISTIC_ERRORS), **kw)
        else:
            store.complete(key, self._replayable_result(state, config), **kw)
```

> Corrected from the initial draft: (1) `__resolve_idempotency()` mirrors `BaseWorkflow`'s actual private method rather than calling a non-existent `_resolve_idempotency()` mixin helper; (2) `_generate_idempotency_key()` is declared as an abstract hook; (3) both the Governance gate and the Observability handoff are actually wired into the control flow, not just described in prose.

### 5.1 Atomic Claim, Replay and Settlement

`__resolve_idempotency()` now only derives the key: the client key wins, otherwise the subclass mints one through `_generate_idempotency_key()`. Ownership is decided by the atomic claim in `execute()`, not by a lookup followed by a write, so two concurrent callers with the same key can never both proceed. Reads take no claim.

When `config['idempotency_store']` is a `BaseIdempotencyStore`, a command calls `store.claim(key, tenant_id=…, owner=correlation_id, fingerprint=…, lease_seconds=idempotency_lease_seconds, ttl_seconds=idempotency_ttl_seconds)`. Keys are tenant-scoped; the fingerprint is `_request_fingerprint()`, a hash of the command's business content that excludes volatile fields. Anything other than `CLAIMED` short-circuits through `__short_circuit()`, and no service code runs:

| Claim result | `state['status']` | What the caller receives | Retry guidance |
|---|---|---|---|
| `CLAIMED` | — (proceeds) | The command runs | — |
| `REPLAY` | `REPLAYED` | The stored `result` / `outcome` from `_replayable_result()` (post-redaction), `replayed = True` | None — the effect already happened |
| `IN_PROGRESS` | `IN_PROGRESS` | No result | Retry later; another owner holds an unexpired claim |
| `CONFLICT` | `CONFLICT` | No result | Same key with a different fingerprint — a client bug, never a replay; do not retry unchanged |
| `RECOVERY_REQUIRED` | `RECOVERY_REQUIRED` | No result | The prior owner's lease expired or its outcome is `UNKNOWN`; only the reconciler (Section 13) may take it over |
| `FAILED_FINAL` | `FAILED_FINAL` | No result | A deterministic failure is on record; a corrected request needs a new key |

In the `finally` block `__settle_claim()` closes the claim exactly once, owner-checked, so a stale owner cannot complete a key it no longer holds:

| Run ended as | Settlement | Key afterwards |
|---|---|---|
| Success | `complete(key, _replayable_result(state))` | `SUCCEEDED` — duplicates replay |
| Typed `CONFLICT` (OCC / fencing) or `DehydrationInterrupt` | `fail(retryable=True)` | `FAILED_RETRYABLE` — claimable again |
| Exception that is not deterministic | `fail(retryable=True)`, key appended to `failed_keys` | `FAILED_RETRYABLE` |
| Deterministic error (`_DETERMINISTIC_ERRORS`: validation, bad request) | `fail(retryable=False)` | `FAILED_FINAL` |
| `state['outcome'] == UNKNOWN` | `fail(retryable=False, outcome=UNKNOWN)` | Parked as `UNKNOWN` for reconciliation; never retried or replayed blindly |

A `ConcurrencyConflict` from the command service is a typed outcome, not a failure: the orchestrator sets `state['status'] = 'CONFLICT'` and `state['conflict']`, does not append to `failed_keys`, and nothing is dead-lettered. The caller reloads and re-evaluates its preconditions. When no `idempotency_store` is configured the claim is skipped and behaviour is exactly as before; production profiles must bind one.

## 6. BaseCommandService — Write Path (Tier 2)

```python
class BaseCommandService(ABC, _PropagationMixin):
    input_dto_model: Type[BaseModel]
    output_dto_model: Type[BaseModel]

    def mutate(self, payload: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> dict:
        config = config or {}
        key = kwargs.get('idempotency_key')
        failed_keys = kwargs.pop('failed_keys', [])   # popped: _commit_gate() takes it positionally
        message_log = kwargs.get('message_log', [])
        correlation_id = kwargs.get('correlation_id')
        start = time.time()

        try:
            validated_input = self.input_dto_model(**payload)
        except ValidationError as e:
            self._log(message_log, correlation_id, 'ERROR', 'dto_validation_failed', error=str(e))
            raise ValueError(f'Schema validation failed: {e}')

        if key and not self._commit_gate(key, failed_keys, config, **kwargs):
            self._log(message_log, correlation_id, 'WARN', 'commit_gate_rejected')
            return self.output_dto_model(status='blocked', reason='commit_gate_rejection').dict()

        deadline = Deadline.coerce(kwargs.get('deadline'))
        if deadline:
            deadline.check('command execution')
        processed_dto = self._execute_business_logic(validated_input, config, **kwargs)
        # Conditional commit BEFORE the outbox write: the store's atomic
        # compare-and-set is the authority; a loser raises ConcurrencyConflict.
        self._commit_versioned(processed_dto, config, **kwargs)
        self._execute_outbox_commit(processed_dto, config, **kwargs)
        self._synchronize_cache(processed_dto, config, **kwargs)

        latency = time.time() - start
        max_latency = resolve_config('max_latency', config, kwargs, scope='CQRS')
        self._log(message_log, correlation_id, 'INFO', 'command_mutation_complete', latency=round(latency, 3))
        if latency > max_latency:
            raise NFRViolationError(f'Command latency SLO breached: {latency:.2f}s > {max_latency}s')
        return processed_dto.dict()

    def _entity_key(self, processed_dto: BaseModel, **kwargs) -> Optional[str]:
        """Protected hook: the durable key of the aggregate this command
        changes. Default None = no versioned aggregate, no conditional
        commit. A subclass returns the key to opt in to OCC."""
        return None

    def _commit_versioned(self, processed_dto: BaseModel, config: dict, **kwargs) -> Optional[CommitResult]:
        """Base-owned conditional commit through config['transactional_store']
        (a BaseTransactionalStore): put_if_version(expected_version). A
        CONFLICT or FENCED result raises ConcurrencyConflict — it is never
        silently overwritten and never retried blindly against new state. A
        store that can commit the aggregate and the outbox record in ONE
        transaction overrides this and folds _execute_outbox_commit in."""
        key = self._entity_key(processed_dto, **kwargs)
        store = (config or {}).get('transactional_store')
        if key is None or not isinstance(store, BaseTransactionalStore):
            return None
        expected = kwargs.get('expected_version', (config or {}).get('expected_version'))
        res = store.put_if_version(key, processed_dto.dict(), expected, config,
                                   fencing_token=kwargs.get('fencing_token'))
        if not res.committed:
            raise ConcurrencyConflict(
                f'{res.status}: {key!r} expected version {expected}, store has {res.current_version}')
        return res

    @abstractmethod
    def _execute_business_logic(self, validated_input: BaseModel, config: dict, **kwargs) -> BaseModel: pass
    @abstractmethod
    def _execute_outbox_commit(self, processed_dto: BaseModel, config: dict, **kwargs) -> None: pass
    @abstractmethod
    def _synchronize_cache(self, processed_dto: BaseModel, config: dict, **kwargs) -> None: pass
```

**Notes:**
- `_commit_gate()` is the `_PropagationMixin` helper — the same batch-isolation check and the same cheap `expected_version` fast-fail as the agentic profile's tool-call commit gate. It is **advisory**: the version it compares can be stale by the time the write happens. The authority for competing writers is the store's atomic conditional commit, `_commit_versioned()`.
- `_execute_outbox_commit()` must be the last write in the local transaction and must not itself publish to Tier 4 — the CDC sweeper owns that hop.
- `_synchronize_cache()` runs synchronously, before `mutate()` returns — this is what Section 10.2 of the Landing Zone doc (read-after-write consistency) depends on. Do not defer it to an async handler.
- `_entity_key()` is the opt-in hook: it returns the durable key of the aggregate the command changes. The default is `None`, which means no versioned aggregate and no conditional commit, so existing subclasses are unaffected.
- `_commit_versioned()` is base-owned. With an `_entity_key()` and a `config['transactional_store']` (a `BaseTransactionalStore`) it calls `put_if_version(key, dto, expected_version, fencing_token=…)`. A `CONFLICT` or `FENCED` result raises `ConcurrencyConflict`; the write is never overwritten and never retried blindly against newer state. It runs **before** `_execute_outbox_commit()`, so a losing writer leaves no outbox record.
- A store that can commit the aggregate and the outbox record in one transaction overrides `_commit_versioned()` and folds `_execute_outbox_commit()` into it. Where the store's conditional write is single-item only (DynamoDB conditional writes are), the outbox record must be written through the store's multi-item transaction or the order above must be kept with the outbox as an idempotent consumer-side upsert.
- `mutate()` checks `deadline` before `_execute_business_logic()`; an expired deadline raises `DeadlineExceeded`, which the orchestrator treats as an ordinary failure.

### 6.1 Command Conflict and Replay Semantics

| Situation | Detected by | Outcome | DLQ |
|---|---|---|---|
| Same key, same payload, already succeeded | Atomic claim | `REPLAYED`; business logic and outbox not re-run | No |
| Same key, same payload, running elsewhere | Atomic claim | `IN_PROGRESS` | No |
| Same key, different payload | Fingerprint on the claim | `CONFLICT` | No |
| Two writers, same `expected_version`, different keys | `put_if_version` | One commits; the loser gets `CONFLICT`; its key stays `FAILED_RETRYABLE` | No |
| Writer holds a superseded lease | `put_if_version` with `fencing_token` | `CONFLICT` (`FENCED`) | No |
| Deadline already expired | `Deadline.check()` | Failure, key dead-lettered | Yes |
| Commit outcome unknown after a timeout | `state['outcome'] == UNKNOWN` | Key parked for reconciliation | No — parked, not replayed |

## 7. BaseQueryService — Read Path (Tier 2 → Tier 3 Cache-Aside)

```python
class BaseQueryService(ABC, _PropagationMixin):
    output_dto_model: Type[BaseModel]

    def fetch(self, query_params: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> dict:
        config = config or {}
        tenant_id = kwargs.get('tenant_id')
        correlation_id = kwargs.get('correlation_id')
        message_log = kwargs.get('message_log', [])
        io_config = kwargs.get('io_config', {})
        start = time.time()

        deadline = Deadline.coerce(kwargs.get('deadline'))
        if deadline:
            deadline.check('query execution')
        cache_key = self._generate_cache_key(query_params, tenant_id)
        cache_store = config.get('cache_store')
        if isinstance(cache_store, BaseCacheStore):
            # Per-key SINGLE-FLIGHT: concurrent misses share one database
            # read (no distributed lock on the read path).
            loaded = []
            def _load():
                loaded.append(True)
                self._log(message_log, correlation_id, 'INFO', 'query_cache_miss')
                return self._execute_db_read(query_params, config, **kwargs)
            db_data = cache_store.get_or_load(
                cache_key, _load, None, config, deadline=deadline)   # TTL: the cache store's default_ttl_seconds
            if not loaded:
                self._log(message_log, correlation_id, 'INFO', 'query_cache_hit', latency=round(time.time() - start, 3))
                return self.output_dto_model(**db_data).dict()
        else:
            cached_data = self._read_from_cache(cache_key, io_config)
            if cached_data:
                self._log(message_log, correlation_id, 'INFO', 'query_cache_hit', latency=round(time.time() - start, 3))
                return self.output_dto_model(**cached_data).dict()

            self._log(message_log, correlation_id, 'INFO', 'query_cache_miss')
            db_data = self._execute_db_read(query_params, config, **kwargs)
            if db_data:
                self._write_to_cache(cache_key, db_data, io_config)

        latency = time.time() - start
        max_latency = resolve_config('max_latency', config, kwargs, scope='CQRS')
        self._log(message_log, correlation_id, 'INFO', 'query_fetch_complete', latency=round(latency, 3))
        if latency > max_latency:
            raise NFRViolationError(f'Query latency SLO breached: {latency:.2f}s > {max_latency}s')
        return self.output_dto_model(**db_data).dict()

    @abstractmethod
    def _generate_cache_key(self, query_params: dict, tenant_id: str) -> str: pass
    @abstractmethod
    def _read_from_cache(self, cache_key: str, io_config: dict) -> Optional[dict]: pass
    @abstractmethod
    def _execute_db_read(self, query_params: dict, config: dict, **kwargs) -> dict: pass
    @abstractmethod
    def _write_to_cache(self, cache_key: str, data: dict, io_config: dict) -> None: pass
```

**Notes:**
- `_generate_cache_key()` must fold `tenant_id` into the key (e.g. `TENANT:acme:ORDER:123`) — the tenant-isolation control for the cache tier, equivalent in weight to the namespacing `BaseRAGPipeline._search_index(query_text, query_vector, …)` performs in the agentic profile (hybrid BM25 + vector search).
- Stampede protection is single-flight, not a distributed lock. When `config['cache_store']` is a `BaseCacheStore`, `fetch()` calls `get_or_load(cache_key, loader, ttl, config, deadline=deadline)`: concurrent misses on the same key share one `_execute_db_read()`, the other callers wait and read the filled value, and a loader failure propagates and is not cached. The abstract `_read_from_cache()` / `_write_to_cache()` hooks remain the path when no `cache_store` is bound, and are then where a subclass implements its own stampede guard.
- `_execute_db_read()` must connect through the read-only replica credential path — a write attempt from this method should fail at the database driver level, not rely on application code getting it right.
- The read path is bounded: the read pool is sized by `db_pool_size`, and `fetch()` checks `deadline` before any cache or database work. A cache miss therefore costs at most one pooled connection per key, however many callers arrive together.

### 7.1 Single-Flight and Stale Policy

| Setting | Meaning |
|---|---|
| `cache_fill_timeout_seconds` (default `5.0`) | How long a follower waits for the in-flight fill, further bounded by the remaining `deadline` |
| `cache_fill_on_timeout = load` (default) | The follower runs its own load — freshest, but can add database load during a slow fill |
| `cache_fill_on_timeout = stale` | The follower returns the last cached value if one exists — protects the database, may serve stale data within the TTL |
| `cache_fill_on_timeout = raise` | The follower fails with a timeout — strictest, for reads that must not be stale or duplicated |

Choose `stale` for read models that tolerate bounded staleness; keep `load` where correctness requires a fresh read. The cache stays advisory throughout: `_synchronize_cache()` on the command path (Section 6) is what gives read-after-write consistency, and a cache miss or a failed fill never changes what the database says. TTL deletion on AWS and GCP caches can lag the stated expiry, so freshness is decided by the value's own expiry field and not by key presence.

## 8. Config / kwargs Reference

Every key below is registered in E2A_CONFIG (Implementation Playbook Section 5), which is the only place names, environment variables and defaults are defined. This table lists only the keys the CQRS path reads and how each is supplied; project-specific keys are added with register_config_key() in src/settings.py.

| Key | Scope | Purpose |
|---|---|---|
| `observability_engine` | config | Instance of a `BaseObservability` subclass; if absent, `BaseOrchestrator` falls back to local structured logging. |
| `governance_engine` | config | Instance of a `BaseGovernanceFramework` subclass; if absent, the governance gate is skipped entirely (fail-open) — set explicitly in production. |
| `commit_gate_enabled` | config / kwargs | Toggles the OCC/batch-isolation commit gate on `BaseCommandService.mutate()`; defaults to `True`. |
| `expected_version` | kwargs | Optimistic Concurrency Control comparison value. `_commit_gate()` uses it as an advisory fast-fail; `_commit_versioned()` passes it to `put_if_version`, which is the authoritative check. |
| `dlq_queue_url` | config / kwargs / env `DLQ_QUEUE_URL` | Destination for `failed_keys` dispatch on the command path only. |
| `io_config_prefix` | config / kwargs / env `IO_CONFIG_PREFIX` | Namespace prefix (default `io_`) used to carve `io_config` out of the full config dict. |
| `max_latency` | config / kwargs / env `MAX_LATENCY` | Latency SLO ceiling checked at the end of `mutate()` and `fetch()` (Sections 6, 7); default `1.0s` for both — tighter than the agentic profile's `2.0s`/`5.0s` since neither CQRS path waits on an LLM or an external tool call. Raises `NFRViolationError` on breach. |
| `fencing_token` | kwargs | Lease fencing token of the writer; passed to `put_if_version` so the store rejects a stale writer. |
| `idempotency_store` | config | A `BaseIdempotencyStore`; when bound, commands claim their key atomically. Absent means no claim (set explicitly in production). |
| `transactional_store` | config | A `BaseTransactionalStore`; when bound and `_entity_key()` returns a key, the command commits through `put_if_version`. |
| `cache_store` | config | A `BaseCacheStore`; when bound, `fetch()` uses per-key single-flight. |
| `idempotency_lease_seconds` | config / kwargs / env `IDEMPOTENCY_LEASE_SECONDS` (default `300`) | Expiry of an `IN_PROGRESS` claim. An expired claim needs reconciliation, never blind re-execution. |
| `idempotency_ttl_seconds` | config / kwargs / env `IDEMPOTENCY_TTL_SECONDS` (default `86400`) | How long a settled key is retained for replay; stored as an explicit expiry field, with store TTL used only for garbage collection. |
| `request_deadline_seconds` | config / kwargs / env `REQUEST_DEADLINE_SECONDS` (default `0` = none) | Origin of the `deadline` field when the caller supplies none. |
| `cache_fill_timeout_seconds` | config / kwargs / env `CACHE_FILL_TIMEOUT_SECONDS` (default `5.0`) | Follower wait for an in-flight cache fill (Section 7.1). |
| `cache_fill_on_timeout` | config / kwargs / env `CACHE_FILL_ON_TIMEOUT` (default `load`) | Follower policy when the fill times out: `load`, `stale` or `raise`. |
| `db_pool_size` | config / env `DB_POOL_SIZE` (default `20`) | Per-instance database pool; bounds command and query concurrency. |
| `worker_concurrency`, `worker_prefetch` | config / env `WORKER_CONCURRENCY` (default `8`), `WORKER_PREFETCH` (default `16`) | Consumer in-flight messages and broker prefetch window per instance (Section 12). |
| `drain_timeout_seconds` | config / env `DRAIN_TIMEOUT_SECONDS` (default `20.0`) | Graceful-shutdown bound for consumers; must be shorter than the platform termination grace. |
| `lease_ttl_seconds`, `lease_renew_interval_seconds` | config / env `LEASE_TTL_SECONDS` (default `60`), `LEASE_RENEW_INTERVAL_SECONDS` (default `20`) | Lease for the outbox sweeper and reconciler (Section 13); renew must be shorter than TTL. |

## 9. Error Handling & DLQ Semantics

`BaseOrchestrator.execute()` catches exceptions from either service uniformly and appends `idempotency_key` to `failed_keys`, exactly as `BaseWorkflow` does for `BaseAgent`. The one CQRS-specific branch is in the `finally` block's DLQ dispatch: `if failed_keys and not is_read`. A failed query never reaches `_send_to_dlq()` — see the Landing Zone doc's Section 6 for the reasoning. A concrete `_handle_error()` implementation should still surface a query failure to the caller as a normal error response; it simply does not enter the Saga's compensation flow. `NFRViolationError` — raised by the tenant-quota check in `enforce_governance_gate()` (Section 4.2) or by a latency-SLO breach in `mutate()`/`fetch()` (Sections 6, 7) — is not a distinct branch either; it is caught by the same `except Exception` clause as a schema-validation or outbox failure.

Concurrency adds four rules to this section. (1) A `ConcurrencyConflict` is caught before the generic clause, becomes `state['status'] = 'CONFLICT'`, and is never added to `failed_keys` — it is the correct answer to a lost race, not a fault. (2) A duplicate, in-progress, conflicting, recovery-required or final-failed claim returns before any service runs and never reaches the DLQ. (3) A deterministic error (`_DETERMINISTIC_ERRORS`) settles the claim as `FAILED_FINAL`, so a redelivery is not retried forever; any other error settles it as `FAILED_RETRYABLE`. (4) A command whose commit outcome is `UNKNOWN` — the write may or may not have landed — is parked for the reconciler and is never retried, replayed from the DLQ, or reported as a failure. Replaying a dead-lettered command goes through the same claim, so a replay cannot duplicate an effect that did land.

## 10. End-to-End Execution Trace (Annotated)

**Write Path (Command)**
1. Tier 1: `BaseValidationService.validate()` mints `correlation_id`, classifies the intent as a write, publishes to the Intake Topic.
2. Tier 2: `BaseOrchestrator.execute()` resolves `io_config`, `deadline` and `idempotency_key` → `idempotency_store.claim()` (a non-`CLAIMED` result returns here, see below) → `governance_engine.enforce_governance_gate()` → routes to the resolved `BaseCommandService`.
3. Tier 2: `BaseCommandService.mutate()` validates the DTO, checks the commit gate and the deadline, executes business logic, commits the aggregate with `put_if_version` (`_commit_versioned()`), commits the outbox, synchronizes the cache.
4. Tier 2 (finally): `observability_engine.record_telemetry()` ships `message_log`; `__settle_claim()` completes or fails the claim; on any failure, `failed_keys` forwards to Tier 5 via `_send_to_dlq()`.
5. Tier 3 → Tier 4: CDC sweeper fans the committed event out to domain consumers.
6. Tier 3 → Tier 5: Task Queue feeds the Saga edge for the commit/compensate/DLQ decision.

**Read Path (Query)**
1. Tier 1: `BaseValidationService.validate()` classifies the intent as a read; Tier 2 is invoked synchronously, no Intake Topic hop.
2. Tier 2: `BaseOrchestrator.execute()` resolves `io_config` → `governance_engine.enforce_governance_gate()` → routes to the resolved `BaseQueryService`.
3. Tier 2: `BaseQueryService.fetch()` checks the deadline, then the cache (single-flight when a `cache_store` is bound: concurrent misses share one database read), and on a hit returns immediately with no Tier 3 database or Tier 5 involvement.
4. Tier 2 (finally): `observability_engine.record_telemetry()` ships `message_log`; `failed_keys`, if any, is not forwarded to Tier 5.

**Duplicate Command (same key, same payload)**
1. Tier 2: `execute()` calls `claim()`. A prior success returns `REPLAY`; `__short_circuit()` sets `state['status'] = 'REPLAYED'` with the stored `result` / `outcome`.
2. No governance gate, service, outbox write or cache write runs; `failed_keys` is empty and nothing reaches Tier 5.

**Conflicting Commands (two writers, same `expected_version`)**
1. Both claim distinct keys and run business logic.
2. `put_if_version` commits one. The other raises `ConcurrencyConflict`; `execute()` sets `state['status'] = 'CONFLICT'`, `__settle_claim()` leaves its key `FAILED_RETRYABLE`, and nothing is dead-lettered or written to the outbox.
3. The loser's caller reloads, re-evaluates and resubmits with the new version.

## 11. Divergences From the Agentic Profile — Summary

| Agentic Class | CQRS Profile | Reasoning |
|---|---|---|
| `BaseWorkflow` | `BaseOrchestrator` | Same lifecycle-manager role; routes to Command/Query services instead of a single `BaseAgent`. |
| `BaseAgent` | `BaseCommandService` + `BaseQueryService` | One class per CQRS side — the write and read paths have materially different consistency and caching contracts. |
| `BaseRAGPipeline` / `BaseToolService` (Tier 4) | Domain Event Consumers (Tier 4) | Same tier, same pull-based/queue-depth-scaled role; consumes committed domain events instead of tool-call tasks. |
| `BasePromptRegistry` | Not used | No LLM prompts exist on this profile's request path. |
| `BaseLLMAgentService` / `BaseIndexingPipeline` | Not used | No LLM call or vector index exists on this profile's request path; the Inference Gateway tier is not provisioned. |
| `BaseObservability` | CQRS-adapted (`ABC`, same hook contract) | Abstract-class contract unchanged; extracted metrics replaced (command/query/cache counters instead of token counts). |
| `BaseGovernanceFramework` | CQRS-adapted (`ABC`, same hook contract) | Abstract-class contract unchanged; token-budget enforcement replaced with tenant request-quota enforcement, MCP sandboxing generalized to external-API-call isolation. |
| `BaseValidationService` | Reused unmodified | Gains one responsibility: classifying the intent as read or write ahead of the Intake Topic/direct-invoke branch. |
| `BaseIdempotencyStore` / `BaseTransactionalStore` / `BaseCacheStore` | Reused unmodified from `e2a_base.py` | Same ports as the agentic profile; the orchestrator claims, the command service commits conditionally, the query service single-flights. No CQRS-specific store contract. |
| Consumers (Tier 4) | Plain consumers behind the outbox, scaffolded as obligations in Section 12 | The scaffold has no consumer base class; the runtime wraps the existing contracts. |

## 12. Consumer Model — Partition Ordering, Bounded In-Flight Work, Retry/DLQ/Replay

The scaffold does not ship a consumer base class. A Tier 4 domain-event consumer is a thin runtime around contracts that already exist, and this section states the obligations that runtime must meet. Treat it as the implementation contract for the consumer; the primitives it relies on (`claim`, `complete`, `fail`, `Deadline`, `ResiliencePolicy`) are implemented and tested, the runtime loop itself is not part of the scaffold.

**Ordering.** Order is guaranteed only within a partition, never across partitions. Partition by the aggregate key that `_entity_key()` returns (prefixed by `tenant_id`), so every event for one aggregate lands on one partition and is handled in order; two aggregates are never assumed to be ordered. A consumer must not rely on any cross-aggregate order.

**Bounded in-flight work.** Each instance holds at most `worker_concurrency` messages in flight, with a broker prefetch window of `worker_prefetch`, and `worker_prefetch ≥ worker_concurrency`. In-flight work is bounded by the database pool as well: `worker_concurrency ≤ db_pool_size`, because every handler may hold a connection. `validate_capacity()` (Implementation Playbook Section 2.19.11) checks these equations at startup, so a mis-sized consumer fails the scaffold-compliance gate instead of saturating the database in production.

**Handler contract.**

```text
for each message m from partition p (at most worker_concurrency at a time):
    deadline = Deadline.from_remaining_ms(m.remaining_ms) or Deadline.after(handler_budget)
    claim = idempotency_store.claim(m.event_id, tenant_id=m.tenant_id, owner=worker_id,
                                    fingerprint=hash(m.body), lease_seconds=idempotency_lease_seconds)
    if claim is REPLAY:                 ack m              # already applied
    elif claim is IN_PROGRESS:          nack m, delayed    # another worker owns it
    elif claim is RECOVERY_REQUIRED:    ack m, leave it to the reconciler
    elif claim is CLAIMED:
        try:    apply m under deadline;       complete(m.event_id, result);          ack m
        except deterministic_error:           fail(retryable=False);                 ack m   # poison: park, do not block p
        except other_error:                   fail(retryable=True);                  nack m  # redeliver with backoff
        on timeout after a possible commit:   fail(retryable=False, outcome=UNKNOWN); ack m  # reconciler decides
```

**Retry, DLQ and replay all go through the idempotency claim.** A redelivery is harmless because it hits `REPLAY`. A dead-lettered message is replayed only by the reconciler (Section 13), which takes the key over with `recover=True`, confirms physical truth and only then re-applies. A message is never replayed from a DLQ by hand.

**Poison messages behave differently per broker, so the handler must park them rather than rely on the broker.** On SQS FIFO a failing message blocks its whole message group until it is deleted or reaches the maximum receive count; on Pub/Sub with ordering a nack redelivers the failed message and the key's tail behind it; on Service Bus a message that exceeds the delivery count is dead-lettered and the session moves on. Therefore: classify deterministic errors immediately and acknowledge them after settling the key `FAILED_FINAL`; cap delivery attempts in the broker configuration; and alarm on DLQ depth and oldest-message age (Service Bus exposes no oldest-message-age metric, so use DLQ message count and enqueue time).

**Graceful shutdown.** On a termination signal the consumer stops polling, lets in-flight handlers finish for at most `drain_timeout_seconds`, and nacks what remains. `drain_timeout_seconds` must be shorter than the platform's termination grace (about 10 s on Cloud Run, 30 s default on ECS up to 120 s, 30 s on Azure Container Apps), or in-flight messages are killed mid-write and recovered only through lease expiry.

## 13. Reconciliation and Leases

Scheduled repair is the job of `BaseOutboxSweeper` and `BaseReconciliationService` (Implementation Playbook Section 2.16). Neither runs per request; both run on a fixed cadence and are exclusive by **lease**, not by a lock that can outlive its holder.

- **Lease, not lock.** `BaseDistributedLock.acquire()` returns a `LeaseHandle(owner_token, fencing_token, expires_at)`. The worker renews it between records; the renew interval must be shorter than `lease_ttl_seconds`. A worker that can no longer prove ownership stops with `LEASE_LOST`. Release is owner-checked, so a late release by a stale worker is a no-op.
- **Fencing only where a stale writer can commit.** The sweeper passes the lease's `fencing_token` on every publish. A downstream store that enforces it rejects a sweeper whose lease has been superseded. No cloud provider offers a native fencing token, so the store adapter must implement the check; where the downstream write is already idempotent through the claim, no fencing is needed and none is added.
- **Reconciliation takes over, confirms, then acts.** For each parked or dead-lettered key the reconciler claims it with `recover=True`, calls `_confirm_physical_outcome()` (default `UNKNOWN`) and acts on the answer: `COMMITTED` marks the key done and never replays; `NOT_COMMITTED` replays once through the normal claim and completes it; `UNKNOWN` stays parked for the next run. An unconfirmed command is never retried, because that is how a duplicate material effect happens.
- **Capacity.** The reconciler's concurrency is bounded and budgeted like any other worker (`worker_concurrency`, `db_pool_size`); a stale claim waiting for repair holds no connection.

## 14. Race and Saturation Test Matrix

Tests in the left column are implemented in `smoke_test_cqrs_concurrency.py` (CQRS path) or `smoke_test_concurrency.py` (shared contracts) against the in-memory test doubles in `e2a_inmemory_adapters.py`. Rows marked *runtime* need the consumer runtime or a load environment; the scaffold does not claim them as passed.

| ID | Scenario | Expected result | Evidence |
|---|---|---|---|
| T-01 | Duplicate command, same key and payload | `REPLAYED`; business logic and outbox run once | `T-01 CQRS: duplicate is REPLAYED and executed once` |
| T-01 | 20 concurrent claimers on one key | Exactly one wins | `T-01 exactly one of 20 concurrent claimers wins` |
| T-01 | Same key, different payload | `CONFLICT`; nothing executed | `same key, different payload -> CONFLICT` |
| T-02 | Two writers, same `expected_version` | One commits; loser gets typed `CONFLICT`, not DLQ; its key stays claimable | `T-02 CQRS: one writer commits, losers get typed CONFLICT (not DLQ)`; `conflicted command key stays claimable` |
| T-09 | Writer with a superseded lease | Rejected as `FENCED` / `CONFLICT` | `stale-lease command rejected as a typed conflict` |
| T-03 | 15 concurrent cold reads, one key | One database read | `T-03 CQRS: 15 concurrent cold reads -> 1 database read` |
| T-03 | Loader failure during a shared fill | Propagates to waiters and is not cached | `T-03 loader failure propagates and is not cached` |
| — | Warm read | Served from cache, no database read | `warm read served from cache` |
| T-10 | Expired deadline | Rejected before work; dead-lettered as a failure | `expired deadline rejected, key dead-lettered as a failure` |
| T-12 | Commit outcome `UNKNOWN` | Key parked; not retried or replayed | `T-12 UNKNOWN stays parked: no replay` |
| T-12 | Reconciler, `NOT_COMMITTED` truth | Replayed once, then `SUCCEEDED` | `T-12 NOT_COMMITTED truth: replayed once, then SUCCEEDED` |
| — | Lease held by another sweeper | Second sweeper skips | `sweeper skips when lease is held` |
| — | Stale sweeper publishes | Fencing token travels with the publish | `sweeper passes the lease fencing token to publish` |
| — | Mis-sized consumer or pool | Startup fails the capacity gate | `BasePipeline scaffold-compliance gate fails on a capacity violation` |
| — | Same-partition ordering under redelivery | Events for one aggregate applied in order, once | *runtime* |
| — | Poison message on a FIFO group | Parked as `FAILED_FINAL`; the rest of the partition proceeds | *runtime* |
| — | Consumer saturation (all `worker_concurrency` slots busy) | Prefetch window holds; database pool not exhausted | *runtime* |
| — | Rolling deploy under load | In-flight messages drain within `drain_timeout_seconds`; no key left `IN_PROGRESS` past its lease | *runtime* |

## Appendix A — `reference/e2a_cqrs_base.py` Complete Source

The full, contiguous scaffold file for this profile, mirroring the base Implementation Playbook's convention of one complete drop-in file rather than fragments spread across prose. Place it at `reference/e2a_cqrs_base.py`, never modify it directly, and subclass its abstract classes in your service code. It has been syntax-validated (`python3 -m ast`) as a single, self-contained, importable module.

```python
# e2a_cqrs_base.py — E2A Architecture Framework Scaffold, Deterministic CQRS Profile
# Drop into src/ or framework/. Import and subclass. Never modify directly.
# Companion to e2a_base.py (agentic profile) — reuses the same propagation
# contract and single-entry-point rule; replaces BaseWorkflow/BaseAgent with
# BaseOrchestrator/BaseCommandService/BaseQueryService for deterministic,
# non-LLM request paths.

import os
import json
import uuid
import time
import hashlib
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, ValidationError
from e2a_base import (resolve_config,   # central config registry (E2A_CONFIG) — shared with the agentic profile
                      Deadline, ClaimStatus, BaseIdempotencyStore, BaseCacheStore,
                      BaseTransactionalStore, ConcurrencyConflict, CommitResult,
                      PhysicalOutcome, _DETERMINISTIC_ERRORS)   # shared concurrency contracts

logging.basicConfig(level=logging.INFO)


class DehydrationInterrupt(Exception):
    """Signal exception used to cleanly halt execution and scale compute to
    zero pending manual approval (e.g. a Command above a policy threshold)."""
    pass


class NFRViolationError(Exception):
    """Raised when a measured non-functional requirement — the tenant request
    quota in BaseGovernanceFramework.__enforce_tenant_quota(), or the latency
    SLO in BaseCommandService.mutate() / BaseQueryService.fetch() — is
    breached. Falls through the normal exception path like any other
    failure; there is no token-budget variant on this profile since neither
    path calls an LLM."""
    pass


# ==================================================
# 1. Shared Propagation Helpers
# ==================================================
class _PropagationMixin:
    """Shared, base-owned helpers for cross-class state threading. Identical
    to the agentic profile's mixin — observability is not handled here, it is
    migrated to BaseObservability below."""

    def _resolve_io_config(self, config=None, **kwargs):
        config = config or {}
        prefix = resolve_config('io_config_prefix', config, kwargs)
        return {k: v for k, v in config.items() if k.startswith(prefix)}

    def _log(self, message_log, correlation_id, level, event, **fields):
        if message_log is None:
            return
        message_log.append({
            'timestamp': time.time(), 'correlation_id': correlation_id,
            'level': level, 'event': event,
            'class': type(self).__name__, **fields,
        })

    def _send_to_dlq(self, failed_keys, config=None, **kwargs):
        config = config or {}
        queue_url = resolve_config('dlq_queue_url', config, kwargs)
        if queue_url and failed_keys:
            logging.warning(f'DLQ dispatch -> {queue_url}: {failed_keys}')

    def _commit_gate(self, key, failed_keys, config=None, **kwargs):
        """Base-owned commit gate. Enforces batch isolation and a cheap,
        ADVISORY Optimistic Concurrency fast-fail via expected_version. The
        authority for competing writers is the store's atomic conditional
        commit (BaseTransactionalStore.put_if_version), driven from
        BaseCommandService._commit_versioned()."""
        config = config or {}
        enabled = kwargs.get('commit_gate_enabled', config.get('commit_gate_enabled', True))
        if not enabled:
            return True
        if key in (failed_keys or []):
            return False
        expected_version = kwargs.get('expected_version')
        if expected_version is not None:
            actual_version = kwargs.get('actual_version_lookup', lambda k, c: expected_version)(key, config)
            if expected_version != actual_version:
                return False  # OCC collision
        return True


# ==================================================
# 2. Foundation Classes (Observability, Governance) — CQRS profile
# ==================================================
class BaseObservability(ABC):
    """Template method abstract class for CQRS Service Telemetry. Same
    abstract-class contract as the agentic profile's BaseObservability
    (_ship_logs / _emit_metrics / _export_traces remain the three hooks a
    subclass must implement) — only the derived metrics change, since token
    usage has no meaning on a request path with no LLM call."""

    def record_telemetry(self, message_log: List[dict], correlation_id: str,
                          config: Dict[str, Any] = None, **kwargs) -> None:
        config = config or {}
        enriched_logs = []
        for entry in (message_log or []):
            enriched_entry = {
                **entry,
                'correlation_id': correlation_id,
                'tenant_id': kwargs.get('tenant_id', 'UNKNOWN_TENANT'),
                'host_epoch_ms': int(time.time() * 1000),
                'framework_version': 'e2a-cqrs-v1.0',
            }
            enriched_logs.append(enriched_entry)

        extracted_metrics = self.__extract_metrics(enriched_logs)
        try:
            self._ship_logs(enriched_logs, config, **kwargs)
            if extracted_metrics:
                self._emit_metrics(extracted_metrics, config, **kwargs)
            self._export_traces(correlation_id, enriched_logs, config, **kwargs)
        except Exception as e:
            logging.error(f"Telemetry dispatch failed for {correlation_id}: {str(e)}")
            for log in enriched_logs:
                logging.info(log)

    def __extract_metrics(self, enriched_logs: List[dict]) -> Dict[str, float]:
        # CQRS-relevant counters, derived from BaseOrchestrator/BaseCommandService/
        # BaseQueryService's _log() events — no token/cost metrics on this profile.
        metrics = {
            'command_count': 0, 'query_count': 0,
            'cache_hit_count': 0, 'cache_miss_count': 0,
            'error_count': 0,
        }
        for log in enriched_logs:
            event = log.get('event')
            if log.get('level') == 'ERROR':
                metrics['error_count'] += 1
            if event == 'routing_to_command_service':
                metrics['command_count'] += 1
            elif event == 'routing_to_query_service':
                metrics['query_count'] += 1
            elif event == 'query_cache_hit':
                metrics['cache_hit_count'] += 1
            elif event == 'query_cache_miss':
                metrics['cache_miss_count'] += 1
        return metrics

    @abstractmethod
    def _ship_logs(self, enriched_logs: List[dict], config: Dict[str, Any], **kwargs):
        pass

    @abstractmethod
    def _emit_metrics(self, metrics: Dict[str, float], config: Dict[str, Any], **kwargs):
        pass

    @abstractmethod
    def _export_traces(self, correlation_id: str, logs: List[dict], config: Dict[str, Any], **kwargs):
        pass


class BaseGovernanceFramework(ABC):
    """Template method abstract class for Deterministic Service Governance:
    tenant quota enforcement, an injection/PII semantic firewall on inbound
    Command/Query payloads, isolation for Command handlers that call external
    APIs, and circuit breaking on downstream failures. This replaces the
    agentic profile's AI Safety/Economics framing (LLM token budgets, MCP
    tool sandboxing) with the equivalent deterministic-service concerns."""

    def enforce_governance_gate(self, state: Dict[str, Any], io_config: Dict[str, Any],
                                config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        self.__enforce_tenant_quota(state, io_config)

        if state.get('requires_manual_approval') and not state.get('approval_granted'):
            self.__trigger_dehydration(state, config, **kwargs)

        if state.get('pending_external_call'):
            self._verify_sandbox_profile(config, **kwargs)

        state = self._execute_semantic_firewall(state, config, **kwargs)
        return state

    def __enforce_tenant_quota(self, state: Dict[str, Any], io_config: Dict[str, Any]) -> None:
        # Deterministic-service equivalent of the agentic profile's token
        # budget: a per-tenant request quota over a rolling window, protecting
        # shared Tier 2/Tier 3 capacity rather than LLM spend.
        max_quota = io_config.get('max_requests_per_window', float('inf'))
        current_usage = state.get('cumulative_requests_in_window', 0)
        if current_usage >= max_quota:
            raise NFRViolationError(
                f"Tenant request quota exhausted. "
                f"Used {current_usage}, Limit {max_quota}")

    def __trigger_dehydration(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> None:
        correlation_id = kwargs.get('correlation_id', state.get('correlation_id'))
        tenant_id = kwargs.get('tenant_id', state.get('tenant_id'))
        self._dehydrate_state_to_perimeter(state, correlation_id, tenant_id, config)
        raise DehydrationInterrupt(f"State {correlation_id} dehydrated. Awaiting webhook.")

    @abstractmethod
    def _execute_semantic_firewall(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """Inspect the raw Command/Query payload for injection patterns and
        redact PII before it reaches business logic."""
        pass

    @abstractmethod
    def _verify_sandbox_profile(self, config: Dict[str, Any], **kwargs) -> None:
        """Assert a Command handler's downstream external-API call executes
        inside an isolated network/IAM boundary (dedicated egress security
        group, scoped credential) before that call is made."""
        pass

    @abstractmethod
    def _circuit_breaker(self, failures: int, config: Dict[str, Any], **kwargs) -> bool:
        """Trip on repeated downstream failures (outbox commit, read-replica
        connection, external API call) and short-circuit further attempts."""
        pass

    @abstractmethod
    def _dehydrate_state_to_perimeter(self, state: Dict[str, Any], correlation_id: str,
                                      tenant_id: str, config: Dict[str, Any]) -> None:
        pass


# ==================================================
# 3. Validation (Tier 1 Edge) — reused unmodified from the agentic profile
# ==================================================
class ValidationResult(dict):
    """Thin dict subclass for HTTP 400 serializable validation returns."""
    pass


class BaseValidationService(ABC, _PropagationMixin):
    validator_registry: Dict[str, Any] = {}

    def validate(self, agent_name: str, state: Dict[str, Any],
                 config: Dict[str, Any] = None, correlation_id: str = None,
                 **kwargs) -> 'ValidationResult':
        config = config or {}
        correlation_id = correlation_id or state.get('correlation_id', str(uuid.uuid4()))
        validator = self._resolve_validator(agent_name, config, **kwargs)

        if validator is None:
            strict = resolve_config('strict_mode', config, kwargs)
            if strict:
                return self.__build_result(False, [f'No validator registered for {agent_name}'], agent_name, correlation_id)
            return self.__build_result(True, [], agent_name, correlation_id)

        valid, errors = validator(state, config, **kwargs)
        return self.__build_result(valid, errors, agent_name, correlation_id)

    def _resolve_validator(self, agent_name, config=None, **kwargs):
        return self.validator_registry.get(agent_name)

    def __build_result(self, valid, errors, agent_name, correlation_id) -> 'ValidationResult':
        return ValidationResult(valid=valid, errors=errors, agent_name=agent_name, correlation_id=correlation_id)


# ==================================================
# 4. Orchestration (Tier 2 Compute) — BaseOrchestrator
# ==================================================
class BaseOrchestrator(ABC, _PropagationMixin):
    """Tier 2 lifecycle manager. Replaces BaseWorkflow: routes to
    BaseCommandService or BaseQueryService instead of a BaseAgent, and never
    issues an LLM call."""

    def execute(self, state: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        tenant_id = state.get('tenant_id')
        if not tenant_id:
            raise ValueError('state["tenant_id"] is required')

        correlation_id = kwargs.get('correlation_id', state.get('correlation_id', str(uuid.uuid4())))
        io_config = self._resolve_io_config(config, **kwargs)
        deadline = self.__resolve_deadline(config, kwargs)
        idempotency_key = self.__resolve_idempotency(state, config, **kwargs)

        message_log: List[dict] = []
        failed_keys: List[str] = []
        is_read = self._is_read_scenario(state, config)

        common = dict(correlation_id=correlation_id, tenant_id=tenant_id,
                      io_config=io_config, idempotency_key=idempotency_key,
                      message_log=message_log, failed_keys=failed_keys, deadline=deadline)
        for _k in ('expected_version', 'fencing_token'):     # caller-supplied OCC / lease evidence
            _v = kwargs.get(_k, state.get(_k))
            if _v is not None:
                common[_k] = _v

        # Commands claim their idempotency key atomically; reads need no claim.
        store = config.get('idempotency_store')
        owned, dehydrated, last_error, conflicted = False, False, None, False
        if not is_read and isinstance(store, BaseIdempotencyStore):
            claim = store.claim(
                idempotency_key, config, tenant_id=tenant_id, owner=correlation_id,
                fingerprint=self._request_fingerprint(state, config, **kwargs),
                lease_seconds=int(resolve_config('idempotency_lease_seconds', config, kwargs)),
                ttl_seconds=int(resolve_config('idempotency_ttl_seconds', config, kwargs)))
            if claim.status != ClaimStatus.CLAIMED:
                return self.__short_circuit(state, claim, message_log, correlation_id)
            owned = True

        try:
            gov_engine = config.get('governance_engine')
            if gov_engine and isinstance(gov_engine, BaseGovernanceFramework):
                state = gov_engine.enforce_governance_gate(state, io_config, config, **common)

            if is_read:
                self._log(message_log, correlation_id, 'INFO', 'routing_to_query_service')
                service = self._get_query_service(state.get('resource'), config)
                state = service.fetch(state, config, **common)
            else:
                self._log(message_log, correlation_id, 'INFO', 'routing_to_command_service')
                service = self._get_command_service(state.get('action'), config)
                state = service.mutate(state, config, **common)

        except DehydrationInterrupt as d:
            self._log(message_log, correlation_id, 'INFO', 'orchestrator_dehydrated', reason=str(d))
            state['status'] = 'AWAITING_WEBHOOK'
            dehydrated = True
        except ConcurrencyConflict as c:
            # A typed outcome, not a failure: nothing is dead-lettered. The
            # caller reloads and re-evaluates its preconditions.
            conflicted = True
            state['status'] = 'CONFLICT'
            state['conflict'] = str(c)
            self._log(message_log, correlation_id, 'WARN', 'concurrency_conflict', detail=str(c))
        except Exception as e:
            last_error = e
            failed_keys.append(idempotency_key)
            self._handle_error(e, state, config, **common)
        finally:
            obs_engine = config.get('observability_engine')
            if obs_engine and isinstance(obs_engine, BaseObservability):
                obs_engine.record_telemetry(message_log, correlation_id, config, tenant_id=tenant_id)
            else:
                for entry in message_log:
                    logging.info(entry)  # fallback
            if owned:
                self.__settle_claim(store, idempotency_key, tenant_id, correlation_id, state,
                                    failed_keys, dehydrated or conflicted, last_error, config)
            if failed_keys and not is_read:
                self._send_to_dlq(failed_keys, config)
            state['message_log'] = message_log
            state['failed_keys'] = failed_keys

        return state

    @abstractmethod
    def _is_read_scenario(self, state: Dict[str, Any], config: Dict[str, Any]) -> bool: pass
    @abstractmethod
    def _get_query_service(self, resource: str, config: Dict[str, Any]) -> 'BaseQueryService': pass
    @abstractmethod
    def _get_command_service(self, action: str, config: Dict[str, Any]) -> 'BaseCommandService': pass
    @abstractmethod
    def _generate_idempotency_key(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> str: pass
    @abstractmethod
    def _handle_error(self, error: Exception, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> None: pass

    def _request_fingerprint(self, state, config=None, **kwargs) -> str:
        """Stable hash of the command's business content; the same key with
        a different fingerprint is a CONFLICT, never a replay."""
        volatile = {'correlation_id', 'idempotency_key', 'message_log', 'failed_keys', 'deadline',
                    'status', 'result', 'outcome', 'conflict'}
        body = {k: v for k, v in state.items() if k not in volatile}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def _replayable_result(self, state, config=None, **kwargs) -> Dict[str, Any]:
        """What a duplicate caller receives on REPLAY (post-redaction)."""
        return {k: state[k] for k in ('result', 'outcome') if k in state}

    def __resolve_deadline(self, config, kwargs):
        d = kwargs.pop('deadline', None)
        if d is not None:
            return Deadline.coerce(d)
        secs = float(resolve_config('request_deadline_seconds', config, kwargs))
        return Deadline.after(secs) if secs > 0 else None

    def __resolve_idempotency(self, state, config=None, **kwargs) -> str:
        """Client key wins, else the subclass mints one. Only DERIVES the
        key; ownership is the atomic claim in execute()."""
        client_key = kwargs.get('idempotency_key', state.get('idempotency_key'))
        return client_key if client_key else self._generate_idempotency_key(state, config, **kwargs)

    def __short_circuit(self, state, claim, message_log, correlation_id):
        state['status'] = {ClaimStatus.REPLAY: 'REPLAYED', ClaimStatus.IN_PROGRESS: 'IN_PROGRESS',
                           ClaimStatus.CONFLICT: 'CONFLICT', ClaimStatus.RECOVERY_REQUIRED: 'RECOVERY_REQUIRED',
                           ClaimStatus.FAILED_FINAL: 'FAILED_FINAL'}[claim.status]
        state['idempotency_status'] = claim.status.value
        if claim.status == ClaimStatus.REPLAY and claim.result:
            state.update({k: v for k, v in claim.result.items() if k in ('result', 'outcome')})
            state['replayed'] = True
        self._log(message_log, correlation_id, 'INFO', 'idempotent_short_circuit', claim_status=claim.status.value)
        state['message_log'], state['failed_keys'] = message_log, []
        return state

    def __settle_claim(self, store, key, tenant_id, owner, state, failed_keys, released, error, config):
        kw = dict(config=config, tenant_id=tenant_id, owner=owner)
        outcome = state.get('outcome')
        if released:
            store.fail(key, retryable=True, **kw)      # paused or conflicted: claimable again
        elif outcome == PhysicalOutcome.UNKNOWN:
            store.fail(key, retryable=False, outcome=PhysicalOutcome.UNKNOWN, **kw)
        elif failed_keys:
            store.fail(key, retryable=not isinstance(error, _DETERMINISTIC_ERRORS), **kw)
        else:
            store.complete(key, self._replayable_result(state, config), **kw)


# ==================================================
# 5. BaseCommandService — Write Path (Tier 2)
# ==================================================
class BaseCommandService(ABC, _PropagationMixin):
    input_dto_model: Type[BaseModel]
    output_dto_model: Type[BaseModel]

    def mutate(self, payload: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> dict:
        config = config or {}
        key = kwargs.get('idempotency_key')
        failed_keys = kwargs.pop('failed_keys', [])   # popped: _commit_gate() takes it positionally
        message_log = kwargs.get('message_log', [])
        correlation_id = kwargs.get('correlation_id')
        start = time.time()

        try:
            validated_input = self.input_dto_model(**payload)
        except ValidationError as e:
            self._log(message_log, correlation_id, 'ERROR', 'dto_validation_failed', error=str(e))
            raise ValueError(f'Schema validation failed: {e}')

        if key and not self._commit_gate(key, failed_keys, config, **kwargs):
            self._log(message_log, correlation_id, 'WARN', 'commit_gate_rejected')
            return self.output_dto_model(status='blocked', reason='commit_gate_rejection').dict()

        deadline = Deadline.coerce(kwargs.get('deadline'))
        if deadline:
            deadline.check('command execution')
        processed_dto = self._execute_business_logic(validated_input, config, **kwargs)
        # Conditional commit BEFORE the outbox write: the store's atomic
        # compare-and-set is the authority; a loser raises ConcurrencyConflict.
        self._commit_versioned(processed_dto, config, **kwargs)
        self._execute_outbox_commit(processed_dto, config, **kwargs)
        self._synchronize_cache(processed_dto, config, **kwargs)

        latency = time.time() - start
        max_latency = resolve_config('max_latency', config, kwargs, scope='CQRS')
        self._log(message_log, correlation_id, 'INFO', 'command_mutation_complete', latency=round(latency, 3))
        if latency > max_latency:
            raise NFRViolationError(f'Command latency SLO breached: {latency:.2f}s > {max_latency}s')
        return processed_dto.dict()

    def _entity_key(self, processed_dto: BaseModel, **kwargs) -> Optional[str]:
        """Protected hook: the durable key of the aggregate this command
        changes. Default None = no versioned aggregate, no conditional
        commit. A subclass returns the key to opt in to OCC."""
        return None

    def _commit_versioned(self, processed_dto: BaseModel, config: dict, **kwargs) -> Optional[CommitResult]:
        """Base-owned conditional commit through config['transactional_store']
        (a BaseTransactionalStore): put_if_version(expected_version). A
        CONFLICT or FENCED result raises ConcurrencyConflict — it is never
        silently overwritten and never retried blindly against new state. A
        store that can commit the aggregate and the outbox record in ONE
        transaction overrides this and folds _execute_outbox_commit in."""
        key = self._entity_key(processed_dto, **kwargs)
        store = (config or {}).get('transactional_store')
        if key is None or not isinstance(store, BaseTransactionalStore):
            return None
        expected = kwargs.get('expected_version', (config or {}).get('expected_version'))
        res = store.put_if_version(key, processed_dto.dict(), expected, config,
                                   fencing_token=kwargs.get('fencing_token'))
        if not res.committed:
            raise ConcurrencyConflict(
                f'{res.status}: {key!r} expected version {expected}, store has {res.current_version}')
        return res

    @abstractmethod
    def _execute_business_logic(self, validated_input: BaseModel, config: dict, **kwargs) -> BaseModel: pass
    @abstractmethod
    def _execute_outbox_commit(self, processed_dto: BaseModel, config: dict, **kwargs) -> None: pass
    @abstractmethod
    def _synchronize_cache(self, processed_dto: BaseModel, config: dict, **kwargs) -> None: pass


# ==================================================
# 6. BaseQueryService — Read Path (Tier 2 -> Tier 3 Cache-Aside)
# ==================================================
class BaseQueryService(ABC, _PropagationMixin):
    output_dto_model: Type[BaseModel]

    def fetch(self, query_params: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> dict:
        config = config or {}
        tenant_id = kwargs.get('tenant_id')
        correlation_id = kwargs.get('correlation_id')
        message_log = kwargs.get('message_log', [])
        io_config = kwargs.get('io_config', {})
        start = time.time()

        deadline = Deadline.coerce(kwargs.get('deadline'))
        if deadline:
            deadline.check('query execution')
        cache_key = self._generate_cache_key(query_params, tenant_id)
        cache_store = config.get('cache_store')
        if isinstance(cache_store, BaseCacheStore):
            # Per-key SINGLE-FLIGHT: concurrent misses share one database
            # read (no distributed lock on the read path).
            loaded = []
            def _load():
                loaded.append(True)
                self._log(message_log, correlation_id, 'INFO', 'query_cache_miss')
                return self._execute_db_read(query_params, config, **kwargs)
            db_data = cache_store.get_or_load(
                cache_key, _load, None, config, deadline=deadline)   # TTL: the cache store's default_ttl_seconds
            if not loaded:
                self._log(message_log, correlation_id, 'INFO', 'query_cache_hit', latency=round(time.time() - start, 3))
                return self.output_dto_model(**db_data).dict()
        else:
            cached_data = self._read_from_cache(cache_key, io_config)
            if cached_data:
                self._log(message_log, correlation_id, 'INFO', 'query_cache_hit', latency=round(time.time() - start, 3))
                return self.output_dto_model(**cached_data).dict()

            self._log(message_log, correlation_id, 'INFO', 'query_cache_miss')
            db_data = self._execute_db_read(query_params, config, **kwargs)
            if db_data:
                self._write_to_cache(cache_key, db_data, io_config)

        latency = time.time() - start
        max_latency = resolve_config('max_latency', config, kwargs, scope='CQRS')
        self._log(message_log, correlation_id, 'INFO', 'query_fetch_complete', latency=round(latency, 3))
        if latency > max_latency:
            raise NFRViolationError(f'Query latency SLO breached: {latency:.2f}s > {max_latency}s')
        return self.output_dto_model(**db_data).dict()

    @abstractmethod
    def _generate_cache_key(self, query_params: dict, tenant_id: str) -> str: pass
    @abstractmethod
    def _read_from_cache(self, cache_key: str, io_config: dict) -> Optional[dict]: pass
    @abstractmethod
    def _execute_db_read(self, query_params: dict, config: dict, **kwargs) -> dict: pass
    @abstractmethod
    def _write_to_cache(self, cache_key: str, data: dict, io_config: dict) -> None: pass
```

---

*E2A CQRS Implementation Playbook — Class Contracts & Scaffold · Subham Gupta, Principal Platform Architect*
