#!/usr/bin/env python3
"""
fetcher.py — resilient web fetch + attachment reading for the crawler.
Handles HTML pages and PDF/doc attachments, with retries, timeouts, a browser
User-Agent, and a clear BLOCKED signal so the crawler can raise a HELP flag.
"""
import io, re, time, urllib.request, urllib.error, urllib.parse, ssl

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 45
_CTX = ssl.create_default_context()


class Blocked(Exception):
    """Site actively refused the bot (403/401/captcha). Needs hold-the-door."""


def _req(url):
    return urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })


def get(url, retries=2):
    """Fetch a URL. Returns (content_bytes, content_type, final_url).
    Raises Blocked on 401/403/429-captcha; returns (None,...) on soft failure."""
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(_req(url), timeout=TIMEOUT, context=_CTX) as r:
                ct = (r.headers.get("Content-Type") or "").lower()
                return r.read(), ct, r.geturl()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise Blocked(f"HTTP {e.code} at {url}")
            if e.code in (429,):
                raise Blocked(f"HTTP 429 (rate-limited / challenge) at {url}")
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
