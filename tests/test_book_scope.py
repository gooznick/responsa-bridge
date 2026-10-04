"""Smoke tests for the `books` scope parameter of search().

    pytest tests/test_book_scope.py -v

Uses a narrow, already-verified-low-hit-count query throughout (1 hit
in Mishna, 4 across all three scopes of the union test, 1 in Geonim)
rather than a single common word -- a first version of this test used a
common word alone, which still produced 1356 hits scoped to just two
large corpora (Mishna + Rambam), making the print-to-PDF step take
several minutes (~280+ pages) for what should be a quick smoke test.
"""
from responsa_api import BookScope

QUERY = "זנב לאריות"

# Specific citations from a live run of QUERY under each scope
# -- checking for these exact sources, not just "some
# non-empty citation", confirms the scope filter is actually selecting
# real, correct content rather than e.g. an empty/garbled parse that
# happens to satisfy a prefix check.
MISHNA_CITATION = "משנה מסכת אבות פרק ד משנה טו"
UNION_CITATION = 'שו"ת תשב"ץ חלק א סימן א ד"ה באתר זיקוקין'  # an Old-Shut hit, not Mishna


def test_single_enum_scope(client):
    results = client.search(QUERY, books=BookScope.MISHNA)
    assert results.hits, "expected at least one hit scoped to Mishna"
    for hit in results.hits:
        assert hit.citation.startswith("משנה"), \
            f"citation outside Mishna scope: {hit.citation!r}"

    citations = [hit.citation for hit in results.hits]
    assert MISHNA_CITATION in citations, \
        f"expected to find {MISHNA_CITATION!r} among {citations!r}"


def test_list_union_of_scopes(client):
    results = client.search(QUERY, books=[BookScope.MISHNA, BookScope.RAMBAM, BookScope.OLD_SHUT])
    assert results.hits, "expected at least one hit across the three scopes"
    citations = [hit.citation for hit in results.hits]
    for hit in results.hits:
        print(f"  {hit.index}. {hit.citation}")

    assert MISHNA_CITATION in citations, \
        f"expected to find {MISHNA_CITATION!r} among {citations!r}"
    assert UNION_CITATION in citations, \
        f"expected to find {UNION_CITATION!r} among {citations!r}"


def test_free_text_scope(client):
    # A sources-tree node name not covered by the BookScope enum at all --
    # just confirms free-text scoping resolves and runs without raising.
    results = client.search(QUERY, books="גאונים")
    print(f"Geonim (free text): {len(results.hits)} hits")
