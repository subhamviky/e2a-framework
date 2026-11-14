"""DelegatedRuntime: a managed agent runtime kept inside the E2A agent lifecycle
(v1.6.1, design note MP-04, P15).

Some platforms run the reasoning loop on their side (a hosted agent runtime, a responses API
with server-side tools). E2A's guarantees must not move there: screening before anything reads
the request, the agent's own policy, idempotency, approval, material effects only through
io_modify with an idempotency key, confirmation against the system of record. So a managed
runtime is bound as the model step of one agent, never as a replacement for the agent:

    BaseAgent.run():  screen -> _prepare -> decide -> _apply_policy -> _build_messages
                      -> infer  == DelegatedRuntime: one managed session   <- delegated
                      -> _evaluate_output -> _act (io_modify) -> _confirm -> redaction

Bind it to one agent with a scope, so the rest of the workflow keeps its own llm_service:

    scopes = {"agent:research": {"llm_service": "@delegated_runtime"}}

Rules enforced here, for every platform:
  * the runtime may call READ tools only: a tool whose `material` is True is refused when it is
    bound, and a call naming one is answered with an error result;
  * host-side tool calls go through the tool's own execute(), scoped to the request's tenant,
    so typed outcomes, the circuit breaker and tenant isolation still apply;
  * a session is bounded by `delegated_max_turns` tool round-trips (run_tool raises
    DelegatedTurnLimit past it) and, like any model call, by the upstream timeout, quota and
    the request deadline of BaseLLMAgentService.

A provider implements _run_session (and usually _build_client); it has no vendor code here.
"""
import json
from abc import abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from .config import resolve_config
from .llm import BaseLLMAgentService
from .tools import BaseToolService

__all__ = ['DelegatedRuntime', 'DelegatedTurnLimit', 'MaterialToolRefused']

ToolSpec = Tuple[BaseToolService, Dict[str, Any]]      # (tool, {"description", "parameters"})


class MaterialToolRefused(PermissionError):
    """A tool with a material effect was offered to a delegated runtime."""


class DelegatedTurnLimit(RuntimeError):
    """A managed session asked for more tool round-trips than delegated_max_turns."""


def _as_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for attr in ('model_dump', 'to_dict'):
        fn = getattr(obj, attr, None)
        if callable(fn):
            return fn()
    return dict(vars(obj))


class DelegatedRuntime(BaseLLMAgentService):
    capabilities = frozenset({'managed_agent_runtime'})

    def __init__(self, config: Optional[Dict[str, Any]] = None, client: Any = None,
                 tools: Optional[Dict[str, ToolSpec]] = None, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self._client = client
        self.tools: Dict[str, ToolSpec] = {}
        for name, spec in (tools or {}).items():
            self.bind_tool(name, *spec)

    def bind_tool(self, name: str, tool: BaseToolService, schema: Dict[str, Any]) -> None:
        if getattr(tool, 'material', False):
            raise MaterialToolRefused(f'{name} has a material effect; a delegated runtime may only read')
        self.tools[name] = (tool, schema)

    def tool_declarations(self) -> List[Dict[str, Any]]:
        """The bound read tools as canonical declarations (name, description, parameters)."""
        return [{'name': n, 'description': s.get('description', ''),
                 'parameters': s.get('parameters') or {'type': 'object', 'properties': {}}}
                for n, (_, s) in self.tools.items()]

    async def run_tool(self, name: str, arguments: Dict[str, Any], tenant_id: str,
                       config: Dict[str, Any], session: Dict[str, Any]) -> str:
        """Execute one host-side read tool for the runtime; the JSON result text goes back to it.
        `session` is the per-session dict _dispatch_upstream_call passes to _run_session; it
        counts the round-trips against delegated_max_turns."""
        session['turns'] = session.get('turns', 0) + 1
        limit = int(resolve_config('delegated_max_turns', config))
        if session['turns'] > limit:
            raise DelegatedTurnLimit(f'the managed session exceeded delegated_max_turns={limit}')
        if name not in self.tools:
            return json.dumps({'error': f"tool '{name}' is not available to this agent"})
        tool, _ = self.tools[name]
        if getattr(tool, 'material', False):
            return json.dumps({'error': f"tool '{name}' has a material effect and is refused"})
        try:
            out = await tool.execute(dict(arguments or {}), config, tenant_id=tenant_id, safe_to_retry=True)
        except Exception as e:                        # report it to the runtime; do not crash the session
            return json.dumps({'error': f'{type(e).__name__}: {e}'})
        return json.dumps(_as_dict(out), default=str)

    def _client_for(self, config: Dict[str, Any], kwargs: Dict[str, Any]) -> Any:
        if self._client is None:
            self._client = self._build_client({**self._config, **(config or {})}, kwargs)
        return self._client

    def _build_client(self, config: Dict[str, Any], kwargs: Dict[str, Any]) -> Any:
        """Hook: the platform client, built on first use. Default: none."""
        return None

    @abstractmethod
    async def _run_session(self, messages: List[Dict[str, Any]], config: Dict[str, Any],
                           tenant_id: str, session: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Run one managed session to completion, calling self.run_tool(..., session=session)
        for each host-side tool call; return a canonical result (ModelResult or its raw dict)."""

    async def _dispatch_upstream_call(self, messages, model_id, temperature, max_tokens,
                                      tools, stop_sequences, cache_handle, config, **kwargs):
        return await self._run_session(messages, config, kwargs.get('tenant_id'), {'turns': 0}, kwargs)

    async def _dispatch_upstream_stream(self, messages, model_id, temperature, max_tokens, config, **kwargs):
        out = await self._run_session(messages, config, kwargs.get('tenant_id'), {'turns': 0}, kwargs)
        yield _as_dict(out).get('text', '') if not hasattr(out, 'text') else out.text
