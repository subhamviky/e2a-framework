"""v1.9.0 (hub design note EV-01): evaluation across the framework.

EvalOutcome from _evaluate_output; the run's trajectory; reusable scorers; the release gate's
expect-based defaults, trials and evidence labels; the online-evaluation port; calibration;
promotion; and the settlement and telemetry fixes (E2A O-3, family X-9, E2A O-10)."""
import json

import pytest

from e2a import (BaseAgent, BaseGovernanceFramework, BaseObservability, BaseToolService, BaseWorkflow,
                 ClaimStatus, Step, Steps)
from e2a.evals import (BaseOnlineEvaluator, EvalOutcome, OutboxOnlineEvaluator, RecordingOnlineEvaluator,
                       ScoringOnlineEvaluator, calibration_report, contains, exact, no_pii, promote_candidates,
                       refusal, run_evals_cli, schema, score_case, trajectory)
from e2a.messaging import InMemoryOutboxStore
from e2a.release import ProjectReleaseGate
from e2a.core import NFRViolationError, is_retryable
from e2a.stages import GovernanceRefusal
from e2a_inmemory_adapters import InMemoryIdempotencyStore
from e2a_simulated_llm import SimulatedLLMService, always


# ------------------------------------------------------------------ a small service
class Lookup(BaseToolService):
    material = False

    def _get_endpoint(self, config=None, tenant_id=None, **kw): return "local://lookup"
    def _build_payload(self, payload, config=None, **kw): return payload
    def _validate_input(self, payload, config=None, **kw): return True

    async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kw):
        return {"physical_success": True, "status": "OK", "order": body.get("order_id")}


class Refund(BaseToolService):
    material = True

    def __init__(self):
        self.calls = []

    def _get_endpoint(self, config=None, tenant_id=None, **kw): return "local://refund"
    def _build_payload(self, payload, config=None, **kw): return payload
    def _validate_input(self, payload, config=None, **kw): return True

    async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kw):
        self.calls.append(body)
        return {"physical_success": True, "status": "OK"}


class Agent(BaseAgent):
    agent_name = "RefundAgent"

    def __init__(self, score=1.0, error=None):
        self.score, self.error = score, error

    def _prepare(self, state, ctx, io_read):
        state["order"] = io_read.call("lookup", {"order_id": state["order_id"]})

    def _build_messages(self, state, ctx):
        if self.error:
            raise self.error
        return [{"role": "user", "content": state["query"]}]

    def _evaluate_output(self, response, state, ctx):
        return self.score

    def _act(self, state, ctx, io_modify):
        return [io_modify.call("refund", {"order_id": state["order_id"], "amount": "10.00"})]

    def _fallback(self, state, ctx):
        return {"text": "a person will follow up", "degraded": True}


class Flow(BaseWorkflow):
    def __init__(self, agent):
        self.agent = agent

    def _build_workflow(self, ctx):
        return Steps(Step("refund", self.agent, material=True))

    def _generate_idempotency_key(self, state, ctx):
        return f"refund-{state['order_id']}"

    def _handle_error(self, error, state, ctx, messages):
        state.setdefault("errors", []).append(str(error))


def config(**extra):
    return {"llm_service": SimulatedLLMService().add_rule("any", always(), "Refund REF-1 approved"),
            "min_confidence": 0.5, "tools": {"lookup": Lookup(), "refund": Refund()},
            "idempotency_store": InMemoryIdempotencyStore(), **extra}


def request(order="o-1"):
    return {"tenant_id": "t1", "query": f"refund order {order}", "order_id": order}


# ------------------------------------------------------------------ EvalOutcome from the agent
def test_a_float_still_works_and_the_outcome_is_recorded():
    state = Flow(Agent(score=0.9)).execute(request(), config())
    assert state["evaluation"] == "ACCEPTED"
    assert state["eval_outcome"]["score"] == 0.9 and state["eval_outcome"]["threshold"] == 0.5
    assert state["eval_outcome"]["passed"] is True and state["eval_outcome"]["grader"] == "RefundAgent"
    events = [e for e in state["message_log"] if e.get("event") == "evaluation"]
    assert events and events[0]["eval.score"] == 0.9 and events[0]["eval.fallback"] is False


def test_an_eval_outcome_with_a_reason_is_kept_and_a_low_score_falls_back():
    state = Flow(Agent(score=EvalOutcome(0.2, reason="no citation", grader="citations"))).execute(request(), config())
    assert state["evaluation"] == "FALLBACK" and state["response"]["degraded"] is True
    assert state["eval_outcome"]["reason"] == "no citation" and state["eval_outcome"]["grader"] == "citations"
    assert state["eval_outcome"]["passed"] is False


def test_a_score_outside_0_and_1_is_refused():
    with pytest.raises(ValueError):
        EvalOutcome(1.5)


# ------------------------------------------------------------------ the trajectory
def test_every_tool_call_and_the_agent_are_on_the_trajectory():
    state = Flow(Agent()).execute(request(), config())
    tools = [t for t in state["trajectory"] if t["kind"] == "tool"]
    assert [(t["tool"], t["access"]) for t in tools] == [("lookup", "read"), ("refund", "modify")]
    assert tools[1]["args"] == {"order_id": "o-1", "amount": "10.00"} and tools[1]["outcome"] == "COMMITTED"
    assert len(tools[0]["payload_sha256"]) == 16 and tools[0]["latency_ms"] >= 0
    agents = [t for t in state["trajectory"] if t["kind"] == "agent"]
    assert agents[-1]["agent"] == "RefundAgent" and agents[-1]["score"] == 1.0


def test_a_denied_tool_call_is_on_the_trajectory_too():
    class OnlyLookup(Agent):
        allowed_tools = ["lookup"]
    state = Flow(OnlyLookup()).execute(request(), config())
    denied = [t for t in state["trajectory"] if t.get("outcome") == "DENIED"]
    assert denied and denied[0]["tool"] == "refund"


# ------------------------------------------------------------------ scorers
def test_scorers():
    assert exact("Refund  REF-1 approved.", "refund ref-1 approved").passed
    assert not exact("no", "yes").passed
    c = contains("Refund REF-1 approved", ["REF-1", "policy 4.2"], forbidden=["card number"])
    assert not c.passed and c.score == pytest.approx(2 / 3) and c.evidence["missing"] == ["policy 4.2"]
    s = schema({"text": json.dumps({"intent": "REFUND"})}, {"type": "object", "required": ["intent"]})
    assert s.passed and not schema("not json", {"type": "object"}).passed
    assert refusal({"refusal": {"code": "TOOL_DENIED"}}).passed
    assert not refusal({"evaluation": "ACCEPTED"}).passed
    assert not no_pii("mail me at a.b@example.com").passed and no_pii("all good").passed


def test_trajectory_checks():
    state = {"trajectory": [{"kind": "tool", "tool": "lookup", "args": {"order_id": "o-1"}},
                            {"kind": "tool", "tool": "refund", "args": {"amount": "10.00"}}],
             "steps": [{"step": "refund", "status": "DONE"}]}
    ok = trajectory(state, {"tools_required": ["lookup", "refund"], "tools_order": ["lookup", "refund"],
                            "max_tool_calls": 2, "args": {"refund": {"amount": "10.00"}},
                            "steps_required": ["refund"]})
    assert ok.passed and ok.score == 1.0
    bad = trajectory(state, {"tools_forbidden": ["refund"], "tools_order": ["refund", "lookup"],
                             "max_tool_calls": 1})
    assert not bad.passed and "forbidden" in bad.reason and "order" in bad.reason and bad.score == 0.0


def test_score_case_takes_the_lowest_and_an_empty_expect_fails():
    state = {"response": {"text": "Refund REF-1 approved"}, "trajectory": []}
    combined = score_case(state, {"contains": ["REF-1"], "trajectory": {"tools_required": ["refund"]}})
    assert not combined.passed and combined.score == 0.0
    assert not score_case(state, {}).passed
    assert "unknown checks" in score_case(state, {"vibes": 1}).reason


# ------------------------------------------------------------------ settlement (O-3, X-9) and telemetry (O-10)
class Store(InMemoryIdempotencyStore):
    def __init__(self):
        super().__init__()
        self.failures = []

    def fail(self, key, retryable=True, **kw):
        self.failures.append((key, retryable))
        return super().fail(key, retryable=retryable, **kw)


class PolicyViolation(NFRViolationError):
    """A project's own policy error that says it is final (v1.9.1: is_retryable believes it)."""
    retryable = False


@pytest.mark.parametrize("error,retryable", [
    (GovernanceRefusal("over the limit", stage="policy", code="LIMIT"), False),
    (ValueError("bad amount"), False),
    (RuntimeError("upstream hiccup"), True),
    (PolicyViolation("cloud not approved"), False),                   # v1.9.1
    (NFRViolationError("latency SLO breached"), True),                # v1.9.1: transient, as before
])
def test_an_agents_failure_settles_as_it_says(error, retryable):
    store = Store()
    state = Flow(Agent(error=error)).execute(request(), config(idempotency_store=store))
    assert state["failure"]["retryable"] is retryable
    assert store.failures == [("refund-o-1", retryable)]
    again = Flow(Agent()).execute(request(), config(idempotency_store=store))
    if retryable:
        assert again.get("evaluation") == "ACCEPTED"           # claimable again
    else:
        assert again["status"] == "FAILED_FINAL"               # never retried


class Telemetry(BaseObservability):
    def __init__(self):
        self.batches = []

    def _emit_metrics(self, *a, **k): pass
    def _export_traces(self, *a, **k): pass
    def _ship_logs(self, *a, **k): pass

    def record_telemetry(self, message_log, correlation_id, config=None, **kwargs):
        self.batches.append([e.get("event") for e in message_log])


def test_a_replay_reaches_the_observability_engine():
    telemetry, cfg = Telemetry(), config()
    cfg["observability_engine"] = telemetry
    Flow(Agent()).execute(request(), cfg)
    Flow(Agent()).execute(request(), cfg)
    assert any("idempotent_short_circuit" in batch for batch in telemetry.batches)


# ------------------------------------------------------------------ online evaluation (B-2)
def test_a_settled_run_is_recorded_for_online_evaluation(tmp_path):
    out = tmp_path / "online.jsonl"
    cfg = config(online_evaluator=RecordingOnlineEvaluator(out), online_eval_sample_rate=1.0)
    Flow(Agent()).execute(request(), cfg)
    Flow(Agent()).execute(request(), cfg)                       # a replay is not submitted again
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["workflow"] == "Flow" and rows[0]["eval_outcome"]["score"] == 1.0
    assert [t["tool"] for t in rows[0]["trajectory"] if t["kind"] == "tool"] == ["lookup", "refund"]
    assert "message_log" not in rows[0]


def test_sampling_and_budget(tmp_path):
    out = tmp_path / "online.jsonl"
    Flow(Agent()).execute(request("a"), config(online_evaluator=RecordingOnlineEvaluator(out),
                                               online_eval_sample_rate=0.0))
    assert not out.exists()
    evaluator = RecordingOnlineEvaluator(out, max_per_minute=1)
    for order in ("b", "c", "d"):
        Flow(Agent()).execute(request(order), config(online_evaluator=evaluator, online_eval_sample_rate=1.0))
    assert len(out.read_text().splitlines()) == 1


def test_a_failing_evaluator_never_fails_the_request():
    class Broken(BaseOnlineEvaluator):
        def submit(self, record, config=None):
            raise RuntimeError("evaluator down")
    state = Flow(Agent()).execute(request(), config(online_evaluator=Broken(), online_eval_sample_rate=1.0))
    assert state["outcome"] == "COMMITTED"
    assert any(e.get("event") == "online_evaluation_failed" for e in state["message_log"])


def test_outbox_and_scoring_evaluators():
    outbox = InMemoryOutboxStore()
    Flow(Agent()).execute(request(), config(online_evaluator=OutboxOnlineEvaluator(outbox),
                                            online_eval_sample_rate=1.0))
    record = outbox.pending()[0]
    assert record["destination"] == "e2a.evaluation" and json.loads(record["body"])["workflow"] == "Flow"
    scored = []
    evaluator = ScoringOnlineEvaluator([lambda r: contains(r["response"], ["REF-1"])], scored.append,
                                       background=False)
    Flow(Agent()).execute(request("o-2"), config(online_evaluator=evaluator, online_eval_sample_rate=1.0))
    assert scored and scored[0]["score"] == 1.0


def test_personal_data_is_redacted_from_the_record(tmp_path):
    out = tmp_path / "online.jsonl"
    req = dict(request(), query="refund o-1 and email me at jane@example.com")
    Flow(Agent()).execute(req, config(online_evaluator=RecordingOnlineEvaluator(out), online_eval_sample_rate=1.0))
    assert "jane@example.com" not in out.read_text() and "[email]" in out.read_text()


# ------------------------------------------------------------------ the release gate
def gate_project(tmp_path, golden=None, redteam=None):
    (tmp_path / "src").mkdir()
    (tmp_path / "evals").mkdir()
    if golden is not None:
        (tmp_path / "evals" / "golden.json").write_text(json.dumps(golden))
    if redteam is not None:
        (tmp_path / "evals" / "redteam.json").write_text(json.dumps(redteam))
    return tmp_path


class RefundGate(ProjectReleaseGate):
    def _run_case(self, case):
        return Flow(Agent()).execute(dict(request(case["input"]["order_id"]), **case["input"]), config())


GOLDEN = [{"id": "g1", "input": {"order_id": "o-1"},
           "expect": {"contains": ["REF-1"], "trajectory": {"tools_required": ["lookup", "refund"]}}}]


def test_a_case_with_expect_is_scored_without_writing_a_scorer(tmp_path):
    gate = RefundGate(root=gate_project(tmp_path, GOLDEN), run_tests=False)
    report = gate.run_pipeline({"fleet_replica_count": 1})
    assert report["status"] == "SUCCESS", report.get("error")
    assert gate.evidence["evaluation"][0]["score"] == 1.0
    assert gate.evidence["run"]["evidence_kind"] == "wiring" and gate.evidence["run"]["profile"] == "simulated"


def test_fail_closed_without_run_case_or_without_expect(tmp_path):
    report = ProjectReleaseGate(root=gate_project(tmp_path, GOLDEN), run_tests=False).run_pipeline(
        {"fleet_replica_count": 1})
    assert report["status"] != "SUCCESS" and "_run_case" in str(report.get("error"))
    other = tmp_path / "other"
    other.mkdir()
    report = RefundGate(root=gate_project(other, [{"id": "g2", "input": {"order_id": "o-9"}}]),
                        run_tests=False).run_pipeline({"fleet_replica_count": 1})
    assert report["status"] != "SUCCESS"


def test_trials_report_variance_and_pass_at_k(tmp_path):
    scores = iter([1.0, 0.0, 1.0])

    class Flaky(ProjectReleaseGate):
        def _score_case(self, case):
            return next(scores)
    gate = Flaky(root=gate_project(tmp_path, [{"id": "f", "input": {}}]), run_tests=False)
    gate.run_pipeline({"fleet_replica_count": 1, "eval_trials": 3})
    row = gate.evidence["evaluation"][0]
    assert row["trials"] == 3 and row["scores"] == [1.0, 0.0, 1.0] and row["min"] == 0.0
    assert row["score"] == pytest.approx(2 / 3) and row["stdev"] > 0 and row["pass_at_k"] is True


def test_a_red_team_case_with_expect_is_probed_by_default(tmp_path):
    redteam = [{"id": "r1", "input": {"order_id": "o-1"}, "expect": {"trajectory": {"tools_forbidden": ["refund"]}}}]
    gate = RefundGate(root=gate_project(tmp_path, GOLDEN, redteam), run_tests=False)
    report = gate.run_pipeline({"fleet_replica_count": 1})
    assert report["status"] != "SUCCESS" and gate.evidence["redteam"] == ["r1: refund was called but is forbidden"]


def test_evidence_kind_live_and_quality(tmp_path, monkeypatch):
    monkeypatch.setenv("E2A_SWAP_LIVE", "true")
    monkeypatch.setenv("E2A_PROVIDER_PROFILE", "anthropic_bedrock")
    gate = RefundGate(root=gate_project(tmp_path, GOLDEN), run_tests=False)
    gate.run_pipeline({"fleet_replica_count": 1})
    assert gate.evidence["run"]["evidence_kind"] == "live" and gate.evidence["run"]["profile"] == "anthropic_bedrock"

    class Recorded(RefundGate):
        evidence_kind = "quality"
    other = tmp_path / "q"
    other.mkdir()
    gate = Recorded(root=gate_project(other, GOLDEN), run_tests=False)
    gate.run_pipeline({"fleet_replica_count": 1})
    assert gate.evidence["run"]["evidence_kind"] == "quality"


# ------------------------------------------------------------------ calibration and promotion
def test_calibration_report():
    cases = [{"score": s, "label": l} for s, l in [(0.9, True), (0.8, True), (0.7, True), (0.4, False),
                                                    (0.3, False), (0.6, False)]]
    report = calibration_report(cases, threshold=0.5)
    assert report["cases"] == 6 and report["at_threshold"]["false_pass"] == 1
    assert report["at_suggested"]["kappa"] == 1.0 and 0.6 < report["suggested_threshold"] <= 0.7
    assert report["correlation"] > 0.7


def test_promotion_makes_redacted_candidates_for_review(tmp_path):
    records = [
        {"query": "refund o-1, my card is 4111 1111 1111 1111", "eval_outcome": {"score": 0.2},
         "correlation_id": "c1", "tenant_id": "t1", "trajectory": [{"kind": "tool", "tool": "lookup"}]},
        {"query": "fine one", "eval_outcome": {"score": 0.95}, "correlation_id": "c2"},
        {"query": "refused one", "refusal": {"code": "LIMIT"}, "correlation_id": "c3"},
    ]
    found = promote_candidates(records, below=0.7, source="test")
    assert [f["provenance"]["correlation_id"] for f in found] == ["c1", "c3"]
    assert "4111" not in json.dumps(found) and found[0]["expect"] == {}
    assert found[0]["provenance"]["status"] == "NEEDS_REVIEW" and found[0]["observed"]["tools"] == ["lookup"]
    src = tmp_path / "online.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in records))
    out = tmp_path / "evals" / "candidates.json"
    assert run_evals_cli(["promote", "--records", str(src), "--out", str(out)]) == 0
    assert run_evals_cli(["promote", "--records", str(src), "--out", str(out)]) == 0      # idempotent
    assert len(json.loads(out.read_text())) == 2


def test_is_retryable_believes_an_exceptions_own_flag():
    """v1.9.1: the one rule the agent, the workflow and AI-DLC use."""
    assert is_retryable(RuntimeError()) and not is_retryable(ValueError()) and not is_retryable(PermissionError())
    assert not is_retryable(PolicyViolation()) and is_retryable(NFRViolationError())
    assert not is_retryable(GovernanceRefusal("no", stage="s")) and is_retryable(GovernanceRefusal("wait", retryable=True))
    flagged = ValueError("but this one says otherwise")
    flagged.retryable = True
    assert is_retryable(flagged)
