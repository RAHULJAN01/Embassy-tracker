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
    "WHAT A NO-BID CITATION MUST BE — the costliest mistake you can make is a NO on a\n"
    "contract M&M could have won, so the bar is high:\n"
    "  * Quote a CLAUSE — a full sentence stating an obligation or an exclusion.\n"
    "  * NEVER quote a website menu or page heading (e.g. 'Economic Opportunity',\n"
    "    'Commercial Opportunities', 'Doing Business in ...'). Those are site navigation,\n"
    "    not terms of the solicitation, and they are NOT set-asides.\n"
    "  * NEVER quote a submission deadline. A passed deadline archives a notice; it is\n"
    "    never a reason to refuse to bid.\n"
    "  * A requirement for a LOCAL, in-country or licensed firm is NOT a no-bid. A local\n"
    "    partner or subcontractor can hold it -> tier MID and name that route.\n"
    "  * If you cannot quote a sentence that genuinely excludes M&M, the tier is not NO.\n\n"
    "STAGE 1 — TRUE FATAL TRIGGERS (any one = tier NO, quote the clause VERBATIM):\n"
    "  * ITAR / EAR / embargoed goods: weapons, ammunition, military-specific or dual-use tech.\n"
    "  * A set-aside M&M cannot qualify for (U.S. small-business, 8(a), SDVOSB, HUBZone, "
    "or a local/national set-aside restricted to host-country firms).\n"
    "  * The requirement cannot be sourced or fulfilled by anyone M&M could engage.\n"
    "  * No margin is possible (landed cost >= the competitive price).\n"
    "  * Illegal / sanctioned counterparty or destination.\n"
    "  NOTE: bonding, on-site labour, installation, local licences, past-performance demands and "
    "RFP narrative scoring are NOT fatal. They are GAPS -> handle them in Stage 3.\n\n"
    "REGISTRATION IS SETTLED. DO NOT CONSIDER IT AT ALL:\n"
    "  M&M's SAM.gov registration is COMPLETE and active, and it holds UEI XWL3YN7QNKT7 "
    "and an EIN. Registration is a closed question.\n"
    "  * Do NOT list SAM.gov, System for Award Management, FAR 52.204-7, UEI, CAGE, NCAGE, "
    "DUNS or any vendor-portal registration as a restriction, a gap, a risk or a gotcha.\n"
    "  * Do NOT let any of them influence the tier, the route or the confidence.\n"
    "  * Do NOT mention them anywhere in your answer. Treat those clauses as if they were "
    "not in the document. They are satisfied.\n\n"
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
    "DATES — YOU DO THE READING, AND YOU SHOW YOUR WORK.\n"
    "  A wrong deadline is the most damaging error in this whole job: it sends us to a\n"
    "  solicitation that closed last year, or hides one closing on Friday. So for EVERY date\n"
    "  you report you must also return the EXACT WORDS from the document that state it.\n"
    "  * `closing_quote` — copy the sentence, line or table row that states the submission\n"
    "    deadline, CHARACTER FOR CHARACTER as it appears. Do not paraphrase it, do not tidy\n"
    "    it up, do not translate it, do not fix its spelling. It is checked against the\n"
    "    document and a quote that is not found there is thrown away along with your date.\n"
    "  * The quote MUST contain the date itself. 'Offers are due by the date below' is not\n"
    "    usable; quote the part that carries the date.\n"
    "  * A deadline is written a hundred different ways and they ALL count. 'No quotations\n"
    "    will be accepted after 12 October 2026', 'Offers due date: 12-OCT-2026', 'bids shall\n"
    "    reach this office not later than 1600 hrs on 12.10.2026', 'Submission closes COB\n"
    "    Monday 12 October', a row in a table reading 'Closing | 2026-10-12' — all are\n"
    "    deadlines. Read it the way a person would. Do not look for a particular phrase.\n"
    "  * NEVER report as the closing date: a delivery or completion date, a period of\n"
    "    performance, a site-visit or pre-bid meeting date, a validity or warranty expiry, a\n"
    "    contract start date, or the date the notice was issued. If the only dates present are\n"
    "    of that kind, return closing \"\" — that is the correct answer, not a failure.\n"
    "  * Same rule for `posted_quote` and `qa_quote` where you report those dates.\n"
    "  * If a date is genuinely not stated anywhere, return \"\" for it and \"\" for its quote.\n"
    "    An empty deadline is honest. A guessed one is not, and it will be caught.\n"
    "  Convert every date you report to YYYY-MM-DD. For a date written day-first vs\n"
    "  month-first ambiguously (e.g. 03/04/2026), prefer the reading consistent with the rest\n"
    "  of the document, and if truly ambiguous say so in `gotchas`.\n\n"
    "WRITE THE BRIEF LIKE A HUMAN WOULD SAY IT. `brief` is one plain sentence a busy person "
    "can read in two seconds and know whether to care: who is buying, what exactly, how many, "
    "and by when. Example: \"U.S. Embassy Kathmandu wants 120 office chairs and 60 desks "
    "delivered to the chancery, quotes close 15 Dec 2026.\" No jargon, no restating the title.\n\n"
    "Return ONLY a JSON object with keys:\n"
    '  brief (one plain sentence, as described above),\n'
    '  title, sol, posted (YYYY-MM-DD|""), closing (YYYY-MM-DD|""), qa_due (YYYY-MM-DD|""),\n'
    '  closing_quote (the verbatim words stating the deadline, containing the date; "" if none),\n'
    '  posted_quote (""), qa_quote (""),\n'
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


# ================= SITE CHROME =================
# Embassy pages carry their whole navigation in ordinary text: "Economic
# Opportunity", "Commercial Opportunities", "Doing Business in ...". The model
# read one of those menu headings as a set-aside and threw away a winnable
# contract. Navigation is not contract language and must never reach the model.
_CHROME_LINES = re.compile(
    r"^(?:\s*(?:home|menu|search|close|skip to (?:main )?content|jump into the main content"
    r"|economic opportunit(?:y|ies)|commercial opportunit(?:y|ies)|doing business in[^\n]*"
    r"|business ready|u\.?s\.?\s*citizen services|visas?|education\s*&\s*culture"
    r"|news\s*&\s*events|embassy\s*&\s*consulates?|about(?: us)?|contact(?: us)?"
    r"|jobs?|careers?|privacy policy|accessibility|follow us|share this page"
    r"|social media|newsletter|subscribe|sitemap|related content|previous|next"
    r"|read more|learn more|back to top|all news|events|press releases?)\s*)$",
    re.I | re.M)

_CHROME_PHRASES = re.compile(
    r"\b(economic opportunit(?:y|ies)\s+commercial opportunit(?:y|ies)"
    r"|commercial opportunit(?:y|ies)\s+economic opportunit(?:y|ies))\b", re.I)


def clean_source_text(text):
    """Strip website furniture so only the notice itself is adjudicated."""
    if not text:
        return text
    t = _CHROME_LINES.sub("", text)
    t = _CHROME_PHRASES.sub(" ", t)
    return re.sub(r"\n{3,}", "\n\n", t)


# ================= WHAT ACTUALLY DISQUALIFIES =================
# A no-bid is the expensive mistake: a contract we could have won, silently
# discarded. So its cited clause has to be a clause — an obligation that
# genuinely excludes M&M — not a heading, a deadline, or a sentence that merely
# contains the word "local".
_EXCLUDES = re.compile(
    r"\b(set[- ]aside|reserved (?:for|exclusively)|restricted to|limited to|only .{0,40}(?:firms|companies|nationals|bidders|offerors)"
    r"|must be (?:a |an )?(?:registered|licen[sc]ed|incorporated|established|national|citizen|resident)"
    r"|shall be (?:a |an )?(?:registered|licen[sc]ed|incorporated|national)"
    r"|not eligible|ineligible|will not be considered|are excluded"
    r"|8\(a\)|hubzone|sdvosb|wosb|edwosb|service[- ]disabled"
    r"|itar|export[- ]controlled|military|ammunition|weapon)\b", re.I)

_DEADLINE_ONLY = re.compile(
    r"^\W*(?:quotations?|offers?|proposals?|bids?|submissions?)[^.]{0,80}"
    r"(?:are |is |)due\b|^\W*no (?:quotations?|offers?|bids?) will be accepted", re.I)

_LOCALITY_ONLY = re.compile(
    r"\blocal(?:ly)?\b|\bin[- ]country\b|\bresident\b|\bdomestic(?:ally)?\b", re.I)

_HARD_NATIONALITY = re.compile(
    r"\b(must be (?:a |an )?(?:\w+ )?(?:national|citizen)|nationals? (?:of \w+ )?only"
    r"|restricted to (?:\w+ )?(?:firms|companies|nationals|entities)"
    r"|reserved (?:for|exclusively to) (?:\w+ )?(?:firms|companies|nationals)"
    r"|only (?:\w+ )?[- ]?registered (?:firms|companies))\b", re.I)


def citation_excludes(quote, full_text=""):
    """(ok, why_not). True only if the quote is genuinely a disqualifying clause."""
    q = (quote or "").strip()
    if len(q) < 25:
        return False, "the cited text is too short to be a contract clause"
    if _CHROME_PHRASES.search(q) or _CHROME_LINES.search(q.strip()):
        return False, "the cited text is website navigation, not a clause in the notice"
    if _DEADLINE_ONLY.search(q):
        return False, ("the cited text is a submission deadline. A passed deadline archives "
                       "a solicitation; it never makes it a no-bid")
    words = len(q.split())
    if words < 6:
        return False, "the cited text is a heading, not a sentence that excludes anyone"
    if not _EXCLUDES.search(q):
        return False, "the cited text states no restriction that would exclude M&M"
    # "local" alone is a gap a partner closes — that is MID, by Rahul's own rule
    if _LOCALITY_ONLY.search(q) and not _HARD_NATIONALITY.search(q) and not re.search(
            r"set[- ]aside|8\(a\)|hubzone|sdvosb|wosb", q, re.I):
        return False, ("the clause asks for a LOCAL firm, which a local partner or "
                       "subcontractor can satisfy — that is conditional, not a no-bid")
    return True, ""


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



def date_in_text(iso, text):
    """(found, evidence). A date is only real if it is actually in the document.

    The model returned a closing date, a posted date and a Q&A date for a notice
    whose text contains no dates at all, and the record was then marked VERIFIED
    because a deadline was present. A date nobody can point to in the source is
    worse than no date: it sends you to a deadline that does not exist.
    """
    if not iso or not text:
        return False, ""
    if iso not in set(find_dates(text)):
        return False, ""
    # pull the sentence it sits in, so a human can check it in one glance
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            a, b, c = m.group(1), m.group(2), m.group(3)
            got = (_mk(a, b, c) if kind == "ymd" else
                   _mk(c, b, a) if kind == "dmy" else
                   _mk(c, _month_num(b), a) if kind == "dMy" else
                   (_mk("20" + c, _month_num(b), a) if _month_num(b) else "") if kind == "dMyy" else
                   _mk(c, _month_num(a), b))
            if got == iso:
                lo = max(0, m.start() - 110)
                hi = min(len(text), m.end() + 70)
                return True, re.sub(r"\s+", " ", text[lo:hi]).strip()
    return True, ""


def cue_anchored(iso, text, cues, window=200):
    """(anchored, evidence). Does this date sit on a line that SAYS it is a
    deadline — not merely somewhere in the document?

    Existence was the old test, and it is not enough. A delivery date, a period
    of performance, a warranty expiry and a pre-bid meeting all exist in the
    document, and any of them passed an existence check and went onto the page
    labelled "Deadline". This asks the only question that matters: is there a
    phrase next to it that calls it a closing date.
    """
    if not iso or not text:
        return False, ""
    low = text.lower()
    for cue in cues:
        i = low.find(cue)
        while i != -1:
            seg = text[max(0, i - 60): i + window]
            if iso in find_dates(seg):
                return True, re.sub(r"\s+", " ", seg).strip()
            i = low.find(cue, i + 1)
    return False, ""


# Prefix on the warning that says "this notice states no deadline". It is a
# marker so the claim can be RETRACTED if a later, better-informed step proves a
# deadline -- substring-matching a sentence would rot the moment the wording
# changed.
NO_CLOSING = "[no-closing]"


def clear_no_closing(rec):
    """Retract the no-deadline finding, because a deadline has now been proven."""
    rec["date_warnings"] = [w for w in (rec.get("date_warnings") or [])
                            if not w.startswith(NO_CLOSING)]
    if not rec["date_warnings"]:
        rec.pop("date_warnings", None)
    rec.pop("no_closing", None)
    rec.pop("dates_seen", None)
    if (rec.get("needs") or "").startswith("a deadline"):
        rec.pop("needs", None)
    return rec


def date_windows(text, span=260, cap=7000):
    """The parts of a document that could possibly hold a deadline, and nothing else.

    The date gate has to be model-read to be accurate, but sending a whole
    40-page solicitation to a model just to ask "which of these dates is the
    deadline" is paying for 40 pages to answer one question. Every candidate
    answer is a date that is printed somewhere, so only the text AROUND the
    printed dates can matter. This returns those windows, merged and capped.

    The payoff is also the cheapest possible no: a document with no dates in it
    returns "", and the caller skips the model entirely at zero cost.
    """
    if not text:
        return ""
    spans = []
    for rx, _kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            spans.append((max(0, m.start() - span), min(len(text), m.end() + span)))
    if not spans:
        return ""
    spans.sort()
    merged = [list(spans[0])]
    for lo, hi in spans[1:]:
        if lo <= merged[-1][1] + 40:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    out, used = [], 0
    for lo, hi in merged:
        chunk = text[lo:hi]
        if used + len(chunk) > cap:
            chunk = chunk[: max(0, cap - used)]
        if chunk:
            out.append(chunk)
            used += len(chunk)
        if used >= cap:
            break
    return "\n...\n".join(out)


DATE_PROMPT = (
    "You are reading an excerpt of a government procurement notice. Your ONLY job is to "
    "identify which of the dates printed here is the SUBMISSION DEADLINE — the last moment a "
    "quote, bid, offer or proposal may be handed in.\n\n"
    "Read it the way a person would. A deadline is written a hundred different ways and all of "
    "them count: 'no quotations will be accepted after 12 October 2026', 'offers due date: "
    "12-OCT-2026', 'bids must be in our hands by 1600 hrs on 12.10.2026', 'the tender box is "
    "sealed at 15:00 on 12 October 2026', 'nothing submitted past 12/10/2026 shall be "
    "entertained', a table row reading 'Submission | 2026-10-12', or the same sentence in "
    "another language. Do not look for a particular phrase; understand the sentence.\n\n"
    "These are NEVER the submission deadline, no matter how prominent they are:\n"
    "  * a delivery, completion or installation date\n"
    "  * a period of performance or contract start/end date\n"
    "  * a site-visit or pre-bid meeting date\n"
    "  * a quote-validity period or warranty expiry\n"
    "  * the date the notice was issued or published\n"
    "  * a date in a company history, a licence number or an address\n\n"
    "If none of the dates here is a submission deadline, say so by returning \"\". That is a "
    "correct and useful answer. Do NOT pick the nearest future date to be helpful — a guess is "
    "worse than nothing, because it cannot be told apart from a real one.\n\n"
    "For every date you report, copy the exact words it comes from, CHARACTER FOR CHARACTER as "
    "they appear in the excerpt. The quote is checked against the document; one that is not "
    "found there is discarded together with your date, so do not paraphrase, tidy, translate or "
    "correct it, and make sure the quote contains the date itself.\n\n"
    "Return ONLY a JSON object:\n"
    '  {"closing":"YYYY-MM-DD|\\"\\"", "closing_quote":"the exact words",\n'
    '   "posted":"YYYY-MM-DD|\\"\\"", "posted_quote":"",\n'
    '   "qa_due":"YYYY-MM-DD|\\"\\"", "qa_quote":"",\n'
    '   "why":"one short sentence on what made you pick it, or why none of them qualifies"}\n\n'
    "EXCERPT:\n"
)


def read_dates(text, call_ai, model=None):
    """Ask the model which printed date is the deadline, and prove its answer.

    Returns (rec_like, note). rec_like carries closing/posted/qa_due plus
    date_evidence and date_proof, grounded against the FULL text — so a quote
    the model trimmed from the excerpt is still checked against the real
    document. ("", note) when there is nothing to find or nothing provable.
    """
    excerpt = date_windows(text)
    if not excerpt:
        return {}, "no date of any kind is printed in the notice or its documents"
    try:
        data = call_ai(DATE_PROMPT + excerpt, model=model) if model else \
            call_ai(DATE_PROMPT + excerpt)
    except Exception as e:
        if type(e).__name__ == "AllExhausted":
            raise
        return {}, f"the date reader could not run ({str(e)[:60]})"
    if not isinstance(data, dict) or data.get("_gerr"):
        return {}, f"the date reader failed ({(data or {}).get('_gerr', 'no answer')})"
    rec = {"closing": str(data.get("closing") or "")[:10],
           "posted": str(data.get("posted") or "")[:10],
           "qa_due": str(data.get("qa_due") or "")[:10],
           "closing_quote": str(data.get("closing_quote") or "")[:400],
           "posted_quote": str(data.get("posted_quote") or "")[:400],
           "qa_quote": str(data.get("qa_quote") or "")[:400]}
    ground_dates(rec, text)
    note = str(data.get("why") or "")[:200]
    return rec, note


def quoted_date(iso, quote, text):
    """(ok, evidence, why_not) — the model read it, and we check its homework.

    This is the heart of the date repair, and it exists because of a design
    error worth writing down. The decision used to be made by a Python tuple of
    cue phrases — "closing date", "offers are due" and forty more — and the
    model's answer was only accepted if MY regex independently agreed. The
    thing that is actually good at reading English was benched, and a phrase
    list I guessed at was put in charge of the most important field on the page.
    A list like that can never be complete: "bids shall reach this office not
    later than", "submission closes COB Monday", a bare date in a table cell
    under a heading three rows up — all invisible to it.

    So the model now reports the date AND copies out the exact words that state
    it, and the only job here is to police that claim:
      1. the quoted words must really appear in the document (not paraphrased,
         not invented) — verify_citation tolerates whitespace and OCR drift;
      2. the date must be derivable FROM THOSE WORDS, which is what ties the
         number to the sentence that justifies it.
    Both hold and the date is real, in any phrasing, in any language of layout,
    with no list to outgrow. Either fails and the date does not exist.
    """
    if not iso:
        return False, "", "no date given"
    q = (quote or "").strip()
    if not q:
        return False, "", ("it does not appear in the notice or its documents, and the "
                           "reader gave no words to back it up")
    if not verify_citation(q, text):
        return False, "", ("the words the model quoted for this date are not in the "
                           "document — the date was invented")
    if iso not in set(find_dates(q)):
        return False, "", ("the quoted line does not contain this date, so nothing ties "
                           "the two together")
    # AND the date must be in the document itself. The quote check tolerates
    # whitespace and OCR drift on purpose, so a near-copy of a real sentence
    # with one digit of the YEAR changed passes it — 2026 quoted back as 2027,
    # a whole year wrong, on a sentence that otherwise matches perfectly. The
    # quote proves the date is a deadline; this proves the date is the one that
    # is actually printed. Both are needed and neither is enough alone.
    if iso not in set(find_dates(text)):
        return False, "", ("the quoted words are in the document but this exact date is "
                           "not — the date in the quote was altered")
    return True, re.sub(r"\s+", " ", q)[:300], ""


def ground_dates(rec, text):
    """Keep only the dates we can PROVE, and keep the proof.

    Every date the model returns is checked against the document. One that is
    not there is thrown away — an empty deadline is honest, an invented one
    sends Rahul to a solicitation that does not close when he thinks it does.
    The line each surviving date came from is stored so a human can check it in
    one glance, and so VERIFIED can mean something.
    """
    dropped, evidence, proof = [], {}, {}
    names = {"closing": "closing date", "posted": "posted date", "qa_due": "Q&A date"}
    for field, cues, qkey in (("closing", _DEADLINE_CUES, "closing_quote"),
                              ("posted", _POSTED_CUES, "posted_quote"),
                              ("qa_due", _QA_CUES, "qa_quote")):
        v = rec.get(field) or ""
        if v:
            # ROUTE 1 — the model read it and quoted the words. This is the main
            # path and the only one that handles a phrasing nobody listed.
            good, ev, why = quoted_date(v, rec.get(qkey), text)
            if good:
                evidence[field] = ev
                proof[field] = "quoted"
                continue
            gave_quote = bool((rec.get(qkey) or "").strip())

            # ROUTE 2 — no usable quote, so fall back to the old cue-phrase
            # corroboration. It is a safety net for a model that forgot to quote,
            # NOT the decider any more. For the closing date the cue must really
            # be there; a date that merely exists somewhere is not a deadline.
            if field == "closing":
                found, ev2 = cue_anchored(v, text, _DEADLINE_CUES)
            else:
                found, ev2 = date_in_text(v, text)
            if found:
                # Still real evidence: the date sits beside deadline wording in
                # the document itself. Graded lower than a quote, and only
                # flagged when the reader DID quote something and the quote
                # turned out not to be in the document — that is a fabrication,
                # and it is worth knowing about even though the date survived.
                evidence[field] = ev2 or "(found in the document)"
                proof[field] = "phrase"
                if gave_quote:
                    rec.setdefault("date_warnings", []).append(
                        f"the {names[field]} {v} is in the document, but the words the reader "
                        f"quoted for it are not — {why}; the date was kept on phrase-matching "
                        f"alone, so confirm it")
                continue

            # Neither route. The date does not exist as this kind of date.
            # Say WHICH of the two failures it was, because they mean very
            # different things to whoever reads the record: a date that is
            # simply absent was invented, while a date that is present but
            # never called a deadline is almost always a delivery date or a
            # performance period that got mislabelled.
            if v in set(find_dates(text)):
                why = ("it is printed in the document but not on any line that calls it a "
                       + ("deadline" if field == "closing" else names[field])
                       + ", so it belongs to something else — most often a delivery date "
                         "or a period of performance")
            dropped.append((field, v, why))
            rec[field] = ""

        # Nothing from the model at all — last chance on cue phrases alone.
        h = harvest_date(text, cues)
        if h:
            found, ev3 = (cue_anchored(h, text, cues) if field == "closing"
                          else date_in_text(h, text))
            if found:
                rec[field] = h
                evidence[field] = ev3 or "(found in the document)"
                proof[field] = "phrase"
    rec["date_evidence"] = evidence
    rec["date_proof"] = proof
    if dropped:
        rec["dropped_dates"] = [f for f, _, _ in dropped]
        rec.setdefault("date_warnings", []).append(
            "; ".join(f"the {names.get(f, f)} {v} was discarded — {why}"
                      for f, v, why in dropped))

    # --- SANITY. Two dates that are each provable can still be impossible
    # together, and an impossible pair means at least one of them is attached to
    # the wrong thing — exactly the failure that put a 2025 solicitation in the
    # BID section with a 2026 deadline. It gets flagged, never silently kept.
    cl, po = rec.get("closing") or "", rec.get("posted") or ""
    if cl and po and cl < po:
        rec.setdefault("date_warnings", []).append(
            f"the closing date {cl} is before the posted date {po} — one of the two is "
            f"attached to the wrong thing, so neither can be trusted")
    return [f for f, _, _ in dropped]


# How real notices actually phrase a deadline. Rahul: "they may find the deadline
# for sure, like 'no quotations are allowed past this date' — they will get the
# idea." These are the forms that appear in embassy and UN documents; a cue list
# that only knew "closing date" was walking past deadlines written any other way.
_DEADLINE_CUES = (
    "closing date", "close date", "closes on", "closing time", "closing on",
    "due date", "due by", "due on", "due no later",
    "offers are due", "quotations are due", "quotes are due", "proposals are due",
    "bids are due", "offers due", "quotations due", "quotes due", "bids due",
    "submission deadline", "deadline for", "deadline is", "deadline:",
    "must be received", "must be submitted", "must reach", "must arrive",
    "no later than", "not later than", "on or before", "by close of business",
    "will not be accepted after", "will be accepted until",
    "no quotations will be accepted", "no offers will be accepted",
    "no bids will be accepted", "no proposals will be accepted",
    "accepted after", "received after", "submitted after",
    "response date", "response due", "last date", "latest date",
    "expiration", "expires", "valid until", "open until", "closes at",
    "cob ", "cot ",
)

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
        # the model's own proof for each date, checked against the document
        "closing_quote": str(g("closing_quote"))[:400],
        "posted_quote": str(g("posted_quote"))[:400],
        "qa_quote": str(g("qa_quote"))[:400],
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


def strip_registration(rec):
    """Registration is settled, so it never appears in the register at all.

    Rahul: "remove the criteria fundamentally which demands SAM registration from
    the entire portal, now and forever — this is already done."

    The prompt tells the model to ignore it. This is the deterministic backstop
    behind that instruction: any registration clause that still slips through is
    deleted outright — not downgraded, not shown as boilerplate — and anything
    held back solely by one is released. A model that drifts cannot quietly
    reintroduce a barrier that no longer exists.
    """
    before = rec.get("restrictions") or []
    rec["restrictions"] = [r for r in before
                           if not _REGISTRATION_RX.search(r.get("text", "") or "")]
    removed = len(before) - len(rec["restrictions"])

    rec["gotchas"] = [g for g in (rec.get("gotchas") or [])
                      if not _REGISTRATION_RX.search(g)]
    for f in ("license", "route", "challenge_draft"):
        v = rec.get(f) or ""
        if v and _REGISTRATION_RX.search(v):
            # keep the parts that are about something real
            keep = [seg.strip() for seg in re.split(r"(?<=[.;])\s+", v)
                    if seg.strip() and not _REGISTRATION_RX.search(seg)]
            rec[f] = " ".join(keep)
            removed += 1

    # released: nothing real is holding it back any more
    if removed and rec.get("tier") == "MID":
        if not [r for r in rec["restrictions"] if r.get("kind") == "real"] \
                and not (rec.get("route") or "").strip():
            rec["tier"] = "BID"
            rec["promoted_from_mid"] = "registration is complete — nothing else was holding it"
    # A no-bid whose ONLY cited reason is registration has no reason left at all,
    # whether or not anything else was stripped.
    if rec.get("tier") == "NO":
        cit = (rec.get("citation") or {}).get("quote", "")
        if cit and _REGISTRATION_RX.search(cit):
            rec["tier"] = "REVIEW"
            rec["citation"] = None
            rec["review_reason"] = ("no-bid rested only on a registration clause, which no "
                                    "longer applies — needs a fresh look")
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
    text = clean_source_text(text)          # website menus are not contract language
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
    strip_registration(rec)           # registration is settled; it never appears

    # --- DATES MUST BE REAL, NOT PLAUSIBLE. Every date is checked against the
    # document and the line it came from is kept as proof. A date nobody can
    # point to is discarded, because an empty deadline is honest and an
    # invented one is not.
    dropped = ground_dates(rec, text)

    # A title sometimes carries the deadline, e.g. "... (by 18 August 2026)".
    # Only when the title itself says the date is a deadline — a title that just
    # mentions a year ("FY2026 Supplies") must not hand us a closing date.
    if not rec["closing"] and rec.get("title"):
        t_low = (rec["title"] or "").lower()
        if any(w in t_low for w in ("by ", "due", "deadline", "closing", "closes",
                                    "close ", "before", "until", "no later")):
            for d in find_dates(rec["title"]):
                found, ev = date_in_text(d, text)
                if found:
                    rec["closing"] = d
                    rec.setdefault("date_evidence", {})["closing"] = ev or "(from the title)"
                    break

    # NO CUE, NO DEADLINE. There used to be a "last resort" here that took the
    # soonest future date printed anywhere in the document and called it the
    # closing date. That is how a 2025 gate-parts notice with a 2027 delivery
    # date ended up sitting in the BID section with a live 2026 deadline: a
    # delivery date, a period of performance, a warranty expiry and a meeting
    # date are all future dates, and none of them is a deadline.
    #
    # A guess dressed as a deadline is the single most expensive thing this
    # register can do, because it is indistinguishable from a real one on the
    # page. So when no line states a deadline, the record says so, carries the
    # dates it DID see for a human to look at, and never claims one.
    if not rec["closing"]:
        today_iso = today or datetime.date.today().isoformat()
        seen = sorted(set(find_dates(text)))
        future = [d for d in seen if d >= today_iso]
        rec["dates_seen"] = seen[:12]
        # Marked, not just worded. The date gate may prove a deadline AFTER the
        # adjudicator ran, and a warning left behind from this branch would keep
        # the record UNVERIFIED for ever even though its deadline is proven.
        # clear_no_closing() below is the one way to retract it.
        rec["no_closing"] = True
        rec.setdefault("date_warnings", []).append(
            NO_CLOSING + ": no line in the notice or its documents states a closing date"
            + (" — dates do appear (" + ", ".join(future[:4]) +
               ") but each belongs to something else (delivery, performance period, "
               "a meeting), so none was taken as the deadline"
               if future else " and no future date appears anywhere in them"))
        rec["needs"] = "a deadline — open the notice and read the submission date"

    # --- sector fallback
    if not rec["sector"]:
        rec["sector"] = guess_sector(text)

    # --- ACCURACY GATE 1: a NO-BID must quote a clause that REALLY EXISTS and
    # that REALLY EXCLUDES US. Existing in the text is not enough: a menu
    # heading, a deadline and the word "local" all exist in the text, and each
    # of them threw away a contract we could have won.
    if rec["tier"] == "NO":
        cit = rec.get("citation")
        q = (cit or {}).get("quote") or ""
        if not (q and verify_citation(q, text)):
            rec["tier"] = "REVIEW"
            rec["review_reason"] = "no-bid rejected: cited clause not found verbatim in source"
            rec["citation"] = None
        else:
            good, why = citation_excludes(q, text)
            if not good:
                # a locality requirement is a GAP, not a bar — that is conditional
                locality = ("local" in why) or _LOCALITY_ONLY.search(q)
                rec["tier"] = "MID" if locality else "REVIEW"
                rec["review_reason"] = f"no-bid rejected: {why}"
                rec["overturned_nobid"] = why
                rec["citation"] = None
                if rec["tier"] == "MID" and not (rec.get("route") or "").strip():
                    rec["route"] = ("Engage a locally licensed partner or subcontractor to hold "
                                    "the licence and perform in country; M&M primes.")

    # --- ACCURACY GATE 2: low confidence never becomes a hard decision
    if rec["tier"] in ("BID", "MID", "NO") and rec["confidence"] < min_conf:
        rec["review_reason"] = rec["review_reason"] or f"low confidence ({rec['confidence']:.2f})"
        rec["tier"] = "REVIEW"

    return rec


if __name__ == "__main__":
    print("analyzer v2 — import adjudicate(); run test_analyzer.py for checks")


SECOND_OPINION_PROMPT = (
    "A first pass refused to bid on this solicitation for Madison & Main LLC. "
    "A wrong refusal silently throws away a contract M&M could have won, so check it.\n\n"
    "M&M is a U.S. LLC that sources goods and ships them to overseas U.S. missions and UN "
    "agencies, and can also act as prime and fulfil through others: an authorized dealer, a "
    "supplier, a LOCAL subcontractor, an installer, or a teaming/JV partner. Its SAM.gov "
    "registration is complete. It has no in-house workforce and no in-country presence of its "
    "own, but it CAN engage a local licensed partner.\n\n"
    "A refusal is only correct if the document contains a clause that genuinely EXCLUDES M&M:\n"
    "  * a set-aside M&M cannot qualify for (U.S. small business, 8(a), HUBZone, SDVOSB, WOSB,\n"
    "    or a bar restricted to host-country nationals/firms);\n"
    "  * ITAR / export-controlled / military goods;\n"
    "  * experience that must explicitly be the PRIME's own, with subcontractor experience\n"
    "    expressly disallowed;\n"
    "  * an illegal or sanctioned counterparty or destination.\n\n"
    "These are NOT reasons to refuse:\n"
    "  * a website menu or page heading ('Economic Opportunity', 'Commercial Opportunities');\n"
    "  * a submission deadline, however near or past — that archives a notice, it does not bar us;\n"
    "  * a requirement for a local, in-country, licensed, registered or bonded firm — a local\n"
    "    partner or subcontractor can hold it, which makes it CONDITIONAL, not refused;\n"
    "  * on-site labour, installation, past performance, insurance or bonding;\n"
    "  * SAM.gov / UEI / CAGE / NCAGE registration, which is already complete.\n\n"
    "THE FIRST PASS SAID: {reason}\nIT QUOTED: \"{quote}\"\n\n"
    "Answer ONLY this JSON:\n"
    '  {"upheld": true|false,\n'
    '   "tier": "NO"|"MID"|"BID"|"REVIEW",\n'
    '   "citation": {"doc":"...","quote":"verbatim excluding clause"} or null,\n'
    '   "route": "if not NO, the concrete way M&M fulfils this",\n'
    '   "why": "one sentence"}\n'
    "Uphold the refusal ONLY if you can quote a clause that truly excludes M&M. "
    "If the only obstacle is something a partner could hold, answer tier MID.\n"
    "TEXT:\n{body}"
)


def second_opinion(rec, text, call_ai, model=None):
    """Re-check a NO-BID with a stronger model before a winnable contract is lost.

    Rahul: "if you need to use a heavier model to check things at the last moment,
    use them so they don't do shitty things like these."
    """
    if rec.get("tier") != "NO":
        return rec, False
    body = re.sub(r"\s+", " ", clean_source_text(text) or "")[:18000]
    prompt = (SECOND_OPINION_PROMPT
              .replace("{reason}", (rec.get("review_reason") or "no reason recorded")[:300])
              .replace("{quote}", ((rec.get("citation") or {}).get("quote") or "")[:400])
              .replace("{body}", body))
    try:
        d = call_ai(prompt, model=model) if model else call_ai(prompt)
    except Exception as e:
        if type(e).__name__ == "AllExhausted":
            raise
        return rec, False
    if not isinstance(d, dict) or d.get("_gerr"):
        return rec, False

    rec["second_opinion"] = {"upheld": bool(d.get("upheld")),
                             "why": str(d.get("why", ""))[:300],
                             "model": model or "primary"}
    if d.get("upheld"):
        cit = d.get("citation") or {}
        q = str(cit.get("quote", ""))
        good, _why = citation_excludes(q, text) if q else (False, "")
        if q and good and verify_citation(q, text):
            rec["citation"] = {"doc": str(cit.get("doc", ""))[:160], "quote": q[:400]}
        return rec, False

    # overturned — do not throw the contract away
    t = str(d.get("tier", "MID")).upper()
    rec["tier"] = t if t in ("BID", "MID", "REVIEW") else "MID"
    rec["citation"] = None
    rec["overturned_nobid"] = (rec.get("second_opinion") or {}).get("why", "") or \
                              "a second, stronger check found no clause that excludes us"
    if d.get("route"):
        rec["route"] = str(d["route"])[:600]
    rec["review_reason"] = ""
    return rec, True

