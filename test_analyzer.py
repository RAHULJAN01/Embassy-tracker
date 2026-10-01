#!/usr/bin/env python3
import analyzer as A

CONSTRUCTION_TEXT = ("U.S. EMBASSY ADDIS ABABA SOLICITATION 19ET1026C0003. The Contractor shall "
  "furnish performance and payment bonds (SF-25 / SF-25A) and shall perform all work on-site at "
  "the Embassy compound, Addis Ababa. Scope: renovation of the perimeter security wall.")

def fake(resp):
    return lambda prompt: resp

passed = 0; failed = 0
def check(name, cond):
    global passed, failed
    cond = bool(cond)
    print(("  OK  " if cond else "*FAIL*")+" "+name)
    passed += cond; failed += (not cond)

# 1. clean goods -> BID
r = A.adjudicate("U.S. Embassy Nairobi Request for Quotation. Supply and delivery of 120 office "
                 "chairs, desks and filing units to the Embassy compound. Full and open competition. "
                 "Commercial items, F.O.B. Destination Nairobi. Quotations are due 2026-10-20. Payment "
                 "net 30 after acceptance. Vendors must have an active SAM registration.",
  fake({"title":"Office furniture","sol":"19KE0001","tier":"BID","confidence":0.9,
        "classification":"Commercial Goods","shipping":"F.O.B. Destination","restrictions":[]}))
check("clean goods -> BID", r["tier"]=="BID")

# 2. boilerplate trade license -> MID with challenge
r = A.adjudicate("U.S. Embassy Kampala Request for Quotation. Supply of toilet tissue, paper towels "
                 "and related hygiene paper products for the mission. A valid trade/import license is "
                 "required to participate. Full and open competition. Delivered to post, net 30.",
  fake({"title":"Paper products","sol":"19UG0031","tier":"MID","confidence":0.8,
        "restrictions":[{"text":"Trade license demanded for COTS paper","kind":"boiler","note":"boilerplate"}],
        "route":"Ask the CO via Q&A whether the licence is truly required.",
        "challenge_draft":"Request for clarification ..."}))
check("boilerplate -> MID", r["tier"]=="MID" and r["restrictions"][0]["kind"]=="boiler" and r["challenge_draft"])

# 3. NO-BID with a REAL citation that exists in the text -> stays NO
r = A.adjudicate(CONSTRUCTION_TEXT,
  fake({"title":"Perimeter wall","sol":"19ET1026C0003","tier":"NO","confidence":0.95,
        "citation":{"doc":"SF-1442 Section I","quote":"The Contractor shall furnish performance and payment bonds (SF-25 / SF-25A) and shall perform all work on-site"}}))
check("NO with verified citation -> NO", r["tier"]=="NO" and r["citation"] is not None)

# 4. NO-BID with a HALLUCINATED citation (not in text) -> downgraded to REVIEW
r = A.adjudicate(CONSTRUCTION_TEXT,
  fake({"title":"Perimeter wall","sol":"19ET1026C0003","tier":"NO","confidence":0.95,
        "citation":{"doc":"Section X","quote":"Offerors must hold a top-secret facility clearance and ISO 9001 certification issued in Belgium"}}))
check("NO with fake citation -> REVIEW", r["tier"]=="REVIEW" and r["citation"] is None and "not found" in r["review_reason"])

# 5. low confidence -> REVIEW
r = A.adjudicate("U.S. Embassy notice with ambiguous and unclear terms, no clear classification, and "
                 "a mix of goods and services that could not be confidently categorized from the text "
                 "provided here for this particular requirement at the mission for the coming period.",
  fake({"title":"Unclear","sol":"X1","tier":"BID","confidence":0.3,"restrictions":[]}))
check("low confidence -> REVIEW", r["tier"]=="REVIEW" and "confidence" in r["review_reason"])

# 6. too-short text -> REVIEW (no AI call needed)
r = A.adjudicate("RFQ.", fake({"tier":"BID","confidence":0.9}))
check("short text -> REVIEW", r["tier"]=="REVIEW")

# 7. verify_citation unit
check("verify real quote true", A.verify_citation("perform all work on-site at the Embassy compound", CONSTRUCTION_TEXT))
check("verify fake quote false", not A.verify_citation("must hold a facility clearance issued in Belgium", CONSTRUCTION_TEXT))

# 8. evidence snippet always attached
check("evidence stored", bool(r.get("_evidence") is not None))

print(f"\n{passed} passed, {failed} failed")
assert failed==0, "some checks failed"
print("ALL ADJUDICATOR TESTS PASSED")
