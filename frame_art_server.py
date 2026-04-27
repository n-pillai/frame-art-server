#!/usr/bin/env python3
"""
frame_art_server.py — Main orchestrator for the Frame Art Server.

This is the entry point. It ties together:
  - Museum API art fetching
  - Gallery-quality image processing
  - Samsung Frame TV upload and display
  - Time-based scheduling and rotation

Usage:
  python frame_art_server.py                   # Run the scheduled rotation daemon
  python frame_art_server.py --once            # Fetch and display one artwork, then exit
  python frame_art_server.py --upload <path>   # Upload and display a specific image
  python frame_art_server.py --list            # List art currently on the TV
  python frame_art_server.py --status          # Show scheduler and TV status
  python frame_art_server.py --test-fetch      # Test fetching art without uploading to TV
"""

import argparse
import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from art_sources import (
    download_image,
    fetch_random_met_artwork,
    fetch_random_rijks_artwork,
    fetch_random_local_artwork,
)
from image_processor import process_image
from scheduler import ArtScheduler
from tv_controller import FrameTVController

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
RUNNING = True


def signal_handler(sig, frame):
    global RUNNING
    RUNNING = False
    print("\nShutting down gracefully...")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(config_path: str = "config.yaml") -> dict:
    """Load and validate configuration."""
    path = Path(config_path)
    if not path.exists():
        print(f"Config file not found: {config_path}")
        print("Copy config.yaml.example to config.yaml and edit it.")
        sys.exit(1)

    with open(path) as f:
        config = yaml.safe_load(f)

    return config


# ---------------------------------------------------------------------------
# History tracking (avoid repeats)
# ---------------------------------------------------------------------------
class ArtHistory:
    """Track which artworks have been shown to avoid repeats."""

    def __init__(self, history_file: str, max_size: int = 200):
        self.history_file = history_file
        self.max_size = max_size
        self.shown = self._load()

    def _load(self) -> list:
        try:
            with open(self.history_file) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self):
        with open(self.history_file, "w") as f:
            json.dump(self.shown, f, indent=2)

    def was_shown(self, art_id: str) -> bool:
        return art_id in self.shown

    def record(self, art_id: str, metadata: dict = None):
        self.shown.append(art_id)
        if len(self.shown) > self.max_size:
            self.shown = self.shown[-self.max_size:]
        self._save()

    def count(self) -> int:
        return len(self.shown)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def fetch_artwork(config: dict, queries: list[str] = None) -> dict | None:
    """Fetch a random artwork from enabled sources."""
    sources = config.get("art_sources", {})
    available_sources = []

    if sources.get("met_museum", {}).get("enabled"):
        available_sources.append("met")
    if sources.get("rijksmuseum", {}).get("enabled"):
        available_sources.append("rijks")
    if sources.get("local", {}).get("enabled"):
        available_sources.append("local")

    if not available_sources:
        logging.error("No art sources enabled in config!")
        return None

    # Pick a random source
    random.shuffle(available_sources)

    for source in available_sources:
        if source == "met":
            met_config = sources["met_museum"]
            q = queries or met_config.get("queries", ["painting"])
            artwork = fetch_random_met_artwork(
                q, met_config.get("public_domain_only", True)
            )
            if artwork:
                return artwork

        elif source == "rijks":
            rijks_config = sources["rijksmuseum"]
            q = queries or rijks_config.get("queries", ["painting"])
            artwork = fetch_random_rijks_artwork(
                q, rijks_config.get("types")
            )
            if artwork:
                return artwork

        elif source == "local":
            local_path = sources["local"].get("path", "./my_art")
            artwork = fetch_random_local_artwork(local_path)
            if artwork:
                return artwork

    return None


def process_and_upload(
    artwork: dict,
    config: dict,
    tv: FrameTVController | None = None,
) -> bool:
    """Download, process, and upload an artwork to the Frame TV."""
    storage = config.get("storage", {})
    display = config.get("display", {})
    processing = config.get("processing", {})

    cache_dir = storage.get("cache_dir", "./art_cache")

    # Download the image
    local_path = download_image(artwork["image_url"], cache_dir)
    if not local_path:
        return False

    # Process for gallery-quality display
    processed_dir = os.path.join(cache_dir, "processed")
    ext = ".jpg"
    output_filename = f"{artwork['source']}_{artwork['id']}{ext}"
    output_path = os.path.join(processed_dir, output_filename)

    overlay = config.get("overlay", {})

    result = process_image(
        input_path=local_path,
        output_path=output_path,
        target_resolution=tuple(display.get("resolution", [3840, 2160])),
        aspect_mode=display.get("aspect_mode", "matte"),
        matte_color_config=display.get("matte_color", "neutral"),
        sharpen=processing.get("sharpen", True),
        warmth_adjust=processing.get("warmth_adjust", 0),
        jpeg_quality=processing.get("jpeg_quality", 95),
        min_width=processing.get("min_width", 1500),
        min_height=processing.get("min_height", 1000),
        title=artwork.get("title", "") if overlay.get("enabled", True) else "",
        artist=artwork.get("artist", "") if overlay.get("enabled", True) else "",
        date=artwork.get("date", "") if overlay.get("enabled", True) else "",
        overlay_position=overlay.get("position", "bottom_right"),
        overlay_opacity=overlay.get("opacity", 0.85),
    )

    if not result:
        return False

    # Upload to TV if connected
    if tv:
        matte_type = display.get("matte_type", "none")
        content_id = tv.upload_image(result, matte_type=matte_type)
        if content_id:
            tv.set_active_art(content_id)
            return True
        return False

    return True


def upload_fallback(config: dict, tv: FrameTVController | None) -> bool:
    """
    Upload a random already-processed image from the cache as a fallback.

    Used when all fetch attempts fail — at minimum the TV keeps rotating
    rather than going stale.
    """
    logger = logging.getLogger("frame_art")
    storage = config.get("storage", {})
    cache_dir = storage.get("cache_dir", "./art_cache")
    processed_dir = os.path.join(cache_dir, "processed")

    candidates = list(Path(processed_dir).glob("*.jpg")) + list(Path(processed_dir).glob("*.png"))
    if not candidates:
        logger.warning("No cached images available for fallback")
        return False

    chosen = random.choice(candidates)
    logger.info(f"Fallback: uploading cached image {chosen.name}")

    if tv:
        matte_type = config.get("display", {}).get("matte_type", "none")
        content_id = tv.upload_image(str(chosen), matte_type=matte_type)
        if content_id:
            tv.set_active_art(content_id)
            logger.info(f"Fallback displayed: {chosen.name}")
            return True
        logger.error("Fallback upload also failed")
        return False

    return True  # offline mode, nothing to upload


def run_once(config: dict, tv: FrameTVController | None, history: ArtHistory, queries: list[str] = None):
    """Fetch and display one artwork."""
    logger = logging.getLogger("frame_art")

    # Try up to 5 times to find an artwork we haven't shown recently
    for attempt in range(5):
        artwork = fetch_artwork(config, queries)
        if not artwork:
            logger.warning(f"Attempt {attempt + 1}: No artwork found")
            continue

        art_id = f"{artwork['source']}:{artwork['id']}"
        if history.was_shown(art_id):
            logger.debug(f"Already shown {art_id}, trying again...")
            continue

        logger.info(
            f"Selected: \"{artwork['title']}\" by {artwork['artist']} "
            f"[{artwork['source']}]"
        )

        if process_and_upload(artwork, config, tv):
            history.record(art_id, artwork)
            logger.info(
                f"Now displaying: {artwork['title']} by {artwork['artist']}"
            )
            return True
        else:
            logger.warning(f"Failed to process/upload, trying another...")

    logger.warning("Failed to find and display artwork after 5 attempts — trying cached fallback")
    return upload_fallback(config, tv)


def run_daemon(config: dict, tv: FrameTVController | None):
    """Run the main scheduling loop."""
    logger = logging.getLogger("frame_art")

    storage = config.get("storage", {})
    history = ArtHistory(
        storage.get("history_file", "./art_history.json"),
        storage.get("history_size", 200),
    )

    scheduler = ArtScheduler(config.get("schedule", {}))

    # Gather default queries from all enabled sources
    sources = config.get("art_sources", {})
    default_queries = []
    if sources.get("met_museum", {}).get("enabled"):
        default_queries.extend(sources["met_museum"].get("queries", []))
    if sources.get("rijksmuseum", {}).get("enabled"):
        default_queries.extend(sources["rijksmuseum"].get("queries", []))

    logger.info("Frame Art Server daemon started")
    logger.info(f"Schedule mode: {scheduler.mode}")
    logger.info(f"History: {history.count()} artworks shown previously")

    # Initial art change
    queries = scheduler.get_current_queries(default_queries)
    if run_once(config, tv, history, queries):
        scheduler.mark_changed()

    # Main loop
    while RUNNING:
        time.sleep(60)  # Check every minute

        if not RUNNING:
            break

        if scheduler.should_change_art():
            queries = scheduler.get_current_queries(default_queries)
            status = scheduler.get_status()
            logger.info(f"Scheduler triggered art change: {status}")

            if run_once(config, tv, history, queries):
                scheduler.mark_changed()

    logger.info("Frame Art Server stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def setup_logging(config: dict):
    """Configure logging based on config."""
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    log_file = log_config.get("file", "./frame_art.log")

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file),
    ]

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
    )


def connect_tv(config: dict) -> FrameTVController | None:
    """Create and connect to the Frame TV."""
    tv_config = config.get("tv", {})
    ip = tv_config.get("ip", "")

    if not ip or ip == "192.168.1.XXX":
        logging.warning(
            "TV IP not configured. Running in offline mode "
            "(art will be fetched and processed but not uploaded)."
        )
        return None

    tv = FrameTVController(
        ip=ip,
        port=tv_config.get("port", 8002),
        token_file=tv_config.get("token_file", "tv_token.txt"),
    )

    if tv.connect():
        if tv.is_art_mode_supported():
            logging.info("Frame TV connected and Art Mode supported")
            return tv
        else:
            logging.error("Connected but Art Mode not supported. Is this a Frame TV?")
    else:
        logging.warning("Could not connect to TV. Running in offline mode.")

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Frame Art Server — Gallery-quality art for your Samsung Frame TV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                      Start the scheduled rotation daemon
  %(prog)s --once               Display one artwork and exit
  %(prog)s --test-fetch         Test fetching art (no TV needed)
  %(prog)s --upload photo.jpg   Upload a specific image to the TV
  %(prog)s --list               List artworks on the TV
  %(prog)s --status             Show current status
        """,
    )

    parser.add_argument(
        "-c", "--config", default="config.yaml", help="Path to config file"
    )
    parser.add_argument(
        "--once", action="store_true", help="Fetch and display one artwork, then exit"
    )
    parser.add_argument(
        "--upload", metavar="PATH", help="Upload a specific image to the TV"
    )
    parser.add_argument(
        "--list", action="store_true", help="List artworks currently on the TV"
    )
    parser.add_argument(
        "--status", action="store_true", help="Show current scheduler and TV status"
    )
    parser.add_argument(
        "--test-fetch",
        action="store_true",
        help="Test fetching and processing art without connecting to TV",
    )

    args = parser.parse_args()
    config = load_config(args.config)
    setup_logging(config)
    logger = logging.getLogger("frame_art")

    # --- Test fetch mode (no TV needed) ---
    if args.test_fetch:
        logger.info("Test fetch mode — no TV connection needed")
        storage = config.get("storage", {})
        history = ArtHistory(
            storage.get("history_file", "./art_history.json"),
            storage.get("history_size", 200),
        )
        if run_once(config, tv=None, history=history):
            logger.info("Test fetch successful! Check art_cache/processed/")
        else:
            logger.error("Test fetch failed")
        return

    # --- Connect to TV ---
    tv = connect_tv(config)

    # --- Upload specific image ---
    if args.upload:
        if not tv:
            logger.error("Cannot upload: TV not connected")
            sys.exit(1)

        artwork = {
            "source": "local",
            "id": Path(args.upload).stem,
            "title": Path(args.upload).stem,
            "artist": "",
            "image_url": args.upload,
        }
        if process_and_upload(artwork, config, tv):
            logger.info(f"Uploaded and displaying: {args.upload}")
        else:
            logger.error(f"Failed to upload: {args.upload}")
        return

    # --- List art on TV ---
    if args.list:
        if not tv:
            logger.error("Cannot list: TV not connected")
            sys.exit(1)

        art_list = tv.list_uploaded_art()
        print(f"\nArtwork on TV ({len(art_list)} items):")
        print("-" * 60)
        for art in art_list:
            cid = art.get("content_id", "?")
            cat = art.get("category_id", "?")
            print(f"  [{cat}] {cid}")
        return

    # --- Status ---
    if args.status:
        scheduler = ArtScheduler(config.get("schedule", {}))
        status = scheduler.get_status()
        print("\nFrame Art Server Status")
        print("=" * 40)
        print(f"  Schedule mode:  {status['mode']}")
        print(f"  Current slot:   {status['current_slot'] or 'N/A'}")
        print(f"  Current mood:   {status['current_mood'] or 'N/A'}")
        print(f"  Last change:    {status['last_change'] or 'Never'}")
        print(f"  Should change:  {status['should_change']}")

        if tv:
            print(f"\n  TV connected:   Yes ({config['tv']['ip']})")
            print(f"  Art mode:       {tv.get_art_mode_status()}")
            art_count = len(tv.list_uploaded_art())
            print(f"  Art on TV:      {art_count} items")
        else:
            print(f"\n  TV connected:   No")
        return

    # --- Run once ---
    if args.once:
        storage = config.get("storage", {})
        history = ArtHistory(
            storage.get("history_file", "./art_history.json"),
            storage.get("history_size", 200),
        )
        run_once(config, tv, history)
        return

    # --- Daemon mode (default) ---
    run_daemon(config, tv)


if __name__ == "__main__":
    main()
