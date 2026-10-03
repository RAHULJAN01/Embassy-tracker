#!/usr/bin/env python3
"""test_pipelinetraps.py — the silent failures found by auditing the bot's path.

None of these raised an error. Every one produced a record that looked fine on
the page and was wrong, or deleted a record with nothing to show it had ever
existed. That is what makes them worth a permanent test: a crash gets noticed,
a confident wrong answer does not.
"""
import sys, pathlib

SRC = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import analyzer as A, pipeline as P, crawler as C, merge_shards as M, docreader as D  # noqa

FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


# =============================================== one page, two notices
print("\n=== a notice keeps its OWN deadline, not its neighbour's ===")
PAGE = ("U.S. Embassy Kathmandu — Office Chairs\n"
        "Solicitation No. 19AB5026Q0011\nSupply of 40 office chairs for the chancery.\n"
        + "Specification details follow. " * 12 +
        "Quotations are due 15 October 2026 at 1500 hrs local time.\n\n"
        "U.S. Embassy Kathmandu — Janitorial Services\n"
        "Solicitation No. 19AB5026R0022\nProvision of janitorial services.\n"
        + "Scope of work details follow. " * 12 +
        "Proposals are due 28 February 2027 at 1500 hrs local time.\n")
blocks = dict(C.split_inline_solicitations(PAGE))
ok("both notices are found", len(blocks) == 2, str(list(blocks)))
got = {s: A.harvest_date(t, A._DEADLINE_CUES) for s, t in blocks.items()}
ok("the first keeps 15 October", got.get("19AB5026Q0011") == "2026-10-15",
   str(got.get("19AB5026Q0011")))
ok("the second keeps 28 February", got.get("19AB5026R0022") == "2027-02-28",
   str(got.get("19AB5026R0022")))
for sol, body in blocks.items():
    others = [o for o in blocks if o != sol]
    ok(f"  {sol} does not contain {others[0]}", others[0] not in body)

# =============================================== identity
print("\n=== two solicitations never collapse into one row ===")
ROWS = [{"sol": "RFQ-001", "post": "Kathmandu", "title": "Office chairs"},
        {"sol": "RFP-001", "post": "Dhaka", "title": "Janitorial"},
        {"sol": "RFQ-001", "post": "Dhaka", "title": "Stationery"},
        {"sol": "19CA1026Q0002", "post": "Lusaka", "title": "Fuel"},
        {"sol": "RFQ 19CA1026Q0002", "post": "Lusaka", "title": "Fuel, seen again"},
        {"sol": "#19CA1026Q0002", "post": "Lusaka", "title": "Fuel, third sighting"}]
keys = [M.key_of(r) for r in ROWS]
ok("three different short references stay three records", len(set(keys[:3])) == 3, str(keys[:3]))
ok("one long reference written three ways stays ONE record", len(set(keys[3:])) == 1, str(keys[3:]))

print("  -- and a title can never be used as an identity --")
for bad in ("Supply of laptops", "Request for quotation", "Janitorial services"):
    ok(f"  {bad!r} is rejected as a reference", M.clean_sol({"sol": bad})["sol"] == "")
for good in ("19NP5026Q0014", "RFQ-001", "PR15305534"):
    ok(f"  {good!r} is kept", M.clean_sol({"sol": good})["sol"] == good)

# =============================================== the junk filter
print("\n=== a record kept on purpose is never deleted as junk ===")
SAFETY = {"title": "Skip to main content", "sol": "", "deadline": "",
          "link": "https://np.usembassy.gov/notice",
          "needs": "a deadline — open the notice and read the submission date"}
ok("the no-deadline safety record survives the fleet merge", not M.is_junk(SAFETY))
ok("a record listing the dates it saw survives too",
   not M.is_junk({"title": "Home", "sol": "", "deadline": "",
                  "link": "https://x.usembassy.gov/n", "datesSeen": ["2026-10-12"]}))
ok("a real index page is still removed",
   M.is_junk({"title": "Home", "sol": "", "deadline": "",
              "link": "https://np.usembassy.gov/business/"}))

print("  -- and the title picker does not hand it nav text in the first place --")
ok("website furniture is skipped",
   P._first_title("Skip to main content\nU.S. Embassy in Zambia\n"
                  "Supply of Gate Spare Parts - PR15305534\nItems:")
   == "Supply of Gate Spare Parts - PR15305534")

# =============================================== documents
print("\n=== a file is either read or reported, never quietly half-read ===")
ok("a .docx is sent to the document reader, not the HTML stripper",
   D.is_document("https://x.usembassy.gov/files/sow.docx", ""))
ok("an .xlsx too", D.is_document("sched.xlsx", ""))
ok("a web page is not", not D.is_document("https://x.usembassy.gov/business/", "text/html"))
ok("content-type alone is enough",
   D.is_document("download", "application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document"))

print("  -- text files in the encodings Windows and Excel actually produce --")
for label, raw in [
    ("UTF-16 with BOM", "RFQ 19NP5026Q0014\nClosing date: 2026-10-12\n".encode("utf-16")),
    ("UTF-16 no BOM", "RFQ 19NP5026Q0014\nClosing date: 2026-10-12\n".encode("utf-16-le")),
    ("cp1252 accents", "Clôture des offres: 2026-10-12\n".encode("cp1252")),
    ("plain utf-8", "Closing date: 2026-10-12\n".encode("utf-8")),
]:
    t, _n = D.read_bytes(raw, "schedule.csv")
    ok(f"  {label}: the date inside is reachable", "2026-10-12" in A.find_dates(t), repr(t[:40]))

# =============================================== a model typo
print("\n=== a malformed answer from the model costs one record, never a run ===")
for bad in ({"tier": "BID", "confidence": "high"},
            {"tier": "BID", "confidence": 0.9, "restrictions": {"text": "x"}},
            {"tier": "BID", "confidence": None, "closing": {"d": 1}}):
    try:
        r = A.adjudicate("Some notice text. " * 30, lambda p, b=bad: b, today="2026-10-03")
        crashed, tier = False, r.get("tier")
    except Exception as e:
        crashed, tier = True, type(e).__name__
    ok(f"  {str(bad)[:46]} does not raise", not crashed, str(tier))

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
