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
    fetch_random_met_artwork,
    fetch_random_rijks_artwork,
    fetch_random_aic_artwork,
    fetch_random_cma_artwork,
    fetch_random_wikimedia_artwork,
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
def gather_met_object_ids(queries: list[str], per_query: int = 100) -> list[int]:
    """
    Search the Met for multiple queries and gather a pool of object IDs.
    Searches the Paintings departments (11=European, 21=Modern) first to
    prioritise actual paintings over prints and drawings.
    Returns a deduplicated, shuffled list.
    """
    all_ids = set()

    # Search painting departments first — much higher hit rate
    PAINTING_DEPTS = [11, 21]  # European Paintings, Modern & Contemporary Art
    for query in queries:
        for dept in PAINTING_DEPTS:
            logger.info(f"Searching Met (dept {dept}): '{query}'...")
            ids = search_met(query, public_domain_only=True, department_id=dept)
            if ids:
                all_ids.update(ids)  # Keep ALL results — don't sub-sample
                logger.info(f"  -> {len(ids)} IDs from dept {dept}")
            time.sleep(0.3)

    # Also search without department filter for broader coverage
    for query in queries:
        logger.info(f"Searching Met (all depts): '{query}'...")
        ids = search_met(query, public_domain_only=True)
        if ids:
            all_ids.update(ids)  # Keep ALL results
            logger.info(f"  -> {len(ids)} IDs from all depts")
        time.sleep(0.3)

    result = list(all_ids)
    random.shuffle(result)
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

    # Build a clean filename: "Artist - Title.jpg"
    artist_clean = artwork.get("artist", "Unknown").strip()
    title_clean = artwork.get("title", "Untitled").strip()
    # Sanitize for filesystem
    for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        artist_clean = artist_clean.replace(ch, '')
        title_clean = title_clean.replace(ch, '')
    # Truncate to avoid path length issues
    filename = f"{artist_clean[:40]} - {title_clean[:60]}.jpg"
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
        title=title_clean if overlay.get("enabled", True) else "",
        artist=artist_clean if overlay.get("enabled", True) else "",
        date=artwork.get("date", "") if overlay.get("enabled", True) else "",
        museum=artwork.get("museum", "") if overlay.get("enabled", True) else "",
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
) -> list[dict]:
    """
    Search Wikimedia Commons by text queries and/or categories.
    Returns a shuffled list of artwork dicts ready for processing.
    """
    all_artworks = []
    seen_ids = set()

    if categories:
        for cat in categories:
            logger.info(f"Searching Wikimedia category: '{cat}'...")
            results = search_wikimedia_commons("", category=cat, limit=per_query)
            for art in results:
                art_id = art.get("id")
                if art_id and art_id not in seen_ids:
                    seen_ids.add(art_id)
                    all_artworks.append(art)
            logger.info(f"  -> {len(results)} results from category '{cat}'")
            time.sleep(0.5)

    if queries:
        for query in queries:
            logger.info(f"Searching Wikimedia: '{query}'...")
            results = search_wikimedia_commons(query, limit=per_query)
            for art in results:
                art_id = art.get("id")
                if art_id and art_id not in seen_ids:
                    seen_ids.add(art_id)
                    all_artworks.append(art)
            logger.info(f"  -> {len(results)} results from '{query}'")
            time.sleep(0.5)

    random.shuffle(all_artworks)
    logger.info(f"Total unique Wikimedia artworks gathered: {len(all_artworks)}")
    return all_artworks


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

    # ---- Build a unified pool of artworks from all enabled sources ----
    artwork_pool = []

    # Met Museum
    met_config = sources.get("met_museum", {})
    if met_config.get("enabled", True):
        met_queries = met_config.get("queries", [
            "landscape painting", "impressionist", "renaissance portrait",
            "japanese woodblock", "dutch golden age", "watercolor",
        ])
        # Need a large pool because many will be filtered out
        per_query = max(100, remaining * 5 // len(met_queries))
        met_ids = gather_met_object_ids(met_queries, per_query)
        # We'll resolve Met objects on-the-fly since there can be thousands
        # Store them as lightweight placeholders
        for obj_id in met_ids:
            artwork_pool.append({"_source": "met", "_met_id": obj_id})

    # Art Institute of Chicago
    aic_config = sources.get("art_institute_chicago", {})
    if aic_config.get("enabled", True):
        aic_queries = aic_config.get("queries", [
            "impressionist", "landscape", "modern art",
            "American painting", "European painting",
        ])
        per_q = 100  # AIC API max per request
        aic_artworks = gather_aic_artworks(aic_queries, per_q)
        for art in aic_artworks:
            artwork_pool.append({"_source": "aic", "_artwork": art})

    # Cleveland Museum of Art
    cma_config = sources.get("cleveland_museum", {})
    if cma_config.get("enabled", True):
        cma_queries = cma_config.get("queries", [
            "painting", "landscape", "portrait",
            "impressionist", "European art",
        ])
        per_q = 100  # CMA API max per request
        cma_artworks = gather_cma_artworks(cma_queries, per_q)
        for art in cma_artworks:
            artwork_pool.append({"_source": "cma", "_artwork": art})

    # Rijksmuseum
    rijks_config = sources.get("rijksmuseum", {})
    if rijks_config.get("enabled", True):
        rijks_queries = rijks_config.get("queries", [
            "Vermeer", "Rembrandt", "landscape", "portrait",
        ])
        rijks_types = rijks_config.get("types", ["painting"])
        per_q = max(10, remaining // (len(rijks_queries) * 2))
        rijks_artworks = gather_rijks_artworks(rijks_queries, rijks_types, per_q)
        for art in rijks_artworks:
            artwork_pool.append({"_source": "rijks", "_artwork": art})

    # Wikimedia Commons
    wiki_config = sources.get("wikimedia_commons", {})
    if wiki_config.get("enabled", False):
        wiki_queries = wiki_config.get("queries", [])
        wiki_categories = wiki_config.get("categories", [
            "Raja_Ravi_Varma",
        ])
        per_q = max(30, remaining // max(1, len(wiki_queries) + len(wiki_categories)))
        wiki_artworks = gather_wikimedia_artworks(wiki_queries, wiki_categories, per_q)
        for art in wiki_artworks:
            artwork_pool.append({"_source": "wikimedia", "_artwork": art})

    # Shuffle to mix sources together
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
        source_counts = {}
        for item in artwork_pool:
            s = item["_source"]
            source_counts[s] = source_counts.get(s, 0) + 1
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

    enabled_sources = [s for s, c in sources.items() if isinstance(c, dict) and c.get("enabled", True)]
    logger.info(f"Processing {remaining} images -> {output_dir}")
    logger.info(f"Pool: {len(artwork_pool)} candidates from {len(enabled_sources)} sources")
    logger.info("-" * 60)

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

        # Filter by major artists if enabled
        if major_only and not is_major_artist(artwork.get("artist", "")):
            logger.debug(f"  Skipping minor artist: {artwork.get('artist', 'Unknown')} — \"{artwork.get('title', '')}\"")
            skipped_minor_artist += 1
            continue

        progress = f"[{success + already_done + 1}/{count}]"
        logger.info(f"{progress} ({artwork['source']}) \"{artwork['title']}\" by {artwork['artist']}")

        if process_single(artwork, output_dir, config, state):
            success += 1
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
    logger.info(f"  Output:          {output_dir}")
    logger.info(f"  Total on disk:   {len(os.listdir(output_dir))} images ready")
    logger.info("")
    logger.info("Tip: If yield is low, try 'major_artists_only: false' in config.yaml")

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
  5. Set to shuffle/slideshow — done!

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
            "display": {"resolution": [3840, 2160], "aspect_mode": "matte", "matte_color": "auto"},
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
