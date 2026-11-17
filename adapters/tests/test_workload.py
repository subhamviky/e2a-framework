"""v1.8.0 (hub design note SI-01): outbound workload identity, and a service caller at ingress.
No cloud is reached: the metadata server, STS, Entra and the managed identity are fakes."""
import base64
import datetime as dt
import json
import time
import urllib.parse

import pytest

from e2a import ClaimMapIdentityResolver, IdentityRefused, require_roles
from e2a_adapters.identity import preset
from e2a_adapters.ingress import verified_claims
from e2a_adapters.workload import (ENTRA_EXCHANGE_AUDIENCE, AwsWebIdentityTokenSource, EntraFederatedTokenSource,
                                   GoogleIdTokenSource, WorkloadCredentialProvider, WorkloadTokenError,
                                   jwt_expiry, workload_credentials)


def jwt(**claims) -> str:
    part = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip('=')
    return f"{part({'alg': 'RS256'})}.{part(claims)}.sig"


class FakeCloud:
    """The metadata server and Entra's token endpoint, recording what they were asked."""

    def __init__(self, entra_status=200):
        self.calls, self.entra_status = [], entra_status

    def __call__(self, method, url, headers=None, data=None, timeout=5.0):
        self.calls.append((method, url, dict(headers or {}), data))
        if 'metadata.google.internal' in url:
            assert headers.get('Metadata-Flavor') == 'Google'
            aud = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)['audience'][0]
            return 200, jwt(iss='https://accounts.google.com', sub='1234567890', aud=aud,
                            exp=int(time.time()) + 3600).encode()
        if 'login.microsoftonline.com' in url:
            form = urllib.parse.parse_qs(data.decode())
            if self.entra_status != 200:
                return self.entra_status, json.dumps({'error': 'invalid_client',
                                                      'error_description': 'AADSTS70021: no matching federated identity'}).encode()
            return 200, json.dumps({'access_token': f"entra-for-{form['scope'][0]}", 'expires_in': 3599}).encode()
        raise AssertionError(url)


class FakeSTS:
    def __init__(self):
        self.calls = []

    def get_web_identity_token(self, **kw):
        self.calls.append(kw)
        return {'WebIdentityToken': jwt(iss='https://abc.tokens.sts.global.api.aws', sub='arn:aws:iam::1:role/recon',
                                        exp=int(time.time()) + 900),
                'Expiration': dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=900)}


SCOPES = {'https://settlement.example': 'api://settlement/.default',
          'https://settlement.example/internal': 'api://settlement-admin/.default'}


def test_google_token_is_exchanged_for_an_entra_token_for_the_target():
    cloud = FakeCloud()
    provider = workload_credentials({'workload_identity': 'gcp', 'workload_entra_tenant': 'dir',
                                     'workload_entra_client_id': 'o2c-app', 'workload_token_scopes': SCOPES}, http=cloud)
    headers = provider.headers('https://settlement.example/api/v1/settlements')
    assert headers == {'Authorization': 'Bearer entra-for-api://settlement/.default'}
    (_, meta_url, _, _), (method, token_url, _, data) = cloud.calls
    assert f'audience={urllib.parse.quote(ENTRA_EXCHANGE_AUDIENCE, safe="")}' in meta_url
    form = urllib.parse.parse_qs(data.decode())
    assert method == 'POST' and token_url.endswith('/dir/oauth2/v2.0/token')
    assert form['client_id'] == ['o2c-app'] and form['grant_type'] == ['client_credentials']
    assert form['client_assertion_type'] == ['urn:ietf:params:oauth:client-assertion-type:jwt-bearer']
    assert jwt_expiry(form['client_assertion'][0]) > time.time()


def test_tokens_are_cached_per_scope_until_shortly_before_expiry():
    cloud, now = FakeCloud(), [1_000_000.0]
    source = EntraFederatedTokenSource(GoogleIdTokenSource(cloud), 'dir', 'o2c-app', cloud)
    real_token = source.token
    source.token = lambda scope: (real_token(scope)[0], now[0] + 3600)
    provider = WorkloadCredentialProvider(source, SCOPES, refresh_seconds=300, clock=lambda: now[0])
    provider.headers('https://settlement.example/a')
    provider.headers('https://settlement.example/b')
    assert len(cloud.calls) == 2                                  # one exchange (metadata + Entra)
    provider.headers('https://settlement.example/internal/x')     # the longest prefix: another scope
    assert len(cloud.calls) == 4
    now[0] += 3400                                                # inside the refresh window
    provider.headers('https://settlement.example/a')
    assert len(cloud.calls) == 6


def test_a_target_without_a_scope_gets_no_credential():
    provider = WorkloadCredentialProvider(GoogleIdTokenSource(FakeCloud()), SCOPES)
    assert provider.headers('https://elsewhere.example/x') == {}


def test_aws_web_identity_token_is_exchanged_for_entra():
    sts, cloud = FakeSTS(), FakeCloud()
    provider = workload_credentials({'workload_identity': 'aws', 'workload_entra_tenant': 'dir',
                                     'workload_entra_client_id': 'recon-app', 'workload_token_scopes': SCOPES},
                                    http=cloud, sts_client=sts)
    assert provider.headers('https://settlement.example/x')['Authorization'] == 'Bearer entra-for-api://settlement/.default'
    assert sts.calls == [{'Audience': [ENTRA_EXCHANGE_AUDIENCE], 'SigningAlgorithm': 'RS256', 'DurationSeconds': 900}]
    assert AwsWebIdentityTokenSource(sts).token('x')[1] > time.time()


def test_azure_managed_identity_needs_no_exchange():
    class Credential:
        def get_token(self, scope):
            return type('AccessToken', (), {'token': f'mi-{scope}', 'expires_on': time.time() + 3600})()
    provider = workload_credentials({'workload_identity': 'azure', 'workload_token_scopes': SCOPES},
                                    azure_credential=Credential())
    assert provider.headers('https://settlement.example/x') == {'Authorization': 'Bearer mi-api://settlement/.default'}


def test_a_refused_exchange_is_a_workload_token_error():
    provider = workload_credentials({'workload_identity': 'gcp', 'workload_entra_tenant': 'dir',
                                     'workload_entra_client_id': 'o2c-app', 'workload_token_scopes': SCOPES},
                                    http=FakeCloud(entra_status=401))
    with pytest.raises(WorkloadTokenError, match='AADSTS70021'):
        provider.headers('https://settlement.example/x')


def test_unset_means_no_provider_and_bad_settings_are_refused():
    assert workload_credentials({}) is None
    with pytest.raises(ValueError):
        workload_credentials({'workload_identity': 'gcp'})        # no Entra app
    with pytest.raises(ValueError):
        workload_credentials({'workload_identity': 'oracle'})


def principal(roles, appid='o2c-app'):
    claims = [{'typ': 'iss', 'val': 'https://login.microsoftonline.com/dir/v2.0'}, {'typ': 'azp', 'val': appid},
              {'typ': 'http://schemas.microsoft.com/identity/claims/tenantid', 'val': 'dir'}]
    claims += [{'typ': 'roles', 'val': r} for r in roles]
    return base64.b64encode(json.dumps({'auth_typ': 'aad', 'claims': claims, 'role_typ': 'roles'}).encode()).decode()


def test_a_service_caller_at_container_apps_ingress():
    cfg = {'identity_claim_map': preset('entra_service'), 'identity_tenants_from_roles': 'tenant:'}
    claims, verified = verified_claims('azure_container_apps',
                                       headers={'X-MS-CLIENT-PRINCIPAL': principal(['settlement.submit', 'tenant:acme'])})
    identity = require_roles(ClaimMapIdentityResolver(cfg).resolve(claims, verified, 'acme'), 'settlement.submit')
    assert (identity.tenant_id, identity.subject) == ('acme', 'o2c-app')
    with pytest.raises(IdentityRefused):
        ClaimMapIdentityResolver(cfg).resolve(claims, verified, 'globex')
    with pytest.raises(IdentityRefused):
        require_roles(identity, 'settlement.adjust')
