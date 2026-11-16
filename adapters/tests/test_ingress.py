"""v1.7.4: verified claims as each cloud's platform hands them over, resolved by E2A's
ClaimMapIdentityResolver with the cloud's preset (e2a_adapters.ingress, design note MP-05)."""
import base64
import json

import pytest

from e2a import ClaimMapIdentityResolver, IdentityRefused
from e2a_adapters.identity import preset
from e2a_adapters.ingress import verified_claims


def b64url(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')   # unpadded, as JWTs are


def test_aws_http_api_jwt_authorizer_claims():
    event = {'requestContext': {'authorizer': {'jwt': {
        'claims': {'sub': 'u-1', 'custom:tenant_id': 'acme', 'cognito:groups': '[approver finance]',
                   'iss': 'https://cognito-idp.eu-west-1.amazonaws.com/pool'},
        'scopes': None}}}}
    claims, verified = verified_claims('aws_http_api', event=event)
    assert verified and claims['cognito:groups'] == ['approver', 'finance']
    ident = ClaimMapIdentityResolver({'identity_claim_map': preset('cognito')}).resolve(claims, verified, 'acme')
    assert (ident.tenant_id, ident.subject, ident.roles) == ('acme', 'u-1', ['approver', 'finance'])
    assert verified_claims('aws_http_api', event={'requestContext': {}}) == ({}, False)


def test_gcp_api_gateway_userinfo_header():
    payload = {'sub': 'u-2', 'iss': 'https://securetoken.google.com/demo', 'firebase': {'tenant': 'globex'},
               'roles': ['viewer']}
    claims, verified = verified_claims('gcp_api_gateway', headers={'X-Apigateway-Api-Userinfo': b64url(payload)})
    assert verified and claims == payload
    ident = ClaimMapIdentityResolver({'identity_claim_map': preset('identity_platform'),
                                      'identity_trusted_issuers': ['https://securetoken.google.com/demo']}) \
        .resolve(claims, verified)
    assert (ident.tenant_id, ident.roles) == ('globex', ['viewer'])
    assert verified_claims('gcp_api_gateway', headers={}) == ({}, False)


def test_azure_container_apps_client_principal_with_mapped_claim_names():
    principal = {'auth_typ': 'aad', 'name_typ': 'name',
                 'role_typ': 'http://schemas.microsoft.com/ws/2008/06/identity/claims/role',
                 'claims': [
                     {'typ': 'http://schemas.microsoft.com/identity/claims/tenantid', 'val': '9f1c-directory'},
                     {'typ': 'http://schemas.microsoft.com/identity/claims/objectidentifier', 'val': 'oid-7'},
                     {'typ': 'http://schemas.microsoft.com/ws/2008/06/identity/claims/role', 'val': 'auditor'},
                     {'typ': 'http://schemas.microsoft.com/ws/2008/06/identity/claims/role', 'val': 'reader'},
                     {'typ': 'iss', 'val': 'https://login.microsoftonline.com/9f1c-directory/v2.0'}]}
    header = base64.b64encode(json.dumps(principal).encode()).decode()
    claims, verified = verified_claims('azure_container_apps', headers={'x-ms-client-principal': header})
    assert verified and claims['tid'] == '9f1c-directory' and claims['roles'] == ['auditor', 'reader']
    ident = ClaimMapIdentityResolver({'identity_claim_map': preset('entra_id'),
                                      'identity_tenant_map': {'9f1c-directory': 'acme'}}).resolve(claims, verified,
                                                                                                    'acme')
    assert (ident.tenant_id, ident.subject, ident.roles) == ('acme', 'oid-7', ['auditor', 'reader'])


def test_no_carrier_is_refused_by_the_resolver_and_a_bad_one_is_an_error():
    claims, verified = verified_claims('gcp_api_gateway', headers={'Authorization': 'Bearer forged'})
    with pytest.raises(IdentityRefused, match='not verified'):
        ClaimMapIdentityResolver({}).resolve(claims, verified)
    with pytest.raises(ValueError, match='malformed'):
        verified_claims('azure_container_apps', headers={'X-MS-CLIENT-PRINCIPAL': 'not base64 json!'})
    with pytest.raises(ValueError, match='unknown identity source'):
        verified_claims('okta', headers={})


def test_each_cloud_profile_names_its_identity_source(monkeypatch):
    import os

    from e2a.bindings import Profile
    from e2a_adapters import profiles
    monkeypatch.delenv('E2A_IDENTITY_SOURCE', raising=False)
    folder = os.path.dirname(profiles.__file__)
    for cloud, source in (('aws', 'aws_http_api'), ('gcp', 'gcp_api_gateway'), ('azure', 'azure_container_apps')):
        profile = Profile.load(os.path.join(folder, f'{cloud}.json'))
        assert profile.config['identity_source'] == source


def test_identity_at_ingress_is_off_without_a_source_and_fail_closed_with_one(monkeypatch):
    from e2a_adapters.ingress import identity_at_ingress
    monkeypatch.delenv('E2A_IDENTITY_SOURCE', raising=False)
    assert identity_at_ingress({}, headers={'X-Tenant-Id': 'acme'}) is None          # local: headers as given
    cfg = {'identity_source': 'gcp_api_gateway', 'identity_claim_map': preset('identity_platform')}
    header = {'X-Apigateway-Api-Userinfo': b64url({'sub': 'u', 'firebase': {'tenant': 'acme'}})}
    assert identity_at_ingress(cfg, headers=header, request_tenant='acme').tenant_id == 'acme'
    with pytest.raises(IdentityRefused, match='another tenant'):
        identity_at_ingress(cfg, headers=header, request_tenant='globex')
    with pytest.raises(IdentityRefused, match='not verified'):
        identity_at_ingress(cfg, headers={'X-Tenant-Id': 'acme'})                 # no gateway: refused
