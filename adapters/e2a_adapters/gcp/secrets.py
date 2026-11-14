"""Google Secret Manager as the source of tool credentials (v1.7.0, N2), through the service account."""
from typing import Any, Dict

from e2a import resolve_config

from ..secrets import SecretHeaderProvider


class SecretManagerCredentialProvider(SecretHeaderProvider):
    def _build_client(self, config: Dict[str, Any]) -> Any:
        from google.cloud import secretmanager
        return secretmanager.SecretManagerServiceClient()

    def _fetch(self, secret: str) -> str:
        name = secret if secret.startswith('projects/') else \
            f"projects/{resolve_config('gcp_project', self.config)}/secrets/{secret}/versions/latest"
        return self.client.access_secret_version(request={'name': name}).payload.data.decode('utf-8')
