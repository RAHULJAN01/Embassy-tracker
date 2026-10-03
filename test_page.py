#!/usr/bin/env python3
"""test_page.py — the portal must actually parse.

A single duplicated `const` once took the whole page down: no register, no
buttons, nothing. Nobody notices until they open it. So the built page is now
syntax-checked, and its critical functions are confirmed to exist exactly once,
before anything ships.
"""
import sys, re, json, shutil, pathlib, subprocess, tempfile

SRC = pathlib.Path(__file__).resolve().parent
FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


# ---------------------------------------------------------------- build it
work = pathlib.Path(tempfile.mkdtemp(prefix="page-"))
for f in ("site_template.html", "build_site.py"):
    shutil.copy(SRC / f, work / f)
for f in ("data.json", "status.json", "blocked.json", "control.json",
          "operator.json", "company.json", "flags.json"):
    if (SRC / f).exists():
        shutil.copy(SRC / f, work / f)
r = subprocess.run([sys.executable, "build_site.py"], cwd=work,
                   capture_output=True, text=True)
ok("the site builds", r.returncode == 0, (r.stderr or r.stdout)[-160:])
page = work / "public" / "index.html"
ok("index.html is produced", page.exists())
if not page.exists():
    sys.exit(1)
html = page.read_text()
print(f"  built {len(html)//1024} KB")

# ---------------------------------------------------------------- it must parse
i, j = html.rindex("<script>"), html.rindex("</script>")
js = html[i + 8:j].replace("/*__ENC__*/", "").replace("/*__LOCKED__*/", "false")
jsf = work / "page.js"
jsf.write_text(js)
node = shutil.which("node")
if node:
    r = subprocess.run([node, "--check", str(jsf)], capture_output=True, text=True)
    err = (r.stderr or "").strip().splitlines()
    ok("the page's JavaScript parses", r.returncode == 0,
       " / ".join(err[-3:])[:150] if r.returncode else "")
else:
    print("  SKIP  node not available to syntax-check")

# ---------------------------------------------------------------- no duplicates
# A function defined twice means an edit landed on top of itself; the second
# definition silently wins and the first one's callers get the wrong behaviour.
CRITICAL = ["render", "rowHTML", "dossier", "renderNav", "renderMC", "renderLive",
            "renderAlarm", "renderCompany", "downloadExcel", "buildWorkbook",
            "flagOf", "isoOf", "esc", "reviewNeeded", "needsBox", "collapseAll",
            "opAction", "effTier", "init", "unlock", "_sheetXML"]
for fn in CRITICAL:
    n = len(re.findall(r"\bfunction\s+" + re.escape(fn) + r"\s*\(", js))
    ok(f"  exactly one {fn}()", n == 1, f"found {n}")

# A genuine duplicate declaration in the same scope is a hard syntax error, so
# `node --check` above already catches it — which is exactly how the broken page
# was found. A regex cannot tell a module-level const from a function-local one,
# so it is not used here: the parser is the authority.

# ---------------------------------------------------------------- it must contain
MUST = {
    "numbering on every row": 'class="rnum"',
    "country flag under the reference": 'flagOf(s.country,"ref")',
    "what a review needs": "What this needs from you",
    "the line a date came from": "the line it came from",
    "bots-down alarm": "renderAlarm",
    "Excel export button": "downloadExcel()",
    "Collapse All": "collapseAll()",
    "DELETE / HIDE / SWITCH": "opAction(",
    "history of deletions": "I WAS DELETED ON",
}
for label, needle in MUST.items():
    ok(f"  page contains {label}", needle in html, "" if needle in html else needle)

# the header must not resize itself any more
ok("the header does not resize on scroll",
   "body.scrolled .mastwrap .logo" not in html)

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
