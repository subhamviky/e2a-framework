# Design documents

Detailed HLD and LLD documents for the E2A profiles and the family as a whole. Each folder holds the document as Markdown, its diagrams (PNG images plus PlantUML and Graphviz sources) and a Word copy.

| Folder | Document | Read it for |
|---|---|---|
| [`core/`](core/E2A_FRAMEWORK_DESIGN.md) | E2A Framework — Agentic Core | Base classes, store ports, governance, observability, the inference gateway, the concurrency model |
| [`cqrs/`](cqrs/E2A_CQRS_DESIGN.md) | E2A Deterministic CQRS Profile | The command/query profile |
| [`aidlc/`](aidlc/E2A_AIDLC_DESIGN.md) | E2A AI-DLC Profile | `AIDLCPipelineOrchestrator`, admission, phases A–C, saga compensation |
| [`system/`](system/E2A_FAMILY_SYSTEM_DESIGN.md) | E2A Family System | How E2A, P0, G2C, A2C and AI-DLC fit together; claim namespaces; shared artifacts; open system-level gaps |

The P0, G2C and A2C documents are in the `a2c-framework` repository under `docs/design/`.

All four are aligned to E2A v1.2.0 (docs release v1.2.1); the core document's Section 15 summarises what v1.2 changed. The PlantUML diagrams share `common/style.puml`; the Graphviz diagrams carry their style inline. Render with `dot -Tpng -Gdpi=220 diagrams/<name>.dot -o images/<name>.png` and `java -jar plantuml.jar -tpng -o ../images diagrams/<name>.puml`.

The Python files in `reference/` (the `e2a` package) remain the reference for line-level detail.
