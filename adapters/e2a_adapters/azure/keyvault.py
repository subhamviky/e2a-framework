"""Azure Key Vault as the source of tool credentials (v1.7.0, N2), through the managed identity."""
from typing import Any, Dict

from e2a import resolve_config

from ..secrets import SecretHeaderProvider


class KeyVaultCredentialProvider(SecretHeaderProvider):
    def _build_client(self, config: Dict[str, Any]) -> Any:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        return SecretClient(vault_url=resolve_config('keyvault_url', config), credential=DefaultAzureCredential())

    def _fetch(self, secret: str) -> str:
        return self.client.get_secret(secret).value
