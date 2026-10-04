"""Smoke tests for the `max_hits` search() param and `time_budget`
auto-shortening.

    pytest tests/test_max_hits.py -v

Uses a query known to match 143 results across the whole database (per a
live check) -- big enough that an unbounded export takes a real, noticeable
amount of time, small enough that the *capped* runs here stay fast.
"""
import pytest

from responsa_api import ResponsaClient, ResponsaTimeoutError

QUERY = "תפוחי זהב במשכיות"


def test_explicit_max_hits_caps_results(client):
    """Explicit max_hits: hard cap regardless of any time budget."""
    results = client.search(QUERY, max_hits=5)
    assert len(results.hits) == 5
    assert results.truncated
    assert results.total_hits == 143


def test_time_budget_auto_shortens():
    """time_budget set, no explicit max_hits: should auto-shorten rather
    than run the full (slow) export."""
    with ResponsaClient(time_budget=60) as client:
        results = client.search(QUERY)
    assert results.truncated
    assert 0 < len(results.hits) <= 30


def test_time_budget_too_small_raises():
    """time_budget too small for even the smallest extraction: should
    raise up front, without ever starting the export."""
    with ResponsaClient(time_budget=10) as client:
        with pytest.raises(ResponsaTimeoutError):
            client.search(QUERY)
