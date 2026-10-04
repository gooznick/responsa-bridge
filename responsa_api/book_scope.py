"""BookScope: the optional book/corpus-selection parameter for
ResponsaClient.search().

Each value maps to one or more node names in the "sources tree" (the
checkbox tree behind the "המאגרים המשתתפים" nav button in Advanced
Search) -- see internal/versions/v33.py's BOOK_SCOPE_NODE_TEXTS for the
mapping, and scripts/sources_tree.txt for the full dumped tree it was
read from (its node names are also what `search(books="...")` accepts as
free text). Most scopes are a single tree node; TORA has no single
matching node (תנ"ך's direct children are the individual books, not a
"Torah" grouping), so it's a union of the five Torah books' leaf nodes.
"""
from enum import Enum
from typing import List, Union


class BookScope(Enum):
    ALL = "all"
    SHAS = "shas"
    TORA = "tora"
    BIBLE = "bible"
    MISHNA = "mishna"
    GEMARA = "gemara"
    CHAZAL_LITERATURE = "chazal_literature"
    RAMBAM = "rambam"
    SHUT = "shut"
    OLD_SHUT = "old_shut"
    NEW_SHUT = "new_shut"


# What `ResponsaClient.search(books=...)` accepts: one scope, or a list of
# scopes to search their union. A scope is a BookScope member or a plain
# string naming a node in the sources tree (see scripts/sources_tree.txt).
BookScopeInput = Union[BookScope, str, List[Union[BookScope, str]]]
