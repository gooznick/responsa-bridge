"""
Read-only dump of the Responsa "sources tree" (checkbox tree of books/databases,
dialog titled 'עץ מקורות') to an indented text file.

Does not click or change anything -- only sends TVM_GETNEXTITEM / TVM_GETITEMW
read messages (via pywinauto's TreeViewWrapper), which is how pywinauto reads
any TreeView control's contents regardless of what's visually expanded.

Usage:
    python dump_tree.py [output_path]

Assumes RESPONSA.exe is already running with the "עץ מקורות" dialog open
(Search -> Advanced Search -> "המאגרים המשתתפים" -> tree view showing).
"""
import sys

import win32gui
import win32process
from pywinauto.controls.common_controls import TreeViewWrapper

RESPONSA_IMAGE_NAME = "RESPONSA.exe"
TREE_DIALOG_TITLE = "עץ מקורות"
TREE_CONTROL_CLASS = "SysTreeView32"

DEFAULT_OUTPUT = "sources_tree.txt"


def find_responsa_pids():
    import win32com.client
    pids = []
    wmi = win32com.client.GetObject("winmgmts:")
    for proc in wmi.InstancesOf("Win32_Process"):
        if proc.Name and proc.Name.lower() == RESPONSA_IMAGE_NAME.lower():
            pids.append(proc.ProcessId)
    return pids


def find_tree_hwnd():
    pids = set(find_responsa_pids())
    if not pids:
        raise RuntimeError("RESPONSA.exe is not running")

    top_level_hwnds = []

    def top_level_cb(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in pids:
            top_level_hwnds.append(hwnd)
        return True

    win32gui.EnumWindows(top_level_cb, None)

    candidates = []

    def collect_descendants(hwnd):
        def child_cb(child_hwnd, _):
            if win32gui.GetClassName(child_hwnd) == TREE_CONTROL_CLASS and win32gui.IsWindowVisible(child_hwnd):
                candidates.append(child_hwnd)
            collect_descendants(child_hwnd)
            return True
        win32gui.EnumChildWindows(hwnd, child_cb, None)

    for hwnd in top_level_hwnds:
        collect_descendants(hwnd)

    if not candidates:
        raise RuntimeError(
            f"Could not find a visible {TREE_CONTROL_CLASS} control anywhere in "
            f"RESPONSA.exe's windows. Is the sources-tree dialog ({TREE_DIALOG_TITLE!r}) open?"
        )

    return candidates[0]


MAX_DEPTH = 2  # 0-indexed -> 3 levels total
ORDINAL_MIN_ITEMS = 3
ORDINAL_MIN_SHARE = 0.7


def is_ordinal_group(children):
    """Detect a numbered/enumerated sibling list (e.g. 'פרק א'..'פרק נ', 'סימן א'..)
    by checking whether most siblings share the same first word."""
    if len(children) < ORDINAL_MIN_ITEMS:
        return False
    first_words = [(c.text().split() or [c.text()])[0] for c in children]
    most_common = max(set(first_words), key=first_words.count)
    return first_words.count(most_common) / len(first_words) >= ORDINAL_MIN_SHARE


def dump_children(f, children, depth):
    if is_ordinal_group(children):
        indent = "    " * depth
        f.write(f"{indent}[...] {len(children)} ordered items: {children[0].text()!r} .. {children[-1].text()!r}\n")
        return
    for child in children:
        dump_node(f, child, depth)


def dump_node(f, node, depth):
    checked = node.is_checked()
    mark = "[x]" if checked else "[ ]"
    f.write(("    " * depth) + f"{mark} {node.text()}\n")
    if depth >= MAX_DEPTH:
        return
    # force lazy-loaded children to populate (control message, not a UI click)
    node.expand()
    dump_children(f, node.children(), depth + 1)


def main():
    output_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUTPUT

    tree_hwnd = find_tree_hwnd()
    print(f"Found sources tree control: hwnd={tree_hwnd}")

    tree = TreeViewWrapper(tree_hwnd)
    total = tree.item_count()
    print(f"Tree reports {total} total items. Walking and writing to {output_path} ...")

    with open(output_path, "w", encoding="utf-8") as f:
        dump_children(f, tree.roots(), 0)

    print("Done.")


if __name__ == "__main__":
    main()
