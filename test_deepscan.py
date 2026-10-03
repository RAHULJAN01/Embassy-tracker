#!/usr/bin/env python3
"""test_deepscan.py — the DEEP SCAN button must actually do something.

It never had. `log(...)` was called in both branches of the deepone handler and
`log` is not defined anywhere in crawler.py, so every press raised NameError
immediately. The broad handler around the run turned that into a status line,
the job reported success, and not one document was re-read. Rahul pressed a
button, watched it finish, and got the same record back.

What the button is for, in his words: "if I think I want to bid on this, but
want to make sure the data here is correct, then I'll hit that deep scan button
of that particular solicitation, then only then a single smart AI does that job
for that particular solicitation only."

So this suite runs the real deepone path end to end against a stubbed model and
checks the three things that matter: it runs at all, it touches ONLY the chosen
solicitation, and every call goes to the strong model.
"""
import sys, os, json, pathlib, shutil, tempfile

SRC = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


WORK = pathlib.Path(tempfile.mkdtemp(prefix="deep-"))
for f in ("roots.json", "company.json", "flags.json"):
    if (SRC / f).exists():
        shutil.copy(SRC / f, WORK / f)

TARGET = "19AB5026Q0099"
OTHER = "19AB5026Q0111"
NOTICE = ("U.S. Embassy Example — Gate Spare Parts\n"
          f"Solicitation No. {TARGET}\n"
          "The Embassy requires control modules, drive wheels and sensors.\n"
          + "Specification and delivery details follow. " * 20 +
          "Quotations are due 28 November 2026 at 1500 hrs local time.\n")

(WORK / "data.json").write_text(json.dumps({"meta": {}, "solicitations": [
    {"sol": TARGET, "link": "https://x.usembassy.gov/gate", "title": "Gate Spare Parts",
     "tier": "MID", "post": "U.S. Embassy Example", "country": "Example",
     "deadline": "2026-11-28", "verified": "UNVERIFIED", "source": "Site",
     "archived": False, "fp": "old", "repairTries": 0},
    {"sol": OTHER, "link": "https://x.usembassy.gov/other", "title": "Other notice",
     "tier": "MID", "post": "U.S. Embassy Example", "country": "Example",
     "deadline": "2026-12-01", "verified": "UNVERIFIED", "source": "Site",
     "archived": False, "fp": "old2", "repairTries": 0}]}))
for name, body in (("status.json", {}), ("control.json", {"paused": False}),
                   ("operator.json", {}), ("blocked.json", {"sites": {}}),
                   ("state.json", {})):
    (WORK / name).write_text(json.dumps(body))

os.environ["DEEP_SOL"] = TARGET
os.environ["MAX_AI_CALLS"] = "12"
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-used"

import crawler, ai, analyzer  # noqa: E402

crawler.HERE = WORK
for n in ("DATA", "STATUS", "CONTROL", "OPERATOR", "BLOCKED", "ROOTS", "STATE"):
    if hasattr(crawler, n):
        old = pathlib.Path(getattr(crawler, n))
        setattr(crawler, n, WORK / old.name)
crawler.DEEP_SOL = TARGET

MODELS, PROMPTS = [], []


def fake_call(prompt, model=None):
    MODELS.append(model)
    PROMPTS.append(prompt[:40])
    if "closing_quote" in prompt:
        return {"closing": "2026-11-28",
                "closing_quote": "Quotations are due 28 November 2026 at 1500 hrs local time.",
                "posted": "", "posted_quote": "", "qa_due": "", "qa_quote": "",
                "why": "stated as the submission deadline"}
    if "est_value" in prompt or "usd" in prompt.lower():
        return {"low_usd": 8000, "high_usd": 16000, "basis": "stub"}
    return {"tier": "BID", "confidence": 0.93, "title": "Gate Spare Parts Supply",
            "sol": TARGET, "sector": "COTS", "restrictions": [], "route": "dealer",
            "closing": "2026-11-28",
            "closing_quote": "Quotations are due 28 November 2026 at 1500 hrs local time.",
            "brief": "Gate spare parts for the chancery.", "citation": None}


crawler.ai.make_caller = lambda: (type("C", (), {
    "model": "claude-haiku-4-5-20251001", "names": lambda s: ["stub"],
    "diag": lambda s: {"ok": {}, "errors": {}}, "providers": []})(), fake_call)
crawler.chase_solicitation = lambda u, **k: (NOTICE, [], 1, 0, "", [])
crawler.fetcher.read_attachment_full = lambda u, retries=1: (NOTICE, "")
crawler.discover_proc_pages = lambda root, cfg: []
crawler.collect_candidates = lambda *a, **k: []
crawler.sam_search = lambda cfg: []
crawler.un_sources.UN_SOURCES = []

print("\n=== pressing DEEP SCAN on one solicitation ===")
crashed = ""
try:
    crawler.run("roots")   # the mode deepone dispatches to, with DEEP_SOL set
except SystemExit:
    pass
except Exception as e:
    crashed = f"{type(e).__name__}: {e}"
ok("the run does not crash", not crashed, crashed)

out = json.loads((WORK / "data.json").read_text()).get("solicitations", [])
by = {r.get("sol"): r for r in out}
ok("both records still exist", set(by) == {TARGET, OTHER}, str(sorted(by)))

ok("the chosen solicitation was re-read", (by.get(TARGET) or {}).get("fp") != "old",
   str((by.get(TARGET) or {}).get("fp"))[:14])
ok("and nothing else was touched", (by.get(OTHER) or {}).get("fp") == "old2",
   str((by.get(OTHER) or {}).get("fp"))[:14])

print("\n=== and every call went to the strong model ===")
ok("the model was actually called", bool(MODELS), str(MODELS))
ok("every call used the strong model",
   bool(MODELS) and all(m == ai.REVIEW_MODEL for m in MODELS), str(set(MODELS)))
ok("the strong model is Sonnet", "sonnet" in (ai.REVIEW_MODEL or "").lower(), ai.REVIEW_MODEL)

t = by.get(TARGET) or {}
print("\n=== and the record came back better than it went in ===")
ok("it carries a deadline", bool(t.get("deadline")), str(t.get("deadline")))
ok("the deadline is traceable to a line in the source",
   bool((t.get("dateEvidence") or {}).get("closing")),
   str((t.get("dateEvidence") or {}).get("closing"))[:60])

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
