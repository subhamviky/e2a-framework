"""Outbound workload identity: a service proves who it is to another service with its own
cloud's identity, never a stored secret (v1.8.0, hub design note SI-01).

Each cloud gives a workload a signed token for itself:
  GoogleIdTokenSource       Google Cloud: an ID token from the metadata server for the service
                            account (issuer https://accounts.google.com, subject = its unique id)
  AwsWebIdentityTokenSource AWS: STS GetWebIdentityToken (IAM outbound identity federation; enable
                            it once per account; regional endpoint, not the global one)
  AzureManagedIdentitySource Azure: a managed identity's Entra token for a scope, directly

The landscape trusts one issuer, Entra ID. EntraFederatedTokenSource exchanges a Google or AWS
token for an Entra access token through workload identity federation: the client credentials
grant with the native token as `client_assertion`, for the caller's Entra app (whose federated
credential trusts the native issuer and subject).

WorkloadCredentialProvider is the E2A BaseCredentialProvider the tools already use
(config['credential_provider'], or a tool's own): it picks the scope for the call's target URL
(`workload_token_scopes`, longest URL prefix wins), and caches each token until
`workload_token_refresh_seconds` (300) before it expires. A failure to get a token raises
WorkloadTokenError, which E2A's tool contract treats as a transport failure: retried for a read,
UNKNOWN for a material call.

    provider = workload_credentials({
        "workload_identity": "gcp",                       # or "aws", "azure"
        "workload_entra_tenant": "<directory id>",        # not needed for "azure"
        "workload_entra_client_id": "<caller app id>",
        "workload_token_scopes": {"https://settlement.example": "api://settlement/.default"},
    })
    provider.headers("https://settlement.example/api/v1/settlements")   # {"Authorization": "Bearer ..."}

Google Cloud alone (v1.11.0, hub design note GCP-01): with `workload_identity = "gcp_id_token"`
the service account's Google ID token is itself the credential, with the target's own URL as its
audience. Cloud Run IAM verifies it in front of the target (roles/run.invoker), and the target
reads it with the ingress source gcp_cloud_run. No Entra app, no exchange. The audience is the
target's origin (https://host) unless `workload_token_scopes` names one for its URL prefix.

Every network call goes through an injectable `http` (or STS client, or Azure credential), so the
sources are tested without a cloud.
"""
import base64
import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from e2a import BaseCredentialProvider
from e2a.config import resolve_config

__all__ = ['WorkloadTokenError', 'GoogleIdTokenSource', 'AwsWebIdentityTokenSource', 'AzureManagedIdentitySource',
           'EntraFederatedTokenSource', 'WorkloadCredentialProvider', 'GoogleIdTokenCredentialProvider',
           'workload_credentials',
           'ENTRA_EXCHANGE_AUDIENCE', 'jwt_expiry']

# The audience Entra expects on a federated (native) token
ENTRA_EXCHANGE_AUDIENCE = 'api://AzureADTokenExchange'
GOOGLE_METADATA_IDENTITY = ('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/'
                            'default/identity')

Token = Tuple[str, float]           # (token, expires_at as a UNIX time)
Http = Callable[..., Tuple[int, bytes]]


class WorkloadTokenError(RuntimeError):
    """A workload token could not be obtained or exchanged."""


def jwt_expiry(token: str) -> float:
    """The `exp` claim of a JWT (not verified: only to know when to refresh our own token)."""
    try:
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        return float(claims['exp'])
    except (IndexError, KeyError, ValueError) as e:
        raise WorkloadTokenError(f'not a JWT with an exp claim: {e}') from e


def _urllib_http(method: str, url: str, headers: Optional[Mapping[str, str]] = None,
                 data: Optional[bytes] = None, timeout: float = 5.0) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise WorkloadTokenError(f'{method} {url}: {e}') from e


class GoogleIdTokenSource:
    """Google Cloud: the service account's ID token for an audience (metadata server)."""

    def __init__(self, http: Http = None, metadata_url: str = GOOGLE_METADATA_IDENTITY) -> None:
        self.http, self.metadata_url = http or _urllib_http, metadata_url

    def token(self, audience: str) -> Token:
        url = f'{self.metadata_url}?{urllib.parse.urlencode({"audience": audience, "format": "full"})}'
        status, body = self.http('GET', url, headers={'Metadata-Flavor': 'Google'})
        if status != 200:
            raise WorkloadTokenError(f'metadata server answered {status}')
        token = body.decode().strip()
        return token, jwt_expiry(token)


class AwsWebIdentityTokenSource:
    """AWS: a signed JWT for the IAM role (STS GetWebIdentityToken, outbound identity federation)."""

    def __init__(self, sts_client=None, region: Optional[str] = None, duration_seconds: int = 900,
                 signing_algorithm: str = 'RS256') -> None:
        self._client, self.region = sts_client, region
        self.duration_seconds, self.signing_algorithm = duration_seconds, signing_algorithm

    def _sts(self):
        if self._client is None:
            import boto3        # e2a-adapters[aws]; a regional endpoint: the global one has no such API
            self._client = boto3.client('sts', region_name=self.region)
        return self._client

    def token(self, audience: str) -> Token:
        try:
            answer = self._sts().get_web_identity_token(Audience=[audience], SigningAlgorithm=self.signing_algorithm,
                                                        DurationSeconds=self.duration_seconds)
        except Exception as e:                       # botocore errors, disabled federation
            raise WorkloadTokenError(f'STS GetWebIdentityToken: {e}') from e
        expiration = answer.get('Expiration')
        expires_at = expiration.timestamp() if hasattr(expiration, 'timestamp') else jwt_expiry(answer['WebIdentityToken'])
        return answer['WebIdentityToken'], expires_at


class AzureManagedIdentitySource:
    """Azure: the managed identity's Entra token for a scope (already an Entra token: no exchange)."""

    def __init__(self, credential=None, client_id: Optional[str] = None) -> None:
        self._credential, self.client_id = credential, client_id

    def _cred(self):
        if self._credential is None:
            from azure.identity import ManagedIdentityCredential      # e2a-adapters[azure]
            self._credential = ManagedIdentityCredential(client_id=self.client_id)
        return self._credential

    def token(self, scope: str) -> Token:
        try:
            access = self._cred().get_token(scope)
        except Exception as e:
            raise WorkloadTokenError(f'managed identity: {e}') from e
        return access.token, float(access.expires_on)


class EntraFederatedTokenSource:
    """An Entra access token for `scope`, obtained by presenting a native workload token (Google
    or AWS) as the client assertion of the caller's Entra app (workload identity federation)."""

    def __init__(self, native, tenant_id: str, client_id: str, http: Http = None,
                 authority: str = 'https://login.microsoftonline.com') -> None:
        self.native, self.tenant_id, self.client_id = native, tenant_id, client_id
        self.http, self.authority = http or _urllib_http, authority.rstrip('/')

    def token(self, scope: str) -> Token:
        assertion, _ = self.native.token(ENTRA_EXCHANGE_AUDIENCE)
        form = urllib.parse.urlencode({
            'grant_type': 'client_credentials', 'client_id': self.client_id, 'scope': scope,
            'client_assertion_type': 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
            'client_assertion': assertion}).encode()
        status, body = self.http('POST', f'{self.authority}/{self.tenant_id}/oauth2/v2.0/token',
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'}, data=form)
        try:
            answer = json.loads(body or b'{}')
        except ValueError:
            answer = {}
        if status != 200 or 'access_token' not in answer:
            raise WorkloadTokenError(f"Entra token exchange answered {status}: "
                                     f"{answer.get('error')} {answer.get('error_description', '')[:200]}".strip())
        return answer['access_token'], time.time() + float(answer.get('expires_in', 3600))


class WorkloadCredentialProvider(BaseCredentialProvider):
    """E2A's credential seam for outbound calls: a Bearer token for the call's target, cached."""

    def __init__(self, source, scopes: Mapping[str, str], refresh_seconds: float = 300.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.source, self.refresh_seconds, self.clock = source, float(refresh_seconds), clock
        self.scopes = dict(scopes or {})
        self._cache: Dict[str, Token] = {}
        self._lock = threading.Lock()

    def scope_for(self, target: str, scope: Optional[str] = None) -> Optional[str]:
        if scope:
            return scope
        matches = [prefix for prefix in self.scopes if str(target or '').startswith(prefix)]
        return self.scopes[max(matches, key=len)] if matches else None

    def token(self, scope: str) -> str:
        with self._lock:
            cached = self._cache.get(scope)
            if cached and cached[1] - self.refresh_seconds > self.clock():
                return cached[0]
        token = self.source.token(scope)
        with self._lock:
            self._cache[scope] = token
        return token[0]

    def headers(self, target, scope=None, config=None, **kwargs) -> Dict[str, str]:
        chosen = self.scope_for(target, scope)
        if chosen is None:
            return {}                    # a target with no scope configured gets no credential
        return {'Authorization': f'Bearer {self.token(chosen)}'}


class GoogleIdTokenCredentialProvider(WorkloadCredentialProvider):
    """v1.11.0: a Google ID token for every target, its audience the target's origin (Cloud Run
    IAM checks it against the service URL) unless a configured URL prefix names another."""

    def scope_for(self, target: str, scope: Optional[str] = None) -> Optional[str]:
        chosen = super().scope_for(target, scope)
        if chosen:
            return chosen
        parts = urllib.parse.urlsplit(str(target or ''))
        return f'{parts.scheme}://{parts.netloc}' if parts.scheme in ('http', 'https') and parts.netloc else None


def workload_credentials(config: Optional[Dict[str, Any]] = None, *, http: Http = None, sts_client=None,
                         azure_credential=None) -> Optional[WorkloadCredentialProvider]:
    """The provider configured by `workload_identity` (gcp | aws | azure | gcp_id_token), or None when it is not
    set (local runs and tests: calls go without a token, as before)."""
    cfg = dict(config or {})
    kind = (resolve_config('workload_identity', cfg) or '').strip().lower()
    if not kind:
        return None
    scopes = resolve_config('workload_token_scopes', cfg) or {}
    refresh = float(resolve_config('workload_token_refresh_seconds', cfg))
    if kind == 'gcp_id_token':
        return GoogleIdTokenCredentialProvider(GoogleIdTokenSource(http), scopes, refresh)
    if kind == 'azure':
        source = AzureManagedIdentitySource(azure_credential, resolve_config('workload_azure_client_id', cfg) or None)
    elif kind in ('gcp', 'aws'):
        native = (GoogleIdTokenSource(http) if kind == 'gcp' else
                  AwsWebIdentityTokenSource(sts_client, resolve_config('workload_aws_region', cfg) or None))
        tenant, client = resolve_config('workload_entra_tenant', cfg), resolve_config('workload_entra_client_id', cfg)
        if not tenant or not client:
            raise ValueError(f'workload_identity={kind!r} needs workload_entra_tenant and workload_entra_client_id')
        source = EntraFederatedTokenSource(native, tenant, client, http)
    else:
        raise ValueError(f'workload_identity={kind!r}: use gcp, aws, azure or gcp_id_token')
    return WorkloadCredentialProvider(source, scopes, refresh)
