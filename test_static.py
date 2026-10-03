#!/usr/bin/env python3
"""test_static.py — a name that does not exist must never reach the field.

Two live faults were found by a thirty-second static check that nothing was
running:

  crawler.py:982  undefined name 'log'        -> DEEP SCAN raised NameError on
  crawler.py:990  undefined name 'log'           every press. The broad handler
                                                 turned it into a status line,
                                                 so the button reported success
                                                 and re-read nothing. It had
                                                 never once worked.

  crawler.py:1505 undefined name '_first_title' -> the date field test died on
                                                   shard 0 the first time it ran.

Both sit on branches the unit suites do not execute: a button nobody presses in
a test, and a diagnostic mode. No amount of fixture testing would have found
them, because the code was never run. A parser does not need to run it.

So every module is now compiled and checked for undefined names before anything
ships. This is the cheapest test in the repo and it caught two real outages.
"""
import ast, sys, pathlib, subprocess

SRC = pathlib.Path(__file__).resolve().parent
FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


FILES = sorted(p for p in SRC.glob("*.py") if p.name != pathlib.Path(__file__).name)

# ------------------------------------------------------------- it must compile
for p in FILES:
    try:
        ast.parse(p.read_text(), filename=p.name)
        bad = ""
    except SyntaxError as e:
        bad = f"line {e.lineno}: {e.msg}"
    ok(f"  {p.name} parses", not bad, bad)

# ------------------------------------------------- no name that does not exist
# pyflakes is the authority here. If it is not installed the check still runs,
# because a missing linter must not silently turn the gate off.
try:
    import pyflakes  # noqa: F401
    have = True
except ImportError:
    have = False
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                        "--break-system-packages", "pyflakes"],
                       capture_output=True, text=True)
    have = r.returncode == 0

if not have:
    print("  FAIL  pyflakes could not be installed — the undefined-name gate cannot run")
    FAILS.append("pyflakes unavailable")
else:
    r = subprocess.run([sys.executable, "-m", "pyflakes"] + [str(p) for p in FILES],
                       capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    # Only the fatal classes. An unused import is untidy; a name that does not
    # exist is an outage the moment that line is reached.
    fatal = [ln for ln in out.splitlines()
             if "undefined name" in ln
             or "undefined local" in ln
             or "syntax error" in ln.lower()]
    ok("no undefined names anywhere in the crawler", not fatal,
       fatal[0][:90] if fatal else "")
    for ln in fatal:
        print("        " + ln)

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
