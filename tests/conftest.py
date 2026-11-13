"""v1.3 preparation (design note V13-01): no test may use E2A's v1.1 adapters (v1.1 hooks,
get_endpoint), which v1.3 removes, unless it is marked @pytest.mark.v11_adapters: those
tests exercise the adapters on purpose and become the tests that v1.3 refuses them."""
import warnings

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "v11_adapters: the test exercises E2A's v1.1 adapters on purpose")


@pytest.fixture(autouse=True)
def no_e2a_v11_adapters(request):
    if request.node.get_closest_marker("v11_adapters"):
        yield
        return
    from e2a import _contract
    _contract._WARNED.clear()          # one warning per class per process: start each test fresh
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        yield
    legacy = [str(w.message) for w in seen if "removed in v1.3" in str(w.message)]
    assert not legacy, legacy
