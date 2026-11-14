"""AWS Secrets Manager as the source of tool credentials (v1.7.0, N2), through the platform's role."""
from typing import Any, Dict

from e2a import resolve_config

from ..secrets import SecretHeaderProvider


class SecretsManagerCredentialProvider(SecretHeaderProvider):
    def _build_client(self, config: Dict[str, Any]) -> Any:
        import boto3
        return boto3.client('secretsmanager', region_name=resolve_config('aws_region', config),
                            endpoint_url=resolve_config('aws_endpoint_url', config))

    def _fetch(self, secret: str) -> str:
        return self.client.get_secret_value(SecretId=secret)['SecretString']
