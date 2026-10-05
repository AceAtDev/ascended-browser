"""Site keys and a small paced runner for the omnibox's autocomplete lookups."""
from __future__ import annotations

import asyncio
import ipaddress
import threading
import time
from enum import IntEnum
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

T = TypeVar("T")


def registrable_domain(url: str) -> str:
    """eTLD+1 of a URL or host (``news.bbc.co.uk`` -> ``bbc.co.uk``), offline."""
    text = str(url or "")
    host = (urlparse(text if "//" in text else f"//{text}").hostname or "").strip(".").casefold()
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        import tldextract

        found = tldextract.TLDExtract(suffix_list_urls=())(host)
        return ".".join(part for part in (found.domain, found.suffix) if part) or host
    except Exception:
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host


class OutboundPriority(IntEnum):
    BACKGROUND = 0
    FOREGROUND = 1


def provider_slot(provider: str, url: str = "") -> str:
    return f"provider:{provider}"


class _PacedRunner:
    """One request at a time per slot, at least ``spacing`` seconds apart."""

    def __init__(self, spacing: float = 1.0):
        self._spacing = spacing
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    async def run_blocking(self, kind: Any, slot: str, function: Callable[[], T], **_kwargs) -> T:
        with self._lock:
            wait = self._last.get(slot, 0.0) + self._spacing - time.monotonic()
            self._last[slot] = time.monotonic() + max(0.0, wait)
        if wait > 0:
            await asyncio.sleep(wait)
        return await asyncio.to_thread(function)


_runner = _PacedRunner()


def get_outbound_scheduler() -> _PacedRunner:
    return _runner
