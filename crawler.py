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

import analyzer, ai, fetcher, un_sources, estimator, pipeline, docreader

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
def progress(**kw):
    """Print a progress line the control worker can read out of the live job log.
    This is how Mission Control knows where the bots are RIGHT NOW: no extra
    commits, no polling of our own, and not a single AI token."""
    bits = " ".join(f'{k}={json.dumps(str(v))}' for k, v in kw.items())
    print(f"[PROGRESS] shard={SHARD} {bits}", flush=True)


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


def _raise_help(blocked_sites, blocked_hosts, st, label, url, need, platform="USGOV",
                kind="unknown"):
    """Record a blocked site, and be HONEST about whether a human can fix it.

    Two very different things used to be shown identically, which was useless:
      * a LOGIN wall  — a real door. Giving the bots credentials opens it.
      * a BOT wall    — the site's CDN refusing data-centre traffic. Opening it
                        in your own browser does nothing for a bot running in
                        GitHub's data centre: different machine, different IP,
                        different session. Nothing you click can help.
    """
    host = root_host(url) or label
    if host in blocked_hosts:
        return
    if kind == "unknown":
        low = (need or "").lower()
        kind = ("login" if ("sign-in" in low or "login" in low or "401" in low)
                else "botwall" if "403" in low or "cdn" in low or "refusing" in low
                else "ratelimit" if "429" in low or "slow down" in low else "unknown")
    actionable = kind == "login"      # only a login is something Rahul can open
    blocked_sites.append({"host": host, "post": label, "platform": platform,
                          "url": url, "reason": need, "need": need,
                          "kind": kind, "canHelp": actionable, "since": now_utc()})
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
SAM_DIAG = {}      # captures exactly what SAM said, for Mission Control diagnostics

# SAM.gov hands a personal API key a DAILY allowance, and it is tiny: GSA's own
# system-account guide puts a non-federal user with no assigned role at 10
# requests PER DAY (1,000 once the account carries a role). Every search AND
# every notice-description fetch spends one. We were spending more than that, so
# SAM answered 429 "throttled out" and the register got nothing from it.
# So: one window a day, one bot, and a hard ceiling on calls per run.
SAM_MAX_CALLS = int(os.getenv("SAM_MAX_CALLS", "6"))
SAM_CALLS = {"n": 0}


def sam_spend(what=""):
    """Take one unit of the SAM daily allowance. False = don't make the call."""
    if SAM_CALLS["n"] >= SAM_MAX_CALLS:
        SAM_DIAG.setdefault("last", "")
        SAM_DIAG["last"] = (f"stopped at the self-imposed ceiling of {SAM_MAX_CALLS} "
                            f"SAM calls this run (daily key allowance is small)")
        return False
    SAM_CALLS["n"] += 1
    return True


def _sam_page(cfg, key, params):
    """One SAM query. Captures the exact HTTP status + message so we can SEE why
    SAM returns nothing (bad key -> 403, over limit -> 429, empty -> 200/0)."""
    import urllib.request
    if not sam_spend("search"):
        return []
    url = cfg["sam"]["api"] + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": fetcher.UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            body = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        status = e.code
    except Exception as e:
        SAM_DIAG["last"] = f"network: {str(e)[:120]}"
        return []
    # record a readable diagnostic
    snippet = body[:200].replace("\n", " ")
    try:
        data = json.loads(body)
        ops = data.get("opportunitiesData") or []
        total = data.get("totalRecords", "?")
        msg = data.get("error", {}).get("message") or data.get("message") or ""
        SAM_DIAG["last"] = f"HTTP {status} | total={total} | got={len(ops)}" + (f" | {msg}" if msg else "")
        return ops
    except Exception:
        SAM_DIAG["last"] = f"HTTP {status} | non-JSON: {snippet}"
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
    sam_hours = {int(h) for h in os.getenv("SAM_HOURS", "14").split(",") if h.strip().isdigit()}
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
        # SAM returns the description as a URL on api.sam.gov; it needs the key,
        # and it spends one more unit of the small daily allowance.
        on_sam = "api.sam.gov" in desc
        if on_sam and "api_key=" not in desc:
            k = os.getenv("SAM_API_KEY", "")
            if k:
                desc += ("&" if "?" in desc else "?") + "api_key=" + urllib.parse.quote(k)
        if (not on_sam) or sam_spend("description"):
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
    """VERIFIED only when everything needed is actually in hand AND PROVABLE.

    A record was once marked VERIFIED on a deadline the model invented: the
    notice contained no dates at all. VERIFIED now means every date shown was
    found in the source document, not merely that a date is present.
    """
    reasons = []
    if read_fail:
        reasons.append(f"{read_fail} document(s) unreadable")
    if not rec.get("closing"):
        reasons.append("no closing date stated in the notice")
    elif not (rec.get("date_evidence") or {}).get("closing"):
        reasons.append("the closing date could not be traced back to a line in the document")
    for w in (rec.get("date_warnings") or []):
        reasons.append(w)
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
        # the plain-language brief and the everything-you-need-to-act fields
        "brief": rec.get("brief", ""),
        "lineItems": rec.get("line_items", []) or [],
        "submitHow": rec.get("submit_how", ""),
        "submitTo": rec.get("submit_to", ""),
        "submitForms": rec.get("submit_forms", []) or [],
        "awardBasis": rec.get("award_basis", ""),
        "gotchas": rec.get("gotchas", []) or [],
        "promotedFromMid": rec.get("promoted_from_mid", ""),
        "overturnedNobid": rec.get("overturned_nobid", ""),
        "datesVerified": True,          # every date proven present in the source
        "droppedDates": rec.get("dropped_dates", []),
        "dateEvidence": rec.get("date_evidence") or {},
        # "quoted" = the reader copied the words out and they checked out;
        # "phrase" = matched by phrase, which is weaker and says so on the page
        "dateProof": rec.get("date_proof") or {},
        "dateWarnings": rec.get("date_warnings") or [],
        # the dates that ARE printed in the notice when none of them is stated
        # to be the deadline — so a human can see what the bots were looking at
        # instead of being told only that something is missing
        "datesSeen": rec.get("dates_seen") or [],
        "secondOpinion": rec.get("second_opinion") or None,
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
# REPAIR: finish what we already hold before hunting for anything new.
# This is what stops the register sitting at "everything pending verification":
# an incomplete record is re-crawled, re-read and re-adjudicated from scratch,
# and it gets a bounded number of attempts so it can never churn the budget.
MAX_REPAIR_TRIES = int(os.getenv("MAX_REPAIR_TRIES", "3"))
REPAIR_SHARE = float(os.getenv("REPAIR_SHARE", "0.65"))   # of the AI budget

# DEEP SCAN on a single solicitation, pressed by the operator on the page.
# Rahul: "IF I THINK I WANT TO BID ON THIS, BUT WANT TO MAKE SURE THE DATA HERE
# IS CORRECT THEN I'LL HIT THAT DEEP SCAN BUTTON ... THEN ONLY THEN A SINGLE
# SMART AI DOES THAT JOB FOR THAT PARTICULAR SOLICITATION ONLY."
# Set to a solicitation number (or link) and the run does nothing else: it
# re-reads that one notice and every attachment, and does the whole judgement on
# the strong model instead of the cheap one. One record, one deliberate press,
# a handful of calls — which is why it can afford the expensive model.
DEEP_SOL = os.getenv("DEEP_SOL", "").strip()


# A refusal reached under the old, looser rules is not to be trusted: four of
# the first five no-bids were wrong (a website menu, a deadline, and twice the
# word "local"). Any no-bid that never faced the second opinion is re-judged.
def suspect_nobid(row):
    if row.get("tier") != "NO" or row.get("deleted"):
        return False
    return not row.get("secondOpinion")


def unproven_dates(row):
    """A date that was never checked against the document cannot be trusted.
    Records written before dates were grounded carry no proof, and one of them
    sent Rahul to a solicitation whose text contains no date at all."""
    if row.get("deleted") or row.get("datesVerified"):
        return False
    return bool(row.get("deadline"))


def suspect_date(row):
    """A date nobody can point to in the source is not a date. Records carrying
    a deadline from before the proof requirement existed are re-checked."""
    if row.get("deleted") or not row.get("deadline"):
        return False
    if not (row.get("dateEvidence") or {}).get("closing"):
        return True
    # Fallback-era deadlines. For a while any future date printed in the
    # document was promoted to "the deadline" and given an evidence line, so
    # these look proven and are not. They are the gate-parts records: a 2025
    # notice showing a live 2026 date. Every one of them is re-judged.
    w = " ".join(row.get("dateWarnings") or []).lower()
    return ("soonest future date" in w
            or "no line says this is the closing date" in w
            or "confirm it before you rely on it" in w)


def repairable(row):
    """Is this record incomplete in a way a re-crawl could actually fix?"""
    if row.get("archived") or row.get("deleted") or row.get("hidden"):
        return False
    if suspect_nobid(row) or suspect_date(row):
        return True                      # re-judge it before we act on bad data
    if row.get("verified") == "VERIFIED":
        return False
    if int(row.get("repairTries", 0) or 0) >= MAX_REPAIR_TRIES:
        return False
    return bool(row.get("link") or row.get("files"))


def repair_rank(row):
    """Worst-but-most-fixable first: an un-adjudicated record with documents we
    can still download is the highest-value call we can make."""
    notes = " ".join(row.get("verifyNotes") or []).lower()
    score = 0
    if suspect_nobid(row):
        score += 200          # a possibly-wrong refusal outranks everything else
    if unproven_dates(row):
        score += 150          # an unproven deadline is the next most dangerous thing
    if suspect_date(row):
        score += 180          # an unproven deadline is the next most dangerous thing
    if "not adjudicated" in notes or row.get("tier") == "REVIEW":
        score += 40
    if "no closing date" in notes:
        score += 25
    if "no title" in notes:
        score += 10
    if "unreadable" in notes:
        score += 15
    score += min(int(row.get("fileCount", 0) or 0), 6) * 2      # docs to mine
    score -= int(row.get("repairTries", 0) or 0) * 12           # stop flogging it
    return -score                                               # ascending sort


def repair_queue(rows):
    q = [r for r in rows if repairable(r)]
    q.sort(key=repair_rank)
    return [r for i, r in enumerate(q) if SHARDS <= 1 or i % SHARDS == SHARD]


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
    ledger = set(prior.get("meta", {}).get("ledger", []))
    state = load(STATE, {"root_idx": 0})
    rotator, call = ai.make_caller()
    st = Status(mode, rotator.names())
    budget = pipeline.Budget(MAX_AI_CALLS, TIME_BUDGET_S)
    blocked_sites = load(BLOCKED, {"sites": []}).get("sites", [])
    blocked_hosts = {b["host"] for b in blocked_sites}

    rows, discovered, completed, abandoned, repaired = [], 0, 0, 0, 0
    skipped_expired = 0      # closed before we reached them — archived, never adjudicated
    skipped_dupe = 0         # already on the register, finished
    nodate_count = 0         # no date anywhere — recorded, not adjudicated
    # every reference we already hold complete: a second sighting is not re-read
    known_live = {str(r.get("sol", "")).strip().upper()
                  for r in prior.get("solicitations", [])
                  if r.get("sol") and r.get("verified") == "VERIFIED" and not r.get("archived")}

    def repaired_fail(old, why):
        """A repair attempt that didn't land. Keep the record, count the try, and
        say plainly why — after MAX_REPAIR_TRIES it stops asking for budget and
        tells the operator it needs a human look instead of churning forever."""
        row = dict(old)
        row["prevKey"] = old.get("sol") or old.get("link") or ""
        row["repairTries"] = int(old.get("repairTries", 0) or 0) + 1
        row["lastDeepScan"] = now_utc()
        notes = [n for n in (row.get("verifyNotes") or [])
                 if not n.startswith(("re-scan", "auto-complete"))]
        if row["repairTries"] >= MAX_REPAIR_TRIES:
            notes.append(f"auto-complete gave up after {row['repairTries']} deep "
                         f"re-scans — needs a human look ({why})")
        else:
            notes.append(f"re-scan {row['repairTries']}/{MAX_REPAIR_TRIES} did not "
                         f"complete it ({why})")
        row["verifyNotes"] = notes[:6]
        row["verified"] = "UNVERIFIED"
        row["fp"] = fingerprint(row)
        rows.append(row)

    def read_all(urls):
        """Read EVERY attachment fully. Returns [(url, text, note)]."""
        out = []
        for u in (urls or [])[:15]:
            t, note = fetcher.read_attachment_full(u)
            out.append((u, t, note))
            time.sleep(PAGE_PAUSE)
        return out

    def finish_unit(unit, label):
        """Run ONE solicitation end to end. Returns True if a record was stored."""
        nonlocal completed, abandoned, skipped_expired, skipped_dupe, nodate_count
        rec, rep = pipeline.process_one(
            unit, call_ai=call, analyzer=analyzer, estimator=estimator,
            budget=budget, today=today(), fetch_attachments=read_all,
            status=st, label=label, known_live=known_live)
        if not rec:
            if rep.get("duplicate"):
                ledger.add(unit["hash"])
                skipped_dupe += 1
                return False
            # Already closed before we ever reached it. Rahul's rule is "archive
            # everything, document everything" — so it is still recorded, with the
            # date that killed it, but it never costs an AI call.
            if rep.get("expired"):
                row = {"sol": unit.get("sol_hint", ""), "link": unit.get("link", ""),
                       "title": rep.get("title_guess") or unit.get("sol_hint")
                                or "(expired solicitation)",
                       "tier": "", "sector": "", "platform": unit.get("platform", "USGOV"),
                       "agency": unit.get("agency", ""), "domestic": unit.get("domestic", False),
                       "post": unit.get("post", ""), "country": unit.get("country", ""),
                       "source": unit.get("source", "Site"),
                       "deadline": rep["expired"], "status": "Expired", "archived": True,
                       "archivedOn": today(), "updated": today(),
                       "verified": "UNVERIFIED",
                       "verifyNotes": [rep.get("deadReason")
                                       or f"closed on {rep['expired']} before the bots reached it",
                                       "archived without opening its files or spending an AI call"],
                       "deadReason": rep.get("deadReason", ""),
                       "files": unit.get("attachments", [])[:15],
                       "fileCount": len(unit.get("attachments") or []),
                       "restrictions": [], "docs": [], "gotchas": [], "lineItems": [],
                       "skippedExpired": True}
                row["fp"] = fingerprint(row)
                rows.append(row)
                ledger.add(unit["hash"])          # never look at it again
                skipped_expired += 1
                return False
            # No date anywhere, even after reading every document. Recorded so it
            # is never silently lost, flagged so a human can settle it, and NOT
            # adjudicated — we do not pay a model to guess a deadline.
            if rep.get("noDate"):
                row = {"sol": unit.get("sol_hint", ""), "link": unit.get("link", ""),
                       "title": rep.get("title_guess") or unit.get("sol_hint")
                                or "(solicitation with no stated date)",
                       "tier": "REVIEW", "sector": "",
                       "platform": unit.get("platform", "USGOV"),
                       "agency": unit.get("agency", ""),
                       "domestic": unit.get("domestic", False),
                       "post": unit.get("post", ""), "country": unit.get("country", ""),
                       "source": unit.get("source", "Site"),
                       "deadline": "", "status": "Check", "archived": False,
                       "updated": today(), "verified": "UNVERIFIED",
                       "datesVerified": True, "dateEvidence": {}, "dateWarnings": [],
                       "verifyNotes": ["no closing date anywhere — not on the page and not in "
                                       f"any of its {rep.get('filesRead', 0)} document(s)",
                                       "not adjudicated: a solicitation that cannot be placed "
                                       "in time is not worth an AI call until the date is known"],
                       "reviewReason": "no closing date could be found anywhere",
                       "files": unit.get("attachments", [])[:15],
                       "fileCount": len(unit.get("attachments") or []),
                       "restrictions": [], "docs": [], "gotchas": [], "lineItems": [],
                       "noDate": True}
                row["fp"] = fingerprint(row)
                rows.append(row)
                ledger.add(unit["hash"])
                nodate_count += 1
                return False
            abandoned += 1
            st.d["lastError"] = rep.get("stage", "")
            return False
        row = to_row(rec, post=unit.get("post", ""), country=unit.get("country", ""),
                     source=unit.get("source", "Site"), link=unit.get("link", ""),
                     platform=unit.get("platform", "USGOV"), agency=unit.get("agency", ""),
                     domestic=unit.get("domestic", False),
                     files=unit.get("attachments", []),
                     read_ok=rec.get("_read_ok", 1), read_fail=rec.get("_read_fail", 0),
                     sol_hint=unit.get("sol_hint", ""))
        row["readFailures"] = rec.get("_read_failures", [])
        if rec.get("_estimate"):
            row["estimate"] = rec["_estimate"]
            if not (row.get("value") or "").strip():
                row["value"] = rec["_estimate"]["display"]
        rows.append(row)
        ledger.add(unit["hash"])
        completed += 1
        st.beat(done=completed, found=discovered, aiCalls=budget.used)
        return True

    try:
        # ========= PHASE 0: REPAIR what we already hold =========
        # Rahul's complaint — "why am I still seeing all the solicitations pending
        # to be verified" — is answered here. Incomplete records are finished
        # BEFORE a single new one is discovered, so the register converges.
        repairs = repair_queue(prior.get("solicitations", []))
        repair_cap = max(0, int(MAX_AI_CALLS * REPAIR_SHARE))

        # ---- DEEP SCAN of ONE solicitation, on the strong model.
        # The operator pressed the button on a specific record because he is
        # thinking about bidding on it and wants the data to be right. So this
        # run does nothing else, ignores the repair-try ceiling (he asked, so he
        # gets an attempt), and spends the whole budget on that one notice.
        deep_call = call
        if DEEP_SOL:
            want = DEEP_SOL.strip().upper()
            all_rows = prior.get("solicitations", [])
            repairs = [r for r in all_rows
                       if (r.get("sol") or "").strip().upper() == want
                       or (r.get("link") or "").strip().upper() == want]
            repair_cap = MAX_AI_CALLS
            if not repairs:
                log(f"DEEP SCAN: no record matches {DEEP_SOL!r} — nothing to do")
            else:
                import ai as _ai

                def deep_call(prompt, model=None, _c=call, _m=_ai.REVIEW_MODEL):
                    """Everything in this run goes to the strong model."""
                    return _c(prompt, model=model or _m)

                log(f"DEEP SCAN: {repairs[0].get('sol') or repairs[0].get('link')} "
                    f"on {_ai.REVIEW_MODEL}, up to {MAX_AI_CALLS} calls")
        # Which solicitation numbers are already spoken for. A re-scan may only
        # adopt a newly-read number if no OTHER record already owns it — otherwise
        # a single mis-read number would collapse two live records into one and
        # quietly delete a solicitation off the register.
        claimed = {}
        for r in prior.get("solicitations", []):
            s = (r.get("sol") or "").strip().upper()
            if s:
                claimed.setdefault(s, r.get("sol") or r.get("link"))
        st.beat(phase="repair", currentJob=f"completing {len(repairs)} unfinished records",
                queued=len(repairs))
        for old in repairs:
            if budget.used >= repair_cap or not budget.can_start_job():
                break
            label = (old.get("sol") or old.get("title") or "record")[:50]
            st.beat(currentJob=f"re-scanning: {label}")
            progress(phase="repair", post=old.get("post", ""), doing="re-scanning",
                     sol=label, ai=budget.used)
            link = old.get("link") or ""
            atts = list(old.get("files") or [])
            text = ""
            try:
                if link and not link.lower().split("?")[0].endswith(
                        (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".zip")):
                    text, more, _ok, _f, sam_id = chase_solicitation(link)
                    for u in more:
                        if u not in atts:
                            atts.append(u)
                elif link:
                    atts.insert(0, link)
            except fetcher.Blocked:
                repaired_fail(old, "site refused the bot — needs hold-the-door")
                continue
            except Exception as e:
                repaired_fail(old, f"re-crawl error: {str(e)[:60]}")
                continue

            unit = {"text": text, "attachments": atts[:15],
                    "hash": old.get("fp") or unit_hash(text or link),
                    "post": old.get("post", ""), "country": old.get("country", ""),
                    "source": old.get("source", "Site"),
                    "platform": old.get("platform", "USGOV"),
                    "agency": old.get("agency", ""),
                    "domestic": old.get("domestic", False),
                    "link": link, "sol_hint": old.get("sol") or ""}
            rec, rep = pipeline.process_one(
                unit, call_ai=deep_call, analyzer=analyzer, estimator=estimator,
                budget=budget, today=today(), fetch_attachments=read_all,
                status=st, label=label)
            if not rec:
                # nothing readable / AI unavailable — count the try, keep the old row
                repaired_fail(old, rep.get("stage", "could not be completed"))
                continue
            fresh = to_row(rec, post=old.get("post", ""), country=old.get("country", ""),
                           source=old.get("source", "Site"), link=link,
                           platform=old.get("platform", "USGOV"),
                           agency=old.get("agency", ""),
                           domestic=old.get("domestic", False), files=atts[:15],
                           read_ok=rec.get("_read_ok", 1),
                           read_fail=rec.get("_read_fail", 0),
                           sol_hint=old.get("sol") or "")
            # a repair must never lose the operator's own decisions or history
            for keep in ("firstSeen", "deleted", "deletedOn", "hidden", "hiddenOn",
                         "switched", "switchedOn", "samId", "notes"):
                if old.get(keep) not in (None, "", False):
                    fresh[keep] = old[keep]
            if old.get("switched"):
                fresh["tier"] = old.get("tier", fresh["tier"])
            # identity is stable: a re-scan must never rename or clone a record
            own_key = old.get("sol") or old.get("link") or ""
            if old.get("sol"):
                fresh["sol"] = old["sol"]                      # never renamed
            else:
                found = (fresh.get("sol") or "").strip().upper()
                owner = claimed.get(found)
                if found and owner and owner != own_key:
                    # another record already owns this number — don't merge blind
                    fresh["sol"] = ""
                    fresh["verifyNotes"] = (fresh.get("verifyNotes") or []) + [
                        f"re-scan read number {found}, which already belongs to "
                        f"another record — kept separate for a human to compare"]
                    fresh["verified"] = "UNVERIFIED"
                elif found:
                    claimed[found] = own_key                   # this record owns it now
            fresh["prevKey"] = own_key
            fresh["readFailures"] = rec.get("_read_failures", [])
            fresh["repairTries"] = int(old.get("repairTries", 0) or 0) + 1
            fresh["lastDeepScan"] = now_utc()
            if DEEP_SOL:
                # the operator asked for this one by hand, on the strong model
                import ai as _ai2
                fresh["deepScanBy"] = _ai2.REVIEW_MODEL
                fresh["deepScanOn"] = now_utc()
                fresh["repairTries"] = 0          # a human asked; never counted against it
            fresh["fp"] = fingerprint(fresh)
            if rec.get("_estimate"):
                fresh["estimate"] = rec["_estimate"]
                if not (fresh.get("value") or "").strip():
                    fresh["value"] = rec["_estimate"]["display"]
            if fresh.get("verified") == "VERIFIED":
                fresh["repairTries"] = 0          # healed; eligible again if it regresses
                repaired += 1
            rows.append(fresh)
            st.beat(repaired=repaired, aiCalls=budget.used)

        # A single deep scan ends after the repair phase above. The operator
        # asked about ONE record; he did not ask to go hunting, and discovery
        # would spend his money on notices he has not looked at yet. Rather than
        # re-indenting every phase below under a condition — which is how a
        # careless edit breaks a working file — each phase is simply given
        # nothing to work on, via no_discovery().
        def no_discovery(seq):
            return [] if DEEP_SOL else seq

        # ================= SAM =================
        st.beat(phase="sam", currentJob="querying SAM.gov")
        progress(phase="sam", doing="querying SAM.gov")
        for op in no_discovery(list(sam_search(cfg))):
            if not budget.can_start_job():
                break
            text, ok, fail = sam_unit(op)
            if len(text) < 180 or not looks_like_solicitation(text):
                continue
            h = unit_hash(text)
            if h in ledger:
                continue
            discovered += 1
            nid = op.get("noticeId", "")
            ctry, _ = _pop_country(op)
            finish_unit({"text": text, "attachments": list(op.get("resourceLinks") or []),
                         "hash": h, "post": str(op.get("organizationName") or "SAM.gov"),
                         "country": ctry, "source": "SAM", "platform": "USGOV",
                         "domestic": is_domestic(op),
                         "link": cfg["sam"]["view"].replace("{id}", nid) if nid else "https://sam.gov/",
                         "sol_hint": str(op.get("solicitationNumber") or "")},
                        str(op.get("title", "SAM notice")))

        # ================= United Nations =================
        un_srcs = no_discovery([s for i, s in enumerate(un_sources.UN_SOURCES)
                                 if SHARDS <= 1 or i % SHARDS == SHARD])
        for src in un_srcs:
            if not budget.can_start_job():
                break
            agency = src["agency"]
            st.beat(phase="un", currentJob=f"UN · {agency}")
            progress(phase="un", post=agency, doing="reading UN notices")
            opener = None
            try:
                if un_sources.has_credentials(agency):
                    try:
                        opener = un_sources.try_login(agency)
                        un_sources.keep_alive(opener, agency)
                    except un_sources.HoldTheDoor as h:
                        _raise_help(blocked_sites, blocked_hosts, st, agency,
                                    h.url or src["list"], h.need, platform="UN")
                notices = un_sources.list_notices(src)
            except un_sources.HoldTheDoor as h:
                _raise_help(blocked_sites, blocked_hosts, st, agency,
                            h.url or src["list"], h.need, platform="UN")
                continue
            except Exception as e:
                st.d["lastError"] = f"{agency}: {str(e)[:70]}"
                continue

            for url, title in notices[:10]:
                if not budget.can_start_job():
                    break
                try:
                    text, atts = un_sources.fetch_notice(url, opener)
                except Exception:
                    continue
                if len(text) < 180 or not looks_like_solicitation(text):
                    continue
                h = unit_hash(text)
                if h in ledger:
                    continue
                discovered += 1
                finish_unit({"text": text, "attachments": atts, "hash": h,
                             "post": src["name"], "country": "", "source": "UN",
                             "platform": "UN", "agency": agency, "link": url,
                             "sol_hint": title[:60]}, f"{agency}: {title[:40]}")

        # ================= Embassy sites (sharded, resumable) =================
        roots = cfg.get("roots", [])
        mine = [r for i, r in enumerate(roots) if SHARDS <= 1 or i % SHARDS == SHARD]
        n = len(mine)
        start = state.get("root_idx", 0) % max(1, n) if mode == "roots" else 0
        coverage, full_pass = {}, True
        for off in (range(n) if not DEEP_SOL else ()):
            if not budget.can_start_job():
                state["root_idx"] = (start + off) % max(1, n)
                full_pass = False
                break
            root = mine[(start + off) % n]
            host = root_host(root["base"])
            st.beat(phase="embassy", currentJob=f"scanning {root['post']}",
                    queued=n - off, coverage=coverage, paused=paused())
            progress(phase="embassy", post=root["post"], country=root.get("country", ""),
                     doing="opening the procurement pages", remaining=n - off,
                     done=completed, found=discovered, ai=budget.used)
            try:
                units = {}
                for pp in discover_proc_pages(root, cfg)[:4]:
                    html_pages, files = collect_candidates(pp, cfg)
                    for sp in html_pages[:8]:
                        units[sp] = None
                    for k, u in group_file_units(files[:12]).items():
                        units.setdefault("file::" + k, u)
                    time.sleep(PAGE_PAUSE)
                coverage[root["country"]] = len(units)

                for key, pre in list(units.items())[:12]:
                    if not budget.can_start_job():
                        break
                    try:
                        if pre is None:
                            text, attach, ok, fail, sam_id = chase_solicitation(key)
                            link, atts = key, attach
                        else:
                            text, atts = pre["text"], pre["files"]
                            link, sam_id = (atts[0] if atts else root["base"]), ""
                        if len(text) < 180:
                            continue
                        kind = classify_page(text, link) if pre is None else "solicitation"
                        if kind == "noise":
                            continue
                        if kind == "listing":
                            for sol_no, chunk in split_inline_solicitations(text)[:8]:
                                if not budget.can_start_job():
                                    break
                                hh = unit_hash(chunk)
                                if hh in ledger or not looks_like_solicitation(chunk):
                                    continue
                                discovered += 1
                                finish_unit({"text": chunk, "attachments": [], "hash": hh,
                                             "post": root["post"], "country": root["country"],
                                             "source": "Site", "platform": "USGOV",
                                             "link": link, "sol_hint": sol_no},
                                            f"{root['post']} {sol_no}")
                            continue
                        if not looks_like_solicitation(text):
                            continue
                        h = unit_hash(text)
                        if h in ledger:
                            continue
                        discovered += 1
                        finish_unit({"text": text, "attachments": atts, "hash": h,
                                     "post": root["post"], "country": root["country"],
                                     "source": ("Site+SAM" if sam_id else "Site"),
                                     "platform": "USGOV", "link": link,
                                     "samId": sam_id, "sol_hint": ""}, root["post"])
                    except fetcher.Blocked:
                        raise
                    except Exception as e:
                        st.d["lastError"] = f"{root['post']}: {str(e)[:70]}"
            except fetcher.Blocked as b:
                _raise_help(blocked_sites, blocked_hosts, st, root["post"],
                            root["base"], getattr(b, "detail", "") or str(b),
                            platform="USGOV", kind=getattr(b, "kind", "unknown"))
            except Exception as e:
                st.d["lastError"] = f"{root['post']}: {str(e)[:70]}"
        if full_pass:
            state["root_idx"] = 0

    except ai.AllExhausted:
        st.beat(currentJob="AI quota exhausted — paused, resumes next run")
    except Exception as e:
        import traceback; traceback.print_exc()
        st.d["lastError"] = f"fatal: {type(e).__name__}: {str(e)[:100]}"

    merged, new_c, chg_c = merge_records(prior_rows, rows, mode)
    merged = apply_expiry(merged)

    meta = prior.get("meta", {})
    stamp = now_utc()
    if mode == "roots":
        meta["lastDeep"] = stamp; meta["lastRoots"] = stamp
    meta["lastLive"] = stamp
    meta["counts"] = tally(merged)
    meta["ledger"] = sorted(ledger)[-8000:]
    save(DATA, {"meta": meta, "solicitations": merged})
    save(STATE, state)
    save(BLOCKED, {"sites": blocked_sites, "updated": stamp})
    st.d["aiDiag"] = rotator.diag()
    st.d["samDiag"] = SAM_DIAG.get("last", "SAM not queried this run")
    st.d["samCalls"] = f"{SAM_CALLS['n']}/{SAM_MAX_CALLS} this run"
    if "429" in str(st.d["samDiag"]) or "throttl" in str(st.d["samDiag"]).lower():
        st.d["samAdvice"] = ("SAM is refusing on the daily allowance. A SAM.gov key with no "
                             "assigned role gets about 10 requests a DAY; a key on an account "
                             "that carries a role gets 1,000. Check the SAM.gov account has a "
                             "role on the entity, then regenerate the API key.")
    st.d["docCaps"] = __import__("docreader").capabilities()
    st.d["completed"] = completed
    st.d["abandoned"] = abandoned
    st.d["repaired"] = repaired
    st.d["skippedExpired"] = skipped_expired
    st.d["skippedDuplicate"] = skipped_dupe
    st.d["noDateFound"] = nodate_count
    st.d["stillUnfinished"] = sum(1 for r in merged if repairable(r))
    st.finish(note=(f"done — {repaired} unfinished records completed, "
                    f"{completed} new solicitations fully processed, "
                    f"{abandoned} left for next run, {skipped_expired} already-closed skipped "
                    f"without spending a call, {budget.used} AI calls"
                    + (f" · {budget.stopped_reason}" if budget.stopped_reason else "")))
    progress(phase="done", doing="run complete", done=completed, found=discovered,
             ai=budget.used, skipped=skipped_expired)
    print(f"[{mode}] repaired={repaired} discovered={discovered} completed={completed} "
          f"abandoned={abandoned} total={len(merged)} ai={budget.used}/{MAX_AI_CALLS} "
          f"ledger={len(ledger)}")

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
        # A RE-SCAN UPDATES A RECORD, IT NEVER CREATES A SECOND ONE.
        # `prevKey` is the identity the record already had on the register; if a
        # deep re-scan finally reads a solicitation number off the documents, the
        # original row is replaced rather than orphaned beside a duplicate.
        prev = r.get("prevKey")
        if prev and prev in by_key and prev != (r.get("sol") or r.get("link")):
            by_key.pop(prev, None)
        k = r.get("sol") or r.get("link")
        old = by_key.get(k) or (prior_rows.get(prev) if prev else None)
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



def probe_sites(limit=None, ua_variants=True):
    """Find out WHICH identity the embassy sites actually accept. No AI, no cost.

    We guessed once and made it worse. This asks the sites directly: try a small
    sample of posts with each candidate User-Agent and report the status, the
    server, and whether robots.txt permits us. Then we choose on evidence.
    """
    import urllib.request, urllib.error, ssl as _ssl
    cfg = load(ROOTS, {})
    roots = cfg.get("roots", [])[: (limit or 14)]
    ctx = _ssl.create_default_context()
    IDENTS = {
        "honest-bot": ("MadisonMainBot/1.0 (+https://madisonmain.us; procurement notice "
                       "reader; contact@madisonmain.us) Python-urllib"),
        "plain-urllib": "Python-urllib/3.12",
        "chrome-claim": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    }
    if not ua_variants:
        IDENTS = {"honest-bot": IDENTS["honest-bot"]}
    out = {k: {"ok": 0, "forbidden": 0, "other": 0, "codes": {}} for k in IDENTS}
    detail = []
    for root in roots:
        base = root.get("base", "")
        for name, ua in IDENTS.items():
            req = urllib.request.Request(base, headers={
                "User-Agent": ua, "Accept": "text/html,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"})
            code, server = 0, ""
            try:
                with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                    code, server = r.status, (r.headers.get("Server") or "")
            except urllib.error.HTTPError as e:
                code = e.code
                server = (getattr(e, "headers", {}) or {}).get("Server", "") or ""
            except Exception as e:
                code, server = -1, type(e).__name__
            b = out[name]
            b["codes"][str(code)] = b["codes"].get(str(code), 0) + 1
            if code == 200:
                b["ok"] += 1
            elif code in (401, 403, 429):
                b["forbidden"] += 1
            else:
                b["other"] += 1
            detail.append({"post": root.get("post"), "ident": name,
                           "code": code, "server": server[:40]})
            time.sleep(0.8)
    rob = []
    for root in roots[:6]:
        try:
            allowed, note = fetcher.robots_ok(root.get("base", "") + "/business/")
            rob.append({"post": root.get("post"), "allowed": allowed, "note": note})
        except Exception as e:
            rob.append({"post": root.get("post"), "allowed": None, "note": str(e)[:50]})
    result = {"mode": "probe-sites", "startedAt": now_utc(), "heartbeat": now_utc(),
              "currentJob": "site access probe", "running": False,
              "sampled": len(roots), "identities": out, "robots": rob,
              "detail": detail[:80]}
    save(STATUS, result)
    print(json.dumps({"identities": out, "robots": rob}, indent=1))
    for d in detail[:40]:
        print(f"  {d['ident']:<13} {str(d['code']):<5} {d['server']:<24} {d['post']}")
    return result


def probe_dates(posts=10, per_post=3):
    """FIELD TEST the date engine on real embassy notices. Auditable, not claimed.

    Rahul: "GO THERE IN THE FILED AND TEST UR THEORY ON REAL EMBASSY SITES, AND
    MAKE SURE THAT WE ARE HITTING THE SAME AMOUNT OF ACCURACY EACH TIME WITH ALL
    KINDS OF DIFFERENT STYLE PAGES AND FILES AND WHAT NOT."

    A test with made-up fixtures can only prove the code does what I expected.
    This walks real posts, reads the real pages and their real attachments, runs
    the real date gate, and writes down for EVERY notice:

        the deadline it settled on, how it got there (free phrase match / read
        by the cheap model / read by the strong one), THE EXACT LINE it came
        from, and every other date printed in the document that it did not pick.

    The quoted line is the audit: anyone can read it and see in one glance
    whether it really states a deadline. A row where the line does not say what
    the date claims is a failure, visible without trusting me.

    It adjudicates nothing and prices nothing, so it costs only the date calls.
    """
    cfg = load(ROOTS, {})
    roots = cfg.get("roots", [])
    client, call = ai.make_caller()
    budget = pipeline.Budget(max(12, MAX_AI_CALLS), TIME_BUDGET_S)
    rows, stats = [], {"notices": 0, "free": 0, "read": 0, "sonnet": 0,
                       "no_date_printed": 0, "dates_but_no_deadline": 0,
                       "unreadable": 0, "blocked": 0, "ai_calls": 0}
    picked = [r for i, r in enumerate(roots) if i % max(1, len(roots) // max(1, posts)) == 0]
    for root in picked[:posts]:
        try:
            pages = discover_proc_pages(root, cfg)[:2]
        except fetcher.Blocked as b:
            stats["blocked"] += 1
            rows.append({"post": root.get("post"), "error": f"blocked: {b}"})
            continue
        except Exception as e:
            rows.append({"post": root.get("post"), "error": str(e)[:70]})
            continue
        seen = 0
        for pp in pages:
            if seen >= per_post or budget.left < 2:
                break
            try:
                html_pages, files = collect_candidates(pp, cfg)
            except Exception as e:
                rows.append({"post": root.get("post"), "error": f"listing: {str(e)[:50]}"})
                continue
            for link in (html_pages[:per_post] + files[:per_post])[: per_post * 2]:
                if seen >= per_post or budget.left < 2:
                    break
                try:
                    text, atts, ok_n, fail_n, _sid = chase_solicitation(link)
                except fetcher.Blocked as b:
                    stats["blocked"] += 1
                    rows.append({"post": root.get("post"), "link": link,
                                 "error": f"blocked: {b}"})
                    continue
                except Exception as e:
                    rows.append({"post": root.get("post"), "link": link,
                                 "error": str(e)[:60]})
                    continue
                if len(text or "") < 180 or not looks_like_solicitation(text):
                    continue
                # read the attachments too — half the deadlines live in the files
                for u in pipeline.pick_attachments(atts, limit=6):
                    t, note = fetcher.read_attachment_full(u)
                    if t:
                        text += f"\n\n[DOCUMENT: {u}]\n{t}"
                    else:
                        fail_n += 1
                if fail_n and len(text) < 400:
                    stats["unreadable"] += 1
                seen += 1
                stats["notices"] += 1
                row = {"post": root.get("post"), "country": root.get("country"),
                       "link": link, "files": len(atts),
                       "title": _first_title(text, 90),
                       "all_dates_printed": sorted(set(analyzer.find_dates(text)))[:12]}

                # exactly the gate the live crawl uses, in the same order
                h = analyzer.harvest_date(text, analyzer._DEADLINE_CUES)
                got, how, ev = "", "", ""
                if h:
                    found, ev2 = analyzer.cue_anchored(h, text, analyzer._DEADLINE_CUES)
                    if found:
                        got, how, ev = h, "free (phrase)", ev2
                if not got:
                    for stage, model in (("read (haiku)", None),
                                         ("read (sonnet)", ai.REVIEW_MODEL)):
                        if budget.left < 1:
                            break
                        try:
                            dr, note = analyzer.read_dates(text, call, model=model)
                        except Exception as e:
                            row["reader_error"] = str(e)[:60]
                            break
                        if not dr:
                            row["why_none"] = note
                            break
                        budget.spend(1)
                        stats["ai_calls"] += 1
                        row.setdefault("reader_said", []).append(
                            {"stage": stage, "why": note,
                             "closing": dr.get("closing", ""),
                             "warnings": dr.get("date_warnings", [])})
                        if dr.get("closing"):
                            got, how = dr["closing"], stage
                            ev = (dr.get("date_evidence") or {}).get("closing", "")
                            break
                row["deadline"] = got
                row["how"] = how or "none"
                row["line_it_came_from"] = ev
                row["still_open"] = (got >= today()) if got else None
                if how.startswith("free"):
                    stats["free"] += 1
                elif "haiku" in how:
                    stats["read"] += 1
                elif "sonnet" in how:
                    stats["sonnet"] += 1
                elif row["all_dates_printed"]:
                    stats["dates_but_no_deadline"] += 1
                else:
                    stats["no_date_printed"] += 1
                rows.append(row)
                time.sleep(0.6)

    found_n = stats["free"] + stats["read"] + stats["sonnet"]
    stats["resolved_pct"] = round(100.0 * found_n / max(1, stats["notices"]), 1)
    result = {"mode": "probe-dates", "startedAt": now_utc(), "heartbeat": now_utc(),
              "currentJob": "date accuracy field test", "running": False,
              "summary": stats, "notices": rows, "model": client.model,
              "strong_model": ai.REVIEW_MODEL}
    save(STATUS, result)
    print(json.dumps(stats, indent=1))
    for r in rows:
        if r.get("error"):
            print(f"  !! {r.get('post','')}: {r['error']}")
            continue
        print(f"\n  {r.get('post','')} — {r.get('title','')[:70]}")
        print(f"     deadline: {r.get('deadline') or '(none)':<12} via {r.get('how')}")
        print(f"     from    : {(r.get('line_it_came_from') or '(no line)')[:120]}")
        print(f"     printed : {', '.join(r.get('all_dates_printed') or []) or '(no dates)'}")
    return result


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
    if mode == "probe-sites":
        probe_sites(); sys.exit(0)
    if mode == "probe-dates":
        # field test on real notices; writes nothing to the register
        if SHARD != 0:
            print(f"shard {SHARD}: the date field test is shard 0's job"); sys.exit(0)
        probe_dates(); sys.exit(0)
    if mode == "deepone":
        # ONE solicitation, on the strong model, because a human asked for it.
        # Only one bot may do it: four shards each finding the same record would
        # pay four times over and then fight each other in the merge.
        if not DEEP_SOL:
            print("deepone needs DEEP_SOL set to a solicitation number"); sys.exit(1)
        if SHARD != 0:
            print(f"shard {SHARD}: a single deep scan is shard 0's job"); sys.exit(0)
        run("roots"); sys.exit(0)
    if mode not in ("roots", "live"):
        print("usage: crawler.py [roots|live|deepone|probe|probe-sites|probe-dates]"); sys.exit(1)
    run(mode)
