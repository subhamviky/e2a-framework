"""e2a_simulated_llm.py — a deterministic, offline LLM provider for E2A.

SimulatedLLMService is a concrete BaseLLMAgentService. It never calls a
model: each request is answered by the first registered rule whose matcher
accepts the messages. Because it is a normal provider adapter, everything
around the call stays real — permits, RPM/TPM quota, circuit breaker,
DLP redaction, deadlines and usage accounting all run exactly as they would
in front of a hosted model.

Use it when no model access exists (local development, CI, tests,
air-gapped builds) or when a run must be reproducible. Swapping to a real
provider later is a binding change only: config['llm_service'] = <adapter>.

    llm = SimulatedLLMService()
    llm.add_rule('intent', system_contains('classify the intent'),
                 lambda messages, ctx: '{"intent": "FINANCE", "confidence": 0.92}')
    result = await llm.process_inference(messages, tenant_id='t1')

A responder returns either a string (the completion text) or a dict in the
provider contract shape ({'text': ...} and/or {'tool_calls': [...]}).
"""
import asyncio
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Union

from e2a_base import BaseLLMAgentService, ConfigKey, register_config_key, resolve_config

Messages = List[Dict[str, Any]]
Matcher = Callable[[Messages], bool]
Responder = Callable[[Messages, Dict[str, Any]], Union[str, Dict[str, Any]]]

for _spec in (
    ConfigKey('simulated_llm_latency_ms', 'SIMULATED_LLM_LATENCY_MS', 0, int, 'LLM service',
              'SimulatedLLMService — artificial latency per call (milliseconds)'),
    ConfigKey('simulated_llm_cost_per_1k_tokens', 'SIMULATED_LLM_COST_PER_1K_TOKENS', 0.003, float,
              'LLM service', 'SimulatedLLMService — price used for simulated cost accounting (USD)'),
):
    register_config_key(_spec)


# ------------------------------------------------------------------ matchers
def _content(message: Dict[str, Any]) -> str:
    return str(message.get('content') or '')


def system_contains(*phrases: str) -> Matcher:
    """Match when the system message contains every phrase (case-insensitive)."""
    wanted = [p.lower() for p in phrases]

    def match(messages: Messages) -> bool:
        system = ' '.join(_content(m) for m in messages if m.get('role') == 'system').lower()
        return all(p in system for p in wanted)
    return match


def user_contains(*phrases: str) -> Matcher:
    """Match when the last user message contains every phrase (case-insensitive)."""
    wanted = [p.lower() for p in phrases]

    def match(messages: Messages) -> bool:
        users = [m for m in messages if m.get('role') == 'user']
        last = _content(users[-1]).lower() if users else ''
        return all(p in last for p in wanted)
    return match


def user_matches(pattern: str) -> Matcher:
    """Match when the last user message matches a regular expression."""
    rx = re.compile(pattern, re.IGNORECASE | re.DOTALL)

    def match(messages: Messages) -> bool:
        users = [m for m in messages if m.get('role') == 'user']
        return bool(users and rx.search(_content(users[-1])))
    return match


def always() -> Matcher:
    return lambda messages: True


@dataclass
class SimulatedRule:
    name: str
    matcher: Matcher
    responder: Responder


# ------------------------------------------------------------------- service
class SimulatedLLMService(BaseLLMAgentService):
    """Deterministic rule-based provider. First matching rule wins; when no
    rule matches, `default_response` is returned (rule name 'default')."""

    DEFAULT_RESPONSE = 'SIMULATED-LLM: no rule matched this prompt.'

    def __init__(self, rules: Optional[List[SimulatedRule]] = None,
                 default_response: Union[str, Responder, None] = None,
                 config: Dict[str, Any] = None, **kwargs):
        super().__init__(config=config, **kwargs)
        self._rules: List[SimulatedRule] = list(rules or [])
        self._default = default_response if default_response is not None else self.DEFAULT_RESPONSE
        self._lock = threading.Lock()
        self.calls: List[Dict[str, Any]] = []

    # -------------------------------------------------------------- rules
    def add_rule(self, name: str, matcher: Matcher, responder: Union[str, Responder]) -> 'SimulatedLLMService':
        """Register a rule; a plain string responder always returns that text."""
        fn = responder if callable(responder) else (lambda messages, ctx, _t=responder: _t)
        self._rules.append(SimulatedRule(name, matcher, fn))
        return self

    def rule_names(self) -> List[str]:
        return [r.name for r in self._rules]

    # -------------------------------------------------------------- usage
    def usage_summary(self) -> Dict[str, Any]:
        """Totals for FinOps evidence: calls, tokens and simulated cost."""
        with self._lock:
            calls = list(self.calls)
        by_rule: Dict[str, int] = {}
        for c in calls:
            by_rule[c['rule']] = by_rule.get(c['rule'], 0) + 1
        return {'calls': len(calls),
                'input_tokens': sum(c['input_tokens'] for c in calls),
                'output_tokens': sum(c['output_tokens'] for c in calls),
                'cost_usd': round(sum(c['cost_usd'] for c in calls), 6),
                'by_rule': by_rule}

    # ---------------------------------------------------------- transport
    async def _dispatch_upstream_call(self, messages, model_id, temperature, max_tokens,
                                      tools, stop_sequences, cache_handle, config,
                                      **kwargs) -> Dict[str, Any]:
        cfg = config or {}
        rule = next((r for r in self._rules if r.matcher(messages)), None)
        context = {'model_id': model_id, 'tools': tools or [], 'config': cfg,
                   'max_tokens': max_tokens, 'temperature': temperature}
        if rule is not None:
            name, raw = rule.name, rule.responder(messages, context)
        else:
            name = 'default'
            raw = self._default(messages, context) if callable(self._default) else self._default
        out: Dict[str, Any] = raw if isinstance(raw, dict) else {'text': str(raw)}

        latency_ms = int(resolve_config('simulated_llm_latency_ms', cfg, kwargs) or 0)
        if latency_ms > 0:
            await asyncio.sleep(latency_ms / 1000.0)

        input_tokens = max(1, sum(len(_content(m)) for m in messages) // 4)
        output_tokens = max(1, len(str(out.get('text', ''))) // 4)
        price = float(resolve_config('simulated_llm_cost_per_1k_tokens', cfg, kwargs))
        cost = (input_tokens + output_tokens) / 1000.0 * price
        with self._lock:
            self.calls.append({'rule': name, 'model_id': model_id, 'input_tokens': input_tokens,
                               'output_tokens': output_tokens, 'cost_usd': cost})
        meta = dict(out.get('metadata') or {})
        meta.update({'tokens_used': input_tokens + output_tokens, 'input_tokens': input_tokens,
                     'output_tokens': output_tokens, 'cost_usd': round(cost, 6),
                     'rule': name, 'simulated': True})
        out['metadata'] = meta
        return out

    async def _dispatch_upstream_stream(self, messages, model_id, temperature, max_tokens,
                                        config, **kwargs):
        result = await self._dispatch_upstream_call(messages, model_id, temperature, max_tokens,
                                                    None, None, None, config, **kwargs)
        for word in re.findall(r'\S+\s*', str(result.get('text', ''))):
            yield word