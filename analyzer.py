#!/usr/bin/env python3
"""
Madison & Main — Solicitation Adjudicator v2 (the brain)
========================================================
Takes the full text of ONE solicitation (page + ALL its attachments, merged) and
returns a complete dossier record.

v2 changes (per Rahul's direction):
  * ALL SECTORS. COTS, Services AND Construction are assessed. Construction /
    on-site services are NO LONGER automatic fatal triggers — they run the same
    gate as everything else:
        "Am I eligible? -> NO = NO-BID (cite the blocker).
                           YES = how do I fulfil it?"
    Services/Construction are fulfilled via a local partner / subcontractor / JV
    with M&M as prime. Harder bar -> usually MID (conditional on partner), but
    never skipped.
  * Encodes the M&M 4-stage framework: Fatal Triggers -> Core Fit -> Gap
    Resolution -> Margin & Sanity.
  * Every record carries a `sector` (COTS | SERVICES | CONSTRUCTION) and the
    dates are mandatory.

Accuracy is enforced in code, not trusted to the model:
  * a NO-BID is only allowed if its cited quote actually EXISTS in the source
    text (anti-hallucination). If it can't be verified -> REVIEW.
  * low confidence or unreadable text -> REVIEW (never a guessed tier).
  * the raw evidence snippet is stored on every record for audit.
"""
import re, json, difflib, datetime

TIERS = {"BID", "MID", "NO", "REVIEW"}
SECTORS = {"COTS", "SERVICES", "CONSTRUCTION", "MIXED", ""}

ADJUDICATE_PROMPT = (
    "You are a U.S. federal / UN procurement analyst adjudicating ONE solicitation for "
    "Madison & Main LLC.\n\n"
    "WHO M&M IS: a U.S. LLC that SOURCES goods and ships them F.O.B. Destination to overseas "
    "U.S. missions and UN agencies (Dubai logistics hub). Foreign-owned. No in-house workforce, "
    "no bonding capacity of its own, no in-country tax registration. BUT it CAN act as prime and "
    "fulfil through others: an authorized-dealer/manufacturer letter, a supplier, a local "
    "subcontractor, an installer, or a teaming/JV partner.\n\n"
    "POLICY: M&M pursues EVERY sector — commercial goods (COTS), SERVICES and CONSTRUCTION. "
    "Construction and on-site services are NOT automatically disqualifying. The only question is:\n"
    "  (1) Is M&M ELIGIBLE to hold this contract? If not -> NO.\n"
    "  (2) If eligible, HOW is it fulfilled? Name the concrete route.\n\n"
    "STAGE 1 — TRUE FATAL TRIGGERS (any one = tier NO, quote the clause VERBATIM):\n"
    "  * ITAR / EAR / embargoed goods: weapons, ammunition, military-specific or dual-use tech.\n"
    "  * A set-aside M&M cannot qualify for (U.S. small-business, 8(a), SDVOSB, HUBZone, "
    "or a local/national set-aside restricted to host-country firms).\n"
    "  * The requirement cannot be sourced or fulfilled by anyone M&M could engage.\n"
    "  * No margin is possible (landed cost >= the competitive price).\n"
    "  * Illegal / sanctioned counterparty or destination.\n"
    "  NOTE: bonding, on-site labour, installation, local licences, past-performance demands and "
    "RFP narrative scoring are NOT fatal. They are GAPS -> handle them in Stage 3.\n\n"
    "REGISTRATION IS NOT A RESTRICTION — READ THIS CAREFULLY:\n"
    "  M&M already holds UEI XWL3YN7QNKT7 and an EIN, and its SAM.gov registration is in "
    "progress. A clause requiring SAM.gov registration (FAR 52.204-7), an active SAM record, "
    "a UEI, a CAGE code, an NCAGE code, or registration on a buyer's own vendor portal is "
    "ROUTINE ADMINISTRATIVE BOILERPLATE that appears on virtually every U.S. government "
    "solicitation. It is paperwork M&M is already completing, not a barrier.\n"
    "  * NEVER let a registration requirement make the tier MID or NO.\n"
    "  * NEVER list it as a controlling restriction. If you list it at all, use kind \"boiler\".\n"
    "  * Judge the solicitation as if the registration were already active.\n"
    "  * The ONLY time it matters is if the closing date is so near that registration could not "
    "be active in time — in that case mention it under gotchas, and still do not change the tier.\n\n"
    "SERVICES & CONSTRUCTION — decide experience exactly like this:\n"
    "  * No past-performance/experience requirement at all -> BID.\n"
    "  * Experience required BUT the solicitation allows a subcontractor's, partner's, JV's or\n"
    "    affiliate's experience to be relied on -> BID (name the route).\n"
    "  * Experience required and it MUST be the PRIME's own (explicitly 'the offeror itself',\n"
    "    'the prime contractor's own experience', no subcontractor substitution allowed) -> NO.\n"
    "  * Requires something M&M cannot obtain at all — a host-country licence, local registration,\n"
    "    in-country tax ID held BY THE PRIME, a local pass/permit, or nationality/locality\n"
    "    restriction -> NO.\n"
    "  * Requires it but a local partner could legitimately hold it on the contract -> MID.\n\n"
    "STAGE 2 — CORE FIT (judge by sector):\n"
    "  COTS: commercial item, clean Incoterms (CIF/DAP/CPT) to port/depot/dock, USD payment, "
    "customs cleared by the mission's diplomatic status, workable deadline, real competition.\n"
    "  SERVICES / CONSTRUCTION: can a qualified LOCAL partner/subcontractor perform it? Is the "
    "licence/registration obtainable or held by that partner? Is bonding obtainable or waived? "
    "Is the scope definable and priceable? Is the timeline real?\n\n"
    "STAGE 3 — GAP RESOLUTION (bridge missing points if executable before the deadline):\n"
    "  * past performance missing -> supplier/subcontractor pass-through, teaming agreement, "
    "commercial B2B references, or principal's supply-chain resume.\n"
    "  * OEM/dealer authorization required -> 1-page authorization letter signed by the regional "
    "distributor.\n"
    "  * on-site assembly / installation / labour -> local technician or contractor at a flat rate, "
    "priced into the bid.\n"
    "  * bonding / local licence / in-country tax ID -> a local partner or JV who holds it.\n"
    "  * restrictive boilerplate on a plain commercial buy -> a Q&A-period challenge to the CO "
    "(cite FAR 52.212-1 flexibility / Advocate for Competition).\n"
    "  If the gap is bridgeable before the deadline -> tier MID and state the exact route.\n\n"
    "STAGE 4 — MARGIN & SANITY: landed cost knowable, margin plausible (~15-20%), timeline beats "
    "the delivery date. If margin is impossible -> NO.\n\n"
    "TIERS:\n"
    "  BID = eligible, fulfillable as-is, no controlling gap. Ready to quote.\n"
    "  MID = winnable but ONE thing must be fixed first (a Stage 3 route). State the route.\n"
    "  NO  = a Stage 1 fatal trigger. You MUST quote the controlling sentence VERBATIM.\n"
    "STANDING RULE: when torn between BID and MID, prefer the one whose route you can actually name. "
    "When torn between MID and NO with no fatal trigger present, choose MID.\n\n"
    "DATES ARE MANDATORY: find posted / closing(deadline) / Q&A-due dates in the text. Convert any "
    "format to YYYY-MM-DD. If a date genuinely is not stated anywhere, return \"\" for it.\n\n"
    "WRITE THE BRIEF LIKE A HUMAN WOULD SAY IT. `brief` is one plain sentence a busy person "
    "can read in two seconds and know whether to care: who is buying, what exactly, how many, "
    "and by when. Example: \"U.S. Embassy Kathmandu wants 120 office chairs and 60 desks "
    "delivered to the chancery, quotes close 15 Dec 2026.\" No jargon, no restating the title.\n\n"
    "Return ONLY a JSON object with keys:\n"
    '  brief (one plain sentence, as described above),\n'
    '  title, sol, posted (YYYY-MM-DD|""), closing (YYYY-MM-DD|""), qa_due (YYYY-MM-DD|""),\n'
    '  sector ("COTS"|"SERVICES"|"CONSTRUCTION"|"MIXED"),\n'
    '  classification, scope, est_value, shipping, payment, ship_after, setaside, license,\n'
    '  line_items (array of strings: the actual things wanted WITH quantities, e.g.\n'
    '      "120 x ergonomic task chair, mesh back"; [] if the notice never itemises),\n'
    '  submit_how (how a quote is submitted: email / portal / hand delivery, with the address\n'
    '      or URL if stated; "" if not stated),\n'
    '  submit_to (the contracting officer, office or email to send it to; "" if not stated),\n'
    '  submit_forms (array: specific forms or documents that MUST accompany the quote,\n'
    '      e.g. "SF-1449", "signed SF-18", "manufacturer authorization letter"),\n'
    '  award_basis (how they choose: "lowest price technically acceptable", "best value",\n'
    '      "trade-off", etc; "" if not stated),\n'
    '  gotchas (array of SHORT plain-language warnings about anything unusual, easy to miss,\n'
    '      or likely to disqualify a careless bidder; [] if nothing stands out),\n'
    '  docs (array of strings),\n'
    '  tier ("BID"|"MID"|"NO"),\n'
    '  restrictions (array of {text, kind:("real"|"boiler"|"fix"), note}),\n'
    '  route (string; the exact fulfilment/fix move for MID or BID, else ""),\n'
    '  challenge_draft (string; a short Q&A clarification to the CO for boilerplate, else ""),\n'
    '  citation ({doc, quote} with quote copied VERBATIM from the text for NO, else null),\n'
    '  confidence (0.0-1.0 for the tier decision).\n'
    "Use ONLY what the text states; never invent a date, value or quote. today is {today}.\n"
    "TEXT:\n{body}"
)

# ---------------------------------------------------------------- date harvesting
_MONTHS = ("january february march april may june july august september october "
           "november december").split()
_MON3 = [m[:3] for m in _MONTHS]

_DATE_PATTERNS = [
    # 2026-10-20 / 2026/10/20
    (re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b"), "ymd"),
    # 20-10-2026 / 20/10/2026  (day first — common outside the US)
    (re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b"), "dmy"),
    # 20 October 2026  /  20 Oct 2026
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(20\d{2})\b"), "dMy"),
    # October 20, 2026 / Oct 20 2026
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d{2})\b"), "Mdy"),
    # 18-Aug-25 / 18 Aug 25   (two-digit year)
    (re.compile(r"\b(\d{1,2})[-\s]([A-Za-z]{3,9})\.?[-\s](\d{2})\b(?!\d)"), "dMyy"),
    # 20.10.2026  (dotted, common in EU/UN docs)
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b"), "dmy"),
    # 2026.10.20
    (re.compile(r"\b(20\d{2})\.(\d{1,2})\.(\d{1,2})\b"), "ymd"),
]


def _mk(y, m, d):
    try:
        y, m, d = int(y), int(m), int(d)
        if not (1 <= m <= 12 and 1 <= d <= 31 and 2000 <= y <= 2100):
            return ""
        return datetime.date(y, m, d).isoformat()
    except Exception:
        return ""


def _month_num(name):
    n = name.lower().rstrip(".")
    if n in _MONTHS:
        return _MONTHS.index(n) + 1
    if n[:3] in _MON3:
        return _MON3.index(n[:3]) + 1
    return 0


def find_dates(text):
    """Return every parseable date in the text as ISO strings, in order of appearance."""
    out = []
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text or ""):
            a, b, c = m.group(1), m.group(2), m.group(3)
            if kind == "ymd":
                iso = _mk(a, b, c)
            elif kind == "dmy":
                iso = _mk(c, b, a)
            elif kind == "dMy":
                iso = _mk(c, _month_num(b), a)
            elif kind == "dMyy":
                mn = _month_num(b)
                iso = _mk("20" + c, mn, a) if mn else ""
            else:  # Mdy
                iso = _mk(c, _month_num(a), b)
            if iso:
                out.append((m.start(), iso))
    out.sort()
    return [iso for _, iso in out]


_DEADLINE_CUES = ("closing date", "close date", "due date", "offers are due", "quotations are due",
                  "proposals are due", "bids are due", "submission deadline", "deadline for",
                  "must be received", "no later than", "closing time", "response date",
                  "expiration", "expires", "last date")
_QA_CUES = ("questions are due", "q&a", "questions due", "clarification", "inquiries")
_POSTED_CUES = ("posted", "issue date", "issued on", "date of issue", "published", "release date")


def harvest_date(text, cues, window=180):
    """Find the date nearest to any of these cue phrases. Returns ISO or ''."""
    low = (text or "").lower()
    best = ""
    for cue in cues:
        i = low.find(cue)
        while i != -1:
            seg = text[i: i + window]
            ds = find_dates(seg)
            if ds:
                best = ds[0]
                return best
            i = low.find(cue, i + 1)
    return best


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def verify_citation(quote, source_text, min_frac=0.60):
    """True only if `quote` genuinely appears in `source_text`. Requires an exact
    normalized substring, or a single CONTIGUOUS shared block covering >= min_frac
    of the quote (tolerates minor whitespace/OCR drift, rejects invented quotes).
    This is the anti-hallucination gate for NO-BID."""
    q, src = _norm(quote), _norm(source_text)
    if len(q) < 15 or not src:
        return False
    if q in src:
        return True
    m = difflib.SequenceMatcher(None, q, src).find_longest_match(0, len(q), 0, len(src))
    return m.size >= max(25, int(min_frac * len(q)))


def _coerce(data):
    """Normalize the model's JSON into our record shape with safe defaults."""
    g = lambda k, d="": (data.get(k) if data.get(k) is not None else d)
    sector = str(g("sector", "")).upper().strip()
    if sector not in SECTORS:
        sector = ""
    rec = {
        "brief": str(g("brief"))[:300],
        "line_items": [str(x)[:120] for x in (g("line_items", []) or [])][:20],
        "submit_how": str(g("submit_how"))[:240],
        "submit_to": str(g("submit_to"))[:160],
        "submit_forms": [str(x)[:90] for x in (g("submit_forms", []) or [])][:12],
        "award_basis": str(g("award_basis"))[:120],
        "gotchas": [str(x)[:180] for x in (g("gotchas", []) or [])][:8],
        "title": str(g("title"))[:200], "sol": str(g("sol")).upper()[:60],
        "posted": str(g("posted"))[:10], "closing": str(g("closing"))[:10],
        "qa_due": str(g("qa_due"))[:10], "sector": sector,
        "classification": str(g("classification"))[:80],
        "scope": str(g("scope"))[:160], "est_value": str(g("est_value"))[:60],
        "shipping": str(g("shipping"))[:80], "payment": str(g("payment"))[:80],
        "ship_after": str(g("ship_after"))[:60], "setaside": str(g("setaside"))[:60],
        "license": str(g("license"))[:80], "docs": [str(x)[:80] for x in (g("docs", []) or [])][:12],
        "tier": str(g("tier", "REVIEW")).upper(), "route": str(g("route"))[:600],
        "challenge_draft": str(g("challenge_draft"))[:900],
        "confidence": float(g("confidence", 0) or 0),
        "restrictions": [], "citation": None,
    }
    for r in (g("restrictions", []) or [])[:6]:
        if isinstance(r, dict):
            k = str(r.get("kind", "")).lower()
            k = k if k in ("real", "boiler", "fix") else "real"
            rec["restrictions"].append({"text": str(r.get("text", ""))[:200], "kind": k,
                                        "note": str(r.get("note", ""))[:300]})
    c = g("citation", None)
    if isinstance(c, dict) and c.get("quote"):
        rec["citation"] = {"doc": str(c.get("doc", ""))[:160], "quote": str(c.get("quote", ""))[:400]}
    if rec["tier"] not in TIERS:
        rec["tier"] = "REVIEW"
    rec["confidence"] = max(0.0, min(1.0, rec["confidence"]))
    return rec


# Registration paperwork M&M is already completing. These words in a restriction
# never make something less biddable, and the model is not allowed to pretend
# otherwise — this is the deterministic backstop behind the prompt rule.
_REGISTRATION_RX = re.compile(
    r"\b(sam\.gov|system for award management|far\s*52\.204-7|unique entity (id|identifier)"
    r"|\buei\b|cage code|ncage|duns|vendor (portal )?registration|register(ed)? in sam)\b", re.I)


def declassify_registration(rec):
    """A SAM / UEI / CAGE / NCAGE clause is boilerplate, not a controlling restriction.

    Rahul: "remove the restriction which requires SAM registration, it is causing
    fully biddable ones to sit in BNB." He is right — it is paperwork already under
    way, and it was demoting work he can actually win.
    """
    moved = 0
    for r in rec.get("restrictions") or []:
        if r.get("kind") == "real" and _REGISTRATION_RX.search(r.get("text", "") or ""):
            r["kind"] = "boiler"
            r["note"] = ("Routine registration boilerplate — M&M holds a UEI and its SAM "
                         "record is in progress. Not a barrier to bidding.")
            moved += 1
    # If registration was the ONLY thing holding it at MID, it belongs at BID.
    if moved and rec.get("tier") == "MID":
        real_left = [r for r in (rec.get("restrictions") or []) if r.get("kind") == "real"]
        route = (rec.get("route") or "")
        route_is_registration = bool(route) and bool(_REGISTRATION_RX.search(route))
        if not real_left and (not route or route_is_registration):
            rec["tier"] = "BID"
            rec["promoted_from_mid"] = "registration paperwork is not a barrier"
    return rec


def guess_sector(text):
    """Cheap sector fallback when the model doesn't give one."""
    low = (text or "").lower()
    con = sum(low.count(w) for w in ("construction", "sf-1442", "renovation", "civil works",
                                     "build", "refurbish", "remodel", "masonry", "roofing"))
    svc = sum(low.count(w) for w in ("services", "maintenance", "cleaning", "security guard",
                                     "staffing", "consultancy", "training", "insurance", "catering"))
    good = sum(low.count(w) for w in ("supply", "delivery", "goods", "equipment", "furniture",
                                      "procurement of", "commodity", "f.o.b", "spare parts"))
    if con > max(svc, good):
        return "CONSTRUCTION"
    if svc > max(con, good):
        return "SERVICES"
    if good:
        return "COTS"
    return ""


def adjudicate(text, call_ai, today="", min_conf=0.5, min_chars=180):
    """Adjudicate one solicitation. `call_ai(prompt)` returns a parsed dict
    (or raises / returns {} on failure). Returns a directory record with the
    accuracy gates applied, dates harvested, and an evidence snippet attached."""
    body = re.sub(r"\s+", " ", text or "")
    evidence = body[:1500]
    if len(body) < min_chars:
        return {"tier": "REVIEW", "confidence": 0.0, "review_reason": "source text too short / unreadable",
                "restrictions": [], "citation": None, "_evidence": evidence, "sector": ""}
    prompt = ADJUDICATE_PROMPT.replace("{today}", today or "").replace("{body}", body[:18000])
    try:
        data = call_ai(prompt) or {}
    except Exception as e:
        if type(e).__name__ == "AllExhausted":
            raise
        return {"tier": "REVIEW", "confidence": 0.0, "review_reason": f"AI error: {str(e)[:80]}",
                "restrictions": [], "citation": None, "_evidence": evidence, "sector": ""}
    if data.get("_gerr"):
        return {"tier": "REVIEW", "confidence": 0.0, "review_reason": f"AI: {data['_gerr'][:80]}",
                "restrictions": [], "citation": None, "_evidence": evidence, "sector": ""}
    rec = _coerce(data)
    rec["_evidence"] = evidence
    rec["review_reason"] = ""
    declassify_registration(rec)      # registration paperwork never costs us a bid

    # --- DATES ARE MANDATORY: back-fill anything the model missed, from the raw text
    if not rec["closing"]:
        rec["closing"] = harvest_date(text, _DEADLINE_CUES)
    if not rec["posted"]:
        rec["posted"] = harvest_date(text, _POSTED_CUES)
    if not rec["qa_due"]:
        rec["qa_due"] = harvest_date(text, _QA_CUES)
    # titles often carry the deadline, e.g. "... (by August 18, 2025)"
    if not rec["closing"] and rec.get("title"):
        td = find_dates(rec["title"])
        if td:
            rec["closing"] = td[-1]
    # last resort: the soonest future date anywhere in the document
    if not rec["closing"]:
        today_iso = today or datetime.date.today().isoformat()
        future = [d for d in find_dates(text) if d >= today_iso]
        if future:
            rec["closing"] = sorted(future)[0]
    # still nothing, but the document clearly has dates -> use the latest one seen
    if not rec["closing"]:
        all_d = find_dates(text)
        if all_d:
            rec["closing"] = sorted(all_d)[-1]

    # --- sector fallback
    if not rec["sector"]:
        rec["sector"] = guess_sector(text)

    # --- ACCURACY GATE 1: a NO-BID must carry a citation that really exists in the text
    if rec["tier"] == "NO":
        cit = rec.get("citation")
        if not (cit and cit.get("quote") and verify_citation(cit["quote"], text)):
            rec["tier"] = "REVIEW"
            rec["review_reason"] = "no-bid rejected: cited clause not found verbatim in source"
            rec["citation"] = None

    # --- ACCURACY GATE 2: low confidence never becomes a hard decision
    if rec["tier"] in ("BID", "MID", "NO") and rec["confidence"] < min_conf:
        rec["review_reason"] = rec["review_reason"] or f"low confidence ({rec['confidence']:.2f})"
        rec["tier"] = "REVIEW"

    return rec


if __name__ == "__main__":
    print("analyzer v2 — import adjudicate(); run test_analyzer.py for checks")
