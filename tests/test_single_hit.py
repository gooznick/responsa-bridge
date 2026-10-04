"""A query with exactly one hit.

    pytest tests/test_single_hit.py -v

Regression: the results window's title (where the hit count is
read from) was confirmed live to be EMPTY for this search, so search()
reported total_hits=None and truncated=True for a search that had returned
its one and only hit.
"""
QUERY = "שאני טועם יום"
BOOKS = "תלמוד בבלי"
EXPECTED_CITATION = "תלמוד בבלי מסכת נדרים דף ס עמוד ב"


def test_single_hit_count_and_text(client):
    results = client.search(QUERY, books=BOOKS, max_hits=5)
    assert [h.citation for h in results.hits] == [EXPECTED_CITATION]
    assert results.total_hits == 1
    assert not results.truncated

    source = client.get_result_text(1)
    assert "שאני טועם יום" in source.text
