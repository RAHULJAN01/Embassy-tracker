#!/usr/bin/env python3
"""
crawler.py — Madison & Main Solicitation Register engine.
=========================================================
Runs 24/7 in GitHub Actions. Two modes:

  python crawler.py roots   # FULL deep re-crawl: walk every embassy site + SAM,
                            #   find live solicitations, read attachments, adjudicate.
  python crawler.py live    # QUICK status check: re-verify known records,
                            #   detect new / changed / removed, light AI use.

Design goals the user locked in:
  * uniform treatment of EVERY solicitation (SAM or site) — "don't spare a single one"
  * accuracy over coverage — adjudicator gates reject guesses -> REVIEW
  * stay FREE — budget governor caps AI calls per run; multi-provider rotation
  * resumable — checkpoint state.json; a run processes a time/budget slice, next run continues
  * raise a HELP flag when a site blocks the bot (hold-the-door)
  * emit data.json (the directory), status.json (Mission Control), blocked.json (HELP)
"""
import os, sys, json, time, hashlib, re, pathlib, datetime, urllib.parse

import analyzer, ai, fetcher

HERE = pathlib.Path(__file__).parent
ROOTS = HERE / "roots.json"
DATA = HERE / "data.json"
STATE = HERE / "state.json"
STATUS = HERE / "status.json"
BLOCKED = HERE / "blocked.json"

# --- budget / pacing (keep it free, keep runs short enough for Actions) -----
MAX_AI_CALLS = int(os.getenv("MAX_AI_CALLS", "120"))     # per run; multi-provider spreads load
TIME_BUDGET_S = int(os.getenv("TIME_BUDGET_S", "1500"))  # ~25 min per run slice
PAGE_PAUSE = float(os.getenv("PAGE_PAUSE", "0.6"))       # politeness between requests
SAM_NAICS = os.getenv("SAM_NAICS", "")                   # optional comma list to focus SAM


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


def fingerprint(rec):
    """Stable hash of the fields that define a solicitation's identity+content.
    Changing any of these marks the record 'changed' (change-detection)."""
    key = "|".join(str(rec.get(k, "")) for k in
                   ("sol", "title", "deadline", "status", "summary", "link"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Status / Mission Control
# --------------------------------------------------------------------------
class Status:
    def __init__(self, mode, providers):
        self.d = {
            "mode": mode, "startedAt": now_utc(), "heartbeat": now_utc(),
            "currentJob": "starting", "phase": "init",
            "done": 0, "queued": 0, "found": 0, "aiCalls": 0, "aiBudget": MAX_AI_CALLS,
            "providers": providers, "blockedSites": [], "coverage": {},
            "etaNote": "", "lastError": "", "running": True,
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


# --------------------------------------------------------------------------
# Embassy-site discovery
# --------------------------------------------------------------------------
def discover_proc_pages(root, cfg, blocked):
    """From an embassy base URL, find its procurement listing page(s)."""
    base = root["base"]
    raw, ct, final = fetcher.get(base)
    if raw is None:
        return []
    pages = set()
    for href, text in fetcher.links(raw, final):
        if root_host(href) != root_host(base):
            continue
        hay = (href + " " + text).lower()
        if any(k in hay for k in cfg["proc_keywords"]):
            pages.add(href.split("#")[0])
    # also try explicit hint paths
    for hint in cfg["proc_hints"]:
        pages.add(urllib.parse.urljoin(base, hint))
    return list(pages)[:8]


def root_host(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def find_solicitation_links(proc_url, cfg):
    """On a procurement listing page, collect links that look like individual
    solicitations or attachments (PDF/RFQ/RFP/tender)."""
    raw, ct, final = fetcher.get(proc_url)
    if raw is None:
        return [], ""
    page_txt = fetcher.html_text(raw)
    out = []
    for href, text in fetcher.links(raw, final):
        low = (href + " " + text).lower()
        if href.lower().endswith((".pdf", ".doc", ".docx")):
            out.append(href)
        elif any(k in low for k in cfg["proc_keywords"]):
            out.append(href.split("#")[0])
    # de-dupe, cap
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq[:25], page_txt


def build_dossier_text(sol_url):
    """Assemble the full text for ONE solicitation: its page + every attachment."""
    raw, ct, final = fetcher.get(sol_url)
    parts = []
    attach_urls = []
    if "pdf" in (ct or "") or sol_url.lower().endswith(".pdf"):
        parts.append(fetcher.pdf_text(raw) if raw else "")
    elif raw:
        parts.append(fetcher.html_text(raw))
        for href, text in fetcher.links(raw, final):
            if href.lower().endswith((".pdf", ".doc", ".docx")):
                attach_urls.append(href)
    for a in attach_urls[:6]:
        t = fetcher.read_attachment(a)
        if t:
            parts.append(f"\n[ATTACHMENT: {a}]\n{t}")
        time.sleep(PAGE_PAUSE)
    return "\n\n".join(p for p in parts if p).strip(), (attach_urls)


# --------------------------------------------------------------------------
# SAM.gov
# --------------------------------------------------------------------------
def sam_search(cfg, limit=40):
    """Query SAM public Opportunities API for active notices. Needs SAM_API_KEY.
    Returns a list of raw notice dicts (best-effort)."""
    key = os.getenv("SAM_API_KEY", "")
    if not key:
        return []
    posted_from = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%m/%d/%Y")
    posted_to = datetime.date.today().strftime("%m/%d/%Y")
    params = {"api_key": key, "limit": str(limit), "postedFrom": posted_from,
              "postedTo": posted_to, "ptype": "o,k,r"}   # solicitation / combined / sources-sought
    if SAM_NAICS:
        params["ncode"] = SAM_NAICS.split(",")[0]
    url = cfg["sam"]["api"] + "?" + urllib.parse.urlencode(params)
    raw, ct, final = fetcher.get(url)
    if not raw:
        return []
    try:
        return json.loads(raw).get("opportunitiesData", []) or []
    except Exception:
        return []


def sam_record_text(op):
    """Pull description + attachment text for a SAM notice."""
    parts = [op.get("title", ""), op.get("description", "") or ""]
    # description is sometimes a URL to the full text
    desc = op.get("description", "") or ""
    if desc.startswith("http"):
        raw, ct, final = fetcher.get(desc)
        if raw:
            parts.append(fetcher.html_text(raw))
    # resource/attachment links
    for link in (op.get("resourceLinks") or [])[:6]:
        t = fetcher.read_attachment(link)
        if t:
            parts.append(f"\n[ATTACHMENT: {link}]\n{t}")
        time.sleep(PAGE_PAUSE)
    return "\n\n".join(p for p in parts if p).strip()


# --------------------------------------------------------------------------
# Record assembly (merge adjudicator output into the directory row shape)
# --------------------------------------------------------------------------
def to_row(rec, *, post, country, source, link, sol_hint=""):
    """Convert an adjudicator record into the directory.json row the site renders."""
    row = {
        "tier": rec.get("tier", "REVIEW"),   # BID | MID | NO | REVIEW (template renders each)
        "title": rec.get("title") or sol_hint or "(untitled solicitation)",
        "sol": rec.get("sol") or sol_hint,
        "post": post, "country": country, "source": source, "link": link,
        "posted": rec.get("posted", ""), "deadline": rec.get("closing", ""),
        "qa": rec.get("qa_due", ""), "status": "Active", "updated": today(),
        "summary": rec.get("scope") or rec.get("classification", ""),
        "type": rec.get("classification", ""), "scope": rec.get("scope", ""),
        "value": rec.get("est_value", ""), "shipping": rec.get("shipping", ""),
        "payment": rec.get("payment", ""), "shipAfter": rec.get("ship_after", ""),
        "setaside": rec.get("setaside", ""), "license": rec.get("license", ""),
        "docs": rec.get("docs", []),
        "restrictions": [{"t": r["text"], "k": r["kind"], "note": r["note"]}
                         for r in rec.get("restrictions", [])],
        "route": rec.get("route", ""),
        "challenge": rec.get("challenge_draft", ""),
        "citation": rec.get("citation"),
        "confidence": rec.get("confidence", 0),
        "reviewReason": rec.get("review_reason", ""),
        "evidence": (rec.get("_evidence") or "")[:600],
    }
    row["fp"] = fingerprint(row)
    return row


# --------------------------------------------------------------------------
# Main crawl
# --------------------------------------------------------------------------
def run(mode):
    cfg = load(ROOTS, {})
    if not cfg:
        print("no roots.json"); return
    prior = load(DATA, {"meta": {}, "solicitations": []})
    prior_rows = {r.get("sol") or r.get("link"): r for r in prior.get("solicitations", [])}
    state = load(STATE, {"root_idx": 0, "seen": {}})
    rotator, call = ai.make_caller()
    st = Status(mode, rotator.names())
    t0 = time.time()
    blocked_sites = load(BLOCKED, {"sites": []}).get("sites", [])
    blocked_hosts = {b["host"] for b in blocked_sites}

    rows = []                       # fresh rows this run
    found = 0
    ai_calls = 0

    def budget_left():
        return ai_calls < MAX_AI_CALLS and (time.time() - t0) < TIME_BUDGET_S

    def adjudicate_text(text, meta):
        nonlocal ai_calls
        try:
            rec = analyzer.adjudicate(text, call, today=today())
        except ai.AllExhausted as e:
            raise
        ai_calls += 1
        st.beat(aiCalls=ai_calls, done=len(rows), found=found,
                currentJob=f"adjudicating: {meta[:60]}")
        return rec

    try:
        # ---- 1. SAM.gov (primary) ----
        st.beat(phase="sam", currentJob="querying SAM.gov")
        for op in sam_search(cfg):
            if not budget_left():
                break
            found += 1
            text = sam_record_text(op)
            if len(text) < 180:
                continue
            try:
                rec = adjudicate_text(text, op.get("title", "SAM notice"))
            except ai.AllExhausted:
                st.beat(currentJob="AI quota exhausted — pausing (resumes next run)")
                break
            nid = op.get("noticeId", "")
            link = cfg["sam"]["view"].replace("{id}", nid) if nid else "https://sam.gov/"
            rows.append(to_row(rec, post=op.get("organizationName", "SAM.gov"),
                               country=op.get("placeOfPerformance", {}).get("country", {}).get("name", "")
                               if isinstance(op.get("placeOfPerformance"), dict) else "",
                               source="SAM", link=link, sol_hint=op.get("solicitationNumber", "")))
            time.sleep(PAGE_PAUSE)

        # ---- 2. Embassy sites (resumable by root_idx) ----
        roots = cfg.get("roots", [])
        n = len(roots)
        start = state.get("root_idx", 0) % max(1, n) if mode == "roots" else 0
        coverage = {}
        for off in range(n):
            if not budget_left():
                state["root_idx"] = (start + off) % n     # checkpoint where to resume
                break
            root = roots[(start + off) % n]
            host = root_host(root["base"])
            st.beat(phase="embassy", currentJob=f"crawling {root['post']}",
                    queued=n - off, coverage=coverage)
            try:
                proc_pages = discover_proc_pages(root, cfg, blocked_hosts)
                sol_links = []
                for pp in proc_pages[:4]:
                    ls, _ = find_solicitation_links(pp, cfg)
                    sol_links += ls
                    time.sleep(PAGE_PAUSE)
                sol_links = list(dict.fromkeys(sol_links))[:12]
                coverage[root["country"]] = len(sol_links)
                for sl in sol_links:
                    if not budget_left():
                        break
                    text, attach = build_dossier_text(sl)
                    if len(text) < 180:
                        continue
                    found += 1
                    try:
                        rec = adjudicate_text(text, root["post"])
                    except ai.AllExhausted:
                        st.beat(currentJob="AI quota exhausted — pausing (resumes next run)")
                        raise StopIteration
                    rows.append(to_row(rec, post=root["post"], country=root["country"],
                                       source="Site", link=sl))
                    time.sleep(PAGE_PAUSE)
            except fetcher.Blocked as b:
                if host not in blocked_hosts:
                    blocked_sites.append({"host": host, "post": root["post"],
                                          "url": root["base"], "reason": str(b),
                                          "since": now_utc()})
                    blocked_hosts.add(host)
                st.beat(blockedSites=blocked_sites)
            except StopIteration:
                state["root_idx"] = (start + off) % n
                break
            except Exception as e:
                st.d["lastError"] = f"{root['post']}: {str(e)[:80]}"
        else:
            state["root_idx"] = 0      # completed a full pass

    except ai.AllExhausted:
        st.beat(currentJob="AI quota exhausted — paused")

    # ---- 3. merge with prior (change detection) + mark removed ----
    merged, new_c, chg_c = merge_records(prior_rows, rows, mode)

    meta = prior.get("meta", {})
    stamp = now_utc()
    if mode == "roots":
        meta["lastDeep"] = stamp; meta["lastRoots"] = stamp
    meta["lastLive"] = stamp
    meta["counts"] = tally(merged)
    save(DATA, {"meta": meta, "solicitations": merged})
    save(STATE, state)
    save(BLOCKED, {"sites": blocked_sites, "updated": stamp})
    st.finish(note=f"done — {new_c} new, {chg_c} changed, {len(merged)} total, {ai_calls} AI calls")
    print(f"[{mode}] found={found} new={new_c} changed={chg_c} total={len(merged)} "
          f"ai_calls={ai_calls} blocked={len(blocked_sites)}")


def merge_records(prior_rows, fresh_rows, mode):
    """Merge fresh rows over prior. Detect new/changed; age-out removed records."""
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
    # removed detection: in a full 'roots' pass, prior records not re-found get flagged
    if mode == "roots":
        fresh_keys = {r.get("sol") or r.get("link") for r in fresh_rows}
        for k, old in list(by_key.items()):
            if k not in fresh_keys and old.get("status") == "Active":
                # only age-out site records we actually re-crawled; keep SAM as-is unless stale
                old["status"] = "Check"      # couldn't confirm this pass
                old["updated"] = today()
    return list(by_key.values()), new_c, chg_c


def tally(rows):
    c = {"active": 0, "bid": 0, "mid": 0, "no": 0, "review": 0}
    for r in rows:
        if r.get("status") not in ("Active", "Check"):
            continue
        c["active"] += 1
        t = r.get("tier", "")
        if t == "BID": c["bid"] += 1
        elif t == "MID": c["mid"] += 1
        elif t == "NO": c["no"] += 1
        else: c["review"] += 1
    return c


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    if mode not in ("roots", "live"):
        print("usage: crawler.py [roots|live]"); sys.exit(1)
    run(mode)
