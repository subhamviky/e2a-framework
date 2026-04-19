# E2A Mapping Tables

## The 10-Layer Clean Architecture Mapping

> The runtime changes. The architecture does not.

| Architectural Layer | SAP RAP / ABAP | Spring Boot | FastAPI / Python | LangGraph Agent |
|---|---|---|---|---|
| **Data model** | CDS View Entity | `@Entity` JPA class | Pydantic `BaseModel` | `AgentState TypedDict` |
| **Interface contract** | Behavior Definition (BDEF) | Java `interface` | `Protocol` / ABC | `BaseAgent` abstract class |
| **Business logic** | Behavior Implementation (BIMP) | `@Service` class | Concrete service class | Agent class + LLM calls |
| **Callable operation** | RAP Action / BOPF Action | `@Transactional` method | Route handler function | `AgentTool` / MCP Tool |
| **Auto quality gate** | Determination | Spring `@Aspect` / AOP | Middleware / decorator | `CriticAgent` |
| **Exposure layer** | OData Service Binding | `@RestController` | `FastAPI APIRouter` | `/workflow` endpoint |
| **Async events** | BOPF Event Framework | Spring Kafka `@KafkaListener` | SQS worker / consumer | LangGraph conditional edge |
| **Observability** | CDS Analytical Views + ATC | Micrometer + Actuator | structlog + OTel + CloudWatch | `observability/` module |
| **Governance** | Authorization Objects + Validations | Spring Security + `@Validated` | `policies/` YAML + OPA | CriticAgent SLO gate + policy engine |
| **Deployment** | SAP CTS+ transport | Docker + Helm + kubectl | ECS Fargate + GitHub Actions | GitHub Actions → ECR → ECS deploy |

## Agent Architecture Detail

| Python Component | ABAP OOP Equivalent | RAP Equivalent | Role |
|---|---|---|---|
| `AgentState TypedDict` | Global DDIC Structure | CDS View Entity | Central data model passed across all components |
| `IF_AGENT` interface | ABAP Interface (IF_*) | Behavior Definition (BDEF) | Contract — declares operations all agents must implement |
| `BaseAgent` abstract class | Abstract Superclass | Abstract BIMP | Shared LLM setup, token tracking — not directly instantiated |
| `RouterAgent` | Inherited class — classifyIntent | Concrete BIMP — intent determination | Routes query to correct downstream agent |
| `KnowledgeAgent` | Inherited class — retrieveAndRespond | Concrete BIMP — RAG-based action | Grounded Q&A with mandatory citations |
| `OrderOpsAgent` | Inherited class — processOrder | Concrete BIMP — transactional action | Order creation, stock checks, idempotent writes |
| `FinanceAgent` | Inherited class — evaluateRisk | Concrete BIMP — approval/validation | Risk scoring, approval gates, escalation routing |
| `CriticAgent` | Validation class | Post-processing Determination | Auto-triggered groundedness scoring; SLO >= 0.85 enforced |
| `LangGraph StateGraph` | Workflow dispatcher | Determinations + Actions sequence | Conditional routing between agents on AgentState |
| `FastAPI APIRouter` | Service binding | Service Definition + Binding | Exposes agent workflow as OData-equivalent HTTP endpoint |

## RAG Pipeline Mapping

| Python Component | ABAP OOP | RAP Managed | Purpose |
|---|---|---|---|
| `rag/index_config.py` | Abstract Persistence Class | CDS Entity + DDL Source | Defines OpenSearch schema (KNN vector + BM25 text) |
| `rag/embeddings.py` | Utility class | Determination for derived fields | Converts text to semantic vectors |
| `rag/chunker.py` | Data transformation class | Behavior Determination for child entities | Splits documents into overlapping chunks with metadata |
| `rag/indexer.py` | Controller class | Action class — bulk ingestion | Read docs → chunk → embed → index to OpenSearch |
| `rag/retriever.py` | Search class — multi-strategy | Determination for ranked results | Hybrid: BM25 + KNN → Reciprocal Rank Fusion |
| `rag/reranker.py` | Validation class | Determination for precision refinement | Cross-encoder reranker improves top-k precision |
| `rag/evaluator.py` | Test class | ATC check / Unit test | RAGAS: faithfulness, relevancy, precision, recall |

## Tool Call Architecture Mapping

| Python Component | ABAP OOP | RAP Managed | Purpose |
|---|---|---|---|
| `tools/base_tool.py` | Abstract Class | Abstract BIMP | Idempotency (DynamoDB lock), structured logging, metrics |
| `tools/create_order.py` | Inherited class — write | Action: `createOrder()` | Order creation with approval threshold, DynamoDB persistence |
| `tools/check_stock.py` | Inherited class — read | Determination: `checkStock()` | Inventory read — deterministic, no side effects |
| `tools/risk_score.py` | Utility class — evaluation | Determination: `evaluateRisk()` | Risk scoring — returns recommendation, no write |
| `tools/open_case.py` | Inherited class — write | Action: `openCase()` | Case creation with SLA assignment and idempotency |
| FastAPI tool router | Facade class | Service Definition + Binding | Aggregates all tool endpoints under one API surface |

## Cloud Platform Portability

| E2A Component | AWS | GCP | Azure |
|---|---|---|---|
| Container hosting | ECS Fargate | Cloud Run | Azure Container Apps |
| Async messaging | SQS FIFO + DLQ | Cloud Pub/Sub + Dead Letter | Service Bus + DLQ |
| Vector store | OpenSearch (KNN) | Vertex AI Vector Search | Azure AI Search |
| LLM inference | Amazon Bedrock | Vertex AI (Gemini) | Azure OpenAI Service |
| Secret management | Secrets Manager | Secret Manager | Key Vault |
| Observability | CloudWatch + X-Ray | Cloud Monitoring + Cloud Trace | Azure Monitor + App Insights |
| IaC | Terraform (AWS provider) | Terraform (GCP provider) | Terraform (Azure provider) |

---

→ Back to [README](../README.md)
