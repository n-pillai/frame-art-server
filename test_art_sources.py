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

    print("\ncache filename — AIC names by image_id, everything else untouched:")
    check("AIC filename carries the image id",
          aic_cache_filename(AIC) == "aic_a38e2828-ec6f-ece1-a30f-70243449197b.jpg",
          str(aic_cache_filename(AIC)))
    check("two AIC artworks never share a filename",
          aic_cache_filename(AIC)
          != aic_cache_filename(AIC.replace("a38e2828", "2d484387")))
    check("Met URL -> None (generic naming applies)",
          aic_cache_filename("https://images.metmuseum.org/CRDImages/ep/original/DT2165.jpg") is None)

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
