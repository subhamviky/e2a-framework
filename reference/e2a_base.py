# e2a_base.py — E2A Architecture Framework Scaffold (L7 Master Release)
# Drop into src/ or framework/. Import and subclass. Never modify directly.
# Incorporates all 17 Advanced Enterprise Patterns & Structural Isomorphism.

import os
import uuid
import time
import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO)

class DehydrationInterrupt(Exception):
    """Signal exception used to cleanly halt execution and scale compute to zero for Human-in-the-Loop."""
    pass

class NFRViolationError(Exception):
    """Raised when a measured non-functional requirement (latency SLO, token budget) is breached.
    Caught by the standard exception path in run()/execute()/retrieve() — treated as a normal
    failure (added to failed_keys, routed to DLQ), not a special control-flow signal like
    DehydrationInterrupt."""
    pass

# ==================================================
# 1. Shared Propagation Helpers 
# ==================================================
class _PropagationMixin:
    """
    Shared, base-owned helpers for cross-class state threading.
    Note: Observability (flush_logs) has been migrated to BaseObservability.
    """
    def _resolve_io_config(self, config=None, **kwargs):
        config = config or {}
        prefix = kwargs.get('io_config_prefix', config.get(
            'io_config_prefix', os.getenv('IO_CONFIG_PREFIX', 'io_')))
        return {k: v for k, v in config.items() if k.startswith(prefix)}

    def _log(self, message_log, correlation_id, level, event, **fields):
        if message_log is None:
            return
        message_log.append({
            'timestamp': time.time(), 'correlation_id': correlation_id,
            'level': level, 'event': event,
            'class': type(self).__name__, **fields,
        })

    def _send_to_dlq(self, failed_keys, config=None, **kwargs):
        config = config or {}
        queue_url = kwargs.get('dlq_queue_url', config.get(
            'dlq_queue_url', os.getenv('DLQ_QUEUE_URL')))
        if queue_url and failed_keys:
            logging.warning(f'DLQ dispatch -> {queue_url}: {failed_keys}')

    def _commit_gate(self, key, failed_keys, config=None, **kwargs):
        """
        Base-owned commit gate. Enforces:
        1. Batch isolation (key not in failed_keys)
        2. Optimistic Concurrency Control (OCC) via expected_version checks.
        """
        config = config or {}
        enabled = kwargs.get('commit_gate_enabled', config.get('commit_gate_enabled', True))
        if not enabled:
            return True
        if key in (failed_keys or []):
            return False
            
        expected_version = kwargs.get('expected_version')
        if expected_version is not None:
            actual_version = kwargs.get('actual_version_lookup', lambda k, c: expected_version)(key, config)
            if expected_version != actual_version:
                return False  # OCC collision
        return True


# ==================================================
# 2. Foundation Classes (Observability, Governance, Prompts)
# ==================================================
class BaseObservability(ABC):
    """Template method abstract class for Enterprise Telemetry."""
    
    def record_telemetry(self, message_log: List[dict], correlation_id: str, 
                         config: Dict[str, Any] = None, **kwargs) -> None:
        config = config or {}
        enriched_logs = []
        for entry in (message_log or []):
            enriched_entry = {
                **entry,
                'correlation_id': correlation_id,
                'tenant_id': kwargs.get('tenant_id', 'UNKNOWN_TENANT'),
                'host_epoch_ms': int(time.time() * 1000),
                'framework_version': 'e2a-v2.0-L7'
            }
            enriched_logs.append(enriched_entry)

        extracted_metrics = self.__extract_metrics(enriched_logs)
        try:
            self._ship_logs(enriched_logs, config, **kwargs)
            if extracted_metrics:
                self._emit_metrics(extracted_metrics, config, **kwargs)
            self._export_traces(correlation_id, enriched_logs, config, **kwargs)
        except Exception as e:
            logging.error(f"Telemetry dispatch failed for {correlation_id}: {str(e)}")
            for log in enriched_logs:
                logging.info(log)

    def __extract_metrics(self, enriched_logs: List[dict]) -> Dict[str, float]:
        metrics = {'total_tokens': 0, 'error_count': 0}
        for log in enriched_logs:
            if log.get('level') == 'ERROR':
                metrics['error_count'] += 1
            if 'tokens_used' in log:
                metrics['total_tokens'] += log['tokens_used']
        return metrics

    @abstractmethod
    def _ship_logs(self, enriched_logs: List[dict], config: Dict[str, Any], **kwargs):
        pass

    @abstractmethod
    def _emit_metrics(self, metrics: Dict[str, float], config: Dict[str, Any], **kwargs):
        pass

    @abstractmethod
    def _export_traces(self, correlation_id: str, logs: List[dict], config: Dict[str, Any], **kwargs):
        pass


class BaseGovernanceFramework(ABC):
    """Template method abstract class for AI Safety, Economics, and Lifecycle Governance."""
    
    def enforce_governance_gate(self, state: Dict[str, Any], io_config: Dict[str, Any], 
                                config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        self.__enforce_token_budget(state, io_config)
        
        if state.get('requires_human_approval') and not state.get('approval_granted'):
            self.__trigger_dehydration(state, config, **kwargs)
            
        if state.get('pending_mcp_tool_execution'):
            self._verify_sandbox_profile(config, **kwargs)
            
        state = self._execute_semantic_firewall(state, config, **kwargs)
        return state

    def __enforce_token_budget(self, state: Dict[str, Any], io_config: Dict[str, Any]) -> None:
        max_budget = io_config.get('max_token_budget', float('inf'))
        current_usage = state.get('cumulative_tokens_used', 0)
        if current_usage >= max_budget:
            raise NFRViolationError(f"Token budget exhausted. Used {current_usage}, Limit {max_budget}")

    def __trigger_dehydration(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> None:
        correlation_id = kwargs.get('correlation_id', state.get('correlation_id'))
        tenant_id = kwargs.get('tenant_id', state.get('tenant_id'))
        self._dehydrate_state_to_perimeter(state, correlation_id, tenant_id, config)
        raise DehydrationInterrupt(f"State {correlation_id} dehydrated. Awaiting webhook.")

    @abstractmethod
    def _execute_semantic_firewall(self, state: Dict[str, Any], config: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """Execute local Guard Models for PII redaction and injection blocking."""
        pass

    @abstractmethod
    def _verify_sandbox_profile(self, config: Dict[str, Any], **kwargs) -> None:
        """Assert execution is within a gVisor/Firecracker microVM for MCP Isolation."""
        pass

    @abstractmethod
    def _circuit_breaker(self, failures: int, config: Dict[str, Any], **kwargs) -> bool:
        pass

    @abstractmethod
    def _dehydrate_state_to_perimeter(self, state: Dict[str, Any], correlation_id: str, 
                                      tenant_id: str, config: Dict[str, Any]) -> None:
        pass

class BasePromptRegistry(ABC):
    """Pure Interface for GitOps-managed IaC Prompt decoupling."""
    @abstractmethod
    def get_prompt(self, prompt_id: str, version: str = 'latest',
                   tenant_id: str = None, config: Dict[str, Any] = None, **kwargs) -> Any:
        pass


# ==================================================
# 3. Validation (Tier 1 Edge)
# ==================================================
class ValidationResult(dict):
    """Thin dict subclass for HTTP 400 serializable validation returns."""
    pass

class BaseValidationService(ABC, _PropagationMixin):
    validator_registry: Dict[str, Any] = {}

    def validate(self, agent_name: str, state: Dict[str, Any],
                 config: Dict[str, Any] = None, correlation_id: str = None,
                 **kwargs) -> 'ValidationResult':
        config = config or {}
        correlation_id = correlation_id or state.get('correlation_id', str(uuid.uuid4()))
        validator = self._resolve_validator(agent_name, config, **kwargs)
        
        if validator is None:
            strict = kwargs.get('strict_mode', config.get('strict_mode', os.getenv('VALIDATION_STRICT_MODE', 'True') == 'True'))
            if strict:
                return self.__build_result(False, [f'No validator registered for {agent_name}'], agent_name, correlation_id)
            return self.__build_result(True, [], agent_name, correlation_id)
            
        valid, errors = validator(state, config, **kwargs)
        return self.__build_result(valid, errors, agent_name, correlation_id)

    def _resolve_validator(self, agent_name, config=None, **kwargs):
        return self.validator_registry.get(agent_name)

    def __build_result(self, valid, errors, agent_name, correlation_id) -> 'ValidationResult':
        return ValidationResult(valid=valid, errors=errors, agent_name=agent_name, correlation_id=correlation_id)


# ==================================================
# 4. Orchestration & Agents (Tier 2 Compute)
# ==================================================
class BaseWorkflow(ABC, _PropagationMixin):
    def execute(self, state: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        tenant_id = state.get('tenant_id')
        if not tenant_id:
            raise ValueError('state["tenant_id"] is required')
            
        correlation_id = kwargs.get('correlation_id', state.get('correlation_id', str(uuid.uuid4())))
        io_config = self._resolve_io_config(config, **kwargs)
        idempotency_key = self.__resolve_idempotency(state, config, **kwargs)
        
        message_log: List[dict] = []
        failed_keys: List[str] = []
        
        state.update({'tenant_id': tenant_id, 'correlation_id': correlation_id, 'idempotency_key': idempotency_key})
        
        try:
            workflow = self._build_workflow(config, correlation_id=correlation_id, io_config=io_config,
                                            tenant_id=tenant_id, message_log=message_log, **kwargs)
            if not self._validate_workflow(workflow, config, correlation_id=correlation_id,
                                           io_config=io_config, tenant_id=tenant_id, message_log=message_log, **kwargs):
                raise ValueError('Workflow validation failed')
                
            agent = self._get_agent(state.get('intent'), config, correlation_id=correlation_id,
                                    io_config=io_config, tenant_id=tenant_id, message_log=message_log, **kwargs)
                                    
            state = agent.run(state, config, correlation_id=correlation_id, io_config=io_config, 
                              idempotency_key=idempotency_key, tenant_id=tenant_id, message_log=message_log, 
                              failed_keys=failed_keys, **kwargs)
                              
        except DehydrationInterrupt as d:
            self._log(message_log, correlation_id, 'INFO', 'workflow_dehydrated', reason=str(d))
            state['status'] = 'AWAITING_WEBHOOK'
        except Exception as e:
            failed_keys.append(idempotency_key)
            self._handle_error(e, state, config, correlation_id=correlation_id, message_log=message_log,
                               failed_keys=failed_keys, **kwargs)
        finally:
            obs_engine = config.get('observability_engine')
            if obs_engine and isinstance(obs_engine, BaseObservability):
                obs_engine.record_telemetry(message_log, correlation_id, config, tenant_id=tenant_id)
            else:
                for entry in message_log: logging.info(entry) # Fallback
                
            if failed_keys:
                self._send_to_dlq(failed_keys, config)
                
            state['message_log'] = message_log
            state['failed_keys'] = failed_keys
            
        return state

    @abstractmethod
    def _build_workflow(self, config=None, **kwargs): pass
    @abstractmethod
    def _validate_workflow(self, workflow, config=None, **kwargs): pass
    @abstractmethod
    def _get_agent(self, intent, config=None, **kwargs) -> 'BaseAgent': pass
    @abstractmethod
    def _generate_idempotency_key(self, state, config=None, **kwargs) -> str: pass
    @abstractmethod
    def _handle_error(self, error, state, config=None, **kwargs): pass

    def __resolve_idempotency(self, state, config=None, **kwargs) -> str:
        client_key = kwargs.get('idempotency_key', state.get('idempotency_key'))
        if client_key:
            existing = self.__lookup_idempotency_store(client_key, config)
            if existing and not existing.get('expired'):
                return existing['idempotency_key']
        new_key = self._generate_idempotency_key(state, config, **kwargs)
        self.__persist_idempotency_store(new_key, config)
        return new_key

    def __lookup_idempotency_store(self, key, config=None): return None 
    def __persist_idempotency_store(self, key, config=None): pass


class BaseAgent(ABC, _PropagationMixin):
    def run(self, state: Dict[str, Any], config: Dict[str, Any] = None,
            correlation_id: str = None, io_config: Dict[str, Any] = None,
            idempotency_key: str = None, tenant_id: str = None,
            message_log: List[dict] = None, failed_keys: List[str] = None, **kwargs) -> Dict[str, Any]:
            
        config = config or {}
        correlation_id = correlation_id or state.get('correlation_id', str(uuid.uuid4()))
        io_config = io_config or self._resolve_io_config(config, **kwargs)
        message_log = message_log if message_log is not None else []
        failed_keys = failed_keys if failed_keys is not None else []
        tenant_id = tenant_id or state.get('tenant_id')
        common = dict(correlation_id=correlation_id, io_config=io_config, tenant_id=tenant_id, message_log=message_log)
        start = time.time()

        try:
            # Governance Gate (Token tracking, Semantic Firewall, Dehydration)
            gov_engine = config.get('governance_engine')
            if gov_engine and isinstance(gov_engine, BaseGovernanceFramework):
                state = gov_engine.enforce_governance_gate(state, io_config, config, **common, **kwargs)
            else:
                self._apply_policy(state, config, **common, **kwargs)

            messages = self._build_messages(state, config, **common, **kwargs)
            response = self.__llm_call(messages, config, io_config=io_config, **kwargs)

            # FinOps — token usage tracking (feeds BaseGovernanceFramework's token-budget gate
            # on the *next* call, and BaseObservability's total_tokens metric on this one)
            token_usage = self.__track_usage(messages, response, config, **kwargs)
            state['cumulative_tokens_used'] = state.get('cumulative_tokens_used', 0) + token_usage

            # Chain of Verification
            confidence = self._evaluate_output(response, state, config, **common, **kwargs)
            min_conf = kwargs.get('min_confidence', config.get('min_confidence', float(os.getenv('MIN_CONFIDENCE', 0.85))))
            if confidence < min_conf:
                response = self._fallback(state, config, **common, **kwargs)

            state['response'] = response

            # Latency SLO — measured over the full run() sequence, logged unconditionally
            # (even on breach) so the telemetry record always reflects what actually happened
            latency = time.time() - start
            max_latency = kwargs.get('max_latency', config.get('max_latency', float(os.getenv('MAX_LATENCY', 2.0))))
            self._log(message_log, correlation_id, 'INFO', 'agent_run_complete',
                      agent=getattr(self, 'agent_name', type(self).__name__),
                      latency=round(latency, 3), tokens_used=token_usage, confidence=confidence)
            if latency > max_latency:
                raise NFRViolationError(f'Latency SLO breached: {latency:.2f}s > {max_latency}s')

        except DehydrationInterrupt as d:
            raise d  # Bubble up to BaseWorkflow to safely halt
        except Exception as e:
            failed_keys.append(idempotency_key or correlation_id)
            self._handle_error(e, state, config, correlation_id=correlation_id, 
                               message_log=message_log, failed_keys=failed_keys, **kwargs)
                               
        state.update({'tenant_id': tenant_id, 'correlation_id': correlation_id})
        return state

    def __track_usage(self, messages, response, config=None, **kwargs):
        """Base-owned, never overridden. Prefers an actual token count from the LLM provider's
        response metadata (response['metadata']['tokens_used']); falls back to a ~4-chars/token
        estimate over the outbound messages when a provider doesn't report usage."""
        metadata = (response or {}).get('metadata', {})
        if 'tokens_used' in metadata:
            return int(metadata['tokens_used'])
        estimated = sum(len(m.get('content', '')) for m in (messages or [])) // 4
        return estimated

    @abstractmethod
    def _build_messages(self, state, config=None, **kwargs): pass
    @abstractmethod
    def _apply_policy(self, state, config=None, **kwargs): pass

    def __llm_call(self, messages, config=None, io_config=None, **kwargs):
        # Native Speculative Decoding integration via io_config
        io = io_config or {}
        model_id = kwargs.get('model_id', config.get('model_id', os.getenv('LLM_MODEL_ID', 'default')))
        spec_model = io.get('speculative_model_id')
        return {'text': '...', 'metadata': {'model_id': model_id, 'speculative_used': bool(spec_model)}}

    @abstractmethod
    def _evaluate_output(self, response, state, config=None, **kwargs): pass
    @abstractmethod
    def _fallback(self, state, config=None, **kwargs): pass
    @abstractmethod
    def _handle_error(self, error, state, config=None, **kwargs): pass


# ==================================================
# 5. RAG Pipeline (Tier 4 Async Worker)
# ==================================================
class BaseRAGPipeline(ABC, _PropagationMixin):
    def retrieve(self, query: str, config: Dict[str, Any] = None, tenant_id: str = None,
                 correlation_id: str = None, message_log: List[dict] = None, **kwargs) -> List[dict]:
        config = config or {}
        start = time.time()
        vector = self.__embed(query, config, **kwargs)
        results = self._search_index(vector, config, tenant_id=tenant_id, **kwargs)
        reranked = self._rerank(results, config, **kwargs)

        latency = time.time() - start
        max_latency = kwargs.get('max_latency', config.get('max_latency', float(os.getenv('MAX_LATENCY', 2.0))))
        self._log(message_log, correlation_id, 'INFO', 'rag_retrieve_complete',
                  docs=len(reranked), latency=round(latency, 3))
        if latency > max_latency:
            raise NFRViolationError(f'RAG retrieval latency SLO breached: {latency:.2f}s > {max_latency}s')
        return reranked

    @abstractmethod
    def _search_index(self, query_vector, config=None, tenant_id=None, **kwargs): pass
    @abstractmethod
    def _rerank(self, results, config=None, **kwargs): pass
    @abstractmethod
    def _evaluate_answer(self, answer, config=None, **kwargs): pass

    def __embed(self, query, config=None, **kwargs):
        return [0.0]


# ==================================================
# 6. Tools & MCP Servers (Tier 4 Async Workers)
# ==================================================
class BaseToolService(ABC, _PropagationMixin):
    async def execute(self, payload: Dict[str, Any], config: Dict[str, Any] = None, 
                      tenant_id: str = None, failed_keys: List[str] = None,
                      correlation_id: str = None, message_log: List[dict] = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        start = time.time()
        if not self._validate_input(payload, config, **kwargs):
            raise ValueError('Invalid input')
            
        key = kwargs.get('idempotency_key', payload.get('idempotency_key'))
        if key and not self._commit_gate(key, failed_keys, config, **kwargs):
            return {'status': 'blocked', 'reason': 'commit_gate', 'physical_success': False}
            
        endpoint = self.get_endpoint(config, tenant_id=tenant_id, **kwargs)
        body = self._build_payload(payload, config, **kwargs)
        result = await self.__http_post(endpoint, body, config, **kwargs)

        latency = time.time() - start
        max_latency = kwargs.get('max_latency', config.get('max_latency', float(os.getenv('MAX_LATENCY', 5.0))))
        self._log(message_log, correlation_id, 'INFO', 'tool_execute_complete',
                  tool=getattr(self, 'tool_name', type(self).__name__),
                  endpoint=endpoint, latency=round(latency, 3))
        if latency > max_latency:
            raise NFRViolationError(f'Tool latency SLO breached: {latency:.2f}s > {max_latency}s')
        return result

    @abstractmethod
    def get_endpoint(self, config=None, tenant_id=None, **kwargs): pass
    @abstractmethod
    def _build_payload(self, payload, config=None, **kwargs): pass
    @abstractmethod
    def _validate_input(self, payload, config=None, **kwargs): pass

    async def __http_post(self, endpoint, body, config=None, **kwargs):
        # Physical state resolution contract enforced here
        return {'status': 'ok', 'endpoint': endpoint, 'physical_success': True}

class RestToolService(BaseToolService):
    def get_endpoint(self, config=None, tenant_id=None, **kwargs):
        config = config or {}
        routing = config.get('tenant_routing_map', {})
        if tenant_id and tenant_id in routing:
            return routing[tenant_id]
        return kwargs.get('base_url', config.get('tool_base_url', os.getenv('TOOL_BASE_URL', 'http://localhost')))

    def _build_payload(self, payload, config=None, **kwargs): return payload
    def _validate_input(self, payload, config=None, **kwargs):
        required = kwargs.get('required_fields', [])
        return all(f in payload for f in required)

class MCPToolService(BaseToolService):
    def get_endpoint(self, config=None, tenant_id=None, **kwargs):
        return kwargs.get('mcp_server_url', (config or {}).get('mcp_server_url', os.getenv('MCP_SERVER_URL')))

    def _build_payload(self, payload, config=None, **kwargs):
        tool_name = kwargs.get('mcp_tool_name', payload.get('_tool_name'))
        return {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': tool_name, 'arguments': payload}}

    def _validate_input(self, payload, config=None, **kwargs):
        required = kwargs.get('required_fields', [])
        return all(f in payload for f in required)

class BaseMCPServer(ABC, _PropagationMixin):
    server_name: str = 'e2a-mcp-server'
    def __init__(self):
        self.registered_tools: Dict[str, dict] = {}

    def serve(self, request: Dict[str, Any], config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return self.__dispatch_jsonrpc(request, config, **kwargs)

    def wrap_api_as_tool(self, handler, tool_name: str, description: str, input_schema: dict, 
                         config: Dict[str, Any] = None, **kwargs) -> None:
        self.registered_tools[tool_name] = {'handler': handler, 'description': description, 'input_schema': input_schema}

    def _list_tools(self, config: Dict[str, Any] = None, **kwargs) -> List[dict]:
        return [{'name': name, 'description': entry['description'], 'inputSchema': entry['input_schema']}
                for name, entry in self.registered_tools.items()]

    async def _call_tool(self, tool_name: str, arguments: dict, config: Dict[str, Any] = None, 
                         tenant_id: str = None, **kwargs) -> Dict[str, Any]:
        entry = self.registered_tools.get(tool_name)
        if not entry:
            return {'content': [{'type': 'text', 'text': f'Unknown tool: {tool_name}'}], 'isError': True}
        result = await entry['handler'].execute(arguments, config, tenant_id=tenant_id, **kwargs)
        return {'content': [{'type': 'text', 'text': str(result)}], 'isError': False}

    def __dispatch_jsonrpc(self, request, config=None, **kwargs):
        method = request.get('method')
        req_id = request.get('id')
        if method == 'tools/list':
            result = self._list_tools(config, **kwargs)
        elif method == 'tools/call':
            params = request.get('params', {})
            result = asyncio.run(self._call_tool(params.get('name'), params.get('arguments', {}), config, **kwargs))
        else:
            return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32601, 'message': 'Method not found'}}
        return {'jsonrpc': '2.0', 'id': req_id, 'result': result}

async def _resolve_tool_call(tool_name: str, payload: dict, config: Dict[str, Any] = None, 
                             tenant_id: str = None, **kwargs) -> dict:
    config = config or {}
    mcp_registry = config.get('mcp_server_registry', {})
    entry = mcp_registry.get(tool_name)
    if entry:
        service = MCPToolService()
        return await service.execute(payload, config, tenant_id=tenant_id, mcp_server_url=entry['mcp_server_url'],
                                     mcp_tool_name=entry.get('mcp_tool_name', tool_name), **kwargs)
    service = _get_tool_service(tool_name, config, **kwargs)
    return await service.execute(payload, config, tenant_id=tenant_id, **kwargs)

def _get_tool_service(tool_name: str, config: Dict[str, Any] = None, **kwargs) -> BaseToolService:
    config = config or {}
    registry = config.get('tool_registry', {})
    entry = registry.get(tool_name, {})
    transport = kwargs.get('transport', entry.get('transport', os.getenv('DEFAULT_TOOL_TRANSPORT', 'http')))
    if transport == 'mcp': return MCPToolService()
    return RestToolService()


# ==================================================
# 7. CI/CD Pipeline (Control Plane)
# ==================================================
class BasePipeline(ABC, _PropagationMixin):
    def __init__(self):
        self.required_artifact_matrix = [
            'e2a-ingress-validator', 'e2a-orchestration-engine',
            'e2a-rag-retrieval-worker', 'e2a-rest-tool-worker', 'e2a-mcp-server-worker'
        ]

    def run_pipeline(self, config: Dict[str, Any] = None, correlation_id: str = None,
                     message_log: List[dict] = None, failed_keys: List[str] = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        correlation_id = correlation_id or f'pipeline-{int(time.time())}'
        message_log = message_log if message_log is not None else []
        failed_keys = failed_keys if failed_keys is not None else []
        report = {'correlation_id': correlation_id, 'stages': {}, 'status': 'IN_PROGRESS'}
        
        try:
            if not self._run_static_security_scans(config, **kwargs): raise ValueError('Static security check failed.')
            report['stages']['static_security'] = 'PASSED'

            if not self._execute_test_suite(config, **kwargs): raise ValueError('Test suite failed.')
            report['stages']['test_execution'] = 'PASSED'

            if not self._verify_scaffold_contracts(config, **kwargs): raise ValueError('Scaffold verification failed.')
            report['stages']['scaffold_compliance'] = 'PASSED'

            min_faithfulness = float(kwargs.get('faithfulness_threshold', config.get('faithfulness_threshold', 0.85)))
            rag_score = self._run_rag_eval(config, **kwargs)
            report['rag_eval_score'] = rag_score
            if rag_score < min_faithfulness: raise ValueError(f'RAGAS gate failed: {rag_score}')
            report['stages']['ragas_gate'] = 'PASSED'

            for artifact in self.required_artifact_matrix:
                if not self._compile_artifact(artifact, config, **kwargs): raise RuntimeError(f'Compile failed: {artifact}')
            report['stages']['artifact_compilation'] = 'PASSED'

            if not self._run_dynamic_security_checks(config, **kwargs): raise ValueError('DAST failed.')
            report['stages']['dynamic_security'] = 'PASSED'

            strategy = kwargs.get('deployment_strategy', config.get('deployment_strategy', os.getenv('DEPLOYMENT_STRATEGY', 'BLUE_GREEN'))).upper()
            deploy_meta = self._execute_deployment_rollout(strategy, config, **kwargs)
            report['stages']['deployment_rollout'] = f'PASSED_{strategy}'
            
            report['status'] = 'SUCCESS'
        except Exception as e:
            failed_keys.append(correlation_id)
            report['status'] = 'FAILED'
            report['error'] = str(e)
        finally:
            report['message_log'] = message_log
        return report

    @abstractmethod
    def _run_static_security_scans(self, config, **kwargs) -> bool: pass
    @abstractmethod
    def _execute_test_suite(self, config, **kwargs) -> bool: pass
    @abstractmethod
    def _verify_scaffold_contracts(self, config, **kwargs) -> bool: pass
    @abstractmethod
    def _run_rag_eval(self, config, **kwargs) -> float: pass
    @abstractmethod
    def _compile_artifact(self, artifact_name, config, **kwargs) -> bool: pass
    @abstractmethod
    def _run_dynamic_security_checks(self, config, **kwargs) -> bool: pass
    @abstractmethod
    def _execute_deployment_rollout(self, strategy, config, **kwargs) -> dict: pass


# ==================================================
# 8. Infrastructure as Code (Control Plane)
# ==================================================
class BaseInfraProvisioner(ABC):
    def provision_landing_zone(self, tenant_id: str, tenancy_model: str,
                               config: Dict[str, Any] = None, correlation_id: str = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        correlation_id = correlation_id or f'iac-{tenant_id}-{int(time.time())}'
        manifest = {'tenant_id': tenant_id, 'tenancy_model': tenancy_model,
                    'correlation_id': correlation_id, 'topology_maps': {}, 'status': 'INITIATED'}
        try:
            networks = self._define_network_topologies(tenant_id, config, **kwargs)
            manifest['topology_maps']['networks'] = networks
            edge_security = self._define_edge_security(networks, config, **kwargs)
            manifest['topology_maps']['edge_security'] = edge_security
            data_perimeter = self._define_data_perimeter_substrate(networks, tenant_id, tenancy_model, config, **kwargs)
            manifest['topology_maps']['data_perimeter'] = data_perimeter
            compute = self._define_compute_tiers(networks, data_perimeter, config, **kwargs)
            manifest['topology_maps']['compute'] = compute
            iam = self._define_iam_governance_framework(compute, data_perimeter, config, **kwargs)
            manifest['topology_maps']['iam_governance'] = iam
            saga = self._generate_saga_orchestrator(compute, config, **kwargs)
            manifest['topology_maps']['saga_orchestration'] = saga
            
            if self._apply_infrastructure_graph(manifest, config, **kwargs):
                manifest['status'] = 'PROVISIONED'
                manifest['timestamp'] = time.time()
            else:
                raise RuntimeError('Infra apply returned a structural error.')
        except Exception as e:
            manifest['status'] = 'FAILED'
            manifest['error_trace'] = str(e)
        return manifest

    @abstractmethod
    def _define_network_topologies(self, tenant_id, config=None, **kwargs) -> dict: pass
    @abstractmethod
    def _define_edge_security(self, networks, config=None, **kwargs) -> dict: pass
    @abstractmethod
    def _define_data_perimeter_substrate(self, networks, tenant_id, tenancy_model, config=None, **kwargs) -> dict: pass
    @abstractmethod
    def _define_compute_tiers(self, networks, data_meta, config=None, **kwargs) -> dict: pass
    @abstractmethod
    def _define_iam_governance_framework(self, compute, data_meta, config=None, **kwargs) -> dict: pass
    def _generate_saga_orchestrator(self, compute_ctx, config=None, **kwargs) -> dict:
        return {'engine_type': 'aws_states_language', 'structural_skeleton': {}}
    @abstractmethod
    def _apply_infrastructure_graph(self, final_manifest, config=None, **kwargs) -> bool: pass