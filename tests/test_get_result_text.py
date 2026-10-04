"""Smoke tests for ResponsaClient.get_result_text().

    pytest tests/test_get_result_text.py -v

Uses a fast 2-hit query.
"""
import re

import pytest

from responsa_api import ResponsaError

QUERY = "שבעה רקיעים ושבע ארצות"

# Hit #1's citation for QUERY, from a live run. Its full text is fetched here and
# checked for real content, not just "some non-empty citation" and
# "somewhat longer than the snippet".
EXPECTED_CITATION = "אוצר מדרשים (אייזנשטיין) עשרת הדברות עמוד 451 ד\"ה (ג') עוד ברא [המתחיל בעמוד 450]"


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to a single space -- the snippet
    (clipboard-style, joined with " ") and the full text (its own PDF's
    own independent line-wrapping) can legitimately differ in exactly
    where line breaks/spacing fall, even over identical underlying words.
    """
    return re.sub(r"\s+", " ", text).strip()


def test_full_text_contains_the_search_snippet(client):
    results = client.search(QUERY)
    hit = results.hits[0]
    assert hit.citation == EXPECTED_CITATION, \
        f"expected hits[0].citation == {EXPECTED_CITATION!r}, got {hit.citation!r}"

    source = client.get_result_text(hit.index)

    assert source.text
    assert len(source.text) > len(hit.snippet)

    # The snippet is drawn directly from within the full text -- a
    # meaningful chunk of it (confirmed live to need normalizing
    # whitespace first, since the two exports wrap lines independently)
    # should appear verbatim inside the full text, proving get_result_text
    # actually fetched the SAME source the search snippet came from, not
    # something unrelated or garbled.
    snippet_sample = _normalize(hit.snippet)[:60]
    assert snippet_sample in _normalize(source.text), (
        f"expected the search snippet to appear within the full text; "
        f"looked for {snippet_sample!r}"
    )


def test_multiple_fetches_dont_close_results_window(client):
    """The specific risk flagged during planning: get_result_text() must
    not close (or otherwise invalidate) the results window it depends on,
    since a caller is expected to fetch several different indices from
    the same search in a row."""
    results = client.search(QUERY)
    assert len(results.hits) >= 2, "need at least 2 hits to exercise this"

    # White-box on purpose: this test exists to pin down one internal
    # invariant (the tracked results window survives each fetch), so it
    # reaches past the public API into the automation object.
    first = client.get_result_text(results.hits[0].index)
    assert client._impl._last_results_hwnd is not None

    second = client.get_result_text(results.hits[1].index)
    assert client._impl._last_results_hwnd is not None

    assert first.text
    assert second.text

    # A fresh search should still work normally afterward.
    more_results = client.search(QUERY)
    assert more_results.hits


def test_raises_without_a_prior_search(client):
    with pytest.raises(ResponsaError):
        client.get_result_text(1)


def test_out_of_range_index_raises(client):
    results = client.search(QUERY)
    with pytest.raises(ResponsaError):
        client.get_result_text(len(results.hits) + 1000)
