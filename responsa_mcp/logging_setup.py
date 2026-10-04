"""Circular (size-bounded, rotating) file logging for the MCP server, on
by default -- so a crash can be diagnosed, and the exact call that
triggered it reproduced, from the log alone, after the fact (see
tools.py's `_log_calls` decorator for what actually gets logged).
Configured once, from responsa_mcp/__init__.py, so it's active regardless
of which entry point is used -- the real server, or a test importing
responsa_mcp.tools/lifecycle directly.

Writes ONLY to a file, never to stdout (the JSON-RPC transport channel
for a stdio MCP server) or even stderr -- and this logger explicitly
does not propagate to the root logger, so nothing else that might later
attach a stdout/stderr handler to the root logger can pull our records
onto a stream that matters.
"""
import logging
import logging.handlers
import os

# Per-user, outside the package: a regular (non-editable) pip install puts
# the package in site-packages, which is no place to write logs.
LOG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local"),
    "responsa-bridge", "logs",
)
LOG_FILE = os.path.join(LOG_DIR, "mcp_server.log")
MAX_BYTES = 2 * 1024 * 1024  # 2 MiB per file
BACKUP_COUNT = 5             # + the active file: ~12 MiB of history, circular

PACKAGE_LOGGER_NAME = "responsa_mcp"


def configure() -> None:
    """Idempotent -- safe to call more than once (e.g. if responsa_mcp is
    imported more than once independently, as happens across tests)."""
    logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    if logger.handlers:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
