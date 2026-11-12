"""E2A: BasePipeline (delivery gates).

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

from .config import resolve_config
from .resilience import validate_capacity
from ._propagation import _PropagationMixin
# ==================================================
# BasePipeline — control-plane class. CI/CD lifecycle engine,
# the code-level counterpart to Landing Zone Section 9.
# Run once per commit/deploy, not per request.
# ==================================================
 
class BasePipeline(ABC, _PropagationMixin):
 
    def __init__(self):
        # One image per E2A class family that deploys as its own
        # service. Tier 0 (API Gateway) is a managed service, not a
        # custom image; Tier 5 (Saga) is IaC-defined via
        # BaseInfraProvisioner, not a container — both correctly absent.
        self.required_artifact_matrix = [
            'e2a-ingress-validator',      # Tier 1
            'e2a-orchestration-engine',   # Tier 2 (Workflow + Agent)
            'e2a-rag-retrieval-worker',   # Tier 4
            'e2a-rest-tool-worker',       # Tier 4
            'e2a-mcp-server-worker',      # Tier 4
        ]
 
    def run_pipeline(self, config: Dict[str, Any] = None,
                      correlation_id: str = None,
                      message_log: List[dict] = None,
                      failed_keys: List[str] = None, **kwargs) -> Dict[str, Any]:
        """Public entry point. Seven fixed stages, cheapest/fastest
        checks first. Any stage failing is caught here, not raised
        past this boundary — same philosophy as every other run()/
        execute() in this framework."""
        config = config or {}
        correlation_id = correlation_id or f'pipeline-{int(time.time())}'
        message_log = message_log if message_log is not None else []
        failed_keys = failed_keys if failed_keys is not None else []
        report = {'correlation_id': correlation_id, 'stages': {}, 'status': 'IN_PROGRESS'}
        try:
            if not self._run_static_security_scans(config, **kwargs):
                raise ValueError('Static security check failed: vulnerabilities or exposed secrets.')
            report['stages']['static_security'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'static_security_passed')
 
            if not self._execute_test_suite(config, **kwargs):
                raise ValueError('Test suite failed: regression detected in tests/.')
            report['stages']['test_execution'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'test_suite_passed')
 
            capacity = self._validate_capacity_budget(config, **kwargs)
            report['capacity_violations'] = capacity
            if capacity:
                raise ValueError('Capacity budget violated: ' + '; '.join(capacity))
            if not self._verify_scaffold_contracts(config, **kwargs):
                raise ValueError('Scaffold verification failed: Governed Entry Point rules violated '
                                 '(see e2a.conformance).')
            report['stages']['scaffold_compliance'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'scaffold_compliance_passed')
 
            min_faithfulness = float(resolve_config('faithfulness_threshold', config, kwargs))
            rag_score = self._run_rag_eval(config, **kwargs)
            report['rag_eval_score'] = rag_score
            if rag_score < min_faithfulness:
                raise ValueError(f'RAGAS gate failed: {rag_score} below threshold {min_faithfulness}.')
            report['stages']['ragas_gate'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'ragas_gate_passed', score=rag_score)
 
            for artifact in self.required_artifact_matrix:
                if not self._compile_artifact(artifact, config, **kwargs):
                    raise RuntimeError(f'Failed to compile/push artifact: {artifact}')
            report['stages']['artifact_compilation'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'artifacts_compiled',
                       count=len(self.required_artifact_matrix))
 
            if not self._run_dynamic_security_checks(config, **kwargs):
                raise ValueError('DAST failed in the ephemeral staging sandbox.')
            report['stages']['dynamic_security'] = 'PASSED'
            self._log(message_log, correlation_id, 'INFO', 'dynamic_security_passed')
 
            strategy = str(resolve_config('deployment_strategy', config, kwargs)).upper()
            deploy_meta = self._execute_deployment_rollout(strategy, config, **kwargs)
            report['stages']['deployment_rollout'] = f'PASSED_{strategy}'
            report['deployment_metadata'] = deploy_meta
            report['status'] = 'SUCCESS'
        except Exception as e:
            failed_keys.append(correlation_id)
            report['status'] = 'FAILED'
            report['error'] = str(e)
            self._log(message_log, correlation_id, 'ERROR', 'pipeline_failed', error=str(e))
        finally:
            self._flush_logs(message_log, config)
            report['message_log'] = message_log
        return report
 
    def _validate_capacity_budget(self, config, **kwargs) -> List[str]:
        """Part of the scaffold-compliance gate: the capacity equation
        (replicas x per-instance concurrency vs pools, ceilings, retry
        amplification, lease and shutdown windows). Returns violations;
        override only to add project-specific rules."""
        return validate_capacity(config, **kwargs)

    @abstractmethod
    def _run_static_security_scans(self, config, **kwargs) -> bool:
        """SAST + secret detection (e.g. gitleaks) for exposed LLM API keys."""
 
    @abstractmethod
    def _execute_test_suite(self, config, **kwargs) -> bool:
        """Run tests/ — unit + integration against the scaffold contracts."""
 
    conformance_paths = ('src',)        # v1.2: what the default scaffold check reads

    def _verify_scaffold_contracts(self, config, **kwargs) -> bool:
        """Statically assert the Governed Entry Point rules (e2a.conformance) over
        `conformance_paths`: R1 (a framework-owned public or private method
        overridden) fails; R2 (a protected hook called from outside its class) and D1
        (a deprecated public hook) fail only with conformance_strict=True (the v1.3
        default); R3 (an extra public capability) is reported, never fails. Findings
        are kept in self.conformance_findings. v1.1 asserted "exactly one public
        method per class"; v1.2 allows a public method per real capability."""
        from pathlib import Path
        from .conformance import check_paths, failed
        paths = [p for p in self.conformance_paths if Path(p).exists()]
        self.conformance_findings = check_paths(paths)
        strict = bool(kwargs.get('conformance_strict', (config or {}).get('conformance_strict', False)))
        return not failed(self.conformance_findings, strict=strict)
 
    @abstractmethod
    def _run_rag_eval(self, config, **kwargs) -> float:
        """RAGAS faithfulness score against a golden dataset. See the
        NIAH reference implementation for a concrete example."""
 
    @abstractmethod
    def _compile_artifact(self, artifact_name, config, **kwargs) -> bool:
        """Build + push one OCI/Docker image to the artifact registry."""
 
    @abstractmethod
    def _run_dynamic_security_checks(self, config, **kwargs) -> bool:
        """DAST against a running instance in an ephemeral sandbox —
        shares infrastructure with the NIAH testing pattern."""
 
    @abstractmethod
    def _execute_deployment_rollout(self, strategy, config, **kwargs) -> dict:
        """Canary weight-shift or Blue-Green cutover. Deployment-level —
        not to be confused with Section 2.15's application-level Shadow
        Mode, which mirrors live request traffic instead of environments."""
