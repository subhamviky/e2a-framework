"""Shared helpers for the emulator-backed adapter tests."""
import uuid
import warnings

import pytest


def _emulator_fault(error) -> bool:
    """The Cosmos DB emulator (vnext-preview) sometimes answers a concurrent write with an internal
    PostgreSQL error (HTTP 500, "cannot drop portal ... while it is being set up") instead of the
    409 conflict the service returns. That is an emulator fault, not a contract result."""
    return getattr(error, 'status_code', None) == 500 and 'cannot drop portal' in str(error)


@pytest.fixture
def cosmos_contract():
    """run(database, check) calls check(container) on a fresh container; on the emulator fault
    above (and only that) it warns and runs the whole check again on a new container, up to three
    attempts. Any other error, including a contract violation, fails at once. Clean-up is best effort."""
    from azure.cosmos import PartitionKey

    def run(database, check, attempts=3, prefix='ports'):
        for attempt in range(1, attempts + 1):
            container = database.create_container(f'{prefix}-{uuid.uuid4().hex[:8]}', PartitionKey('/pk'), default_ttl=-1)
            try:
                return check(container)
            except Exception as error:
                if not _emulator_fault(error) or attempt == attempts:
                    raise
                warnings.warn(f'Cosmos DB emulator fault on attempt {attempt}, retrying on a new container: {error}',
                              stacklevel=1)
            finally:
                try:
                    database.delete_container(container.id)
                except Exception as error:          # noqa: BLE001 - emulator-side fault
                    warnings.warn(f'could not delete test container {container.id}: {error}', stacklevel=1)
    return run
