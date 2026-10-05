"""Which page the live view is presenting in its browser context.

Production Camoufox gives every page its own full-screen top-level window
(Juggler's newPage opens a window) and the live view captures the bound one.
Juggler routes every pointer dispatch -- and even Playwright's actionability
*trial* -- through its global activation chain, which raises the acted-on
page's window; a key press does not (measured 2026-10-04). The live view's
stack guard keeps the bound window on top, and the click ladder prefers the
keyboard on pages that are not presented, which also avoids the chain's queue.

The page cannot tell us: Camoufox reports ``document.visibilityState ===
'visible'`` and ``hasFocus()`` for every page. So the harness records the page
the live view binds; an agent's own pointer does not change it, because the
live view puts the bound window back on top.
``unknown`` means nothing has been recorded for that context; callers keep
their previous behaviour in that case.
"""
from __future__ import annotations

import weakref
from typing import Any, Literal

PresentedState = Literal["presented", "background", "unknown"]

_PRESENTED: "weakref.WeakKeyDictionary[Any, weakref.ReferenceType]" = weakref.WeakKeyDictionary()


def _context_of(page: Any) -> Any:
    try:
        return getattr(page, "context", None)
    except Exception:
        return None


def mark_presented(page: Any) -> None:
    """Record ``page`` as the selected tab of its window."""
    context = _context_of(page)
    if context is None:
        return
    try:
        _PRESENTED[context] = weakref.ref(page)
    except TypeError:
        pass  # a test double without weakref support; nothing to remember


def presented_state(page: Any) -> PresentedState:
    """Whether ``page`` is its window's selected tab, as far as the harness knows."""
    context = _context_of(page)
    if context is None:
        return "unknown"
    try:
        ref = _PRESENTED.get(context)
    except TypeError:
        return "unknown"
    current = ref() if ref is not None else None
    if current is None:
        return "unknown"
    return "presented" if current is page else "background"


def forget(page: Any) -> None:
    """Drop the record when ``page`` closes, so a stale page is never 'presented'."""
    context = _context_of(page)
    if context is None:
        return
    try:
        ref = _PRESENTED.get(context)
        if ref is not None and ref() is page:
            del _PRESENTED[context]
    except (TypeError, KeyError):
        pass
