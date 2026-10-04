"""Pure (no GUI) side of the Browse API: the cached sources tree and the
navigation/matching rules over it.

The tree is crawled once from Responsa's own Browse dialog by
scripts/build_browse_cache.py and stored next to the per-version config
(versions/v33_browse_cache/): an `index.json` manifest naming the
top-level categories, and one gzip-JSON file per category. Categories are
loaded on first use, because the whole tree is large.

Node format in those files: a leaf (a text) is its name as a string; a
section is `[name, [child, child, ...]]`. Sibling order is Responsa's own
and duplicate names are kept, so every node is addressed by its INDEX path
(a tuple of child indices, the first being the category) -- that is also
exactly how the GUI side finds the same item in the live tree.
"""
import gzip
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ..exceptions import ResponsaBrowseCacheError
from ..results import BrowseError

# Lives next to the per-version config (versions/v33.py) because the tree's
# content -- and even its order -- belongs to one installed Responsa build.
DEFAULT_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "versions", "v33_browse_cache")
MANIFEST_NAME = "index.json"

Node = Union[str, list]
IndexPath = Tuple[int, ...]


def category_file_name(index: int) -> str:
    return f"{index:02d}.json.gz"


def node_name(node: Node) -> str:
    return node if isinstance(node, str) else node[0]


def node_children(node: Node) -> List[Node]:
    return [] if isinstance(node, str) else node[1]


def build_manifest(cache_dir: str = DEFAULT_CACHE_DIR) -> List[str]:
    """(Re)write `index.json` from the category files present, in order,
    and return the category names. Every category from 0 up to the last
    file must exist."""
    names = []
    i = 0
    while os.path.exists(os.path.join(cache_dir, category_file_name(i))):
        with gzip.open(os.path.join(cache_dir, category_file_name(i)), "rt", encoding="utf-8") as f:
            names.append(node_name(json.load(f)))
        i += 1
    with open(os.path.join(cache_dir, MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump({"categories": names}, f, ensure_ascii=False, indent=1)
    return names


class BrowseTree:
    """The cached tree. The virtual root's children are the categories."""

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR):
        self.cache_dir = cache_dir
        self._categories: Dict[int, Node] = {}
        try:
            with open(os.path.join(cache_dir, MANIFEST_NAME), encoding="utf-8") as f:
                self._names: List[str] = json.load(f)["categories"]
        except FileNotFoundError:
            raise ResponsaBrowseCacheError(
                f"No Browse tree cache in {cache_dir} -- build it with "
                "scripts/build_browse_cache.py") from None
        for i in range(len(self._names)):
            if not os.path.exists(os.path.join(cache_dir, category_file_name(i))):
                raise ResponsaBrowseCacheError(
                    f"Browse tree cache is incomplete: {category_file_name(i)} is missing")

    def category_names(self) -> List[str]:
        return list(self._names)

    def _category(self, i: int) -> Node:
        if i not in self._categories:
            with gzip.open(os.path.join(self.cache_dir, category_file_name(i)),
                           "rt", encoding="utf-8") as f:
                self._categories[i] = json.load(f)
        return self._categories[i]

    def node(self, path: Sequence[int]) -> Optional[Node]:
        """The node at `path`, or None for the virtual root (empty path)."""
        if not path:
            return None
        node = self._category(path[0])
        for i in path[1:]:
            node = node_children(node)[i]
        return node

    def child_names(self, path: Sequence[int]) -> List[str]:
        if not path:
            return self.category_names()
        return [node_name(c) for c in node_children(self.node(path))]

    def is_leaf(self, path: Sequence[int]) -> bool:
        return bool(path) and isinstance(self.node(path), str)

    def names_along(self, path: Sequence[int]) -> List[str]:
        """The name at every level of `path`, top down."""
        names = []
        for depth in range(1, len(path) + 1):
            names.append(node_name(self.node(path[:depth])))
        return names


# -- matching -------------------------------------------------------------

_QUOTES = dict.fromkeys(map(ord, "\"'״׳“”‘’`"))
_PUNCT = re.compile(r"[()\[\]{}<>,.:;!?\-–—/\\_*&|]")


def normalize(text: str) -> str:
    """Forgiving form for comparing names: no vowel points/cantillation,
    no quote marks or geresh (תנ"ך == תנך), other punctuation and
    bracket direction artifacts as spaces, collapsed whitespace."""
    text = "".join(c for c in unicodedata.normalize("NFC", text)
                   if not unicodedata.category(c).startswith("M"))
    text = text.translate(_QUOTES)
    text = _PUNCT.sub(" ", text)
    return " ".join(text.split())


def _contains_words(words: List[str], sub: List[str]) -> bool:
    n = len(sub)
    return n > 0 and any(words[i:i + n] == sub for i in range(len(words) - n + 1))


def match_step(names: Sequence[str], query: str, exact: bool = False) -> List[int]:
    """Indexes into `names` that `query` selects: zero (nothing matches),
    one (the choice), or several (ambiguous).

    exact=True: only entries whose text equals the query (surrounding
    whitespace ignored). Otherwise the first of these tiers that matches
    anything decides:
      1. the same text;
      2. the same text after normalize();
      3. the query's words are the START of the entry's words -- so
         'תנ"ך' selects '(תנ"ך (החומש מחולק לפרקים' and not also
         '(מפרשי תנ"ך (החומש...';
      4. the query's words appear, in order and adjacent, anywhere among
         the entry's words -- so "ב" selects "פרק ב" and "יחזקאל" selects
         "ספר יחזקאל", while "פרק" alone matches every chapter (ambiguous).
    """
    q = query.strip()
    hits = [i for i, n in enumerate(names) if n.strip() == q]
    if hits or exact:
        return hits
    nq = normalize(q)
    if not nq:
        return []
    hits = [i for i, n in enumerate(names) if normalize(n) == nq]
    if hits:
        return hits
    qw = nq.split()
    words = [normalize(n).split() for n in names]
    hits = [i for i, w in enumerate(words) if w[:len(qw)] == qw]
    if hits:
        return hits
    return [i for i, w in enumerate(words) if _contains_words(w, qw)]


# -- navigation -----------------------------------------------------------

@dataclass
class Resolution:
    """The outcome of applying a call's steps from a position.

    On success `position` is where the client is afterwards (the reached
    section, or -- when a text was reached -- the section containing it)
    and `leaf` is the full index path of the text, if one was reached.
    On failure `position` is the UNCHANGED starting position.
    """
    position: IndexPath
    leaf: Optional[IndexPath] = None
    error: Optional[BrowseError] = None
    message: str = ""
    failed_step: Optional[int] = None
    candidates: Optional[List[str]] = None


def resolve(tree: BrowseTree, position: IndexPath, steps: Sequence[str],
            exact: bool = False) -> Resolution:
    """Apply `steps` from `position`, all or nothing."""
    cur = tuple(position)
    for n, step in enumerate(steps):
        if tree.is_leaf(cur):  # only possible after a text was reached earlier in this call
            return Resolution(position, error=BrowseError.NOT_A_SECTION, failed_step=n,
                              message=f"{tree.names_along(cur)[-1]!r} is a text; nothing lies below it")
        names = tree.child_names(cur)
        hits = match_step(names, step, exact)
        if not hits:
            return Resolution(position, error=BrowseError.NOT_FOUND, failed_step=n,
                              message=f"No entry matching {step!r} at this level "
                                      f"({len(names)} entries)")
        if len(hits) > 1:
            return Resolution(position, error=BrowseError.AMBIGUOUS, failed_step=n,
                              candidates=[names[i] for i in hits],
                              message=f"{step!r} matches {len(hits)} entries at this level")
        cur = cur + (hits[0],)
    if tree.is_leaf(cur):
        return Resolution(cur[:-1], leaf=cur)
    return Resolution(cur)
