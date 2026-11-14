"""Identity at ingress: the tenant comes from verified claims, not from the request body
(v1.7.0, design note MP-05, N3).

Token verification (signature, expiry, audience) is the gateway's job: API Gateway / ALB with
Cognito, API Management or App Gateway with Entra ID, API Gateway / IAP with Identity Platform.
E2A receives the claims the gateway verified and resolves them, the same way for every
identity provider, through a claim map from configuration:

    identity_claim_map = {"tenant_id": "custom:tenant_id", "subject": "sub", "roles": "cognito:groups"}

The provider presets (Cognito, Entra ID, Identity Platform) are cloud infrastructure and live
in e2a-adapters' cloud profiles. E2A refuses, with IdentityRefused (a GovernanceRefusal):
  * claims the gateway did not mark verified;
  * an issuer outside `identity_trusted_issuers` (when the list is set);
  * claims without a tenant;
  * a tenant in the request that differs from the tenant in the claims.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import resolve_config
from .stages import GovernanceRefusal

__all__ = ['Identity', 'IdentityRefused', 'BaseIdentityResolver', 'ClaimMapIdentityResolver']


class IdentityRefused(GovernanceRefusal, PermissionError):
    code = 'IDENTITY'


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    subject: Optional[str] = None
    roles: List[str] = field(default_factory=list)
    issuer: Optional[str] = None


def _claim(claims: Dict[str, Any], path: Optional[str]) -> Any:
    """A claim by name; a dotted path reaches into nested claims ("app_metadata.tenant")."""
    if not path:
        return None
    if path in claims:
        return claims[path]
    value: Any = claims
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


class BaseIdentityResolver:
    """Port (v1.7.0). Public: resolve(). A subclass may override _tenant_from(claims, config)
    when a provider encodes the tenant in a way a claim name cannot express."""
    capabilities = frozenset({'identity'})

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = dict(config or {})

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'BaseIdentityResolver':
        return cls(config)

    def resolve(self, claims: Dict[str, Any], verified: bool, request_tenant: Optional[str] = None,
                config: Optional[Dict[str, Any]] = None) -> Identity:
        """The identity of a request whose claims the gateway verified (`verified=True`).
        `request_tenant`, when the request names one, must equal the claims' tenant."""
        cfg = {**self.config, **(config or {})}
        if not verified:
            raise IdentityRefused('claims were not verified by the gateway', 'identity')
        claims = dict(claims or {})
        mapping = {'tenant_id': 'tenant_id', 'subject': 'sub', 'roles': 'roles', 'issuer': 'iss',
                   **(resolve_config('identity_claim_map', cfg) or {})}
        issuer = _claim(claims, mapping.get('issuer'))
        trusted = resolve_config('identity_trusted_issuers', cfg) or []
        if trusted and issuer not in trusted:
            raise IdentityRefused(f'issuer {issuer!r} is not trusted', 'identity')
        tenant = self._tenant_from(claims, mapping, cfg)
        if not tenant:
            raise IdentityRefused(f"no tenant in the claims (claim {mapping.get('tenant_id')!r})", 'identity')
        if request_tenant is not None and str(request_tenant) != str(tenant):
            raise IdentityRefused('the request names another tenant than its credentials', 'identity')
        roles = _claim(claims, mapping.get('roles'))
        if isinstance(roles, str):
            roles = [r for r in roles.replace(',', ' ').split() if r]
        subject = _claim(claims, mapping.get('subject'))
        return Identity(tenant_id=str(tenant), subject=None if subject is None else str(subject),
                        roles=[str(r) for r in (roles or [])], issuer=issuer)

    def _tenant_from(self, claims: Dict[str, Any], mapping: Dict[str, str], config: Dict[str, Any]) -> Optional[str]:
        """Hook: the tenant from the claims. Default: the mapped claim, through
        `identity_tenant_map` when the claim holds an external id (an Entra ID directory id)."""
        value = _claim(claims, mapping.get('tenant_id'))
        if value is None:
            return None
        aliases = resolve_config('identity_tenant_map', config) or {}
        return aliases.get(str(value), str(value)) if aliases else str(value)


class ClaimMapIdentityResolver(BaseIdentityResolver):
    """The resolver as configured: everything comes from identity_claim_map,
    identity_trusted_issuers and identity_tenant_map."""
