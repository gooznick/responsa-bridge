"""pytest configuration for the (real, unmocked) GUI smoke tests under
this directory.

These drive the actual RESPONSA.exe desktop app and need the physical
license dongle plugged in -- there is no mock/fake to run them against,
so they are real integration tests, not unit tests. Run explicitly, e.g.:

    pytest tests/                       # everything except @pytest.mark.slow
    pytest tests/test_client.py -v
    pytest tests/ -m slow               # only the deliberately-slow ones
"""
import os
import sys

# So `import responsa_api` works regardless of the directory pytest is
# invoked from, even without `pip install -e .` -- inserts the repo root
# (this file's grandparent) at the front of sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from responsa_api import ResponsaClient


@pytest.fixture(scope="session", autouse=True)
def keep_display_awake():
    """Keep the screensaver/display-off from kicking in during the run --
    confirmed that a screensaver activating mid-run made
    every GUI test from then on fail. Only lasts for this test session;
    does not prevent a manual lock (Win+L), and a Group Policy screensaver
    may still override it."""
    import ctypes
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    kernel32 = ctypes.windll.kernel32
    kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
    try:
        yield
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)


@pytest.fixture
def client():
    """A ResponsaClient attached to the app -- see ResponsaClient's own
    docstring for the attach-or-launch behavior. Fresh per test function:
    cheap, since start() just attaches if RESPONSA.exe is already running
    rather than relaunching it, and searches are serialized per client
    anyway (one GUI window can't handle concurrent searches)."""
    with ResponsaClient() as c:
        yield c
