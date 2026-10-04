"""Smoke tests for the "(word1/word2/...)" optional/alternative-word
syntax in query strings -- Responsa's own syntax (this module doesn't
build or validate query strings, see search()'s docstring), matching
ANY ONE of the parenthesized words at that position. All hit counts and
citations below are from a live run against the whole database.

    pytest tests/test_optional_words.py -v
"""


def test_two_way_alternation(client):
    results = client.search("בראשית [-2:2] (והוא/והיא)", max_hits=25)
    assert results.total_hits == 557
    # Both alternatives, not just one, must actually appear in the
    # sample -- proves this is genuine OR matching, not e.g. silently
    # matching only the first word in the group.
    snippets = " ".join(h.snippet for h in results.hits)
    assert "והוא" in snippets and "והיא" in snippets


def test_three_way_alternation(client):
    results = client.search("בראשית [-2:2] (אברהם/יצחק/יעקב)", max_hits=5)
    assert results.total_hits == 2372


def test_single_word_group_is_equivalent_to_the_bare_word(client):
    grouped = client.search("בראשית [-2:2] (אברהם)", max_hits=5)
    plain = client.search("בראשית [-2:2] אברהם", max_hits=5)
    assert grouped.total_hits == plain.total_hits == 886


def test_two_alternation_groups_with_a_distance_window(client):
    # (word/word) [-1:1] (word/word): two separate groups, each
    # resolved independently, still composing with [-N:M] like plain
    # words do. Narrow enough (chained with another distance window
    # against a third word) to pin down to a single, exact citation.
    results = client.search("(והוא/והיא) [-1:1] (הלך/הלכה) [-3:3] בראשית")
    assert results.total_hits == 1
    assert results.hits[0].citation == 'שמע שלמה בראשית פרשת ויצא ד"ה אי נמי'


def test_alternation_group_with_prefix_and_suffix_wildcards_inside(client):
    # *word (prefix) and word# (suffix) nest inside a group exactly like
    # they apply to a plain word outside one.
    results = client.search("בראשית [-2:2] (*הלך/הלכה#)", max_hits=5)
    assert results.total_hits == 188
