# ADR 0007: Identity is read at ingress per cloud, with grants by configuration

- **Status:** Accepted
- **Date:** v1.7.4, v1.11.0
- **Deciders:** repository owner

## Context

A caller's tenant and roles must come from a verified token, and a Google ID token names only an email.

## Options considered

A) Trust headers. B) Ingress sources per cloud (v1.7.4); on Cloud Run `gcp_cloud_run` reads the ID token IAM verified, and `identity_subject_roles` grants tenants and roles by configuration; outbound `gcp_id_token` credentials.

## Decision

B. Two issuer spellings (`accounts.google.com` and `https://accounts.google.com`) are handled with `identity_trusted_issuers`; the token must carry an e-mail (`format=full`).

## Consequences

Fail-closed identity with no secret; the grants are deployment configuration.
