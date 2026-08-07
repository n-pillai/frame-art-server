# Pipeline extensions — spec (captured 2026-08-07)

**Status:** captured intent — not yet designed or built.

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

---

**Sequencing:** B first — it is small, proven end-to-end, and removes a recurring manual chore
after every batch. A is the larger design job (per-source theme mapping).
