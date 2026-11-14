"""Secret-backed tool credentials (v1.7.0, N2): Secrets Manager on moto; Secret Manager and Key Vault
through their SDKs' client shape (no official emulator exists for either)."""
import json
from types import SimpleNamespace

import boto3
from moto import mock_aws

from e2a_adapters.aws import SecretsManagerCredentialProvider

SPECS = [{'target': 'https://api.partner.example', 'secret': 'partner-token'},
         {'target': 'https://api.partner.example/v2', 'secret': 'partner-v2', 'header': 'X-Api-Key', 'prefix': '',
          'json_key': 'key'}]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_secrets_manager_supplies_tool_headers_and_caches_them():
    with mock_aws():
        sm = boto3.client('secretsmanager', region_name='us-east-1')
        sm.create_secret(Name='partner-token', SecretString='tok-1')
        sm.create_secret(Name='partner-v2', SecretString=json.dumps({'key': 'k-2'}))
        clock = Clock()
        provider = SecretsManagerCredentialProvider({'secret_headers': SPECS, 'secret_cache_seconds': 60}, clock=clock)
        assert provider.headers('https://api.partner.example/orders') == {'Authorization': 'Bearer tok-1'}
        assert provider.headers('https://api.partner.example/v2/orders') == {'X-Api-Key': 'k-2'}   # longest target
        assert provider.headers('https://elsewhere.example') == {}
        sm.put_secret_value(SecretId='partner-token', SecretString='tok-2')                        # rotated
        assert provider.headers('https://api.partner.example/x') == {'Authorization': 'Bearer tok-1'}
        clock.now = 61
        assert provider.headers('https://api.partner.example/x') == {'Authorization': 'Bearer tok-2'}


def test_a_tool_call_carries_the_secret_header():
    import asyncio

    from e2a import RestToolService
    seen = {}

    class Tool(RestToolService):
        async def _send_request(self, endpoint, body, token=None, timeout=None, config=None, **kwargs):
            seen['headers'] = kwargs.get('auth_headers')
            return {'status': 200, 'physical_success': True}

    with mock_aws():
        boto3.client('secretsmanager', region_name='us-east-1').create_secret(Name='partner-token', SecretString='tok')
        provider = SecretsManagerCredentialProvider({'secret_headers': SPECS})
        asyncio.run(Tool().execute({'q': 1}, {'tool_base_url': 'https://api.partner.example/orders',
                                              'credential_provider': provider}, tenant_id='t1'))
    assert seen['headers'] == {'Authorization': 'Bearer tok'}


def test_secret_manager_reads_the_latest_version():
    from e2a_adapters.gcp import SecretManagerCredentialProvider
    asked = []

    class Client:
        def access_secret_version(self, request):
            asked.append(request['name'])
            return SimpleNamespace(payload=SimpleNamespace(data=b'gtok'))

    provider = SecretManagerCredentialProvider({'secret_headers': SPECS[:1], 'gcp_project': 'p1'}, client=Client())
    assert provider.headers('https://api.partner.example/a') == {'Authorization': 'Bearer gtok'}
    assert asked == ['projects/p1/secrets/partner-token/versions/latest']


def test_key_vault_reads_the_secret_value():
    from e2a_adapters.azure import KeyVaultCredentialProvider

    class Client:
        def get_secret(self, name):
            return SimpleNamespace(value=f'kv-{name}')

    provider = KeyVaultCredentialProvider({'secret_headers': SPECS[:1]}, client=Client())
    assert provider.headers('https://api.partner.example/a') == {'Authorization': 'Bearer kv-partner-token'}
