"""When is a page done responding to an action?

A quiet DOM is not a finished page. Signing in to Workday closes the dialog at
once, then the page holds still while the login request, two follow-up
fetches and a full-document redirect run: the old settle (two equal DOM
samples 250 ms apart) returned the stale "Create Account" page 350 ms before
the navigation even committed, and the signed-in page arrived seconds later.

The reference agents all watch the network the action caused:

- Playwright MCP (`waitForCompletion`): every request started during the
  action must finish; a main-frame navigation instead waits for `load`;
  capped at 10 s.
- Stagehand (`waitForDomNetworkQuiet`): no request in flight for 500 ms,
  WebSocket/EventSource ignored, a request older than 2 s treated as stalled.
- browser-use: reports still-pending requests in the page state so the model
  knows the page is still loading.

This combines them: track document/fetch/XHR/script requests from dispatch on;
follow a main-frame navigation to `load` and keep watching the new page's own
requests; call it settled after 500 ms with nothing in flight; ignore
long-lived streams and treat anything older than 3 s as polling. If the cap is
reached, say so — the caller reports ``still_loading`` rather than presenting a
half-loaded page as the result.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

# Requests that make up a page's response to an action. Images, fonts, media
# and styles do not change what can be acted on; beacons and CSP reports are
# telemetry; streams never finish.
_TRACKED_TYPES = frozenset({"document", "fetch", "xhr", "script"})
_QUIET_SECONDS = 0.5
_STALLED_SECONDS = 3.0
_CAP_SECONDS = 10.0


class ActionActivity:
    """Network and navigation activity caused by one action on one page."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.started = time.monotonic()
        self.inflight: dict[Any, tuple[str, float]] = {}
        self.navigated = False
        self.navigated_at: float | None = None
        self.last_change = self.started
        self._listeners: list[tuple[str, Any]] = []

    # -- listening -----------------------------------------------------------

    def start(self) -> "ActionActivity":
        page = self.page

        def on_request(request: Any) -> None:
            try:
                if str(request.resource_type or "") not in _TRACKED_TYPES:
                    return
                self.inflight[request] = (str(request.url or "")[:160], time.monotonic())
                self.last_change = time.monotonic()
            except Exception:
                return

        def on_done(request: Any) -> None:
            if self.inflight.pop(request, None) is not None:
                self.last_change = time.monotonic()

        def on_navigated(frame: Any) -> None:
            try:
                if frame is not page.main_frame:
                    return
            except Exception:
                return
            self.navigated = True
            self.navigated_at = self.last_change = time.monotonic()

        for event, handler in (
            ("request", on_request), ("requestfinished", on_done),
            ("requestfailed", on_done), ("framenavigated", on_navigated),
        ):
            try:
                page.on(event, handler)
                self._listeners.append((event, handler))
            except Exception:
                continue
        return self

    def stop(self) -> None:
        for event, handler in self._listeners:
            try:
                self.page.remove_listener(event, handler)
            except Exception:
                pass
        self._listeners.clear()

    # -- waiting -------------------------------------------------------------

    def pending(self) -> list[str]:
        now = time.monotonic()
        return [url for url, since in self.inflight.values() if now - since < _STALLED_SECONDS]

    async def settle(self, *, cap: float = _CAP_SECONDS, from_now: bool = False) -> dict[str, Any]:
        """Wait until the action's activity is over, or the cap is reached.

        Returns ``{"state": "settled" | "still_loading", "navigated": bool,
        "waited_ms": int, "pending": [...]}``; ``pending`` names what was still
        loading when the cap was reached.
        """
        # from_now: a later observe continues an action that was still loading
        # when its result was returned, with a fresh budget.
        now = time.monotonic()
        if not from_now:
            # Quiet only counts once the action has been dispatched. On a loaded
            # machine the click itself can outlast the quiet window, and the
            # navigation it caused is requested after it returns: counting from
            # start() reported "settled" before that request was even seen.
            self.last_change = max(self.last_change, now)
        deadline = max(
            (now if from_now else self.started) + max(0.0, cap),
            now + _QUIET_SECONDS,
        )
        loaded = False
        while time.monotonic() < deadline:
            if self.navigated and not loaded:
                try:
                    await asyncio.wait_for(
                        self.page.wait_for_load_state("load"),
                        timeout=max(0.05, deadline - time.monotonic()),
                    )
                    loaded = True
                    self.last_change = time.monotonic()
                except Exception:
                    break  # cap reached mid-load, or the page closed
                continue
            if not self.pending() and time.monotonic() - self.last_change >= _QUIET_SECONDS:
                return self._report("settled")
            await asyncio.sleep(0.05)
        return self._report("still_loading" if self.pending() or (self.navigated and not loaded) else "settled")

    def _report(self, state: str) -> dict[str, Any]:
        report: dict[str, Any] = {
            "state": state,
            "navigated": self.navigated,
            "waited_ms": round((time.monotonic() - self.started) * 1000),
        }
        if state == "still_loading":
            report["pending"] = self.pending()[:3]
        return report


def still_loading_note(report: dict[str, Any] | None) -> str:
    """One next step for a result returned while the page was still loading."""
    if not isinstance(report, dict) or report.get("state") != "still_loading":
        return ""
    return (
        "The page was still loading when this result was taken"
        + (" (it navigated)" if report.get("navigated") else "")
        + "; observe the tab again before concluding what the action did."
    )
