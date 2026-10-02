#!/usr/bin/env python3
"""
merge_shards.py — combine the parallel bots' findings into ONE register.

Each sharded worker owns a different slice of the sources, so their results are
disjoint by construction. This merges them and still de-duplicates defensively
(by solicitation number, then by link) so a solicitation can never appear twice
even if two bots happened to see it.
"""
import sys, re, json, pathlib, datetime

HERE = pathlib.Path(__file__).parent


def today():
    return datetime.date.today().isoformat()


def load(p, d):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return d


def key_of(r):
    """Normalised identity: '#19CA1026Q0002', '19CA1026Q0002' and 'RFQ 19CA1026Q0002'
    are the SAME solicitation and must never become two rows."""
    sol = (r.get("sol") or "").upper()
    sol = re.sub(r"^(RFQ|RFP|ITB|IFB|SOL|NO\.?|#)[\s:#-]*", "", sol.strip())
    sol = re.sub(r"[^A-Z0-9]", "", sol)
    return sol or (r.get("link") or "")


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
    if len(sol) > 30 or (" " in sol and not _SOL_OK.match(sol.replace(" ", ""))):
        r["sol"] = ""
    elif not _SOL_OK.match(sol.replace(" ", "")):
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
    for s in statuses:
        dg = s.get("aiDiag") or {}
        for k, v in (dg.get("ok") or {}).items():
            agg["aiDiag"]["ok"][k] = agg["aiDiag"]["ok"].get(k, 0) + v
        for k, v in (dg.get("errors") or {}).items():
            agg["aiDiag"]["errors"][k] = v

    counts = {"active": 0, "bid": 0, "mid": 0, "no": 0, "review": 0, "archived": 0,
              "verified": 0, "unverified": 0}
    for r in rows:
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
