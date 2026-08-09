#!/usr/bin/env python3
"""Tests for the pure logic in tv_upload.py.

Run with: python test_tv_upload.py

No TV, no network: the art object is a fake recording upload calls. The
connected flow was proven live 2026-08-08 (single-image probe: content_id
returned, matte "none" applied, content_type "mobile").
"""

import sys
import tempfile
from pathlib import Path

from tv_upload import build_receipt, select_images, upload_items

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
    """Records upload calls; raises for filenames listed in *failing*."""

    def __init__(self, failing=()):
        self.calls = []
        self.failing = set(failing)
        self.counter = 0

    def upload(self, path, matte, portrait_matte):
        name = Path(path).name
        if name in self.failing:
            raise RuntimeError("simulated TV error")
        self.counter += 1
        self.calls.append((name, matte, portrait_matte))
        return f"MY_F{9000 + self.counter}"


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name in ("b.jpg", "a.jpg", "c.PNG", "d.jpeg"):
            (root / name).write_bytes(b"x" * 1024)
        (root / "notes.txt").write_text("not art")
        (root / "no_mat_undo_x.json").write_text("{}")
        (root / "sub").mkdir()

        print("selection — images only, sorted, junk ignored:")
        sel = select_images(str(root))
        check("four images found, sorted",
              [p.name for p in sel] == ["a.jpg", "b.jpg", "c.PNG", "d.jpeg"],
              str([p.name for p in sel]))
        check("non-images and subdirs ignored",
              all(p.suffix.lower() in {".jpg", ".jpeg", ".png"} for p in sel))
        check("missing folder -> empty", select_images(str(root / "nope")) == [])

        print("\nupload loop — one failure never aborts the pass:")
        art = FakeArt(failing={"b.jpg"})
        uploaded, failed = upload_items(art, sel, "none", log=lambda *_: None)
        check("failing item reported", [n for n, _ in failed] == ["b.jpg"])
        check("later items still processed",
              [n for n, _ in uploaded] == ["a.jpg", "c.PNG", "d.jpeg"])
        check("matte passed for both orientations",
              all(m == "none" and pm == "none" for _, m, pm in art.calls))
        check("content ids captured",
              all(cid.startswith("MY_F") for _, cid in uploaded))

        print("\nreceipt — filename -> content_id for everything uploaded:")
        receipt = build_receipt(uploaded, "none")
        check("receipt names its writer", receipt["written_by"] == "tv_upload.py")
        check("receipt records the matte", receipt["matte"] == "none")
        check("receipt covers only successes",
              [u["file"] for u in receipt["uploaded"]] == ["a.jpg", "c.PNG", "d.jpeg"])

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
