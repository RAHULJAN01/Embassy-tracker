#!/usr/bin/env python3
"""End-to-end test of crawler.run() with every external dependency stubbed.

Proves the user's rule: a bot never starts a solicitation it cannot finish,
and a half-processed solicitation is NEVER stored.
"""
import os, sys, json, shutil, pathlib, tempfile

SRC = pathlib.Path(__file__).resolve().parent
WORK = pathlib.Path(tempfile.mkdtemp(prefix="e2e-"))
for f in ("crawler.py", "analyzer.py", "ai.py", "fetcher.py", "un_sources.py",
          "estimator.py", "pipeline.py", "docreader.py", "roots.json"):
    shutil.copy(SRC / f, WORK / f)
sys.path.insert(0, str(WORK))
os.environ["MAX_AI_CALLS"] = "9"          # fuel for exactly 3 full jobs
os.environ["TIME_BUDGET_S"] = "600"
os.environ["PAGE_PAUSE"] = "0"
os.environ["SHARDS"] = "1"
os.environ["SHARD"] = "0"

import crawler, pipeline, analyzer, fetcher, un_sources, estimator, ai

(WORK / "control.json").write_text('{"paused": false}')
(WORK / "data.json").write_text('{"meta":{},"solicitations":[]}')

# ------------------------------------------------------------------ fixtures
def sol_text(n, days="2026-12-15"):
    return f"""
REQUEST FOR QUOTATION
Solicitation Number: 19IN5026Q{n:04d}
Issued by: U.S. Embassy New Delhi, India
Title: Supply and delivery of office furniture for the Chancery
The U.S. Embassy New Delhi invites quotations for the supply and delivery of
office furniture. Quotations must be submitted electronically.
Closing date for receipt of quotations: {days} at 17:00 local time.
Delivery shall be DDP to the Embassy warehouse in New Delhi.
Offerors must be registered in SAM.gov prior to award.
Payment terms: Net 30 days after acceptance and receipt of invoice.
This is a total small business set-aside is not applicable; open to all sources.
Estimated quantity: 120 task chairs, 60 desks, 40 filing cabinets.
"""

ATTACH_TEXT = """
STATEMENT OF WORK — ATTACHMENT 1
Technical specifications for office furniture.
All chairs shall be ergonomic, mesh back, 5-point base.
Country of origin must be disclosed. No prior experience in India required.
Bidders may rely on the experience of subcontractors or partners.
Warranty: 24 months minimum, parts and labour.
"""

# ------------------------------------------------------------------ stubs
AI_CALLS = {"n": 0}
AI_MODE = {"mode": "ok"}


def fake_call(prompt, **kw):
    AI_CALLS["n"] += 1
    if AI_MODE["mode"] == "dead":
        raise ai.AllExhausted("every provider is rate-limited")
    if "pricing analyst" in prompt:
        return {"low_usd": 42000, "likely_usd": 52000, "high_usd": 61000,
                "confidence": 0.62, "currency_note": "Award paid in INR at embassy rate",
                "drivers": ["quantity stated", "Indian import duty"],
                "basis": "120 task chairs at roughly 180 USD, 60 desks at 320 USD and "
                         "40 cabinets at 240 USD, Indian market pricing plus delivery"}
    return {
        "tier": "BID", "confidence": 0.86,
        "title": "Supply and delivery of office furniture for the Chancery",
        "sol": f"19IN5026Q{AI_CALLS['n']:04d}",
        "closing": "2026-12-15", "sector": "COTS",
        "classification": "Commercial off-the-shelf goods",
        "scope": "Supply 120 chairs, 60 desks, 40 cabinets DDP New Delhi",
        "shipping": "DDP Embassy warehouse New Delhi",
        "payment": "Net 30 after acceptance",
        "setaside": "None — open to all sources",
        "reason": "Quotations must be submitted electronically.",
        "citation": {"doc": "RFQ page",
                     "quote": "Closing date for receipt of quotations: 2026-12-15 at 17:00 local time."},
        "restrictions": [], "route": "Direct quote with Indian logistics partner",
    }


def fake_make_caller():
    class R:
        def names(self): return ["stub-a", "stub-b"]
        def diag(self): return {"ok": {"stub-a": AI_CALLS["n"]}, "errors": {}}
        providers = []
    return R(), fake_call


SAM_OPS = [
    {"noticeId": "n1", "title": "SAM furniture notice", "solicitationNumber": "19IN5026Q0101",
     "organizationName": "U.S. Embassy New Delhi", "resourceLinks": ["https://x/a1.pdf"],
     "placeOfPerformance": {"country": {"name": "India"}}},
]


def fake_sam_search(cfg):
    return SAM_OPS


def fake_sam_unit(op):
    return sol_text(101), 1, 0


READ_MODE = {"mode": "ok"}


def fake_read_attachment_full(url, retries=1):
    if READ_MODE["mode"] == "fail":
        return "", "scanned PDF (OCR libraries unavailable)"
    return ATTACH_TEXT, ""


# embassy site stubs
def fake_discover_proc_pages(root, cfg):
    return [root["base"] + "/business/"]


CAND = {"n": 0}


def fake_collect_candidates(pp, cfg):
    CAND["n"] += 1
    base = pp.rstrip("/")
    return ([f"{base}/rfq-{CAND['n']}-a", f"{base}/rfq-{CAND['n']}-b"], [])


def fake_chase(url):
    n = abs(hash(url)) % 9000
    return sol_text(n), ["https://x/sow.pdf"], 1, 0, ""


def fake_group_file_units(files):
    return {}


crawler.ai.make_caller = fake_make_caller
crawler.sam_search = fake_sam_search
crawler.sam_unit = fake_sam_unit
crawler.fetcher.read_attachment_full = fake_read_attachment_full
crawler.discover_proc_pages = fake_discover_proc_pages
crawler.collect_candidates = fake_collect_candidates
crawler.chase_solicitation = fake_chase
crawler.group_file_units = fake_group_file_units
crawler.un_sources.UN_SOURCES = []          # UN tested separately
crawler.HERE = WORK
crawler.DATA = WORK / "data.json"
crawler.STATE = WORK / "state.json"
crawler.STATUS = WORK / "status.json"
crawler.BLOCKED = WORK / "blocked.json"
crawler.CONTROL = WORK / "control.json"
crawler.ROOTS = WORK / "roots.json"
crawler.SAM_DIAG = {"last": "stubbed"}
_real_uh = crawler.unit_hash
TRACE = {"on": False}
ADJUDICATED = []
_real_po = crawler.pipeline.process_one
def traced_po(unit, **kw):
    if TRACE["on"]:
        ADJUDICATED.append(unit.get("hash"))
    return _real_po(unit, **kw)
crawler.pipeline.process_one = traced_po

# trim roots so the test is quick
cfg = json.loads((WORK / "roots.json").read_text())
cfg["roots"] = cfg["roots"][:3]
(WORK / "roots.json").write_text(json.dumps(cfg))


def reset():
    AI_CALLS["n"] = 0
    CAND["n"] = 0
    (WORK / "data.json").write_text('{"meta":{},"solicitations":[]}')
    (WORK / "state.json").write_text('{"root_idx":0}')


def results():
    d = json.loads((WORK / "data.json").read_text())
    s = json.loads((WORK / "status.json").read_text())
    return d, s


FAILS = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


# ================================================================ SCENARIO 1
print("\n=== 1. Full fuel: every stored record must be COMPLETE ===")
reset(); AI_MODE["mode"] = "ok"; READ_MODE["mode"] = "ok"
crawler.MAX_AI_CALLS = 9
crawler.run("roots")
d, s = results()
rows = d["solicitations"]
print(f"  stored={len(rows)}  completed={s.get('completed')}  abandoned={s.get('abandoned')}  ai={s.get('aiCalls')}")
check("at least one record stored", len(rows) >= 1, f"{len(rows)}")
check("never stored more records than AI calls spent",
      len(rows) <= s.get("aiCalls", 0), f"{len(rows)} records / {s.get('aiCalls')} calls")
for r in rows:
    check(f"  [{r.get('sol','?')}] has a title", bool(r.get("title")))
    check(f"  [{r.get('sol','?')}] has a closing date", bool(r.get("deadline")), r.get("deadline", ""))
    check(f"  [{r.get('sol','?')}] adjudicated (not REVIEW)", r.get("tier") in ("BID", "MID", "NO"), r.get("tier", ""))
    check(f"  [{r.get('sol','?')}] VERIFIED", r.get("verified") == "VERIFIED",
          str(r.get("verifyNotes")))
    check(f"  [{r.get('sol','?')}] cited from source text", bool(r.get("citation")))
    check(f"  [{r.get('sol','?')}] valued by estimator", bool(r.get("value")), str(r.get("value")))
check("ai spend within budget", s.get("aiCalls", 99) <= 9, str(s.get("aiCalls")))
check("finish note reports completed/abandoned", "fully processed" in str(s.get("currentJob", "")),
      str(s.get("currentJob"))[:70])
check("doc capabilities reported", isinstance(s.get("docCaps"), dict))

# ================================================================ SCENARIO 2
print("\n=== 2. Nearly-empty tank: refuses to START rather than half-finish ===")
reset()
crawler.MAX_AI_CALLS = 2            # less than FULL_JOB_COST (3)
crawler.run("roots")
d, s = results()
check("nothing stored on an un-finishable tank", len(d["solicitations"]) == 0, str(len(d["solicitations"])))
check("no AI calls burned", AI_CALLS["n"] == 0, str(AI_CALLS["n"]))
check("clean-stop reason recorded", "stopping cleanly" in str(s.get("currentJob", "")),
      str(s.get("currentJob"))[:80])

# ================================================================ SCENARIO 3
print("\n=== 3. AI down: store NOTHING, leave the work for next run ===")
reset(); AI_MODE["mode"] = "dead"
crawler.MAX_AI_CALLS = 9
crawler.run("roots")
d, s = results()
check("no half-records stored when the AI is dead", len(d["solicitations"]) == 0,
      str(len(d["solicitations"])))
check("ledger NOT poisoned (work can be retried)",
      len(json.loads((WORK / "data.json").read_text())["meta"].get("ledger", [])) == 0)

# ================================================================ SCENARIO 4
print("\n=== 4. Unreadable attachments: record kept, reason named, not VERIFIED ===")
reset(); AI_MODE["mode"] = "ok"; READ_MODE["mode"] = "fail"
crawler.MAX_AI_CALLS = 9
crawler.run("roots")
d, s = results()
rows = d["solicitations"]
check("records still stored", len(rows) >= 1, str(len(rows)))
if rows:
    r = rows[0]
    check("flagged UNVERIFIED", r.get("verified") == "UNVERIFIED", str(r.get("verified")))
    notes = " ".join(r.get("verifyNotes") or [])
    check("verify note names the document problem", "document" in notes.lower() or "unread" in notes.lower(), notes)
    rf = r.get("readFailures") or []
    check("per-file reason recorded", bool(rf) and "OCR" in str(rf), str(rf)[:90])

# ================================================================ SCENARIO 5
print("\n=== 5. Ledger: finished work is never redone ===")
reset(); AI_MODE["mode"] = "ok"; READ_MODE["mode"] = "ok"
crawler.MAX_AI_CALLS = 9
crawler.run("roots")
d1, _ = results()
done_a = set(d1["meta"]["ledger"])
first = len(d1["solicitations"])
led = len(d1["meta"].get("ledger", []))
check("ledger populated after a completed run", led >= 1, str(led))
# run again with the IDENTICAL fixtures: the ledger must make it a no-op
CAND["n"] = 0
AI_CALLS["n"] = 0
# rewind the resume pointer so the SAME roots are visited again
(WORK / "state.json").write_text('{"root_idx":0}')
TRACE["on"] = True; ADJUDICATED.clear()
crawler.run("roots")
TRACE["on"] = False
d2, s2 = results()
check("re-run adds NO duplicate records",
      len(d2["solicitations"]) == first, f"{first} -> {len(d2['solicitations'])}")
redone = [h for h in ADJUDICATED if h in done_a]
check("re-run never re-adjudicates a finished solicitation", not redone, str(redone))
check("re-run spends its fuel on NEW work instead",
      set(d2["meta"]["ledger"]) > done_a,
      f"{len(done_a)} -> {len(d2['meta']['ledger'])}")

# ================================================================ SCENARIO 6
print("\n=== 6. Operator STOP is honoured ===")
reset()
(WORK / "control.json").write_text('{"paused": true}')
crawler.MAX_AI_CALLS = 9
crawler.run("roots")
d, s = results()
check("paused run does no work", len(d["solicitations"]) == 0, str(len(d["solicitations"])))
check("status says paused", s.get("paused") is True or "paused" in str(s.get("currentJob", "")).lower())
(WORK / "control.json").write_text('{"paused": false}')

print("\n" + "=" * 62)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 62)
print("workdir:", WORK)
sys.exit(1 if FAILS else 0)
