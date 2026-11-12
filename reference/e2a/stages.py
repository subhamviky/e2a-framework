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
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Sequence, Tuple

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
    """Screens the state before the model or any tool sees it."""
    name = 'ingress'

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
