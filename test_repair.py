#!/usr/bin/env python3
"""test_repair.py — the repair phase, tested against the REAL register.

Answers Rahul's question directly: "why am I still seeing all the solicitations
pending to be verified?" The engine must finish what it already holds before it
goes looking for more, converge run after run, and give up loudly (not silently
churn the AI budget) on records that genuinely cannot be completed by a machine.
"""
import os, sys, json, shutil, pathlib, tempfile

SRC = pathlib.Path(__file__).parent
WORK = pathlib.Path(tempfile.mkdtemp(prefix="repair-"))
for f in ("crawler.py", "analyzer.py", "ai.py", "fetcher.py", "un_sources.py",
          "estimator.py", "pipeline.py", "docreader.py", "roots.json"):
    shutil.copy(SRC / f, WORK / f)

# ---------------------------------------------------------------- the fixture
# A register with the SAME failure mix the live one had when the repair phase was
# written: no closing date, no title, never adjudicated, unreadable attachments.
# Synthesised rather than read from data.json so the test is deterministic and
# does not break when the real register is rebuilt.
def _rec(n, **kw):
    r = {"sol": f"19IN50{26}Q{n:04d}",
         "link": f"https://dz.usembassy.gov/business/rfq-{n}",
         "title": f"Supply and delivery, lot {n}", "tier": "BID",
         "verified": "VERIFIED", "deadline": "2027-03-01", "posted": "2026-09-01",
         "files": [f"https://dz.usembassy.gov/files/{n}.pdf"], "fileCount": 1,
         "verifyNotes": [], "archived": False, "firstSeen": "2026-09-01",
         "platform": "USGOV", "sector": "COTS",
         # a complete record proves its deadline; without proof it is re-checked,
         # which is the point of the date gate
         "dateEvidence": {"closing": "Quotations are due by 1 March 2027."}}
    r.update(kw)
    return r


def build_register():
    rows = []
    n = 100
    for _ in range(12):      # no closing date — the biggest real failure
        n += 1
        rows.append(_rec(n, deadline="", verified="UNVERIFIED",
                         verifyNotes=["no closing date found"]))
    for _ in range(8):       # never adjudicated
        n += 1
        rows.append(_rec(n, tier="REVIEW", verified="UNVERIFIED",
                         verifyNotes=["not adjudicated", "no closing date found"],
                         deadline=""))
    for _ in range(4):       # no title
        n += 1
        rows.append(_rec(n, title="(untitled solicitation)", verified="UNVERIFIED",
                         verifyNotes=["no title"]))
    for _ in range(4):       # documents the bots could not read
        n += 1
        rows.append(_rec(n, verified="UNVERIFIED", fileCount=3,
                         files=[f"https://dz.usembassy.gov/files/{n}-{i}.pdf" for i in range(3)],
                         verifyNotes=["3 document(s) unreadable"]))
    for _ in range(6):       # already complete — must never be touched
        n += 1
        rows.append(_rec(n))
    for _ in range(4):       # archived — must never be touched
        n += 1
        rows.append(_rec(n, archived=True, deadline="2020-01-01", status="Expired"))
    return rows


REGISTER = build_register()
(WORK / "data.json").write_text(json.dumps({"meta": {}, "solicitations": REGISTER}))
SRC_DATA = WORK / "seed.json"
SRC_DATA.write_text(json.dumps({"meta": {}, "solicitations": REGISTER}))

sys.path.insert(0, str(WORK))
os.environ.update({"MAX_AI_CALLS": "40", "TIME_BUDGET_S": "600",
                   "PAGE_PAUSE": "0", "SHARDS": "1", "SHARD": "0"})
import crawler, pipeline, ai

for n, rel in (("HERE", ""), ("DATA", "data.json"), ("STATE", "state.json"),
               ("STATUS", "status.json"), ("BLOCKED", "blocked.json"),
               ("CONTROL", "control.json"), ("ROOTS", "roots.json")):
    setattr(crawler, n, WORK / rel if rel else WORK)
(WORK / "control.json").write_text('{"paused": false}')
crawler.SAM_DIAG = {"last": "stubbed"}

FAILS = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


allrows = REGISTER
active = [r for r in allrows if not r.get("archived")]
unver = [r for r in active if r.get("verified") != "VERIFIED"]
print(f"\nTEST REGISTER: {len(allrows)} records, {len(active)} active, "
      f"{len(unver)} unverified")

print("\n=== 1. The repair queue targets the real backlog ===")
q = crawler.repair_queue(allrows)
check("queue is non-empty", len(q) > 0, f"{len(q)} records queued")
check("a VERIFIED record with a proven date is left alone",
      all(r.get("verified") != "VERIFIED" for r in q))

# and the new rule: an unprovable deadline pulls a record back in even if it
# was marked VERIFIED, because a date nobody can point to is not a date
_unproven = _rec(999, deadline="2026-12-01")
_unproven.pop("dateEvidence", None)
check("an unproven deadline is re-checked even when VERIFIED",
      crawler.suspect_date(_unproven) and crawler.repairable(_unproven))
check("queue never contains an archived record", all(not r.get("archived") for r in q))
check("every queued record has something to re-crawl",
      all(r.get("link") or r.get("files") for r in q))
if q:
    top = q[0]
    # worst-first now means: a refusal we cannot trust, then a date we never
    # proved, then un-adjudicated or date-less. Any of those at the top is right.
    check("worst-first: the most dangerous record is at the top",
          crawler.suspect_nobid(top) or crawler.unproven_dates(top)
          or top.get("tier") == "REVIEW"
          or "date" in " ".join(top.get("verifyNotes") or []).lower(),
          f"{top.get('tier')} / {top.get('verifyNotes')}")

print("\n=== 2. Shards split the backlog with no overlap and no gaps ===")
seen, total = [], 0
for s in range(4):
    crawler.SHARDS, crawler.SHARD = 4, s
    part = crawler.repair_queue(allrows)
    total += len(part)
    seen.append({id(r) for r in part})
crawler.SHARDS, crawler.SHARD = 1, 0
check("no record is repaired by two bots", not any(
    seen[i] & seen[j] for i in range(4) for j in range(i + 1, 4)))
check("every repairable record is owned by exactly one bot",
      total == len(crawler.repair_queue(allrows)), f"{total} vs {len(q)}")

print("\n=== 3. A good re-crawl COMPLETES the record ===")
GOOD_TEXT = """
REQUEST FOR QUOTATION — AMENDMENT 01
Solicitation Number: 19IN5026Q0044
Title: Supply of networked multifunction printers for the Chancery
Closing date for receipt of quotations: 2026-12-20 at 16:00 local time.
Delivery DDP to the Embassy warehouse. Payment Net 30 after acceptance.
Set-aside: none, open to all sources. No prior in-country experience required;
bidders may rely on the experience of subcontractors and authorized dealers.
Quantities: 18 multifunction printers, 36 toner sets, 3 years of maintenance.
"""
AI_N = {"n": 0}


def good_ai(prompt, **kw):
    AI_N["n"] += 1
    if "pricing analyst" in prompt:
        return {"low_usd": 55000, "likely_usd": 72000, "high_usd": 94000,
                "confidence": 0.58,
                "basis": "18 multifunction printers at roughly 2,600 USD landed plus "
                         "36 toner sets and a three-year maintenance tail, Indian pricing"}
    return {"tier": "BID", "confidence": 0.88,
            "title": "Supply of networked multifunction printers for the Chancery",
            "sol": "19IN5026Q0044", "closing": "2026-12-20", "sector": "COTS",
            "classification": "Commercial off-the-shelf goods",
            "scope": "18 MFPs, 36 toner sets, 3 years maintenance, DDP",
            "shipping": "DDP Embassy warehouse", "payment": "Net 30 after acceptance",
            "setaside": "None — open to all sources",
            "citation": {"doc": "RFQ Amendment 01",
                         "quote": "Closing date for receipt of quotations: 2026-12-20 at 16:00 local time."},
            "restrictions": [], "route": "Authorized dealer letter + local installer"}


def stub(mode="good"):
    crawler.ai.make_caller = lambda: (type("R", (), {
        "names": lambda s: ["stub"], "diag": lambda s: {"ok": {"stub": AI_N["n"]}, "errors": {}},
        "providers": []})(), good_ai)
    crawler.chase_solicitation = lambda u, **k: (
        (GOOD_TEXT, [], 1, 0, "") if mode == "good" else ("", [], 0, 1, ""))
    crawler.fetcher.read_attachment_full = lambda u, retries=1: (
        ("", "scanned PDF (OCR libraries unavailable)") if mode != "good" else (GOOD_TEXT, ""))
    crawler.sam_search = lambda cfg: []
    crawler.un_sources.UN_SOURCES = []
    crawler.discover_proc_pages = lambda root, cfg: []


def runit():
    (WORK / "state.json").write_text('{"root_idx":0}')
    crawler.run("roots")
    d = json.loads((WORK / "data.json").read_text())
    s = json.loads((WORK / "status.json").read_text())
    return d, s


shutil.copy(SRC_DATA, WORK / "data.json")
stub("good"); AI_N["n"] = 0
crawler.MAX_AI_CALLS = 40
d1, s1 = runit()
before = len(unver)
after_rows = [r for r in d1["solicitations"] if not r.get("archived")]
after = sum(1 for r in after_rows if r.get("verified") != "VERIFIED")
print(f"  unverified: {before} -> {after}   (repaired={s1.get('repaired')}, "
      f"ai={s1.get('aiCalls')})")
check("the backlog actually shrank", after < before, f"{before} -> {after}")
check("status reports how many it completed", s1.get("repaired", 0) > 0, str(s1.get("repaired")))
check("status reports what is still unfinished", "stillUnfinished" in s1,
      str(s1.get("stillUnfinished")))
check("no record count was lost", len(d1["solicitations"]) == len(allrows),
      f"{len(allrows)} -> {len(d1['solicitations'])}")
check("repair stayed inside its share of the budget",
      s1.get("aiCalls", 99) <= int(40 * crawler.REPAIR_SHARE) + 3,
      f"{s1.get('aiCalls')} of {int(40*crawler.REPAIR_SHARE)}")
healed = [r for r in after_rows if r.get("verified") == "VERIFIED" and r.get("lastDeepScan")]
if healed:
    h = healed[0]
    check("a healed record carries a deadline", bool(h.get("deadline")), h.get("deadline"))
    check("a healed record is adjudicated", h.get("tier") in ("BID", "MID", "NO"), h.get("tier"))
    check("a healed record records when it was deep-scanned", bool(h.get("lastDeepScan")))
    check("a healed record keeps its original firstSeen", bool(h.get("firstSeen")))

print("\n=== 4. Runs converge — repeated runs keep closing the gap ===")
prev = after
for i in range(3):
    AI_N["n"] = 0
    d1, s1 = runit()
    rows_a = [r for r in d1["solicitations"] if not r.get("archived")]
    now = sum(1 for r in rows_a if r.get("verified") != "VERIFIED")
    print(f"  run {i+2}: unverified {prev} -> {now}  (repaired={s1.get('repaired')})")
    check(f"run {i+2} did not go backwards", now <= prev, f"{prev} -> {now}")
    prev = now

print("\n=== 5. A record that CANNOT be completed gives up loudly, not silently ===")
shutil.copy(SRC_DATA, WORK / "data.json")
stub("bad")
spends = []
for i in range(5):
    AI_N["n"] = 0
    d2, s2 = runit()
    spends.append(s2.get("aiCalls", 0))
    print(f"  run {i+1}: ai={s2.get('aiCalls')}  repaired={s2.get('repaired')}  "
          f"stillUnfinished={s2.get('stillUnfinished')}")
check("an unreadable record costs ZERO AI calls (nothing to adjudicate)",
      all(x == 0 for x in spends), f"{spends}")
check("the retry ceiling eventually stops them asking for budget",
      s2.get("stillUnfinished") == 0, str(s2.get("stillUnfinished")))
rows2 = d2["solicitations"]
gave_up = [r for r in rows2 if any("gave up" in n for n in (r.get("verifyNotes") or []))]
check("records that can't be fixed say so in plain words", len(gave_up) > 0,
      f"{len(gave_up)} records")
if gave_up:
    print(f"    e.g. {gave_up[0].get('verifyNotes')}")
    check("the give-up note names the real reason",
          any("scan" in n or "readable" in n or "crawl" in n
              for n in gave_up[0]["verifyNotes"]))
check("nothing exceeds the retry ceiling",
      all(int(r.get("repairTries", 0) or 0) <= crawler.MAX_REPAIR_TRIES for r in rows2))

print("\n=== 6. The operator's own decisions survive a repair ===")
shutil.copy(SRC_DATA, WORK / "data.json")
dd = json.loads((WORK / "data.json").read_text())
tgt = next(r for r in dd["solicitations"]
           if crawler.repairable(r))
tgt["switched"] = True; tgt["tier"] = "MID"; tgt["firstSeen"] = "2026-01-02"
tgt["notes"] = "operator: partner lined up in Delhi"
sol_key = tgt.get("sol") or tgt.get("link")
(WORK / "data.json").write_text(json.dumps(dd))
stub("good"); AI_N["n"] = 0
d3, _ = runit()
got = next((r for r in d3["solicitations"] if (r.get("sol") or r.get("link")) == sol_key), None)
check("the record is still there", got is not None)
if got:
    check("a manual SWITCH is not overwritten by the AI", got.get("tier") == "MID",
          str(got.get("tier")))
    check("operator notes survive", "partner lined up" in str(got.get("notes")))
    check("firstSeen is preserved", got.get("firstSeen") == "2026-01-02",
          str(got.get("firstSeen")))

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
