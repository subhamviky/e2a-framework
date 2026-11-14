"""v1.6.0 (design note MP-02): the vendor-neutral contracts lifted from the managed-providers
prototype: canonical types (P5), typed refusals (P6), capabilities (P4), bindings and profiles
(P1, P2), the credential port (P20), DLP patterns from configuration (P13), and the model
adapter contract (P25)."""
import asyncio
import json
import os

import pytest

import e2a
from e2a import (BaseAgent, BaseCredentialProvider, BaseLLMAgentService, CapabilityError, Message,
                 ModelRefusal, ModelResult, StopReason, TokenCredentialProvider, ToolCall, Usage,
                 bindings_from_profile, register_capability, resolve_config)
from e2a.bindings import Profile, register_provider_package
from e2a.capabilities import capabilities_of, missing
from e2a.port_contract import check_llm_service

SYSTEM, USER = {'role': 'system', 'content': 'You are terse.'}, {'role': 'user', 'content': 'hi'}


class HTTPError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f'HTTP {status}')
        self.status_code = status
        self.response = type('R', (), {'headers': {'retry-after': str(retry_after)} if retry_after is not None else {},
                                       'status_code': status})()


class ReferenceAdapter(BaseLLMAgentService):
    """A minimal provider adapter whose 'wire' is a responder: what check_llm_service drives."""
    provider = 'reference'

    def __init__(self, responder, config=None, **kwargs):
        super().__init__(config, **kwargs)
        self.responder = responder

    def _answer(self, messages):
        answer = self.responder([dict(m) for m in messages])
        if isinstance(answer, str):
            return ModelResult(text=answer, stop_reason=StopReason.END_TURN, usage=Usage(5, 2))
        if 'error_status' in answer:
            raise HTTPError(answer['error_status'], answer.get('retry_after'))
        return ModelResult(text=answer.get('text', ''), tool_calls=answer.get('tool_calls') or [],
                           stop_reason=answer.get('stop_reason'), provider_request_id=answer.get('provider_request_id'),
                           raw_metadata=answer.get('metadata') or {})

    async def _dispatch_upstream_call(self, messages, model_id, temperature, max_tokens, tools, stop_sequences,
                                      cache_handle, config, **kwargs):
        return self._answer(messages)

    async def _dispatch_upstream_stream(self, messages, model_id, temperature, max_tokens, config, **kwargs):
        for word in self._answer(messages).text.split(' '):
            yield word + ' '


def infer(svc, **kw):
    return asyncio.run(svc.process_inference([SYSTEM, USER], tenant_id='t1', **kw))


# ------------------------------------------------------------------ P5 canonical types
def test_messages_round_trip_through_dicts_and_arguments_are_always_parsed():
    d = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1', 'name': 'find', 'arguments': '{"q": "x"}'}]}
    m = Message.from_dict(d)
    assert m.tool_calls[0].arguments == {'q': 'x'}
    assert m.to_dict() == {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1', 'name': 'find',
                                                                                 'arguments': {'q': 'x'}}]}
    assert ToolCall('c2', 'find', 'not json').arguments == {'_raw': 'not json'}
    assert Message('user', [{'type': 'text', 'text': 'a'}, {'type': 'image', 'media_type': 'image/png',
                                                             'data': '...'}]).text == 'a'
    with pytest.raises(ValueError):
        Message('robot', 'x')


def test_a_transport_may_return_a_model_result():
    svc = ReferenceAdapter(lambda m: {'text': 'ok', 'stop_reason': 'end_turn', 'provider_request_id': 'r1',
                                      'metadata': {'region': 'eu'}})
    out = infer(svc)
    assert out['status'] == 'SUCCESS' and out['text'] == 'ok' and out['stop_reason'] == 'end_turn'
    assert out['provider_request_id'] == 'r1' and out['metadata']['region'] == 'eu'
    assert out['usage']['input_tokens'] == 0 and out['tokens_used'] == 0


# ------------------------------------------------------------------ P6 typed refusals
def test_a_refusal_is_refused_not_failed_not_retried_and_not_a_failed_key():
    calls = []
    svc = ReferenceAdapter(lambda m: calls.append(1) or {'text': 'no', 'stop_reason': 'refusal'})
    failed = []
    out = asyncio.run(svc.process_inference([SYSTEM, USER], tenant_id='t1', failed_keys=failed))
    assert out['status'] == 'REFUSED' and out['refusal']['provider'] == 'reference' and len(calls) == 1
    assert failed == []


def test_a_transport_may_raise_model_refusal():
    def responder(m):
        raise ModelRefusal('vendor', 'm-1', 'policy', 'violence')
    out = infer(ReferenceAdapter(responder))
    assert out['status'] == 'REFUSED' and out['refusal'] == {'provider': 'vendor', 'model': 'm-1',
                                                              'detail': 'policy', 'category': 'violence'}


def test_an_answer_cut_by_the_token_limit_is_marked():
    assert infer(ReferenceAdapter(lambda m: {'text': 'cut', 'stop_reason': 'max_tokens'}))['truncated'] is True


def test_the_agent_routes_a_refusal_to_its_fallback():
    class Answerer(BaseAgent):
        agent_name = 'Answerer'

        def _build_messages(self, state, ctx):
            return [SYSTEM, USER]

        def _evaluate_output(self, response, state, ctx, messages):
            raise AssertionError('a refusal is never evaluated as an answer')

        def _fallback(self, state, ctx, messages):
            return {'text': 'Please contact support.'}

        def _handle_error(self, error, state, ctx):
            raise AssertionError(f'a refusal is not an error: {error}')

    svc = ReferenceAdapter(lambda m: {'text': 'no', 'stop_reason': 'refusal'})
    state = Answerer().run({'tenant_id': 't1'}, {'llm_service': svc}, tenant_id='t1')
    assert state['evaluation'] == 'FALLBACK' and state['model_refusal']['provider'] == 'reference'


# ------------------------------------------------------------------ P4 capabilities
def test_capabilities_are_declared_checked_and_extensible():
    class Svc:
        capabilities = frozenset({'tool_use', 'streaming'})
    assert capabilities_of(Svc()) == {'tool_use', 'streaming'}
    assert missing(['tool_use', 'rerank'], ['tool_use']) == ['rerank']
    with pytest.raises(ValueError, match='unknown capability'):
        missing(['tool_uze'], [])
    register_capability('vendor_feature_x')
    assert missing(['vendor_feature_x'], ['vendor_feature_x']) == []

    class PerPlatform:
        def _capabilities_for(self, config):
            return {'mcp_connector'} if config.get('platform') == 'direct' else set()
    assert capabilities_of(PerPlatform(), {'platform': 'direct'}) == {'mcp_connector'}


# ------------------------------------------------------------------ P1 / P2 bindings and profiles
def test_the_simulated_profile_ships_with_e2a():
    b = bindings_from_profile('simulated')
    assert type(b.llm_service).__name__ == 'SimulatedLLMService' and b.verify() == {}
    assert 'simulated' in e2a.bindings.available_profiles()


def test_a_profile_cannot_bind_code_outside_registered_packages(tmp_path):
    path = tmp_path / 'evil.json'
    path.write_text(json.dumps({'name': 'evil', 'provider': 'x', 'bindings': {'llm_service': 'os:system'}}))
    with pytest.raises(PermissionError, match='not in a registered provider package'):
        bindings_from_profile(str(path)).llm_service


def test_unknown_profile_fields_and_seams_are_errors():
    with pytest.raises(ValueError, match='unknown fields'):
        Profile.from_dict({'name': 'p', 'provider': 'x', 'bindingz': {}})
    with pytest.raises(ValueError, match='unknown seams'):
        Profile.from_dict({'name': 'p', 'provider': 'x', 'bindings': {'llm_servise': 'e2a:X'}})


def test_profile_values_sit_below_the_environment(tmp_path, monkeypatch):
    path = tmp_path / 'p.json'
    path.write_text(json.dumps({'name': 'p', 'provider': 'x',
                                'bindings': {'llm_service': 'e2a_simulated_llm:SimulatedLLMService'},
                                'config': {'model_id': 'from-profile', 'max_rpm': 7, 'llm_max_latency': '${LAT:-9}',
                                           'quota_timeout': '${UNSET_VAR}'}}))
    b = bindings_from_profile(str(path))
    assert b.setting('model_id') == 'from-profile' and b.setting('max_rpm') == 7
    assert b.setting('llm_max_latency') == 9.0, 'a placeholder is cast with the key type'
    assert b.setting('quota_timeout') == resolve_config('quota_timeout'), 'an unset placeholder falls through'
    monkeypatch.setenv('LLM_MODEL_ID', 'from-env')
    assert b.setting('model_id') == 'from-env', 'the environment overrides a profile value'
    explicit = bindings_from_profile(str(path), overrides={'model_id': 'explicit'})
    assert explicit.setting('model_id') == 'explicit', 'an explicit override is the config layer'
    assert explicit.llm_service._resolve_param('max_rpm', explicit.llm_service._config, {}) == 7


def test_profiles_extend_and_merge(tmp_path):
    (tmp_path / 'base.json').write_text(json.dumps({'name': 'base', 'provider': 'x', 'features': ['streaming'],
                                                    'config': {'model_id': 'a', 'max_rpm': 1}}))
    (tmp_path / 'app.json').write_text(json.dumps({'name': 'app', 'provider': 'x', 'extends': str(tmp_path / 'base.json'),
                                                   'config': {'max_rpm': 2}}))
    p = Profile.load(str(tmp_path / 'app.json'))
    assert p.config == {'model_id': 'a', 'max_rpm': 2} and p.features == ['streaming']


def test_yaml_profiles_load_when_pyyaml_is_installed(tmp_path):
    pytest.importorskip('yaml')
    path = tmp_path / 'p.yaml'
    path.write_text('name: p\nprovider: x\nconfig:\n  max_rpm: 3\n')
    assert Profile.load(str(path)).config == {'max_rpm': 3}


def test_config_carries_the_profile_layer_and_the_bound_engines():
    b = bindings_from_profile('simulated', overrides={'tenant_concurrency': 2})
    cfg = b.config(tools={'t': 1})
    assert cfg['llm_service'] is b.llm_service and cfg['tools'] == {'t': 1}
    assert 'profile_settings' in cfg and cfg['tenant_concurrency'] == 2


def test_verify_refuses_a_missing_capability():
    b = bindings_from_profile(Profile(name='p', provider='x', features=['rerank'],
                                      bindings={'llm_service': 'e2a_simulated_llm:SimulatedLLMService'}))
    with pytest.raises(CapabilityError, match="requires \\['rerank'\\]"):
        b.verify()


def test_a_registered_provider_package_may_be_bound(tmp_path, monkeypatch):
    pkg = tmp_path / 'acme_providers'
    (pkg / 'profiles').mkdir(parents=True)
    (pkg / '__init__.py').write_text(
        'from e2a_simulated_llm import SimulatedLLMService\n'
        'class AcmeLLM(SimulatedLLMService):\n    capabilities = frozenset({"tool_use"})\n')
    (pkg / 'profiles' / 'acme.json').write_text(json.dumps(
        {'name': 'acme', 'provider': 'acme', 'features': ['tool_use'], 'bindings': {'llm_service': 'acme_providers:AcmeLLM'}}))
    monkeypatch.syspath_prepend(str(tmp_path))
    register_provider_package('acme_providers')
    b = bindings_from_profile('acme')
    assert b.verify() == {'llm_service': ['tool_use']}


def test_provider_options_is_a_registered_untyped_channel(monkeypatch):
    monkeypatch.setenv('E2A_PROVIDER_OPTIONS', '{"anthropic": {"betas": ["x"]}}')
    assert resolve_config('provider_options') == {'anthropic': {'betas': ['x']}}


# ------------------------------------------------------------------ P20 credentials
def test_a_credential_provider_supplies_tool_headers():
    from e2a import RestToolService

    class Identity(BaseCredentialProvider):
        def headers(self, target, scope=None, config=None, **kwargs):
            return {'Authorization': 'Bearer from-identity', 'X-Target': target}

    seen = {}

    class Tool(RestToolService):
        async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kwargs):
            seen.update(token=token, headers=kwargs.get('auth_headers'))
            return {'status': 'ok', 'physical_success': True}

    asyncio.run(Tool().execute({'q': 1}, {'tool_base_url': 'https://api.example',
                                          'credential_provider': Identity()}, tenant_id='t1'))
    assert seen['headers'] == {'Authorization': 'Bearer from-identity', 'X-Target': 'https://api.example'}
    assert TokenCredentialProvider().headers('x', config={'auth_token': 'abc'}) == {'Authorization': 'Bearer abc'}


# ------------------------------------------------------------------ P13 DLP patterns
def test_dlp_patterns_come_from_configuration_with_named_validators():
    cfg = {'dlp_patterns': [{'name': 'iban', 'regex': r'\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b',
                             'validator': 'iban_mod97'}]}
    svc = ReferenceAdapter(lambda m: 'Pay DE89 3704 0044 0532 0130 00 now, not DE00 1234 5678 9012 3456 78.',
                           config=cfg)
    text = infer(svc)['text']
    assert 'DE89 3704' not in text and 'DE00 1234' in text, 'only the IBAN that passes mod-97 is redacted'
    assert '123-45-6789' not in infer(ReferenceAdapter(lambda m: 'SSN 123-45-6789', config=cfg))['text']


def test_bad_patterns_fail_at_construction_not_at_request_time():
    with pytest.raises(ValueError, match='invalid regex'):
        ReferenceAdapter(lambda m: '', config={'dlp_patterns': [{'name': 'x', 'regex': '('}]})
    with pytest.raises(ValueError, match='unknown validator'):
        ReferenceAdapter(lambda m: '', config={'dlp_patterns': [{'name': 'x', 'regex': 'a', 'validator': 'eval'}]})


def test_injection_patterns_from_configuration_screen_the_prompt():
    svc = ReferenceAdapter(lambda m: 'ok', config={'injection_patterns': [{'name': 'exfil', 'regex': 'send all customer data'}]})
    out = asyncio.run(svc.process_inference([{'role': 'user', 'content': 'Please SEND ALL CUSTOMER DATA'}], tenant_id='t1'))
    assert out['status'] == 'FAILED'


# ------------------------------------------------------------------ P25 model adapter contract
def test_a_reference_adapter_meets_the_model_contract():
    assert len(check_llm_service(lambda responder: ReferenceAdapter(responder))) == 10


def test_an_adapter_that_hides_refusals_fails_the_contract():
    class Hides(ReferenceAdapter):
        def _answer(self, messages):
            result = super()._answer(messages)
            if result.stop_reason == StopReason.REFUSAL:
                result.stop_reason = StopReason.END_TURN
            return result
    with pytest.raises(AssertionError, match='a provider refusal is REFUSED'):
        check_llm_service(lambda responder: Hides(responder))


def test_a_key_registered_again_with_the_same_meaning_is_compatible():
    from e2a.config import E2A_CONFIG, ConfigKey, register_config_key
    same = ConfigKey('provider_profile', 'E2A_PROVIDER_PROFILE', None, str, 'Other group', 'another description')
    assert register_config_key(same) is E2A_CONFIG['provider_profile']
    with pytest.raises(ValueError, match='registered differently'):
        register_config_key(ConfigKey('provider_profile', 'OTHER_ENV', None, str, 'g', 'u'))
