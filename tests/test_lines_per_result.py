"""Smoke tests for the `lines_per_result` search() param (Responsa's
"תצוגה" -> "מספר שורות" / Alt+R option).

    pytest tests/test_lines_per_result.py -v

Same 143-hit query as test_max_hits.py, always capped with `max_hits` so
each run stays fast.
"""
import pytest

from responsa_api import ResponsaClient

QUERY = "תפוחי זהב במשכיות"


def _avg_snippet_len(results):
    return sum(len(h.snippet) for h in results.hits) / len(results.hits)


def test_more_lines_gives_longer_snippets(client):
    short = client.search(QUERY, max_hits=6, lines_per_result=1)
    long = client.search(QUERY, max_hits=6, lines_per_result=21)
    assert len(short.hits) == len(long.hits) == 6
    # Live measurement: ~70 vs ~740 characters per snippet.
    assert _avg_snippet_len(long) > 3 * _avg_snippet_len(short)


def test_default_is_unchanged_and_the_setting_does_not_stick(client):
    """None leaves the app's own setting alone: the result must be the
    same as before the param existed, and a search that set 21 lines must
    not leak into the next search that leaves it None."""
    before = client.search(QUERY, max_hits=6)
    client.search(QUERY, max_hits=6, lines_per_result=21)
    after = client.search(QUERY, max_hits=6)
    assert [h.snippet for h in after.hits] == [h.snippet for h in before.hits]
    # And the app default is the 3-line setting.
    explicit_default = client.search(QUERY, max_hits=6, lines_per_result=3)
    assert [h.snippet for h in explicit_default.hits] == [h.snippet for h in before.hits]


def test_max_hits_is_still_exact_with_the_maximum_lines(client):
    """21 lines make each hit take far more of a page; the page-range cap
    behind max_hits must account for that, or it prints too few pages and
    returns fewer hits than asked for."""
    results = client.search(QUERY, max_hits=40, lines_per_result=21)
    assert len(results.hits) == 40
    assert results.truncated
    assert results.total_hits == 143


@pytest.mark.parametrize("bad", [0, 22, -1, 2.5, "3", True])
def test_out_of_range_or_wrong_type_is_rejected_before_touching_the_app(client, bad):
    with pytest.raises(ValueError, match="lines_per_result"):
        client.search(QUERY, lines_per_result=bad)
