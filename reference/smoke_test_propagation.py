"""Smoke test (v1.1.1): propagation fields given to a public entry point reach the hooks
exactly once.
  1. BaseWorkflow.execute(state, config, idempotency_key=..., correlation_id=...) must not
     pass those keys to agent.run() twice (v1.1.0: TypeError "multiple values").
  2. BaseToolService.execute(payload, config, tenant_id=...) must hand tenant_id on to
     _build_payload() and _send_request() (v1.1.0: lost after get_endpoint()).
Run: python smoke_test_propagation.py"""
import asyncio
import logging

from e2a_base import BaseAgent, BaseToolService, BaseWorkflow
from e2a_inmemory_adapters import InMemoryIdempotencyStore
from e2a_simulated_llm import SimulatedLLMService, always

logging.disable(logging.CRITICAL)
ok = 0


def check(name, cond):
    global ok
    assert cond, name
    ok += 1
    print('PASS', name)


class EchoAgent(BaseAgent):
    def _build_messages(self, state, config=None, **kw):
        return [{'role': 'user', 'content': 'hi'}]

    def _apply_policy(self, state, config=None, **kw):
        state['seen_key'] = state.get('idempotency_key')

    def _evaluate_output(self, response, state, config=None, **kw):
        return 1.0

    def _fallback(self, state, config=None, **kw):
        return {'text': ''}

    def _handle_error(self, error, state, config=None, **kw):
        state.setdefault('errors', []).append(repr(error))


class Flow(BaseWorkflow):
    def _build_workflow(self, config=None, **kw):
        return {}

    def _validate_workflow(self, workflow, config=None, **kw):
        return True

    def _get_agent(self, intent, config=None, **kw):
        return EchoAgent()

    def _generate_idempotency_key(self, state, config=None, **kw):
        return 'derived-key'

    def _handle_error(self, error, state, config=None, **kw):
        state.setdefault('errors', []).append(repr(error))


config = {'llm_service': SimulatedLLMService().add_rule('any', always(), 'ok'),
          'idempotency_store': InMemoryIdempotencyStore(), 'min_confidence': 0.5, 'max_latency': 10}
state = Flow().execute({'tenant_id': 't1', 'intent': 'x'}, config,
                       idempotency_key='client-key', correlation_id='corr-1')
check('client idempotency_key as a keyword runs the agent (no duplicate keyword)', not state.get('errors'))
check('the client key wins over the derived one', state['seen_key'] == 'client-key')
check('correlation_id as a keyword is kept', state['correlation_id'] == 'corr-1')
again = Flow().execute({'tenant_id': 't1', 'intent': 'x'}, config, idempotency_key='client-key')
check('the same key again is a replay', again.get('status') == 'REPLAYED')


class TenantTool(BaseToolService):
    def get_endpoint(self, config=None, tenant_id=None, **kw):
        return f'local://{tenant_id}'

    def _build_payload(self, payload, config=None, **kw):
        return dict(payload, built_for=kw.get('tenant_id'))

    def _validate_input(self, payload, config=None, **kw):
        return True

    async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kw):
        return {'physical_success': True, 'endpoint': endpoint, 'body': body, 'sent_for': kw.get('tenant_id')}


result = asyncio.run(TenantTool().execute({'x': 1}, {}, tenant_id='t9'))
check('get_endpoint receives tenant_id', result['endpoint'] == 'local://t9')
check('_build_payload receives tenant_id', result['body']['built_for'] == 't9')
check('_send_request receives tenant_id', result['sent_for'] == 't9')
print(f'\n{ok} checks passed')
