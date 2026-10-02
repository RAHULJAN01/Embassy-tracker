#!/usr/bin/env python3
"""
ai.py — ONE reliable AI. Claude, and nothing else.
==================================================
Rahul's call, and it's the right one:

    "I only want ONE reliable AI in my portal. Remove all the earlier free
     APIs and junk bots. And I want an alarm if Claude is down."

So this file has exactly one provider. The eleven free ones are gone — they
gave inconsistent judgments (a solicitation adjudicated by a different model
each run is not one standard), they churned their model lineups constantly,
and they starved the register of calls.

The trade is honest: with no fallback, the register stops if Claude stops. So
this module's other job is to say LOUDLY and SPECIFICALLY why it stopped, so
the portal can raise the alarm instead of going quiet.

    make_caller() -> (client, call)
    call(prompt)  -> parsed dict
                  -> {"_gerr": "..."} on a bad reply (record becomes REVIEW)
                  -> raises AllExhausted when Claude is genuinely unreachable

Pure stdlib HTTP so it runs anywhere with no extra dependencies.
"""
import os, json, time, re, urllib.request, urllib.error

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
TIMEOUT = int(os.getenv("AI_TIMEOUT", "120"))
MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "2000"))
MIN_INTERVAL = float(os.getenv("AI_PACE", "1.0"))   # keep bursts off the per-minute limit
MAX_RETRIES = int(os.getenv("AI_RETRIES", "4"))

# Haiku 4.5 is the working model. The dated id is the stable one; the aliases are
# tried only if the account cannot see it, so a model rename can't take us down.
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
MODEL_FALLBACKS = ["claude-haiku-4-5", "claude-3-5-haiku-latest"]

# The key. ANTHROPIC_API_KEY is the name we ask for; the others are accepted so a
# differently-named secret doesn't look like an outage. Which one was found is
# reported in the diagnostics.
KEY_NAMES = ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_KEY", "API_KEY_ANTHROPIC"]


class AllExhausted(Exception):
    """Claude is unreachable: no credit, bad key, or an outage. Raise the alarm."""


def find_key():
    """Returns (key, which_env_var). Empty key means nothing is configured."""
    for n in KEY_NAMES:
        v = os.getenv(n, "").strip()
        if v:
            return v, n
    return "", ""


# ---------- JSON extraction (models sometimes wrap JSON in prose / fences) ----------
def _parse_json(text):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return None
    return None


def _post(key, model, prompt):
    """One call. Returns (status, text, headers_dict). Never raises on HTTP error."""
    body = json.dumps({
        "model": model,
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "system": ("You are a precise government-procurement analyst. You answer ONLY "
                   "with a single valid JSON object and nothing else — no prose, no "
                   "code fences, no explanation outside the JSON."),
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": "mm-solicitation-register/2.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            txt = e.read().decode("utf-8", "replace")
        except Exception:
            txt = ""
        return e.code, txt, dict(getattr(e, "headers", {}) or {})
    except Exception as e:
        return 0, f"__network__ {type(e).__name__}: {str(e)[:160]}", {}


def _why(status, text):
    """Turn an API failure into something a human can act on."""
    msg = ""
    try:
        msg = (json.loads(text).get("error") or {}).get("message", "")
    except Exception:
        msg = str(text)[:200]
    low = (msg or "").lower()
    if status == 401 or "authentication" in low or "invalid x-api-key" in low:
        return ("the API key was rejected — check the ANTHROPIC_API_KEY secret is the "
                "full key and has not been revoked", True)
    if status == 403:
        return (f"Claude refused the request (403): {msg[:120]}", True)
    if "credit balance is too low" in low or "insufficient" in low or status == 402:
        return ("the Claude account is out of credit — top it up in the Console "
                "(Settings - Plans & Billing)", True)
    if status == 429:
        return (f"rate limited by Claude: {msg[:120]}", False)
    if status in (500, 502, 503, 529):
        return (f"Claude is having an outage (HTTP {status})", False)
    if status == 0:
        return (f"could not reach Claude: {text[12:160]}", False)
    if status == 404:
        return (f"model not available to this account: {msg[:120]}", True)
    return (f"HTTP {status}: {msg[:140]}", False)


class Claude:
    """A single, reliable provider. No rotation, no fallbacks, no guessing."""

    def __init__(self):
        self.key, self.key_var = find_key()
        self.model = MODEL
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.ok = {}
        self.errors = {}
        self.down_reason = ""          # non-empty => the portal raises the alarm
        self._last = 0.0
        self._model_checked = False
        # kept so existing callers (crawler.probe) keep working
        self.providers = [["claude", self.key, None, 0]] if self.key else []

    # ---- diagnostics the portal shows ----
    def names(self):
        return ["claude"] if self.key else []

    def diag(self):
        d = {"ok": dict(self.ok), "errors": dict(self.errors),
             "model": self.model, "keyVar": self.key_var or "(none found)",
             "calls": self.calls,
             "inputTokens": self.input_tokens, "outputTokens": self.output_tokens}
        if self.down_reason:
            d["down"] = self.down_reason
        if self.input_tokens or self.output_tokens:
            d["spendUSD"] = round(self.input_tokens / 1e6 * 1.00
                                  + self.output_tokens / 1e6 * 5.00, 4)
        return d

    def _pace(self):
        gap = time.time() - self._last
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)

    def _resolve_model(self):
        """If the configured model isn't visible to this account, try the aliases
        ONCE rather than failing every call for the rest of the run."""
        if self._model_checked:
            return
        self._model_checked = True
        for m in [self.model] + [x for x in MODEL_FALLBACKS if x != self.model]:
            status, text, _ = _post(self.key, m, 'Reply with only: {"ok":true}')
            if status == 200:
                self.model = m
                return
            if status != 404:
                return                      # a real error, not a missing model
        self.errors["claude"] = "no Haiku model id was accepted by this account"

    def call(self, prompt):
        if not self.key:
            self.down_reason = ("no Claude API key is configured — add the repository "
                                "secret ANTHROPIC_API_KEY")
            raise AllExhausted(self.down_reason)
        self._resolve_model()

        wait = 3.0
        last = ""
        for attempt in range(MAX_RETRIES):
            self._pace()
            status, text, _ = _post(self.key, self.model, prompt)
            self._last = time.time()

            if status == 200:
                try:
                    data = json.loads(text)
                except Exception:
                    return {"_gerr": "Claude returned a malformed response"}
                u = data.get("usage") or {}
                self.input_tokens += int(u.get("input_tokens") or 0)
                self.output_tokens += int(u.get("output_tokens") or 0)
                self.calls += 1
                self.ok["claude"] = self.ok.get("claude", 0) + 1
                self.down_reason = ""
                parts = data.get("content") or []
                raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                parsed = _parse_json(raw)
                if parsed is None:
                    # a real answer we couldn't read — the record becomes REVIEW,
                    # which is honest, rather than a guess
                    return {"_gerr": f"reply was not JSON: {raw[:120]}"}
                return parsed

            why, fatal = _why(status, text)
            last = why
            self.errors["claude"] = why
            if fatal:
                self.down_reason = why
                raise AllExhausted(why)
            time.sleep(wait)
            wait = min(wait * 2, 30)

        # retries used up on a transient fault — treat as down so the alarm fires
        self.down_reason = f"{last} (gave up after {MAX_RETRIES} attempts)"
        raise AllExhausted(self.down_reason)


def make_caller():
    """Factory: returns (client, call_fn). call_fn(prompt) -> dict."""
    c = Claude()
    return c, c.call


# back-compat for anything still importing the old name
Rotator = Claude


if __name__ == "__main__":
    c, call = make_caller()
    print("key found in:", c.key_var or "(nothing — set ANTHROPIC_API_KEY)")
    print("model:", c.model)
    print("JSON-parse self-test:",
          _parse_json('```json\n{"tier":"BID","confidence":0.9}\n```'))
