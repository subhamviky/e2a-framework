# Enterprise-to-Agentic (E2A) Architecture Framework

> *"The patterns that make $350M financial settlement systems reliable
> are the same patterns that make Agentic AI production-ready."*

---

## What Is E2A?

The E2A Framework is a formal methodology for translating enterprise
distributed systems patterns into production-grade agentic AI systems.
It is a **layered abstract class hierarchy** — not a library — that
enforces NFRs, governance, and observability as structural contracts.

**Agent = Microservice · RAG = CQRS Knowledge Layer ·
MCP Tool = Idempotency-Aware API · Orchestration = Saga-Compensating Control Plane**

---

## The SAP RAP → Agentic AI Equivalence

![E2A Mental Model](docs/sap-to-agentic-mental-model.svg)

| SAP RAP Layer | E2A / Agentic AI Layer |
|---|---|
| CDS View Entity | `AgentState` TypedDict |
| Behavior Definition (BDEF) | `BaseAgent` abstract class |
| Behavior Implementation (BIMP) | Agent class (LLM + tools) |
| RAP Action / BOPF Action | MCP Tool (DynamoDB idempotency) |
| Determination (auto quality gate) | `CriticAgent` (RAGAS faithfulness) |
| OData Service Binding | FastAPI `APIRouter` |

---

## Framework Architecture — 8 Abstract Classes

### Primary Classes (shared implementations enforce NFRs)

| Class | Public Method | What the base class enforces |
|---|---|---|
| `BaseAgent` | `run()` | Idempotency, token budget, governance, latency SLO, observability |
| `BaseWorkflow` | `execute()` | Governance approval, intent routing, agent dispatch |
| `BaseRAGPipeline` | `retrieve()` | Retries, latency SLO, faithfulness gate, observability |
| `BaseToolService` | `execute()` | Governance, idempotency, auth, async HTTP, latency SLO |

### Foundation Classes (Interface pattern — environment-specific)

`BaseInfraProvisioner` · `BaseObservability` · `BasePipeline` · `BaseGovernanceFramework`

> Foundation classes are **Interfaces** (Python `Protocol` or pure ABC).
> No shared implementations — cloud provider, observability vendor,
> and CI/CD platform differ per deployment. The interface enforces
> the contract; the subclass owns the complete implementation.

---

## Cloud Portability

The same subclass runs on any cloud by changing one config value:

```python
# AWS Bedrock — Claude 3.5 Sonnet
agent.run(state, {'model_id': 'anthropic.claude-3-5-sonnet-20241022-v2:0'})

# GCP Vertex AI — Gemini 2.5 Pro
agent.run(state, {'model_id': 'gemini-2.5-pro'})

# Azure AI Foundry — GPT-4o
agent.run(state, {'model_id': 'gpt-4o', 'provider': 'azure'})

# Meta Llama 4 — Bedrock or standalone
agent.run(state, {'model_id': 'meta.llama4-maverick-17b-instruct-v1:0'})
```

---

## Documentation

### 📘 [Master Abstraction Reference](docs/E2A_Master_Abstraction_Reference.pdf)
The complete class contract specification. Covers:
- All 8 abstract classes with PUBLIC / PROTECTED / PRIVATE / INTERFACE / CLASS VAR access modifiers
- Full method signatures with input/output parameter tables
- NFR enforcement matrix (13 NFRs × BaseAgent / BaseRAG / BaseTool)
- Universal applicability (Python · Java Spring Boot · TypeScript · Go)
- SAP RAP to Agentic AI structural equivalence

### 📗 [Implementation Playbook](docs/E2A_Implementation_Playbook.pdf)
The hands-on guide for using the framework. Covers:
- Complete `e2a_base.py` scaffold file — drop into `src/`, never modify
- Config / environment variable resolution chain (`kwargs > config > env > default`)
- Repo directory structure recommendation
- `RefundAgent` inheritance example with every protected method shown
- Three invocation patterns: direct call, LangGraph node, FastAPI endpoint
- Multi-class end-to-end example (agent + RAG + tool in one workflow)

### 📙 [Multi-Cloud AI Ecosystem Reference](docs/E2A_MultiCloud_Reference.pdf)
Cloud provider mapping and decision framework. Covers:
- AWS, GCP, Azure, and Meta Llama: 6-pillar breakdown per vendor
- 11-step `BaseAgent.run()` → managed service mapping for all three major clouds
- Corrected E2A-compliant code examples for all four vendors
- Cross-cloud comparison: orchestration frameworks, anti-lock-in strategy
- Decision matrix: 10 business scenarios × 4 providers
- Domain-specific recommendations (Finance, SAP/ERP, Healthcare, Retail, Manufacturing)

---

## Quick Start

```bash
# 1. Copy scaffold into your repo
cp e2a_base.py src/

# 2. Create your agent — override only what you need
```

```python
from src.e2a_base import BaseAgent, AgentState

class OrderOpsAgent(BaseAgent):
    agent_name = 'OrderOpsAgent'   # governance key

    def _build_messages(self, state, config=None, **kwargs) -> list:
        return [{'role': 'user', 'content': [{'text': state['query']}]}]

    def _validate_state(self, state, config=None, **kwargs) -> bool:
        return 'query' in state and 'intent' in state

    def _evaluate_output(self, response, state, config=None, **kwargs) -> float:
        return 0.95  # or run RAGAS

    # run() is NOT overridden — inherited from BaseAgent
    # All NFR enforcement (idempotency, latency SLO, token budget) inherited
```

```python
# 3. Invoke
agent = OrderOpsAgent(config={
    'model_id': 'anthropic.claude-3-5-sonnet-20241022-v2:0',
    'approved_agents': ['OrderOpsAgent'],
    'min_confidence': 0.85,
    'max_latency': 2.0
}, state=state)

result = await agent.run(state, agent.config)
```

---

## Reference Implementation

The **Order-to-Cash Agentic AI Platform** is the primary E2A reference implementation:
[github.com/subhamviky/order-to-cash-agentic-ai](https://github.com/subhamviky/order-to-cash-agentic-ai)

| E2A Class | O2C Implementation |
|---|---|
| `BaseAgent` | `RouterAgent`, `KnowledgeAgent`, `OrderOpsAgent`, `FinanceAgent`, `CriticAgent` |
| `BaseRAGPipeline` | `ConcreteRAGPipeline` (OpenSearch KNN + BM25 hybrid) |
| `BaseToolService` | `CreateOrderTool`, `CheckStockTool`, `RiskScoreTool`, `OpenCaseTool` |
| `BaseWorkflow` | `O2CWorkflow` (LangGraph StateGraph) |

---

## Author

**Subham Gupta**
[github.com/subhamviky](https://github.com/subhamviky) · [linkedin.com/in/subhamgupta-0a05a058](https://linkedin.com/in/subhamgupta-0a05a058)
