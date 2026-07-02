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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("batch_build.log"),
    ],
)
logger = logging.getLogger("batch_build")


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


def run_batch(config: dict, count: int, output_dir: str, resume: bool, dry_run: bool):
    """Main batch processing loop — pulls from all enabled museum sources."""
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
    # Only gather ~3x what we need.  Typical filter pass rate is ~30-40%,
    # so a 3x multiplier gives a comfortable margin without over-fetching.
    candidate_budget = remaining * 3
    # Split budget across enabled sources (Met gets a bigger share because
    # it has more lossy filtering — many IDs turn out to be prints/drawings).
    enabled_count = sum(1 for key in ("met_museum", "art_institute_chicago",
                                       "cleveland_museum", "rijksmuseum",
                                       "wikimedia_commons", "local")
                        if sources.get(key, {}).get("enabled", key == "met_museum"))
    per_source_budget = max(80, candidate_budget // max(1, enabled_count))
    logger.info(f"Candidate budget: {candidate_budget} total, ~{per_source_budget}/source "
                f"(for {remaining} images @ 3x multiplier)")

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
        if major_only and not is_local and not is_featured and not is_major_artist(artwork.get("artist", "")):
            logger.debug(f"  Skipping minor artist: {artwork.get('artist', 'Unknown')} — \"{artwork.get('title', '')}\"")
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
            # Determine cap for this artist
            cap = max_per_artist
            for fname, fcap in featured_caps.items():
                if fname in artist_norm:
                    cap = fcap
                    break

            current = artist_counts.get(artist_norm, 0)
            if current >= cap:
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

    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
    else:
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

    run_batch(config, args.count, args.output, args.resume, args.dry_run)


if __name__ == "__main__":
    main()
