"""
Stage A discovery tool for the Responsa desktop app.

Launches (or attaches to) RESPONSA.exe, then repeatedly enumerates every
top-level window belonging to that process -- this catches the main window
AND any modal dialogs/messageboxes (missing-dongle error, "too many windows"
warning, etc.) since those are separate top-level windows owned by the same
process.

Does NOT click or type anything -- pure observation, so we can see exactly
what dialogs look like (title, static text, button labels) before writing
any interaction code.

Usage:
    python inspect_app.py
    python inspect_app.py --attach     # only attach to an already-running instance, don't launch
"""
import os
import sys
import time
import argparse
import subprocess

import win32gui
import win32process
import win32api
import win32con

RESPONSA_DIR = r"C:\Program Files (x86)\ResponsaCD33"
RESPONSA_EXE = os.path.join(RESPONSA_DIR, "RESPONSA.exe")
RESPONSA_IMAGE_NAME = "RESPONSA.exe"
POLL_SECONDS = 0.5
POLL_TIMEOUT_SECONDS = 20


def find_responsa_pids():
    pids = []
    try:
        import win32com.client
        wmi = win32com.client.GetObject("winmgmts:")
        for proc in wmi.InstancesOf("Win32_Process"):
            if proc.Name and proc.Name.lower() == RESPONSA_IMAGE_NAME.lower():
                pids.append(proc.ProcessId)
    except Exception as e:
        print(f"[warn] WMI process lookup failed: {e}", file=sys.stderr)
    return pids


def enum_top_level_windows_for_pids(pids):
    pid_set = set(pids)
    results = []

    def callback(hwnd, _):
        _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
        if found_pid in pid_set:
            results.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    return results


def dump_children(hwnd, indent="    "):
    children = []

    def callback(child_hwnd, _):
        children.append(child_hwnd)
        return True

    try:
        win32gui.EnumChildWindows(hwnd, callback, None)
    except Exception as e:
        print(f"{indent}[warn] EnumChildWindows failed: {e}")
        return

    for child in children:
        try:
            cls = win32gui.GetClassName(child)
            text = win32gui.GetWindowText(child)
            ctrl_id = win32gui.GetDlgCtrlID(child)
            rect = win32gui.GetWindowRect(child)
            visible = win32gui.IsWindowVisible(child)
            print(f"{indent}hwnd={child} id={ctrl_id} class={cls!r} text={text!r} visible={visible} rect={rect}")
        except Exception as e:
            print(f"{indent}[warn] failed to inspect child {child}: {e}")


def dump_window(hwnd):
    cls = win32gui.GetClassName(hwnd)
    text = win32gui.GetWindowText(hwnd)
    rect = win32gui.GetWindowRect(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    print(f"\n=== TOP-LEVEL WINDOW hwnd={hwnd} ===")
    print(f"class={cls!r} text={text!r} visible={visible} rect={rect}")
    print(f"style=0x{style:08X} ex_style=0x{ex_style:08X}")
    print("children:")
    dump_children(hwnd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attach", action="store_true", help="only attach, do not launch")
    args = parser.parse_args()

    pids = find_responsa_pids()
    if pids:
        print(f"Found already-running RESPONSA.exe processes: {pids}")
    elif args.attach:
        print("No running RESPONSA.exe found and --attach was given. Exiting.")
        return
    else:
        print(f"No running RESPONSA.exe found. Launching: {RESPONSA_EXE} (cwd={RESPONSA_DIR})")
        subprocess.Popen([RESPONSA_EXE], cwd=RESPONSA_DIR)
        time.sleep(1)
        pids = find_responsa_pids()
        print(f"Process(es) after launch: {pids}")

    print(f"\nPolling for top-level windows owned by pid(s) {pids} for up to {POLL_TIMEOUT_SECONDS}s...")
    seen_hwnds = set()
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        pids = find_responsa_pids()
        if not pids:
            print("[warn] process list is empty (process may have exited)")
        hwnds = enum_top_level_windows_for_pids(pids)
        new_hwnds = [h for h in hwnds if h not in seen_hwnds]
        for h in new_hwnds:
            seen_hwnds.add(h)
            dump_window(h)
        if new_hwnds:
            # give it a moment in case more windows are about to appear (e.g. a dialog after the main window)
            deadline = max(deadline, time.time() + 3)
        time.sleep(POLL_SECONDS)

    if not seen_hwnds:
        print("\nNo top-level windows were ever observed for this process. It may have exited immediately -- check Task Manager / Event Viewer.")
    else:
        print(f"\nDone. Observed {len(seen_hwnds)} top-level window(s) total: {sorted(seen_hwnds)}")


if __name__ == "__main__":
    main()
