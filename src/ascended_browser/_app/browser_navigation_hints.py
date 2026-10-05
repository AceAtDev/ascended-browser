"""Post-click navigation hints and overlay dismissal for browser automation."""

from __future__ import annotations

import asyncio
import re
from typing import Any

OVERLAY_DETECT_SCRIPT = """() => {
  const selectors = [
    '[role=dialog]',
    '[aria-modal="true"]',
    '[aria-modal=true]',
    '#bannerModal',
    '[id*="Modal"]',
    '[id*="modal"]',
    '.modal.open',
    '.modal.show',
    '.overlay',
    '.drawer.open',
    '[class*="modal"][class*="open"]',
    '[class*="Modal"]',
  ];
  const hints = [];
  for (const sel of selectors) {
    try {
      document.querySelectorAll(sel).forEach(el => {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return;
        const rect = el.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) return;
        hints.push(sel);
      });
    } catch (e) {}
  }
  const bodyText = document.body ? (document.body.innerText || '') : '';
  return {
    overlay_count: hints.length,
    overlay_hints: [...new Set(hints)].slice(0, 8),
    body_text_len: bodyText.length,
  };
}"""

BLOCKING_OVERLAY_SCRIPT = """() => {
  function visible(el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 2 && rect.height > 2 && rect.bottom > 0 && rect.right > 0
      && rect.top < window.innerHeight && rect.left < window.innerWidth;
  }
  function blockingCandidate(el) {
    if (!visible(el)) return false;
    const style = window.getComputedStyle(el);
    if (style.pointerEvents === 'none') return false;
    if (el.getAttribute('aria-modal') === 'true') return true;
    if (el.tagName === 'DIALOG' && el.matches(':modal')) return true;
    // Name substrings are discovery hints, not evidence of an overlay.
    // Wikipedia citations containing "modal" are ordinary inline links.
    return (style.position === 'fixed' || style.position === 'absolute')
      && (el.getAttribute('role') === 'dialog' || Number(style.zIndex) > 0);
  }
  function selectorFor(el) {
    if (el.id) return '#' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id);
    return '';
  }
  const overlaySelectors = [
    '#bannerModal',
    '[role=dialog]',
    '[aria-modal="true"]',
    '[aria-modal=true]',
    '[id*="Modal"]',
    '[id*="modal"]',
    '.modal.open',
    '.modal.show',
    '.overlay',
    '.drawer.open',
    '[class*="modal"][class*="open"]',
  ];
  const blocking = [];
  const dismiss = [];
  const seen = new Set();
  for (const sel of overlaySelectors) {
    try {
      document.querySelectorAll(sel).forEach(el => {
        if (!blockingCandidate(el) || seen.has(el)) return;
        seen.add(el);
        const id = el.id || '';
        const text = (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
        blocking.push({ selector: selectorFor(el) || sel, id, text });
        el.querySelectorAll(
          'button[aria-label*="close" i], button.close, [data-dismiss="modal"], .close, .modal-close, .bg, .modal-backdrop'
        ).forEach(btn => {
          if (!visible(btn)) return;
          const btnSel = selectorFor(btn);
          if (btnSel) dismiss.push(btnSel);
        });
      });
    } catch (e) {}
  }
  return {
    blocking_overlays: blocking.slice(0, 6),
    dismiss_candidates: [...new Set(dismiss)].slice(0, 8),
    overlay_count: blocking.length,
  };
}"""

DISMISS_CLOSE_SELECTORS = (
    'button[aria-label*="close" i]',
    'button.close',
    '[data-dismiss="modal"]',
    '.modal .close',
    '.modal-header button',
    '#bannerModal .bg',
    '.modal-backdrop',
)


def is_click_intercept_error(exc: BaseException) -> bool:
    """True only when Playwright proves a pointer interception.

    A generic click timeout is not equivalent to an interception.  It can occur
    after dispatch, so sending a mouse/force/JavaScript fallback risks applying
    the effect twice.  Callers must report that case as uncertain instead of
    extending it into another dispatch attempt.
    """
    msg = str(exc).lower()
    return (
        "intercepts pointer events" in msg
        or "not clickable" in msg
    )


async def detect_blocking_overlays(page) -> dict[str, Any]:
    """Return blocking overlay metadata for snapshots and dismiss logic."""
    try:
        data = await page.evaluate(BLOCKING_OVERLAY_SCRIPT)
        if isinstance(data, dict):
            return {
                "blocking_overlays": list(data.get("blocking_overlays") or []),
                "dismiss_candidates": list(data.get("dismiss_candidates") or []),
                "overlay_count": int(data.get("overlay_count") or 0),
            }
    except Exception:
        pass
    return {"blocking_overlays": [], "dismiss_candidates": [], "overlay_count": 0}


async def collect_open_tabs(page) -> list[dict[str, str]]:
    """List open browser tabs for the current context."""
    tabs: list[dict[str, str]] = []
    try:
        context = page.context
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        return tabs
    for index, tab in enumerate(pages[:12]):
        try:
            url = getattr(tab, "url", "") or ""
        except Exception:
            url = ""
        try:
            title = await tab.title()
        except Exception:
            title = ""
        tabs.append({"index": index, "url": url, "title": title})
    return tabs


async def dismiss_blocking_overlays(page, *, max_attempts: int = 3, allow_escape: bool = True) -> bool:
    """Try to dismiss visible overlays. Returns True if none remain.

    ``allow_escape=False`` when the click target sits inside an open listbox or
    menu: Escape closes that popup, and with it the thing being clicked.
    """
    for _ in range(max_attempts):
        info = await detect_blocking_overlays(page)
        if not info.get("blocking_overlays"):
            return True

        dismissed = False
        for candidate in list(info.get("dismiss_candidates") or [])[:3]:
            try:
                await page.locator(candidate).first.click(timeout=1500)
                dismissed = True
                await asyncio.sleep(0.15)
                break
            except Exception:
                continue

        if not dismissed:
            for sel in DISMISS_CLOSE_SELECTORS:
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=1500)
                        dismissed = True
                        await asyncio.sleep(0.15)
                        break
                except Exception:
                    continue

        if not dismissed and allow_escape:
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.15)
                await page.keyboard.press("Escape")
            except Exception:
                pass

        await asyncio.sleep(0.1)

    remaining = await detect_blocking_overlays(page)
    return not remaining.get("blocking_overlays")


async def capture_page_state(page) -> dict[str, Any]:
    """Capture URL, title, and DOM signals for before/after click comparison."""
    try:
        url = getattr(page, "url", "") or ""
    except Exception:
        url = ""
    try:
        title = await page.title()
    except Exception:
        title = ""
    dom_info: dict[str, Any] = {"overlay_count": 0, "overlay_hints": [], "body_text_len": 0}
    try:
        dom_info = await page.evaluate(OVERLAY_DETECT_SCRIPT)
        if not isinstance(dom_info, dict):
            dom_info = {"overlay_count": 0, "overlay_hints": [], "body_text_len": 0}
    except Exception:
        pass
    return {
        "url": url,
        "title": title,
        "overlay_count": int(dom_info.get("overlay_count") or 0),
        "overlay_hints": list(dom_info.get("overlay_hints") or []),
        "body_text_len": int(dom_info.get("body_text_len") or 0),
    }


def classify_navigation(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Return navigation_type: overlay, full_navigation, spa_route, or none."""
    url_changed = (before.get("url") or "") != (after.get("url") or "")
    title_changed = (before.get("title") or "") != (after.get("title") or "")
    dom_changed = (
        before.get("body_text_len", 0) != after.get("body_text_len", 0)
        or before.get("overlay_count", 0) != after.get("overlay_count", 0)
    )
    overlay_grew = after.get("overlay_count", 0) > before.get("overlay_count", 0)
    has_overlays = bool(after.get("overlay_hints"))

    if overlay_grew or (not url_changed and dom_changed and has_overlays):
        return "overlay"
    if url_changed:
        return "full_navigation"
    if dom_changed or title_changed:
        return "spa_route"
    return "none"


def format_navigation_hints(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Format navigation hint block appended to click tool results."""
    nav_type = classify_navigation(before, after)
    if nav_type == "none":
        return ""

    url_changed = (before.get("url") or "") != (after.get("url") or "")
    title_changed = (before.get("title") or "") != (after.get("title") or "")
    dom_changed = (
        before.get("body_text_len", 0) != after.get("body_text_len", 0)
        or before.get("overlay_count", 0) != after.get("overlay_count", 0)
    )

    lines = [
        "[Navigation hint]",
        f"navigation_type: {nav_type}",
        f"url_changed: {str(url_changed).lower()}",
        f"title_changed: {str(title_changed).lower()}",
        f"dom_changed: {str(dom_changed).lower()}",
        f"body_text_len: {after.get('body_text_len', 0)}",
    ]
    overlay_hints = after.get("overlay_hints") or []
    if overlay_hints:
        lines.append(f"overlay_hints: {', '.join(overlay_hints)}")
    if nav_type == "overlay":
        lines.append(
            "Note: URL did not change but the DOM did — likely a modal/overlay. "
            "Use browser_snapshot or browser_evaluate on the overlay content. "
            "browser_navigate_back may not work; look for a close/back button or press Escape."
        )
    elif nav_type == "spa_route":
        lines.append(
            "Note: DOM changed without a full page navigation — content may have updated in-place."
        )
    return "\n".join(lines)


def fingerprint_from_result(result_text: str) -> str:
    """Extended browser result fingerprint including navigation signals."""
    text = result_text or ""
    url_m = re.search(r"(?:^|\n)\s*(?:url|current url)\s*[:=]\s*(\S+)", text, re.IGNORECASE)
    title_m = re.search(r"(?:^|\n)\s*(?:title|page title)\s*[:=]\s*(.+)", text, re.IGNORECASE)
    nav_m = re.search(r"navigation_type:\s*(\S+)", text)
    dom_m = re.search(r"dom_changed:\s*(true|false)", text, re.IGNORECASE)
    body_m = re.search(r"body_text_len:\s*(\d+)", text)

    url = (url_m.group(1).strip() if url_m else "")
    title = ((title_m.group(1).strip() if title_m else "")[:80])
    nav = nav_m.group(1) if nav_m else ""
    dom = dom_m.group(1).lower() if dom_m else ""
    body_len = body_m.group(1) if body_m else ""
    return f"{url}|{title}|{nav}|{dom}|{body_len}"
