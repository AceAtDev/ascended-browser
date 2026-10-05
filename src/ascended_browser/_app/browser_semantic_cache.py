"""Best-effort semantic replay cache for browser targets.

Inspired by Stagehand's cache contract: a cache hit is an optimization, never
an authority. Ascended only caches strong, deterministic selector hints and
always falls back to normal semantic resolution when replay fails or cannot be
verified.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlparse

from ascended_browser._app.browser_semantic_target import SemanticTarget


def _route_key(url: str) -> str:
    parsed = urlparse(str(url or ""))
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return ""
    parts = []
    for part in parsed.path.split("/"):
        if not part:
            continue
        if re.fullmatch(r"\d+", part) or re.fullmatch(r"[0-9a-f-]{16,}", part, re.I):
            parts.append("*")
        else:
            parts.append(part[:80])
    return f"{hostname}/{'/'.join(parts)}"


def _css_string(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _replayable_form_control(target: SemanticTarget) -> bool:
    """Limit automatic replay to controls with falsifiable state receipts.

    Generic button/link clicks can succeed at the pointer layer without proving
    the intended application effect. Until click-specific postconditions are
    universally available, do not let those successes warm deterministic replay.
    """
    role = str(target.role or "").strip().casefold()
    tag = str(target.tag or "").strip().casefold()
    typ = str(target.type or "").strip().casefold()
    if role in {"textbox", "combobox", "checkbox", "radio", "switch", "searchbox", "spinbutton"}:
        return True
    if tag in {"select", "textarea"}:
        return True
    if tag == "input" and typ not in {"button", "submit", "reset", "image"}:
        return True
    return False


@dataclass
class ReplayEntry:
    selector: str
    successes: int = 0
    failures: int = 0

    @property
    def ready(self) -> bool:
        # Stagehand exposes a configurable confidence threshold. Keep the same
        # idea locally: do not serve a replay until the same hint has proven
        # itself twice.
        return self.successes >= 2 and self.failures == 0


class SemanticReplayCache:
    def __init__(self, max_entries: int = 256) -> None:
        self.max_entries = max(16, int(max_entries))
        self._entries: dict[tuple[str, str], ReplayEntry] = {}

    def _key(self, url: str, target: SemanticTarget) -> tuple[str, str] | None:
        signature = target.signature()
        route = _route_key(url)
        if not route or not signature:
            return None
        return route, signature

    def selector_hint(self, target: SemanticTarget) -> str:
        # Automatic replay is intentionally stricter than ordinary semantic
        # healing. Require both a strong author ID and a stateful form control
        # whose outcome can be independently checked by the receipt layer.
        if target.element_id and _replayable_form_control(target):
            return f'css=[id="{_css_string(target.element_id)}"]'
        return ""

    def learn_success(self, url: str, target: SemanticTarget) -> None:
        key = self._key(url, target)
        selector = self.selector_hint(target)
        if key is None or not selector:
            return
        entry = self._entries.get(key)
        if entry is None or entry.selector != selector:
            entry = ReplayEntry(selector=selector)
            self._entries[key] = entry
        entry.successes += 1
        entry.failures = 0
        while len(self._entries) > self.max_entries:
            self._entries.pop(next(iter(self._entries)))

    def lookup(self, url: str, target: SemanticTarget) -> ReplayEntry | None:
        key = self._key(url, target)
        entry = self._entries.get(key) if key is not None else None
        if entry and entry.ready:
            return entry
        return None

    def record_failure(self, url: str, target: SemanticTarget) -> None:
        key = self._key(url, target)
        if key is None:
            return
        entry = self._entries.get(key)
        if entry is None:
            return
        entry.failures += 1
        # One failed replay is enough to stop serving it. Re-learning requires
        # two fresh verified successes.
        entry.successes = 0

    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "ready": sum(1 for entry in self._entries.values() if entry.ready),
        }
