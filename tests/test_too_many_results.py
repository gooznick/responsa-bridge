"""SLOW test: a query matching an enormous number of results should raise
ResponsaTooManyResultsError, not hang trying to process ~32000+ hits.

Marked `slow` and excluded from the default `pytest` run (see pytest.ini)
-- Responsa itself takes a long time to even determine that there are too
many results for a broad query like this before it shows the "too many
results" dialog. Run it explicitly and only when you actually want to
re-verify this specific path:

    pytest tests/test_too_many_results.py -m slow
"""
import pytest

from responsa_api import ResponsaClient, ResponsaTooManyResultsError

# Broad enough to blow past Responsa's ~32000-result cap.
HUGE_QUERY = "*בא*"


@pytest.mark.slow
def test_too_many_results_raises():
    # The default 60s search_timeout is likely too short -- this query
    # takes a very long time even just to produce the "too many results"
    # dialog.
    with ResponsaClient(search_timeout=600) as client:
        with pytest.raises(ResponsaTooManyResultsError):
            client.search(HUGE_QUERY)
