"""Smoke test (v1.1.2): fixes found while building the three Order-to-Cash projects.
  E-1  max_token_budget in config is enforced by the governance gate
  E-2  BaseMCPServer.serve() completes tools/call (no un-awaited coroutine)
  E-3  a latency SLO breach after the work is recorded, not raised
  E-4  query embedding is a protected hook (_embed_query) reaching _search_index
  E-5  run_sync() is the public bridge from sync code to async tools
  E-6  BaseValidationService subclasses can build results with _result()
Run: python smoke_test_v112.py"""
import asyncio
import logging
import time

import e2a_base
from e2a_base import (BaseAgent, BaseGovernanceFramework, BaseMCPServer, BaseRAGPipeline, BaseToolService,
                      BaseValidationService)
from e2a_simulated_llm import SimulatedLLMService, always

logging.disable(logging.CRITICAL)
ok = 0


def check(name, cond):
    global ok
    assert cond, name
    ok += 1
    print('PASS', name)


# ---------------------------------------------------------------- E-1
class Gov(BaseGovernanceFramework):
    def _check_execution_capacity(self, state, io_config, config=None, **kw): return True
    def _evaluate_dependency_health(self, config=None, **kw): return True
    def _execute_semantic_firewall(self, state, config, **kw): return state
    def _redact_output_fields(self, state, response, config=None, **kw): return response
    def _verify_sandbox_profile(self, config, **kw): pass
    def _circuit_breaker(self, failures, config, **kw): return False
    def _dehydrate_state_to_perimeter(self, state, correlation_id, tenant_id, config): pass


try:
    Gov().enforce_governance_gate({'cumulative_tokens_used': 500}, {}, {'max_token_budget': 100})
    check('E-1 budget in config is enforced', False)
except ValueError as e:
    check('E-1 budget in config is enforced', 'token budget exhausted' in str(e))
Gov().enforce_governance_gate({'cumulative_tokens_used': 50}, {}, {'max_token_budget': 100})
check('E-1 a request within the budget passes', True)


# ---------------------------------------------------------------- E-2
class EchoTool(BaseToolService):
    def get_endpoint(self, config=None, tenant_id=None, **kw): return 'local://echo'
    def _build_payload(self, payload, config=None, **kw): return payload
    def _validate_input(self, payload, config=None, **kw): return True
    async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kw):
        return {'physical_success': True, 'echo': body.get('x'), 'tenant': kw.get('tenant_id')}


class Server(BaseMCPServer):
    def __init__(self):
        super().__init__()
        self.wrap_api_as_tool(EchoTool(), 'echo', 'echo x', {'type': 'object'})


answer = Server().serve({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                         'params': {'name': 'echo', 'arguments': {'x': 7}}}, {}, tenant_id='t1')
check('E-2 tools/call returns a finished result', isinstance(answer['result'], dict))
check('E-2 the tool really ran', "'echo': 7" in answer['result']['content'][0]['text'])


# ---------------------------------------------------------------- E-3
class SlowAgent(BaseAgent):
    effects = 0
    def _build_messages(self, state, config=None, **kw): return [{'role': 'user', 'content': 'x'}]
    def _apply_policy(self, state, config=None, **kw):
        time.sleep(0.05)
        SlowAgent.effects += 1                     # a material effect that has committed
    def _evaluate_output(self, response, state, config=None, **kw): return 1.0
    def _fallback(self, state, config=None, **kw): return {'text': ''}
    def _handle_error(self, error, state, config=None, **kw): state['error'] = repr(error)


cfg = {'llm_service': SimulatedLLMService().add_rule('any', always(), 'ok'), 'max_latency': 0.01}
try:
    st = SlowAgent().run({'tenant_id': 't1'}, cfg)
    check('E-3 a late but committed run is not an error', SlowAgent.effects == 1)
    check('E-3 the breach is recorded', st.get('slo_breached', {}).get('max_latency') == 0.01)
except Exception as e:                              # v1.1.1: NFRViolationError after the effect
    check(f'E-3 a late but committed run is not an error ({type(e).__name__})', False)


# ---------------------------------------------------------------- E-4
class Rag(BaseRAGPipeline):
    def _embed_query(self, query, config=None, **kw): return [float(len(query))]
    def _search_index(self, query_text, query_vector, config=None, tenant_id=None, **kw):
        return [{'vector': query_vector}]
    def _rerank(self, results, config=None, **kw): return results
    def _evaluate_answer(self, answer, config=None, **kw): return 1.0


check('E-4 the subclass embedding reaches _search_index', Rag().retrieve('abcd', {}, tenant_id='t')[0]['vector'] == [4.0])


# ---------------------------------------------------------------- E-5
check('E-5 run_sync is public', hasattr(e2a_base, 'run_sync'))
result = e2a_base.run_sync(EchoTool().execute({'x': 3}, {}, tenant_id='t2'), timeout=5)
check('E-5 run_sync returns the tool result', result['echo'] == 3 and result['tenant'] == 't2')


# ---------------------------------------------------------------- E-6
class Validation(BaseValidationService):
    def __init__(self):
        self.validator_registry = {'A': lambda state, config=None, **kw: (True, [])}
    def _enforce_entitlement_gate(self, agent_name, state, config=None, **kw):
        if state.get('role') != 'admin':
            return self._result(False, ['role not allowed'], agent_name, state.get('correlation_id'), forbidden=True)
        return self._result(True, [], agent_name, state.get('correlation_id'))


denied = Validation().validate('A', {'role': 'viewer'})
check('E-6 _result builds a ValidationResult with extra fields',
      denied['valid'] is False and denied['forbidden'] is True and denied['errors'] == ['role not allowed'])
check('E-6 a granted result', Validation().validate('A', {'role': 'admin'})['valid'] is True)

print(f'\n{ok} checks passed')
