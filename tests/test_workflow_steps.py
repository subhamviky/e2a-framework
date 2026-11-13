"""v1.2 step 5: declared workflow steps under one claim (design note 05) and resume()
after an approval pause (design note 01, E-17)."""
import pytest

from e2a import (ApprovalStage, BaseAgent, BaseGovernanceFramework, BaseToolService, BaseWorkflow,
                 ResumeRefused, Route, Step, Steps)
from e2a_inmemory_adapters import InMemoryCheckpointStore, InMemoryIdempotencyStore
from e2a_simulated_llm import SimulatedLLMService, always


class Pay(BaseToolService):
    material = True

    def __init__(self):
        self.calls = []

    def _get_endpoint(self, config=None, tenant_id=None, **kw): return "local://pay"
    def _build_payload(self, payload, config=None, **kw): return payload
    def _validate_input(self, payload, config=None, **kw): return True

    async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kw):
        self.calls.append(kw.get("idempotency_key"))
        return {"physical_success": True}


class Router(BaseAgent):
    def __init__(self):
        self.runs = 0

    def _prepare(self, state, ctx):
        self.runs += 1
        state["intent"] = "PAY" if "pay" in state["query"] else "STATUS"

    def _build_messages(self, state, ctx): return None


class Intake(BaseAgent):
    def __init__(self, confidence=1.0):
        self.runs, self.confidence = 0, confidence

    def _prepare(self, state, ctx):
        self.runs += 1

    def _build_messages(self, state, ctx): return [{"role": "user", "content": state["query"]}]
    def _evaluate_output(self, response, state, ctx): return self.confidence

    def _act(self, state, ctx):                       # no io_modify: intake commits nothing
        state["amount"] = "10.00"

    def _fallback(self, state, ctx): return {"text": "please say how much", "degraded": True}


class Payer(BaseAgent):
    def __init__(self):
        self.runs = 0

    def _prepare(self, state, ctx):
        self.runs += 1

    def _build_messages(self, state, ctx): return None

    def _act(self, state, ctx, io_modify):
        return [io_modify.call("pay", {"amount": state["amount"]})]


class Status(BaseAgent):
    def _build_messages(self, state, ctx): return None


class Approvals(BaseGovernanceFramework):
    stages = [ApprovalStage(required=lambda state, ctx: "amount" in state)]   # set by intake's _act


class Flow(BaseWorkflow):
    def __init__(self, intake_confidence=1.0):
        self.router, self.intake, self.payer = Router(), Intake(intake_confidence), Payer()

    def _build_workflow(self, ctx):
        return Steps(Step("route", self.router),
                     Step("intake", self.intake, when=lambda s: s["intent"] == "PAY"),
                     Route("act", by="intent", agents={"PAY": self.payer, "STATUS": Status()}, material=True))

    def _generate_idempotency_key(self, state, ctx):
        return f"pay-{state['user_id']}-{state.get('amount')}"

    def _requires_idempotency_claim(self, state, ctx):
        return state["intent"] == "PAY"

    def _handle_error(self, error, state, ctx, messages):
        state.setdefault("errors", []).append(str(error))


def config(**extra):
    return {"llm_service": SimulatedLLMService().add_rule("any", always(), "ok"), "min_confidence": 0.5,
            "tools": {"pay": Pay()}, "idempotency_store": InMemoryIdempotencyStore(),
            "checkpoint_store": InMemoryCheckpointStore(), **extra}


def request(query="pay my bill"):
    return {"tenant_id": "t1", "user_id": "u1", "query": query}


def test_three_steps_run_under_one_claim_and_a_repeat_is_replayed_as_a_whole():
    cfg, flow = config(), Flow()
    first = flow.execute(request(), cfg)
    assert [r["step"] for r in first["steps"]] == ["route", "intake", "act"]
    assert first["outcome"] == "COMMITTED" and cfg["tools"]["pay"].calls == ["pay-u1-10.00"]
    again = flow.execute(request(), cfg)
    assert again["status"] == "REPLAYED" and again["outcome"] == "COMMITTED"
    assert cfg["tools"]["pay"].calls == ["pay-u1-10.00"]            # never paid twice
    assert flow.payer.runs == 1


def test_a_skipped_step_is_recorded_and_a_read_only_request_takes_no_claim():
    cfg = config()
    state = Flow().execute(request("status please"), cfg)
    assert {"step": "intake", "status": "SKIPPED"} in state["steps"]
    assert cfg["idempotency_store"].state_of(state["idempotency_key"], "t1") is None


def test_a_fallback_ends_the_workflow_before_the_material_step():
    cfg, flow = config(), Flow(intake_confidence=0.1)
    state = flow.execute(request(), cfg)
    assert [r["step"] for r in state["steps"]] == ["route", "intake"]
    assert state["steps"][-1]["status"] == "FALLBACK" and state["response"]["degraded"]
    assert cfg["tools"]["pay"].calls == [] and flow.payer.runs == 0


def paused(flow=None, cfg=None):
    cfg = cfg or config(governance_engine=Approvals())
    flow = flow or Flow()
    state = flow.execute(request(), cfg)
    return flow, cfg, state


def test_a_pause_stores_a_checkpoint_and_resume_continues_at_the_paused_step():
    flow, cfg, state = paused()
    assert state["status"] == "AWAITING_WEBHOOK" and state["steps"][-1]["status"] == "PAUSED"
    assert cfg["tools"]["pay"].calls == []
    resumed = flow.resume(state["checkpoint_id"], {"approved": True, "decided_by": "boss"}, cfg, tenant_id="t1")
    assert resumed["outcome"] == "COMMITTED"
    assert (flow.router.runs, flow.intake.runs, flow.payer.runs) == (1, 1, 2)   # route + intake not re-run
    assert cfg["tools"]["pay"].calls == [state["idempotency_key"]]               # the original key, once
    assert [r["status"] for r in resumed["steps"]][-2:] == ["PAUSED", "DONE"]


def test_resume_refuses_an_unknown_checkpoint_a_second_decision_and_the_requester():
    flow, cfg, state = paused()
    with pytest.raises(ResumeRefused) as unknown:
        flow.resume("nope", {"approved": True, "decided_by": "boss"}, cfg, tenant_id="t1")
    assert unknown.value.code == "UNKNOWN_CHECKPOINT"
    with pytest.raises(ResumeRefused) as other_tenant:
        flow.resume(state["checkpoint_id"], {"approved": True, "decided_by": "boss"}, cfg, tenant_id="t2")
    assert other_tenant.value.code == "UNKNOWN_CHECKPOINT"
    with pytest.raises(ResumeRefused) as four_eyes:
        flow.resume(state["checkpoint_id"], {"approved": True, "decided_by": "u1"}, cfg, tenant_id="t1")
    assert four_eyes.value.code == "DECIDER_REFUSED"
    flow.resume(state["checkpoint_id"], {"approved": True, "decided_by": "boss"}, cfg, tenant_id="t1")
    with pytest.raises(ResumeRefused) as twice:
        flow.resume(state["checkpoint_id"], {"approved": True, "decided_by": "boss"}, cfg, tenant_id="t1")
    assert twice.value.code == "ALREADY_DECIDED" and len(cfg["tools"]["pay"].calls) == 1


def test_a_rejection_executes_nothing():
    flow, cfg, state = paused()
    rejected = flow.resume(state["checkpoint_id"], {"approved": False, "decided_by": "boss"}, cfg, tenant_id="t1")
    assert rejected["status"] == "REJECTED" and cfg["tools"]["pay"].calls == [] and flow.payer.runs == 1


def test_the_decision_hooks_can_be_specialised():
    class RoleFlow(Flow):
        def _authorize_decision(self, checkpoint, decision, ctx):
            return decision.get("role") == "FINANCE_MANAGER"

    flow, cfg, state = paused(RoleFlow())
    with pytest.raises(ResumeRefused):
        flow.resume(state["checkpoint_id"], {"approved": True, "decided_by": "boss"}, cfg, tenant_id="t1")
    ok = flow.resume(state["checkpoint_id"], {"approved": True, "decided_by": "boss", "role": "FINANCE_MANAGER"},
                     cfg, tenant_id="t1")
    assert ok["outcome"] == "COMMITTED"


@pytest.mark.v11_adapters        # exercises the v1.1 adapters on purpose (V13-01)
def test_a_v1_1_workflow_that_pauses_can_be_resumed_too():
    payer = Payer()

    class Old(BaseWorkflow):
        def _build_workflow(self, config=None, **kw): return {}
        def _validate_workflow(self, workflow, config=None, **kw): return True
        def _get_agent(self, intent, config=None, **kw): return payer
        def _generate_idempotency_key(self, state, config=None, **kw): return "old-1"
        def _handle_error(self, error, state, config=None, **kw): state["error"] = str(error)

    cfg = config(governance_engine=Approvals())
    state = Old().execute(dict(request(), intent="PAY", amount="5.00"), cfg)
    assert state["status"] == "AWAITING_WEBHOOK" and state["checkpoint_id"]
    done = Old().resume(state["checkpoint_id"], {"approved": True, "decided_by": "boss"}, cfg, tenant_id="t1")
    assert done["outcome"] == "COMMITTED" and cfg["tools"]["pay"].calls == ["old-1"]


def test_steps_are_validated():
    with pytest.raises(ValueError, match="unique"):
        Steps(Step("a", None), Step("a", None))
    with pytest.raises(ValueError, match="at least one step"):
        Steps()
