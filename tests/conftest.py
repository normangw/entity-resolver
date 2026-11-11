import os

# Keep the test suite offline: the fallback is on by default in production.
os.environ.setdefault("WIKIDATA_FALLBACK", "false")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Each test starts with empty rate-limit buckets (they are per process)."""
    from entity_resolver.protection import limiter

    limiter._buckets.clear()
    yield
