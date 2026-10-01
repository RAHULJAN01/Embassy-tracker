#!/usr/bin/env python3
"""
estimator.py — the valuation specialist.
========================================
This bot does NOT run on everything. It is deliberately expensive and is only
called once the other bots have finished their job on a solicitation:

    * the record is VERIFIED (all docs read, dates found, fields complete), and
    * it is actually doable for us (tier BID or MID),
    * and the notice does not already state a firm value.

Then it estimates what the contract is worth, from:
    1. ARITHMETIC first — line items x quantities x a defensible unit price.
       If the notice gives quantities, maths beats guessing.
    2. MARKET PATTERN — typical award values for that commodity/service class,
       in that country, at that kind of post, over past years.
    3. CURRENCY + LOCAL COST REALITY — local price level, import duty, freight
       to that destination, and the currency the award will actually be paid in.

It must return a RANGE, the BASIS it reasoned from, and a confidence. A number
without a basis is worthless for bidding, so a missing basis is rejected.
"""
import re, json

ESTIMATE_PROMPT = (
    "You are a government-contract pricing analyst. Estimate what ONE solicitation is "
    "likely to be worth, for a bidder deciding whether it is worth pursuing.\n\n"
    "Work in this order:\n"
    "1. ARITHMETIC. If the notice states line items and quantities, price them. Show the maths "
    "in the basis (e.g. '120 task chairs x ~$180 landed = ~$21,600'). Arithmetic beats intuition.\n"
    "2. MARKET PATTERN. Compare with what this class of requirement typically awards for at a "
    "mission/agency of this size in THIS country, drawing on how such contracts have been priced "
    "over past years. Say what you are comparing to.\n"
    "3. LOCAL REALITY. Account for the destination country's price level, import duty, inland "
    "freight, and the currency the award is paid in.\n\n"
    "Rules:\n"
    "* Give a RANGE (low / likely / high) in USD. Never a single false-precision number.\n"
    "* If the notice states quantities, your estimate MUST be consistent with them.\n"
    "* If there is genuinely too little information to estimate, say so: set confidence <= 0.3 "
    "and explain what is missing. Do NOT invent a number to look useful.\n"
    "* Keep the basis short and concrete — the reasoning, not padding.\n\n"
    "Return ONLY JSON:\n"
    '{"low_usd": <number>, "likely_usd": <number>, "high_usd": <number>,\n'
    ' "currency_note": "<currency the award is actually paid in, and any FX caveat>",\n'
    ' "basis": "<the maths and the comparison you used>",\n'
    ' "drivers": ["<cost driver>", "..."],\n'
    ' "confidence": <0.0-1.0>}\n\n'
    "SOLICITATION\n"
    "Title: {title}\nCountry / post: {country} / {post}\nSector: {sector}\n"
    "Classification: {classification}\nScope: {scope}\nStated value (if any): {stated}\n"
    "Shipping terms: {shipping}\n\n"
    "FULL TEXT:\n{body}"
)


def _num(x):
    try:
        v = float(x)
        return v if v >= 0 else 0.0
    except Exception:
        return 0.0


def _usd(n):
    n = float(n)
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n/1000:.0f}K"
    return f"${n:,.0f}"


def should_estimate(row):
    """The gate: only verified, doable records with no firm stated value."""
    if row.get("verified") != "VERIFIED":
        return False
    if row.get("tier") not in ("BID", "MID"):
        return False
    if row.get("archived"):
        return False
    stated = (row.get("value") or "").strip()
    # a stated value that actually contains digits is good enough — don't spend a call
    if stated and re.search(r"\d", stated):
        return False
    return True


def estimate(row, body, call_ai, min_conf=0.35):
    """Return an estimate dict, or None if it can't be made responsibly."""
    prompt = (ESTIMATE_PROMPT
              .replace("{title}", str(row.get("title", ""))[:160])
              .replace("{country}", str(row.get("country", ""))[:60])
              .replace("{post}", str(row.get("post", ""))[:80])
              .replace("{sector}", str(row.get("sector", ""))[:30])
              .replace("{classification}", str(row.get("type", ""))[:80])
              .replace("{scope}", str(row.get("scope", ""))[:200])
              .replace("{stated}", str(row.get("value", "") or "not stated")[:60])
              .replace("{shipping}", str(row.get("shipping", ""))[:80])
              .replace("{body}", re.sub(r"\s+", " ", body or "")[:9000]))
    try:
        d = call_ai(prompt) or {}
    except Exception as e:
        if type(e).__name__ == "AllExhausted":
            raise
        return None
    if not isinstance(d, dict) or d.get("_gerr"):
        return None

    low, likely, high = _num(d.get("low_usd")), _num(d.get("likely_usd")), _num(d.get("high_usd"))
    basis = str(d.get("basis", "")).strip()
    conf = max(0.0, min(1.0, _num(d.get("confidence"))))

    # a number with no reasoning behind it is not usable for bidding
    if not basis or len(basis) < 25:
        return None
    if likely <= 0 and low <= 0 and high <= 0:
        return None
    if conf < min_conf:
        return None
    # keep the range coherent
    vals = sorted(v for v in (low, likely, high) if v > 0)
    if not vals:
        return None
    low, high = vals[0], vals[-1]
    likely = likely if low <= likely <= high and likely > 0 else vals[len(vals) // 2]

    return {
        "low": low, "likely": likely, "high": high,
        "display": f"{_usd(low)} – {_usd(high)}" if high > low else _usd(likely),
        "basis": basis[:700],
        "currencyNote": str(d.get("currency_note", ""))[:160],
        "drivers": [str(x)[:70] for x in (d.get("drivers") or [])][:6],
        "confidence": round(conf, 2),
        "estimated": True,
    }


if __name__ == "__main__":
    print("estimator — runs only on VERIFIED + BID/MID records with no stated value")
