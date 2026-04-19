
# The Calibration Principle

> Financial integrity controls belong at the business logic layer
> when domain complexity justifies it — not by default.

## The Three Questions

Ask these three questions before deciding where integrity controls live:

### 1. Input Mutability
**Can amounts, rates, or quantities change legitimately after the process starts?**

- **YES** → The idempotency key must be a business identity, not a technical hash.
  The system needs to distinguish "revised amount for the same charge" from
  "duplicate charge." Build controls at the business logic layer.
- **NO** → A standard API-layer idempotency key header is sufficient.
  Infrastructure handles it.

### 2. Output Ambiguity
**Can two records with the same charge type and amount carry different business meaning?**

- **YES** → Reconciliation must live in business logic. The system cannot
  deduplicate by value alone — it must understand context (calculation level,
  resolution level, agreement line).
- **NO** → Standard deduplication by payload hash or idempotency key is sufficient.

### 3. Audit Exposure
**What is the cost of a wrong posting — financial, regulatory, or reputational?**

- **HIGH** (vendor payments, regulatory reporting, audit trails) → Business-layer
  controls, immutable ledger, first-class dispute workflow.
- **LOW** (duplicate log entry, retry triggers a duplicate email) → Let
  infrastructure handle it. Do not build a saga.

## Complexity Scoring Table

| Payment Scenario | Complexity | Appropriate Layer | Pattern Applied |
|---|---|---|---|
| SAP TM Charge Management — rate tables, calculation levels, revised carrier invoices, dispute management | VERY HIGH | Business logic | `REF_ELEM_KEY` deterministic mapping, Completely Invoiced gate, first-class Dispute workflow |
| Distributed financial settlement — multi-service saga, cross-ledger posting, regulatory reporting | HIGH | Business logic | Saga + compensation, `@Idempotent` AOP, double-entry DB invariant, RAG audit |
| E-commerce checkout — fixed price, single charge type | MEDIUM | API layer | Idempotency key header, payment gateway dedup, standard retry |
| SaaS subscription billing — fixed monthly, same account | LOW | Infrastructure layer | SQS message deduplication ID, exactly-once delivery setting |
| P2P transfer — known sender, receiver, fixed amount | LOW | Infrastructure layer | API idempotency key on payment API call; DLQ for failed sends |

## The SAP TM Origin

SAP TM Charge Management scores HIGH on all three factors:

| SAP TM Mechanism | What It Enforces | Cloud-Native Equivalent |
|---|---|---|
| `REF_ELEM_KEY` — business identity key | Deterministic 1:1 charge-to-settlement mapping — revised amounts route as valid updates, never duplicates | Client-supplied `X-Idempotency-Key` + Redis `SETNX` `@Idempotent` AOP |
| "Completely Invoiced" gate | Ledger posting blocked until business confirms invoice is final — immutable by contract, not by code | `SettlementState.COMPLETED` as sole pre-condition for ledger write |
| Dispute Management — first-class workflow | Charge delta mediation as a business process — unblocks final posting without bypassing integrity | RAG-powered CriticAgent / Audit-AI reasoning against policy documents |
| FI Ledger — write-once source of truth | Finance Ledger is immutable | `UNIQUE INDEX` on `(settlement_id, direction, entry_type)` + reversal-only corrections |

## The Staff-Level Insight

> Over-engineering financial controls is as much a design failure
> as under-engineering them. The skill is matching control weight
> to domain complexity — not applying enterprise-grade safeguards
> to every payment flow because they feel safer.

---

→ Back to [README](../README.md)
