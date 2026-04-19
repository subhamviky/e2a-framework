Architectural Layer SAP RAP / ABAP Spring Boot FastAPI / Python LangGraph
Agent
Data model CDS View Entity @Entity JPA class Pydantic
BaseModel
AgentState
TypedDict
Interface contract Behavior Definition
(BDEF)
Java interface Protocol / ABC BaseAgent
abstract class
Business logic Behavior
Implementation (BIMP)
@Service class Concrete service
class
Agent class +
LLM calls
Callable operation RAP Action / BOPF
Action
@Transactional
method
Route handler
function
AgentTool / MCP
Tool
Auto quality gate Determination Spring @Aspect /
AOP
Middleware /
decorator
CriticAgent
Exposure layer OData Service Binding @RestController FastAPI APIRouter FastAPI /workflow
endpoint
Async events BOPF Event Framework Spring Kafka
@KafkaListener
SQS worker /
consumer
LangGraph
conditional edge
Observability CDS Analytical Views +
ATC
Micrometer +
Actuator
structlog + OTel +
CloudWatch
observability/
(tracer, metrics,
logger)
Governance Authorization Objects +
Validations
Spring Security +
@Validated
policies/ YAML +
OPA
CriticAgent SLO
gate + policy
engine
Deployment SAP CTS+ transport Docker + Helm +
kubectl
ECS Fargate +
GitHub Actions
GitHub Actions ->
ECR -> ECS
deploy
