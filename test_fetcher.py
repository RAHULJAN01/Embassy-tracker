#!/usr/bin/env python3
"""test_fetcher.py — the identity the bots crawl with is locked down here.

Why this file exists, in one paragraph, so nobody re-learns it the hard way:

Adding `Sec-Fetch-*` / `Sec-CH-UA` headers to the fetcher took the crawl from
0 refused sites to 167 refused sites in two runs. The repair attempt — switching
to an honest named-crawler User-Agent — was then measured against the real
embassy posts and was WORSE: 403 on 14 of 14. The only configuration ever
measured as working is a plain browser User-Agent plus three ordinary headers:

    honest-bot      ok=0   refused=14   codes={'403': 14}
    plain-urllib    ok=0   refused=14   codes={'403': 14}
    chrome-claim    ok=14  refused=0    codes={'200': 14}

So this suite fails the build if anyone (including a future me, reasoning from
first principles instead of from numbers) reintroduces a fingerprint header,
drops the browser UA, or sneaks a header in on a code path that the obvious
check on BROWSER_HEADERS would miss.
"""
import sys, pathlib, importlib

SRC = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


import fetcher
importlib.reload(fetcher)

# ------------------------------------------------------------------ the dict
hdrs = fetcher.BROWSER_HEADERS
ok("exactly three headers are sent", len(hdrs) == 3, f"{len(hdrs)}: {sorted(hdrs)}")
ok("User-Agent claims a browser",
   "Mozilla/5.0" in hdrs.get("User-Agent", "") and "Chrome/" in hdrs.get("User-Agent", ""),
   hdrs.get("User-Agent", "")[:50])
ok("no header names itself a bot/crawler",
   not any(w in hdrs.get("User-Agent", "").lower()
           for w in ("bot", "crawler", "spider", "madison")),
   hdrs.get("User-Agent", "")[:60])
for name in sorted(hdrs):
    ok(f"  header {name} is not a fingerprint header",
       not name.lower().startswith("sec-"))

# --------------------------------------------- every request actually built
# The blackout header lived in _req(), not in BROWSER_HEADERS, so check the
# real Request objects on BOTH branches rather than the dict alone.
for label, referer in (("without a referer", ""), ("with a referer", "https://example.gov/")):
    req = fetcher._req("https://example.gov/page", referer=referer)
    sent = {k.lower(): v for k, v in req.header_items()}
    bad = [k for k in sent if k.startswith("sec-")]
    ok(f"request {label} sends no Sec-* header", not bad, ",".join(bad))
    ok(f"request {label} carries the browser UA", "Mozilla/5.0" in sent.get("user-agent", ""))
ok("a referer is still passed through when given",
   fetcher._req("https://example.gov/p", referer="https://example.gov/").get_header("Referer")
   == "https://example.gov/")

# ------------------------------------------------------- nothing in the source
# A header could also be set on a path this suite does not call. The source is
# scanned so that even an unreached line fails the build.
src = (SRC / "fetcher.py").read_text()
offenders = [ln.strip() for ln in src.splitlines()
             if ('"Sec-' in ln or "'Sec-" in ln) and not ln.strip().startswith("#")]
ok("no Sec-* header is set anywhere in fetcher.py", not offenders,
   offenders[0][:60] if offenders else "")

# ------------------------------------------------------------------- manners
# Looking like a browser is allowed. Ignoring a site's stated wishes is not —
# that distinction is the whole reason this is defensible, so it is a test.
ok("robots.txt is still consulted before fetching", "robots_ok(url)" in src)
ok("a robots refusal is a hard stop", 'kind="robots"' in src)
ok("refusals are classified so a human knows which ones he can open",
   all(k in src for k in ('"login"', '"botwall"', '"ratelimit"')))

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
