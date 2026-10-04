"""The data types returned by ResponsaClient: `SearchResults` (from
`search()`), its `Hit` entries, and `SourceText` (from `get_result_text()`).
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


@dataclass
class Hit:
    """One search result.

    `index` is the 1-based number Responsa itself shows for this result --
    pass it to `ResponsaClient.get_result_text()` to fetch the source's full
    text. `snippet` is only the short excerpt Responsa displays in its
    results list, not the whole source.
    """
    index: int
    citation: str
    snippet: str


@dataclass
class SearchResults:
    """Everything `ResponsaClient.search()` returns.

    `hits` is empty for a query with no matches (that is not an error).
    """
    query: Optional[str]
    hits: List[Hit] = field(default_factory=list)
    # The exported results text before it was split into hits -- mainly
    # useful for debugging the parser.
    raw_text: str = ""
    # The total number of results Responsa itself reported for the query
    # (e.g. 2974), which can be larger than len(hits) when `max_hits` or
    # `time_budget` limited how many were actually extracted. None when
    # unavailable (e.g. a query with no matches never opens a results
    # window).
    total_hits: Optional[int] = None
    # True when `hits` was deliberately cut short of `total_hits` -- by an
    # explicit `max_hits`, or automatically to fit the client's
    # `time_budget`.
    truncated: bool = False


@dataclass
class SourceText:
    """The full text of one search result, from
    `ResponsaClient.get_result_text()`.

    `citation` is the source's title as Responsa shows it (usually a
    chapter-level heading, so it can be shorter than the `Hit.citation`
    it was fetched for); `text` is the complete content.
    """
    citation: Optional[str]
    text: str
    # The exported text before cleanup (page numbers, header/footer
    # lines) -- mainly useful for debugging the parser.
    raw_text: str = ""


class BrowseError(str, Enum):
    """Why a `ResponsaClient.browse()` call was rejected."""
    # A step matched none of the entries at that level.
    NOT_FOUND = "NOT_FOUND"
    # A step matched more than one entry at that level (see
    # `BrowseResult.candidates`) -- write it more fully to pick one.
    AMBIGUOUS = "AMBIGUOUS"
    # A step tried to go below an entry that has nothing below it (it is a
    # text, not a section).
    NOT_A_SECTION = "NOT_A_SECTION"


@dataclass
class BrowseResult:
    """What one `ResponsaClient.browse()` call returns; exactly one of
    `options`, `text` or `error` describes the outcome.

    * a section was reached: `options` lists its entries, to choose from
      in the next call;
    * a text was reached: `text` holds its full content, and the position
      stays at the section that contains it;
    * the call was rejected: `error` says why and the position is exactly
      as it was before the call.
    """
    # The names from the top down to the current position: the reached
    # section, or the section containing the text that was returned. On an
    # error it is the unchanged position from before the call.
    path: List[str] = field(default_factory=list)
    options: Optional[List[str]] = None
    text: Optional[SourceText] = None
    # The names of the text's own path, when `text` is set (`path` plus the
    # text's own name).
    text_path: Optional[List[str]] = None
    error: Optional[BrowseError] = None
    # A human-readable explanation when `error` is set.
    message: str = ""
    # When `error` is set: which step of the call's path (0-based) was
    # rejected, and, for AMBIGUOUS, the entries it matched.
    failed_step: Optional[int] = None
    candidates: Optional[List[str]] = None

    @property
    def ok(self) -> bool:
        return self.error is None
