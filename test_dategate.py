#!/usr/bin/env python3
"""test_dategate.py — the date gate must be accurate AND cheap.

Two promises are tested here, and they pull against each other:

  ACCURACY. "BRO WE NEED A DATE FIRST ... THATS THE VERY BASIC THING TO EVEN
  DECIDE TO WETHER CHECK IT FULLY OR NOT." The gate used to be phrase-matching
  alone, so a notice reading "nothing submitted past 12/10/2026 shall be
  entertained" matched no phrase, was filed as having no date, and was never
  adjudicated at all. The model has to do that reading.

  COST. "MAKE SURE THE BOTS BURN TOKEN ONLY ON WHAT IS NECESSERY." So: a date
  the phrase list can find costs nothing; a document with no dates printed in it
  costs nothing, because there is nothing to ask about; the stronger model is
  only paid when the cheap one has already failed; and the excerpt sent is the
  text around the dates, not the document.
"""
import sys, pathlib

SRC = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import analyzer, estimator, pipeline                      # noqa: E402

FAILS = []
TODAY = "2026-10-02"


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


class Spy:
    """Stands in for Claude. Records every prompt and which model was asked."""

    def __init__(self, date_answer=None, sonnet_answer=None):
        self.calls = []                       # (kind, model, prompt_len)
        self.date_answer = date_answer or {}
        self.sonnet_answer = sonnet_answer

    def __call__(self, prompt, model=None):
        if prompt.startswith(analyzer.TRIAGE_PROMPT[:60]):
            self.calls.append(("date", model, len(prompt)))
            base = {"is_solicitation": True, "status": "open", "status_quote": "",
                    "what_it_is": "RFQ"}
            if model and self.sonnet_answer is not None:
                base.update(self.sonnet_answer)
            else:
                base.update(self.date_answer)
            return base
        if "adjudicating ONE solicitation" in prompt:
            self.calls.append(("adjudicate", model, len(prompt)))
            return {"tier": "BID", "confidence": 0.9, "title": "Gate spare parts",
                    "sol": "PR15305534", "sector": "COTS", "restrictions": [],
                    "route": "authorised dealer", "closing": "", "closing_quote": ""}
        self.calls.append(("estimate", model, len(prompt)))
        return {"unit_cost_usd": 10, "total_cost_usd": 100, "margin_usd": 20,
                "price_usd": 120, "confidence": 0.6}

    def kinds(self):
        return [k for k, _, _ in self.calls]

    def models(self, kind):
        return [m for k, m, _ in self.calls if k == kind]


def run(text, spy, attachments=(), calls=12):
    unit = {"text": text, "attachments": list(attachments), "link": "https://x.gov/n",
            "post": "Embassy Bujumbura", "country": "Burundi", "source": "SITE",
            "platform": "USGOV", "agency": "State", "domestic": False,
            "sol_hint": "PR15305534", "hash": "h1"}
    budget = pipeline.Budget(calls, 600)
    return pipeline.process_one(
        unit, call_ai=spy, analyzer=analyzer, estimator=estimator, budget=budget,
        today=TODAY, fetch_attachments=lambda urls: [], label="gate parts")


# ===================================================== THE PHRASING THAT BROKE IT
print("\n=== 'nothing submitted past X' — no phrase matches, the model reads it ===")
ODD = ("Request for Quotations: Gate Spare Parts Supply - PR15305534. The mission "
       "requires control modules, drive wheels, sensors and inverters. Nothing submitted "
       "past 12/10/2026 shall be entertained by the contracting officer. Questions to "
       "BujProcurement@state.gov. ") * 4
assert not analyzer.harvest_date(ODD, analyzer._DEADLINE_CUES), "a cue leaked into the fixture"

spy = Spy(date_answer={"closing": "2026-10-12",
                       "closing_quote": "Nothing submitted past 12/10/2026 shall be "
                                        "entertained by the contracting officer.",
                       "why": "it is the last moment a quote may be handed in"})
rec, rep = run(ODD, spy)
ok("the record is produced at all", rec is not None, rep.get("stage", ""))
ok("the gate resolved the deadline", rep.get("closing_found") == "2026-10-12",
   str(rep.get("closing_found")))
ok("and it says the model read it, not a phrase", rep.get("closing_how") == "read",
   str(rep.get("closing_how")))
if rec:
    ok("the deadline on the record is the proven one", rec.get("closing") == "2026-10-12",
       str(rec.get("closing")))
    ok("it carries the words it came from",
       bool((rec.get("date_evidence") or {}).get("closing")),
       str((rec.get("date_evidence") or {}).get("closing"))[:60])
    ok("it is marked as quoted, not phrase-matched",
       (rec.get("date_proof") or {}).get("closing") == "quoted", str(rec.get("date_proof")))
    ok("nothing is flagged on it", not rec.get("date_warnings"),
       str(rec.get("date_warnings"))[:70])
    import crawler as C
    v, why = C.verification_state(rec, 1, 0)
    ok("and the record can be VERIFIED", v == "VERIFIED", v + " " + "; ".join(why)[:70])
ok("the stronger model was not needed", spy.models("date") == [None], str(spy.models("date")))

# ================================== EVEN AN EASY ONE IS READ, NOT PATTERN-MATCHED
print("\n=== a phrase match is not allowed to settle the deadline ===")
# Phrase-matching used to decide this for free whenever a cue matched. A field
# test on real embassy notices measured what free was worth: of six notices it
# resolved, two right and four wrong -- including the cutoff for QUESTIONS on a
# New Delhi notice whose closing date was printed one line below it, and a 2023
# date lifted out of set-aside boilerplate on an Ottawa page, which would have
# archived a live procurement and retired it to the never-look-again ledger.
# A cue proves a deadline is DISCUSSED nearby; it cannot say which date is the
# one. That is reading comprehension, and it goes to the model.
PLAIN = ("Request for Quotations: Gate Spare Parts. Quotations are due by 12 October 2026 "
         "at 1600 hours local time. Items: control modules, drive wheels. ") * 4
spy2 = Spy(date_answer={"closing": "2026-10-12",
                        "closing_quote": "Quotations are due by 12 October 2026 at 1600 "
                                         "hours local time.",
                        "why": "it is the stated submission deadline"})
rec2, rep2 = run(PLAIN, spy2)
ok("the deadline is still resolved", rep2.get("closing_found") == "2026-10-12",
   str(rep2.get("closing_found")))
ok("the model is the one that read it", "date" in spy2.kinds(), str(spy2.kinds()))
ok("and it carries the line it came from", bool(rep2.get("closing_evidence")),
   str(rep2.get("closing_evidence"))[:60])

print("  -- the real notices the phrase matcher got wrong --")
import analyzer as _A
DELHI = ("U.S. Embassy New Delhi. Questions received after 5 p.m. on August 21, 2026, will not "
         "be answered. BID CLOSING DATE Quotations are due no later than 5 p.m. (India Time) "
         "on September 1, 2026. All quotations must be submitted electronically. ") * 3
ok("the phrase matcher alone still picks the WRONG date on New Delhi",
   _A.harvest_date(DELHI, _A._DEADLINE_CUES) == "2026-08-21",
   "it no longer decides, which is the point")
spy3 = Spy(date_answer={"closing": "2026-09-01",
                        "closing_quote": "Quotations are due no later than 5 p.m. (India Time) "
                                         "on September 1, 2026.",
                        "why": "the line headed BID CLOSING DATE"})
rec3, rep3 = run(DELHI, spy3)
ok("but the reader gets it right", rep3.get("closing_found") == "2026-09-01",
   str(rep3.get("closing_found")))

# ============================================== NO DATES PRINTED = FREE REJECTION
print("\n=== a notice with no dates printed stops after ONE cheap call ===")
# The first pass is a single call on an excerpt, and it is the only thing that
# runs before the money is spent. A notice with no deadline in it is not
# adjudicated, not priced, and no files are opened for it -- so the whole page
# costs one small call instead of the six a full job would take.
NONE = ("Request for Solicitations: Gate Spare Parts Supply - PR15305534. Items being "
        "acquired: control modules, drive wheels, sensors, switches, inverters and "
        "batteries. All questions to BujProcurement@state.gov. ") * 6
assert not analyzer.find_dates(NONE), "the fixture accidentally contains a date"
spy3 = Spy()
rec3, rep3 = run(NONE, spy3)
ok("it is not adjudicated", rec3 is None, str(rep3.get("stage")))
ok("it is recorded as having no date", rep3.get("noDate") is True)
ok("nothing was adjudicated or priced", "adjudicate" not in spy3.kinds()
   and "estimate" not in spy3.kinds(), str(spy3.kinds()))
ok("it cost the first pass and no more", len(spy3.kinds()) <= 2, str(spy3.kinds()))

print("\n=== the AI decides what IS a solicitation, not a keyword list ===")
INDEX = ("Procurement and Business Opportunities. Browse current and past solicitations "
         "below. See also: visas, education, news and events. ") * 8
spy4 = Spy()
spy4.date_answer = {"is_solicitation": False, "what_it_is": "index of notices",
                    "status": "open", "closing": "", "closing_quote": ""}
rec4, rep4 = run(INDEX, spy4)
ok("an index page is rejected by the reader", rec4 is None, str(rep4.get("stage"))[:50])
ok("and it says what the page actually is",
   "index" in (rep4.get("notSolicitation") or ""), str(rep4.get("notSolicitation")))
ok("nothing further was spent on it",
   "adjudicate" not in spy4.kinds() and "estimate" not in spy4.kinds(), str(spy4.kinds()))

print("\n=== a cancellation must be QUOTED, or the notice stays open ===")
LIVE = ("Request for Quotations PR15305534 - Gate Spare Parts. Quotations are due by "
        "12 October 2026 at 1600 hours. Items: control modules, drive wheels. ") * 4
spy5 = Spy()
spy5.date_answer = {"is_solicitation": True, "status": "closed",
                    "status_quote": "This solicitation has been cancelled.",
                    "closing": "2026-10-12",
                    "closing_quote": "Quotations are due by 12 October 2026 at 1600 hours."}
rec5, rep5 = run(LIVE, spy5)
ok("a cancellation nobody can point to is overruled", rec5 is not None,
   str(rep5.get("deadReason"))[:60])

# ===================================== THE STRONGER MODEL, ONLY AFTER A CHEAP MISS
print("\n=== Sonnet is asked only after the cheap read comes back empty ===")
HARD = ("Procurement schedule for Gate Spare Parts PR15305534.\nSite visit | 2026-09-20\n"
        "Submission | 2026-10-12\nAward | 2026-11-01\nItems: control modules, drive "
        "wheels, sensors, inverters, batteries for the compound gates. ") * 3
assert not analyzer.harvest_date(HARD, analyzer._DEADLINE_CUES), "a cue leaked in"
spy4 = Spy(date_answer={"closing": "", "closing_quote": "",
                        "why": "none of these looked like a submission deadline to me"},
           sonnet_answer={"closing": "2026-10-12", "closing_quote": "Submission | 2026-10-12",
                          "why": "the schedule row labelled Submission is the deadline"})
rec4, rep4 = run(HARD, spy4)
ok("the cheap model was asked first, unqualified", spy4.models("date")[:1] == [None],
   str(spy4.models("date")))
ok("then the stronger model was asked once",
   len(spy4.models("date")) == 2 and spy4.models("date")[1], str(spy4.models("date")))
ok("the stronger model is Sonnet", "sonnet" in str(spy4.models("date")[1] or "").lower(),
   str(spy4.models("date")[1]))
ok("and it rescued the deadline", rep4.get("closing_found") == "2026-10-12",
   str(rep4.get("closing_found")))
ok("recorded as read by the stronger model", rep4.get("closing_how") == "read-by-sonnet",
   str(rep4.get("closing_how")))

# ================================================ A MODEL GUESS IS STILL REFUSED
print("\n=== the reader cannot invent one either ===")
spy5 = Spy(date_answer={"closing": "2026-11-30",
                        "closing_quote": "Quotations are due by 30 November 2026.",
                        "why": "I inferred it"},
           sonnet_answer={"closing": "", "closing_quote": "", "why": "none stated"})
rec5, rep5 = run(ODD, spy5)
ok("a quote that is not in the document is refused",
   rep5.get("closing_found") != "2026-11-30", str(rep5.get("closing_found")))
ok("so the notice is filed as having no proven deadline", rec5 is None,
   str(rep5.get("stage")))

# ==================================================== THE EXCERPT, NOT THE BOOK
print("\n=== the model is sent the lines around the dates, not the document ===")
BIG = (("Standard terms and conditions clause. " * 400)
       + "Nothing submitted past 12/10/2026 shall be entertained. "
       + ("More boilerplate that nobody needs to read for a date. " * 400))
spy6 = Spy(date_answer={"closing": "2026-10-12",
                        "closing_quote": "Nothing submitted past 12/10/2026 shall be "
                                         "entertained.", "why": "deadline"})
rec6, rep6 = run(BIG, spy6)
sent = [n for k, _, n in spy6.calls if k == "date"]
adj = [n for k, _, n in spy6.calls if k == "adjudicate"]
ok("the deadline is still found in a long document", rep6.get("closing_found") == "2026-10-12",
   str(rep6.get("closing_found")))
ok("the date prompt is far smaller than the document",
   bool(sent) and sent[0] < len(BIG) / 3, f"{sent[0] if sent else 0} chars vs {len(BIG)}")
ok("and smaller than the adjudication prompt it replaces",
   bool(sent and adj) and sent[0] < adj[0], f"date={sent[0] if sent else 0} adj={adj[0] if adj else 0}")

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
