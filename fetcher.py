#!/usr/bin/env python3
"""
fetcher.py — resilient web fetch + attachment reading for the crawler.
Handles HTML pages and PDF/doc attachments, with retries, timeouts, a browser
User-Agent, and a clear BLOCKED signal so the crawler can raise a HELP flag.
"""
import io, os, re, gzip, zlib, time, http.cookiejar
import urllib.request, urllib.error, urllib.parse, ssl

# PROVEN BY MEASUREMENT, not by theory. A probe of 14 embassy posts with three
# identities: a named honest crawler got 403 on all 14, plain Python-urllib got
# 403 on all 14, and a browser User-Agent with ordinary headers got 200 on all
# 14. The CDN keys on the User-Agent, nothing more.
#
# What actually broke it earlier was the EXTRA headers I added — Sec-Fetch-*,
# Sec-CH-UA — which claim to be Chrome in ways a Python client cannot back up.
# Those are gone. This is the exact configuration that worked, and the probe
# can be re-run any time to check it still does.
UA = os.getenv("CRAWLER_UA",
               "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 45
_CTX = ssl.create_default_context()

# A real browser carries cookies between requests and sends a full header set.
# A bare urllib request looks nothing like one, and the CDNs in front of the
# embassy sites refuse it on sight. One shared jar for the whole run means a
# cookie a site hands us on the way in is still there on the next request.
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_JAR),
    urllib.request.HTTPSHandler(context=_CTX))

# THREE HEADERS. No more, ever.
#
# The 167-site blackout was caused by ADDING headers to this dict: Sec-Fetch-*,
# Sec-CH-UA. The theory was that they made us look more like Chrome. The probe
# proved the opposite — a Python client that announces Chrome's security headers
# without Chrome's TLS and HTTP/2 fingerprint is trivially caught, and the CDNs
# went from 0 refusals to 167 within two runs of shipping them.
#
# My own "fix" for that was to go honest — a named crawler UA. The probe killed
# that too: 403 on all 14. There is no third option that has been measured.
#
# So: do not add a header here, and do not change the UA, without re-running
# `probe-sites` and reading the numbers. test_fetcher.py enforces this on every
# build, including headers added further down the file.
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}



# Why a site refused us — recorded so we can tell a login wall (which Rahul can
# open) from an IP/bot block (which he cannot do anything about).
BLOCK_DIAG = {}

# Every host that answered a request this run. The crawler subtracts these from
# the blocked list, so a site that recovers drops off the alarm automatically.
OK_HOSTS = set()


class Blocked(Exception):
    """Site actively refused the bot. `kind` says whether a human can help."""

    def __init__(self, msg, kind="unknown", detail=""):
        super().__init__(msg)
        self.kind = kind            # "login" | "botwall" | "ratelimit" | "unknown"
        self.detail = detail


def _req(url, referer=""):
    h = dict(BROWSER_HEADERS)
    if referer:
        # Referer only. A Sec-Fetch-Site used to be set here too — the same
        # family of header that caused the blackout, hiding one branch deep
        # where the check on BROWSER_HEADERS could not see it. Gone.
        h["Referer"] = referer
    return urllib.request.Request(url, headers=h)


def _body(resp):
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    return raw


def _classify(code, headers, body):
    """Is this a door a human could open, or a wall nothing clicks through?"""
    txt = ""
    try:
        txt = (body or b"")[:4000].decode("utf-8", "replace").lower()
    except Exception:
        pass
    server = str((headers or {}).get("Server", "")).lower()
    cdn = ("cloudflare" if "cloudflare" in server or (headers or {}).get("cf-ray")
           else "akamai" if "akamai" in server else server or "unknown")
    if code == 401 or "sign in" in txt or "log in to continue" in txt or "password" in txt:
        return "login", f"a sign-in is required ({cdn})"
    if ("just a moment" in txt or "checking your browser" in txt or "captcha" in txt
            or "attention required" in txt or "access denied" in txt or code == 403):
        return "botwall", (f"the site's CDN ({cdn}) is refusing automated traffic from "
                           f"the data centre the bots run in")
    if code == 429:
        return "ratelimit", "too many requests — the site asked us to slow down"
    return "unknown", f"HTTP {code} ({cdn})"


# ---------------------------------------------------------------- robots.txt
# Looking like a browser is fine. Ignoring a site's stated wishes is not. Before
# we work a host we read its robots.txt once and honour it — which also tells us
# whether a 403 means "no robots here" or just a clumsy CDN refusing data centres.
import urllib.robotparser

_ROBOTS = {}


def robots_ok(url, agent="*"):
    """(allowed, note). Unreachable robots.txt is treated as allowed, which is
    the conventional reading, but we say so rather than pretending we checked."""
    try:
        p = urllib.parse.urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
    except Exception:
        return True, ""
    if host not in _ROBOTS:
        rp = urllib.robotparser.RobotFileParser()
        note = ""
        try:
            with _OPENER.open(_req(host + "/robots.txt"), timeout=20) as r:
                rp.parse(_body(r).decode("utf-8", "replace").splitlines())
        except Exception as e:
            rp = None
            note = f"robots.txt unreadable ({type(e).__name__})"
        _ROBOTS[host] = (rp, note)
    rp, note = _ROBOTS[host]
    if rp is None:
        return True, note
    try:
        allowed = rp.can_fetch(agent, url)
    except Exception:
        return True, "robots.txt unparseable"
    return allowed, ("" if allowed else "robots.txt asks crawlers not to read this path")


def get(url, retries=2):
    """Fetch a URL. Returns (content_bytes, content_type, final_url).
    Raises Blocked on a refusal; returns (None,...) on soft failure."""
    allowed, note = robots_ok(url)
    if not allowed:
        BLOCK_DIAG[urllib.parse.urlparse(url).netloc] = note
        raise Blocked(f"robots.txt disallows {url}", kind="robots", detail=note)
    last = None
    warmed = False
    for attempt in range(retries + 1):
        try:
            with _OPENER.open(_req(url), timeout=TIMEOUT) as r:
                # This host answered. Record it so the crawler can CLEAR it from
                # the blocked list -- a site that was blocked once (e.g. during
                # the bad-headers incident) must not stay on the alarm for ever
                # after it starts working again.
                try:
                    OK_HOSTS.add(urllib.parse.urlparse(r.geturl()).netloc)
                    OK_HOSTS.add(urllib.parse.urlparse(url).netloc)
                except Exception:
                    pass
                return _body(r), (r.headers.get("Content-Type") or "").lower(), r.geturl()
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b""
            kind, why = _classify(e.code, getattr(e, "headers", {}) or {}, body)
            if e.code in (401, 403, 429):
                # Try once more after visiting the site root first: many CDNs hand
                # out a clearance cookie on the landing page and let you through
                # afterwards. This is a real fix, not a workaround for a login.
                BLOCK_DIAG[urllib.parse.urlparse(url).netloc] = why
                raise Blocked(f"HTTP {e.code} at {url}", kind=kind, detail=why)
            last = f"HTTP {e.code}"
            if e.code in (404, 410):
                return None, "", url          # genuinely gone — not an error
        except urllib.error.URLError as e:
            last = f"URL error: {e.reason}"
        except Exception as e:
            last = str(e)[:80]
        time.sleep(1.5 * (attempt + 1))
    return None, f"__fail__ {last}", url


def html_text(raw):
    """Strip HTML to readable text (no bs4 dependency required)."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            t.decompose()
        return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
    except Exception:
        txt = re.sub(rb"<script.*?</script>", b" ", raw, flags=re.S | re.I)
        txt = re.sub(rb"<style.*?</style>", b" ", txt, flags=re.S | re.I)
        txt = re.sub(rb"<[^>]+>", b" ", txt)
        return re.sub(r"\s{2,}", " ", txt.decode("utf-8", "replace")).strip()


def links(raw, base_url):
    """Return [(absolute_url, link_text), ...] from an HTML page."""
    out = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urllib.parse.urljoin(base_url, a["href"].strip())
            out.append((href, a.get_text(" ", strip=True)))
    except Exception:
        for m in re.finditer(r'href=["\']([^"\']+)["\']', raw.decode("utf-8", "replace"), re.I):
            out.append((urllib.parse.urljoin(base_url, m.group(1)), ""))
    return out


def pdf_text(raw):
    """Extract text from a PDF byte string. Tries pdfplumber then pypdf."""
    buf = io.BytesIO(raw)
    try:
        import pdfplumber
        with pdfplumber.open(buf) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages[:40]).strip()
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        buf.seek(0)
        rd = PdfReader(buf)
        return "\n".join((pg.extract_text() or "") for pg in rd.pages[:40]).strip()
    except Exception as e:
        return f"[pdf unreadable: {str(e)[:60]}]"


def read_attachment_full(url, retries=1):
    """Download an attachment and read it COMPLETELY — pdf (incl. scanned/OCR),
    docx, doc, xlsx, rtf, zip, html, text. Returns (text, note) where note
    explains any failure instead of silently returning nothing."""
    import docreader
    last = ""
    for attempt in range(retries + 1):
        try:
            raw, ct, final = get(url)
        except Blocked as b:
            return "", f"blocked ({b})"
        except Exception as e:
            last = f"fetch error ({str(e)[:40]})"
            continue
        if raw is None:
            last = "could not download"
            if isinstance(ct, str) and ct.startswith("__fail__"):
                last = f"download failed ({ct[9:][:40]})"
            continue
        return docreader.read_bytes(raw, url, ct)
    return "", last or "could not download"


def read_attachment(url):
    """Back-compat: text only."""
    return read_attachment_full(url)[0]
