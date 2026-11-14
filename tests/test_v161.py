"""v1.6.1 (design note MP-04): vendor-neutral handling for managed providers. Model routes (P8),
the features map and structured-output checks (P9), telemetry and cost (P12), the
delegated-runtime base (P15), the reranker port and dimension check (P17), classifier
guardrail stages (P21), adaptive admission and the quota authority (P7 b, N10), pipeline
checks (P24), MCP per instance (P19), and agent and tenant scopes (P3)."""
import asyncio
import json
import time

import pytest

import e2a
from e2a import (BaseAgent, BaseCredentialProvider, BaseEmbeddingModel, BaseLLMAgentService, BaseMCPServer,
                 BaseRAGPipeline, BaseReranker, BaseToolService, BaseVectorIndex, DelegatedRuntime,
                 DelegatedTurnLimit, FirewallRefusal, GuardrailVerdict, MCPToolService,
                 ManagedGuardrailEgressStage, ManagedGuardrailStage, MaterialToolRefused, ModelResult,
                 StopReason, Usage, bindings_from_profile, check_dimensions, schema_errors, scoped_config)
from e2a.bindings import Profile
from e2a.context import MessageLog

SYSTEM, USER = {'role': 'system', 'content': 'You are terse.'}, {'role': 'user', 'content': 'hi'}


class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f'HTTP {status}')
        self.status_code = status


class Adapter(BaseLLMAgentService):
    """A provider adapter whose wire is a responder(messages, kwargs)."""

    def __init__(self, responder, config=None, features=None, capabilities=(), **kwargs):
        super().__init__(config, **kwargs)
        self.responder, self.seen = responder, []
        if features is not None:
            self.features = features
        self.capabilities = frozenset(capabilities)

    async def _dispatch_upstream_call(self, messages, model_id, temperature, max_tokens, tools, stop_sequences,
                                      cache_handle, config, **kwargs):
        self.seen.append({'model_id': model_id, 'temperature': temperature, **kwargs})
        answer = self.responder(model_id)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict):
            return ModelResult(text=answer.get('text', ''), stop_reason=answer.get('stop_reason'), usage=Usage(1000, 500))
        return ModelResult(text=answer, stop_reason=StopReason.END_TURN, usage=Usage(1000, 500))

    async def _dispatch_upstream_stream(self, messages, model_id, temperature, max_tokens, config, **kwargs):
        yield str(self.responder(model_id))


def infer(svc, config=None, **kw):
    return asyncio.run(svc.process_inference([SYSTEM, USER], config, tenant_id='t1', **kw))


NO_RETRY = {'llm_max_retries': 0, 'enable_semantic_cache': False}


# ------------------------------------------------------------------ P8 model routes
def test_a_failure_kind_in_a_route_is_answered_by_the_route_model():
    svc = Adapter(lambda m: HTTPError(503) if m == 'big' else f'answer from {m}')
    failed = []
    out = infer(svc, {**NO_RETRY, 'model_id': 'big',
                      'model_routes': [{'on': ['overloaded', 'transient'], 'to': 'small'}]}, failed_keys=failed)
    assert out['status'] == 'SUCCESS' and out['text'] == 'answer from small'
    assert out['fallback_from'] == {'model_id': 'big', 'reason': out['fallback_from']['reason'], 'service': 'Adapter'}
    assert out['fallback_from']['reason'] in ('overloaded', 'transient')
    assert failed == []                       # the routed answer stands: no failed key


def test_a_kind_not_in_any_route_stays_failed_and_routes_run_once():
    calls = []
    svc = Adapter(lambda m: calls.append(m) or HTTPError(400))
    failed = []
    out = infer(svc, {**NO_RETRY, 'model_id': 'big', 'model_routes': [{'on': ['overloaded'], 'to': 'small'}]},
                failed_keys=failed)
    assert out['status'] == 'FAILED' and out['error_kind'] == 'deterministic' and calls == ['big'] and failed
    calls.clear()
    out = infer(Adapter(lambda m: calls.append(m) or HTTPError(503)),
                {**NO_RETRY, 'model_id': 'big', 'model_routes': [{'on': ['overloaded', 'transient'], 'to': 'small'}]})
    assert out['status'] == 'FAILED'          # a failing route is not itself routed again


def test_a_refusal_route_goes_to_another_service_unless_the_provider_falls_back_itself():
    other = Adapter(lambda m: 'answered elsewhere')
    refusing = Adapter(lambda m: {'text': 'no', 'stop_reason': 'refusal'})
    out = infer(refusing, {**NO_RETRY, 'model_routes': [{'on': ['refusal'], 'llm_service': other}]})
    assert out['status'] == 'SUCCESS' and out['text'] == 'answered elsewhere' and out['fallback_from']['reason'] == 'refusal'
    server_side = Adapter(lambda m: {'text': 'no', 'stop_reason': 'refusal'}, capabilities={'refusal_fallback_server'})
    out = infer(server_side, {**NO_RETRY, 'model_routes': [{'on': ['refusal'], 'llm_service': other}]})
    assert out['status'] == 'REFUSED'


def test_a_route_to_an_unbound_seam_name_is_an_error():
    svc = Adapter(lambda m: {'text': 'no', 'stop_reason': 'refusal'})
    with pytest.raises(ValueError, match='not a bound'):
        infer(svc, {**NO_RETRY, 'model_routes': [{'on': ['refusal'], 'llm_service': '@delegated_runtime'}]})


def test_bindings_resolve_seam_references_in_routes_and_scopes():
    b = bindings_from_profile(Profile(name='p', provider='x', bindings={
        'llm_service': 'e2a_simulated_llm:SimulatedLLMService',
        'delegated_runtime': 'e2a_simulated_llm:SimulatedLLMService'}), overrides={
        'model_routes': [{'on': ['refusal'], 'llm_service': '@delegated_runtime'}],
        'scopes': {'agent:research': {'llm_service': '@delegated_runtime'}}})
    cfg = b.config()
    assert cfg['model_routes'][0]['llm_service'] is b.delegated_runtime
    assert cfg['scopes']['agent:research']['llm_service'] is b.delegated_runtime
    with pytest.raises(ValueError, match='unknown seam'):
        bindings_from_profile(Profile(name='p', provider='x'), overrides={'model_routes': [{'llm_service': '@nope'}]}).config()


# ------------------------------------------------------------------ P9 features and structured output
SCHEMA = {'type': 'object', 'required': ['amount'], 'additionalProperties': False,
          'properties': {'amount': {'type': 'number', 'minimum': 0}, 'currency': {'enum': ['EUR', 'USD']}}}


def test_the_schema_check_covers_the_structured_output_keywords():
    assert schema_errors(SCHEMA, {'amount': 3, 'currency': 'EUR'}) == []
    errs = schema_errors(SCHEMA, {'amount': -1, 'currency': 'GBP', 'x': 1})
    assert any('minimum' in e for e in errs) and any('GBP' in e for e in errs) and any("'x'" in e for e in errs)
    assert schema_errors({'type': 'array', 'items': {'type': 'integer'}, 'maxItems': 2}, [1, 'a', 3]) == \
        ['$: more than 2 items', "$[1]: expected integer, got str"]
    assert schema_errors({'oneOf': [{'type': 'string'}, {'type': 'integer'}]}, 1.5) == ['$: matches 0 of the oneOf options']
    assert schema_errors({'type': 'integer'}, True) == ['$: expected integer, got bool']


def test_every_structured_result_is_checked_and_strict_makes_a_mismatch_fail():
    svc = Adapter(lambda m: '{"amount": -5}')
    out = infer(svc, NO_RETRY, response_schema=SCHEMA)
    assert out['status'] == 'SUCCESS' and out['structured_output_valid'] is False and out['physical_success'] is False
    assert out['schema_errors'] == ['$.amount: -5 violates minimum 0']
    failed = []
    out = infer(svc, {**NO_RETRY, 'structured_output_strict': True}, response_schema=SCHEMA, failed_keys=failed)
    assert out['status'] == 'FAILED' and out['error_type'] == 'StructuredOutputInvalid' and failed
    good = infer(Adapter(lambda m: '{"amount": 5}'), NO_RETRY, response_schema=SCHEMA)
    assert good['structured_output_valid'] is True and good['physical_success'] is True
    bad_json = infer(Adapter(lambda m: 'not json'), NO_RETRY, response_schema=SCHEMA)
    assert bad_json['structured_output_valid'] is False and 'not valid JSON' in bad_json['schema_errors'][0]


def test_features_decide_what_is_sent_not_the_vendor():
    svc = Adapter(lambda m: '{"amount": 1}', features={'structured_output': 'none'})
    out = infer(svc, NO_RETRY, response_schema=SCHEMA)
    assert out['status'] == 'FAILED' and out['error_type'] == 'StructuredOutputUnsupported'
    log = []
    svc = Adapter(lambda m: 'ok', features={'sampling': 'unsupported', 'reasoning': 'none'})
    infer(svc, {**NO_RETRY, 'temperature': 0.7}, reasoning_effort='high', message_log=log)
    assert svc.seen[0]['temperature'] is None and 'reasoning_effort' not in svc.seen[0]
    assert [e.get('param') for e in log if e.get('event') == 'param_dropped'] == ['temperature', 'reasoning_effort']
    out = infer(Adapter(lambda m: 'ok'), NO_RETRY, reasoning_effort='extreme')
    assert out['status'] == 'FAILED' and 'reasoning_effort' in out['error']
    plain = Adapter(lambda m: 'ok')
    infer(plain, {**NO_RETRY, 'temperature': 0.7}, reasoning_effort='low')
    assert plain.seen[0]['temperature'] == 0.7 and plain.seen[0]['reasoning_effort'] == 'low'


# ------------------------------------------------------------------ P12 telemetry and cost
def test_results_carry_cost_and_telemetry_reaches_the_engine_and_the_hook():
    seen = []

    class Engine:
        def record_inference(self, attributes):
            seen.append(attributes)

    prices = {'m1': {'input_per_mtok': 3.0, 'output_per_mtok': 15.0}}
    out = infer(Adapter(lambda m: 'ok'), {**NO_RETRY, 'model_id': 'm1', 'model_price_table': prices,
                                          'observability_engine': Engine()}, agent_name='triage', workflow_step='s1')
    assert out['cost_usd'] == pytest.approx((1000 * 3 + 500 * 15) / 1e6)
    a = seen[0]
    assert a['e2a.tenant_id'] == 't1' and a['e2a.agent'] == 'triage' and a['e2a.workflow_step'] == 's1'
    assert a['gen_ai.request.model'] == 'm1' and a['gen_ai.usage.input_tokens'] == 1000 and a['e2a.cost_usd'] == out['cost_usd']
    assert 'cost_usd' not in infer(Adapter(lambda m: 'ok'), {**NO_RETRY, 'model_id': 'unpriced', 'model_price_table': prices})


def test_an_agent_names_itself_to_the_service():
    seen = []

    class Svc(Adapter):
        def _record_telemetry(self, attributes, config):
            seen.append(attributes)

    class Triage(BaseAgent):
        agent_name = 'triage'

        def _build_messages(self, state, ctx):
            return [SYSTEM, USER]

        def _evaluate_output(self, response, state, ctx, messages):
            return 1.0

    Triage().run({'tenant_id': 't1'}, {**NO_RETRY, 'llm_service': Svc(lambda m: 'ok')}, tenant_id='t1')
    assert seen[0]['e2a.agent'] == 'triage'


# ------------------------------------------------------------------ P7 (b) adaptive admission, N10
def test_a_throttle_lowers_the_pod_share_and_it_recovers():
    svc = Adapter(lambda m: HTTPError(429), {**NO_RETRY, 'max_rpm': 600, 'admission_recovery_seconds': 10})
    out = infer(svc, {**NO_RETRY, 'admission_recovery_seconds': 10})
    assert out['status'] == 'FAILED' and out['error_kind'] == 'throttled'
    assert svc._throttle_factor == pytest.approx(0.5, abs=0.01) and svc.stats['throttled'] == 1
    assert svc._rpm_tokens <= 600 * 0.5
    for _ in range(5):
        infer(svc, {**NO_RETRY, 'admission_recovery_seconds': 10})
    assert svc._throttle_factor == pytest.approx(0.1, abs=0.02)        # the floor
    svc.responder = lambda m: 'ok'
    svc._last_bucket_update = time.monotonic() - 20                       # recovered after the window
    assert infer(svc, {**NO_RETRY, 'admission_recovery_seconds': 10})['status'] == 'SUCCESS'
    assert svc._throttle_factor == 1.0


def test_a_gateway_quota_authority_skips_the_pod_bucket():
    svc = Adapter(lambda m: 'ok', {'max_rpm': 1})
    svc._rpm_tokens = 0                       # the pod bucket is empty
    out = infer(svc, {**NO_RETRY, 'quota_authority': 'gateway', 'quota_timeout': 0.05})
    assert out['status'] == 'SUCCESS'


# ------------------------------------------------------------------ P3 scopes
def test_scopes_overlay_tenant_then_agent_and_refuse_bad_keys():
    cfg = {'temperature': 0.5, 'scopes': {'tenant:acme': {'max_output_tokens': 100, 'temperature': 0.2},
                                          'agent:triage': {'temperature': 0.0}}}
    out = scoped_config(cfg, 'triage', 'acme')
    assert out['temperature'] == 0.0 and out['max_output_tokens'] == 100
    assert scoped_config(cfg, 'other', 'acme')['temperature'] == 0.2
    assert scoped_config(cfg, 'other', 'globex')['temperature'] == 0.5
    assert scoped_config({'temperature': 1}, 'a', 't') == {'temperature': 1}
    with pytest.raises(ValueError, match='may not set tenant_id'):
        scoped_config({'scopes': {'agent:a': {'tenant_id': 'x'}}}, 'a')
    with pytest.raises(ValueError, match='unregistered'):
        scoped_config({'scopes': {'agent:a': {'temprature': 0}}}, 'a')
    with pytest.raises(ValueError, match='must be "tenant'):
        scoped_config({'scopes': {'team:a': {}}}, 'a')


def test_an_agent_scope_binds_its_own_llm_service():
    default, research = Adapter(lambda m: 'default'), Adapter(lambda m: 'research')

    class Research(BaseAgent):
        agent_name = 'research'

        def _build_messages(self, state, ctx):
            return [SYSTEM, USER]

        def _evaluate_output(self, response, state, ctx, messages):
            state['answer'] = response['text']
            return 1.0

    cfg = {**NO_RETRY, 'llm_service': default, 'scopes': {'agent:research': {'llm_service': research}}}
    assert Research().run({'tenant_id': 't1'}, cfg, tenant_id='t1')['answer'] == 'research'
    assert research.seen and not default.seen


# ------------------------------------------------------------------ P15 delegated runtime
class ReadTool(e2a.RestToolService):
    async def execute(self, payload, config=None, tenant_id=None, failed_keys=None, **kwargs):
        return {'physical_success': True, 'tenant': tenant_id, 'q': payload.get('q')}


class WriteTool(ReadTool):
    material = True


class Runtime(DelegatedRuntime):
    async def _run_session(self, messages, config, tenant_id, session, kwargs):
        results = [json.loads(await self.run_tool(name, args, tenant_id, config, session))
                   for name, args in self.plan]
        return ModelResult(text=json.dumps(results), stop_reason=StopReason.END_TURN)


def test_a_delegated_runtime_reads_through_tools_and_never_writes():
    with pytest.raises(MaterialToolRefused):
        Runtime(tools={'pay': (WriteTool(), {'parameters': {}})})
    rt = Runtime(tools={'find': (ReadTool(), {'description': 'find', 'parameters': {'type': 'object'}})})
    assert rt.tool_declarations() == [{'name': 'find', 'description': 'find', 'parameters': {'type': 'object'}}]
    assert 'managed_agent_runtime' in e2a.capabilities_of(rt)
    rt.plan = [('find', {'q': 'x'}), ('pay', {})]
    out = infer(rt, NO_RETRY)
    results = json.loads(out['text'])
    assert results[0] == {'physical_success': True, 'tenant': 't1', 'q': 'x'} and 'not available' in results[1]['error']


def test_a_delegated_session_is_bounded_by_its_turns():
    rt = Runtime(tools={'find': (ReadTool(), {'parameters': {}})})
    rt.plan = [('find', {})] * 3
    out = infer(rt, {**NO_RETRY, 'delegated_max_turns': 2})
    assert out['status'] == 'FAILED' and out['error_type'] == 'DelegatedTurnLimit'


# ------------------------------------------------------------------ P17 reranker port, dimensions
class Index(BaseVectorIndex):
    dimensions = 3

    def upsert(self, namespace, chunks, vectors):
        pass

    def delete(self, namespace, doc_id):
        pass

    def search(self, namespace, query_text, query_vector, top_k):
        return [{'id': 'a', 'text': 'alpha'}, {'id': 'b', 'text': 'beta'}, {'id': 'c', 'text': 'gamma'}]


class Model(BaseEmbeddingModel):
    model_id, dimensions = 'emb', 3

    def embed_documents(self, texts):
        return [[0.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [0.0, 0.0, 0.0]


class Reverse(BaseReranker):
    def rerank(self, query, hits, top_n, config=None):
        return [dict(h, rerank_score=i, query=query) for i, h in enumerate(reversed(hits))][:top_n]


class Pipeline(BaseRAGPipeline):
    embedding_model, vector_index = Model(), Index()

    def _evaluate_answer(self, answer, config=None, **kwargs):
        return 1.0


def test_the_reranker_port_is_the_default_rerank():
    assert [h['id'] for h in Pipeline().retrieve('q', {}, tenant_id='t1')] == ['a', 'b', 'c']
    assert [h['id'] for h in Pipeline().retrieve('q', {'rerank_top_n': 2}, tenant_id='t1')] == ['a', 'b']
    p = Pipeline()
    p.reranker = Reverse()
    hits = p.retrieve('which', {'rerank_top_n': 2}, tenant_id='t1')
    assert [h['id'] for h in hits] == ['c', 'b'] and hits[0]['query'] == 'which'


def test_embedding_and_index_dimensions_must_agree():
    check_dimensions(Model(), Index())
    wide = Model()
    wide.dimensions = 1024
    with pytest.raises(ValueError, match='1024-dimension'):
        check_dimensions(wide, Index())
    check_dimensions(Model(), None)


def test_verify_warns_on_recommended_capabilities():
    b = bindings_from_profile(Profile(name='p', provider='x', recommends=['rerank'],
                                      bindings={'llm_service': 'e2a_simulated_llm:SimulatedLLMService'}))
    b.verify()
    assert b.warnings == ["recommended capability 'rerank' is not provided"]


# ------------------------------------------------------------------ P21 classifier guardrails
class Classifier(ManagedGuardrailStage):
    def _screen(self, text, ctx):
        if 'boom' in text:
            raise RuntimeError('classifier down')
        if 'attack' in text:
            return GuardrailVerdict(False, ['prompt_attack'])
        return GuardrailVerdict(True, sanitized=text.replace('4111', '****'))


def test_a_classifier_stage_blocks_masks_and_fails_closed():
    log = MessageLog([], 'c1', 'test')
    assert Classifier()._client is None and Classifier().fields == ['query']
    state = Classifier().check({'query': 'card 4111'}, None, log)
    assert state['query'] == 'card ****'
    with pytest.raises(FirewallRefusal, match='prompt_attack'):
        Classifier().check({'query': 'an attack'}, None, log)
    with pytest.raises(FirewallRefusal, match='unavailable'):
        Classifier().check({'query': 'boom'}, None, log)
    assert Classifier({'guardrail_fail_open': True}).check({'query': 'boom'}, None, log) == {'query': 'boom'}
    egress = ManagedGuardrailEgressStage(Classifier())
    assert egress.redact({'text': 'card 4111'}, {}, None, log) == {'text': 'card ****'}
    with pytest.raises(FirewallRefusal):
        egress.redact('an attack', {}, None, log)


# ------------------------------------------------------------------ P24 pipeline checks
def test_the_pipeline_scans_for_secrets_verifies_bindings_and_takes_the_artifact_matrix(tmp_path):
    from e2a import BasePipeline
    built = []

    class Gate(BasePipeline):
        conformance_paths = ()

        def _run_static_security_scans(self, config, **kwargs):
            return True

        def _execute_test_suite(self, config, **kwargs):
            return True

        def _run_rag_eval(self, config, **kwargs):
            return 1.0

        def _compile_artifact(self, artifact_name, config, **kwargs):
            built.append(artifact_name)
            return True

        def _run_dynamic_security_checks(self, config, **kwargs):
            return True

        def _execute_deployment_rollout(self, strategy, config, **kwargs):
            return {}

    src = tmp_path / 'src'
    src.mkdir()
    (src / 'ok.py').write_text('KEY = os.environ["KEY"]\n')
    report = Gate().run_pipeline({'secret_scan_paths': [str(src)], 'artifact_matrix': ['e2a-orchestration-engine']})
    assert report['status'] == 'SUCCESS' and built == ['e2a-orchestration-engine']
    (src / 'leak.py').write_text('KEY = "AKIA' + 'ABCDEFGHIJKLMNOP"\nOK = "AKIA' + 'ABCDEFGHIJKLMNOQ"  # e2a: allow-secret\n')
    report = Gate().run_pipeline({'secret_scan_paths': [str(src)]})
    assert report['status'] == 'FAILED' and report['secret_findings'] == [f'{src / "leak.py"}:1: aws_access_key_id']
    bindings = bindings_from_profile(Profile(name='p', provider='x', features=['rerank'],
                                             bindings={'llm_service': 'e2a_simulated_llm:SimulatedLLMService'}))
    report = Gate().run_pipeline({'secret_scan_paths': []}, bindings=bindings)
    assert report['status'] == 'FAILED' and 'rerank' in report['error']


# ------------------------------------------------------------------ P19 MCP
def test_each_mcp_tool_has_its_own_server_and_identity_and_servers_export_a_manifest():
    class Identity(BaseCredentialProvider):
        def headers(self, target, scope=None, config=None, **kwargs):
            return {'Authorization': f'Bearer for {target}'}

    seen = []

    class Gateway(MCPToolService):
        async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kwargs):
            seen.append((endpoint, kwargs.get('auth_headers')))
            return {'status': 'ok', 'physical_success': True}

    a = Gateway('https://gw-a.example/mcp', Identity())
    b = Gateway('https://gw-b.example/mcp')
    asyncio.run(a.execute({'_tool_name': 'x'}, {'mcp_server_url': 'https://default/mcp'}, tenant_id='t1'))
    asyncio.run(b.execute({'_tool_name': 'x'}, {'mcp_server_url': 'https://default/mcp'}, tenant_id='t1'))
    asyncio.run(Gateway().execute({'_tool_name': 'x'}, {'mcp_server_url': 'https://default/mcp'}, tenant_id='t1'))
    assert seen == [('https://gw-a.example/mcp', {'Authorization': 'Bearer for https://gw-a.example/mcp'}),
                    ('https://gw-b.example/mcp', None), ('https://default/mcp', None)]

    class Server(BaseMCPServer):
        server_name = 'orders'

    server = Server()
    server.wrap_api_as_tool(ReadTool(), 'find_order', 'Find an order', {'type': 'object'})
    manifest = server.export_tool_manifest()
    assert manifest['server'] == 'orders' and manifest['protocol_version']
    assert manifest['tools'] == server.serve({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']


def test_the_manifest_has_a_declarations_format_and_the_engine_records_inference():
    class Server(BaseMCPServer):
        server_name = 'orders'

    server = Server()
    server.wrap_api_as_tool(ReadTool(), 'find_order', 'Find an order', {'type': 'object', 'properties': {}})
    assert server.export_tool_manifest(fmt='declarations') == {'server': 'orders', 'tools': [
        {'name': 'find_order', 'description': 'Find an order', 'parameters': {'type': 'object', 'properties': {}}}]}
    with pytest.raises(ValueError):
        server.export_tool_manifest(fmt='yaml')

    emitted = []

    class Obs(e2a.BaseObservability):
        def _ship_logs(self, enriched_logs, config, **kwargs):
            pass

        def _emit_metrics(self, metrics, config, **kwargs):
            pass

        def _export_traces(self, correlation_id, logs, config, **kwargs):
            pass

        def _emit_counter(self, name, value, dimensions=None, config=None, **kwargs):
            emitted.append((name, value, dimensions))

        def _emit_histogram(self, name, value, dimensions=None, config=None, **kwargs):
            emitted.append((name, value, dimensions))

    prices = {'m1': {'input_per_mtok': 1.0, 'output_per_mtok': 2.0}}
    infer(Adapter(lambda m: 'ok'), {**NO_RETRY, 'model_id': 'm1', 'model_price_table': prices,
                                    'observability_engine': Obs()}, agent_name='a')
    names = [n for n, _, _ in emitted]
    assert names == ['e2a.llm.inference_total', 'e2a.llm.tokens', 'e2a.llm.tokens', 'e2a.llm.cost_usd']
    assert emitted[0][2] == {'tenant_id': 't1', 'agent': 'a', 'status': 'SUCCESS', 'model': 'm1'}
