"""Recovery from a modal dialog a previous call left open: a Print/Save
dialog, or the results-summary dialog of a search that timed out.

    pytest tests/test_leftover_dialogs.py -v

Regression, seen live by the user: the "Save Print Output As"
dialog kept gaining and losing focus while the automation tried to press
Enter in it; when that gave up, the dialog was left open and the NEXT call
started a new search underneath it -- that call failed, and so did every
call after it. These tests recreate that state on purpose (print a results
window up to the Save dialog, then stop) and check that the next call
cleans it up and works.
"""
import pytest
import win32con
import win32gui

from pywinauto.controls.hwndwrapper import HwndWrapper

from responsa_api import ResponsaTimeoutError
from responsa_api.internal import winutil

QUERY = "כדור הארץ עגול"  # a single, fast hit
# 8 hits, but ~35-55s of Responsa computing before any result is ready --
# same query as test_duration_spectrum.py's hung-window regression test.
SLOW_QUERY = "תלמי [-10:10] *גלגל#"


def _leave_a_save_dialog_open(impl):
    """Ctrl+P on the current results window, accept Print, and stop at
    the Save dialog -- exactly the state a failed export left behind."""
    cfg = impl.config
    hwnd = impl._last_results_hwnd
    impl._bring_to_foreground(hwnd)
    impl._safe_set_focus(HwndWrapper(hwnd))
    impl._require_foreground(hwnd)
    impl._send_keys(win32con.VK_CONTROL, ord("P"))
    print_hwnd = impl._wait_for_new_top_level_dialog(timeout=10, title=cfg.PRINT_DIALOG_TITLE)
    impl._accept_print_dialog(print_hwnd)
    winutil.wait_for(impl._find_current_save_dialog, timeout=40, interval=0.3)


def test_search_after_a_leftover_save_dialog(client):
    impl = client._impl
    client.search(QUERY)
    _leave_a_save_dialog_open(impl)

    results = client.search(QUERY)

    assert results.hits
    assert impl._find_current_save_dialog() is None


def test_get_result_text_after_a_leftover_save_dialog(client):
    impl = client._impl
    results = client.search(QUERY)
    _leave_a_save_dialog_open(impl)

    source = client.get_result_text(results.hits[0].index)

    assert source.text
    assert impl._find_current_save_dialog() is None


def test_search_after_a_search_that_timed_out(client):
    """Regression, reproduced live: a search that times out while Responsa
    is still computing leaves the app busy, and its results-summary dialog
    ("N תוצאות") appears only later. The next search used to start
    underneath that dialog and fail ("'ניהול המאגרים' never opened"), and
    so did every search after it, until hard_reset()."""
    impl = client._impl
    original_timeout = impl.search_timeout
    # How long SLOW_QUERY takes depends on the machine (35-55s on the
    # original VM, ~4s on a fast one), so a short timeout alone doesn't
    # reliably time out. Recreate the state deterministically instead: the
    # search never gets to close its summary dialog, so it runs into the
    # timeout with that dialog (and the probe it blocks) still open.
    impl.search_timeout = 8
    impl._close_summary_dialog = lambda *a, **k: None
    impl._close_any_lingering_summary_dialog = lambda *a, **k: None
    try:
        with pytest.raises(ResponsaTimeoutError, match="confirmed-finished"):
            client.search(SLOW_QUERY)
    finally:
        impl.search_timeout = original_timeout
        del impl._close_summary_dialog
        del impl._close_any_lingering_summary_dialog
    # The state the next call met in the live failure: Responsa done
    # computing, and the timed-out search's summary dialog open.
    winutil.wait_for(lambda: _summary_dialog(impl), timeout=120, interval=0.5)

    results = client.search(QUERY)

    assert results.hits
    assert _summary_dialog(impl) is None


def _summary_dialog(impl):
    suffix = impl.config.RESULTS_SUMMARY_TITLE_SUFFIX
    for hwnd in winutil.enum_top_level_windows_for_pids([impl._pid]):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == "#32770" \
                and win32gui.GetWindowText(hwnd).endswith(suffix):
            return hwnd
    return None
