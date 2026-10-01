#!/usr/bin/env python3
"""
ai.py — multi-provider FREE AI rotation for the adjudicator.
-----------------------------------------------------------
One function `make_caller()` returns a `call(prompt) -> dict` that the analyzer
plugs into its `call_gemini` slot. It rotates across every free provider whose
API key is present in the environment, combining their free quotas:

    GEMINI_API_KEY      -> Google Gemini (Flash, free tier)
    GROQ_API_KEY        -> Groq (Llama 3.x, free tier)
    MISTRAL_API_KEY     -> Mistral (free tier)
    OPENROUTER_API_KEY  -> OpenRouter (free models)

Behaviour:
  * Tries providers in order; on 429 / quota / rate-limit it marks that
    provider "cooling" and falls through to the next one.
  * If EVERY provider is exhausted, raises AllExhausted so the crawler can
    pause and resume later (24/7 "sleep until quota refills").
  * Always returns a parsed dict. On a non-quota error it returns
    {"_gerr": "..."} so the analyzer downgrades that record to REVIEW
    instead of guessing.
  * No paid providers. Ever.

Pure-stdlib HTTP (urllib) so it runs anywhere with no extra deps.
"""
import os, json, time, re, urllib.request, urllib.error

TIMEOUT = 90
COOL_SECONDS = 60          # how long to rest a provider after a 429 before retrying it


class AllExhausted(Exception):
    """Every configured provider is rate-limited / out of quota right now."""


# ---------- JSON extraction (models love to wrap JSON in prose / fences) ----------
def _parse_json(text):
    if not text:
        return None
    t = text.strip()
    # strip ```json ... ``` fences
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # grab the outermost {...}
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return None
    return None


def _post(url, headers, payload):
    """POST JSON. Returns (status, body_text) even on HTTP errors (body captured safely)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body


def _is_quota(status, body):
    if status in (429, 402, 503):
        return True
    b = (body or "").lower()
    return any(k in b for k in ("rate limit", "quota", "resource_exhausted",
                                "exceeded", "too many requests", "insufficient"))


# ---------- provider adapters: each returns raw model text or raises ----------
# Gemini free models to try in order (first that works wins; handles deprecations/404).
GEMINI_MODELS = [m for m in [os.getenv("GEMINI_MODEL", "")] if m] + [
    "gemini-2.0-flash", "gemini-1.5-flash", "gemini-flash-latest", "gemini-1.5-flash-8b"]


def _gemini(key, prompt):
    last = ""
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        payload = {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048}}
        st, body = _post(url, {"Content-Type": "application/json"}, payload)
        if _is_quota(st, body):
            raise _Quota(f"gemini {st}")
        if st == 404:                       # model not served -> try the next one
            last = f"404 {model}"; continue
        if st != 200:
            raise RuntimeError(f"gemini HTTP {st}: {body[:120]}")
        d = json.loads(body)
        return d["candidates"][0]["content"]["parts"][0]["text"]
    raise RuntimeError(f"gemini: no model served ({last})")


def _openai_style(url, key, model, prompt):
    payload = {"model": model, "temperature": 0.1,
               "messages": [{"role": "user", "content": prompt}]}
    st, body = _post(url, {"Content-Type": "application/json",
                           "Authorization": f"Bearer {key}"}, payload)
    if _is_quota(st, body):
        raise _Quota(f"{model} {st}")
    if st != 200:
        raise RuntimeError(f"{model} HTTP {st}: {body[:120]}")
    d = json.loads(body)
    return d["choices"][0]["message"]["content"]


def _groq(key, prompt):
    return _openai_style("https://api.groq.com/openai/v1/chat/completions",
                         key, "llama-3.3-70b-versatile", prompt)


def _mistral(key, prompt):
    return _openai_style("https://api.mistral.ai/v1/chat/completions",
                         key, "mistral-small-latest", prompt)


def _openrouter(key, prompt):
    return _openai_style("https://openrouter.ai/api/v1/chat/completions",
                         key, "meta-llama/llama-3.3-70b-instruct:free", prompt)


class _Quota(Exception):
    pass


# ---------- the rotating caller ----------
class Rotator:
    def __init__(self):
        self.providers = []
        if os.getenv("GEMINI_API_KEY"):
            self.providers.append(["gemini", os.environ["GEMINI_API_KEY"], _gemini, 0])
        if os.getenv("GROQ_API_KEY"):
            self.providers.append(["groq", os.environ["GROQ_API_KEY"], _groq, 0])
        if os.getenv("MISTRAL_API_KEY"):
            self.providers.append(["mistral", os.environ["MISTRAL_API_KEY"], _mistral, 0])
        if os.getenv("OPENROUTER_API_KEY"):
            self.providers.append(["openrouter", os.environ["OPENROUTER_API_KEY"], _openrouter, 0])
        self.calls = 0

    def names(self):
        return [p[0] for p in self.providers]

    def call(self, prompt):
        """Return a parsed dict. Rotate on quota; raise AllExhausted if all cooling."""
        if not self.providers:
            return {"_gerr": "no AI provider keys configured"}
        now = time.time()
        last_err = ""
        tried = 0
        for p in self.providers:
            name, key, fn, cool_until = p
            if cool_until > now:
                continue                    # still resting after a 429
            tried += 1
            try:
                raw = fn(key, prompt)
                self.calls += 1
                parsed = _parse_json(raw)
                if parsed is None:
                    return {"_gerr": f"{name}: unparseable response"}
                parsed["_provider"] = name
                return parsed
            except _Quota as e:
                p[3] = now + COOL_SECONDS     # rest this provider
                last_err = str(e)
                continue
            except urllib.error.HTTPError as e:
                try:
                    b = e.read().decode("utf-8", "replace")
                except Exception:
                    b = ""
                if _is_quota(e.code, b):
                    p[3] = now + COOL_SECONDS
                    last_err = f"{name} {e.code}"
                    continue
                return {"_gerr": f"{name} HTTP {e.code}"}
            except Exception as e:
                return {"_gerr": f"{name}: {str(e)[:80]}"}
        # nothing succeeded
        if tried == 0:
            raise AllExhausted(last_err or "all providers cooling")
        return {"_gerr": f"all providers failed: {last_err}"}


def make_caller():
    """Factory: returns (rotator, call_fn). call_fn(prompt)->dict for analyzer."""
    r = Rotator()
    return r, r.call


if __name__ == "__main__":
    r, call = make_caller()
    print("configured providers:", r.names() or "(none — set API keys)")
    print("JSON-parse self-test:",
          _parse_json('```json\n{"tier":"BID","confidence":0.9}\n```'))
