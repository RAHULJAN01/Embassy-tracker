#!/usr/bin/env python3
"""test_nobid.py — a refusal must be earned.

Refusing a contract M&M could have won is the only error in this system that
costs real money, and it is silent: the solicitation simply sits in SECTION III
and nobody ever looks at it again. Four of the first five no-bids on the live
register were wrong — a website menu, a deadline, and twice the word "local".

Every one of those is a standing test here.
"""
import sys, pathlib

SRC = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import analyzer as A          # noqa: E402

FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


# ============================================================ THE REAL FAILURES
print("\n=== The four that actually happened, on the live register ===")
REAL = [
    ("19KE5026Q0127 — cited a website menu",
     "Economic Opportunity Commercial Opportunities Request for Proposals"),
    ("19BY7026Q0017 — cited a deadline",
     "Quotations are due by September 14, 2026. No quotations will be accepted after this time."),
    ("19CT2026Q0005 — cited 'local qualified Supplier'",
     "The U.S. Embassy Bangui requires the services of a local qualified Supplier to provide "
     "a regular procurement and delivery of bottled water to support the mission."),
    ("19CT2026Q0004 — cited 'local fuel contractor'",
     "The U.S. Embassy Bangui requires the services of a local fuel contractor with proven "
     "experience and logistics capabilities to deliver diesel and gasoline."),
]
for label, quote in REAL:
    good, why = A.citation_excludes(quote)
    ok(label, not good, why[:72])

# ============================================================ STILL REFUSE REAL BARS
print("\n=== A genuine bar must still be refused ===")
GENUINE = [
    ("U.S. small-business set-aside",
     "This procurement is a total small business set-aside under FAR 52.219-6 and only "
     "U.S. small business concerns may submit an offer."),
    ("8(a) only",
     "This acquisition is limited to 8(a) certified small disadvantaged business concerns."),
    ("HUBZone", "Award is restricted to HUBZone certified firms only; others are not eligible."),
    ("SDVOSB", "This requirement is reserved exclusively for service-disabled veteran-owned "
               "small businesses and other offerors will not be considered."),
    ("host-country nationals only",
     "Offers are restricted to firms that are nationals of and registered in the Republic of Kenya."),
    ("ITAR", "The items are ITAR export-controlled military components and may not be "
             "supplied by foreign entities."),
]
for label, quote in GENUINE:
    good, why = A.citation_excludes(quote)
    ok(label + " is still a no-bid", good, why[:72])

# ============================================================ THE RULE
print("\n=== 'Local' is conditional, never a refusal — Rahul's own rule ===")
for q in ["The contractor must be a locally licensed waste handler.",
          "Bidders must hold a valid in-country business registration.",
          "Only firms with a local office may perform the work.",
          "The supplier shall be a domestic contractor registered for VAT."]:
    good, why = A.citation_excludes(q)
    ok(f"  '{q[:46]}…'", not good, why[:60])

# ============================================================ WEBSITE CHROME
print("\n=== Website furniture never reaches the model ===")
RAW = """Economic Opportunity
Commercial Opportunities
Doing Business in Kenya
Skip to main content
UNDERGROUND STORAGE WATER TANK CLEANING - ROSSLYN RIDGE
The U.S. Embassy Nairobi requires cleaning of two underground storage tanks.
Quotations are due 15 October 2026.
U.S. Citizen Services
Contact Us
"""
c = A.clean_source_text(RAW)
for junk in ("Economic Opportunity", "Commercial Opportunities", "Skip to main content",
             "U.S. Citizen Services", "Contact Us"):
    ok(f"  stripped: {junk}", junk not in c)
for keep in ("UNDERGROUND STORAGE WATER TANK", "two underground storage tanks",
             "15 October 2026"):
    ok(f"  kept: {keep[:40]}", keep in c)

# ============================================================ END TO END
print("\n=== The whole gate, end to end ===")
TEXT = ("Economic Opportunity Commercial Opportunities Request for Proposals\n"
        "UNDERGROUND STORAGE WATER TANK CLEANING - ROSSLYN RIDGE\n"
        "The U.S. Embassy Nairobi requires cleaning of underground storage water tanks at the "
        "Rosslyn Ridge compound. Quotations are due 15 October 2026. " * 4)


def ai_says_no(prompt):
    return {"tier": "NO", "confidence": 0.9, "title": "Underground tank cleaning",
            "closing": "2026-10-15", "sector": "SERVICES",
            "citation": {"doc": "RFQ",
                         "quote": "Economic Opportunity Commercial Opportunities Request for Proposals"},
            "restrictions": [], "route": ""}


rec = A.adjudicate(TEXT, ai_says_no, today="2026-10-02")
ok("a no-bid built on a menu heading is refused", rec["tier"] != "NO", rec["tier"])
# Either gate is a correct outcome: the chrome stripper removes the menu text
# before adjudication, so the quote no longer exists in the source and the
# verbatim check rejects it first. Belt and braces — what matters is that the
# refusal does not stand and the reason is stated.
_r = (rec.get("review_reason") or "").lower()
ok("and the reason is recorded in plain words",
   ("navigation" in _r) or ("not found verbatim" in _r), rec.get("review_reason", "")[:70])

TEXT2 = ("This procurement is a total small business set-aside under FAR 52.219-6 and only U.S. "
         "small business concerns may submit an offer. " * 6)


def ai_says_no2(prompt):
    return {"tier": "NO", "confidence": 0.9, "title": "Janitorial", "closing": "2026-11-11",
            "sector": "SERVICES",
            "citation": {"doc": "RFQ", "quote":
                         "This procurement is a total small business set-aside under FAR 52.219-6 "
                         "and only U.S. small business concerns may submit an offer."},
            "restrictions": [], "route": ""}


rec2 = A.adjudicate(TEXT2, ai_says_no2, today="2026-10-02")
ok("a real set-aside is still a no-bid", rec2["tier"] == "NO", rec2["tier"])
ok("and keeps its cited clause", bool(rec2.get("citation")))

# ============================================================ SECOND OPINION
print("\n=== The heavier model gets the last word on a refusal ===")
LOCAL = ("The U.S. Embassy Bangui requires the services of a local qualified Supplier to "
         "provide bottled water to the mission. " * 6)
rec3 = {"tier": "NO", "review_reason": "local supplier required", "route": "",
        "citation": {"doc": "RFQ", "quote": "requires the services of a local qualified Supplier"}}
r3, changed = A.second_opinion(dict(rec3), LOCAL, lambda p, model=None: {
    "upheld": False, "tier": "MID", "citation": None,
    "route": "Engage a licensed Bangui supplier as subcontractor; M&M primes.",
    "why": "The only obstacle is a local supplier requirement, which a partner satisfies."})
ok("a wrong refusal is overturned", changed and r3["tier"] == "MID", r3["tier"])
ok("and a concrete route is recorded", "Bangui" in (r3.get("route") or ""))

SETASIDE = "This procurement is a total small business set-aside and only U.S. small businesses may submit. " * 8
rec4 = {"tier": "NO", "review_reason": "set-aside",
        "citation": {"doc": "RFQ", "quote": "total small business set-aside"}}
r4, ch4 = A.second_opinion(dict(rec4), SETASIDE, lambda p, model=None: {
    "upheld": True, "tier": "NO",
    "citation": {"doc": "RFQ", "quote":
                 "This procurement is a total small business set-aside and only U.S. small "
                 "businesses may submit."},
    "why": "A genuine U.S. small-business set-aside."})
ok("a correct refusal is upheld", (not ch4) and r4["tier"] == "NO", r4["tier"])
ok("the second opinion is recorded either way",
   bool(r3.get("second_opinion")) and bool(r4.get("second_opinion")))

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
