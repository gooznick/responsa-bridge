"""Smoke test: a query with zero hits should return an empty SearchResults,
not raise. Fast -- part of the regular test pass.

    pytest tests/test_no_results.py
"""

# All-Hebrew nonsense (the query language rejects non-Hebrew/non-digit
# characters outright with a different error, so this has to be Hebrew
# letters that just don't spell anything in the corpus).
NONSENSE_QUERY = "קשדגכקשדגכקשדגכ"


def test_zero_hits_returns_empty_results(client):
    results = client.search(NONSENSE_QUERY)
    assert results.hits == []


def test_zero_hits_reports_total_hits_zero(client):
    """Regression: a no-results search used to report
    total_hits=None ("unknown") instead of 0."""
    results = client.search(NONSENSE_QUERY)
    assert results.total_hits == 0
    assert not results.truncated


def test_scoped_search_right_after_zero_hits(client):
    """The sequence that preceded the first 'ניהול המאגרים' failure in the
    MCP server log: a scoped no-results search leaves the
    Advanced Search dialog open, then the next scoped search must still
    open the databases dialog and run normally."""
    empty = client.search("יום אחד כהיום", books="תלמוד בבלי")
    assert empty.hits == []
    results = client.search("שאני טועם יום", books="תלמוד בבלי")
    assert results.hits
