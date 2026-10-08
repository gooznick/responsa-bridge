"""Fetching the full text of SEVERAL hits of one search, in a row.

    pytest tests/test_multi_fetch.py -v

Regression, seen through the MCP server: after a search with many hits
(447 for QUERY), the first get_result_text() worked and the next ones failed
with "None of ('דילוג למופע',) became visible within 10s" -- the "Skip to
number" dialog never opened, and every later fetch of that search failed the
same way.
"""
import time

QUERY = "שיתחשב"
# Not ascending on purpose: "skip to number" has to work backwards too.
INDICES = [9, 20, 6, 23, 1]


def _check(source, hit):
    assert source.text, f"empty text for result {hit.index}"
    assert source.citation


def test_several_fetches_of_one_search(client):
    results = client.search(QUERY, max_hits=30)
    assert results.total_hits > 100
    for i in INDICES:
        _check(client.get_result_text(i), results.hits[i - 1])


def test_fetches_with_idle_time_between_them(client):
    """The MCP server sits idle between calls while the model works."""
    results = client.search(QUERY, max_hits=30)
    for i in INDICES[:3]:
        _check(client.get_result_text(i), results.hits[i - 1])
        time.sleep(20)


def test_several_fetches_through_the_mcp_tools():
    """The MCP server runs every call through asyncio.to_thread, i.e. on
    whichever pool thread is free -- not always the thread that ran the
    search. This is the path where fetches after the first one failed."""
    import asyncio

    from responsa_mcp import lifecycle, tools

    async def run():
        try:
            results = await tools.search(QUERY, max_hits=30)
            for i in INDICES:
                source = await tools.get_result_text(i)
                assert source["text"], f"empty text for result {i}"
        finally:
            await lifecycle.shutdown()

    asyncio.run(run())

