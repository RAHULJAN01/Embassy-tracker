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
COOL_SECONDS = 50          # how long to rest a provider after a 429 before retrying it
MIN_INTERVAL = float(os.getenv("AI_PACE", "4.5"))   # min seconds between calls (respect free RPM limits)
MAX_COOL_WAIT = 55         # if ALL providers are cooling, wait up to this long then retry


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


def _get(url, headers):
    """GET JSON (for model discovery). Returns (status, body_text)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""


_MODEL_CACHE = {}


def _discover(provider, url, key, prefer, want_free=False):
    """Query a provider's /models endpoint and pick a usable chat model.
    `prefer` is an ordered list of substrings to prioritise. Cached per process."""
    if provider in _MODEL_CACHE:
        return _MODEL_CACHE[provider]
    st, body = _get(url, {"Authorization": f"Bearer {key}", "User-Agent": _UA})
    ids = []
    if st == 200:
        try:
            for m in json.loads(body).get("data", []):
                mid = m.get("id", "")
                if want_free and not mid.endswith(":free"):
                    continue
                low = mid.lower()
                if any(x in low for x in ("whisper", "tts", "embed", "guard", "vision", "image")):
                    continue
                ids.append(mid)
        except Exception:
            pass
    # order by preference
    ranked = [m for p in prefer for m in ids if p in m.lower()] + ids
    seen, out = set(), []
    for m in ranked:
        if m not in seen:
            seen.add(m); out.append(m)
    _MODEL_CACHE[provider] = out
    return out


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


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _openai_style(url, key, models, prompt, extra_headers=None):
    """OpenAI-compatible chat call. `models` is a list tried in order (404 -> next).
    Sends a browser User-Agent so Cloudflare (Groq cf-1010) doesn't block the call."""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}",
               "User-Agent": _UA}
    if extra_headers:
        headers.update(extra_headers)
    last = ""
    for model in models:
        payload = {"model": model, "temperature": 0.1,
                   "messages": [{"role": "user", "content": prompt}]}
        st, body = _post(url, headers, payload)
        if _is_quota(st, body):
            raise _Quota(f"{model} {st}")
        if st == 404:
            last = f"404 {model}"; continue
        if st != 200:
            raise RuntimeError(f"{model} HTTP {st}: {body[:120]}")
        d = json.loads(body)
        return d["choices"][0]["message"]["content"]
    raise RuntimeError(f"no model served ({last})")


def _groq(key, prompt):
    models = _discover("groq", "https://api.groq.com/openai/v1/models", key,
                       prefer=["llama-3.3-70b", "llama-3.1-8b-instant", "llama-3", "mixtral"]) \
        or ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]
    return _openai_style("https://api.groq.com/openai/v1/chat/completions", key, models[:5], prompt)


def _mistral(key, prompt):
    models = _discover("mistral", "https://api.mistral.ai/v1/models", key,
                       prefer=["mistral-small", "open-mistral", "ministral", "mistral"]) \
        or ["mistral-small-latest", "open-mistral-7b"]
    return _openai_style("https://api.mistral.ai/v1/chat/completions", key, models[:5], prompt)


def _openrouter(key, prompt):
    models = _discover("openrouter", "https://openrouter.ai/api/v1/models", key,
                       prefer=["llama-3.3-70b", "llama-3.1", "deepseek", "qwen", "gemma"],
                       want_free=True) \
        or ["meta-llama/llama-3.1-8b-instruct:free"]
    return _openai_style("https://openrouter.ai/api/v1/chat/completions", key, models[:6], prompt,
                         extra_headers={"HTTP-Referer": "https://rahuljan01.github.io/Embassy-tracker/",
                                        "X-Title": "Madison Main Solicitation Register"})


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
        self._last_call = 0.0
        self.errors = {}          # provider -> last error seen (for diagnostics)
        self.ok = {}              # provider -> successful call count

    def names(self):
        return [p[0] for p in self.providers]

    def diag(self):
        return {"ok": dict(self.ok), "errors": dict(self.errors)}

    def _pace(self):
        gap = time.time() - self._last_call
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)

    def _attempt(self, prompt):
        """One sweep over providers that aren't cooling. Returns (dict|None, err)."""
        now = time.time()
        last_err = ""
        tried = 0
        for p in self.providers:
            name, key, fn, cool_until = p
            if cool_until > now:
                continue
            tried += 1
            self._pace()
            try:
                raw = fn(key, prompt)
                self._last_call = time.time()
                self.calls += 1
                parsed = _parse_json(raw)
                if parsed is None:
                    last_err = f"{name}: unparseable response"
                    self.errors[name] = last_err
                    continue                      # try another provider rather than give up
                parsed["_provider"] = name
                self.ok[name] = self.ok.get(name, 0) + 1
                return parsed, ""
            except _Quota as e:
                p[3] = time.time() + COOL_SECONDS
                last_err = str(e)
                self.errors[name] = last_err
                continue
            except Exception as e:
                last_err = f"{name}: {str(e)[:120]}"
                self.errors[name] = last_err
                continue
        return None, (last_err, tried)

    def call(self, prompt):
        """Return a parsed dict. Rotate on quota; if everything is cooling, WAIT up to
        MAX_COOL_WAIT then retry; only raise AllExhausted if still nothing after that."""
        if not self.providers:
            return {"_gerr": "no AI provider keys configured"}
        parsed, info = self._attempt(prompt)
        if parsed is not None:
            return parsed
        last_err, tried = info
        if tried == 0:
            # every provider is cooling — wait for the soonest to free up, then retry once
            now = time.time()
            wait = min(MAX_COOL_WAIT, max(1, int(min(p[3] for p in self.providers) - now) + 1))
            time.sleep(wait)
            parsed, info = self._attempt(prompt)
            if parsed is not None:
                return parsed
            if info[1] == 0:
                raise AllExhausted(last_err or "all providers cooling")
            last_err = info[0]
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
