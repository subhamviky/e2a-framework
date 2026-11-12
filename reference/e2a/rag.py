"""E2A: BaseRAGPipeline (read side) and BaseIndexingPipeline (write side).

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
from ._propagation import _PropagationMixin
# ==================================================
# BaseRAGPipeline
# ==================================================
 
class BaseRAGPipeline(ABC, _PropagationMixin):
 
    def retrieve(self, query: str, config: Dict[str, Any] = None,
                 tenant_id: str = None, **kwargs) -> List[dict]:
        config = config or {}
        vector = self._embed_query(query, config, **kwargs)
        # hybrid: the index gets BOTH the raw text (keyword/BM25 leg)
        # and the vector (semantic leg) and fuses them itself.
        results = self._search_index(query_text=query, query_vector=vector,
                                     config=config, tenant_id=tenant_id, **kwargs)
        reranked = self._rerank(results, config, **kwargs)
        return reranked
 
    @abstractmethod
    def _search_index(self, query_text, query_vector, config=None, tenant_id=None, **kwargs):
        """Hybrid keyword + vector search. Namespace the search to
        f'{tenant_index_prefix}_{tenant_id}'. kwargs: top_k, hybrid_alpha"""
 
    @abstractmethod
    def _rerank(self, results, config=None, **kwargs):
        """kwargs: rerank_top_n"""
 
    @abstractmethod
    def _evaluate_answer(self, answer, config=None, **kwargs):
        """kwargs: groundedness_threshold"""
 
    def _embed_query(self, query, config=None, **kwargs):
        """Protected hook (v1.1.2): embed the query with the same model the indexing
        pipeline used for the documents. The default is the provider placeholder."""
        model = resolve_config('embedding_model', config, kwargs)
        return [0.0]  # provider call
 
# ==================================================
# BaseIndexingPipeline — write side of BaseRAGPipeline
# Fed by a change stream (CDC) or a bulk load; keeps the vector /
# keyword index consistent with the system of record.
# ==================================================
 
class BaseIndexingPipeline(ABC, _PropagationMixin):
    """Single public entry point: ingest(). Each document dict carries
    {'id', 'op': 'upsert'|'delete', 'text'/fields}. Per-document
    isolation: one failing document lands in failed_keys and never
    blocks the batch. The embedding model MUST match the one
    BaseRAGPipeline.retrieve() embeds queries with (same
    'embedding_model' key)."""
 
    def ingest(self, documents: List[Dict[str, Any]], config: Dict[str, Any] = None,
              correlation_id: str = None, io_config: Dict[str, Any] = None,
              idempotency_key: str = None, tenant_id: str = None,
              message_log: List[dict] = None, failed_keys: List[str] = None,
              **kwargs) -> Dict[str, Any]:
        config = config or {}
        if not tenant_id:
            raise ValueError('tenant_id is required for indexing')
        correlation_id = correlation_id or str(uuid.uuid4())
        io_config = io_config or self._resolve_io_config(config, **kwargs)
        message_log = message_log if message_log is not None else []
        failed_keys = failed_keys if failed_keys is not None else []
        common = dict(correlation_id=correlation_id, io_config=io_config,
                      tenant_id=tenant_id, message_log=message_log)
        indexed = deleted = 0
        for doc in documents:
            doc_id = doc.get('id')
            try:
                if not self._commit_gate(doc_id, failed_keys, config, **kwargs):
                    continue
                if doc.get('op', 'upsert') == 'delete':
                    self._delete_from_index(doc_id, config, **common, **kwargs)
                    deleted += 1
                else:
                    chunks = self._chunk_documents(doc, config, **common, **kwargs)
                    vectors = self._embed_documents([c['text'] for c in chunks], config,
                                                **common, **kwargs)
                    if len(vectors) != len(chunks):
                        raise ValueError('embedding count != chunk count')
                    self._write_index(chunks, vectors, config, **common, **kwargs)
                    indexed += 1
            except Exception as e:
                failed_keys.append(doc_id)
                self._handle_error(e, doc, config, correlation_id=correlation_id,
                                   message_log=message_log, failed_keys=failed_keys, **kwargs)
        self._log(message_log, correlation_id, 'INFO', 'indexing_complete',
                  indexed=indexed, deleted=deleted, failed=len(failed_keys))
        return {'correlation_id': correlation_id, 'tenant_id': tenant_id,
                'indexed': indexed, 'deleted': deleted, 'failed_keys': failed_keys}
 
    @abstractmethod
    def _chunk_documents(self, document, config=None, **kwargs) -> List[Dict[str, Any]]:
        """Return [{'text': ..., 'metadata': {...}}]. kwargs: chunk_size, chunk_overlap"""
 
    @abstractmethod
    def _embed_documents(self, texts, config=None, **kwargs) -> List[List[float]]:
        """kwargs: embedding_model — must equal the retrieval-side model."""
 
    @abstractmethod
    def _write_index(self, chunks, vectors, config=None, tenant_id=None, **kwargs):
        """Write to f'{tenant_index_prefix}_{tenant_id}'. Must be idempotent
        on chunk id so CDC at-least-once delivery is safe."""
 
    @abstractmethod
    def _delete_from_index(self, doc_id, config=None, tenant_id=None, **kwargs):
        """Remove every chunk of doc_id from the tenant namespace."""
 
    @abstractmethod
    def _handle_error(self, error, document, config=None, **kwargs):
        """Appends to failed_keys via kwargs['failed_keys'].append(...)"""
