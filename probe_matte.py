#!/usr/bin/env python3
"""Probe whether the Frame TV's per-image mat can be set over the network.

WHY THIS EXISTS
---------------
frame-art-server can only control the *software* mat it draws into the image
file (config.yaml -> display.aspect_mode). The mat you currently clear by hand,
image by image, is a TV-side per-artwork setting the tool has never touched --
all TV-facing code was removed in the April 2026 batch-build refactor.

The samsungtvws library exposes `change_matte(content_id, matte_id)`, and its
source falls back to the literal string "none". On naming grounds its values
look like mount borders (shadowbox, panoramic, triptych, plus a colour palette)
rather than a surface finish -- but that is inference, not proof. Only your TV
can answer it.

This script does NOT commit to the architecture change. It answers one question:
can the mat be cleared by script, on your TV, on your firmware?

SAFETY
------
Read-only unless you pass --apply. It never uploads and never deletes. With
--apply it changes the mat on exactly ONE artwork you name, and prints how to
put it back.

USAGE
-----
    pip install samsungtvws

    # 1. Look only. Reports what the TV offers and what your art currently has.
    python probe_matte.py --ip 192.168.1.42

    # 2. Change one image, then LOOK AT THE TV.
    python probe_matte.py --ip 192.168.1.42 --apply <content_id>

    # 3. Put it back if you want.
    python probe_matte.py --ip 192.168.1.42 --apply <content_id> --matte shadowbox_polar

Find the TV's IP: TV Menu > Settings > General > Network > Network Status > IP
Settings. The TV must be ON (or in Art Mode) and on the same network as this
machine. First connection may raise a prompt on the TV asking you to allow it --
accept it there, then re-run.
"""

from __future__ import annotations

import argparse
import json
import sys

# Pairing, token storage, and the samsungtvws token-logging pin all live in
# tv_session.py, shared with tv_no_mat.py -- see that module for the rationale.
from tv_session import connect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ip", required=True, help="Frame TV IP address")
    parser.add_argument(
        "--apply",
        metavar="CONTENT_ID",
        help="Change the mat on this one artwork. Omit to run read-only.",
    )
    parser.add_argument(
        "--matte",
        default="none",
        help="Matte value to set with --apply (default: none, i.e. no mat)",
    )
    args = parser.parse_args()

    try:
        tv = connect(args.ip)
    except ImportError:
        print("samsungtvws is not installed.  pip install samsungtvws", file=sys.stderr)
        return 2

    art = tv.art()

    # ---- 1. Is this even a Frame? -----------------------------------------
    print("== art mode support ==")
    try:
        supported = art.supported()
    except Exception as e:
        print(f"  could not reach the TV: {type(e).__name__}: {e}")
        print("  check the IP, that the TV is on, and that both are on the same network.")
        return 1
    print(f"  supported: {supported}")
    if not supported:
        print("  This TV does not report Art Mode support. Stopping.")
        return 1
    try:
        print(f"  api version: {art.get_api_version()}")
    except Exception as e:
        print(f"  api version unavailable ({type(e).__name__})")

    # ---- 2. THE KEY QUESTION: what does the TV mean by 'matte'? -----------
    # If this returns mount styles with a colour palette, 'matte' is the mat.
    # If it returns something finish-like, or nothing, the premise is wrong.
    print("\n== what the TV offers as 'matte' ==")
    try:
        matte_list = art.get_matte_list()
        print(json.dumps(matte_list, indent=2)[:1500])
        types = matte_list.get("matte_types") or []
        names = [t.get("matte_type", t) if isinstance(t, dict) else t for t in types]
        print(f"\n  -> {len(names)} type(s): {names}")
        print(f"  -> is 'none' offered? {'none' in [str(n).lower() for n in names]}")
    except Exception as e:
        print(f"  get_matte_list failed: {type(e).__name__}: {e}")

    # ---- 3. What is on the TV, and what mat does it have now? -------------
    print("\n== artwork currently on the TV ==")
    try:
        items = art.available() or []
    except Exception as e:
        print(f"  available() failed: {type(e).__name__}: {e}")
        return 1

    print(f"  {len(items)} item(s)")
    for item in items[:10]:
        cid = item.get("content_id", "?")
        print(
            f"    {cid:24} matte={item.get('matte_id', '-'):20} "
            f"category={item.get('category_id', '-')}"
        )
    if len(items) > 10:
        print(f"    ... and {len(items) - 10} more")

    mattes = {str(i.get("matte_id")) for i in items}
    print(f"\n  distinct matte_id values in use: {sorted(mattes)}")
    print("  ^ if these match what the TV menu shows per image, they are the same setting")

    # ---- 4. Optional: change exactly one -----------------------------------
    if not args.apply:
        print("\nRead-only run. To test a change, pick a content_id above and run:")
        print(f"  python probe_matte.py --ip {args.ip} --apply <content_id>")
        print("\nTHEN LOOK AT THE TV. That is the actual test -- the API returning")
        print("success is not proof the border disappeared.")
        return 0

    target = next((i for i in items if i.get("content_id") == args.apply), None)
    if target is None:
        print(f"\n{args.apply} is not on the TV. Pick a content_id from the list above.")
        return 1

    before = target.get("matte_id")
    print(f"\n== changing {args.apply} ==")
    print(f"  before: matte_id={before}")
    try:
        art.change_matte(args.apply, args.matte)
    except Exception as e:
        print(f"  change_matte failed: {type(e).__name__}: {e}")
        return 1

    after = next(
        (i.get("matte_id") for i in (art.available() or [])
         if i.get("content_id") == args.apply),
        "?",
    )
    print(f"  after:  matte_id={after}")
    print(f"  API reports change: {before != after}")

    print("\n>> NOW LOOK AT THE TV. Select that artwork in Art Mode.")
    print("   Did the border actually disappear?")
    print("   The API reporting a new matte_id is NOT proof the display changed.")
    print(f"\n   To undo: python probe_matte.py --ip {args.ip} "
          f"--apply {args.apply} --matte {before or 'shadowbox_polar'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
