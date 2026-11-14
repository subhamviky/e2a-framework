"""E2A: governance as stages (v1.2, design note 04).

A governance engine is an ordered list of stages. Ingress stages screen the state
before the model or any tool sees it; egress stages screen the response. Every
refusal is a typed GovernanceRefusal (code, stage, reason, retryable) that is also an
instance of the exception type v1.1 raised for the same case, so existing
`except PermissionError` / `except ValueError` / `except NFRViolationError` still work.
"""
import contextlib
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Sequence, Tuple

from .config import resolve_config
from .core import DehydrationInterrupt, NFRViolationError


class GovernanceRefusal(Exception):
    """A governance stage refused the request."""
    code = 'REFUSED'
    retryable = False

    def __init__(self, reason: str, stage: str = '', code: Optional[str] = None,
                 retryable: Optional[bool] = None) -> None:
        super().__init__(reason)
        self.reason, self.stage = reason, stage
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable


class CapacityRefusal(GovernanceRefusal, NFRViolationError):
    code, retryable = 'CAPACITY', True


class TokenBudgetRefusal(GovernanceRefusal, ValueError):
    code = 'TOKEN_BUDGET'


class FirewallRefusal(GovernanceRefusal, PermissionError):
    code = 'FIREWALL'


class SandboxRefusal(GovernanceRefusal, PermissionError):
    code = 'SANDBOX'


class ToolRefusal(GovernanceRefusal, PermissionError):
    code = 'TOOL_DENIED'


class ApprovalRequired(DehydrationInterrupt):
    """Raised by ApprovalStage: the workflow pauses (dehydrates) until a decision."""

    def __init__(self, reason: str, stage: str = 'approval') -> None:
        super().__init__(reason)
        self.reason, self.stage = reason, stage


class IngressStage(ABC):
    """Screens the state before the model or any tool sees it.

    phase: 'screen' stages run before the agent's _prepare (nothing, not even a read
    or a retrieval, sees an unscreened request); 'decide' stages run after _prepare,
    because they need the facts it read (ApprovalStage needs the amount)."""
    name = 'ingress'
    phase = 'screen'

    @abstractmethod
    def check(self, state: Dict[str, Any], ctx, messages) -> Dict[str, Any]:
        """Return the (possibly redacted) state, or raise a GovernanceRefusal."""


class EgressStage(ABC):
    """Screens the response before the caller sees it."""
    name = 'egress'

    @abstractmethod
    def redact(self, response: Any, state: Dict[str, Any], ctx, messages) -> Any:
        """Return the (possibly redacted) response."""


# --------------------------------------------------------------------- built-in stages
class InflightLimiter:
    """Counts requests in flight; use `with limiter.slot():` around a request."""

    def __init__(self, limit: int) -> None:
        self.limit, self.inflight, self._lock = int(limit), 0, threading.Lock()

    @contextlib.contextmanager
    def slot(self):
        with self._lock:
            self.inflight += 1
        try:
            yield self
        finally:
            with self._lock:
                self.inflight -= 1

    def available(self) -> bool:
        return self.inflight <= self.limit


class CapacityStage(IngressStage):
    """Admission control: refuse (retryable) when over capacity or a dependency's
    circuit is open, before any model or tool is touched."""
    name = 'capacity'

    def __init__(self, limiter: Optional[InflightLimiter] = None, breakers: Iterable[Any] = ()) -> None:
        self.limiter, self.breakers = limiter, list(breakers)

    def check(self, state, ctx, messages):
        if self.limiter is not None and not self.limiter.available():
            raise CapacityRefusal('execution capacity exceeded; retry later', self.name)
        if any(getattr(b, 'state', None) == 'OPEN' for b in self.breakers):
            raise CapacityRefusal('a dependency circuit is open; retry later', self.name)
        return state


class TokenBudgetStage(IngressStage):
    """Stops the request before another model call once ctx.setting('max_token_budget')
    is used up (counted in state['cumulative_tokens_used'])."""
    name = 'token_budget'

    def check(self, state, ctx, messages):
        budget = float(ctx.setting('max_token_budget', float('inf')))
        used = state.get('cumulative_tokens_used', 0)
        if used >= budget:
            raise TokenBudgetRefusal(f'token budget exhausted: used {used}, limit {budget}', self.name)
        return state


class ApprovalStage(IngressStage):
    """Pauses the workflow for a human decision. `required(state, ctx)` decides whether
    this request needs approval (default: state['requires_human_approval']); a request
    that carries state['approval_granted'] passes. The workflow stores the checkpoint
    and continues it with resume()."""
    name = 'approval'
    phase = 'decide'

    def __init__(self, required: Optional[Callable[[Dict[str, Any], Any], bool]] = None,
                 reason: str = 'human approval required') -> None:
        self.required = required or (lambda state, ctx: bool(state.get('requires_human_approval')))
        self.reason = reason

    def check(self, state, ctx, messages):
        if self.required(state, ctx) and not state.get('approval_granted'):
            messages.info('approval_required', reason=self.reason)
            raise ApprovalRequired(self.reason, self.name)
        return state


class SandboxStage(IngressStage):
    """Before a pending MCP tool execution: the runtime must declare a sandbox profile."""
    name = 'sandbox'

    def check(self, state, ctx, messages):
        if state.get('pending_mcp_tool_execution') and not ctx.setting('sandbox_profile', None):
            raise SandboxRefusal('MCP tool execution requires a sandboxed runtime', self.name)
        return state


Redaction = Tuple[str, Pattern, str]      # (name, pattern, mask)


def compile_redactions(rules: Iterable[Dict[str, str]]) -> List[Redaction]:
    """[{'name', 'pattern', 'mask'}] -> compiled rules (the shape of a policy file)."""
    return [(r['name'], re.compile(r['pattern']), r['mask']) for r in rules]


def redact_text(text: str, rules: Sequence[Redaction]) -> Tuple[str, List[str]]:
    found = []
    for name, pattern, mask in rules:
        text, count = pattern.subn(mask, text)
        if count:
            found.append(name)
    return text, found


class FirewallStage(IngressStage):
    """Semantic firewall on the given state fields: refuses prompt-injection patterns and
    over-long input, and masks sensitive data before any prompt is built."""
    name = 'firewall'

    def __init__(self, injection_patterns: Iterable[str] = (), redactions: Sequence[Redaction] = (),
                 fields: Sequence[str] = ('query',), max_chars: Optional[int] = None) -> None:
        self.patterns = [re.compile(p, re.I) for p in injection_patterns]
        self.redactions, self.fields, self.max_chars = list(redactions), tuple(fields), max_chars

    def check(self, state, ctx, messages):
        for field in self.fields:
            value = state.get(field)
            if not isinstance(value, str):
                continue
            if self.max_chars and len(value) > self.max_chars:
                raise FirewallRefusal(f'{field} longer than {self.max_chars} characters', self.name)
            if any(p.search(value) for p in self.patterns):
                raise FirewallRefusal('request refused by the semantic firewall (prompt injection)', self.name)
            state[field], found = redact_text(value, self.redactions)
            if found:
                state['redactions'] = sorted(set(state.get('redactions', [])) | set(found))
                messages.info('redacted', field=field, kinds=found)
        return state


class RedactionStage(EgressStage):
    """Masks sensitive data in the response's text fields before the caller sees it."""
    name = 'redaction'

    def __init__(self, redactions: Sequence[Redaction] = (), fields: Sequence[str] = ('text',)) -> None:
        self.redactions, self.fields = list(redactions), tuple(fields)

    def redact(self, response, state, ctx, messages):
        if isinstance(response, dict):
            for field in self.fields:
                if isinstance(response.get(field), str):
                    response[field], _ = redact_text(response[field], self.redactions)
        elif isinstance(response, str):
            response, _ = redact_text(response, self.redactions)
        return response


# --------------------------------------------------------------------- v1.6.1 classifier stages
@dataclass
class GuardrailVerdict:
    """What a classifier (a managed guardrail service, a local safety model) says about a text."""
    allowed: bool
    categories: List[str] = field(default_factory=list)
    sanitized: Optional[str] = None          # masked text, when the classifier returns one
    detail: str = ''


class ManagedGuardrailStage(IngressStage):
    """v1.6.1 (design note MP-04, P21): screening by a classifier, as one more ingress stage
    next to the pattern FirewallStage (cheap first screen, then the model-based one).

    A subclass implements only _screen(text, ctx) -> GuardrailVerdict; which service it asks
    is the provider layer's business. E2A owns the rules, the same for every classifier:
    * the fields screened are `guardrail_fields`;
    * a block is FirewallRefusal (callers and tests see one type, whoever screened);
    * a classifier error fails CLOSED (FirewallRefusal) unless `guardrail_fail_open`;
    * a sanitized text replaces the field, and is logged as guardrail_masked."""
    name = 'managed_guardrail'
    phase = 'screen'
    capabilities = frozenset({'guardrails'})

    def __init__(self, config: Optional[Dict[str, Any]] = None, client: Any = None,
                 fail_open: Optional[bool] = None) -> None:
        self.config = dict(config or {})
        self.fields = list(resolve_config('guardrail_fields', self.config))
        self.fail_open = bool(resolve_config('guardrail_fail_open', self.config)) if fail_open is None else fail_open
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client(self.config)
        return self._client

    def _build_client(self, config: Dict[str, Any]) -> Any:
        """Hook: the classifier's client, built on first use. Default: none."""
        return None

    @abstractmethod
    def _screen(self, text: str, ctx) -> GuardrailVerdict:
        """Ask the classifier about one text."""

    def _verdict(self, text, ctx, messages) -> Optional[GuardrailVerdict]:
        try:
            return self._screen(text, ctx)
        except Exception as e:                       # classifier down or throttled
            messages.warn('guardrail_unavailable', stage=self.name, error=type(e).__name__)
            if self.fail_open:
                return None
            raise FirewallRefusal(f'{self.name} unavailable; not screened', self.name) from e

    def _refuse(self, verdict, messages):
        messages.warn('guardrail_blocked', stage=self.name, categories=verdict.categories)
        cats = f" ({', '.join(verdict.categories)})" if verdict.categories else ''
        raise FirewallRefusal(f'refused by {self.name}{cats}', self.name)

    def screen_text(self, text: str, ctx, messages) -> str:
        """Public (v1.6.1): screen one text under this stage's rules; returns the text to use
        (sanitized when the classifier masked it) or raises FirewallRefusal. The egress
        variant and a project's own checks call it."""
        verdict = self._verdict(text, ctx, messages)
        if verdict is None:
            return text
        if not verdict.allowed:
            self._refuse(verdict, messages)
        return verdict.sanitized if verdict.sanitized is not None else text

    def check(self, state, ctx, messages):
        for name in self.fields:
            value = state.get(name)
            if not isinstance(value, str) or not value:
                continue
            screened = self.screen_text(value, ctx, messages)
            if screened != value:
                state[name] = screened
                messages.info('guardrail_masked', stage=self.name, field=name)
        return state


class ManagedGuardrailEgressStage(EgressStage):
    """v1.6.1: the same classifier on the way out (the response text). A blocked response is
    FirewallRefusal; a sanitized one replaces the text. Shares the subclass's _screen by
    composition: pass the ingress stage; its public screen_text() is used."""
    name = 'managed_guardrail_egress'
    capabilities = frozenset({'guardrails'})

    def __init__(self, screen: ManagedGuardrailStage, fields: Sequence[str] = ('text',)) -> None:
        self.screen, self.fields = screen, tuple(fields)

    def redact(self, response, state, ctx, messages):
        def one(text):
            return self.screen.screen_text(text, ctx, messages)
        if isinstance(response, str):
            return one(response)
        if isinstance(response, dict):
            for f in self.fields:
                if isinstance(response.get(f), str) and response[f]:
                    response[f] = one(response[f])
        return response

