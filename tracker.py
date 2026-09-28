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
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate
from urllib.parse import urljoin

import requests, yaml
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
SITES_FILE = os.path.join(HERE, "sites.yaml")
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
    for a in scope.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"])
        if not text: continue
        for m in EMAIL_RE.findall(text + " " + href):
            if "state.gov" in m or "usembassy" in m: emails.add(m)
        if EMAIL_RE.fullmatch(text) or JUNK.search(text): continue
        if not (STRONG.search(text) or SOLNUM.search(text) or SOLNUM.search(href)): continue
        key = (text.lower(), href)
        if key in seen: continue
        seen.add(key)
        d = ""
        par = a.find_parent(["li", "tr", "p"])
        if par:
            md = DATE_RE.search(par.get_text(" ", strip=True))
            if md: d = md.group(0)
        items.append({"text": text[:180], "href": href, "date": d})
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


def eff_status(it, max_age):
    """active / closed (deadline passed) / stale (no date, too old on page)."""
    dl = (it.get("deadline") or "").strip()
    today = datetime.now(timezone.utc).date().isoformat()
    if dl:
        return "closed" if dl < today else "active"
    fs = it.get("first_seen") or it.get("posted") or ""
    age = days_since(fs)
    if age is not None and age > max_age:
        return "stale"
    return "active"


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

    # ---------- ACTIVE INVENTORY (everything currently open) ----------
    max_age = int(os.getenv("MAX_AGE_DAYS", "90"))
    active = {}
    if not is_baseline:
        for it in current_site:  # embassy items on pages now, enriched from SAM
            sol = (it.get("sol") or "").upper()
            if sol and sol in sam_index:
                rec = sam_index[sol]
                if not it.get("posted"): it["posted"] = rec.get("posted", "")
                it["deadline"] = it.get("deadline") or rec.get("deadline", "")
                it["setaside"] = it.get("setaside") or rec.get("setaside", "")
                it["psc"] = it.get("psc") or rec.get("psc", "")
                it["naics"] = it.get("naics") or rec.get("naics", "")
                it["source"] = "Site+SAM"
            if eff_status(it, max_age) == "active":
                active[sol or ("_" + it["href"])] = it
        for sol, d in sam_index.items():  # SAM-only open items (from the wide index)
            if sol in active:
                active[sol]["source"] = "Site+SAM"; continue
            it = {"name": f"SAM · {d.get('country', '—')}", "text": (d.get("title", "") or "")[:180],
                  "sol": sol, "href": d.get("href", "https://sam.gov"), "source": "SAM",
                  "posted": d.get("posted", ""), "first_seen": d.get("posted", ""),
                  "deadline": d.get("deadline", ""), "setaside": d.get("setaside", ""),
                  "psc": d.get("psc", ""), "naics": d.get("naics", "")}
            if eff_status(it, max_age) == "active":
                active[sol] = it
    active_list = sorted(active.values(), key=bidfit_key)
    winnable = [it for it in active_list if is_fit(it)]
    closing_soon = sorted([it for it in active_list if it.get("deadline")
                           and days_until(it["deadline"]) is not None and 0 <= days_until(it["deadline"]) <= 7],
                          key=lambda it: it.get("deadline") or "9999")

    html = build_html(buckets, counts, errors, checked, baseline_rows, gso_emails,
                      is_baseline, sam_note, len(sam_items),
                      len(active_list), len(winnable), closing_soon)

    attachments = []
    if not is_baseline and active_list:  # full active list attached to EVERY digest
        attachments.append((f"active_solicitations_{today}.csv", build_active_csv(active_list), "text/csv"))
        sam_note += f" · {len(active_list)} active ({len(winnable)} winnable) CSV attached"

    send = bool(total or errors or is_baseline or attachments) or os.getenv("SEND_DAILY_DIGEST") == "1"
    subject = build_subject(counts, errors, is_baseline)
    if not is_baseline and closing_soon:
        subject = f"[{len(closing_soon)} closing ≤7d] " + subject
    if send: send_email(subject, html, attachments); print("EMAIL SENT:", subject)
    else: print("No changes — no email.")
    print(f"site_changes={len(site_items)} sam_new={len(sam_items)} active={len(active_list)} errors={len(errors)} | {sam_note}")


def build_subject(counts, errors, baseline):
    d = datetime.now(timezone.utc).strftime("%d %b")
    if baseline: return f"Embassy Digest — baseline captured ({d})"
    bits = []
    if counts["new"]: bits.append(f"{counts['new']} new")
    if counts["amendment"]: bits.append(f"{counts['amendment']} amend")
    if counts["cancelled"]: bits.append(f"{counts['cancelled']} cancel")
    if counts["updated"]: bits.append(f"{counts['updated']} upd")
    if errors: bits.append(f"{len(errors)} down")
    return "Embassy Digest — " + (", ".join(bits) if bits else "all clear") + f" ({d})"


CAT_STYLE = {"new": ("NEW SOLICITATIONS", "#16a34a"), "amendment": ("AMENDMENTS", "#d97706"),
             "cancelled": ("CANCELLED / CLOSED", "#dc2626"), "updated": ("UPDATED", "#2563eb")}
SRC_COLOR = {"Site": "#475569", "SAM": "#7c3aed", "Site+SAM": "#0f766e"}


def _tile(label, value, color):
    return (f'<td align="center" style="padding:12px 6px;background:{color};border-radius:8px;color:#fff;'
            f'font-family:Arial">' f'<div style="font-size:24px;font-weight:700">{value}</div>'
            f'<div style="font-size:10px;letter-spacing:.5px;margin-top:3px">{label}</div></td>')


def _pill(text, bg, fg):
    return (f'<span style="background:{bg};color:{fg};font-size:9px;padding:1px 5px;'
            f'border-radius:4px;margin-right:3px;white-space:nowrap">{text}</span>')


def _badges(it):
    b = []
    if is_fit(it):
        b.append(_pill("⭐ FIT", "#fef08a", "#854d0e"))
    if is_open(it):
        b.append(_pill("OPEN", "#dcfce7", "#166534"))
    else:
        b.append(_pill("SET-ASIDE: " + (it.get("setaside", "")[:22] or "yes"), "#ffedd5", "#9a3412"))
    if is_goods(it):
        b.append(_pill("GOODS/COTS", "#dbeafe", "#1e40af"))
    elif it.get("psc"):
        b.append(_pill("SERVICE", "#f1f5f9", "#475569"))
    if is_trap(it):
        b.append(_pill("⚠ TRAP", "#fee2e2", "#991b1b"))
    meta = []
    if it.get("psc"): meta.append("PSC " + it["psc"])
    if it.get("naics"): meta.append("NAICS " + it["naics"])
    m = (' <span style="color:#94a3b8;font-size:10px">' + " · ".join(meta) + '</span>') if meta else ''
    return '<div style="margin-top:4px">' + "".join(b) + m + '</div>'


def _rows(items):
    out = []
    for it in items:
        src = it.get("source", "Site"); c = SRC_COLOR.get(src, "#475569")
        posted = it.get("posted", ""); deadline = it.get("deadline", "")
        dc = []
        if deadline:
            n = days_until(deadline)
            if n is not None and n < 0:
                dc.append(f'<span style="color:#94a3b8">✖ closed {deadline}</span>')
            elif n is not None and n <= 5:
                dc.append(f'<b style="background:#dc2626;color:#fff;padding:1px 5px;border-radius:4px">🔴 CLOSES IN {n}d</b><br>'
                          f'<span style="color:#dc2626">⏰ {deadline}</span>')
            else:
                dc.append(f'<b style="color:#dc2626">⏰ Due {deadline}</b>')
        if posted: dc.append(f'<span style="color:#475569">🗓 {posted}</span>')
        datecell = "<br>".join(dc) if dc else "—"
        out.append(
            f'<tr>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;font-family:Arial;font-size:13px;white-space:nowrap;vertical-align:top">'
            f'<div style="font-weight:600">{it["name"]}</div>'
            f'<span style="background:{c};color:#fff;font-size:9px;padding:1px 5px;border-radius:4px">{src}</span></td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;font-family:Arial;font-size:13px;vertical-align:top">'
            f'<a href="{it["href"]}" style="color:#1d4ed8;text-decoration:none">{it["text"]}</a>'
            f'{_badges(it)}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;font-family:monospace;font-size:12px;'
            f'color:#555;white-space:nowrap;vertical-align:top">{it.get("sol") or "—"}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;font-family:Arial;font-size:12px;'
            f'white-space:nowrap;vertical-align:top">{datecell}</td></tr>')
    return "".join(out)


def build_html(buckets, counts, errors, checked, baseline_rows, gso_emails, is_baseline, sam_note, sam_count,
               active_count=0, winnable_count=0, closing_soon=None):
    closing_soon = closing_soon or []
    now = datetime.now(timezone.utc)
    label = "Morning" if now.hour < 12 else "Evening"
    stamp = now.strftime("%A, %d %b %Y · %H:%M UTC")
    S = ['<div style="max-width:760px;margin:0 auto;background:#f6f7f9;padding:18px;font-family:Arial,sans-serif">']
    S.append('<div style="background:#0f172a;border-radius:10px;padding:18px 20px;color:#fff">'
             '<div style="font-size:18px;font-weight:700">🏛️ Madison &amp; Main — Embassy Procurement Digest</div>'
             f'<div style="font-size:12px;color:#94a3b8;margin-top:4px">{label} run · {stamp}</div>'
             f'<div style="font-size:11px;color:#64748b;margin-top:6px">Sources: {checked} embassy pages + SAM.gov API</div></div>')

    if is_baseline:
        S.append('<p style="font-size:13px;color:#334155;margin:14px 4px">First run — <b>baseline</b> of what is '
                 'currently open on embassy pages. From next run you get only changes, plus SAM.gov feed.</p>')
        S.append('<table width="100%" cellspacing="0" style="background:#fff;border-radius:10px;overflow:hidden">'
                 '<tr style="background:#0f172a;color:#fff"><td style="padding:8px 10px;font-size:12px">EMBASSY</td>'
                 '<td style="padding:8px 10px;font-size:12px">OPEN ITEMS</td></tr>')
        for r in sorted(baseline_rows, key=lambda x: -x["count"]):
            S.append(f'<tr><td style="padding:7px 10px;border-bottom:1px solid #eee;font-size:13px">'
                     f'<a href="{r["url"]}" style="color:#1d4ed8;text-decoration:none">{r["name"]}</a></td>'
                     f'<td style="padding:7px 10px;border-bottom:1px solid #eee;font-size:13px">{r["count"]}</td></tr>')
        S.append('</table>')
    else:
        S.append('<table width="100%" cellspacing="6" style="margin:14px 0"><tr>')
        S.append(_tile("NEW", counts["new"], "#16a34a"))
        S.append(_tile("AMEND", counts["amendment"], "#d97706"))
        S.append(_tile("CANCELLED", counts["cancelled"], "#dc2626"))
        S.append(_tile("UPDATED", counts["updated"], "#2563eb"))
        S.append(_tile("SITE DOWN", len(errors), "#64748b"))
        S.append('</tr></table>')
        S.append(f'<p style="font-size:12px;color:#64748b;margin:0 4px 10px">{checked} embassy pages checked · '
                 f'{sam_count} items from SAM.gov · Source tag shows Site / SAM / both.</p>')

        # active inventory banner
        S.append(f'<div style="background:#0f766e;color:#fff;border-radius:8px;padding:10px 14px;margin:0 0 6px">'
                 f'<b style="font-size:15px">📋 {active_count} solicitations currently ACTIVE</b> '
                 f'<span style="color:#99f6e4">· {winnable_count} winnable (Open + Goods/COTS)</span>'
                 f'<div style="font-size:11px;color:#99f6e4;margin-top:3px">Full list attached as CSV — with the date each appeared + its deadline.</div></div>')

        # CLOSING THIS WEEK — pinned at top
        if closing_soon:
            S.append('<div style="margin-top:10px;background:#b91c1c;color:#fff;padding:8px 12px;'
                     'border-radius:8px 8px 0 0;font-size:13px;font-weight:700">⏰ CLOSING THIS WEEK (≤7 days) — '
                     f'{len(closing_soon)}</div>')
            S.append('<table width="100%" cellspacing="0" style="background:#fff;border-radius:0 0 8px 8px">'
                     '<tr style="background:#fee2e2"><td style="padding:6px 10px;font-size:11px;color:#991b1b">EMBASSY / SRC</td>'
                     '<td style="padding:6px 10px;font-size:11px;color:#991b1b">ITEM</td>'
                     '<td style="padding:6px 10px;font-size:11px;color:#991b1b">SOL #</td>'
                     '<td style="padding:6px 10px;font-size:11px;color:#991b1b">DATES</td></tr>')
            S.append(_rows(closing_soon[:25])); S.append('</table>')

        for cat in ["new", "amendment", "cancelled", "updated"]:
            if not buckets[cat]: continue
            title, color = CAT_STYLE[cat]
            S.append(f'<div style="margin-top:14px;background:{color};color:#fff;padding:8px 12px;'
                     f'border-radius:8px 8px 0 0;font-size:13px;font-weight:700">{title} ({counts[cat]})</div>')
            S.append('<table width="100%" cellspacing="0" style="background:#fff;border-radius:0 0 8px 8px">'
                     '<tr style="background:#f1f5f9"><td style="padding:6px 10px;font-size:11px;color:#475569">EMBASSY / SRC</td>'
                     '<td style="padding:6px 10px;font-size:11px;color:#475569">ITEM</td>'
                     '<td style="padding:6px 10px;font-size:11px;color:#475569">SOL #</td>'
                     '<td style="padding:6px 10px;font-size:11px;color:#475569">DATES</td></tr>')
            S.append(_rows(buckets[cat])); S.append('</table>')
        if not any(buckets.values()) and not errors:
            S.append('<p style="font-size:14px;color:#16a34a;padding:10px 4px">✓ All quiet — no changes.</p>')

    if errors:
        S.append('<div style="margin-top:16px;background:#64748b;color:#fff;padding:8px 12px;border-radius:8px 8px 0 0;'
                 'font-size:13px;font-weight:700">⚠️ NOT RESPONDING — check by hand</div>'
                 '<table width="100%" cellspacing="0" style="background:#fff;border-radius:0 0 8px 8px">')
        for e in errors:
            S.append(f'<tr><td style="padding:7px 10px;border-bottom:1px solid #eee;font-size:13px;font-weight:600">{e["name"]}</td>'
                     f'<td style="padding:7px 10px;border-bottom:1px solid #eee;font-size:12px;color:#64748b">{e["err"]}</td></tr>')
        S.append('</table>')

    if is_baseline and gso_emails:
        S.append('<div style="margin-top:16px;background:#0f766e;color:#fff;padding:8px 12px;border-radius:8px 8px 0 0;'
                 'font-size:13px;font-weight:700">📇 GSO VENDOR EMAILS FOUND</div>'
                 '<table width="100%" cellspacing="0" style="background:#fff;border-radius:0 0 8px 8px">')
        for name, addrs in gso_emails.items():
            S.append(f'<tr><td style="padding:7px 10px;border-bottom:1px solid #eee;font-size:13px;font-weight:600">{name}</td>'
                     f'<td style="padding:7px 10px;border-bottom:1px solid #eee;font-size:12px;color:#0f766e">{", ".join(addrs)}</td></tr>')
        S.append('</table>')

    S.append(f'<p style="font-size:11px;color:#94a3b8;margin:16px 4px 0">{sam_note} · '
             'public embassy pages + SAM.gov · email-only tenders still need GSO vendor-list signup.</p></div>')
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
    w.writerow(["Post/Country", "Title", "Solicitation #", "Source", "Set-Aside (blank=Open)",
                "Type", "Trap?", "PSC", "NAICS", "First Seen / Posted", "Deadline", "Winnable"])
    for it in items:
        typ = "Goods/COTS" if is_goods(it) else ("Service" if it.get("psc") else "")
        w.writerow([it.get("name", ""), it.get("text", ""), it.get("sol", ""), it.get("source", ""),
                    it.get("setaside", ""), typ, "TRAP" if is_trap(it) else "",
                    it.get("psc", ""), it.get("naics", ""),
                    it.get("posted", "") or it.get("first_seen", ""), it.get("deadline", ""),
                    "YES" if is_fit(it) else ""])
    return buf.getvalue().encode("utf-8")


def send_email(subject, html, attachments=None):
    user, pw = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    to = os.getenv("ALERT_TO") or user
    if not (user and pw and to):
        print("!! Email not sent: missing secrets", file=sys.stderr); return
    outer = MIMEMultipart("mixed")
    outer["Subject"], outer["From"], outer["To"], outer["Date"] = subject, user, to, formatdate(localtime=True)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Open in an HTML-capable mail client to view the digest.", "plain"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    outer.attach(alt)
    for fname, data, mime in (attachments or []):
        part = MIMEApplication(data, _subtype=mime.split("/")[-1])
        part.add_header("Content-Disposition", "attachment", filename=fname)
        outer.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, pw)
        server.sendmail(user, [x.strip() for x in to.split(",")], outer.as_string())


if __name__ == "__main__":
    main()
