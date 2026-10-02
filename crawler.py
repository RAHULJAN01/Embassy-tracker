#!/usr/bin/env python3
"""
crawler.py — Madison & Main Solicitation Register engine (v2)
=============================================================
Modes:
  python crawler.py roots   # deep re-crawl slice (resumable)
  python crawler.py live    # quick status re-check of known records
  python crawler.py probe   # provider self-test (fast)

v2 rules, per Rahul:
  * ONE SOLICITATION = ONE RECORD. A notice with 5 attachments is ONE row, not 5.
    Attachments are merged into a single dossier before adjudication.
  * DATES ARE MANDATORY. Harvested from the page AND every attachment.
  * EXPIRED (past deadline) or CANCELLED leaves the active list -> ARCHIVE, documented.
  * VERIFIED state on every record (all docs read + dates + fields + citation).
  * STOP/RESUME honoured from control.json between every unit of work.
  * Shared DONE-LEDGER so bots never redo finished work.
"""
import os, sys, json, time, hashlib, re, pathlib, datetime, urllib.parse

import analyzer, ai, fetcher, un_sources, estimator

HERE = pathlib.Path(__file__).parent
ROOTS = HERE / "roots.json"
DATA = HERE / "data.json"
STATE = HERE / "state.json"
STATUS = HERE / "status.json"
BLOCKED = HERE / "blocked.json"
CONTROL = HERE / "control.json"

MAX_AI_CALLS = int(os.getenv("MAX_AI_CALLS", "120"))
TIME_BUDGET_S = int(os.getenv("TIME_BUDGET_S", "1500"))
PAGE_PAUSE = float(os.getenv("PAGE_PAUSE", "0.6"))
SHARD = int(os.getenv("SHARD", "0"))          # this worker's slice index
SHARDS = int(os.getenv("SHARDS", "1"))        # total workers
SAM_NAICS = os.getenv("SAM_NAICS", "")


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def today():
    return datetime.date.today().isoformat()


def load(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def save(p, obj):
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


def paused():
    """STOP/RESUME switch — Mission Control writes control.json."""
    return bool(load(CONTROL, {}).get("paused"))


def fingerprint(rec):
    key = "|".join(str(rec.get(k, "")) for k in
                   ("sol", "title", "deadline", "status", "summary", "link"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def unit_hash(text):
    """Content hash of a solicitation's merged dossier — the done-ledger key."""
    return hashlib.sha256(re.sub(r"\s+", " ", text or "")[:4000].encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
class Status:
    def __init__(self, mode, providers):
        self.d = {
            "mode": mode, "startedAt": now_utc(), "heartbeat": now_utc(),
            "currentJob": "starting", "phase": "init",
            "done": 0, "queued": 0, "found": 0, "aiCalls": 0, "aiBudget": MAX_AI_CALLS,
            "providers": providers, "blockedSites": [], "coverage": {},
            "shard": SHARD, "shards": SHARDS,
            "etaNote": "", "lastError": "", "running": True, "paused": False,
        }

    def beat(self, **kw):
        self.d.update(kw)
        self.d["heartbeat"] = now_utc()
        save(STATUS, self.d)

    def finish(self, note=""):
        self.d["running"] = False
        self.d["finishedAt"] = now_utc()
        self.d["currentJob"] = note or "idle"
        save(STATUS, self.d)


def root_host(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _raise_help(blocked_sites, blocked_hosts, st, label, url, need, platform="USGOV"):
    """Record a hold-the-door request so the red HELP banner can ask Rahul."""
    host = root_host(url) or label
    if host in blocked_hosts:
        return
    blocked_sites.append({"host": host, "post": label, "platform": platform,
                          "url": url, "reason": need, "need": need, "since": now_utc()})
    blocked_hosts.add(host)
    st.beat(blockedSites=blocked_sites)


SOCIAL = ("x.com", "twitter.com", "facebook.com", "instagram.com", "youtube.com",
          "linkedin.com", "flickr.com", "t.me", "wa.me", "google.com")


def is_offsite(url, base=""):
    h = root_host(url)
    return any(s in h for s in SOCIAL)


_SOL_NUM = re.compile(r"\b(19[A-Z]{2}\d{2}[A-Z0-9]{4,}|PR\d{6,}|[A-Z]{2,}-\d{2,}-[A-Z0-9-]+|"
                      r"RFQ[-\s]?\d{3,}|RFP[-\s]?\d{3,}|ITB[-\s]?\d{3,}|SOL[-\s]?\d+)\b")
_SIG = ("request for quotation", "request for proposal", "invitation for bid",
        "solicitation", "rfq", "rfp", "ifb", "itb", "offers are due", "quotation", "closing date",
        "deadline", "submit your", "scope of work", "statement of work", "f.o.b", "fob ",
        "tender", "expression of interest")


def looks_like_solicitation(text):
    low = (text or "").lower()
    score = 0
    if _SOL_NUM.search(text or ""):
        score += 1
    if analyzer.find_dates(text or ""):
        score += 1
    score += sum(1 for s in _SIG if s in low)
    return score >= 2


def extract_sol_number(text):
    m = _SOL_NUM.search(text or "")
    return m.group(1).upper().replace(" ", "") if m else ""


def all_sol_numbers(text):
    """Every distinct solicitation reference in a page, in order of appearance."""
    out, seen = [], set()
    for m in _SOL_NUM.finditer(text or ""):
        s = m.group(1).upper().replace(" ", "")
        if s not in seen:
            seen.add(s); out.append((m.start(), s))
    return out


# A page that is clearly just navigation / an article, never a solicitation.
_NOISE_MARKERS = ("skip to main content", "bilateral investment and trade",
                  "latest report", "privacy policy", "cookie", "newsletter",
                  "visa information", "consular services")
_UNIT_SIGNALS = ("closing date", "due date", "offers are due", "quotations are due",
                 "deadline", "scope of work", "statement of work", "period of performance",
                 "delivery date", "submit", "f.o.b", "incoterm", "line item", "quantity")


def classify_page(text, url=""):
    """Decide what a fetched page actually IS, so we never store an index page
    as if it were a solicitation.
        'solicitation' -> one real notice, adjudicate it
        'listing'      -> an index carrying several notices, split it
        'noise'        -> navigation / article / nothing procurable
    """
    low = (text or "").lower()
    sols = all_sol_numbers(text or "")
    signals = sum(1 for s in _UNIT_SIGNALS if s in low)
    looks_index = any(k in (url or "").lower()
                      for k in ("/business", "/procurement", "/tenders", "/opportunit",
                                "/notice", "/solicitation", "/jobs"))

    if len(sols) >= 2:
        return "listing"
    if len(sols) == 1 and signals >= 2:
        return "solicitation"
    # no reference number: only trust it if it reads like a real notice and isn't an index
    if not sols:
        if signals >= 3 and not looks_index and not any(m in low for m in _NOISE_MARKERS):
            return "solicitation"
        return "noise"
    # exactly one reference but thin content
    return "solicitation" if signals >= 1 and not looks_index else "noise"


def split_inline_solicitations(text, min_len=200):
    """Many embassies publish each solicitation INLINE on one page with no
    attachment. Cut that page into one block per solicitation reference so each
    becomes its own record instead of the whole page becoming one blob."""
    marks = all_sol_numbers(text or "")
    if len(marks) < 2:
        return []
    blocks = []
    for i, (pos, sol) in enumerate(marks):
        start = max(0, pos - 400)                      # keep the heading above the ref
        end = marks[i + 1][0] - 400 if i + 1 < len(marks) else len(text)
        chunk = (text or "")[start:max(start + min_len, end)].strip()
        if len(chunk) >= min_len:
            blocks.append((sol, chunk))
    return blocks


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def discover_proc_pages(root, cfg):
    base = root["base"]
    raw, ct, final = fetcher.get(base)
    if raw is None:
        return []
    pages = set()
    for href, text in fetcher.links(raw, final):
        if root_host(href) != root_host(base) or is_offsite(href):
            continue
        hay = (href + " " + text).lower()
        if any(k in hay for k in cfg["proc_keywords"]):
            pages.add(href.split("#")[0])
    for hint in cfg["proc_hints"]:
        pages.add(urllib.parse.urljoin(base, hint))
    return list(pages)[:8]


def collect_candidates(proc_url, cfg):
    """From a procurement listing page, return (html_pages, file_links)."""
    raw, ct, final = fetcher.get(proc_url)
    if raw is None:
        return [], []
    html_pages, files = [], []
    host = root_host(final)
    for href, text in fetcher.links(raw, final):
        if is_offsite(href):
            continue
        # stay on the mission's own site — a link out to worldbank.org or a news
        # article is never this embassy's solicitation
        if root_host(href) != host:
            continue
        low = (href + " " + text).lower()
        if href.lower().split("?")[0].endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx")):
            files.append(href)
        elif any(k in low for k in cfg["proc_keywords"]):
            html_pages.append(href.split("#")[0])
    return list(dict.fromkeys(html_pages))[:15], list(dict.fromkeys(files))[:25]


_SAM_LINK = re.compile(r"https?://(?:www\.)?sam\.gov/opp/([0-9a-fA-F]{8,})", re.I)
_DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".rtf", ".txt")


def _sam_notice_text(notice_id):
    """A site pointed us at SAM. Go get the actual notice and its documents."""
    key = os.getenv("SAM_API_KEY", "")
    parts, atts = [], []
    if key:
        url = ("https://api.sam.gov/opportunities/v2/search?"
               + urllib.parse.urlencode({"api_key": key, "noticeid": notice_id, "limit": "1"}))
        try:
            raw, ct, final = fetcher.get(url)
            if raw:
                ops = json.loads(raw).get("opportunitiesData") or []
                if ops:
                    op = ops[0]
                    parts += [str(op.get("title") or ""), str(op.get("description") or "")]
                    atts = list(op.get("resourceLinks") or [])
        except Exception:
            pass
    if not parts:                      # no API key / API refused -> read the public page
        try:
            raw, ct, final = fetcher.get(f"https://sam.gov/opp/{notice_id}/view")
            if raw:
                parts.append(fetcher.html_text(raw))
        except Exception:
            pass
    for a in atts[:8]:
        t = fetcher.read_attachment(a)
        if t:
            parts.append(f"\n[SAM ATTACHMENT: {a}]\n{t}")
        time.sleep(PAGE_PAUSE)
    return "\n\n".join(p for p in parts if p).strip(), atts


def chase_solicitation(start_url, max_hops=3):
    """Follow a solicitation wherever it hides — through pages, attachments and
    SAM redirects — until the real thing is in hand. Returns
    (text, files, read_ok, read_fail, sam_id)."""
    seen, queue = set(), [(start_url, 0)]
    parts, files, ok, fail = [], [], 0, 0
    sam_id = ""

    while queue:
        url, hop = queue.pop(0)
        if url in seen or hop > max_hops:
            continue
        seen.add(url)
        try:
            raw, ct, final = fetcher.get(url)
        except fetcher.Blocked:
            fail += 1
            continue
        if raw is None:
            fail += 1
            continue

        low_url = url.lower().split("?")[0]
        if "pdf" in (ct or "") or low_url.endswith(".pdf"):
            t = fetcher.pdf_text(raw)
            if t and not t.startswith("[pdf unreadable"):
                parts.append(f"\n[DOCUMENT: {url}]\n{t}"); ok += 1
                if url not in files: files.append(url)
            else:
                fail += 1
            continue

        page = fetcher.html_text(raw)
        if page:
            parts.append(page); ok += 1

        # Did this page bounce us to SAM? Then the real notice lives there.
        m = _SAM_LINK.search(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw))
        if m and not sam_id:
            sam_id = m.group(1)
            stext, satts = _sam_notice_text(sam_id)
            if stext:
                parts.append(f"\n[VIA SAM {sam_id}]\n{stext}"); ok += 1
                for a in satts:
                    if a not in files: files.append(a)

        if hop >= max_hops:
            continue
        host = root_host(final)
        for href, text in fetcher.links(raw, final):
            if is_offsite(href) or href in seen:
                continue
            hl = href.lower().split("?")[0]
            if hl.endswith(_DOC_EXT):
                queue.append((href, hop + 1))                 # always chase a document
            elif root_host(href) == host and hop + 1 <= max_hops:
                blob = (href + " " + (text or "")).lower()
                # only follow links that smell like the solicitation itself
                if any(k in blob for k in ("solicitation", "rfq", "rfp", "itb", "ifb", "tender",
                                           "attachment", "amendment", "sow", "scope", "bid",
                                           "download", "annex", "document")):
                    queue.append((href.split("#")[0], hop + 1))
        time.sleep(PAGE_PAUSE)

    return "\n\n".join(p for p in parts if p).strip(), files, ok, fail, sam_id


def build_unit_from_page(sol_url):
    """ONE solicitation page + ALL its attachments -> a single merged dossier.
    Returns (text, attachment_urls, read_ok, read_fail)."""
    try:
        raw, ct, final = fetcher.get(sol_url)
    except fetcher.Blocked:
        return "", [], 0, 1
    if raw is None:
        return "", [], 0, 1
    parts, attach, ok, fail = [], [], 0, 0
    if "pdf" in (ct or "") or sol_url.lower().endswith(".pdf"):
        t = fetcher.pdf_text(raw)
        if t and not t.startswith("[pdf unreadable"):
            parts.append(t); ok += 1
        else:
            fail += 1
    else:
        parts.append(fetcher.html_text(raw)); ok += 1
        for href, _ in fetcher.links(raw, final):
            if href.lower().split("?")[0].endswith((".pdf", ".doc", ".docx")) and not is_offsite(href):
                attach.append(href)
    for a in attach[:8]:
        t = fetcher.read_attachment(a)
        if t:
            parts.append(f"\n[ATTACHMENT: {a}]\n{t}"); ok += 1
        else:
            fail += 1
        time.sleep(PAGE_PAUSE)
    return "\n\n".join(p for p in parts if p).strip(), attach, ok, fail


def group_file_units(file_urls):
    """Orphan files on a listing page: read each, then GROUP them so that files
    belonging to the same solicitation become ONE unit (keyed by solicitation
    number when present, else by the file's own URL)."""
    units = {}
    for u in file_urls:
        t = fetcher.read_attachment(u)
        time.sleep(PAGE_PAUSE)
        if not t or len(t) < 120:
            continue
        sol = extract_sol_number(t)
        key = sol or u
        slot = units.setdefault(key, {"text": [], "files": [], "ok": 0, "fail": 0, "sol": sol})
        slot["text"].append(f"\n[ATTACHMENT: {u}]\n{t}")
        slot["files"].append(u)
        slot["ok"] += 1
    return {k: {"text": "\n\n".join(v["text"]), "files": v["files"],
                "ok": v["ok"], "fail": v["fail"], "sol": v["sol"]}
            for k, v in units.items()}


# --------------------------------------------------------------------------
# SAM
# --------------------------------------------------------------------------
def _sam_page(cfg, key, params):
    """One SAM query. Returns [] on any failure — SAM must never kill a run."""
    try:
        raw, ct, final = fetcher.get(cfg["sam"]["api"] + "?" + urllib.parse.urlencode(params))
    except fetcher.Blocked as b:
        print(f"SAM rate-limited ({b}) — skipping this query")
        return []
    except Exception as e:
        print(f"SAM error ({str(e)[:70]})")
        return []
    if not raw:
        return []
    try:
        return json.loads(raw).get("opportunitiesData", []) or []
    except Exception:
        return []


def sam_search(cfg, limit=60):
    """Pull BOTH overseas and domestic U.S. notices. Each shard takes a different
    offset so four bots don't all fetch the same first page."""
    key = os.getenv("SAM_API_KEY", "")
    if not key:
        print("no SAM_API_KEY — SAM skipped")
        return []
    # SAM's per-account rate limit is small. Only ONE bot (shard 0) queries SAM,
    # so four parallel bots don't burn the daily quota four times over.
    if SHARDS > 1 and SHARD != 0:
        print(f"shard {SHARD}: skipping SAM (only shard 0 queries it)")
        return []
    # And only at a couple of hours a day — SAM postings don't change every 2h,
    # and querying every run would exhaust the daily key limit. SAM_HOURS overrides.
    sam_hours = {int(h) for h in os.getenv("SAM_HOURS", "2,14").split(",") if h.strip().isdigit()}
    hr = datetime.datetime.now(datetime.timezone.utc).hour
    if sam_hours and hr not in sam_hours and os.getenv("FORCE_SAM", "") != "1":
        print(f"hour {hr} UTC not a SAM window {sorted(sam_hours)} — skipping SAM this run")
        return []
    pf = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%m/%d/%Y")
    pt = datetime.date.today().strftime("%m/%d/%Y")
    base = {"api_key": key, "limit": str(limit), "postedFrom": pf, "postedTo": pt, "ptype": "o,k,r"}
    if SAM_NAICS:
        base["ncode"] = SAM_NAICS.split(",")[0]

    out, seen = [], set()
    queries = [dict(base, offset=str(SHARD * limit))]              # general sweep
    # an explicit domestic sweep so the Domestic tab is actually populated
    queries.append(dict(base, offset=str(SHARD * limit), **{"state": "", "country": "US"}))
    for q in queries:
        for op in _sam_page(cfg, key, q):
            nid = op.get("noticeId") or op.get("solicitationNumber") or ""
            if nid and nid in seen:
                continue
            if nid:
                seen.add(nid)
            out.append(op)
        time.sleep(PAGE_PAUSE)
    return out


def sam_unit(op):
    """ONE SAM notice + all its resource links -> one merged dossier."""
    parts = [op.get("title", ""), op.get("description", "") or ""]
    ok, fail = 1, 0
    desc = op.get("description", "") or ""
    if desc.startswith("http"):
        raw, ct, final = fetcher.get(desc)
        if raw:
            parts.append(fetcher.html_text(raw)); ok += 1
        else:
            fail += 1
    for link in (op.get("resourceLinks") or [])[:8]:
        t = fetcher.read_attachment(link)
        if t:
            parts.append(f"\n[ATTACHMENT: {link}]\n{t}"); ok += 1
        else:
            fail += 1
        time.sleep(PAGE_PAUSE)
    return "\n\n".join(p for p in parts if p).strip(), ok, fail


def _pop_country(op):
    """SAM's placeOfPerformance shape varies (dict / nested dict / string / missing).
    Return (country_name, country_code) defensively — never raise."""
    try:
        pop = op.get("placeOfPerformance") or {}
        if isinstance(pop, str):
            return pop, ""
        if not isinstance(pop, dict):
            return "", ""
        c = pop.get("country")
        if isinstance(c, dict):
            return str(c.get("name") or ""), str(c.get("code") or "")
        if isinstance(c, str):
            return c, c
        return str(pop.get("countryName") or ""), str(pop.get("countryCode") or "")
    except Exception:
        return "", ""


_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA",
    "ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
    "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC","PR","GU","VI"}


def is_domestic(op):
    """SAM notice performed inside the US? -> Domestic tab, else Overseas."""
    name, code = _pop_country(op)
    c = (code or name).strip().upper()
    if c in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"):
        return True
    if c and c not in ("", "NONE"):
        return False                      # a stated non-US country settles it
    # no country stated: a US state in the place of performance means domestic
    try:
        pop = op.get("placeOfPerformance") or {}
        if isinstance(pop, dict):
            st = pop.get("state")
            code2 = (st.get("code") or st.get("name") or "") if isinstance(st, dict) else (st or "")
            if str(code2).strip().upper() in _US_STATES:
                return True
            if str(pop.get("zip") or "").strip()[:5].isdigit():
                return True               # a ZIP code is a US address
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# Record assembly
# --------------------------------------------------------------------------
SECTOR_ORDER = {"COTS": 0, "SERVICES": 1, "CONSTRUCTION": 2, "MIXED": 3, "": 4}


def verification_state(rec, read_ok, read_fail):
    """VERIFIED only when everything needed is actually in hand."""
    reasons = []
    if read_fail:
        reasons.append(f"{read_fail} document(s) unreadable")
    if not rec.get("closing"):
        reasons.append("no closing date found")
    if not rec.get("title"):
        reasons.append("no title")
    if rec.get("tier") == "REVIEW":
        reasons.append("not adjudicated")
    if rec.get("tier") == "NO" and not rec.get("citation"):
        reasons.append("no cited clause")
    return ("VERIFIED" if not reasons else "UNVERIFIED"), reasons


def to_row(rec, *, post, country, source, link, platform="USGOV", agency="",
           domestic=False, files=None, read_ok=1, read_fail=0, sol_hint=""):
    closing = rec.get("closing", "")
    expired = bool(closing and closing < today())
    vstate, vreasons = verification_state(rec, read_ok, read_fail)
    row = {
        "tier": rec.get("tier", "REVIEW"),
        "sector": rec.get("sector", "") or "",
        "platform": platform, "agency": agency, "domestic": bool(domestic),
        "title": rec.get("title") or sol_hint or "(untitled solicitation)",
        "sol": rec.get("sol") or sol_hint,
        "post": post, "country": country, "source": source, "link": link,
        "posted": rec.get("posted", ""), "deadline": closing,
        "qa": rec.get("qa_due", ""),
        "status": "Expired" if expired else "Active",
        "archived": expired,
        "updated": today(),
        "summary": rec.get("scope") or rec.get("classification", ""),
        "type": rec.get("classification", ""), "scope": rec.get("scope", ""),
        "value": rec.get("est_value", ""), "shipping": rec.get("shipping", ""),
        "payment": rec.get("payment", ""), "shipAfter": rec.get("ship_after", ""),
        "setaside": rec.get("setaside", ""), "license": rec.get("license", ""),
        "docs": rec.get("docs", []),
        "files": files or [],
        "fileCount": len(files or []),
        "restrictions": [{"t": r["text"], "k": r["kind"], "note": r["note"]}
                         for r in rec.get("restrictions", [])],
        "route": rec.get("route", ""),
        "challenge": rec.get("challenge_draft", ""),
        "citation": rec.get("citation"),
        "confidence": rec.get("confidence", 0),
        "reviewReason": rec.get("review_reason", ""),
        "verified": vstate, "verifyNotes": vreasons,
        "evidence": (rec.get("_evidence") or "")[:600],
    }
    row["fp"] = fingerprint(row)
    return row


# --------------------------------------------------------------------------
def run(mode):
    cfg = load(ROOTS, {})
    if not cfg:
        print("no roots.json"); return
    if paused():
        print("PAUSED by control.json — exiting without work")
        s = load(STATUS, {}); s.update({"paused": True, "running": False,
                                        "currentJob": "paused by operator",
                                        "heartbeat": now_utc()})
        save(STATUS, s); return

    prior = load(DATA, {"meta": {}, "solicitations": []})
    prior_rows = {r.get("sol") or r.get("link"): r for r in prior.get("solicitations", [])}
    ledger = set(prior.get("meta", {}).get("ledger", []))     # content hashes already adjudicated
    state = load(STATE, {"root_idx": 0})
    rotator, call = ai.make_caller()
    st = Status(mode, rotator.names())
    t0 = time.time()
    blocked_sites = load(BLOCKED, {"sites": []}).get("sites", [])
    blocked_hosts = {b["host"] for b in blocked_sites}

    rows, found, ai_calls = [], 0, 0

    def budget_left():
        return ai_calls < MAX_AI_CALLS and (time.time() - t0) < TIME_BUDGET_S and not paused()

    def adjudicate_unit(text, meta):
        nonlocal ai_calls
        rec = analyzer.adjudicate(text, call, today=today())
        ai_calls += 1
        st.beat(aiCalls=ai_calls, done=len(rows), found=found,
                currentJob=f"adjudicating: {meta[:60]}")
        return rec

    def transient(rec):
        rr = (rec.get("review_reason") or "").lower()
        return rec.get("tier") == "REVIEW" and (rr.startswith("ai") or "provider" in rr
                                                or "quota" in rr or "cooling" in rr)

    try:
        # ---------------- SAM ----------------
        st.beat(phase="sam", currentJob="querying SAM.gov")
        for op in sam_search(cfg):
            if not budget_left():
                break
            try:
                text, ok, fail = sam_unit(op)
                if len(text) < 180 or not looks_like_solicitation(text):
                    continue
                h = unit_hash(text)
                if h in ledger:
                    continue                    # another bot already did this one
                found += 1
                rec = adjudicate_unit(text, str(op.get("title", "SAM notice")))
                if transient(rec):
                    continue
                ledger.add(h)
                nid = op.get("noticeId", "")
                link = cfg["sam"]["view"].replace("{id}", nid) if nid else "https://sam.gov/"
                ctry, _ = _pop_country(op)
                rows.append(to_row(rec, post=str(op.get("organizationName") or "SAM.gov"),
                                   country=ctry, source="SAM", link=link, platform="USGOV",
                                   domestic=is_domestic(op),
                                   files=(op.get("resourceLinks") or []), read_ok=ok, read_fail=fail,
                                   sol_hint=str(op.get("solicitationNumber") or "")))
            except ai.AllExhausted:
                st.beat(currentJob="AI quota exhausted — pausing (resumes next run)")
                break
            except Exception as e:
                st.d["lastError"] = f"SAM record: {str(e)[:90]}"   # one bad notice never kills the bot
                continue
            time.sleep(PAGE_PAUSE)

        # ---------------- United Nations (UNGM / UNDP / IOM / ILO / UNICEF) ----------------
        un_srcs = [s for i, s in enumerate(un_sources.UN_SOURCES)
                   if SHARDS <= 1 or i % SHARDS == SHARD]
        for src in un_srcs:
            if not budget_left():
                break
            agency = src["agency"]
            st.beat(phase="un", currentJob=f"UN · {agency}")
            opener = None
            try:
                if un_sources.has_credentials(agency):
                    try:
                        opener = un_sources.try_login(agency)
                        un_sources.keep_alive(opener, agency)      # stop the session timing out
                    except un_sources.HoldTheDoor as h:
                        _raise_help(blocked_sites, blocked_hosts, st, agency, h.url or src["list"],
                                    h.need, platform="UN")
                        opener = None                               # carry on with public access
                notices = un_sources.list_notices(src)
            except un_sources.HoldTheDoor as h:
                _raise_help(blocked_sites, blocked_hosts, st, agency, h.url or src["list"],
                            h.need, platform="UN")
                continue
            except Exception as e:
                st.d["lastError"] = f"{agency}: {str(e)[:80]}"
                continue

            for url, title in notices[:10]:
                if not budget_left():
                    break
                try:
                    text, atts = un_sources.fetch_notice(url, opener)
                    if len(text) < 180 or not looks_like_solicitation(text):
                        continue
                    h = unit_hash(text)
                    if h in ledger:
                        continue
                    found += 1
                    rec = adjudicate_unit(text, f"{agency}: {title[:40]}")
                    if transient(rec):
                        continue
                    ledger.add(h)
                    rows.append(to_row(rec, post=src["name"], country="", source="UN",
                                       link=url, platform="UN", agency=agency,
                                       files=atts, read_ok=1 + len(atts), read_fail=0,
                                       sol_hint=title[:60]))
                except ai.AllExhausted:
                    st.beat(currentJob="AI quota exhausted — pausing (resumes next run)")
                    break
                except un_sources.HoldTheDoor as hh:
                    _raise_help(blocked_sites, blocked_hosts, st, agency, hh.url or url,
                                hh.need, platform="UN")
                except Exception as e:
                    st.d["lastError"] = f"{agency} notice: {str(e)[:80]}"
                time.sleep(PAGE_PAUSE)

        # ---------------- Embassy sites (sharded + resumable) ----------------
        roots = cfg.get("roots", [])
        mine = [r for i, r in enumerate(roots) if SHARDS <= 1 or i % SHARDS == SHARD]
        n = len(mine)
        start = state.get("root_idx", 0) % max(1, n) if mode == "roots" else 0
        coverage = {}
        completed_pass = True
        for off in range(n):
            if not budget_left():
                state["root_idx"] = (start + off) % max(1, n)
                completed_pass = False
                break
            root = mine[(start + off) % n]
            host = root_host(root["base"])
            st.beat(phase="embassy", currentJob=f"crawling {root['post']}",
                    queued=n - off, coverage=coverage, paused=paused())
            try:
                units = {}
                for pp in discover_proc_pages(root, cfg)[:4]:
                    html_pages, files = collect_candidates(pp, cfg)
                    # each HTML solicitation page = one unit (with its own attachments)
                    for sp in html_pages[:8]:
                        units[sp] = None
                    # orphan files grouped into units by solicitation number
                    for k, u in group_file_units(files[:12]).items():
                        units.setdefault("file::" + k, u)
                    time.sleep(PAGE_PAUSE)
                coverage[root["country"]] = len(units)

                for key, pre in list(units.items())[:12]:
                    if not budget_left():
                        break
                    try:
                        sam_id = ""
                        if pre is None:
                            # chase it through pages, documents and SAM redirects
                            text, attach, ok, fail, sam_id = chase_solicitation(key)
                            link, files = key, attach
                        else:
                            text, files, ok, fail = pre["text"], pre["files"], pre["ok"], pre["fail"]
                            link = files[0] if files else root["base"]
                        if len(text) < 180:
                            continue

                        # What IS this page? Never store an index page as a solicitation.
                        kind = classify_page(text, link) if pre is None else "solicitation"
                        if kind == "noise":
                            continue
                        if kind == "listing":
                            # solicitations written inline on the page -> one record each
                            units_inline = split_inline_solicitations(text)
                            for sol_no, chunk in units_inline[:8]:
                                if not budget_left():
                                    break
                                hh2 = unit_hash(chunk)
                                if hh2 in ledger or not looks_like_solicitation(chunk):
                                    continue
                                found += 1
                                rec2 = adjudicate_unit(chunk, f"{root['post']} {sol_no}")
                                if transient(rec2):
                                    continue
                                ledger.add(hh2)
                                rows.append(to_row(rec2, post=root["post"], country=root["country"],
                                                   source="Site", link=link, platform="USGOV",
                                                   files=files, read_ok=ok, read_fail=fail,
                                                   sol_hint=sol_no))
                                time.sleep(PAGE_PAUSE)
                            continue

                        if not looks_like_solicitation(text):
                            continue
                        h = unit_hash(text)
                        if h in ledger:
                            continue
                        found += 1
                        rec = adjudicate_unit(text, root["post"])
                        if transient(rec):
                            continue
                        ledger.add(h)
                        # if the trail ended at SAM, it is the SAME solicitation SAM
                        # carries — tag it so it merges instead of duplicating
                        r2 = to_row(rec, post=root["post"], country=root["country"],
                                    source=("Site+SAM" if sam_id else "Site"), link=link,
                                    platform="USGOV", files=files, read_ok=ok, read_fail=fail)
                        if sam_id:
                            r2["samId"] = sam_id
                        rows.append(r2)
                    except ai.AllExhausted:
                        st.beat(currentJob="AI quota exhausted — pausing (resumes next run)")
                        raise StopIteration
                    except fetcher.Blocked:
                        raise
                    except Exception as e:
                        st.d["lastError"] = f"{root['post']} unit: {str(e)[:80]}"
                    time.sleep(PAGE_PAUSE)
            except fetcher.Blocked as b:
                if host not in blocked_hosts:
                    blocked_sites.append({"host": host, "post": root["post"], "platform": "USGOV",
                                          "url": root["base"], "reason": str(b), "since": now_utc()})
                    blocked_hosts.add(host)
                st.beat(blockedSites=blocked_sites)
            except StopIteration:
                state["root_idx"] = (start + off) % max(1, n)
                completed_pass = False
                break
            except Exception as e:
                st.d["lastError"] = f"{root['post']}: {str(e)[:80]}"
        if completed_pass:
            state["root_idx"] = 0

    except ai.AllExhausted:
        st.beat(currentJob="AI quota exhausted — paused")
    except Exception as e:
        # never lose a shard's work to one unexpected error — log it and still save
        import traceback
        traceback.print_exc()
        st.d["lastError"] = f"fatal: {type(e).__name__}: {str(e)[:110]}"

    # ---------------- ESTIMATOR: last stage, only on cleared records ----------------
    est_done = 0
    try:
        for r in rows:
            if ai_calls >= MAX_AI_CALLS + 25 or (time.time() - t0) > TIME_BUDGET_S + 120:
                break
            if paused() or not estimator.should_estimate(r):
                continue
            st.beat(phase="estimate", currentJob=f"valuing: {str(r.get('title',''))[:50]}")
            try:
                e = estimator.estimate(r, r.get("evidence", ""), call)
            except ai.AllExhausted:
                break
            except Exception:
                continue
            ai_calls += 1
            if e:
                r["estimate"] = e
                if not (r.get("value") or "").strip():
                    r["value"] = e["display"]      # show the estimate where no value was stated
                est_done += 1
    except Exception as e:
        st.d["lastError"] = f"estimator: {str(e)[:80]}"

    # ---------------- merge + archive ----------------
    merged, new_c, chg_c = merge_records(prior_rows, rows, mode)
    merged = apply_expiry(merged)

    meta = prior.get("meta", {})
    stamp = now_utc()
    if mode == "roots":
        meta["lastDeep"] = stamp; meta["lastRoots"] = stamp
    meta["lastLive"] = stamp
    meta["counts"] = tally(merged)
    meta["ledger"] = sorted(ledger)[-8000:]        # cap so the file can't grow forever
    save(DATA, {"meta": meta, "solicitations": merged})
    save(STATE, state)
    save(BLOCKED, {"sites": blocked_sites, "updated": stamp})
    st.d["aiDiag"] = rotator.diag()
    st.finish(note=f"done — {new_c} new, {chg_c} changed, {len(merged)} total, {ai_calls} AI calls")
    print(f"[{mode}] found={found} new={new_c} changed={chg_c} total={len(merged)} est={est_done} "
          f"ai_calls={ai_calls} blocked={len(blocked_sites)} ledger={len(ledger)}")


def apply_expiry(rows):
    """Past deadline or cancelled -> archived + documented, out of the active list."""
    t = today()
    for r in rows:
        dl = r.get("deadline", "")
        expired = bool(dl and dl < t)
        cancelled = str(r.get("status", "")).lower() in ("cancelled", "canceled", "removed")
        if expired or cancelled:
            r["archived"] = True
            if expired and str(r.get("status", "")) in ("Active", "Check", ""):
                r["status"] = "Expired"
            r.setdefault("archivedOn", t)
        else:
            r["archived"] = False
    return rows


def merge_records(prior_rows, fresh_rows, mode):
    by_key = dict(prior_rows)
    new_c = chg_c = 0
    for r in fresh_rows:
        k = r.get("sol") or r.get("link")
        old = by_key.get(k)
        if not old:
            r["firstSeen"] = today(); new_c += 1
        elif old.get("fp") != r.get("fp"):
            r["firstSeen"] = old.get("firstSeen", today()); chg_c += 1
            r["changed"] = today()
        else:
            r["firstSeen"] = old.get("firstSeen", today())
        by_key[k] = r
    return list(by_key.values()), new_c, chg_c


def tally(rows):
    c = {"active": 0, "bid": 0, "mid": 0, "no": 0, "review": 0, "archived": 0,
         "verified": 0, "unverified": 0}
    for r in rows:
        if r.get("archived"):
            c["archived"] += 1
            continue
        c["active"] += 1
        t = r.get("tier", "")
        if t == "BID": c["bid"] += 1
        elif t == "MID": c["mid"] += 1
        elif t == "NO": c["no"] += 1
        else: c["review"] += 1
        if r.get("verified") == "VERIFIED": c["verified"] += 1
        else: c["unverified"] += 1
    return c


def probe():
    rotator, call = ai.make_caller()
    prompt = 'Return ONLY this JSON: {"tier":"BID","confidence":0.9}'
    results = {}
    for p in rotator.providers:
        name, key, fn = p[0], p[1], p[2]
        try:
            raw = fn(key, prompt)
            parsed = ai._parse_json(raw)
            results[name] = "OK" if parsed else f"reply not JSON: {str(raw)[:70]}"
        except Exception as e:
            results[name] = f"ERROR: {str(e)[:130]}"
    out = {"mode": "probe", "startedAt": now_utc(), "heartbeat": now_utc(),
           "currentJob": "provider self-test", "running": False,
           "providers": rotator.names(), "probe": results,
           "discovered": {k: v[:3] for k, v in ai._MODEL_CACHE.items()}}
    save(STATUS, out)
    print("probe:", json.dumps(results, indent=1))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    if mode == "probe":
        probe(); sys.exit(0)
    if mode not in ("roots", "live"):
        print("usage: crawler.py [roots|live|probe]"); sys.exit(1)
    run(mode)
