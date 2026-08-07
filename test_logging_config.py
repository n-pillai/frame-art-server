#!/usr/bin/env python3
"""Tests for logging configuration precedence in batch_build.py.

Run with: python test_logging_config.py

Precedence under test, ascending:
    built-in default  ->  config.yaml `logging:`  ->  CLI flag

resolve_logging_settings() is deliberately separate from configure_logging()
so precedence can be checked without mutating global logging state.
"""

import sys

from batch_build import (
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_LEVEL,
    resolve_logging_settings,
)

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


CFG = {"logging": {"level": "DEBUG", "file": "from-config.log"}}


def main():
    print("layer 1 — built-in defaults, nothing else supplied:")
    f, lvl = resolve_logging_settings()
    check("file defaults", f == DEFAULT_LOG_FILE, f)
    check("level defaults", lvl == DEFAULT_LOG_LEVEL, lvl)

    print("\nlayer 2 — config.yaml beats the defaults:")
    f, lvl = resolve_logging_settings(CFG)
    check("config file wins", f == "from-config.log", f)
    check("config level wins", lvl == "DEBUG", lvl)

    print("\nlayer 3 — CLI beats config:")
    f, lvl = resolve_logging_settings(CFG, log_file="from-cli.log", log_level="ERROR")
    check("cli file wins", f == "from-cli.log", f)
    check("cli level wins", lvl == "ERROR", lvl)

    print("\npartial overrides do not clobber the other layer:")
    f, lvl = resolve_logging_settings(CFG, log_file="from-cli.log")
    check("cli file + config level", (f, lvl) == ("from-cli.log", "DEBUG"), f"{f},{lvl}")
    f, lvl = resolve_logging_settings(CFG, log_level="ERROR")
    check("config file + cli level", (f, lvl) == ("from-config.log", "ERROR"), f"{f},{lvl}")

    print("\nmissing or empty config falls back cleanly:")
    for label, cfg in (("None", None), ("empty dict", {}),
                       ("no logging section", {"display": {}}),
                       ("null logging section", {"logging": None}),
                       ("empty logging section", {"logging": {}})):
        f, lvl = resolve_logging_settings(cfg)
        check(f"{label} -> defaults", (f, lvl) == (DEFAULT_LOG_FILE, DEFAULT_LOG_LEVEL))

    print("\nlevel is normalised to upper case:")
    f, lvl = resolve_logging_settings({"logging": {"level": "debug"}})
    check("lowercase config level upper-cased", lvl == "DEBUG", lvl)
    f, lvl = resolve_logging_settings(None, log_level="warning")
    check("lowercase cli level upper-cased", lvl == "WARNING", lvl)

    print("\nthe stale filename is gone from the shipped config:")
    import yaml
    shipped = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    check("config.yaml no longer points at the dead daemon log",
          shipped["logging"]["file"] != "./frame_art.log",
          shipped["logging"]["file"])
    check("config.yaml matches what the tool actually writes",
          shipped["logging"]["file"].endswith(DEFAULT_LOG_FILE),
          shipped["logging"]["file"])

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
