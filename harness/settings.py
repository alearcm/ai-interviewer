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
    },
    "paths": {
        "sessions_dir": "sessions",
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
