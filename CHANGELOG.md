# Changelog

All notable changes to the E2A framework documents and reference code.

## Concurrency and idempotency

### Added
- **Concurrency model across the framework docs:** ownership by invariant, effectively-once effects under at-least-once delivery, atomic claims, leases and fencing, deadlines, compare-and-set (CAS) artifact ownership, and gateway-only model calls.
- **Claim statuses** `CLAIMED`, `REPLAY`, `IN_PROGRESS`, `CONFLICT`, `RECOVERY_REQUIRED`, `FAILED_FINAL`, and settlement as retryable, final or `UNKNOWN`.
- **Claim namespaces:** `g2c:<generator_key>`, `p0:<scaffold_key>`, `a2c:<project_name>:<project_type>:<target_cloud>`, `aidlc:<build_key>`.
- **Implementation Playbook, Section 2.19:** concurrency for the Agentic profile.
- **Cloud Landing Zone** and **MultiCloud Reference:** concurrency, admission and capacity sections, with a new capacity and admission figure.
- **AI-DLC pipeline** (`reference/ai_dlc_pipeline.py`):
  - `build_key` is the pipeline identity (`project_name:runtime:build_tool:platform`); `generator_key` remains as a deprecated alias and is not G2C's `generator_key`.
  - New `phase_reached` values: `REJECTED_INVALID`, `SKIPPED_IN_PROGRESS`, `REJECTED_CONFLICT`, `REJECTED_OVERLOAD`, `RECOVERY_REQUIRED`, `FAILED_FINAL`.
  - The build lease is max(`idempotency_lease_seconds`, remaining deadline + 60 s).
  - `deadline` and tenant propagate to G2C.
  - A validation tier (`a2c_validation_service`, default `DevPlatformValidationService`) runs before `SDLCWorkflow`, which receives a deep copy of the `dev_request`.
  - `a2c_sdlc.enabled` without an inherited generator is `REJECTED_INVALID`; without a `dev_request` it is `FAILED_A2C`.
  - `_ArtifactRegistryAdapter` presents `artifact_store` as G2C's registry (`artifact:<path>` → `{refs, created_by, deleted}`).
- `reference/smoke_test_aidlc_concurrency.py`: 26 checks using in-memory doubles. `smoke_test_aidlc_integration.py` (a2c-framework, 15 checks) runs AI-DLC over the real G2C, P0 and A2C.
- **Design documents** under `docs/design/` in both repositories (E2A core, CQRS, AI-DLC and family system in e2a-framework; A2C, G2C and P0 in a2c-framework), each with diagrams and a Word copy.

### Changed
- **Saga compensation:** never deletes outputs vouched for by a settled claim. Deleting an orphaned abstract class is opt-in (`config['compensation_delete_orphan_abstract']`) and only the creator may do it, after the last reference is released. A `G2C_P0_DONE` (Phase C) failure keeps the outputs and releases the reference.
- **Validation** moved from a per-agent `_validate_state` to `BaseValidationService.validate()` with per-agent `_validate_<agent_name>` validators and an ABAC entitlement gate.
- **A2C, G2C and P0 Framework References:** aligned with the reference implementation (`a2c_base.py`, `base_generator_agent.py`, `inherited_generator.py`), including the code-aligned `CodeCriticAgent` (score = min of model score and deterministic NFR-pattern score), G2C template mode and claim namespaces.
- **AI-DLC Implementation Playbook and Landing Zone:** `SDLCWorkflow` is now described as implemented (`execute` inherited from `BaseWorkflow`); embedded code regenerated from the repository file; saga compensation figure redrawn for the rules above.

### Notes
- With `compensation_delete_orphan_abstract` set, deleting a shared base makes later replays return `recovery_required` until the claim record is reconciled; it is off by default.
- All tests use in-memory doubles and scripted model responses. Nothing here has been validated against live services, and template-mode tests prove the sequence, not model quality.
- `docs/images/aidlc_capacity_admission.png` is referenced by `docs/AIDLC_LANDING_ZONE.md` but missing from the repository.
