"""Smoke test: a query containing characters other than Hebrew letters/
digits should raise ResponsaInvalidQueryError with a clear message,
rather than the generic "Unexpected dialog during search" error, and the
search dialog should still be usable afterward. Fast -- part of the
regular test pass.

    pytest tests/test_invalid_query.py
"""
import pytest

from responsa_api import ResponsaInvalidQueryError

# Nikud (vowel points) trigger Responsa's own "שגיאה בהגדרת השאילתה"
# (Error in query definition) dialog -- confirmed live.
NIKUD_QUERY = "כְּבֵיצָה"


def test_invalid_query_raises_clear_error(client):
    with pytest.raises(ResponsaInvalidQueryError) as exc_info:
        client.search(NIKUD_QUERY)
    assert "עבריות וספרות" in exc_info.value.message

    # The dialog is dismissed as part of raising -- confirm the client is
    # left in a working state, not stuck behind a stray dialog.
    results = client.search("כוכב השחר הוא")
    assert results.hits
