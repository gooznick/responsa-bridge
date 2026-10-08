"""The four MCP tools, as plain async functions -- not decorated with
@mcp.tool() here (see server.py), so they're directly callable with
asyncio.run(...) in tests without going through the MCP protocol or
whatever the installed mcp SDK's registration does internally. (They ARE
wrapped with `_log_calls` below, which -- unlike @mcp.tool() -- preserves
direct callability; see its own docstring.)

Each tool: (1) maps its MCP-JSON-friendly parameters onto what
ResponsaClient actually accepts (params.py), (2) runs the blocking call
through lifecycle.locked_call (one GUI op at a time, off the event loop
thread, message preserved on a ResponsaError -- see locked_call's
docstring), (3) converts the dataclass result to a plain JSON-safe dict
(serialization.to_jsonable).
"""
import functools
import logging
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver.exceptions import ToolError

from .lifecycle import locked_call
from .params import resolve_books
from .serialization import to_jsonable

logger = logging.getLogger(__name__)


def _log_calls(fn):
    """Logs every call to a tool -- its name and full arguments on entry,
    and how it ended (ok, or the full exception and traceback) on exit --
    to the circular log (see logging_setup.py). The whole point is being
    able to reproduce, after the fact, the exact call that led to a
    crash, so arguments are logged in full: not redacted, not truncated
    (there's nothing secret in a Responsa query/path/index).

    Uses functools.wraps, which sets __wrapped__ -- inspect.signature()
    (what the mcp SDK's schema generation and this module's own direct
    callers both rely on) follows that automatically, so the wrapped
    function's real parameters/type hints/docstring are unaffected;
    confirmed live against the actually-installed SDK that
    the generated tool schemas are identical with and without this
    decorator.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        bound = dict(zip(fn.__code__.co_varnames, args))
        bound.update(kwargs)
        logger.info("-> %s(%r)", fn.__name__, bound)
        try:
            result = await fn(*args, **kwargs)
        except Exception:
            logger.exception("!! %s failed", fn.__name__)
            raise
        logger.info("<- %s ok", fn.__name__)
        return result
    return wrapper


@_log_calls
async def search(
    query: str,
    books: Optional[List[str]] = None,
    search_all_databases: bool = True,
    max_hits: Optional[int] = None,
    lines_per_result: Optional[int] = None,
) -> Dict[str, Any]:
    """Run a query against Responsa (the Bar-Ilan Responsa Project) and
    return its results.

    QUICK RULES (read first; this is NOT a web/natural-language search):
      1. Words separated by a space must appear ADJACENT, in exactly that
         order. Two or three space-separated words is therefore an exact
         phrase and usually finds nothing. To find words that merely occur
         near each other, put a window between them: `word1 [-5:5] word2`.
      2. Never type a whole sentence or an exact quote. Use 1-3 key words,
         and use wildcards on each: `*word*` (any prefix/suffix) or
         `#word#` (only real Hebrew prefix/suffix), e.g.
         `#שחיטה# [-8:8] #מכונה#`. A bare word matches only that exact form.
      3. Alternatives: `(word1/word2)`. No other punctuation: parentheses
         only for `(a/b)` groups; no quotes, no abbreviation marks (write
         רמבם, not רמב"ם).
      4. Start broad (one word + wildcards, small `max_hits`), then narrow
         by adding a second word with a `[-N:N]` window.

    NEVER call this in parallel with another call to this tool or to
    get_result_text/browse/hard_reset -- Responsa automates a real
    desktop GUI app and can only do one thing at a time. Call tools ONE
    AT A TIME: wait for this call's result before calling the next tool.
    A parallel/overlapping call is rejected immediately with a clear
    error, not queued -- it will NOT run once the first one finishes.

    `query` is in Responsa's own query language, space-separated (a word
    may contain only Hebrew letters and digits -- strip nikud/vowel
    points first).

    IMPORTANT -- a bare word matches ONLY that exact standalone form.
    This is NOT a fuzzy/substring search like a general search engine
    or a regex "word" pattern: Hebrew words routinely carry a prefix
    (ו/ה/ב/כ/ל/מ/ש, or a compound like כש, e.g. מנגנון -> המנגנון) or
    suffix (mainly plural/construct forms, e.g. מנגנון -> מנגנונים)
    attached, and a bare word will NOT match those forms. For each
    word, decide whether you want the exact standalone form, or a
    prefix/suffix allowed -- and which of the two wildcard kinds
    below, since they aren't the same:
      - a bare word matches only that exact form.
      - `*` is a FREE wildcard, not limited to real Hebrew grammar:
        `*word` allows anything attached BEFORE the word, `word*`
        allows anything attached AFTER it, `*word*` both.
      - `#` is GRAMMAR-RESTRICTED: `#word` allows only a real Hebrew
        grammatical prefix BEFORE the word (ו/ה/ב/כ/ל/מ/ש, or a
        compound like כש), `word#` only a real grammatical suffix
        AFTER it (mainly plural/construct forms like ים/ות), `#word#`
        both.
      - `*` and `#` can be mixed on the two sides of the same word,
        e.g. `#word*`. Neither is automatically right for every word
        -- a specific proper name, or a word you deliberately want in
        its exact bare form, may not want either.
      - `(word1/word2/...)` matches ANY ONE of several alternatives at
        that position -- e.g. "(והוא/והיא)" for either gender,
        "(הלך/הלכה)" for either number. An entry may itself use `*`/`#`,
        e.g. "(*הלך/הלכה#)". A single-entry group "(word)" is the same
        as the bare word.
      - `[-3:3]` between two words/groups allows that many other words
        in between (before:after) rather than requiring them adjacent.
    Examples: "*כוכב# [-3:3] *לבנה#", "(והוא/והיא) [-1:1] (הלך/הלכה)",
    "(אברהם/יצחק/יעקב) [-2:2] בראשית", "#מנגנון#".

    A query with no matches is not an error -- it returns an empty
    `hits` list. `books` narrows which
    books/corpora are searched: each entry is either the name of one of
    the built-in scopes (case-insensitive: "mishna", "shut", "old_shut",
    "all", "tora", "bible", "gemara", "chazal_literature", "rambam",
    "new_shut", "shas") or the exact name of a node in Responsa's
    sources tree; omit to search the whole database. `search_all_databases`
    only matters when `books` is omitted: True (default) searches
    everything, False leaves Responsa's current selection as-is
    (advanced -- prefer `books`). `max_hits` extracts at most this many
    results instead of all of them (the search still finds every match:
    `total_hits` is the real count, `truncated` says whether `hits` was
    cut short) -- use it for queries that might match thousands of
    results, since extracting a large result set is slow (roughly 3
    seconds per page). `lines_per_result` (integer 1-21; omit for
    Responsa's default, which is 3) sets how many lines of text are shown
    per result, i.e. how long each hit's `snippet` is: raise it to see
    more context around each match, lower it for a compact list. More
    lines make the extraction slower (more pages).

    If a call fails because another window has focus or the Windows
    session is locked, retrying (or hard_reset) won't help -- tell the
    user, who has to leave the computer alone / unlock it. For other
    repeated failures, hard_reset restarts Responsa."""
    if lines_per_result is not None and (
        isinstance(lines_per_result, bool) or not 1 <= lines_per_result <= 21
    ):
        # ToolError, not ValueError -- see get_result_text below / lifecycle.locked_call:
        # only ToolError's message reaches the calling agent.
        raise ToolError(
            f"lines_per_result must be an integer from 1 to 21, got {lines_per_result!r}"
        )
    resolved = resolve_books(books)
    result = await locked_call(
        lambda client: client.search(
            query, books=resolved,
            search_all_databases=search_all_databases, max_hits=max_hits,
            lines_per_result=lines_per_result,
        )
    )
    return to_jsonable(result)


@_log_calls
async def get_result_text(index: int) -> Dict[str, Any]:
    """Fetch the full text of one result of the most recent `search` call
    in this session.

    NEVER call this in parallel with another call to this tool or to
    search/browse/hard_reset -- Responsa automates a real desktop GUI
    app and can only do one thing at a time. Call tools ONE AT A TIME:
    wait for this call's result before calling the next tool. A
    parallel/overlapping call is rejected immediately with a clear
    error, not queued -- it will NOT run once the first one finishes.

    `index` is the result's 1-based number -- the `index` field of one
    of the `hits` `search` returned. Can be called repeatedly for
    different results of the same search (one at a time, waiting for
    each). A long source (a whole chapter) can take a few minutes to
    fetch."""
    if not isinstance(index, int) or index < 1:
        # ToolError, not ValueError -- see lifecycle.locked_call's
        # docstring: only ToolError's message reaches the calling agent
        # with this mcp SDK version, any other exception is masked.
        raise ToolError(
            f"index must be a positive integer (a hit's `index` from search), got {index!r}"
        )
    result = await locked_call(lambda client: client.get_result_text(index))
    return to_jsonable(result)


@_log_calls
async def hard_reset(clear_windows: bool = True) -> Dict[str, Any]:
    """Actually quit Responsa (every running instance, not just this
    server's) and start a fresh one -- for recovering when the app is
    stuck or behaving oddly.

    NEVER call this in parallel with another call to this tool or to
    search/get_result_text/browse -- Responsa automates a real desktop
    GUI app and can only do one thing at a time. Call tools ONE AT A
    TIME: wait for this call's result before calling the next tool. A
    parallel/overlapping call is rejected immediately with a clear
    error, not queued -- it will NOT run once the first one finishes.

    This affects the real app on this machine, so only use it if nothing
    else needs the current session to stay up. Takes longer than a
    normal call (Responsa's own relaunch plus
    restoring its previous windows). `clear_windows` (default true):
    once the fresh instance is up, also close every window it restores
    from its last session, leaving a genuinely clean slate; set false to
    leave those restored windows as Responsa put them. Don't use it for
    focus or locked-session errors: restarting doesn't fix those."""
    await locked_call(lambda client: client.hard_reset(clear_windows=clear_windows))
    return {"ok": True}


@_log_calls
async def browse(
    path: Optional[List[str]] = None,
    exact: bool = False,
) -> Dict[str, Any]:
    """Walk Responsa's sources tree (its Browse/"עיון" feature) one call
    at a time: a hierarchy of categories, books, chapters, etc. down to
    the texts themselves.

    NEVER call this in parallel with another call to this tool or to
    search/get_result_text/hard_reset -- Responsa automates a real
    desktop GUI app and can only do one thing at a time. Call tools ONE
    AT A TIME: wait for this call's result before calling the next tool
    -- this matters even more here, since each call also depends on
    where the PREVIOUS one left the walk. A parallel/overlapping call
    is rejected immediately with a clear error, not queued -- it will
    NOT run once the first one finishes.

    Each call continues from wherever the previous call in this session
    left off. Omit `path` (or pass null) to reset
    to the top level and list its categories. Pass one or more names to
    descend one step per name, e.g. ["תנ\\"ך", "בראשית", "א"] -- by
    default a name only has to identify the entry ("ב" matches "פרק ב"),
    set `exact=true` to require the complete text. If the call lands on
    a section, `options` lists its children for the next call. If it
    lands on a text (a leaf), `text`/`text_path` hold its full content
    and the position stays at the text's *parent* section, so its
    siblings are one call away. A wrong or ambiguous step leaves the
    position unchanged and sets `error` to one of "NOT_FOUND",
    "AMBIGUOUS", "NOT_A_SECTION", plus `message` and, for "AMBIGUOUS",
    `candidates`."""
    result = await locked_call(lambda client: client.browse(path, exact=exact))
    return to_jsonable(result)
