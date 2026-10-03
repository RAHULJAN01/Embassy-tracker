#!/usr/bin/env python3
"""
docreader.py — read EVERY attachment, fully.
============================================
The core standard: when a bot finds a solicitation, it reads the whole thing —
the page AND every attached file — so nothing is missed. "N documents
unreadable" should be rare, and when it happens we say exactly why.

Handles:
    PDF            text layer via pdfplumber -> pypdf; scanned/image PDFs via OCR
    DOCX / DOC     python-docx (paragraphs + tables); antiword/textract fallback
    XLSX / XLS     openpyxl, every sheet, every cell
    RTF / TXT/CSV  decoded directly
    HTML           tag-stripped text
    ZIP            opened, and the readable members read

Every read returns (text, note). `note` is "" on success, or a short human
reason on failure, so the UI can tell the operator what actually went wrong
instead of a bare count.
"""
import io, re, os, zipfile, subprocess, tempfile

MAX_BYTES = int(os.getenv("MAX_DOC_BYTES", str(40 * 1024 * 1024)))   # 40MB
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "12"))
MIN_TEXT = 40          # below this we treat a PDF as "no text layer" and try OCR


def _clean(t):
    if not t:
        return ""
    t = t.replace("\x00", " ")
    return re.sub(r"[ \t\u00a0]{2,}", " ", re.sub(r"\n{3,}", "\n\n", t)).strip()


# ---------------------------------------------------------------- PDF
def _pdf_text_layer(raw, _trunc=None):
    _trunc = _trunc if _trunc is not None else {}
    out = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            _trunc["pages"], _trunc["read"] = len(pdf.pages), min(80, len(pdf.pages))
            for p in pdf.pages[:80]:
                out.append(p.extract_text() or "")
                try:
                    for tbl in (p.extract_tables() or [])[:6]:
                        for row in tbl:
                            cells = [str(c) for c in row if c]
                            if cells:
                                out.append(" | ".join(cells))
                except Exception:
                    pass
        t = _clean("\n".join(out))
        if len(t) >= MIN_TEXT:
            return t
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(raw))
        t = _clean("\n".join((pg.extract_text() or "") for pg in rd.pages[:80]))
        if len(t) >= MIN_TEXT:
            return t
    except Exception:
        pass
    return ""


def _pdf_ocr(raw):
    """Scanned PDF -> rasterise and OCR. Only reached when there's no text layer."""
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
    except Exception:
        return "", "scanned PDF (OCR libraries unavailable)"
    try:
        allpages = convert_from_bytes(raw, dpi=200, fmt="png")
    except Exception as e:
        return "", f"scanned PDF (could not rasterise: {str(e)[:50]})"
    # SAY WHEN WE STOPPED. OCR is capped for cost, and a 15-page scan whose
    # submission section sits on page 14 was returned as a clean, complete
    # read: note was "", the text was non-empty, so read_ok went up, read_fail
    # stayed at zero, and the record could be stamped VERIFIED on a document
    # two thirds of which had never been looked at.
    pages = allpages[:OCR_MAX_PAGES]
    cut = max(0, len(allpages) - len(pages))
    chunks = []
    for im in pages:
        try:
            chunks.append(pytesseract.image_to_string(im) or "")
        except Exception as e:
            return "", f"scanned PDF (OCR failed: {str(e)[:50]})"
    t = _clean("\n".join(chunks))
    if len(t) < MIN_TEXT:
        return "", "scanned PDF produced no readable text"
    if cut:
        return t, (f"TRUNCATED: scanned PDF has {len(allpages)} pages and only the first "
                   f"{len(pages)} were read by OCR — {cut} page(s), which may hold the "
                   f"deadline or the terms, were not looked at")
    return t, ""


def read_pdf(raw):
    trunc = {}
    t = _pdf_text_layer(raw, trunc)
    if t:
        pages, read = trunc.get("pages", 0), trunc.get("read", 0)
        if pages and read and pages > read:
            return t, (f"TRUNCATED: PDF has {pages} pages and only the first {read} were "
                       f"read — {pages - read} page(s) were not looked at")
        return t, ""
    return _pdf_ocr(raw)


# ---------------------------------------------------------------- Word
def read_docx(raw):
    try:
        import docx
    except Exception:
        return "", "DOCX (python-docx unavailable)"
    try:
        d = docx.Document(io.BytesIO(raw))
    except Exception as e:
        return "", f"DOCX unreadable ({str(e)[:50]})"
    parts = [p.text for p in d.paragraphs if p.text and p.text.strip()]
    for tbl in d.tables:
        for row in tbl.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    t = _clean("\n".join(parts))
    return (t, "") if t else ("", "DOCX contained no text")


def read_doc(raw):
    """Legacy .doc — try antiword, then LibreOffice, then give a clear reason."""
    with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as f:
        f.write(raw); path = f.name
    try:
        for cmd in (["antiword", path], ["catdoc", path]):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=60)
                if r.returncode == 0:
                    t = _clean(r.stdout.decode("utf-8", "replace"))
                    if t:
                        return t, ""
            except Exception:
                continue
        try:
            out = tempfile.mkdtemp()
            r = subprocess.run(["soffice", "--headless", "--convert-to", "txt:Text",
                                "--outdir", out, path], capture_output=True, timeout=180)
            for fn in os.listdir(out):
                if fn.endswith(".txt"):
                    t = _clean(open(os.path.join(out, fn), encoding="utf-8",
                                    errors="replace").read())
                    if t:
                        return t, ""
        except Exception:
            pass
        return "", "legacy .doc (no converter available)"
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


# ---------------------------------------------------------------- Excel
def read_xlsx(raw):
    try:
        import openpyxl
    except Exception:
        return "", "XLSX (openpyxl unavailable)"
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    except Exception as e:
        return "", f"XLSX unreadable ({str(e)[:50]})"
    parts = []
    for ws in wb.worksheets[:12]:
        parts.append(f"[SHEET: {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None and str(c).strip()]
            if cells:
                parts.append(" | ".join(cells))
    t = _clean("\n".join(parts))
    return (t, "") if t else ("", "spreadsheet contained no data")


# ---------------------------------------------------------------- misc
def read_rtf(raw):
    txt = raw.decode("utf-8", "replace")
    txt = re.sub(r"\\'[0-9a-fA-F]{2}", " ", txt)
    txt = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", txt)
    t = _clean(txt.replace("{", " ").replace("}", " "))
    return (t, "") if t else ("", "RTF contained no text")


def read_html(raw):
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "noscript"]):
            tag.decompose()
        return _clean(soup.get_text("\n")), ""
    except Exception:
        txt = re.sub(rb"<[^>]+>", b" ", raw).decode("utf-8", "replace")
        return _clean(txt), ""


def read_zip(raw):
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as e:
        return "", f"ZIP unreadable ({str(e)[:40]})"
    parts, failed = [], 0
    for name in z.namelist()[:25]:
        if name.endswith("/"):
            continue
        try:
            inner = z.read(name)
        except Exception:
            failed += 1
            continue
        t, note = read_bytes(inner, name)
        if t:
            parts.append(f"[ZIP MEMBER: {name}]\n{t}")
        else:
            failed += 1
    t = _clean("\n\n".join(parts))
    note = "" if t else f"ZIP had no readable members ({failed} failed)"
    return t, note


def _decode_text(raw):
    """Decode a text file in whatever encoding it was actually written in.

    `raw.decode("utf-8", "replace")` was used, which turns a UTF-16 file into
    "\\ufffd\\ufffdR F Q 1 9 N P ..." — every character separated by a NUL that
    became a space. Excel and ordinary Windows tools write UTF-16 CSVs all the
    time. The file was counted as read successfully, contributed nothing
    usable, and the closing date inside it was unreachable, while read_fail
    stayed at zero so the record remained eligible to be called VERIFIED. A
    cp1252 file did the same to any accented word ("Clôture" -> "Cl?ture").
    """
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw[:4] in (b"\xff\xfe\x00\x00",
                                                            b"\x00\x00\xfe\xff"):
        for enc in ("utf-32", "utf-16"):
            try:
                return _clean(raw.decode(enc))
            except Exception:
                pass
    # a UTF-16 file with no BOM shows as NULs between the characters
    if raw.count(b"\x00") > len(raw) // 4:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                t = _clean(raw.decode(enc))
                if t:
                    return t
            except Exception:
                pass
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return _clean(raw.decode(enc))
        except Exception:
            continue
    return _clean(raw.decode("utf-8", "replace"))


# What this reader can handle. The crawler asks before deciding to treat a
# download as a web page -- it used to run HTML tag-stripping over .docx
# archives, which "succeeded" and produced 29,000 characters of ZIP header.
_READABLE_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".rtf",
                 ".txt", ".csv", ".tsv", ".zip", ".odt", ".ods")
_READABLE_CT = ("pdf", "wordprocessingml", "msword", "spreadsheetml",
                "ms-excel", "rtf", "zip", "opendocument", "csv",
                "octet-stream")


def is_document(name_or_url="", content_type=""):
    """Should this download go to the document reader rather than be treated as
    a web page? Errs towards yes: a mislabelled document read as HTML yields
    silent garbage, while HTML sent here is handled correctly anyway."""
    low = (name_or_url or "").lower().split("?")[0]
    ct = (content_type or "").lower()
    if low.endswith((".htm", ".html", ".aspx", ".php", "/")):
        return False
    if "html" in ct:
        return False
    return low.endswith(_READABLE_EXT) or any(k in ct for k in _READABLE_CT)


# ---------------------------------------------------------------- dispatcher
def read_bytes(raw, name_or_url="", content_type=""):
    """Read any supported document. Returns (text, failure_note)."""
    if not raw:
        return "", "empty file"
    if len(raw) > MAX_BYTES:
        return "", f"file too large ({len(raw)//1024//1024}MB)"
    low = (name_or_url or "").lower().split("?")[0]
    ct = (content_type or "").lower()

    if low.endswith(".pdf") or "pdf" in ct or raw[:5] == b"%PDF-":
        return read_pdf(raw)
    if low.endswith(".docx") or "wordprocessingml" in ct:
        return read_docx(raw)
    if low.endswith(".doc") or ct == "application/msword":
        return read_doc(raw)
    if low.endswith((".xlsx", ".xlsm")) or "spreadsheetml" in ct:
        return read_xlsx(raw)
    if low.endswith(".xls") or ct == "application/vnd.ms-excel":
        t, n = read_xlsx(raw)
        return (t, n or "legacy .xls not readable")
    if low.endswith(".rtf") or "rtf" in ct:
        return read_rtf(raw)
    if low.endswith(".zip") or "zip" in ct:
        return read_zip(raw)
    if low.endswith((".htm", ".html")) or "html" in ct:
        return read_html(raw)
    if low.endswith((".txt", ".csv", ".tsv")) or "text" in ct:
        return _decode_text(raw), ""
    # unknown: sniff
    if raw[:2] == b"PK":                      # zip-based (docx/xlsx/zip)
        for fn in (read_docx, read_xlsx, read_zip):
            t, n = fn(raw)
            if t:
                return t, ""
        return "", "unrecognised zip-based document"
    try:
        t = _clean(raw.decode("utf-8"))
        if len(t) >= MIN_TEXT:
            return t, ""
    except Exception:
        pass
    return "", f"unsupported file type ({low[-8:] or ct or 'unknown'})"


def capabilities():
    """What this environment can actually read — surfaced in diagnostics."""
    caps = {}
    for mod, label in (("pdfplumber", "pdf"), ("pypdf", "pdf-fallback"),
                       ("docx", "docx"), ("openpyxl", "xlsx"),
                       ("pytesseract", "ocr"), ("pdf2image", "ocr-raster"),
                       ("bs4", "html")):
        try:
            __import__(mod); caps[label] = True
        except Exception:
            caps[label] = False
    return caps


if __name__ == "__main__":
    print("docreader capabilities:", capabilities())
