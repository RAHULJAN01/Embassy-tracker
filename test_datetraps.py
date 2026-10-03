#!/usr/bin/env python3
"""test_datetraps.py — the date traps found by auditing the bot's real path.

Every case here was reproduced against the live code before it was fixed. They
are kept as tests because each one published a WRONG DEADLINE for free, with no
AI call anywhere in the loop to disagree with it, and a wrong deadline is the
one error on this register that cannot be told apart from a right one.
"""
import sys, pathlib

SRC = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import analyzer as A, pipeline as P          # noqa: E402

TODAY = "2026-10-03"
FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


# ============================================================ 10/12/2026
print("\n=== TRAP 1: an all-numeric date is two dates, not one ===")
US = "Offers must be received no later than 10/12/2026 at 1600 hours local time."
ok("both readings are reported", set(A.find_dates(US)) == {"2026-10-12", "2026-12-10"},
   str(A.find_dates(US)))
ok("both are marked ambiguous", A.ambiguous_at(US) == {"2026-10-12", "2026-12-10"})
ok("the free phrase path refuses to pick one", A.harvest_date(US, A._DEADLINE_CUES) == "",
   repr(A.harvest_date(US, A._DEADLINE_CUES)))
# the whole point: whichever convention the post used, the model's reading stands
for iso in ("2026-10-12", "2026-12-10"):
    good, _ev, why = A.quoted_date(iso, US, US)
    ok(f"  the model's reading {iso} can be proven", good, why)

print("  -- a date with only one possible reading is still decided for free --")
for s, want in [("Bids due 20/10/2026.", "2026-10-20"),      # 20 cannot be a month
                ("Bids due 10/20/2026.", "2026-10-20"),      # nor can 20
                ("Bids due 05/05/2026.", "2026-05-05")]:     # both readings agree
    got = A.harvest_date(s, A._DEADLINE_CUES)
    ok(f"  {s:26} -> {want}", got == want, got)
    ok("    and is not called ambiguous", want not in A.ambiguous_at(s))

# ============================================================ validity dates
print("\n=== TRAP 2: a validity or licence expiry is not a deadline ===")
for c in ("expiration", "expires", "valid until", "open until"):
    ok(f"  {c!r} is not a deadline cue", c not in A._DEADLINE_CUES)

VALID = ("Sealed bids shall be dropped in the tender box before 1500 hrs on 09 November 2026. "
         "Prices quoted shall remain valid until 30 June 2027.")
ok("a price-validity date is never harvested as the deadline",
   A.harvest_date(VALID, A._DEADLINE_CUES) != "2027-06-30",
   A.harvest_date(VALID, A._DEADLINE_CUES))

# the same list decides a notice is already closed, so it could DELETE live work
LIVE = ("Bid submission: 20 November 2026 at 1400 hrs. A licence that expires before "
        "01 January 2025 will not be considered. " * 4)
verdict, detail = P.triage(LIVE, TODAY, None, "")
ok("a licence-expiry clause does not archive a live solicitation",
   verdict != "expired", f"{verdict} {detail}")

# ============================================================ nearest, not first
print("\n=== TRAP 3: the date a cue points AT, not the first one after it ===")
VISIT = ("Closing date: see the schedule in Annex B. A pre-bid site visit will be held on "
         "15 October 2026 at the chancery. Bids are due 28 November 2026 at 1500 hrs.")
ok("the site-visit date is not taken as the deadline",
   A.harvest_date(VISIT, A._DEADLINE_CUES) == "2026-11-28",
   A.harvest_date(VISIT, A._DEADLINE_CUES))

DELIVERY = ("Quotations are due 14 November 2026. Delivery shall be completed by "
            "30 June 2027. Period of performance: 1 January 2027 to 31 December 2027.")
ok("a delivery date does not win over a stated deadline",
   A.harvest_date(DELIVERY, A._DEADLINE_CUES) == "2026-11-14",
   A.harvest_date(DELIVERY, A._DEADLINE_CUES))

# ============================================================ the model's turn
print("\n=== the model is still asked about documents whose dates are numeric ===")
# date_windows used to walk the pattern list, which no longer holds the numeric
# form -- so a document written entirely in 10/12/2026 looked dateless and the
# model was never asked about precisely the notices that need it most.
NUM_DOC = ("U.S. Embassy supply notice. " * 400) + "Quotes must be received by 11/12/2026. " \
          + ("Standard terms and conditions apply. " * 400)
w = A.date_windows(NUM_DOC)
ok("the excerpt is not empty", bool(w), f"{len(w)} chars")
ok("it contains the numeric date", "11/12/2026" in w)
ok("and it is far smaller than the document", 0 < len(w) < len(NUM_DOC) / 4,
   f"{len(w)} vs {len(NUM_DOC)}")

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
