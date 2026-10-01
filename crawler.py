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

import analyzer, ai, fetcher, un_sources

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
    for href, text in fetcher.links(raw, final):
        if is_offsite(href):
            continue
        low = (href + " " + text).lower()
        if href.lower().split("?")[0].endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx")):
            files.append(href)
        elif any(k in low for k in cfg["proc_keywords"]) and root_host(href) == root_host(final):
            html_pages.append(href.split("#")[0])
    return list(dict.fromkeys(html_pages))[:15], list(dict.fromkeys(files))[:25]


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
def sam_search(cfg, limit=60):
    key = os.getenv("SAM_API_KEY", "")
    if not key:
        return []
    pf = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%m/%d/%Y")
    pt = datetime.date.today().strftime("%m/%d/%Y")
    params = {"api_key": key, "limit": str(limit), "postedFrom": pf, "postedTo": pt, "ptype": "o,k,r"}
    if SAM_NAICS:
        params["ncode"] = SAM_NAICS.split(",")[0]
    raw, ct, final = fetcher.get(cfg["sam"]["api"] + "?" + urllib.parse.urlencode(params))
    if not raw:
        return []
    try:
        return json.loads(raw).get("opportunitiesData", []) or []
    except Exception:
        return []


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


def is_domestic(op):
    """SAM notice performed inside the US? -> Domestic tab, else Overseas."""
    pop = op.get("placeOfPerformance") or {}
    if isinstance(pop, dict):
        c = ((pop.get("country") or {}).get("code") or (pop.get("country") or {}).get("name") or "")
        if c:
            return str(c).strip().upper() in ("US", "USA", "UNITED STATES")
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
            text, ok, fail = sam_unit(op)
            if len(text) < 180 or not looks_like_solicitation(text):
                continue
            h = unit_hash(text)
            if h in ledger:
                continue                        # another bot already did this one
            found += 1
            try:
                rec = adjudicate_unit(text, op.get("title", "SAM notice"))
            except ai.AllExhausted:
                st.beat(currentJob="AI quota exhausted — pausing (resumes next run)")
                break
            if transient(rec):
                continue
            ledger.add(h)
            nid = op.get("noticeId", "")
            link = cfg["sam"]["view"].replace("{id}", nid) if nid else "https://sam.gov/"
            pop = op.get("placeOfPerformance") or {}
            ctry = ((pop.get("country") or {}).get("name", "") if isinstance(pop, dict) else "")
            rows.append(to_row(rec, post=op.get("organizationName", "SAM.gov"), country=ctry,
                               source="SAM", link=link, platform="USGOV",
                               domestic=is_domestic(op),
                               files=(op.get("resourceLinks") or []), read_ok=ok, read_fail=fail,
                               sol_hint=op.get("solicitationNumber", "")))
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
                text, atts = un_sources.fetch_notice(url, opener)
                if len(text) < 180 or not looks_like_solicitation(text):
                    continue
                h = unit_hash(text)
                if h in ledger:
                    continue
                found += 1
                try:
                    rec = adjudicate_unit(text, f"{agency}: {title[:40]}")
                except ai.AllExhausted:
                    st.beat(currentJob="AI quota exhausted — pausing (resumes next run)")
                    break
                if transient(rec):
                    continue
                ledger.add(h)
                rows.append(to_row(rec, post=src["name"], country="", source="UN",
                                   link=url, platform="UN", agency=agency,
                                   files=atts, read_ok=1 + len(atts), read_fail=0,
                                   sol_hint=title[:60]))
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
                    if pre is None:
                        text, attach, ok, fail = build_unit_from_page(key)
                        link, files = key, attach
                    else:
                        text, files, ok, fail = pre["text"], pre["files"], pre["ok"], pre["fail"]
                        link = files[0] if files else root["base"]
                    if len(text) < 180 or not looks_like_solicitation(text):
                        continue
                    h = unit_hash(text)
                    if h in ledger:
                        continue
                    found += 1
                    try:
                        rec = adjudicate_unit(text, root["post"])
                    except ai.AllExhausted:
                        st.beat(currentJob="AI quota exhausted — pausing (resumes next run)")
                        raise StopIteration
                    if transient(rec):
                        continue
                    ledger.add(h)
                    rows.append(to_row(rec, post=root["post"], country=root["country"],
                                       source="Site", link=link, platform="USGOV",
                                       files=files, read_ok=ok, read_fail=fail))
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
    print(f"[{mode}] found={found} new={new_c} changed={chg_c} total={len(merged)} "
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
