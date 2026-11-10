"""Smoke test: SimulatedLLMService behind the real BaseLLMAgentService pipeline.
Run: python smoke_test_simulated_llm.py"""
import asyncio
import json
import logging

from e2a_simulated_llm import SimulatedLLMService, system_contains, user_contains, user_matches

logging.disable(logging.CRITICAL)
ok = 0


def check(name, cond):
    global ok
    assert cond, name
    ok += 1
    print('PASS', name)


def msgs(system, user):
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]


INTENT = 'Classify the intent of the request.'
llm = (SimulatedLLMService()
       .add_rule('intent', system_contains('classify the intent'),
                 lambda m, ctx: json.dumps({'intent': 'FINANCE', 'confidence': 0.92}))
       .add_rule('tool', user_contains('check stock'),
                 lambda m, ctx: {'text': '', 'tool_calls': [{'name': 'check_stock', 'arguments': {'sku': 'A1'}}]})
       .add_rule('card', user_matches(r'echo card'), 'card 4111 1111 1111 1111 on file'))


async def main():
    r = await llm.process_inference(msgs(INTENT, 'pay invoice 7'), tenant_id='t1')
    check('first matching rule answers', json.loads(r['text'])['intent'] == 'FINANCE')
    check('physical_success contract returned', r['status'] == 'SUCCESS' and r['physical_success'])

    r2 = await llm.process_inference(msgs(INTENT, 'pay invoice 7'), tenant_id='t1')
    check('same prompt, same answer (deterministic)', r2['text'] == r['text'])

    r = await llm.process_inference(msgs('You are OrderOps.', 'please check stock for A1'), tenant_id='t1')
    check('a responder can return tool calls',
          r.get('action') == 'TOOL_CALL_DETECTED' and r['tool_calls'][0]['name'] == 'check_stock')

    r = await llm.process_inference(msgs('You are a helper.', 'something unknown'), tenant_id='t1')
    check('unmatched prompt gets the default response', 'no rule matched' in r['text'])

    r = await llm.process_inference(msgs('You are a helper.', 'echo card'), tenant_id='t1')
    check('egress DLP still redacts card numbers', '4111 1111 1111 1111' not in r['text'])

    chunks = [c async for c in llm.process_inference_stream(msgs(INTENT, 'x'), tenant_id='t1')]
    check('streaming yields the same text in chunks', json.loads(''.join(chunks))['intent'] == 'FINANCE')

    try:
        await llm.process_inference(msgs(INTENT, 'x'), tenant_id='')
        rejected = False
    except ValueError:
        rejected = True
    check('tenant_id is still mandatory (base pipeline enforced)', rejected)

    usage = llm.usage_summary()
    check('usage summary counts calls per rule',
          usage['by_rule'].get('intent', 0) >= 3 and usage['by_rule'].get('default') == 1)
    check('simulated cost is accounted', usage['cost_usd'] > 0 and usage['input_tokens'] > 0)


asyncio.run(main())
print(f'\n{ok} checks passed')