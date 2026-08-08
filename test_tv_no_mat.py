#!/usr/bin/env python3
"""Tests for the pure logic in tv_no_mat.py.

Run with: python test_tv_no_mat.py

Everything here runs without a TV: the art object is a fake that records
calls, and the functions under test are the selection / capability / undo /
loop logic that decides WHAT to change. The connected flow around them is
exercised against the real TV (plan step B3), not here.
"""

import sys

from tv_no_mat import (
    NO_MATTE,
    apply_matte,
    build_undo,
    dedupe_items,
    items_needing_change,
    matte_type_names,
    none_offered,
    restore_targets,
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


class FakeArt:
    """Records change_matte calls; raises for content_ids listed in *failing*."""

    def __init__(self, failing=()):
        self.calls = []
        self.failing = set(failing)

    def change_matte(self, content_id, matte_id):
        if content_id in self.failing:
            raise RuntimeError("simulated TV error")
        self.calls.append((content_id, matte_id))


# The shape a real TV returned on 2026-08-07: dicts with matte_type keys.
REAL_SHAPE = {
    "matte_types": [
        {"matte_type": "none"},
        {"matte_type": "shadowbox"},
        {"matte_type": "panoramic"},
    ]
}

ITEMS = [
    {"content_id": "MY_F0001", "matte_id": "shadowbox_polar"},
    {"content_id": "MY_F0002", "matte_id": "none"},
    {"content_id": "MY_F0003", "matte_id": "flexible_apricot"},
    {"content_id": "MY_F0004"},                      # no matte field at all
    {"content_id": "MY_F0005", "matte_id": None},    # explicit null
]


def main():
    print("capability discovery — matte_type_names / none_offered:")
    check("dict-shaped types parsed",
          matte_type_names(REAL_SHAPE) == ["none", "shadowbox", "panoramic"])
    check("plain-string types parsed",
          matte_type_names({"matte_types": ["none", "modern"]}) == ["none", "modern"])
    check("'none' found in real shape", none_offered(REAL_SHAPE))
    check("'NONE' matched case-insensitively",
          none_offered({"matte_types": [{"matte_type": "NONE"}]}))
    check("no 'none' -> not offered",
          not none_offered({"matte_types": [{"matte_type": "shadowbox"}]}))
    check("empty response -> not offered", not none_offered({}))
    check("non-dict response -> not offered", not none_offered(None))

    print("\ndedupe — available() repeats an artwork per category (seen live 2026-08-08):")
    tripled = [dict(ITEMS[0], category_id=c) for c in ("MY-C0002", "MY-C0004", "MY-C0008")]
    deduped = dedupe_items(tripled + ITEMS[1:])
    check("one entry per content_id",
          [i["content_id"] for i in deduped]
          == ["MY_F0001", "MY_F0002", "MY_F0003", "MY_F0004", "MY_F0005"],
          str([i.get("content_id") for i in deduped]))
    check("first occurrence wins", deduped[0].get("category_id") == "MY-C0002")
    check("deduped selection counts one, not three",
          len(items_needing_change(deduped)) == 2)
    check("empty list ok", dedupe_items([]) == [])

    print("\nselection — items_needing_change:")
    needing = items_needing_change(ITEMS)
    check("only real non-none mats selected",
          [i["content_id"] for i in needing] == ["MY_F0001", "MY_F0003"],
          str([i.get("content_id") for i in needing]))
    check("already-none left alone",
          all(i.get("matte_id") != "none" for i in needing))
    check("missing/null matte_id left alone",
          all(i.get("content_id") not in ("MY_F0004", "MY_F0005") for i in needing))
    check("empty list -> nothing", items_needing_change([]) == [])

    print("\napply loop — one failure never aborts the pass:")
    art = FakeArt(failing={"MY_F0001"})
    changed, failed = apply_matte(art, needing, lambda i: NO_MATTE)
    check("failing item reported", [c for c, _ in failed] == ["MY_F0001"])
    check("later item still processed", [c for c, _ in changed] == ["MY_F0003"])
    check("previous matte captured for undo",
          changed[0][1] == "flexible_apricot", str(changed))
    check("TV asked to set 'none'", art.calls == [("MY_F0003", "none")])

    print("\nundo file — build_undo round-trips through restore_targets:")
    art_ok = FakeArt()
    changed, failed = apply_matte(art_ok, items_needing_change(ITEMS), lambda i: NO_MATTE)
    check("clean pass changes both", len(changed) == 2 and not failed)
    undo = build_undo(changed)
    check("undo names its writer", undo["written_by"] == "tv_no_mat.py")
    check("undo carries previous mats",
          {(c["content_id"], c["previous_matte"]) for c in undo["changes"]}
          == {("MY_F0001", "shadowbox_polar"), ("MY_F0003", "flexible_apricot")})

    # After the clearing pass those items are 'none' on the TV.
    on_tv_after = [dict(i, matte_id="none") if i.get("matte_id") not in (None, "")
                   else i for i in ITEMS]
    targets, missing = restore_targets(undo, on_tv_after)
    check("all undo entries found on TV", len(targets) == 2 and not missing)
    check("restore carries the previous mat",
          {t["_restore_matte"] for t in targets}
          == {"shadowbox_polar", "flexible_apricot"})

    art_restore = FakeArt()
    changed_r, failed_r = apply_matte(
        art_restore, targets, lambda i: i.get("_restore_matte") or NO_MATTE
    )
    check("restore sets the previous mats",
          set(art_restore.calls)
          == {("MY_F0001", "shadowbox_polar"), ("MY_F0003", "flexible_apricot")})
    check("restore reports no failures", not failed_r)

    print("\nundo vs deleted art — missing ids reported, not fatal:")
    targets, missing = restore_targets(undo, [{"content_id": "MY_F0003",
                                               "matte_id": "none"}])
    check("present id targeted", [t["content_id"] for t in targets] == ["MY_F0003"])
    check("deleted id listed as missing", missing == ["MY_F0001"])

    print("\ndry-run guarantee — selection alone never touches the TV:")
    art_dry = FakeArt()
    items_needing_change(ITEMS)
    check("no change_matte calls from selection", art_dry.calls == [])

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
