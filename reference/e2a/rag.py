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
 
class BaseEmbeddingModel(ABC):
    """Port (v1.2): one embedding model shared by the indexing and the retrieval side,
    so documents and queries are embedded by the same model by construction.
    v1.6.1: `dimensions` (None = not declared) is checked against the index's."""
    model_id: str = 'unspecified'
    dimensions: Optional[int] = None

    @abstractmethod
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """One vector per text."""

    @abstractmethod
    def embed_query(self, text: str) -> List[float]:
        """The vector of one query."""


class BaseVectorIndex(ABC):
    """Port (v1.2): the index both sides share, namespaced per tenant.
    v1.6.1: `dimensions` (None = not declared) must equal the embedding model's."""
    dimensions: Optional[int] = None

    @abstractmethod
    def upsert(self, namespace: str, chunks: List[Dict[str, Any]], vectors: List[List[float]]) -> None:
        """Write chunks idempotently by chunk id."""

    @abstractmethod
    def delete(self, namespace: str, doc_id: str) -> None:
        """Remove every chunk of doc_id."""

    @abstractmethod
    def search(self, namespace: str, query_text: str, query_vector: List[float], top_k: int) -> List[dict]:
        """Hybrid keyword + vector search."""


class BaseReranker(ABC):
    """Port (v1.6.1, design note MP-04, P17): reorders retrieved hits for a query (a
    cross-encoder, a managed rerank service). Bound as BaseRAGPipeline.reranker, it is the
    default _rerank; a pipeline that implements _rerank itself keeps doing so."""
    model_id: str = 'unspecified'

    @abstractmethod
    def rerank(self, query: str, hits: List[dict], top_n: int, config: Dict[str, Any] = None) -> List[dict]:
        """The top_n hits, best first, each with a `rerank_score`. Must not add hits."""


def check_dimensions(embedding_model: Optional['BaseEmbeddingModel'], vector_index: Optional['BaseVectorIndex']) -> None:
    """v1.6.1: an index built for one vector size cannot serve another model's vectors.
    Raises ValueError when both declare `dimensions` and they differ."""
    a = getattr(embedding_model, 'dimensions', None)
    b = getattr(vector_index, 'dimensions', None)
    if a is not None and b is not None and int(a) != int(b):
        raise ValueError(f'embedding model {getattr(embedding_model, "model_id", "?")!r} makes {a}-dimension '
                         f'vectors; the vector index expects {b}')


class BaseRAGPipeline(ABC, _PropagationMixin):
    """Read side. Public: retrieve() and (v1.2) evaluate_answer(). Optional v1.2 ports:
    `embedding_model` (BaseEmbeddingModel) and `vector_index` (BaseVectorIndex); with
    them, _embed_query and _search_index have working defaults."""

    embedding_model: Optional[BaseEmbeddingModel] = None
    vector_index: Optional[BaseVectorIndex] = None
    reranker: Optional[BaseReranker] = None          # v1.6.1
    namespace_prefix: str = 'rag'

    def retrieve(self, query: str, config: Dict[str, Any] = None,
                 tenant_id: str = None, ctx: Any = None, **kwargs) -> List[dict]:
        config = config or {}
        if ctx is not None:                      # v1.2: the tenant comes from the request context
            if tenant_id and tenant_id != ctx.tenant_id:
                raise ValueError('retrieve(): tenant_id differs from ctx.tenant_id')
            tenant_id = ctx.tenant_id
        vector = self._embed_query(query, config, **kwargs)
        # hybrid: the index gets BOTH the raw text (keyword/BM25 leg)
        # and the vector (semantic leg) and fuses them itself.
        results = self._search_index(query_text=query, query_vector=vector,
                                     config=config, tenant_id=tenant_id, **kwargs)
        # v1.5.1: the reranker gets the query (kwargs['query']), so it can score pairs
        reranked = self._rerank(results, config, query=query, **kwargs)
        return reranked
 
    def evaluate_answer(self, answer: str, retrieved: Optional[List[dict]] = None,
                        config: Dict[str, Any] = None, **kwargs) -> float:
        """Public (v1.2): score how well an answer is grounded in what was retrieved.
        A capability of its own (agents, release gates and golden sets call it); it
        delegates to the protected _evaluate_answer()."""
        return float(self._evaluate_answer(answer, config or {}, retrieved=retrieved or [], **kwargs))

    def _search_index(self, query_text, query_vector, config=None, tenant_id=None, **kwargs):
        """Hybrid keyword + vector search. Namespace the search to
        f'{tenant_index_prefix}_{tenant_id}'. kwargs: top_k, hybrid_alpha.
        Default (v1.2): the shared vector_index port."""
        if self.vector_index is None:
            raise NotImplementedError('implement _search_index, or set vector_index')
        return self.vector_index.search(f'{self.namespace_prefix}_{tenant_id}', query_text, query_vector,
                                        int(kwargs.get('top_k', resolve_config('rag_top_k', config, kwargs))))

    def _rerank(self, results, config=None, **kwargs):
        """kwargs: rerank_top_n, query. Default (v1.6.1, was abstract): the bound `reranker`
        port, else the results in index order; both cut to rerank_top_n when it is set."""
        top_n = kwargs.get('rerank_top_n') or resolve_config('rerank_top_n', config, kwargs)
        results = list(results or [])
        if self.reranker is not None:
            ranked = self.reranker.rerank(kwargs.get('query') or '', results, int(top_n or len(results)), config)
            if len(ranked) > len(results):
                raise ValueError(f'{type(self.reranker).__name__}.rerank() returned hits it was not given')
            return ranked
        return results[:int(top_n)] if top_n else results
 
    @abstractmethod
    def _evaluate_answer(self, answer, config=None, **kwargs):
        """kwargs: groundedness_threshold"""
 
    def _embed_query(self, query, config=None, **kwargs):
        """Protected hook (v1.1.2): embed the query with the same model the indexing
        pipeline used for the documents. The default is the provider placeholder."""
        if self.embedding_model is not None:
            return self.embedding_model.embed_query(query)
        model = resolve_config('embedding_model', config, kwargs)
        return [0.0]  # provider call
 
# ==================================================
# BaseIndexingPipeline — write side of BaseRAGPipeline
# Fed by a change stream (CDC) or a bulk load; keeps the vector /
# keyword index consistent with the system of record.
# ==================================================
 
class BaseIndexingPipeline(ABC, _PropagationMixin):
    """Write side. Public: ingest(). Each document dict carries
    {'id', 'op': 'upsert'|'delete', 'text'/fields}. Per-document
    isolation: one failing document lands in failed_keys and never
    blocks the batch. The embedding model MUST match the one
    BaseRAGPipeline.retrieve() embeds queries with (same
    'embedding_model' key)."""

    embedding_model: Optional[BaseEmbeddingModel] = None      # v1.2 ports, as on BaseRAGPipeline
    vector_index: Optional[BaseVectorIndex] = None
    namespace_prefix: str = 'rag'
 
    def ingest(self, documents: List[Dict[str, Any]], config: Dict[str, Any] = None,
              correlation_id: str = None, io_config: Dict[str, Any] = None,
              idempotency_key: str = None, tenant_id: str = None,
              message_log: List[dict] = None, failed_keys: List[str] = None,
              ctx: Any = None, **kwargs) -> Dict[str, Any]:
        config = config or {}
        if ctx is not None:                      # v1.2: the tenant comes from the request context
            if tenant_id and tenant_id != ctx.tenant_id:
                raise ValueError('ingest(): tenant_id differs from ctx.tenant_id')
            tenant_id, correlation_id = ctx.tenant_id, correlation_id or ctx.correlation_id
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
 
    def _embed_documents(self, texts, config=None, **kwargs) -> List[List[float]]:
        """kwargs: embedding_model — must equal the retrieval-side model."""
        if self.embedding_model is None:
            raise NotImplementedError('implement _embed_documents, or set embedding_model')
        return self.embedding_model.embed_documents(texts)
 
    def _write_index(self, chunks, vectors, config=None, tenant_id=None, **kwargs):
        """Write to f'{tenant_index_prefix}_{tenant_id}'. Must be idempotent
        on chunk id so CDC at-least-once delivery is safe."""
        if self.vector_index is None:
            raise NotImplementedError('implement _write_index, or set vector_index')
        self.vector_index.upsert(f'{self.namespace_prefix}_{tenant_id}', chunks, vectors)
 
    def _delete_from_index(self, doc_id, config=None, tenant_id=None, **kwargs):
        """Remove every chunk of doc_id from the tenant namespace."""
        if self.vector_index is None:
            raise NotImplementedError('implement _delete_from_index, or set vector_index')
        self.vector_index.delete(f'{self.namespace_prefix}_{tenant_id}', doc_id)
 
    @abstractmethod
    def _handle_error(self, error, document, config=None, **kwargs):
        """Appends to failed_keys via kwargs['failed_keys'].append(...)"""


class RAGPipeline:
    """Optional façade (v1.2): one object with three governed paths for a service that
    both indexes and retrieves. Each public method delegates to its own pipeline, which
    keeps its protected and private steps; the two sides stay separate classes, so they
    can still be deployed and scaled apart.

        rag = RAGPipeline(PolicyIndexing(...), PolicyRetrieval(...))
        rag.ingest(documents, tenant_id='acme')
        results = rag.retrieve('payment terms?', tenant_id='acme')
        score = rag.evaluate_answer(answer, results)

    Both sides must use the same embedding model: a mismatch is refused at construction."""

    def __init__(self, indexing: BaseIndexingPipeline, retrieval: BaseRAGPipeline) -> None:
        a, b = indexing.embedding_model, retrieval.embedding_model
        check_dimensions(a, indexing.vector_index)              # v1.6.1
        check_dimensions(b, retrieval.vector_index)
        if a is not None and b is not None and a.model_id != b.model_id:
            raise ValueError(f'embedding models differ: indexing uses {a.model_id!r}, retrieval {b.model_id!r}')
        if (a is None) != (b is None):
            raise ValueError('set embedding_model on both sides or on neither')
        self.indexing, self.retrieval = indexing, retrieval

    def ingest(self, documents: List[Dict[str, Any]], config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return self.indexing.ingest(documents, config, **kwargs)

    def retrieve(self, query: str, config: Dict[str, Any] = None, **kwargs) -> List[dict]:
        return self.retrieval.retrieve(query, config, **kwargs)

    def evaluate_answer(self, answer: str, retrieved: Optional[List[dict]] = None,
                        config: Dict[str, Any] = None, **kwargs) -> float:
        return self.retrieval.evaluate_answer(answer, retrieved, config, **kwargs)

