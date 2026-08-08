#!/usr/bin/env python3
"""
batch_build.py — Build a gallery of Frame TV-ready art in one shot.

Pull hundreds of public domain masterpieces from museum APIs, process
them to gallery quality (4K, auto-matte, metadata labels), and save
them to a folder you can copy to USB or upload to the TV.

No Pi needed. No WebSocket. No ongoing maintenance.
Run it on your laptop whenever you want fresh art.

Usage:
  python batch_build.py                        # Build with defaults from config.yaml
  python batch_build.py --count 200            # Pull 200 images
  python batch_build.py --output ./usb_drive   # Output to a specific folder
  python batch_build.py --resume               # Resume an interrupted batch
  python batch_build.py --dry-run              # Show what would be fetched, don't download
  python batch_build.py --theme impressionist  # Build from a named theme in config.yaml
  python batch_build.py --list-themes          # Show the theme catalog
  python batch_build.py --artist "Claude Monet"  # Single-artist batch
"""

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from art_sources import (
    download_image,
    gather_local_artworks,
    search_met,
    get_met_object,
    search_aic,
    search_cma,
    search_rijksmuseum,
    resolve_rijks_object,
    search_wikimedia_commons,
    is_landscape_enough,
    is_major_artist,
    is_painting,
    is_display_worthy,
)
from image_processor import process_image

try:
    from PIL import Image as _PILImage
except ImportError:
    _PILImage = None

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
DEFAULT_LOG_FILE = "batch_build.log"
DEFAULT_LOG_LEVEL = "INFO"

logger = logging.getLogger("batch_build")


def resolve_logging_settings(
    config: dict | None = None,
    log_file: str | None = None,
    log_level: str | None = None,
) -> tuple[str, str]:
    """Resolve the log destination and level from three sources.

    Ascending precedence, so a default always exists and can always be beaten:

        built-in default  ->  config.yaml `logging:`  ->  CLI flag

    Separated from configure_logging() so the precedence rules can be tested
    without touching global logging state.
    """
    section = (config or {}).get("logging") or {}
    resolved_file = log_file or section.get("file") or DEFAULT_LOG_FILE
    resolved_level = str(log_level or section.get("level") or DEFAULT_LOG_LEVEL).upper()
    return resolved_file, resolved_level


def configure_logging(
    config: dict | None = None,
    log_file: str | None = None,
    log_level: str | None = None,
) -> None:
    """Configure root logging. Called from main(), never at import time.

    Configuring at module scope gave a bare import two side effects it should
    not have: it created a log file in the working directory (which blocks
    testing), and it switched INFO logging on process-wide, third-party
    libraries included. That second one matters concretely -- samsungtvws logs
    the Frame TV pairing token at INFO, so a TV-facing script importing this
    module would have written a credential in plaintext to the log and stdout.
    Configuring only from the entry point leaves importers in control.
    """
    resolved_file, level_name = resolve_logging_settings(config, log_file, log_level)

    level = getattr(logging, level_name, None)
    unknown_level = not isinstance(level, int)
    if unknown_level:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(resolved_file),
        ],
    )

    # Only reportable now that handlers exist.
    if unknown_level:
        logger.warning(
            "Unknown log level %r in config; falling back to %s",
            level_name, DEFAULT_LOG_LEVEL,
        )


# ---------------------------------------------------------------------------
# Themes — named bundles of per-source search inputs (config.yaml `themes:`)
# ---------------------------------------------------------------------------
# Selecting a theme swaps the per-source inputs before gathering; the rest of
# the pipeline -- landscape/painting filters, per-artist caps, featured
# artists, processing -- runs unchanged. Catalog only, no free-text themes:
# each source interprets a bare string differently, so free-text passthrough
# would be unpredictable per source (build-plan decision 5).

# The four keyless sources a theme may re-parameterize. Rijksmuseum and the
# local folder are deliberately not themeable: Rijks search is disabled, and
# the local folder is the user's own explicit choice, not a search input.
THEMEABLE_SOURCES = (
    "met_museum",
    "art_institute_chicago",
    "cleveland_museum",
    "wikimedia_commons",
)
THEME_OPTION_KEYS = ("keywords_any", "major_artists_only", "max_per_artist")


class UnknownThemeError(ValueError):
    """Raised for a --theme name not in the catalog; message lists what is."""


def theme_catalog(config: dict | None) -> dict:
    """Return the `themes:` mapping from config ({} if absent or null)."""
    return (config or {}).get("themes") or {}


def theme_summary_line(name: str, theme: dict) -> str:
    """One line per theme for --list-themes: name + per-source input counts."""
    parts = []
    for src in THEMEABLE_SOURCES:
        entry = theme.get(src)
        if not isinstance(entry, dict):
            continue
        bits = [f"{len(entry[key])} {key}"
                for key in ("queries", "categories") if entry.get(key)]
        parts.append(f"{src} ({', '.join(bits) if bits else 'no inputs'})")
    if theme.get("keywords_any"):
        parts.append(f"keywords_any: {', '.join(theme['keywords_any'])}")
    if "major_artists_only" in theme:
        parts.append(f"major_artists_only: {str(theme['major_artists_only']).lower()}")
    if "max_per_artist" in theme:
        parts.append(f"max_per_artist: {theme['max_per_artist']}")
    return f"{name}: {'; '.join(parts) if parts else 'no sources'}"


def format_theme_catalog(themes: dict) -> str:
    """Human-readable catalog listing for --list-themes."""
    if not themes:
        return "No themes defined in config.yaml (add a `themes:` section)."
    lines = ["Available themes:"]
    for name in sorted(themes):
        lines.append(f"  {theme_summary_line(name, themes[name])}")
    return "\n".join(lines)


def unknown_theme_message(name: str, themes: dict) -> str:
    """Error text for an unknown theme — always lists what IS available."""
    available = ", ".join(sorted(themes)) if themes else "(none defined in config.yaml)"
    return f"Unknown theme {name!r}. Available themes: {available}"


def resolve_theme_sources(sources: dict, theme: dict) -> dict:
    """Return a new art_sources dict with the theme's per-source inputs swapped in.

    Sources the theme names are enabled with exactly the theme's
    queries/categories -- base-config inputs for that source are replaced,
    not merged, because a theme is the whole search surface. Themeable
    sources the theme does NOT name are disabled for the run; otherwise
    their base-config queries would dilute the theme. Both input keys are
    always replaced so a stale base-config list on a source the theme only
    gave one input kind to cannot leak through. Everything else (caps,
    featured artists, local folder) passes through untouched, except the
    optional per-theme `major_artists_only` override. Never mutates input.
    """
    resolved = dict(sources or {})
    for src in THEMEABLE_SOURCES:
        entry = dict(resolved.get(src) or {})
        themed = theme.get(src)
        if isinstance(themed, dict):
            entry["enabled"] = True
            entry["queries"] = list(themed.get("queries") or [])
            entry["categories"] = list(themed.get("categories") or [])
        else:
            entry["enabled"] = False
        resolved[src] = entry
    if "major_artists_only" in theme:
        resolved["major_artists_only"] = bool(theme["major_artists_only"])
    if "max_per_artist" in theme:
        # A themed batch deliberately concentrates on fewer artists, so the
        # global variety cap (4) binds hard there — 50 cap-skips in the
        # 2026-08-08 impressionist run. Themes may raise (or lower) it.
        resolved["max_per_artist"] = int(theme["max_per_artist"])
    return resolved


def derive_artist_theme(artist: str) -> dict:
    """Mechanically derive a single-artist theme for --artist.

    Queries are just the name for the three museum APIs. Wikimedia gets a
    text query plus a best-guess "Paintings_by_<Name>" category -- if that
    category does not exist on Commons, the category fetch returns zero
    files and the run proceeds on the text query alone.
    """
    name = artist.strip()
    return {
        "met_museum": {"queries": [name]},
        "art_institute_chicago": {"queries": [name]},
        "cleveland_museum": {"queries": [name]},
        "wikimedia_commons": {
            "queries": [f"{name} painting"],
            "categories": [f"Paintings_by_{name.replace(' ', '_')}"],
        },
    }


def resolve_batch_inputs(
    config: dict,
    theme_name: str | None = None,
    artist: str | None = None,
) -> tuple[dict, list[str] | None, str | None]:
    """Resolve --theme/--artist into (config, keywords_any, exempt_artist).

    With neither flag the input config comes back as the same object --
    the no-flag path must stay identical to today's behavior. In --artist
    mode the artist is exempt from major_artists_only and the per-artist
    cap: a Monet-only batch obviously exceeds a cap of 4.
    """
    if theme_name and artist:
        raise ValueError("--theme and --artist are mutually exclusive")
    if not theme_name and not artist:
        return config, None, None

    if artist:
        theme = derive_artist_theme(artist)
        exempt = artist.strip().lower()
    else:
        themes = theme_catalog(config)
        if theme_name not in themes:
            raise UnknownThemeError(unknown_theme_message(theme_name, themes))
        theme = themes[theme_name]
        exempt = None

    resolved = dict(config or {})
    resolved["art_sources"] = resolve_theme_sources(
        (config or {}).get("art_sources") or {}, theme
    )
    keywords = list(theme.get("keywords_any") or []) or None
    return resolved, keywords, exempt


def matches_keywords_any(artwork: dict, keywords: list[str] | None) -> bool:
    """Case-insensitive substring match for a theme's keywords_any post-filter.

    Empty/None keywords means no post-filter -- everything passes. The
    fields checked are the ones the sources actually populate: title
    everywhere; medium (Met/AIC/CMA); department, which carries the Met's
    department and doubles as the classification slot; and culture.
    """
    if not keywords:
        return True
    haystack = " ".join(
        str(artwork.get(field) or "")
        for field in ("title", "medium", "department", "culture")
    ).lower()
    return any(str(kw).lower() in haystack for kw in keywords)


def artist_cap_for(
    artist_norm: str,
    max_per_artist: int,
    featured_caps: dict,
    exempt_artist: str | None = None,
) -> int | None:
    """Per-artist cap for a normalized artist name; None means uncapped.

    The exemption (--artist mode) is checked first: a featured-artist cap
    must not re-cap the artist the whole batch is about.
    """
    if exempt_artist and exempt_artist in artist_norm:
        return None
    for fname, fcap in featured_caps.items():
        if fname in artist_norm:
            return fcap
    return max_per_artist


# ---------------------------------------------------------------------------
# Label sanitization — common-sense cleanup before text goes on the image
# ---------------------------------------------------------------------------
import re as _re

# Characters that indicate the text is garbled / HTML residue / not human-readable
_GARBAGE_PATTERNS = _re.compile(
    r"<[^>]+>"           # HTML tags
    r"|QS:\S+"           # Wikidata QS: entries
    r"|Q\d{5,}"          # Wikidata Q-IDs
    r"|P\d{3,}"          # Wikidata P-IDs
    r"|https?://\S+"     # URLs
    r"|www\.\S+"         # URLs without protocol
    r"|class="           # CSS class attributes
    r"|style="           # CSS style attributes
    r'|display:\s*none'  # hidden CSS
    r"|cite_ref"         # Wikipedia citation refs
    r"|\{[^}]*\}"        # JSON/template braces
)

# Cyrillic, CJK, Arabic, Devanagari ranges — for title translation check
_NON_LATIN = _re.compile(
    r"[\u0400-\u04FF"    # Cyrillic
    r"\u4E00-\u9FFF"     # CJK
    r"\u0600-\u06FF"     # Arabic
    r"\u0900-\u097F"     # Devanagari
    r"\u3040-\u309F"     # Hiragana
    r"\u30A0-\u30FF"     # Katakana
    r"]"
)


def sanitize_label(title: str, artist: str, date: str, museum: str) -> tuple:
    """
    Clean up all four label fields so they make sense to an English-speaking
    viewer on a TV screen.

    Rules:
      - Strip any surviving HTML tags, URLs, Wikidata markup
      - Remove non-Latin text (Cyrillic, CJK, etc.) but keep the Latin portion
      - Collapse whitespace
      - Cap field lengths to prevent label overflow
      - Clean up artist field (remove life dates in parentheses if too long)
      - Ensure museum field is meaningful (not a URL or empty)
      - Reject obviously garbled labels entirely
    """
    def _clean(text: str) -> str:
        """Strip HTML, URLs, Wikidata junk, collapse whitespace."""
        if not text:
            return ""
        # Remove hidden divs first (Wikidata junk blocks)
        text = _re.sub(r'<div[^>]*style="display:\s*none[^"]*"[^>]*>.*?</div>',
                        "", text, flags=_re.DOTALL | _re.IGNORECASE)
        # Remove <sup> citation blocks entirely (including inner text)
        text = _re.sub(r"<sup[^>]*>.*?</sup>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
        # Remove all HTML tags
        text = _re.sub(r"<[^>]+>", "", text)
        # Decode HTML entities: &amp; -> &, &quot; -> ", etc.
        import html as _html_mod
        text = _html_mod.unescape(text)
        # Remove remaining garbage patterns
        text = _GARBAGE_PATTERNS.sub("", text)
        text = _re.sub(r"\s+", " ", text).strip()
        return text

    def _latin_only(text: str) -> str:
        """Extract just the Latin-script portion if mixed with non-Latin."""
        if not text:
            return ""
        if not _NON_LATIN.search(text):
            return text  # All Latin already
        # Remove non-Latin characters but keep Latin, digits, punctuation, spaces
        latin = _re.sub(r"[^\x00-\x7F\u00C0-\u024F\u1E00-\u1EFF]+", " ", text)
        latin = _re.sub(r"\s+", " ", latin).strip()
        return latin

    def _clean_artist(text: str) -> str:
        """Clean artist name — shorten overly verbose attribution strings."""
        if not text:
            return "Unknown"
        text = _clean(text)
        text = _latin_only(text)
        if not text:
            return "Unknown"
        # If the artist string is very long (verbose attribution), try to
        # extract just the name before any parenthetical biography
        if len(text) > 60:
            # "Albert Bierstadt (American, born Prussia, 1830–1902)" -> "Albert Bierstadt"
            paren = text.find("(")
            if paren > 5:
                text = text[:paren].strip()
        # Remove trailing commas, semicolons
        text = text.rstrip(",;. ")
        return text[:80]

    # Language labels used in Wikimedia multilingual titles
    _LANGUAGES = (
        "english", "french", "german", "dutch", "italian", "spanish",
        "portuguese", "russian", "chinese", "japanese", "korean",
        "arabic", "hindi", "swedish", "norwegian", "danish", "finnish",
        "polish", "czech", "hungarian", "romanian", "turkish", "greek",
        "latin", "catalan", "basque", "galician",
    )

    def _extract_english_title(text: str) -> str:
        """From a multilingual Wikimedia title, extract just the English portion.

        Wikimedia titles often look like:
          'German: Klassische Landschaft ... English: Landscape with Temple Ruins'
          'French: Le Pont Neuf English: The New Bridge'
          'Landscape with Ruins (German: Landschaft mit Ruinen)'
        """
        # Pattern 1: "English: <title>" somewhere in the string
        eng_match = _re.search(r"(?:^|[;,]\s*)English:\s*(.+?)(?:\s*(?:German|French|Dutch|Italian|Spanish|Russian|Chinese|Japanese|Korean|Latin|Portuguese):|$)",
                                text, flags=_re.IGNORECASE)
        if eng_match:
            return eng_match.group(1).strip()

        # Pattern 2: "<Language>: <foreign text> <English text>" — the English
        # part often follows after the foreign text without a label.
        # Try to detect: if it starts with a language label, look for the
        # transition to English words after the foreign block.
        for lang in _LANGUAGES:
            if text.lower().startswith(lang + ":"):
                remainder = text[len(lang) + 1:].strip()
                # If there's also an English-labeled section
                eng_idx = remainder.lower().find("english:")
                if eng_idx >= 0:
                    return remainder[eng_idx + 8:].strip()
                # Otherwise strip the language prefix and return what's left —
                # it might still be in the foreign language, which we'll handle below
                text = remainder
                break

        # Pattern 3: Mixed foreign + English without labels.
        # e.g., "Klassische Landschaft mit... Landscape with Temple Ruins"
        # Try to find where English words start after a run of foreign words.
        # Look for common English art title starters after foreign text.
        _ENG_STARTERS = r"\b(Landscape|Portrait|View|Scene|Still Life|The |A |An |Study|Night|Morning|Evening|Sunset|Sunrise|River|Lake|Mountain|Forest|Bridge|Garden|Harbor|Harbour|Church|Castle|Village|City|Street|Market|Battle|Dance|Feast|Storm|Calm|Coast|Shore|Bay|Sea|Ocean|Ship|Boat|Winter|Spring|Summer|Autumn|Interior|Exterior)"
        eng_start = _re.search(_ENG_STARTERS, text)
        if eng_start and eng_start.start() > 10:
            # There's a substantial foreign prefix before the English part
            candidate = text[eng_start.start():].strip()
            if candidate and len(candidate) > 5 and not _likely_non_english(candidate):
                return candidate

        # Pattern 4: Parenthetical foreign text — "Landscape (Landschaft mit...)"
        # Keep only the part outside the parentheses
        if "(" in text:
            outside = _re.sub(r"\([^)]*\)", "", text).strip()
            if outside and len(outside) > 3:
                text = outside

        return text

    # Common non-English words that indicate the title isn't in English
    _NON_ENGLISH_MARKERS = {
        "mit", "und", "von", "der", "die", "das", "des", "dem", "den",  # German
        "avec", "dans", "sur", "les", "des", "une", "pour", "aux",      # French
        "con", "del", "los", "las", "una", "por",                       # Spanish
        "della", "nella", "delle", "degli", "sul", "alla",              # Italian
        "van", "het", "een", "bij", "uit",                               # Dutch
        "paysage", "landschaft", "paisaje", "paesaggio", "landschap",    # "landscape"
    }

    def _likely_non_english(text: str) -> bool:
        """Heuristic: does this title look like it's in a non-English language?"""
        words = text.lower().split()
        if len(words) < 3:
            return False  # Too short to tell
        non_eng_count = sum(1 for w in words if w.strip(",.;:()") in _NON_ENGLISH_MARKERS)
        return non_eng_count >= 2  # Two or more marker words = probably not English

    def _clean_title(text: str) -> str:
        """Clean title — extract English, remove HTML residue, non-Latin portions."""
        if not text:
            return "Untitled"
        text = _clean(text)
        # Handle titles wrapped in guillemets: «Title» -> Title
        text = text.replace("\u00AB", "").replace("\u00BB", "")
        # Extract Latin portion if mixed with Cyrillic/CJK
        text = _latin_only(text)
        if not text or len(text) < 2:
            return "Untitled"
        # Try to extract English from multilingual titles
        text = _extract_english_title(text)
        # Remove leading/trailing quotes and whitespace
        text = text.strip("'\"` ")
        # Remove language-label prefixes like "Russian:" or "French:"
        for lang in _LANGUAGES:
            if text.lower().startswith(lang + ":"):
                text = text[len(lang) + 1:].strip()
                break
            if text.lower().startswith(lang + " "):
                text = text[len(lang):].strip()
                break
        # If the remaining title is still clearly non-English, flag as Untitled
        # rather than showing gibberish on the TV
        if _likely_non_english(text):
            return "Untitled"
        # Clean up orphaned punctuation from removed text
        text = _re.sub(r"^[\s.,;:]+|[\s.,;:]+$", "", text)
        if not text or len(text) < 2:
            return "Untitled"
        return text[:100]

    def _clean_date(text: str) -> str:
        """Clean date — extract just the year/date portion.
        Returns empty string for unknown/missing dates (never 'Unknown date')."""
        if not text:
            return ""
        text = _clean(text)
        text = _latin_only(text)
        # Reject meaningless date strings
        if text.lower() in ("unknown", "unknown date", "undated", "n.d.", "n/a", "none"):
            return ""
        # If it's still gibberish or too long, try to extract just a year
        if len(text) > 30 or not text.strip():
            years = _re.findall(r"\b(\d{4})\b", text)
            if years:
                return f"ca. {years[0]}" if len(years) == 1 else f"{years[0]}-{years[-1]}"
            return ""
        return text[:40]

    def _clean_museum(text: str) -> str:
        """Clean museum — ensure it's a real institution name, not a
        Google Art Project ID, Wikimedia URL, or other junk."""
        if not text:
            return ""
        text = _clean(text)
        text = _latin_only(text)
        lower = text.lower()
        # Reject non-museum strings
        _MUSEUM_REJECT = [
            "wikimedia commons", "wikimedia", "commons",
            "google cultural institute", "google art project",
            "maximum zoom level", "zoom level",
            "wikidata", "file:", "category:",
            "on facebook", "on twitter", "on instagram",
            "facebook.com", "twitter.com", "instagram.com",
            ".blogspot", ".wordpress",
        ]
        for reject in _MUSEUM_REJECT:
            if reject in lower:
                return ""
        # Reject if it looks like a hash/ID (alphanumeric gibberish)
        if _re.match(r"^[A-Za-z0-9_-]{10,}$", text.split()[0] if text.split() else ""):
            return ""
        if not text or len(text) < 3:
            return ""
        return text[:80]

    return (
        _clean_title(title),
        _clean_artist(artist),
        _clean_date(date),
        _clean_museum(museum),
    )


# ---------------------------------------------------------------------------
# State tracking (resume support)
# ---------------------------------------------------------------------------
STATE_FILE = "batch_state.json"


def load_state(state_file: str = STATE_FILE) -> dict:
    """Load batch state for resume support."""
    try:
        with open(state_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"completed_ids": [], "failed_ids": [], "started_at": None}


def save_state(state: dict, state_file: str = STATE_FILE):
    """Save batch state."""
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Met Museum batch fetcher
# ---------------------------------------------------------------------------
def gather_met_object_ids(queries: list[str], per_query: int = 100, max_ids: int = 0) -> list[int]:
    """
    Search the Met for multiple queries and gather a pool of object IDs.
    Searches the Paintings departments (11=European, 21=Modern) first to
    prioritise actual paintings over prints and drawings.

    Args:
        max_ids: Stop gathering once we have this many unique IDs (0 = no limit).
                 This prevents over-fetching when we only need a few hundred.

    Returns a deduplicated, shuffled list.
    """
    all_ids = set()

    def _budget_reached():
        return max_ids > 0 and len(all_ids) >= max_ids

    # Search painting departments first — much higher hit rate
    PAINTING_DEPTS = [11, 21]  # European Paintings, Modern & Contemporary Art
    for query in queries:
        if _budget_reached():
            logger.info(f"Met budget reached ({len(all_ids)} IDs), stopping search")
            break
        for dept in PAINTING_DEPTS:
            if _budget_reached():
                break
            logger.info(f"Searching Met (dept {dept}): '{query}'...")
            ids = search_met(query, public_domain_only=True, department_id=dept)
            if ids:
                all_ids.update(ids)
                logger.info(f"  -> {len(ids)} IDs from dept {dept} (total: {len(all_ids)})")
            time.sleep(0.3)

    # Also search without department filter for broader coverage,
    # but only if we haven't hit budget yet
    if not _budget_reached():
        for query in queries:
            if _budget_reached():
                logger.info(f"Met budget reached ({len(all_ids)} IDs), stopping search")
                break
            logger.info(f"Searching Met (all depts): '{query}'...")
            ids = search_met(query, public_domain_only=True)
            if ids:
                all_ids.update(ids)
                logger.info(f"  -> {len(ids)} IDs from all depts (total: {len(all_ids)})")
            time.sleep(0.3)

    result = list(all_ids)
    random.shuffle(result)
    # If we overshot the budget, trim to max_ids
    if max_ids > 0 and len(result) > max_ids:
        result = result[:max_ids]
    logger.info(f"Total unique Met IDs gathered: {len(result)}")
    return result


# ---------------------------------------------------------------------------
# Main batch processor
# ---------------------------------------------------------------------------
def process_single(
    artwork: dict,
    output_dir: str,
    config: dict,
    state: dict,
) -> bool:
    """Download, process, and save one artwork. Returns True on success."""
    display = config.get("display", {})
    processing = config.get("processing", {})
    overlay = config.get("overlay", {})

    art_id = f"{artwork['source']}_{artwork['id']}"

    # Skip if already done
    if art_id in state["completed_ids"]:
        return True

    cache_dir = config.get("storage", {}).get("cache_dir", "./art_cache")

    # Download
    local_path = download_image(artwork["image_url"], cache_dir)
    if not local_path:
        state["failed_ids"].append(art_id)
        save_state(state)
        return False

    # Check actual image dimensions — reject non-landscape images
    # This catches cases where the API didn't provide dimensions (e.g., Met)
    # NOTE: We do NOT add these to failed_ids — they're expected filter
    # rejections, not download errors. Adding them would permanently
    # blacklist the ID and exhaust the candidate pool on re-runs.
    if _PILImage and display.get("aspect_mode", "crop") == "crop":
        try:
            with _PILImage.open(local_path) as check_img:
                iw, ih = check_img.size
                if not is_landscape_enough(iw, ih):
                    logger.info(f"  Skipping non-landscape ({iw}x{ih}): {artwork.get('title', '')}")
                    return False
        except Exception:
            pass  # If we can't check, let process_image handle it

    # Sanitize all label fields — strip HTML, URLs, non-Latin text, etc.
    label_title, label_artist, label_date, label_museum = sanitize_label(
        artwork.get("title", "Untitled"),
        artwork.get("artist", "Unknown"),
        artwork.get("date", ""),
        artwork.get("museum", ""),
    )
    # Local images have no artist metadata — don't label them "Unknown"
    if artwork.get("source") == "local":
        label_artist = ""

    # Build a clean filename: "Artist - Title.jpg"
    artist_file = label_artist
    title_file = label_title
    # Sanitize for filesystem
    for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        artist_file = artist_file.replace(ch, '')
        title_file = title_file.replace(ch, '')
    # Truncate to avoid path length issues
    if artist_file:
        filename = f"{artist_file[:40]} - {title_file[:60]}.jpg"
    else:
        filename = f"{title_file[:60]}.jpg"
    output_path = os.path.join(output_dir, filename)

    # Skip if output already exists
    if os.path.exists(output_path):
        logger.info(f"  Already exists: {filename}")
        state["completed_ids"].append(art_id)
        save_state(state)
        return True

    # Process
    result = process_image(
        input_path=local_path,
        output_path=output_path,
        target_resolution=tuple(display.get("resolution", [3840, 2160])),
        aspect_mode=display.get("aspect_mode", "crop"),
        matte_color_config=display.get("matte_color", "auto"),
        sharpen=processing.get("sharpen", True),
        warmth_adjust=processing.get("warmth_adjust", 3),
        jpeg_quality=processing.get("jpeg_quality", 95),
        min_width=processing.get("min_width", 1500),
        min_height=processing.get("min_height", 1000),
        title=label_title if overlay.get("enabled", True) else "",
        artist=label_artist if overlay.get("enabled", True) else "",
        date=label_date if overlay.get("enabled", True) else "",
        museum=label_museum if overlay.get("enabled", True) else "",
        overlay_position=overlay.get("position", "bottom_right"),
        overlay_opacity=overlay.get("opacity", 0.85),
    )

    if result:
        state["completed_ids"].append(art_id)
        save_state(state)
        return True
    else:
        state["failed_ids"].append(art_id)
        save_state(state)
        return False


def gather_aic_artworks(queries: list[str], per_query: int = 50) -> list[dict]:
    """
    Search the Art Institute of Chicago and gather artwork dicts with image_ids.
    Returns a shuffled list of artwork dicts ready for processing.
    """
    all_artworks = []
    seen_ids = set()
    skipped_portrait = 0
    for query in queries:
        logger.info(f"Searching AIC: '{query}'...")
        results = search_aic(query, limit=per_query)
        for art in results:
            art_id = art.get("id")
            if art_id and art_id not in seen_ids:
                seen_ids.add(art_id)
                image_id = art.get("image_id")
                if image_id:
                    # Filter by aspect ratio using thumbnail dimensions
                    thumb = art.get("thumbnail", {}) or {}
                    tw = thumb.get("width", 0)
                    th = thumb.get("height", 0)
                    if tw and th and not is_landscape_enough(tw, th):
                        skipped_portrait += 1
                        continue
                    # Filter: paintings only
                    aic_medium = art.get("medium_display", "")
                    aic_class = art.get("classification_title", "")
                    if not is_painting(aic_medium, aic_class):
                        continue
                    all_artworks.append({
                        "source": "aic",
                        "id": str(art_id),
                        "title": art.get("title", "Untitled"),
                        "artist": art.get("artist_title", "Unknown"),
                        "date": art.get("date_display", ""),
                        "medium": aic_medium,
                        "department": "",
                        "image_url": f"https://www.artic.edu/iiif/2/{image_id}/full/3840,/0/default.jpg",
                        "dimensions": "",
                        "culture": "",
                        "museum": "Art Institute of Chicago",
                    })
        logger.info(f"  -> {len(results)} results from '{query}'")
        time.sleep(0.5)

    random.shuffle(all_artworks)
    if skipped_portrait:
        logger.info(f"  (Skipped {skipped_portrait} portrait/square images)")
    logger.info(f"Total unique AIC landscape artworks gathered: {len(all_artworks)}")
    return all_artworks


def gather_cma_artworks(queries: list[str], per_query: int = 50) -> list[dict]:
    """
    Search the Cleveland Museum of Art and gather artwork dicts.
    Returns a shuffled list of artwork dicts ready for processing.
    """
    all_artworks = []
    seen_ids = set()
    skipped_portrait = 0
    for query in queries:
        logger.info(f"Searching CMA: '{query}'...")
        results = search_cma(query, limit=per_query)
        for obj in results:
            art_id = obj.get("id")
            if art_id and art_id not in seen_ids:
                seen_ids.add(art_id)
                images = obj.get("images", {})
                image_url = None
                img_w, img_h = 0, 0
                for key in ("print", "web", "full"):
                    img_data = images.get(key, {})
                    if img_data.get("url"):
                        image_url = img_data["url"]
                        img_w = img_data.get("width", 0)
                        img_h = img_data.get("height", 0)
                        break

                if not image_url:
                    continue

                # Filter by aspect ratio
                if img_w and img_h and not is_landscape_enough(img_w, img_h):
                    skipped_portrait += 1
                    continue

                # Filter: paintings only
                cma_medium = obj.get("technique", "")
                cma_type = obj.get("type", "")
                if not is_painting(cma_medium, cma_type):
                    continue

                creators = obj.get("creators", [])
                artist = creators[0].get("description", "Unknown") if creators else "Unknown"
                all_artworks.append({
                    "source": "cma",
                    "id": str(art_id),
                    "title": obj.get("title", "Untitled"),
                    "artist": artist,
                    "date": obj.get("creation_date", ""),
                    "medium": cma_medium,
                    "department": obj.get("department", ""),
                    "image_url": image_url,
                    "dimensions": "",
                    "culture": obj.get("culture", [""])[0] if obj.get("culture") else "",
                    "museum": "Cleveland Museum of Art",
                })
        logger.info(f"  -> {len(results)} results from '{query}'")
        time.sleep(0.5)

    random.shuffle(all_artworks)
    if skipped_portrait:
        logger.info(f"  (Skipped {skipped_portrait} portrait/square images)")
    logger.info(f"Total unique CMA landscape artworks gathered: {len(all_artworks)}")
    return all_artworks


def gather_rijks_artworks(queries: list[str], types: list[str] = None, per_query: int = 20) -> list[dict]:
    """
    Search the Rijksmuseum and resolve artwork details.
    Returns a shuffled list of artwork dicts ready for processing.
    """
    all_artworks = []
    seen_ids = set()
    for query in queries:
        logger.info(f"Searching Rijksmuseum: '{query}'...")
        object_uris = search_rijksmuseum(query, types)
        # Only try a limited number per query to keep things fast
        random.shuffle(object_uris)
        resolved = 0
        for uri in object_uris[:per_query]:
            obj_id = uri.split("/")[-1]
            if obj_id in seen_ids:
                continue
            seen_ids.add(obj_id)
            artwork = resolve_rijks_object(uri)
            if artwork and artwork.get("image_url"):
                all_artworks.append(artwork)
                resolved += 1
            time.sleep(0.3)  # Be respectful of rate limits
        logger.info(f"  -> {resolved} resolved from '{query}'")

    random.shuffle(all_artworks)
    logger.info(f"Total unique Rijksmuseum artworks gathered: {len(all_artworks)}")
    return all_artworks


def gather_wikimedia_artworks(
    queries: list[str] = None,
    categories: list[str] = None,
    per_query: int = 50,
    max_total: int = 0,
) -> list[dict]:
    """
    Search Wikimedia Commons by text queries and/or categories.

    Args:
        max_total: Stop gathering once we have this many unique artworks
                   (0 = no limit).

    Returns a shuffled list of artwork dicts ready for processing.
    """
    all_artworks = []
    seen_ids = set()

    def _budget_reached():
        return max_total > 0 and len(all_artworks) >= max_total

    if categories:
        for cat in categories:
            if _budget_reached():
                logger.info(f"Wikimedia budget reached ({len(all_artworks)}), stopping")
                break
            logger.info(f"Searching Wikimedia category: '{cat}'...")
            results = search_wikimedia_commons("", category=cat, limit=per_query)
            for art in results:
                art_id = art.get("id")
                if art_id and art_id not in seen_ids:
                    seen_ids.add(art_id)
                    all_artworks.append(art)
            logger.info(f"  -> {len(results)} results from category '{cat}' (total: {len(all_artworks)})")
            time.sleep(0.5)

    if queries and not _budget_reached():
        for query in queries:
            if _budget_reached():
                logger.info(f"Wikimedia budget reached ({len(all_artworks)}), stopping")
                break
            logger.info(f"Searching Wikimedia: '{query}'...")
            results = search_wikimedia_commons(query, limit=per_query)
            for art in results:
                art_id = art.get("id")
                if art_id and art_id not in seen_ids:
                    seen_ids.add(art_id)
                    all_artworks.append(art)
            logger.info(f"  -> {len(results)} results from '{query}' (total: {len(all_artworks)})")
            time.sleep(0.5)

    random.shuffle(all_artworks)
    logger.info(f"Total unique Wikimedia artworks gathered: {len(all_artworks)}")
    return all_artworks


def prune_cache(cache_dir: str, max_cached: int):
    """Delete the oldest cached downloads (by mtime) beyond max_cached files."""
    cache = Path(cache_dir)
    if max_cached <= 0 or not cache.is_dir():
        return
    files = sorted(
        (f for f in cache.iterdir() if f.is_file()),
        key=lambda f: f.stat().st_mtime,
    )
    excess = len(files) - max_cached
    if excess <= 0:
        return
    removed = 0
    for f in files[:excess]:
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            logger.warning(f"Could not delete cached file {f}: {e}")
    logger.info(f"Cache pruned: removed {removed} oldest files, kept {len(files) - removed} (max_cached: {max_cached})")


def run_batch(
    config: dict,
    count: int,
    output_dir: str,
    resume: bool,
    dry_run: bool,
    keywords_any: list[str] | None = None,
    exempt_artist: str | None = None,
    candidate_multiplier: int = 3,
):
    """Main batch processing loop — pulls from all enabled museum sources.

    keywords_any, exempt_artist and candidate_multiplier come from
    --theme/--artist resolution in main(); all default to no-ops so the
    no-flag path is unchanged.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Load or initialize state
    if resume:
        state = load_state()
    else:
        state = {
            "completed_ids": [],
            "failed_ids": [],
            "started_at": datetime.now().isoformat(),
        }
        # Clear old state file so stale failed_ids don't poison future runs
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            logger.info("Cleared old batch_state.json for fresh run")

    already_done = len(state["completed_ids"])
    if resume and already_done > 0:
        logger.info(f"Resuming: {already_done} already completed, need {count - already_done} more")
    remaining = count - already_done

    if remaining <= 0:
        logger.info(f"Already have {already_done} images. Nothing to do!")
        return

    sources = config.get("art_sources", {})

    # ---- Smart candidate budget ----
    # Gather a multiple of what we need.  The default 3x assumes a ~30-40%
    # filter pass rate — right for the wide default config, but themed/artist
    # runs narrow the query space AND skew survival (the 2026-08-08
    # impressionist run passed only ~14%: portrait-heavy artists die at the
    # aspect check), so they gather 5x — see main().  The budget also sets
    # per-query fetch depth in every source, not just the total.
    candidate_budget = remaining * candidate_multiplier
    # Split budget across enabled sources (Met gets a bigger share because
    # it has more lossy filtering — many IDs turn out to be prints/drawings).
    enabled_count = sum(1 for key in ("met_museum", "art_institute_chicago",
                                       "cleveland_museum", "rijksmuseum",
                                       "wikimedia_commons", "local")
                        if sources.get(key, {}).get("enabled", key == "met_museum"))
    per_source_budget = max(80, candidate_budget // max(1, enabled_count))
    logger.info(f"Candidate budget: {candidate_budget} total, ~{per_source_budget}/source "
                f"(for {remaining} images @ {candidate_multiplier}x multiplier)")

    # ---- Build a unified pool of artworks from all enabled sources ----
    artwork_pool = []

    # Met Museum
    met_config = sources.get("met_museum", {})
    if met_config.get("enabled", True):
        met_queries = met_config.get("queries", [
            "landscape painting", "impressionist", "renaissance portrait",
            "japanese woodblock", "dutch golden age", "watercolor",
        ])
        # Met budget is 1.5x per_source because many IDs get filtered on resolve
        met_budget = int(per_source_budget * 1.5)
        per_query = max(20, met_budget // max(1, len(met_queries)))
        met_ids = gather_met_object_ids(met_queries, per_query, max_ids=met_budget)
        for obj_id in met_ids:
            artwork_pool.append({"_source": "met", "_met_id": obj_id})

    # Art Institute of Chicago
    aic_config = sources.get("art_institute_chicago", {})
    if aic_config.get("enabled", True):
        aic_queries = aic_config.get("queries", [
            "impressionist", "landscape", "modern art",
            "American painting", "European painting",
        ])
        per_q = min(100, max(20, per_source_budget // max(1, len(aic_queries))))
        aic_artworks = gather_aic_artworks(aic_queries, per_q)
        artwork_pool.extend({"_source": "aic", "_artwork": art} for art in aic_artworks)

    # Cleveland Museum of Art
    cma_config = sources.get("cleveland_museum", {})
    if cma_config.get("enabled", True):
        cma_queries = cma_config.get("queries", [
            "painting", "landscape", "portrait",
            "impressionist", "European art",
        ])
        per_q = min(100, max(20, per_source_budget // max(1, len(cma_queries))))
        cma_artworks = gather_cma_artworks(cma_queries, per_q)
        artwork_pool.extend({"_source": "cma", "_artwork": art} for art in cma_artworks)

    # Rijksmuseum
    rijks_config = sources.get("rijksmuseum", {})
    if rijks_config.get("enabled", True):
        rijks_queries = rijks_config.get("queries", [
            "Vermeer", "Rembrandt", "landscape", "portrait",
        ])
        rijks_types = rijks_config.get("types", ["painting"])
        per_q = max(10, per_source_budget // max(1, len(rijks_queries) * 2))
        rijks_artworks = gather_rijks_artworks(rijks_queries, rijks_types, per_q)
        artwork_pool.extend({"_source": "rijks", "_artwork": art} for art in rijks_artworks)

    # Wikimedia Commons
    wiki_config = sources.get("wikimedia_commons", {})
    if wiki_config.get("enabled", False):
        wiki_queries = wiki_config.get("queries", [])
        wiki_categories = wiki_config.get("categories", [
            "Raja_Ravi_Varma",
        ])
        wiki_total_queries = len(wiki_queries) + len(wiki_categories)
        per_q = max(10, per_source_budget // max(1, wiki_total_queries))
        wiki_artworks = gather_wikimedia_artworks(
            wiki_queries, wiki_categories, per_q, max_total=per_source_budget,
        )
        artwork_pool.extend({"_source": "wikimedia", "_artwork": art} for art in wiki_artworks)

    # Local folder — your own images, included as-is (no artist filters apply)
    local_config = sources.get("local", {})
    if local_config.get("enabled", False):
        local_artworks = gather_local_artworks(local_config.get("path", "./my_art"))
        artwork_pool.extend({"_source": "local", "_artwork": art} for art in local_artworks)

    # ---- Theme keywords_any post-filter (non-Met candidates) ----
    # Met candidates are bare object IDs until resolved, so they are filtered
    # in the processing loop below; every other source's candidates already
    # carry metadata here, which also makes --dry-run counts honest for them.
    skipped_keywords = 0
    if keywords_any:
        kept = []
        for item in artwork_pool:
            if item["_source"] == "met" or matches_keywords_any(item["_artwork"], keywords_any):
                kept.append(item)
            else:
                skipped_keywords += 1
        logger.info(f"keywords_any {keywords_any}: kept {len(kept)} of "
                    f"{len(artwork_pool)} candidates (Met checked at resolve time)")
        artwork_pool = kept

    # ---- Featured artists: move their candidates to the front ----
    featured_config = sources.get("featured_artists", [])
    featured_pool = []   # items for featured artists, processed first
    general_pool = []    # everything else

    if featured_config:
        featured_names = {f["name"].lower(): f.get("min_count", 2) for f in featured_config}
        logger.info(f"Featured artists: {', '.join(f['name'] for f in featured_config)}")

        for item in artwork_pool:
            # Check artist name — need to peek at the artwork dict
            artist = ""
            if item["_source"] != "met":
                artist = item.get("_artwork", {}).get("artist", "").lower()

            is_featured = False
            for fname in featured_names:
                if fname in artist:
                    is_featured = True
                    break

            if is_featured:
                featured_pool.append(item)
            else:
                general_pool.append(item)

        # Shuffle within each pool, then combine: featured first
        random.shuffle(featured_pool)
        random.shuffle(general_pool)
        artwork_pool = featured_pool + general_pool
        logger.info(f"Featured artist candidates: {len(featured_pool)} (will be processed first)")
    else:
        # No featured artists — just shuffle everything
        random.shuffle(artwork_pool)

    # Log pool composition
    source_counts = {}
    for item in artwork_pool:
        s = item["_source"]
        source_counts[s] = source_counts.get(s, 0) + 1
    logger.info(f"Candidate pool: {len(artwork_pool)} total")
    for s, c in sorted(source_counts.items()):
        logger.info(f"  {s}: {c} candidates")

    if dry_run:
        logger.info(f"DRY RUN: Would process up to {remaining} images from {len(artwork_pool)} candidates")
        for s, c in sorted(source_counts.items()):
            logger.info(f"  {s}: {c} candidates")
        logger.info(f"Output directory: {output_dir}")
        return

    # ---- Process artworks from the pool ----
    success = 0
    failures = 0
    skipped = 0
    skipped_non_painting = 0
    skipped_minor_artist = 0

    major_only = sources.get("major_artists_only", False)
    if major_only:
        logger.info("Major artists only mode: ON — filtering for well-known artists")

    # Per-artist cap to prevent any single artist from dominating
    max_per_artist = sources.get("max_per_artist", 4)
    artist_counts = {}  # normalized artist name -> count of saved works
    # Build featured artist caps (use their min_count as cap)
    featured_caps = {}
    if featured_config:
        for f in featured_config:
            featured_caps[f["name"].lower()] = f.get("min_count", 3)
    logger.info(f"Max works per artist: {max_per_artist} (featured artists have their own caps)")

    enabled_sources = [s for s, c in sources.items() if isinstance(c, dict) and c.get("enabled", True)]
    logger.info(f"Processing {remaining} images -> {output_dir}")
    logger.info(f"Pool: {len(artwork_pool)} candidates from {len(enabled_sources)} sources")
    logger.info("-" * 60)

    skipped_artist_cap = 0

    for item in artwork_pool:
        if success >= remaining:
            break

        # Resolve the artwork dict
        if item["_source"] == "met":
            obj_id = item["_met_id"]
            art_id = f"met_museum_{obj_id}"
            if art_id in state["completed_ids"] or art_id in state["failed_ids"]:
                continue
            artwork = get_met_object(obj_id)
            if not artwork or not artwork["image_url"]:
                skipped += 1
                continue
            # Filter: paintings only (skip drawings, prints, photos, etc.)
            if not is_painting(artwork.get("medium", ""), artwork.get("department", "")):
                logger.debug(f"  Skipping non-painting: {artwork.get('medium', '')} — \"{artwork.get('title', '')}\"")
                skipped_non_painting += 1
                continue
            # Theme keywords_any — Met is checked here because its metadata
            # only exists after resolve (see the pool-level filter above)
            if not matches_keywords_any(artwork, keywords_any):
                skipped_keywords += 1
                continue
        else:
            artwork = item["_artwork"]
            art_id = f"{artwork['source']}_{artwork['id']}"
            if art_id in state["completed_ids"] or art_id in state["failed_ids"]:
                continue

        # Local images are the user's own choices — exempt from the title,
        # major-artist, and per-artist-cap filters (they have no artist metadata)
        is_local = artwork.get("source") == "local"

        # Filter: skip studies, fragments, and non-display pieces by title
        if not is_local and not is_display_worthy(artwork.get("title", "")):
            logger.debug(f"  Skipping non-display: \"{artwork.get('title', '')}\"")
            skipped_non_painting += 1
            continue

        # Filter by major artists if enabled — but always allow featured artists through
        is_featured = False
        if featured_config:
            artist_lower = artwork.get("artist", "").lower()
            for f in featured_config:
                if f["name"].lower() in artist_lower:
                    is_featured = True
                    break
        # --artist mode: the requested artist is exempt from the major-artist
        # filter and the per-artist cap — the batch is ABOUT that artist
        is_exempt = bool(exempt_artist) and exempt_artist in artwork.get("artist", "").lower()
        if major_only and not is_local and not is_featured and not is_exempt and not is_major_artist(artwork.get("artist", "")):
            # INFO, not debug: the summary counts these, and deciding whether a
            # theme needs major_artists_only: false requires seeing WHO was
            # skipped (44 nameless skips in the 2026-08-08 impressionist run).
            logger.info(f"  Skipping minor artist: {artwork.get('artist', 'Unknown')} — \"{artwork.get('title', '')}\"")
            skipped_minor_artist += 1
            continue

        # Per-artist cap — normalize the artist name for counting
        artist_norm = artwork.get("artist", "Unknown").lower().strip()
        # Extract just the primary name (before parenthetical bio)
        paren = artist_norm.find("(")
        if paren > 3:
            artist_norm = artist_norm[:paren].strip()
        artist_norm = artist_norm.rstrip(",;. ")

        if not is_local:
            # Determine cap for this artist (None = uncapped, --artist mode)
            cap = artist_cap_for(artist_norm, max_per_artist, featured_caps, exempt_artist)
            current = artist_counts.get(artist_norm, 0)
            if cap is not None and current >= cap:
                logger.debug(f"  Artist cap reached ({current}/{cap}): {artwork.get('artist', '')} — \"{artwork.get('title', '')}\"")
                skipped_artist_cap += 1
                continue

        progress = f"[{success + already_done + 1}/{count}]"
        logger.info(f"{progress} ({artwork['source']}) \"{artwork['title']}\" by {artwork['artist']}")

        if process_single(artwork, output_dir, config, state):
            success += 1
            if not is_local:
                artist_counts[artist_norm] = artist_counts.get(artist_norm, 0) + 1
        else:
            failures += 1

        # Rate limit: ~1 request per second to be respectful
        time.sleep(1.0)

    # Summary
    logger.info("=" * 60)
    logger.info(f"Batch complete!")
    logger.info(f"  Pool size:       {len(artwork_pool)} candidates")
    logger.info(f"  Saved:           {success} new images")
    logger.info(f"  Previously done: {already_done}")
    logger.info(f"  Download/process failures: {failures}")
    logger.info(f"  Skipped (no image / non-landscape): {skipped}")
    logger.info(f"  Skipped (non-painting): {skipped_non_painting}")
    logger.info(f"  Skipped (minor artist): {skipped_minor_artist}")
    logger.info(f"  Skipped (artist cap):  {skipped_artist_cap}")
    if keywords_any:
        logger.info(f"  Skipped (keywords_any): {skipped_keywords}")
    logger.info(f"  Output:          {output_dir}")
    logger.info(f"  Total on disk:   {len(os.listdir(output_dir))} images ready")
    logger.info("")
    logger.info("Tip: If yield is low, try 'major_artists_only: false' in config.yaml")

    # Prune the download cache to the configured limit
    storage = config.get("storage", {})
    prune_cache(storage.get("cache_dir", "./art_cache"), storage.get("max_cached", 500))

    # Final state
    state["finished_at"] = datetime.now().isoformat()
    save_state(state)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Batch-build a gallery of Frame TV-ready art",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
How to use:
  1. Run this script to download and process art
  2. Copy the output folder to a USB drive
  3. Plug USB into the Frame TV's One Connect Box
  4. On the TV: Menu -> Art Mode -> My Photos -> import from USB
  5. IMPORTANT: enable the slideshow (Art Mode -> My Photos -> select all ->
     Start Slideshow, shuffle on, pick an interval). Without this step the
     TV shows ONE static image forever — this script does not rotate art.

The TV handles rotation. No Pi or server needed.
        """,
    )

    parser.add_argument(
        "-c", "--config", default="config.yaml", help="Path to config file"
    )
    parser.add_argument(
        "--count", type=int, default=100,
        help="Number of images to process (default: 100)"
    )
    parser.add_argument(
        "--output", default="./frame_tv_art",
        help="Output directory for processed images (default: ./frame_tv_art)"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume an interrupted batch"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be fetched without downloading"
    )
    theme_group = parser.add_mutually_exclusive_group()
    theme_group.add_argument(
        "--theme", default=None, metavar="NAME",
        help="Build from a named theme in config.yaml's themes: catalog "
             "(see --list-themes)"
    )
    theme_group.add_argument(
        "--artist", default=None, metavar="NAME",
        help='Single-artist batch, e.g. --artist "Claude Monet". Searches all '
             "sources for that name and lifts the per-artist cap for them."
    )
    parser.add_argument(
        "--list-themes", action="store_true",
        help="Print the theme catalog from config.yaml and exit"
    )
    parser.add_argument(
        "--log-file", default=None,
        help=f"Where to write the log (default: config.yaml logging.file, "
             f"else {DEFAULT_LOG_FILE})"
    )
    parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help=f"Log verbosity (default: config.yaml logging.level, "
             f"else {DEFAULT_LOG_LEVEL})"
    )

    args = parser.parse_args()

    # Chicken and egg: the logging settings live in the config, but loading the
    # config is itself something worth logging about. So load first, hold
    # anything that wants reporting, configure logging, then say it. The
    # alternative -- configuring twice with basicConfig(force=True) -- would
    # briefly open the wrong log file.
    config_path = Path(args.config)
    config_missing = not config_path.exists()
    parse_error = None
    config = None

    if not config_missing:
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            parse_error = e

    configure_logging(config, args.log_file, args.log_level)

    if parse_error is not None:
        # Previously this surfaced as a bare traceback, since logging was
        # already up. Now that config is read first, report it properly.
        logger.error("Could not parse %s: %s", args.config, parse_error)
        return 1

    if config_missing:
        logger.warning(f"Config {args.config} not found, using defaults")
        config = {
            "display": {"resolution": [3840, 2160], "aspect_mode": "crop", "matte_color": "auto"},
            "processing": {"sharpen": True, "warmth_adjust": 3, "jpeg_quality": 95},
            "overlay": {"enabled": True, "position": "bottom_right", "opacity": 0.85},
            "storage": {"cache_dir": "./art_cache"},
            "art_sources": {"met_museum": {"queries": [
                "landscape painting", "impressionist", "renaissance portrait",
                "japanese woodblock", "dutch golden age", "watercolor",
                "abstract modern art", "photography nature",
            ]}},
        }

    if args.list_themes:
        print(format_theme_catalog(theme_catalog(config)))
        return 0

    try:
        config, keywords_any, exempt_artist = resolve_batch_inputs(
            config, args.theme, args.artist
        )
    except UnknownThemeError as e:
        logger.error(str(e))
        return 1

    if args.theme:
        logger.info(f"Theme: {args.theme}")
    elif args.artist:
        logger.info(f"Single-artist batch: {args.artist}")

    # Themed/artist runs deepen the candidate pool: a theme narrows the query
    # space and skews toward filter-heavy candidates, so 3x under-gathers
    # (43/200 on the 2026-08-08 impressionist run). The default path stays 3x.
    multiplier = 5 if (args.theme or args.artist) else 3
    run_batch(config, args.count, args.output, args.resume, args.dry_run,
              keywords_any=keywords_any, exempt_artist=exempt_artist,
              candidate_multiplier=multiplier)
    return 0


if __name__ == "__main__":
    # main() now has a failure path (unparseable config), so propagate its exit
    # code rather than always exiting 0.
    sys.exit(main())
