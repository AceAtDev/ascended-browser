"""Embedded challenge widgets: read their state and press their checkbox.

A full-page Cloudflare interstitial blocks the whole document and is cleared
by ``browser_wall``'s cookie harvest. An *embedded* widget is different: a
Turnstile, reCAPTCHA v2 or hCaptcha box sitting inside an ordinary form, whose
only output is a response token written into a hidden field on submit. There
is no cookie to harvest; the widget either passes on its own (Turnstile's
managed and non-interactive modes usually do in Camoufox) or wants one press of
its checkbox, after which a clean browser usually gets a token without a
puzzle.

This module only reads and presses. It never solves an image/puzzle challenge:
when the provider escalates to one, the state says so and the caller hands the
page to a human. Invisible variants (reCAPTCHA v3/invisible badge, Turnstile
invisible) have no checkbox and are never pressed.
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any

# Frame URL -> (provider, role). ``anchor`` hosts the checkbox; ``challenge``
# is the image/puzzle popup that only a human can clear.
_FRAME_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"challenges\.cloudflare\.com/.*(?:/turnstile/|/challenge-platform/)"), "turnstile", "anchor"),
    (re.compile(r"/recaptcha/(?:api2|enterprise)/anchor"), "recaptcha", "anchor"),
    (re.compile(r"/recaptcha/(?:api2|enterprise)/bframe"), "recaptcha", "challenge"),
    (re.compile(r"hcaptcha\.com/.*[#&?]frame=checkbox(?!-invisible)"), "hcaptcha", "anchor"),
    (re.compile(r"hcaptcha\.com/.*[#&?]frame=challenge"), "hcaptcha", "challenge"),
)

# Checkbox inside the anchor frame. Turnstile's lives in a closed shadow root
# that selectors cannot reach, so it falls back to the geometric offset.
_CHECKBOX_SELECTORS = {
    "recaptcha": "#recaptcha-anchor",
    "hcaptcha": "#checkbox",
    "turnstile": "input[type=checkbox]",
}

# All three providers draw the checkbox ~26-30 px from the frame's left edge,
# vertically centred, in a ~300x65-78 frame (SeleniumBase, Scrapling and
# Botasaurus all click there). Measured from the iframe box, so a centred or
# right-aligned container does not break it.
_CHECKBOX_LEFT_OFFSET = 28
_JITTER_PX = 3
_MIN_WIDGET_SIDE = 20

# Read in the main document: one entry per provider whose response field is
# present, ``filled`` when the widget has already produced a token. The hidden
# field is the primary signal: Camoufox evaluates in an isolated world where
# the page's ``turnstile``/``grecaptcha``/``hcaptcha`` globals are undefined,
# so those API reads only help on engines that share the main world.
_TOKEN_PROBE_JS = """() => {
  const fields = {
    turnstile: 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]',
    recaptcha: 'textarea[name="g-recaptcha-response"], input[name="g-recaptcha-response"]',
    hcaptcha: 'textarea[name="h-captcha-response"], input[name="h-captcha-response"]',
  };
  const out = {};
  for (const [provider, selector] of Object.entries(fields)) {
    const nodes = Array.from(document.querySelectorAll(selector));
    let filled = nodes.some(node => String(node.value || '').length > 0);
    if (provider === 'turnstile' && !filled) {
      try { filled = Boolean(window.turnstile && window.turnstile.getResponse && window.turnstile.getResponse()); } catch (e) {}
    }
    if (provider === 'recaptcha' && !filled) {
      try { filled = Boolean(window.grecaptcha && window.grecaptcha.getResponse && window.grecaptcha.getResponse()); } catch (e) {}
    }
    if (provider === 'hcaptcha' && !filled) {
      filled = Array.from(document.querySelectorAll('iframe[data-hcaptcha-response]'))
        .some(node => String(node.getAttribute('data-hcaptcha-response') || '').length > 0);
      try { filled = filled || Boolean(window.hcaptcha && window.hcaptcha.getResponse && window.hcaptcha.getResponse()); } catch (e) {}
    }
    if (nodes.length || filled) out[provider] = {present: nodes.length > 0, filled};
  }
  return out;
}"""


def classify_frame_url(url: str) -> tuple[str, str]:
    """``(provider, role)`` for a widget frame URL, ``("", "")`` otherwise."""
    lowered = str(url or "").lower()
    if "size=invisible" in lowered:
        return "", ""
    for pattern, provider, role in _FRAME_RULES:
        if pattern.search(lowered):
            return provider, role
    return "", ""


async def _bounded(awaitable: Any, seconds: float) -> Any:
    return await asyncio.wait_for(awaitable, timeout=seconds)


async def _frame_box(frame: Any) -> tuple[dict[str, float] | None, bool]:
    """Page-coordinate box of a frame's ``<iframe>`` element and its visibility."""
    try:
        element = await _bounded(frame.frame_element(), 2.0)
        visible = bool(await _bounded(element.is_visible(), 2.0))
        box = await _bounded(element.bounding_box(), 2.0)
    except Exception:  # noqa: BLE001 - a detached frame is simply not a widget
        return None, False
    if not box:
        return None, False
    if box.get("width", 0) < _MIN_WIDGET_SIDE or box.get("height", 0) < _MIN_WIDGET_SIDE:
        return box, False
    return box, visible


async def widget_state(page: Any) -> dict[str, Any]:
    """Read-only snapshot of the embedded widgets on ``page``.

    ``pending`` is the provider of a visible checkbox widget that has not
    produced a token yet, ``challenge`` the provider whose image/puzzle popup
    is showing. Both empty means there is nothing for the solver to do.
    """
    widgets: list[dict[str, Any]] = []
    challenge = ""
    for frame in list(getattr(page, "frames", []) or []):
        provider, role = classify_frame_url(str(getattr(frame, "url", "") or ""))
        if not provider:
            continue
        box, visible = await _frame_box(frame)
        if role == "challenge":
            if visible and not challenge:
                challenge = provider
            continue
        if visible and box:
            widgets.append({"provider": provider, "box": box, "frame": frame})
    try:
        tokens = await _bounded(page.evaluate(_TOKEN_PROBE_JS), 3.0) or {}
    except Exception:  # noqa: BLE001 - unreadable tokens read as "not yet"
        tokens = {}
    solved = sorted(p for p, t in tokens.items() if isinstance(t, dict) and t.get("filled"))
    pending = next(
        (w["provider"] for w in widgets if w["provider"] not in solved), "",
    )
    return {
        "pending": pending,
        "challenge": challenge,
        "solved": solved,
        "widgets": widgets,
    }


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    """``widget_state`` without live frame handles, safe to return to callers."""
    return {
        "pending": state.get("pending", ""),
        "challenge": state.get("challenge", ""),
        "solved": list(state.get("solved") or []),
        "widgets": [
            {"provider": w.get("provider", ""), "box": w.get("box")}
            for w in state.get("widgets") or []
        ],
    }


async def _checkbox_point(widget: dict[str, Any]) -> tuple[float, float]:
    frame = widget["frame"]
    box = widget["box"]
    selector = _CHECKBOX_SELECTORS.get(widget["provider"], "")
    if selector:
        try:
            inner = await _bounded(frame.locator(selector).first.bounding_box(timeout=1_500), 2.5)
        except Exception:  # noqa: BLE001 - closed shadow roots hide the box
            inner = None
        if inner and inner.get("width") and inner.get("height"):
            return inner["x"] + inner["width"] / 2, inner["y"] + inner["height"] / 2
    left = min(_CHECKBOX_LEFT_OFFSET, box["width"] / 2)
    return (
        box["x"] + left + random.uniform(-_JITTER_PX, _JITTER_PX),
        box["y"] + box["height"] / 2 + random.uniform(-_JITTER_PX, _JITTER_PX),
    )


async def press_checkbox(page: Any, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Press the pending widget's checkbox with a real mouse event.

    The mouse goes through ``page.mouse`` so Camoufox's humanized cursor path
    applies; a synthetic ``element.click()`` is exactly what the providers
    score against. Returns ``{"pressed": bool, "provider": str, "detail": str}``.
    """
    state = state if state is not None else await widget_state(page)
    provider = str(state.get("pending") or "")
    if state.get("challenge"):
        return {"pressed": False, "provider": state["challenge"], "detail": "image challenge showing"}
    widget = next((w for w in state.get("widgets") or [] if w.get("provider") == provider), None)
    if not provider or widget is None:
        return {"pressed": False, "provider": "", "detail": "no pending widget"}
    try:
        element = await _bounded(widget["frame"].frame_element(), 2.0)
        await _bounded(element.scroll_into_view_if_needed(timeout=2_000), 3.0)
        box, visible = await _frame_box(widget["frame"])
        if box and visible:
            widget = {**widget, "box": box}
        x, y = await _checkbox_point(widget)
        # A press that lasts 100-200 ms, like a finger, not a zero-length event.
        await _bounded(page.mouse.click(x, y, delay=random.randint(100, 200)), 10.0)
    except Exception as exc:  # noqa: BLE001 - a failed press is a soft miss
        return {"pressed": False, "provider": provider, "detail": f"press failed: {type(exc).__name__}"}
    return {"pressed": True, "provider": provider, "detail": "checkbox pressed"}
