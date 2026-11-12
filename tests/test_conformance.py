"""v1.2 step 6: the conformance kit (design note 05 §3): rules R1-R3 (note 01), the
D1 deprecation, the command line, BasePipeline's default scaffold check and the
AgentContract pytest kit."""
import textwrap

from e2a import BaseAgent, BasePipeline
from e2a.conformance import AgentContract, check_sources, failed, main

PLANTED = {
    "agents.py": textwrap.dedent('''
        from e2a_base import BaseAgent as Agent, BaseRAGPipeline, RestToolService

        class Overrider(Agent):
            def run(self, state, config=None, **kw):          # R1: framework-owned public
                return state

            def __track_usage(self, *a):                      # R1: framework-private
                return 0

            def _build_messages(self, state, ctx):            # a hook: fine
                return None

        class Child(Overrider):                               # followed transitively
            def run(self, state, config=None, **kw):          # R1 again
                return state

        class Rag(BaseRAGPipeline):
            def _rerank(self, results, config=None, **kw):
                return results

            def score_all(self, answers):                     # R3: an extra public method
                return [self.evaluate_answer(a) for a in answers]

        class Old(RestToolService):
            def get_endpoint(self, config=None, tenant_id=None, **kw):   # D1
                return "http://x"
        '''),
    "service.py": textwrap.dedent('''
        class Service:
            def __init__(self, rag):
                self.rag = rag

            def answer(self, text):
                return self.rag._evaluate_answer(text)        # R2: a hook from outside

            def fine(self, text):
                self._log(text)                               # own protected method: fine
                return self.rag.evaluate_answer(text)
        '''),
}


def rules(findings):
    return sorted((f.rule, f.where) for f in findings)


def test_the_planted_violations_are_found_and_nothing_else():
    found = rules(check_sources(PLANTED))
    assert found == sorted([
        ("R1", "Overrider.run"), ("R1", "Overrider.__track_usage"), ("R1", "Child.run"),
        ("R3", "Rag.score_all"), ("D1", "Old.get_endpoint"), ("R2", "self.rag._evaluate_answer"),
    ])


def test_r1_fails_r2_and_d1_fail_only_when_strict_and_r3_never_fails():
    only = lambda rule: [f for f in check_sources(PLANTED) if f.rule == rule]   # noqa: E731
    assert failed(only("R1"))
    assert not failed(only("R2")) and failed(only("R2"), strict=True)
    assert not failed(only("D1")) and failed(only("D1"), strict=True)
    assert not failed(only("R3"), strict=True)


def test_a_conforming_class_with_several_capabilities_passes():
    clean = {"ok.py": textwrap.dedent('''
        from e2a import BaseWorkflow, Steps, Step

        class Flow(BaseWorkflow):
            def _build_workflow(self, ctx):
                return Steps(Step("only", None))
        ''')}
    assert check_sources(clean) == []


def test_the_command_line_exits_1_on_an_error(tmp_path, capsys):
    for name, text in PLANTED.items():
        (tmp_path / name).write_text(text)
    assert main([str(tmp_path)]) == 1
    assert "1 R2 warning(s), 1 D1 deprecation(s)" in capsys.readouterr().out
    (tmp_path / "agents.py").unlink()
    assert main([str(tmp_path), "--quiet"]) == 0
    assert main([str(tmp_path), "--strict"]) == 1


def test_base_pipeline_uses_the_rules_by_default(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "agents.py").write_text(PLANTED["agents.py"])

    class Gate(BasePipeline):
        conformance_paths = (str(tmp_path / "src"),)
        def _run_static_security_scans(self, config, **kw): return True
        def _execute_test_suite(self, config, **kw): return True
        def _run_rag_eval(self, config, **kw): return 1.0
        def _compile_artifact(self, artifact, config, **kw): return True
        def _run_dynamic_security_checks(self, config, **kw): return True
        def _execute_deployment_rollout(self, strategy, config, **kw): return {}

    gate = Gate()
    report = gate.run_pipeline({})
    assert report["status"] == "FAILED" and "Governed Entry Point" in report["error"]
    assert {f.rule for f in gate.conformance_findings} >= {"R1", "R3"}


class Deterministic(BaseAgent):
    def _build_messages(self, state, ctx):
        return None


class WithModel(BaseAgent):
    def _build_messages(self, state, ctx):
        return [{"role": "user", "content": state["query"]}]

    def _evaluate_output(self, response, state, ctx):
        return 0.9

    def _fallback(self, state, ctx):
        return {"text": "sorry", "degraded": True}


class TestDeterministicAgentContract(AgentContract):
    valid_state = {"tenant_id": "t1"}

    def make_agent(self):
        return Deterministic()


class TestModelAgentContract(AgentContract):
    valid_state = {"tenant_id": "t1", "query": "hello"}

    def make_agent(self):
        return WithModel()
