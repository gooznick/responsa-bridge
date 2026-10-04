"""Maps the MCP-facing `books` parameter (a plain list of strings, for a
JSON-schema-friendly tool signature) onto what ResponsaClient.search()
actually accepts (BookScopeInput: a BookScope, a free-text sources-tree
node name, or a list of either)."""
from typing import List, Optional, Union

from responsa_api import BookScope


def resolve_books(books: Optional[List[str]]) -> Optional[List[Union[BookScope, str]]]:
    """Each entry is matched, case-insensitively, against a BookScope
    member name (spaces/hyphens folded to underscores, e.g. "old shut" or
    "OLD-SHUT" both match BookScope.OLD_SHUT); anything that doesn't
    match is passed through unchanged as a free-text sources-tree node
    name, exactly like passing a plain string to
    ResponsaClient.search(books=...) directly. None/empty stays None
    (search()'s own default: whole database)."""
    if not books:
        return None
    resolved: List[Union[BookScope, str]] = []
    for item in books:
        key = item.strip().upper().replace(" ", "_").replace("-", "_")
        resolved.append(BookScope.__members__.get(key, item))
    return resolved
