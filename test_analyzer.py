#!/usr/bin/env python3
import analyzer as A

CONSTRUCTION_TEXT = ("U.S. EMBASSY ADDIS ABABA SOLICITATION 19ET1026C0003. The Contractor shall "
  "furnish performance and payment bonds (SF-25 / SF-25A) and shall perform all work on-site at "
  "the Embassy compound, Addis Ababa. Scope: renovation of the perimeter security wall. "
  "Offers are due 20 November 2026. Issue date: 2026-09-15.")

ARMS_TEXT = ("U.S. Embassy solicitation for the supply of 5.56mm ammunition and small arms spare "
  "parts for the Marine Security Guard detachment. These items are controlled under ITAR and the "
  "International Traffic in Arms Regulations and may not be exported without a license. "
  "Closing date: 2026-12-01.")

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
  fake({"title":"Office furniture","sol":"19KE0001","tier":"BID","confidence":0.9,"sector":"COTS",
        "classification":"Commercial Goods","shipping":"F.O.B. Destination","restrictions":[]}))
check("clean goods -> BID", r["tier"]=="BID" and r["sector"]=="COTS")

# 2. boilerplate trade license -> MID with challenge
r = A.adjudicate("U.S. Embassy Kampala Request for Quotation. Supply of toilet tissue, paper towels "
                 "and related hygiene paper products for the mission. A valid trade/import license is "
                 "required to participate. Full and open competition. Delivered to post, net 30.",
  fake({"title":"Paper products","sol":"19UG0031","tier":"MID","confidence":0.8,"sector":"COTS",
        "restrictions":[{"text":"Trade license demanded for COTS paper","kind":"boiler","note":"boilerplate"}],
        "route":"Ask the CO via Q&A whether the licence is truly required.",
        "challenge_draft":"Request for clarification ..."}))
check("boilerplate -> MID", r["tier"]=="MID" and r["restrictions"][0]["kind"]=="boiler" and r["challenge_draft"])

# 3. v2: CONSTRUCTION is no longer auto-fatal — it may be MID via a local partner
r = A.adjudicate(CONSTRUCTION_TEXT,
  fake({"title":"Perimeter wall","sol":"19ET1026C0003","tier":"MID","confidence":0.8,
        "sector":"CONSTRUCTION",
        "route":"Team with a licensed Addis contractor who holds bonding; M&M primes.",
        "restrictions":[{"text":"Bonds + on-site work","kind":"fix","note":"local partner"}]}))
check("construction -> MID (not auto-fatal)", r["tier"]=="MID" and r["sector"]=="CONSTRUCTION")

# 4. real fatal trigger (ITAR) with a verified citation -> stays NO
r = A.adjudicate(ARMS_TEXT,
  fake({"title":"Ammunition","sol":"ARM1","tier":"NO","confidence":0.95,"sector":"COTS",
        "citation":{"doc":"RFQ","quote":"These items are controlled under ITAR and the International Traffic in Arms Regulations"}}))
check("ITAR -> NO with citation", r["tier"]=="NO" and r["citation"] is not None)

# 5. NO-BID with a HALLUCINATED citation -> downgraded to REVIEW
r = A.adjudicate(CONSTRUCTION_TEXT,
  fake({"title":"Perimeter wall","sol":"19ET1026C0003","tier":"NO","confidence":0.95,
        "citation":{"doc":"Section X","quote":"Offerors must hold a top-secret facility clearance issued in Belgium"}}))
check("fake citation -> REVIEW", r["tier"]=="REVIEW" and r["citation"] is None and "not found" in r["review_reason"])

# 6. low confidence -> REVIEW
r = A.adjudicate("U.S. Embassy notice with ambiguous and unclear terms, no clear classification, and "
                 "a mix of goods and services that could not be confidently categorized from the text "
                 "provided here for this particular requirement at the mission for the coming period.",
  fake({"title":"Unclear","sol":"X1","tier":"BID","confidence":0.3,"restrictions":[]}))
check("low confidence -> REVIEW", r["tier"]=="REVIEW" and "confidence" in r["review_reason"])

# 7. too-short text -> REVIEW
r = A.adjudicate("RFQ.", fake({"tier":"BID","confidence":0.9}))
check("short text -> REVIEW", r["tier"]=="REVIEW")

# 8. citation verifier unit
check("verify real quote true", A.verify_citation("perform all work on-site at the Embassy compound", CONSTRUCTION_TEXT))
check("verify fake quote false", not A.verify_citation("must hold a facility clearance issued in Belgium", CONSTRUCTION_TEXT))

# 9. DATE HARVESTING — model returned no dates, code must back-fill from the text
r = A.adjudicate(CONSTRUCTION_TEXT,
  fake({"title":"Perimeter wall","sol":"19ET1026C0003","tier":"MID","confidence":0.8,
        "sector":"CONSTRUCTION","posted":"","closing":"","qa_due":""}))
check("closing date harvested from text", r["closing"]=="2026-11-20")
check("posted date harvested from text", r["posted"]=="2026-09-15")

# 10. date parser formats
check("ymd parse", "2026-10-20" in A.find_dates("due 2026-10-20 ok"))
check("dMy parse", "2026-11-20" in A.find_dates("due 20 November 2026 ok"))
check("Mdy parse", "2026-11-20" in A.find_dates("due November 20, 2026 ok"))
check("dmy parse", "2026-03-04" in A.find_dates("due 04/03/2026 ok"))

# 11. sector fallback when model omits it
check("sector guess construction", A.guess_sector("renovation and civil works construction of the wall")=="CONSTRUCTION")
check("sector guess services", A.guess_sector("provision of cleaning services and maintenance services")=="SERVICES")
check("sector guess cots", A.guess_sector("supply and delivery of equipment and furniture goods")=="COTS")

# 12. evidence snippet always attached
check("evidence stored", bool(r.get("_evidence")))

print(f"\n{passed} passed, {failed} failed")
assert failed==0, "some checks failed"
print("ALL ADJUDICATOR v2 TESTS PASSED")
