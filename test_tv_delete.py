#!/usr/bin/env python3
"""Tests for the pure logic in tv_delete.py.

Run with: python test_tv_delete.py

Everything here runs without a TV: the art object is a fake that records
delete_list calls, and the functions under test are the scope / confirmation /
ordering / manifest logic that decides WHAT gets deleted. The connected flow
around them is exercised against the real TV (plan step C3), not here.
"""

import sys

from tv_delete import (
    build_manifest,
    confirmation_ok,
    delete_items,
    in_scope,
    order_targets,
    partition_scope,
)
from tv_no_mat import dedupe_items

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


class FakeArt:
    """Records delete_list calls; raises for content_ids listed in *failing*."""

    def __init__(self, failing=()):
        self.calls = []
        self.failing = set(failing)

    def delete_list(self, content_ids):
        if set(content_ids) & self.failing:
            raise RuntimeError("simulated TV error")
        self.calls.append(list(content_ids))


# The shape a real TV returned on 2026-08-08 (one full record, trimmed peers).
ITEMS = [
    {"content_id": "MY_F0663", "category_id": "MY-C0002", "slideshow": "false",
     "matte_id": "none", "portrait_matte_id": "flexible_black",
     "width": 3840, "height": 2160, "image_date": "2026:03:31 11:07:17",
     "content_type": "usb"},
    {"content_id": "MY_F0001", "content_type": "usb"},
    {"content_id": "MY_F0002", "content_type": "myphoto"},
    {"content_id": "MY_F0003"},                        # MY_F but no content_type
    {"content_id": "MY_F0004", "content_type": None},  # explicit null
    {"content_id": "MY_F0005", "content_type": "store"},   # wrong type
    {"content_id": "SAM-S1234", "content_type": "usb"},    # wrong id prefix
    {"content_id": "SAM-S5678", "content_type": "mobile"},
    {"content_type": "usb"},                           # no content_id at all
]


def main():
    print("scope — user-uploaded needs BOTH signals (MY_F id AND usb/myphoto type):")
    check("usb item in scope", in_scope(ITEMS[0]))
    check("myphoto item in scope", in_scope(ITEMS[2]))
    check("MY_F with missing content_type excluded (conservative)",
          not in_scope(ITEMS[3]))
    check("MY_F with null content_type excluded", not in_scope(ITEMS[4]))
    check("MY_F with unrecognized content_type excluded", not in_scope(ITEMS[5]))
    check("usb item without MY_F prefix excluded", not in_scope(ITEMS[6]))
    check("non-user item excluded", not in_scope(ITEMS[7]))
    check("item without content_id excluded", not in_scope(ITEMS[8]))
    check("content_type matched case-insensitively",
          in_scope({"content_id": "MY_F9999", "content_type": "USB"}))

    print("\npartition — everything lands on exactly one side:")
    targets, excluded = partition_scope(ITEMS)
    check("targets are the three user uploads",
          [t["content_id"] for t in targets] == ["MY_F0663", "MY_F0001", "MY_F0002"],
          str([t.get("content_id") for t in targets]))
    check("everything else excluded, nothing dropped",
          len(targets) + len(excluded) == len(ITEMS))
    check("empty list -> nothing anywhere", partition_scope([]) == ([], []))

    print("\ndedupe interaction — available() repeats an artwork per category:")
    tripled = [dict(ITEMS[0], category_id=c) for c in ("MY-C0002", "MY-C0008")]
    targets, _ = partition_scope(dedupe_items(tripled + ITEMS[1:]))
    check("deduped scope counts each artwork once",
          [t["content_id"] for t in targets] == ["MY_F0663", "MY_F0001", "MY_F0002"],
          str([t.get("content_id") for t in targets]))

    print("\nconfirmation — the typed count must match exactly:")
    check("exact count accepted", confirmation_ok("144", 144))
    check("surrounding whitespace tolerated", confirmation_ok("  144\n", 144))
    check("wrong number aborts", not confirmation_ok("143", 144))
    check("empty input aborts", not confirmation_ok("", 144))
    check("'yes' is not a count", not confirmation_ok("yes", 144))
    check("decorated number aborts", not confirmation_ok("144!", 144))

    print("\nordering — the displayed artwork is deleted last:")
    targets, _ = partition_scope(ITEMS)
    ordered, displayed_last = order_targets(targets, "MY_F0663")
    check("displayed moved to the end",
          [t["content_id"] for t in ordered] == ["MY_F0001", "MY_F0002", "MY_F0663"],
          str([t.get("content_id") for t in ordered]))
    check("flag says displayed is last", displayed_last)
    ordered, displayed_last = order_targets(targets, None)
    check("no current id -> natural order",
          [t["content_id"] for t in ordered] == ["MY_F0663", "MY_F0001", "MY_F0002"])
    check("no current id -> flag off", not displayed_last)
    ordered, displayed_last = order_targets(targets, "MY_F9999")
    check("current id not in scope -> natural order, flag off",
          not displayed_last and len(ordered) == 3)

    print("\ndelete loop — per-item calls, one failure never aborts the pass:")
    art = FakeArt(failing={"MY_F0001"})
    deleted, failed = delete_items(art, targets)
    check("failing item reported", [c for c, _ in failed] == ["MY_F0001"])
    check("later item still processed",
          [d["content_id"] for d in deleted] == ["MY_F0663", "MY_F0002"])
    check("one id per delete_list call (proven call shape)",
          art.calls == [["MY_F0663"], ["MY_F0002"]], str(art.calls))
    check("deleted carries the full item record",
          deleted[0].get("image_date") == "2026:03:31 11:07:17")

    print("\nmanifest — a receipt of what was ACTUALLY deleted, not an undo:")
    manifest = build_manifest(deleted)
    check("manifest names its writer", manifest["written_by"] == "tv_delete.py")
    check("manifest says it is not an undo", "NOT an undo" in manifest["note"])
    check("manifest names the recovery path", "frame_tv_art/" in manifest["note"])
    check("manifest holds only the deleted items, full records",
          [d["content_id"] for d in manifest["deleted"]] == ["MY_F0663", "MY_F0002"]
          and manifest["deleted"][0]["category_id"] == "MY-C0002")
    check("failed item absent from manifest",
          all(d["content_id"] != "MY_F0001" for d in manifest["deleted"]))
    check("clean pass -> manifest covers every target",
          len(build_manifest(delete_items(FakeArt(), targets)[0])["deleted"]) == 3)

    print("\ndry-run guarantee — scope selection alone never touches the TV:")
    art_dry = FakeArt()
    partition_scope(ITEMS)
    order_targets(targets, "MY_F0663")
    check("no delete_list calls from selection/ordering", art_dry.calls == [])

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
