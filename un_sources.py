#!/usr/bin/env python3
"""
un_sources.py — United Nations procurement sources.
===================================================
Agencies: UNGM (the central portal — carries UNDP, IOM, ILO, UNICEF, WFP and more),
plus each agency's own notice board as a second net.

Access model (hybrid, as agreed):
  * PUBLIC notice feeds are read 24/7. They need no login, so there is no lockout
    risk and the bots never stall.
  * AUTHENTICATED access is attempted only when credentials for that agency are
    present in the environment. If login fails — wrong password, a captcha, or a
    2FA/OTP prompt — we do NOT retry blindly and we do NOT burn the account. We
    raise HoldTheDoor, which surfaces in the red HELP banner naming the platform
    and exactly what it needs from Rahul.
  * A light keep-alive touch keeps an established session from timing out.

Nothing here ever stores credentials; it only reads them from the environment.
"""
import os, re, json, time, urllib.parse

import fetcher


class HoldTheDoor(Exception):
    """Bot needs a human to open a door (login / OTP / captcha) on this platform."""
    def __init__(self, platform, url, need):
        super().__init__(f"{platform}: {need}")
        self.platform, self.url, self.need = platform, url, need


# --------------------------------------------------------------------------
# Public notice feeds — these work with no account at all.
# --------------------------------------------------------------------------
UN_SOURCES = [
    {"agency": "UNGM", "name": "UN Global Marketplace",
     "list": "https://www.ungm.org/Public/Notice",
     "base": "https://www.ungm.org"},
    {"agency": "UNDP", "name": "UNDP Procurement Notices",
     "list": "https://procurement-notices.undp.org/",
     "base": "https://procurement-notices.undp.org"},
    # UNDP runs supplier bidding on Oracle "Quantum" (eTendering); IOM uses an
    # Oracle supplier portal too. Both are login-gated, so they go through the
    # hold-the-door flow when credentials or a human session are available.
    {"agency": "UNDP", "name": "UNDP Quantum (eTendering)",
     "list": "https://estm.fa.em2.oraclecloud.com/fscmUI/faces/PrcPosRegisterSupplier",
     "base": "https://estm.fa.em2.oraclecloud.com", "gated": True},
    {"agency": "IOM", "name": "IOM Supplier Portal",
     "list": "https://www.iom.int/procurement-opportunities",
     "base": "https://www.iom.int", "gated": True},
    {"agency": "IOM", "name": "IOM Procurement",
     "list": "https://www.iom.int/procurement-opportunities",
     "base": "https://www.iom.int"},
    {"agency": "ILO", "name": "ILO Procurement",
     "list": "https://www.ilo.org/procurement/lang--en/index.htm",
     "base": "https://www.ilo.org"},
    {"agency": "UNICEF", "name": "UNICEF Supply Tenders",
     "list": "https://www.unicef.org/supply/tenders",
     "base": "https://www.unicef.org"},
]

NOTICE_HINTS = ("notice", "tender", "rfq", "rfp", "itb", "eoi", "procurement",
                "solicitation", "bid", "opportunit", "award", "requisition")


def _is_notice_link(href, text):
    hay = (href + " " + (text or "")).lower()
    if any(s in hay for s in ("facebook", "twitter", "x.com", "linkedin", "youtube")):
        return False
    return any(k in hay for k in NOTICE_HINTS)


def list_notices(src, limit=25):
    """Public notice links for one agency. Returns [(url, title), ...]."""
    try:
        raw, ct, final = fetcher.get(src["list"])
    except fetcher.Blocked as b:
        raise HoldTheDoor(src["agency"], src["list"],
                          "the site refused the bot (challenge or IP block) — "
                          "open it once in your browser so it lets us through")
    if raw is None:
        return []
    out, seen = [], set()
    for href, text in fetcher.links(raw, final):
        if not _is_notice_link(href, text):
            continue
        u = urllib.parse.urljoin(src["base"], href.split("#")[0])
        if u in seen:
            continue
        seen.add(u)
        out.append((u, (text or "").strip()[:180]))
    return out[:limit]


# --------------------------------------------------------------------------
# Authenticated access (only attempted when credentials exist)
# --------------------------------------------------------------------------
AUTH_ENV = {
    "UNGM": ("UNGM_USER", "UNGM_PASS"),
    "UNDP": ("UNDP_USER", "UNDP_PASS"),
    "IOM":  ("IOM_USER", "IOM_PASS"),
    "ILO":  ("ILO_USER", "ILO_PASS"),
}

_2FA_MARKERS = ("one-time", "otp", "two-factor", "2fa", "verification code",
                "authenticator", "security code", "captcha", "recaptcha")


def has_credentials(agency):
    u, p = AUTH_ENV.get(agency, ("", ""))
    return bool(u and p and os.getenv(u) and os.getenv(p))


def try_login(agency):
    """Attempt an authenticated session. Returns an opener on success.
    Raises HoldTheDoor when a human is required (2FA / captcha / rejected)."""
    if not has_credentials(agency):
        raise HoldTheDoor(agency, "", "no stored credentials — add them, or sign in "
                                      "so the bot can continue behind the login")
    import http.cookiejar, urllib.request
    src = next((s for s in UN_SOURCES if s["agency"] == agency), None)
    if not src:
        raise HoldTheDoor(agency, "", "unknown agency")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", fetcher.UA)]
    login_url = src["base"] + "/Account/Login"
    try:
        with opener.open(login_url, timeout=40) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception as e:
        raise HoldTheDoor(agency, login_url, f"could not reach the login page ({str(e)[:60]})")

    low = page.lower()
    if any(m in low for m in _2FA_MARKERS):
        raise HoldTheDoor(agency, login_url,
                          "this account asks for a one-time code / captcha — "
                          "sign in once and the bot will carry on from there")

    # carry any anti-forgery token the form hands us
    tok = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', page)
    user = os.getenv(AUTH_ENV[agency][0], "")
    pw = os.getenv(AUTH_ENV[agency][1], "")
    fields = {"UserName": user, "Email": user, "Password": pw}
    if tok:
        fields["__RequestVerificationToken"] = tok.group(1)
    data = urllib.parse.urlencode(fields).encode()
    try:
        with opener.open(urllib.request.Request(login_url, data=data), timeout=40) as r:
            after = r.read().decode("utf-8", "replace").lower()
    except Exception as e:
        raise HoldTheDoor(agency, login_url, f"login request failed ({str(e)[:60]})")

    if any(m in after for m in _2FA_MARKERS):
        raise HoldTheDoor(agency, login_url,
                          "a one-time code was requested — open the door and the bot resumes")
    if "invalid" in after or "incorrect" in after or "sign in" in after and "sign out" not in after:
        raise HoldTheDoor(agency, login_url,
                          "the credentials were not accepted — check them or sign in manually")
    return opener


def keep_alive(opener, agency):
    """Light touch so an established session doesn't time out between runs."""
    src = next((s for s in UN_SOURCES if s["agency"] == agency), None)
    if not (opener and src):
        return False
    try:
        with opener.open(src["list"], timeout=25) as r:
            r.read(2048)
        return True
    except Exception:
        return False


def fetch_notice(url, opener=None):
    """Read one notice page (authenticated if we have a session) + its attachments."""
    if opener is not None:
        try:
            with opener.open(url, timeout=40) as r:
                raw = r.read()
                final = r.geturl()
        except Exception:
            raw, final = None, url
        if raw:
            text = fetcher.html_text(raw)
            atts = [urllib.parse.urljoin(final, h) for h, _ in fetcher.links(raw, final)
                    if h.lower().split("?")[0].endswith((".pdf", ".doc", ".docx"))]
            for a in atts[:6]:
                t = fetcher.read_attachment(a)
                if t:
                    text += f"\n\n[ATTACHMENT: {a}]\n{t}"
                time.sleep(0.4)
            return text, atts
    # public path
    try:
        raw, ct, final = fetcher.get(url)
    except fetcher.Blocked:
        return "", []
    if raw is None:
        return "", []
    if "pdf" in (ct or "") or url.lower().endswith(".pdf"):
        return fetcher.pdf_text(raw), [url]
    text = fetcher.html_text(raw)
    atts = [h for h, _ in fetcher.links(raw, final)
            if h.lower().split("?")[0].endswith((".pdf", ".doc", ".docx"))]
    for a in atts[:6]:
        t = fetcher.read_attachment(a)
        if t:
            text += f"\n\n[ATTACHMENT: {a}]\n{t}"
        time.sleep(0.4)
    return text, atts


if __name__ == "__main__":
    print("UN agencies configured:", [s["agency"] for s in UN_SOURCES])
    print("credentials present for:", [a for a in AUTH_ENV if has_credentials(a)])
