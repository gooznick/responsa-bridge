"""Regression tests for the "lost click" failures found in the MCP server log:

  - search(): "None of ('ניהול המאגרים',) became visible within 10s" --
    the databases nav button's click never registered.
  - get_result_text(): "Result 21's text window did not open in time" --
    the user saw the "דילוג למופע" dialog left open with OK never pressed.

Both came from _click_control using a real mouse click (click_input) that
lands on whatever window is on top at that spot. These tests reproduce it
deterministically with a ClickBlockingOverlay (see _overlay.py) covering
the screen: an invisible topmost window that swallows every real click.

    pytest tests/test_click_robustness.py -v

Don't touch the mouse/keyboard while these run.
"""
import pytest
import win32gui

from pywinauto.controls.hwndwrapper import HwndWrapper

from responsa_api import BookScope
from responsa_api.internal import winutil

from _overlay import ClickBlockingOverlay

QUERY = "גשר של ברזל"                       # same fast 6-hit query as test_client.py
SCOPED_QUERY = "סייג לחכמה שתיקה"            # 1 hit in Mishna


def _open_advanced_search(impl):
    cfg = impl.config
    impl._send_command(impl._main_hwnd, cfg.COMMAND_CLOSE_ALL_WINDOWS)
    impl._send_command(impl._main_hwnd, cfg.COMMAND_OPEN_SEARCH)
    impl._switch_to_advanced_search()
    return impl._find_visible_dialog(cfg.SEARCH_DIALOG_TITLES["advanced"])


def _database_manager_visible(impl):
    return impl._visible_dialog_or_none(impl.config.DATABASE_MANAGER_DIALOG_TITLE)


def test_real_click_is_lost_under_overlay_but_click_control_is_not(client):
    """Reproduces the root cause directly (white-box): with another window
    on top, the OLD way of clicking the databases button (a real mouse
    click) never opens "ניהול המאגרים"; _click_control does."""
    impl = client._impl
    cfg = impl.config
    advanced = _open_advanced_search(impl)
    button = winutil.find_child_by_id(advanced, cfg.NAV_DATABASES_BUTTON_ID)
    try:
        with ClickBlockingOverlay() as overlay:
            impl._bring_to_foreground(advanced)
            HwndWrapper(button).click_input()          # the pre-fix behaviour
            with pytest.raises(TimeoutError):
                winutil.wait_for(lambda: _database_manager_visible(impl), timeout=3, interval=0.2)
            assert overlay.clicks >= 1, "the overlay should have swallowed the real click"

            impl._click_control(advanced, cfg.NAV_DATABASES_BUTTON_ID)   # the fix
            tree_dialog = winutil.wait_for(lambda: _database_manager_visible(impl), timeout=5, interval=0.2)
            impl._confirm_database_manager(tree_dialog)
    finally:
        if win32gui.IsWindow(advanced) and win32gui.IsWindowVisible(advanced):
            impl._click_control(advanced, cfg.ADVANCED_CANCEL_BUTTON_ID)


def test_scoped_search_under_overlay(client):
    """The failing search path from the log: explicit book scope (databases
    button, "התחל שוב", tree OK)."""
    with ClickBlockingOverlay():
        results = client.search(SCOPED_QUERY, books=BookScope.MISHNA)
    assert results.hits
    assert all(hit.citation.startswith("משנה") for hit in results.hits)


def test_all_databases_search_under_overlay(client):
    """The default path (books=None -> "בחר הכל" in the tree dialog)."""
    with ClickBlockingOverlay():
        results = client.search(QUERY)
    assert results.hits


def test_get_result_text_under_overlay(client):
    """The failing get_result_text path from the log ("דילוג למופע" OK),
    fetching a result that is not the first one, like the logged failure
    (index 21)."""
    results = client.search(QUERY)
    assert len(results.hits) >= 2
    with ClickBlockingOverlay():
        source = client.get_result_text(results.hits[1].index)
    assert source.text
    skip_title = client._impl.config.SKIP_TO_NUMBER_DIALOG_TITLE
    assert client._impl._visible_dialog_or_none(skip_title) is None, \
        "the skip-to-number dialog must not be left open"


def test_max_hits_search_under_overlay(client):
    """max_hits goes through the Print dialog's "Pages" RADIO button --
    the one control here that is not a push button."""
    with ClickBlockingOverlay():
        results = client.search(QUERY, max_hits=2)
    assert len(results.hits) == 2
    assert results.truncated


def test_no_results_under_overlay(client):
    """The "no results" info box's "לא" button."""
    with ClickBlockingOverlay():
        results = client.search("קשדגכקשדגכקשדגכ")
    assert results.hits == []
    assert results.total_hits == 0
