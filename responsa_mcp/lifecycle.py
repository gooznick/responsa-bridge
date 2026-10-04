"""The one process-wide ResponsaClient, and the two locks that serialize
every GUI operation against it:
  - an in-process asyncio.Lock, guarding calls WITHIN this one server
    process;
  - a real Windows named mutex, for entirely separate "python -m
    responsa_mcp" processes (e.g. a second MCP client spawning its own
    copy of this server) -- an asyncio.Lock alone is invisible across
    process boundaries and would let two separate server processes drive
    the same Responsa window at once.

Deliberately NOT in ResponsaClient/Layer 1 itself -- decided with the
user to keep this purely an MCP-server-level concern, not touch the
already-stable automation core. BOTH locks FAIL FAST (the user's
explicit choice) rather than queuing -- see ResponsaBusyError. This
matters even for the in-process case, and isn't just about clarity: an
MCP client that fires off several tool calls at once (rather than
waiting for each result before issuing the next) caused real breakage
live -- a stray Print dialog and the automation then failing repeatedly
against a confused Responsa, needing manual recovery. Every tool's
docstring says explicitly not to call it in parallel; this is the
enforcement of that when a client does it anyway. Responsa is a real
GUI app, not a stateless API -- there is no safe way to serve two calls
"at once", only one at a time, ever.

A real Win32 mutex is used, not a lock file, specifically because it
isn't at risk of getting stuck locked forever if the holding process
dies (crash, taskkill, anything): the OS marks it abandoned and the next
waiter acquires it immediately (WAIT_ABANDONED) instead of hanging.

Lazily started on the first tool call, not at server startup: start()
can block up to launch_timeout (default 60s) and can raise
ResponsaLaunchError, and MCP clients expect the `initialize` handshake
to return promptly.
"""
import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Optional, TypeVar

import pywintypes
import win32api
import win32com.client
import win32con
import win32event
from mcp.server.mcpserver.exceptions import ToolError

from .logging_setup import LOG_DIR

from responsa_api import ResponsaClient
from responsa_api.exceptions import ResponsaError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# No "Global\\" prefix: every "python -m responsa_mcp" process that could
# plausibly collide runs in this same interactive desktop session
# (spawned by an MCP client like Claude Desktop/Code on this machine),
# so the default (session-local) kernel object namespace already covers
# it, without "Global\\"'s extra privilege requirements.
_MUTEX_NAME = "ResponsaBridgeMCPServer"

# Records the PID of whichever "python -m responsa_mcp" process started
# most recently, so the NEXT one to start can find and kill it -- see
# takeover(). Lives in the same per-user directory as the log file.
_PID_FILE = Path(LOG_DIR) / "server.pid"

# Substrings that mark a command line as one of ours: `python -m
# responsa_mcp`, the `-c "...from responsa_mcp.server import main..."`
# one-liner, or the `responsa-bridge-mcp.exe` console script.
_SERVER_CMDLINE_MARKERS = ("responsa_mcp", "responsa-bridge-mcp")

_lock = asyncio.Lock()
_client: Optional[ResponsaClient] = None
_mutex = win32event.CreateMutex(None, False, _MUTEX_NAME)


def _kill_if_still_a_server_process(pid: int) -> None:
    """Force-terminate `pid` if, and only if, it's still running AND its
    command line still looks like a responsa_mcp server invocation --
    never blindly kill a PID a stale file happens to name, since PIDs get
    reused by unrelated processes once the original exits.

    No graceful WM_CLOSE step (unlike winutil.terminate_process, used for
    Responsa.exe itself): this is a headless stdio process with no window
    to close nicely, and per the user's explicit choice, a fresh server
    process always wins immediately over a stale one -- even one that's
    mid-operation against Responsa. If that leaves Responsa's GUI stuck,
    hard_reset() is what recovers it; that's a strictly better outcome
    than two server processes silently coexisting and eventually
    colliding mid-call (confirmed live: exactly this was
    found happening -- a second, fully orphaned server process, invisible
    to this very log, left running from an earlier reconnect).
    """
    if pid == os.getpid():
        return
    wmi = win32com.client.GetObject("winmgmts:")
    cmdline = None
    for proc in wmi.InstancesOf("Win32_Process"):
        if proc.ProcessId == pid:
            cmdline = proc.CommandLine
            break
    if not cmdline or not any(m in cmdline for m in _SERVER_CMDLINE_MARKERS):
        return  # not running any more, or not one of ours (recycled PID)
    logger.warning("a previous responsa_mcp process (pid=%s) is still running -- killing it", pid)
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
    except pywintypes.error:
        return  # already gone
    try:
        win32api.TerminateProcess(handle, 1)
    except pywintypes.error:
        pass  # already gone
    finally:
        win32api.CloseHandle(handle)


def takeover() -> None:
    """Call once, at server startup, before anything else. If a previous
    "python -m responsa_mcp" process is still alive, kill it, then record
    this process as the current one -- so there is only ever one server
    process at a time, no matter how many times an MCP client
    reconnects/respawns it. The per-call mutex below only protects a
    single GUI operation while it's in flight; it does nothing to stop
    two idle server processes from both existing at once between calls,
    which is exactly the gap this closes.
    """
    try:
        stale_pid = int(_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        stale_pid = None
    if stale_pid is not None:
        _kill_if_still_a_server_process(stale_pid)
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


class ResponsaBusyError(ToolError):
    """Raised immediately -- never queued -- when a call can't run right
    now because Responsa is already busy, either:
      - another tool call is already in progress in THIS server process
        (the caller made a parallel/overlapping call instead of waiting
        for the previous one's result -- this is never correct, see the
        module docstring), or
      - a genuinely separate "python -m responsa_mcp" process is mid-
        operation.
    Not a ResponsaError -- this is a server-level contention signal, not
    something the automation layer itself raised.

    Subclasses ToolError (not a plain Exception) so its message reaches
    the calling agent -- see the module docstring on locked_call for why
    that distinction matters with this mcp SDK version: any other
    exception type is treated as an unexpected crash and its text is
    withheld from the client."""


def _try_acquire_mutex() -> bool:
    """Non-blocking (0ms) poll -- fail fast, per the user's choice, never
    wait for the other process to finish. WAIT_ABANDONED counts as a
    successful acquire (the previous holder died mid-operation; Windows
    hands it to us -- see the module docstring)."""
    result = win32event.WaitForSingleObject(_mutex, 0)
    return result in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED)


async def locked_call(fn: Callable[[ResponsaClient], T]) -> T:
    """Run one blocking ResponsaClient operation. Checks, in order: the
    in-process lock (rejecting immediately, NOT queuing, if another call
    is already in progress in this process -- see the module docstring
    for why silently queuing a parallel call is worse than rejecting it),
    then the cross-process mutex (rejecting immediately if a separate
    server process is mid-operation) -- lazily creates/starts the shared
    client if this is the first call, then runs `fn(client)` in a worker
    thread so the event loop isn't blocked. `fn` should be a small
    closure wrapping exactly one ResponsaClient method call.

    Raises ResponsaBusyError immediately for either kind of contention
    above. A ResponsaError from start() or fn() itself is re-raised as a
    ToolError carrying the SAME message -- confirmed live (against
    the actually-installed mcp 2.2.0): a plain exception raised
    from a tool function does NOT reach the calling agent's message at
    all, only a generic "Error executing tool <name>" -- only
    mcp.server.mcpserver.exceptions.ToolError (or a subclass) delivers
    its own text. Any OTHER, genuinely unexpected exception (not a
    ResponsaError) is left alone on purpose, to keep behaving like a
    real crash -- tools.py's `_log_calls` decorator logs its full
    traceback to the circular log either way, so the generic message
    the client sees isn't the only record of what actually happened.
    """
    global _client
    if _lock.locked():
        # Checked, and rejected, BEFORE ever awaiting the lock: awaiting
        # `async with _lock` here would just queue this call silently
        # behind whichever one is already running, which is exactly the
        # confusing, eventually-harmful behavior this whole check exists
        # to avoid (see the module docstring's real incident). No
        # `await` happens between this check and the one inside `async
        # with _lock` below, so there's no window for a second caller to
        # slip through between them -- asyncio is cooperative, and
        # neither statement yields control on its own.
        logger.warning("another call already in progress in this process -- rejecting")
        raise ResponsaBusyError(
            "Responsa is already processing another call in this session. "
            "Tools must be called ONE AT A TIME, never in parallel -- wait "
            "for the current call's result before calling the next tool."
        )
    async with _lock:
        if not _try_acquire_mutex():
            logger.warning("mutex already held by another process -- rejecting")
            raise ResponsaBusyError(
                "Responsa is busy in another MCP server process -- try again shortly."
            )
        try:
            if _client is None:
                logger.info("lazily starting a fresh ResponsaClient (first call)")
                client = ResponsaClient()
                try:
                    await asyncio.to_thread(client.start)
                except ResponsaError as e:
                    raise ToolError(str(e)) from e
                _client = client
            try:
                return await asyncio.to_thread(fn, _client)
            except ResponsaError as e:
                raise ToolError(str(e)) from e
        finally:
            win32event.ReleaseMutex(_mutex)


async def shutdown() -> None:
    """Close the shared client once, if any tool call ever started it.
    Safe to call even if none did. Never closes Responsa itself --
    ResponsaClient.close() only detaches."""
    global _client
    async with _lock:
        if _client is not None:
            await asyncio.to_thread(_client.close)
            _client = None
