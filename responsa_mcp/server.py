"""Builds the MCPServer app, registers the four tools, and runs it over
stdio. Installed as the `responsa-bridge-mcp` console script (see
pyproject.toml); `python -m responsa_mcp` runs the same main(). See the
"MCP server" section of README.md for a launch-config example.
"""
import asyncio
import logging
import os

from mcp.server.mcpserver import MCPServer

from . import lifecycle, tools

logger = logging.getLogger(__name__)

mcp = MCPServer("responsa-bridge")

# Plain calls, not @mcp.tool() decorators on the def itself -- see
# tools.py's module docstring: keeps tools.search/get_result_text/browse
# directly importable and callable with asyncio.run(...) in tests,
# independent of whatever this registration call does internally.
mcp.add_tool(tools.search)
mcp.add_tool(tools.get_result_text)
mcp.add_tool(tools.browse)
mcp.add_tool(tools.hard_reset)


def main() -> None:
    logger.info("MCP server starting (pid=%s)", os.getpid())
    # Before anything else: if a previous server process is still alive
    # (an MCP client reconnected without cleanly killing it -- confirmed
    # live, as a real orphaned process), kill it, so there is
    # only ever one server process driving Responsa. See its docstring.
    lifecycle.takeover()
    try:
        mcp.run()  # blocks for the server's whole lifetime (stdio transport)
    except Exception:
        # Every per-tool-call failure is already logged by tools.py's
        # _log_calls; this is for anything that somehow escapes that --
        # a crash in the SDK's own transport/dispatch code, say -- so
        # even that isn't silently missing from the log.
        logger.exception("MCP server crashed")
        raise
    finally:
        logger.info("MCP server stopping")
        # Runs exactly once, on normal exit and on interruption. Safe to
        # call even if no tool call ever started the shared client (see
        # lifecycle.shutdown's own docstring).
        asyncio.run(lifecycle.shutdown())


if __name__ == "__main__":
    main()
