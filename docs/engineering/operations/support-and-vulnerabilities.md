# Support and vulnerability handling

- **Support:** one maintainer; questions and defects through GitHub issues. Consumers pin a tag, so a fix is a new tag they adopt deliberately; the previous tag is never changed.
- **Vulnerabilities:** report privately through GitHub security advisories on the repository, not in a public issue. Expect an answer within a week. A fix is released as a patch version with a CHANGELOG entry that says what was affected.
- **Severity of a defect** follows the platform scale ([incident response](https://github.com/subhamviky/enterprise-finance-genai-assistant/blob/main/docs/engineering/operations/incident-response.md)): a defect that lets a model-driven action bypass a stage, or a claim complete without its effects, is SEV-1/2 for consumers and gets a patch release and a review (see the order-to-cash review of the `BaseException` claim defect).
- **Compatibility promise:** additive changes in minor versions; a breaking change is announced in an earlier release and removed in a later one (the v1.1 hooks were deprecated, then removed in v1.3.0).
