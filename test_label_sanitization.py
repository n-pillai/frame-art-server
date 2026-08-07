#!/usr/bin/env python3
"""Tests for sanitize_label() in batch_build.py.

Run with: python test_label_sanitization.py

sanitize_label(title, artist, date, museum) is the last line of defense
before museum metadata goes on-screen as a label. It is pure text
processing -- no I/O, no network -- so every case here is a plain string
in, tuple out.

Covers: HTML tag stripping, Wikidata markup cleanup, non-Latin text
handling, empty/None/whitespace edge cases, and over-long strings.

Deliberately out of scope: the museum-source-specific extraction code in
art_sources.py (_parse_commons_page and friends) that produces the raw,
not-yet-sanitized strings sanitize_label() receives -- that path touches
live API response shapes and is not pure logic in the same way.
"""

import sys

from batch_build import sanitize_label

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


def _ascii_safe(value):
    """Render a value for a failure message without risking a console
    encoding crash if it happens to contain non-Latin characters (the
    non-Latin test cases below feed such strings through sanitize_label,
    and a failing assertion should still be able to print its detail)."""
    if isinstance(value, str):
        return value.encode("ascii", "backslashreplace").decode("ascii")
    return value


# ---------------------------------------------------------------------------
# HTML tag stripping
# ---------------------------------------------------------------------------

def test_strips_simple_html_tags():
    title, artist, date, museum = sanitize_label(
        "<i>Landscape</i>", "<b>Claude Monet</b>", "", "",
    )
    check("italic tags stripped from title", title == "Landscape", title)
    check("bold tags stripped from artist", artist == "Claude Monet", artist)


def test_strips_hidden_display_none_divs():
    title, artist, date, museum = sanitize_label(
        '<div style="display: none;">hidden junk</div>Visible Title', "Artist", "", "",
    )
    check("hidden display:none block removed entirely, only visible text kept",
          title == "Visible Title", title)


def test_strips_sup_citation_blocks_including_their_text():
    title, artist, date, museum = sanitize_label(
        "Landscape<sup class=\"cite_ref\">[1]</sup> Study", "Artist", "", "",
    )
    check("sup citation block and its inner text are both removed",
          "[1]" not in title and "cite_ref" not in title, title)


def test_decodes_html_entities():
    title, artist, date, museum = sanitize_label(
        "Artist&#39;s Landscape &amp; Sky", "Artist", "", "",
    )
    check("HTML entities are decoded", title == "Artist's Landscape & Sky", title)


# ---------------------------------------------------------------------------
# Wikidata markup cleanup
# ---------------------------------------------------------------------------

def test_strips_wikidata_qs_entries():
    title, artist, date, museum = sanitize_label(
        "Title QS:P571,+1885-00-00T00:00:00Z/9 rest", "Artist Q123456 name", "", "",
    )
    check("QS: entry removed from title", title == "Title rest", title)
    check("Q-ID removed from artist", artist == "Artist name", artist)


def test_strips_wikidata_p_ids_and_json_braces():
    title, artist, date, museum = sanitize_label(
        "Title P1234 {\"lang\": \"en\"} rest", "Artist", "", "",
    )
    check("P-ID and JSON-brace fragments removed",
          "P1234" not in title and "{" not in title, title)


def test_museum_rejects_wikimedia_commons_boilerplate():
    _, _, _, museum = sanitize_label("Title", "Artist", "", "Wikimedia Commons")
    check("'Wikimedia Commons' is rejected as a museum name", museum == "", museum)


def test_museum_rejects_file_prefixed_identifiers():
    _, _, _, museum = sanitize_label(
        "Title", "Artist", "", "File:SomeRandomWikimediaIdentifierString123456",
    )
    check("a File: identifier is rejected as a museum name", museum == "", museum)


# ---------------------------------------------------------------------------
# Non-Latin text handling
# ---------------------------------------------------------------------------

def test_all_cyrillic_title_becomes_untitled():
    title, artist, date, museum = sanitize_label(
        "Пейзаж с рекой",
        "Artist", "", "",
    )
    check("a title with no Latin content at all falls back to 'Untitled'",
          title == "Untitled", _ascii_safe(title))


def test_all_cyrillic_artist_becomes_unknown():
    title, artist, date, museum = sanitize_label(
        "Title",
        "Иван Шишкин",
        "", "",
    )
    check("an artist with no Latin content at all falls back to 'Unknown'",
          artist == "Unknown", _ascii_safe(artist))


def test_mixed_cyrillic_and_latin_keeps_the_latin_portion():
    title, artist, date, museum = sanitize_label(
        "Landscape Пейзаж",
        "Claude Monet Клод Моне",
        "", "",
    )
    check("Latin words survive when mixed with Cyrillic in the title",
          title == "Landscape", _ascii_safe(title))
    check("Latin words survive when mixed with Cyrillic in the artist",
          artist == "Claude Monet", _ascii_safe(artist))


def test_multilingual_title_extracts_the_english_labeled_portion():
    title, _, _, _ = sanitize_label(
        "German: Klassische Landschaft mit Ruinen English: Landscape with Ruins",
        "Artist", "", "",
    )
    check("'English:'-labeled portion of a multilingual title is extracted",
          title == "Landscape with Ruins", title)


def test_title_made_of_only_foreign_marker_words_becomes_untitled():
    # A title built entirely from German function words (no real content)
    # trips the "likely non-English" heuristic even though every character
    # is technically Latin script.
    title, _, _, _ = sanitize_label(
        "mit und von der die das des dem den blah", "Artist", "", "",
    )
    check("a title that reads as non-English by word heuristic becomes 'Untitled'",
          title == "Untitled", title)


# ---------------------------------------------------------------------------
# Empty / None / whitespace edge cases
# ---------------------------------------------------------------------------

def test_all_none_inputs_produce_safe_defaults():
    title, artist, date, museum = sanitize_label(None, None, None, None)
    check("None title -> 'Untitled'", title == "Untitled", title)
    check("None artist -> 'Unknown'", artist == "Unknown", artist)
    check("None date -> ''", date == "", repr(date))
    check("None museum -> ''", museum == "", repr(museum))


def test_all_empty_string_inputs_produce_safe_defaults():
    title, artist, date, museum = sanitize_label("", "", "", "")
    check("empty title -> 'Untitled'", title == "Untitled", title)
    check("empty artist -> 'Unknown'", artist == "Unknown", artist)
    check("empty date -> ''", date == "", repr(date))
    check("empty museum -> ''", museum == "", repr(museum))


def test_whitespace_only_inputs_produce_safe_defaults():
    title, artist, date, museum = sanitize_label("   ", "\t\t", "  \n ", "   ")
    check("whitespace-only title -> 'Untitled'", title == "Untitled", title)
    check("whitespace-only artist -> 'Unknown'", artist == "Unknown", artist)
    check("whitespace-only date -> ''", date == "", repr(date))
    check("whitespace-only museum -> ''", museum == "", repr(museum))


def test_date_rejects_known_placeholder_strings():
    for placeholder in ("Unknown date", "undated", "N/A", "n.d.", "None"):
        _, _, date, _ = sanitize_label("Title", "Artist", placeholder, "")
        check(f"date placeholder {placeholder!r} -> ''", date == "", repr(date))


# ---------------------------------------------------------------------------
# Over-long strings
# ---------------------------------------------------------------------------

def test_title_is_capped_at_100_characters():
    title, _, _, _ = sanitize_label("A" * 150, "Artist", "", "")
    check("title longer than 100 chars is truncated to 100",
          len(title) == 100, len(title))


def test_artist_is_capped_at_80_characters():
    _, artist, _, _ = sanitize_label("Title", "B" * 150, "", "")
    check("artist longer than 80 chars is truncated to 80",
          len(artist) == 80, len(artist))


def test_verbose_artist_attribution_is_shortened_to_the_name():
    _, artist, _, _ = sanitize_label(
        "Title",
        "Albert Bierstadt (American, born Solingen, Prussia, 1830–1902)",
        "", "",
    )
    check("a long parenthetical biography is dropped, keeping just the name",
          artist == "Albert Bierstadt", _ascii_safe(artist))


def test_long_date_string_with_one_year_extracts_a_circa_year():
    _, _, date, _ = sanitize_label(
        "Title", "Artist",
        "circa 1850, oil on canvas, catalogued much later than expected", "",
    )
    check("a long date description with one year collapses to 'ca. <year>'",
          date == "ca. 1850", date)


def test_long_date_string_with_two_years_extracts_a_range():
    _, _, date, _ = sanitize_label(
        "Title", "Artist",
        "1850-1860, painted over the decade with additions after", "",
    )
    check("a long date description with two years collapses to a year range",
          date == "1850-1860", date)


def test_museum_is_capped_at_80_characters_when_multi_word():
    long_museum = "The Rijksmuseum of Amsterdam and Beyond " * 5  # multi-word, >80 chars
    _, _, _, museum = sanitize_label("Title", "Artist", "", long_museum)
    check("a long multi-word museum name is truncated to 80, not rejected",
          len(museum) == 80, len(museum))


def test_single_long_word_museum_is_rejected_as_id_like():
    # A single unbroken run of 10+ alphanumeric characters is treated as a
    # hash/ID rather than a real institution name, even if it happens to be
    # a legitimate (if unusual) long word -- documents current behavior,
    # not something this test suite changes.
    _, _, _, museum = sanitize_label("Title", "Artist", "", "C" * 150)
    check("a single very long word for museum is rejected as ID-like gibberish",
          museum == "", repr(museum))


def test_hash_like_museum_id_is_rejected():
    _, _, _, museum = sanitize_label("Title", "Artist", "", "a1b2c3d4e5f6g7h8i9j0")
    check("a hash-like alphanumeric museum string is rejected",
          museum == "", repr(museum))


def main():
    tests = [
        test_strips_simple_html_tags,
        test_strips_hidden_display_none_divs,
        test_strips_sup_citation_blocks_including_their_text,
        test_decodes_html_entities,
        test_strips_wikidata_qs_entries,
        test_strips_wikidata_p_ids_and_json_braces,
        test_museum_rejects_wikimedia_commons_boilerplate,
        test_museum_rejects_file_prefixed_identifiers,
        test_all_cyrillic_title_becomes_untitled,
        test_all_cyrillic_artist_becomes_unknown,
        test_mixed_cyrillic_and_latin_keeps_the_latin_portion,
        test_multilingual_title_extracts_the_english_labeled_portion,
        test_title_made_of_only_foreign_marker_words_becomes_untitled,
        test_all_none_inputs_produce_safe_defaults,
        test_all_empty_string_inputs_produce_safe_defaults,
        test_whitespace_only_inputs_produce_safe_defaults,
        test_date_rejects_known_placeholder_strings,
        test_title_is_capped_at_100_characters,
        test_artist_is_capped_at_80_characters,
        test_verbose_artist_attribution_is_shortened_to_the_name,
        test_long_date_string_with_one_year_extracts_a_circa_year,
        test_long_date_string_with_two_years_extracts_a_range,
        test_museum_is_capped_at_80_characters_when_multi_word,
        test_single_long_word_museum_is_rejected_as_id_like,
        test_hash_like_museum_id_is_rejected,
    ]
    for test in tests:
        print(f"\n{test.__name__}:")
        test()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
