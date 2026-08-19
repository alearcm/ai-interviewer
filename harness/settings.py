"""Engine settings: model endpoint, run limits, output paths.

Read from config.toml at the repository root (or a path given with
--config). Every key has a default, and the default model endpoint is
a local OpenAI-compatible server — no key is ever required to run.
"""

from __future__ import annotations

import copy
import os
import tomllib
from typing import Any, Dict, Optional

DEFAULTS: Dict[str, Any] = {
    "model": {
        "provider": "openai-compat",  # openai-compat | anthropic | canned
        "base_url": "http://127.0.0.1:11434/v1",
        "name": "qwen3:4b-instruct-2507-q4_K_M",
        "api_key_env": "",
        "temperature": 0.4,
        "max_tokens": 160,
        "timeout_s": 60.0,
    },
    "run": {
        "timeout_s": 30.0,
        "output_max_chars": 4000,
        # "local" executes directly; "container" wraps every run in
        # docker (no network, bounded cpu/memory) — REQUIRED before
        # anyone but you can reach the web pane.
        "backend": "local",
        "container_image": "",
        "container_cpus": "1.0",
        "container_memory": "512m",
    },
    "paths": {
        "sessions_dir": "sessions",
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8765,
        "ui_dir": "",       # "" = the webui directory next to this package
        "packs_dir": "packs",
        "max_live": 8,      # concurrent live sessions the manager allows
        # If the named env var (or `password`) is set, the pane requires
        # a one-time login per device: a long-lived cookie remembers it.
        # Empty = no auth (localhost/tailnet use).
        "password_env": "HARNESS_PASSWORD",
        "password": "",
    },
    # The deep-review model for `analyze` — deliberately separate from
    # [model]: the live interviewer stays small/local, the post-session
    # read wants the strongest model you have a key for.
    "analyze": {
        "provider": "anthropic",
        "base_url": "",
        "name": "claude-sonnet-5",
        "api_key_env": "",  # anthropic default: ANTHROPIC_API_KEY
        "temperature": 0.3,
        "max_tokens": 4000,
        "timeout_s": 240.0,
    },
}


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(path: Optional[str] = None) -> Dict[str, Any]:
    settings = copy.deepcopy(DEFAULTS)
    if path is None and os.path.isfile("config.toml"):
        path = "config.toml"
    if path:
        with open(path, "rb") as fh:
            settings = _merge(settings, tomllib.load(fh))
    return settings
