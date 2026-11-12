"""E2A: BaseToolService, RestToolService, MCPToolService, BaseMCPServer and tool routing.

Split from e2a_base.py in v1.2.0 without code changes; e2a_base re-exports every name."""
import os
import re
import json
import uuid
import time
import asyncio
import inspect
import hashlib
import logging
import threading
import contextlib
import random
import concurrent.futures
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from .core import Deadline, PhysicalOutcome
from .config import resolve_config
from .resilience import ResiliencePolicy, run_sync
from ._propagation import _PropagationMixin
 
# ==================================================
# BaseToolService  (client — REST or MCP transport)
# ==================================================
 
class BaseToolService(ABC, _PropagationMixin):
 
    async def execute(self, payload: Dict[str, Any],
                       config: Dict[str, Any] = None, tenant_id: str = None,
                       failed_keys: List[str] = None, **kwargs) -> Dict[str, Any]:
        """Return contract: every result MUST include physical_success: bool.
        This is the framework's Logical-vs-Physical rule — an agent's
        textual narrative ('I processed the transfer') is never treated
        as ground truth. Only this typed field is. Callers (BaseWorkflow,
        _resolve_tool_call()) check physical_success before proceeding,
        regardless of what the agent's response text claims."""
        config = config or {}
        if not self._validate_input(payload, config, **kwargs):
            raise ValueError('Invalid input')
        key = kwargs.get('idempotency_key', payload.get('idempotency_key'))
        if key and not self._commit_gate(key, failed_keys, config, **kwargs):
            return {'status': 'blocked', 'reason': 'commit_gate'}
        endpoint = self.get_endpoint(config, tenant_id=tenant_id, **kwargs)
        # tenant_id reaches every hook, so a transport can scope the call (v1.1.1)
        body = self._build_payload(payload, config, tenant_id=tenant_id, **kwargs)
        return await self.__http_post(endpoint, body, config, tenant_id=tenant_id, **kwargs)
 
    @abstractmethod
    def get_endpoint(self, config=None, tenant_id=None, **kwargs):
        """kwargs: base_url, service_name, tenant_routing_map"""
 
    @abstractmethod
    def _build_payload(self, payload, config=None, **kwargs):
        """kwargs: schema, enrichments"""
 
    @abstractmethod
    def _validate_input(self, payload, config=None, **kwargs):
        """kwargs: schema, required_fields"""
 
    # PRIVATE — framework owns HTTP mechanics, resilience and outcome typing
    def _resilience_policy(self, config=None, kwargs=None) -> 'ResiliencePolicy':
        """One ResiliencePolicy (retry budget, circuit, bulkhead) per tool
        service instance = per dependency, so one slow dependency saturates
        only its own bulkhead."""
        pol = self.__dict__.get('_e2a_policy')
        if pol is None:
            pol = self.__dict__['_e2a_policy'] = ResiliencePolicy(type(self).__name__, config, kwargs)
        return pol

    async def __http_post(self, endpoint, body, config=None, **kwargs):
        timeout = float(resolve_config('timeout', config, kwargs))
        retries = int(resolve_config('retries', config, kwargs))
        token = resolve_config('auth_token', config, kwargs)
        deadline = Deadline.coerce(kwargs.get('deadline'))
        # A call is replay-safe only with an idempotency key (or an explicit
        # safe_to_retry for reads); only then may a timeout be retried.
        replay_safe = bool(kwargs.get('idempotency_key')) or bool(kwargs.get('safe_to_retry'))

        async def attempt():
            t = deadline.bound(timeout) if deadline else timeout
            return await asyncio.wait_for(
                self._send_request(endpoint, body, token=token, timeout=t, config=config, **kwargs),
                timeout=t)
        try:
            result = await self._resilience_policy(config, kwargs).call(
                attempt, deadline=deadline, max_retries=retries if replay_safe else 0,
                idempotent=replay_safe)
        except (asyncio.TimeoutError, TimeoutError):
            # The request may or may not have been committed: typed UNKNOWN,
            # never a plain failure that a retry would re-execute.
            return {'status': 'UNKNOWN', 'endpoint': endpoint, 'physical_success': False,
                    'outcome': PhysicalOutcome.UNKNOWN,
                    'error': 'timeout after the request may have been committed; reconcile, do not blindly retry'}
        # physical_success reflects the real network/API outcome —
        # callers must check this, never an agent's narrative about it.
        result.setdefault('outcome', PhysicalOutcome.COMMITTED if result.get('physical_success') else PhysicalOutcome.NOT_COMMITTED)
        return result

    async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kwargs):
        """Protected transport hook (aiohttp/httpx call in a real subclass).
        Return {'status', 'physical_success': bool, ...}; raise on
        transport failure. Default is the reference stub."""
        return {'status': 'ok', 'endpoint': endpoint, 'physical_success': True}

class RestToolService(BaseToolService):
    """Classic REST/HTTP tool."""
 
    def get_endpoint(self, config=None, tenant_id=None, **kwargs):
        config = config or {}
        routing = resolve_config('tenant_routing_map', config, kwargs)
        if tenant_id and tenant_id in routing:
            return routing[tenant_id]
        return kwargs.get('base_url') or resolve_config('tool_base_url', config, kwargs)
 
    def _build_payload(self, payload, config=None, **kwargs):
        return payload
 
    def _validate_input(self, payload, config=None, **kwargs):
        required = kwargs.get('required_fields', [])
        return all(f in payload for f in required)
 
class MCPToolService(BaseToolService):
    """MCP tool over Streamable HTTP. Reuses BaseToolService.__http_post
    unchanged — MCP Streamable HTTP is JSON-RPC over HTTP POST, so only
    get_endpoint() and _build_payload() differ from RestToolService.
    Points at either an external MCP server or one hosted locally via
    a BaseMCPServer instance."""
 
    def get_endpoint(self, config=None, tenant_id=None, **kwargs):
        config = config or {}
        return resolve_config('mcp_server_url', config, kwargs)
 
    def _build_payload(self, payload, config=None, **kwargs):
        tool_name = kwargs.get('mcp_tool_name', payload.get('_tool_name'))
        return {
            'jsonrpc': '2.0', 'method': 'tools/call',
            'params': {'name': tool_name, 'arguments': payload},
        }
 
    def _validate_input(self, payload, config=None, **kwargs):
        required = kwargs.get('required_fields', [])
        return all(f in payload for f in required)
 
# ==================================================
# BaseMCPServer  (server — wraps existing APIs as MCP tools)
# ==================================================
 
class BaseMCPServer(ABC, _PropagationMixin):
    """Turns wrapped BaseToolService instances into a spec-compliant
    MCP server: tools/list + tools/call over JSON-RPC."""
 
    server_name: str = 'e2a-mcp-server'
 
    def __init__(self):
        self.registered_tools: Dict[str, dict] = {}
 
    def serve(self, request: Dict[str, Any],
              config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        """Public entry point. Single JSON-RPC dispatch surface for the
        server, mirroring run()/execute()/retrieve()."""
        response = self.__dispatch_jsonrpc(request, config, **kwargs)
        # v1.1.2: tools/call delegates to an async handler; complete it here so the
        # caller always receives a finished JSON-RPC result, never a coroutine.
        if inspect.isawaitable(response.get('result')):
            response['result'] = run_sync(response['result'],
                                          timeout=float(resolve_config('timeout', config, kwargs)) * 4)
        return response
 
    def wrap_api_as_tool(self, handler, tool_name: str, description: str,
                          input_schema: dict, config: Dict[str, Any] = None,
                          **kwargs) -> None:
        """Factory method — 'create an MCP server by wrapping an API'.
        handler is any existing BaseToolService instance; its execute()
        is reused as-is. No new transport code is written per tool."""
        self.registered_tools[tool_name] = {
            'handler': handler, 'description': description,
            'input_schema': input_schema,
        }
 
    def _list_tools(self, config: Dict[str, Any] = None, **kwargs) -> List[dict]:
        """Default: derive tools/list output from registered_tools.
        Override only to filter/augment (e.g. per-tenant visibility)."""
        return [
            {'name': name, 'description': entry['description'],
             'inputSchema': entry['input_schema']}
            for name, entry in self.registered_tools.items()
        ]
 
    async def _call_tool(self, tool_name: str, arguments: dict,
                          config: Dict[str, Any] = None, tenant_id: str = None,
                          **kwargs) -> Dict[str, Any]:
        """Default: resolve tool_name and delegate to the wrapped
        handler's execute(). Returns an MCP-shaped result."""
        entry = self.registered_tools.get(tool_name)
        if not entry:
            return {'content': [{'type': 'text',
                    'text': f'Unknown tool: {tool_name}'}], 'isError': True}
        result = await entry['handler'].execute(
            arguments, config, tenant_id=tenant_id, **kwargs)
        return {'content': [{'type': 'text', 'text': str(result)}],
                'isError': False}
 
    # PRIVATE — framework-owned JSON-RPC envelope handling
    def __dispatch_jsonrpc(self, request, config=None, **kwargs):
        method = request.get('method')
        req_id = request.get('id')
        if method == 'tools/list':
            result = self._list_tools(config, **kwargs)
        elif method == 'tools/call':
            params = request.get('params', {})
            result = self._call_tool(
                params.get('name'), params.get('arguments', {}),
                config, **kwargs)
        else:
            return {'jsonrpc': '2.0', 'id': req_id,
                    'error': {'code': -32601, 'message': 'Method not found'}}
        return {'jsonrpc': '2.0', 'id': req_id, 'result': result}
 
# ==================================================
# Tool call routing — MCP-first, REST fallback
# lives beside _get_agent(); called from the orchestration layer
# ==================================================
 
async def _resolve_tool_call(tool_name: str, payload: dict,
                              config: Dict[str, Any] = None,
                              tenant_id: str = None, **kwargs) -> dict:
    """1. Does a wrapped MCP server exist for this tool? -> call it as
    an MCP tool call. 2. Otherwise -> execute the normal tool call
    (REST, via _get_tool_service's existing resolution)."""
    config = config or {}
    mcp_registry = resolve_config('mcp_server_registry', config, kwargs)
    entry = mcp_registry.get(tool_name)
    if entry:
        service = MCPToolService()
        return await service.execute(
            payload, config, tenant_id=tenant_id,
            mcp_server_url=entry['mcp_server_url'],
            mcp_tool_name=entry.get('mcp_tool_name', tool_name),
            **kwargs)
    service = _get_tool_service(tool_name, config, **kwargs)
    return await service.execute(payload, config, tenant_id=tenant_id, **kwargs)
 
def _get_tool_service(tool_name: str, config: Dict[str, Any] = None,
                       **kwargs) -> BaseToolService:
    """Fallback resolver, used only when no local MCP server wraps this
    tool. Resolution: kwargs > config['tool_registry'][tool_name] >
    default_transport."""
    config = config or {}
    registry = resolve_config('tool_registry', config, kwargs)
    entry = registry.get(tool_name, {})
    transport = kwargs.get('transport') or entry.get(
        'transport', resolve_config('default_transport', config, kwargs))
    # One long-lived instance per (transport, tool): its ResiliencePolicy
    # (retry budget, circuit, bulkhead) must be SHARED by every call to the
    # same dependency — a fresh instance per request would give each
    # request its own, empty breaker and budget.
    cache_key = (transport, tool_name)
    with _TOOL_SERVICE_LOCK:
        svc = _TOOL_SERVICE_CACHE.get(cache_key)
        if svc is None:
            svc = _TOOL_SERVICE_CACHE[cache_key] = MCPToolService() if transport == 'mcp' else RestToolService()
    return svc


_TOOL_SERVICE_CACHE: Dict[Any, 'BaseToolService'] = {}
_TOOL_SERVICE_LOCK = threading.Lock()
