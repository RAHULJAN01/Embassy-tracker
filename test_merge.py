#!/usr/bin/env python3
"""test_merge.py — the fleet merge must never lose work or override the operator.

Four bots run at once and each carries its own copy of the register. When their
results are combined, two things must survive no matter which copy "wins":

  1. the operator's own DELETE / HIDE / SWITCH decisions, and
  2. the record of deep re-scans already attempted — otherwise a solicitation
     that cannot be completed gets retried for ever instead of giving up and
     asking for a human.
"""
import json, sys, shutil, pathlib, tempfile

SRC = pathlib.Path(__file__).resolve().parent
FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


def fresh(rows, operator=None, shards=None):
    W = pathlib.Path(tempfile.mkdtemp(prefix="merge-"))
    shutil.copy(SRC / "merge_shards.py", W / "merge_shards.py")
    (W / "data.json").write_text(json.dumps({"meta": {}, "solicitations": rows}))
    (W / "operator.json").write_text(json.dumps(
        operator or {"deleted": {}, "hidden": {}, "switched": {}}))
    sd = W / "shards"
    for i, rs in enumerate(shards or []):
        (sd / f"shard-{i}").mkdir(parents=True)
        (sd / f"shard-{i}" / "data.json").write_text(json.dumps({"meta": {}, "solicitations": rs}))
    sys.path.insert(0, str(W))
    import importlib
    M = importlib.import_module("merge_shards")
    importlib.reload(M)
    M.HERE = W
    M.main(str(sd))
    out = json.loads((W / "data.json").read_text())
    sys.path.remove(str(W))
    return out, {r["sol"]: r for r in out["solicitations"]}


def rec(sol, title, tier="BID", deadline="2027-01-01", **kw):
    r = {"sol": sol, "link": f"https://dz.usembassy.gov/{sol}", "title": title, "tier": tier,
         "verified": "VERIFIED", "deadline": deadline, "files": []}
    r.update(kw)
    return r


print("\n=== 1. The operator's decisions outrank the bots' ===")
out, rows = fresh(
    [rec("19IN5026Q0101", "Keep me"),
     rec("19IN5026Q0102", "Delete me"),
     rec("19IN5026Q0103", "Hide me", tier="NO"),
     rec("19IN5026Q0104", "Switch me", tier="NO"),
     rec("19IN5026Q0105", "Hidden but expired", deadline="2020-01-01")],
    operator={"deleted": {"19IN5026Q0102": {"on": "2026-10-02 11:00 UTC"}},
              "hidden": {"19IN5026Q0103": {"on": "2026-10-02 11:05 UTC"},
                         "19IN5026Q0105": {"on": "2026-10-02 11:06 UTC"}},
              "switched": {"19IN5026Q0104": {"tier": "BID", "on": "2026-10-02 11:10 UTC"}}})
ok("an untouched record stays active",
   not rows["19IN5026Q0101"].get("deleted") and not rows["19IN5026Q0101"].get("archived"))
ok("DELETE takes it off the register, stamped with the date and time",
   rows["19IN5026Q0102"].get("deleted") and rows["19IN5026Q0102"].get("archived"),
   rows["19IN5026Q0102"].get("deletedOn"))
ok("a deleted record is KEPT in History, never destroyed", "19IN5026Q0102" in rows)
ok("HIDE sets it aside, stamped", rows["19IN5026Q0103"].get("hidden"),
   rows["19IN5026Q0103"].get("hiddenOn"))
ok("SWITCH wins over whatever the bots decided",
   rows["19IN5026Q0104"]["tier"] == "BID" and rows["19IN5026Q0104"].get("switched"),
   rows["19IN5026Q0104"]["tier"])
ok("hidden + expired leaves the Hidden cart for History, documented",
   not rows["19IN5026Q0105"].get("hidden") and rows["19IN5026Q0105"].get("archived"),
   rows["19IN5026Q0105"].get("unhiddenBecause", ""))
c = out["meta"]["counts"]
ok("deleted and hidden are counted apart from active",
   c["deleted"] == 1 and c["hidden"] == 1 and c["active"] == 2, json.dumps(c))

print("\n=== 2. A deep re-scan is never forgotten by another bot's stale copy ===")
base = rec("19IN5026Q0201", "Cannot be completed", tier="REVIEW", deadline="",
           verified="UNVERIFIED", verifyNotes=["no closing date found"])
tried = dict(base)
tried.update({"repairTries": 3, "lastDeepScan": "2026-10-02 16:20 UTC",
              "verifyNotes": ["no closing date found",
                              "auto-complete gave up after 3 deep re-scans — "
                              "needs a human look (nothing readable)"]})
out, rows = fresh([dict(base)], shards=[[tried], [dict(base)]])
r = rows["19IN5026Q0201"]
ok("the attempts survive the merge", r.get("repairTries") == 3, str(r.get("repairTries")))
ok("the deep-scan timestamp survives", r.get("lastDeepScan") == "2026-10-02 16:20 UTC",
   str(r.get("lastDeepScan")))
ok("the 'needs a human look' note survives",
   any("gave up" in n for n in r.get("verifyNotes") or []), str(r.get("verifyNotes"))[:80])
sys.path.insert(0, str(SRC))
import crawler
ok("and it now stops asking for AI budget", not crawler.repairable(r))

print("\n=== 3. A record healed by one bot beats a stale unverified copy ===")
good = rec("19IN5026Q0301", "Healed", verified="VERIFIED", deadline="2027-02-02",
           lastDeepScan="2026-10-02 16:25 UTC", repairTries=0)
stale = rec("19IN5026Q0301", "Healed", tier="REVIEW", deadline="", verified="UNVERIFIED",
            verifyNotes=["not adjudicated"])
out, rows = fresh([dict(stale)], shards=[[good], [stale]])
r = rows["19IN5026Q0301"]
ok("the completed version wins", r.get("verified") == "VERIFIED", str(r.get("verified")))
ok("and it keeps its closing date", r.get("deadline") == "2027-02-02", str(r.get("deadline")))


# ============================================== A CORRECTION MUST SURVIVE
print("\n=== a correction beats the fat, confident, wrong record ===")
import merge_shards as _M
# The gate-parts notice: re-read, correctly found closed, the finding written
# into its own notes -- and it still showed as a live BID, because another bot
# held the old copy and the old copy had more files on it. Richness was the
# whole test, and a correction is almost always POORER than what it replaces.
_STALE = {"sol": "PR15305534", "tier": "BID", "status": "Active", "archived": False,
          "deadline": "2026-10-12", "files": ["a", "b", "c", "d"],
          "citation": {"quote": "x"}, "restrictions": ["r1", "r2"],
          "updated": "2026-10-01", "dateEvidence": {}}
_FRESH = {"sol": "PR15305534", "tier": "NO", "status": "Expired", "archived": True,
          "deadline": "2025-10-12", "files": [], "updated": "2026-10-03",
          "lastDeepScan": "2026-10-03 12:35 UTC", "dateEvidence": {},
          "verifyNotes": ["a re-crawl found this notice is no longer open"]}
ok("the newer 'this is closed' wins", _M.better(_STALE, _FRESH) is _FRESH)
ok("whichever way round they are given", _M.better(_FRESH, _STALE) is _FRESH)
_m = _M._merge_pair(dict(_STALE), dict(_FRESH))
ok("and the merged row is archived, not active",
   _m.get("archived") is True and _m.get("status") == "Expired",
   f"{_m.get('status')} archived={_m.get('archived')}")
ok("with the tier dropped out of BID", _m.get("tier") == "NO", str(_m.get("tier")))

print("  -- but a stale 'closed' must not beat a newer re-open --")
_OLDDEAD = dict(_FRESH, updated="2026-09-01", lastDeepScan="2026-09-01 00:00 UTC")
_NEWLIVE = dict(_STALE, updated="2026-10-03", lastDeepScan="2026-10-03 13:00 UTC")
ok("the newer reading wins again", _M.better(_OLDDEAD, _NEWLIVE) is _NEWLIVE)

print("  -- and a provable deadline beats a fatter record with an unprovable one --")
_PROV = {"sol": "X", "deadline": "2026-11-01", "files": [],
         "dateEvidence": {"closing": "Quotations are due 1 November 2026"}}
_FAT = {"sol": "X", "deadline": "2026-12-09", "files": ["a", "b", "c"],
        "citation": {"q": 1}, "tier": "BID", "dateEvidence": {}}
ok("the traceable date wins", _M.better(_PROV, _FAT) is _PROV)


print("\n" + "=" * 62)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 62)
sys.exit(1 if FAILS else 0)
