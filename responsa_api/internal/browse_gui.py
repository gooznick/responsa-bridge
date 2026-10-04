"""Driving Responsa's Browse ("עיון") dialog: opening it on its sources
tree, and reading/selecting that tree's items quickly.

Nothing here knows about the cached tree or name matching (browse_tree.py);
this is only the GUI side.
"""
import ctypes
from typing import List, Sequence, Tuple

import win32con
import win32gui
from pywinauto import win32defines, win32structures
from pywinauto.controls.common_controls import TabControlWrapper, TreeViewWrapper, _treeview_element
from pywinauto.handleprops import is64bitprocess
from pywinauto.remote_memory_block import RemoteMemoryBlock
from pywinauto.sysinfo import is_x64_Python

from . import winutil
from ..exceptions import ResponsaBrowseCacheError

TCM_GETITEMCOUNT = 0x1304
WM_SETREDRAW = 0x000B
TVM_SELECTITEM = 0x110B
TVGN_CARET = 0x0009
# TVE_COLLAPSE | TVE_COLLAPSERESET: collapse AND free the item's children,
# so crawling a huge tree doesn't leave millions of items alive inside
# Responsa's own process.
TVE_COLLAPSE_RESET = 0x8001


def open_browse_tree(automation) -> Tuple[int, int]:
    """Open the Browse dialog on its "עץ מקורות" tab and return
    (dialog_hwnd, tree_hwnd). `automation` is a started ResponsaAutomation.

    Posted rather than sent for the same reason as every dialog-opening
    command (see ResponsaAutomation._post_command) even though this one
    turns out to be modeless.
    """
    cfg = automation.config
    automation._post_command(automation._main_hwnd, cfg.COMMAND_OPEN_BROWSE)
    dialog = automation._find_visible_dialog(cfg.BROWSE_DIALOG_TITLE)
    tab = winutil.find_child_by_id(dialog, cfg.BROWSE_TAB_CONTROL_ID)
    # Confirmed live: right after the dialog appears its tab control can
    # still be empty (0 tabs), and a reopened dialog remembers whichever
    # tab was last active -- so wait for the tabs, then always select ours.
    winutil.wait_for(lambda: win32gui.SendMessage(tab, TCM_GETITEMCOUNT, 0, 0) > 0, timeout=10)
    TabControlWrapper(tab).select(cfg.BROWSE_SOURCES_TAB_INDEX)

    def visible_tree():
        found = winutil.find_descendants(dialog, class_name=cfg.BROWSE_TREE_CLASS)
        return found[0] if found else None

    try:
        tree = winutil.wait_for(visible_tree, timeout=10)
    except TimeoutError as e:
        raise LookupError("no visible tree on the Browse dialog's sources tab") from e
    return dialog, tree


def reset_tree(reader: "TreeReader"):
    """Collapse every top-level category, discarding whatever a PRIOR
    navigation in this same dialog (it can outlive a single
    browse_get_text call -- Responsa keeps reusing the same modeless
    dialog/tree instance rather than recreating it each time) left
    expanded. Defensive: cheap, and removes any chance of stale
    expansion/scroll state from an earlier call affecting this one.
    Also forces painting back on -- a crawl killed mid-run (Ctrl+C /
    taskkill, not a clean exit) can leave WM_SETREDRAW stuck off on this
    same tree, since that is real window state, not process state.
    """
    reader.set_redraw(True)
    for r in reader.roots():
        reader.collapse_reset(r)


def find_item(reader: "TreeReader", index_path: Sequence[int], names: Sequence[str]) -> int:
    """The live tree's HTREEITEM at `index_path` (child indices from the
    top, as in browse_tree.py), expanding each level on the way.

    `names` are the cached names along that path; every live item's text
    is checked against them, so a cache that no longer matches this
    Responsa's tree fails loudly rather than opening some other text.
    """
    level = reader.roots()
    hitem = 0
    for depth, (idx, expected) in enumerate(zip(index_path, names)):
        if idx >= len(level):
            raise ResponsaBrowseCacheError(
                f"The Browse tree cache is out of date: level {depth + 1} has "
                f"only {len(level)} entries, expected at least {idx + 1} "
                f"(looking for {expected!r})")
        hitem = level[idx]
        text, has_children = reader.read(hitem)
        if text != expected:
            raise ResponsaBrowseCacheError(
                f"The Browse tree cache is out of date: expected {expected!r} "
                f"at level {depth + 1}, Responsa shows {text!r}")
        if depth + 1 < len(index_path):
            if not has_children:
                raise ResponsaBrowseCacheError(
                    f"The Browse tree cache is out of date: {text!r} has nothing below it")
            reader.expand(hitem)
            level = reader.children(hitem)
    return hitem


def open_leaf_via_keyboard(automation, reader: "TreeReader", dialog_hwnd: int, tree_hwnd: int,
                            index_path: Sequence[int], names: Sequence[str]):
    """Move the tree's real highlight to the leaf at `index_path`, using
    real Home/Down key presses, then press a real Enter to open it --
    the user's own suggestion, after two other mechanisms
    both proved unreliable for actually OPENING the right item (though
    fine for pure structure reading/expanding, which stays message-based
    below):
      - a message-based TVM_SELECTITEM does move the visual highlight,
        but does not register as a real selection with the app's own
        "what's highlighted" logic -- confirmed live, a leaf identified
        correctly by find_item still opened an unrelated, already-open
        text regardless of double-click or a dedicated button.
      - a REAL (hardware-simulated) double-click, at a correctly
        computed on-screen point, worked once but was not reproducible
        -- the tree's own item rect can shift between the click-down and
        click-up in ways message-based selection doesn't have to worry
        about.
    Real arrow keys have neither problem: each Down press is exactly one
    visible row, counted from a known start (Home = the first root),
    with every intervening level already collapsed by reset_tree() so
    the count isn't thrown off by leftover expansion from a prior call.
    """
    # NOT calling _safe_set_focus(tree_hwnd) here: confirmed live, doing
    # so can itself make GetForegroundWindow() start reporting the TREE's
    # own hwnd rather than the dialog's -- apparently a lasting change,
    # since it then tripped a later, unrelated call's _require_foreground
    # check too. The caller already brought the DIALOG to real
    # foreground, which restores whichever control it last remembered as
    # focused -- the tree, in every case seen so far (it's the dialog's
    # only real control) -- so real key presses already reach it without
    # this extra, apparently not side-effect-free step.
    automation._send_keys(win32con.VK_HOME)
    hitem = None
    for depth, (idx, expected) in enumerate(zip(index_path, names)):
        if depth > 0:
            # already sitting on the parent (previous iteration) -- expand
            # it (structural, message-based -- safe, see class docstring)
            # and move onto its first child, then further down to `idx`.
            reader.expand(hitem)
            downs = idx + 1
        else:
            downs = idx  # Home already put us on root 0
        for _ in range(downs):
            automation._send_keys(win32con.VK_DOWN)
        hitem = reader.caret()
        text, _ = reader.read(hitem)
        if text != expected:
            raise ResponsaBrowseCacheError(
                f"Keyboard navigation landed on {text!r}, expected {expected!r} at "
                f"level {depth + 1} -- the Browse tree cache may be out of date")
    automation._send_keys(win32con.VK_RETURN)


class TreeReader:
    """Reads a SysTreeView32 that lives in ANOTHER process (Responsa is
    32-bit, so this is the cross-bitness case pywinauto handles).

    pywinauto's own item reads allocate and free a remote memory block per
    item; this keeps one for the whole crawl, roughly an order of
    magnitude faster over the ~100k+ items the whole Browse tree holds.
    Items are HTREEITEM handles (ints).
    """

    def __init__(self, tree_hwnd: int):
        self.hwnd = tree_hwnd
        self._ctrl = TreeViewWrapper(tree_hwnd)
        self._mem = RemoteMemoryBlock(self._ctrl)
        if is64bitprocess(self._ctrl.process_id()) or not is_x64_Python():
            self._item_cls = win32structures.TVITEMW
        else:
            self._item_cls = win32structures.TVITEMW32
        self._text_chars = 512
        self._text_addr = self._mem.Address() + ctypes.sizeof(self._item_cls) + 16
        self._text_buf = ctypes.create_unicode_buffer(self._text_chars)

    def close(self):
        """Free the remote memory block, and repaint the tree if painting
        was switched off."""
        self.set_redraw(True)
        self._mem.CleanUp()

    def set_redraw(self, on: bool):
        """Switch the tree's painting off while crawling: every expansion
        otherwise makes it lay out and repaint its (up to hundreds of
        thousands of) visible lines, which is most of the crawl's time."""
        if win32gui.IsWindow(self.hwnd):
            self._send(WM_SETREDRAW, 1 if on else 0, 0)
            if on:
                win32gui.InvalidateRect(self.hwnd, None, True)

    def _send(self, msg: int, wparam: int, lparam: int) -> int:
        return win32gui.SendMessage(self.hwnd, msg, wparam, lparam)

    def roots(self) -> List[int]:
        return self._siblings(self._send(win32defines.TVM_GETNEXTITEM, win32defines.TVGN_ROOT, 0))

    def caret(self) -> int:
        """The item currently highlighted/focused in the tree."""
        return self._send(win32defines.TVM_GETNEXTITEM, TVGN_CARET, 0)

    def children(self, hitem: int) -> List[int]:
        """Direct children of `hitem`, which must already be expanded (the
        tree populates lazily)."""
        return self._siblings(self._send(win32defines.TVM_GETNEXTITEM, win32defines.TVGN_CHILD, hitem))

    def _siblings(self, first: int) -> List[int]:
        out = []
        cur = first
        while cur:
            out.append(cur)
            cur = self._send(win32defines.TVM_GETNEXTITEM, win32defines.TVGN_NEXT, cur)
        return out

    def read(self, hitem: int) -> Tuple[str, bool]:
        """(text, has_children) -- has_children is the item's "+"."""
        # SendMessage to a destroyed window just returns 0, which would
        # look like "no more children" -- e.g. someone closed the dialog
        # mid-crawl. Fail instead of quietly recording a truncated tree.
        if not win32gui.IsWindow(self.hwnd):
            raise LookupError("the Browse tree window was closed")
        item = self._item_cls()
        item.mask = win32defines.TVIF_TEXT | win32defines.TVIF_HANDLE | win32defines.TVIF_CHILDREN
        item.pszText = self._text_addr
        item.cchTextMax = self._text_chars
        item.hItem = hitem
        self._mem.Write(item)
        if not self._send(win32defines.TVM_GETITEMW, 0, self._mem.Address()):
            raise ctypes.WinError()
        self._mem.Read(item)
        self._mem.Read(self._text_buf, self._text_addr)
        return self._text_buf.value, bool(item.cChildren)

    def expand(self, hitem: int):
        self._send(win32defines.TVM_EXPAND, win32defines.TVE_EXPAND, hitem)

    def collapse_reset(self, hitem: int):
        self._send(win32defines.TVM_EXPAND, TVE_COLLAPSE_RESET, hitem)

    def select(self, hitem: int):
        """NOT USED -- kept as a documented dead end. A message-based
        TVM_SELECTITEM does move the visual highlight, but confirmed live
        that it does not register as a real selection with
        the app's own "what's highlighted" logic: a leaf selected this
        way, then opened (by double-click or the dialog's own button),
        opened an unrelated, already-open text instead. Use
        open_leaf_via_keyboard, which moves the highlight with real
        Home/Down key presses instead."""
        self._send(TVM_SELECTITEM, TVGN_CARET, hitem)

    def double_click(self, hitem: int):
        """NOT USED -- kept as a documented dead end. A REAL,
        hardware-simulated double click (pywinauto's click_input()) at a
        freshly, correctly computed on-screen point opened the right
        text once in isolation, but was not reproducible in the actual
        test flow -- confirmed live, it opened an unrelated,
        already-open text just like the message-based approaches above,
        so whatever makes this app's real selection state change
        reliably is narrower than "any real click on the right point".
        Use open_leaf_via_keyboard (real Home/Down/Enter key presses --
        the user's own suggestion, confirmed reliable) instead."""
        _treeview_element(hitem, self._ctrl).click_input(double=True)
