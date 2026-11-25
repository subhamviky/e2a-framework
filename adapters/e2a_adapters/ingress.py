"""Verified claims at ingress, as each cloud's platform hands them to the service (v1.7.4,
design note MP-05, N3).

E2A's ClaimMapIdentityResolver resolves claims a gateway VERIFIED; it never verifies a token
itself. Each cloud delivers the verified claims differently, and this module reads them:

  aws_http_api          API Gateway HTTP API with a JWT authorizer: the Lambda event's
                        requestContext.authorizer.jwt.claims (payload format 2.0)
  gcp_api_gateway       Google Cloud API Gateway: the X-Apigateway-Api-Userinfo header, the
                        base64url-encoded JWT payload
  azure_container_apps  Container Apps (and App Service) authentication: the X-MS-CLIENT-PRINCIPAL
                        header, base64 JSON {"claims": [{"typ", "val"}], "role_typ", ...}
  gcp_cloud_run         v1.11.0 (GCP-01): Cloud Run IAM. The caller's Google ID token, which Cloud
                        Run verified (signature, expiry, audience = the service URL) before the
                        request reached the container: X-Serverless-Authorization when the caller
                        sent it, else Authorization: Bearer. Its payload is read, not re-verified.

verified_claims(source, event=..., headers=...) returns (claims, verified). `verified` is True
only when the platform's own carrier is present. That is trustworthy only where the platform
guarantees the carrier:
  * AWS: the event is built by API Gateway after the authorizer accepted the token;
  * Azure: "External requests aren't allowed to set these headers" (Container Apps docs);
  * Google Cloud: API Gateway sets the header, so the backend must accept calls ONLY from the
    gateway (Cloud Run IAM: the gateway's service account is the sole invoker). Otherwise a
    caller could reach the backend directly and send the header itself.
  * Cloud Run IAM (gcp_cloud_run): only while the service requires authentication, i.e. no
    allUsers / allAuthenticatedUsers invoker. A public service passes unverified tokens through.
    A Google ID token names its subject only (email, sub); grant roles and tenants to subjects
    with identity_subject_roles, and use the preset google_id_token.
"""
import base64
import json
from typing import Any, Dict, Mapping, Optional, Tuple

__all__ = ['SOURCES', 'verified_claims', 'identity_at_ingress', 'claims_from_aws_http_api', 'claims_from_gcp_api_gateway',
           'claims_from_azure_container_apps', 'claims_from_gcp_cloud_run']

Claims = Dict[str, Any]

GCP_USERINFO_HEADER = 'x-apigateway-api-userinfo'
SERVERLESS_AUTHORIZATION_HEADER = 'x-serverless-authorization'
AZURE_PRINCIPAL_HEADER = 'x-ms-client-principal'

# Container Apps / App Service map claim names before injecting them (long URI forms). The
# short names the presets use are added beside them, so preset('entra_id') reads either.
_AZURE_ALIASES = {
    'http://schemas.microsoft.com/identity/claims/tenantid': 'tid',
    'http://schemas.microsoft.com/identity/claims/objectidentifier': 'oid',
    'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier': 'sub',
    'http://schemas.microsoft.com/ws/2008/06/identity/claims/role': 'roles',
}


def _header(headers: Optional[Mapping[str, str]], name: str) -> Optional[str]:
    """A header by name, whatever its case (frameworks present them differently)."""
    for key, value in (headers or {}).items():
        if str(key).lower() == name:
            return value
    return None


def _b64json(text: str, urlsafe: bool) -> Dict[str, Any]:
    padded = text.strip() + '=' * (-len(text.strip()) % 4)
    raw = base64.urlsafe_b64decode(padded) if urlsafe else base64.b64decode(padded)
    decoded = json.loads(raw.decode('utf-8'))
    if not isinstance(decoded, dict):
        raise ValueError('the identity header does not hold a JSON object')
    return decoded


def _listish(value: Any) -> Any:
    """A list claim that reached us as one string "[a b]" or "[a,b]" becomes a list."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == '[' and value[-1] == ']':
        inner = value[1:-1].replace(',', ' ').split()
        return [v.strip('"\'') for v in inner if v.strip('"\'')]
    return value


def claims_from_aws_http_api(event: Optional[Mapping[str, Any]] = None, **_) -> Tuple[Claims, bool]:
    jwt = (((event or {}).get('requestContext') or {}).get('authorizer') or {}).get('jwt') or {}
    claims = jwt.get('claims')
    if not isinstance(claims, dict) or not claims:
        return {}, False
    return {k: _listish(v) for k, v in claims.items()}, True


def claims_from_gcp_api_gateway(headers: Optional[Mapping[str, str]] = None, **_) -> Tuple[Claims, bool]:
    value = _header(headers, GCP_USERINFO_HEADER)
    if not value:
        return {}, False
    return _b64json(value, urlsafe=True), True


def claims_from_azure_container_apps(headers: Optional[Mapping[str, str]] = None, **_) -> Tuple[Claims, bool]:
    value = _header(headers, AZURE_PRINCIPAL_HEADER)
    if not value:
        return {}, False
    principal = _b64json(value, urlsafe=False)
    role_typ = principal.get('role_typ')
    claims: Claims = {}
    for item in principal.get('claims') or []:
        typ, val = item.get('typ'), item.get('val')
        if typ is None:
            continue
        names = {typ, _AZURE_ALIASES.get(typ, typ)}
        if role_typ and typ == role_typ:
            names.add('roles')
        for name in names:
            if name == 'roles':                      # repeated: one claim per role
                claims.setdefault(name, []).append(val)
            elif name in claims:
                existing = claims[name]
                claims[name] = (existing if isinstance(existing, list) else [existing]) + [val]
            else:
                claims[name] = val
    if principal.get('auth_typ'):
        claims.setdefault('idp', principal['auth_typ'])
    return claims, True


def claims_from_gcp_cloud_run(headers: Optional[Mapping[str, str]] = None, **_) -> Tuple[Claims, bool]:
    value = _header(headers, SERVERLESS_AUTHORIZATION_HEADER) or _header(headers, 'authorization')
    if not value:
        return {}, False
    scheme, _, token = value.strip().partition(' ')
    if scheme.lower() != 'bearer' or not token.strip():
        return {}, False
    parts = token.strip().split('.')
    if len(parts) != 3:
        raise ValueError('the bearer token is not a JWT')
    claims = _b64json(parts[1], urlsafe=True)
    if not claims.get('iss') or not (claims.get('email') or claims.get('sub')):
        raise ValueError('the token names no issuer or subject')
    return claims, True


SOURCES = {
    'aws_http_api': claims_from_aws_http_api,
    'gcp_api_gateway': claims_from_gcp_api_gateway,
    'azure_container_apps': claims_from_azure_container_apps,
    'gcp_cloud_run': claims_from_gcp_cloud_run,
}


def verified_claims(source: str, event: Optional[Mapping[str, Any]] = None,
                    headers: Optional[Mapping[str, str]] = None) -> Tuple[Claims, bool]:
    """(claims, verified) from the named platform carrier. A malformed carrier raises ValueError."""
    if source not in SOURCES:
        raise ValueError(f'unknown identity source {source!r}: one of {", ".join(sorted(SOURCES))}')
    try:
        return SOURCES[source](event=event, headers=headers)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f'malformed {source} identity carrier: {e}') from e


def identity_at_ingress(config: Optional[Mapping[str, Any]] = None, event: Optional[Mapping[str, Any]] = None,
                        headers: Optional[Mapping[str, str]] = None, request_tenant: Optional[str] = None,
                        resolver=None):
    """The E2A Identity of one request, or None when no `identity_source` is configured (local
    runs, where the caller's headers are taken as given). Configured, it is fail-closed: no
    carrier, an untrusted issuer, no tenant, or `request_tenant` naming another tenant raise
    IdentityRefused; a malformed carrier raises ValueError. The resolver defaults to E2A's
    ClaimMapIdentityResolver over the same configuration (identity_claim_map, ...)."""
    from e2a import ClaimMapIdentityResolver, resolve_config
    from . import config_keys  # noqa: F401
    cfg = dict(config or {})
    source = resolve_config('identity_source', cfg)
    if not source:
        return None
    claims, verified = verified_claims(source, event=event, headers=headers)
    return (resolver or ClaimMapIdentityResolver(cfg)).resolve(claims, verified, request_tenant or None)
