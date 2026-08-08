#!/usr/bin/env python3
"""Set every artwork on the Frame TV to "no mat" in one command.

After a USB import the TV shows a mat border on each new image, and its own
menu has no global "no mat" setting -- clearing them means clicking through
every image by hand. This script does that pass over the network instead.
Proven end-to-end on a real TV on 2026-08-07 (probe_matte.py cleared 20/20).

SAFETY
------
Read-only by default: without --apply it only reports what would change.
Every --apply run writes an undo file (no_mat_undo_<timestamp>.json, kept out
of git) mapping each changed artwork to its previous mat, and --restore
replays that file. It never uploads and never deletes.

The script discovers what YOUR TV offers via get_matte_list() and verifies
"none" is among the options before touching anything -- matte options differ
across Frame models and firmware. If the art channel is unreachable or "none"
is not offered, it fails loud and points at the manual path; it never guesses.

USAGE
-----
    pip install samsungtvws

    # 1. Dry run: report what is on the TV and what would change.
    python tv_no_mat.py --ip 192.168.1.42

    # 2. Clear the mat on everything that has one.
    python tv_no_mat.py --ip 192.168.1.42 --apply

    # 3. Put things back the way they were.
    python tv_no_mat.py --ip 192.168.1.42 --restore no_mat_undo_20260808T120000Z.json

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

from tv_session import connect

NO_MATTE = "none"

PRECONDITIONS_HINT = """\
Could not reach the TV's art channel. Check, in order:
  1. The TV is in Art Mode or fully ON (standby -> ms.channel.timeOut).
  2. The IP is right and this machine is on the same network.
  3. Pairing: first contact shows an Allow prompt on the TV -- accept it.
     ms.channel.clientDisconnect with token 'None' means not paired.
Details: docs/solutions/integration-issues/
frame-tv-art-channel-pairing-and-matte-api-2026-08-07.md
Manual fallback: TV menu > Art Mode > select each image > set mat to None."""


# ---------------------------------------------------------------------------
# Pure logic -- everything below is testable without a TV (test_tv_no_mat.py)
# ---------------------------------------------------------------------------

def matte_type_names(matte_list) -> list[str]:
    """Extract the offered matte type names from a get_matte_list() response.

    The TV returns a dict whose "matte_types" entries are usually dicts with a
    "matte_type" key, but plain strings have been seen too -- accept both.
    """
    if not isinstance(matte_list, dict):
        return []
    types = matte_list.get("matte_types") or []
    names = []
    for t in types:
        name = t.get("matte_type") if isinstance(t, dict) else t
        if name:
            names.append(str(name))
    return names


def none_offered(matte_list) -> bool:
    """Does this TV offer "none" as a matte option? Never assume it does."""
    return NO_MATTE in (n.lower() for n in matte_type_names(matte_list))


def dedupe_items(items: list[dict]) -> list[dict]:
    """Collapse available() to one entry per content_id, first occurrence wins.

    Observed on a real TV (2026-08-08): available() lists an artwork once per
    category it appears in, so the same content_id can come back several
    times. Without this, counts are inflated, --apply calls change_matte
    repeatedly on the same artwork, and the undo file gets duplicate entries.
    """
    seen, out = set(), []
    for item in items:
        cid = item.get("content_id")
        if cid in seen:
            continue
        seen.add(cid)
        out.append(item)
    return out


def items_needing_change(items: list[dict]) -> list[dict]:
    """The artworks whose mat would be cleared by an --apply run.

    An item needs changing when it carries a real matte_id other than "none".
    Items with no matte_id at all are left alone -- absence means the setting
    does not apply to them, not that they have a mat to clear.
    """
    needing = []
    for item in items:
        matte = item.get("matte_id")
        if matte and str(matte).lower() != NO_MATTE:
            needing.append(item)
    return needing


def build_undo(changed: list[tuple[str, str]]) -> dict:
    """Undo-file payload for a set of (content_id, previous_matte_id) changes."""
    return {
        "written_by": "tv_no_mat.py",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ"),
        "changes": [
            {"content_id": cid, "previous_matte": prev} for cid, prev in changed
        ],
    }


def apply_matte(art, targets: list[dict], matte_for) -> tuple[list, list]:
    """Run change_matte over *targets*; one failure never aborts the loop.

    *matte_for* maps an item dict to the matte value to set, so the same loop
    serves both the clearing pass (always "none") and --restore (per-item
    previous value). Returns (changed, failed): changed as
    (content_id, previous_matte_id) pairs, failed as (content_id, error).
    """
    changed, failed = [], []
    for item in targets:
        cid = item.get("content_id")
        try:
            art.change_matte(cid, matte_for(item))
            changed.append((cid, item.get("matte_id")))
        except Exception as e:  # per-item: keep going, report at the end
            failed.append((cid, f"{type(e).__name__}: {e}"))
    return changed, failed


def restore_targets(undo: dict, on_tv: list[dict]) -> tuple[list[dict], list[str]]:
    """Resolve an undo file against what is currently on the TV.

    Returns (targets, missing): targets are item dicts carrying the matte to
    restore under "_restore_matte"; missing are content_ids from the undo file
    no longer present on the TV (reported, not an error -- art gets deleted).
    """
    by_id = {i.get("content_id"): i for i in on_tv}
    targets, missing = [], []
    for entry in undo.get("changes", []):
        cid = entry.get("content_id")
        if cid in by_id:
            item = dict(by_id[cid])
            item["_restore_matte"] = entry.get("previous_matte")
            targets.append(item)
        else:
            missing.append(cid)
    return targets, missing


# ---------------------------------------------------------------------------
# Connected flow
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ip", required=True, help="Frame TV IP address")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually clear the mats. Without this, dry run: report only.",
    )
    parser.add_argument(
        "--restore",
        metavar="UNDO_FILE",
        help="Replay an undo file written by a previous --apply run.",
    )
    args = parser.parse_args()

    if args.apply and args.restore:
        print("--apply and --restore are mutually exclusive.", file=sys.stderr)
        return 2

    undo_to_replay = None
    if args.restore:
        try:
            undo_to_replay = json.loads(Path(args.restore).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"Could not read undo file {args.restore}: {e}", file=sys.stderr)
            return 2

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

    # -- Capability discovery: never assume this TV's option set. ------------
    try:
        matte_list = art.get_matte_list()
        items = dedupe_items(art.available() or [])
    except Exception as e:
        print(f"Art channel call failed: {type(e).__name__}: {e}", file=sys.stderr)
        print(PRECONDITIONS_HINT, file=sys.stderr)
        return 1

    if not args.restore and not none_offered(matte_list):
        offered = matte_type_names(matte_list)
        print(f"This TV does not offer '{NO_MATTE}' as a matte option.", file=sys.stderr)
        print(f"It offers: {offered}", file=sys.stderr)
        print("Stopping without changing anything. Use the manual TV menu path.",
              file=sys.stderr)
        return 1

    # -- Restore mode. -------------------------------------------------------
    if undo_to_replay is not None:
        targets, missing = restore_targets(undo_to_replay, items)
        for cid in missing:
            print(f"  skip {cid}: no longer on the TV")
        changed, failed = apply_matte(
            art, targets, lambda i: i.get("_restore_matte") or NO_MATTE
        )
        for cid, err in failed:
            print(f"  FAIL {cid}: {err}")
        print(f"\nRestored {len(changed)}, failed {len(failed)}, "
              f"missing {len(missing)} (of {len(undo_to_replay.get('changes', []))}).")
        return 1 if failed else 0

    # -- Report what is there and what would change. -------------------------
    needing = items_needing_change(items)
    already = len(items) - len(needing)
    print(f"{len(items)} artwork(s) on the TV; {already} already have no mat, "
          f"{len(needing)} would be set to '{NO_MATTE}'.")
    for item in needing:
        print(f"  {item.get('content_id', '?'):24} matte={item.get('matte_id')}")

    if not args.apply:
        if needing:
            print(f"\nDry run -- nothing changed. Run with --apply to clear "
                  f"{len(needing)} mat(s).")
        else:
            print("\nNothing to do.")
        return 0

    if not needing:
        print("\nNothing to do.")
        return 0

    # -- Apply, with an undo file written first-class. -----------------------
    changed, failed = apply_matte(art, needing, lambda i: NO_MATTE)

    if changed:
        undo = build_undo(changed)
        undo_path = Path(f"no_mat_undo_{undo['created']}.json")
        undo_path.write_text(json.dumps(undo, indent=2), encoding="utf-8")
        print(f"\nUndo file written: {undo_path}")
        print(f"  to revert: python tv_no_mat.py --ip {args.ip} --restore {undo_path}")

    for cid, err in failed:
        print(f"  FAIL {cid}: {err}")

    print(f"\nChanged {len(changed)}, failed {len(failed)}, "
          f"already-none {already}, total {len(items)}.")
    print("The API reporting success is not proof -- glance at the TV.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
