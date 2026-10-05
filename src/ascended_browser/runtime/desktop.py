"""The Ascended desktop app's browser host, which never connects here.

With no desktop host the browser always runs its own Camoufox.
"""
from __future__ import annotations

from typing import Any


class DesktopBrowserUnavailable(RuntimeError):
    error_kind = "desktop_browser_unavailable"


class _NoHosts:
    def select(self, owner: str):
        return None

    def status(self, owner: str) -> dict[str, Any]:
        return {"connected": False, "use_for_agent": False, "hosts": 0, "client_id": "",
                "electron": "", "chromium": "", "guests": 0, "channels": 0}

    def absent_for(self, owner: str) -> float:
        return float("inf")


registry = _NoHosts()


def desktop_host_status(owner: str) -> dict[str, Any]:
    return registry.status(owner)


def desktop_host_absent_for(owner: str) -> float:
    return registry.absent_for(owner)


def mint_cdp_token(owner: str) -> str:
    raise DesktopBrowserUnavailable("no Ascended desktop app is connected")
