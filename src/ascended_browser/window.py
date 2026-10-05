"""Where the browser window lives, and resizing it for responsive checks.

Linux with Xvfb (the default): the package starts its own virtual display and
launches the browser on it in flexible-window mode, so ``browser_viewport``
can resize the real window and the page reflows as it would for a person.
``ASCENDED_BROWSER_WINDOW=show`` puts the window on your own display instead.
Elsewhere the browser runs headless (or visible with ``show``) and the window
keeps its launch size.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from typing import Any

log = logging.getLogger("ascended_browser")


def wants_visible() -> bool:
    return os.environ.get("ASCENDED_BROWSER_WINDOW", "").strip().lower() in {"show", "visible", "1", "true"}


class BrowserWindow:
    def __init__(self) -> None:
        self.display = ""
        self._xvfb = None
        self.headless: bool | str = True
        linux = sys.platform.startswith("linux")
        if wants_visible():
            self.headless = False
            self.display = os.environ.get("DISPLAY", "") if linux else ""
        elif linux and shutil.which("Xvfb"):
            self.headless = False
            from ._app.browser_workspace.engine import _start_virtual_display

            self._xvfb, self.display = _start_virtual_display()
        self._launch_size: dict[str, tuple[int, int]] = {}

    @property
    def resizable(self) -> bool:
        from .runtime import xwindow

        return bool(self.display) and xwindow.available(self.display)

    def describe(self) -> str:
        if self._xvfb is not None:
            return f"private virtual display {self.display} (resizable)"
        if self.headless is False:
            return f"visible window{' on ' + self.display if self.display else ''}"
        return "headless"

    def install(self, manager: Any) -> None:
        """Launch headed on this window's display, in flexible-window mode when resizable."""
        engine = manager.backend.engine
        original = engine.launch_options
        kiosk = os.environ.get("ASCENDED_BROWSER_KIOSK", "").strip() in {"1", "true", "yes"}

        def launch_options(*args, **kwargs):
            from .browser_build import pin

            pin(download=True)  # the tested Camoufox build, never whatever is newest
            options = original(*args, **kwargs)
            options["headless"] = self.headless
            if self.display:
                options.setdefault("env", {})["DISPLAY"] = self.display
                options["env"].pop("WAYLAND_DISPLAY", None)
                options["virtual_display"] = self.display
                # The page lays out at the real window size, so resizing reflows it.
                options["_odysseus_flexible_window"] = True
            if os.environ.get("ASCENDED_DEMO_EVENTS", "").strip():
                # Recordings draw the pointer from the logged path; hide Camoufox's red dot.
                options["config"] = {**(options.get("config") or {}), "showcursor": False}
            if kiosk and "--kiosk" not in options.get("args", []):
                options["args"] = list(options.get("args") or []) + ["--kiosk"]
            return options

        engine.launch_options = launch_options

    async def resize(self, manager: Any, owner: str, session: str, tab_id: str,
                     width: int | None, height: int | None) -> dict[str, Any]:
        """Resize the window so the page's viewport is ``width`` x ``height``
        (None: back to the launch size). Returns the measured result."""
        from .runtime import xwindow

        key = (manager.workspace_id(owner, session), tab_id)
        page = manager._pages.get(key)
        if page is None:
            raise ValueError("browser_viewport: open the tab first (no live page for this tab_id)")
        measure = "() => [innerWidth, innerHeight, outerWidth - innerWidth, outerHeight - innerHeight]"
        inner_w, inner_h, chrome_w, chrome_h = await page.evaluate(measure)
        self._launch_size.setdefault(self.display, (inner_w, inner_h))
        target_w, target_h = (width, height) if width and height else self._launch_size[self.display]
        outer_w, outer_h = target_w + chrome_w, target_h + chrome_h
        measured = (inner_w, inner_h)
        # The browser's own frame estimate can be off by a border; measure the
        # result and correct once.
        for _attempt in range(2):
            await asyncio.to_thread(xwindow.resize, self.display, outer_w, outer_h)
            for _ in range(30):  # wait for the page to reflow at the new size
                await asyncio.sleep(0.1)
                now = await page.evaluate("() => [innerWidth, innerHeight]")
                if (int(now[0]), int(now[1])) != measured or (int(now[0]), int(now[1])) == (target_w, target_h):
                    await asyncio.sleep(0.15)
                    now = await page.evaluate("() => [innerWidth, innerHeight]")
                    measured = (int(now[0]), int(now[1]))
                    break
            if measured == (target_w, target_h):
                break
            outer_w += target_w - measured[0]
            outer_h += target_h - measured[1]
        invalidate = getattr(manager, "_invalidate_owner_geometry_observations", None)
        if callable(invalidate):
            invalidate(owner)
        return {"requested": [target_w, target_h], "before": [inner_w, inner_h], "width": measured[0],
                "height": measured[1], "clamped": measured != (target_w, target_h)}

    def close(self) -> None:
        if self._xvfb is not None:
            self._xvfb.terminate()
            try:
                self._xvfb.wait(timeout=5)
            except Exception:
                self._xvfb.kill()
            self._xvfb = None


# ── Recording support (off unless the environment asks for it) ──

def demo_log(event: dict) -> None:
    """Append one event to ASCENDED_DEMO_EVENTS (a demo recorder's timeline)."""
    path = os.environ.get("ASCENDED_DEMO_EVENTS", "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"t": time.time(), **event}) + "\n")
    except OSError:
        pass


def install_demo_events(window: "BrowserWindow", manager: Any) -> None:
    """Log where each pointer action lands, for a recorder to draw clicks and zoom.

    Uses the agent-pointer events the browser code already emits (positions are
    fractions of the viewport). Nothing is drawn in the page.
    """
    if not os.environ.get("ASCENDED_DEMO_EVENTS", "").strip():
        return
    from ._app import browser_agent_cursor

    async def sink(workspace_id, tab_id, kind, x, y, label, tone, detail=""):
        demo_log({"type": "pointer", "kind": kind, "x": x, "y": y, "label": label, "detail": detail})

    browser_agent_cursor.install_sink(sink)


async def log_geometry(manager: Any, owner: str, session: str, tab_id: str) -> None:
    """Where the page's viewport sits on the screen, for mapping pointer fractions."""
    if not os.environ.get("ASCENDED_DEMO_EVENTS", "").strip():
        return
    page = manager._pages.get((manager.workspace_id(owner, session), tab_id))
    if page is None:
        return
    try:
        x, y, w, h = await page.evaluate("() => [mozInnerScreenX, mozInnerScreenY, innerWidth, innerHeight]")
        demo_log({"type": "geometry", "x": x, "y": y, "width": w, "height": h})
    except Exception:
        pass


async def apply_frame_size(window: "BrowserWindow") -> None:
    """ASCENDED_BROWSER_WINDOW_SIZE=WxH: size the window once, after the first page opens."""
    wanted = os.environ.get("ASCENDED_BROWSER_WINDOW_SIZE", "").strip().lower()
    if not wanted or getattr(window, "_frame_applied", False) or not window.resizable:
        return
    window._frame_applied = True
    try:
        width, height = (int(part) for part in wanted.split("x", 1))
        from .runtime import xwindow

        await asyncio.to_thread(xwindow.resize, window.display, width, height)
        await asyncio.sleep(0.5)
    except Exception:
        log.debug("could not apply ASCENDED_BROWSER_WINDOW_SIZE=%s", wanted, exc_info=True)


_REF = re.compile(r"ref=(e[0-9a-f]{6,})")
_IMPACT = re.compile(r"^\s*\[(critical|serious|moderate|minor)\] [\w-]+:")


def refs_in_result(text: str, audit: bool) -> list[tuple[str, str]]:
    """(ref, tag) for each element a result names; audits tag each with its impact."""
    readable = [json.loads(f'"{m}"') for m in re.findall(r'"relevant_text": "((?:[^"\\]|\\.)*)"', text)]
    found, impact, seen = [], "query", set()
    for line in "\n".join(readable or [text]).splitlines():
        match = _IMPACT.match(line)
        if audit and match:
            impact = match.group(1)
            continue
        for ref in _REF.findall(line):
            if ref not in seen:
                seen.add(ref)
                found.append((ref, impact if audit else "query"))
    return found


async def log_boxes(manager: Any, owner: str, session: str, tab_id: str, kind: str,
                    items: list[tuple[str, str]]) -> None:
    """Where named elements sit in the viewport (fractions), for a recorder to outline them."""
    if not items or not os.environ.get("ASCENDED_DEMO_EVENTS", "").strip():
        return
    workspace = manager.workspace_id(owner, session)
    page = manager._pages.get((workspace, tab_id))
    if page is None:
        return
    from ._app.browser_click_helpers import resolve_target

    try:
        width, height = await page.evaluate("() => [innerWidth, innerHeight]")
    except Exception:
        return
    boxes = []
    for ref, tag in items[:24]:
        try:
            root, selector = resolve_target(page, ref, workspace_id=workspace, tab_id=tab_id)
            box = await asyncio.wait_for(root.locator(selector).first.bounding_box(), 1.0)
        except Exception:
            continue
        if not box or box["width"] <= 1 or box["height"] <= 1:
            continue
        if box["y"] + box["height"] < 0 or box["y"] > height or box["x"] + box["width"] < 0 or box["x"] > width:
            continue
        boxes.append({"x": box["x"] / width, "y": box["y"] / height, "w": box["width"] / width,
                      "h": box["height"] / height, "tag": tag, "ref": ref})
    if boxes:
        demo_log({"type": "boxes", "kind": kind, "items": boxes})


_FIND_JS = """(query) => {
  const out = [], needle = query.toLowerCase();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode()) && out.length < 4) {
    const text = node.data.toLowerCase();
    for (let at = text.indexOf(needle); at >= 0 && out.length < 4; at = text.indexOf(needle, at + 1)) {
      const range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + needle.length);
      const r = range.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth)
        out.push({x: r.left / innerWidth, y: r.top / innerHeight, w: r.width / innerWidth, h: r.height / innerHeight, tag: 'find'});
    }
  }
  return out;
}"""


async def log_find(manager: Any, owner: str, session: str, tab_id: str, query: str) -> None:
    """Where a found phrase shows on screen, for a recorder to mark it."""
    if not query or not os.environ.get("ASCENDED_DEMO_EVENTS", "").strip():
        return
    page = manager._pages.get((manager.workspace_id(owner, session), tab_id))
    if page is None:
        return
    try:
        boxes = await asyncio.wait_for(page.evaluate(_FIND_JS, query), 2.0)
    except Exception:
        return
    if boxes:
        demo_log({"type": "boxes", "kind": "find", "items": boxes})
