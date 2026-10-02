#!/usr/bin/env python3
"""
pipeline.py — process ONE solicitation, completely, or not at all.
==================================================================
Rahul's rule, and it's the right one:

    "If the bots have refill fuel then it should be spent properly on one single
     thing until it completely processes — not add 20 different solicitation
     numbers and then have no tokens left to even fully scan one.
     Complete them fully, one by one, one at a time."

So a bot never *starts* a solicitation it cannot *finish*. Before beginning it
reserves the AI budget the full job needs; if the tank is too low it stops and
leaves the work for the next run. The result is a register of records that are
genuinely complete, instead of many half-read ones all begging to be verified.

A complete record means:
    1. every attachment downloaded and READ (pdf/docx/xls/scanned-OCR/zip)
    2. adjudicated by the AI into a tier with a cited reason
    3. dates and title present (harvested from the full text if the model missed)
    4. verification state computed from what we actually hold
    5. valued by the estimator when it is verified and doable
"""
import time

# AI calls a single solicitation can need: 1 adjudication (+1 retry) + 1 estimate
COST_ADJUDICATE = 1
COST_ESTIMATE = 1
COST_RETRY = 1
FULL_JOB_COST = COST_ADJUDICATE + COST_RETRY + COST_ESTIMATE      # reserve this much


class Budget:
    """The fuel tank. A job may only start if the whole job fits."""

    def __init__(self, max_calls, time_budget_s, started_at=None):
        self.max_calls = max_calls
        self.time_budget_s = time_budget_s
        self.started = started_at or time.time()
        self.used = 0
        self.stopped_reason = ""

    @property
    def left(self):
        return self.max_calls - self.used

    def elapsed(self):
        return time.time() - self.started

    def time_left(self):
        return self.time_budget_s - self.elapsed()

    def can_start_job(self, cost=FULL_JOB_COST, min_seconds=90):
        """Only begin a solicitation if we can see it through to the end."""
        if self.left < cost:
            self.stopped_reason = (f"stopping cleanly: {self.left} AI calls left, "
                                   f"a full solicitation needs {cost}")
            return False
        if self.time_left() < min_seconds:
            self.stopped_reason = "stopping cleanly: not enough time left to finish one"
            return False
        return True

    def spend(self, n=1):
        self.used += n
        return self.used


def process_one(unit, *, call_ai, analyzer, estimator, budget, today,
                fetch_attachments, status=None, label=""):
    """Take ONE solicitation all the way through. Returns (record|None, report).

    `unit` must carry:
        text         : the page/base text already in hand
        attachments  : list of attachment URLs still to read
        link, post, country, source, platform, agency, domestic, sol_hint
    `fetch_attachments(urls)` -> list of (url, text, note)
    """
    report = {"read_ok": 0, "read_fail": 0, "failures": [], "ai_calls": 0,
              "stage": "start", "complete": False}

    # ---- 1. READ EVERYTHING. No partial reads; every attachment gets opened.
    report["stage"] = "reading documents"
    if status:
        status.beat(currentJob=f"reading all documents: {label[:50]}")
    text = unit.get("text") or ""
    if text:
        report["read_ok"] += 1
    for url, atext, note in fetch_attachments(unit.get("attachments") or []):
        if atext:
            text += f"\n\n[DOCUMENT: {url}]\n{atext}"
            report["read_ok"] += 1
        else:
            report["read_fail"] += 1
            report["failures"].append({"file": url, "why": note or "unreadable"})

    if len(text.strip()) < 180:
        report["stage"] = "abandoned: nothing readable"
        return None, report

    # ---- 2. ADJUDICATE (with one retry, which the reservation already covers)
    report["stage"] = "adjudicating"
    if status:
        status.beat(currentJob=f"adjudicating: {label[:50]}")
    rec = analyzer.adjudicate(text, call_ai, today=today)
    budget.spend(COST_ADJUDICATE)
    report["ai_calls"] += 1

    rr = (rec.get("review_reason") or "").lower()
    transient = rec.get("tier") == "REVIEW" and (
        rr.startswith("ai") or "provider" in rr or "quota" in rr or "cooling" in rr)
    if transient and budget.left >= COST_RETRY:
        time.sleep(2)
        rec = analyzer.adjudicate(text, call_ai, today=today)
        budget.spend(COST_RETRY)
        report["ai_calls"] += 1
        rr = (rec.get("review_reason") or "").lower()
        transient = rec.get("tier") == "REVIEW" and (
            rr.startswith("ai") or "provider" in rr or "quota" in rr or "cooling" in rr)

    if transient:
        # the AI never actually answered — do NOT store a half-record
        report["stage"] = "abandoned: AI unavailable (will retry next run)"
        return None, report

    # ---- 3. BUILD THE RECORD (dates/title already back-filled by the analyzer)
    report["stage"] = "building record"
    rec["_full_text_len"] = len(text)
    rec["_read_ok"] = report["read_ok"]
    rec["_read_fail"] = report["read_fail"]
    rec["_read_failures"] = report["failures"][:10]

    # ---- 4. VALUE IT — only when it's verified and actually doable
    report["stage"] = "valuing"
    row_preview = {
        "verified": "VERIFIED" if (report["read_fail"] == 0 and rec.get("closing")
                                   and rec.get("title") and rec.get("tier") != "REVIEW") else "UNVERIFIED",
        "tier": rec.get("tier"), "archived": False, "value": rec.get("est_value", ""),
        "title": rec.get("title"), "country": unit.get("country", ""),
        "post": unit.get("post", ""), "sector": rec.get("sector", ""),
        "type": rec.get("classification", ""), "scope": rec.get("scope", ""),
        "shipping": rec.get("shipping", ""),
    }
    if estimator.should_estimate(row_preview) and budget.left >= COST_ESTIMATE:
        if status:
            status.beat(currentJob=f"valuing: {label[:50]}")
        est = estimator.estimate(row_preview, text[:12000], call_ai)
        budget.spend(COST_ESTIMATE)
        report["ai_calls"] += 1
        if est:
            rec["_estimate"] = est

    report["stage"] = "complete"
    report["complete"] = True
    return rec, report
