# Pipeline extensions — spec (captured 2026-08-07)

**Status:** B built 2026-08-08 (`tv_no_mat.py`); A planned, not yet built; C added 2026-08-08,
planned, not yet built. Build plan at
`docs/plans/pipeline-extensions-build-plan-2026-08-07.md`.

**Context:** on 2026-08-07 `probe_matte.py` (committed with this spec) proved the Frame TV's
art API can clear the TV-side hardware mat programmatically: `change_matte(content_id, "none")`
visually removed the border on the displayed artwork, and a loop cleared all 20 remaining
matted artworks with zero failures. Full account, including the pairing and Art-Mode
preconditions, in
`docs/solutions/integration-issues/frame-tv-art-channel-pairing-and-matte-api-2026-08-07.md`.
These two features fold that capability — and one long-wanted selection feature — into the
pipeline proper.

---

## A. Theme-based batch selection

**What:** when building a batch, the user can specify what type of pictures to include — e.g.
"Impressionist" or "Cityscapes" — and the batch is drawn from artworks matching that theme.

**Today:** `config.yaml` selects by artist lists, per-source queries, and quality filters.
There is no user-facing *theme* — changing the character of a batch means hand-editing source
queries.

**Constraints:**
- Must work across the existing sources (Met, AIC, Cleveland, Wikimedia), which classify art
  differently — a theme needs a per-source mapping (movement, genre, classification, category),
  not a single query string.
- Keeps the no-API-key promise.

**Open design questions (for the plan pass):**
- CLI (`--theme "Impressionist"`) vs `config.yaml` entry vs both.
- A curated theme catalog (predictable, testable mappings) vs free-text passthrough (flexible,
  unpredictable per source) — or a catalog with free-text fallback.
- How a theme composes with the existing artist filters and per-artist caps.

## B. Post-upload no-mat pass

**What:** once a new batch has been uploaded to the TV via USB, one command sets every artwork
on the TV to "no mat" — no more clicking through images in the TV menu.

**Basis:** `probe_matte.py` — remote-channel pairing to obtain the token, then a
`change_matte(id, "none")` loop over artworks whose `matte_id` is not already `none`.

**Constraints:**
- **Different TVs expose different matte types and options.** The script must discover the
  TV's actual capabilities via `get_matte_list()` and verify `"none"` is offered before
  applying anything — never assume this TV's option set. On models where the art websocket
  channel is unavailable, fail loud and point at the manual path; do not guess.
- Preconditions from the solutions doc apply: TV in Art Mode (or fully awake), token paired
  via the remote channel first.
- **Stays a one-shot post-upload script.** `batch_build.py` remains TV-free and USB-capable;
  no daemon, scheduler, or persistent connection returns. This narrows the April 2026
  batch-only decision (a one-shot script is now justified by the probe), it does not reverse
  the architecture.

## C. Bulk artwork deletion (added 2026-08-08)

**What:** one command deletes all user-uploaded artworks from the TV — the front half of the
refresh cycle. A full refresh then becomes: delete the old batch → USB-import the new one →
no-mat pass. Today, clearing out an old batch means deleting images one at a time in the TV
menu, the same per-image chore B just eliminated for mats.

**Basis:** the `samsungtvws` art API exposes `delete(content_id)` / `delete_list(content_ids)`,
and B's plumbing (`tv_session.py`, pairing, Art-Mode preconditions, `available()` + dedupe)
carries over unchanged. The delete calls themselves are **unverified against the real TV** —
verification on a single sacrificial artwork is the first build step, per the probe-first
method that de-risked B.

**Constraints:**
- **Deletion is irreversible on the TV — there is no undo file this time, and the safety model
  must say so honestly.** Recovery is re-importing from the local `frame_tv_art/` output (with
  new content ids), never an in-place restore. The script writes a manifest of what it deleted
  (ids + labels) for the record, but a manifest is a receipt, not an undo.
- **User-uploaded content only.** The pass must scope itself to the TV's user content (the
  `MY_F*` ids / `MY-C*` categories observed on this TV) and never touch anything else —
  verified against the live TV before bulk use, not assumed from naming.
- Same fail-loud posture as B: capability and scope verified before anything is deleted;
  unreachable art channel points at the preconditions doc.
- **Stays a one-shot post-build script.** Same architectural narrowing as B — no daemon, no
  persistent connection; `batch_build.py` stays TV-free.

**Open design questions (for the plan pass — answered there):**
- Confirmation shape for an irreversible bulk operation (dry-run default is not enough here).
- Per-item `delete()` loop (granular errors, proven pattern) vs chunked `delete_list()`
  (fewer round-trips) — decide at the single-artwork verification step.
- What happens when the currently-displayed artwork is deleted — observe live, don't guess.

---

**Sequencing:** B first — built and verified 2026-08-08. A and C remain; C is small (it rides
on B's plumbing and safety model, minus the undo), A is the larger design job (per-source
theme mapping). Order between them is Nisha's call per session.
