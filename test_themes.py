#!/usr/bin/env python3
"""Tests for theme-based batch selection logic in batch_build.py.

Run with: python test_themes.py

Everything here is pure logic, no network: theme resolution and per-source
swap/override, the unknown-theme error text, --artist derivation including
the cap bypass, the keywords_any predicate, and the no-flag regression
guarantee (no theme selected -> the config object passes through untouched).
Live tuning of the shipped themes is plan step A3, not here.
"""

import sys

from batch_build import (
    THEMEABLE_SOURCES,
    UnknownThemeError,
    artist_cap_for,
    derive_artist_theme,
    format_theme_catalog,
    matches_keywords_any,
    resolve_batch_inputs,
    resolve_theme_sources,
    theme_catalog,
    unknown_theme_message,
)

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


BASE_SOURCES = {
    "major_artists_only": True,
    "max_per_artist": 4,
    "featured_artists": [{"name": "Raja Ravi Varma", "min_count": 3}],
    "met_museum": {"enabled": True, "queries": ["landscape painting"],
                   "public_domain_only": True},
    "art_institute_chicago": {"enabled": True, "queries": ["Claude Monet"]},
    "cleveland_museum": {"enabled": True, "queries": ["Monet"]},
    "wikimedia_commons": {"enabled": True,
                          "categories": ["Paintings_in_the_Hermitage"],
                          "queries": ["Turner landscape painting"]},
    "rijksmuseum": {"enabled": False, "queries": ["Vermeer"]},
    "local": {"enabled": True, "path": "./my_art"},
}

THEME = {
    "met_museum": {"queries": ["Rembrandt"]},
    "wikimedia_commons": {"categories": ["Paintings_by_Rembrandt"]},
    "keywords_any": ["rembrandt"],
    "major_artists_only": False,
}

CONFIG = {
    "art_sources": BASE_SOURCES,
    "themes": {"dutch": THEME},
    "display": {"aspect_mode": "crop"},
}


def main():
    print("catalog access — theme_catalog:")
    check("themes section returned", theme_catalog(CONFIG) == {"dutch": THEME})
    check("absent section -> empty", theme_catalog({}) == {})
    check("null section -> empty", theme_catalog({"themes": None}) == {})
    check("None config -> empty", theme_catalog(None) == {})

    print("\nper-source swap — resolve_theme_sources:")
    resolved = resolve_theme_sources(BASE_SOURCES, THEME)
    check("named source enabled", resolved["met_museum"]["enabled"] is True)
    check("named source queries replaced, not merged",
          resolved["met_museum"]["queries"] == ["Rembrandt"])
    check("unnamed input kind cleared on named source",
          resolved["wikimedia_commons"]["queries"] == [],
          str(resolved["wikimedia_commons"]["queries"]))
    check("categories swapped on wikimedia",
          resolved["wikimedia_commons"]["categories"] == ["Paintings_by_Rembrandt"])
    check("unnamed themeable sources disabled",
          resolved["art_institute_chicago"]["enabled"] is False
          and resolved["cleveland_museum"]["enabled"] is False)
    check("per-theme major_artists_only override applied",
          resolved["major_artists_only"] is False)
    check("non-source keys pass through",
          resolved["max_per_artist"] == 4
          and resolved["featured_artists"] == BASE_SOURCES["featured_artists"])
    check("non-themeable sources untouched",
          resolved["rijksmuseum"] == BASE_SOURCES["rijksmuseum"]
          and resolved["local"] == BASE_SOURCES["local"])
    check("other per-source keys survive the swap",
          resolved["met_museum"]["public_domain_only"] is True)
    check("input sources dict not mutated",
          BASE_SOURCES["met_museum"]["queries"] == ["landscape painting"]
          and BASE_SOURCES["art_institute_chicago"]["enabled"] is True
          and BASE_SOURCES["major_artists_only"] is True)
    no_override = resolve_theme_sources(BASE_SOURCES, {"met_museum": {"queries": ["X"]}})
    check("no override -> global major_artists_only kept",
          no_override["major_artists_only"] is True)
    check("no override -> global max_per_artist kept",
          no_override["max_per_artist"] == 4)
    capped = resolve_theme_sources(
        BASE_SOURCES, {"met_museum": {"queries": ["X"]}, "max_per_artist": 8})
    check("per-theme max_per_artist override applied",
          capped["max_per_artist"] == 8)
    check("max_per_artist override does not mutate input",
          BASE_SOURCES["max_per_artist"] == 4)

    print("\nunknown theme — error text lists what is available:")
    try:
        resolve_batch_inputs(CONFIG, theme_name="impressionst")
        check("unknown theme raises", False)
    except UnknownThemeError as e:
        msg = str(e)
        check("unknown theme raises", True)
        check("error names the bad theme", "impressionst" in msg, msg)
        check("error lists available themes", "dutch" in msg, msg)
    check("empty catalog says so",
          "(none defined" in unknown_theme_message("x", {}),
          unknown_theme_message("x", {}))

    print("\ncatalog listing — format_theme_catalog:")
    listing = format_theme_catalog(CONFIG["themes"])
    check("theme name listed", "dutch" in listing, listing)
    check("per-source summary present", "met_museum" in listing, listing)
    check("empty catalog message",
          "No themes defined" in format_theme_catalog({}))

    print("\nno-flag regression — config passes through untouched:")
    same, keywords, exempt = resolve_batch_inputs(CONFIG)
    check("same config object returned", same is CONFIG)
    check("no keywords, no exemption", keywords is None and exempt is None)

    print("\ntheme selection — resolve_batch_inputs with --theme:")
    themed, keywords, exempt = resolve_batch_inputs(CONFIG, theme_name="dutch")
    check("art_sources swapped",
          themed["art_sources"]["met_museum"]["queries"] == ["Rembrandt"])
    check("rest of config carried over",
          themed["display"] is CONFIG["display"])
    check("keywords_any passed through", keywords == ["rembrandt"])
    check("no artist exemption in theme mode", exempt is None)
    check("original config untouched",
          CONFIG["art_sources"]["met_museum"]["queries"] == ["landscape painting"])

    print("\n--artist derivation — derive_artist_theme / resolve_batch_inputs:")
    theme = derive_artist_theme("Claude Monet")
    check("all four sources named",
          set(theme) >= set(THEMEABLE_SOURCES))
    check("museum queries are just the name",
          all(theme[s]["queries"] == ["Claude Monet"]
              for s in ("met_museum", "art_institute_chicago", "cleveland_museum")))
    check("wikimedia query adds 'painting'",
          theme["wikimedia_commons"]["queries"] == ["Claude Monet painting"])
    check("wikimedia category best-guessed with underscores",
          theme["wikimedia_commons"]["categories"] == ["Paintings_by_Claude_Monet"])

    artisted, keywords, exempt = resolve_batch_inputs(CONFIG, artist="Claude Monet")
    check("artist becomes the exempt name", exempt == "claude monet")
    check("no keywords in artist mode", keywords is None)
    check("unnamed sources disabled in artist mode",
          artisted["art_sources"]["rijksmuseum"]["enabled"] is False
          or artisted["art_sources"]["met_museum"]["queries"] == ["Claude Monet"])
    try:
        resolve_batch_inputs(CONFIG, theme_name="dutch", artist="Claude Monet")
        check("theme+artist rejected", False)
    except ValueError:
        check("theme+artist rejected", True)

    print("\ncap bypass — artist_cap_for:")
    featured_caps = {"raja ravi varma": 3}
    check("normal artist gets the global cap",
          artist_cap_for("john constable", 4, featured_caps) == 4)
    check("featured artist gets their own cap",
          artist_cap_for("raja ravi varma", 4, featured_caps) == 3)
    check("exempt artist is uncapped",
          artist_cap_for("claude monet", 4, featured_caps, "claude monet") is None)
    check("exemption matches within verbose attribution",
          artist_cap_for("claude monet (french, 1840-1926)", 4,
                         featured_caps, "claude monet") is None)
    check("exemption is not contagious",
          artist_cap_for("edouard manet", 4, featured_caps, "claude monet") == 4)
    check("exemption beats a featured cap",
          artist_cap_for("raja ravi varma", 4, featured_caps, "raja ravi varma") is None)

    print("\nkeywords_any predicate — matches_keywords_any:")
    artwork = {"title": "The Beach at Trouville", "medium": "Oil on canvas",
               "department": "European Paintings", "culture": "French"}
    check("no keywords -> everything passes",
          matches_keywords_any(artwork, None) and matches_keywords_any(artwork, []))
    check("title matched case-insensitively",
          matches_keywords_any(artwork, ["TROUVILLE"]))
    check("medium matched", matches_keywords_any(artwork, ["oil on canvas"]))
    check("classification slot matched",
          matches_keywords_any(artwork, ["european paintings"]))
    check("partial keyword matches (impressionis ~ Impressionism)",
          matches_keywords_any({"title": "", "medium": "Impressionism",
                                "department": "", "culture": ""}, ["impressionis"]))
    check("ANY semantics — one hit is enough",
          matches_keywords_any(artwork, ["no-such-thing", "beach"]))
    check("no hit -> filtered out",
          not matches_keywords_any(artwork, ["woodblock", "etching"]))
    check("missing/None fields tolerated",
          not matches_keywords_any({"title": None}, ["beach"]))

    print("\nshipped catalog — config.yaml themes are well-formed:")
    import yaml
    shipped = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    themes = theme_catalog(shipped)
    expected = {"impressionist", "cityscapes", "old-masters", "women-artists",
                "landscapes"}
    check("the five starter themes ship", set(themes) == expected, str(set(themes)))
    for name, theme in themes.items():
        named = set(theme) & set(THEMEABLE_SOURCES)
        check(f"'{name}' names at least one source", bool(named))
        check(f"'{name}' has only known keys",
              set(theme) <= set(THEMEABLE_SOURCES) | {"keywords_any",
                                                      "major_artists_only",
                                                      "max_per_artist"},
              str(set(theme)))
    check("cityscapes widens the artist filter",
          themes["cityscapes"]["major_artists_only"] is False)
    check("women-artists widens the artist filter",
          themes["women-artists"]["major_artists_only"] is False)
    check("impressionist raises the per-artist cap",
          themes["impressionist"]["max_per_artist"] == 8)

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
