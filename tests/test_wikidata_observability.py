"""Tests for Wikidata Prometheus instrumentation."""

import pytest

from entity_resolver.observability import metrics
from entity_resolver.wikidata import (
    CircuitBreaker,
    WikidataClient,
    WikidataUnavailable,
)


def _call_count() -> float:
    """Return the number of observed Wikidata API attempts."""
    return (
        metrics.registry.get_sample_value("wikidata_call_duration_seconds_count") or 0.0
    )


def test_wikidata_cache_metrics():
    """Count cache hits and misses without exposing company names."""
    calls = 0

    def fake_fetch(params):
        """Simulate an empty search result."""
        nonlocal calls
        calls += 1
        return {"search": []}

    client = WikidataClient(fetch=fake_fetch, cache_ttl=3600)
    hits = metrics.value("wikidata_cache_requests_total", result="hit")
    misses = metrics.value("wikidata_cache_requests_total", result="miss")
    assert client.lookup("Private Test Company") is None
    assert client.lookup("Private Test Company") is None
    assert calls == 1
    assert metrics.value("wikidata_cache_requests_total", result="hit") == hits + 1
    assert metrics.value("wikidata_cache_requests_total", result="miss") == misses + 1
    assert "Private Test Company" not in metrics.render()


def test_wikidata_breaker_open_metric():
    """Count the closed-to-open transition, not requests blocked while open."""

    def failing_fetch(params):
        """Simulate a network failure."""
        raise OSError("Simulated network failure")

    breaker = CircuitBreaker(threshold=2, cooldown=30, clock=lambda: 0.0)
    client = WikidataClient(
        fetch=failing_fetch, breaker=breaker, retries=0, cache_ttl=0
    )
    before = metrics.value("wikidata_breaker_opens_total")
    calls_before = _call_count()
    with pytest.raises(WikidataUnavailable):
        client.lookup("Example Company")
    assert not breaker.is_open
    assert metrics.value("wikidata_breaker_opens_total") == before
    with pytest.raises(WikidataUnavailable):
        client.lookup("Example Company")
    assert breaker.is_open
    assert metrics.value("wikidata_breaker_opens_total") == before + 1
    with pytest.raises(WikidataUnavailable):
        client.lookup("Example Company")
    assert metrics.value("wikidata_breaker_opens_total") == before + 1
    assert _call_count() == calls_before + 2


def test_wikidata_latency_counts_retry_attempts():
    """Record failed and successful attempts in the latency histogram."""
    calls = 0

    def fake_fetch(params):
        """Fail once and then return an empty search result."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("Simulated timeout")
        return {"search": []}

    client = WikidataClient(
        fetch=fake_fetch, retries=1, cache_ttl=0, sleep=lambda _: None
    )
    before = _call_count()
    assert client.lookup("Example Company") is None
    assert calls == 2
    assert _call_count() == before + 2
