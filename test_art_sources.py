#!/usr/bin/env python3
"""Tests for the pure URL logic in art_sources.py.

Run with: python test_art_sources.py

Covers the AIC IIIF handling added 2026-08-08: the 403 size-fallback ladder
and the cache-filename derivation (every AIC URL ends in /default.jpg, so the
generic last-segment name would collide ALL AIC artworks onto one cache file).
No network — download_image's retry loop is exercised live, not here.
"""

import sys

from art_sources import aic_cache_filename, aic_iiif_fallback_urls

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


AIC = "https://www.artic.edu/iiif/2/a38e2828-ec6f-ece1-a30f-70243449197b/full/3840,/0/default.jpg"


def main():
    print("fallback ladder — AIC URLs get smaller sizes, others get nothing:")
    ladder = aic_iiif_fallback_urls(AIC)
    check("two rungs", len(ladder) == 2, str(ladder))
    check("first rung is 1686", "/full/1686,/0/default.jpg" in ladder[0], ladder[0])
    check("second rung is native full", "/full/full/0/default.jpg" in ladder[1], ladder[1])
    check("image id preserved in every rung",
          all("a38e2828-ec6f-ece1-a30f-70243449197b" in u for u in ladder))
    check("Met URL -> no ladder",
          aic_iiif_fallback_urls("https://images.metmuseum.org/CRDImages/ep/original/DT2165.jpg") == [])
    check("Wikimedia URL -> no ladder",
          aic_iiif_fallback_urls("https://upload.wikimedia.org/wikipedia/commons/a/ab/X.jpg") == [])
    check("non-IIIF artic URL -> no ladder",
          aic_iiif_fallback_urls("https://www.artic.edu/artworks/123") == [])

    print("\nmajor-artists list — the 2026-08-08 skip-log additions match:")
    from art_sources import is_major_artist
    for artist in ("Eugène Boudin", "Henri Fantin-Latour", "George Inness",
                   "Theodore Robinson", "James Tissot",
                   "Pierre Puvis de Chavannes", "Ernest Meissonier",
                   "Goya (Francisco de Goya y Lucientes)"):
        check(f"{artist} is major", is_major_artist(artist))
    check("bare 'Robinson' does NOT match (common surname)",
          not is_major_artist("Boardman Robinson"))
    check("stock-scan junk still excluded", not is_major_artist("Rawpixel Ltd"))

    print("\ncache filename — AIC names by image_id, everything else untouched:")
    check("AIC filename carries the image id",
          aic_cache_filename(AIC) == "aic_a38e2828-ec6f-ece1-a30f-70243449197b.jpg",
          str(aic_cache_filename(AIC)))
    check("two AIC artworks never share a filename",
          aic_cache_filename(AIC)
          != aic_cache_filename(AIC.replace("a38e2828", "2d484387")))
    check("Met URL -> None (generic naming applies)",
          aic_cache_filename("https://images.metmuseum.org/CRDImages/ep/original/DT2165.jpg") is None)

    print("\noutbound identity — every request identifies us, contact is reachable:")
    import re
    from pathlib import Path
    from art_sources import REPO_URL, _art_session, _wiki_session

    check("REPO_URL is the real repo (owner segment present)",
          REPO_URL == "https://github.com/n-pillai/frame-art-server", REPO_URL)
    for label, sess in (("wiki", _wiki_session), ("art", _art_session)):
        ua = sess.headers.get("User-Agent", "")
        check(f"{label} session sends a descriptive User-Agent",
              ua.startswith("FrameArtServer/"), ua)
        check(f"{label} session UA carries the reachable repo URL", REPO_URL in ua, ua)
        check(f"{label} session UA has no email address", "@" not in ua, ua)
    check("art session sends AIC's documented header",
          REPO_URL in _art_session.headers.get("AIC-User-Agent", ""),
          _art_session.headers.get("AIC-User-Agent", ""))
    check("wiki session does NOT send the AIC header",
          "AIC-User-Agent" not in _wiki_session.headers)

    # The 2026-08-08 AIC incident was on the download path, so the download path
    # was what got an identifying session. Search and metadata calls stayed
    # anonymous against the same WAF. This guards the whole module, not one path.
    source = Path(__file__).with_name("art_sources.py").read_text(encoding="utf-8")
    bare = [line.strip() for line in source.splitlines()
            if re.search(r"\brequests\.get\s*\(", line)]
    check("no bare requests.get anywhere in art_sources.py",
          not bare, f"{len(bare)} found: {bare[:3]}")

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
