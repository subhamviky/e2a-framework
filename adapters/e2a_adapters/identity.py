"""Claim maps of the cloud identity providers (v1.7.0, design note MP-05, N3), for E2A's
ClaimMapIdentityResolver. The cloud profiles set `identity_claim_map` to the preset of their
provider; these are the same maps, for code that configures a resolver directly.

Verification (signature, expiry, audience) stays in the gateway in front of the service; E2A
resolves the verified claims. The tenant claim is the deployment's choice, so each preset names
the convention these projects use and can be overridden key by key."""
from typing import Dict

PRESETS: Dict[str, Dict[str, str]] = {
    # Amazon Cognito user pools: a custom attribute for the tenant, groups as roles
    'cognito': {'tenant_id': 'custom:tenant_id', 'subject': 'sub', 'roles': 'cognito:groups', 'issuer': 'iss'},
    # Microsoft Entra ID: the directory (tid) is the tenant, mapped with identity_tenant_map; app roles
    'entra_id': {'tenant_id': 'tid', 'subject': 'oid', 'roles': 'roles', 'issuer': 'iss'},
    # Google Identity Platform with multi-tenancy: firebase.tenant; roles from a custom claim
    'identity_platform': {'tenant_id': 'firebase.tenant', 'subject': 'sub', 'roles': 'roles', 'issuer': 'iss'},
    # v1.8.0 (SI-01): another SERVICE calling with an Entra access token. The subject is the calling
    # app (azp, or appid in v1 tokens); app roles carry actions and granted tenants ("tenant:<id>"),
    # so use it with identity_tenants_from_roles = "tenant:". No tenant claim is read.
    'entra_service': {'tenant_id': 'tid', 'subject': 'azp', 'roles': 'roles', 'issuer': 'iss'},
    # v1.11.0 (GCP-01): a Google ID token, verified by Cloud Run IAM (ingress source gcp_cloud_run),
    # from a service account or a user. The subject is the email; the token carries no tenant and no
    # roles: grant them with identity_subject_roles and use identity_tenants_from_roles = "tenant:".
    'google_id_token': {'tenant_id': 'tenant_id', 'subject': 'email', 'roles': 'roles', 'issuer': 'iss'},
}


def preset(name: str, **overrides: str) -> Dict[str, str]:
    """A provider's claim map, with any claim renamed: preset('cognito', tenant_id='custom:org')."""
    if name not in PRESETS:
        raise ValueError(f'unknown identity preset {name!r}: one of {", ".join(sorted(PRESETS))}')
    return {**PRESETS[name], **overrides}
