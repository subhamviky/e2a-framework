"""E2A: BaseInfraProvisioner.

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

 
# ==================================================
# BaseInfraProvisioner — control-plane class. Stands up the
# substrate the Cloud Landing Zone HLD/LLD document describes.
# Not part of the request-time propagation contract (Section 2.1) —
# this runs from CI/CD, once per environment/tenant, not per request.
# ==================================================
 
class BaseInfraProvisioner(ABC):
 
    def provision_landing_zone(self, tenant_id: str, tenancy_model: str,
                                availability_tier: str = 'STANDARD',
                                config: Dict[str, Any] = None,
                                correlation_id: str = None, **kwargs) -> Dict[str, Any]:
        """Public entry point. tenancy_model is 'POOL' or 'SILO' (Section 10.2
        of the Landing Zone doc). availability_tier is 'STANDARD', 'HIGH', or
        'CRITICAL' (Landing Zone Section 8.4) — selects Multi-AZ/Multi-Region
        topology; carries no domain meaning of its own. Runs all seven steps
        in fixed order and returns a manifest; never raises past this boundary."""
        config = config or {}
        correlation_id = correlation_id or f'iac-{tenant_id}-{int(time.time())}'
        manifest = {
            'tenant_id': tenant_id, 'tenancy_model': tenancy_model,
            'availability_tier': availability_tier,
            'correlation_id': correlation_id, 'topology_maps': {}, 'status': 'INITIATED',
        }
        try:
            networks = self._define_network_topologies(
                tenant_id, availability_tier, config, **kwargs)
            manifest['topology_maps']['networks'] = networks
            edge_security = self._define_edge_security(networks, config, **kwargs)
            manifest['topology_maps']['edge_security'] = edge_security
            data_perimeter = self._define_data_perimeter_substrate(
                networks, tenant_id, tenancy_model, config, **kwargs)
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
    def _define_network_topologies(self, tenant_id, availability_tier,
                                    config=None, **kwargs) -> dict:
        """Public VPC/DMZ, Private VPC application space, and the Data &
        Messaging Perimeter's service boundary (VPC-SC / PrivateLink).
        availability_tier determines AZ/region count (STANDARD: 1, HIGH:
        >=2 AZs, CRITICAL: >=2 AZs across >=2 regions) — this contract only
        guarantees the count is honored; the subclass owns the concrete list."""
 
    @abstractmethod
    def _define_edge_security(self, networks, config=None, **kwargs) -> dict:
        """Tier 0: API Gateway + WAF/Cloud Armor, including prompt-injection
        filter rules (Semantic Firewall's edge-layer half)."""
 
    @abstractmethod
    def _define_data_perimeter_substrate(self, networks, tenant_id,
                                          tenancy_model, config=None, **kwargs) -> dict:
        """Tier 3: Intake Topic (push), Task Queue (pull, redrive at
        max_receives=3 to a DLQ), State/Outbox table routed per
        tenancy_model (POOL: shared table + partition key; SILO: separate
        instance/account)."""
 
    @abstractmethod
    def _define_compute_tiers(self, networks, data_meta, config=None, **kwargs) -> dict:
        """Tier 1 (validation function), Tier 2 (BaseWorkflow + BaseAgent,
        co-located), Tier 4 (decoupled workers, queue-depth autoscaled)."""
 
    @abstractmethod
    def _define_iam_governance_framework(self, compute, data_meta,
                                          config=None, **kwargs) -> dict:
        """One role binding PER TIER, not one shared credential:
        Tier 1 -> publish-only on the Intake Topic.
        Tier 2/4 -> broader intra-perimeter read/write.
        Tier 2 -> tool/database credentials read-only where the
        Read-Only Data pattern applies."""
 
    def _generate_saga_orchestrator(self, compute_ctx, config=None, **kwargs) -> dict:
        """Concrete AWS-flavored DEFAULT — override this for GCP/Azure.
        Unlike __http_post or _commit_gate, this is NOT framework-locked;
        Step Functions ASL is not valid GCP Workflows YAML or Azure Logic
        Apps JSON, so a non-AWS subclass must override the syntax while
        keeping the same logical shape (Choice on failed_keys -> Parallel
        with 3 tracks -> webhook dispatch)."""
        return {
            'engine_type': 'aws_states_language',
            'structural_skeleton': {
                'Comment': 'E2A Distributed Saga Orchestrator & Save Prevention Engine',
                'StartAt': 'EvaluateFailedKeys',
                'States': {
                    'EvaluateFailedKeys': {
                        'Type': 'Choice',
                        'Choices': [{'Variable': '$.failed_keys[0]', 'IsPresent': True,
                                     'Next': 'IsolateAndSplitBatch'}],
                        'Default': 'StandardDatabaseCommit',
                    },
                    'StandardDatabaseCommit': {'Type': 'Task', 'Next': 'DispatchFinalWebhook'},
                    'IsolateAndSplitBatch': {
                        'Type': 'Parallel',
                        'Branches': [
                            {'StartAt': 'CommitSuccessfulItems', 'States': {'...': '...'}},
                            {'StartAt': 'RunCompensatingRollback', 'States': {'...': '...'}},
                            {'StartAt': 'RouteToDLQ', 'States': {'...': '...'}},
                        ],
                        'Next': 'DispatchFinalWebhook',
                    },
                    'DispatchFinalWebhook': {'Type': 'Task', 'End': True},
                },
            },
        }
 
    @abstractmethod
    def _apply_infrastructure_graph(self, final_manifest, config=None, **kwargs) -> bool:
        """Terraform apply / Pulumi up / CDK deploy / raw SDK client. The
        only step that touches real infrastructure."""
 
 
class GCPInfraProvisioner(BaseInfraProvisioner):
    """Corrected reference implementation — overrides
    _generate_saga_orchestrator (GCP Workflows YAML, not ASL) and
    provisions one IAM role per tier instead of one shared account."""
 
    def _define_network_topologies(self, tenant_id, availability_tier='STANDARD',
                                    config=None, **kwargs):
        region_count = {'STANDARD': 1, 'HIGH': 1, 'CRITICAL': 2}.get(availability_tier, 1)
        return {
            'vpc_network_name': f'e2a-vpc-{tenant_id}',
            'vpc_service_perimeter': 'accessPolicies/e2a_policy/servicePerimeters/prod_perimeter',
            'availability_tier': availability_tier,
            'region_count': region_count,
        }
 
    def _define_edge_security(self, networks, config=None, **kwargs):
        return {
            'api_gateway_id': 'e2a-cloud-endpoints-gw',
            'cloud_armor_policy': 'securityPolicies/block-prompt-injection-rules',
        }
 
    def _define_data_perimeter_substrate(self, networks, tenant_id, tenancy_model, config=None, **kwargs):
        table_id = (f'projects/e2a-prod/databases/{tenant_id}-state-db' if tenancy_model == 'SILO'
                    else 'projects/e2a-prod/databases/shared-pool-state-db')
        return {
            'pubsub_intake_topic': 'projects/e2a-prod/topics/intake-topic',
            'pubsub_task_queue': 'projects/e2a-prod/topics/task-queue',
            'firestore_nosql_table': table_id,
        }
 
    def _define_compute_tiers(self, networks, data_meta, config=None, **kwargs):
        return {
            'tier1_validation_function': 'gcr.io/e2a-prod/ingress-validator:latest',
            'tier2_orchestrator_cloudrun': 'gcr.io/e2a-prod/orchestration-engine:latest',
            'tier4_worker_cloudrun': 'gcr.io/e2a-prod/async-workers:latest',
        }
 
    def _define_iam_governance_framework(self, compute, data_meta, config=None, **kwargs):
        # One binding per tier — a single shared service account would
        # collapse the Tier 1 publish-only / Tier 2+4 read-write
        # separation the Landing Zone doc's Section 3.2 depends on.
        return {
            'tier1_sa': 'e2a-validator-sa@e2a-prod.iam.gserviceaccount.com',
            'tier1_role': 'roles/pubsub.publisher',  # Intake Topic only
            'tier2_sa': 'e2a-orchestrator-sa@e2a-prod.iam.gserviceaccount.com',
            'tier2_role': 'roles/pubsub.editor,roles/datastore.user',
            'tier2_db_role': 'roles/datastore.viewer',  # read-only replica, if Read-Only Data pattern applies
            'tier4_sa': 'e2a-worker-sa@e2a-prod.iam.gserviceaccount.com',
            'tier4_role': 'roles/pubsub.subscriber,roles/datastore.user',
        }
 
    def _generate_saga_orchestrator(self, compute_ctx, config=None, **kwargs):
        # Overridden: GCP Workflows uses YAML, not Amazon States
        # Language — same logical shape as the AWS default, different syntax.
        return {
            'engine_type': 'gcp_workflows_yaml',
            'structural_skeleton': {
                'main': {
                    'steps': [
                        {'evaluate_failed_keys': {'switch': [
                            {'condition': '${len(failed_keys) > 0}', 'next': 'isolate_and_split_batch'},
                        ], 'next': 'standard_database_commit'}},
                        {'standard_database_commit': {'call': 'commit_all', 'next': 'dispatch_final_webhook'}},
                        {'isolate_and_split_batch': {'parallel': {'branches': [
                            {'commit_successful_items': {'call': 'commit_partial'}},
                            {'run_compensating_rollback': {'call': 'compensate'}},
                            {'route_to_dlq': {'call': 'dlq_publish'}},
                        ]}, 'next': 'dispatch_final_webhook'}},
                        {'dispatch_final_webhook': {'call': 'http.post', 'end': True}},
                    ],
                },
            },
        }
 
    def _apply_infrastructure_graph(self, final_manifest, config=None, **kwargs):
        return True  # gcloud SDK client or Terraform google provider apply
