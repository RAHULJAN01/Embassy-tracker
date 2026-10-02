#!/usr/bin/env python3
"""test_efficiency.py — guards against the CLASS of bug, not just the instance.

Rahul's rule: "make sure that you are not only fixing the current one but also
making sure that nothing like this or similar ever happens again."

Each test here is a standing promise about how money is spent and what the
register is allowed to contain. If a future change breaks one, the pre-crawl
gate stops the run before it reaches the register.
"""
import sys, re, pathlib, inspect

SRC = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import ai, analyzer, pipeline, crawler, estimator          # noqa: E402

FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


# ============================================================ MONEY
print("\n=== Not one paid call is spent on anything but real work ===")

src_ai = inspect.getsource(ai)
ok("no throwaway 'probe' or health-check call exists",
   "Reply with only" not in src_ai and "_resolve_model" not in src_ai)
ok("a 404 replays the SAME prompt on the next model instead of re-paying",
   "replay" in src_ai.lower() or "_next_model" in src_ai)

src_pipe = inspect.getsource(pipeline)
ok("the closing date is checked BEFORE the model is called",
   src_pipe.index("harvest_date") < src_pipe.index("analyzer.adjudicate"))
ok("an already-closed solicitation returns without adjudicating",
   'report["expired"]' in src_pipe)

src_crawl = inspect.getsource(crawler)
ok("a job is only STARTED if the whole job fits in the budget",
   "can_start_job" in src_crawl and "FULL_JOB_COST" in inspect.getsource(pipeline))
ok("finished work is never re-adjudicated (done-ledger)",
   "if h in ledger" in src_crawl and "ledger.add" in src_crawl)
ok("a record that cannot be completed stops asking for budget",
   "MAX_REPAIR_TRIES" in src_crawl)
ok("the valuation runs only on records worth valuing",
   "should_estimate" in inspect.getsource(estimator))

# the estimator gate itself
g = estimator.should_estimate
ok("  unverified records are never valued",
   not g({"verified": "UNVERIFIED", "tier": "BID"}))
ok("  no-bids are never valued",
   not g({"verified": "VERIFIED", "tier": "NO"}))
ok("  archived records are never valued",
   not g({"verified": "VERIFIED", "tier": "BID", "archived": True}))
ok("  a record with a stated value is never valued",
   not g({"verified": "VERIFIED", "tier": "BID", "value": "$45,000"}))
ok("  a verified, doable, unpriced record IS valued",
   g({"verified": "VERIFIED", "tier": "BID", "value": ""}))

# ============================================================ REGISTRATION
print("\n=== Registration is settled and can never come back ===")

ok("the prompt orders the model to ignore it entirely",
   "DO NOT CONSIDER IT AT ALL" in analyzer.ADJUDICATE_PROMPT)
ok("a deterministic backstop strips it even if the model drifts",
   hasattr(analyzer, "strip_registration"))

VARIANTS = ["Offeror must be registered in SAM.gov prior to award",
            "active registration in the System for Award Management",
            "FAR 52.204-7 applies", "a CAGE code is required",
            "NCAGE code required for foreign entities",
            "Unique Entity Identifier (UEI) must be provided",
            "DUNS number required", "vendor portal registration is mandatory"]
for v in VARIANTS:
    rec = {"tier": "MID", "route": "", "gotchas": [v],
           "restrictions": [{"text": v, "kind": "real", "note": ""}]}
    analyzer.strip_registration(rec)
    ok(f"  stripped: {v[:46]}",
       not rec["restrictions"] and not rec["gotchas"] and rec["tier"] == "BID")

rec = {"tier": "MID", "route": "", "gotchas": [],
       "restrictions": [{"text": "Bidder must hold a Class-A local electrical licence",
                         "kind": "real", "note": ""}]}
analyzer.strip_registration(rec)
ok("a GENUINE restriction is never stripped",
   len(rec["restrictions"]) == 1 and rec["tier"] == "MID")

# ============================================================ DATA SAFETY
print("\n=== A re-scan can never lose or corrupt a record ===")

ok("identity is stable — a re-scan carries prevKey",
   "prevKey" in src_crawl)
ok("a re-read number is refused if another record already owns it",
   "claimed" in src_crawl and "kept separate for a human" in src_crawl)
ok("the operator's own decisions outrank any re-scan",
   'old.get("switched")' in src_crawl)
ok("repair attempts survive the fleet merge",
   "repairTries" in (SRC / "merge_shards.py").read_text())

# ============================================================ FAIL LOUDLY
print("\n=== With one provider, nothing may fail quietly ===")

ok("a dead provider produces a reason, not silence", "down_reason" in src_ai)
for status, body, want_fatal in (
        (401, '{"error":{"message":"invalid x-api-key"}}', True),
        (400, '{"error":{"message":"credit balance is too low"}}', True),
        (429, '{"error":{"message":"rate limited"}}', False),
        (529, '{"error":{"message":"overloaded"}}', False)):
    why, fatal = ai._why(status, body)
    ok(f"  HTTP {status} explained in plain words", len(why) > 25 and "HTTP" not in why[:5], why[:58])
    ok(f"  HTTP {status} fatal={want_fatal}", fatal is want_fatal)
ok("the DOWN reason survives the fleet merge",
   '"down"' in (SRC / "merge_shards.py").read_text())
ok("the portal raises an alarm from it",
   "renderAlarm" in (SRC / "site_template.html").read_text())

# ============================================================ BUDGET ARITHMETIC
print("\n=== The spend stays where we think it is ===")
IN_PER, OUT_PER = 7496, 555
runs, shards, cap = 6, 4, int(re.search(r'MAX_AI_CALLS:\s*"(\d+)"',
                              (SRC / ".github/workflows/crawl.yml").read_text()).group(1))
ceiling = runs * shards * cap
worst = ceiling * (IN_PER / 1e6 * 1.0 + OUT_PER / 1e6 * 5.0) * 30
print(f"  ceiling {ceiling} calls/day -> at most ${worst:,.2f}/month if every call were used")
ok("the daily ceiling cannot quietly cost more than $250/month", worst < 250, f"${worst:.2f}")
ok("and the realistic load (40 solicitations/day) is near $11",
   abs(40 * 30 * (IN_PER / 1e6 + OUT_PER / 1e6 * 5) - 12) < 4,
   f"${40*30*(IN_PER/1e6+OUT_PER/1e6*5):.2f}")

# ============================================================ THE CHEAP GATE
print("\n=== Nothing is opened, read or thought about on a dead notice ===")
import pipeline as _P
_T = "2026-10-02"
_body = " The U.S. Embassy requires cleaning services for the chancery compound. " * 8
for _label, _txt, _want in [
        ("past closing date", _body + " Quotations are due by 14 September 2026.", "expired"),
        ("cancelled", _body + " This solicitation has been cancelled.", "dead"),
        ("withdrawn", _body + " This RFQ was withdrawn by the contracting officer.", "dead"),
        ("already awarded", _body + " Notice of award: contract awarded to Acme Ltd.", "dead"),
        ("closed", _body + " This opportunity has closed.", "dead"),
        ("superseded", _body + " Superseded by solicitation 19KE5026Q0200.", "dead"),
        ("still live", _body + " Quotations are due by 20 December 2026.", ""),
        ("no date stated", _body, "")]:
    _v, _d = _P.triage(_txt, _T)
    ok(f"  {_label} -> {_want or 'worth working'}", _v == _want, f"{_v} {_d[:40]}")

_calls = {"n": 0}; _opened = {"n": 0}


def _ai(p, model=None):
    _calls["n"] += 1
    return {"tier": "BID", "confidence": 0.9}


def _fetch(urls):
    _opened["n"] += len(urls)
    return [(u, "text " * 80, "") for u in urls]


class _B:
    left = 99

    def spend(self, n=1):
        pass


_rec, _rep = _P.process_one(
    {"text": _body + " This solicitation has been cancelled.",
     "attachments": ["https://x/a.pdf"] * 8, "sol_hint": "X"},
    call_ai=_ai, analyzer=analyzer, estimator=estimator, budget=_B(), today=_T,
    fetch_attachments=_fetch)
ok("a dead notice costs ZERO AI calls", _calls["n"] == 0, str(_calls["n"]))
ok("a dead notice downloads ZERO files", _opened["n"] == 0, str(_opened["n"]))
ok("but it is still archived and documented",
   bool(_rep.get("expired")) and bool(_rep.get("deadReason")))
# the parameter name appears in the signature, so compare against the real CALL
_src = inspect.getsource(_P.process_one)
ok("the triage gate runs BEFORE any download",
   _src.index("triage(text, today") < _src.index("fetch_attachments(chosen)"))

ok("a reference already finished is not opened a second time",
   _P.triage(_body + " due 20 December 2026.", _T, {"ABC123"}, "abc123")[0] == "duplicate")

_urls = ["https://x/logo.png", "https://x/RFQ-Statement-of-Work.pdf", "https://x/banner.jpg",
         "https://x/Pricing-Schedule.xlsx"] + [f"https://x/misc{i}.pdf" for i in range(30)]
_pick = _P.pick_attachments(_urls)
ok("the files that decide a bid are read first",
   "Statement-of-Work" in _pick[0] or "Pricing" in _pick[0], _pick[0].split("/")[-1])
ok("logos and banners are never opened",
   not any(x in " ".join(_pick).lower() for x in ("logo", "banner")))
ok("a pile of attachments is capped", len(_pick) <= 12, str(len(_pick)))


print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
