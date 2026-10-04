"""Low-level, version-agnostic Win32 window/process helpers used by the
automation driver (automation.py).

Nothing here knows about Responsa specifically -- all Responsa-specific
identifiers live in the versions/ package next to this module.
"""
import ctypes
import time
from typing import Callable, Iterable, List, Optional, TypeVar

import pywintypes
import win32api
import win32com.client
import win32con
import win32event
import win32gui
import win32process
from pywinauto import win32defines, win32structures
from pywinauto.handleprops import is64bitprocess
from pywinauto.remote_memory_block import RemoteMemoryBlock
from pywinauto.sysinfo import is_x64_Python

T = TypeVar("T")


def find_pids(image_name: str) -> List[int]:
    """Return PIDs of all running processes with the given image name.

    Uses WMI rather than psutil (not a dependency here) or tasklist
    parsing -- pywin32 (already a dependency) ships a WMI client.
    """
    pids = []
    wmi = win32com.client.GetObject("winmgmts:")
    for proc in wmi.InstancesOf("Win32_Process"):
        if proc.Name and proc.Name.lower() == image_name.lower():
            pids.append(proc.ProcessId)
    return pids


def terminate_process(pid: int, graceful_hwnd: Optional[int] = None, timeout: float = 15) -> None:
    """Close a process by PID -- for hard_reset()'s "actually quit
    Responsa, not just detach" step, which close() deliberately never
    does (see its docstring).

    Tries a graceful WM_CLOSE to `graceful_hwnd` first if given (the same
    request a user clicking the window's own close button sends, letting
    the app shut down normally), then force-terminates if the process
    hasn't actually exited within `timeout` -- waiting indefinitely isn't
    an option here since this exists specifically to recover from an app
    that might be stuck and never processing that message at all.

    A process handle, not polling find_pids() again, is used to detect
    exit: WaitForSingleObject on a process handle blocks until the OS
    itself reports the process gone, no polling interval to tune and no
    race against a PID being reused in between checks.
    """
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_INFORMATION,
            False, pid,
        )
    except pywintypes.error:
        return  # already gone
    try:
        if graceful_hwnd is not None and win32gui.IsWindow(graceful_hwnd):
            try:
                win32gui.PostMessage(graceful_hwnd, win32con.WM_CLOSE, 0, 0)
            except pywintypes.error:
                pass
        if win32event.WaitForSingleObject(handle, int(timeout * 1000)) != win32event.WAIT_OBJECT_0:
            win32api.TerminateProcess(handle, 1)
            win32event.WaitForSingleObject(handle, 5000)
    finally:
        win32api.CloseHandle(handle)


def enum_top_level_windows_for_pids(pids: Iterable[int]) -> List[int]:
    """Return every top-level window handle owned by any of the given PIDs."""
    pid_set = set(pids)
    results = []

    def callback(hwnd, _):
        _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
        if found_pid in pid_set:
            results.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    return results


def find_child_by_id(parent_hwnd: int, control_id: int, recursive: bool = False) -> int:
    """Find a child control by its dialog control ID.

    Prefer this over win32gui.GetDlgItem: GetDlgItem relies on the dialog
    manager's internal traversal and was found (empirically, against
    Responsa's main frame window) to raise "Control ID not found" for
    controls that plainly exist as immediate children with that exact ID
    -- EnumChildWindows + GetDlgCtrlID, as used by the discovery scripts,
    does not have that problem.

    `recursive=True` walks all descendants, not just immediate children --
    needed for e.g. modern Explorer-style common dialogs, which nest their
    controls (filename combo box, etc.) several levels deep inside a Shell
    UI hierarchy, unlike Responsa's own classic dialogs where one level is
    normally enough.
    """
    match = []

    def cb(child, _):
        if win32gui.GetDlgCtrlID(child) == control_id:
            match.append(child)
        return True

    if recursive:
        def visit(hwnd):
            def child_cb(child, _):
                cb(child, None)
                visit(child)
                return True
            win32gui.EnumChildWindows(hwnd, child_cb, None)
        visit(parent_hwnd)
    else:
        win32gui.EnumChildWindows(parent_hwnd, cb, None)

    if not match:
        raise LookupError(f"No child of {parent_hwnd} with control id {control_id}")
    return match[0]


def find_descendants(hwnd: int, class_name: Optional[str] = None,
                      visible_only: bool = True) -> List[int]:
    """Recursively collect descendant windows of hwnd, optionally filtered
    by class name.

    Needed because some Responsa dialogs are nested inside other dialogs
    (e.g. the "עץ מקורות" sources tree lives inside "ניהול המאגרים"), so a
    single-level EnumChildWindows pass isn't always enough.
    """
    found = []

    def visit(parent):
        def child_cb(child, _):
            if (class_name is None or win32gui.GetClassName(child) == class_name) and \
               (not visible_only or win32gui.IsWindowVisible(child)):
                found.append(child)
            visit(child)
            return True
        win32gui.EnumChildWindows(parent, child_cb, None)

    visit(hwnd)
    return found


def set_tree_item_checked(tree_ctrl, htreeitem: int, checked: bool):
    """Set a SysTreeView32 item's checkbox state directly via TVM_SETITEMW,
    mirroring how pywinauto's `_treeview_element.is_checked()` reads it
    (TVM_GETITEMSTATE / TVIS_STATEIMAGEMASK).

    Needed because this app's tree checkboxes are not hit-testable via
    TVM_HITTEST -- confirmed live: pywinauto's `.click(where="check")`
    raises "Area ('check') not found for this tree view item" even for a
    visible, correctly-scrolled-to item. Setting the state bits directly
    bypasses hit-testing/mouse simulation entirely.

    `tree_ctrl` must be a pywinauto TreeViewWrapper (or any HwndWrapper
    for the SysTreeView32 control); `htreeitem` is the raw HTREEITEM
    handle (e.g. from a `_treeview_element`'s `.elem`).
    """
    remote_mem = RemoteMemoryBlock(tree_ctrl)
    try:
        if is64bitprocess(tree_ctrl.process_id()) or not is_x64_Python():
            item = win32structures.TVITEMW()
        else:
            item = win32structures.TVITEMW32()
        item.mask = win32defines.TVIF_HANDLE | win32defines.TVIF_STATE
        item.hItem = htreeitem
        item.stateMask = win32defines.TVIS_STATEIMAGEMASK
        item.state = (2 if checked else 1) << 12  # INDEXTOSTATEIMAGEMASK

        remote_mem.Write(item)
        retval = tree_ctrl.send_message(win32defines.TVM_SETITEMW, 0, remote_mem)
        if not retval:
            raise ctypes.WinError()
    finally:
        del remote_mem


def is_window_hung(hwnd: int) -> bool:
    """True if Windows' own hang-detector currently considers `hwnd`'s
    owning thread unresponsive (not pumping messages) -- IsHungAppWindow.

    Confirmed live as the real root cause behind what was
    misdiagnosed first as "another window has focus" and then as a
    locked/disconnected session: a long/complex search leaves Responsa's
    results window genuinely hung (still computing, message queue not
    pumped) for tens of seconds, and attempting pywinauto's mouse-move-
    based set_focus() against a window in that state is what was raising
    a spurious "no active desktop" RuntimeError -- nothing to do with the
    real interactive desktop at all. Callers should check this BEFORE
    attempting any focus/mouse operation against a window that might
    still be busy, and treat True as "still working, try again later",
    not as a failure.
    """
    return bool(ctypes.windll.user32.IsHungAppWindow(hwnd))


def wait_for(predicate: Callable[[], Optional[T]], timeout: float,
             interval: float = 0.3) -> T:
    """Poll `predicate` until it returns a truthy value, or raise TimeoutError.

    `predicate` should return the value of interest (e.g. a window handle)
    or None/False if not ready yet.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(f"Condition not met within {timeout}s")
