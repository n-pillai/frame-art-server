#!/usr/bin/env python3
"""Delete every user-uploaded artwork from the Frame TV in one command.

The front half of a batch refresh: delete the old batch, USB-import the new
one, then run tv_no_mat.py. Today, clearing out an old batch means deleting
images one at a time in the TV menu -- this script does that pass over the
network instead. The delete call was verified on a real TV on 2026-08-08
(one sacrificial artwork, gone from available() and the screen).

SAFETY
------
Read-only by default: without --apply it only reports what would be deleted.

Deletion is IRREVERSIBLE on the TV -- there is no undo file this time, and
that is not an oversight. Recovery is re-importing from the local
frame_tv_art/ output via USB (the images come back with new content ids).
Because there is no undo, --apply adds a gate tv_no_mat.py does not have: it
prompts for a typed confirmation of the exact count, and anything else aborts
with nothing deleted. --yes bypasses the prompt for scripted use.

Scope: ONLY user-uploaded content is ever targeted -- content ids starting
with MY_F AND content_type "usb" or "myphoto" (both signals observed live,
belt and braces). Anything else, including an MY_F item with no content_type,
is excluded and reported, never deleted.

Every applied run writes a deletion manifest
(deleted_artworks_<timestamp>.json, kept out of git) with the full item
records of everything actually deleted. The manifest is a receipt, not an
undo.

USAGE
-----
    pip install samsungtvws

    # 1. Dry run: list what would be deleted. Nothing is touched.
    python tv_delete.py --ip 192.168.1.42

    # 2. Delete, after typing the exact count at the prompt.
    python tv_delete.py --ip 192.168.1.42 --apply

    # 3. Scripted use only: skip the prompt.
    python tv_delete.py --ip 192.168.1.42 --apply --yes

PRECONDITIONS (see docs/solutions/integration-issues/
frame-tv-art-channel-pairing-and-matte-api-2026-08-07.md)
- The TV must be in Art Mode or fully on. In standby the art app is not
  running and every call fails with ms.channel.timeOut.
- First run pairs over the remote channel: accept the Allow prompt on the TV.
  ms.channel.clientDisconnect with token 'None' means pairing has not
  happened -- the art channel alone can never issue the token.

Find the TV's IP: TV Menu > Settings > General > Network > Network Status.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from tv_no_mat import dedupe_items
from tv_session import connect

USER_ID_PREFIX = "MY_F"
# "usb" and "myphoto" observed at the C1 probe (2026-08-08); "mobile" is what
# the TV stamps on network uploads (tv_upload.py) -- verified live the same
# day. Without it, a network-uploaded batch would be invisible to this pass.
USER_CONTENT_TYPES = {"usb", "myphoto", "mobile"}

PRECONDITIONS_HINT = """\
Could not reach the TV's art channel. Check, in order:
  1. The TV is in Art Mode or fully ON (standby -> ms.channel.timeOut).
  2. The IP is right and this machine is on the same network.
  3. Pairing: first contact shows an Allow prompt on the TV -- accept it.
     ms.channel.clientDisconnect with token 'None' means not paired.
Details: docs/solutions/integration-issues/
frame-tv-art-channel-pairing-and-matte-api-2026-08-07.md
Manual fallback: TV menu > Art Mode > select each image > delete."""

RECOVERY_HINT = (
    "Recovery path: re-import from the local frame_tv_art/ output via USB\n"
    "(the images come back with new content ids)."
)


# ---------------------------------------------------------------------------
# Pure logic -- everything below is testable without a TV (test_tv_delete.py)
# ---------------------------------------------------------------------------

def in_scope(item: dict) -> bool:
    """Is *item* user-uploaded content this script is allowed to delete?

    Belt and braces, both verified live on a real TV (2026-08-08): the
    content_id must start with MY_F AND the content_type must be "usb" or
    "myphoto". An MY_F item with a missing content_type fails the check --
    conservative: when one of the two signals is absent, exclude and report
    rather than trust the other alone.
    """
    cid = item.get("content_id")
    if not isinstance(cid, str) or not cid.startswith(USER_ID_PREFIX):
        return False
    ctype = item.get("content_type")
    return str(ctype).lower() in USER_CONTENT_TYPES if ctype else False


def partition_scope(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split *items* into (targets, excluded) by the scope filter."""
    targets, excluded = [], []
    for item in items:
        (targets if in_scope(item) else excluded).append(item)
    return targets, excluded


def confirmation_ok(reply: str, count: int) -> bool:
    """Did the operator type the exact target count? Anything else aborts."""
    return reply.strip() == str(count)


def order_targets(targets: list[dict], current_id) -> tuple[list[dict], bool]:
    """Order *targets* so the currently-displayed artwork is deleted last.

    Deleting what is on screen mid-pass is the one moment the TV might react
    badly, so it goes last: if that single delete misbehaves, everything else
    is already done. Returns (ordered, displayed_is_last) -- the flag is True
    only when *current_id* is actually among the targets.
    """
    if not current_id:
        return list(targets), False
    rest = [t for t in targets if t.get("content_id") != current_id]
    displayed = [t for t in targets if t.get("content_id") == current_id]
    return rest + displayed, bool(displayed)


def build_manifest(deleted: list[dict]) -> dict:
    """Deletion-manifest payload: full item records, labeled a receipt.

    Deliberately NOT shaped like tv_no_mat.py's undo file -- there is nothing
    to replay. The note field says so inside the file itself, so the manifest
    read months later does not get mistaken for a restore path.
    """
    return {
        "written_by": "tv_delete.py",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ"),
        "note": (
            "Receipt of deleted artworks -- NOT an undo. Deletion on the TV "
            "is irreversible; recover by re-importing from the local "
            "frame_tv_art/ output via USB (new content ids)."
        ),
        "deleted": [dict(item) for item in deleted],
    }


def delete_items(art, targets: list[dict]) -> tuple[list[dict], list]:
    """Delete *targets* one at a time; one failure never aborts the pass.

    Per-item delete_list([cid]) is the call shape proven live on 2026-08-08.
    One id per call keeps errors granular: a failed item is known by name and
    the loop moves on. Returns (deleted, failed): deleted as the full item
    dicts (the manifest wants the whole record), failed as (content_id, error).
    """
    deleted, failed = [], []
    for item in targets:
        cid = item.get("content_id")
        try:
            art.delete_list([cid])
            deleted.append(item)
        except Exception as e:  # per-item: keep going, report at the end
            failed.append((cid, f"{type(e).__name__}: {e}"))
    return deleted, failed


# ---------------------------------------------------------------------------
# Connected flow
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ip", required=True, help="Frame TV IP address")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this, dry run: report only.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the typed-count confirmation (scripted use only).",
    )
    args = parser.parse_args()

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

    try:
        items = dedupe_items(art.available() or [])
    except Exception as e:
        print(f"Art channel call failed: {type(e).__name__}: {e}", file=sys.stderr)
        print(PRECONDITIONS_HINT, file=sys.stderr)
        return 1

    # -- Scope: report both sides before doing anything. ---------------------
    targets, excluded = partition_scope(items)

    print(f"{len(items)} artwork(s) on the TV; {len(targets)} in scope "
          f"(user-uploaded), {len(excluded)} excluded.")
    for item in targets:
        print(f"  {item.get('content_id', '?'):24} "
              f"content_type={item.get('content_type')}")
    for item in excluded:
        print(f"  excluded (out of scope): {item.get('content_id', '?')} "
              f"(content_type={item.get('content_type')})")

    if not args.apply:
        if targets:
            print(f"\nDry run -- nothing deleted. Run with --apply to delete "
                  f"{len(targets)} artwork(s).")
            print("Deletion is irreversible: no undo file, only a manifest "
                  "(a receipt).")
            print(RECOVERY_HINT)
        else:
            print("\nNothing in scope to delete.")
        return 0

    if not targets:
        print("\nNothing in scope to delete.")
        return 0

    # -- The gate: irreversible, so a typed count, not just a flag. ----------
    if not args.yes:
        if not sys.stdin.isatty():
            print("stdin is not a terminal, so the typed-count confirmation "
                  "cannot be asked.", file=sys.stderr)
            print("Nothing deleted. Re-run interactively, or pass --yes for "
                  "scripted use.", file=sys.stderr)
            return 2
        reply = input(f"About to permanently delete {len(targets)} artworks. "
                      f"Type {len(targets)} to confirm: ")
        if not confirmation_ok(reply, len(targets)):
            print("Confirmation did not match the count. Nothing deleted.")
            print("(A count that changed since your dry run is itself a "
                  "warning sign -- re-check before retrying.)")
            return 2

    # -- Delete the displayed artwork last. ----------------------------------
    current_id = None
    try:
        current = art.get_current() or {}
        current_id = current.get("content_id")
    except Exception as e:
        print(f"note: get_current() failed ({type(e).__name__}: {e}) -- "
              f"proceeding in natural order.")

    ordered, displayed_last = order_targets(targets, current_id)
    if displayed_last:
        print(f"Deleting the currently-displayed artwork ({current_id}) last.")

    deleted, failed = delete_items(art, ordered)

    # -- Manifest: a receipt, not an undo. -----------------------------------
    if deleted:
        manifest = build_manifest(deleted)
        manifest_path = Path(f"deleted_artworks_{manifest['created']}.json")
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"\nDeletion manifest written: {manifest_path}")
        print("The manifest is a receipt of what was deleted -- it is NOT an "
              "undo; there is no undo.")
        print(RECOVERY_HINT)

    for cid, err in failed:
        print(f"  FAIL {cid}: {err}")

    print(f"\nDeleted {len(deleted)}, failed {len(failed)}, "
          f"excluded {len(excluded)}, total on TV {len(items)}.")
    print("The API reporting success is not proof -- glance at the TV.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
