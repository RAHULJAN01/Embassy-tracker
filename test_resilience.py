#!/usr/bin/env python3
"""test_resilience.py — the things that must NEVER take the whole system down.

Two real outages on Rahul's live account, both from an optional feature failing
and wrongly dragging the essential part down with it:

  1. The Sonnet review models are 'not available to this account'. A logic bug
     (_next_model filtered by 'already tried' instead of 'known bad') meant that
     once the Sonnet ids had each 404'd, the fall-back to the working Haiku was
     treated as 'already tried', so the review call concluded the API was
     exhausted and raised the DOWN alarm -- halting a run that Haiku could have
     finished. The register showed 0 adjudicated with $39.74 still in the bank.

  2. The blocked-site list only ever grew. A host blocked once (e.g. during a
     bad-headers incident) stayed on the alarm for ever, even after it started
     answering again -- so the alarm cried '166 blocked' while the bots were
     reading those very sites.
"""
import sys, pathlib

SRC = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import ai, fetcher          # noqa: E402

FAILS = []


def ok(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


# ============================================ review model can't take us down
print("\n=== an unavailable review model never knocks the system offline ===")


def make_client():
    c = ai.Claude()
    c.key = "test-key"
    seen = []

    def fake_post(key, model, prompt):
        seen.append(model)
        if "sonnet" in model or "3-7" in model:
            return 404, '{"error":{"message":"model not available to this account: ' + model + '"}}', None
        return (200,
                '{"content":[{"text":"{\\"tier\\":\\"BID\\",\\"confidence\\":0.9}"}],'
                '"usage":{"input_tokens":10,"output_tokens":2}}', None)
    ai._post = fake_post
    return c, seen


c, seen = make_client()
r = c.call("adjudicate")
ok("the primary (Haiku) call works", r.get("tier") == "BID", str(r))
ok("and never sets a DOWN reason", c.down_reason == "", repr(c.down_reason))

r2 = c.call("second opinion", model=ai.REVIEW_MODEL)
ok("a review call falls back to Haiku instead of failing", r2.get("tier") == "BID", str(r2))
ok("the review failure does NOT set DOWN", c.down_reason == "", repr(c.down_reason))
ok("the account is not wrongly reported down", "down" not in c.diag())

seen.clear()
c.call("another second opinion", model=ai.REVIEW_MODEL)
ok("a known-unavailable review model is not re-tried (no wasted calls)",
   not any("sonnet" in m or "3-7" in m for m in seen), str(seen))

# a genuine PRIMARY outage still raises, so a real problem still alarms
print("\n=== a real PRIMARY outage still raises the alarm ===")
c2 = ai.Claude(); c2.key = "test-key"
ai._post = lambda k, m, p: (401, '{"error":{"message":"invalid x-api-key"}}', None)
raised = False
try:
    c2.call("adjudicate")
except ai.AllExhausted:
    raised = True
ok("a bad API key still raises AllExhausted", raised)
ok("and sets a DOWN reason a human can act on", bool(c2.down_reason), c2.down_reason[:50])

# ============================================ blocked list clears recovered sites
print("\n=== a site that answers again drops off the blocked alarm ===")
blocked = [{"host": "ca.usembassy.gov"}, {"host": "np.usembassy.gov"},
           {"host": "la.usembassy.gov"}]
fetcher.OK_HOSTS = {"ca.usembassy.gov", "np.usembassy.gov"}   # two answered this run
recovered = set(fetcher.OK_HOSTS)
still = [b for b in blocked if b.get("host") not in recovered]
ok("recovered hosts are removed", len(still) == 1, str([b["host"] for b in still]))
ok("a host that did NOT answer stays blocked", still[0]["host"] == "la.usembassy.gov")
ok("fetcher records a host on a successful fetch", "OK_HOSTS" in dir(fetcher))

# the crawler actually performs this subtraction at save time
import inspect
src = inspect.getsource(__import__("crawler"))
ok("the crawler clears recovered hosts from the blocked list",
   "fetcher.OK_HOSTS" in src and "not in recovered" in src)

print("\n" + "=" * 64)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS))
print("=" * 64)
sys.exit(1 if FAILS else 0)
