"""Thin provider layer for document extraction with a vision-capable LLM.

Supports Google Gemini, Anthropic Claude and OpenAI. Choose with LLM_PROVIDER
(gemini | anthropic | openai | none). If unset, the first provider with an API key is used.
"""
import base64
import json
import os
import re

import httpx

TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))

MODELS = {
    "gemini": os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
    "anthropic": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"),
    "openai": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
}
# Backup models: used if the main model is retired (404) or stays overloaded (429/5xx) after retries.
FALLBACK_MODELS = {"gemini": ["gemini-3.5-flash", "gemini-flash-latest"], "anthropic": ["claude-haiku-4-5"],
                   "openai": ["gpt-4.1-mini"]}
RETRY_STATUS = {429, 500, 502, 503, 504}
KEYS = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
ATTEMPTS = int(os.environ.get("LLM_ATTEMPTS", "3"))


def provider() -> str | None:
    p = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if p == "none":
        return None
    if p in KEYS:
        return p if os.environ.get(KEYS[p]) else None
    for name, key in KEYS.items():
        if os.environ.get(key):
            return name
    return None


def describe() -> dict:
    p = provider()
    return {"provider": p, "model": (LAST_MODEL.get(p) or MODELS.get(p)) if p else None, "enabled": bool(p)}


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def extract(file_bytes: bytes, mime: str, prompt: str) -> dict:
    """Call the provider with retries (busy / rate-limited / network errors) and backup models."""
    import time
    p = provider()
    if not p:
        raise RuntimeError("no LLM provider configured")
    models = [MODELS[p]] + [m for m in FALLBACK_MODELS.get(p, []) if m != MODELS[p]]
    last = "unknown error"
    for mi, model in enumerate(models):
        tries = ATTEMPTS if mi == 0 else 2
        for attempt in range(tries):
            try:
                return _call(p, model, file_bytes, mime, prompt)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                last = f"{model}: HTTP {code} {e.response.text[:120]}"
                if code == 404:
                    break                                   # model id not available -> next model
                if code in RETRY_STATUS:
                    time.sleep(1.5 * (attempt + 1))         # busy / rate-limited -> back off and retry
                    continue
                raise RuntimeError(f"{p} API error {code}: {e.response.text[:160]}") from e
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = f"{model}: {type(e).__name__}"
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{p} unavailable after retries and backup models ({last})")


LAST_MODEL = {}


def _call(p, model, file_bytes, mime, prompt) -> dict:
    LAST_MODEL[p] = model
    b64 = base64.b64encode(file_bytes).decode()
    key = os.environ[KEYS[p]]

    if p == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {"contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": b64}}, {"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"}}
        r = httpx.post(url, params={"key": key}, json=body, timeout=TIMEOUT)
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return _parse_json(text)

    if p == "anthropic":
        block = ({"type": "document", "source": {"type": "base64", "media_type": mime, "data": b64}}
                 if mime == "application/pdf" else
                 {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
        body = {"model": model, "max_tokens": 4000,
                "messages": [{"role": "user", "content": [block, {"type": "text", "text": prompt}]}]}
        r = httpx.post("https://api.anthropic.com/v1/messages", json=body, timeout=TIMEOUT,
                       headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
        return _parse_json(text)

    if p == "openai":
        part = ({"type": "file", "file": {"filename": "invoice.pdf", "file_data": f"data:{mime};base64,{b64}"}}
                if mime == "application/pdf" else
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        body = {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": [part, {"type": "text", "text": prompt}]}]}
        r = httpx.post("https://api.openai.com/v1/chat/completions", json=body, timeout=TIMEOUT,
                       headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        return _parse_json(r.json()["choices"][0]["message"]["content"])

    raise RuntimeError(f"unknown provider {p}")
