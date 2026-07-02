---
title: Frame TV art stopped rotating after batch-build refactor
date: 2026-07-01
category: integration-issues
module: batch_build
problem_type: integration_issue
component: tooling
symptoms:
  - "Frame TV displays the same static image; art never rotates"
  - "No rotation errors in batch_build.log — the last run only fetched and processed images to disk"
  - "config.yaml contained history/cache keys (history_file, history_size, max_cached) that no code reads, implying software rotation still existed"
root_cause: missing_workflow_step
resolution_type: documentation_update
severity: medium
tags: [frame-tv, rotation, refactor, dead-config, batch-build, samsung-art-mode]
---

# Frame TV art stopped rotating after batch-build refactor

## Problem

Pictures on the Samsung Frame TV stopped rotating. The repo looked like it should handle rotation (it used to), so the bug was reported against the software — but the current code contains no rotation mechanism at all.

## Symptoms

- The TV shows one static image indefinitely
- `batch_build.log` shows clean runs with zero rotation-related activity or errors — the code path simply doesn't exist
- `config.yaml` still carried `storage.history_file`, `history_size`, and `max_cached` keys referenced by no Python code, suggesting rotation/history machinery that had actually been deleted

## What Didn't Work

- Searching the logs for rotation failures — there are none, because commit `ca6170f` ("refactor: simplify to batch-build-only architecture") deleted the entire rotation engine (`frame_art_server.py`, `scheduler.py`, `tv_controller.py`, which called `samsungtvws art.set_slideshow_status({"type": "shuffleSlideshow"})` on an interval). You cannot instrument a code path that no longer exists.

## Solution

The refactor intentionally delegated rotation to the TV's built-in Art Mode shuffle slideshow — a **manual TV menu step** (README step 5) that was never actually enabled. The fix (PR #4):

1. README now states explicitly that the script never rotates art and makes the slideshow step a bolded must-do, with a "Troubleshooting: art is not rotating" section (TV test: import batch → select all → Start Slideshow, shuffle on, shortest interval → wait one interval in Art Mode → confirm change).
2. Removed the orphaned config keys that implied software rotation/history still existed (`history_file`, `history_size` deleted; `max_cached` made real by implementing `prune_cache()`).
3. CLI epilog warns that skipping the slideshow step means one static image forever.

## Why This Works

The root cause was not a code defect — it was a responsibility handoff from software to a manual step that (a) was never performed and (b) was undermined by leftover config that made the software still look responsible. Making the manual step loud and deleting the misleading dead config aligns the repo's apparent behavior with its actual behavior.

## Prevention

- **When a feature "stops working" after a simplifying refactor, first check whether the feature was intentionally moved out of the software into a manual step** — diff the refactor commit for deleted modules before hunting for bugs in the surviving code.
- **When a refactor deletes a subsystem, delete its config keys in the same commit.** Orphaned keys (`history_file`, `history_size`) are active misdirection: they make users and future debuggers believe the capability still exists.
- If a workflow depends on a manual step, the docs must state the failure mode of skipping it ("the TV shows one static image forever"), not just list the step.
- Some Frame TV firmware resets the slideshow setting after a new USB import — re-enable it after every refresh.

## Related Issues

- PR #4 — fix: rotation root cause + no-mat default + local source + cleanup
- Commit `ca6170f` — the refactor that removed rotation code (deleted `frame_art_server.py`, `scheduler.py`, `tv_controller.py`; recoverable via `git show ca6170f~1:tv_controller.py` if a thin programmatic slideshow setup is ever wanted)
