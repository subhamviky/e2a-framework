"""v1.2 step 2: the BOPF-style propagation contract (design note 02) and the agent
steps it enables (note 03): role-separated, declared parameters; io_read / io_modify;
messages and failed_keys as outputs; one context per request."""
import dataclasses
import time
import warnings

import pytest

from e2a import (BaseAgent, BaseToolService, BaseWorkflow, MessageLog, ModifyAccess, PhysicalOutcome,
                 ReadAccess, RequestContext)
from e2a_inmemory_adapters import InMemoryIdempotencyStore
from e2a_simulated_llm import SimulatedLLMService, always


class Tool(BaseToolService):
    def __init__(self, material=False, fail=False):
        self.material, self.fail, self.calls = material, fail, []

    def _get_endpoint(self, config=None, tenant_id=None, **kw):
        return "local://tool"

    def _build_payload(self, payload, config=None, **kw):
        return payload

    def _validate_input(self, payload, config=None, **kw):
        return True

    async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kw):
        self.calls.append({"body": body, "key": kw.get("idempotency_key"), "tenant": kw["ctx"].tenant_id})
        if self.fail:
            raise ConnectionError("connection reset")
        return {"physical_success": True}


def llm(text='{"ok": true}'):
    return SimulatedLLMService().add_rule("any", always(), text)


def config(**extra):
    return {"llm_service": llm(), "min_confidence": 0.5, "max_latency": 10,
            "tools": {"lookup": Tool(), "pay": Tool(material=True)}, **extra}


class Recorder(BaseAgent):
    """A v1.2 agent that records what each step received."""

    def __init__(self, confidence=1.0, deterministic=False):
        self.confidence, self.deterministic, self.seen = confidence, deterministic, {}

    def _prepare(self, state, ctx, io_read, messages):
        self.seen["prepare"] = (ctx, io_read)
        state["fact"] = io_read.call("lookup", {"q": 1})["outcome"].value

    def _apply_policy(self, state, ctx, messages, failed_keys):
        self.seen["policy"] = ctx

    def _build_messages(self, state, ctx):
        return None if self.deterministic else [{"role": "user", "content": "hi"}]

    def _evaluate_output(self, response, state, ctx, messages):
        return self.confidence

    def _fallback(self, state, ctx, messages):
        return {"text": "fallback", "degraded": True}

    def _act(self, state, ctx, io_modify, messages, failed_keys):
        self.seen["act"] = True
        return [io_modify.call("pay", {"amount": "10.00"})]

    def _handle_error(self, error, state, ctx, messages, failed_keys):
        state.setdefault("errors", []).append(str(error))


def test_each_step_receives_only_what_it_declares():
    agent = Recorder()
    state = agent.run({"tenant_id": "t1"}, config(), idempotency_key="k1")
    ctx, io_read = agent.seen["prepare"]
    assert isinstance(ctx, RequestContext) and isinstance(io_read, ReadAccess)
    assert ctx is agent.seen["policy"]                      # one context for the whole run
    assert state["fact"] == "NOT_COMMITTED"                 # a read commits nothing
    assert state["outcome"] == "COMMITTED" and "errors" not in state


def test_the_context_is_frozen_and_kept_out_of_the_state():
    agent = Recorder()
    state = agent.run({"tenant_id": "t1"}, config(), idempotency_key="k1")
    ctx, _ = agent.seen["prepare"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.tenant_id = "other"
    assert not any(isinstance(v, RequestContext) for v in state.values())
    assert ctx.child(idempotency_key="k2").tenant_id == "t1"


def test_a_step_cannot_declare_a_parameter_its_role_does_not_allow():
    class Sneaky(Recorder):
        def _prepare(self, state, ctx, io_modify):          # material access before acting
            io_modify.call("pay", {})

    state = Sneaky().run({"tenant_id": "t1"}, config(), idempotency_key="k1")
    assert any("'io_modify' is not available in prepare" in e for e in state["errors"])
    assert config()["tools"]["pay"].calls == []


def test_reads_and_material_effects_use_different_accessors():
    agent = Recorder()
    cfg = config()
    agent.run({"tenant_id": "t1"}, cfg, idempotency_key="order-7")
    assert cfg["tools"]["pay"].calls[0]["key"] == "order-7"          # material: always with the key
    assert cfg["tools"]["pay"].calls[0]["tenant"] == "t1"
    ctx = RequestContext.begin(tenant_id="t1", config=cfg, idempotency_key="k")
    with pytest.raises(PermissionError, match="material; it cannot be called through io_read"):
        ReadAccess(ctx, cfg).call("pay", {})
    with pytest.raises(PermissionError, match="read-only; it cannot be called through io_modify"):
        ModifyAccess(ctx, cfg).call("lookup", {})


def test_allowed_tools_limit_what_an_agent_can_reach():
    class Limited(Recorder):
        allowed_tools = {"lookup"}

    state = Limited().run({"tenant_id": "t1"}, config(), idempotency_key="k1")
    assert any("'pay' is not allowed here" in e for e in state["errors"])


def test_nothing_is_executed_when_the_evaluation_is_not_accepted():
    agent = Recorder(confidence=0.1)
    cfg = config()
    state = agent.run({"tenant_id": "t1"}, cfg, idempotency_key="k1")
    assert "act" not in agent.seen and cfg["tools"]["pay"].calls == []
    assert state["response"]["degraded"] and "outcome" not in state


def test_a_deterministic_agent_skips_the_model_and_still_acts():
    cfg = config()
    state = Recorder(deterministic=True).run({"tenant_id": "t1"}, cfg, idempotency_key="k1")
    assert cfg["llm_service"].usage_summary()["calls"] == 0 and state["outcome"] == "COMMITTED"


def test_no_material_effect_after_the_deadline():
    class Slow(Recorder):
        def _apply_policy(self, state, ctx, messages):
            time.sleep(0.05)

    cfg = config()
    state = Slow().run({"tenant_id": "t1"}, cfg, idempotency_key="k1", deadline=0.01)
    assert cfg["tools"]["pay"].calls == [] and state["outcome"] == "NOT_COMMITTED"


def test_a_transport_failure_of_a_material_call_is_unknown_and_parks_the_claim():
    class Flow(BaseWorkflow):
        def _build_workflow(self, config=None, **kw): return {}
        def _validate_workflow(self, workflow, config=None, **kw): return True
        def _get_agent(self, intent, config=None, **kw): return Recorder(deterministic=True)
        def _generate_idempotency_key(self, state, config=None, **kw): return "order-9"
        def _handle_error(self, error, state, config=None, **kw): state.setdefault("errors", []).append(str(error))

    cfg = config(idempotency_store=InMemoryIdempotencyStore(), retries=0)
    cfg["tools"]["pay"] = Tool(material=True, fail=True)
    first = Flow().execute({"tenant_id": "t1"}, cfg)
    assert first["outcome"] == "UNKNOWN"
    assert Flow().execute({"tenant_id": "t1"}, cfg)["status"] == "RECOVERY_REQUIRED"
    assert len(cfg["tools"]["pay"].calls) == 1                       # never re-sent blindly


def test_the_workflow_creates_one_context_and_the_agent_receives_it():
    agent = Recorder()

    class Flow(BaseWorkflow):
        def _build_workflow(self, config=None, **kw): return {}
        def _validate_workflow(self, workflow, config=None, **kw): return True
        def _get_agent(self, intent, config=None, **kw): return agent
        def _generate_idempotency_key(self, state, config=None, **kw): return "derived"
        def _handle_error(self, error, state, config=None, **kw): pass

    Flow().execute({"tenant_id": "t1"}, config(), idempotency_key="client-key", correlation_id="corr-1")
    ctx = agent.seen["policy"]
    assert (ctx.tenant_id, ctx.correlation_id, ctx.idempotency_key) == ("t1", "corr-1", "client-key")
    given = RequestContext.begin(tenant_id="t1", config=config(), correlation_id="corr-2", idempotency_key="k-2")
    Flow().execute({"tenant_id": "t1"}, config(), ctx=given)
    assert (agent.seen["policy"].correlation_id, agent.seen["policy"].idempotency_key) == ("corr-2", "k-2")


def test_a_context_and_a_different_keyword_are_refused():
    ctx = RequestContext.begin(tenant_id="t1", config=config(), idempotency_key="k1")
    with pytest.raises(ValueError, match="idempotency_key given both in ctx and as a keyword"):
        Recorder().run({"tenant_id": "t1"}, config(), ctx=ctx, idempotency_key="other")


def test_settings_are_resolved_once_for_every_registered_key_and_app_keys():
    ctx = RequestContext.begin(tenant_id="t1", config={"max_token_budget": 500, "approval_threshold": 5000})
    assert ctx.setting("max_token_budget") == 500           # not only io_-prefixed keys (E-1's root cause)
    assert ctx.setting("approval_threshold") == 5000        # an application key
    assert ctx.setting("min_confidence") == 0.85            # registry default
    with pytest.raises(KeyError):
        ctx.setting("no_such_key")


def test_outputs_are_explicit():
    log = MessageLog(correlation_id="c")
    log.info("hello", n=1)
    assert log.entries[0]["event"] == "hello" and len(log) == 1
    agent = Recorder()
    state = agent.run({"tenant_id": "t1"}, config(), idempotency_key="k1", message_log=[])
    assert state["outcome"] == "COMMITTED"


def test_a_v1_1_agent_is_refused_when_its_class_is_defined():
    """v1.3: the v1.1 keyword signature (deprecated in v1.2) is refused, naming the hook."""
    with pytest.raises(TypeError, match=r"Old\._build_messages uses the v1.1 keyword signature, removed in E2A v1.3"):
        class Old(BaseAgent):
            def _build_messages(self, state, config=None, **kw): return [{"role": "user", "content": "x"}]
    assert PhysicalOutcome  # imported for the type
