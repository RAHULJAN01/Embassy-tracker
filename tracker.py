#!/usr/bin/env python3
"""
Embassy Procurement Tracker
---------------------------
Checks a list of US-embassy procurement pages once per run, detects NEW
solicitations and CHANGES/AMENDMENTS to existing ones, and emails a single
summary only when something changed (or a site failed to load).

State (what each page looked like last time) is stored as JSON in state/ and
committed back to the repo by the GitHub Action, so nothing is lost between runs.

Reads credentials from environment variables (set as GitHub Secrets):
    GMAIL_USER          the Gmail address that sends the alert
    GMAIL_APP_PASSWORD  a Gmail App Password (NOT your normal password)
    ALERT_TO            where to send alerts (can be the same address)
Optional:
    SEND_DAILY_DIGEST=1 also email on quiet days ("all clear") so you know it ran
"""

import os
import re
import sys
import json
import time
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
SITES_FILE = os.path.join(HERE, "sites.yaml")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Words that mark a link as procurement-relevant (used to focus the diff)
KEYWORDS = re.compile(
    r"(solicitation|procurement|rfq|rfp|rfi|request for (quotation|proposal|information)"
    r"|tender|bid|amendment|pre-?solicitation|combined synopsis|award|invitation to bid"
    r"|sources sought|contract|quotation|19\w{6,}|pr\d{6,})",
    re.I,
)


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fetch(url, tries=3):
    """Fetch a page with retries. Returns HTML text or raises."""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (i + 1))
    raise last


def extract(html, base_url, selector=None):
    """
    Turn a page into a stable fingerprint:
      - items: list of {text, href} for procurement-relevant links
      - text_hash: hash of the cleaned visible text of the content area
    Both together catch NEW posts, REMOVED posts, and edits/AMENDMENTS.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Kill noise that changes on every load
    for tag in soup(["script", "style", "noscript", "svg", "form", "iframe"]):
        tag.decompose()
    for sel in ["header", "footer", "nav", ".menu", "#menu", ".site-header",
                ".site-footer", ".cookie", ".social", ".breadcrumb"]:
        for t in soup.select(sel):
            t.decompose()

    scope = soup
    if selector:
        picked = soup.select_one(selector)
        if picked:
            scope = picked
    else:
        # Prefer an obvious main content region if present
        for sel in ["main", "article", "#content", ".entry-content",
                    ".page-content", ".content"]:
            picked = soup.select_one(sel)
            if picked:
                scope = picked
                break

    items = []
    seen = set()
    for a in scope.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"])
        if not text:
            continue
        if KEYWORDS.search(text) or KEYWORDS.search(href):
            key = (text.lower(), href)
            if key not in seen:
                seen.add(key)
                items.append({"text": text, "href": href})

    visible = " ".join(scope.get_text(" ", strip=True).split())
    text_hash = hashlib.sha256(visible.encode("utf-8", "ignore")).hexdigest()

    return {"items": items, "text_hash": text_hash}


def load_state(s):
    p = os.path.join(STATE_DIR, f"{s}.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_state(s, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"{s}.json"), "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def diff_items(old, new):
    """Return (added, removed) item lists based on (text, href) identity."""
    ok = {(i["text"].lower(), i["href"]): i for i in old}
    nk = {(i["text"].lower(), i["href"]): i for i in new}
    added = [nk[k] for k in nk if k not in ok]
    removed = [ok[k] for k in ok if k not in nk]
    return added, removed


def main():
    with open(SITES_FILE) as f:
        cfg = yaml.safe_load(f)
    sites = cfg.get("sites", [])

    new_hits = []       # (site_name, [items])   -> genuinely new solicitations
    changed = []        # (site_name)            -> content changed, no clear new link
    baselines = []      # (site_name, [items])   -> first-ever check
    errors = []         # (site_name, url, err)

    for site in sites:
        name = site["name"]
        url = site["url"]
        selector = site.get("selector")
        s = slug(name)

        try:
            html = fetch(url)
            fp = extract(html, url, selector)
        except Exception as e:
            errors.append((name, url, str(e)[:200]))
            continue  # IMPORTANT: don't overwrite good state on a failed fetch

        prev = load_state(s)
        if prev is None:
            baselines.append((name, fp["items"]))
            save_state(s, {"url": url, **fp})
            continue

        added, removed = diff_items(prev.get("items", []), fp["items"])
        hash_changed = prev.get("text_hash") != fp["text_hash"]

        if added:
            new_hits.append((name, url, added))
        elif hash_changed:
            # content moved but no new procurement link we could isolate
            # (could be an amendment edited into an existing post, a date change)
            changed.append((name, url))

        save_state(s, {"url": url, **fp})

    body = build_email(new_hits, changed, baselines, errors)
    has_news = bool(new_hits or changed or errors)
    first_run = bool(baselines) and not (new_hits or changed)

    if has_news or first_run or os.getenv("SEND_DAILY_DIGEST") == "1":
        subject = build_subject(new_hits, changed, errors, first_run)
        send_email(subject, body)
        print(subject)
    else:
        print("No changes. No email sent.")
    print(body)


def build_subject(new_hits, changed, errors, first_run):
    n = sum(len(x[2]) for x in new_hits)
    from datetime import date
    d = date.today().isoformat()
    if first_run:
        return f"[Embassy Tracker] Baseline captured ({d})"
    parts = []
    if n:
        parts.append(f"{n} NEW")
    if changed:
        parts.append(f"{len(changed)} changed")
    if errors:
        parts.append(f"{len(errors)} unreachable")
    tag = ", ".join(parts) if parts else "all clear"
    return f"[Embassy Tracker] {tag} ({d})"


def build_email(new_hits, changed, baselines, errors):
    L = []
    if new_hits:
        L.append("=== NEW SOLICITATIONS / AMENDMENTS ===\n")
        for name, url, items in new_hits:
            L.append(f"[{name}]  {url}")
            for it in items:
                L.append(f"   • {it['text']}\n     {it['href']}")
            L.append("")
    if changed:
        L.append("=== PAGES THAT CHANGED (check manually — possible edit/amendment) ===\n")
        for name, url in changed:
            L.append(f"   • {name}\n     {url}")
        L.append("")
    if errors:
        L.append("=== COULD NOT CHECK (site down / blocked — verify by hand) ===\n")
        for name, url, err in errors:
            L.append(f"   • {name}: {err}\n     {url}")
        L.append("")
    if baselines:
        L.append("=== BASELINE CAPTURED (first check — currently listed) ===\n")
        for name, items in baselines:
            L.append(f"[{name}] — {len(items)} items now on file")
            for it in items[:15]:
                L.append(f"   • {it['text']}")
            L.append("")
    if not L:
        L.append("No changes across all monitored embassy pages.")
    L.append("\n— Madison & Main embassy tracker")
    return "\n".join(L)


def send_email(subject, body):
    user = os.getenv("GMAIL_USER")
    pw = os.getenv("GMAIL_APP_PASSWORD")
    to = os.getenv("ALERT_TO") or user
    if not (user and pw and to):
        print("!! Email not sent: missing GMAIL_USER / GMAIL_APP_PASSWORD / ALERT_TO", file=sys.stderr)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, pw)
        server.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())


if __name__ == "__main__":
    main()
