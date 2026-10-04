"""MCP server exposing responsa_api's ResponsaClient (search, get_result_text,
browse, hard_reset) as four MCP tools over stdio, for use by an MCP client
(Claude Desktop, Claude Code) on this machine. See responsa_mcp.server / README.md.

Logging is on by default (see logging_setup.py): every tool call and any
error is recorded to a circular (size-rotated) log file at
%LOCALAPPDATA%esponsa-bridge\logs\mcp_server.log, so a crash can be diagnosed and the exact
call that triggered it reproduced after the fact. Configured here, at
package import time, so it's active no matter which entry point is used.
"""
from . import logging_setup as _logging_setup

_logging_setup.configure()
