#!/usr/bin/env python3
"""
fetcher.py — resilient web fetch + attachment reading for the crawler.
Handles HTML pages and PDF/doc attachments, with retries, timeouts, a browser
User-Agent, and a clear BLOCKED signal so the crawler can raise a HELP flag.
"""
import io, re, gzip, zlib, time, http.cookiejar
import urllib.request, urllib.error, urllib.parse, ssl

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
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

BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,application/pdf;q=0.8,*/*;q=0.7"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-CH-UA": '"Chromium";v="124", "Not:A-Brand";v="24", "Google Chrome";v="124"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Connection": "keep-alive",
}

# Why a site refused us — recorded so we can tell a login wall (which Rahul can
# open) from an IP/bot block (which he cannot do anything about).
BLOCK_DIAG = {}


class Blocked(Exception):
    """Site actively refused the bot. `kind` says whether a human can help."""

    def __init__(self, msg, kind="unknown", detail=""):
        super().__init__(msg)
        self.kind = kind            # "login" | "botwall" | "ratelimit" | "unknown"
        self.detail = detail


def _req(url, referer=""):
    h = dict(BROWSER_HEADERS)
    if referer:
        h["Referer"] = referer
        h["Sec-Fetch-Site"] = "same-origin"
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


def get(url, retries=2):
    """Fetch a URL. Returns (content_bytes, content_type, final_url).
    Raises Blocked on a refusal; returns (None,...) on soft failure."""
    last = None
    warmed = False
    for attempt in range(retries + 1):
        try:
            with _OPENER.open(_req(url), timeout=TIMEOUT) as r:
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
                if not warmed and e.code == 403:
                    warmed = True
                    try:
                        p = urllib.parse.urlparse(url)
                        root = f"{p.scheme}://{p.netloc}/"
                        with _OPENER.open(_req(root), timeout=TIMEOUT) as rr:
                            rr.read(2048)
                        time.sleep(2.0)
                        continue
                    except Exception:
                        pass
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
