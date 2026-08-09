---
title: The AIC 403s were User-Agent filtering, and fixing them would have activated a second latent bug
date: 2026-08-08
category: integration-issues
module: art_sources
problem_type: integration_issue
root_cause: client_filtering_plus_latent_defect
resolution_type: code_fix
severity: high
tags: [aic, iiif, user-agent, 403, cache-collision, latent-bug, probe-first]
---

# The AIC 403s were UA filtering — and fixing them would have activated a second latent bug

## Problem

Every Art Institute of Chicago download failed with `403 Forbidden` on
`/iiif/2/<id>/full/3840,/0/default.jpg` — 17 of 17 in one themed batch — which had silently
zeroed AIC's contribution to every batch for an unknown period. The obvious diagnosis (a size
cap on the IIIF service) was wrong twice over.

## Root cause — found by refusing to stop at the first plausible story

1. **Not a size policy.** `info.json` itself returned 403 — a metadata endpoint has no size to
   cap, so the server was rejecting the *client*, not the request.
2. **Not fixed by a descriptive User-Agent alone.** Matrix-testing five header variants showed
   a descriptive UA still got 403; requests carrying the **`AIC-User-Agent` header their API
   docs ask consumers to send** (or a browser UA) got 200 — at the full 3840 width. The
   "honest, documented" header is the fix; masquerading as a browser was not needed.
3. **The fix would have detonated a second, worse bug.** Every AIC IIIF URL ends in
   `/default.jpg`, and the download cache named files by the URL's last segment — so with
   downloads succeeding, **all AIC artworks would have shared one cache file**: first image
   wins, every later artwork silently reuses the wrong picture under the wrong label. It had
   never manifested *only because* the downloads always failed. Cache files are now named by
   the IIIF `image_id`.

## Solution

`_art_session` (identifying UA + `AIC-User-Agent`, contact = repo URL, never an email) for all
non-Wikimedia downloads; `aic_cache_filename()` names AIC cache entries by image id; a size
fallback ladder (1686, native `full`) retained as defence in depth should AIC ever add a real
width cap. Verified live: the exact image that failed all batch downloads at 3840 wide.

## Prevention

- **A 403 on `info.json` means client filtering, not request policy** — matrix-test headers
  before redesigning the request.
- **Before shipping a fix that unblocks a dead code path, walk the newly-live path end to
  end** — a path that never executed is a path whose bugs have never been seen. The cache
  collision was found by reasoning through "what happens the first time this succeeds",
  before the first success.
- Wikimedia and AIC now both demonstrate the same rule: free museum APIs increasingly reject
  anonymous script clients; every fetcher should send an identifying session from day one.

## Follow-through (same day)

Writing that last rule down did not apply it. An audit prompted by an unrelated question — *what
identifying header do we actually send?* — found the fix had reached only the path that broke:

- **Six search/metadata calls were still anonymous** (`search_met`, `get_met_object`,
  `search_aic`, `search_cma`, and both Rijksmuseum calls) — bare `requests.get`, against the same
  WAFs. AIC's search endpoint was one 403 away from repeating the incident, and the identical
  diagnosis would have had to be made twice.
- **The Wikimedia session's contact URL was wrong** — `github.com/frame-art-server`, missing the
  owner segment, so it 404s. It satisfied Wikimedia's policy as a *string* while defeating its
  purpose: the policy exists so they can reach the operator of a misbehaving client.

Both fixed by hoisting `_wiki_session` / `_art_session` above every fetcher, deriving both User-Agents
from one `REPO_URL` constant, and routing every outbound call through a session. Met, AIC, and
Cleveland re-verified live. `test_art_sources.py` now asserts each session's UA is descriptive,
carries the reachable repo URL, and contains no email address — plus a source scan that fails if a
bare `requests.get` reappears in the module.

**The generalizable part:** a fix lands on the path that failed, and the prevention note gets written
about the *class*. Nothing checks the rest of the class. When a learning doc's rule is broader than
the diff that prompted it, the gap between them is a to-do — grep for the pattern before closing.
