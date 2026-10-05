"""Settings: the app's defaults for the keys the browser code reads, overridable.

Override a value in ``<data dir>/settings.json`` or with an environment
variable ``ASCENDED_SETTING_<KEY>`` (upper case; the value is parsed as JSON
when it can be, e.g. ``ASCENDED_SETTING_BROWSER_WORKSPACE_OBSERVE_FORMAT=outline``).
"""
from __future__ import annotations

import json
import os
import threading
import time
from functools import lru_cache
from importlib import resources
from typing import Any

from .paths import data_dir

_CACHE_SECONDS = 2.0
# Where the standalone package differs from the app's defaults. The live view
# streams the browser to Ascended's own UI; there is none here.
_STANDALONE = {"browser_liveview_enabled": False}
_lock = threading.Lock()
_cached: tuple[float, dict] | None = None


@lru_cache(maxsize=1)
def _defaults() -> dict:
    text = resources.files("ascended_browser._app").joinpath("settings_defaults.json").read_text()
    return json.loads(text)


def _env_overrides() -> dict:
    out = {}
    for name, raw in os.environ.items():
        if name.startswith("ASCENDED_SETTING_"):
            key = name[len("ASCENDED_SETTING_"):].lower()
            try:
                out[key] = json.loads(raw)
            except ValueError:
                out[key] = raw
    return out


def load_settings() -> dict:
    global _cached
    now = time.monotonic()
    with _lock:
        if _cached and now - _cached[0] < _CACHE_SECONDS:
            return _cached[1]
    merged = {**_defaults(), **_STANDALONE}
    path = data_dir() / "settings.json"
    try:
        saved = json.loads(path.read_text())
        if isinstance(saved, dict):
            merged.update(saved)
    except (OSError, ValueError):
        pass
    merged.update(_env_overrides())
    with _lock:
        _cached = (now, merged)
    return merged


def get_setting(key: str, default: Any = None) -> Any:
    return load_settings().get(key, default)
