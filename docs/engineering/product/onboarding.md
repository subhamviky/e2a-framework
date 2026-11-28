# Onboarding

Read: `README.md`, `docs/E2A_STRATEGIC_OVERVIEW.md`, `docs/design/core` (the framework design), then the [ADRs](../adr/README.md).

Run locally:

```bash
pip install ".[test]" && python -m pytest -q tests
python -m e2a.conformance --strict --quiet reference/e2a $(ls reference/*.py | grep -v "smoke_test\|e2a_base.py")
```

Map: `reference/e2a/` (the package), `adapters/e2a_adapters/` (clouds), `java/` (libraries), `tests/`, `docs/`. Working agreements: one concern per branch; CI green; a test for every defect; an ADR for decisions; additive changes by default; every cloud-facing change is verified in a real run and recorded in the CHANGELOG.
