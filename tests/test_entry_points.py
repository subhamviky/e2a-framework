"""v1.2 step 4: entry points (design note 01, principle E-15). Several public methods
are allowed when each is a real capability: RAG ingest / retrieve / evaluate_answer
(with shared embedding and index ports and an optional façade), MCP serve /
serve_async, and the tool endpoint as a protected hook."""
import asyncio
import math
import warnings

import pytest

from e2a import (BaseEmbeddingModel, BaseIndexingPipeline, BaseMCPServer, BaseRAGPipeline, BaseToolService,
                 BaseVectorIndex, MCPToolService, RAGPipeline, RequestContext, RestToolService)


# ----------------------------------------------------------------------- RAG ports
class BagOfWords(BaseEmbeddingModel):
    def __init__(self, model_id="bow-v1"):
        self.model_id = model_id

    def _vec(self, text):
        words = text.lower().split()
        return [float(words.count(w)) for w in ("payment", "terms", "refund", "days")]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class MemoryIndex(BaseVectorIndex):
    def __init__(self):
        self.spaces = {}

    def upsert(self, namespace, chunks, vectors):
        space = self.spaces.setdefault(namespace, {})
        for chunk, vector in zip(chunks, vectors):
            space[chunk["id"]] = (chunk, vector)

    def delete(self, namespace, doc_id):
        space = self.spaces.get(namespace, {})
        for key in [k for k, (c, _) in space.items() if c["doc_id"] == doc_id]:
            del space[key]

    def search(self, namespace, query_text, query_vector, top_k):
        def cos(a, b):
            n = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
            return sum(x * y for x, y in zip(a, b)) / n if n else 0.0
        hits = [dict(c, score=cos(v, query_vector)) for c, v in self.spaces.get(namespace, {}).values()]
        return sorted(hits, key=lambda h: -h["score"])[:top_k]


class Indexing(BaseIndexingPipeline):
    def __init__(self, model, index):
        self.embedding_model, self.vector_index = model, index

    def _chunk_documents(self, document, config=None, **kw):
        return [{"id": f"{document['id']}#0", "doc_id": document["id"], "text": document["text"]}]

    def _handle_error(self, error, document, config=None, **kw):
        pass


class Retrieval(BaseRAGPipeline):
    def __init__(self, model, index):
        self.embedding_model, self.vector_index = model, index

    def _rerank(self, results, config=None, **kw):
        return results

    def _evaluate_answer(self, answer, config=None, **kw):
        sources = " ".join(r["text"] for r in kw["retrieved"]).lower()
        words = answer.lower().split()
        return sum(w in sources for w in words) / len(words)


def rag(model_a=None, model_b=None):
    index = MemoryIndex()
    return RAGPipeline(Indexing(model_a or BagOfWords(), index), Retrieval(model_b or BagOfWords(), index)), index


def test_the_facade_indexes_and_retrieves_through_the_shared_ports():
    pipeline, index = rag()
    report = pipeline.ingest([{"id": "d1", "text": "payment terms are 30 days"},
                              {"id": "d2", "text": "refund within 14 days"}], tenant_id="acme")
    assert report["indexed"] == 2 and report["failed_keys"] == []
    assert set(index.spaces) == {"rag_acme"}                      # one namespace per tenant
    hits = pipeline.retrieve("payment terms", tenant_id="acme", top_k=1)
    assert [h["doc_id"] for h in hits] == ["d1"]
    assert pipeline.retrieve("payment terms", tenant_id="other") == []   # tenants never mix
    pipeline.ingest([{"id": "d1", "op": "delete"}], tenant_id="acme")
    assert [h["doc_id"] for h in pipeline.retrieve("payment terms", tenant_id="acme")] == ["d2"]


def test_a_mismatched_embedding_model_is_refused_at_construction():
    with pytest.raises(ValueError, match="embedding models differ"):
        rag(BagOfWords("bow-v1"), BagOfWords("bow-v2"))
    index = MemoryIndex()
    with pytest.raises(ValueError, match="both sides or on neither"):
        RAGPipeline(Indexing(BagOfWords(), index), Retrieval(None, index))


def test_evaluate_answer_is_a_public_capability_that_delegates():
    pipeline, _ = rag()
    pipeline.ingest([{"id": "d1", "text": "payment terms are 30 days"}], tenant_id="acme")
    hits = pipeline.retrieve("payment terms", tenant_id="acme")
    assert pipeline.evaluate_answer("payment terms 30 days", hits) == 1.0
    assert pipeline.evaluate_answer("refund now", hits) == 0.0
    assert pipeline.retrieval.evaluate_answer("payment", hits) == 1.0        # the same on the class


def test_retrieve_and_ingest_take_the_tenant_from_the_context():
    pipeline, index = rag()
    ctx = RequestContext.begin(tenant_id="acme", correlation_id="c-1")
    assert pipeline.ingest([{"id": "d1", "text": "payment terms"}], ctx=ctx)["correlation_id"] == "c-1"
    assert pipeline.retrieve("payment", ctx=ctx)[0]["doc_id"] == "d1"
    with pytest.raises(ValueError, match="differs from ctx.tenant_id"):
        pipeline.retrieve("payment", tenant_id="other", ctx=ctx)


def test_a_v1_1_pipeline_without_ports_still_needs_its_own_hooks():
    class Old(BaseRAGPipeline):
        def _rerank(self, results, config=None, **kw): return results
        def _evaluate_answer(self, answer, config=None, **kw): return 0.5

    with pytest.raises(NotImplementedError, match="set vector_index"):
        Old().retrieve("q", tenant_id="t1")


# ----------------------------------------------------------------------- tools and MCP
class EchoTool(BaseToolService):
    def _get_endpoint(self, config=None, tenant_id=None, **kw):
        return "local://echo"

    def _build_payload(self, payload, config=None, **kw):
        return payload

    def _validate_input(self, payload, config=None, **kw):
        return True

    async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kw):
        return {"physical_success": True, "echo": body.get("x"), "endpoint": endpoint}


class Server(BaseMCPServer):
    def __init__(self):
        super().__init__()
        self.wrap_api_as_tool(EchoTool(), "echo", "echo x", {"type": "object"})


CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"x": 7}}}


def test_serve_async_gives_the_same_answer_as_serve_inside_a_running_loop():
    blocking = Server().serve(dict(CALL), {}, tenant_id="t1")

    async def host():                     # an ASGI host: a loop is already running
        return await Server().serve_async(dict(CALL), {}, tenant_id="t1")

    assert asyncio.run(host()) == blocking
    assert "'echo': 7" in blocking["result"]["content"][0]["text"]


def test_the_endpoint_is_a_protected_hook_and_the_public_name_is_an_alias():
    tool = EchoTool()
    result = asyncio.run(tool.execute({"x": 1}, {}, tenant_id="t1"))
    assert result["endpoint"] == "local://echo"
    assert tool.get_endpoint({}, tenant_id="t1") == "local://echo"           # deprecated alias
    assert RestToolService._get_endpoint is not BaseToolService._get_endpoint
    assert MCPToolService()._get_endpoint({"mcp_server_url": "http://mcp"}) == "http://mcp"


def test_a_v1_1_get_endpoint_override_still_works_and_is_told_once_to_migrate():
    class Legacy(RestToolService):                 # as O2C's HttpJsonTool: inherits a _get_endpoint
        def _build_payload(self, payload, config=None, **kw): return payload

        async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kw):
            return {"physical_success": True, "endpoint": endpoint}

        def get_endpoint(self, config=None, tenant_id=None, **kw):
            return f"local://legacy/{tenant_id}"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = asyncio.run(Legacy().execute({"x": 1}, {}, tenant_id="t1"))
        asyncio.run(Legacy().execute({"x": 1}, {}, tenant_id="t1"))
    assert first["endpoint"] == "local://legacy/t1"
    assert sum("v1.1 keyword signature" in str(w.message) for w in caught) == 1


def test_a_tool_without_an_endpoint_says_what_to_implement():
    class NoEndpoint(EchoTool):
        _get_endpoint = BaseToolService._get_endpoint

    with pytest.raises(NotImplementedError, match="implement _get_endpoint"):
        NoEndpoint()._get_endpoint({})
