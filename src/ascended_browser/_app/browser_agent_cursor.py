"""Agent tool-pointer cursor publication.

Spectator decoration only. Browser tool actions publish where the agent's
pointer is acting so a live-view watcher can see the work happen; none of
these events carry authority, persist state, or affect tool results. The
publisher is installed by the workspace manager (the owner of the live-view
runtime); anywhere else publication is a no-op.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import weakref
from typing import Awaitable, Callable

from ascended_browser._app.browser_click_helpers import _LEGACY_TAB, _LEGACY_WORKSPACE, _resolved_scope

log = logging.getLogger(__name__)

# (workspace_id, tab_id, kind, x, y, label, tone, detail). The installed sink
# must only enqueue: delivery to the daemon happens off the tool path.
Sink = Callable[[str, str, str, float, float, str, int, str], Awaitable[None]]

_sink: Sink | None = None

# One bounded attempt. Publication failing must never stall a tool action;
# a dropped event just means the cursor is not drawn.
_PUBLISH_DEADLINE = 0.15

# Reading the viewport is one page round trip. Pages on the virtual display
# report no Playwright viewport, so without a cache every pointer event paid
# that trip on the tool path (up to 2s each under load).
_VIEWPORT_TTL = 2.0
_VIEWPORT_READ_DEADLINE = 0.25
_viewport_cache: "weakref.WeakKeyDictionary[object, tuple[float, int, int]]" = weakref.WeakKeyDictionary()
# The last size read from any page. Tabs share the live view's one native
# window, so when a busy page cannot answer in time (a stalled click on the
# virtual display) this stands in rather than dropping the cue.
_last_viewport: tuple[int, int] | None = None

# Kinds whose position is optional: the viewer keeps the pointer where it was.
# `status` only narrates (navigation, reading, waiting) and never moves it.
_POSITION_OPTIONAL = {"wheel", "status"}

# The action caption a watcher reads ("Clicking “Sign in”"). Bounded so a
# long accessible name cannot cover the stream.
CAPTION_LIMIT = 64
_NAME_LIMIT = 36
# Opaque snapshot refs ("e12", "e3f9") name nothing a person can read.
_REF_LIKE = re.compile(r"^@?[a-z]{1,3}\d[\w-]*$", re.IGNORECASE)

# Each emitted event bumps its tab's sequence, so a late geometry read can
# tell whether a newer event already moved the pointer elsewhere.
_sequence: dict[tuple[str, str], int] = {}


def install_sink(sink: Sink | None) -> None:
    """Bind the manager-owned publisher; None restores the no-op default."""
    global _sink
    _sink = sink


def caption(verb: str, name: object = "", *, extra: str = "") -> str:
    """One readable action caption: `Clicking “Sign in”`.

    Opaque refs and empty names leave just the verb; the caption is spectator
    narration only and never carries authority or tool evidence.
    """
    text = " ".join(str(name or "").split())
    if text and _REF_LIKE.match(text):
        text = ""
    if len(text) > _NAME_LIMIT:
        text = text[:_NAME_LIMIT - 1].rstrip() + "…"
    out = f"{verb} “{text}”" if text else str(verb)
    if extra:
        out = f"{out} {extra}"
    return out[:CAPTION_LIMIT]


def sequence(workspace_id: str | None, tab_id: str | None) -> int:
    """The tab's current event sequence (see `_sequence`)."""
    scope = _resolved_scope(workspace_id, tab_id)
    return _sequence.get(scope, 0)


def invalidate_viewport_size(page) -> None:
    """Forget a page's cached geometry after an authoritative viewport change."""
    try:
        _viewport_cache.pop(page, None)
    except TypeError:
        pass


def cached_viewport_size(page) -> tuple[int, int] | None:
    """The same page's known CSS dimensions, without a tool-path round trip."""
    try:
        viewport = page.viewport_size or {}
        width, height = int(viewport.get("width") or 0), int(viewport.get("height") or 0)
        if width > 0 and height > 0:
            return width, height
        cached = _viewport_cache.get(page)
        if cached and time.monotonic() - cached[0] <= _VIEWPORT_TTL:
            return cached[1], cached[2]
    except (TypeError, ValueError, AttributeError):
        pass
    return None


async def _page_size(page) -> tuple[int, int]:
    """Page viewport dimensions; browser tools run on virtual-display pages
    whose Playwright viewport_size is empty, so window metrics are the truth.
    """
    global _last_viewport
    try:
        viewport = page.viewport_size or {}
    except Exception:
        viewport = {}
    width = int(viewport.get("width") or 0)
    height = int(viewport.get("height") or 0)
    if width > 0 and height > 0:
        return width, height
    now = time.monotonic()
    try:
        cached = _viewport_cache.get(page)
    except TypeError:
        cached = None
    if cached and now - cached[0] < _VIEWPORT_TTL:
        return cached[1], cached[2]
    try:
        size = await asyncio.wait_for(
            page.evaluate("() => [window.innerWidth, window.innerHeight]"),
            timeout=_VIEWPORT_READ_DEADLINE,
        ) or (0, 0)
        width, height = int(size[0] or 0), int(size[1] or 0)
    except Exception:
        # A slow read keeps the last known size rather than dropping the cue.
        if cached:
            return cached[1], cached[2]
        return _last_viewport or (0, 0)
    if width > 0 and height > 0:
        _last_viewport = (width, height)
        try:
            _viewport_cache[page] = (now, width, height)
        except TypeError:
            pass
    return width, height


async def _normalized(page, x: float | None, y: float | None) -> tuple[float, float] | None:
    """CSS pixels fed by the tools, normalized against the page viewport.

    Callers may also pass explicit fractions (both in [0,1]) when the page has
    no readable viewport; those are clamped, not rescaled.
    """
    if x is None or y is None:
        return None
    width, height = await _page_size(page)
    if width <= 0 or height <= 0:
        if 0.0 <= float(x) <= 1.0 and 0.0 <= float(y) <= 1.0:
            return min(1.0, max(0.0, float(x))), min(1.0, max(0.0, float(y)))
        return None
    return (
        min(1.0, max(0.0, float(x) / width)),
        min(1.0, max(0.0, float(y) / height)),
    )


async def emit(
    page,
    kind: str,
    *,
    x: float | None = None,
    y: float | None = None,
    label: str = "",
    tone: int = 0,
    detail: str = "",
    require_position: bool = True,
    workspace_id: str | None = None,
    tab_id: str | None = None,
    if_sequence: int | None = None,
) -> bool:
    """Publish one normalized pointer event if a publisher is installed.

    The scope comes from the acting tool call (helpers pass their explicit
    workspace/tab); an uncommented legacy scope is never published.
    `detail` is the action caption shown to the watcher. `if_sequence` drops
    the event when a newer one was already emitted for the tab, so a late
    geometry read can never pull the pointer back to an earlier target.
    Returns whether the event was handed to the publisher, so a caller can
    skip the follow-ups of a cue that was dropped.
    """
    sink = _sink
    if sink is None:
        return False
    scope_workspace, scope_tab = _resolved_scope(workspace_id, tab_id)
    if (not scope_workspace or not scope_tab
            or scope_workspace == _LEGACY_WORKSPACE or scope_tab == _LEGACY_TAB):
        return False
    scope = (scope_workspace, scope_tab)
    if if_sequence is not None and _sequence.get(scope, 0) != if_sequence:
        return False
    if kind == "status":
        coords = None
    else:
        coords = await _normalized(page, x, y)
    known = coords is not None
    if not known and (require_position and kind not in _POSITION_OPTIONAL):
        return False
    fx, fy = coords if known else (0.0, 0.0)
    if if_sequence is not None and _sequence.get(scope, 0) != if_sequence:
        return False
    _sequence[scope] = _sequence.get(scope, 0) + 1
    if len(_sequence) > 512:
        _sequence.clear()
    try:
        await asyncio.wait_for(
            sink(scope_workspace, scope_tab, str(kind or ""), fx, fy,
                 str(label or ""), int(tone), str(detail or "")[:CAPTION_LIMIT]),
            timeout=_PUBLISH_DEADLINE,
        )
    except Exception:
        log.debug("agent cursor event dropped", exc_info=True)
        return False
    return True


def box_center(box: dict | None) -> tuple[float, float] | None:
    """Center of an element bounding box (page CSS pixels)."""
    if not isinstance(box, dict):
        return None
    try:
        return (
            float(box["x"]) + float(box["width"]) / 2.0,
            float(box["y"]) + float(box["height"]) / 2.0,
        )
    except (KeyError, TypeError, ValueError):
        return None
