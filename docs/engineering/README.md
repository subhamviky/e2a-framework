# Engineering records: e2a-framework

The Enterprise-to-Agentic Architecture framework: the `e2a` package, `e2a-adapters` (AWS, Google Cloud, Azure) and the `e2a-java` libraries.

| Area | Document |
|---|---|
| Decisions | [ADRs](adr/README.md) |
| Security | [Threat model](security/threat-model.md), [data handling](security/data-handling.md) |
| Delivery | [Release process](operations/release-process.md), [`CHANGELOG.md`](../../CHANGELOG.md) |
| Support | [Support and vulnerability handling](operations/support-and-vulnerabilities.md) |
| Quality | [Test strategy](testing/test-strategy.md) |
| Product | [Requirements and user stories](product/requirements-and-user-stories.md), [solution overview](product/solution-overview.md), [onboarding](product/onboarding.md) |

**Not applicable to a library, and why.** SLOs and an error budget, incident response, disaster recovery, cost and capacity, teardown and an HTTP API reference describe a *running service*. e2a-framework is code that other projects install and pin to a tag: it has no deployment, no traffic and no data of its own. Their service-level equivalents are in the [assistant repository](https://github.com/subhamviky/enterprise-finance-genai-assistant/blob/main/docs/engineering/README.md) and the other service repositories. The library's public interface is documented in its design documents and its code (listed in the onboarding page).

Related: the projects built on it: [settlement](https://github.com/subhamviky/financial-settlement-platform), [reconciliation](https://github.com/subhamviky/payment-reconciliation-engine), [order-to-cash](https://github.com/subhamviky/order-to-cash-agentic-ai), [the assistant](https://github.com/subhamviky/enterprise-finance-genai-assistant).
