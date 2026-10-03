#!/usr/bin/env python3
"""test_dates.py — a date must be provable, or it does not exist.

The register once showed a BID with a closing date of 2026-10-12, a posted date
and a Q&A date, on a notice whose captured text contained NO DATES AT ALL. All
three were invented by the model, and the record was then marked VERIFIED
*because* a deadline was present. A wrong deadline is worse than none: it sends
you to a date that does not exist, or hides one that does.

Two faults caused it and both are tested here:
  1. fallbacks that promoted any stray date in the document to "the deadline"
  2. nothing checking that a model-supplied date appears in the source at all
"""
import sys, pathlib

SRC = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import analyzer as A, crawler as C          # noqa: E402

FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


TODAY = "2026-10-02"

# ============================================================ THE REAL FAILURE
print("\n=== PR15305534: the notice had no dates; the model invented three ===")
NODATES = ("Request for Solicitations: Gate Spare Parts Supply - PR15305534. "
           "Items being acquired: control modules, drive wheels, sensors, switches, "
           "inverters and batteries. All questions should be sent to "
           "BujProcurement@state.gov four days prior to closing. ") * 6


def invents(prompt):
    return {"tier": "BID", "confidence": 0.92, "title": "Gate Spare Parts Supply",
            "sol": "PR15305534", "closing": "2026-10-12", "posted": "2026-10-02",
            "qa_due": "2026-10-08", "sector": "COTS", "restrictions": [], "route": "dealer"}


rec = A.adjudicate(NODATES, invents, today=TODAY)
ok("the invented closing date is discarded", rec["closing"] == "", repr(rec["closing"]))
ok("the invented posted date is discarded", rec["posted"] == "", repr(rec["posted"]))
ok("the invented Q&A date is discarded", rec["qa_due"] == "", repr(rec["qa_due"]))
ok("and it is written down why",
   any("does not appear" in w for w in rec.get("date_warnings", [])),
   (rec.get("date_warnings") or [""])[0][:70])
v, why = C.verification_state(rec, 1, 0)
ok("the record is NOT VERIFIED on an invented date", v == "UNVERIFIED", v)

# ============================================================ NO INVENTION
print("\n=== No stray date is ever promoted to a deadline ===")
for label, body in [
    ("a delivery date", "Work shall be completed by 30 June 2027. " * 8),
    ("a company history date", "The contractor was established on 14 March 2019. " * 8),
    ("a period of performance", "Period of performance: 1 January 2027 to 31 December 2027. " * 6),
    ("a reference to a meeting", "A pre-bid meeting was held on 2 February 2026. " * 8),
]:
    r = A.adjudicate(body, lambda p: {"tier": "BID", "confidence": 0.9, "title": "X",
                                      "sector": "COTS", "closing": "", "restrictions": [],
                                      "route": "x"}, today=TODAY)
    ok(f"  {label} does not become the deadline", r["closing"] == "", repr(r["closing"]))

# ===================================== EXISTING IN THE DOC IS NOT A DEADLINE
# The gate-parts record. The document really did contain the date the portal
# showed — as a DELIVERY date. Existence was the only test it had to pass, and
# it passed, and the notice had actually closed in 2025.
print("\n=== A date must be CALLED a deadline, not merely be present ===")
for label, body, bad in [
    ("a delivery date the model called the deadline",
     "Gate spare parts. Delivery shall be completed by 30 June 2027 at the latest. " * 6,
     "2027-06-30"),
    ("a performance period end",
     "Period of performance: 1 January 2027 through 31 December 2027. " * 6, "2027-12-31"),
    ("a warranty expiry",
     "All parts carry a warranty valid through 15 August 2027. " * 6, "2027-08-15"),
]:
    r = A.adjudicate(body, lambda p, b=bad: {"tier": "BID", "confidence": 0.95, "title": "Gate parts",
                                             "closing": b, "sector": "COTS",
                                             "restrictions": [], "route": "x"}, today=TODAY)
    ok(f"  {label} is refused", r["closing"] == "", repr(r["closing"]))
    ok("    and the reason says so",
       any("not on any line that calls it a deadline" in w
           for w in r.get("date_warnings", [])),
       (r.get("date_warnings") or [""])[0][:80])
    vv, _ = C.verification_state(r, 1, 0)
    ok("    and the record is not VERIFIED", vv == "UNVERIFIED", vv)

# ============================================ THE READER, NOT THE PHRASE LIST
# Rahul's objection, and he was right: "THE WORD DEADLINE CAN BE WRITTEN IN MANY
# WAYS". A tuple of cue phrases can never cover them all. So the model now
# quotes the words it read the date from, and the code only checks the quote is
# really in the document and really contains the date.
#
# Every phrasing below is deliberately chosen to contain NO phrase from
# _DEADLINE_CUES. Each one must still work. If someone ever puts the phrase list
# back in charge, these fail.
print("\n=== A deadline is read, not pattern-matched (none of these hit a cue) ===")
ODD = [
    ("in our hands by",
     "Bids must be in our hands by 1600 hrs on 12.10.2026 and will be opened the next "
     "morning in the presence of bidders. "),
    ("tender box closes",
     "The tender box at the chancery gate is sealed at 15:00 on 12 October 2026. "),
    ("nothing entertained",
     "Nothing submitted past 12/10/2026 shall be entertained by the contracting officer. "),
    ("window shuts",
     "The submission window shuts 12-Oct-26 at close of play; late entries are returned "
     "unopened. "),
    ("table row",
     "Procurement schedule\nSite visit | 2026-09-20\nSubmission | 2026-10-12\n"
     "Award | 2026-11-01\n"),
    ("hindi-english mix",
     "Quotation jama karne ki antim tithi 12 October 2026 hai. "),
]
for label, line in ODD:
    body = line * 4
    # the phrase list must genuinely be useless here, or the test proves nothing
    assert not A.harvest_date(body, A._DEADLINE_CUES), f"{label}: a cue leaked in"
    r = A.adjudicate(body, lambda p, q=line: {
        "tier": "BID", "confidence": 0.9, "title": "X", "sector": "COTS",
        "closing": "2026-10-12", "closing_quote": q.strip(),
        "restrictions": [], "route": "x"}, today=TODAY)
    ok(f"  {label}", r["closing"] == "2026-10-12", repr(r["closing"]))
    ok("    on the strength of the quote, not a phrase",
       (r.get("date_proof") or {}).get("closing") == "quoted",
       str(r.get("date_proof")))
    ok("    and nothing is flagged", not r.get("date_warnings"),
       str(r.get("date_warnings"))[:60])

print("\n=== A quote the document does not contain is still refused ===")
GOOD_BODY = "Bids must be in our hands by 1600 hrs on 12.10.2026. " * 4
for label, iso, quote in [
    ("an invented sentence", "2026-10-12",
     "Offers are due by 12 October 2026 at 1600 hours local time."),
    ("a real sentence with the date swapped", "2027-10-12",
     "Bids must be in our hands by 1600 hrs on 12.10.2027."),
    ("a quote with no date in it", "2026-10-12",
     "Bids must be in our hands by"),
]:
    r = A.adjudicate(GOOD_BODY, lambda p, i=iso, q=quote: {
        "tier": "BID", "confidence": 0.95, "title": "X", "sector": "COTS",
        "closing": i, "closing_quote": q, "restrictions": [], "route": "x"}, today=TODAY)
    # the honest date is still in the document, so phrase-matching may rescue a
    # correct date -- what must never happen is the FABRICATED one surviving
    ok(f"  {label} does not survive as given",
       r["closing"] != iso or iso == "2026-10-12", repr(r["closing"]))
    if r["closing"] == "2026-10-12":
        ok("    the rescued date is marked as phrase-matched, not quoted",
           (r.get("date_proof") or {}).get("closing") == "phrase", str(r.get("date_proof")))

# ============================================================ REAL DATES KEPT
print("\n=== A real, stated deadline is kept — with the line it came from ===")
REAL = ("The U.S. Embassy Bujumbura requires gate spare parts. "
        "Quotations are due by 12 October 2026 at 1600 hours local time. "
        "Questions must be submitted by 8 October 2026. ") * 5


def honest(prompt):
    return {"tier": "BID", "confidence": 0.9, "title": "Gate Spare Parts", "sol": "PR15305534",
            "closing": "2026-10-12", "qa_due": "2026-10-08", "sector": "COTS",
            "restrictions": [], "route": "x"}


r2 = A.adjudicate(REAL, honest, today=TODAY)
ok("the stated closing date is kept", r2["closing"] == "2026-10-12", r2["closing"])
ok("the stated Q&A date is kept", r2["qa_due"] == "2026-10-08", r2["qa_due"])
ok("the closing date carries its source line", bool(r2["date_evidence"].get("closing")))
print("      proof:", (r2["date_evidence"].get("closing") or "")[:88])
v2, _ = C.verification_state(r2, 1, 0)
ok("so this record CAN be verified", v2 == "VERIFIED", v2)

# ============================================================ FORMATS
print("\n=== Dates the bots must be able to prove, in every format ===")
for label, written, iso in [
    ("12 October 2026", "Offers are due 12 October 2026.", "2026-10-12"),
    ("October 12, 2026", "Offers are due October 12, 2026.", "2026-10-12"),
    ("2026-10-12", "Closing date: 2026-10-12.", "2026-10-12"),
    ("12/10/2026 day-first", "Closing date: 12/10/2026.", "2026-10-12"),
    ("12.10.2026 dotted", "Closing date: 12.10.2026.", "2026-10-12"),
    ("12-Oct-26 short year", "Closing date: 12-Oct-26.", "2026-10-12"),
]:
    found, ev = A.date_in_text(iso, written * 4)
    ok(f"  {label}", found, ev[:52])

print("\n=== A date NOT in the document is never accepted ===")
for iso in ("2026-10-12", "2027-01-01", "2025-05-05"):
    found, _ = A.date_in_text(iso, "This notice contains no dates whatsoever. " * 10)
    ok(f"  {iso} is refused", not found)

# ============================================================ SANITY
print("\n=== Impossible date combinations are flagged ===")
BACK = ("Quotations are due by 1 March 2026. This notice was issued on 15 June 2026. ") * 5


def backwards(prompt):
    return {"tier": "BID", "confidence": 0.9, "title": "X", "sector": "COTS",
            "closing": "2026-03-01", "posted": "2026-06-15", "restrictions": [], "route": "x"}


r3 = A.adjudicate(BACK, backwards, today=TODAY)
ok("a closing date before the posted date is flagged",
   any("before the posted date" in w for w in r3.get("date_warnings", [])),
   (r3.get("date_warnings") or [""])[-1][:70])
v3, why3 = C.verification_state(r3, 1, 0)
ok("and it cannot be VERIFIED", v3 == "UNVERIFIED", v3)

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
