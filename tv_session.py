#!/usr/bin/env python3
"""Shared plumbing for the one-shot TV-facing scripts (probe_matte, tv_no_mat).

Everything here is safety-critical and easy to get subtly wrong, which is why
it lives in one place instead of being repeated per script (see
docs/solutions/integration-issues/frame-tv-art-channel-pairing-and-matte-api-2026-08-07.md):

- The pairing token is a credential and this repo is public, so it is stored
  OUTSIDE the repo, in the home directory. It matches none of the .gitignore
  patterns, so keeping it out of the tree entirely removes the stray
  `git add -A` failure mode rather than relying on an ignore rule.
- samsungtvws logs the pairing token at INFO level (connection.py and
  authenticator.py both do). The logger is pinned to WARNING and detached from
  propagation BEFORE the library is imported, so the token cannot reach a
  terminal or a log file regardless of what the importing script configures.
- The art channel never issues a pairing token; only the remote-control
  channel does. connect() pairs on the remote channel first when no token is
  stored, which is the step the first probe attempt missed three times.

This module imports samsungtvws lazily: importing tv_session (e.g. in CI's
import smoke check) must not require the TV dependency, because the core
pipeline is deliberately TV-free.
"""

from __future__ import annotations

import logging
from pathlib import Path

TOKEN_FILE = str(Path.home() / ".frame_art_probe_token")

# One client name shared by all scripts in this repo, so the TV's Allow prompt
# is accepted once and the stored token covers every script.
CLIENT_NAME = "frame-art-probe"


def quiet_samsungtvws_logging() -> None:
    """Pin the samsungtvws logger shut so the pairing token cannot be logged."""
    logging.getLogger("samsungtvws").setLevel(logging.WARNING)
    logging.getLogger("samsungtvws").propagate = False


def connect(ip: str, timeout: int = 30):
    """Return a SamsungTVWS for *ip* with a valid pairing token.

    If no token is stored yet, pairs on the remote channel first — the TV
    shows an Allow prompt, and accepting it writes the token to TOKEN_FILE.
    The art channel alone can never produce a token, so skipping this step
    fails forever with ms.channel.clientDisconnect.

    Raises ImportError if samsungtvws is not installed — callers catch it and
    print the install hint, keeping the dependency out of requirements.txt.
    """
    quiet_samsungtvws_logging()
    from samsungtvws import SamsungTVWS

    tv = SamsungTVWS(
        host=ip, port=8002, token_file=TOKEN_FILE, name=CLIENT_NAME, timeout=timeout
    )
    if not Path(TOKEN_FILE).exists():
        # Remote-channel handshake -> Allow prompt on the TV -> token written.
        tv.open()
        tv.close()
    return tv
