"""Smoke tests across a spectrum of real durations, from seconds to a few
minutes -- unlike test_max_hits.py, these are all FULL, uncapped
extractions (no `max_hits`/`time_budget`), meant to exercise the complete
search -> Ctrl+P-probe-completion-detection -> print -> save -> parse
pipeline at genuinely different scales, not just the tiny near-instant
cases test_client.py/test_book_scope.py use.

    pytest tests/test_duration_spectrum.py -v

Hit counts and approximate real timings were established live
against the whole database (BookScope.ALL) unless noted:
  - "חמור לבן"                             ->    3 hits
  - "נר לרגלי" scoped to CHAZAL_LITERATURE  ->   29 hits
  - "דבש וחלב תחת לשונך" (BookScope.ALL)    ->  223 hits, ~3.5min

Deliberately stops there -- thousands of hits reach tens of minutes
(an earlier full-scale run of a ~2974-hit query took ~40 minutes end to
end). Those are exercised via
`max_hits`/`time_budget` capping in test_max_hits.py instead of ever
running uncapped in a routine test.
"""
import time

import pytest

from responsa_api import BookScope

CASES = [
    pytest.param("חמור לבן", None, 3, id="seconds-3hits"),
    pytest.param("נר לרגלי", BookScope.CHAZAL_LITERATURE, 29, id="tens_of_seconds-29hits"),
    pytest.param("דבש וחלב תחת לשונך", BookScope.ALL, 223, id="minutes-223hits"),
]


@pytest.mark.parametrize("query, books, expected_hits", CASES)
def test_full_extraction_at_scale(client, query, books, expected_hits):
    start = time.time()
    results = client.search(query, books=books)
    elapsed = time.time() - start
    print(f"{query!r} books={books}: {len(results.hits)} hits in {elapsed:.1f}s")

    assert not results.truncated
    assert len(results.hits) == expected_hits
    assert results.total_hits == expected_hits


def test_a_slow_computation_before_any_hits_are_found_does_not_falsely_time_out(client):
    # Regression test, root-caused live: this specific query is
    # cheap to PRINT/EXTRACT (only 8 hits) but genuinely slow to COMPUTE
    # (~35-55s of Responsa just searching, before the results window is
    # even print-ready) -- unlike the other cases above, whose duration is
    # mostly extraction time for many hits. Before the fix, this failed
    # every single time: the results window's hwnd appears within a
    # couple of seconds of submitting, long before the search itself is
    # done, and _wait_for_search_outcome's Ctrl+P-based completion probe
    # started hammering it immediately -- hitting a results window that
    # Windows' own IsHungAppWindow correctly reports as hung (Responsa's
    # UI thread genuinely busy computing, not pumping messages). Probing
    # a hung window that way raised a spurious pywinauto "no active
    # desktop" RuntimeError almost immediately (~7s in), which was then
    # misdiagnosed first as "another window has focus" and later as a
    # genuinely locked/disconnected session (ResponsaSessionLockedError)
    # -- neither was the real cause. The fix: skip the probe entirely
    # (winutil.is_window_hung() check) while the results window is hung,
    # treating that as ordinary "still working", not a failure.
    start = time.time()
    results = client.search("תלמי [-10:10] *גלגל#", max_hits=20)
    elapsed = time.time() - start
    print(f"took {elapsed:.1f}s")

    assert not results.truncated
    assert results.total_hits == 8
    assert len(results.hits) == 8


def test_a_delayed_print_dialog_is_recognized_not_treated_as_unexpected(client):
    # Regression test, root-caused live (same investigation as
    # the hung-window test above, found immediately after fixing that
    # one): _probe_print_dialog's own Ctrl+P only polls for 1s for the
    # real Print dialog to appear before giving up and returning None.
    # For a slow/complex query, confirmed live that the real dialog can
    # take ~2.5s LONGER than that 1s window to actually open -- by the
    # time _wait_for_search_outcome's next loop iteration ran its dialog
    # scan, the now-existing Print dialog didn't match any known dialog
    # type and was misclassified as a genuinely unexpected one, aborting
    # the whole search (ResponsaSearchError: "Unexpected dialog during
    # search: 'Print'") -- and left it open, which then blocked the next
    # search attempt's own _apply_book_scope too. The fix: recognize
    # PRINT_DIALOG_TITLE explicitly in the dialog scan and treat it as
    # confirmation the search is done, however late it actually shows up
    # -- nothing else in this flow ever sends Ctrl+P to the results
    # window, so any Print dialog seen there must be this probe's own.
    start = time.time()
    results = client.search("*כנפות# הארץ [-15:15] *כדור#", max_hits=25)
    elapsed = time.time() - start
    print(f"took {elapsed:.1f}s")

    assert not results.truncated
    assert results.total_hits == 6
    assert len(results.hits) == 6
