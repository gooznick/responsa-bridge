"""ResponsaAutomation: the GUI-automation driver behind the public
`ResponsaClient` (see responsa_api/client.py, which is what callers use).

Responsa (Bar-Ilan Responsa Project) has no CLI, SDK, or COM automation
interface -- confirmed by a registry scan for its ProgID/CLSID during
discovery, and its own website has no developer docs. Everything this
module does is GUI automation: driving the same window/toolbar/dialog
controls a human would, discovered by hands-on, read-only probing of the
live app (see scripts/inspect_app.py and scripts/dump_tree.py).

IMPORTANT: the main window's visible toolbar (Search/Windows/Browse/...)
is a third-party component that does NOT respond to any simulated click
-- not hardware input, not message-based clicks, not WM_COMMAND with its
own control ID, not MSAA's default action. Every action that used to go
through those buttons is instead sent as a raw WM_COMMAND with a real
classic-menu command ID straight to the main window (see COMMAND_* in
versions/v33.py) -- this goes through MFC's actual command-routing path
and was confirmed live to work regardless of window focus/foreground
state. Don't reintroduce toolbar-button clicking.

`search()` is confirmed working end to end by the real, unmocked pytest
suite against the live app (`pytest tests/`): COMMAND_OPEN_SEARCH and
COMMAND_CLOSE_ALL_WINDOWS via WM_COMMAND, switching search-dialog mode via
the nav buttons' message-based .click(), typing the query and submitting,
the sources-tree book-selection dialog, the results-summary dialog,
print-to-PDF (including the Save-As dialog -- see
_find_save_dialog_filename_edit's docstring for a real bug that was found
and fixed there), and parsing.py against the resulting PDF. Launching
with the correct working directory (a wrong CWD produces a misleading
"environment variable undefined" error, not a real bug) is exercised
whenever the suite has to start Responsa itself.

Confirmed working by hand, not yet independently re-verified by this
code's own test run: both observed missing-dongle/license failure dialogs
(fail-fast, no recovery -- see exceptions.ResponsaLaunchError).
"""
import os
import re
import subprocess
import tempfile
import time
import uuid
from typing import Optional

import win32api
import win32con
import win32gui
import win32process
import pywintypes
from pywinauto.controls.common_controls import TreeViewWrapper
from pywinauto.controls.hwndwrapper import HwndWrapper

from . import browse_gui, winutil
from ..book_scope import BookScope, BookScopeInput
from ..exceptions import (
    ResponsaError,
    ResponsaFocusError,
    ResponsaInvalidQueryError,
    ResponsaLaunchError,
    ResponsaSearchError,
    ResponsaSessionLockedError,
    ResponsaTimeoutError,
    ResponsaTooManyResultsError,
)
from ..results import BrowseResult, SearchResults, SourceText
from . import browse_tree
from .parsing import parse_results_pdf, parse_source_pdf
from .versions import v33 as default_config


class ResponsaAutomation:
    """Drives one Responsa session. The public `ResponsaClient` holds one
    of these and forwards its calls here -- the user-facing documentation
    of `search()`/`get_result_text()` and the constructor arguments lives
    on `ResponsaClient`; the docstrings below are implementation notes.
    """

    # Rough, empirically-calibrated estimate of export cost per hit
    # (printing + PDF parsing combined), used only to decide whether a
    # search's FULL result set would blow past `time_budget` -- see
    # search()'s `max_hits` auto-shortening. Calibrated from two real
    # observations: a ~1356-hit search (~280 pages, ~7min) and a
    # ~2974-hit search (~743 pages, ~36min print + ~4.5min parse) --
    # per-hit cost was NOT linear between them (roughly 0.33s/hit vs
    # 0.85s/hit), so this deliberately uses the slower, more conservative
    # rate rather than an interpolation: the goal is to avoid
    # underestimating and blowing the budget anyway, not to predict
    # exactly, and it's fine to shorten more aggressively than strictly
    # necessary.
    ESTIMATED_SECONDS_PER_HIT = 1.0
    # Fixed per-search overhead (dialogs, Save-As, etc.) unrelated to hit
    # count, folded into the same estimate.
    ESTIMATED_FIXED_OVERHEAD_SECONDS = 30.0
    # Conservative LOWER bound on hits-per-printed-page (the real
    # 2974-hit/743-page run measured ~4.0/page; assuming fewer hits per
    # page than reality just means over-printing slightly, which is
    # harmless -- assuming MORE would under-print and truncate results
    # short of the requested max_hits, which would be a real bug). Used
    # to convert a hit cap into a page-range cap for the Print dialog.
    MIN_HITS_PER_PAGE = 4
    # Same lower bound when `lines_per_result` is high: each hit then takes
    # more of a page. Measured live on a 143-hit search (full export):
    # 21 lines -> 42 pages (3.4 hits/page), 3 lines (the app's default) ->
    # 16 pages (8.9), 1 line -> 10 pages (14). Up to about 12 lines the
    # plain MIN_HITS_PER_PAGE still holds (~98/(lines+8) hits per page);
    # above that, 2 keeps a safety margin under the 3.4 measured at the
    # maximum of 21.
    MIN_HITS_PER_PAGE_MANY_LINES = 2
    MANY_LINES_THRESHOLD = 12

    def __init__(self, launch_timeout: float = 60,
                 search_timeout: float = 60, pdf_export_timeout: float = 3600,
                 time_budget: Optional[float] = None,
                 pdf_dir: Optional[str] = None, config=None):
        self.config = default_config if config is None else config
        # Also covers waiting for Responsa to become responsive again
        # after restoring whatever result windows were open on its last
        # exit (see _wait_until_responsive) -- bumped from an original
        # 20s default since that restore's duration scales with however
        # many windows a PRIOR session happened to leave open, which this
        # client has no way to know in advance.
        self.launch_timeout = launch_timeout
        self.search_timeout = search_timeout
        # Confirmed live: printing a genuinely large result set (thousands
        # of hits, many hundreds of pages) is just slow in real wall-clock
        # time -- a ~1356-hit search reached ~280 pages at roughly 1.5s/page
        # (~7 minutes), and a ~2974-hit search was observed still printing
        # past page 430 after ~20+ minutes (roughly 3s/page, and apparently
        # slowing further as the job goes on). This is real printer-driver
        # behavior, not something to fix in code, so the default here is a
        # generous ceiling, not an expected normal-case duration (a small
        # search finishes its whole export in well under a minute). When
        # `time_budget` is set, search() uses that instead for whichever
        # single call it applies to.
        self.pdf_export_timeout = pdf_export_timeout
        # How long a single search() call is allowed to take overall
        # (from submitting the query to returning parsed results), or
        # None for no ceiling beyond the individual step timeouts above.
        # If a query's FULL result set is estimated to take longer than
        # this (see ESTIMATED_SECONDS_PER_HIT), search() automatically
        # caps how many hits it actually extracts (see `max_hits` on
        # search() to set that explicitly instead) rather than silently
        # running for however long the real job takes -- and raises
        # ResponsaTimeoutError up front, without starting the export at
        # all, if even the smallest extraction wouldn't fit.
        self.time_budget = time_budget
        self.pdf_dir = pdf_dir or tempfile.gettempdir()
        # The `lines_per_result` of the search currently being exported
        # (None = the app's own setting); read by _limit_print_page_range.
        self._export_lines_per_result: Optional[int] = None

        self._pid: Optional[int] = None
        self._main_hwnd: Optional[int] = None
        # The most recent search()'s results window and Responsa's own
        # reported hit count for it -- used by get_result_text() to know
        # what it can fetch from and to validate `index` client-side
        # before ever touching the GUI. None whenever there's no active
        # result set (before any search(), or after one with zero hits).
        self._last_results_hwnd: Optional[int] = None
        self._last_total_hits: Optional[int] = None
        # Browse: the cached sources tree (loaded on first use) and where
        # in it the caller currently is, as child indexes from the top.
        self.browse_cache_dir = browse_tree.DEFAULT_CACHE_DIR
        self._browse_tree: Optional[browse_tree.BrowseTree] = None
        self._browse_position: browse_tree.IndexPath = ()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "ResponsaAutomation":
        """Attach to a running Responsa instance, or launch a new one.

        Raises ResponsaLaunchError if a "תקלה"/"Application Error" style
        dialog appears before the main window does (missing dongle, etc.)
        -- these are fail-fast on the app's side, so there is nothing to
        click through, only the dialog's text to report.
        """
        cfg = self.config
        pids = winutil.find_pids(cfg.PROCESS_IMAGE_NAME)
        if pids:
            self._pid = pids[0]
        else:
            # Must set cwd explicitly: the desktop shortcut's "Start in"
            # field points here, and launching from any other working
            # directory makes the app unable to find its own config and
            # fail with a misleading "environment variable undefined"
            # error that looks like a bug but isn't one.
            subprocess.Popen([cfg.EXE_PATH], cwd=cfg.WORKING_DIR)
            self._pid = winutil.wait_for(
                lambda: (winutil.find_pids(cfg.PROCESS_IMAGE_NAME) or [None])[-1],
                timeout=self.launch_timeout,
            )

        self._main_hwnd = self._wait_for_main_window_or_raise()
        # Confirmed live: Responsa saves whatever result
        # windows were open when it last exited and restores them on the
        # next launch -- and the main window class appears (and even
        # reports IsWindowEnabled) BEFORE that restore finishes. A
        # real-world crash was traced to this exact gap: search()'s early
        # COMMAND_CLOSE_ALL_WINDOWS/COMMAND_OPEN_SEARCH commands, sent as
        # soon as start() returned, collided with Responsa still busy
        # loading its restored windows. Directly observed via
        # SendMessageTimeout: the main window's message thread genuinely
        # doesn't pump messages (times out) for as long as that restore
        # is in progress -- confirmed a real, non-trivial gap even for
        # just 4 saved windows (~1.7s), and there's no reason to expect a
        # fixed sleep to cover every real case (a user's session could
        # have left far more windows open). Wait for the thread to
        # actually go idle instead of guessing a duration -- same
        # "probe for the real signal, don't guess a fixed wait" principle
        # already used for search()'s own completion detection.
        self._wait_until_responsive(self._main_hwnd, timeout=self.launch_timeout)
        return self

    def _wait_for_main_window_or_raise(self) -> int:
        cfg = self.config

        def check():
            # Scan the whole window list before deciding: if the main
            # window is present anywhere in this pass, it wins over any
            # dialog, regardless of enumeration order. Needed because
            # EnumWindows order isn't guaranteed, and when attaching to an
            # already-running instance there can be leftover dialogs open
            # (e.g. a search dialog left open from prior use) alongside a
            # perfectly healthy main window -- those must not be mistaken
            # for a fatal launch-error dialog.
            dialog_hwnd = None
            for hwnd in winutil.enum_top_level_windows_for_pids([self._pid]):
                try:
                    cls = win32gui.GetClassName(hwnd)
                    if cls == cfg.MAIN_WINDOW_CLASS:
                        return ("main", hwnd)
                    if dialog_hwnd is None and cls == "#32770" and win32gui.IsWindowVisible(hwnd):
                        # Windows' own generic hang-detector ghost dialog
                        # ("Loading. Please wait..." / "Terminate the
                        # process") can appear-and-become-visible briefly
                        # while a slow app is still starting up -- confirmed
                        # live, it was mistaken for a fatal launch-error
                        # dialog here even though the app went on to start
                        # completely normally. It's not a Responsa dialog at
                        # all, so ignore it and keep waiting for the real
                        # main window.
                        if self._read_dialog_static_texts(hwnd) == cfg.HANG_DETECTOR_TEXT:
                            continue
                        dialog_hwnd = hwnd
                except pywintypes.error:
                    # Confirmed live (via hard_reset()'s
                    # kill-then-relaunch, which churns through far more
                    # short-lived windows than attaching to an already-
                    # settled process ever does): enum_top_level_windows_
                    # for_pids snapshots the window LIST first, so a
                    # window (or, via _read_dialog_static_texts above, one
                    # of a dialog's CHILD windows -- the ghost dialog this
                    # very branch is guarding against is exactly this kind
                    # of short-lived window) can be destroyed between being
                    # listed and being queried here, and GetClassName/
                    # GetWindowText then raise "Invalid window handle"
                    # instead of returning anything (unlike IsWindowVisible,
                    # which safely returns False for a dead handle instead
                    # of raising). It's gone, so it's neither the main
                    # window nor a real dialog; skip it and keep looking
                    # rather than letting the whole check blow up.
                    continue
            if dialog_hwnd is not None:
                return ("dialog", dialog_hwnd)
            return None

        try:
            kind, hwnd = winutil.wait_for(check, timeout=self.launch_timeout)
        except TimeoutError as e:
            raise ResponsaTimeoutError(
                f"{cfg.MAIN_WINDOW_CLASS} did not appear within "
                f"{self.launch_timeout}s"
            ) from e

        if kind == "dialog":
            title = win32gui.GetWindowText(hwnd)
            text = self._read_dialog_static_texts(hwnd)
            raise ResponsaLaunchError(title or "(untitled dialog)", text)

        return hwnd

    @staticmethod
    def _wait_until_responsive(hwnd: int, timeout: float, stable_for: float = 2.0, interval: float = 0.3):
        """Poll until `hwnd`'s owning thread is actually pumping messages
        -- i.e. genuinely idle, not just "the window exists" -- and stays
        that way for `stable_for` seconds in a row before declaring it
        ready. Posts WM_NULL via SendMessageTimeout(SMTO_ABORTIFHUNG):
        this returns promptly once the thread is free, and raises a
        pywintypes.error (winerror 1460, "the timeout period expired")
        while it's still busy with synchronous work -- confirmed live,
        this is exactly what happens while Responsa is restoring a prior
        session's result windows on launch (see the call site in
        start()).

        A single successful check is NOT enough: confirmed live, this
        restore process can have brief responsive gaps between loading
        each individual saved window, so a one-shot check can catch the
        thread mid-restore and declare it ready too early -- reproduced
        directly (a crash still happened even after adding the one-shot
        version of this check; a generous FIXED sleep before proceeding
        did avoid it, confirming this really is a timing gap and not a
        separate bug, which is what justified switching to a debounced
        "stay responsive for N seconds straight" check instead of either
        a one-shot probe or a guessed fixed duration -- same principle as
        _wait_for_results_window_to_settle elsewhere in this module).
        """
        deadline = time.time() + timeout
        responsive_since = None
        while True:
            try:
                win32gui.SendMessageTimeout(hwnd, win32con.WM_NULL, 0, 0, win32con.SMTO_ABORTIFHUNG, 2000)
                now = time.time()
                if responsive_since is None:
                    responsive_since = now
                elif now - responsive_since >= stable_for:
                    return
            except pywintypes.error:
                responsive_since = None
            if time.time() >= deadline:
                raise ResponsaTimeoutError(f"{hwnd} did not become responsive within {timeout}s")
            time.sleep(interval)

    @staticmethod
    def _read_dialog_static_texts(hwnd: int) -> str:
        """Concatenate a dialog's Static child texts -- covers both the
        "תקלה" family (message in a Static control) and "Application
        Error" (same pattern), without needing to hardcode which control
        ID holds the message for each known dialog variant.
        """
        texts = []

        def cb(child, _):
            if win32gui.GetClassName(child) == "Static":
                text = win32gui.GetWindowText(child)
                if text:
                    texts.append(text)
            return True

        win32gui.EnumChildWindows(hwnd, cb, None)
        return " / ".join(texts)

    def close(self):
        """Stop using Responsa. It is deliberately left RUNNING, even if
        this client was the one that launched it.

        This used to post WM_CLOSE to a Responsa we had launched. Removed
        because it made every `with ResponsaClient()` block
        end by closing the app, so the next one paid a full relaunch
        (~40s including Responsa's own restore of its saved windows) -- or
        worse, attached to the still-closing process and timed out waiting
        for a main window that was disappearing. Leaving it open is also
        what makes back-to-back searches cheap.
        """
        self._pid = None
        self._main_hwnd = None
        self._last_results_hwnd = None
        self._last_total_hits = None

    def hard_reset(self, clear_windows: bool = True) -> "ResponsaAutomation":
        """Actually quit Responsa (every running instance, not just the
        one this client is attached to) and start a fresh one -- for
        recovering when the app is stuck or behaving oddly, which
        close()'s plain detach can't fix since it deliberately never
        touches the real process (see its docstring). This DOES affect
        the real app, so don't call it if something else might be
        relying on the current session staying up.

        Tries a graceful close (WM_CLOSE, the same request a user
        closing the window sends) before force-terminating anything
        still alive after a timeout -- see winutil.terminate_process.

        `clear_windows` (default True): once the fresh instance is
        responsive, also closes every window it restores from its last
        session (start()'s docstring covers that restore) so hard_reset()
        leaves a genuinely empty slate, not just "a responsive app that
        happens to have whatever windows it had before". False skips
        that and leaves the restored windows as Responsa put them.
        """
        cfg = self.config
        for pid in winutil.find_pids(cfg.PROCESS_IMAGE_NAME):
            winutil.terminate_process(pid, graceful_hwnd=self._main_hwnd if pid == self._pid else None)
        self.close()
        self.start()
        if clear_windows:
            self._send_command(self._main_hwnd, cfg.COMMAND_CLOSE_ALL_WINDOWS)
            time.sleep(0.5)
        return self

    # -- search -------------------------------------------------------------

    def search(
        self,
        query: str,
        books: Optional[BookScopeInput] = None,
        search_all_databases: bool = True,
        max_hits: Optional[int] = None,
        lines_per_result: Optional[int] = None,
    ) -> SearchResults:
        """Run one query through Advanced Search mode and return structured
        hits (the user-facing contract is documented on
        ResponsaClient.search).

        `query` is typed into the query box as-is -- this module does not
        validate or build Responsa's query syntax. `max_hits` caps how many
        hits are printed/extracted by limiting the Print dialog's page
        range rather than printing everything (that is what keeps export
        time bounded for a search matching thousands of results). If
        `max_hits` is None and `self.time_budget` is set, the FULL result
        set's export time is estimated (see ESTIMATED_SECONDS_PER_HIT) as
        soon as the hit count is known (from the results window's title,
        before any printing starts); if that estimate exceeds the budget a
        `max_hits` is chosen automatically to fit, and if even the smallest
        extraction wouldn't fit, ResponsaTimeoutError is raised before the
        export starts at all.

        `lines_per_result`, if given, is applied to the results window
        through its "מספר שורות" dialog (see _set_lines_per_result) before
        extraction; None leaves the app's own setting untouched.
        """
        if self._main_hwnd is None:
            raise ResponsaError("start() must be called before search()")
        if lines_per_result is not None:
            self._validate_lines_per_result(lines_per_result)

        cfg = self.config
        # This search's own COMMAND_CLOSE_ALL_WINDOWS below is about to
        # invalidate any results window a PRIOR search left tracked here
        # anyway -- clear it up front so get_result_text() fails clearly
        # if called mid-search rather than possibly racing against it.
        self._last_results_hwnd = None
        self._last_total_hits = None
        self._dismiss_leftover_dialogs()

        # Pauses between these early steps: confirmed live
        # that RESPONSA.exe itself can become unstable/crash (not just a
        # UI-state race) when too many automated actions are chained with
        # no pause between them in one continuous run, even though the
        # identical sequence run as separate, naturally-paced commands
        # never had this problem. These sleeps exist to reproduce that
        # natural pacing.
        self._send_command(self._main_hwnd, cfg.COMMAND_CLOSE_ALL_WINDOWS)
        time.sleep(0.5)
        self._send_command(self._main_hwnd, cfg.COMMAND_OPEN_SEARCH)
        time.sleep(0.5)
        self._switch_to_advanced_search()
        time.sleep(0.5)

        advanced_hwnd = self._find_visible_dialog(cfg.SEARCH_DIALOG_TITLES["advanced"])
        # Apply book scope BEFORE typing the query: "התחל שוב" (Start
        # Over), used internally to reset the tree's checkboxes, resets
        # the WHOLE search form, not just the tree -- confirmed live, the
        # query box ended up empty when the query was set first. Doing
        # scope selection first and the query last avoids that.
        if books is None and search_all_databases:
            # Route the common default case through the same deterministic
            # tree-based path as an explicit BookScope.ALL -- see
            # _apply_book_scope's docstring for why just the checkbox
            # isn't reliable on its own.
            books = BookScope.ALL
        if books is not None:
            self._apply_book_scope(advanced_hwnd, books)
        else:
            # search_all_databases=False with no `books` given: an
            # advanced/edge-case combination with no explicit scope to
            # apply, so this just unchecks "search all" and leaves the
            # sources tree exactly as it currently is (whatever a prior
            # call in this session left it, or the app's own last-used
            # state). Prefer passing `books` explicitly instead of relying
            # on this.
            self._set_checkbox(advanced_hwnd, cfg.ADVANCED_SEARCH_ALL_DBS_CHECKBOX_ID, False)
        self._set_edit_text(advanced_hwnd, cfg.ADVANCED_QUERY_EDIT_ID, query)
        if books is not None:
            # Confirmed live/reproducible: submitting immediately after
            # closing the book-scope tree dialog silently loses the
            # search (the search dialog vanishes, nothing else -- no
            # results, no summary, no info dialog, ever -- yet the app
            # remains otherwise responsive). Running the exact same steps
            # as separate, naturally-paced commands works every time; only
            # chaining them with no delay in one go reproduces the
            # failure. A short buffer avoids it.
            time.sleep(0.5)
        # Baseline right before submitting: a leftover results window from
        # an earlier search (e.g. one COMMAND_CLOSE_ALL_WINDOWS didn't
        # finish closing yet -- that command isn't guaranteed synchronous
        # either) must not be mistaken for THIS search's outcome. Real bug,
        # reproduced live: without this, _wait_for_search_outcome could
        # match a pre-existing results window instantly and return
        # ("results", stale_hwnd) before the current search's own summary
        # dialog ever had a chance to appear, silently skipping past it.
        pre_existing_results_hwnds = {
            h for h in winutil.find_descendants(self._main_hwnd)
            if win32gui.GetClassName(h).startswith(cfg.RESULTS_WINDOW_CLASS_PREFIX)
        }
        self._click_control(advanced_hwnd, cfg.ADVANCED_SUBMIT_BUTTON_ID)

        outcome, results_hwnd = self._wait_for_search_outcome(exclude_results_hwnds=pre_existing_results_hwnds)
        if outcome == "no_results":
            return SearchResults(query=query, hits=[], raw_text="", total_hits=0)
        # outcome == "results": _wait_for_search_outcome only returns this
        # once it has POSITIVELY confirmed the search is finished (see its
        # docstring for the Ctrl+P probe this relies on instead of guessing
        # a duration) -- any results-summary dialog has already been
        # closed along the way, so there's nothing left to do here but
        # move on to extraction.
        self._ensure_expanded_display()

        # Poll briefly: the title is populated asynchronously (same race as
        # the source window's title in get_result_text). Confirmed live
        # it can also stay EMPTY for good -- a 1-hit Bavli
        # search's results window had no title text at all -- so a miss
        # here is handled after extraction instead of being an error.
        try:
            reported_count = winutil.wait_for(
                lambda: self._parse_hit_count_from_title(win32gui.GetWindowText(results_hwnd)),
                timeout=3, interval=0.2,
            )
        except TimeoutError:
            reported_count = None
        effective_max_hits = max_hits
        if effective_max_hits is None and self.time_budget is not None and reported_count is not None:
            estimated_seconds = (
                reported_count * self.ESTIMATED_SECONDS_PER_HIT + self.ESTIMATED_FIXED_OVERHEAD_SECONDS
            )
            if estimated_seconds > self.time_budget:
                affordable = int(
                    (self.time_budget - self.ESTIMATED_FIXED_OVERHEAD_SECONDS) / self.ESTIMATED_SECONDS_PER_HIT
                )
                if affordable < 1:
                    raise ResponsaTimeoutError(
                        f"Query matched {reported_count} results; even extracting "
                        f"just one is estimated to take longer than the configured "
                        f"time_budget ({self.time_budget}s). Narrow the query/scope, "
                        f"raise time_budget, or pass an explicit max_hits."
                    )
                effective_max_hits = affordable

        pdf_export_timeout = self.pdf_export_timeout
        if self.time_budget is not None:
            pdf_export_timeout = min(pdf_export_timeout, self.time_budget)

        # Tracked BEFORE printing (not after): get_result_text() only
        # needs the results window and the app's own reported count, both
        # already known at this point -- no reason to make it wait on (or
        # be affected by the outcome of) this search's own PDF export.
        self._last_results_hwnd = results_hwnd
        self._last_total_hits = reported_count

        if lines_per_result is not None:
            self._set_lines_per_result(results_hwnd, lines_per_result)

        self._export_lines_per_result = lines_per_result
        try:
            pdf_path = self._export_to_pdf(results_hwnd, max_hits=effective_max_hits,
                                            pdf_export_timeout=pdf_export_timeout)
        finally:
            self._export_lines_per_result = None
        results = parse_results_pdf(pdf_path)
        if reported_count is None and (effective_max_hits is None or len(results.hits) < effective_max_hits):
            # No count in the title, but fewer hits came out than the cap
            # (or there was no cap), so the extraction got all of them:
            # the real count is known after all. Previously this case
            # reported total_hits=None AND truncated=True for a search
            # that had returned its one and only hit.
            reported_count = len(results.hits)
            self._last_total_hits = reported_count
        results.total_hits = reported_count
        if effective_max_hits is not None:
            # The page-range cap above is deliberately generous (rounds up
            # using MIN_HITS_PER_PAGE, a lower bound), so a page can carry
            # a few more hits than strictly asked for -- trim to the exact
            # requested/computed count.
            results.truncated = reported_count is None or effective_max_hits < reported_count
            results.hits = results.hits[:effective_max_hits]
        return results

    # -- fetching one result's full text ---------------------------------

    def get_result_text(self, index: int) -> SourceText:
        """Fetch the full text of result `index` (1-based, matching
        `Hit.index` from the most recent search()). Raises ResponsaError
        if no search has been run yet, its results window is gone, or
        `index` is out of range for that search's total hit count.

        Mirrors doing this by hand: right-click a result, "Skip to
        number" to bring it to the front of the results list, then open
        it. Confirmed live:
          - "Skip to number" is a real menu command (COMMAND_SKIP_TO_NUMBER,
            "תצוגה" -> "דלג למספר", found via the same read-only classic-menu
            dump technique every other COMMAND_* here came from) -- driven
            the same reliable way as everything else in this module,
            rather than by simulating a right-click and hit-testing a row.
          - Confirming it only repositions the results window (its title's
            visible range shifts, e.g. "1-6" -> "3-6") -- it does NOT open
            anything by itself.
          - Sending Enter to the (still-focused) results window DOES open
            the now-first item directly, in a brand new window -- no
            simulated mouse double-click at a computed row position
            needed at all.
          - That new window is the exact same kind of custom-drawn MDI
            child as the results window itself (same class prefix), and
            _export_to_pdf works completely unmodified against it --
            confirmed live by running it directly against one.
        """
        cfg = self.config
        if self._last_results_hwnd is None or not win32gui.IsWindow(self._last_results_hwnd):
            raise ResponsaError("No active search results to fetch from -- call search() first")
        if self._last_total_hits is not None and not (1 <= index <= self._last_total_hits):
            raise ResponsaError(
                f"Result index {index} is out of range (1-{self._last_total_hits})"
            )
        self._dismiss_leftover_dialogs()

        # POST, not send -- see _post_command's docstring: this specific
        # command opens a modal dialog, and a blocking send would deadlock
        # this thread against the dialog it's about to go find below.
        #
        # The command is routed by MFC to the ACTIVE MDI child, so the
        # results window has to be the active one, and a posted command
        # that gets dropped (seen once through the MCP server: the dialog
        # never opened, 10s timeout, on a fetch right after a successful
        # one) is posted again after re-activating it.
        attempts = 3
        for attempt in range(attempts):
            self._activate_mdi_child(self._last_results_hwnd)
            self._bring_to_foreground(self._last_results_hwnd)
            self._post_command(self._main_hwnd, cfg.COMMAND_SKIP_TO_NUMBER)
            try:
                dialog_hwnd = self._find_visible_dialog(
                    cfg.SKIP_TO_NUMBER_DIALOG_TITLE,
                    timeout=10 if attempt == attempts - 1 else 4)
                break
            except ResponsaTimeoutError:
                if attempt == attempts - 1:
                    raise
        self._set_edit_text(dialog_hwnd, cfg.SKIP_TO_NUMBER_EDIT_ID, str(index))
        # Verify the OK really closed the dialog. Real bug:
        # the OK click was lost, the dialog stayed open on screen (the
        # user saw it and pressed OK by hand), and nothing noticed until
        # the 60s wait for the source window below timed out.
        try:
            self._click_and_wait(
                dialog_hwnd, cfg.SKIP_TO_NUMBER_OK_BUTTON_ID,
                lambda: (not win32gui.IsWindow(dialog_hwnd)
                         or not win32gui.IsWindowVisible(dialog_hwnd)) or None,
                what=f"OK in {cfg.SKIP_TO_NUMBER_DIALOG_TITLE!r} (the dialog stayed open)",
            )
        except ResponsaError:
            # Don't leave it open to block the next call.
            if win32gui.IsWindow(dialog_hwnd):
                self._click_control(dialog_hwnd, cfg.SKIP_TO_NUMBER_CANCEL_BUTTON_ID)
            raise

        # Exclude every currently-open window of this class (the results
        # window included) so only the newly-opened source window matches
        # below -- same pattern as search()'s pre_existing_results_hwnds.
        pre_existing = {
            h for h in winutil.find_descendants(self._main_hwnd)
            if win32gui.GetClassName(h).startswith(cfg.RESULTS_WINDOW_CLASS_PREFIX)
        }
        # Re-bring to foreground: the dialog interactions just above
        # (_set_edit_text/_click_control) each foreground the DIALOG, not
        # the results window, so it needs reclaiming right before typing.
        self._bring_to_foreground(self._last_results_hwnd)
        self._require_foreground(self._last_results_hwnd)
        wrapper = HwndWrapper(self._last_results_hwnd)
        self._safe_set_focus(wrapper)
        self._send_keys(win32con.VK_RETURN)

        def find_source_window():
            for h in winutil.find_descendants(self._main_hwnd):
                if h not in pre_existing and win32gui.GetClassName(h).startswith(cfg.RESULTS_WINDOW_CLASS_PREFIX):
                    return h
            return None

        try:
            source_hwnd = winutil.wait_for(find_source_window, timeout=self.search_timeout)
        except TimeoutError as e:
            raise ResponsaTimeoutError(
                f"Result {index}'s text window did not open in time. "
                f"Visible Responsa windows: {self._describe_visible_windows()}"
            ) from e
        # Poll, don't read once: confirmed live, this window's title can
        # still be empty for a moment right after the hwnd itself first
        # appears -- the same class of async-population race seen
        # elsewhere in this app (the printer combo box, the Save dialog's
        # filename field, etc.). Fall back to None rather than raise if
        # it genuinely never populates -- a missing citation shouldn't
        # block getting the text itself.
        try:
            citation = winutil.wait_for(
                lambda: win32gui.GetWindowText(source_hwnd) or None, timeout=5, interval=0.2
            )
        except TimeoutError:
            citation = None

        try:
            pdf_path = self._export_to_pdf(source_hwnd)
        finally:
            # Close by this specific hwnd, never by "close the active
            # window" -- see the plan's "not closing the results window"
            # discussion: a focus/activation race could otherwise make a
            # "close active window" command hit the original results
            # window instead of this one.
            if win32gui.IsWindow(source_hwnd):
                win32gui.PostMessage(source_hwnd, win32con.WM_CLOSE, 0, 0)
                winutil.wait_for(lambda: not win32gui.IsWindow(source_hwnd), timeout=5, interval=0.2)

        if not win32gui.IsWindow(self._last_results_hwnd):
            raise ResponsaError(
                "Results window closed unexpectedly while fetching result text -- "
                "call search() again before any further get_result_text() calls"
            )

        return parse_source_pdf(pdf_path, citation=citation)

    # -- Browse ("עיון"): opening one text of the sources tree -------------

    def browse(self, path=None, exact: bool = False) -> BrowseResult:
        """One step of a walk down Responsa's sources tree (see
        ResponsaClient.browse for the user-facing description). Everything
        except fetching a leaf's text is answered from the cached tree
        without touching Responsa."""
        tree = self._browse_tree
        if tree is None:
            tree = self._browse_tree = browse_tree.BrowseTree(self.browse_cache_dir)
        if path is None:
            self._browse_position = ()
            return BrowseResult(options=tree.child_names(()))
        steps = [path] if isinstance(path, str) else list(path)

        res = browse_tree.resolve(tree, self._browse_position, steps, exact)
        here = tree.names_along(res.position)
        if res.error is not None:
            return BrowseResult(path=here, error=res.error, message=res.message,
                                failed_step=res.failed_step, candidates=res.candidates)
        if res.leaf is None:
            self._browse_position = res.position
            return BrowseResult(path=here, options=tree.child_names(res.position))

        if self._main_hwnd is None:
            raise ResponsaError("Responsa is not started -- call start() before fetching a text")
        names = tree.names_along(res.leaf)
        text = self.browse_get_text(res.leaf, names)
        # Only now does the position move (all or nothing): to the section
        # containing the text.
        self._browse_position = res.position
        return BrowseResult(path=here, text=text, text_path=names)

    def browse_get_text(self, index_path, names) -> SourceText:
        """Open the text at `index_path` in Responsa's Browse dialog and
        return its full content. `names` are the cached names along that
        path, used to verify the live tree still matches the cache (see
        browse_gui.find_item). Never called for a section, only for a
        leaf (an entry with no "+").

        Confirmed live: moving the tree's real highlight to a leaf with
        real Home/Down key presses, then a real Enter (see
        browse_gui.open_leaf_via_keyboard -- the user's own suggestion,
        after both a message-based selection and a real double-click
        proved unreliable) makes the Browse dialog close itself and opens
        the text in a new MDI window of the same custom-drawn kind as a
        results window, which _export_to_pdf prints unmodified -- just
        that leaf's own text (e.g. one verse, one Mishnah), not its
        containing chapter.
        """
        cfg = self.config
        # Confirmed live: if the target text's window is already open (left
        # over from an earlier browse()/search(), possibly a previous
        # session Responsa restored on launch), double-clicking the leaf
        # just brings that EXISTING window to the front rather than opening
        # a new one -- so waiting for a "new" window below would time out
        # forever. Closing everything first (same idiom search() already
        # uses before opening its dialog) guarantees a fresh window every
        # time. This also invalidates any results window a prior search()
        # left tracked -- clear it, same reasoning as search()'s own use
        # of this command.
        self._last_results_hwnd = None
        self._last_total_hits = None
        self._dismiss_leftover_dialogs()
        self._send_command(self._main_hwnd, cfg.COMMAND_CLOSE_ALL_WINDOWS)
        time.sleep(0.5)
        dialog_hwnd, tree_hwnd = browse_gui.open_browse_tree(self)
        # The keyboard input below is REAL simulated input (see
        # browse_gui.open_leaf_via_keyboard's docstring for why), which
        # goes wherever OS foreground/focus actually is -- unlike every
        # other action in this method, which is message-based and
        # hwnd-targeted regardless of foreground.
        self._bring_to_foreground(dialog_hwnd)
        self._require_foreground(dialog_hwnd)
        reader = browse_gui.TreeReader(tree_hwnd)
        try:
            browse_gui.reset_tree(reader)
            pre_existing = {
                h for h in winutil.find_descendants(self._main_hwnd)
                if win32gui.GetClassName(h).startswith(cfg.RESULTS_WINDOW_CLASS_PREFIX)
            }
            browse_gui.open_leaf_via_keyboard(self, reader, dialog_hwnd, tree_hwnd, index_path, names)
        finally:
            reader.close()

        def find_text_window():
            for h in winutil.find_descendants(self._main_hwnd):
                if h not in pre_existing and win32gui.GetClassName(h).startswith(cfg.RESULTS_WINDOW_CLASS_PREFIX):
                    return h
            return None

        try:
            text_hwnd = winutil.wait_for(find_text_window, timeout=self.search_timeout)
        except TimeoutError as e:
            raise ResponsaTimeoutError(f"The text window for {names[-1]!r} did not open in time") from e
        # Poll for the title, same async-population race as in get_result_text.
        try:
            citation = winutil.wait_for(
                lambda: win32gui.GetWindowText(text_hwnd) or None, timeout=5, interval=0.2
            )
        except TimeoutError:
            citation = None

        try:
            pdf_path = self._export_to_pdf(text_hwnd)
        finally:
            # By its own hwnd, never "close the active window" (see get_result_text).
            if win32gui.IsWindow(text_hwnd):
                win32gui.PostMessage(text_hwnd, win32con.WM_CLOSE, 0, 0)
                winutil.wait_for(lambda: not win32gui.IsWindow(text_hwnd), timeout=5, interval=0.2)
        return parse_source_pdf(pdf_path, citation=citation)

    def _switch_to_advanced_search(self):
        """Click the Advanced Search nav button on whichever of the four
        preloaded search dialogs is currently visible (they all share the
        same nav button IDs on their right sidebar)."""
        cfg = self.config
        current = self._find_visible_dialog(*cfg.SEARCH_DIALOG_TITLES.values())
        self._click_control(current, cfg.NAV_ADVANCED_BUTTON_ID)

    def _apply_book_scope(self, advanced_hwnd: int, books: BookScopeInput):
        """Resolve `books` (a BookScope, a free-text tree-node name, or a
        list of either) to a set of sources-tree node texts, then open the
        tree dialog, reset it, check exactly those nodes, and confirm.

        BookScope.ALL (alone or in a list) also goes through the tree
        (Select All) rather than just the "search in all databases"
        checkbox. Checking the checkbox alone was tried first and is NOT
        reliable on its own: confirmed live, a search run with the
        checkbox checked still came back scoped to whatever a handful of
        specific nodes had been left checked by a PRIOR call, producing a
        drastically different (and wrong) hit count. Explicitly selecting
        every node is the only way found so far to get a deterministic
        "search everything" regardless of leftover tree state from
        earlier calls (manual or automated) in the same session.
        """
        cfg = self.config
        items = books if isinstance(books, list) else [books]

        if BookScope.ALL in items:
            self._set_checkbox(advanced_hwnd, cfg.ADVANCED_SEARCH_ALL_DBS_CHECKBOX_ID, True)
            time.sleep(0.3)
            tree_dialog_hwnd = self._open_database_manager(advanced_hwnd)
            self._click_control(tree_dialog_hwnd, cfg.TREE_SELECT_ALL_BUTTON_ID)
            time.sleep(0.3)
            self._confirm_database_manager(tree_dialog_hwnd)
            return

        target_texts = set()
        for item in items:
            if isinstance(item, BookScope):
                target_texts.update(cfg.BOOK_SCOPE_NODE_TEXTS[item.value])
            else:
                target_texts.add(item)

        self._set_checkbox(advanced_hwnd, cfg.ADVANCED_SEARCH_ALL_DBS_CHECKBOX_ID, False)
        time.sleep(0.3)
        tree_dialog_hwnd = self._open_database_manager(advanced_hwnd)
        tree_hwnd = winutil.find_child_by_id(
            tree_dialog_hwnd, cfg.SOURCES_TREE_CONTROL_ID, recursive=True
        )
        tree = TreeViewWrapper(tree_hwnd)

        # Confirmed live: this button clears every checkbox in the tree.
        self._click_control(tree_dialog_hwnd, cfg.TREE_START_OVER_BUTTON_ID)
        time.sleep(0.3)

        remaining = set(target_texts)
        self._check_tree_nodes(tree, remaining)
        if remaining:
            raise ResponsaError(
                f"Could not find sources-tree node(s) for book scope: {sorted(remaining)!r}"
            )

        time.sleep(0.3)
        self._click_control(tree_dialog_hwnd, cfg.TREE_OK_BUTTON_ID)
        time.sleep(0.3)
        # Not _confirm_database_manager here: on this path an OK that
        # Responsa REJECTS ("select at least one database", handled just
        # below) legitimately leaves the tree dialog open, so "dialog
        # closed" isn't the right success condition to wait/retry on.

        # Defensive: if none of our clicks actually registered as a
        # selection for some unforeseen reason, Responsa shows a warning
        # rather than silently reopening -- surface that as a clear error
        # instead of leaving the caller to time out later in
        # _wait_for_search_outcome with no idea why.
        warning_hwnd = self._find_no_databases_selected_warning()
        if warning_hwnd is not None:
            self._click_control(warning_hwnd, cfg.NO_DATABASES_SELECTED_DISMISS_BUTTON_ID)
            raise ResponsaError(
                f"Responsa rejected the book scope {sorted(target_texts)!r} with "
                "'select at least one database' -- the requested scope may not "
                "have resolved to a selectable tree node after all."
            )
        # No rejection: the OK must actually have closed the tree dialog
        # (closing may finish after the click returns -- and the click
        # could be lost outright before this became message-based).
        self._confirm_database_manager(tree_dialog_hwnd, already_clicked=True)

    def _open_database_manager(self, advanced_hwnd: int) -> int:
        cfg = self.config
        hwnd = self._click_and_wait(
            advanced_hwnd, cfg.NAV_DATABASES_BUTTON_ID,
            lambda: self._visible_dialog_or_none(cfg.DATABASE_MANAGER_DIALOG_TITLE),
            what=f"the databases button ({cfg.DATABASE_MANAGER_DIALOG_TITLE!r} never opened)",
        )
        time.sleep(0.3)
        return hwnd

    def _confirm_database_manager(self, tree_dialog_hwnd: int, already_clicked: bool = False):
        """Click the tree dialog's OK (unless the caller already did) and
        make sure the dialog really closed, clicking once more if not."""
        cfg = self.config

        def closed():
            return (not win32gui.IsWindow(tree_dialog_hwnd)
                    or not win32gui.IsWindowVisible(tree_dialog_hwnd)) or None

        if already_clicked:
            try:
                winutil.wait_for(closed, timeout=5, interval=0.1)
                time.sleep(0.3)
                return
            except TimeoutError:
                pass  # fall through: click it (again)
        self._click_and_wait(
            tree_dialog_hwnd, cfg.TREE_OK_BUTTON_ID, closed,
            what=f"OK in {cfg.DATABASE_MANAGER_DIALOG_TITLE!r} (the dialog stayed open)",
        )
        time.sleep(0.3)

    def _find_no_databases_selected_warning(self, timeout: float = 3) -> Optional[int]:
        cfg = self.config

        def check():
            for hwnd in winutil.enum_top_level_windows_for_pids([self._pid]):
                if not win32gui.IsWindowVisible(hwnd) or win32gui.GetClassName(hwnd) != "#32770":
                    continue
                if win32gui.GetWindowText(hwnd) != "":
                    continue
                for child in winutil.find_descendants(hwnd, class_name="Static"):
                    if win32gui.GetWindowText(child) == cfg.NO_DATABASES_SELECTED_HEADING:
                        return hwnd
            return None

        try:
            return winutil.wait_for(check, timeout=timeout, interval=0.2)
        except TimeoutError:
            return None

    @staticmethod
    def _check_tree_nodes(tree: "TreeViewWrapper", remaining: set, max_depth: int = 2):
        """Check every tree node whose text is in `remaining` (removing it
        from the set as found), searching up to `max_depth` levels deep --
        deep enough for every current BOOK_SCOPE_NODE_TEXTS entry (all are
        top-level categories or their direct children), without needlessly
        expanding into chapter/verse-level enumerations further down.

        Uses `.click(where="icon")` (message-based) to toggle the
        checkbox. This tree's checkboxes are custom-drawn as the item's
        regular icon (confirmed live via a TVM_HITTEST scan across the
        whole row: there is no TVHT_ONITEMSTATEICON region at all, only
        TVHT_ONITEMICON), not real TVS_CHECKBOXES state images -- two
        things that look like the "obvious" fix were tried and both
        failed before finding this: pywinauto's `.click(where="check")`
        raises "Area ('check') not found" (no such region exists), and
        directly writing the state via TVM_SETITEM (mirroring how
        `is_checked()` reads it) appeared to succeed -- `is_checked()`
        read back True -- but Responsa's own "select at least one
        database" validation still fired when confirming, meaning that
        state bit isn't what the app's selection logic actually uses;
        only a real click on the icon toggles its real internal state.
        """
        def walk(nodes, depth):
            for node in nodes:
                if not remaining:
                    return
                text = node.text()
                if text in remaining:
                    node.click(where="icon")
                    time.sleep(0.2)
                    remaining.discard(text)
                if depth < max_depth:
                    node.expand()
                    walk(node.children(), depth + 1)

        walk(tree.roots(), 1)

    def _find_visible_dialog(self, *titles: str, timeout: float = 10) -> int:
        title_set = set(titles)

        def check():
            for hwnd in winutil.enum_top_level_windows_for_pids([self._pid]):
                if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) in title_set:
                    return hwnd
            return None

        try:
            return winutil.wait_for(check, timeout=timeout)
        except TimeoutError as e:
            raise ResponsaTimeoutError(
                f"None of {titles!r} became visible within {timeout}s. "
                f"Visible Responsa windows: {self._describe_visible_windows()}"
            ) from e

    def _visible_dialog_or_none(self, *titles: str) -> Optional[int]:
        """Non-waiting variant of _find_visible_dialog, for use as a
        _click_and_wait condition."""
        title_set = set(titles)
        for hwnd in winutil.enum_top_level_windows_for_pids([self._pid]):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) in title_set:
                return hwnd
        return None

    def _wait_for_search_outcome(self, timeout: Optional[float] = None, exclude_results_hwnds=None):
        """Wait for whatever happens after submitting a search, and
        return ("results", results_hwnd) once the search is CONFIRMED
        finished (any results-summary dialog already closed along the
        way), or ("no_results", None).

        `exclude_results_hwnds` must be the set of results-window hwnds
        that already existed right before this search was submitted --
        see the long comment at the call site in search() for why this
        matters (a leftover window from a previous search, not yet fully
        closed, would otherwise be mistaken for this search's own
        outcome).

        Completion is detected by actually probing with Ctrl+P (see
        _probe_print_dialog) rather than inferring it from UI timing.
        Two earlier approaches were tried and both failed live:
          - a fixed sleep before checking for the summary dialog: can't
            distinguish "still searching" from "done, no summary coming"
            when actual search duration ranges from ~2s to well past 15s
            depending on scope/query size.
          - waiting for the results window's title (e.g. "נמצאו 15
            תוצאות") to stop changing for a couple of seconds, then
            polling briefly for the summary dialog: still just a guessed
            settle window under a different name -- confirmed live, a
            slow multi-source search can leave the title unchanged for
            longer than any reasonable guess before the count jumps again
            and the real summary finally appears, so this still
            mis-fired as "done" too early.
        Ctrl+P doesn't have that ambiguity: confirmed live, it's a silent
        no-op while Responsa is still genuinely busy searching, no matter
        how long that takes, and only actually opens the Print dialog
        once it's truly idle -- so "did a Print dialog open" is a real,
        ground-truth completion signal instead of a guess.

        Two other outcomes are handled here rather than left to surface
        as opaque timeouts:
          - zero hits and "too many results": both appear as the same
            generic empty-titled "מידע" (Info) message box (see
            INFO_DIALOG_HEADING_TEXT/NO_RESULTS_MARKER/
            TOO_MANY_RESULTS_MARKER in versions/v33.py), distinguished
            only by their message text. Zero hits is not an error -- this
            dismisses that dialog (declining to broaden the search) and
            returns ("no_results", None) for search() to turn into an
            empty SearchResults. Too many results IS an error -- this
            dismisses that dialog too (aborting back to the search
            dialog, without ever generating the enormous underlying
            result set) and raises ResponsaTooManyResultsError directly.
        """
        cfg = self.config
        timeout = timeout or self.search_timeout
        exclude_results_hwnds = exclude_results_hwnds or set()
        deadline = time.time() + timeout
        # Confirmed live: repeatedly retrying the print
        # probe for the FULL timeout when it never succeeds is itself a
        # crash risk, not just a slow failure -- reproduced directly,
        # RESPONSA.exe crashed after ~40 consecutive failed probe cycles
        # against a results window that had already appeared (a fast,
        # normally ~2s query). Cap how many times this specific probe is
        # allowed to fail in a row once a results window exists, and bail
        # out with a clear, specific error well before the full timeout
        # -- failing fast and safely beats hammering an app that's
        # already shown signs of not cooperating.
        MAX_CONSECUTIVE_PROBE_FAILURES = 10
        consecutive_probe_failures = 0

        while True:
            for hwnd in winutil.enum_top_level_windows_for_pids([self._pid]):
                if not win32gui.IsWindowVisible(hwnd):
                    continue
                if win32gui.GetClassName(hwnd) != "#32770":
                    continue
                title = win32gui.GetWindowText(hwnd)
                if title.endswith(cfg.RESULTS_SUMMARY_TITLE_SUFFIX):
                    # Close it as soon as it's seen and re-scan from
                    # scratch -- don't act on the window lists gathered
                    # below, they were taken before this dialog closed.
                    self._close_summary_dialog(hwnd)
                    break

                if self._is_info_dialog(hwnd, title):
                    message = self._read_dialog_static_texts(hwnd)
                    if cfg.NO_RESULTS_MARKER in message:
                        self._click_control(hwnd, self._info_dialog_button_id(
                            hwnd, cfg.NO_RESULTS_DECLINE_BUTTON_ID))
                        return ("no_results", None)
                    if cfg.TOO_MANY_RESULTS_MARKER in message:
                        self._click_control(hwnd, self._info_dialog_button_id(
                            hwnd, cfg.TOO_MANY_RESULTS_ABORT_BUTTON_ID))
                        raise ResponsaTooManyResultsError(message)
                    raise ResponsaSearchError(f"Unexpected info dialog: {message!r}")

                if title == cfg.INVALID_QUERY_DIALOG_TITLE:
                    # Unlike the "מידע" dialogs above, this one HAS a
                    # title -- e.g. a query word containing nikud (vowel
                    # points) triggers it. Dismiss it (returns cleanly to
                    # the still-open search dialog, ready for a corrected
                    # query) before raising, so the caller isn't left with
                    # a stray dialog on screen if they don't catch this.
                    message = self._read_dialog_static_texts(hwnd)
                    self._click_control(hwnd, cfg.INVALID_QUERY_DISMISS_BUTTON_ID)
                    raise ResponsaInvalidQueryError(message)

                if self._is_print_dialog(hwnd):
                    # Our own _probe_print_dialog's Ctrl+P can legitimately
                    # take longer than its own 1s polling window to
                    # actually open this dialog -- confirmed live:
                    # on a slow/complex query, the real Print
                    # dialog appeared ~2.5s AFTER a probe's 1s wait had
                    # already given up and returned None. By the time this
                    # scan runs on the NEXT loop iteration, the dialog
                    # already exists and was previously misclassified by
                    # the generic "unexpected dialog" catch-all below,
                    # aborting the whole search with the dialog left open
                    # (which then blocked the next attempt too). Nothing
                    # else in this whole flow ever sends Ctrl+P to the
                    # results window, so any Print dialog seen here IS the
                    # search being done -- treat it exactly like a
                    # successful probe, whenever it actually shows up.
                    self._cancel_dialog(hwnd)
                    results_hwnd = None
                    for candidate in winutil.find_descendants(self._main_hwnd):
                        if candidate in exclude_results_hwnds:
                            continue
                        if win32gui.GetClassName(candidate).startswith(cfg.RESULTS_WINDOW_CLASS_PREFIX):
                            results_hwnd = candidate
                            break
                    if results_hwnd is None:
                        raise ResponsaSearchError(
                            "A Print dialog appeared during search, but no results "
                            "window could be found to match it"
                        )
                    self._close_any_lingering_summary_dialog()
                    return ("results", results_hwnd)

                # Any other #32770 popping up here isn't something we've
                # mapped yet -- surface it rather than silently waiting
                # out the full timeout.
                if title and title != cfg.SEARCH_DIALOG_TITLES["advanced"]:
                    raise ResponsaSearchError(
                        f"Unexpected dialog during search: {title!r} "
                        f"({self._read_dialog_static_texts(hwnd)!r})"
                    )
            else:
                results_hwnd = None
                for hwnd in winutil.find_descendants(self._main_hwnd):
                    if hwnd in exclude_results_hwnds:
                        continue
                    if win32gui.GetClassName(hwnd).startswith(cfg.RESULTS_WINDOW_CLASS_PREFIX):
                        results_hwnd = hwnd
                        break

                if results_hwnd is not None:
                    if winutil.is_window_hung(results_hwnd):
                        # Genuinely still busy -- Windows' own hang-
                        # detector agrees the window isn't pumping
                        # messages, which is the NORMAL state for a
                        # long/complex search still computing (confirmed
                        # live: the results window frame can
                        # appear within a couple of seconds of
                        # submitting, long before a slow query actually
                        # finishes -- probing it that early just finds it
                        # hung). Skip the probe entirely rather than
                        # attempt it: _probe_print_dialog's mouse-move-
                        # based focus dance is exactly what was found to
                        # raise a spurious "no active desktop" error
                        # against a hung window -- not a real session
                        # lock, and not a real focus-stealing problem
                        # either. Doesn't count against
                        # consecutive_probe_failures: that counter exists
                        # to catch a genuinely different, abnormal case
                        # (the app IS responsive but the probe still
                        # isn't indicating done), not ordinary business
                        # for a slow query -- see MAX_CONSECUTIVE_PROBE_
                        # FAILURES's own docstring above.
                        pass
                    else:
                        print_hwnd = self._probe_print_dialog(results_hwnd)
                        if print_hwnd is not None:
                            self._cancel_dialog(print_hwnd)
                            self._close_any_lingering_summary_dialog()
                            return ("results", results_hwnd)
                        consecutive_probe_failures += 1
                        if consecutive_probe_failures >= MAX_CONSECUTIVE_PROBE_FAILURES:
                            raise ResponsaTimeoutError(
                                f"Print-completion probe failed {consecutive_probe_failures} times in a "
                                "row against a results window that already exists, and it is NOT "
                                "reported as hung/busy by Windows. This usually means another window "
                                "(e.g. an editor) currently has real OS focus and is intercepting the "
                                "probe's keystrokes -- repeating it further risks destabilizing Responsa "
                                "rather than ever succeeding. Avoid using another window while automation "
                                "is running, then retry."
                            )

            if time.time() >= deadline:
                raise ResponsaTimeoutError(
                    f"Search did not produce a confirmed-finished result within {timeout}s"
                )
            time.sleep(1.5)

    def _probe_print_dialog(self, results_hwnd: int) -> Optional[int]:
        """Send Ctrl+P to `results_hwnd` and report whether a Print
        dialog actually opened -- the ground-truth "is the search really
        done" signal used by _wait_for_search_outcome. Returns the new
        dialog's hwnd, or None if nothing opened (still busy).

        Matches specifically by PRINT_DIALOG_TITLE, not "any new #32770":
        confirmed live, a results-summary dialog can legitimately finish
        rendering as its own new top-level window at almost exactly the
        same moment (the search genuinely just finished, independent of
        this probe's Ctrl+P), and matching any new #32770 grabbed THAT
        one instead at least once -- which then only ever appeared
        "disabled" to the caller once the real Print dialog opened on top
        of it moments later, since the wrong hwnd was already committed to.
        """
        before = set(winutil.enum_top_level_windows_for_pids([self._pid]))
        try:
            self._bring_to_foreground(results_hwnd)
            wrapper = HwndWrapper(results_hwnd)
            self._safe_set_focus(wrapper, timeout=1.0, interval=0.2)
        except RuntimeError as e:
            # NOT swallowed as "still busy" below -- but see the caller
            # (_wait_for_search_outcome): it already skips calling this
            # method at all while winutil.is_window_hung(results_hwnd) is
            # true, which was root-caused live as the REAL
            # trigger for this exact RuntimeError ("There is no active
            # desktop required for moving mouse cursor!" -- a stable
            # pywinauto message): attempting the mouse-move-based
            # set_focus() above against a window that's hung (still
            # computing a long/complex search, not pumping messages) is
            # what raises it -- it has nothing to do with the real
            # interactive desktop. Two misdiagnoses happened here before
            # that was found: first as "another window has focus" (a
            # broad `except Exception` swallowed it as ordinary busy-
            # ness), then as a genuinely locked/disconnected session
            # (ResponsaSessionLockedError, which fixed the swallowing but
            # not the underlying cause). Since the caller now filters out
            # the hung case before ever getting here, a RuntimeError
            # reaching this point is a much stronger signal of an actual
            # locked/disconnected session -- kept as ResponsaSessionLockedError
            # for that genuinely remaining case, not removed.
            if "no active desktop" in str(e).lower():
                raise ResponsaSessionLockedError(str(e).strip()) from e
            return None
        except Exception:
            # Focus can legitimately fail while the app is busy (e.g.
            # WaitGuiThreadIdle timing out, or the window being briefly
            # disabled) -- that just means "still searching", same as if
            # Ctrl+P had silently done nothing.
            return None
        # NOT swallowed by the except above: if ANOTHER APPLICATION (e.g.
        # the user's editor) holds real OS focus, retrying won't help on
        # its own, so this must surface as a clear error rather than look
        # like "still searching" (which would otherwise silently retry
        # and fail with a confusing generic timeout). But a different
        # window of Responsa itself in front -- the results-summary dialog
        # that pops up just after the results window -- is not an error:
        # skip this round; _wait_for_search_outcome's next scan closes it.
        if not self._require_foreground(results_hwnd, tolerate_own_windows=True):
            return None
        self._send_keys(win32con.VK_CONTROL, ord("P"))

        probe_deadline = time.time() + 1.0
        while time.time() < probe_deadline:
            for hwnd in winutil.enum_top_level_windows_for_pids([self._pid]):
                if hwnd in before:
                    continue
                if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == "#32770" \
                        and self._is_print_dialog(hwnd):
                    return hwnd
            time.sleep(0.1)
        return None

    def _cancel_dialog(self, hwnd: int, timeout: float = 5):
        """Dismiss a dialog with Escape and confirm it's actually gone --
        used to close the Print dialog opened purely as a completion
        probe (see _probe_print_dialog); the real print happens later,
        through _export_to_pdf's own fresh Ctrl+P.

        Retries the whole enabled-check/focus/Escape sequence across
        `timeout`, rather than a single wait-then-act: confirmed live, a
        one-shot "wait until enabled, then act" isn't enough here -- the
        window can flip enabled and disabled again in quick succession
        (e.g. a results-summary dialog racing to appear around the same
        moment can repeatedly steal modal focus), so `type_keys` still
        raised `ElementNotEnabled` even right after a passing enabled
        check moments earlier.
        """
        wrapper = HwndWrapper(hwnd)
        deadline = time.time() + timeout
        while True:
            if win32gui.IsWindowEnabled(hwnd):
                try:
                    self._bring_to_foreground(hwnd)
                    self._safe_set_focus(wrapper, timeout=1.0, interval=0.2)
                except Exception:
                    pass
                else:
                    # NOT swallowed: a real focus-stealing condition
                    # should surface clearly rather than exhaust this
                    # retry loop and raise a misleading "never stayed
                    # enabled" error instead (see _probe_print_dialog's
                    # matching comment).
                    # A window of Responsa's own in front (say, the summary
                    # dialog popping up over the probe's Print dialog) just
                    # means try again next round, like the enabled/disabled
                    # flapping this loop already handles.
                    if self._require_foreground(hwnd, tolerate_own_windows=True):
                        self._send_keys(win32con.VK_ESCAPE)
                        break
            if time.time() >= deadline:
                raise ResponsaTimeoutError(f"Could not dismiss probe dialog {hwnd} (never stayed enabled)")
            time.sleep(0.2)
        winutil.wait_for(lambda: not win32gui.IsWindow(hwnd), timeout=timeout, interval=0.2)

    def _close_summary_dialog(self, hwnd: int, timeout: float = 10):
        """Click OK on the results-summary dialog and verify it actually
        closes, retrying the click if needed. Confirmed live: a single
        click doesn't always actually dismiss it right away -- once, it
        was still open (just disabled, i.e. a modal child had appeared on
        top of it) at the same time as the Print dialog from
        _export_to_pdf, meaning printing had proceeded despite the
        summary never really being dismissed. Don't just click once and
        hope -- verify the window is actually gone, and retry if not.
        """
        cfg = self.config
        deadline = time.time() + timeout
        while win32gui.IsWindow(hwnd):
            self._click_control(hwnd, cfg.RESULTS_SUMMARY_OK_BUTTON_ID)
            try:
                winutil.wait_for(lambda: not win32gui.IsWindow(hwnd), timeout=1.5, interval=0.2)
                return
            except TimeoutError:
                if time.time() >= deadline:
                    raise ResponsaTimeoutError(
                        f"Results-summary dialog {hwnd} did not close after repeated OK clicks"
                    ) from None

    def _close_any_lingering_summary_dialog(self):
        """Defensive cleanup: confirmed live, the results-summary dialog
        can still be open (disabled, sitting under something else) even
        after it was supposedly closed already -- close any that's still
        around, quietly, rather than let it interfere with the Print
        dialog that's about to be requested."""
        cfg = self.config
        for hwnd in winutil.enum_top_level_windows_for_pids([self._pid]):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == "#32770" \
                    and win32gui.GetWindowText(hwnd).endswith(cfg.RESULTS_SUMMARY_TITLE_SUFFIX):
                self._close_summary_dialog(hwnd)

    def _info_dialog_button_id(self, hwnd: int, preferred_id: int) -> int:
        """The control id to click to dismiss an info dialog: `preferred_id`
        when the dialog has it (the Yes/No/Cancel-style dialogs seen on the
        original machine), else the dialog's first visible push button (a
        plain message box with only an OK button, id 1 or 2, seen on other
        installs)."""
        buttons = [c for c in winutil.find_descendants(hwnd, class_name="Button")
                   if win32gui.IsWindowVisible(c)]
        ids = [win32gui.GetDlgCtrlID(c) for c in buttons]
        if preferred_id in ids or not ids:
            return preferred_id
        return ids[0]

    def _is_info_dialog(self, hwnd: int, title: str = "") -> bool:
        heading = self.config.INFO_DIALOG_HEADING_TEXT
        # Some installs show a plain titled message box ("מידע" as the
        # window title) instead of an untitled dialog with a heading Static.
        if title == heading:
            return True
        if title != "":
            return False
        for child in winutil.find_descendants(hwnd, class_name="Static"):
            if win32gui.GetWindowText(child) == heading:
                return True
        return False

    def _validate_lines_per_result(self, lines: int):
        cfg = self.config
        if isinstance(lines, bool) or not isinstance(lines, int) or not (cfg.LINES_MIN <= lines <= cfg.LINES_MAX):
            raise ValueError(
                f"lines_per_result must be an integer between {cfg.LINES_MIN} and "
                f"{cfg.LINES_MAX} (Responsa's own limits), got {lines!r}"
            )

    def _set_lines_per_result(self, results_hwnd: int, lines: int):
        """Set "how many lines to show per result" on the results window,
        the way it is done by hand: "תצוגה" -> "מספר שורות" (Alt+R), type
        the number, "אישור".

        Same recipe as get_result_text's "Skip to number": the command
        opens a MODAL dialog, so it is POSTED (a blocking send would
        deadlock this thread against the dialog it is about to find), and
        MFC routes it to the ACTIVE MDI child, so the results window is
        activated first and the post repeated if the dialog never shows.
        """
        cfg = self.config
        attempts = 3
        for attempt in range(attempts):
            self._activate_mdi_child(results_hwnd)
            self._bring_to_foreground(results_hwnd)
            self._post_command(self._main_hwnd, cfg.COMMAND_LINES_PER_RESULT)
            try:
                dialog_hwnd = self._find_visible_dialog(
                    cfg.LINES_DIALOG_TITLE,
                    timeout=10 if attempt == attempts - 1 else 4)
                break
            except ResponsaTimeoutError:
                if attempt == attempts - 1:
                    raise
        self._set_edit_text(dialog_hwnd, cfg.LINES_EDIT_ID, str(lines))
        try:
            self._click_and_wait(
                dialog_hwnd, cfg.LINES_OK_BUTTON_ID,
                lambda: (not win32gui.IsWindow(dialog_hwnd)
                         or not win32gui.IsWindowVisible(dialog_hwnd)) or None,
                what=f"OK in {cfg.LINES_DIALOG_TITLE!r} (the dialog stayed open)",
            )
        except ResponsaError:
            # Don't leave it open to block the next call.
            if win32gui.IsWindow(dialog_hwnd):
                self._click_control(dialog_hwnd, cfg.LINES_CANCEL_BUTTON_ID)
            raise
        # Applying re-lays-out the results window; let it settle before
        # the Print dialog is opened on it.
        time.sleep(0.5)

    def _ensure_expanded_display(self):
        """Sent as WM_COMMAND to the main frame, not a keystroke to
        results_hwnd -- MFC's command routing sends frame-level View-menu
        commands like this to the active MDI child automatically, the same
        way COMMAND_OPEN_SEARCH/COMMAND_CLOSE_ALL_WINDOWS were confirmed to
        work regardless of focus/foreground state."""
        self._send_command(self._main_hwnd, self.config.COMMAND_EXPANDED_RESULTS)

    # -- PDF export -----------------------------------------------------

    def _export_to_pdf(self, results_hwnd: int, max_hits: Optional[int] = None,
                        pdf_export_timeout: Optional[float] = None) -> str:
        """Print `results_hwnd` to a fresh PDF file and return its path.

        Confirmed working end to end live: Ctrl+P -> Print (with
        "Microsoft Print to PDF" as the already-selected default printer)
        -> a "Save Print Output As" dialog -> filename field -> Enter
        reliably captures the full result set into a real PDF file.

        `max_hits`, if given, limits the Print dialog's page range (see
        _limit_print_page_range) instead of printing every page -- this
        is what actually bounds export time for a search matching a huge
        number of results, since the page range is what controls how
        much the printer driver has to render.
        """
        pdf_path = os.path.join(self.pdf_dir, f"responsa_{uuid.uuid4().hex}.pdf")

        # Defensive final check: confirmed live, the results-summary
        # dialog can still be open (just disabled, sitting behind/under
        # what search() moved on to) even after search()'s own
        # close-and-verify loop -- close any that's still around before
        # printing, rather than risk the Print dialog appearing stacked
        # on top of a still-open summary dialog and coming up disabled.
        self._close_any_lingering_summary_dialog()

        self._bring_to_foreground(results_hwnd)
        wrapper = HwndWrapper(results_hwnd)
        self._safe_set_focus(wrapper)
        self._require_foreground(results_hwnd)
        self._send_keys(win32con.VK_CONTROL, ord("P"))

        try:
            print_hwnd = self._wait_for_new_top_level_dialog(timeout=10, title=self.config.PRINT_DIALOG_TITLE)
            self._accept_print_dialog(print_hwnd, max_hits=max_hits)
            self._fill_save_as_dialog(pdf_path)
        except Exception:
            # Never leave a modal Print/Save dialog open behind a failure:
            # confirmed live, the next call then started a new
            # search underneath it and failed too, and so did every call
            # after that.
            self._dismiss_leftover_export_dialogs()
            raise

        winutil.wait_for(lambda: os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0,
                          timeout=pdf_export_timeout or self.pdf_export_timeout, interval=0.5)
        # PDF writers can take a moment to finish flushing after the file
        # first appears -- give it a beat and confirm the size settles.
        self._wait_for_stable_file_size(pdf_path)
        return pdf_path

    @staticmethod
    def _parse_hit_count_from_title(title: str) -> Optional[int]:
        """Extract the hit count Responsa itself reports from a results
        window's title, e.g. "נמצאו 2974 תוצאות    1-8" -> 2974, or the
        singular-form "נמצאה תוצאה אחת    1-1" (no digit at all) -> 1.
        Returns None if the title doesn't match either known shape (used
        only for the best-effort time-budget estimate in search(), so a
        miss here just means that estimate can't be made, not an error).
        """
        match = re.search(r"\d+", title)
        if match:
            return int(match.group())
        if "תוצאה אחת" in title:
            return 1
        return None

    def _limit_print_page_range(self, print_hwnd: int, max_hits: int):
        """Restrict the Print dialog to only the pages needed to cover
        `max_hits` results, via its standard "Pages: from/to" range
        controls (see PRINT_RANGE_* in versions/v33.py -- confirmed live,
        these are the standard Windows common-dialog PrintDlg IDs, not
        Responsa-specific). Uses MIN_HITS_PER_PAGE (a conservative LOWER
        bound) to convert the hit cap into a page count, so this always
        prints at least enough pages, possibly a few more than strictly
        needed -- search() trims the parsed hits list down to the exact
        count afterward.
        """
        cfg = self.config
        hits_per_page = self.MIN_HITS_PER_PAGE
        lines = self._export_lines_per_result
        if lines is not None and lines > self.MANY_LINES_THRESHOLD:
            hits_per_page = self.MIN_HITS_PER_PAGE_MANY_LINES
        max_page = -(-max_hits // hits_per_page)  # ceil division
        self._click_control(print_hwnd, cfg.PRINT_RANGE_PAGES_RADIO_ID)
        from_edit = winutil.find_child_by_id(print_hwnd, cfg.PRINT_RANGE_FROM_EDIT_ID)
        to_edit = winutil.find_child_by_id(print_hwnd, cfg.PRINT_RANGE_TO_EDIT_ID)
        win32gui.SendMessage(from_edit, win32con.WM_SETTEXT, 0, "1")
        win32gui.SendMessage(to_edit, win32con.WM_SETTEXT, 0, str(max_page))

    def _wait_for_new_top_level_dialog(self, timeout: float, exclude: Optional[set] = None,
                                        title: Optional[str] = None) -> int:
        """`title`, if given, requires an exact match rather than
        accepting any new #32770 -- see _probe_print_dialog's docstring
        for the race this guards against (a results-summary dialog
        finishing rendering as its own new top-level window at almost
        the same moment as the dialog actually being waited for here)."""
        exclude = exclude or set()

        def check():
            for hwnd in winutil.enum_top_level_windows_for_pids([self._pid]):
                if hwnd in exclude:
                    continue
                if not win32gui.IsWindowVisible(hwnd) or win32gui.GetClassName(hwnd) != "#32770":
                    continue
                if title is not None:
                    if title == self.config.PRINT_DIALOG_TITLE:
                        if not self._is_print_dialog(hwnd):
                            continue
                    elif win32gui.GetWindowText(hwnd) != title:
                        continue
                return hwnd
            return None

        try:
            return winutil.wait_for(check, timeout=timeout)
        except TimeoutError as e:
            raise ResponsaTimeoutError("Expected print/save dialog did not appear") from e

    def _accept_print_dialog(self, hwnd: int, max_hits: Optional[int] = None):
        """Select a PDF-capable printer (see _select_pdf_printer),
        optionally restrict the page range (see _limit_print_page_range),
        and press the dialog's default button (Enter) to trigger Print.

        Waits for the dialog to actually be enabled first -- confirmed
        live, this can appear stacked on top of a summary dialog that
        `search()` hadn't actually finished closing yet (a separate now-
        fixed bug), which left THIS dialog temporarily disabled and made
        `type_keys` raise `ElementNotEnabled`.
        """
        winutil.wait_for(lambda: win32gui.IsWindowEnabled(hwnd), timeout=10, interval=0.2)
        # Poll rather than a single-shot check: confirmed live, the
        # printer combo box's item list can still be empty/unpopulated
        # for a moment right after the dialog becomes enabled (the same
        # class of async-population race seen elsewhere in this app and
        # in Windows' own common dialogs -- e.g. the Save dialog's
        # filename field).
        try:
            winutil.wait_for(lambda: self._select_pdf_printer(hwnd), timeout=5, interval=0.3)
        except TimeoutError as e:
            raise ResponsaError(
                "No printer with 'pdf' in its name is installed/listed in the "
                "Print dialog -- ResponsaClient extracts results by printing "
                "to a PDF printer, so one must be available (e.g. the "
                "built-in 'Microsoft Print to PDF')."
            ) from e
        if max_hits is not None:
            self._limit_print_page_range(hwnd, max_hits)
        self._bring_to_foreground(hwnd)
        wrapper = HwndWrapper(hwnd)
        self._safe_set_focus(wrapper)
        self._require_foreground(hwnd)
        self._send_keys(win32con.VK_RETURN)

    # Case-insensitive substrings to look for in a printer's name/description
    # in the Print dialog's printer combo box. Not just "Microsoft Print to
    # PDF" by exact name -- the user may have a different PDF-capable
    # printer installed (confirmed: a second one was added specifically to
    # test this), so match anything that looks PDF-related instead of
    # assuming a specific product name or that the OS default is correct.
    PDF_PRINTER_NAME_SUBSTRINGS = ("pdf",)

    def _select_pdf_printer(self, print_hwnd: int) -> bool:
        """Find the Print dialog's printer-name combo box, select the
        first entry whose text contains "pdf" (case-insensitive), and
        notify the dialog of the change. Returns True if one was found
        and selected, False if no such printer is listed.
        """
        combo = None
        for child in winutil.find_descendants(print_hwnd, class_name="ComboBox"):
            combo = child
            break
        if combo is None:
            return False

        count = win32gui.SendMessage(combo, win32con.CB_GETCOUNT, 0, 0)
        for i in range(count):
            length = win32gui.SendMessage(combo, win32con.CB_GETLBTEXTLEN, i, 0)
            buf = win32gui.PyMakeBuffer((length + 1) * 2)
            win32gui.SendMessage(combo, win32con.CB_GETLBTEXT, i, buf)
            text = bytes(buf[: length * 2]).decode("utf-16-le")
            if any(kw in text.lower() for kw in self.PDF_PRINTER_NAME_SUBSTRINGS):
                win32gui.SendMessage(combo, win32con.CB_SETCURSEL, i, 0)
                # Notify the dialog the selection changed, the same way a
                # real UI interaction would, so it updates dependent state
                # (the Status/Type/Where labels, "Print to file" default).
                combo_id = win32gui.GetDlgCtrlID(combo)
                wparam = win32api.MAKELONG(combo_id, win32con.CBN_SELCHANGE)
                win32gui.SendMessage(print_hwnd, win32con.WM_COMMAND, wparam, combo)
                return True
        return False

    @staticmethod
    def _safe_set_focus(wrapper: HwndWrapper, timeout: float = 60, interval: float = 1.0):
        """pywinauto's set_focus() calls WaitGuiThreadIdle, which raises
        RuntimeError("... is not responding!") if the target thread
        doesn't go idle in time -- confirmed live, this fires legitimately
        (not a hang) right after submitting a search over the *entire*
        database (thousands of hits), where Responsa is still genuinely
        busy processing when we try to move on to printing. Retry instead
        of treating a single such RuntimeError as fatal.
        """
        deadline = time.time() + timeout
        while True:
            try:
                wrapper.set_focus()
                return
            except RuntimeError:
                if time.time() >= deadline:
                    raise
                time.sleep(interval)

    def _find_save_dialog_filename_edit(self):
        """Locate the actual "File name" edit box inside a modern
        (IFileSaveDialog-based) Explorer-style Save dialog.

        This dialog type has no stable control ID for it (unlike the
        legacy comdlg32 "Save As" dialog's well-known id 1148 -- this one
        uses a completely different internal implementation, confirmed
        live: the filename Edit here had id 1001, and there is no reason
        to expect that to be stable either). Worse, there are multiple
        Edit controls in the tree, and a naive "first Edit control found"
        search (tried first, via UI Automation) grabbed the address/
        breadcrumb bar's inline-rename edit instead of the filename field,
        and feeding it a full Windows path produced an "invalid character"
        error -- that bug is why this method exists.

        The structural distinction that *does* reliably identify the
        filename field (verified against a live dialog's control tree):
        its immediate parent is a plain "ComboBox", and that ComboBox's
        OWN parent is not "ComboBoxEx32". The breadcrumb bar's edit is
        also parented by a "ComboBox", but that ComboBox is itself hosted
        inside a "ComboBoxEx32" (the breadcrumb control's real class) --
        checking the grandparent is what tells them apart.

        IMPORTANT: this dialog's top-level window itself gets destroyed
        and recreated (a new hwnd) shortly after first appearing --
        confirmed live: a hwnd captured when the dialog was first detected
        was already stale by the time this search ran against it moments
        later (children never populated), while re-resolving the dialog
        fresh BY TITLE at that same moment found a different, fully
        populated hwnd. So this re-resolves the dialog by title on every
        poll attempt rather than searching a single hwnd captured once.
        """
        def find_it():
            dialog_hwnd = self._find_current_save_dialog()
            if dialog_hwnd is None:
                return None
            for edit_hwnd in winutil.find_descendants(dialog_hwnd, class_name="Edit", visible_only=False):
                parent = win32gui.GetParent(edit_hwnd)
                if win32gui.GetClassName(parent) != "ComboBox":
                    continue
                grandparent = win32gui.GetParent(parent)
                if win32gui.GetClassName(grandparent) == "ComboBoxEx32":
                    continue
                return (dialog_hwnd, edit_hwnd)
            return None

        # The dialog's internal DirectUI/shell-view content populates
        # asynchronously after the top-level window itself appears -- seen
        # taking more than a few seconds, and apparently longer
        # still for a bigger combined result set (more pages to prepare
        # before the print pipeline gets to the save step), so poll
        # generously.
        try:
            return winutil.wait_for(find_it, timeout=40, interval=0.3)
        except TimeoutError as e:
            raise ResponsaError(
                "Could not locate the filename field in the Save dialog"
            ) from e

    def _dismiss_leftover_dialogs(self):
        """Called at the start of search(), get_result_text() and
        browse_get_text(), so none of them starts underneath something an
        earlier (failed or interrupted) call left behind -- confirmed live,
        each of these made the next call fail, and every call after it:
          - Responsa still busy with an earlier search that timed out:
            wait for it first (bounded by search_timeout). Its summary
            dialog only appears once it's done.
          - a Print / Save dialog: see _dismiss_leftover_export_dialogs.
          - a results-summary dialog ("N תוצאות"). Reproduced live: a
            search timed out while Responsa was still computing; ~1s
            after it became responsive again the summary appeared, with
            no search left to close it, and the next search failed
            ("'ניהול המאגרים' never opened") until hard_reset()."""
        if winutil.is_window_hung(self._main_hwnd):
            try:
                # Debounced (stable for 2s), which also covers the ~1s gap
                # before the summary dialog appears.
                self._wait_until_responsive(self._main_hwnd, timeout=self.search_timeout)
            except ResponsaTimeoutError:
                raise ResponsaTimeoutError(
                    "Responsa is still busy (probably with an earlier search that timed "
                    f"out) and did not become responsive within {self.search_timeout}s "
                    "-- retry shortly."
                ) from None
        self._dismiss_leftover_export_dialogs()
        self._close_any_lingering_summary_dialog()

    def _dismiss_leftover_export_dialogs(self, timeout: float = 120):
        """Get rid of a Print / "Save Print Output As" dialog a previous
        (failed or interrupted) call left open. Called when an export
        fails, and (via _dismiss_leftover_dialogs) at the start of
        search(), get_result_text() and browse_get_text().

        A leftover SAVE dialog is COMPLETED (saved to a throwaway file),
        never cancelled: its print job is already running, and cancelling
        it (a posted IDCANCEL) crashed RESPONSA.exe live (an
        access violation in mfc110u.dll seconds later -- the same crash
        signature seen before in the Windows event log). A Print
        dialog that hasn't printed yet is simply cancelled, as the
        completion probe already does routinely. Then waits until the
        print job is over (the main window enabled again, i.e. no modal
        progress dialog left) before returning."""
        cfg = self.config
        if self._find_current_save_dialog() is None and self._find_print_dialog() is None:
            return
        discard = os.path.join(self.pdf_dir, f"responsa_discard_{uuid.uuid4().hex}.pdf")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._find_current_save_dialog() is not None:
                try:
                    self._fill_save_as_dialog(discard)
                except ResponsaError:
                    pass  # re-checked on the next round
            else:
                print_hwnd = self._find_print_dialog()
                if print_hwnd is not None and win32gui.IsWindowEnabled(print_hwnd):
                    win32gui.PostMessage(print_hwnd, win32con.WM_COMMAND, win32con.IDCANCEL, 0)
                elif print_hwnd is None and win32gui.IsWindowEnabled(self._main_hwnd):
                    break
            time.sleep(0.5)
        else:
            raise ResponsaTimeoutError(
                "Could not clear a Print/Save dialog left open by an earlier call. "
                f"Visible Responsa windows: {self._describe_visible_windows()}"
            )
        if os.path.exists(discard):
            try:
                self._wait_for_stable_file_size(discard)
                os.remove(discard)
            except OSError:
                pass  # best effort; it's only a temp file

    def _is_print_dialog(self, hwnd: int) -> bool:
        """True for the Print dialog, whatever language Windows shows its
        title in: a known title, or (structurally) the standard
        page-range radios every Windows print dialog has."""
        titles = getattr(self.config, "PRINT_DIALOG_TITLES", (self.config.PRINT_DIALOG_TITLE,))
        if win32gui.GetWindowText(hwnd) in titles:
            return True
        if win32gui.GetClassName(hwnd) != "#32770":
            return False
        try:
            win32gui.GetDlgItem(hwnd, self.config.PRINT_RANGE_ALL_RADIO_ID)
            win32gui.GetDlgItem(hwnd, self.config.PRINT_RANGE_PAGES_RADIO_ID)
        except win32gui.error:
            return False
        return True

    def _find_print_dialog(self) -> Optional[int]:
        for hwnd in winutil.enum_top_level_windows_for_pids([self._pid]):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == "#32770" \
                    and self._is_print_dialog(hwnd):
                return hwnd
        return None

    def _find_current_save_dialog(self) -> Optional[int]:
        titles = getattr(self.config, "SAVE_DIALOG_TITLES", (self.config.SAVE_DIALOG_TITLE,))
        for hwnd in winutil.enum_top_level_windows_for_pids([self._pid]):
            if not win32gui.IsWindowVisible(hwnd) or win32gui.GetClassName(hwnd) != "#32770":
                continue
            if win32gui.GetWindowText(hwnd) in titles:
                return hwnd
            # The title is localized with Windows' display language; fall
            # back on the dialog's structure (an Explorer-style file
            # dialog with a Save/Cancel pair hosting a shell view).
            if winutil.find_descendants(hwnd, class_name="SHELLDLL_DefView", visible_only=False):
                return hwnd
        return None

    def _fill_save_as_dialog(self, path: str, timeout: float = 15):
        """Confirmed working live: WM_SETTEXT to the filename Edit control
        (identified structurally, see _find_save_dialog_filename_edit)
        followed by Enter reliably saves to `path`. Note GetWindowText on
        this specific Edit control is itself unreliable (reads back empty
        even right after a confirmed-successful WM_SETTEXT) -- this is a
        DirectUI quirk of the control, not a sign the set failed; don't
        add a read-back check here.

        Retries the whole thing (re-resolving the dialog fresh each time)
        if _require_foreground fails: confirmed live, this specific
        dialog's known destroy-and-recreate-with-a-new-hwnd behavior (see
        _find_save_dialog_filename_edit's docstring) can happen again in
        the moment between resolving it and finishing this method,
        leaving `dialog_hwnd` stale -- that's a normal, self-resolving
        occurrence for THIS dialog specifically, not a real
        focus-stolen-by-another-app condition, so re-resolving and
        retrying is the right response, not propagating the error.
        """
        # Confirms with the Save button (BM_CLICK, see
        # SAVE_DIALOG_SAVE_BUTTON_ID in versions/v33.py and
        # _click_control) instead of Enter. Enter needed real keyboard
        # focus, which the user's editor kept taking back -- the dialog
        # visibly flickered between gaining and losing focus, and when
        # this method's retry window ran out the dialog was left open,
        # blocking every later call. "Done" is checked by title (no Save
        # dialog visible at all), never by the captured hwnd, because of
        # this dialog's destroy-and-recreate behavior described above.
        cfg = self.config
        deadline = time.time() + timeout
        while True:
            dialog_hwnd, edit_hwnd = self._find_save_dialog_filename_edit()
            win32gui.SendMessage(edit_hwnd, win32con.WM_SETTEXT, 0, path)
            try:
                self._click_and_wait(
                    dialog_hwnd, cfg.SAVE_DIALOG_SAVE_BUTTON_ID,
                    lambda: self._find_current_save_dialog() is None or None,
                    what=f"Save in {cfg.SAVE_DIALOG_TITLE!r} (the dialog stayed open)",
                    attempts=1,
                )
                return
            except ResponsaError:
                # Most likely the dialog was recreated under a new hwnd
                # between resolving and clicking -- re-resolve and retry.
                if time.time() >= deadline:
                    raise
                time.sleep(0.3)

    @staticmethod
    def _wait_for_stable_file_size(path: str, checks: int = 3, interval: float = 0.5):
        last_size = -1
        stable_count = 0
        while stable_count < checks:
            time.sleep(interval)
            size = os.path.getsize(path)
            if size == last_size:
                stable_count += 1
            else:
                stable_count = 0
                last_size = size

    # -- generic control helpers ---------------------------------------------

    @staticmethod
    def _send_command(hwnd: int, command_id: int):
        """Send a real MFC menu command ID as WM_COMMAND directly to
        `hwnd`. This is the reliable way to trigger main-window actions in
        this app: the visible toolbar buttons (Search, Windows, ...) are a
        third-party component that does not respond to any form of
        simulated click (see the long comment in versions/v33.py), but the
        main window has a real (if normally hidden) classic menu bar
        underneath, and sending WM_COMMAND with one of its actual item IDs
        goes through MFC's normal command-routing path -- confirmed live to
        work with zero dependency on window focus or foreground state,
        unlike every click-based approach that was tried first.
        """
        wparam = win32api.MAKELONG(command_id, 0)
        win32gui.SendMessage(hwnd, win32con.WM_COMMAND, wparam, 0)

    @staticmethod
    def _activate_mdi_child(hwnd: int):
        """Make `hwnd` the active child of its MDIClient (WM_MDIACTIVATE),
        independent of which application window has the keyboard focus."""
        client = win32gui.GetParent(hwnd)
        if client and win32gui.GetClassName(client) == "MDIClient":
            win32gui.SendMessage(client, win32con.WM_MDIACTIVATE, hwnd, 0)

    @staticmethod
    def _post_command(hwnd: int, command_id: int):
        """Like _send_command, but POSTS (asynchronous) rather than SENDS
        (blocking) the WM_COMMAND. Required for any menu command that
        opens a MODAL dialog: confirmed live, COMMAND_SKIP_TO_NUMBER's
        "Skip to instance" dialog pumps its own nested modal message loop
        INSIDE the main window's WM_COMMAND handler -- a blocking
        SendMessage there doesn't return until that dialog is dismissed,
        which deadlocks the calling thread against itself (it needs to
        run the code that finds and dismisses that very dialog). Every
        other COMMAND_* sent via _send_command happens to open either a
        modeless dialog or nothing at all, so this never came up before.
        """
        wparam = win32api.MAKELONG(command_id, 0)
        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, wparam, 0)

    @staticmethod
    def _bring_to_foreground(hwnd: int):
        """click_input() performs a real mouse click at screen coordinates
        -- it lands wherever is actually on screen at that spot, foreground
        or not. Confirmed by hitting this directly: with an unrelated
        window (a maximized editor) covering the same screen region as
        Responsa's main window, clicks silently landed on the other app
        instead of Responsa's toolbar. Every interaction must activate the
        target window first.

        Tries a plain win32gui.SetForegroundWindow() first and only falls
        back to pywinauto's set_focus() (which works around Windows'
        foreground-lock policy by briefly simulating an Alt keypress) on
        failure. NOTE: an attempt to make this ALWAYS use set_focus() was
        tried and reverted -- it regressed the plain, no-book-scope search
        flow (previously 100% reliable) into failing to set the query
        text at all. Alt keypresses are not free in this app: a bare Alt
        toggles its toolbar into mnemonic-highlight mode (see the
        COMMAND_* discussion in versions/v33.py), so unconditionally
        simulating one before every single click/text-set is itself a
        source of bugs, not just a safe fallback. Keep this as
        try-real-first, only fall back on actual failure.

        NOTE: a version of this method that verified success
        via GetForegroundWindow() and fell back to set_focus() whenever
        that check failed -- not just on an outright exception -- was
        tried and reverted. It sounded like a strict improvement (catches
        SetForegroundWindow being silently DENIED by Windows'
        foreground-lock policy, which happens with no exception at all
        when a different application currently holds real input focus),
        but confirmed live, under SUSTAINED focus contention (the user
        actively using another window throughout) it made the Alt-key
        fallback fire on nearly every single call instead of rarely --
        exactly the "unconditionally simulating one before every
        click/text-set is itself a source of bugs" case the paragraph
        above already warns about -- and reproduced an actual RESPONSA.exe
        crash. Detecting this condition is handled separately and safely
        by _require_foreground (a read-only check, no fallback attempt)
        instead -- see its call sites.
        """
        top = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
        if win32gui.IsIconic(top):
            win32gui.ShowWindow(top, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(top)
        except Exception:
            HwndWrapper(top).set_focus()

    @staticmethod
    def _require_foreground(hwnd: int, tolerate_own_windows: bool = False) -> bool:
        """Check that `hwnd`'s top-level window truly has OS foreground
        focus right now, right before a simulated keystroke (see
        _bring_to_foreground's docstring for why a prior bring-to-foreground
        attempt reporting no exception is NOT sufficient proof). Returns
        True if it does. Otherwise raises ResponsaFocusError naming
        whatever window has it -- turning a silently misdirected keystroke
        (and, downstream, a confusing generic timeout) into an immediate,
        clear error.

        `tolerate_own_windows`: if the window in front is a DIFFERENT window
        of Responsa's own process (typically a dialog that just popped up),
        return False instead of raising, so a caller that polls can simply
        try again next round. That is not "another application stole
        focus" -- the results-summary dialog appears moments after the
        results window does, and confirmed live (the user saw
        the unclicked summary dialog in front after the failure) treating
        it as a focus error made the print probe fail intermittently on
        queries that show a summary. Before this check existed the same
        race was harmless: the keystroke hit the dialog, the probe found no
        Print dialog, and the next round closed the summary.

        Accepts EITHER `fg == hwnd` itself OR `fg == GetAncestor(hwnd,
        GA_ROOT)` (hwnd's top-level frame) as real focus -- confirmed live
        as a genuine, reproducible bug: for an MDI CHILD hwnd
        (a results window), GetForegroundWindow() can correctly return
        the CHILD's own hwnd directly once it's the active MDI child, not
        just its frame's hwnd. Comparing only against GA_ROOT then always
        disagreed with a focus state that was actually completely correct
        -- with `tolerate_own_windows=True` this was silently swallowed as
        "try again next round" (same pid, so "own=True") rather than ever
        raising, so it surfaced as _probe_print_dialog looping to "Print-
        completion probe failed 10 times" -- Ctrl+P was never even SENT in
        any of those 10 rounds, this check rejected them all first.
        Reproduced directly: with fg == hwnd (this exact case), a Print
        dialog opened immediately once Ctrl+P was actually sent.
        """
        top = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
        fg = win32gui.GetForegroundWindow()
        if fg == top or fg == hwnd:
            return True
        try:
            fg_pid = win32process.GetWindowThreadProcessId(fg)[1]
            top_pid = win32process.GetWindowThreadProcessId(top)[1]
        except Exception:
            fg_pid = top_pid = None
        own = fg_pid is not None and fg_pid == top_pid
        if own and tolerate_own_windows:
            return False
        # Say exactly what the foreground window is: a bare title was
        # ambiguous in practice (once '(untitled window)', once the title
        # of Responsa's own results window, which the "another application
        # has focus" wording got wrong).
        if fg_pid is None:
            details = f"hwnd {fg:#x}"
        else:
            owner = "a window of Responsa itself" if own else "another application"
            details = (f"{owner}, class {win32gui.GetClassName(fg)!r}, hwnd {fg:#x}; "
                       f"expected Responsa's main window {top:#x}")
        raise ResponsaFocusError(win32gui.GetWindowText(fg) or "(untitled window)", details)

    @staticmethod
    def _send_keys(*vk_codes: int, hold: float = 0.02):
        """Send a chord of virtual-key codes via raw win32api.keybd_event
        (e.g. Ctrl+P: `_send_keys(win32con.VK_CONTROL, ord('P'))`) --
        presses each key down in order, then releases them in reverse.

        Replaces pywinauto's HwndWrapper.type_keys() everywhere in this
        module. Confirmed live: type_keys() stopped
        reliably delivering keystrokes to Responsa in this
        session/environment for reasons never fully root-caused -- with
        BOTH real OS foreground (GetForegroundWindow) AND MDI activation
        (WM_MDIGETACTIVE) independently verified correct at the exact
        moment type_keys("^p") failed to open a Print dialog, a raw
        keybd_event-based Ctrl+P succeeded immediately under otherwise
        identical conditions. This still goes through the same OS input
        queue as type_keys (so _bring_to_foreground/_require_foreground
        before calling this are still meaningful and necessary -- a raw
        keybd_event is delivered to whatever window has TRUE keyboard
        focus, exactly like any other simulated input), it just doesn't
        go through whatever pywinauto-internal path was failing.
        """
        for code in vk_codes:
            win32api.keybd_event(code, 0, 0, 0)
            time.sleep(hold)
        for code in reversed(vk_codes):
            win32api.keybd_event(code, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(hold)

    # BS_TYPEMASK values (winuser.h) of the Button styles clicked via a
    # posted WM_COMMAND/BN_CLICKED rather than a real mouse click.
    _PUSH_BUTTON_TYPES = {0x0, 0x1, 0xB}  # BS_PUSHBUTTON, BS_DEFPUSHBUTTON, BS_OWNERDRAW
    _RADIO_OR_CHECK_TYPES = {0x2, 0x3, 0x4, 0x5, 0x6, 0x9}  # (AUTO)CHECKBOX/(AUTO)RADIOBUTTON/3STATE

    @classmethod
    def _click_control(cls, parent_hwnd: int, control_id: int):
        """Click a dialog control WITHOUT depending on the mouse where
        possible.

        Real bug, reproduced from the MCP server log: this
        used to be a plain click_input() -- a real mouse click at the
        control's screen coordinates, which silently lands on whatever
        window is actually on top at that spot (the user's editor, a
        notification toast) or misses if the user moves the mouse. Nothing
        verified the click took effect, so a lost click surfaced much
        later as an unrelated-looking timeout: "ניהול המאגרים" never
        opening after the databases nav button, and the "דילוג למופע"
        dialog left open with OK never pressed (seen on screen by the
        user) until get_result_text's 60s wait gave up.

        Every control clicked here was confirmed (read-only class check)
        to be a standard Win32 "Button". A push button is clicked the way
        the button is asked to click itself: BM_CLICK, posted to it --
        focus/cursor/z-order independent (see the comment there for why
        it is not a WM_COMMAND sent to the parent). It is asynchronous,
        like the real click it replaces, so callers that depend on the
        effect wait for it (see _click_and_wait). The toolbar
        that is known NOT to respond to message-based clicks (see the
        module docstring) is a different, third-party component and is
        never clicked through here.

        Radio buttons/checkboxes need their own checked state toggled,
        which a WM_COMMAND to the parent doesn't do -- those get BM_CLICK
        (still message-based), verified via BM_GETCHECK for radios, with
        the old real click only as a fallback. Anything that isn't a
        Button keeps the real click.
        """
        child = winutil.find_child_by_id(parent_hwnd, control_id)
        btn_type = None
        if win32gui.GetClassName(child) == "Button":
            btn_type = win32gui.GetWindowLong(child, win32con.GWL_STYLE) & 0xF

        if btn_type in cls._PUSH_BUTTON_TYPES:
            # A real click on a disabled button -- or on a dialog disabled
            # because a modal child is open on top of it -- does nothing,
            # and callers like _close_summary_dialog rely on exactly that
            # (they verify and retry). A posted WM_COMMAND would bypass
            # the disabled state, so keep the real click's semantics: wait
            # briefly for it to become clickable, else do nothing.
            dialog = win32gui.GetAncestor(child, win32con.GA_ROOT)

            def clickable():
                return (win32gui.IsWindowEnabled(child) and win32gui.IsWindowEnabled(dialog)) or None

            try:
                winutil.wait_for(clickable, timeout=3, interval=0.1)
            except TimeoutError:
                return
            # BM_CLICK POSTED to the button itself: the button then runs
            # its own normal click handling on Responsa's UI thread and
            # notifies its parent exactly as for a real click -- only the
            # mouse is taken out of it. Deliberately NOT a WM_COMMAND
            # SENT (SendMessageTimeout) to the parent from this thread:
            # tried first, and RESPONSA.exe crashed 4 times in 10 minutes
            # (heap corruption 0xc0000374) -- it made the
            # app run e.g. the databases button's handler, which opens a
            # modal dialog, inside a cross-thread sent-message call, which
            # a real click never does. Posted, like the real click it
            # replaces (mouse input is queued too), so the timing the rest
            # of this module was tuned against is unchanged.
            cls._bring_to_foreground(parent_hwnd)
            win32gui.PostMessage(child, win32con.BM_CLICK, 0, 0)
            return

        cls._bring_to_foreground(parent_hwnd)
        if btn_type in cls._RADIO_OR_CHECK_TYPES:
            win32gui.SendMessage(child, win32con.BM_CLICK, 0, 0)
            is_radio = btn_type in (0x4, 0x9)
            if not is_radio or win32gui.SendMessage(child, win32con.BM_GETCHECK, 0, 0) == win32con.BST_CHECKED:
                return
        HwndWrapper(child).click_input()

    def _click_and_wait(self, parent_hwnd: int, control_id: int, done, what: str,
                        timeout: float = 5, attempts: int = 2):
        """_click_control, then wait until `done()` returns truthy (and
        return that value) -- clicking once more if it hasn't happened
        within `timeout`. A second click is only ever sent when the first
        one visibly had no effect, so it can't double-trigger anything."""
        for attempt in range(attempts):
            self._click_control(parent_hwnd, control_id)
            try:
                return winutil.wait_for(done, timeout=timeout, interval=0.1)
            except TimeoutError:
                continue
        raise ResponsaTimeoutError(
            f"Clicking {what} had no effect after {attempts} attempts. "
            f"Visible Responsa windows: {self._describe_visible_windows()}"
        )

    def _describe_visible_windows(self) -> str:
        """Every visible top-level window of Responsa as 'title' [class],
        for error messages -- without this a "dialog didn't appear"
        failure gives no clue what WAS on screen instead."""
        parts = []
        try:
            for hwnd in winutil.enum_top_level_windows_for_pids([self._pid]):
                if win32gui.IsWindowVisible(hwnd):
                    parts.append(f"{win32gui.GetWindowText(hwnd)!r} [{win32gui.GetClassName(hwnd)}]")
        except Exception as e:  # diagnostics must never mask the real error
            return f"(could not list windows: {e})"
        return ", ".join(parts) or "(none)"

    @classmethod
    def _set_edit_text(cls, parent_hwnd: int, control_id: int, text: str):
        """Set, don't verify: confirmed live that GetWindowText read-back
        on this specific query Edit control is unreliable and reads back
        empty even when a human just typed real, visibly-present text
        into it moments earlier -- the same class of DirectUI-style quirk
        seen with the Save dialog's filename field, just on an otherwise
        plain-looking classic Edit control. An earlier version of this
        method added a verify-and-retry loop keyed off that read-back,
        which was actively wrong: it always saw empty and therefore
        treated every successful set as a failure, raising an error on
        perfectly good runs. Don't reintroduce a read-back check here.
        """
        cls._bring_to_foreground(parent_hwnd)
        child = winutil.find_child_by_id(parent_hwnd, control_id)
        win32gui.SendMessage(child, win32con.WM_SETTEXT, 0, text)

    @classmethod
    def _set_checkbox(cls, parent_hwnd: int, control_id: int, checked: bool):
        cls._bring_to_foreground(parent_hwnd)
        child = winutil.find_child_by_id(parent_hwnd, control_id)
        win32gui.SendMessage(child, win32con.BM_SETCHECK,
                              win32con.BST_CHECKED if checked else win32con.BST_UNCHECKED, 0)
