#!/usr/bin/env python3
"""
pipeline.py — process ONE solicitation, completely, or not at all.
==================================================================
Rahul's rule, and it's the right one:

    "If the bots have refill fuel then it should be spent properly on one single
     thing until it completely processes — not add 20 different solicitation
     numbers and then have no tokens left to even fully scan one.
     Complete them fully, one by one, one at a time."

So a bot never *starts* a solicitation it cannot *finish*. Before beginning it
reserves the AI budget the full job needs; if the tank is too low it stops and
leaves the work for the next run. The result is a register of records that are
genuinely complete, instead of many half-read ones all begging to be verified.

A complete record means:
    1. every attachment downloaded and READ (pdf/docx/xls/scanned-OCR/zip)
    2. adjudicated by the AI into a tier with a cited reason
    3. dates and title present (harvested from the full text if the model missed)
    4. verification state computed from what we actually hold
    5. valued by the estimator when it is verified and doable
"""
import re, time

# AI calls a single solicitation can need: 1 adjudication (+1 retry) + 1 estimate
COST_ADJUDICATE = 1
COST_ESTIMATE = 1
COST_RETRY = 1
COST_SECOND_OPINION = 1
# Reading the dates. Only spent when phrase-matching cannot find a deadline,
# which is exactly the case the phrase list was getting wrong. The prompt is
# sent a compact excerpt of the text around the printed dates, not the whole
# document, so these are the cheapest calls in the run -- and a document with
# no dates at all costs nothing, because there is nothing to ask about.
COST_DATE_READ = 1
COST_DATE_ESCALATE = 1
FULL_JOB_COST = (COST_DATE_READ + COST_DATE_ESCALATE + COST_ADJUDICATE
                 + COST_RETRY + COST_SECOND_OPINION + COST_ESTIMATE)


class Budget:
    """The fuel tank. A job may only start if the whole job fits."""

    def __init__(self, max_calls, time_budget_s, started_at=None):
        self.max_calls = max_calls
        self.time_budget_s = time_budget_s
        self.started = started_at or time.time()
        self.used = 0
        self.stopped_reason = ""

    @property
    def left(self):
        return self.max_calls - self.used

    def elapsed(self):
        return time.time() - self.started

    def time_left(self):
        return self.time_budget_s - self.elapsed()

    def can_start_job(self, cost=FULL_JOB_COST, min_seconds=90):
        """Only begin a solicitation if we can see it through to the end."""
        if self.left < cost:
            self.stopped_reason = (f"stopping cleanly: {self.left} AI calls left, "
                                   f"a full solicitation needs {cost}")
            return False
        if self.time_left() < min_seconds:
            self.stopped_reason = "stopping cleanly: not enough time left to finish one"
            return False
        return True

    def spend(self, n=1):
        self.used += n
        return self.used


def _first_title(text, limit=140):
    """A usable title straight out of the text, so an expired notice can still be
    archived and documented without spending a single AI call on it."""
    import re as _re
    for line in (text or "").splitlines():
        s = line.strip(" \t:-—·|")
        if 12 <= len(s) <= limit and not s.lower().startswith(("http", "[document")):
            if _re.search(r"[a-zA-Z]{4}", s):
                return s[:limit]
    return ""


# Words that mean a notice is over, whatever its dates say.
_DEAD_RX = re.compile(
    r"\b(this (?:solicitation|rfq|rfp|itb|tender|notice) (?:has been |is |was )?"
    r"(?:cancell?ed|withdrawn|rescinded|terminated)"
    r"|\bcancell?ed\b[^.\n]{0,40}\b(?:solicitation|rfq|rfp|tender|notice)"
    r"|notice of award|award(?:ed)? to\b|contract (?:has been )?awarded"
    r"|no longer (?:open|accepting|available)|closed to (?:new )?(?:offers|bids|submissions)"
    r"|submissions? (?:are )?closed|this opportunity (?:has )?closed"
    r"|superseded by|replaced by (?:solicitation|rfq))\b", re.I)

# Attachments worth opening first when a notice carries a pile of them.
_WORTH_READING = re.compile(
    r"(rfq|rfp|itb|ifb|sow|statement.of.work|scope|spec|requirement|terms|conditions"
    r"|solicitation|tender|bid|quotation|attachment|annex|schedule|sf-?1449|sf-?18"
    r"|amendment|addendum|instruction|evaluation|pricing|bom)", re.I)
_LOW_VALUE = re.compile(r"(logo|banner|header|footer|map|photo|image|privacy|accessib)", re.I)


def triage(text, today, known_live=None, sol_hint=""):
    """The cheap gate. Runs on the page text ALONE, before a single file is
    opened and long before the model is called — because nothing about a dead
    solicitation is worth paying for.

    Returns (verdict, detail). verdict is "" when the notice is worth working.
    """
    t = text or ""
    if len(t.strip()) < 180:
        return "thin", "the page carried almost no text"

    m = _DEAD_RX.search(t)
    if m:
        return "dead", f"the notice says it is over: “{m.group(0)[:70]}”"

    # a stated closing date already in the past
    try:
        import analyzer as _a
        closing = _a.harvest_date(t, _a._DEADLINE_CUES)
    except Exception:
        closing = ""
    if closing and closing < today:
        return "expired", closing

    # already on the register, finished, and nothing new to learn
    if sol_hint and known_live and sol_hint.strip().upper() in known_live:
        return "duplicate", f"{sol_hint} is already on the register and verified"
    return "", ""


def pick_attachments(urls, limit=12):
    """Read the files that decide a bid first. A notice with 41 attachments can
    eat a whole run in OCR alone; the scope, the terms and the forms are what
    matter, and the site's logo never is."""
    scored = []
    for u in urls or []:
        name = str(u).split("/")[-1][:120]
        s = 0
        if _WORTH_READING.search(name):
            s += 10
        if _LOW_VALUE.search(name):
            s -= 20
        if name.lower().endswith((".pdf", ".docx", ".doc")):
            s += 3
        elif name.lower().endswith((".xlsx", ".xls", ".zip")):
            s += 2
        elif name.lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
            s -= 8
        scored.append((-s, u))
    scored.sort(key=lambda x: x[0])
    return [u for _, u in scored[:limit]]


def process_one(unit, *, call_ai, analyzer, estimator, budget, today,
                fetch_attachments, status=None, label="", known_live=None):
    """Take ONE solicitation all the way through. Returns (record|None, report).

    `unit` must carry:
        text         : the page/base text already in hand
        attachments  : list of attachment URLs still to read
        link, post, country, source, platform, agency, domestic, sol_hint
    `fetch_attachments(urls)` -> list of (url, text, note)
    """
    report = {"read_ok": 0, "read_fail": 0, "failures": [], "ai_calls": 0,
              "stage": "start", "complete": False}
    text = unit.get("text") or ""

    # ---- 0. IS IT ALIVE? Decided on the page text alone: no downloads, no OCR,
    # no model. Rahul's rule — "if it has crossed its deadline or is cancelled,
    # just move on; no need to scan it or open any files."
    v, detail = triage(text, today, known_live, unit.get("sol_hint", ""))
    if v == "thin":
        report["stage"] = "abandoned: nothing readable on the page"
        return None, report
    if v == "duplicate":
        report["stage"] = f"skipped: {detail}"
        report["duplicate"] = detail
        return None, report
    if v in ("dead", "expired"):
        report["stage"] = (f"skipped before opening anything: "
                           + (f"closed on {detail}" if v == "expired" else detail))
        report["expired"] = detail if v == "expired" else today
        report["deadReason"] = detail
        report["title_guess"] = _first_title(text)
        report["skippedFiles"] = len(unit.get("attachments") or [])
        return None, report

    # ---- 1. THE DATE COMES FIRST. Rahul's rule: "we need a date first — that's
    # the very basic thing to even decide whether to check it fully. If there is
    # no genuine date on the notice or the webpage, go and check the documents."
    # So: look on the page; if nothing, open the files and look there; only then
    # decide whether this is worth adjudicating at all.
    report["stage"] = "finding the closing date"
    if status:
        status.beat(currentJob=f"finding the closing date: {label[:44]}")
    page_date = ""
    try:
        page_date = analyzer.harvest_date(text, analyzer._DEADLINE_CUES)
    except Exception:
        page_date = ""

    chosen = pick_attachments(unit.get("attachments") or [])
    report["attachments_skipped"] = max(0, len(unit.get("attachments") or []) - len(chosen))

    if not page_date and chosen:
        report["dateFrom"] = "documents"
    elif page_date:
        report["dateFrom"] = "page"

    # read the files — we need them for the date when the page has none, and for
    # the adjudication either way
    if text:
        report["read_ok"] += 1
    for url, atext, note in fetch_attachments(chosen):
        if atext:
            text += f"\n\n[DOCUMENT: {url}]\n{atext}"
            report["read_ok"] += 1
        else:
            report["read_fail"] += 1
            report["failures"].append({"file": url, "why": note or "unreadable"})

    if len(text.strip()) < 180:
        report["stage"] = "abandoned: nothing readable"
        return None, report

    # ---- THE DATE GATE. Free first, then the reader, then a stronger reader.
    #
    # This gate used to be phrase-matching alone, and that was the whole bug:
    # a notice saying "nothing submitted past 12/10/2026 shall be entertained"
    # matched no phrase, so it was filed as having no date and never adjudicated
    # at all. The phrase list now only gets the FIRST attempt, because when it
    # works it is free. When it fails the model reads the dates itself, which is
    # what it is good at, on a compact excerpt rather than the whole document.
    closing, closing_ev, how = "", "", ""
    try:
        h = analyzer.harvest_date(text, analyzer._DEADLINE_CUES)
        if h:
            found, ev = analyzer.cue_anchored(h, text, analyzer._DEADLINE_CUES)
            if found:
                closing, closing_ev, how = h, ev, "phrase"
    except Exception:
        pass

    date_rec = {}
    if not closing:
        try:
            import ai as _ai
            stronger = _ai.REVIEW_MODEL
        except Exception:
            stronger = None
        for stage, model, cost in (("read", None, COST_DATE_READ),
                                   ("escalated", stronger, COST_DATE_ESCALATE)):
            if stage == "escalated" and not stronger:
                break
            if budget.left < cost:
                break
            try:
                dr, note = analyzer.read_dates(text, call_ai, model=model)
            except Exception as e:
                if type(e).__name__ == "AllExhausted":
                    raise
                report.setdefault("notes", []).append(f"date reader: {str(e)[:60]}")
                break
            if not dr:
                # nothing printed to read -- no call was made, so nothing spent
                report["date_note"] = note
                break
            budget.spend(cost)
            report["ai_calls"] += 1
            report["date_note"] = note
            date_rec = dr
            if dr.get("closing"):
                closing = dr["closing"]
                closing_ev = (dr.get("date_evidence") or {}).get("closing", "")
                how = "read" if stage == "read" else "read-by-sonnet"
                break
            # The cheap reader found nothing it could prove. Before accepting
            # that a notice has no deadline -- which takes it out of the
            # register -- the question goes to the stronger model once. This is
            # the one place where paying more is obviously worth it: the
            # alternative is dropping a live contract.
    report["closing_found"] = closing
    report["closing_evidence"] = closing_ev
    report["closing_how"] = how
    if date_rec.get("date_warnings"):
        report["date_warnings"] = date_rec["date_warnings"]

    # dead by its own date, now that we have actually looked for it
    if closing and closing < today:
        report["stage"] = f"skipped: closed on {closing}"
        report["expired"] = closing
        report["deadReason"] = f"the documents give a closing date of {closing}, already past"
        report["title_guess"] = _first_title(text)
        return None, report

    # dead by its own words, in the documents this time
    v2, detail2 = triage(text, today, None, "")
    if v2 in ("dead", "expired"):
        report["stage"] = ("skipped after reading the files: "
                           + (f"closed on {detail2}" if v2 == "expired" else detail2))
        report["expired"] = detail2 if v2 == "expired" else today
        report["deadReason"] = detail2
        report["title_guess"] = _first_title(text)
        return None, report

    # NO DATE ANYWHERE — not on the page, not in a single document. We will not
    # pay to adjudicate something we cannot even place in time, and we will not
    # let a model invent one. It is recorded, flagged, and handed to a human.
    if not closing:
        report["stage"] = "no closing date anywhere — not adjudicated"
        report["noDate"] = True
        report["title_guess"] = _first_title(text)
        report["filesRead"] = report["read_ok"]
        # the dates that ARE printed, so the record can show a human what the
        # bots were looking at instead of only saying something is missing
        try:
            report["datesSeen"] = sorted(set(analyzer.find_dates(text)))[:12]
        except Exception:
            report["datesSeen"] = []
        return None, report

    # ---- 2. ADJUDICATE (with one retry, which the reservation already covers)
    report["stage"] = "adjudicating"
    if status:
        status.beat(currentJob=f"adjudicating: {label[:50]}")
    rec = analyzer.adjudicate(text, call_ai, today=today)
    budget.spend(COST_ADJUDICATE)
    report["ai_calls"] += 1

    rr = (rec.get("review_reason") or "").lower()
    transient = rec.get("tier") == "REVIEW" and (
        rr.startswith("ai") or "provider" in rr or "quota" in rr or "cooling" in rr)
    if transient and budget.left >= COST_RETRY:
        time.sleep(2)
        rec = analyzer.adjudicate(text, call_ai, today=today)
        budget.spend(COST_RETRY)
        report["ai_calls"] += 1
        rr = (rec.get("review_reason") or "").lower()
        transient = rec.get("tier") == "REVIEW" and (
            rr.startswith("ai") or "provider" in rr or "quota" in rr or "cooling" in rr)

    # THE GATE'S DATE WINS. The gate resolved this deadline with its own proof
    # before a cent was spent on adjudication, and it did so with a prompt whose
    # only job was dates. The adjudicator is thinking about eligibility and
    # routes; its date is a by-product. So where the gate proved a deadline and
    # the adjudicator came back with a different one or with nothing, the gate's
    # is kept, together with the words it came from. Without this the record
    # could still end up showing a date the gate had already rejected.
    if closing and not transient:
        # The adjudicator may have concluded "no deadline stated" from its own
        # reading. The gate has since proven one, so that finding is retracted
        # rather than left to sit on the record and hold it at UNVERIFIED.
        analyzer.clear_no_closing(rec)
        if rec.get("closing") != closing:
            if rec.get("closing"):
                rec.setdefault("date_warnings", []).append(
                    f"the adjudicator read the deadline as {rec['closing']}; the date check "
                    f"proved {closing} from the notice's own words, and that is what is shown")
            rec["closing"] = closing
        rec.setdefault("date_evidence", {})["closing"] = (
            closing_ev or rec.get("date_evidence", {}).get("closing") or "")
        rec.setdefault("date_proof", {})["closing"] = (
            "quoted" if how.startswith("read") else "phrase")
        for f in ("posted", "qa_due"):
            if not rec.get(f) and date_rec.get(f):
                rec[f] = date_rec[f]
                ev = (date_rec.get("date_evidence") or {}).get(f)
                if ev:
                    rec.setdefault("date_evidence", {})[f] = ev

    if transient:
        # the AI never actually answered — do NOT store a half-record
        report["stage"] = "abandoned: AI unavailable (will retry next run)"
        return None, report

    # ---- 2b. A NO-BID GETS A SECOND, STRONGER OPINION BEFORE WE THROW IT AWAY.
    # Refusing a contract we could have won is the only error here that costs
    # real money, and it is silent. No-bids are a minority, so this is cheap.
    if rec.get("tier") == "NO" and budget.left >= COST_SECOND_OPINION:
        if status:
            status.beat(currentJob=f"double-checking a no-bid: {label[:40]}")
        try:
            import ai as _ai
            rec, changed = analyzer.second_opinion(rec, text, call_ai,
                                                   model=getattr(_ai, "REVIEW_MODEL", None))
            budget.spend(COST_SECOND_OPINION)
            report["ai_calls"] += 1
            report["second_opinion"] = "overturned" if changed else "upheld"
        except Exception as e:
            if type(e).__name__ == "AllExhausted":
                raise
            report["second_opinion"] = f"could not run ({str(e)[:60]})"

    # ---- 3. BUILD THE RECORD (dates/title already back-filled by the analyzer)
    report["stage"] = "building record"
    rec["_full_text_len"] = len(text)
    rec["_read_ok"] = report["read_ok"]
    rec["_read_fail"] = report["read_fail"]
    rec["_read_failures"] = report["failures"][:10]

    # ---- 4. VALUE IT — only when it's verified and actually doable
    report["stage"] = "valuing"
    row_preview = {
        "verified": "VERIFIED" if (report["read_fail"] == 0 and rec.get("closing")
                                   and rec.get("title") and rec.get("tier") != "REVIEW") else "UNVERIFIED",
        "tier": rec.get("tier"), "archived": False, "value": rec.get("est_value", ""),
        "title": rec.get("title"), "country": unit.get("country", ""),
        "post": unit.get("post", ""), "sector": rec.get("sector", ""),
        "type": rec.get("classification", ""), "scope": rec.get("scope", ""),
        "shipping": rec.get("shipping", ""),
    }
    if estimator.should_estimate(row_preview) and budget.left >= COST_ESTIMATE:
        if status:
            status.beat(currentJob=f"valuing: {label[:50]}")
        est = estimator.estimate(row_preview, text[:12000], call_ai)
        budget.spend(COST_ESTIMATE)
        report["ai_calls"] += 1
        if est:
            rec["_estimate"] = est

    report["stage"] = "complete"
    report["complete"] = True
    return rec, report
