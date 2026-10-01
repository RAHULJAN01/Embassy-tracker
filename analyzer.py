#!/usr/bin/env python3
"""
Madison & Main — Solicitation Adjudicator (the brain)
-----------------------------------------------------
Takes the full text of ONE solicitation (embassy page or SAM record + any
attachment text) and returns a complete dossier record for the directory:
classification, dates, terms, a BID / BID-NO-BID (MID) / NO-BID tier, the
barriers with a route-to-eligibility, and — for NO-BID — a CITED clause.

Accuracy is enforced in code, not trusted to the model:
  * a NO-BID is only allowed if its cited quote actually EXISTS in the source
    text (anti-hallucination). If it can't be verified -> REVIEW.
  * low confidence or unreadable text -> REVIEW (never a guessed tier).
  * the raw evidence snippet is stored on every record for audit.

`call_gemini(prompt)->dict` is injected so this module is testable offline and
reuses the tracker's existing Gemini plumbing in production.
"""
import re, json, difflib

TIERS = {"BID", "MID", "NO", "REVIEW"}

ADJUDICATE_PROMPT = (
    "You are a U.S. federal procurement analyst adjudicating ONE solicitation for "
    "Madison & Main LLC — a U.S. LLC that SOURCES commercial goods and ships them "
    "F.O.B. Destination to overseas U.S. missions (Dubai logistics hub). It has NO "
    "on-site workforce, NO bonding, and is foreign-owned (so U.S. small-business "
    "set-asides and on-soil work are out). It CAN: supply commercial goods, obtain an "
    "authorized-dealer/manufacturer letter, and rely on a subcontractor's or supplier's "
    "experience via an open teaming arrangement.\n\n"
    "Classify the solicitation into exactly one tier:\n"
    "  BID  = lowest barrier: full-and-open, commercial goods, F.O.B. destination overseas, "
    "no controlling restriction.\n"
    "  MID  = would be BID except ONE restriction that is EITHER (a) likely copy-paste "
    "boilerplate open to a Q&A clarification (e.g. past-performance or a trade licence "
    "demanded on a plain commercial COTS buy), OR (b) satisfiable via an authorized-dealer/"
    "manufacturer letter, a supplier, or a subcontractor's experience. State the exact route.\n"
    "  NO   = a REAL controlling blocker: construction / SF-1442 / bonds; on-site labour, "
    "installation or guard/staffing services; RFP scored on a technical narrative; a genuine "
    "set-aside; domestic (U.S.-soil) delivery / F.O.B. Origin; or a trap category "
    "(oil/gas/fuel, arms/ammo/ITAR, military-specific, ship/aircraft, perishables). "
    "For NO you MUST quote the exact controlling sentence VERBATIM from the text.\n\n"
    "Return ONLY a JSON object with keys:\n"
    '  title, sol, posted (YYYY-MM-DD|""), closing (YYYY-MM-DD|""), qa_due (YYYY-MM-DD|""),\n'
    '  classification, scope, est_value, shipping, payment, ship_after, setaside, license,\n'
    '  docs (array of strings),\n'
    '  tier ("BID"|"MID"|"NO"),\n'
    '  restrictions (array of {text, kind:("real"|"boiler"|"fix"), note}),\n'
    '  route (string; the exact move for MID, else ""),\n'
    '  challenge_draft (string; a short Q&A clarification to the CO for a boilerplate MID, else ""),\n'
    '  citation ({doc, quote} with quote copied VERBATIM from the text for NO, else null),\n'
    '  confidence (0.0-1.0 for the tier decision).\n'
    "Use ONLY what the text states; never invent a date, value or quote. today is {today}.\n"
    "TEXT:\n{body}"
)


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
    rec = {
        "title": str(g("title"))[:200], "sol": str(g("sol")).upper()[:60],
        "posted": str(g("posted"))[:10], "closing": str(g("closing"))[:10],
        "qa_due": str(g("qa_due"))[:10], "classification": str(g("classification"))[:80],
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


def adjudicate(text, call_gemini, today="", min_conf=0.5, min_chars=180):
    """Adjudicate one solicitation. `call_gemini(prompt)` returns a parsed dict
    (or raises / returns {} on failure). Returns a directory record with the
    accuracy gates applied and an evidence snippet attached."""
    body = re.sub(r"\s+", " ", text or "")
    evidence = body[:1500]
    if len(body) < min_chars:
        return {"tier": "REVIEW", "confidence": 0.0, "review_reason": "source text too short / unreadable",
                "restrictions": [], "citation": None, "_evidence": evidence}
    prompt = ADJUDICATE_PROMPT.replace("{today}", today or "").replace("{body}", body[:18000])
    try:
        data = call_gemini(prompt) or {}
    except Exception as e:
        return {"tier": "REVIEW", "confidence": 0.0, "review_reason": f"AI error: {str(e)[:80]}",
                "restrictions": [], "citation": None, "_evidence": evidence}
    if data.get("_gerr"):
        return {"tier": "REVIEW", "confidence": 0.0, "review_reason": f"AI: {data['_gerr'][:80]}",
                "restrictions": [], "citation": None, "_evidence": evidence}
    rec = _coerce(data)
    rec["_evidence"] = evidence
    rec["review_reason"] = ""

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
    print("analyzer module — import adjudicate(); run test_analyzer.py for checks")
