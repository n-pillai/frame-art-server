---
title: Frame TV art channel needs remote-channel pairing first, and only answers in Art Mode
date: 2026-08-07
category: integration-issues
module: probe_matte
problem_type: integration_issue
component: tooling
symptoms:
  - "Every art API call fails with ConnectionFailure: {'event': 'ms.channel.timeOut'} while art.supported() returns True"
  - "TV shows the Allow-connection prompt and the user accepts, but no token file is ever written"
  - "In Art Mode the error changes to ms.channel.clientDisconnect with attributes token: 'None'"
root_cause: missing_workflow_step
resolution_type: documentation_update
severity: medium
tags: [frame-tv, samsungtvws, art-mode, matte, pairing, websocket]
---

# Frame TV art channel needs remote-channel pairing first, and only answers in Art Mode

## Problem

`probe_matte.py` (samsungtvws 3.0.4, art API v5.0.1.0 on the TV) could not reach the art
channel at all: `art.supported()` worked (it is a plain REST call), but every websocket art
call — `get_api_version()`, `get_matte_list()`, `available()` — failed, and the pairing token
was never stored even after the user accepted the TV's Allow prompt three times.

## Root cause — two separate preconditions, each producing a different error

1. **The TV only runs the art service when it is in Art Mode or fully awake.** While the TV
   was playing normal content or in standby, the art app never joined the channel and the TV
   answered `ms.channel.timeOut`. No amount of client-side timeout raising fixes this.
2. **The art channel never issues a pairing token; only the remote channel does.** Connecting
   straight to the art channel with `token=None` gets `ms.channel.clientDisconnect` (the
   `attributes.token: 'None'` in the event payload is the tell). The Allow prompts the user
   accepted were for those unauthenticated connections, but the token that acceptance issues
   is delivered on the *remote-control* channel handshake, which the probe never opened.

## Solution

Pair on the remote channel once, then run art calls with the stored token, with the TV in
Art Mode:

```python
tv = SamsungTVWS(host=ip, port=8002, token_file=tf, name="frame-art-probe", timeout=30)
tv.open()   # remote-channel handshake -> Allow prompt on TV -> token written to token_file
tv.close()
tv.art().get_matte_list()   # now works (TV in Art Mode)
```

After that, everything in the probe worked first try: `matte_types` confirmed as the mount mat
(`none` offered), `change_matte(content_id, "none")` visually removed the border on the
displayed artwork, and a loop cleared all 20 remaining matted artworks (284 total) with zero
failures.

## Prevention / notes for future TV-facing scripts

- Sequence: TV in Art Mode (or fully on) → `tv.open()`/`tv.close()` to pair → art calls.
- Error signatures: `ms.channel.timeOut` = art app not running (TV state problem);
  `ms.channel.clientDisconnect` with `token: 'None'` = unauthenticated (pairing problem).
- The token lives outside the repo (`~/.frame_art_probe_token`) because it is a credential and
  the repo is public.
- `samsungtvws` logs the pairing token at INFO level — keep its logger at WARNING or above
  (tv_session.py pins this for every script using it) so the token cannot reach a terminal or
  log file.
- **`available()` lists the same artwork once per category it appears in** (observed
  2026-08-08: 284 entries for 145 distinct artworks). Dedupe by `content_id` before counting
  or iterating, or a bulk pass double-applies and over-reports (tv_no_mat.py's
  `dedupe_items()`).
