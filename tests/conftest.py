"""Since v1.3 a v1.1 hook or a get_endpoint() override is refused when its class is defined
(TypeError), so no test can run on the removed adapters. This guard stays as a second line:
no test may raise E2A's v1.1 deprecation warning either."""
import warnings

import pytest


@pytest.fixture(autouse=True)
def no_e2a_v11_adapters():
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        yield
    legacy = [str(w.message) for w in seen if "uses the v1.1 keyword signature" in str(w.message)]
    assert not legacy, legacy
