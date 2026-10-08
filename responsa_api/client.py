"""ResponsaClient: the class you use to drive the Responsa desktop app."""
from types import ModuleType
from typing import Optional, Sequence, Union

from .book_scope import BookScopeInput
from .internal.automation import ResponsaAutomation
from .results import BrowseResult, SearchResults, SourceText


class ResponsaClient:
    """Automates one Responsa session: runs searches and returns the
    results as Python objects.

    Responsa (the Bar-Ilan Responsa Project desktop app) has no API of its
    own, so this drives its real window. Usage:

        from responsa_api import ResponsaClient, BookScope

        with ResponsaClient() as client:
            results = client.search("*כוכב# [-3:3] *לבנה#", books=BookScope.MISHNA)
            for hit in results.hits:
                print(hit.index, hit.citation)
            source = client.get_result_text(results.hits[0].index)
            print(source.text)

    Attaches to an already-running RESPONSA.exe if one exists (so it
    doesn't fight with a copy you have open manually); otherwise launches
    one. It never closes Responsa -- it is left running for the next call
    or client, which is much faster than restarting it.

    Because it uses the real window, don't use another application while a
    search is running -- simulated keystrokes go to whichever window has
    focus (ResponsaFocusError is raised when that is detected). Needs
    Windows, the installed Responsa app, and its license dongle.

    NOT safe to call from more than one thread/async task at a time, and
    this class does nothing to stop you -- it has no internal locking
    (that's deliberately layered on top, in responsa_mcp/lifecycle.py, not
    here). Calling two methods concurrently, on the same client or two
    different ones, drives the same real window from two places at once
    and can genuinely corrupt Responsa's state (confirmed live: a stray
    Print dialog and repeated automation failures needing manual
    recovery) -- always await/wait for one call's result before starting
    the next.

    Every error this raises is a `ResponsaError` (see responsa_api's
    exceptions).
    """

    def __init__(
        self,
        launch_timeout: float = 60,
        search_timeout: float = 60,
        pdf_export_timeout: float = 3600,
        time_budget: Optional[float] = None,
        pdf_dir: Optional[str] = None,
        config: Optional[ModuleType] = None,
    ):
        """
        Args:
            launch_timeout: Seconds to wait for Responsa's window to appear
                and to finish restoring the windows that were open the last
                time it was closed.
            search_timeout: Seconds to wait for Responsa to finish a search
                (the search itself, before results are extracted).
            pdf_export_timeout: Ceiling, in seconds, for extracting the
                results. Extraction prints them to a PDF, which is slow for
                a large result set (roughly 3 seconds per page; thousands
                of results take tens of minutes), so this is generous --
                a small search finishes in seconds.
            time_budget: How many seconds one `search()` call may take, or
                None (the default) for no limit. If extracting *all* of a
                query's results is estimated to take longer, `search()`
                extracts only as many as fit (see `SearchResults.truncated`),
                and raises ResponsaTimeoutError right away, before
                extracting anything, if not even one would fit.
            pdf_dir: Directory for the temporary PDFs used to extract
                results. Defaults to the system temp directory.
            config: Advanced. The GUI description for a different Responsa
                version; the default supports version 33.
        """
        self._impl = ResponsaAutomation(
            launch_timeout=launch_timeout,
            search_timeout=search_timeout,
            pdf_export_timeout=pdf_export_timeout,
            time_budget=time_budget,
            pdf_dir=pdf_dir,
            config=config,
        )

    def __enter__(self) -> "ResponsaClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def start(self) -> "ResponsaClient":
        """Attach to a running Responsa, or launch a new one. `with` calls
        this for you.

        Raises:
            ResponsaLaunchError: Responsa failed to start (for example a
                missing license dongle) and showed an error dialog.
            ResponsaTimeoutError: its window did not appear, or did not
                finish loading, within `launch_timeout`.
        """
        self._impl.start()
        return self

    def close(self) -> None:
        """Stop using Responsa (further calls need `start()` again). Responsa
        itself is left running. `with` calls this for you."""
        self._impl.close()

    def hard_reset(self, clear_windows: bool = True) -> "ResponsaClient":
        """Actually quit Responsa (every running instance, not just this
        client's) and start a fresh one -- for recovering when the app is
        stuck or behaving oddly, which `close()` can't fix since that only
        detaches and deliberately never touches the real process. This
        affects the real app, so don't call it if something else might be
        relying on the current session staying up.

        Args:
            clear_windows: Once the fresh instance is responsive, also
                close every window it restores from its last session
                (Responsa does this automatically on launch) so the reset
                leaves a genuinely clean slate. False leaves those
                restored windows as Responsa put them.

        Raises:
            ResponsaLaunchError, ResponsaTimeoutError: same as `start()`.
        """
        self._impl.hard_reset(clear_windows=clear_windows)
        return self

    def search(
        self,
        query: str,
        books: Optional[BookScopeInput] = None,
        search_all_databases: bool = True,
        max_hits: Optional[int] = None,
        lines_per_result: Optional[int] = None,
    ) -> SearchResults:
        """Run a query and return its results.

        A query with no matches is not an error: it returns a
        `SearchResults` with no hits.

        Args:
            query: In Responsa's own query language, space-separated
                (a word may contain only Hebrew letters and digits, so
                strip nikud/vowel points first).

                NOTE: space-separated words must be ADJACENT and in that
                order (an exact phrase); to match words that merely occur
                near each other use `word1 [-5:5] word2`. Use 1-3 key
                words with wildcards, never whole sentences.

                IMPORTANT -- a bare word matches ONLY that exact
                standalone form. This is NOT a fuzzy/substring search
                like a general search engine or a regex "word" pattern:
                Hebrew words routinely carry a prefix (ו/ה/ב/כ/ל/מ/ש,
                or a compound like כש, e.g. מנגנון -> המנגנון) or suffix
                (mainly plural/construct forms, e.g. מנגנון -> מנגנונים)
                attached, and a bare word will NOT match those forms.
                For each word, decide whether you want the exact
                standalone form, or a prefix/suffix allowed -- and
                which of the two wildcard kinds below, since they
                aren't the same:
                  - a bare word matches only that exact form.
                  - `*` is a FREE wildcard, not limited to real Hebrew
                    grammar: `*word` allows anything attached BEFORE
                    the word, `word*` allows anything attached AFTER
                    it, `*word*` both.
                  - `#` is GRAMMAR-RESTRICTED: `#word` allows only a
                    real Hebrew grammatical prefix BEFORE the word
                    (ו/ה/ב/כ/ל/מ/ש, or a compound like כש), `word#`
                    only a real grammatical suffix AFTER it (mainly
                    plural/construct forms like ים/ות), `#word#` both.
                  - `*` and `#` can be mixed on the two sides of the
                    same word, e.g. `#word*`. Neither is automatically
                    right for every word -- a specific proper name, or
                    a word you deliberately want in its exact bare
                    form, may not want either.
                  - `(word1/word2/...)` matches ANY ONE of several
                    alternatives at that position -- e.g. `(והוא/והיא)`
                    for either gender, `(הלך/הלכה)` for either number.
                    An entry may itself use `*`/`#`, e.g. `(*הלך/הלכה#)`.
                    A single-entry group `(word)` is the same as the bare
                    word.
                  - `[-3:3]` between two words/groups allows that many
                    other words in between (before:after) rather than
                    requiring them adjacent.
                Examples: `"*כוכב# [-3:3] *לבנה#"`,
                `"(והוא/והיא) [-1:1] (הלך/הלכה)"`,
                `"(אברהם/יצחק/יעקב) [-2:2] בראשית"`, `"#מנגנון#"`.
            books: Which books to search: a `BookScope`, the exact name of a
                node in Responsa's sources tree (see
                scripts/sources_tree.txt for all of them), or a list of
                either to search their union.
            search_all_databases: Only matters when `books` is None: True
                (the default) searches the whole database. False leaves
                Responsa's current source selection unchanged (advanced --
                prefer passing `books`).
            max_hits: Extract at most this many results, instead of all of
                them. The search still finds every match --
                `SearchResults.total_hits` is the real count and
                `.truncated` says whether `hits` was cut short. Use it for
                queries that may match thousands of results, since
                extracting a large result set is slow.
            lines_per_result: How many lines of text Responsa shows per
                result (its "מספר שורות" setting, Alt+R), from 1 to 21 --
                so each `Hit.snippet` is longer or shorter. The default,
                None, leaves Responsa's own setting as it is. The value
                applies to this search's results only (confirmed live: the
                next search is back to the app's default).

        Raises:
            ValueError: `lines_per_result` is not an integer from 1 to 21.
            ResponsaInvalidQueryError: Responsa rejected the query (for
                example it contains nikud or other non-Hebrew characters).
            ResponsaTooManyResultsError: The query matches so many results
                (over about 32000) that Responsa refuses to produce them;
                narrow the query or the `books`.
            ResponsaTimeoutError: The search or its extraction did not
                finish in time (see `search_timeout`, `time_budget`).
            ResponsaFocusError: Responsa's window did not have keyboard
                focus when it was needed.
        """
        return self._impl.search(
            query,
            books=books,
            search_all_databases=search_all_databases,
            max_hits=max_hits,
            lines_per_result=lines_per_result,
        )

    def get_result_text(self, index: int) -> SourceText:
        """Fetch the full text of one result of the most recent `search()`.

        Can be called repeatedly, for different results of the same
        search. A long source (a whole chapter) takes a few minutes, since
        it is extracted the same way search results are.

        Args:
            index: The result's 1-based number -- `Hit.index`.

        Raises:
            ResponsaError: No search has been run yet (or the last one had
                no results), or `index` is out of range for it.
            ResponsaTimeoutError: The text did not arrive in time.
            ResponsaFocusError: Responsa's window did not have keyboard
                focus when it was needed.
        """
        return self._impl.get_result_text(index)

    def browse(
        self,
        path: Optional[Union[str, Sequence[str]]] = None,
        exact: bool = False,
    ) -> BrowseResult:
        """Walk Responsa's sources tree (its "עיון" / Browse feature) one
        call at a time, and read a text when you reach one.

        The tree is a hierarchy: categories, then books, then chapters and
        so on, down to the texts themselves. Each call moves from where
        the previous call left you:

            client.browse()                        # top level: the categories
            client.browse('תנ"ך')                  # -> its books
            client.browse("ספר יחזקאל")            # -> its chapters
            client.browse("ב")                     # -> "פרק ב": a text, so
                                                   #    its full content
            client.browse("ג")                     # its sibling "פרק ג"
            client.browse(['תנ"ך', "בראשית", "א"])  # or several steps at once

        Whatever the call reaches is what you get back (see `BrowseResult`):
        a section gives its `options`, the entries to choose from next; a
        text gives its full `text`; a wrong choice gives an `error` code.
        After a text, you stay at the section that contains it, so its
        siblings are one call away.

        The tree structure comes from a cache file shipped with the
        package, so choosing your way down never touches Responsa; only
        reading a text does (extracted through the same print-to-PDF route
        as `search()`, a few seconds for a chapter).

        Args:
            path: What to choose, from the current position: one name, or a
                list of names to choose in turn. With no path at all the
                position is reset to the top and the categories are
                returned.
            exact: By default a name only has to identify the entry:
                "ב" selects "פרק ב" and "יחזקאל" selects "ספר יחזקאל",
                quote marks are ignored (תנך == תנ"ך), and a better match
                wins over a looser one: the entry's full text, then
                entries that START with your words, then entries that
                contain them. If it still fits more than one entry the
                call fails with `BrowseError.AMBIGUOUS`. With
                `exact=True` a name must be the entry's complete text.

        Returns:
            A `BrowseResult`. A rejected call (`result.error` set: name not
            found, ambiguous, or going below a text) is all-or-nothing:
            with a list of names nothing moves, and the position stays
            exactly where it was.

        Raises:
            ResponsaBrowseCacheError: The cache is missing, or Responsa's
                own tree no longer matches it (rebuild it with
                scripts/build_browse_cache.py).
            ResponsaError: A text was requested but Responsa is not
                started.
            ResponsaTimeoutError: The text did not arrive in time.
            ResponsaFocusError: Responsa's window did not have keyboard
                focus when it was needed.
        """
        return self._impl.browse(path, exact=exact)
