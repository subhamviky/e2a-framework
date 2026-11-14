"""v1.5.1 (design note MP-01): the defects found while assessing the managed-providers proposal.

P7  model calls are retried when the failure is transient, throttled or overloaded,
    honouring Retry-After within the deadline; throttling does not open the circuit
P6  the provider's metadata (stop reason, request id, token split) is kept
P12 log records carry the installed E2A version
P17 the reranker receives the query
"""
import asyncio
import time

import pytest

from e2a import BaseLLMAgentService, BaseObservability, BaseRAGPipeline, ErrorClass, Deadline, classify_error
from e2a import __version__

M = [{'role': 'user', 'content': 'hello'}]


class HTTPError(Exception):
    """Shaped like the SDKs' errors: a status_code and a response with headers."""

    def __init__(self, status, headers=None, message='upstream error'):
        super().__init__(message)
        self.status_code = status
        self.response = type('R', (), {'headers': headers or {}, 'status_code': status})()


class ScriptedLLM(BaseLLMAgentService):
    """Raises the scripted errors in order, then answers."""

    def __init__(self, errors=(), raw=None, **kwargs):
        super().__init__(kwargs.pop('config', None), **kwargs)
        self.errors, self.raw, self.calls, self.times = list(errors), raw, 0, []

    async def _dispatch_upstream_call(self, messages, model_id, temperature, max_tokens, tools,
                                      stop_sequences, cache_handle, config, **kwargs):
        self.calls += 1
        self.times.append(time.monotonic())
        if self.errors:
            raise self.errors.pop(0)
        return self.raw or {'text': 'ok'}

    async def _dispatch_upstream_stream(self, messages, model_id, temperature, max_tokens, config, **kwargs):
        yield 'ok'


def infer(llm, **kwargs):
    return asyncio.run(llm.process_inference(M, tenant_id='t1', **kwargs))


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize('error, kind', [
    (HTTPError(429), 'throttled'),
    (HTTPError(503), 'overloaded'),
    (HTTPError(529), 'overloaded'),
    (HTTPError(500), 'transient'),
    (HTTPError(401), 'auth'),
    (HTTPError(400, message='prompt is too long: 250000 tokens > 200000 maximum'), 'context_overflow'),
    (HTTPError(400), 'deterministic'),
    (HTTPError(404), 'deterministic'),
    (asyncio.TimeoutError(), 'transient'),
    (ConnectionResetError(), 'transient'),
    (type('APIConnectionError', (Exception,), {})(), 'transient'),
    (ValueError('bad'), 'deterministic'),
    (RuntimeError('who knows'), 'unknown'),
])
def test_errors_are_classified_from_what_sdks_expose(error, kind):
    assert classify_error(error).kind == kind


def test_retry_after_is_read_in_seconds_and_milliseconds():
    assert classify_error(HTTPError(429, {'Retry-After': '2'})) == ErrorClass('throttled', 2.0)
    assert classify_error(HTTPError(429, {'retry-after-ms': '250'})) == ErrorClass('throttled', 0.25)
    assert classify_error(HTTPError(429, {'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'})).retry_after is None


def test_botocore_style_errors_are_classified():
    error = Exception('ThrottlingException')
    error.response = {'Error': {'Code': 'ThrottlingException'},
                      'ResponseMetadata': {'HTTPStatusCode': 429, 'HTTPHeaders': {'retry-after': '1'}}}
    assert classify_error(error) == ErrorClass('throttled', 1.0)


# ------------------------------------------------------------------ P7: retries
def test_a_throttled_call_is_retried_after_retry_after():
    llm = ScriptedLLM([HTTPError(429, {'retry-after': '0.3'})])
    out = infer(llm)
    assert out['status'] == 'SUCCESS' and llm.calls == 2
    assert llm.times[1] - llm.times[0] >= 0.3, 'the retry waited at least the server\'s Retry-After'


def test_overloaded_and_transient_failures_are_retried_up_to_the_limit():
    llm = ScriptedLLM([HTTPError(503), HTTPError(500)])
    assert infer(llm)['status'] == 'SUCCESS' and llm.calls == 3
    llm = ScriptedLLM([HTTPError(503)] * 3)
    out = infer(llm)
    assert out['status'] == 'FAILED' and llm.calls == 3, 'llm_max_retries=2: three attempts, then the failure'


@pytest.mark.parametrize('error', [HTTPError(400), HTTPError(401), RuntimeError('a bug in the adapter')])
def test_deterministic_auth_and_unknown_failures_are_not_retried(error):
    llm = ScriptedLLM([error])
    assert infer(llm)['status'] == 'FAILED' and llm.calls == 1


def test_retries_can_be_switched_off_by_configuration():
    llm = ScriptedLLM([HTTPError(503)])
    assert infer(llm, llm_max_retries=0)['status'] == 'FAILED' and llm.calls == 1


def test_a_retry_after_beyond_the_cap_or_the_deadline_is_not_waited_for():
    llm = ScriptedLLM([HTTPError(429, {'retry-after': '120'})])
    start = time.monotonic()
    assert infer(llm)['status'] == 'FAILED' and llm.calls == 1 and time.monotonic() - start < 1.0
    llm = ScriptedLLM([HTTPError(429, {'retry-after': '2'})])
    start = time.monotonic()
    assert infer(llm, deadline=Deadline.after(1.0))['status'] == 'FAILED' and llm.calls == 1
    assert time.monotonic() - start < 1.0, 'never sleeps past the request deadline'


def test_throttling_does_not_open_the_circuit_but_an_outage_does():
    config = {'circuit_failure_threshold': 3, 'llm_max_retries': 0}
    throttled = ScriptedLLM([HTTPError(429)] * 5, config=config)
    for _ in range(5):
        infer(throttled)
    assert throttled.calls == 5, 'five 429s: every call still reaches the provider'
    down = ScriptedLLM([HTTPError(503)] * 5, config=config)
    results = [infer(down) for _ in range(5)]
    assert down.calls == 3 and results[-1]['error_type'] == 'CircuitOpenError'


def test_a_provider_specific_classification_is_one_hook():
    class Quirky(ScriptedLLM):
        def _classify_error(self, error):
            return ErrorClass('throttled') if 'slow down' in str(error) else super()._classify_error(error)
    llm = Quirky([RuntimeError('slow down please')])
    assert infer(llm)['status'] == 'SUCCESS' and llm.calls == 2


# ------------------------------------------------------------------ P6: metadata
def test_provider_metadata_is_kept_and_the_base_fields_win():
    llm = ScriptedLLM(raw={'text': 'ok', 'stop_reason': 'end_turn', 'provider_request_id': 'req_123',
                           'metadata': {'usage': {'input_tokens': 12, 'output_tokens': 3}, 'model_id': 'theirs',
                                        'region': 'eu-west-1'}})
    out = infer(llm, model_id='claude-x')
    assert out['metadata']['model_id'] == 'claude-x' and out['metadata']['region'] == 'eu-west-1'
    assert out['stop_reason'] == 'end_turn' and out['provider_request_id'] == 'req_123'
    assert out['usage'] == {'input_tokens': 12, 'output_tokens': 3}


def test_a_plain_result_has_no_extra_fields():
    out = infer(ScriptedLLM())
    assert set(out['metadata']) == {'model_id', 'tokens_used'} and 'stop_reason' not in out


# ------------------------------------------------------------------ P12: version
def test_log_records_carry_the_installed_version():
    from e2a.observability import _framework_version
    assert _framework_version() == f'e2a-v{__version__}'


# ------------------------------------------------------------------ P17: rerank query
def test_the_reranker_receives_the_query():
    seen = {}

    class Pipeline(BaseRAGPipeline):
        def _embed_query(self, query, config=None, **kwargs):
            return [0.0]

        def _search_index(self, query_text, query_vector, config=None, tenant_id=None, **kwargs):
            return [{'chunk_id': 'a', 'score': 0.5}]

        def _rerank(self, results, config=None, **kwargs):
            seen.update(kwargs)
            return results

        def _evaluate_answer(self, answer, config=None, **kwargs):
            return 1.0

    Pipeline().retrieve('refund policy', {}, tenant_id='t1')
    assert seen['query'] == 'refund policy'
