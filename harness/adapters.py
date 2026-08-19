"""One model interface, three back-ends.

`openai-compat` speaks the /chat/completions dialect served by Ollama,
LM Studio, llama.cpp and vLLM — pick the endpoint with base_url and a
model name in config. A bearer token is attached only when the
configured environment variable is actually set, so a purely local
endpoint needs no key of any sort. `anthropic` is a thin alternative
speaking /v1/messages. `canned` needs no network at all: it cycles the
pack's fallback lines, which keeps tests and dry runs fully offline.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class AdapterError(RuntimeError):
    pass


Message = Dict[str, str]


class Adapter:
    name = "base"

    def reply(self, system: str, messages: List[Message]) -> str:
        raise NotImplementedError


def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout_s: float) -> Dict[str, Any]:
    body = bytes(json.dumps(payload), "utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    # the default urllib signature trips WAF/bot filters (Cloudflare
    # "error 1010") in front of some hosted endpoints
    request.add_header("User-Agent", "interview-harness/0.2")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(exc.read()[:400], "utf-8", "replace")
        except Exception:
            pass
        raise AdapterError("endpoint refused the request (%s): %s" % (exc, detail)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise AdapterError("endpoint unreachable at %s: %s" % (url, exc)) from None
    try:
        return json.loads(str(raw, "utf-8"))
    except ValueError as exc:
        raise AdapterError("endpoint returned non-JSON: %s" % exc) from None


class OpenAICompat(Adapter):
    name = "openai-compat"

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg

    def reply(self, system: str, messages: List[Message]) -> str:
        cfg = self.cfg
        url = str(cfg["base_url"]).rstrip("/") + "/chat/completions"
        payload = {
            "model": cfg["name"],
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": cfg.get("temperature", 0.4),
            "max_tokens": cfg.get("max_tokens", 160),
            "stream": False,
        }
        headers: Dict[str, str] = {}
        key_env = str(cfg.get("api_key_env") or "")
        key = os.environ.get(key_env, "") if key_env else ""
        if key:
            headers["Authorization"] = "Bearer " + key
        data = _post_json(url, payload, headers, float(cfg.get("timeout_s", 60.0)))
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AdapterError("unexpected reply shape: %r" % (data,)) from None
        return text or ""


class Anthropic(Adapter):
    name = "anthropic"

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg

    def reply(self, system: str, messages: List[Message]) -> str:
        cfg = self.cfg
        key_env = str(cfg.get("api_key_env") or "ANTHROPIC_API_KEY")
        key = os.environ.get(key_env, "")
        if not key:
            raise AdapterError(
                "the anthropic provider needs a key in $%s; the default "
                "openai-compat provider needs none" % key_env
            )
        base = str(cfg.get("base_url") or "https://api.anthropic.com").rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        payload = {
            "model": cfg["name"],
            "system": system,
            "messages": messages,
            "temperature": cfg.get("temperature", 0.4),
            "max_tokens": cfg.get("max_tokens", 160),
        }
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        data = _post_json(base + "/v1/messages", payload, headers, float(cfg.get("timeout_s", 60.0)))
        try:
            parts = [p.get("text", "") for p in data["content"] if p.get("type") == "text"]
        except (KeyError, TypeError):
            raise AdapterError("unexpected reply shape: %r" % (data,)) from None
        return "".join(parts)


class Canned(Adapter):
    """Offline stand-in: cycles a fixed list of lines. Used by tests
    and by --provider canned dry runs."""

    name = "canned"

    def __init__(self, lines: Optional[List[str]] = None) -> None:
        self.lines = list(lines or []) or ["Okay."]
        self._i = 0

    def reply(self, system: str, messages: List[Message]) -> str:
        line = self.lines[self._i % len(self.lines)]
        self._i += 1
        return line


def make_adapter(model_cfg: Dict[str, Any], canned_lines: Optional[List[str]] = None) -> Adapter:
    provider = str(model_cfg.get("provider", "openai-compat"))
    if provider == "openai-compat":
        return OpenAICompat(model_cfg)
    if provider == "anthropic":
        return Anthropic(model_cfg)
    if provider == "canned":
        return Canned(canned_lines)
    raise AdapterError("unknown provider %r" % provider)
