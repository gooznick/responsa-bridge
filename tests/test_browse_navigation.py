"""Tests of ResponsaClient.browse()'s navigation and matching rules.

    pytest tests/test_browse_navigation.py -v

These do NOT touch Responsa: choosing your way down the sources tree is
answered from the cached tree (a small synthetic one here, so the expected
answers are obvious), and only reading a text needs the app -- that part is
replaced by a stub (`fetched` below records which text was requested). The
live counterpart, which reads real texts, is test_browse.py.
"""
import gzip
import json

import pytest

from responsa_api import BrowseError, ResponsaBrowseCacheError, SourceText
from responsa_api.internal import browse_tree
from responsa_api.internal.automation import ResponsaAutomation

# category 0: books with chapters ("פרק א".."פרק יא") as texts; category 1:
# a book whose name differs only by prefix; a text directly under a
# category; a section with duplicate sibling names.
TREE = [
    ['תנ"ך (החומש מחולק לפרקים', [
        ["ספר בראשית", ["פרק א", "פרק ב", "פרק י", "פרק יא"]],
        ["ספר יחזקאל", ["פרק א", "פרק ב"]],
        ["ספר ישעיהו", ["פרק א", ["פרק ב", ["פסוק א", "פסוק ב"]]]],
    ]],
    ["ספרות חז\"ל", [
        "הקדמה",
        ["משנה", [["ברכות", ["פרק א", "פרק ב"]]]],
        ["כפול", ["דף", "דף"]],
    ]],
]


@pytest.fixture
def auto(tmp_path, monkeypatch):
    for i, cat in enumerate(TREE):
        with gzip.open(tmp_path / browse_tree.category_file_name(i), "wt", encoding="utf-8") as f:
            json.dump(cat, f, ensure_ascii=False)
    browse_tree.build_manifest(str(tmp_path))
    a = ResponsaAutomation()
    a.browse_cache_dir = str(tmp_path)
    a._main_hwnd = 1  # pretend started: the GUI part is stubbed
    a.fetched = []

    def fake_get_text(index_path, names):
        a.fetched.append(list(names))
        return SourceText(citation=names[-1], text="text of " + " / ".join(names))

    monkeypatch.setattr(a, "browse_get_text", fake_get_text)
    return a


def test_no_arguments_returns_the_categories_and_resets(auto):
    r = auto.browse()
    assert r.ok and r.path == []
    assert r.options == ['תנ"ך (החומש מחולק לפרקים', 'ספרות חז"ל']
    auto.browse("תנך")
    assert auto.browse().path == []          # back at the top
    assert auto.browse("ספרות").options[0] == "הקדמה"


def test_step_by_step_descent(auto):
    r = auto.browse('תנ"ך')
    assert r.path == ['תנ"ך (החומש מחולק לפרקים']
    assert r.options == ["ספר בראשית", "ספר יחזקאל", "ספר ישעיהו"]
    r = auto.browse("ספר יחזקאל")
    assert r.options == ["פרק א", "פרק ב"]
    assert r.path == ['תנ"ך (החומש מחולק לפרקים', "ספר יחזקאל"]
    assert auto.fetched == []                # choosing never reads a text


def test_a_list_of_steps(auto):
    r = auto.browse(["תנך", "יחזקאל"])
    assert r.options == ["פרק א", "פרק ב"]
    assert r.path[-1] == "ספר יחזקאל"


def test_partial_names_select_when_unambiguous(auto):
    auto.browse(["תנך"])
    # "יחזקאל" is enough for "ספר יחזקאל"
    assert auto.browse("יחזקאל").path[-1] == "ספר יחזקאל"
    # "ב" is enough for "פרק ב" -- a text, so its content comes back
    r = auto.browse("ב")
    assert r.text is not None and r.text_path[-1] == "פרק ב"


def test_leaf_returns_text_and_stays_at_its_parent(auto):
    auto.browse(["תנך", "בראשית"])
    r = auto.browse("ב")
    assert r.ok and r.options is None
    assert r.text.text.endswith("פרק ב")
    assert r.path == ['תנ"ך (החומש מחולק לפרקים', "ספר בראשית"]
    assert r.text_path == r.path + ["פרק ב"]
    # a sibling is one call away, and the option list is still there
    r = auto.browse("יא")
    assert r.text_path[-1] == "פרק יא"
    assert auto.fetched[-1][-1] == "פרק יא"
    assert len(auto.fetched) == 2


def test_leaf_reached_in_the_middle_of_a_list_of_steps(auto):
    r = auto.browse(["תנך", "בראשית", "פרק י"])
    assert r.text_path[-1] == "פרק י"        # "י" must not match "פרק יא" too
    assert auto.browse().path == []


def test_word_matching_is_by_whole_words(auto):
    auto.browse(["תנך", "בראשית"])
    assert auto.browse("י").text_path[-1] == "פרק י"
    assert auto.browse("יא").text_path[-1] == "פרק יא"


def test_ambiguous_choice_is_an_error_and_lists_the_candidates(auto):
    auto.browse(["תנך", "בראשית"])
    r = auto.browse("פרק")
    assert r.error is BrowseError.AMBIGUOUS
    assert r.candidates == ["פרק א", "פרק ב", "פרק י", "פרק יא"]
    assert r.failed_step == 0 and r.message


def test_not_found_is_an_error_and_leaves_the_position_unchanged(auto):
    auto.browse("תנך")
    r = auto.browse("איוב")
    assert r.error is BrowseError.NOT_FOUND and not r.ok
    assert r.path == ['תנ"ך (החומש מחולק לפרקים']
    # still there: the next call continues from the same place
    assert auto.browse("ישעיהו").path[-1] == "ספר ישעיהו"


def test_a_failing_list_moves_nothing(auto):
    auto.browse("תנך")
    r = auto.browse(["ישעיהו", "ב", "פסוק ז"])   # the 3rd step doesn't exist
    assert r.error is BrowseError.NOT_FOUND and r.failed_step == 2
    assert r.path == ['תנ"ך (החומש מחולק לפרקים']  # not at "פרק ב"
    assert auto.browse("בראשית").options == ["פרק א", "פרק ב", "פרק י", "פרק יא"]


def test_going_below_a_text_is_an_error(auto):
    r = auto.browse(["תנך", "בראשית", "א", "פסוק א"])
    assert r.error is BrowseError.NOT_A_SECTION and r.failed_step == 3
    assert auto.fetched == []                 # rejected before reading anything
    assert auto.browse().path == []


def test_choosing_after_a_text_continues_from_its_section(auto):
    auto.browse(["תנך", "יחזקאל", "א"])
    r = auto.browse("אחר")
    assert r.error is BrowseError.NOT_FOUND
    assert r.path[-1] == "ספר יחזקאל"


def test_exact_needs_the_complete_text(auto):
    auto.browse("תנך")
    r = auto.browse("יחזקאל", exact=True)
    assert r.error is BrowseError.NOT_FOUND
    r = auto.browse("ספר יחזקאל", exact=True)
    assert r.ok and r.options == ["פרק א", "פרק ב"]
    # exact does not forgive quote marks either
    auto.browse()
    assert auto.browse('תנ"ך', exact=True).error is BrowseError.NOT_FOUND
    assert auto.browse('תנ"ך (החומש מחולק לפרקים', exact=True).ok


def test_an_exact_text_wins_over_a_partial_match(auto):
    auto.browse(["תנך", "ישעיהו"])
    # "פרק ב" is both the whole text of one entry and part of nothing else
    r = auto.browse("פרק ב")
    assert r.ok and r.options == ["פסוק א", "פסוק ב"]


def test_duplicate_sibling_names_are_ambiguous(auto):
    auto.browse(["ספרות", "כפול"])
    r = auto.browse("דף")
    assert r.error is BrowseError.AMBIGUOUS and r.candidates == ["דף", "דף"]
    assert auto.browse("דף", exact=True).error is BrowseError.AMBIGUOUS


def test_a_text_directly_under_a_category(auto):
    auto.browse("ספרות")
    r = auto.browse("הקדמה")
    assert r.text is not None and r.path == ['ספרות חז"ל']


def test_missing_cache_is_reported(tmp_path):
    a = ResponsaAutomation()
    a.browse_cache_dir = str(tmp_path)
    with pytest.raises(ResponsaBrowseCacheError):
        a.browse()


def test_normalize_and_match_step():
    assert browse_tree.normalize('תנ"ך') == browse_tree.normalize("תנך") == "תנך"
    assert browse_tree.normalize("בְּרֵאשִׁית") == "בראשית"     # vowel points ignored
    assert browse_tree.match_step(["פרק ב", "פרק בב"], "ב") == [0]
    assert browse_tree.match_step(["א"], "   ") == []


def test_words_at_the_start_win_over_words_elsewhere():
    names = ['(מפרשי תנ"ך (החומש', '(תנ"ך (החומש', "ספר יחזקאל"]
    assert browse_tree.match_step(names, 'תנ"ך') == [1]           # not also 0
    assert browse_tree.match_step(names, "יחזקאל") == [2]         # only inside
    assert browse_tree.match_step(names, "מפרשי") == [0]
    assert browse_tree.match_step(["ספר א", "ספר ב"], "ספר") == [0, 1]   # still ambiguous
