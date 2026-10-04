"""Tests of ResponsaClient.browse() against the REAL cached sources tree,
and (the second half) against the live app, reading real texts.

    pytest tests/test_browse.py -v

The first half needs no Responsa: the shape of the real tree comes from the
cache. The second half opens real texts in Responsa (each takes a few
seconds, since it is extracted through the print-to-PDF route), so don't use
another window while it runs. The rules themselves (all-or-nothing errors,
partial names, exact, position after a text...) are covered on a small
synthetic tree in test_browse_navigation.py.
"""
import re
import unicodedata

import pytest

from responsa_api import BrowseError, ResponsaClient

CATEGORIES = 22


def letters(text: str) -> str:
    """Only the Hebrew consonants of `text`, for comparing extracted text
    with known text. The extracted PDF text comes with vowel points and
    cantillation, spaces inside words, and a couple of glyphs it can only
    spell as "(cid:NNN)" (seen live: 708 is ל and 706 is ך) -- none of that says anything about whether the RIGHT text was
    fetched."""
    text = text.replace("(cid:708)", "ל").replace("(cid:706)", "ך")
    text = unicodedata.normalize("NFC", text)
    return re.sub(r"[^א-ת]", "", text)


@pytest.fixture
def offline():
    """A client that is never started: choosing a way down the tree is
    answered from the cache, without Responsa."""
    return ResponsaClient()


# -- the real tree, from the cache (no Responsa) ---------------------------

def test_top_level_has_all_the_categories(offline):
    r = offline.browse()
    assert r.ok and r.path == [] and len(r.options) == CATEGORIES
    assert 'ספרות חז"ל' in r.options and "כתבי עת" in r.options


def test_the_bible_category_lists_its_books(offline):
    offline.browse()
    r = offline.browse('תנ"ך')                   # the real name has extra words
    assert len(r.options) == 39
    assert r.options[:5] == ["בראשית", "שמות", "ויקרא", "במדבר", "דברים"]
    assert "יחזקאל" in r.options


def test_chapters_and_verses_of_genesis(offline):
    offline.browse()
    r = offline.browse(["תנ״ך", "בראשית"])
    assert len(r.options) == 50 and r.options[0] == "פרק א" and r.options[-1] == "פרק נ"
    r = offline.browse("א")                        # "פרק א" is a section of verses
    assert r.options[:2] == ["פסוק א", "פסוק ב"] and len(r.options) == 31


def test_partial_names_in_the_real_tree(offline):
    offline.browse()
    offline.browse('ספרות')                        # 'ספרות חז"ל'
    assert offline.browse("תלמוד בבלי").ok
    r = offline.browse("ברכות")
    assert r.ok and r.options[0] == "דף ב"         # Bavli Berakhot starts at page 2
    r = offline.browse("ב")
    assert r.options == ["עמוד א", "עמוד ב"]


def test_wrong_and_ambiguous_choices_in_the_real_tree(offline):
    offline.browse()
    r = offline.browse("אין קטגוריה כזו")
    assert r.error is BrowseError.NOT_FOUND and r.path == []
    r = offline.browse("ספרי")                     # many categories start with it
    assert r.error is BrowseError.AMBIGUOUS and len(r.candidates) > 3
    assert offline.browse().options                # position unchanged: still the top
    r = offline.browse('תנ"ך', exact=True)         # not the complete name
    assert r.error is BrowseError.NOT_FOUND


# -- real texts, from the live app -----------------------------------------

def test_reading_a_verse_returns_just_that_verse(client):
    """Exodus 1:1, then its sibling 1:2 -- each leaf's own text only, not
    the whole chapter (confirmed live: double-clicking a verse
    opens just that verse)."""
    client.browse()
    r = client.browse(['תנ"ך', "שמות", "א", "א"])
    assert r.ok and r.text is not None
    assert r.path == ['(תנ"ך (החומש מחולק לפרקים', "שמות", "פרק א"]  # stays at the parent
    assert r.text_path[-1] == "פסוק א"
    body = letters(r.text.text)
    assert "ואלהשמותבניישראלהבאיםמצרימה" in body       # 1:1, and ONLY 1:1
    assert "ראובןשמעוןלויויהודה" not in body              # not 1:2

    # the position stayed in the chapter: its next verse is one call away
    r2 = client.browse("ב")
    assert r2.ok and r2.text_path[-1] == "פסוק ב"
    body2 = letters(r2.text.text)
    assert "ראובןשמעוןלויויהודה" in body2                 # 1:2
    assert "ואלהשמותבני" not in body2                     # not 1:1


def test_mishna_walk_step_by_step_then_a_sibling(client):
    """Each Mishnah leaf is its own text only, not the whole chapter."""
    client.browse()
    assert client.browse("ספרות").ok
    assert client.browse("משנה").ok
    r = client.browse("ברכות")
    assert r.options[0] == "פרק א"
    r = client.browse("א")
    assert r.options[:2] == ["משנה א", "משנה ב"]

    r = client.browse("א")                               # "משנה א"
    assert r.ok and r.text_path[-2:] == ["פרק א", "משנה א"]
    body = letters(r.text.text)
    assert "מאימתיקוריןאתשמעבערבית" in body              # Mishnah 1:1's own opening
    assert "משיכירביןתכלת" not in body                   # not 1:2's

    r = client.browse("ב")                               # its sibling, "משנה ב"
    assert r.ok and r.text_path[-1] == "משנה ב"
    body2 = letters(r.text.text)
    assert "משיכירביןתכלת" in body2                      # Mishnah 1:2's own opening
    assert "מאימתיקוריןאתשמעבערבית" not in body2         # not 1:1's


def test_talmud_page_and_a_wrong_choice_on_the_way(client):
    client.browse()
    client.browse(["ספרות", "תלמוד בבלי", "ברכות"])
    bad = client.browse(["ב", "עמוד ג"])                  # no such side of a page
    assert bad.error is BrowseError.NOT_FOUND and bad.failed_step == 1
    assert bad.path[-1] == "ברכות"                        # nothing moved
    r = client.browse(["ב", "א"])                         # "דף ב", "עמוד א"
    assert r.ok and r.text_path[-2:] == ["דף ב", "עמוד א"]
    assert "מאימתיקורין" in letters(r.text.text)


def test_starting_over_after_a_text(client):
    client.browse(['תנ"ך', "בראשית", "א", "א"])
    r = client.browse()
    assert r.path == [] and len(r.options) == CATEGORIES


# -- deep, real hierarchies (commentaries several levels below a plain
# book), deliberately choosing an entry from the MIDDLE of the list at
# EVERY level (never the first or last option) -- so a wrong offset
# anywhere in the keyboard-navigation arithmetic (browse_gui.
# open_leaf_via_keyboard's Home + per-level Down-press counts) would
# land on a neighboring, visibly wrong entry instead of accidentally
# still working by starting at 0 every time.

def test_deep_hierarchy_in_a_single_call_knesset_hagedolah(client):
    """Knesset HaGedolah's Hagahot Tur, Yoreh Deah, Hilchot Dam (65-68),
    siman 66: category -> Tur's own commentators -> this one -> its
    Hagahot Tur work -> the chelek -> the siman group -> the siman, 7
    real steps below the top, fuzzy-matched and fetched in one call. At
    every level the chosen entry sits mid-list, e.g. index 6 of 11, 6 of
    15 -- never index 0 or the last one."""
    client.browse()
    r = client.browse([
        "טור", "מפרשי הטור", "כנסת הגדולה", "הגהות טור", "יורה דעה", "דם", "סו",
    ])
    assert r.ok, (r.error, r.message)
    assert r.text_path == [
        "טור, שולחן ערוך, מפרשים וחיבורים", "מפרשי הטור", "כנסת הגדולה", "הגהות טור",
        "יורה דעה", "(הלכות דם (סה - סח", "סימן סו",
    ]
    assert r.path == r.text_path[:-1]                     # stayed at the siman group
    assert "כנסת הגדולה" in r.text.citation and "סו" in r.text.citation
    body = letters(r.text.text)
    assert "טורסימןסוובוטסעיפים" in body                   # the entry's own opening


def test_deep_hierarchy_step_by_step_magid_mishneh(client):
    """Magid Mishneh's commentary on the Rambam, Hilchot Zechiya
    U'Matana, chapter 3, halacha 4 -- same idea, one step per call, and
    also a mid-list index at every level (e.g. 8 of 29, 3 of 13, 3 of
    13)."""
    client.browse()
    assert client.browse("רמבם").ok
    assert client.browse("מפרשים על הרמבם על הדף").ok
    assert client.browse("מגיד משנה").ok
    r = client.browse("זכיה ומתנה")
    assert "פרק א" in r.options
    r = client.browse("ג")                                 # "פרק ג", not "פרק א"
    assert r.options[0] == "הלכה א"
    r = client.browse("ד")                                  # "הלכה ד", not "הלכה א"
    assert r.ok and r.text_path == [
        'רמב"ם ומפרשיו', "מפרשים על הרמב\"ם על הדף", "מגיד משנה",
        "הלכות זכיה ומתנה", "פרק ג", "הלכה ד",
    ]
    body = letters(r.text.text)
    assert "כדרךשאיןצריכיןעדים" in body                    # the entry's own opening
    # its immediate neighbors are NOT what came back (would catch an
    # off-by-one in the keyboard navigation landing one row short/long)
    assert "כדרךשאיןצריכיןעדים" not in letters(client.browse("ה").text.text)  # halacha 5
