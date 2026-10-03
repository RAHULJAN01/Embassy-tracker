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
CRITICAL = ["proven", "budgetPanel", "whereTheWorkWent", "decisionLog", "renderPageTabs", "setPage", "beep",
            "toggleSound", "setVolume", "soundTheBots", "render", "rowHTML", "dossier", "renderNav", "renderMC", "renderLive",
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

# ------------------------------------------- VERIFIED must mean one thing only
# Five separate places each decided for themselves what VERIFIED meant, and the
# badge ended up on 24 records whose deadline could not be traced to any line in
# the source. There is now exactly one definition and every caller uses it; a
# new raw comparison to the stored flag is a regression, so it fails the build.
raw = len(re.findall(r"""verified\s*===?\s*['"]VERIFIED['"]""", js))
ok("nothing compares the stored VERIFIED flag directly except proven()'s own note",
   raw <= 1, f"{raw} raw comparisons")
ok("proven() requires the deadline to carry its source line",
   "dateEvidence && s.dateEvidence.closing" in js)
ok("proven() refuses a record carrying a date warning",
   "(s.dateWarnings||[]).length" in js)
ok("an untraceable deadline is called out to the reader",
   "Do not trust the deadline shown" in html)

# ------------------------------------------------- the masthead's shape is fixed
# The bar was a logo pinned far left, five buttons pinned far right, and 400px
# of nothing between them, because the brand was told to grow into the leftover
# space. It is now two rows that share one left edge with every band below:
# brand + timestamps, then the buttons. Each of these is the rule that holds
# that shape, so each one is a test.
ok("the logo and flag never resize", "--logo-h:90px" in html and "--flag-h:58px" in html)
ok("no scroll-triggered size rule survives anywhere",
   "body.scrolled .mastwrap .logo" not in html and "body.scrolled .mastwrap .flag" not in html)
ok("the brand does not grow into empty space", "flex:0 1 auto;max-width:100%}" in html)
ok("the buttons take a full row of their own", ".mnav{" in html and "flex:1 1 100%" in html)
ok("and they start on the same gutter, not the screen edge",
   "justify-content:flex-start" in html)
ok("the old right-hand stack is gone", "mhead-right" not in html)
ok("the control bar's spacer no longer steals width from the search controls",
   ".toolbar .spacer{display:none}" in html)

# ------------------------------------------------- one section per page
# The register used to be four tables stacked in one scroll. Each category now
# has its own tab and only one is drawn at a time.
ok("there is a tab strip", 'id="pageTabs"' in html)
ok("every category has a tab",
   all(k in html for k in ("'HIDDEN'", "'HISTORY'", "'REVIEW'", "'BID'", "'MID'", "'NO'")))
ok("only the open page is rendered", "html=tierSection(PAGE)" in js.replace(" ", ""))
ok("the open page is remembered between visits", "localStorage.setItem('mm_page'" in html)

# ------------------------------------------------- the look Rahul asked for
ok("the title bar buttons are equal cells in one row",
   ".mnav{display:grid" in html and "grid-template-columns:repeat(7,1fr)" in html)
ok("they are red with white text", "background:var(--red);border-color:var(--red);color:#fff" in html)
ok("the LLC name is larger", ".brand .bt .n{font-size:25px" in html)
ok("every country flag sits in a red badge with white text",
   ".flaglab{" in html and "color:#fff;background:var(--red)" in html)
ok("the country code is always printed, not only when the image fails",
   '<span class="flaglab">' in html)

# ------------------------------------------------- sound and budget
ok("a beep is made in the page, with no file to download",
   "AudioContext" in js and "createOscillator" in js)
ok("the beep follows what the bots are doing", "soundTheBots" in js)
ok("sound can be switched off", "function toggleSound(" in js)
ok("and has a volume control", "function setVolume(" in js and 'id="vol"' in html)
ok("both settings survive a reload",
   "localStorage.setItem('mm_snd'" in html and "localStorage.setItem('mm_vol'" in html)
ok("Mission Control shows what is left of the deposit", "function budgetPanel(" in js)
ok("and it is built from reported tokens, not a guess",
   "counted from what the API reported" in html)

# ------------------------------------------------- Mission Control detail
# A count of AI calls says what a run cost, not what it bought. These two
# panels say where the work went and what the bots actually decided.
ok("Mission Control shows where the run's work went", "function whereTheWorkWent(" in js)
ok("every outcome the bots can reach is broken out",
   all(k in html for k in ("Not a solicitation", "Already closed", "No deadline stated",
                           "Already settled", "Left for next run")))
ok("it shows the cost per page handled", "calls each" in html)
ok("the bots' own decisions are listed", "function decisionLog(" in js)
ok("each decision carries the post and the reason",
   'class="dl-p"' in html and 'class="dl-d"' in html)

# --------------------------------------------- DEEP SCAN is one record, not all
# Pressing DEEP SCAN on a solicitation used to dispatch a whole-fleet re-crawl,
# so the budget went everywhere except the record being asked about.
ok("DEEP SCAN asks for one solicitation on the strong model",
   "mode:'deepone',sol:id" in js.replace(" ", ""))
ok("and it no longer triggers a fleet-wide re-crawl",
   "action==='deepscan'||action==='roots'" not in js.replace(" ", ""))
ok("the record shows which model judged it", "s.deepScanBy" in js)
ok("the record shows how the deadline was established",
   "How the deadline was established" in html)
ok("and shows the line the deadline came from",
   "The line the deadline came from" in html)

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
