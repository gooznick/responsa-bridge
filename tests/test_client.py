"""Smoke test for ResponsaClient.

Not mocked on purpose: Responsa is a real desktop app with no test double,
so the only meaningful test is running it end to end against the real GUI
and a real license dongle (needs both plugged in / installed on this
machine).

    pytest tests/test_client.py
"""

# Deliberately narrow (no wildcards, no distance window -- just the
# words adjacent) to keep this smoke test fast: a query matching thousands
# of results is a genuinely multi-hundred-page, ~40-minute print+parse job
# (confirmed live) -- fine as a one-off validation of the largest
# realistic case (see test_duration_spectrum.py / test_max_hits.py), but
# far too slow for a routine smoke test. This query matches only 6.
QUERY = "גשר של ברזל"

# One specific hit's citation from a live run of QUERY --
# checking for this exact source, not just "some non-empty citation",
# confirms the parser is actually reading real content correctly rather
# than e.g. silently matching on garbled/misaligned text.
EXPECTED_CITATION = 'מדרגת האדם תיקון המדות פרק א ד"ה אם נתבונן'


def test_search_returns_structured_hits(client):
    results = client.search(QUERY)
    assert results.hits, "expected at least one hit for a known-good query"
    for hit in results.hits:
        assert hit.citation
        assert hit.snippet

    citations = [hit.citation for hit in results.hits]
    assert EXPECTED_CITATION in citations, \
        f"expected to find {EXPECTED_CITATION!r} among {citations!r}"
