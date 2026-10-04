"""responsa_api: drive the Responsa desktop app from Python.

    from responsa_api import ResponsaClient

    with ResponsaClient() as client:
        results = client.search("גשר צר מאד")

Everything you need is importable from here. The `internal` subpackage is
implementation detail.
"""
from .book_scope import BookScope
from .client import ResponsaClient
from .exceptions import (
    ResponsaBrowseCacheError,
    ResponsaError,
    ResponsaFocusError,
    ResponsaInvalidQueryError,
    ResponsaLaunchError,
    ResponsaSearchError,
    ResponsaSessionLockedError,
    ResponsaTimeoutError,
    ResponsaTooManyResultsError,
)
from .results import BrowseError, BrowseResult, Hit, SearchResults, SourceText

__all__ = [
    "ResponsaClient",
    "BookScope",
    "BrowseError",
    "BrowseResult",
    "Hit",
    "SearchResults",
    "SourceText",
    "ResponsaBrowseCacheError",
    "ResponsaError",
    "ResponsaFocusError",
    "ResponsaInvalidQueryError",
    "ResponsaLaunchError",
    "ResponsaSearchError",
    "ResponsaSessionLockedError",
    "ResponsaTimeoutError",
    "ResponsaTooManyResultsError",
]
