"""The one true end-to-end test: spawns the MCP server as a real
subprocess and talks to it over the actual stdio JSON-RPC transport via
the mcp package's own client, to catch schema/registration/transport
wiring bugs test_mcp_tools.py's direct calls can't (it bypasses both the
wire protocol and the mcp SDK's tool-registration/schema-generation code
entirely). Deliberately just search, the fastest of the four tools --
tool *behavior* is already covered elsewhere; this file only proves the
server starts, advertises its tools, and answers a real call correctly.
Not mocked -- spawns and drives the real app via the real server process.

    pytest tests/test_mcp_server_protocol.py -v
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUERY = "גשר צר מאד"
EXPECTED_CITATION = 'ליקוטי מוהר"ן תניינא תורה מח ד"ה וצריך להיות'
SERVER_PARAMS = StdioServerParameters(command=sys.executable, args=["-m", "responsa_mcp"], cwd=REPO_ROOT)


def test_server_advertises_exactly_the_four_expected_tools_over_stdio():
    async def run():
        async with stdio_client(SERVER_PARAMS) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.list_tools()

    tools = asyncio.run(run())
    assert {t.name for t in tools.tools} == {"search", "get_result_text", "browse", "hard_reset"}


def test_calling_search_over_the_real_protocol_returns_the_known_citation():
    async def run():
        async with stdio_client(SERVER_PARAMS) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("search", {"query": QUERY})

    result = asyncio.run(run())
    assert not result.is_error, result.content

    # Prefer structured_content (proves that plumbing works too) when
    # the installed SDK version populates it; this version wraps the
    # tool's actual return value under a "result" key.
    if result.structured_content is not None:
        payload = result.structured_content.get("result", result.structured_content)
    else:
        payload = json.loads(result.content[0].text)

    citations = [h["citation"] for h in payload["hits"]]
    assert EXPECTED_CITATION in citations
