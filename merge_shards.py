#!/usr/bin/env python3
"""
merge_shards.py — combine the parallel bots' findings into ONE register.

Each sharded worker owns a different slice of the sources, so their results are
disjoint by construction. This merges them and still de-duplicates defensively
(by solicitation number, then by link) so a solicitation can never appear twice
even if two bots happened to see it.
"""
import sys, json, pathlib, datetime

HERE = pathlib.Path(__file__).parent


def today():
    return datetime.date.today().isoformat()


def load(p, d):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return d


def key_of(r):
    return (r.get("sol") or "").strip().upper() or (r.get("link") or "")


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
        merged[key_of(r)] = r

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
            merged[k] = better(merged[k], r) if k in merged else r
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

    rows = [normalize(r) for r in merged.values()]

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
           "aiDiag": {"ok": {}, "errors": {}}}
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
    print(f"merged {len(shard_dirs)} shards -> {len(rows)} records "
          f"(active {counts['active']}, archived {counts['archived']}, ledger {len(ledger)})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "shards")
