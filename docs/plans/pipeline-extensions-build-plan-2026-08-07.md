# Pipeline extensions — build plan (2026-08-07)

**Status:** B built 2026-08-08 (`tv_no_mat.py` + `tv_session.py`, verified end-to-end against a
real TV — apply, undo file, restore all exercised live). A planned, not yet built. Implements
[`docs/specs/pipeline-extensions-2026-08-07.md`](../specs/pipeline-extensions-2026-08-07.md).
Build order is **B then A**, per the spec: B is small and proven end-to-end by
`probe_matte.py` (20/20 artworks cleared on a real TV, 2026-08-07); A is the larger design job.

---

## Decisions locked by this plan

1. **The TV's IP address is never committed.** It is a CLI argument (`--ip`), exactly as in
   `probe_matte.py` — not a `config.yaml` entry. `config.yaml` is tracked in a public repo, and
   a home network address does not belong in it. Same rule for anything else that identifies a
   specific TV or network.
2. **The pairing token stays at `~/.frame_art_probe_token`**, shared with the probe (same
   client name, so pairing once covers both scripts). The reasoning is unchanged from the
   probe: it is a credential, the repo is public, and keeping it out of the tree entirely beats
   relying on an ignore rule.
3. **`samsungtvws` stays out of `requirements.txt`.** The core pipeline
   (`batch_build.py` → USB) must keep working with zero TV-facing dependencies — that is the
   April 2026 batch-only decision, which the spec narrows but does not reverse. TV scripts
   import it lazily and print the `pip install samsungtvws` hint when it is missing (the
   probe's existing pattern).
4. **Shared TV plumbing is extracted to a small `tv_session.py`.** The pairing sequence, token
   path, and the samsungtvws logger pin (it logs the pairing token at INFO) are safety-critical
   and about to exist in two scripts; the solutions doc already documents them as rules for
   "future TV-facing scripts". One ~40-line module keeps them in one place.
   `probe_matte.py` is refactored to use it, behavior unchanged.
5. **Theme catalog over free-text (extension A).** Themes are curated entries in `config.yaml`
   with explicit per-source mappings — predictable and testable. Free-text passthrough is
   deliberately not built in v1: each source interprets a bare string differently, so the
   results would be unpredictable per source, and the catalog can always gain entries. Revisit
   only if the catalog proves too rigid in use.

---

## B. Post-upload no-mat pass — `tv_no_mat.py`

One command after a USB import: set every artwork on the TV to "no mat".

### Behavior contract

1. `python tv_no_mat.py --ip <tv-ip>` — **dry run by default**: connects, pairs if needed,
   discovers capabilities, and reports what *would* change (per-artwork current `matte_id`,
   count needing changes). Nothing is modified without `--apply`. This mirrors the probe's
   safety model.
2. **Capability discovery before any change:** `get_matte_list()` must confirm the TV offers
   `"none"` — never assume this TV's option set. If the art channel is unreachable or `"none"`
   is not offered, fail loud with the error signature and point at the manual TV-menu path and
   the pairing/Art-Mode preconditions doc. No guessing, no partial fallback.
3. `--apply` changes only artworks whose `matte_id` is not already `none`, one
   `change_matte(content_id, "none")` at a time, with per-item error handling — one failure
   does not abort the loop.
4. **Every apply run writes an undo file** (`no_mat_undo_<timestamp>.json`, gitignored):
   `content_id` → previous `matte_id` for everything changed. A bulk operation against 200+
   artworks must be reversible; the probe printed a per-item undo command, this is the bulk
   equivalent. A `--restore <file>` flag replays it.
5. Exit summary: changed / already-none / failed counts. Exit non-zero if anything failed, and
   remind the operator that the API reporting success is not proof — look at the TV.
6. Preconditions surfaced in `--help` and on connection failure: TV in Art Mode (or fully on),
   pairing prompt accepted once. Error signatures from the solutions doc are echoed
   (`ms.channel.timeOut` = art app not running; `clientDisconnect` + `token: 'None'` =
   pairing problem).

### Build steps

| # | Step | Verify |
|---|------|--------|
| B1 | Extract `tv_session.py` (token path, logger pin, `connect(ip)` returning a paired `SamsungTVWS`); refactor `probe_matte.py` onto it | probe runs unchanged against the TV (read-only run) |
| B2 | `tv_no_mat.py` selection + capability logic as pure functions (which items need changing; is `"none"` offered; undo-file shape) | `test_tv_no_mat.py` — pure logic, mocked art responses, no TV |
| B3 | Wire the connected flow: dry-run report, `--apply` loop, undo write, `--restore`, exit codes | real-TV run: dry-run first, then `--apply` after a USB import, border visually gone |
| B4 | CI: add the TV scripts to the `compileall` and import steps (lazy import keeps them importable without `samsungtvws`); run `test_tv_no_mat.py` | ⚠️ closes an existing gap — `probe_matte.py` is not compile-checked by CI today |
| B5 | Docs: README Quick Start step 4 becomes "run `tv_no_mat.py`" with the manual path kept as fallback; Known Limitations updated; Roadmap trimmed | docs match behavior |

**Acceptance:** after a fresh USB import, one dry-run + one `--apply` clears every mat with the
TV verified visually; a TV that does not offer `"none"` (or an unreachable art channel) produces
a loud, specific failure and changes nothing; CI green.

**Size:** one short session. The risky half (pairing, Art Mode, `change_matte` semantics) is
already proven by the probe.

## A. Theme-based batch selection

Build a batch by theme — `python batch_build.py --count 200 --theme impressionist` — instead of
hand-editing source queries.

### Design

A theme is a **named bundle of per-source search inputs**, exactly the shape each source
already consumes from `config.yaml` — Met/AIC/Cleveland query lists, Wikimedia categories and
queries — plus an optional post-filter:

```yaml
themes:
  impressionist:
    met_museum:
      queries: ["impressionist painting", "Claude Monet", "Camille Pissarro"]
    art_institute_chicago:
      queries: ["Claude Monet", "Alfred Sisley", "Gustave Caillebotte"]
    cleveland_museum:
      queries: ["Monet", "Pissarro", "Sisley"]
    wikimedia_commons:
      categories: ["Paintings_by_Claude_Monet", "Paintings_by_Camille_Pissarro"]
    # optional precision filter, applied to source metadata after fetch:
    keywords_any: ["impressionis"]     # matched against classification/medium/title fields
    # optional per-theme override; defaults to the global setting:
    # major_artists_only: false
```

Why this shape:

- **Themes are configuration, not new code paths.** Selecting a theme swaps the per-source
  inputs before gathering; the entire existing pipeline (filters, caps, featured artists,
  processing) runs unchanged. The per-source mapping the spec requires falls out of the shape
  rather than needing a translation layer.
- **The no-API-key promise holds** — themes only re-parameterize the four existing keyless
  sources.
- **Composition with existing filters:** artist filters and per-artist caps apply as normal. A
  theme may override `major_artists_only` only, because some themes (e.g. Cityscapes) would
  starve under the ~90-artist list while others (Impressionist) work fine with it. Featured
  artists still get their guaranteed minimum only if the theme's sources surface them — a theme
  batch is allowed to not contain Raja Ravi Varma.
- **Post-filter is optional and conservative:** `keywords_any` tightens precision where source
  metadata supports it (AIC `classification_title`, Met/CMA medium and classification,
  titles everywhere). Wikimedia categories are already the theme, so most themes won't need it.

Ship **4 starter themes** as catalog entries: `impressionist`, `cityscapes`, `seascapes`,
`indian-masters`. Each must be validated against real source results before shipping (a theme
that returns 6 images is worse than no theme).

### Build steps

| # | Step | Verify |
|---|------|--------|
| A1 | Theme resolution: load `themes:`, `--theme` selects one, unknown theme errors **listing available themes**, `--list-themes` prints the catalog | `test_themes.py` — resolution, merge/override, error text; no APIs |
| A2 | Wire into `batch_build.py`: theme swaps per-source inputs, `major_artists_only` per-theme override, `keywords_any` post-filter | unit tests with mocked metadata |
| A3 | Author + tune the 4 starter themes against live sources (`--dry-run` shows candidate counts per source) | each theme yields a healthy candidate pool at `--count 100` |
| A4 | CI config validation extended: `themes:` entries must only name known sources and valid keys | CI fails on a malformed theme |
| A5 | Docs: README "Themes" section (usage + how to add a theme); Roadmap updated | docs match behavior |

**Acceptance:** `--theme impressionist --dry-run` shows a themed candidate pool; a full themed
batch builds end-to-end; no theme flag → behavior identical to today (regression: existing
config untouched by default); CI green.

**Size:** one to two sessions — A3 (tuning real source results per theme) is the honest
unknown, not the code.

---

## Out of scope (deliberate)

- **No daemon, scheduler, uploader, or persistent TV connection** — the spec narrows the April
  2026 batch-only decision to one-shot scripts; this plan does not widen it further.
- **No slideshow/auto-rotation control** (`set_auto_rotation_status` exists in samsungtvws and
  would automate the manual shuffle step behind the July rotation issue). Real, but a separate
  decision — it belongs in a future spec revision if the manual step keeps hurting, not
  smuggled into B.
- **No free-text theme passthrough** (decision 5).

## Personal-data & credentials posture

Reviewed 2026-08-07: **this repo needs no personal-data gating.** It reads and writes no
personal data — no `PERSONAL_DATA_DIR` mount, no integrity layer (L1/L2), no denylist secret
(a public repo must never carry the denylist; its generic stand-in, the vendored
`pii-public-scan.yml` content floor, is already in CI). The two sensitive artifacts in play are
handled structurally: the **pairing token** lives outside the repo at `~/.frame_art_probe_token`
with the samsungtvws logger pinned to WARNING so it cannot reach a log; the **TV's IP** is a
CLI argument, never a tracked file (decision 1). These two rules are the whole posture — new
TV-facing work must keep both.
