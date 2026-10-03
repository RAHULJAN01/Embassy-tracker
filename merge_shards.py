#!/usr/bin/env python3
"""
merge_shards.py — combine the parallel bots' findings into ONE register.

Each sharded worker owns a different slice of the sources, so their results are
disjoint by construction. This merges them and still de-duplicates defensively
(by solicitation number, then by link) so a solicitation can never appear twice
even if two bots happened to see it.
"""
import os, sys, re, json, pathlib, datetime

HERE = pathlib.Path(__file__).parent


def today():
    return datetime.date.today().isoformat()


def load(p, d):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return d


def _norm(s):
    s = (s or "").upper().strip()
    s = re.sub(r"^(RFQ|RFP|ITB|IFB|SOL|NO\.?|#)[\s:#-]*", "", s)
    return re.sub(r"[^A-Z0-9]", "", s)


# A reference that is globally unique on its own, versus one that is only
# unique within its own post. '19CA1026Q0002' is the first kind. '001' is the
# second, and dozens of posts issue an RFQ-001 every year.
_WEAK_REF = re.compile(r"^\d{1,6}$")
_REF_TYPE = re.compile(r"^(RFQ|RFP|ITB|IFB|SOL)\b", re.I)


def key_of(r):
    """Normalised identity: '#19CA1026Q0002', '19CA1026Q0002' and 'RFQ 19CA1026Q0002'
    are the SAME solicitation and must never become two rows.

    AND TWO DIFFERENT SOLICITATIONS MUST NEVER BECOME ONE ROW. _norm strips the
    RFQ/RFP/ITB prefix and all punctuation, which turned Kathmandu's 'RFQ-001'
    (office chairs, closing 20 October) and Dhaka's 'RFP-001' (janitorial,
    closing 30 November) into the same key '001'. The fleet merge then kept one
    and the other was gone from the register -- not archived, not counted as
    dropped, no note anywhere. A silent delete is the worst failure this
    pipeline has, because nothing on the page shows that it happened.

    So a short or purely numeric reference is qualified by the post that issued
    it and by its own RFQ/RFP type. A long distinctive reference is left exactly
    as it was, which is what keeps the de-duplication above working.
    """
    sol = (r.get("sol") or "").strip()
    base = _norm(sol)
    if not base:
        return r.get("link") or ""
    if len(base) >= 7 and not _WEAK_REF.match(base):
        return base
    m = _REF_TYPE.match(sol)
    kind = (m.group(1).upper() if m else "")
    scope = _norm(r.get("post") or "") or _norm(r.get("country") or "") \
        or _norm(r.get("platform") or "")
    return ":".join(x for x in (scope, kind, base) if x)


def prev_key_of(r):
    """The identity a record held BEFORE a deep re-scan. When a re-scan finally
    reads a solicitation number off the attachments, the old row must be replaced,
    never left behind as a twin."""
    p = r.get("prevKey") or ""
    if not p:
        return ""
    k = _norm(p)
    return k or p


def _merge_pair(a, b):
    """Same solicitation seen twice (e.g. once on SAM, once on the embassy site).
    Keep the richer record but remember it was found on BOTH."""
    keep = better(a, b)
    other = b if keep is a else a
    srcs = set()
    for r in (a, b):
        for piece in str(r.get("source") or "").replace("+", " ").split():
            if piece.strip():
                srcs.add(piece.strip().upper())
    if {"SAM", "SITE"} <= srcs:
        keep["source"] = "Site+SAM"
    elif srcs:
        keep["source"] = "Site+SAM" if len(srcs) > 1 else keep.get("source")
    # don't lose anything the other copy had
    for f in ("deadline", "posted", "value", "setaside", "samId", "estimate", "citation"):
        if not keep.get(f) and other.get(f):
            keep[f] = other[f]
    # The repair counter must SURVIVE the merge. One bot may have just spent a
    # deep re-scan on this record while another still holds an untouched copy;
    # if the untouched copy wins, the attempt is forgotten and the record gets
    # retried for ever instead of giving up and asking for a human.
    tries = max(int(a.get("repairTries", 0) or 0), int(b.get("repairTries", 0) or 0))
    if tries:
        keep["repairTries"] = tries
    scans = [r.get("lastDeepScan") for r in (a, b) if r.get("lastDeepScan")]
    if scans:
        keep["lastDeepScan"] = max(scans)
    # likewise the operator's own decisions, whichever copy carries them
    for f in ("deleted", "deletedOn", "hidden", "hiddenOn", "switched", "switchedOn",
              "firstSeen", "notes"):
        if not keep.get(f) and other.get(f):
            keep[f] = other[f]
    # if the winner has no explanation but the re-scanned copy does, keep the words
    if not keep.get("verifyNotes") and other.get("verifyNotes"):
        keep["verifyNotes"] = other["verifyNotes"]
    if keep.get("verified") != "VERIFIED":
        gave_up = [n for r in (a, b) for n in (r.get("verifyNotes") or [])
                   if "gave up" in n or "re-scan" in n]
        if gave_up:
            notes = [n for n in (keep.get("verifyNotes") or [])
                     if "gave up" not in n and not n.startswith("re-scan")]
            keep["verifyNotes"] = (notes + gave_up[-1:])[:6]
    files = list(dict.fromkeys((keep.get("files") or []) + (other.get("files") or [])))
    keep["files"] = files
    keep["fileCount"] = len(files)
    return keep


def better(a, b):
    """Prefer the richer record: verified > more files > more complete > newer."""
    def score(r):
        s = 0
        if r.get("verified") == "VERIFIED": s += 100
        if r.get("deadline"): s += 20
        if r.get("tier") in ("BID", "MID", "NO"): s += 15
        s += min(len(r.get("files") or []), 10)
        s += 5 if r.get("citation") else 0
        s += len(r.get("restrictions") or [])
        return s
    return a if score(a) >= score(b) else b


_INDEX_PATHS = ("/business/", "/business", "/procurement/", "/procurement", "/jobs/",
                "/tenders/", "/opportunities/", "/doing-business")


_NAV_TITLES = ("jump into the main content", "skip to main content", "doing business in",
               "procurement seminar", "business ready", "home page", "search results",
               "privacy policy", "contact us")
_SOL_OK = re.compile(r"^[A-Z0-9][A-Z0-9\-/_.#]{4,}$", re.I)


def clean_sol(r):
    """A reference number is a reference number. If an earlier pass shoved a whole
    sentence in there, drop it so it can't masquerade as an identity."""
    sol = (r.get("sol") or "").strip()
    if not sol:
        return r
    squashed = sol.replace(" ", "")
    if len(sol) > 30 or not _SOL_OK.match(squashed):
        r["sol"] = ""
    elif not any(c.isdigit() for c in squashed):
        # A REFERENCE NUMBER HAS A NUMBER IN IT. _SOL_OK's character class
        # accepts letters, so a title shoved into this field by an earlier pass
        # sailed through: the UN path passes the title as a fallback hint, and
        # "Supply of laptops" became the record's IDENTITY. Two UN notices with
        # that title then merged into one row and one of them was deleted.
        r["sol"] = ""
    return r


def is_junk(r):
    """A record that is not actually a solicitation — an index/landing page that an
    earlier crawl stored by mistake. These get removed from the register entirely,
    so the operator is never asked to 'verify' something that was never a notice."""
    link = (r.get("link") or "").lower().split("?")[0].rstrip("/")
    title = (r.get("title") or "").strip()
    sol = (r.get("sol") or "").strip()
    has_real_sol = bool(sol) and sol.lower() not in ("none", "n/a", "-")

    # A RECORD DELIBERATELY KEPT IS NEVER JUNK.
    #
    # When a notice is read but no deadline can be proven in it, the crawler
    # writes a row on purpose -- "recorded so it is never silently lost" -- and
    # hands it to a human with a note saying what it needs. Those rows have no
    # deadline by definition, and their title comes from the first readable line
    # of the page, which on an embassy site is often "Skip to main content".
    # Both of the clauses below then matched, and the safety record was deleted
    # in the fleet merge: the exact silent loss it existed to prevent.
    if r.get("needs") or r.get("noDate") or (r.get("datesSeen") or []):
        return False

    # the link IS the procurement index page itself, with nothing identifying a notice
    for p in _INDEX_PATHS:
        if link.endswith(p.rstrip("/")) and not has_real_sol:
            return True
    # off-site pages that were never this mission's solicitation
    if "usembassy.gov" not in link and "sam.gov" not in link and "ungm.org" not in link \
       and "undp.org" not in link and "iom.int" not in link and "ilo.org" not in link \
       and "unicef.org" not in link and link:
        if not has_real_sol:
            return True
    t = title.lower()
    if any(t.startswith(n) or n in t for n in _NAV_TITLES) and not r.get('deadline'):
        return True
    # nothing to show and nothing to chase
    if (not title or title.startswith("(untitled")) and not has_real_sol and not r.get("deadline"):
        return True
    return False


def normalize(r):
    """Backfill fields on records written before the v2 schema so they still
    slot into the platform / sector / archive structure."""
    r.setdefault("platform", "USGOV")
    r.setdefault("agency", "")
    r.setdefault("sector", "")
    r.setdefault("domestic", False)
    r.setdefault("files", [])
    r.setdefault("fileCount", len(r.get("files") or []))
    r.setdefault("verifyNotes", [])
    if not r.get("verified"):
        notes = []
        if not r.get("deadline"): notes.append("no closing date found")
        if r.get("tier") == "REVIEW": notes.append("not adjudicated")
        r["verified"] = "VERIFIED" if not notes else "UNVERIFIED"
        r["verifyNotes"] = notes
    dl = r.get("deadline") or ""
    cancelled = str(r.get("status", "")).lower() in ("cancelled", "canceled", "removed")
    if (dl and dl < today()) or cancelled:
        r["archived"] = True
        if dl and dl < today() and str(r.get("status", "")) in ("Active", "Check", ""):
            r["status"] = "Expired"
        r.setdefault("archivedOn", today())
    else:
        r["archived"] = bool(r.get("archived", False)) if not dl else False
    return r


def apply_operator(rows):
    """The operator's own decisions outrank the bots'.

    DELETE  -> off the active register for good, into History, stamped with the
               date and time it was deleted. Never shown as active again.
    HIDE    -> out of sight in the Hidden cart until it is un-hidden, or until it
               expires or is cancelled, at which point it joins History.
    SWITCH  -> the tier the operator chose, which no re-scan may overwrite.
    """
    op = load(HERE / "operator.json", {})
    dele, hid, sw = op.get("deleted") or {}, op.get("hidden") or {}, op.get("switched") or {}
    if not (dele or hid or sw):
        return rows
    for r in rows:
        for k in (r.get("sol"), r.get("link")):
            if not k:
                continue
            if k in dele:
                r["deleted"] = True
                r["deletedOn"] = dele[k].get("on", "")
                r["archived"] = True
            if k in hid:
                r["hidden"] = True
                r["hiddenOn"] = hid[k].get("on", "")
            if k in sw and sw[k].get("tier") in ("BID", "MID", "NO", "REVIEW"):
                r["tier"] = sw[k]["tier"]
                r["switched"] = True
                r["switchedOn"] = sw[k].get("on", "")
            break
        # a hidden solicitation that has since expired or been cancelled stops
        # hiding and goes into History, documented — nothing is silently dropped
        if r.get("hidden") and r.get("archived") and not r.get("deleted"):
            r["hidden"] = False
            r["unhiddenBecause"] = "expired or cancelled while hidden"
    return rows


# ---------------------------------------------------------------- SPEND LEDGER
# What a million tokens costs, per model. Set MODEL_RATES_JSON in the workflow
# to change these without a code change -- e.g.
#   {"claude-haiku-4-5-20251001": [1.00, 5.00], "claude-sonnet-4-5-20250929": [3.00, 15.00]}
# The first number is input, the second output, both per million tokens.
DEFAULT_RATES = {
    "haiku": (1.00, 5.00),
    "sonnet": (3.00, 15.00),
    "opus": (15.00, 75.00),
}


def _rates_for(model_name):
    try:
        override = json.loads(os.getenv("MODEL_RATES_JSON", "") or "{}")
    except Exception:
        override = {}
    if model_name in override:
        r = override[model_name]
        return float(r[0]), float(r[1])
    low = (model_name or "").lower()
    for k, v in DEFAULT_RATES.items():
        if k in low:
            return v
    return DEFAULT_RATES["haiku"]


def _cost(by_model):
    total = 0.0
    for name, v in (by_model or {}).items():
        rin, rout = _rates_for(name)
        total += int(v.get("in") or 0) / 1e6 * rin + int(v.get("out") or 0) / 1e6 * rout
    return total


def _update_spend_ledger(by_model, tin, tout):
    """Add this run to the running total the portal shows.

    Rahul: "I WOULD ALSO LIKE TO KNOW THE ABOUT THAT IS LEFT FOR THE 40 DOLLERS
    THAT ARE SPENT... SO I CAN ALWAYS CHECK FROM THERE WHERE AM I IN REAL TIME."

    The token counts here are not estimates -- the API returns them with every
    answer, so they are exactly what was used. The dollar figure is those tokens
    priced at the rates above. The deposit is whatever API_DEPOSIT_USD says,
    defaulting to the 40 dollars Rahul put in.
    """
    path = HERE / "spend.json"
    try:
        led = json.loads(path.read_text())
    except Exception:
        led = {}
    led.setdefault("depositUSD", float(os.getenv("API_DEPOSIT_USD", "40") or 40))
    led.setdefault("byModel", {})
    led.setdefault("runs", [])
    if not (tin or tout):
        led["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")
        path.write_text(json.dumps(led, indent=1))
        return led
    for name, v in (by_model or {}).items():
        slot = led["byModel"].setdefault(name, {"in": 0, "out": 0, "calls": 0})
        slot["in"] += int(v.get("in") or 0)
        slot["out"] += int(v.get("out") or 0)
        slot["calls"] += int(v.get("calls") or 0)
    if not by_model:                       # older shard with no per-model split
        slot = led["byModel"].setdefault("unknown", {"in": 0, "out": 0, "calls": 0})
        slot["in"] += tin
        slot["out"] += tout
    led["inputTokens"] = sum(v["in"] for v in led["byModel"].values())
    led["outputTokens"] = sum(v["out"] for v in led["byModel"].values())
    led["calls"] = sum(v.get("calls", 0) for v in led["byModel"].values())
    led["spentUSD"] = round(_cost(led["byModel"]), 4)
    led["leftUSD"] = round(max(0.0, led["depositUSD"] - led["spentUSD"]), 4)
    led["rates"] = {m: list(_rates_for(m)) for m in led["byModel"]}
    led["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    led["runs"] = (led["runs"] + [{
        "at": led["updated"], "in": tin, "out": tout,
        "usd": round(_cost(by_model) if by_model else 0.0, 4)}])[-60:]
    path.write_text(json.dumps(led, indent=1))
    return led


def main(shard_dir):
    base = HERE / "data.json"
    merged = {}
    # start from what's already on the register
    for r in load(base, {"solicitations": []}).get("solicitations", []):
        k = key_of(r)
        merged[k] = _merge_pair(merged[k], r) if k in merged else r

    meta = load(base, {"meta": {}}).get("meta", {}) or {}
    ledger = set(meta.get("ledger", []))
    blocked, statuses = [], []
    seen_hosts = set()
    state = load(HERE / "state.json", {"root_idx": 0})

    root = pathlib.Path(shard_dir)
    shard_dirs = sorted([p for p in root.glob("shard-*") if p.is_dir()]) if root.exists() else []
    for sd in shard_dirs:
        d = load(sd / "data.json", {"meta": {}, "solicitations": []})
        for r in d.get("solicitations", []):
            k = key_of(r)
            if not k:
                continue
            pk = prev_key_of(r)
            if pk and pk != k and pk in merged:
                # the re-scan gave this record a firmer identity — retire the old row
                old = merged.pop(pk)
                for f in ("firstSeen", "deleted", "deletedOn", "hidden", "hiddenOn",
                          "switched", "notes"):
                    if r.get(f) in (None, "", False) and old.get(f) not in (None, "", False):
                        r[f] = old[f]
            merged[k] = _merge_pair(merged[k], r) if k in merged else r
        ledger |= set((d.get("meta") or {}).get("ledger", []))
        b = load(sd / "blocked.json", {"sites": []})
        for s in b.get("sites", []):
            if s.get("host") and s["host"] not in seen_hosts:
                seen_hosts.add(s["host"]); blocked.append(s)
        st = load(sd / "status.json", {})
        if st:
            statuses.append(st)
        sstate = load(sd / "state.json", {})
        if sstate.get("root_idx"):
            state["root_idx"] = sstate["root_idx"]

    rows = [normalize(clean_sol(r)) for r in merged.values() if not is_junk(clean_sol(r))]
    dropped = len(merged) - len(rows)
    rows = apply_operator(rows)

    # fleet-wide status roll-up for Mission Control
    agg = {"mode": (statuses[0].get("mode") if statuses else "roots"),
           "startedAt": min([s.get("startedAt", "") for s in statuses] or [""]) or "",
           "heartbeat": max([s.get("heartbeat", "") for s in statuses] or [""]) or "",
           "currentJob": "fleet run complete",
           "found": sum(s.get("found", 0) for s in statuses),
           "queued": sum(s.get("queued", 0) for s in statuses),
           "aiCalls": sum(s.get("aiCalls", 0) for s in statuses),
           "aiBudget": sum(s.get("aiBudget", 0) for s in statuses),
           "providers": sorted({p for s in statuses for p in (s.get("providers") or [])}),
           "coverage": {k: v for s in statuses for k, v in (s.get("coverage") or {}).items()},
           "bots": len(statuses), "running": False,
           "lastError": next((s.get("lastError") for s in statuses if s.get("lastError")), ""),
           "aiDiag": {"ok": {}, "errors": {}},
           "samDiag": next((x.get("samDiag") for x in statuses if x.get("samDiag") and "not queried" not in str(x.get("samDiag"))), "SAM not queried")}
    # Claude's health, model, token spend and — most importantly — any DOWN
    # reason must survive the fleet merge. The portal's alarm reads `down` from
    # here; if it were dropped the register could fail silently, which is the one
    # thing a single-provider setup must never do.
    tin = tout = 0
    by_model = {}
    for s in statuses:
        dg = s.get("aiDiag") or {}
        for k, v in (dg.get("ok") or {}).items():
            agg["aiDiag"]["ok"][k] = agg["aiDiag"]["ok"].get(k, 0) + v
        for k, v in (dg.get("errors") or {}).items():
            agg["aiDiag"]["errors"][k] = v
        for f in ("model", "keyVar"):
            if dg.get(f) and not agg["aiDiag"].get(f):
                agg["aiDiag"][f] = dg[f]
        if dg.get("down") and not agg["aiDiag"].get("down"):
            agg["aiDiag"]["down"] = dg["down"]          # any bot down = alarm
        tin += int(dg.get("inputTokens") or 0)
        tout += int(dg.get("outputTokens") or 0)
        for mname, mv in (dg.get("byModel") or {}).items():
            slot = by_model.setdefault(mname, {"in": 0, "out": 0, "calls": 0})
            slot["in"] += int(mv.get("in") or 0)
            slot["out"] += int(mv.get("out") or 0)
            slot["calls"] += int(mv.get("calls") or 0)
    if tin or tout:
        agg["aiDiag"]["inputTokens"] = tin
        agg["aiDiag"]["outputTokens"] = tout
        agg["aiDiag"]["byModel"] = by_model
        agg["aiDiag"]["spendUSD"] = round(_cost(by_model) if by_model
                                          else tin / 1e6 * 1.00 + tout / 1e6 * 5.00, 4)

    # ---- THE RUNNING TOTAL. Every run's usage is added to a ledger that
    # survives the run, so the portal can say what is LEFT of the deposit
    # rather than what this one crawl happened to cost. The token counts are
    # exact -- they come back from the API with every answer. The dollar figure
    # is those tokens priced at the rates in RATES below, which can be set
    # without touching the code if they ever change.
    _update_spend_ledger(by_model, tin, tout)

    counts = {"active": 0, "bid": 0, "mid": 0, "no": 0, "review": 0, "archived": 0,
              "verified": 0, "unverified": 0, "deleted": 0, "hidden": 0}
    for r in rows:
        if r.get("deleted"):
            counts["deleted"] += 1; continue
        if r.get("hidden"):
            counts["hidden"] += 1; continue
        if r.get("archived"):
            counts["archived"] += 1; continue
        counts["active"] += 1
        t = r.get("tier", "")
        counts["bid" if t == "BID" else "mid" if t == "MID" else "no" if t == "NO" else "review"] += 1
        counts["verified" if r.get("verified") == "VERIFIED" else "unverified"] += 1

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    meta.update({"lastLive": stamp, "counts": counts, "ledger": sorted(ledger)[-8000:]})
    if agg["mode"] == "roots":
        meta["lastDeep"] = stamp; meta["lastRoots"] = stamp

    (HERE / "data.json").write_text(json.dumps({"meta": meta, "solicitations": rows},
                                               indent=1, ensure_ascii=False))
    (HERE / "status.json").write_text(json.dumps(agg, indent=1, ensure_ascii=False))
    (HERE / "blocked.json").write_text(json.dumps({"sites": blocked, "updated": stamp},
                                                  indent=1, ensure_ascii=False))
    (HERE / "state.json").write_text(json.dumps(state, indent=1))
    print(f"merged {len(shard_dirs)} shards -> {len(rows)} records (dropped {dropped} non-solicitations) "
          f"(active {counts['active']}, archived {counts['archived']}, ledger {len(ledger)})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "shards")
