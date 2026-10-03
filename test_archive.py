#!/usr/bin/env python3
"""test_archive.py — a correction must survive the merge, not be undone by it.

THE BUG THIS LOCKS OUT, in full, because it is the one Rahul kept seeing:

A gate-parts notice was re-crawled and CORRECTLY found closed. The pipeline
stamped it tier=NO, archived=True, and wrote the finding into its notes. The
row still showed as a live BID with a 2026 deadline. Three separate things
conspired, and each is tested here:

  1. normalize() forced archived=False for any deadline not in the past, and
     only recognised "cancelled/canceled/removed" as a dead status -- so a
     record marked Expired, or marked archived with a today-dated deadline,
     was quietly un-archived.
  2. once the archive flag was gone, the lean correction lost better() to the
     fat, confident, WRONG copy on richness, and that copy won the merge.
  3. _merge_pair back-filled the deadline from the stale copy onto the
     correction, handing the fabricated date straight back.

normalize now only ever ADDS an archive, never removes one; the dead-status
list covers every word the pipeline writes; and a deadline is never
back-filled onto an archived record.
"""
import sys, pathlib

SRC = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import merge_shards as M          # noqa: E402

FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


TODAY = M.today()


def past(days=40):
    import datetime
    return (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=days)).isoformat()


def future(days=40):
    import datetime
    return (datetime.date.fromisoformat(TODAY) + datetime.timedelta(days=days)).isoformat()


# ============================================ normalize only ever archives
print("\n=== normalize archives the dead and never un-archives a correction ===")
ok("a past deadline is archived",
   M.normalize({"deadline": past(), "tier": "BID", "status": "Active"})["archived"] is True)
for word in ("Expired", "Cancelled", "Withdrawn", "Awarded", "Superseded", "Closed"):
    r = M.normalize({"deadline": "", "tier": "NO", "status": word})
    ok(f"  a '{word}' notice is archived", r["archived"] is True, str(r.get("archived")))
ok("a record already flagged archived STAYS archived, even with a today deadline",
   M.normalize({"deadline": TODAY, "tier": "NO", "status": "Expired",
                "archived": True})["archived"] is True)
ok("a record flagged archived with NO deadline stays archived",
   M.normalize({"deadline": "", "tier": "NO", "status": "Cancelled",
                "archived": True})["archived"] is True)
ok("a genuinely live record is NOT archived",
   M.normalize({"deadline": future(), "tier": "BID", "status": "Active",
                "dateEvidence": {"closing": "due later"}})["archived"] is False)


# ============================================ the full gate-parts chain
print("\n=== the gate-parts record comes out of the merge correct ===")
correction = {"sol": "PR15305534", "title": "Gate Spare Parts", "link": "https://bi.usembassy.gov/business/",
              "tier": "NO", "status": "Expired", "archived": True, "deadline": past(),
              "verified": "UNVERIFIED", "updated": TODAY, "lastDeepScan": TODAY + " 12:31 UTC",
              "verifyNotes": ["a re-crawl found this notice is no longer open: closed 14 Sep"]}
stale = {"sol": "PR15305534", "title": "Gate Spare Parts", "link": "https://bi.usembassy.gov/business/",
         "tier": "BID", "status": "Active", "archived": False, "deadline": future(),
         "verified": "VERIFIED", "updated": past(1), "files": ["a.pdf", "b.pdf", "c.pdf"]}

for order, (x, y) in {"correction first": (correction, stale),
                      "stale first": (stale, correction)}.items():
    r = M.normalize(M._merge_pair(dict(x), dict(y)))
    ok(f"  ({order}) it is NON-ELIGIBLE, not BID", r["tier"] == "NO", r["tier"])
    ok(f"  ({order}) it is archived", r["archived"] is True, str(r["archived"]))
    ok(f"  ({order}) it keeps its real past deadline, not the fabricated one",
       r["deadline"] == past(), r["deadline"])
    ok(f"  ({order}) better() picked the correction, not the fat stale copy",
       M.better(x, y) is (correction if x is correction or y is correction else None)
       or M.better(dict(x), dict(y)).get("tier") == "NO")


# ============================================ a deadline is never resurrected
print("\n=== a cancelled record never gets its old deadline back ===")
cancelled = {"sol": "RFQ-77", "tier": "NO", "status": "Cancelled", "archived": True,
             "deadline": "", "verified": "UNVERIFIED", "updated": TODAY,
             "lastDeepScan": TODAY + " 13:00 UTC"}
fat = {"sol": "RFQ-77", "tier": "BID", "status": "Active", "archived": False,
       "deadline": future(), "verified": "VERIFIED", "updated": past(1), "files": ["x.pdf"]}
r = M.normalize(M._merge_pair(dict(fat), dict(cancelled)))
ok("the cancelled record has NO deadline", r["deadline"] == "", repr(r["deadline"]))
ok("and it is archived NON-ELIGIBLE", r["tier"] == "NO" and r["archived"] is True)


# ============================================ but a live record still enriches
print("\n=== a live record still fills a missing field from its sibling ===")
thin = {"sol": "RFQ-5", "tier": "BID", "status": "Active", "archived": False, "deadline": "",
        "verified": "UNVERIFIED", "updated": TODAY}
rich = {"sol": "RFQ-5", "tier": "BID", "status": "Active", "archived": False,
        "deadline": future(), "verified": "VERIFIED", "updated": TODAY,
        "dateEvidence": {"closing": "due later"}}
r = M._merge_pair(dict(thin), dict(rich))
ok("a live record with no deadline adopts its sibling's", r["deadline"] == future(), r["deadline"])

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
