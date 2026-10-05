"""Pictures of an agent-owned browser tab, for evidence and visual inspection.

The workspace's primary channel is the observation outline: refs the model
acts on. A picture is the complement for what that outline cannot describe
(canvas, charts, visual layout) and the user's proof that work happened (a
filled form before submit, a confirmation page). It is deliberately not a
navigation channel, so a capture carries no refs or coordinates.

This module owns the page-level capture, sensitive-field masking, the saved
evidence file and the model-facing receipt. The browser workspace manager owns
authority, locks and ref resolution. A capture is never published as a chat
media card: the tool row previews it, and the agent decides whether to present
an evidence file with present_artifact.
"""
from __future__ import annotations

import asyncio
import base64
import struct
from dataclasses import dataclass, field
import re
from typing import Any

from ascended_browser._app.browser_sensitive_state import SENSITIVE_CAPTURE_SELECTOR, SENSITIVE_LABEL_PATTERN

CAPTURE_PURPOSES = ("evidence", "inspect")
CAPTURE_SCOPES = ("viewport", "full_page", "element")

#: Bound on one page-level capture. The caller already holds the tab's action
#: lock, so a wedged page must fail instead of stalling every tool on the tab.
CAPTURE_TIMEOUT_SECONDS = 15.0
#: Tall pages (feeds, infinite scroll) would otherwise produce an image the
#: browser cannot allocate and no model can read. Longer pages are cut here and
#: the receipt says so.
FULL_PAGE_MAX_CSS_HEIGHT = 8000
#: Frames searched individually for sensitive fields. The cap bounds the probe
#: on ad-heavy pages; any frame past it, or one that refuses the probe, is not
#: trusted and is masked whole from its parent document instead.
MAX_MASK_FRAMES = 32
#: Deliberately unnatural so a masked field reads as redacted, not as a dark
#: element of the page (a dark mask disappears on dark-themed sites).
MASK_COLOR = "#FF00FF"
_LABEL_RE = re.compile(SENSITIVE_LABEL_PATTERN, re.IGNORECASE)

_PAGE_METRICS_JS = """() => {
  const doc = document.documentElement;
  const body = document.body;
  return {
    viewport_width: window.innerWidth,
    viewport_height: window.innerHeight,
    device_pixel_ratio: window.devicePixelRatio || 1,
    scroll_x: Math.round(window.scrollX),
    scroll_y: Math.round(window.scrollY),
    document_width: Math.max(doc ? doc.scrollWidth : 0, body ? body.scrollWidth : 0),
    document_height: Math.max(doc ? doc.scrollHeight : 0, body ? body.scrollHeight : 0),
  };
}"""


class CaptureError(RuntimeError):
    """A capture that could not produce a trustworthy picture."""

    def __init__(self, message: str, *, error_kind: str = "capture_failed") -> None:
        super().__init__(message)
        self.error_kind = error_kind


@dataclass
class PageCapture:
    data: bytes
    mime_type: str
    image_width: int
    image_height: int
    metrics: dict[str, Any] = field(default_factory=dict)
    masked_fields: int = 0
    truncated: bool = False


async def _bounded(awaitable: Any, timeout: float) -> Any:
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except BaseException:
        task.cancel()
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        raise


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Pixel size of a PNG or baseline/progressive JPEG, from its header."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            index += 1 if marker == 0xFF else 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5:index + 9])
            return int(width), int(height)
        (length,) = struct.unpack(">H", data[index + 2:index + 4])
        index += 2 + length
    return None


async def sensitive_mask_locators(page: Any) -> tuple[list[Any], int]:
    """Locators for every sensitive region, and how many elements they cover.

    Fails closed. The main frame must answer the probe or the capture is
    refused. A child frame that cannot be checked (past the cap, detached
    mid-probe, or refusing it) is masked whole: every frame element in its
    parent document is covered, since which one hosts it is not knowable
    without the probe that just failed.
    """
    frames = list(getattr(page, "frames", None) or [])
    main = getattr(page, "main_frame", None) or (frames[0] if frames else page)
    locators: list[Any] = []
    count = 0
    unchecked_parents: list[Any] = []
    for index, frame in enumerate(frames or [page]):
        if index >= MAX_MASK_FRAMES:
            _note_unchecked(frame, unchecked_parents)
            continue
        locator = frame.locator(SENSITIVE_CAPTURE_SELECTOR).or_(frame.get_by_label(_LABEL_RE))
        try:
            matched = int(await _bounded(locator.count(), 2.0))
        except Exception as exc:
            if frame is main:
                raise CaptureError(
                    "Could not check the page for password or payment fields, "
                    "so no picture was taken",
                    error_kind="mask_unavailable",
                ) from exc
            _note_unchecked(frame, unchecked_parents)
            continue
        if matched:
            locators.append(locator)
            count += matched
    for parent in unchecked_parents:
        locator = parent.locator("iframe, frame")
        try:
            matched = int(await _bounded(locator.count(), 2.0))
        except Exception as exc:
            raise CaptureError(
                "Could not mask a frame that was not checked for password or "
                "payment fields, so no picture was taken",
                error_kind="mask_unavailable",
            ) from exc
        if matched:
            locators.append(locator)
            count += matched
    return locators, count


def _note_unchecked(frame: Any, parents: list[Any]) -> None:
    parent = getattr(frame, "parent_frame", None)
    if parent is None:
        return
    if not any(parent is known for known in parents):
        parents.append(parent)


async def capture_page(
    page: Any,
    *,
    scope: str = "viewport",
    element: Any = None,
    timeout: float = CAPTURE_TIMEOUT_SECONDS,
) -> PageCapture:
    """Capture *page* (or *element*) with sensitive fields masked.

    ``viewport`` is what a person looking at the tab sees now. ``full_page``
    is the whole scrollable document up to ``FULL_PAGE_MAX_CSS_HEIGHT``.
    ``element`` is one located element; bringing it into view may scroll the
    window, so the original scroll position is restored afterwards.
    """
    if scope not in CAPTURE_SCOPES:
        raise CaptureError(f"Unknown capture scope: {scope}", error_kind="invalid_request")
    if scope == "element" and element is None:
        raise CaptureError("An element capture needs a resolved element", error_kind="invalid_request")
    try:
        metrics = dict(await _bounded(page.evaluate(_PAGE_METRICS_JS), 5.0) or {})
    except Exception as exc:
        raise CaptureError("The page did not answer a layout read, so no picture was taken") from exc
    masks, masked = await sensitive_mask_locators(page)

    # CSS-pixel scale keeps the image in the same units as the viewport the
    # receipt reports, whatever the display's device pixel ratio.
    options: dict[str, Any] = {
        "animations": "disabled",
        "caret": "hide",
        "scale": "css",
        "mask": masks,
        "mask_color": MASK_COLOR,
        "timeout": int(timeout * 1000),
    }
    truncated = False
    mime_type = "image/png"
    try:
        if scope == "element":
            try:
                data = await _bounded(element.screenshot(type="png", **options), timeout + 1)
            finally:
                try:
                    await _bounded(page.evaluate(
                        "([x, y]) => window.scrollTo(x, y)",
                        [metrics.get("scroll_x", 0), metrics.get("scroll_y", 0)],
                    ), 2.0)
                except Exception:
                    pass
        elif scope == "full_page":
            # JPEG: a whole document as PNG is routinely several megabytes of
            # mostly flat colour that the model and the chat both pay for.
            mime_type = "image/jpeg"
            document_height = int(metrics.get("document_height") or 0)
            if document_height > FULL_PAGE_MAX_CSS_HEIGHT:
                truncated = True
                options["clip"] = {
                    "x": 0,
                    "y": 0,
                    "width": max(1, int(metrics.get("document_width") or metrics.get("viewport_width") or 1)),
                    "height": FULL_PAGE_MAX_CSS_HEIGHT,
                }
            data = await _bounded(
                page.screenshot(type="jpeg", quality=80, full_page=True, **options),
                timeout + 1,
            )
        else:
            data = await _bounded(page.screenshot(type="png", full_page=False, **options), timeout + 1)
    except CaptureError:
        raise
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise CaptureError(
            f"The browser did not finish the picture within {timeout:g}s",
            error_kind="capture_timeout",
        ) from exc
    except Exception as exc:
        raise CaptureError(f"The browser could not capture the page: {exc}") from exc
    data = bytes(data or b"")
    if not data:
        raise CaptureError("The browser returned an empty picture")
    width, height = image_dimensions(data) or (0, 0)
    return PageCapture(
        data=data,
        mime_type=mime_type,
        image_width=width,
        image_height=height,
        metrics=metrics,
        masked_fields=masked,
        truncated=truncated,
    )


def encode_capture(capture: PageCapture) -> str:
    return base64.b64encode(capture.data).decode("ascii")


def capture_receipt(result: dict[str, Any]) -> str:
    """Model-facing text for a capture: what was taken and what it cannot do."""
    capture = result.get("capture") or {}
    purpose = str(result.get("purpose") or "inspect")
    scope = str(result.get("scope") or "viewport")
    what = {"viewport": "visible viewport", "full_page": "full page", "element": "element"}.get(scope, scope)
    target = result.get("url") or "the tab"
    if scope == "element" and result.get("ref"):
        what = f"element {result['ref']}"
    lines: list[str] = []
    if purpose == "evidence":
        saved = result.get("saved_path") or "the workspace"
        lines.append(
            f"Saved a picture of the {what} of {target} as evidence at {saved}. "
            "Nobody has been shown it yet: if the user wants to see it, call present_artifact "
            f"with path {saved}. It is not in your context and you have not seen it, so do not "
            "describe its contents beyond what your observations already established."
        )
    else:
        lines.append(
            f"Picture of the {what} of {target} attached for visual inspection. "
            "It carries no element refs or coordinates; to act on the page, use refs from browser_observe."
        )
    viewport = f"{capture.get('viewport_width', '?')}x{capture.get('viewport_height', '?')}"
    geometry = (
        f"Viewport at capture: {viewport} CSS px, device pixel ratio "
        f"{capture.get('device_pixel_ratio', 1)}, scrolled to y={capture.get('scroll_y', 0)} "
        f"of a {capture.get('document_height', '?')} px document"
    )
    if capture.get("geometry_revision") is not None:
        geometry += f", geometry revision {capture['geometry_revision']}"
    lines.append(geometry + ".")
    if capture.get("truncated"):
        lines.append(
            f"The page is taller than {FULL_PAGE_MAX_CSS_HEIGHT} px; only its top "
            f"{FULL_PAGE_MAX_CSS_HEIGHT} px were captured."
        )
    masked = int(capture.get("masked_fields") or 0)
    if masked:
        lines.append(f"{masked} password, payment or unchecked embedded region(s) were masked in solid magenta.")
    if capture.get("shared_with_person"):
        lines.append(
            "A person is using this browser window; its size can change at any moment, "
            "so treat this geometry as the size at capture only."
        )
    if not capture.get("refs_current", True):
        lines.append(
            "The page or window changed since your last observation; call browser_observe "
            "before acting."
        )
    return "\n".join(lines)


#: Workspace folder for evidence pictures. Under deliverables/ so the agent
#: can hand one to the user with present_artifact; nothing here is shown in
#: the chat until it does.
EVIDENCE_DIR = "deliverables/browser-evidence"


def save_evidence(
    session_id: str, data: bytes, mime_type: str, *, label: str = "", now: float | None = None,
) -> str:
    """Write an evidence picture into the session workspace; return its relative path."""
    import time
    import uuid

    from ascended_browser.runtime.sandbox import ensure_session_sandbox, workspace_root

    if not session_id:
        raise CaptureError("Evidence needs a chat session to be saved in", error_kind="workspace_unavailable")
    root = workspace_root(session_id) or ensure_session_sandbox(session_id)
    slug = re.sub(r"[^a-z0-9]+", "-", str(label or "page").lower()).strip("-")[:40] or "page"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    ext = "jpg" if mime_type == "image/jpeg" else "png"
    relative = f"{EVIDENCE_DIR}/{stamp}-{slug}-{uuid.uuid4().hex[:6]}.{ext}"
    target = root / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as exc:
        raise CaptureError(
            f"The evidence picture could not be saved to the workspace: {exc}",
            error_kind="workspace_unavailable",
        ) from exc
    return relative


def settle_capture_visibility(result: dict[str, Any], *, session_id: str | None) -> None:
    """Decide who sees a capture, once it has run.

    A capture is never published as a chat media card. The tool row gets small
    previews, the same way it shows an image the agent read. Evidence is then
    withheld from the model (it is already saved as a workspace file the agent
    may present); an inspect picture stays in ``images`` for the next model
    turn.
    """
    if not isinstance(result, dict) or not result.get("images"):
        return
    try:
        from ascended_browser.runtime.noop import materialize_tool_visual_previews

        result["_capture_previews"] = materialize_tool_visual_previews(
            result["images"], session_id=session_id,
        )
    except Exception:
        result["_capture_previews"] = []
    if result.get("visual_audience") == "user":
        result.pop("images", None)
