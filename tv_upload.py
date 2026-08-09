#!/usr/bin/env python3
"""Upload a folder of processed art to the Frame TV over the network.

Replaces the USB walk: batch_build.py writes a folder, this pushes it straight
to the TV with the mat already set to "none" per image — so a full refresh is
tv_delete.py --apply, then this, and tv_no_mat.py becomes a repair tool rather
than a required step. Proven live 2026-08-08: art.upload() returned a working
content_id with matte "none" applied at upload time (content_type "mobile").

SAFETY
------
Read-only by default: without --apply it reports what would upload. Every
applied run writes a receipt (uploaded_artworks_<timestamp>.json, kept out of
git) mapping each filename to its new content_id — useful for a targeted
delete later. The TV does NOT deduplicate: re-running --apply uploads
everything again as new copies, so upload into a freshly-cleared library
(tv_delete.py) or expect duplicates.

USAGE
-----
    pip install samsungtvws

    # 1. Dry run: what would upload, and how much.
    python tv_upload.py --ip 192.168.1.42 --folder ./frame_tv_art_impressionist

    # 2. Upload it (10-20 min for a 100+ image batch; websocket, not USB speed).
    python tv_upload.py --ip 192.168.1.42 --folder ./frame_tv_art_impressionist --apply

PRECONDITIONS (see docs/solutions/integration-issues/
frame-tv-art-channel-pairing-and-matte-api-2026-08-07.md)
- TV in Art Mode or fully on; pairing prompt accepted once (tv_session.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from tv_session import connect

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}

PRECONDITIONS_HINT = """\
Could not reach the TV's art channel. Check, in order:
  1. The TV is in Art Mode or fully ON (standby -> ms.channel.timeOut).
  2. The IP is right and this machine is on the same network.
  3. Pairing: first contact shows an Allow prompt on the TV -- accept it.
Details: docs/solutions/integration-issues/
frame-tv-art-channel-pairing-and-matte-api-2026-08-07.md
Manual fallback: copy the folder to a FAT32/exFAT USB stick -> One Connect Box
-> TV menu > Art Mode > My Photos > import from USB."""


# ---------------------------------------------------------------------------
# Pure logic -- testable without a TV (test_tv_upload.py)
# ---------------------------------------------------------------------------

def select_images(folder: str) -> list[Path]:
    """The image files an --apply run would upload, sorted by name.

    Only common image extensions; anything else in the folder (logs, undo
    files, subdirectories) is ignored rather than errored on.
    """
    root = Path(folder)
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )


def total_megabytes(paths: list[Path]) -> float:
    return round(sum(p.stat().st_size for p in paths) / (1024 * 1024), 1)


def build_receipt(uploaded: list[tuple[str, str]], matte: str) -> dict:
    """Receipt payload: filename -> content_id for everything uploaded."""
    return {
        "written_by": "tv_upload.py",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ"),
        "matte": matte,
        "uploaded": [
            {"file": name, "content_id": cid} for name, cid in uploaded
        ],
    }


def upload_items(art, paths: list[Path], matte: str, log=print) -> tuple[list, list]:
    """Upload each path; one failure never aborts the pass.

    Returns (uploaded, failed): uploaded as (filename, content_id) pairs,
    failed as (filename, error) pairs.
    """
    uploaded, failed = [], []
    for i, path in enumerate(paths, 1):
        try:
            cid = art.upload(str(path), matte=matte, portrait_matte=matte)
            uploaded.append((path.name, cid))
        except Exception as e:  # per-item: keep going, report at the end
            failed.append((path.name, f"{type(e).__name__}: {e}"))
        if i % 10 == 0 or i == len(paths):
            log(f"  [{i}/{len(paths)}] uploaded {len(uploaded)}, failed {len(failed)}")
    return uploaded, failed


# ---------------------------------------------------------------------------
# Connected flow
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ip", required=True, help="Frame TV IP address")
    parser.add_argument(
        "--folder", required=True,
        help="Folder of processed images to upload (a batch_build.py output)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually upload. Without this, dry run: report only.",
    )
    parser.add_argument(
        "--matte", default="none",
        help="Matte to set on each uploaded image (default: none)",
    )
    args = parser.parse_args()

    paths = select_images(args.folder)
    if not paths:
        print(f"No images found in {args.folder}.", file=sys.stderr)
        return 2

    print(f"{len(paths)} image(s) in {args.folder} ({total_megabytes(paths)} MB), "
          f"matte on upload: '{args.matte}'.")

    if not args.apply:
        print(f"\nDry run -- nothing uploaded. Run with --apply to upload "
              f"{len(paths)} image(s).")
        print("Note: the TV does not deduplicate -- upload into a cleared "
              "library (tv_delete.py) or expect duplicates.")
        return 0

    try:
        tv = connect(args.ip)
    except ImportError:
        print("samsungtvws is not installed.  pip install samsungtvws", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Could not connect to the TV: {type(e).__name__}: {e}", file=sys.stderr)
        print(PRECONDITIONS_HINT, file=sys.stderr)
        return 1

    art = tv.art()
    uploaded, failed = upload_items(art, paths, args.matte)

    if uploaded:
        receipt = build_receipt(uploaded, args.matte)
        receipt_path = Path(f"uploaded_artworks_{receipt['created']}.json")
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(f"\nUpload receipt written: {receipt_path}")

    for name, err in failed:
        print(f"  FAIL {name}: {err}")

    print(f"\nUploaded {len(uploaded)}, failed {len(failed)}, of {len(paths)}.")
    print("Remember the TV-side finish: start the slideshow with shuffle ON "
          "(firmware often resets it).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
