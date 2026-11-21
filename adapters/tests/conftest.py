"""Shared helpers for the emulator-backed adapter tests."""
import warnings

import pytest


@pytest.fixture
def drop_cosmos_container():
    """Best-effort clean-up of a Cosmos DB test container. The test has already passed or failed
    on its own; the emulator (vnext-preview) intermittently refuses container deletion with an
    internal PostgresError (E58P01), and that must not turn a passing test into an error."""
    def drop(database, name):
        try:
            database.delete_container(name)
        except Exception as error:                  # noqa: BLE001 - emulator-side fault
            warnings.warn(f'could not delete test container {name}: {error}', stacklevel=2)
    return drop
