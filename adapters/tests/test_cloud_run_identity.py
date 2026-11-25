"""v1.11.0 (hub design note GCP-01): Cloud Run IAM as the ingress identity, roles granted by
configuration, and Google ID tokens as the outbound credential."""
import base64
import json

import pytest

from e2a import ClaimMapIdentityResolver, IdentityRefused, require_roles
from e2a_adapters.identity import preset
from e2a_adapters.ingress import identity_at_ingress, verified_claims
from e2a_adapters.workload import GoogleIdTokenCredentialProvider, workload_credentials

O2C = 'o2c-app@demo.iam.gserviceaccount.com'


def b64url(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')


def id_token(email=O2C, exp=4102444800, **extra) -> str:
    """A Google ID token as Cloud Run hands it to the container (the signature is not read)."""
    payload = {'iss': 'https://accounts.google.com', 'aud': 'https://settlement-abc.a.run.app', 'sub': '1077',
               'email': email, 'email_verified': True, 'exp': exp, **extra}
    return f"{b64url({'alg': 'RS256'})}.{b64url(payload)}.c2ln"


SERVICE = {'identity_source': 'gcp_cloud_run', 'identity_claim_map': preset('google_id_token'),
           'identity_trusted_issuers': ['https://accounts.google.com'], 'identity_tenants_from_roles': 'tenant:',
           'identity_subject_roles': {O2C: ['tenant:acme', 'settlement.submit']}}


def test_the_token_cloud_run_verified_is_read_from_either_header():
    claims, verified = verified_claims('gcp_cloud_run', headers={'Authorization': f'Bearer {id_token()}'})
    assert verified and claims['email'] == O2C
    # X-Serverless-Authorization wins: the caller keeps Authorization for its own use
    claims, _ = verified_claims('gcp_cloud_run', headers={'X-Serverless-Authorization': f'Bearer {id_token("b@x")}',
                                                          'authorization': f'Bearer {id_token()}'})
    assert claims['email'] == 'b@x'


def test_no_bearer_token_is_not_verified_and_a_bad_one_is_an_error():
    assert verified_claims('gcp_cloud_run', headers={}) == ({}, False)
    assert verified_claims('gcp_cloud_run', headers={'Authorization': 'Basic dTpw'}) == ({}, False)
    with pytest.raises(ValueError, match='malformed gcp_cloud_run'):
        verified_claims('gcp_cloud_run', headers={'Authorization': 'Bearer not-a-jwt'})
    with pytest.raises(ValueError, match='no issuer or subject'):
        verified_claims('gcp_cloud_run', headers={'Authorization': f"Bearer {b64url({})}.{b64url({'aud': 'x'})}.s"})


def test_a_service_account_acts_for_the_tenants_and_actions_it_is_granted():
    headers = {'Authorization': f'Bearer {id_token()}'}
    ident = identity_at_ingress(SERVICE, headers=headers, request_tenant='acme')
    assert (ident.tenant_id, ident.subject, ident.issuer) == ('acme', O2C, 'https://accounts.google.com')
    assert require_roles(ident, 'settlement.submit') is ident
    with pytest.raises(IdentityRefused, match='lacks the role'):
        require_roles(ident, 'settlement.adjust')
    with pytest.raises(IdentityRefused, match="not granted tenant 'globex'"):
        identity_at_ingress(SERVICE, headers=headers, request_tenant='globex')
    with pytest.raises(IdentityRefused, match='must name its tenant'):
        identity_at_ingress(SERVICE, headers=headers)


def test_a_subject_nobody_granted_anything_gets_nothing():
    headers = {'Authorization': f'Bearer {id_token("stranger@demo.iam.gserviceaccount.com")}'}
    with pytest.raises(IdentityRefused, match='not granted tenant'):
        identity_at_ingress(SERVICE, headers=headers, request_tenant='acme')


def test_an_issuer_outside_the_trusted_list_is_refused():
    headers = {'Authorization': f"Bearer {id_token(iss='https://evil.example')}"}
    with pytest.raises(IdentityRefused, match='not trusted'):
        identity_at_ingress(SERVICE, headers=headers, request_tenant='acme')


def test_configured_roles_add_to_the_roles_in_the_claims_for_any_provider():
    resolver = ClaimMapIdentityResolver({'identity_claim_map': preset('cognito'),
                                         'identity_subject_roles': {'u-1': ['approver', 'finance']}})
    ident = resolver.resolve({'sub': 'u-1', 'custom:tenant_id': 'acme', 'cognito:groups': ['finance']}, True)
    assert ident.roles == ['finance', 'approver']
    assert resolver.resolve({'sub': 'u-2', 'custom:tenant_id': 'acme'}, True).roles == []


def test_outbound_google_id_token_for_the_target_origin():
    asked = []

    def metadata(method, url, headers=None, data=None, timeout=5.0):
        asked.append(url)
        assert headers == {'Metadata-Flavor': 'Google'}
        return 200, id_token().encode()

    provider = workload_credentials({'workload_identity': 'gcp_id_token'}, http=metadata)
    assert isinstance(provider, GoogleIdTokenCredentialProvider)
    headers = provider.headers('https://settlement-abc.a.run.app/api/v1/settlements')
    assert headers == {'Authorization': f'Bearer {id_token()}'}
    assert 'audience=https%3A%2F%2Fsettlement-abc.a.run.app&' in asked[0]
    provider.headers('https://settlement-abc.a.run.app/api/v1/other')      # cached per audience
    assert len(asked) == 1
    assert provider.headers('not a url') == {}


def test_outbound_audience_can_be_configured_per_url_prefix():
    provider = workload_credentials({'workload_identity': 'gcp_id_token',
                                     'workload_token_scopes': {'https://recon.internal': 'https://recon-xyz.a.run.app'}},
                                    http=lambda *a, **k: (200, id_token().encode()))
    assert provider.scope_for('https://recon.internal/service/v1/runs') == 'https://recon-xyz.a.run.app'
