"""Exceptions raised by the Responsa automation layer."""


class ResponsaError(Exception):
    """Base class for all responsa_api errors."""


class ResponsaLaunchError(ResponsaError):
    """Raised when RESPONSA.exe fails to reach its main window.

    Covers every "תקלה"/"Application Error" style dialog seen during
    discovery (missing dongle, I/O errors, etc.) -- these are all
    fail-fast: the app closes itself once the dialog is dismissed, so
    there is no click-through recovery, only reporting what the dialog said.
    """

    def __init__(self, dialog_title: str, dialog_text: str):
        self.dialog_title = dialog_title
        self.dialog_text = dialog_text
        super().__init__(f"{dialog_title}: {dialog_text}")


class ResponsaTimeoutError(ResponsaError):
    """Raised when an expected window/dialog/state never appears in time."""


class ResponsaSearchError(ResponsaError):
    """Raised when a search cannot be completed (unexpected dialog, etc.)."""


class ResponsaTooManyResultsError(ResponsaSearchError):
    """Raised when Responsa's own "found more than N results, please
    simplify" dialog appears. Zero results is NOT an error (search()
    returns an empty SearchResults for that) -- this is specifically for
    the case where the app itself refuses to produce a usable result set
    at all because there are too many matches to process."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ResponsaInvalidQueryError(ResponsaSearchError):
    """Raised when Responsa's own "שגיאה בהגדרת השאילתה" (Error in query
    definition) dialog appears -- e.g. a word containing characters
    other than Hebrew letters/digits (confirmed live: nikud/vowel points
    trigger this, message "מילה יכולה להכיל אותיות עבריות וספרות בלבד."
    -- "a word may contain only Hebrew letters and digits")."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ResponsaBrowseCacheError(ResponsaError):
    """Raised when the cached Browse tree (internal/versions/) is missing
    or incomplete, or no longer matches what Responsa's own Browse tree
    shows (e.g. after a data update) -- rebuild it with
    scripts/build_browse_cache.py."""


class ResponsaSessionLockedError(ResponsaError):
    """Raised when a simulated mouse/keyboard action fails with pywinauto's
    "There is no active desktop required for moving mouse cursor!" (a
    stable, specific message from its mouse module).

    IMPORTANT, corrected understanding: this message's own
    wording suggests a locked/disconnected Windows session, and that IS
    one real, possible cause -- but root-causing an actual reproducible
    failure live showed the far more common trigger is NOT that at all:
    _probe_print_dialog's mouse-move-based focus attempt raises this
    exact error when aimed at a results window that Windows' own
    IsHungAppWindow reports as hung -- i.e. Responsa is still genuinely
    computing a long/complex search and hasn't been pumping messages, not
    a locked session. _wait_for_search_outcome now checks
    winutil.is_window_hung() BEFORE ever attempting the operation that
    raises this, specifically to avoid hitting it for that ordinary case
    -- so by the time this exception actually surfaces, it's a much
    stronger signal of a genuinely locked/disconnected session. Before
    that fix, this was raised on essentially every sufficiently long
    search, misdiagnosing ordinary "still working" as a locked session.
    (An earlier version of this same misdiagnosis, before this exception
    even existed, was a broad `except Exception` in _probe_print_dialog
    silently swallowing it as "still busy", surfacing many retries later
    as the generic, actively misleading "Print-completion probe failed...
    another window has focus".) Not recoverable by retrying: unlock or
    reconnect the session first, then retry."""

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(
            "The Windows session appears to be locked or disconnected -- "
            "there is no active desktop to send simulated mouse/keyboard "
            "input to right now. Unlock/reconnect the session, then retry."
            + (f" ({detail})" if detail else "")
        )


class ResponsaFocusError(ResponsaError):
    """Raised when a simulated keystroke (Ctrl+P, Enter, Escape) is about
    to be sent but Responsa's window does NOT actually have real OS
    foreground focus -- confirmed live: simulated keystrokes
    go to whatever window has TRUE foreground focus, and Windows'
    foreground-lock policy can silently deny a foreground-focus request
    (no exception anywhere in the chain) when a different application
    -- e.g. the user's own editor -- currently holds it. Without this
    check, that produces a confusing generic timeout instead of a clear
    reason. Not recoverable by retrying automatically: automation needs
    Responsa's window to have focus, so don't use another window while
    it's running.
    """

    def __init__(self, blocking_window_title: str, details: str = ""):
        self.blocking_window_title = blocking_window_title
        self.details = details
        which = f"{blocking_window_title!r} ({details})" if details else repr(blocking_window_title)
        super().__init__(
            f"Responsa's main window does not have real OS focus -- {which} "
            "does. Simulated keystrokes would be delivered there instead. "
            "Avoid using another window while automation is running."
        )
