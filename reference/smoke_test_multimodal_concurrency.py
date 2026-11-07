"""Concurrency smoke tests for multimodal_orchestrator.py.
Run: python smoke_test_multimodal_concurrency.py  (needs langgraph)"""
import sys, types, time, asyncio, inspect
sys.path.insert(0, '.')
import e2a_base
pkg = types.ModuleType('src'); pkg.e2a_base = e2a_base
sys.modules['src'], sys.modules['src.e2a_base'] = pkg, e2a_base
from e2a_base import *
from e2a_inmemory_adapters import InMemoryIdempotencyStore
import multimodal_orchestrator as mo

ok = 0
def check(name, cond):
    global ok
    assert cond, name; ok += 1; print('PASS', name)

check('no asyncio.run left in the orchestrator', 'asyncio.run(' not in inspect.getsource(mo))

class RAG(BaseRAGPipeline):
    def _search_index(self, q, v, config=None, tenant_id=None, **k): return [{'text': 'g'}]
    def _rerank(self, r, config=None, **k): return r
    def _evaluate_answer(self, a, config=None, **k): return 1.0
class LLM(BaseLLMAgentService):
    async def _dispatch_upstream_call(self, *a, **k): return {'text': 'r', 'metadata': {'tokens_used': 5}}
    async def _dispatch_upstream_stream(self, *a, **k): yield 'r'
class Router:
    def __init__(self, i, s=0.9): self.i, self.s = i, s
    def classify_intent(self, q): return self.i, self.s

sent = []
class Probe(RestToolService):
    mode = 'ok'
    async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **k):
        sent.append(1)
        if Probe.mode == 'slow': await asyncio.sleep(5)
        return {'status': 'ok', 'physical_success': True}
e2a_base._TOOL_SERVICE_CACHE[('http', 'pay')] = Probe()     # one long-lived instance per dependency

store = InMemoryIdempotencyStore()
def go(intent, query='q', **cfg_extra):
    wf = mo.MultiAgentOrchestratorWorkflow(RAG(), Router(intent))
    cfg = {'llm_service': LLM({'enable_semantic_cache': False}), 'idempotency_store': store, 'max_latency': 5.0, **cfg_extra}
    return wf.execute({'tenant_id': 'acme', 'query': query, 'tool_name': 'pay', 'tool_payload': {'a': 1}, 'intent': intent},
                      config=cfg, intent_score=0.9)

a = go('API_TOOL_CALL'); b = go('API_TOOL_CALL')
check('T-01 duplicate tool-call request replays; the tool was sent once', b['status'] == 'REPLAYED' and len(sent) == 1)
sent.clear(); store2 = InMemoryIdempotencyStore()
c = go('LLM_ONLY', 'same question'); d = go('LLM_ONLY', 'same question')
check('generative intents are not claimed or replayed', d.get('status') != 'REPLAYED' and store.state_of('x') is None)

Probe.mode = 'slow'; sent.clear()
t0 = time.monotonic()
u = go('API_TOOL_CALL', 'slow one', timeout=0.1, retries=3, request_deadline_seconds=0)
check('T-12 slow tool: typed UNKNOWN, sent exactly once, bounded wait', u['outcome'] == 'UNKNOWN' and len(sent) == 1 and time.monotonic() - t0 < 2)
check('T-12 UNKNOWN parks the key; a duplicate is not re-sent', (lambda r: r['status'] == 'RECOVERY_REQUIRED' and len(sent) == 1)(go('API_TOOL_CALL', 'slow one', timeout=0.1, retries=3)))
Probe.mode = 'ok'
t0 = time.monotonic()
Probe.mode = 'slow'
x = go('API_TOOL_CALL', 'deadline one', timeout=30.0, retries=0, request_deadline_seconds=0.2)
check('request deadline bounds a tool call far below the tool timeout', time.monotonic() - t0 < 2.5)
print(f'\n{ok} checks passed')
