"""Direct-call tests of responsa_mcp's tool implementations
(responsa_mcp.tools) -- bypass the actual MCP stdio wire protocol and the
mcp SDK's registration machinery entirely (see
test_mcp_server_protocol.py for the one true end-to-end test), calling
the plain async functions with asyncio.run(...) to cheaply cover
responsa_mcp's own lifecycle/mapping/serialization logic. Reuses this
repo's known-fast queries. Not mocked, like the rest
of this suite -- every test here still drives the real Responsa app;
each test resets responsa_mcp's shared client afterward so lazy-start
(itself under test) starts fresh each time.

    pytest tests/test_mcp_tools.py -v
"""
import asyncio
import json
import os
import subprocess
import sys
import threading

import pytest
import win32event
from mcp.server.mcpserver.exceptions import ToolError

from responsa_mcp import lifecycle, tools

QUERY = "חמור לבן"
# A known-good hit for QUERY (3 hits in all), from a live run.
EXPECTED_CITATION = 'דרישה חושן משפט סימן רסז אות (ה) והרמב"ם'
# 1 hit in Mishna, 6 in Geonim.
BOOK_SCOPE_QUERY = "*כוכב# [-3:3] *לבנה#"


@pytest.fixture(autouse=True)
def _reset_shared_client():
    yield
    asyncio.run(lifecycle.shutdown())


def test_search_result_is_json_safe_and_matches_the_direct_api():
    result = asyncio.run(tools.search(QUERY))
    citations = [h["citation"] for h in result["hits"]]
    assert EXPECTED_CITATION in citations
    json.dumps(result)  # must not raise


def test_first_tool_call_lazily_starts_the_shared_client():
    assert lifecycle._client is None
    asyncio.run(tools.search(QUERY))
    assert lifecycle._client is not None


def test_shutdown_before_any_tool_call_is_a_no_op():
    asyncio.run(lifecycle.shutdown())  # must not raise


def test_get_result_text_after_search_returns_the_full_source():
    async def run():
        results = await tools.search(QUERY)
        hit = results["hits"][0]
        return hit, await tools.get_result_text(hit["index"])

    hit, source = asyncio.run(run())
    assert len(source["text"]) > len(hit["snippet"])


def test_get_result_text_rejects_a_zero_index_without_starting_the_client():
    with pytest.raises(ToolError):
        asyncio.run(tools.get_result_text(0))
    assert lifecycle._client is None  # the guard fired before the lock/lazy-start


def test_get_result_text_without_a_prior_search_raises_a_tool_error():
    # ToolError (via lifecycle.locked_call's ResponsaError -> ToolError
    # conversion), not the raw ResponsaError -- see lifecycle.py's
    # docstring for why that conversion is needed for this mcp version.
    with pytest.raises(ToolError):
        asyncio.run(tools.get_result_text(1))


def test_browse_top_level_returns_the_categories_with_no_error():
    result = asyncio.run(tools.browse())
    assert result["error"] is None
    assert len(result["options"]) == 22


def test_browse_ambiguous_step_serializes_browse_error_as_a_plain_string():
    async def run():
        await tools.browse()
        return await tools.browse(["ספרי"])

    result = asyncio.run(run())
    assert result["error"] == "AMBIGUOUS"
    assert type(result["error"]) is str  # not still a BrowseError instance


def test_browse_empty_path_relists_the_current_position_not_the_top():
    async def run():
        await tools.browse()
        moved = await tools.browse(["ספרות"])
        relisted = await tools.browse([])
        return moved, relisted

    moved, relisted = asyncio.run(run())
    assert relisted["options"] == moved["options"]
    assert len(relisted["options"]) != 22


def test_books_param_matches_a_scope_by_case_insensitive_enum_name():
    result = asyncio.run(tools.search(BOOK_SCOPE_QUERY, books=["mishna"]))
    assert result["hits"]
    for hit in result["hits"]:
        assert hit["citation"].startswith("משנה")


def test_books_param_passes_through_an_unmatched_name_as_free_text():
    result = asyncio.run(tools.search(BOOK_SCOPE_QUERY, books=["גאונים"]))
    json.dumps(result)  # resolves and runs without raising


def test_hard_reset_then_search_still_works():
    # Slow (~40s+, quits and relaunches the real app -- see
    # test_hard_reset.py's module docstring) and disruptive to anyone
    # else using Responsa right now, so kept to this one combined test
    # rather than a separate one per hard_reset() aspect.
    async def run():
        result = await tools.hard_reset()
        assert result == {"ok": True}
        return await tools.search(QUERY)

    results = asyncio.run(run())
    assert results["hits"]


def test_parallel_calls_in_the_same_process_are_rejected_not_queued():
    # asyncio is cooperative and single-threaded: gather schedules these
    # two coroutines in order, and the first one runs synchronously
    # (resolve_books, the lock check, acquiring the lock, the mutex
    # check, the lazy-start log line) all the way up to its first REAL
    # yield point -- `await asyncio.to_thread(client.start)` -- before
    # the second one gets to run at all. So the first call reliably
    # holds the lock by the time the second one checks lifecycle._lock.
    # locked(), making this deterministic rather than a timing gamble.
    async def run():
        return await asyncio.gather(tools.search(QUERY), tools.search(QUERY), return_exceptions=True)

    first, second = asyncio.run(run())
    outcomes = [first, second]
    ok = [o for o in outcomes if isinstance(o, dict)]
    busy = [o for o in outcomes if isinstance(o, lifecycle.ResponsaBusyError)]
    assert len(ok) == 1 and ok[0]["hits"]
    assert len(busy) == 1
    assert "parallel" in str(busy[0]).lower()


def test_takeover_kills_a_still_running_former_server_process(tmp_path, monkeypatch):
    # Confirmed live: an MCP client reconnecting can leave a
    # previous "python -m responsa_mcp" process running as an orphan --
    # found two such processes coexisting, one invisible to the log
    # entirely. takeover() is the fix: called once at startup (see
    # server.py's main()), it kills whatever the pid file names, if that
    # PID is still alive and still looks like one of ours.
    monkeypatch.setattr(lifecycle, "_PID_FILE", tmp_path / "server.pid")
    # A real subprocess whose command line contains "responsa_mcp" (via an
    # inert extra arg), so _kill_if_still_a_server_process's substring
    # check matches it, without actually being responsa_mcp itself.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "--responsa_mcp-stale-test-marker"]
    )
    try:
        lifecycle._PID_FILE.write_text(str(proc.pid))
        lifecycle.takeover()
        assert proc.wait(timeout=5) == 1  # killed with exit code 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)
    assert lifecycle._PID_FILE.read_text().strip() == str(os.getpid())


def test_takeover_leaves_an_unrelated_process_with_that_pid_alone(tmp_path, monkeypatch):
    # A pid file naming a real, currently-running process that is NOT one
    # of ours (a recycled PID, or simply a stale file pointing at
    # something unrelated) must never be killed.
    monkeypatch.setattr(lifecycle, "_PID_FILE", tmp_path / "server.pid")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lifecycle._PID_FILE.write_text(str(proc.pid))
        lifecycle.takeover()
        assert proc.poll() is None  # still running, untouched
    finally:
        proc.kill()
        proc.wait(5)


def test_takeover_with_no_pid_file_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_PID_FILE", tmp_path / "does_not_exist.pid")
    lifecycle.takeover()  # must not raise
    assert lifecycle._PID_FILE.read_text().strip() == str(os.getpid())


def test_takeover_never_kills_its_own_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_PID_FILE", tmp_path / "server.pid")
    lifecycle._PID_FILE.write_text(str(os.getpid()))
    lifecycle.takeover()  # must not raise or self-terminate
    assert lifecycle._PID_FILE.read_text().strip() == str(os.getpid())


def test_a_second_process_holding_the_mutex_is_rejected_immediately():
    # Simulates a separate process by acquiring lifecycle._mutex from a
    # background THREAD, not just a different handle -- Win32 mutex
    # ownership is per-thread, so the SAME thread re-waiting on a mutex
    # it already owns would just succeed (recursive), not correctly
    # model contention from another owner.
    acquired = threading.Event()
    release = threading.Event()

    def hold_mutex():
        win32event.WaitForSingleObject(lifecycle._mutex, 5000)
        acquired.set()
        release.wait(5)
        win32event.ReleaseMutex(lifecycle._mutex)

    t = threading.Thread(target=hold_mutex)
    t.start()
    try:
        assert acquired.wait(5), "background thread never acquired the mutex"
        with pytest.raises(lifecycle.ResponsaBusyError):
            asyncio.run(tools.search(QUERY))
        assert lifecycle._client is None  # failed before lazy-start
    finally:
        release.set()
        t.join(5)
