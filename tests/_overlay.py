"""Test helper: a nearly invisible, always-on-top window covering every
monitor, that swallows real mouse clicks but never takes focus.

Reproduces, deterministically, the failure found in the MCP server log:
a real mouse click (pywinauto click_input) aimed at a
Responsa dialog button lands on whatever window is actually on top at that
spot -- in real use the user's editor or a notification -- so the button is
never pressed and the automation waits until it times out. Keyboard input
is unaffected (the overlay never becomes the foreground window), so only
mouse-dependent code breaks under it.

    with ClickBlockingOverlay() as overlay:
        ...                      # drive Responsa
    overlay.clicks               # real clicks the overlay swallowed
"""
import threading

import win32api
import win32con
import win32gui

_CLASS_NAME = "ResponsaBridgeTestClickBlockingOverlay"


class ClickBlockingOverlay:
    def __init__(self):
        self.hwnd = None
        self.clicks = 0
        self._ready = threading.Event()
        self._thread = None
        self._error = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, name="click-blocking-overlay", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("overlay window did not come up")
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, *exc):
        if self.hwnd:
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
        self._thread.join(10)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg in (win32con.WM_LBUTTONDOWN, win32con.WM_RBUTTONDOWN, win32con.WM_LBUTTONDBLCLK):
            self.clicks += 1
            return 0
        if msg == win32con.WM_MOUSEACTIVATE:
            return win32con.MA_NOACTIVATE
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _run(self):
        try:
            hinst = win32api.GetModuleHandle(None)
            wc = win32gui.WNDCLASS()
            wc.lpszClassName = _CLASS_NAME
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.hbrBackground = win32gui.GetStockObject(win32con.BLACK_BRUSH)
            try:
                win32gui.RegisterClass(wc)
            except win32gui.error:
                pass  # already registered by an earlier overlay in this process
            x = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
            y = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
            w = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
            h = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
            ex_style = (win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW
                        | win32con.WS_EX_LAYERED | 0x08000000)  # WS_EX_NOACTIVATE
            self.hwnd = win32gui.CreateWindowEx(
                ex_style, _CLASS_NAME, "click-blocking overlay (test)", win32con.WS_POPUP,
                x, y, w, h, 0, 0, hinst, None,
            )
            # Alpha 1/255: invisible to the eye, but (unlike alpha 0 or
            # WS_EX_TRANSPARENT) still hit-tested, so it receives the clicks.
            win32gui.SetLayeredWindowAttributes(self.hwnd, 0, 1, win32con.LWA_ALPHA)
            win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE)
        except Exception as e:  # surface in __enter__, not a silent dead thread
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        win32gui.PumpMessages()
