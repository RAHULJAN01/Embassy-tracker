#!/usr/bin/env python3
"""
Embassy Procurement Tracker  (v3 — embassy pages + SAM.gov API)
---------------------------------------------------------------
Two data sources, one digest:
  1. Embassy public pages (catches small / below-threshold buys)
  2. SAM.gov Get-Opportunities API — every Department of State solicitation
     worldwide (catches everything >~$25k from all ~270 posts)
Items found on BOTH are merged by solicitation number (no duplicates).

Email = colour-coded digest: NEW / AMENDMENT / CANCELLED / UPDATED, with a
SOURCE column (Site / SAM / Site+SAM) and a NOT-RESPONDING table.

Secrets (GitHub → Settings → Secrets and variables → Actions):
    GMAIL_USER, GMAIL_APP_PASSWORD, ALERT_TO
    SAM_API_KEY        (optional but recommended — free from sam.gov)
Optional env:
    SAM_ORG            org filter (default "STATE, DEPARTMENT OF")
    SAM_DAYS           look-back window in days (default 2)
    SEND_DAILY_DIGEST=1
"""

import os, re, sys, json, time, hashlib, smtplib, csv, io
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate
from urllib.parse import urljoin

import requests, yaml
from bs4 import BeautifulSoup

try:
    import pdfplumber
except Exception:
    pdfplumber = None
try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
SITES_FILE = os.path.join(HERE, "sites.yaml")
DEEP_CACHE = os.path.join(STATE_DIR, "_deepcache.json")
SAM_URL = "https://api.sam.gov/opportunities/v2/search"
SAM_SEEN = os.path.join(STATE_DIR, "_sam_seen.json")

HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}

STRONG = re.compile(r"(solicitation|request for quotation|request for proposal|request for information"
                    r"|\brfq\b|\brfp\b|\brfi\b|invitation (to|for) bid|\bitb\b|tender|appel d.?offre"
                    r"|pre-?solicitation|amendment|modification|\bsf-?30\b|\bsf-?1449\b|bid advertisement"
                    r"|invitation to bid|quotation|combined synopsis|sources sought)", re.I)
SOLNUM = re.compile(r"(PR\d{6,}|\b\d{2}[A-Z]{1,2}\d{4}[A-Z]\d{3,4}\b|\b1\d{1,2}[A-Z]{1,2}\d{3,4}[A-Z]\d{3,4}\b)", re.I)
JUNK = re.compile(r"(manage options|manage services|manage \{?vendor|view preferences|\{title\}|\{vendor_count\}"
                  r"|read more|cookie|^twitter|^facebook|privacy policy|^overview$|^notice$|^requirements$"
                  r"|^housing$|^current items$|^attachment$|^the attachment$|^q&a$|^next|^\d+$)", re.I)
EMAIL_RE = re.compile(r"[\w.\-]+@[\w.\-]+\.\w{2,}")
GENERIC_LABEL = re.compile(r"^(solicitation package|solicitation packages|solicitation|solicitations|read more|"
                           r"download|click here|attachment|attachments|rfq|rfp|rfi|document|documents|view|more|"
                           r"pdf|link|here|open|announcement|announcements)s?$", re.I)
MONTH_FMTS = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y")


def normalize_date(s):
    s = (s or "").strip()
    for fmt in MONTH_FMTS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            continue
    return ""


def title_from_href(href):
    base = href.split("#")[0].split("?")[0].rstrip("/")
    name = base.rsplit("/", 1)[-1]
    name = re.sub(r"\.(pdf|docx?|xlsx?)$", "", name, flags=re.I)
    name = re.sub(r"[-_]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()[:180]


# ============================================================================
#  DEEP READ  —  actually open each solicitation (PDF or sub-page), read the
#  text INSIDE it, and pull the real issued/closing dates + cancellation.
#  This is the free accuracy layer: it stops guessing from link text and reads
#  the source document, the way a person would.
# ============================================================================
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
MON = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
ANYDATE = re.compile(
    rf"(\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS}|{MON})[a-z]*\.?,?\s+\d{{4}}\b"      # 15 January 2026
    rf"|\b(?:{MONTHS}|{MON})[a-z]*\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b"       # January 15, 2026
    rf"|\b\d{{4}}-\d{{2}}-\d{{2}}\b"                                                    # 2026-01-15
    rf"|\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b"                                              # 01/15/2026
    rf"|\b\d{{1,2}}\.\d{{1,2}}\.\d{{4}}\b)", re.I)                                      # 15.01.2026

DL_LABEL = re.compile(
    r"(closing date|closing time|response deadline|deadline for (?:the )?(?:receipt|submission|offers?|quotations?|bids?|proposals?)"
    r"|submission deadline|deadline for submission|offers?\s+(?:are\s+)?(?:due|received)"
    r"|quotations?\s+(?:are\s+)?due|bids?\s+(?:are\s+)?due|proposals?\s+(?:are\s+)?due"
    r"|due (?:date|no later than)|no later than|last date (?:for|of)|closes on|bid closing"
    r"|responses? (?:are )?due|questions? (?:are )?due)", re.I)
ISS_LABEL = re.compile(
    r"(issuance date|date issued|date of issue(?:ance)?|issue date|issued on|posted on"
    r"|solicitation date|date of solicitation|opening date|published)", re.I)
CANCEL_TXT = re.compile(r"(this solicitation is cancel|has been cancel|is cancel|no longer available|"
                        r"withdrawn|award(?:ed)? to|has been awarded|notice of cancel)", re.I)
MONTH_FMTS_X = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
                "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%B %d %Y", "%b %d %Y", "%d %B, %Y")


def normalize_any(s):
    s = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", (s or "").strip(), flags=re.I)  # 15th -> 15 only
    s = re.sub(r"\s+", " ", s).strip(" ,.")
    for fmt in MONTH_FMTS_X:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            continue
    # last resort: normalize_date's set
    return normalize_date(s)


def _labeled_date(text, label_re, want="max"):
    """Find a date that sits just after a label like 'Closing Date:'. Returns ISO or ''."""
    hits = []
    for m in label_re.finditer(text):
        window = text[m.end(): m.end() + 90]      # look just ahead of the label
        dm = ANYDATE.search(window)
        if dm:
            iso = normalize_any(dm.group(0))
            if iso:
                hits.append(iso)
    if not hits:
        return ""
    return (max if want == "max" else min)(hits)


def parse_solicitation_text(text):
    """From the full text of a solicitation PDF/page, pull what matters."""
    text = re.sub(r"\s+", " ", text or "")[:60000]
    out = {"deadline": "", "posted": "", "cancelled": False, "sol": "", "title": ""}
    if not text:
        return out
    out["cancelled"] = bool(CANCEL_TXT.search(text))
    out["deadline"] = _labeled_date(text, DL_LABEL, want="max")     # the closing date = latest labeled
    out["posted"] = _labeled_date(text, ISS_LABEL, want="min")      # issued = earliest labeled
    sm = SOLNUM.search(text)
    if sm:
        out["sol"] = sm.group(0).upper()
    # a human-readable subject line if present
    subj = re.search(r"(subject|title|description|for)\s*[:\-]\s*([A-Z0-9][^\n\.]{8,120})", text, re.I)
    if subj:
        out["title"] = re.sub(r"\s+", " ", subj.group(2)).strip()[:180]
    return out


def _pdf_text(data):
    """Extract text from PDF bytes, first two pages are enough for header dates."""
    if pdfplumber:
        try:
            import io as _io
            with pdfplumber.open(_io.BytesIO(data)) as pdf:
                return "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
        except Exception:
            pass
    if PdfReader:
        try:
            import io as _io
            r = PdfReader(_io.BytesIO(data))
            return "\n".join((r.pages[i].extract_text() or "") for i in range(min(3, len(r.pages))))
        except Exception:
            pass
    return ""


def fetch_doc_text(url):
    """Return the readable text of a solicitation URL (PDF read inside, else HTML text)."""
    r = requests.get(url, headers=HEADERS, timeout=40)
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        return _pdf_text(r.content)
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        t.decompose()
    return soup.get_text(" ", strip=True)


def deep_read(url):
    """Open a solicitation link and return parsed {deadline,posted,cancelled,sol,title,_text}.
    Handles PDFs (read inside) and HTML sub-pages (read visible text). Never raises."""
    try:
        text = fetch_doc_text(url)
        info = parse_solicitation_text(text)
        info["_text"] = text
        return info
    except Exception:
        return {"deadline": "", "posted": "", "cancelled": False, "sol": "", "title": "", "_err": True, "_text": ""}


# ---------------------------------------------------------------------------
#  GEMINI BRAIN (optional) — the "common sense" layer. When the regex deep-read
#  cannot confidently read a closing date, hand the document text to Google
#  Gemini and let it return clean structured data with judgment. Free tier.
#  Needs the GEMINI_API_KEY secret; if absent, everything below is skipped.
# ---------------------------------------------------------------------------
GEMINI_PROMPT = (
    "You are a U.S. federal procurement analyst reading ONE embassy solicitation. "
    "Use ONLY what the text states — never invent. Convert any date (words, slashes, "
    "other formats) to YYYY-MM-DD. Return ONLY a JSON object with these keys:\n"
    '  "title"      : short plain-English subject (<=120 chars),\n'
    '  "sol"        : solicitation / RFQ / RFP number exactly as written, else "",\n'
    '  "posted"     : issuance/posted date YYYY-MM-DD, else "",\n'
    '  "closing"    : response/closing/submission deadline YYYY-MM-DD, else "",\n'
    '  "status"     : "open" | "closed" | "cancelled" | "unknown" (relative to today),\n'
    '  "category"   : "goods" | "services" | "construction" | "unknown",\n'
    '  "set_aside"  : any set-aside/eligibility restriction stated (e.g. "small business", '
    '"local vendors only"), else "" if full-and-open,\n'
    '  "demands"    : one tight sentence (<=140 chars) of WHAT they want to buy — the item/'
    'service, quantity or scope, and any must-have (e.g. "Supply & install 40 AC units, '
    '18-month warranty" or "1-yr janitorial contract, ~30 staff"),\n'
    '  "confidence" : 0.0-1.0 confidence in the closing date.\n'
    "today is {today}. TEXT:\n{body}"
)


# which model to use is discovered once per run from the key itself (adapts to
# whatever the account actually has), preferring Pro. Override with GEMINI_MODEL.
_GEMINI = {"model": None, "err": ""}
MODEL_PREF = ["2.5-pro", "1.5-pro", "-pro", "pro", "2.5-flash", "2.0-flash", "flash"]


def pick_gemini_model(key, timeout=30):
    """Ask the key which models it can use for generateContent; pick the best (Pro-first).
    Returns a model name, or '' if none/failure (reason stored in _GEMINI['err'])."""
    if _GEMINI["model"] is not None:
        return _GEMINI["model"]
    forced = os.getenv("GEMINI_MODEL")
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": key, "pageSize": 200}, timeout=timeout)
        if r.status_code != 200:
            _GEMINI["model"] = ""; _GEMINI["err"] = f"list HTTP {r.status_code}: {r.text[:90]}"
            return ""
        names = [m.get("name", "").split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        if forced and forced in names:
            _GEMINI["model"] = forced; return forced
        for pref in MODEL_PREF:
            for n in names:
                if pref in n and "vision" not in n:
                    _GEMINI["model"] = n; return n
        _GEMINI["model"] = names[0] if names else ""
        if not names:
            _GEMINI["err"] = "no generateContent models on this key"
        return _GEMINI["model"]
    except Exception as e:
        _GEMINI["model"] = ""; _GEMINI["err"] = str(e)[:100]
        return ""


def gemini_extract(text, timeout=60):
    """Ask Gemini (best available Pro model) to read the solicitation text. {} on any problem.
    Retries once on rate-limit (429) with a short backoff so free-tier limits don't
    silently drop items."""
    key = os.getenv("GEMINI_API_KEY")
    if not key or not (text or "").strip():
        return {}
    model = pick_gemini_model(key)
    if not model:
        return {"_gerr": _GEMINI["err"] or "no usable model"}
    today = datetime.now(timezone.utc).date().isoformat()
    body = re.sub(r"\s+", " ", text)[:18000]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"contents": [{"parts": [{"text": GEMINI_PROMPT.format(today=today, body=body)}]}],
               "generationConfig": {"temperature": 0, "response_mime_type": "application/json"}}
    for attempt in range(2):
        try:
            r = requests.post(url, params={"key": key}, json=payload, timeout=timeout)
            if r.status_code == 429 and attempt == 0:
                time.sleep(int(os.getenv("GEMINI_BACKOFF", "20"))); continue
            if r.status_code != 200:
                return {"_gerr": f"HTTP {r.status_code}: {r.text[:80]}"}
            cand = (r.json().get("candidates") or [{}])[0]
            raw = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [{}]))
            data = json.loads(raw)
            return {"title": (data.get("title") or "").strip()[:180],
                    "sol": (data.get("sol") or "").strip().upper(),
                    "posted": normalize_any(data.get("posted", "")) or (data.get("posted") or "")[:10],
                    "deadline": normalize_any(data.get("closing", "")) or (data.get("closing") or "")[:10],
                    "status": (data.get("status") or "unknown").lower(),
                    "category": (data.get("category") or "unknown").lower(),
                    "setaside": (data.get("set_aside") or "").strip()[:60],
                    "demands": (data.get("demands") or "").strip()[:160],
                    "confidence": float(data.get("confidence") or 0)}
        except Exception as e:
            return {"_gerr": str(e)[:80]}
    return {"_gerr": "rate-limited"}


def _apply_cache(it, c):
    """Copy everything we've learned about a document onto the live item."""
    if c.get("deadline"): it["deadline"] = c["deadline"]
    if c.get("posted") and not it.get("posted"): it["posted"] = c["posted"]
    if c.get("sol") and not it.get("sol"): it["sol"] = c["sol"]
    if c.get("setaside") and not it.get("setaside"): it["setaside"] = c["setaside"]
    if c.get("demands"): it["demands"] = c["demands"]
    if c.get("title") and (GENERIC_LABEL.match(it.get("text", "")) or len(it.get("text", "")) < 12):
        it["text"] = c["title"]                        # upgrade a junky title with the read one
    if c.get("cancelled"): it["_cancelled"] = True
    if c.get("src"): it["_datesrc"] = c["src"]         # provenance: regex vs gemini


def enrich_deep(items, cache, budget, recheck_days=7, gemini_budget=None):
    """Open each solicitation document ONCE, read its real dates + details, cache forever.
    Token/bandwidth saver: a document that already has a confirmed deadline in the cache is
    NEVER opened again; only still-undated ('unverified') documents are re-checked, and only
    after `recheck_days`. Regex reads first; Gemini (Pro) is asked only when regex finds no
    closing date. `budget` caps downloads/run; `gemini_budget` caps AI calls/run."""
    today = datetime.now(timezone.utc).date().isoformat()
    gem_on = bool(os.getenv("GEMINI_API_KEY"))
    if gemini_budget is None:
        gemini_budget = int(os.getenv("GEMINI_BUDGET", "30"))
    used = gem_used = 0
    for it in items:
        if (it.get("deadline") or "").strip():
            continue                                   # already dated (e.g. by SAM) — trust it
        href = it.get("href") or ""
        c = cache.get(href)
        # ---- decide whether this document needs opening at all ----
        if c:
            if c.get("deadline") or c.get("cancelled"):
                _apply_cache(it, c); continue          # settled once — never re-read (saves tokens)
            seen_ago = days_since(c.get("checked", ""))
            if seen_ago is not None and seen_ago < recheck_days:
                _apply_cache(it, c); continue          # undated but checked recently — wait
        if used >= budget:
            if c: _apply_cache(it, c)
            continue                                    # out of download budget this run
        # ---- open and read the document ----
        info = deep_read(href)
        used += 1
        c = {"deadline": info.get("deadline", ""), "posted": info.get("posted", ""),
             "cancelled": bool(info.get("cancelled")), "title": info.get("title", ""),
             "sol": info.get("sol", ""), "setaside": "", "demands": "",
             "checked": today, "err": bool(info.get("_err")), "src": "regex" if info.get("deadline") else ""}
        # ---- AI fallback: regex found no closing date but we have the text → ask Gemini (Pro) ----
        if gem_on and not c["deadline"] and info.get("_text") and gem_used < gemini_budget:
            g = gemini_extract(info["_text"])
            gem_used += 1
            if g and not g.get("_gerr"):
                if g.get("deadline"): c["deadline"] = g["deadline"]; c["src"] = "gemini"
                if g.get("posted") and not c["posted"]: c["posted"] = g["posted"]
                if g.get("sol") and not c["sol"]: c["sol"] = g["sol"]
                if g.get("title") and not c["title"]: c["title"] = g["title"]
                if g.get("setaside"): c["setaside"] = g["setaside"]
                if g.get("demands"): c["demands"] = g["demands"]
                if g.get("status") == "cancelled": c["cancelled"] = True
                c["gcat"] = g.get("category", ""); c["gconf"] = g.get("confidence", 0)
            elif g.get("_gerr"):
                c["gerr"] = g["_gerr"]
        cache[href] = c
        _apply_cache(it, c)
    return used
DATE_RE = re.compile(r"(\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+20\d{2}\b"
                     r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2}\b"
                     r"|\b20\d{2}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/20\d{2}\b)", re.I)
CANCEL_RE = re.compile(r"(cancel|withdrawn|no longer available)", re.I)
AMEND_RE = re.compile(r"(amendment|modif|\bsf-?30\b|extension|revised|addendum|response to quer|\bp0000\d\b|q&a)", re.I)
TRAP_RE = re.compile(r"(oil|gas|fuel|petroleum|weapon|ammun|firearm|\barms\b|ship repair|aircraft|aviation"
                     r"|\bmilitary\b|guard service|security guard|staffing|personal services|janitor|catering"
                     r"|perishable|construction|renovat|refurb|make ?ready|demolition|roofing|paving|excavat)", re.I)

SAM_SKIP_TYPES = {"Award Notice", "Justification", "Justification and Approval (J&A)",
                  "Sale of Surplus Property", "Intent to Bundle Requirements (DoD-Funded)"}


def slug(name): return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30); r.raise_for_status(); return r.text
        except Exception as e:
            last = e; time.sleep(2 * (i + 1))
    raise last


def extract(html, base_url, selector=None):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "form", "iframe"]): tag.decompose()
    for sel in ["header", "footer", "nav", ".menu", "#menu", ".site-header", ".site-footer",
                ".cookie", ".social", ".breadcrumb"]:
        for t in soup.select(sel): t.decompose()
    scope = soup
    if selector and soup.select_one(selector):
        scope = soup.select_one(selector)
    else:
        for sel in ["main", "article", "#content", ".entry-content", ".page-content", ".content"]:
            if soup.select_one(sel): scope = soup.select_one(sel); break
    items, seen, emails = [], set(), set()
    page = base_url.split("#")[0].rstrip("/")
    for a in scope.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        raw = a["href"].strip()
        href = urljoin(base_url, raw)
        if not text: continue
        for m in EMAIL_RE.findall(text + " " + href):
            if "state.gov" in m or "usembassy" in m: emails.add(m)
        if EMAIL_RE.fullmatch(text) or JUNK.search(text): continue
        href_nofrag = href.split("#")[0]
        # drop in-page anchors / same-page fragment links (navigation, section headers, tabs)
        if raw.startswith("#") or href_nofrag.rstrip("/") == page:
            continue
        is_doc = href_nofrag.lower().endswith((".pdf", ".doc", ".docx"))
        has_sol = bool(SOLNUM.search(text) or SOLNUM.search(href))
        strong = bool(STRONG.search(text))
        # keep ONLY a real solicitation: has a solicitation number, or a document with a solicitation keyword
        if not (has_sol or (is_doc and strong)):
            continue
        disp = text
        if GENERIC_LABEL.match(text) or len(text) < 12:  # generic anchor text → title from the file name
            t2 = title_from_href(href)
            if t2 and len(t2) > len(text): disp = t2
        key = (disp.lower(), href_nofrag)
        if key in seen: continue
        seen.add(key)
        d = ""
        par = a.find_parent(["li", "tr", "p"])
        if par:
            md = DATE_RE.search(par.get_text(" ", strip=True))
            if md: d = normalize_date(md.group(0))
        items.append({"text": disp[:180], "href": href, "date": d})
    visible = " ".join(scope.get_text(" ", strip=True).split())
    return {"items": items, "text_hash": hashlib.sha256(visible.encode("utf-8", "ignore")).hexdigest(),
            "emails": sorted(emails)}


def solnum(text, href=""):
    m = SOLNUM.search(text) or SOLNUM.search(href)
    return m.group(0).upper() if m else ""


def classify(text):
    if CANCEL_RE.search(text): return "cancelled"
    if AMEND_RE.search(text): return "amendment"
    return "new"


def is_open(it):
    return not (it.get("setaside") or "").strip()


def is_goods(it):
    psc = (it.get("psc") or "").strip()
    return bool(psc) and psc[0].isdigit()


def is_trap(it):
    if TRAP_RE.search(it.get("text", "")): return True
    return (it.get("psc") or "")[:1].upper() == "Y"


def is_fit(it):
    return is_open(it) and is_goods(it) and not is_trap(it)


def days_until(deadline):
    try:
        d = datetime.strptime(deadline[:10], "%Y-%m-%d").date()
        return (d - datetime.now(timezone.utc).date()).days
    except Exception:
        return None


def days_since(date_str):
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return (datetime.now(timezone.utc).date() - d).days
    except Exception:
        return None


def stamp_first_seen(items, prev_items, today):
    pf = {(i["text"].lower(), i["href"]): i.get("first_seen") for i in (prev_items or [])}
    for it in items:
        it["first_seen"] = pf.get((it["text"].lower(), it["href"])) or today
    return items


def eff_status(it, recency_days):
    """Conservative status. We ONLY call something 'active' when a real, dated
    signal proves it: a response deadline in the future, OR a posting date within
    the recency window. Everything else is either 'closed' (its date has passed)
    or 'unverified' (no date at all — never counted as active, listed for a manual
    check). first_seen is deliberately NOT used here: the date WE first noticed a
    posting says nothing about whether the posting itself is still open, and using
    it was the cause of the false 'active' counts."""
    today = datetime.now(timezone.utc).date().isoformat()
    dl = (it.get("deadline") or "").strip()[:10]
    if dl:
        return "active" if dl >= today else "closed"
    posted = (it.get("posted") or "").strip()[:10]
    age = days_since(posted) if posted else None
    if age is None:
        return "unverified"          # no deadline, no posting date → cannot confirm
    return "active" if age <= recency_days else "closed"


def bidfit_key(it):
    # Your winnable universe (full-and-open AND not a trap) floats to the very top,
    # ordered by Goods/COTS then soonest deadline. Set-asides and traps sink below.
    biddable = is_open(it) and not is_trap(it)
    return (0 if biddable else 1,
            0 if is_goods(it) else 1,
            it.get("deadline") or "9999-12-31",
            0 if is_open(it) else 1,
            1 if is_trap(it) else 0)


def load_json(p, default):
    try: return json.load(open(p))
    except Exception: return default


def save_state(s, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(data, open(os.path.join(STATE_DIR, f"{s}.json"), "w"), indent=2, ensure_ascii=False)


def diff_items(old, new):
    ok = {(i["text"].lower(), i["href"]): i for i in old}
    nk = {(i["text"].lower(), i["href"]): i for i in new}
    return [nk[k] for k in nk if k not in ok], [ok[k] for k in ok if k not in nk]


# ---------------- SAM.gov API ----------------
def pull_sam():
    """Return (report_items, sol_index, note). Never raises.
    report_items = new/changed State Dept notices to show.
    sol_index    = {SOLNUM: {posted, deadline, href}} for ALL pulled notices,
                   used to stamp dates onto embassy items found by sol-number."""
    key = os.getenv("SAM_API_KEY")
    if not key:
        return [], {}, "SAM skipped — no SAM_API_KEY set."
    org = os.getenv("SAM_ORG", "STATE, DEPARTMENT OF")
    days = int(os.getenv("SAM_INDEX_DAYS", "90"))       # wide pull → active inventory + date index
    report_days = int(os.getenv("SAM_REPORT_DAYS", "3"))  # only this recent counts as "new/changed"
    now = datetime.now(timezone.utc)
    params = {"api_key": key, "organizationName": org,
              "postedFrom": (now - timedelta(days=days)).strftime("%m/%d/%Y"),
              "postedTo": now.strftime("%m/%d/%Y"), "limit": 1000, "offset": 0}
    seen = load_json(SAM_SEEN, {})
    out, index = [], {}
    try:
        records, offset, pages = [], 0, 0
        while pages < 8:
            params["offset"] = offset
            r = requests.get(SAM_URL, params=params, timeout=45)
            if r.status_code != 200:
                return [], {}, f"SAM API HTTP {r.status_code}: {r.text[:120]}"
            data = r.json()
            batch = data.get("opportunitiesData", []) or []
            records += batch
            total = data.get("totalRecords", len(records))
            offset += len(batch); pages += 1
            if len(batch) == 0 or offset >= total: break
        for rec in records:
            path = (rec.get("fullParentPathName") or "").upper()
            if "STATE, DEPARTMENT OF" not in path and org.upper() not in path:
                continue
            nid = rec.get("noticeId", "")
            posted = rec.get("postedDate", "")
            title = rec.get("title", "") or "(untitled)"
            sol = (rec.get("solicitationNumber") or nid or "").upper()
            pd = (posted or "")[:10]
            dl = (rec.get("responseDeadLine", "") or "")[:10]
            sa = (rec.get("typeOfSetAsideDescription") or rec.get("typeOfSetAside") or "").strip()
            psc = (rec.get("classificationCode") or "").strip()
            naics = (rec.get("naicsCode") or "").strip()
            pop = rec.get("placeOfPerformance") or {}
            country = ((pop.get("country") or {}).get("name")
                       or (pop.get("country") or {}).get("code") or "—")
            if sol:  # index EVERY notice (even already-seen) so embassy items can borrow its data
                index[sol] = {"posted": pd, "deadline": dl, "href": rec.get("uiLink", ""),
                              "setaside": sa, "psc": psc, "naics": naics,
                              "title": title, "country": country}
            if rec.get("type", "") in SAM_SKIP_TYPES:
                continue
            if nid in seen and seen[nid] == posted:
                continue  # unchanged — already reported
            recent = days_since(pd)
            if recent is None or recent > report_days:
                continue  # older notice: it's indexed for the active list, but not "new"
            cat = "amendment" if nid in seen else "new"
            if CANCEL_RE.search(title): cat = "cancelled"
            out.append({"name": f"SAM · {country}", "text": title[:180], "sol": sol,
                        "href": rec.get("uiLink", "https://sam.gov"), "cat": cat,
                        "source": "SAM", "posted": pd, "deadline": dl, "first_seen": pd,
                        "setaside": sa, "psc": psc, "naics": naics})
            seen[nid] = posted
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(SAM_SEEN, "w") as f:
            json.dump(seen, f)
        return out, index, f"SAM ok — {len(out)} new/changed, {len(index)} indexed of {len(records)} pulled."
    except Exception as e:
        return [], {}, f"SAM error: {str(e)[:140]}"


# ---------------- MAIN ----------------
def main():
    cfg = yaml.safe_load(open(SITES_FILE))
    sites = cfg.get("sites", [])
    site_items, errors, baseline_rows, gso_emails, current_site = [], [], [], {}, []
    checked, is_baseline = 0, False
    today = datetime.now(timezone.utc).date().isoformat()

    for site in sites:
        name, url, selector = site["name"], site["url"], site.get("selector")
        s = slug(name)
        try:
            fp = extract(fetch(url), url, selector)
        except Exception as e:
            errors.append({"name": name, "url": url, "err": str(e)[:160]}); continue
        checked += 1
        if fp["emails"]: gso_emails[name] = fp["emails"]
        prev = load_json(os.path.join(STATE_DIR, f"{s}.json"), None)
        stamp_first_seen(fp["items"], (prev or {}).get("items", []), today)
        for it in fp["items"]:  # every item currently on the page → active inventory
            current_site.append({"name": name, "text": it["text"], "href": it["href"],
                                 "sol": solnum(it["text"], it["href"]), "source": "Site",
                                 "posted": it.get("date", ""), "first_seen": it.get("first_seen", today),
                                 "deadline": "", "setaside": "", "psc": "", "naics": ""})
        if prev is None:
            is_baseline = True
            baseline_rows.append({"name": name, "count": len(fp["items"]), "url": url})
            save_state(s, {"url": url, **fp}); continue
        added, removed = diff_items(prev.get("items", []), fp["items"])
        for it in added:
            site_items.append({"name": name, "text": it["text"], "sol": solnum(it["text"], it["href"]),
                               "href": it["href"], "cat": classify(it["text"]), "source": "Site",
                               "posted": it.get("date", ""), "first_seen": it.get("first_seen", today),
                               "deadline": "", "setaside": "", "psc": "", "naics": ""})
        for it in removed:
            site_items.append({"name": name, "text": it["text"] + " (removed from page)",
                               "sol": solnum(it["text"], it["href"]), "href": it["href"],
                               "cat": "cancelled", "source": "Site",
                               "posted": it.get("date", ""), "first_seen": it.get("first_seen", today),
                               "deadline": "", "setaside": "", "psc": "", "naics": ""})
        if not added and not removed and prev.get("text_hash") != fp["text_hash"]:
            site_items.append({"name": name, "text": "Page content changed (check listing)",
                               "sol": "", "href": url, "cat": "updated", "source": "Site",
                               "posted": "", "first_seen": today, "deadline": "",
                               "setaside": "", "psc": "", "naics": ""})
        save_state(s, {"url": url, **fp})

    if is_baseline:
        sam_items, sam_index, sam_note = [], {}, "SAM skipped on baseline run"
    else:
        sam_items, sam_index, sam_note = pull_sam()

    # stamp SAM data (dates, set-aside, PSC/NAICS) onto embassy items sharing a solicitation number
    for it in site_items:
        s = (it.get("sol") or "").upper()
        if s and s in sam_index:
            rec = sam_index[s]
            if not it.get("posted"): it["posted"] = rec["posted"]
            if not it.get("deadline"): it["deadline"] = rec["deadline"]
            it["setaside"] = it.get("setaside") or rec.get("setaside", "")
            it["psc"] = it.get("psc") or rec.get("psc", "")
            it["naics"] = it.get("naics") or rec.get("naics", "")
            if it["source"] == "Site": it["source"] = "Site+SAM"

    # merge Site + SAM by solicitation number
    buckets = {"new": [], "amendment": [], "cancelled": [], "updated": []}
    if not is_baseline:
        merged, order = {}, []
        for it in site_items + sam_items:
            k = it["sol"] if it["sol"] else f"_{len(order)}_{id(it)}"
            if k in merged:
                merged[k]["source"] = "Site+SAM"
            else:
                merged[k] = it; order.append(k)
        for k in order:
            buckets[merged[k]["cat"]].append(merged[k])
        for b in buckets.values():
            b.sort(key=bidfit_key)  # Open + Goods/COTS + soonest deadline float to top; traps sink

    counts = {k: len(v) for k, v in buckets.items()}
    total = sum(counts.values())

    # ---------- ACTIVE INVENTORY (only postings with a real, dated signal) ----------
    # RECENCY_DAYS: an embassy posting with NO published deadline is presumed active
    # only if it was posted within this many days; older-or-undated ones drop to
    # closed/unverified and are NEVER counted as active. This is the fix for the
    # false "active" counts (e.g. Oman showing 6 when the site had none open).
    recency = int(os.getenv("RECENCY_DAYS", "30"))
    active, unverified = {}, {}
    deep_used = 0
    if not is_baseline:
        # (1) enrich from the SAM index by solicitation number
        for it in current_site:
            sol = (it.get("sol") or "").upper()
            if sol and sol in sam_index:
                rec = sam_index[sol]
                if not it.get("posted"): it["posted"] = rec.get("posted", "")
                it["deadline"] = it.get("deadline") or rec.get("deadline", "")
                it["setaside"] = it.get("setaside") or rec.get("setaside", "")
                it["psc"] = it.get("psc") or rec.get("psc", "")
                it["naics"] = it.get("naics") or rec.get("naics", "")
                it["source"] = "Site+SAM"
        # (2) DEEP READ — open the documents still missing a deadline and read them
        if os.getenv("DEEP_READ", "1") == "1":
            deep_cache = load_json(DEEP_CACHE, {})
            budget = int(os.getenv("DEEP_BUDGET", "60"))
            deep_used = enrich_deep(current_site, deep_cache, budget)
            os.makedirs(STATE_DIR, exist_ok=True)
            json.dump(deep_cache, open(DEEP_CACHE, "w"), ensure_ascii=False)
            if os.getenv("GEMINI_API_KEY"):
                if _GEMINI.get("model"):
                    sam_note += f" · AI brain: {_GEMINI['model']}"
                elif _GEMINI.get("err"):
                    sam_note += f" · AI off ({_GEMINI['err'][:50]})"
        # (3) classify each with real dates now in hand
        for it in current_site:
            if it.get("_cancelled"):
                continue                      # document says cancelled/awarded → not active
            st = eff_status(it, recency)
            k = (it.get("sol") or "").upper() or ("_" + it["href"])
            if st == "active":
                active[k] = it
            elif st == "unverified":
                unverified[k] = it            # no date even after opening it → manual check
        for sol, d in sam_index.items():  # SAM-only open items (from the wide index)
            if sol in active:
                active[sol]["source"] = "Site+SAM"; continue
            it = {"name": f"SAM · {d.get('country', '—')}", "text": (d.get("title", "") or "")[:180],
                  "sol": sol, "href": d.get("href", "https://sam.gov"), "source": "SAM",
                  "posted": d.get("posted", ""), "first_seen": d.get("posted", ""),
                  "deadline": d.get("deadline", ""), "setaside": d.get("setaside", ""),
                  "psc": d.get("psc", ""), "naics": d.get("naics", "")}
            # SAM records always carry a posted date, so they resolve to active/closed,
            # never unverified. Only genuinely open ones enter the active list.
            if eff_status(it, recency) == "active":
                active[sol] = it
    active_list = sorted(active.values(), key=bidfit_key)
    unverified_list = sorted(unverified.values(), key=lambda it: (it.get("name", ""), it.get("text", "")))
    winnable = [it for it in active_list if is_fit(it)]
    closing_soon = sorted([it for it in active_list if it.get("deadline")
                           and days_until(it["deadline"]) is not None and 0 <= days_until(it["deadline"]) <= 7],
                          key=lambda it: it.get("deadline") or "9999")

    html = build_html(buckets, counts, errors, checked, baseline_rows, gso_emails,
                      is_baseline, sam_note, len(sam_items),
                      active_list, winnable, closing_soon, unverified_list, len(sites))

    if not is_baseline and deep_used:
        sam_note += f" · deep-read {deep_used} documents"
    attachments = []
    if not is_baseline and active_list:  # full active list attached to EVERY digest
        attachments.append((f"active_solicitations_{today}.csv", build_active_csv(active_list), "text/csv"))
        sam_note += f" · {len(active_list)} active ({len(winnable)} winnable), {len(unverified_list)} unverified — CSV attached"

    send = bool(total or errors or is_baseline or attachments) or os.getenv("SEND_DAILY_DIGEST") == "1"
    subject = build_subject(counts, errors, is_baseline)
    if not is_baseline and closing_soon:
        subject = f"[{len(closing_soon)} closing ≤7d] " + subject
    if send: send_email(subject, html, attachments); print("EMAIL SENT:", subject)
    else: print("No changes — no email.")
    print(f"site_changes={len(site_items)} sam_new={len(sam_items)} active={len(active_list)} errors={len(errors)} | {sam_note}")


def build_subject(counts, errors, baseline):
    d = datetime.now(timezone.utc).strftime("%d %b %Y")
    if baseline:
        return f"Overseas Procurement Monitoring — Initial Baseline Established ({d})"
    bits = []
    if counts["new"]: bits.append(f"{counts['new']} New")
    if counts["amendment"]: bits.append(f"{counts['amendment']} Amended")
    if counts["cancelled"]: bits.append(f"{counts['cancelled']} Closed")
    if counts["updated"]: bits.append(f"{counts['updated']} Updated")
    if errors: bits.append(f"{len(errors)} Unreachable")
    body = "; ".join(bits) if bits else "No New Procurement Actions"
    return f"Overseas Procurement Report — {body} ({d})"


# ============================================================================
#  EMAIL DESIGN SYSTEM  —  white ground · sky-blue frame · red/orange/green alerts
# ============================================================================
# Semantic alert palette (bg / tint / ink) — used everywhere so colour = meaning
GREEN  = ("#15803d", "#dcfce7", "#166534")   # good — active / open / winnable
ORANGE = ("#c2410c", "#ffedd5", "#9a3412")   # caution — unverified / amended / set-aside
RED    = ("#b91c1c", "#fee2e2", "#991b1b")   # urgent — closing / cancelled / trap
SKY    = ("#0369a1", "#e0f2fe", "#075985")   # neutral information / updated
INK     = "#0f172a"   # near-black headings
SUBINK  = "#334155"   # body
MUTE    = "#64748b"   # captions
LINE    = "#cbd5e1"   # borders
FAINT   = "#e2e8f0"   # hairlines
PALE    = "#f0f9ff"   # sky-50 zebra
CARD    = "#ffffff"

CAT_STYLE = {"new":       ("I. NEW SOLICITATIONS",             GREEN[0]),
             "amendment": ("II. AMENDMENTS &amp; MODIFICATIONS", ORANGE[0]),
             "cancelled": ("III. CANCELLATIONS &amp; CLOSURES",  RED[0]),
             "updated":   ("IV. UPDATED POSTINGS",               SKY[0])}
SRC_COLOR = {"Site": "#0369a1", "SAM": "#0e7490", "Site+SAM": "#15803d"}


def _tile(label, value, color, note=""):
    sub = (f'<div style="font-size:8px;color:{MUTE};font-family:Arial;margin-top:2px">{note}</div>') if note else ''
    return (f'<td align="center" width="12%" style="padding:13px 4px;background:{CARD};border:1px solid {FAINT};'
            f'border-top:3px solid {color}">'
            f'<div style="font-size:27px;font-weight:800;color:{color};font-family:Georgia,serif;line-height:1">{value}</div>'
            f'<div style="font-size:8.5px;letter-spacing:.5px;margin-top:5px;color:{SUBINK};'
            f'font-family:Arial;text-transform:uppercase;font-weight:700">{label}</div>{sub}</td>')


def _pill(text, tint, ink):
    return (f'<span style="background:{tint};color:{ink};font-size:9px;padding:2px 6px;'
            f'border-radius:3px;margin:0 4px 3px 0;white-space:nowrap;font-family:Arial;display:inline-block;'
            f'letter-spacing:.3px;text-transform:uppercase;font-weight:700">{text}</span>')


def _status_of(it):
    """Return (label, tint, ink, sort_rank) describing openness for the STATUS column."""
    dl = (it.get("deadline") or "").strip()[:10]
    if dl:
        n = days_until(dl)
        if n is None:
            return ("Open", GREEN[1], GREEN[2], 2)
        if n < 0:
            return ("Closed", "#f1f5f9", MUTE, 9)
        if n <= 7:
            return (f"Closing · {n}d", RED[1], RED[2], 0)
        if n <= 21:
            return (f"Open · {n}d left", ORANGE[1], ORANGE[2], 1)
        return ("Open", GREEN[1], GREEN[2], 2)
    posted = (it.get("posted") or "").strip()[:10]
    if posted:
        return ("Open · no deadline", GREEN[1], GREEN[2], 3)
    return ("Unverified", ORANGE[1], ORANGE[2], 5)


def _badges(it):
    b = []
    if is_fit(it):
        b.append(_pill("★ Priority Target", GREEN[1], GREEN[2]))
    if is_open(it):
        b.append(_pill("Full &amp; Open", GREEN[1], GREEN[2]))
    else:
        b.append(_pill("Set-Aside · " + (it.get("setaside", "")[:20] or "Restricted"), ORANGE[1], ORANGE[2]))
    if is_goods(it):
        b.append(_pill("Commercial Goods", SKY[1], SKY[2]))
    elif it.get("psc"):
        b.append(_pill("Services", "#f1f5f9", MUTE))
    if is_trap(it):
        b.append(_pill("⚠ Out of Scope", RED[1], RED[2]))
    ds = it.get("_datesrc")
    if ds == "gemini":
        b.append(_pill("✓ AI-read from document", SKY[1], SKY[2]))
    elif ds == "regex":
        b.append(_pill("✓ Read from document", SKY[1], SKY[2]))
    meta = []
    if it.get("psc"): meta.append("PSC " + it["psc"])
    if it.get("naics"): meta.append("NAICS " + it["naics"])
    m = (f' <span style="color:{MUTE};font-size:10px;font-family:Arial">' + " · ".join(meta) + '</span>') if meta else ''
    return '<div style="margin-top:6px">' + "".join(b) + m + '</div>'


def _demands_line(it):
    d = (it.get("demands") or "").strip()
    if not d:
        return ""
    return (f'<div style="margin-top:4px;font-family:Arial;font-size:12px;color:{SUBINK};line-height:1.45">'
            f'<span style="background:{SKY[1]};color:{SKY[2]};font-size:9px;font-weight:700;padding:1px 5px;'
            f'border-radius:3px;text-transform:uppercase;letter-spacing:.3px">Requires</span> {d}</div>')


def _table_head(cols):
    th = "".join(f'<td style="padding:8px 10px;font-size:9.5px;color:{CARD};font-family:Arial;'
                 f'letter-spacing:.5px;font-weight:700;text-transform:uppercase">{c}</td>' for c in cols)
    return ('<table width="100%" cellspacing="0" cellpadding="0" '
            f'style="background:{CARD};border:1px solid {LINE};border-top:none;border-collapse:collapse">'
            f'<tr style="background:{SKY[0]}">{th}</tr>')


def _datecell(it):
    posted, deadline = it.get("posted", ""), it.get("deadline", "")
    dc = []
    if deadline:
        n = days_until(deadline)
        if n is not None and n < 0:
            dc.append(f'<span style="color:{MUTE}">Closed {deadline}</span>')
        elif n is not None and n <= 7:
            dc.append(f'<span style="background:{RED[0]};color:#fff;padding:2px 7px;border-radius:3px;'
                      f'font-weight:700;font-size:11px">Closes in {n} day{"s" if n != 1 else ""}</span>'
                      f'<br><span style="color:{RED[2]};font-weight:700">Due {deadline}</span>')
        else:
            dc.append(f'<span style="color:{RED[2]};font-weight:700">Due {deadline}</span>')
    if posted:
        dc.append(f'<span style="color:{SUBINK}">Posted {posted}</span>')
    return "<br>".join(dc) if dc else f'<span style="color:{MUTE}">— no date —</span>'


def _rows(items, zebra=True):
    out = []
    for i, it in enumerate(items):
        src = it.get("source", "Site"); c = SRC_COLOR.get(src, "#0369a1")
        lbl, tint, ink, _ = _status_of(it)
        bg = PALE if (zebra and i % 2) else CARD
        view = (f'<a href="{it["href"]}" style="display:inline-block;background:{SKY[0]};color:#fff;'
                f'font-family:Arial;font-size:11px;padding:6px 12px;border-radius:4px;text-decoration:none;'
                f'font-weight:700;white-space:nowrap">Open &#8599;</a>')
        status = (f'<span style="display:inline-block;background:{tint};color:{ink};font-size:10px;'
                  f'font-weight:700;padding:3px 8px;border-radius:3px;font-family:Arial;white-space:nowrap;'
                  f'text-transform:uppercase;letter-spacing:.3px">{lbl}</span>')
        out.append(
            f'<tr style="background:{bg}">'
            f'<td style="padding:11px 10px;border-bottom:1px solid {FAINT};font-family:Arial;font-size:13px;white-space:nowrap;vertical-align:top">'
            f'<div style="font-weight:700;color:{INK}">{it["name"]}</div>'
            f'<span style="background:{c};color:#fff;font-size:9px;padding:1px 6px;border-radius:3px;font-weight:700">{src}</span></td>'
            f'<td style="padding:11px 10px;border-bottom:1px solid {FAINT};font-family:Georgia,serif;font-size:13px;color:{INK};vertical-align:top">'
            f'{it["text"]}{_demands_line(it)}{_badges(it)}</td>'
            f'<td style="padding:11px 10px;border-bottom:1px solid {FAINT};font-family:monospace;font-size:12px;'
            f'color:{SUBINK};white-space:nowrap;vertical-align:top">{it.get("sol") or "—"}</td>'
            f'<td style="padding:11px 10px;border-bottom:1px solid {FAINT};font-family:Arial;font-size:12px;'
            f'white-space:nowrap;vertical-align:top">{_datecell(it)}</td>'
            f'<td style="padding:11px 10px;border-bottom:1px solid {FAINT};vertical-align:top;text-align:center">{status}</td>'
            f'<td style="padding:11px 10px;border-bottom:1px solid {FAINT};vertical-align:top">{view}</td></tr>')
    return "".join(out)


HEAD6 = ("U.S. Mission / Source", "Solicitation", "Ref. No.", "Dates", "Status", "Action")


def _section(title, color, count):
    return (f'<div style="margin-top:22px;background:{color};color:#fff;padding:10px 14px;'
            f'font-family:Georgia,serif;font-size:13px;font-weight:700;letter-spacing:.5px;'
            f'border-radius:4px 4px 0 0">{title} <span style="opacity:.85;font-family:Arial;'
            f'font-size:12px">({count})</span></div>')


def _legend():
    def sw(tint, ink, txt):
        return (f'<td style="padding:6px 10px;font-family:Arial;font-size:10.5px;color:{SUBINK}">'
                f'<span style="display:inline-block;width:11px;height:11px;border-radius:2px;background:{ink};'
                f'vertical-align:middle;margin-right:6px"></span>{txt}</td>')
    return ('<table width="100%" cellspacing="0" cellpadding="0" '
            f'style="margin-top:14px;background:{CARD};border:1px solid {FAINT};border-collapse:collapse">'
            f'<tr style="background:{PALE}"><td colspan="4" style="padding:6px 10px;font-family:Arial;'
            f'font-size:9.5px;font-weight:700;letter-spacing:.5px;color:{MUTE};text-transform:uppercase">Alert Legend</td></tr>'
            f'<tr>{sw(RED[1], RED[0], "RED — closing ≤7 days / cancelled / out of scope")}'
            f'{sw(ORANGE[1], ORANGE[0], "ORANGE — amended / set-aside / unverified")}</tr>'
            f'<tr>{sw(GREEN[1], GREEN[0], "GREEN — active &amp; open / priority target")}'
            f'{sw(SKY[1], SKY[0], "BLUE — informational / updated posting")}</tr></table>')


def build_html(buckets, counts, errors, checked, baseline_rows, gso_emails, is_baseline, sam_note, sam_count,
               active_list=None, winnable=None, closing_soon=None, unverified_list=None, total_sites=0):
    active_list = active_list or []; winnable = winnable or []
    closing_soon = closing_soon or []; unverified_list = unverified_list or []
    active_count, winnable_count = len(active_list), len(winnable)
    now = datetime.now(timezone.utc)
    ref = "MM-OPR-" + now.strftime("%Y%m%d-%H%M")
    period = now.strftime("%A, %d %B %Y · %H:%M UTC")

    S = [f'<div style="max-width:840px;margin:0 auto;background:#eef4f9;font-family:Arial,sans-serif;padding-bottom:2px">']
    # ---- Masthead: white ground, black logo (bigger), sky-blue rule ----
    S.append(f'<table width="100%" cellspacing="0" cellpadding="0" style="background:{CARD}"><tr>'
             f'<td width="112" style="padding:20px 8px 20px 26px" valign="middle">'
             f'<img src="cid:mmlogo" alt="Madison &amp; Main LLC" height="86" style="display:block"></td>'
             f'<td style="padding:20px 26px;text-align:right" valign="middle">'
             f'<div style="color:{INK};font-family:Georgia,serif;font-size:23px;font-weight:800;letter-spacing:1px">MADISON &amp; MAIN LLC</div>'
             f'<div style="color:{SKY[0]};font-family:Arial;font-size:11px;letter-spacing:2.5px;text-transform:uppercase;margin-top:4px;font-weight:700">Overseas Procurement Monitoring</div>'
             f'</td></tr></table>')
    S.append(f'<div style="height:4px;background:{SKY[0]}"></div>')
    # ---- Report metadata ----
    S.append(f'<table width="100%" cellspacing="0" style="background:{PALE};border-bottom:1px solid {LINE}"><tr>'
             f'<td style="padding:14px 26px;font-family:Arial;font-size:11px;color:{SUBINK};line-height:1.7">'
             f'<span style="color:{INK};font-size:13px;font-family:Georgia,serif;font-weight:700">PROCUREMENT INTELLIGENCE REPORT</span><br>'
             f'Report Reference: <b>{ref}</b> &nbsp;|&nbsp; Reporting Period: {period}<br>'
             f'Sources Monitored: {checked} of {total_sites} U.S. mission portals reachable + SAM.gov (Governmentwide Point of Entry)'
             f'</td></tr></table>')
    S.append('<div style="padding:0 26px 6px">')

    if is_baseline:
        S.append(f'<p style="font-family:Georgia,serif;font-size:13px;color:{INK};line-height:1.7;margin:18px 0">'
                 'This transmission establishes the <b>initial baseline</b> of active solicitations across the monitored '
                 'U.S. mission portals. Subsequent reports will identify only new, amended, closed, or modified actions, '
                 'accompanied by the complete active inventory and the SAM.gov feed.</p>')
        S.append(f'<table width="100%" cellspacing="0" style="background:{CARD};border:1px solid {LINE};border-collapse:collapse">'
                 f'<tr style="background:{SKY[0]};color:#fff"><td style="padding:9px 12px;font-size:11px;font-family:Arial;letter-spacing:.5px;font-weight:700">U.S. MISSION</td>'
                 f'<td style="padding:9px 12px;font-size:11px;font-family:Arial;letter-spacing:.5px;font-weight:700">ACTIVE POSTINGS</td></tr>')
        for i, r in enumerate(sorted(baseline_rows, key=lambda x: -x["count"])):
            bg = PALE if i % 2 else CARD
            S.append(f'<tr style="background:{bg}"><td style="padding:8px 12px;border-bottom:1px solid {FAINT};font-size:13px;font-family:Arial">'
                     f'<b style="color:{INK}">{r["name"]}</b></td>'
                     f'<td style="padding:8px 12px;border-bottom:1px solid {FAINT};font-size:13px;font-family:Arial;font-weight:700;color:{SKY[0]}">{r["count"]}</td></tr>')
        S.append('</table>')
    else:
        # ===== EXECUTIVE SUMMARY tiles =====
        S.append(f'<div style="font-family:Georgia,serif;font-size:14px;color:{INK};font-weight:700;'
                 'margin:22px 0 10px;text-transform:uppercase;letter-spacing:.6px;'
                 f'border-bottom:2px solid {SKY[0]};padding-bottom:5px">Executive Summary</div>')
        S.append('<table width="100%" cellspacing="6" cellpadding="0"><tr>')
        S.append(_tile("New", counts["new"], GREEN[0]))
        S.append(_tile("Amended", counts["amendment"], ORANGE[0]))
        S.append(_tile("Closed", counts["cancelled"], RED[0]))
        S.append(_tile("Updated", counts["updated"], SKY[0]))
        S.append(_tile("Active", active_count, "#0e7490"))
        S.append(_tile("Winnable", winnable_count, GREEN[0], "priority"))
        S.append(_tile("Unverified", len(unverified_list), ORANGE[0], "manual"))
        S.append(_tile("Unreachable", len(errors), MUTE))
        S.append('</tr></table>')

        # ===== headline banner (sky) =====
        S.append(f'<table width="100%" cellspacing="0" style="margin-top:14px;background:{SKY[0]};border-radius:4px"><tr>'
                 f'<td style="padding:16px 20px;color:#fff;font-family:Arial">'
                 f'<span style="font-family:Georgia,serif;font-size:17px;font-weight:800">{active_count} solicitations currently active</span>'
                 f'<span style="color:#bae6fd;font-size:13px"> &nbsp;·&nbsp; {winnable_count} meet priority criteria (full-and-open commercial goods)</span>'
                 f'<div style="font-size:11px;color:#e0f2fe;margin-top:6px;line-height:1.6">Every active posting is listed in the '
                 f'<b>Complete Active Inventory</b> below and in the attached CSV, with its posting date and response deadline. '
                 f'Only postings proven open by a real date are counted here; undated ones are held separately for manual check.</div>'
                 f'</td></tr></table>')

        S.append(_legend())

        # ===== CLOSING ≤7 DAYS (RED) =====
        if closing_soon:
            S.append(_section("RESPONSE DEADLINE IMMINENT — CLOSING WITHIN 7 DAYS", RED[0], len(closing_soon)))
            S.append(_table_head(HEAD6)); S.append(_rows(closing_soon[:25])); S.append('</table>')

        # ===== change buckets =====
        for cat in ["new", "amendment", "cancelled", "updated"]:
            if not buckets[cat]: continue
            title, color = CAT_STYLE[cat]
            S.append(_section(title, color, counts[cat]))
            S.append(_table_head(HEAD6)); S.append(_rows(buckets[cat])); S.append('</table>')
        if not any(buckets.values()) and not errors:
            S.append(f'<div style="margin-top:18px;background:{GREEN[1]};border:1px solid #86efac;border-radius:4px;'
                     f'padding:14px 16px;font-family:Georgia,serif;font-size:13px;color:{GREEN[2]};line-height:1.6">'
                     'No new procurement actions were identified during this reporting cycle. '
                     'The complete active inventory below remains current.</div>')

        # ===== COMPLETE ACTIVE INVENTORY (full detailed table, GREEN header) =====
        if active_list:
            S.append(_section("COMPLETE ACTIVE INVENTORY — ALL OPEN SOLICITATIONS", "#0e7490", active_count))
            cap = 100
            S.append(_table_head(HEAD6)); S.append(_rows(active_list[:cap])); S.append('</table>')
            if active_count > cap:
                S.append(f'<div style="background:{PALE};border:1px solid {FAINT};border-top:none;padding:9px 14px;'
                         f'font-family:Arial;font-size:11px;color:{SUBINK}">Showing the top {cap} of {active_count} active '
                         f'solicitations (ranked winnable-first). The full set of {active_count} is in the attached CSV.</div>')

        # ===== UNVERIFIED (ORANGE) =====
        if unverified_list:
            S.append(_section("UNVERIFIED — NO PUBLISHED DATE, MANUAL CHECK ADVISED", ORANGE[0], len(unverified_list)))
            S.append(f'<div style="background:{ORANGE[1]};border:1px solid #fdba74;border-top:none;padding:9px 14px;'
                     f'font-family:Arial;font-size:11px;color:{ORANGE[2]};line-height:1.6">These postings carry no response '
                     f'deadline or posting date we could read, so we do <b>not</b> count them as active. Open each to confirm '
                     f'whether it is still live.</div>')
            S.append(_table_head(HEAD6)); S.append(_rows(unverified_list[:40])); S.append('</table>')

    # ===== SOURCES UNREACHABLE =====
    if errors:
        S.append(_section("SOURCES UNREACHABLE — MANUAL VERIFICATION REQUIRED", MUTE, len(errors)))
        S.append(f'<table width="100%" cellspacing="0" style="background:{CARD};border:1px solid {LINE};border-collapse:collapse">')
        for i, e in enumerate(errors):
            bg = PALE if i % 2 else CARD
            S.append(f'<tr style="background:{bg}"><td style="padding:9px 12px;border-bottom:1px solid {FAINT};font-size:13px;font-family:Arial;font-weight:700;color:{INK}">{e["name"]}</td>'
                     f'<td style="padding:9px 12px;border-bottom:1px solid {FAINT};font-size:12px;font-family:Arial;color:{MUTE}">{e["err"]}</td>'
                     f'<td style="padding:9px 12px;border-bottom:1px solid {FAINT}"><a href="{e["url"]}" style="background:{SKY[0]};color:#fff;font-family:Arial;font-size:11px;padding:6px 12px;border-radius:4px;text-decoration:none;font-weight:700">Open &#8599;</a></td></tr>')
        S.append('</table>')

    # ===== baseline contacts =====
    if is_baseline and gso_emails:
        S.append(_section("MISSION PROCUREMENT CONTACTS IDENTIFIED", "#0e7490", len(gso_emails)))
        S.append(f'<table width="100%" cellspacing="0" style="background:{CARD};border:1px solid {LINE};border-collapse:collapse">')
        for i, (name, addrs) in enumerate(gso_emails.items()):
            bg = PALE if i % 2 else CARD
            S.append(f'<tr style="background:{bg}"><td style="padding:9px 12px;border-bottom:1px solid {FAINT};font-size:13px;font-family:Arial;font-weight:700;color:{INK}">{name}</td>'
                     f'<td style="padding:9px 12px;border-bottom:1px solid {FAINT};font-size:12px;font-family:Arial;color:{SKY[0]}">{", ".join(addrs)}</td></tr>')
        S.append('</table>')

    S.append('</div>')  # end body

    # ===== footer (sky) =====
    S.append(f'<div style="background:{SKY[0]};padding:20px 26px;font-family:Arial;font-size:10px;color:#dbeafe;line-height:1.7">'
             f'<b style="color:#fff;letter-spacing:.5px;font-size:11px">MADISON &amp; MAIN LLC</b> &mdash; Overseas Procurement Monitoring System<br>'
             f'This report is compiled from public U.S. Government procurement postings (SAM.gov) and official U.S. diplomatic '
             f'mission websites. It is prepared for the internal use of Madison &amp; Main LLC. A posting is counted as '
             f'<b>active</b> only when a response deadline in the future, or a posting date within the recency window, confirms '
             f'it is open; postings without any readable date are listed as <b>unverified</b> and never counted as active.<br>'
             f'<span style="color:#bae6fd">System status: {sam_note}</span></div>')
    S.append('</div>')
    return "".join(S)


def build_weekly_csv(index):
    """CSV of currently-open WINNABLE opportunities (Open + Goods/COTS + non-trap, not closed)."""
    today = datetime.now(timezone.utc).date().isoformat()
    rows = []
    for sol, d in index.items():
        it = {"setaside": d.get("setaside", ""), "psc": d.get("psc", ""), "text": d.get("title", "")}
        if not is_fit(it):
            continue
        dl = d.get("deadline", "")
        if dl and dl < today:
            continue  # closed
        rows.append([d.get("country", ""), d.get("title", ""), sol, d.get("psc", ""),
                     d.get("naics", ""), d.get("posted", ""), dl, d.get("href", "")])
    rows.sort(key=lambda r: r[6] or "9999-12-31")
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Post/Country", "Title", "Solicitation #", "PSC", "NAICS", "Posted", "Deadline", "Link"])
    w.writerows(rows)
    return buf.getvalue().encode("utf-8"), len(rows)


def build_active_csv(items):
    """Full list of currently-open solicitations with the date each appeared + deadline."""
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Post/Country", "Title", "What it demands", "Solicitation #", "Source",
                "Set-Aside (blank=Open)", "Type", "Trap?", "PSC", "NAICS",
                "Posted", "Deadline", "Date source", "Winnable"])
    for it in items:
        typ = "Goods/COTS" if is_goods(it) else ("Service" if it.get("psc") else "")
        w.writerow([it.get("name", ""), it.get("text", ""), it.get("demands", ""),
                    it.get("sol", ""), it.get("source", ""),
                    it.get("setaside", ""), typ, "TRAP" if is_trap(it) else "",
                    it.get("psc", ""), it.get("naics", ""),
                    it.get("posted", "") or it.get("first_seen", ""), it.get("deadline", ""),
                    it.get("_datesrc", ""), "YES" if is_fit(it) else ""])
    return buf.getvalue().encode("utf-8")


def send_email(subject, html, attachments=None):
    user, pw = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    to = os.getenv("ALERT_TO") or user
    if not (user and pw and to):
        print("!! Email not sent: missing secrets", file=sys.stderr); return
    root = MIMEMultipart("related")
    root["Subject"], root["From"], root["To"], root["Date"] = subject, user, to, formatdate(localtime=True)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("This report is best viewed in an HTML-capable mail client.", "plain"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    root.attach(alt)
    logo = os.path.join(HERE, "mm_logo.png")
    if os.path.exists(logo):
        try:
            with open(logo, "rb") as f:
                img = MIMEImage(f.read())
            img.add_header("Content-ID", "<mmlogo>")
            img.add_header("Content-Disposition", "inline", filename="mm_logo.png")
            root.attach(img)
        except Exception:
            pass
    for fname, data, mime in (attachments or []):
        part = MIMEApplication(data, _subtype=mime.split("/")[-1])
        part.add_header("Content-Disposition", "attachment", filename=fname)
        root.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, pw)
        server.sendmail(user, [x.strip() for x in to.split(",")], root.as_string())


if __name__ == "__main__":
    main()
