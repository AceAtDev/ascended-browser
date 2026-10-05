"""Resize the browser's window on an X display (Linux).

Ascended resizes the window through its live-view daemon; this package has
no daemon, so it talks to the X server directly. Firefox is launched in
flexible-window mode (the page lays out at the real window size), so a real
resize reflows the page exactly as a person dragging the window edge would.
"""
from __future__ import annotations

import os


def _connect(display: str):
    from Xlib import display as xdisplay

    return xdisplay.Display(display)


def browser_windows(display: str) -> list[tuple[int, int, int]]:
    """(window id, width, height) of every mapped top-level window."""
    from Xlib import X

    conn = _connect(display)
    try:
        found = []
        for window in conn.screen().root.query_tree().children:
            try:
                if window.get_attributes().map_state != X.IsViewable:
                    continue
                geometry = window.get_geometry()
            except Exception:
                continue
            if geometry.width > 100 and geometry.height > 100:
                found.append((window.id, geometry.width, geometry.height))
        return found
    finally:
        conn.close()


def resize(display: str, width: int, height: int) -> int:
    """Resize the browser's top-level windows (Firefox maps two per window,
    both sized alike) to ``width`` x ``height``; returns how many changed."""
    conn = _connect(display)
    try:
        changed = 0
        for window_id, _w, _h in browser_windows(display):
            window = conn.create_resource_object("window", window_id)
            window.configure(x=0, y=0, width=max(1, int(width)), height=max(1, int(height)))
            changed += 1
        conn.sync()
        return changed
    finally:
        conn.close()


def available(display: str) -> bool:
    if not display or os.name == "nt":
        return False
    try:
        import Xlib  # noqa: F401
    except ImportError:
        return False
    return True
