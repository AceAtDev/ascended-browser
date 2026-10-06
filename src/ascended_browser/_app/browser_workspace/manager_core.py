from __future__ import annotations

import asyncio
import collections
import copy
from datetime import date, datetime
import inspect
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from ascended_browser._app.browser_action_settle import ActionActivity
from ascended_browser._app.browser_workspace.auth_state import AuthStateStore
from ascended_browser._app.browser_workspace.backend import (
    BrowserBackend,
    BrowserBackendError,
    BrowserRuntimeUnavailable,
    PageCreationTicket,
    PersistentOwnerBackend,
    CONTROL_PREPARATION_TIMEOUT_SECONDS,
    owner_key,
    supports_dynamic_viewport,
)
from ascended_browser._app.browser_workspace.clocks import ParkingClocks
from ascended_browser._app.browser_workspace.models import Attention, TabHold, TabLease, TabRecord, WorkspaceRecord
from ascended_browser._app.browser_workspace.liveview import (
    LiveViewRegistry,
    LiveViewSession,
    LiveViewSupervisor,
    LiveViewUnavailable,
    RECONNECT_GRACE_SECONDS,
)
from ascended_browser._app.browser_workspace.memory_pressure import read_memory_pressure
from ascended_browser._app.browser_workspace.store import WorkspaceStore
from ascended_browser._app.browser_shadow_dom import SHADOW_DOM_JS, deep_inner_text
from ascended_browser.runtime.settings import get_setting
from ascended_browser.runtime.url_security import validate_public_http_url

log = logging.getLogger(__name__)

# Idle threshold (s) after which a sleeping, browser-less workspace record is
# dropped from the in-memory ``_records`` cache. The record stays on disk in
# ``WorkspaceStore`` and is rehydrated on the next ``record()`` access, so this
# only frees RAM that would otherwise be held for the process lifetime.
_WORKSPACE_EVICT_SECONDS = 60 * 60
# Memory-pressure tab sleep (see _relieve_memory_pressure). Polling is a few
# /proc reads; tabs used within the last minute are never released for it.
MEMORY_PRESSURE_POLL_SECONDS = 5
MEMORY_PRESSURE_MIN_IDLE_SECONDS = 60
MEMORY_PRESSURE_MAX_SLEEPS_PER_PASS = 4
MEMORY_PRESSURE_SETTLE_SECONDS = 0.5
# Rate-limit token window must match ``_rate_limit_domain`` (60s).
_DOMAIN_TOKEN_WINDOW = 60
# The browser client sends an authenticated WebSocket ping every 30 seconds.
# Treat three missed intervals as a half-open transport rather than relying on
# TCP close delivery, which is not dependable on a suspended browser or an
# interrupted mobile/Wi-Fi path.  This is deliberately independent of the
# reconnect grace: a clean socket close still gets the full grace period.
_LIVE_VIEW_CLIENT_LIVENESS_SECONDS = 90.0
# A cold mint rides the canonical browser wake for this long before answering
# "warming". Launches take ~2-4 s, so most first views are served by the same
# request instead of the editor's retry ladder (0.7/2.1/4.9/9.9 s), which left
# the viewer idle for up to five seconds after the browser was ready. Stays
# well inside the editor's 12 s mint deadline.
_LIVE_VIEW_WAKE_WAIT_SECONDS = 8.0
# A selected tab whose page must be revived or reloaded is waited on this long
# before the viewer is told "warming" and polls. The load keeps running in its
# own task, so a stale site that loads slowly is not cancelled and restarted by
# each poll. Fits inside the 8 s selection budget and the 12 s mint deadline.
_LIVE_VIEW_REVIVE_WAIT_SECONDS = 4.0
# Bound the entire native page-creation transaction, including admission and
# Browser.newPage. Playwright supplies no timeout for new_page itself.
_PAGE_CREATION_TIMEOUT_SECONDS = 10.0
# A wake that has waited this long for busy tabs may grow the owner past its
# soft tab limit (never past the hard limit) instead of waiting longer.
_TAB_ROOM_GROW_AFTER_SECONDS = 3.0
# A page Firefox stops answering for must not hold the tab's lock and the
# runtime barrier forever: close gives up on the reply after this long and
# forgets the page anyway.
_PAGE_CLOSE_TIMEOUT_SECONDS = 10.0

def _warm_browser_launch_imports() -> None:
    """Import the browser launch stack before the first launch needs it.

    Every import here is lazy at its call site, so the first browser launch
    of each process paid ~0.3 s (camoufox.utils, browserforge) synchronously
    on the event loop, stalling every other request with it.
    """
    import importlib

    for module in (
        "camoufox.utils", "camoufox.async_api", "camoufox.addons",
        "browserforge.fingerprints", "ascended_browser._app.browser_workspace.fingerprint",
    ):
        try:
            importlib.import_module(module)
        except Exception:
            log.debug("Could not pre-import %s", module, exc_info=True)


_LIVE_WHEEL_EXECUTOR = """({x,y,dx,dy}) => {
  const root = document.elementFromPoint(x, y);
  const scrollable = (element) => {
    if (!element || element === document.body || element === document.documentElement) return false;
    const style = getComputedStyle(element);
    const canX = dx && /(auto|scroll|overlay)/.test(style.overflowX)
      && element.scrollWidth > element.clientWidth;
    const canY = dy && /(auto|scroll|overlay)/.test(style.overflowY)
      && element.scrollHeight > element.clientHeight;
    return canX || canY;
  };
  let target = root;
  while (target && !scrollable(target)) {
    target = target.parentElement || target.getRootNode?.()?.host || null;
  }
  target = target || document.scrollingElement || document.documentElement;
  const beforeX = Number(target.scrollLeft || 0);
  const beforeY = Number(target.scrollTop || 0);
  if (dx) target.scrollLeft = beforeX + dx;
  if (dy) target.scrollTop = beforeY + dy;
  return Number(target.scrollLeft || 0) !== beforeX
    || Number(target.scrollTop || 0) !== beforeY;
}"""
_LIVE_WHEEL_INSTALL_JS = f"""() => {{
  if (typeof globalThis.__odysseusApplyLiveWheelV1 === 'function') return true;
  const apply = {_LIVE_WHEEL_EXECUTOR};
  Object.defineProperty(globalThis, '__odysseusApplyLiveWheelV1', {{
    value: apply, configurable: true, writable: false, enumerable: false,
  }});
  return true;
}}"""
_LIVE_WHEEL_INSTALL_AND_APPLY_JS = f"""arg => {{
  if (typeof globalThis.__odysseusApplyLiveWheelV1 !== 'function') {{
    const apply = {_LIVE_WHEEL_EXECUTOR};
    Object.defineProperty(globalThis, '__odysseusApplyLiveWheelV1', {{
      value: apply, configurable: true, writable: false, enumerable: false,
    }});
  }}
  return globalThis.__odysseusApplyLiveWheelV1(arg);
}}"""
_LIVE_WHEEL_APPLY_JS = """arg => (
  typeof globalThis.__odysseusApplyLiveWheelV1 === 'function'
    ? globalThis.__odysseusApplyLiveWheelV1(arg) : null
)"""

# A native Live View edit can arrive over either the Python signaling socket
# or the daemon's direct RTC input lane. Observe the resulting browser event
# instead of guessing from a pointer/key packet: a click on a checkbox or
# select then has the same durable draft protection as typed text, while mere
# motion and scrolling never do. No field value crosses the binding.
_LIVE_FORM_DIRTY_INSTALL_JS = """() => {
  if (globalThis.__odysseusLiveFormDirtyObserverV1) return true;
  let reported = false;
  let pending = false;
  const changed = (event) => {
    if (!event.isTrusted || reported || pending) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    const tag = target.tagName;
    if (event.type === 'input'
        ? !(tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable)
        : !(tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT')) return;
    const notify = globalThis.__odysseusLiveFormDirtyV1;
    if (typeof notify !== 'function') return;
    pending = true;
    Promise.resolve().then(() => notify()).then((accepted) => {
      if (accepted === true) reported = true;
    }).catch(() => {}).finally(() => { pending = false; });
  };
  document.addEventListener('input', changed, true);
  document.addEventListener('change', changed, true);
  Object.defineProperty(globalThis, '__odysseusLiveFormDirtyObserverV1', {
    value: true, configurable: false, enumerable: false,
  });
  return true;
}"""

# A resting still travels over the same link as the video. Lossless WebP keeps
# a text page exact at ~20-60 KB; photo-heavy pages run to ~500 KB lossless, so
# above this size a near-lossless lossy encode (~140 KB) takes its place.
_LIVE_STILL_LOSSLESS_MAX_BYTES = 200_000


def _encode_live_still(raw: bytes, width: int, height: int) -> tuple[bytes, str]:
    from io import BytesIO

    from PIL import Image

    image = Image.frombytes("RGB", (width, height), raw)
    buffer = BytesIO()
    # method 0 is ~4x larger on text; method 1 at quality 50 costs ~25 ms.
    image.save(buffer, format="WEBP", lossless=True, method=1, quality=50)
    if buffer.tell() > _LIVE_STILL_LOSSLESS_MAX_BYTES:
        buffer = BytesIO()
        image.save(buffer, format="WEBP", quality=90, method=0)
    return buffer.getvalue(), "image/webp"


def _browser_omnibox_target(raw: str) -> str:
    """Hand the address bar's text to the browser; the browser resolves it.

    The typed value is forwarded in one shape: a written URL (any scheme,
    including ``file://`` and ``about:`` pages) goes verbatim, a schemeless
    host powers the default https origin that the browsers resolve to,
    unqualified free text becomes a web search because a stream surface cannot
    open a browser omnibox. The manager decides only what Playwright can
    literally navigate; it never refuses a typed address.
    """
    target = str(raw or "").strip()
    if not target:
        raise WorkspaceError("Enter an address to navigate")

    def search_target(text: str) -> str:
        from urllib.parse import quote_plus

        return "https://duckduckgo.com/?q=" + quote_plus(text)

    if "://" in target:
        return target
    # Whitespace settles it before any scheme guess: no typed URL contains a
    # space, so "note: buy milk" is a search and never an opaque scheme.
    if " " in target:
        return search_target(target)
    # An opaque scheme carries no "//" (`about:`, `mailto:`) and is still the
    # browser's to resolve. A colon followed only by digits is not one: that
    # is a port on a schemeless host, so `localhost:3000` still takes https
    # while `about:config` goes verbatim.
    scheme = re.match(r"([A-Za-z][A-Za-z0-9+.\-]*):(.*)", target)
    if scheme and not scheme.group(2).split("/", 1)[0].isdigit():
        return target
    host = target.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host_without_port = host
    if ":" in host and not host.startswith("["):
        name, port = host.rsplit(":", 1)
        if port.isdigit():
            host_without_port = name
    if "." not in host_without_port and host_without_port.casefold() not in {
        "localhost", "localhost.localdomain",
    }:
        try:
            import ipaddress

            ipaddress.ip_address(host_without_port.strip("[]"))
        except ValueError:
            return search_target(target)
    # A dev server on this machine or the local network speaks plain http;
    # Firefox and Chrome resolve a typed `localhost:3000` the same way. Sending
    # https there fails the connection and the developer's own app never opens.
    from ascended_browser._app.browser_diagnostics import is_dev_origin

    if is_dev_origin("http://" + host):
        return "http://" + target
    return "https://" + target



class WorkspaceError(RuntimeError):
    #: Whether the page may have been changed before this was raised. A refusal
    #: that inspects the target and declines — the wrong verb for the control,
    #: an option no list offers — leaves the page exactly as it was found, and
    #: saying so lets a batch carry on with the fields that have nothing to do
    #: with it. Anything that may have touched the page leaves this True, which
    #: is why it is the default.
    dispatched = True


def _untouched(error: WorkspaceError) -> WorkspaceError:
    """Mark a refusal that inspected the target and changed nothing."""
    error.dispatched = False
    return error


class TabRoomExhausted(WorkspaceError):
    """Every resident tab is in use, so no page can be created right now."""

    error_kind = "tab_limit_busy"


class RuntimeTransitionRequested(WorkspaceError):
    """A safe repeated/navigation effect yielded to human/geometry authority."""

    error_kind = "browser_transition"


class BrowserViewportError(WorkspaceError):
    """Stable public failure classification for the agent viewport tool."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str,
        requires_new_runtime: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.requires_new_runtime = bool(requires_new_runtime)


class TabLifecycleError(WorkspaceError):
    """Classified failure for user-facing tab lifecycle operations.

    Closing or opening a tab is a lifecycle action with its own failure
    vocabulary (unknown target, conflicting authority, unavailable runtime);
    the interface needs those distinctions to stay truthful instead of
    collapsing every failure into one silent rejection.
    """

    def __init__(self, message: str, *, error_kind: str, failure_stage: str = "") -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.failure_stage = failure_stage


# Set while a parent tool batch reads several tabs at once (agent_loop per-tab
# waves). Reads then take only their tab's lock, exactly like a parallel
# browser_act, instead of the owner-wide scope that serialized them.
_PARALLEL_TAB_READ: ContextVar[bool] = ContextVar("browser_parallel_tab_read", default=False)


class _RuntimeEffectBarrier:
    """Allow parallel tab work while geometry/selection transitions drain it."""

    def __init__(self, label: str = "") -> None:
        self.label = label
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0
        self._waiting_priority_writers = 0
        self._transition_revision = 0

    @property
    def transition_pending(self) -> bool:
        """Whether takeover/handback/resize is waiting for current effects."""
        return self._writer or self._waiting_writers > 0

    def _log_slow_wait(self, kind: str, label: str, wait: float) -> None:
        """Attribute a stall to the shared barrier, not the effect that held it.

        Incident diagnosis (2026-09-16, session 57250c22): a browser action and
        the live view serialize on one owner barrier, so a slow effect is
        indistinguishable from a slow lock unless the wait itself is logged.
        """
        try:
            threshold = float(get_setting("browser_workspace_lock_slow_seconds", 2) or 2)
        except Exception:
            threshold = 2.0
        if wait < threshold:
            return
        log.warning(
            "browser effect barrier %s waited %.1fs (%s) readers=%d writer=%s "
            "waiting_writers=%d priority_writers=%d",
            kind, wait, label or self.label or "unknown", self._readers,
            self._writer, self._waiting_writers, self._waiting_priority_writers,
        )

    def _log_held(self, kind: str, label: str, held: float, wait: float) -> None:
        """Log how long one caller kept the barrier from everyone else.

        The wait log alone cannot see a lone caller that holds shared for a
        minute; the live view then hangs its create/switch on exclusive with no
        reader for anyone new to admit (the click in session 57250c22 held
        shared ~98s while every live-view start timed out client-side).
        """
        try:
            threshold = float(get_setting("browser_workspace_lock_slow_seconds", 2) or 2)
        except Exception:
            threshold = 2.0
        if held < threshold:
            return
        log.warning(
            "browser effect barrier %s held %.1fs (waited %.1fs) (%s) "
            "readers=%d writer=%s waiting_writers=%d",
            kind, held, wait, label or self.label or "unknown",
            self._readers, self._writer, self._waiting_writers,
        )

    async def wait_for_transition(self, timeout: float) -> bool:
        """Wait until a writer requests the runtime, bounded by ``timeout``.

        Repeating safe actions use this instead of an uninterruptible sleep so
        human takeover and resize do not sit behind a multi-minute scroll.
        """
        if timeout <= 0:
            return self.transition_pending
        async with self._condition:
            if self.transition_pending:
                return True
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self.transition_pending),
                    timeout=timeout,
                )
                return True
            except asyncio.TimeoutError:
                return False

    async def wait_until_quiet(self, *, quiet_seconds: float = 0.35,
                               timeout: float = 2.0) -> bool:
        """Coalesce a burst of live-view selection and viewport transitions.

        A yielded GET open can retry as soon as one writer exits, only to race
        the next writer queued by the newly mounted view. This waits for a
        bounded gap in authoritative transitions, without changing the
        three-attempt limit or holding a shared effect admission meanwhile.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        async with self._condition:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if self.transition_pending:
                    try:
                        await asyncio.wait_for(
                            self._condition.wait_for(lambda: not self.transition_pending),
                            timeout=remaining,
                        )
                    except asyncio.TimeoutError:
                        return False
                    continue
                revision = self._transition_revision
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(
                            lambda: self._transition_revision != revision,
                        ),
                        timeout=min(max(0.0, quiet_seconds), remaining),
                    )
                except asyncio.TimeoutError:
                    return time.monotonic() < deadline

    @asynccontextmanager
    async def shared(self, label: str = ""):
        started = time.monotonic()
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._writer and self._waiting_writers == 0,
            )
            self._log_slow_wait("shared", label, time.monotonic() - started)
            self._readers += 1
        held_started = time.monotonic()
        try:
            yield
        finally:
            self._log_held(
                "shared", label, time.monotonic() - held_started,
                held_started - started,
            )
            async with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @asynccontextmanager
    async def exclusive(self, *, priority: bool = False, label: str = ""):
        """Drain shared effects; priority transitions overtake queued sizing."""
        started = time.monotonic()
        async with self._condition:
            self._waiting_writers += 1
            self._transition_revision += 1
            if priority:
                self._waiting_priority_writers += 1
            # Wake cooperative repeated effects as soon as a human/geometry
            # transition queues, before waiting for those readers to drain.
            self._condition.notify_all()
            acquired = False
            try:
                await self._condition.wait_for(
                    lambda: (
                        not self._writer
                        and self._readers == 0
                        and (priority or self._waiting_priority_writers == 0)
                    ),
                )
                self._writer = True
                acquired = True
            finally:
                self._waiting_writers -= 1
                if priority:
                    self._waiting_priority_writers -= 1
                if not acquired:
                    self._condition.notify_all()
        held_started = time.monotonic()
        try:
            yield
        finally:
            self._log_held(
                "exclusive", label, time.monotonic() - held_started,
                held_started - started,
            )
            async with self._condition:
                self._writer = False
                self._transition_revision += 1
                self._condition.notify_all()


# Models often emit `type` (or a bare `action` string) instead of `kind`.
# Keep the canonical field as `kind`; these are accepted aliases only.
_ACTION_KIND_ALIASES = {
    "wait_for": "wait",
    "wait_until": "wait",
    "waitfor": "wait",
    "type": "fill",
    "input": "fill",
    "write": "fill",
    "goto": "navigate",
    "fillform": "fill_form",
    "fill_fields": "fill_form",
    "select_option": "select",
    "selectoption": "select",
    "choose": "select",
    "uncheck": "check",
    "go": "navigate",
    "open": "navigate",
    "file": "upload",
    "attach": "upload",
    "attach_file": "upload",
    "upload_file": "upload",
    "set_input_files": "upload",
}
# Live-view narration for whole browser tools that never move the pointer.
# browser_act narrates each action itself, so it is not listed.
_AGENT_CURSOR_TOOL_STATUS = {
    "browser_observe": "Looking at the page",
    "browser_extract": "Reading the page",
    "browser_screenshot": "Taking a screenshot",
    "browser_evaluate": "Running a script on the page",
    "browser_open": "Opening a page",
    "browser_wait": "Waiting for the page",
    "browser_login": "Signing in",
}


def _agent_cursor_navigate_caption(url: str) -> str:
    """`Opening example.com` for the live-view watcher."""
    host = ""
    try:
        host = urlparse(str(url or "")).hostname or ""
    except ValueError:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    return f"Opening {host}"[:64] if host else "Opening a page"


def _agent_cursor_control_caption(kind: str, action: dict[str, Any]) -> str:
    """What a watcher reads while the agent works one control."""
    from ascended_browser._app.browser_agent_cursor import caption

    label = action.get("label") or action.get("name") or ""
    value = action.get("value")
    if kind == "select":
        choice = value if isinstance(value, str) else action.get("option") or ""
        if isinstance(choice, str) and choice.strip():
            return caption("Choosing", choice)
        return caption("Choosing in", label)
    if kind in {"check", "checkbox", "radio"}:
        checked = action.get("checked", value)
        verb = "Unchecking" if checked is False or str(checked).lower() == "false" else "Checking"
        if kind == "radio":
            verb = "Choosing"
        return caption(verb, label)
    if kind == "date":
        return caption("Setting date in", label)
    return caption("Typing in", label)


def _agent_cursor_upload_caption(paths: Any) -> str:
    from ascended_browser._app.browser_agent_cursor import caption

    names = [Path(str(path)).name for path in (paths or []) if str(path or "").strip()]
    if not names:
        return "Uploading a file"
    extra = f"+{len(names) - 1}" if len(names) > 1 else ""
    return caption("Uploading", names[0], extra=extra)


_SUPPORTED_ACTION_KINDS = frozenset({
    "navigate", "click", "fill", "press", "fill_form", "select", "check", "upload",
    "scroll", "wait",
})
# Per-field kinds a fill_form batch may carry. Mirrors the `fields[].kind` enum
# in tool_schemas; anything outside it (navigate/click/press/scroll) means a
# misrouted list is a real `sequence`, not a repairable form batch.
_FILL_FORM_FIELD_KINDS = frozenset({"fill", "select", "check", "date"})


def describe_exception(exc: BaseException, *, limit: int = 400) -> str:
    """Render an exception so it always says something.

    Playwright timeouts frequently carry an empty ``str(exc)``, which rendered
    as a bare ``TimeoutError:`` — a failure with no content at all. Fall back to
    the type name and, where one exists, the stage the control reached.
    """
    text = " ".join(str(exc or "").split())[:limit]
    if text:
        return f"{type(exc).__name__}: {text}"
    stage = str(getattr(exc, "failed_stage", "") or "")
    if stage:
        return f"{type(exc).__name__} with no detail (failed at {stage})"
    return (
        f"{type(exc).__name__} with no detail — the control did not report why; "
        "observe the tab to see its current state"
    )


def _failure_evidence(exc: BaseException) -> dict[str, Any]:
    """Structured evidence an exception captured at the moment it failed.

    Some page state exists only while an action is mid-flight — an open
    combobox popup closes as the failure unwinds — so the control records it on
    the exception. Error prose is truncated for the model envelope; this keeps
    that evidence addressable instead of losing it to a character budget.
    """
    evidence: dict[str, Any] = {}
    options = getattr(exc, "observed_options", None)
    if isinstance(options, list) and options:
        evidence["observed_options"] = [str(item)[:80] for item in options[:12]]
    strategy = str(getattr(exc, "popup_strategy", "") or "")
    if strategy:
        evidence["popup_strategy"] = strategy
    # Why the target could not take the click, read while it still stood
    # (src/browser_inspect.py): what covers it, which container clips it.
    inspection = getattr(exc, "inspect", None)
    if isinstance(inspection, dict) and inspection:
        evidence["inspect"] = inspection
    return evidence


def _unmet_postcondition_detail(
    expectation: dict, *, status_events: list[dict], changed: bool,
) -> str:
    """Say which requested postcondition was missing and what did happen."""
    wanted = "; ".join(
        f"{key}={str(expectation.get(key))[:120]!r}"
        for key in ("text", "text_gone", "url_contains") if expectation.get(key)
    )
    if status_events:
        seen = f"the page reported {str(status_events[-1].get('text') or '')[:160]!r} instead"
    elif changed:
        seen = "the page changed, but not into the requested state"
    else:
        seen = "no page change was observed"
    return (
        f"Click dispatched but the requested postcondition ({wanted}) was not observed; "
        f"{seen}. The click may still have had an effect: observe before retrying."
    )


_ACTION_FIELD_KEYS = (
    "kind", "type", "action", "ref", "url", "value", "text", "content",
    "key", "keys", "label", "query", "option", "observation_id", "fields", "checked",
    "steps", "expect_before", "expect_after", "depends_on", "id",
    "direction", "delta_y", "interval_ms", "duration_seconds",
    "reasoning", "expect",
    # What the action waits for before reporting (src/browser_wait_conditions.py).
    "until",
    # upload only. Every spelling a model reaches for is folded to `paths` by
    # normalize_browser_action.
    "paths", "path", "file_path", "files", "file", "upload_ids",
)


#: Playwright key names are case-sensitive and specific; models reliably send
#: "CTRL", "esc", "ENTER". Rejecting those with `Unknown key: "CTRL"` fails a
#: step for a spelling difference we can simply absorb.
_KEY_ALIASES = {
    "ctrl": "Control", "control": "Control", "cmd": "Meta", "command": "Meta",
    "meta": "Meta", "win": "Meta", "super": "Meta", "alt": "Alt",
    "option": "Alt", "opt": "Alt", "shift": "Shift",
    "esc": "Escape", "escape": "Escape", "enter": "Enter", "return": "Enter",
    "tab": "Tab", "space": "Space", "spacebar": "Space",
    "backspace": "Backspace", "delete": "Delete", "del": "Delete",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
    "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End",
    "insert": "Insert",
}


def _typed_text(key: Any) -> str:
    """A press whose `key` is plain text to type, e.g. "2023"; else "".

    Playwright rejects "2023" as an unknown key. A single chord piece that is
    neither a named key nor F1-F12 can only mean the characters themselves.
    """
    raw = str(key or "").strip()
    if len(raw) < 2 or "+" in raw:
        return ""
    low = raw.lower()
    if low in _KEY_ALIASES or re.fullmatch(r"f\d{1,2}", low):
        return ""
    if raw[:1].isupper() and raw[1:].isalpha() and not raw.isupper():
        return ""  # a named key the alias table lacks (e.g. "CapsLock")
    return raw


def _normalize_key(key: Any) -> str:
    """Map a model's key spelling onto Playwright's, chord by chord."""
    raw = str(key or "").strip() or "Enter"
    parts = [piece for piece in re.split(r"\s*\+\s*", raw) if piece]
    resolved = []
    for piece in parts:
        low = piece.lower()
        if low in _KEY_ALIASES:
            resolved.append(_KEY_ALIASES[low])
        elif len(piece) == 1:
            resolved.append(piece)  # a literal character key
        elif re.fullmatch(r"f\d{1,2}", low):
            resolved.append(piece.upper())  # F1..F12
        else:
            resolved.append(piece[:1].upper() + piece[1:])
    return "+".join(resolved) or "Enter"


#: Element fields the model can act on. Everything else in the raw snapshot is
#: either internal plumbing (four different selector spellings, frame bookkeeping,
#: bbox/center) or duplicated elsewhere.
_ELEMENT_KEEP = (
    "ref", "id", "role", "tag", "type", "text", "label", "placeholder", "aria_label",
    "value", "selected_text", "selected_options", "checked", "aria_checked", "disabled", "required", "options",
    "aria_controls", "aria_expanded", "aria_activedescendant", "aria_autocomplete",
    "controlled_regions", "containing_region",
    "href", "in_form", "context", "nearby", "invalid", "error", "visible", "name", "frame_index",
    "frame_url", "frame_name", "frame_scope", "observation_id",
)


#: Roles whose tag adds nothing — `role: "link"` already implies `tag: "a"`.
_IMPLIED_TAG = {"link": "a", "button": "button", "textbox": "input",
                "combobox": "select", "checkbox": "input", "radio": "input"}
#: Controls a form is filled through. Ranked ahead of navigation chrome so a
#: form is never the part that gets cut off.
_FORM_ROLES = {"textbox", "combobox", "checkbox", "radio", "file", "button"}
#: Kinds after which the page is likely different, so the result carries the new
#: state. A composite select can close a portal, reveal conditional questions,
#: and re-render the form just like a click. Plain `fill`/`check` usually leave
#: the page where it was, and a snapshot per keystroke-equivalent would cost
#: more than it saves.
_RESTATE_AFTER = frozenset({"click", "navigate", "press", "select", "fill_form", "scroll", "wait"})
# Kinds whose result always needs the page re-read: what is on screen is the
# answer (scroll, wait) or the page is new by definition (navigate).
_RESTATE_ALWAYS = frozenset({"scroll", "wait", "navigate"})
# Kinds that normally leave the page as it was. Their result carries the page
# only when the visible text moved (a validation message, a revealed section,
# a redirect) or when the action failed; otherwise it says `page_unchanged`.
_RESTATE_WHEN_CHANGED = frozenset({"fill", "check", "upload"})


#: Element fields whose change makes an element worth showing again.
_DELTA_STATE_KEYS = (
    "text", "label", "value", "selected_text", "selected_options", "checked", "aria_checked",
    "disabled", "aria_expanded", "invalid", "error", "options", "visible",
)
#: Share of a page's elements that may change before the whole page is shown
#: instead of a delta (agent-browser's rule: a big delta costs more than it saves).
_DELTA_FULL_PAGE_SHARE = 0.7


def _delta_projection(previous: dict | None, current: dict) -> dict | None:
    """An action's page as what changed since the model last saw it.

    Act results attached the whole page projection to every action: 63% of
    all browser bytes in the 2026-10-04 sessions (median 13-14K chars per act)
    while the model already held most of it. Same URL, a previous projection
    and a modest change: show the elements that are new (fresh refs, such as
    an opened listbox or submenu) or whose state changed, and say how many are
    unchanged. Returns None when the whole page should be shown.
    """
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return None
    if str(previous.get("url") or "") != str(current.get("url") or ""):
        return None
    before = {str(e.get("ref")): e for e in previous.get("elements") or [] if isinstance(e, dict) and e.get("ref")}
    after = [e for e in current.get("elements") or [] if isinstance(e, dict)]
    if not before or not after:
        return None
    changed = []
    for element in after:
        old = before.get(str(element.get("ref") or ""))
        if old is None or any(old.get(key) != element.get(key) for key in _DELTA_STATE_KEYS):
            changed.append(element)
    if len(changed) >= _DELTA_FULL_PAGE_SHARE * len(after):
        return None
    current_refs = {str(e.get("ref")) for e in after if e.get("ref")}
    removed = [
        str(e.get("label") or e.get("text") or e.get("role") or "")[:80]
        for ref, e in before.items() if ref not in current_refs
    ]
    delta = dict(current)
    delta["elements"] = changed
    if str(previous.get("text") or "") == str(current.get("text") or ""):
        delta.pop("text", None)
    # Readable content blocks follow the same rule: a block already shown,
    # with the same text, is not shown again.
    shown_blocks = {
        (str(b.get("ref") or ""), json.dumps(b, sort_keys=True, default=str))
        for b in previous.get("content_blocks") or [] if isinstance(b, dict)
    }
    blocks = [b for b in current.get("content_blocks") or [] if isinstance(b, dict)]
    if blocks:
        fresh = [b for b in blocks
                 if (str(b.get("ref") or ""), json.dumps(b, sort_keys=True, default=str)) not in shown_blocks]
        delta["content_blocks"] = fresh
        unchanged_blocks = len(blocks) - len(fresh)
    delta["delta"] = {
        "shown": "new or changed elements only",
        "changed_count": len(changed),
        "unchanged_count": len(after) - len(changed),
        "unchanged_content_blocks": unchanged_blocks if blocks else 0,
        "removed": [label for label in removed if label][:20],
        "note": (
            "Only elements that are new or changed since your last look at this page are "
            "listed; the refs you already have for it still apply. Observe for the whole page."
        ),
    }
    return delta


def _compact_element(element: dict) -> dict:
    """One element, reduced to what a model needs to choose and act on it."""
    out: dict[str, Any] = {}
    for key in _ELEMENT_KEEP:
        value = element.get(key)
        if value is None or value == "" or value == []:
            continue
        if key == "options":
            out[key] = [
                str(option.get("text") or "")[:60]
                for option in value[:40] if isinstance(option, dict)
            ]
        elif key == "controlled_regions":
            out[key] = [
                {
                    field: (
                        str(region.get(field) or "")[:240 if field == "text" else 120]
                        if field != "visible" else bool(region.get(field))
                    )
                    for field in ("id", "role", "label", "text", "visible")
                    if region.get(field) not in (None, "")
                }
                for region in value[:4] if isinstance(region, dict)
            ]
        elif key == "containing_region" and isinstance(value, dict):
            out[key] = {
                field: (
                    str(value.get(field) or "")[:240 if field == "text" else 120]
                    if field != "visible" else bool(value.get(field))
                )
                for field in ("id", "role", "label", "text", "visible")
                if value.get(field) not in (None, "")
            }
        elif key in {"ref", "id", "name", "href", "frame_url", "frame_name", "observation_id"}:
            # These are identities, not prose. A prefix is a different target.
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:120 if key != "context" else 60]
        else:
            out[key] = value
    role = str(out.get("role") or "")
    if _IMPLIED_TAG.get(role) == out.get("tag"):
        out.pop("tag", None)
    # A label/aria-label that just repeats the visible text is pure duplication.
    for key in ("aria_label", "label"):
        if out.get(key) and out[key] == out.get("text"):
            out.pop(key)
    # Visibility is carried by ordering; only the exception is worth a field.
    if out.pop("visible", None) is not True:
        out["hidden"] = True
    return out


_CHECKABLE_ROLES = frozenset({"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"})
_DIALOG_ROLES = frozenset({"dialog", "alertdialog"})


def _box(value: object) -> tuple[float, float, float, float] | None:
    """(left, top, right, bottom) from a collector bbox, or None if unusable."""
    if not isinstance(value, dict):
        return None
    try:
        x, y = float(value["x"]), float(value["y"])
        return x, y, x + float(value["width"]), y + float(value["height"])
    except (KeyError, TypeError, ValueError):
        return None


def _point(value: object) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        return float(value["x"]), float(value["y"])
    except (KeyError, TypeError, ValueError):
        return None
#: Flags whose ``False`` is the default state and therefore carries nothing.
#: ``checked: False`` stays on checkable controls, where unchecked is the fact.
_DEFAULT_FALSE_FLAGS = ("disabled", "required", "in_form", "invalid")


def _short_id(prefix: str, identity: object, taken: dict[str, str]) -> str:
    """Deterministic short id for a region or frame, stable across observations.

    Deterministic so ``browser_observe {within: "r1a2b"}`` names the same region
    on the next call; widened on the rare prefix collision.
    """
    import hashlib

    key = json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    for width in (4, 6, 10, 40):
        candidate = prefix + digest[:width]
        if taken.get(candidate, key) == key:
            taken[candidate] = key
            return candidate
    return prefix + digest


def _frame_context_url(value: object) -> str:
    """Origin and path of a frame URL, without its query string or fragment."""
    from urllib.parse import urlsplit

    raw = str(value or "").strip()
    if not raw:
        return ""
    # Only the query and fragment go; origin and path stay exact, like every
    # other identity-bearing value in the projection.
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.split("?", 1)[0].split("#", 1)[0]
    if not parts.scheme or not parts.netloc:
        return raw.split("?", 1)[0].split("#", 1)[0]
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _region_entry(region: dict) -> dict:
    entry = {"role": str(region.get("role") or "region")}
    label = str(region.get("label") or region.get("text") or "").strip()
    if label:
        entry["label"] = label[:80]
    if region.get("id"):
        # Bounded like the per-element region fields it replaces; the model
        # scopes to a region by its short id, never by this DOM id.
        entry["id"] = str(region["id"])[:120]
    if region.get("visible") is False:
        entry["hidden"] = True
    return entry


def _group_shared_context(
    compact: dict, regions: dict, frames: dict, taken: dict[str, str],
) -> dict:
    """State a containing region and a non-main frame once, by reference.

    ``containing_region`` repeated a region's label and up to 240 characters of
    its text on every element inside it: 49-57% of the observation budget on
    region-heavy pages. The element keeps a short ``region`` id; the region is
    described once in the projection's ``regions`` table. The same for the
    popups a control owns (``controls``) and for iframe context (``frame``).
    """
    out = dict(compact)
    region = out.pop("containing_region", None)
    if isinstance(region, dict):
        entry = _region_entry(region)
        rid = _short_id("r", entry, taken)
        regions.setdefault(rid, entry)
        out["region"] = rid
    controlled = out.pop("controlled_regions", None)
    if isinstance(controlled, list) and controlled:
        ids = []
        for owned in controlled:
            if isinstance(owned, dict):
                entry = _region_entry(owned)
                rid = _short_id("r", entry, taken)
                regions.setdefault(rid, entry)
                ids.append(rid)
        if ids:
            out["controls"] = ids
    if any(key in out for key in ("frame_url", "frame_name", "frame_index")):
        # The frame is context for the model, which addresses elements by ref;
        # the registry keeps the exact frame identity for resolution. An
        # iframe's query string is where client ids, nonces and session tokens
        # live — a Google sign-in frame carried 473 characters of it, more than
        # the element itself — so the model sees origin and path only.
        entry = {}
        url = _frame_context_url(out.get("frame_url"))
        if url:
            entry["url"] = url
        if out.get("frame_name"):
            entry["name"] = str(out["frame_name"])
        fid = f"f{out['frame_index']}" if isinstance(out.get("frame_index"), int) else _short_id("f", entry, taken)
        frames.setdefault(fid, entry)
        out["frame"] = fid
        for key in ("frame_url", "frame_name", "frame_index", "frame_scope"):
            out.pop(key, None)
    for flag in _DEFAULT_FALSE_FLAGS:
        if out.get(flag) is False:
            out.pop(flag)
    if out.get("checked") is False and str(out.get("role") or "").casefold() not in _CHECKABLE_ROLES:
        out.pop("checked")
    return out


def _query_tokens(query: object) -> list[str]:
    return [token for token in re.split(r"[^\w@.+#-]+", str(query or "").casefold()) if token]


def _matches_query(raw: dict, compact: dict, tokens: list[str]) -> bool:
    """Every word of the query appears in what the element says about itself."""
    options = raw.get("options") if isinstance(raw.get("options"), list) else []
    haystack = " ".join([
        str(raw.get(key) or "") for key in (
            "role", "tag", "type", "text", "label", "aria_label", "name", "placeholder",
            "value", "selected_text", "context", "nearby", "href",
            # Author ids name fields on most forms ("resume", "first_name"):
            # "Attach resume" missed Greenhouse's #resume input, labelled "Attach".
            "id",
        )
    ] + [str(o.get("text") if isinstance(o, dict) else o) for o in options]).casefold()
    return all(token in haystack for token in tokens)


_BLUR_FOCUSED_FIELD = """() => {
  const el = document.activeElement;
  if (!el || el === document.body || !el.matches('input, textarea, select, [contenteditable="true"]')) return false;
  // Leave a typeahead alone: blurring closes the suggestions it is showing.
  if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete')
      || el.getAttribute('aria-controls') || el.getAttribute('aria-haspopup')) return false;
  el.blur();
  return true;
}"""

_READ_INVALID_FIELDS = """() => Array.from(document.querySelectorAll('[aria-invalid="true"]'))
  .filter(el => el.getClientRects().length).slice(0, 6).map(el => {
    const ids = [el.getAttribute('aria-errormessage'), el.getAttribute('aria-describedby')]
      .join(' ').trim().split(/\\s+/).filter(Boolean);
    const message = ids.map(id => (document.getElementById(id) || {}).innerText || '')
      .join(' ').replace(/\\s+/g, ' ').trim().slice(0, 200);
    const label = String((el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label')
      || el.getAttribute('name') || el.id || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
    return {field: label, message};
  })"""


async def _leave_field_and_read_invalid(page: Any) -> list[dict[str, Any]]:
    """Leave the field just set, as tabbing out would, and report rejections.

    Validation commonly runs on blur (Workday's phone check did), and a fill
    does not blur. Reported by ARIA state (aria-invalid) with the message the
    field points at — never by guessing from page wording.
    """
    frames = [page] + [f for f in (getattr(page, "frames", []) or [])[1:6]]
    try:
        blurred = any([bool(await _bounded(frame.evaluate(_BLUR_FOCUSED_FIELD), 1.0)) for frame in frames])
    except Exception:
        blurred = False
    found: list[dict[str, Any]] = []
    for attempt in range(3 if blurred else 1):
        if attempt or blurred:
            await asyncio.sleep(0.4)  # blur validation may ask the server
        found = []
        for frame in frames:
            try:
                found += list(await _bounded(frame.evaluate(_READ_INVALID_FIELDS), 1.0) or [])
            except Exception:
                continue
        if found:
            break
    return found[:6]


def _page_key(url: Any) -> str:
    """A page's address without query or fragment, for same-page checks."""
    parsed = urlparse(str(url or ""))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/") if parsed.netloc else ""


#: How much page text the action in flight asked to read back, if it asked.
_READ_TEXT_CHARS: ContextVar[int | None] = ContextVar(
    "browser_workspace_read_text_chars", default=None,
)


def _read_budget() -> int:
    """The text budget an in-flight `read` asked for, or 0 for the default."""
    return int(_READ_TEXT_CHARS.get() or 0)


def _attach_still_loading(result: dict, navigation_state: dict | None) -> None:
    """Say so when an open returned at its settle cap with requests in flight."""
    if isinstance(navigation_state, dict) and navigation_state.get("state") == "still_loading":
        from ascended_browser._app.browser_action_settle import still_loading_note

        result["navigation_state"] = {
            **navigation_state, "next_step": still_loading_note(navigation_state),
        }


def read_text_budget(requested: Any) -> int:
    """Normalise a `read` flag into a character budget.

    ``True`` takes the configured page-read size; a number asks for that many
    characters, clamped so one call cannot swallow the whole result window.
    """
    if requested is True:
        return int(get_setting("browser_workspace_read_text_chars", 6000) or 6000)
    if isinstance(requested, (int, float)) and not isinstance(requested, bool):
        return max(0, min(int(requested), 40000))
    return 0


def _passages_for(text: str, find: str, *, window: int = 220, limit: int = 12) -> list[dict]:
    """Every place a page mentions something, with the words around it."""
    # Search and quote the same string: a page's newlines are not the reader's,
    # and folding one but slicing the other would quote the wrong words.
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    needle = re.sub(r"\s+", " ", str(find or "")).strip().casefold()
    hay = flat.casefold()
    if not needle or not hay:
        return []
    passages: list[dict] = []
    start = 0
    while len(passages) < limit:
        at = hay.find(needle, start)
        if at < 0:
            break
        left = max(0, at - window)
        right = min(len(flat), at + len(needle) + window)
        passages.append({
            "at": at,
            "text": ("…" if left else "") + flat[left:right].strip() + ("…" if right < len(flat) else ""),
        })
        start = at + len(needle)
    return passages


def _fold_text(value: Any) -> str:
    """Text compared the way a reader sees it: one space, no case."""
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


_STATE_SUFFIX = re.compile(r"\s+(?:not\s+)?(?:checked|selected)$")
#: The required-field marker a label carries ("Zip/Postal Code *"), which the
#: model drops when it names the field.
_REQUIRED_MARK = re.compile(r"\s*[*∗]+$")


def _state_free_name(value: Any) -> str:
    """A name without the state some widgets append ("Reddit not checked")."""
    folded = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return _REQUIRED_MARK.sub("", _STATE_SUFFIX.sub("", folded))


_POPUP_ITEM_ROLES = frozenset({"option", "treeitem"})
_MENU_ITEM_ROLES = frozenset({"menuitem", "menuitemcheckbox", "menuitemradio"})
_CONTAINER_ROLES = frozenset({"group", "region", "list", "listitem", "article", "section"})
_DECORATIVE_ROLES = frozenset({"", "presentation", "none", "generic", "span", "div", "img", "svg"})
_NAME_KEYS = ("text", "label", "aria_label", "placeholder", "title", "value", "selected_text")


def _actionability_tier(element: dict) -> int:
    """0 a control, 1 a container, 2 a nameless decoration.

    Playwright's ARIA snapshot has no presentation nodes at all. Here they are
    still collected (a ref must resolve if the model names one) but ranked last,
    so an empty svg never takes a slot from a field or an option.
    """
    role = str(element.get("role") or "").casefold()
    named = any(str(element.get(key) or "").strip() for key in _NAME_KEYS)
    if role in _DECORATIVE_ROLES and not named:
        return 2
    if role in _CONTAINER_ROLES or (role in {"link"} and not named):
        return 1
    return 0


def _closest_matches(prepared: list[tuple[dict, dict]], tokens: list[str]) -> list[tuple[dict, dict]]:
    """Elements matching the rarest query words, best first; at most 30.

    A word each element carries ("option", "select") says little; a word only
    one element carries ("reddit") says which one. Keep what scores at least
    half of the best.
    """
    hits = [[t for t in tokens if _matches_query(r, c, [t])] for r, c in prepared]
    frequency = {t: sum(t in words for words in hits) for t in tokens}
    scores = [sum(1.0 / frequency[t] for t in words) for words in hits]
    best = max(scores, default=0.0)
    if best <= 0:
        return []
    ranked = sorted(
        (index for index, score in enumerate(scores) if score >= best / 2),
        key=lambda index: -scores[index],
    )
    return [prepared[index] for index in ranked[:30]]


def _project_links(
    snapshot: dict, prepared: list, view: dict, *, text: str = "", scope_note: str = "",
) -> dict:
    """Where this page can take you: one line per destination.

    A destination needs a name and a URL, not a control's roles, regions and
    states, so this fits a whole index in one answer where the ordinary
    projection fits a screenful. Refs are the same refs, so a link here can be
    clicked without observing again; `query` still applies, and the URL is
    matched too, which is how a caller asks for one section of a site.
    """
    by_href: dict[str, dict[str, Any]] = {}
    for raw, compact in prepared:
        href = str(compact.get("href") or raw.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "#")):
            continue
        name = " ".join(str(
            compact.get("text") or compact.get("label") or compact.get("aria_label")
            or raw.get("text") or ""
        ).split())[:120]
        # A destination is a URL. A card that links its image and its title to
        # the same page is one place to go, and listing it twice halved how
        # much of an index fitted in one answer. Keep the fuller name.
        entry = by_href.get(href)
        if entry is None:
            entry = {"ref": compact.get("ref"), "href": href}
            if compact.get("frame"):
                entry["frame"] = compact["frame"]
            by_href[href] = entry
        if name and len(name) > len(str(entry.get("text") or "")):
            entry["text"] = name
    links = list(by_href.values())

    try:
        offset = max(0, int(str(view.get("cursor") or "c0").lstrip("c") or 0))
    except ValueError:
        offset = 0
    budget = int(get_setting("browser_workspace_observe_links_max_chars", 12000) or 12000)
    shown: list[dict[str, Any]] = []
    spent = 0
    stopped_at: int | None = None
    for index, entry in enumerate(links[offset:], start=offset):
        cost = len(json.dumps(entry)) + 8
        if spent + cost > budget:
            stopped_at = index
            break
        shown.append(entry)
        spent += cost
    projected: dict[str, Any] = {
        "url": snapshot.get("url"),
        "title": snapshot.get("title"),
        "observation_id": str(snapshot.get("observation_id") or ""),
        "links": shown,
        "link_count": len(links),
        "links_complete": len(shown) + offset == len(links),
    }
    if len(shown) + offset < len(links):
        projected["links_omitted"] = len(links) - len(shown) - offset
        resume = stopped_at if stopped_at is not None else offset + len(shown)
        projected["next_cursor"] = f"c{resume}"
        projected["note"] = (
            f"Showing links {offset + 1}-{offset + len(shown)} of {len(links)}. Continue "
            f'with cursor: "c{resume}", or narrow with query (words in the link text or '
            f"its URL)."
        )
    elif scope_note:
        projected["note"] = scope_note
    if view:
        projected["view"] = {k: v for k, v in view.items() if not str(k).startswith("_")}
    for key in ("workspace_id", "tab_id"):
        if key in snapshot:
            projected[key] = snapshot[key]
    if text:
        projected["text"] = text
    return projected


def _project_snapshot(snapshot: dict, *, text: str = "", view: dict | None = None) -> dict:
    """Shrink a raw page snapshot to what actually reaches the model.

    The raw snapshot measured 416,593 characters on one ordinary docs page, of
    which the tool-result formatter shows the first ~8,000. Those 8,000 were
    entirely `accessibility_snapshot` — a wall of navigation text — so **not one
    element ref was ever visible**. The model could only guess at refs, which is
    exactly what it did ("Try it", "bw-1", "5c31c1ee5b2"), and every guess cost
    a full click timeout to reject.

    Keep current viewport controls and bounded reading blocks together; complete
    reads and continuation remain `browser_extract`'s job.

    ``view`` narrows what is projected — ``query`` (every word must match),
    ``within`` (a region id, frame id, or element ref), ``cursor`` (continue past
    what an earlier response showed) and ``filter`` (``all`` adds headings).
    Every ref it returns belongs to this observation and resolves through the
    registry like any other. See browser-observation-system-design.md.
    """
    from ascended_browser._app.browser_observation_outline import (
        OutlineLayout, group_context_enabled, observe_format, outline_budget,
    )

    outline = observe_format() == "outline"
    grouped = group_context_enabled() or outline
    view = {k: v for k, v in (view or {}).items() if v not in (None, "")}
    within_refs = set(view.pop("_within_refs", None) or ())
    headings = view.pop("_headings", None)

    elements = [e for e in (snapshot.get("elements") or []) if isinstance(e, dict)]
    fresh = {str(ref) for ref in snapshot.get("_new_refs") or ()}
    regions: dict[str, dict] = {}
    frames: dict[str, dict] = {}
    taken: dict[str, str] = {}
    prepared: list[tuple[dict, dict]] = []
    for raw in elements:
        compact = _compact_element(raw)
        # The enclosing snapshot already identifies the generation and main
        # document. Repeating them on every field can crowd the last visible
        # controls out of a tall viewport. Full identities stay in the registry.
        if compact.get("observation_id") == snapshot.get("observation_id"):
            compact.pop("observation_id", None)
        if compact.get("frame_scope") == "main":
            for key in ("frame_index", "frame_scope", "frame_url", "frame_name"):
                compact.pop(key, None)
        if grouped:
            compact = _group_shared_context(compact, regions, frames, taken)
        if outline and raw.get("in_viewport"):
            compact["on_screen"] = True
        if str(raw.get("ref") or "") in fresh:
            compact["new"] = True
        prepared.append((raw, compact))

    # Narrow first, so budget, ranking and coverage describe what was asked for.
    scope_note = ""
    within = str(view.get("within") or "").strip()
    if within:
        if within in regions:
            members = [(r, c) for r, c in prepared if c.get("region") == within]
            if members:
                prepared = members
            else:
                # A region can be known only because a control owns it — a
                # closed popup or collapsed panel. Scoping to it must not come
                # back empty and silent: return the owning control and say how
                # to reveal what it holds.
                owners = [(r, c) for r, c in prepared if within in (c.get("controls") or [])]
                prepared = owners
                label = regions[within].get("label") or regions[within].get("role") or within
                scope_note = (
                    f"Region {within} ({label[:60]}) has no observed controls inside it"
                    + (f"; it belongs to {', '.join(str(c.get('ref')) for _, c in owners[:3])}. "
                       "Open or expand that control, then observe again."
                       if owners else ".")
                )
        elif within in frames:
            prepared = [(r, c) for r, c in prepared if c.get("frame") == within]
        elif within_refs:
            prepared = [(r, c) for r, c in prepared
                        if str(r.get("ref") or "") in within_refs or r.get("ref") == within]
        else:
            prepared = []
            known = ", ".join(f"{rid} ({regions[rid].get('label') or regions[rid]['role']})"
                              for rid in list(regions)[:8])
            scope_note = (f"No region, frame or element {within!r} in this observation. "
                          + (f"Regions here: {known}." if known else "This page exposes no regions."))
    tokens = _query_tokens(view.get("query"))
    if tokens:
        exact = [(r, c) for r, c in prepared if _matches_query(r, c, tokens)]
        if exact:
            prepared = exact
        else:
            # Models write descriptive queries ("Reddit TikTok dependent
            # subcategory checkbox"); requiring every word returned nothing and
            # cost a round trip each time. Rank by the rarer words matched.
            closest = _closest_matches(prepared, tokens)
            prepared = closest
            if closest and not scope_note:
                scope_note = (f"No control matches every word of {view.get('query')!r}; "
                              f"showing the {len(closest)} closest.")
        if not prepared and not scope_note:
            scope_note = (f"Nothing on this page matches {view.get('query')!r}. The query "
                          "matches words in each control's role, label, text, value and options.")

    if str(view.get("filter") or "") == "links":
        # Where to go next is a different question from what to operate, and it
        # has a much cheaper answer: a destination is a name and a URL. Asked
        # for the controls, a 152-link index answered with 23 and a cursor
        # worth six more round trips, so live runs harvested links with
        # browser_evaluate instead.
        return _project_links(snapshot, prepared, view, text=text, scope_note=scope_note)

    cap = int(get_setting("browser_workspace_observe_max_elements", 150) or 150)
    if outline:
        cap = max(cap, 400)
    # Rank by what the next action is most likely to target: visible first, then
    # form controls (so a form is never what gets truncated away), then things
    # actually on screen, then document order.
    # When a modal dialog is open, its controls are the current interaction
    # surface.  Letting the background form win the character budget made a
    # 31-day calendar expose only days 1-12, forcing a second extraction just
    # to choose a later date.  DOM collection is document ordered, so controls
    # following the first visible dialog are its descendants before any later
    # background controls.  Keep that modal run ahead of the underlying form.
    visible_dialogs = [
        e for e, _ in prepared
        if e.get("visible") and str(e.get("role") or "").casefold() in _DIALOG_ROLES
    ]

    def _in_active_dialog(element: dict) -> bool:
        """Does this control belong to an open dialog?

        Document order alone ("every control after the dialog") assumed the
        dialog is the last thing in the DOM. A visible dialog early in the page
        — Amazon's ads-notice banner — then claimed 194 later controls, 177 of
        them off screen, and they outranked the whole viewport. A dialog's
        controls are inside its box, so geometry decides when the collector
        measured it; document order remains the fallback without geometry.
        """
        role = str(element.get("role") or "").casefold()
        if role in _DIALOG_ROLES:
            return bool(visible_dialogs)
        if role not in _FORM_ROLES:
            return False
        region = element.get("containing_region")
        if isinstance(region, dict) and str(region.get("role") or "").casefold() in _DIALOG_ROLES:
            return True
        order = int(element.get("document_order") or 0)
        center = _point(element.get("center"))
        for dialog in visible_dialogs:
            if order <= int(dialog.get("document_order") or 0):
                continue
            box = _box(dialog.get("bbox"))
            if box is not None and center is not None:
                if box[0] <= center[0] <= box[2] and box[1] <= center[1] <= box[3]:
                    return True
                continue
            if isinstance(region, dict):
                continue  # placed in a known, non-dialog region
            return True
        return False

    menu_open = any(
        str(e.get("aria_expanded") or "").casefold() == "true" for e, _ in prepared
    )

    def _current(element: dict) -> bool:
        """What the last action revealed: a new element or an open popup's item.

        Opening Workday's "How did you hear" list showed 28 elements and none
        of its options: header links and empty icons outranked them, and the
        model guessed option refs until one resolved.
        """
        if not element.get("visible"):
            return False
        if str(element.get("ref") or "") in fresh:
            return True
        role = str(element.get("role") or "").casefold()
        return role in _POPUP_ITEM_ROLES or (menu_open and role in _MENU_ITEM_ROLES)

    def _interaction_rank(pair: tuple[dict, dict]) -> tuple:
        element = pair[0]
        role = str(element.get("role") or "").casefold()
        order = int(element.get("document_order") or 0)
        in_active_dialog_run = _in_active_dialog(element)
        tier = _actionability_tier(element)
        if outline:
            # The outline guarantees the viewport: everything on screen before
            # anything the model would have to scroll to. An open modal overlays
            # the viewport, so its controls still lead within that; a tall
            # non-modal "dialog" (a filter rail) can no longer pull its
            # off-screen contents ahead of what is actually visible.
            return (
                not element.get("visible"),
                not element.get("in_viewport"),
                tier == 2,
                not _current(element),
                not in_active_dialog_run,
                0 if role in _DIALOG_ROLES else 1,
                not (role in _FORM_ROLES or element.get("in_form")),
                tier,
                order,
            )
        return (
            not element.get("visible"),
            not element.get("in_viewport"),
            tier == 2,
            not _current(element),
            not in_active_dialog_run,
            0 if role == "dialog" else 1,
            not (role in _FORM_ROLES or element.get("in_form")),
            tier,
            order,
        )
    ordered = sorted(prepared, key=_interaction_rank)
    try:
        offset = max(0, int(str(view.get("cursor") or "c0").lstrip("c") or 0))
    except ValueError:
        offset = 0
    considered = ordered[offset:offset + cap]

    # Budget by characters, not by count. A fixed count still overran the
    # formatter's window on pages with long labels, and a list cut off mid-way
    # is worse than a shorter complete one: the model cannot tell which refs it
    # simply never saw.
    budget = outline_budget() if outline else int(get_setting("browser_workspace_observe_max_chars", 5600) or 5600)
    shown: list[dict] = []
    spent = 0
    stopped_at: int | None = None
    first_skipped: int | None = None
    charged: set[str] = set()
    layout = OutlineLayout(regions, frames, page_url=snapshot.get("url")) if outline else None
    for index, (_raw, compact) in enumerate(considered):
        if layout is not None:
            cost = layout.cost(compact)
        else:
            cost = len(json.dumps(compact)) + 24  # +indentation the formatter adds
            for key, table in (("region", regions), ("frame", frames)):
                ref_id = compact.get(key)
                if ref_id and ref_id not in charged:
                    cost += len(json.dumps({ref_id: table.get(ref_id)}))
            for ref_id in compact.get("controls") or []:
                if ref_id not in charged:
                    cost += len(json.dumps({ref_id: regions.get(ref_id)}))
        effective_budget = max(budget, 12000) if _raw.get("in_viewport") else budget
        if spent + cost > effective_budget:
            if layout is not None and cost <= budget // 4:
                # The outline stops at the first element that does not fit, so
                # everything shown is a contiguous run and `cursor` continues
                # exactly where it left off.
                stopped_at = offset + index
                break
            # Do not truncate an identity to make it fit, or let one enormous
            # author-supplied ID exhaust the entire model-facing observation.
            # This path keeps packing, so the resume point is the first element
            # left behind: without it the note offered a `cursor` that the
            # response did not carry, and a filtered list of 152 links ended at
            # 23 with nowhere to go but browser_evaluate.
            if first_skipped is None:
                first_skipped = offset + index
            continue
        if layout is not None:
            layout.place(compact)
        for key in ("region", "frame"):
            if compact.get(key):
                charged.add(compact[key])
        charged.update(compact.get("controls") or [])
        shown.append(compact)
        spent += cost

    used_regions = {rid for e in shown for rid in [e.get("region"), *(e.get("controls") or [])] if rid}
    used_frames = {e.get("frame") for e in shown if e.get("frame")}
    shown_refs = {e.get("ref") for e in shown}
    on_screen_refs = {r.get("ref") for r in elements if r.get("in_viewport")}
    projected = {
        "url": snapshot.get("url"),
        "title": snapshot.get("title"),
        "observation_id": str(snapshot.get("observation_id") or (elements or [{}])[0].get("observation_id") or ""),
        "elements": shown,
        "element_count": len(elements),
        "viewport": snapshot.get("viewport"),
        "elements_complete": bool(snapshot.get("elements_complete", True)) and len(shown) == len(elements)
        and not snapshot.get("budget_exhausted") and not snapshot.get("elements_omitted"),
    }
    if regions and used_regions:
        projected["regions"] = {rid: regions[rid] for rid in regions if rid in used_regions}
    if frames and used_frames:
        projected["frames"] = {fid: frames[fid] for fid in frames if fid in used_frames}
    projected["coverage"] = {
        "total": len(elements),
        "matched": len(prepared),
        "on_screen": len(on_screen_refs),
        "on_screen_shown": len(on_screen_refs & shown_refs),
        "shown": len(shown),
    }
    if view:
        projected["view"] = {k: v for k, v in view.items() if not str(k).startswith("_")}
    if headings:
        projected["headings"] = headings
    if "content_blocks" in snapshot:
        from ascended_browser._app.browser_viewport_content import project_viewport_content
        projected.update(project_viewport_content(snapshot))
        if snapshot.get("viewport_revision"):
            projected["viewport_revision"] = snapshot["viewport_revision"]
        projected["content_note"] = "Text from blocks intersecting the current viewport. For complete block text use browser_extract target_ref; scroll a scroll_containers ref to continue."
        if not projected["content_complete"]:
            projected["content_note"] += " Reading is bounded or incomplete; use browser_extract find or target_ref for missing content."
    if text:
        projected["text"] = text
    # Authentication is page evidence for the agent, not a durable stop
    # decision. Keep it in the compact snapshot even when the raw DOM is later
    # truncated, so the model can deliberately choose browser_login.
    from ascended_browser._app.browser_reliability import classify_auth_state
    auth_state = classify_auth_state(projected)
    if auth_state.get("state") != "unknown":
        projected["auth_state"] = auth_state
    for key in ("workspace_id", "tab_id", "controls_observed", "controls_total", "frames_observed", "frames_total", "collection_diagnostics"):
        if key in snapshot:
            projected[key] = snapshot[key]
    matched = len(prepared)
    if matched > len(shown) or snapshot.get("elements_omitted"):
        projected["elements_omitted"] = matched - len(shown) + int(snapshot.get("elements_omitted") or 0)
        omitted = [c for _, c in ordered if c.get("ref") not in shown_refs]
        projected["omitted_examples"] = [
            name for name in (
                " ".join(str(c.get("label") or c.get("aria_label") or c.get("text") or "").split())[:40]
                for c in omitted[:20]
            ) if name
        ][:5]
        resume = stopped_at if stopped_at is not None else first_skipped
        if resume is None and offset + len(considered) < len(ordered):
            # Everything considered fitted, but the cap stopped short of the
            # ranked list: the next page starts after what was considered.
            resume = offset + len(considered)
        if resume is not None:
            projected["next_cursor"] = f"c{resume}"
        projected["note"] = (
            f"Showing {len(shown)} of {matched} elements, on-screen first. To reach the "
            f"rest, observe again with query (words to find), within (a region, frame "
            f"or element ref)"
            + (f', or cursor: "c{resume}" to continue.' if resume is not None else ".")
        )
    if scope_note:
        projected["note"] = scope_note
    for key in ("blocking_overlays", "dismiss_candidates", "open_tabs"):
        value = snapshot.get(key)
        if value:
            projected[key] = value
    if snapshot.get("overlay_count"):
        projected["overlay_count"] = snapshot["overlay_count"]
    if snapshot.get("budget_exhausted"):
        projected["budget_exhausted"] = True
        projected["note"] = (
            "The page snapshot hit its time budget, so this element list may be "
            "incomplete. Observe again if what you need is missing."
        )
    from ascended_browser._app.browser_sensitive_state import redact_password_snapshot
    redact_password_snapshot(projected)
    return projected


def _same_url(left: str, right: str) -> bool:
    """True when two URLs address the same page for tab-reuse purposes.

    Ignores a trailing slash and the fragment, since `#section` is the same
    document, but keeps the query — `?page=2` is a different page.
    """
    def _key(value: str) -> tuple:
        parsed = urlparse(str(value or "").strip())
        return (
            parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port,
            parsed.path.rstrip("/"), parsed.query,
        )
    return bool(left) and bool(right) and _key(left) == _key(right)


_DESCRIBE_CONTROL = """el => ({
  tag: el.tagName.toLowerCase(),
  type: (el.getAttribute('type') || '').toLowerCase(),
  autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
  role: (el.getAttribute('role') || '').toLowerCase(),
  checked: !!el.checked,
  editable: !!el.isContentEditable || ['INPUT', 'TEXTAREA'].includes(el.tagName),
  readonly: !!el.readOnly,
  multiple: !!el.multiple,
  value: 'value' in el ? String(el.value || '') : '',
  // What a select shows: its value can be "" while it reads "Choose a country",
  // which is what an observation reported for it.
  selected_text: el.tagName === 'SELECT'
    ? Array.from(el.selectedOptions || []).map(o => (o.textContent || '').replace(/\\s+/g, ' ').trim()).join(', ')
    : '',
  text: (el.innerText || el.textContent || '').trim(),
  ariaControls: el.getAttribute('aria-controls') || '',
  ariaExpanded: el.getAttribute('aria-expanded') || '',
  ariaAutocomplete: el.getAttribute('aria-autocomplete') || '',
  ariaHaspopup: (el.getAttribute('aria-haspopup') || '').toLowerCase(),
  ariaPressed: (el.getAttribute('aria-pressed') || '').toLowerCase(),
  options: el.tagName === 'SELECT'
    ? Array.from(el.options || []).slice(0, 200).map(o => ({
        text: (o.textContent || '').trim(), value: String(o.value || '') }))
    : [],
})"""


# Select-skin libraries (selectric, select2, chosen, bootstrap-select) hide
# the native <select> and draw their own list. Committing the native value
# focuses it, which opens the skin's list on top of the page and leaves its
# label stale: the next click hit the open list ("<li> covers it"). When the
# commit opened such a list and it offers the chosen option, choose it there
# too, as a person would; that syncs the skin and closes it.
_MARK_LISTS_BEFORE_SELECT = """() => document.querySelectorAll('ul, ol, [role="listbox"]').forEach(list => {
  if (list.getClientRects().length) list.setAttribute('data-odysseus-preselect-list', '');
})"""
_SKIN_OPTION = """(wanted) => {
  const fold = text => String(text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const hits = [];
  document.querySelectorAll('ul, ol, [role="listbox"]').forEach(list => {
    if (list.hasAttribute('data-odysseus-preselect-list') || !list.getClientRects().length) return;
    list.querySelectorAll('li, [role="option"]').forEach(item => {
      if (item.getClientRects().length && fold(item.textContent) === fold(wanted)) hits.push(item);
    });
  });
  document.querySelectorAll('[data-odysseus-preselect-list], [data-odysseus-skin-option]').forEach(node => {
    node.removeAttribute('data-odysseus-preselect-list');
    node.removeAttribute('data-odysseus-skin-option');
  });
  if (hits.length !== 1) return false;
  hits[0].setAttribute('data-odysseus-skin-option', '');
  return true;
}"""


async def _mark_lists_before_native_select(root: Any) -> None:
    try:
        await _bounded(root.evaluate(_MARK_LISTS_BEFORE_SELECT), 1.0)
    except Exception:
        pass


async def _sync_custom_select_skin(root: Any, chosen: str) -> None:
    try:
        await asyncio.sleep(0.05)
        if await _bounded(root.evaluate(_SKIN_OPTION, chosen), 1.0) is True:
            await root.locator("[data-odysseus-skin-option]").first.click(timeout=2000)
    except Exception:
        pass  # the native value is committed; the readback decides


def _native_option_labels(options: list[dict[str, Any]], limit: int = 20) -> str:
    labels = [repr(str(item.get("text") or item.get("value") or "")) for item in options[:limit]]
    more = len(options) - len(labels)
    return ", ".join(labels) + (f" (+{more} more)" if more > 0 else "") if labels else "(none)"


def _option_key(value: Any) -> str:
    """Whitespace/case-insensitive option identity used for exact matching."""
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _aria_option_selector(control: dict[str, Any]) -> str:
    """Visible option selector, scoped to aria-controls when the widget exposes it."""
    controls = str(control.get("ariaControls") or "").strip()
    if controls:
        escaped = controls.replace("\\", "\\\\").replace('"', '\\"')
        return f'[id="{escaped}"] [role="option"]:visible'
    # React Select, MUI and Greenhouse commonly portal the listbox outside the
    # control's form subtree, so the document/frame root is the safe fallback.
    return '[role="option"]:visible'


async def _visible_option_texts(root: Any, selector: str, *, timeout_ms: int) -> tuple[Any, list[str]]:
    options = root.locator(selector)
    try:
        await options.first.wait_for(state="visible", timeout=min(timeout_ms, 5000))
    except Exception:
        pass
    try:
        texts = await options.all_inner_texts()
    except Exception:
        texts = []
    return options, [re.sub(r"\s+", " ", str(text)).strip() for text in texts]


async def _select_control_unverified(
    root: Any,
    selector: str,
    value: Any,
    *,
    query: Any = None,
    option: Any = None,
    timeout_ms: int = 10000,
) -> str:
    """Select a native option or one exact option from an ARIA combobox.

    A custom combobox is not a text field: filling it only filters suggestions
    and leaves the underlying form value unset. This primitive owns the whole
    open -> filter -> choose -> verify sequence so a model does not need three
    or more turns (and a fragile focused-keypress) for one field.
    """
    locator = root.locator(selector).first
    control = await _bounded(locator.evaluate(_DESCRIBE_CONTROL), 5.0)
    tag = str(control.get("tag") or "")
    wanted = str(option if option not in (None, "") else value if value is not None else "").strip()
    wanted_key = _option_key(wanted)
    if not wanted_key:
        raise WorkspaceError("select requires a non-empty option/value")

    if tag == "select":
        options = control.get("options") or []
        exact = [item for item in options if _option_key(item.get("text")) == wanted_key]
        if len(exact) == 1:
            await _mark_lists_before_native_select(root)
            await locator.select_option(value=str(exact[0].get("value")), timeout=timeout_ms)
            await _sync_custom_select_skin(root, str(exact[0].get("text") or ""))
            return f"selected {exact[0].get('text')!r}"
        if len(exact) > 1:
            raise WorkspaceError(f"Option {wanted!r} is ambiguous ({len(exact)} exact matches)")
        contains = [item for item in options if wanted_key in _option_key(item.get("text"))]
        if len(contains) == 1:
            await locator.select_option(value=str(contains[0].get("value")), timeout=timeout_ms)
            return f"selected {contains[0].get('text')!r}"
        if len(contains) > 1:
            labels = ", ".join(repr(str(item.get("text") or "")) for item in contains[:8])
            raise WorkspaceError(f"Option {wanted!r} is ambiguous. Matches: {labels}")
        by_value = [item for item in options if str(item.get("value") or "") == wanted]
        # _DESCRIBE_CONTROL reads at most 200 options; past that, absence from
        # the list is not proof, so let the page answer.
        if by_value or len(options) >= 200:
            await locator.select_option(value=wanted, timeout=timeout_ms)
            return f"selected value {wanted!r}"
        # The options were just read: a label that is not among them is known
        # to be missing now. Handing it to select_option waited out the whole
        # budget and then named none of the choices.
        raise WorkspaceError(
            f"Option {wanted!r} is not in this select; nothing was selected. "
            f"Its options are: {_native_option_labels(options)}"
        )

    role = str(control.get("role") or "")
    if role != "combobox":
        raise WorkspaceError(
            f"select requires a <select> or role=combobox control, got "
            f"<{tag or 'unknown'}>{' role=' + role if role else ''}"
        )

    await locator.click(timeout=timeout_ms)
    filter_text = str(query if query not in (None, "") else wanted if control.get("editable") and not control.get("readonly") else "")
    if filter_text:
        await locator.fill(filter_text, timeout=timeout_ms)  # replaces; never via empty

    option_selector = _aria_option_selector(control)
    options, texts = await _visible_option_texts(root, option_selector, timeout_ms=timeout_ms)
    exact_indexes = [index for index, text in enumerate(texts) if _option_key(text) == wanted_key]
    if len(exact_indexes) != 1:
        if not exact_indexes and option in (None, ""):
            # Backward-compatible convenience for value="Canada" when the one
            # visible choice is "Canada +1". Explicit `option` always demands
            # an exact match.
            contains = [index for index, text in enumerate(texts) if wanted_key in _option_key(text)]
            if len(contains) == 1:
                exact_indexes = contains
        if len(exact_indexes) != 1:
            candidates = ", ".join(repr(text) for text in texts[:12]) or "(none visible)"
            reason = "ambiguous" if len(exact_indexes) > 1 else "not found"
            raise WorkspaceError(f"Option {wanted!r} was {reason}. Visible options: {candidates}")

    chosen_index = exact_indexes[0]
    chosen_text = texts[chosen_index]
    chosen = options.nth(chosen_index)
    await chosen.click(timeout=timeout_ms)

    try:
        after = await _bounded(locator.evaluate(_DESCRIBE_CONTROL), 5.0)
    except Exception:
        # React commonly replaces the input node when an option commits. A
        # detached old node is not itself a failed selection; the listbox and
        # selected-option evidence below can still verify the outcome.
        after = {}
    selected_texts: list[str] = []
    try:
        selected_texts = await root.locator('[role="option"][aria-selected="true"]:visible').all_inner_texts()
    except Exception:
        pass
    committed = bool(
        _option_key(after.get("value")) in {wanted_key, _option_key(chosen_text)}
        or _option_key(after.get("text")) in {wanted_key, _option_key(chosen_text)}
        or any(_option_key(text) == _option_key(chosen_text) for text in selected_texts)
        or str(after.get("ariaExpanded") or "").casefold() == "false"
    )
    if not committed:
        _, remaining = await _visible_option_texts(
            root, option_selector, timeout_ms=min(timeout_ms, 1000),
        )
        committed = not remaining
    if not committed:
        raise WorkspaceError(
            f"Clicked option {chosen_text!r}, but the combobox did not expose a committed selection"
        )
    return f"selected {chosen_text!r} from combobox"


# Checkables with no native input: the state is an ARIA attribute and the
# page's own click handler is the only way to change it. Workday renders its
# multi-select checklist as role=option with aria-selected; filling one threw.
_ARIA_CHECKABLE_ROLES = frozenset({
    "checkbox", "switch", "radio", "menuitemcheckbox", "menuitemradio", "option",
    "togglebutton",  # any element carrying aria-pressed
})


# The option's own name (its first line of text, else its label without a
# trailing state word), after marking the list it belongs to.
_MARK_CHECK_LIST = """el => {
  const list = el.closest('[role="listbox"], [role="menu"], [role="tree"], [role="grid"]');
  if (list) list.setAttribute('data-odysseus-check-list', '');
  const text = String(el.innerText || '').split('\\n')[0].trim();
  return text || String(el.getAttribute('aria-label') || '')
    .replace(/\\s+(not\\s+)?(checked|selected)$/i, '').trim();
}"""

# ARIA items named `name` that report a choice: chips and chosen entries outside
# the option's list and outside any list still offering unselected options.
_COUNT_CHOSEN_ITEMS = """name => {
  const fold = value => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const wanted = fold(name);
  // ARIA items only: a footer's plain <li> links ("LinkedIn") are not choices.
  return Array.from(document.querySelectorAll('[role="option"], [role="listitem"]'))
    .filter(item => item.getClientRects().length)
    .filter(item => !item.closest('[data-odysseus-check-list]'))
    .filter(item => {
      const list = item.closest('[role="listbox"], [role="menu"], [role="tree"], [role="grid"]');
      return !(list && list.querySelector('[aria-selected="false"]'));
    })
    .filter(item => fold(String(item.innerText || '').split('\\n')[0]) === wanted).length;
}"""


def _input_page(root: Any) -> Any:
    """The Page that takes input for ``root`` (a Page, or a Frame of one)."""
    if root is None:
        return None
    page = root if hasattr(root, "keyboard") else getattr(root, "page", None)
    return page if page is not None and hasattr(page, "keyboard") and hasattr(page, "mouse") else None


async def _activate_checkable(
    locator: Any, root: Any, *, timeout_ms: int, keyboard_only: bool = False,
) -> None:
    """Activate a checkable through the shared click ladder.

    A bare ``locator.click`` here was the first failure in five of the eleven
    2026-10-04 escalation chains (Ashby yes/no, ChatGPT switches and menu
    radios): Playwright's stability wait stalls on Camoufox and on background
    tabs, while the ladder's owned-point mouse and keyboard rungs land.
    """
    page = _input_page(root)
    if page is None:
        await locator.click(timeout=min(timeout_ms, 5000))
        return
    from ascended_browser._app.browser_click_helpers import _ClickBudget, _keyboard_activate, activate_locator

    seconds = max(1.0, min(timeout_ms / 1000 - 1.0, 8.0))
    if keyboard_only:
        if not await _keyboard_activate(page, root, locator, _ClickBudget(min(seconds, 4.0))):
            raise WorkspaceError("The control did not change state and takes no keyboard activation")
        return
    await activate_locator(page, root, locator, target="the control", budget_seconds=seconds)


async def _set_aria_checkable(
    locator: Any, role: str, should_check: bool, *, timeout_ms: int, root: Any = None,
) -> str:
    attribute = {"option": "aria-selected", "togglebutton": "aria-pressed"}.get(role, "aria-checked")
    try:
        raw = await _bounded(locator.get_attribute(attribute, timeout=500), 1.0)
    except Exception:
        raw = None
    current = {"true": True, "false": False}.get(str(raw or "").strip().casefold())
    if current is should_check:
        return "already checked" if should_check else "already unchecked"
    if not should_check and role in {"radio", "menuitemradio"}:
        raise WorkspaceError(
            "A radio choice cannot be cleared by itself; check the other choice in its group."
        )
    # The live readback in verified_fill_control proves the resulting state
    # while the option exists. A menu that closes on a click takes the option
    # with it (Workday's: TikTok became the field's chip and the menu went).
    name = ""
    before = None
    if root is not None:
        try:
            name = str(await _bounded(locator.evaluate(_MARK_CHECK_LIST), 1.5) or "")
            before = await _bounded(root.evaluate(_COUNT_CHOSEN_ITEMS, name), 1.5)
        except Exception:
            before = None
    await _activate_checkable(locator, root, timeout_ms=timeout_ms)
    if attribute and current is not None:
        # Playwright's setChecked contract: read, activate, read again. A
        # second dispatch is sent only when the state is known and still
        # unchanged after the page has had a moment to apply the first; an
        # unknown state never gets one, so a toggle is never flipped back.
        await asyncio.sleep(0.3)
        try:
            again = await _bounded(locator.get_attribute(attribute, timeout=500), 1.0)
        except Exception:
            again = None
        if {"true": True, "false": False}.get(str(again or "").strip().casefold()) is current:
            await _activate_checkable(locator, root, timeout_ms=timeout_ms, keyboard_only=True)
    if before is not None and name:
        await asyncio.sleep(0.4)
        try:
            gone = int(await _bounded(locator.count(), 1.0)) == 0
        except Exception:
            gone = False
        if gone:
            from ascended_browser._app.browser_live_verification import publish_primitive_evidence

            try:
                after = int(await _bounded(root.evaluate(_COUNT_CHOSEN_ITEMS, name), 1.5))
            except Exception:
                after = None
            # The requested end state, not a change: checking an item that is
            # already the field's choice is a success (live, Workday: the
            # option's own state was only in its label text).
            changed = after is not None and (after > 0 if should_check else after == 0)
            publish_primitive_evidence({
                "verified": changed,
                "verification": "chosen_item_readback",
                "requested": should_check,
                "observed": {"item": name, "before": before, "after": after},
                **({} if changed else {"failure_reason": "state_mismatch" if after is not None else "readback_unavailable"}),
            })
    try:
        await _bounded(root.evaluate(
            "() => document.querySelectorAll('[data-odysseus-check-list]')"
            ".forEach(n => n.removeAttribute('data-odysseus-check-list'))"), 1.0)
    except Exception:
        pass
    return "checked" if should_check else "unchecked"


async def _set_checkable(locator: Any, should_check: bool, *, timeout_ms: int, root: Any = None) -> str:
    """Set one exact checkbox/radio and verify its resulting state.

    Styled controls often place their visible label over the native input. A
    plain Playwright ``check`` then waits until timeout because the associated
    label intercepts the pointer. Recover only through nodes deterministically
    associated with this input, then verify the native checked state; never
    guess at a nearby container or treat a dispatched click as success.
    """
    try:
        if bool(await locator.is_checked(timeout=min(timeout_ms, 3000))) == should_check:
            return "already checked" if should_check else "already unchecked"
    except Exception:
        pass

    async def reached() -> bool:
        try:
            return bool(await locator.is_checked(timeout=min(timeout_ms, 3000))) == should_check
        except Exception:
            return False

    from ascended_browser._app.browser_presented_tab import presented_state

    page = _input_page(root)
    # On a tab the user is not looking at, Playwright's check (a pointer
    # click) would bring the tab to the front; the ladder keys it instead.
    background = page is not None and presented_state(page) == "background"
    native = locator.check if should_check else locator.uncheck
    if not background:
        try:
            # A box parked off the page or shrunk to 1px is never actionable:
            # Playwright's check would spend the whole deadline trying to scroll
            # to it, leaving no time for the label below. Go there directly.
            parked = await _bounded(locator.evaluate(
                """el => { const b = el.getBoundingClientRect();
                  return b.right + scrollX <= 0 || b.bottom + scrollY <= 0
                    || (b.width <= 1 && b.height <= 1); }"""), 1.0)
            if parked is True:
                raise RuntimeError("Timeout exceeded before dispatch: the input is parked off the page; use its label")
            # A slice, not the whole deadline: a stalled native check used to
            # spend all ten seconds, and the fallbacks below never ran.
            await native(timeout=min(timeout_ms, 3500) if page is not None else timeout_ms)
            if await reached():
                return "checked" if should_check else "unchecked"
            raise WorkspaceError("Checkbox action returned without setting the requested state")
        except Exception as first_exc:
            from ascended_browser._app.browser_click_helpers import _is_timeout_error, is_click_intercept_error

            # A timeout means the input was never set, for either of the two
            # reasons this recovery exists: the label covers the box, or the box is
            # styled invisible (Workday's facet checkboxes are opacity:0 behind
            # "Full time (808)"). Both are operated through the label below, and
            # the native checked state still decides whether it worked.
            if not is_click_intercept_error(first_exc) and not _is_timeout_error(first_exc):
                raise
    if await reached():
        # A native check that timed out after its input landed.
        return "checked" if should_check else "unchecked"
    if page is not None:
        try:
            await _activate_checkable(locator, root, timeout_ms=timeout_ms)
        except Exception:
            pass
        if await reached():
            state = "checked" if should_check else "unchecked"
            return f"{state} via the click ladder"

    associated = await _bounded(locator.evaluate(
        """(el, desired) => {
          const matches = () => Boolean(el.checked) === Boolean(desired);
          const click = (candidate, method) => {
            if (!candidate || candidate === el) return '';
            candidate.click();
            return matches() ? method : '';
          };
          for (const label of Array.from(el.labels || [])) {
            const result = click(label, 'associated label');
            if (result) return result;
          }
          for (const id of (el.getAttribute('aria-labelledby') || '').trim().split(/\\s+/)) {
            if (!id) continue;
            const result = click(el.ownerDocument.getElementById(id), 'aria-labelledby target');
            if (result) return result;
          }
          return click(el.closest('label'), 'wrapping label');
        }""",
        should_check,
    ), 5.0)
    if bool(await locator.is_checked(timeout=min(timeout_ms, 3000))) == should_check:
        state = "checked" if should_check else "unchecked"
        return f"{state} via {associated or 'associated control'}"
    raise WorkspaceError(
        "Associated checkbox control did not set the requested state; "
        "an unrelated overlay may require a fresh observation"
    )


async def _fill_control_unverified(root: Any, selector: str, value: Any, *, timeout_ms: int = 10000) -> str:
    """Set one control's value, dispatching on what the control actually is.

    `locator.fill()` only works on text-like inputs. Sending it at a <select>,
    a checkbox or a radio raises, which made filling an ordinary form fail on
    its first non-text field. Stagehand solves this by making the caller name
    the method (`selectOption`, `check`, `fill`); here the element is inspected
    instead, so the model does not have to know the tag to fill a form.

    `root` is whatever document the element lives in — the page, or the iframe
    that owns it. See `browser_click_helpers.resolve_target`.
    """
    locator = root.locator(selector).first
    control = await _bounded(locator.evaluate(_DESCRIBE_CONTROL), 5.0)
    tag = str(control.get("tag") or "")
    kind = str(control.get("type") or "")
    text = "" if value is None else str(value)

    if tag == "select":
        return await _select_control(root, selector, text, timeout_ms=timeout_ms)

    if kind in {"checkbox", "radio"}:
        should_check = text.strip().casefold() not in {"false", "0", "no", "off", "unchecked", ""}
        return await _set_checkable(locator, should_check, timeout_ms=timeout_ms, root=root)

    role = str(control.get("role") or "")
    if isinstance(value, bool) and kind not in {"checkbox", "radio"} and tag != "select" and (
        str(control.get("ariaPressed") or "") not in {"true", "false"}
        and role not in _ARIA_CHECKABLE_ROLES
    ):
        # check on something with no checked state ran Locator.fill and threw a
        # Playwright internal. Say what it is and the one thing to do with it.
        raise _untouched(WorkspaceError(
            f"This control has no checked state to set (<{tag or 'element'}>"
            f"{' role=' + role if role else ''}, no aria-checked, aria-selected or "
            "aria-pressed). Click it instead."
        ))
    if str(control.get("ariaPressed") or "") in {"true", "false"} and tag not in {"input", "textarea"}:
        # A toggle button (WAI-ARIA aria-pressed): Ashby's Yes/No answers.
        role = "togglebutton"
    if role in _ARIA_CHECKABLE_ROLES and tag not in {"input", "textarea"}:
        should_check = text.strip().casefold() not in {"false", "0", "no", "off", "unchecked", ""}
        return await _set_aria_checkable(locator, role, should_check, timeout_ms=timeout_ms, root=root)

    if kind == "file":
        # Uploads are a consequential action with their own grant checks; they
        # must not be reachable by calling this a "fill".
        raise _untouched(WorkspaceError(
            "This is a file input. Uploading is not part of fill — retry with "
            "action.kind='upload' and action.paths=[…], which verifies the "
            "attached filenames and asks the user first."
        ))

    # Playwright's fill waits for "visible, enabled and editable" and retries
    # until its timeout, so a control that can never be typed into — a
    # readonly date-picker trigger (flatpickr), a hidden duplicate input — cost
    # the full budget and then failed with nothing actionable. Ask the control
    # first, and say what to do instead. Both reads are bounded and default to
    # "fillable" if the probe itself fails, so a slow page never blocks a fill.
    try:
        visible = await _bounded(locator.is_visible(), 1.0)
    except Exception:
        visible = True
    if not visible:
        raise WorkspaceError(
            "This control is not visible, so it cannot be typed into. Scroll to "
            "it or open the section that contains it, observe again, and use the "
            "visible control's ref."
        )
    try:
        editable = await _bounded(locator.is_editable(timeout=500), 1.0)
    except Exception:
        editable = True
    if not editable:
        # One next step, chosen from what the control declares. A menu of
        # verbs sent the model through each of them in turn.
        popup = str(control.get("ariaHaspopup") or "")
        if popup in {"dialog", "grid"} or kind in {"date", "month"}:
            step = "It opens a calendar: use kind 'date' with the value as YYYY-MM-DD."
        elif popup in {"listbox", "menu", "tree", "true"} or control.get("ariaControls") or control.get("ariaAutocomplete"):
            step = "It opens a list: use kind 'select' with the option's text."
        else:
            step = "Click it to change it."
        raise WorkspaceError(f"This control is read-only, so it cannot be typed into. {step}")

    if role == "spinbutton":
        # Segmented date/number widgets (Workday's MM/YYYY) keep their own
        # state and rebuild the value from keystrokes; a value set in one shot
        # is overwritten with the old one on the next render.
        try:
            before = await _bounded(locator.input_value(timeout=500), 1.0)
        except Exception:
            before = None
        typed = await _type_like_a_user(locator, text, timeout_ms=timeout_ms)
        if await _value_stuck(locator, text):
            return typed
        try:
            after = await _bounded(locator.input_value(timeout=500), 1.0)
        except Exception:
            after = None
        # A spinner that swallows keystrokes (jQuery UI's, with keydown
        # prevented so only its arrows change it) still takes a value set
        # directly. Only when the keys had no effect at all: a segment that is
        # merely slow to settle must not be overwritten in one shot.
        if before is not None and after == before:
            await locator.fill(text, timeout=timeout_ms)
            return "filled"
        return typed

    # fill() replaces the content. Clearing to "" first created an empty state
    # no person produces, and some controls cannot survive it: DemoQA's date
    # picker unmounted the whole app, leaving a page with zero controls. A
    # framework that appends instead ("JohnJane") fails the check below and is
    # retyped over a select-all, which never passes through empty either.
    await locator.fill(text, timeout=timeout_ms)
    if str(control.get("type") or "") != "password" and not await _value_stuck(locator, text):
        # A controlled input that listens to key events only reverts a value
        # set in one shot. Typing is what browser-use does for every field
        # (per-character key events) and what Playwright MCP's `slowly` is for.
        return await _type_like_a_user(locator, text, timeout_ms=timeout_ms)
    if not (
        role in {"combobox", "searchbox", "spinbutton"}
        or control.get("ariaAutocomplete") or control.get("ariaControls")
        or str(control.get("ariaHaspopup") or "") not in {"", "false"}
    ):
        # fill() fires input, not change: a field that normalizes on change
        # ("5551234567" -> "(555) 123-4567") was read back before it did, and
        # the receipt verified a value the field never kept. Commit the edit
        # the way leaving the field does, then give focus back so a following
        # key press without a ref still lands here.
        from ascended_browser._app.browser_click_helpers import commit_edit

        await commit_edit(locator, refocus=True)
    return "filled"


async def _value_stuck(locator: Any, text: str) -> bool:
    """Whether the control still holds ``text`` once the page has reacted."""
    try:
        await asyncio.sleep(0.1)
        actual = await _bounded(locator.input_value(timeout=500), 1.0)
    except Exception:
        return True  # unreadable here; the verified readback decides
    fold = lambda value: re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return fold(actual) == fold(text)


async def _type_like_a_user(locator: Any, text: str, *, timeout_ms: int) -> str:
    """Focus, replace the content with key events, then leave the field.

    Focus needs no pointer hit, so this also reaches a segment input parked
    outside the viewport, which a click can never reach.
    """
    budget = min(timeout_ms, 5000)
    await locator.focus(timeout=budget)
    await locator.press("ControlOrMeta+a", timeout=budget)
    await locator.press_sequentially(text, delay=40, timeout=max(budget, 60 * len(text) + 1000))
    try:
        await _bounded(locator.blur(timeout=1000), 1.5)
    except Exception:
        pass  # leaving the field only commits some widgets; readback decides
    return "typed"


async def _fill_control(root: Any, selector: str, value: Any, *, timeout_ms: int = 10000) -> str:
    from ascended_browser._app.browser_deadline import browser_deadline
    from ascended_browser._app.browser_live_verification import verified_fill_control
    from ascended_browser._app.browser_workspace import manager_core

    try:
        async with browser_deadline(timeout_ms / 1000):
            return await verified_fill_control(
                manager_core, _fill_control_unverified, root, selector, value, timeout_ms=timeout_ms,
            )
    except TimeoutError as exc:
        raise _deadline_error("check" if isinstance(value, bool) else "fill", timeout_ms) from exc


def _deadline_error(what: str, timeout_ms: int) -> WorkspaceError:
    """A deadline that says what ran out, instead of nothing at all.

    A Playwright timeout often carries an empty message, and the operation
    deadline carries none of its own, so six failures in one live run read as
    "TimeoutError with no detail — the control did not report why". The caller
    cannot act on that.
    """
    seconds = round(timeout_ms / 1000)
    if what == "select":
        detail = (
            "A control whose options are fetched as you type can outlast this: observe "
            "the tab to see where it got to, then retry — `query` makes the page fetch "
            "a shorter list."
        )
    elif what == "check":
        # Not a list-fetching problem: naming one sent the model to the wrong
        # remedy and then to pressing Space by hand (2026-10-04).
        detail = (
            "The control did not take the activation in time. Observe the tab: if its "
            "state already changed, it is done; if not, retry once."
        )
    else:
        detail = (
            "Observe the tab to see what the field holds now before typing again; a "
            "retry would add to text that may already be there."
        )
    return WorkspaceError(f"The {what} did not finish within {seconds}s. {detail}")


async def _select_control(
    root: Any, selector: str, value: Any, *, query: Any = None,
    option: Any = None, timeout_ms: int = 10000,
) -> str:
    from ascended_browser._app.browser_deadline import browser_deadline
    from ascended_browser._app.browser_live_verification import verified_select_control
    from ascended_browser._app.browser_workspace import manager_core

    try:
        async with browser_deadline(timeout_ms / 1000):
            return await verified_select_control(
                manager_core, _select_control_unverified, root, selector, value,
                query=query, option=option, timeout_ms=timeout_ms,
            )
    except TimeoutError as exc:
        raise _deadline_error("select", timeout_ms) from exc


async def _select_multiple_control(
    root: Any, selector: str, values: list[Any], *, timeout_ms: int = 10000,
) -> tuple[str, dict[str, Any]]:
    """Commit every requested value and prove their simultaneous final state."""
    from ascended_browser._app.browser_live_verification import consume_select_evidence

    wanted = [str(value).strip() for value in values if str(value or "").strip()]
    if not wanted:
        raise WorkspaceError("multi-select options must contain at least one visible value")
    locator = root.locator(selector).first
    try:
        control = await _bounded(locator.evaluate(_DESCRIBE_CONTROL), 3.0)
    except Exception:
        control = {}
    if str((control or {}).get("tag") or "").casefold() == "select" and bool(
        (control or {}).get("multiple")
    ):
        # Resolve every request against the options first (label, then value)
        # and commit once. Trying label= first waited out the whole budget
        # for requests that named option values.
        options = (control or {}).get("options") or []
        chosen: list[str] = []
        unknown: list[str] = []
        for value in wanted:
            key = _option_key(value)
            match = next((o for o in options if _option_key(o.get("text")) == key), None) or next(
                (o for o in options if str(o.get("value") or "") == value), None,
            )
            if match is None:
                unknown.append(value)
            else:
                chosen.append(str(match.get("value") or ""))
        if unknown and len(options) >= 200:
            # Past the described 200 options absence is not proof.
            try:
                await locator.select_option(label=wanted, timeout=timeout_ms)
            except Exception:
                await locator.select_option(value=wanted, timeout=timeout_ms)
        elif unknown:
            raise WorkspaceError(
                f"{', '.join(repr(v) for v in unknown)} not in this select; nothing was selected. "
                f"Its options are: {_native_option_labels(options)}"
            )
        else:
            await locator.select_option(value=chosen, timeout=timeout_ms)
        observed = await locator.evaluate("""el => Array.from(el.selectedOptions || []).flatMap(
          option => [String(option.value || '').trim(), String(option.textContent || '').trim()]
        ).filter(Boolean)""")
    else:
        observed = []
        for value in wanted:
            await _select_control(
                root, selector, value, query=value, option=value, timeout_ms=timeout_ms,
            )
            evidence = consume_select_evidence() or {}
            if evidence.get("verified") is not True:
                return "multi-select effect could not be verified", {
                    "verified": False,
                    "verification": "multi_select_control_readback",
                    "requested": wanted,
                    "observed": evidence.get("observed") or observed,
                    "failure_reason": evidence.get("failure_reason") or "readback_unavailable",
                }
            observed = list(evidence.get("observed") or [])
    folded = {re.sub(r"\s+", " ", str(value)).strip().casefold() for value in observed}
    missing = [value for value in wanted if re.sub(r"\s+", " ", value).strip().casefold() not in folded]
    return f"selected {len(wanted)} options", {
        "verified": not missing,
        "verification": "multi_select_control_readback",
        "requested": wanted,
        "observed": list(dict.fromkeys(str(value) for value in observed))[:16],
        **({"failure_reason": "state_mismatch", "missing": missing} if missing else {}),
    }


_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


_DAY_DATE_FORMATS = (
    "%Y-%m-%d", "%B %d %Y", "%d %B %Y", "%A %B %d %Y", "%B %d, %Y", "%m/%d/%Y",
)


def _parse_cell_date(raw: object) -> date | None:
    """Parse a day cell's own date, from its accessibility contract.

    Day cells carry the full date they represent — ``aria-label``
    ("October 15, 2026"), ``data-date`` or ``datetime`` — because that is what
    the ARIA date-picker pattern requires for screen readers. Reading the cell
    is exact, where reading the month header is prose: abbreviated, localized,
    sometimes split across a select and an input, and different per library.
    """
    text = " ".join(str(raw or "").split())
    if not text:
        return None
    text = re.sub(r"^(choose|select|pick)\s+", "", text, flags=re.I)
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    text = text.replace(",", " ")
    text = " ".join(text.split())
    for fmt in _DAY_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt.replace(",", "")).date()
        except ValueError:
            continue
    return None


async def _day_cell_dates(dialog: Any) -> list[date]:
    """Every date the calendar's own cells claim to represent."""
    try:
        raw = await _bounded(dialog.evaluate("""el => {
          const out = [];
          el.querySelectorAll('[aria-label],[data-date],[datetime],td[data-month]')
            .forEach(node => {
              const value = node.getAttribute('data-date')
                || node.getAttribute('datetime')
                || node.getAttribute('aria-label');
              if (value) out.push(value);
              // jQuery UI encodes the month/year on the cell instead.
              const m = node.getAttribute('data-month'), y = node.getAttribute('data-year');
              if (m !== null && y !== null) out.push(`${y}-${String(Number(m)+1).padStart(2,'0')}-01`);
            });
          return out.slice(0, 80);
        }"""), 2.0)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    parsed = [_parse_cell_date(item) for item in raw]
    return [value for value in parsed if value is not None]


async def _displayed_month(dialog: Any) -> tuple[int, int] | None:
    """Which month the calendar is showing, decided by its day cells.

    Adjacent-month padding cells are present too, so take the month that most
    cells agree on rather than the first one seen.
    """
    dates = await _day_cell_dates(dialog)
    if not dates:
        return None
    counts: dict[tuple[int, int], int] = {}
    for value in dates:
        key = (value.year, value.month)
        counts[key] = counts.get(key, 0) + 1
    return max(counts.items(), key=lambda item: item[1])[0]


def _visible_month(text: str) -> tuple[int, int] | None:
    """Parse the (year, month) a calendar is currently showing, if it says so."""
    # Calendars abbreviate ("Sep 2026") as often as they spell it out, and a
    # month dropdown may sit beside a separate year field rather than in one
    # phrase. Match a 3-letter stem and allow the year to follow loosely.
    stems = "|".join(name[:3] for name in _MONTH_NAMES)
    match = re.search(rf"\b({stems})[a-z]*\b[\s,]*(\d{{4}})\b", text or "", re.I)
    if not match:
        return None
    stem = match.group(1).casefold()
    return int(match.group(2)), [n[:3] for n in _MONTH_NAMES].index(stem) + 1


async def _calendar_month_text(dialog: Any) -> str:
    """Everything a calendar says about the month it is showing.

    flatpickr renders its month as a ``<select>`` and its year as an
    ``<input>``; neither appears in ``inner_text``, so a text-only read saw no
    month at all and every date outside the current month was unreachable.
    Include the values of the header's own form controls.
    """
    parts: list[str] = []
    try:
        parts.append(str(await dialog.inner_text(timeout=1000)))
    except Exception:
        pass
    try:
        values = await _bounded(dialog.evaluate("""el => {
          const out = [];
          el.querySelectorAll('select').forEach(node => {
            const option = node.selectedOptions && node.selectedOptions[0];
            if (option) out.push((option.textContent || '').trim());
          });
          el.querySelectorAll('input').forEach(node => {
            if (node.value) out.push(String(node.value).trim());
          });
          return out;
        }"""), 1.5)
        if isinstance(values, list):
            parts.extend(str(item) for item in values)
    except Exception:
        pass
    return " ".join(part for part in parts if part)


async def _page_calendar_to_month(
    dialog: Any, parsed: date, *, limit: int = 360, budget_s: float = 30.0,
    refind: Callable[[], Awaitable[Any]] | None = None,
) -> str:
    """Step an open calendar to the requested month, bounded and verified.

    Each step re-reads the month the calendar reports, so this follows the
    widget's own state rather than assuming a click advanced anything. It
    stops as soon as the month matches, when no navigation control exists, or
    when the calendar stops moving — never clicking blindly toward a target.
    """
    # Bounded by time, not a 24-step count: a birthdate or a 2016 deadline is
    # a hundred months away on a calendar that opens on today, and each step
    # is verified, so a responsive widget pages there in seconds.
    text = ""
    deadline = time.monotonic() + budget_s
    for _ in range(limit):
        if time.monotonic() > deadline:
            break
        if refind is not None and not await _safe_count(dialog):
            # jQuery UI rebuilds its grid on every page, taking the marker
            # that identified it along; find the calendar again.
            dialog = await refind() or dialog
        text = await _calendar_month_text(dialog)
        # The cells state their own dates; the header is prose. Prefer the
        # contract, and only read the header when no cell carries a date.
        current = await _displayed_month(dialog) or _visible_month(text) or await _month_around(dialog)
        if current == (parsed.year, parsed.month):
            return text
        if current is None:
            return text
        delta = (parsed.year - current[0]) * 12 + (parsed.month - current[1])
        if delta == 0:
            return text
        want_next = delta > 0
        needle = "next" if want_next else "prev"
        control = None
        # The calendar found may be just its day grid (jQuery UI's table),
        # with Prev/Next in a sibling header: look outward a level or two
        # before concluding there is no way to page.
        for scope in (dialog, dialog.locator("xpath=.."), dialog.locator("xpath=../..")):
            for attribute in ("aria-label", "title", "class"):
                try:
                    # Day cells advertise themselves as next/prev month too
                    # (flatpickr's padding days carry `nextMonthDay`), so a bare
                    # attribute match finds a dozen cells and no button. Exclude
                    # anything that is a day cell — by class and by the date it
                    # claims — and require a single remaining control.
                    found = scope.locator(f'[{attribute}*="{needle}" i]:not([class*="day" i])')
                    usable = []
                    for index in range(min(await found.count(), 8)):
                        item = found.nth(index)
                        if not await item.is_visible(timeout=300):
                            continue
                        if _parse_cell_date(await item.get_attribute("aria-label")) is not None:
                            continue
                        usable.append(item)
                    if len(usable) == 1:
                        control = usable[0]
                        break
                except Exception:
                    continue
            if control is not None:
                break
        if control is None:
            return text
        try:
            await control.click(timeout=1500)
        except Exception:
            return text
        if refind is not None and not await _safe_count(dialog):
            dialog = await refind() or dialog
        moved = await _displayed_month(dialog) or _visible_month(
            await _calendar_month_text(dialog)
        ) or await _month_around(dialog)
        if moved == current:
            return text  # the click changed nothing; stop rather than spin
    return text


async def _month_around(dialog: Any) -> tuple[int, int] | None:
    """The month a calendar shows, read from just outside its day grid.

    When the grid found is a bare table whose cells state no dates, the
    "December 2016" header is its sibling: read the nearest ancestor's text.
    """
    for scope in (dialog.locator("xpath=.."), dialog.locator("xpath=../..")):
        try:
            # Text node by text node: innerText glues inline siblings, and
            # "PrevDecember 2016" has no word boundary before the month.
            text = await _bounded(scope.evaluate("""el => {
              const out = [];
              const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
              for (let node = walker.nextNode(); node; node = walker.nextNode()) out.push(node.textContent);
              return out.join(' ');
            }"""), 1.0)
            month = _visible_month(str(text or ""))
        except Exception:
            month = None
        if month:
            return month
    return None


async def _safe_count(locator: Any) -> int:
    try:
        return int(await _bounded(locator.count(), 1.0) or 0)
    except Exception:
        return 0


async def _safe_mark_trigger(locator: Any) -> None:
    """Tag the control that opened a calendar so ownership can be measured."""
    try:
        await _bounded(
            locator.evaluate("el => el.setAttribute('data-odysseus-date-trigger', '1')"), 1.0,
        )
    except Exception:
        pass


async def _visible_calendar(root: Any, trigger_selector: str = "") -> Any | None:
    """Find the one visible calendar, by day-grid evidence rather than a role.

    Most date pickers are not ARIA dialogs: flatpickr renders a bare
    ``.flatpickr-calendar`` and jQuery UI a plain ``div``. Requiring
    ``[role="dialog"]`` therefore rejected working calendars outright, which
    pushed the caller into hand-written day clicking.

    A calendar is identified by what it demonstrably contains — a grid of day
    numbers — not by a library-specific selector. Strictness is preserved: if
    more than one such container is visible the caller gets nothing, because
    clicking into the wrong calendar is worse than refusing.
    """
    try:
        found = await _bounded(root.evaluate("""(triggerSelector) => {
          const trigger = triggerSelector ? document.querySelector(triggerSelector) : null;
          const isDay = text => /^([1-9]|[12]\\d|3[01])$/.test(String(text || '').trim());
          const seen = [];
          const roots = document.querySelectorAll(
            'dialog,[role="dialog"],[role="application"],[role="grid"],' +
            'div[class*="calendar" i],div[class*="datepicker" i],table[class*="calendar" i]'
          );
          roots.forEach((node, index) => {
            if (!node.getClientRects().length) return;
            const cells = Array.from(node.querySelectorAll('td,button,span,a,div'))
              .filter(el => isDay(el.textContent) && el.getClientRects().length);
            // A month grid always shows most of 28-31 days. Far fewer means a
            // stray container that merely happens to hold a number.
            if (cells.length < 20) return;
            // Keep only the innermost match: a page wrapper may contain the
            // real calendar and would otherwise count as a second one.
            seen.push({index, node});
          });
          let innermost = seen.filter(
            entry => !seen.some(other => other !== entry && entry.node.contains(other.node))
          );
          // A demo page may show several calendars at once. Prefer the one
          // this control owns: marked open/active by its library, or nearest
          // to the control in the DOM. Ownership evidence, not a guess at
          // which calendar the user meant.
          if (innermost.length > 1 && trigger) {
            const open = innermost.filter(entry =>
              /(^|[^a-z])(open|active|visible)([^a-z]|$)/i.test(entry.node.className || '')
            );
            if (open.length === 1) innermost = open;
          }
          if (innermost.length > 1 && trigger) {
            const scored = innermost.map(entry => {
              let depth = 0;
              for (let node = trigger; node; node = node.parentElement, depth++) {
                if (node === entry.node || node.contains(entry.node)) return {entry, depth};
              }
              return {entry, depth: 1e6};
            }).sort((a, b) => a.depth - b.depth);
            if (scored.length && scored[0].depth < scored[1].depth) innermost = [scored[0].entry];
          }
          if (innermost.length !== 1) return {count: innermost.length};
          innermost[0].node.setAttribute('data-odysseus-calendar', '1');
          return {count: 1};
        }""", trigger_selector or None), 3.0)
    except Exception:
        return None
    if not isinstance(found, dict) or found.get("count") != 1:
        return None
    return root.locator('[data-odysseus-calendar="1"]').first


async def _calendar_day(dialog: Any, parsed: date) -> Any | None:
    """Resolve exactly one day cell, refusing anything ambiguous.

    Adjacent-month padding cells repeat the same numbers, so a bare text match
    is not enough. Prefer an accessible name, then an exact-text cell that is
    not marked as padding or disabled.
    """
    # The cell states the date it represents; match that before any text.
    try:
        exact_date = dialog.locator(
            "[aria-label],[data-date],[datetime]"
        )
        usable = []
        for index in range(min(await exact_date.count(), 80)):
            cell = exact_date.nth(index)
            for attribute in ("data-date", "datetime", "aria-label"):
                value = await cell.get_attribute(attribute)
                if value and _parse_cell_date(value) == parsed:
                    usable.append(cell)
                    break
        if len(usable) == 1:
            return usable[0]
    except Exception:
        pass

    for role in ("button", "gridcell", "option", "link"):
        try:
            candidate = dialog.get_by_role(role, name=str(parsed.day), exact=True)
            if await candidate.count() == 1:
                return candidate
        except Exception:
            continue
    # Full-date accessible labels, as flatpickr and several others emit.
    for pattern in ("%B %-d, %Y", "%B %d, %Y"):
        try:
            named = dialog.locator(f'[aria-label="{parsed.strftime(pattern)}"]')
            if await named.count() == 1:
                return named
        except Exception:
            continue
    try:
        exact = dialog.locator(
            f'td:text-is("{parsed.day}"), button:text-is("{parsed.day}"), '
            f'span:text-is("{parsed.day}"), a:text-is("{parsed.day}")'
        )
        usable = []
        for index in range(min(await exact.count(), 12)):
            cell = exact.nth(index)
            classes = str(await cell.get_attribute("class") or "").casefold()
            if any(word in classes for word in ("prev", "next", "outside", "adjacent", "disabled")):
                continue
            if await cell.is_visible(timeout=300):
                usable.append(cell)
        if len(usable) == 1:
            return usable[0]
    except Exception:
        return None
    return None


# Display formats a text date field may already hold, most specific first.
_SHOWN_DATE_FORMATS = (
    "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y",
    "%B %d %Y", "%d.%m.%Y", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y",
)
_PLACEHOLDER_DATE_FORMATS = {
    "yyyy-mm-dd": "%Y-%m-%d", "mm/dd/yyyy": "%m/%d/%Y", "dd/mm/yyyy": "%d/%m/%Y",
    "dd.mm.yyyy": "%d.%m.%Y", "yyyy/mm/dd": "%Y/%m/%d", "mm-dd-yyyy": "%m-%d-%Y",
    "dd-mm-yyyy": "%d-%m-%Y", "m/d/yyyy": "%m/%d/%Y", "d/m/yyyy": "%d/%m/%Y",
}


def _field_date_format(value: str, placeholder: str) -> str:
    """The date format a text field shows or asks for; "" when unknown.

    A placeholder spells the format out, so it wins. A shown value is parsed
    against common formats; "05/06/2026" fits two, and without a placeholder
    the month-first reading is taken.
    """
    hint = re.sub(r"\s+", "", str(placeholder or "")).casefold()
    if hint in _PLACEHOLDER_DATE_FORMATS:
        return _PLACEHOLDER_DATE_FORMATS[hint]
    shown = " ".join(str(value or "").split())
    for fmt in _SHOWN_DATE_FORMATS:
        try:
            datetime.strptime(shown, fmt)
        except ValueError:
            continue
        return fmt
    return ""


_DATE_VALUE = re.compile(r"(\d{4})-(\d{2})(?:-(\d{2}))?")

# Marks the segments of the one date field being set, so each can be located
# without guessing. Cleared from the whole document before every use.
_DATE_SEGMENTS_SCRIPT = """el => {
  document.querySelectorAll('[data-odysseus-date-segment]')
    .forEach(node => node.removeAttribute('data-odysseus-date-segment'));
  // The ARIA range says what a segment holds in any language: 1-12 is a
  // month, a maximum of 28-31 a day, four digits a year. Names are only the
  // fallback for widgets that publish no range.
  const kindOf = node => {
    const min = Number(node.getAttribute('aria-valuemin')), max = Number(node.getAttribute('aria-valuemax'));
    if (max >= 1000) return 'year';
    if (max === 12 && min <= 1) return 'month';
    if (max >= 28 && max <= 31 && min <= 1) return 'day';
    const text = [node.getAttribute('aria-label'), node.getAttribute('placeholder')].join(' ').toLowerCase();
    if (/year|yyyy/.test(text)) return 'year';
    if (/month|(^|[^a-z])mm($|[^a-z])/.test(text)) return 'month';
    if (/(^|[^a-z])(day|dd)($|[^a-z])/.test(text)) return 'day';
    return '';
  };
  let node = el;
  for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
    const segments = Array.from(node.querySelectorAll('[role="spinbutton"]'));
    if (node.getAttribute('role') === 'spinbutton') segments.unshift(node);
    const kinds = segments.map(kindOf);
    if (kinds.filter(kind => kind === 'year').length > 1) return [];  // reached a sibling field
    if (kinds.includes('year') && kinds.includes('month') && new Set(kinds).size === kinds.length) {
      segments.forEach((segment, index) => segment.setAttribute('data-odysseus-date-segment', kinds[index]));
      return kinds;
    }
  }
  return [];
}"""


async def _segmented_date(
    root: Any, kinds: list[str], parts: dict[str, int], wanted: str,
) -> tuple[str, dict[str, Any]]:
    """Type each segment of a MM/DD/YYYY-style field, then read them back."""
    missing = [kind for kind in kinds if kind not in parts]
    if missing:
        raise WorkspaceError(
            f"This date field also has a {missing[0]} segment: give the value as YYYY-MM-DD."
        )
    for kind in kinds:
        width = 4 if kind == "year" else 2
        segment = root.locator(f'[data-odysseus-date-segment="{kind}"]').first
        await _type_like_a_user(segment, str(parts[kind]).zfill(width), timeout_ms=5000)
    observed: dict[str, Any] = {}
    for sample in range(2):
        await asyncio.sleep(0.2)  # the widget validates and re-renders on blur
        observed = await _bounded(root.evaluate("""() => Object.fromEntries(
          Array.from(document.querySelectorAll('[data-odysseus-date-segment]')).map(node => [
            node.getAttribute('data-odysseus-date-segment'),
            node.getAttribute('aria-valuenow') || ('value' in node ? node.value : node.textContent) || '',
          ]))"""), 2.0) or {}
    def number(raw: Any) -> int | None:
        digits = re.sub(r"\D", "", str(raw or ""))
        return int(digits) if digits else None
    verified = bool(observed) and all(number(observed.get(kind)) == parts[kind] for kind in kinds)
    evidence: dict[str, Any] = {
        "verified": verified,
        "verification": "date_segment_readback",
        "requested": wanted,
        "observed": " / ".join(str(observed.get(kind) or "") for kind in kinds),
    }
    if not verified:
        evidence["failure_reason"] = "readback_unavailable" if not observed else "state_mismatch"
    return f"typed {wanted} into the {'/'.join(kinds)} segments", evidence


async def _close_open_calendar(root: Any) -> None:
    """After a failed date attempt, close a calendar it left covering the form."""
    try:
        if await _visible_calendar(root) is None:
            return
        keyboard = getattr(root, "keyboard", None) or getattr(getattr(root, "page", None), "keyboard", None)
        if keyboard is not None:
            await _bounded(keyboard.press("Escape"), 1.0)
    except Exception:
        pass


async def _date_control(
    root: Any, selector: str, value: Any, *, label: str = "",
) -> tuple[str, dict[str, Any] | None]:
    """Set a date on whichever part of the date field the caller named.

    One verb for every shape, so a failure never sends the caller shopping
    for another mechanism: segmented MM/YYYY fields are typed segment by
    segment (Workday), native inputs are filled, calendars are paged.
    """
    wanted = str(value or "").strip()
    match = _DATE_VALUE.fullmatch(wanted)
    try:
        if not match:
            raise ValueError(wanted)
        year, month = int(match.group(1)), int(match.group(2))
        day = int(match.group(3)) if match.group(3) else None
        date(year, month, day or 1)
    except ValueError as exc:
        raise WorkspaceError(
            "date values are ISO: YYYY-MM-DD, or YYYY-MM for a month-and-year field"
        ) from exc
    locator = root.locator(selector).first
    try:
        kinds = await _bounded(locator.evaluate(_DATE_SEGMENTS_SCRIPT), 2.0)
    except Exception:
        kinds = []
    if not (isinstance(kinds, list) and kinds and set(kinds) <= {"year", "month", "day"}):
        kinds = []
    if kinds:
        parts = {"year": year, "month": month, **({"day": day} if day else {})}
        return await _segmented_date(root, list(kinds), parts, wanted)
    if day is None:
        raise WorkspaceError("This date field takes a full day: give the value as YYYY-MM-DD.")
    try:
        return await _calendar_date_control(root, selector, wanted, label=label)
    except WorkspaceError:
        await _close_open_calendar(root)
        raise


async def _calendar_date_control(
    root: Any, selector: str, value: Any, *, label: str = "",
) -> tuple[str, dict[str, Any] | None]:
    """Commit an ISO date through a native input or a scoped ARIA dialog.

    The custom path is deliberately strict: one visible dialog, the requested
    month/year visible inside it, one exact day button, and an independently
    readable labelled value afterward. Unsupported calendars fail without a
    selector escape or a guessed click.
    """
    from ascended_browser._app.browser_live_verification import consume_fill_evidence

    wanted = str(value or "").strip()
    try:
        parsed = date.fromisoformat(wanted)
    except ValueError as exc:
        raise WorkspaceError("date fields require an ISO YYYY-MM-DD value") from exc
    locator = root.locator(selector).first
    try:
        control = await _bounded(locator.evaluate(_DESCRIBE_CONTROL), 3.0)
    except Exception:
        control = {}
    tag = str((control or {}).get("tag") or "").casefold()
    typ = str((control or {}).get("type") or "").casefold()
    if tag == "input" or typ == "date":
        # A readonly input is a calendar trigger wearing an input's clothes
        # (flatpickr and most JS date pickers render exactly this). Filling it
        # cannot work, and Playwright retries "element is not editable" until
        # the deadline — an 8s stall ending in an empty TimeoutError. Fall
        # through to the calendar path instead of spending the budget.
        editable = True
        try:
            editable = await _bounded(locator.is_editable(timeout=500), 1.0)
        except Exception:
            editable = True
        if editable:
            text = wanted
            if typ != "date":
                # A text input with a JS picker takes dates in its own display
                # format: DemoQA's rejected "1995-06-15" and kept "19 Sep 2026".
                # Write the format the field already shows or asks for.
                try:
                    shown = await _bounded(locator.evaluate(
                        "el => [el.value || '', el.getAttribute('placeholder') || '']"), 1.0)
                except Exception:
                    shown = ["", ""]
                fmt = _field_date_format(*(shown or ["", ""]))
                if fmt:
                    text = date.fromisoformat(wanted).strftime(fmt)
            detail = await _fill_control(root, selector, text)
            evidence = consume_fill_evidence()
            await _close_open_calendar(root)  # typing leaves many pickers open
            return detail, evidence
        try:
            await locator.click(timeout=2000)
        except Exception as exc:
            raise WorkspaceError(
                "this date input is read-only and its calendar could not be opened; "
                f"{describe_exception(exc)}"
            ) from exc
        # The calendar is now open and this input was the trigger, so skip the
        # button/dialog target check below — it would reject the input we just
        # successfully used.
        await _safe_mark_trigger(locator)
        opened_readonly_input = True
    else:
        opened_readonly_input = False

    role = str((control or {}).get("role") or "").casefold()
    dialog = None
    if opened_readonly_input:
        pass  # trigger already activated above
    elif role == "dialog" or tag == "dialog":
        # The agent may have opened the calendar explicitly and then target the
        # one observed dialog.  Reuse that state rather than rejecting it and
        # forcing low-level day discovery.
        dialog = locator
    elif tag != "button" and role != "button":
        raise WorkspaceError("Observed date target is neither a date input nor a calendar button")
    else:
        await locator.click(timeout=5000)
    if dialog is None:
        dialog = await _visible_calendar(root, "[data-odysseus-date-trigger]")
        if dialog is None:
            raise WorkspaceError("Date control did not expose exactly one visible calendar")
    month_year = parsed.strftime("%B %Y")
    wanted_month = (parsed.year, parsed.month)
    # Compare structurally, not by substring: a calendar may render its month
    # and year in separate controls ("October" in a select, "2026" in an
    # input), which never contains the phrase "October 2026".
    dialog_text = await _calendar_month_text(dialog)
    showing = await _displayed_month(dialog) or _visible_month(dialog_text) or await _month_around(dialog)
    if showing != wanted_month:
        # A calendar opens on today, so any other month needs paging. Refusing
        # outright made every non-current month unreachable through the date
        # action, which is most of the real uses.
        async def refind() -> Any:
            return await _visible_calendar(root, "[data-odysseus-date-trigger]")

        dialog_text = await _page_calendar_to_month(dialog, parsed, refind=refind)
        if not await _safe_count(dialog):
            dialog = await refind() or dialog
        showing = await _displayed_month(dialog) or _visible_month(dialog_text) or await _month_around(dialog) or await _month_around(dialog)
    if showing != wanted_month:
        raise WorkspaceError(
            f"Calendar opened on another month and could not be paged to {month_year}; "
            "navigate the calendar to that month, then retry with the open dialog"
        )
    day = await _calendar_day(dialog, parsed)
    if day is None:
        raise WorkspaceError("Requested calendar day was missing or ambiguous in the active dialog")
    dialog_label = str(await dialog.get_attribute("aria-label", timeout=500) or "").strip()
    await day.click(timeout=5000)

    # When a marked trigger opened this calendar, that input holds the result.
    # Reading it is exact, where matching an accessible label across the page
    # depends on the widget exposing one that happens to equal our label.
    trigger_value = None
    try:
        trigger_value = await _bounded(root.evaluate(
            """() => {
              const el = document.querySelector('[data-odysseus-date-trigger]');
              return el && 'value' in el ? String(el.value || '') : null;
            }"""), 1.5)
    except Exception:
        trigger_value = None
    if isinstance(trigger_value, str) and trigger_value.strip():
        observed_trigger = trigger_value.strip()
        if _parse_cell_date(observed_trigger) == parsed or observed_trigger == wanted:
            return f"selected {wanted} from calendar", {
                "verified": True,
                "verification": "calendar_trigger_value_readback",
                "requested": wanted,
                "observed": observed_trigger,
            }

    readback = await _bounded(root.evaluate("""({label, expected}) => {
      const norm = value => String(value || '').replace(/\\s+/g, ' ').trim();
      const wanted = norm(label).toLowerCase();
      const nodes = Array.from(document.querySelectorAll('input,output,[role="status"],[aria-label]'));
      const matches = nodes.filter(el => {
        const aria = norm(el.getAttribute('aria-label')).toLowerCase();
        const own = norm(el.labels && el.labels[0] && el.labels[0].textContent).toLowerCase();
        return wanted && (aria === wanted || own === wanted);
      }).map(el => norm('value' in el ? el.value : (el.innerText || el.textContent)));
      return {matches, exact: matches.filter(value => value === expected)};
    }""", {
        "label": dialog_label or re.sub(r"^choose\s+", "", str(label or ""), flags=re.I),
        "expected": wanted,
    }), 2.0)
    observed = list((readback or {}).get("matches") or []) if isinstance(readback, dict) else []
    verified = bool(isinstance(readback, dict) and (readback.get("exact") or []))
    evidence = {
        "verified": verified,
        "verification": "calendar_dialog_value_readback",
        "requested": wanted,
        "observed": observed[:4],
    }
    if not verified:
        evidence["failure_reason"] = "readback_unavailable" if not observed else "state_mismatch"
    return f"selected {wanted} from calendar", evidence


def _requested_control_state(entry: dict[str, Any]) -> dict[str, Any]:
    """What the action asks the control to hold, normalized for a yield check.

    `value` is the requested text/selection; `checked` is the requested toggle
    state for check/radio entries. A missing value means "cannot judge", which
    the comparison treats as no objection.
    """
    kind = str(entry.get("kind") or entry.get("type") or "fill").strip().lower()
    if kind in {"check", "checkbox", "radio"}:
        checked = entry.get("checked") if "checked" in entry else entry.get("value", True)
        return {"checked": bool(checked)}
    if kind in {"select", "choose", "autocomplete"}:
        options = entry.get("options")
        if isinstance(options, list) and options:
            return {"value": str(options[0] if isinstance(options[0], str) else "")}
        value = entry.get("value") if entry.get("value") is not None else entry.get("option")
        return {"value": "" if value is None else str(value)}
    value = entry.get("value")
    if value is None:
        value = entry.get("text", entry.get("content"))
    return {"value": "" if value is None else str(value)}


def _observed_control_states(projection: Any) -> dict[str, dict[str, Any]]:
    """Per-ref control state as the model's last observation showed it.

    This is the baseline a "the person edited this" claim is judged against.
    The projection omits empty values, so a listed control with no ``value``
    was observed empty.
    """
    states: dict[str, dict[str, Any]] = {}
    if not isinstance(projection, dict):
        return states
    for element in projection.get("elements") or []:
        if not isinstance(element, dict) or not element.get("ref"):
            continue
        states[str(element["ref"])] = {
            "value": str(element.get("value") or ""),
            "selected_text": str(element.get("selected_text") or ""),
            "checked": bool(element.get("checked")),
        }
    return states


def _control_state_differs(
    current: dict[str, Any], requested: dict[str, Any],
    observed: dict[str, Any] | None = None,
) -> bool:
    """Whether a live control provably holds a value someone else put there.

    Differing from the request is not enough: every field the model is about
    to fill differs from its request. Compared that way, one human keystroke
    anywhere on the tab made a whole batch yield with "the person edited this
    field" while every field still sat at its default. The claim needs the
    control to have moved since the model looked; without that baseline only
    a non-empty value nobody asked for counts.
    """
    if "checked" in requested:
        if bool(current.get("checked")) == bool(requested["checked"]):
            return False
        return observed is not None and bool(current.get("checked")) != bool(observed.get("checked"))
    if "value" not in requested:
        return False
    current_value = str(current.get("value") or "").strip()
    requested_value = str(requested["value"] or "").strip()
    if current_value == requested_value:
        return False
    if observed is not None:
        seen = {
            text for text in (
                str(observed.get("value") or "").strip(),
                str(observed.get("selected_text") or "").strip(),
            ) if text
        } or {""}
        current_texts = {current_value}
        if str(current.get("selected_text") or "").strip():
            current_texts.add(str(current.get("selected_text") or "").strip())
        return not (seen & current_texts)
    # Writing an empty value (a deliberate clear) or into an empty control
    # overwrites nobody's work.
    return bool(current_value)


async def _control_holds_unrequested_value(
    root: Any, selector: str, requested: dict[str, Any],
    observed: dict[str, Any] | None = None,
) -> bool:
    """Read the live control and report whether someone changed it since observation."""
    if not requested:
        return False
    try:
        current = await _bounded(
            root.locator(selector).first.evaluate(_DESCRIBE_CONTROL), 5.0,
        )
    except Exception:
        # An unreadable control cannot prove a conflict; the ordinary write
        # and its readback verification still apply.
        return False
    if not isinstance(current, dict):
        return False
    return _control_state_differs(current, requested, observed)


async def _apply_form_control(
    root: Any, selector: str, entry: dict[str, Any],
    *, final_probes: list[dict[str, Any]] | None = None,
) -> tuple[str, str, dict[str, Any] | None]:
    """Execute and capture one batch field's private readback."""
    from ascended_browser._app.browser_live_verification import (
        consume_fill_evidence, consume_select_evidence, current_fill_probe,
    )

    entry_kind = str(entry.get("kind") or entry.get("type") or "fill").strip().lower()
    if entry_kind in {"date", "datepicker", "date_picker"}:
        outcome, evidence = await _date_control(
            root, selector, entry.get("value"), label=str(entry.get("label") or ""),
        )
        return "date", outcome, evidence
    if entry_kind in {"select", "choose", "autocomplete"}:
        options = entry.get("options")
        if isinstance(options, list):
            outcome, evidence = await _select_multiple_control(root, selector, options)
            return "select", outcome, evidence
        outcome = await _select_control(
            root, selector, entry.get("value"),
            query=entry.get("query"), option=entry.get("option"),
        )
        return "select", outcome, consume_select_evidence()
    if entry_kind in {"check", "checkbox", "radio"}:
        value = entry.get("checked") if "checked" in entry else entry.get("value", True)
        outcome = await _fill_control(root, selector, value)
        probe = current_fill_probe()
        if probe is not None and final_probes is not None:
            final_probes.append(probe)
        return "check", outcome, consume_fill_evidence()
    outcome = await _fill_control(root, selector, entry.get("value"))
    probe = current_fill_probe()
    if probe is not None and final_probes is not None:
        final_probes.append(probe)
    return "fill", outcome, consume_fill_evidence()


async def _bounded(awaitable, timeout: float):
    """Await with a timeout, without leaving an unretrieved exception behind.

    A bare ``asyncio.wait_for`` cancels the wrapper but leaves the underlying
    Playwright future to finish alone, and its exception is never read — every
    timed-out browser call then logs "Future exception was never retrieved".
    Raises TimeoutError on expiry so callers can report it.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except BaseException:
        task.cancel()
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        raise


async def _reload_page(page: Any, *, timeout: int = 60_000) -> Any:
    """Reload through the page and wait for its new document to commit.

    Camoufox 135's Playwright ``page.reload`` command can hang without issuing
    any request. The browser's ordinary location reload works on that engine
    and on newer Firefox, while keeping reload semantics for the current URL.
    """
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=timeout) as navigation:
        await page.evaluate("() => window.location.reload()")
    return await navigation.value


def normalize_browser_action(action: dict | None) -> dict:
    """Return a copy of *action* with canonical ``kind`` / ``value`` / ``key``.

    Accepts the common model shapes seen in production:
    ``{"kind": "click"}``, ``{"type": "click"}``, and ``{"action": "click"}``.
    Synonyms such as ``type``/``input`` for fill and ``goto`` for navigate are
    folded here so ``act`` does not reject an otherwise valid call. Fill also
    accepts ``text``/``content`` for the typed value; press accepts ``keys``.
    """
    out = dict(action or {})
    raw = out.get("kind")
    if raw is None or str(raw).strip() == "":
        raw = out.get("type")
    if raw is None or str(raw).strip() == "":
        # Only treat `action` as the kind when it is a bare string. Nested
        # objects belong to the older NL browser_act MCP contract.
        candidate = out.get("action")
        if isinstance(candidate, str):
            raw = candidate
    kind = str(raw or "").strip().lower()
    kind = _ACTION_KIND_ALIASES.get(kind, kind)
    # A model occasionally omits only the discriminator while still supplying
    # the complete, unambiguous shape of a text mutation.  Treat an observed
    # target plus an explicit value as ``fill``. An explicit ``expect`` object
    # is click-only in the public schema, so that shape is equally
    # unambiguous. Do not otherwise guess click/select/date semantics or infer
    # a target. Authority and scoped-ref validation still happen in the normal
    # executor before dispatch.
    if not kind and str(out.get("ref") or "").strip() and any(
        out.get(key) not in (None, "", []) for key in ("paths", "files", "path", "file_path")
    ):
        # File paths belong to upload alone. Without this the ref's identity
        # (Workday's "Select file" button) made it a click that "succeeded"
        # and attached nothing.
        kind = "upload"
    elif not kind and str(out.get("ref") or "").strip() and any(
        key in out and out.get(key) is not None for key in ("value", "text", "content")
    ):
        kind = "fill"
    elif not kind and str(out.get("ref") or "").strip() and isinstance(out.get("expect"), dict):
        kind = "click"
    elif not kind and str(out.get("ref") or "").strip() and isinstance(out.get("checked"), bool):
        kind = "check"
    elif not kind and str(out.get("ref") or "").strip() and (
        any(key in out for key in ("option", "options", "query"))
        or out.get("list_options") is True
    ):
        kind = "select"
    elif not kind and str(out.get("key") or out.get("keys") or "").strip() and not any(
        key in out for key in ("value", "text", "content", "url", "fields", "steps")
    ):
        # `key` belongs to press alone; a model writing a keyboard step as
        # {"key": "Escape"} means exactly that.
        kind = "press"
    elif not kind and isinstance(out.get("fields"), list) and out.get("fields") and all(
        isinstance(field, dict) for field in out["fields"]
    ):
        # A non-empty field list is unique to the public fill_form contract.
        # Recover only the omitted discriminator; normal authority, ref and
        # per-control verification still apply before any dispatch.
        kind = "fill_form"
    elif not kind and isinstance(out.get("steps"), list) and out.get("steps") and all(
        isinstance(step, dict) for step in out["steps"]
    ):
        # A non-empty ordered step list is unique to the guarded sequence
        # contract. Individual steps still resolve and verify through the
        # ordinary action lifecycle; this only recovers an omitted envelope
        # discriminator.
        kind = "sequence"
    out["kind"] = kind
    if kind == "select" and str(out.get("query") or "").strip() and not any(
        out.get(key) not in (None, "", []) for key in ("option", "options", "value")
    ):
        # A select naming only a filter has nothing to commit: the caller wants
        # to see what the filter offers ("select value is required" before).
        out["list_options"] = True
    if kind == "fill_form" and not out.get("fields"):
        # Two sibling array keys live on the same action object: `fields`
        # (fill_form) and `steps` (sequence). Models confuse either with
        # fill_form's `fields: object[]` — the action-level select
        # `options: string[]`, or the `steps` list whose per-item `kind`
        # values overlap. Repair only when every item carries a ref and a
        # fill-form-shaped payload; that makes the shapes disjoint, so this
        # is a mechanical envelope repair rather than guessed intent. Scoped
        # refs and normal verification still gate every field before dispatch.
        for source in ("options", "steps"):
            misplaced = out.get(source)
            if (
                isinstance(misplaced, list)
                and misplaced
                and all(
                    isinstance(field, dict)
                    and str(field.get("ref") or "").strip()
                    and any(key in field for key in ("value", "option", "options", "checked"))
                    # A step list holding navigate/click/press/scroll is a real
                    # sequence; reinterpreting it as a form batch would invent
                    # intent. Only per-field kinds may be repaired.
                    and str(field.get("kind") or "fill") in _FILL_FORM_FIELD_KINDS
                    for field in misplaced
                )
            ):
                out["fields"] = [dict(field) for field in misplaced]
                out.pop(source, None)
                break
    if not str(out.get("value") or "").strip():
        for alt in ("text", "content"):
            if out.get(alt) is not None and str(out.get(alt)).strip() != "":
                out["value"] = out[alt]
                break
    if not str(out.get("key") or "").strip() and out.get("keys") is not None:
        out["key"] = out["keys"]
    if out["kind"] == "upload":
        out["paths"] = _upload_paths(out)
    return out


def _infer_missing_kind_from_identity(action: dict, identity: dict | None) -> str:
    """Infer only an exact current control's mechanically unique primitive."""
    if str(action.get("kind") or "") or not isinstance(identity, dict):
        return str(action.get("kind") or "")
    role = str(identity.get("role") or "").casefold()
    tag = str(identity.get("tag") or "").casefold()
    input_type = str(identity.get("type") or "").casefold()
    if role in {"button", "link", "menuitem", "tab"} or tag in {"button", "a"}:
        return "click"
    if role in {"checkbox", "radio", "switch"} or input_type in {"checkbox", "radio"}:
        return "check" if isinstance(action.get("checked"), bool) else ""
    return ""


def scroll_action_settings(action: dict | None) -> tuple[int, float, float]:
    """Return bounded (delta_y, interval_seconds, duration_seconds)."""
    action = action or {}
    direction = str(action.get("direction") or ("up" if isinstance(action.get("delta_y"), (int, float)) and action["delta_y"] < 0 else "down")).strip().lower()
    raw_delta = action.get("delta_y")
    try:
        magnitude = abs(int(raw_delta if raw_delta is not None else 720))
    except (TypeError, ValueError):
        magnitude = 720
    magnitude = max(80, min(4000, magnitude))
    delta_y = -magnitude if direction in {"up", "back", "previous"} else magnitude
    try:
        interval = float(action.get("interval_ms", 750)) / 1000
    except (TypeError, ValueError):
        interval = 0.75
    try:
        duration = float(action.get("duration_seconds", 0))
    except (TypeError, ValueError):
        duration = 0.0
    return delta_y, max(0.1, min(5.0, interval)), max(0.0, min(3600.0, duration))


def action_budget_seconds(action: dict) -> float:
    """One operation budget, retaining explicitly requested scrolling time.

    Continuous scrolling is intentional work, not a browser wait/retry. Keep
    its existing bounded duration plus the configured execution overhead; all
    nested calls still inherit the original absolute deadline.
    """
    ceiling = float(get_setting("browser_work_action_timeout_seconds", 60) or 60)
    normalized = normalize_browser_action(action)
    if normalized.get("kind") == "scroll":
        return ceiling + scroll_action_settings(normalized)[2]
    return ceiling


def _upload_paths(action: dict) -> list[str]:
    """Every spelling of "the file(s) to attach", folded to one list.

    A file input takes a list even for one file, but models send a bare string
    far more often than they send a list, and `value` is what they reach for
    when they have been filling text fields all turn.
    """
    for key in ("paths", "files", "path", "file_path", "file", "value", "text"):
        raw = action.get(key)
        if raw is None:
            continue
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        cleaned = [str(item).strip() for item in items if str(item or "").strip()]
        if cleaned:
            return cleaned
    return []


def coerce_browser_act_action(args: dict | None) -> dict:
    """Pull a browser_act action out of tool args, including flat/string shapes.

    Models sometimes emit ``{"action":"click","ref":"…"}`` or put ``kind``/
    ``ref``/``url`` at the top level instead of nesting under ``action``.
    """
    args = args or {}
    action = args.get("action")
    if isinstance(action, str) and action.strip():
        promoted = {k: args[k] for k in _ACTION_FIELD_KEYS if k in args and k != "action"}
        promoted["kind"] = action
        return normalize_browser_action(promoted)
    if isinstance(action, dict) and action:
        nested = action.get("action")
        if isinstance(nested, dict) and nested:
            wrapper = {key: value for key, value in action.items() if key != "action"}
            conflicts = {
                key for key in wrapper.keys() & nested.keys()
                if wrapper[key] != nested[key]
            }
            if not conflicts:
                # Tolerate one accidental duplicate `action` envelope without
                # weakening authority: tab identity is still resolved and
                # checked separately, and conflicting effect fields continue
                # to fail instead of choosing one silently.
                return normalize_browser_action({**wrapper, **nested})
        return normalize_browser_action(action)
    promoted = {k: args[k] for k in _ACTION_FIELD_KEYS if k in args}
    return normalize_browser_action(promoted)


class SessionChangedError(WorkspaceError):
    pass


class _WorkspaceLockedRuntime:
    """Apply workspace action/domain policy around the existing agent runtime."""

    def __init__(self, manager: "BrowserWorkspaceManager", workspace_id: str, tab_id: str, runtime: Any) -> None:
        self._manager = manager
        self._workspace_id = workspace_id
        self._tab_id = tab_id
        self._runtime = runtime

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    async def observe(self) -> dict[str, Any]:
        record = self._manager._records[self._workspace_id]
        barrier = self._manager._runtime_effect_barrier(record.owner)
        async with barrier.shared(), self._manager._action_lock(self._workspace_id, self._tab_id):
            return await self._runtime.observe()

    async def screenshot(self) -> Any:
        record = self._manager._records[self._workspace_id]
        barrier = self._manager._runtime_effect_barrier(record.owner)
        async with barrier.shared(), self._manager._action_lock(self._workspace_id, self._tab_id):
            return await self._runtime.screenshot()

    async def execute(self, action: dict[str, Any], snapshot: dict[str, Any]) -> Any:
        name = str(action.get("action") or "").lower()
        navigates = name in {"click", "navigate", "goto", "submit", "press"} or str(action.get("submit_intent") or "none") != "none"
        record = self._manager._records[self._workspace_id]
        tab = record.tabs[self._tab_id]
        domain = self._manager._domain(str(action.get("url") or tab.url))
        scope = self._manager._owner_scope(self._workspace_id)
        sign_in = navigates or _can_change_sign_in(name, action)
        domain_lock = (
            self._manager._domain_locks.setdefault((scope, domain), asyncio.Lock())
            if sign_in else nullcontext()
        )
        self._manager._assert_worker_session(self._workspace_id, self._tab_id, domain)
        # Throttling and challenge backoff wait BEFORE any writer lock is taken.
        # Backoff is exponential; waiting inside any writer lock would freeze
        # unrelated tabs in this owner's serialized context.
        release_slots = None
        if navigates:
            await self._manager._wait_challenge_backoff(self._workspace_id, domain)
            await self._manager._rate_limit_domain(domain, record.owner)
            release_slots = await self._manager._acquire_domain_slots(domain, record.owner)
        try:
            barrier = self._manager._runtime_effect_barrier(record.owner)
            # One lock order everywhere: site lock, effect barrier, tab lock
            # (see act); the live-view transition takes barrier then tab.
            async with (
                domain_lock,
                barrier.shared(),
                self._manager._action_lock(self._workspace_id, self._tab_id),
            ):
                before_auth = await self._manager._auth_signature(self._runtime.page, domain) if sign_in else ""
                result = await self._runtime.execute(action, snapshot)
                after_auth = await self._manager._auth_signature(self._runtime.page, domain) if sign_in else ""
        finally:
            if release_slots is not None:
                release_slots()
        payload = getattr(result, "details", None) or getattr(result, "data", None) or {}
        rendered = json.dumps(payload, default=str).lower()
        if before_auth != after_auth:
            self._manager._record_domain_disruption(
                self._workspace_id, domain, "authentication state changed",
            )
            self._manager._acknowledge_worker_session(self._workspace_id, self._tab_id, domain)
        if "challenge_detected" in rendered or "captcha" in rendered or "unauthorized" in rendered or '"401"' in rendered:
            self._manager._record_domain_challenge(self._workspace_id, domain)
            self._manager._record_domain_disruption(
                self._workspace_id, domain, "login, step-up verification, or challenge state changed",
            )
            raise SessionChangedError(
                "session_changed_under_me: the shared owner session entered login, 2FA, or challenge state"
            )
        return result


#: Actions that cannot sign the owner in or out of a site. They skip the
#: per-(owner, site) lock and the login-cookie comparison around it, so one
#: owner's sessions can type into the same site at once. Anything that can
#: navigate, submit, or fire a change handler keeps both.
_SIGN_IN_NEUTRAL_KINDS = frozenset({
    "fill", "fill_form", "type", "scroll",
    # Choosing an option, ticking a box, attaching a file or hovering never
    # signs anyone in or out; serializing them per site queued a three-tab
    # Workday batch for up to 37 s (2026-10-04).
    "select", "check", "uncheck", "upload", "hover", "wait",
})
#: Keys that can submit a form or follow a link, and so can change sign-in.
_SUBMITTING_KEYS = frozenset({"enter", "return", "numpadenter"})


def _can_change_sign_in(kind: str, action: dict[str, Any]) -> bool:
    if str(action.get("submit_intent") or "none") != "none":
        return True
    if kind == "press":
        key = str(action.get("key") or action.get("keys") or "").strip().lower()
        return key.split("+")[-1] in _SUBMITTING_KEYS
    return kind not in _SIGN_IN_NEUTRAL_KINDS


#: The live view's Phone / Tablet / Desktop profile buttons, in CSS pixels.
#: browser_viewport's presets are the same three sizes so an agent resize and
#: a person's button press are the same request.
VIEWPORT_PRESETS: dict[str, tuple[int, int]] = {
    "phone": (390, 844),
    "tablet": (768, 1024),
    "desktop": (1365, 768),
}


def _next_lease_epoch(tab: Any) -> int:
    """The epoch for a tab's next lease: above every epoch it ever carried."""
    floor = int(getattr(tab, "lease_epoch_floor", 0) or 0)
    return max(floor, int(getattr(getattr(tab, "lease", None), "generation", 0) or 0)) + 1


class BrowserWorkspaceManager:
    """Own Camoufox lifecycles, contexts, tab capabilities, and durable manifests."""

    # Set when the process begins stopping. A stop signal reaches the browser
    # children (same process group) seconds before lifespan shutdown runs
    # close_all; their deaths must not read as a crash to recover from.
    _shutting_down = False

    def __init__(
        self,
        *,
        store: WorkspaceStore | None = None,
        auth_store: AuthStateStore | None = None,
        backend: BrowserBackend | None = None,
        liveview: LiveViewSupervisor | None = None,
    ) -> None:
        self.store = store or WorkspaceStore()
        self.auth_store = auth_store or AuthStateStore()
        self.mode = "process_per_owner"
        self.lock_scope = str(get_setting("browser_workspace_lock_scope", "tab") or "tab")
        self._records: dict[str, WorkspaceRecord] = {}
        self._pages: dict[tuple[str, str], Any] = {}
        self._process_lock = asyncio.Lock()
        self._context_locks: dict[str, asyncio.Lock] = {}
        self._owner_capacity_locks: dict[str, asyncio.Lock] = {}
        self._owner_capacity_reserved: dict[str, int] = {}
        # A manual browser-tab request can outlive the HTTP response that
        # started it. Serialize the same opaque operation while the durable
        # record is being created; after that record is the idempotency owner.
        self._manual_open_locks: dict[tuple[str, str, str], tuple[asyncio.Lock, int]] = {}
        self._tab_locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Sleeping tabs revive lazily. Serialize that transition per tab so a
        # user click and an agent call cannot create two replacement pages
        # beneath the same durable tab id.
        self._tab_revive_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._viewer_revive_tasks: dict[tuple[str, str], asyncio.Task] = {}
        # Join the existing open while its first page is not ready. These are
        # execution handles only; the durable tab remains the state owner.
        self._opening_tabs: dict[tuple[str, str], asyncio.Task] = {}
        self._owner_action_slots: dict[str, asyncio.Semaphore] = {}
        self._domain_locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Keyed by site (server-wide) and by (owner, site).
        self._domain_slots: dict[Any, asyncio.Semaphore] = {}
        self._domain_tokens: dict[Any, list[float]] = {}
        self._challenge_backoff: dict[tuple[str, str], tuple[int, float]] = {}
        # A browser_open/browser_act result already contains an authoritative
        # observation. Keep it briefly so an immediate browser_observe is a
        # cheap replay instead of another DOM walk and model round trip.
        self._observation_cache: dict[tuple[str, str], tuple[float, str, dict[str, Any]]] = {}
        # A lost parent document can be reloaded automatically, but the next
        # agent-facing observation must say that its prior fields/uploads were
        # discarded. Keep that one-shot fact beside the lifecycle owner until
        # a browser result consumes it.
        self._pending_recovery_notices: dict[tuple[str, str], dict[str, Any]] = {}
        # What each resident page reported (console errors, uncaught
        # exceptions, failed requests) since the last result that told the
        # model. Runtime evidence only: it is attached with the page's other
        # listeners and dropped with the page, never persisted.
        self._diagnostics: dict[tuple[str, str], Any] = {}
        # Media emulation the agent asked for on a tab (dark scheme, reduced
        # motion, forced colors) and offline mode per owner context. Runtime
        # state: restore clears it, and a page carries none after a restart.
        self._emulation: dict[tuple[str, str], dict[str, Any]] = {}
        # owner -> the runtime generation offline mode was applied to; a
        # relaunched or woken context starts online whatever was set before.
        self._offline_owners: dict[str, int] = {}
        # The last picture taken of each tab, so compare_with="previous" can
        # diff the next one against it without a file round trip.
        self._last_captures: dict[tuple[str, str], dict[str, Any]] = {}
        # A resident tab may be selected by LRU only when no operation is using
        # it. Lease ownership alone is not an activity signal: parent leases are
        # intentionally long-lived across rounds.
        self._operation_pins: dict[tuple[str, str], int] = {}
        # Presentation of agent presence follows actual in-flight browser
        # operations, not a worker's lifetime or a durable "acted recently"
        # stamp. Keep overlapping operations as separate runtime tokens so one
        # completion cannot clear a sibling that is still using the same tab.
        # This state is intentionally ephemeral: after a process restart there
        # can be no surviving in-process operation to advertise.
        self._agent_tab_operations: dict[
            tuple[str, str], dict[str, dict[str, str]]
        ] = {}
        # The durable activity field is refreshed for meaningful user input,
        # not pointer motion.  Keep this tiny process-local write throttle so a
        # held scroll key or wheel cannot turn the manifest into an fsync loop.
        # The durable record remains the source of truth and is always updated
        # in memory immediately.
        self._tab_activity_last_saved: dict[tuple[str, str], float] = {}
        # Pixel-wheel pipelines: one ordered pump per controlling viewer, so a
        # slow Playwright wheel round trip never blocks the relay's read loop
        # (which would stall pointer moves and freeze the visible cursor).
        self._liveview_wheel_states: dict[str, dict[str, Any]] = {}
        self._liveview_wheel_fallback_logs: dict[str, tuple[str, float]] = {}
        self._liveview_wheel_installed_pages: set[int] = set()
        self._live_form_dirty_installed_pages: set[int] = set()
        self._live_form_dirty_pending_save: set[tuple[str, str]] = set()
        # Browser-shell Find is process-local and scoped to the exact selected
        # live-view capability. It never becomes page/session history.
        self._live_find_states: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._live_find_revisions: dict[tuple[str, str, str], int] = {}
        self.liveview = liveview or LiveViewSupervisor()
        self.live_views = LiveViewRegistry()
        self._liveview_selected: dict[str, tuple[str, str]] = {}
        self._liveview_switch_locks: dict[str, asyncio.Lock] = {}
        self._runtime_effect_barriers: dict[str, _RuntimeEffectBarrier] = {}
        # A user lease is durable task authority; a controller view is the
        # ephemeral device connection allowed to exercise it. Keep the latter
        # runtime-scoped so a second same-account viewer remains a spectator.
        self._liveview_controller_by_runtime: dict[str, str] = {}
        # Window sizing is useful before a viewer takes input control. Keep its
        # ephemeral owner separate from the durable user lease/controller; the
        # current controller always supersedes a read-only viewport owner.
        self._liveview_viewport_owner_by_runtime: dict[str, str] = {}
        # The shared window's size mode: "dynamic" follows the watching live
        # view panel; "fixed" holds a size a person (profile button, custom
        # size) or the agent (browser_viewport set) chose. Any person's panel
        # resize returns it to dynamic. Viewers adopt the newest revision.
        self._liveview_size_mode_by_runtime: dict[str, dict[str, Any]] = {}
        self._liveview_resize_locks: dict[str, asyncio.Lock] = {}
        self._liveview_geometry_revision: dict[str, int] = {}
        self._liveview_control_revision: dict[str, int] = {}
        self._liveview_disconnect_tasks: dict[str, asyncio.Task] = {}
        self._live_metrics_logged_at: dict[str, float] = {}
        # WebSocket close callbacks are not a liveness oracle: a half-open
        # connection can keep the registry's current generation present long
        # after the user can no longer operate it.  Keep these records owned by
        # the workspace manager, keyed by the non-restorable transport
        # generation; they are never durable authority.
        self._liveview_client_activity: dict[tuple[str, int], float] = {}
        self._liveview_liveness_tasks: dict[tuple[str, int], asyncio.Task] = {}
        self._liveview_liveness_expiring: set[tuple[str, int]] = set()
        self._liveview_page_loss_tasks: dict[tuple[str, str], asyncio.Task] = {}
        # Browser children can hide the cold owner launch behind their first
        # model round.  Keep one task per workspace so the eventual
        # browser_open awaits that exact wake instead of launching twice.
        self._prewarm_tasks: dict[str, asyncio.Task] = {}
        # A browser born on a virtual display can never grow a live view.
        # Relaunching it display-backed is a heavy, user-visible action, so its
        # scheduling, cooldown, and give-up budget are owned here next to the
        # mint gate that requests it.
        self._capture_relaunch_tasks: dict[str, asyncio.Task] = {}
        self._capture_relaunch_attempts: dict[str, float] = {}
        self._capture_relaunch_failures: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._monitor_task: asyncio.Task | None = None
        self._memory_pressure_task: asyncio.Task | None = None
        self._memory_pressure_active = False
        self._migration_lock = asyncio.Lock()
        self._worker_clocks_by_key: dict[tuple[str, str], ParkingClocks] = {}
        self._intentional_page_closes: set[tuple[str, str]] = set()
        # Owner-scoped browser-state subscribers (SSE). Payloads only ever say
        # "state changed, re-fetch" — never the state itself.
        self._browser_event_subs: dict[str, set[asyncio.Queue]] = {}
        # A page-local Firefox navigation failure can be recovered by swapping
        # the Playwright Page beneath the same TabRecord. This marker prevents
        # the old page's close event from invalidating that stable tab/view.
        self._replacing_page_closes: set[tuple[str, str]] = set()
        # Both context.on("page") and the opener's page.on("popup") fire for the
        # same popup. Each adoption path awaits before registering in _pages, so
        # without a claim taken *before* that await, both create a TabRecord and
        # a 4-popup burst yields 5 tabs.
        self._adopting_pages: set[int] = set()
        self._owner_tab_limit = int(get_setting("browser_workspace_owner_tab_limit", 24) or 24)
        # Resident tabs holding work that must not be dropped (unsent drafts,
        # worker leases, deliverables) may grow the owner past the soft limit
        # up to here; clean tabs are still slept first. Keep it under the
        # recorded page-ceiling artifact.
        self._owner_tab_hard_limit = int(get_setting("browser_workspace_owner_tab_hard_limit", 20) or 20)
        self._domain_session_state: dict[tuple[str, str], dict[str, Any]] = {}
        self._tab_session_versions: dict[tuple[str, str], int] = {}
        # Captured BEFORE any Camoufox launch can mutate the environment. See
        # _headless_mode: a virtual display leaves DISPLAY set after teardown, so
        # re-reading os.environ per launch made every wake after the first fail.
        self._host_display = os.environ.get("DISPLAY") or ""
        self.backend = backend or PersistentOwnerBackend(
            self.store.root,
            headless_mode=self._headless_mode,
            on_disconnect=self._schedule_owner_recovery,
            liveview=self.liveview,
        )
        self._install_agent_cursor_publisher()

    def _install_agent_cursor_publisher(self) -> None:
        """Bind the live-view agent-cursor publisher at the one boundary.

        The manager owns the workspace-to-runtime mapping and the live-view
        supervisor, so it is the publisher; the browser tools stay decoupled
        and emit through src/browser_agent_cursor.py's no-op default.
        """
        from ascended_browser._app import browser_agent_cursor

        browser_agent_cursor.install_sink(self._publish_agent_cursor)

    # Delivery to liveviewd happens on one background worker per manager, in
    # order, off the tool path. Inline delivery under a 150ms deadline dropped
    # the cue whenever the daemon client was slow (a health re-check, a busy
    # loop), which is why the pointer often never appeared at all.
    _AGENT_CURSOR_QUEUE_LIMIT = 96
    _AGENT_CURSOR_STALE_SECONDS = 2.5
    _AGENT_CURSOR_POST_SECONDS = 1.5
    # How long a geometry read may finish late and still move the pointer
    # (only when no newer cue was published for the tab meanwhile).
    _AGENT_CURSOR_LATE_GEOMETRY_SECONDS = 1.0

    async def _publish_agent_cursor(
        self,
        workspace_id: str,
        tab_id: str,
        kind: str,
        x: float,
        y: float,
        label: str,
        tone: int,
        detail: str = "",
    ) -> None:
        """Queue one cursor publication for the selected live-view tab."""
        record = next(
            (candidate for candidate in self._records.values()
             if candidate.workspace_id == workspace_id),
            None,
        )
        if record is None:
            return
        runtime = self.backend.runtime(record.owner)
        runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
        native = None
        if getattr(runtime, "presentation", "") == "embedded":
            from ascended_browser.runtime.desktop import registry
            from ascended_browser._app import browser_agent_cursor

            page = self._pages.get((workspace_id, tab_id))
            if page is None or tab_id not in record.tabs:
                return
            identity = self.backend.page_identity(record.owner, page)
            host = registry.select(record.owner)
            if host is None or not identity.get("guest_key") or not identity.get("target_id"):
                return
            native = {
                "runtime": runtime, "page": page, "host": host, "registry": registry,
                "owner": record.owner, "identity": identity,
                "sequence": browser_agent_cursor.sequence(workspace_id, tab_id),
                "session_id": record.session_id,
                "viewport": browser_agent_cursor.cached_viewport_size(page),
            }
            runtime_id = "embedded:" + str(identity["target_id"])
        elif self._liveview_selected.get(owner_key(record.owner)) != (workspace_id, tab_id) or not runtime_id:
            return
        if not label:
            label, tone = self._agent_cursor_identity(workspace_id, tab_id, tone)
        event = {
            "kind": kind, "x": x, "y": y, "visible": True,
            "label": label or "", "tone": tone, "detail": detail or "",
        }
        if native is not None:
            event["_native"] = native
        queue = getattr(self, "_agent_cursor_queue", None)
        if queue is None:
            queue = self._agent_cursor_queue = collections.deque(
                maxlen=self._AGENT_CURSOR_QUEUE_LIMIT,
            )
        queue.append((
            time.monotonic(), runtime_id, owner_key(record.owner), (workspace_id, tab_id), event,
        ))
        worker = getattr(self, "_agent_cursor_task", None)
        if worker is None or worker.done():
            worker = asyncio.create_task(
                self._deliver_agent_cursor(), name="browser-agent-cursor-delivery",
            )
            self._agent_cursor_task = worker
            tasks = getattr(self, "_tasks", None)
            if tasks is not None:
                tasks.add(worker)
                worker.add_done_callback(tasks.discard)

    async def _deliver_agent_cursor(self) -> None:
        """Drain queued cursor events to the daemon, oldest first."""
        queue = self._agent_cursor_queue
        while queue:
            queued_at, runtime_id, selection_key, scope, event = queue.popleft()
            if time.monotonic() - queued_at > self._AGENT_CURSOR_STALE_SECONDS:
                # A cue this old would show the pointer somewhere it no longer is.
                continue
            native = event.get("_native")
            if native is not None:
                owner = native["owner"]
                page = native["page"]
                def is_current() -> bool:
                    return (
                        time.monotonic() - queued_at <= self._AGENT_CURSOR_STALE_SECONDS
                        and self.backend.runtime(owner) is native["runtime"]
                        and self._pages.get(scope) is page
                        and self.backend.page_identity(owner, page) == native["identity"]
                    )
                if not is_current():
                    continue
                frame = {
                    "type": "agent_cursor", "workspace_id": scope[0],
                    "session_id": native["session_id"], "tab_id": scope[1],
                    "runtime_generation": int(getattr(native["runtime"], "generation", 0) or 0),
                    "page_identity": dict(native["identity"]), "sequence": native["sequence"],
                    "viewport_width": native["viewport"][0] if native["viewport"] else 0,
                    "viewport_height": native["viewport"][1] if native["viewport"] else 0,
                    "event": {key: value for key, value in event.items() if key != "_native"},
                }
                try:
                    await asyncio.wait_for(
                        native["registry"].send_agent_cursor(
                            owner, native["host"], frame, is_current=is_current,
                        ),
                        timeout=self._AGENT_CURSOR_POST_SECONDS,
                    )
                except Exception:
                    log.debug("native agent cursor delivery failed", exc_info=True)
                continue
            if self._liveview_selected.get(selection_key) != scope:
                # The watcher switched tabs while this cue waited. The daemon
                # broadcasts per runtime, not per tab, so sending it now would
                # paint the old tab's pointer over the newly selected page.
                continue
            try:
                await asyncio.wait_for(
                    self.liveview.agent_cursor(runtime_id, **event),
                    timeout=self._AGENT_CURSOR_POST_SECONDS,
                )
            except Exception:
                log.debug("agent cursor delivery failed", exc_info=True)

    def _agent_cursor_identity(self, workspace_id: str, tab_id: str, tone: int) -> tuple[str, int]:
        """Name the pointer after whoever is acting on the tab right now."""
        operations = (getattr(self, "_agent_tab_operations", None) or {}).get(
            (workspace_id, tab_id), {},
        )
        actors = {str(item.get("actor") or "") for item in operations.values()}
        if "worker" in actors and "parent" not in actors:
            return "Sub-agent", 3
        return "", tone

    async def _agent_cursor_status(self, page, detail: str, *, workspace_id: str, tab_id: str) -> None:
        """Narrate a step with no pointer target (navigating, reading, waiting)."""
        from ascended_browser._app import browser_agent_cursor

        try:
            await asyncio.wait_for(browser_agent_cursor.emit(
                page, "status", detail=detail,
                workspace_id=workspace_id, tab_id=tab_id,
            ), timeout=0.3)
        except Exception:
            pass

    async def _agent_cursor_move_for_dispatch(
        self, page, root, selector: str, *, kind: str = "pointer_move",
        workspace_id: str, tab_id: str, detail: str = "", follow: tuple[str, ...] = (),
    ) -> None:
        """Best-effort spectator cue; it must never delay or authorize input.

        `follow` names further kinds published at the same point (a click's
        down/up, a typing pulse) once the target's position is known.
        """
        from ascended_browser._app import browser_agent_cursor

        if kind in {"wheel", "status"}:
            await browser_agent_cursor.emit(
                page, kind, require_position=False, detail=detail,
                workspace_id=workspace_id, tab_id=tab_id,
            )
            return
        try:
            probe_name = f"browser-cursor-geometry:{workspace_id}:{tab_id}"
            if any(task.get_name() == probe_name and not task.done() for task in self._tasks):
                return
            locator = root.locator(selector)
            probe = asyncio.create_task(locator.bounding_box(timeout=150), name=probe_name)
            self._tasks.add(probe)
            sequence = browser_agent_cursor.sequence(workspace_id, tab_id)

            async def publish(box, *, if_sequence=None):
                center = browser_agent_cursor.box_center(box)
                if center is None:
                    return
                for index, cue in enumerate((kind, *follow)):
                    published = await browser_agent_cursor.emit(
                        page, cue, x=center[0], y=center[1], detail=detail,
                        workspace_id=workspace_id, tab_id=tab_id,
                        if_sequence=if_sequence if index == 0 else None,
                    )
                    if index == 0 and not published:
                        # A newer cue already moved the pointer on; a click or
                        # typing pulse here would act out at the old spot.
                        return

            def settled(done):
                self._tasks.discard(done)
                if done.cancelled() or done.exception() is not None:
                    return
                if time.monotonic() - started > self._AGENT_CURSOR_LATE_GEOMETRY_SECONDS:
                    return
                if not late_allowed:
                    return
                # The read finished after the dispatch deadline. Still show
                # where the agent acted, unless a newer cue already moved the
                # pointer on (then this one would drag it backwards).
                late = asyncio.ensure_future(publish(done.result(), if_sequence=sequence))
                self._tasks.add(late)
                late.add_done_callback(lambda task: (
                    self._tasks.discard(task),
                    task.cancelled() or task.exception(),
                ))

            started = time.monotonic()
            late_allowed = False
            probe.add_done_callback(settled)
            # Cancelling a Playwright RPC wrapper can strand its protocol
            # future. Retain this one read-only probe until it settles.
            done, _ = await asyncio.wait({probe}, timeout=0.15)
            if not done:
                late_allowed = True
                return
            await asyncio.wait_for(publish(probe.result()), timeout=0.3)
        except Exception:
            # The cursor is decoration; the ordinary action still owns its
            # own locator resolution, authority, and outcome evidence.
            pass

    def _agent_cursor_key_cue_soon(
        self, page, *, typed: str, key: Any, workspace_id: str, tab_id: str,
    ) -> None:
        """Point at the focused field and narrate the keys, off the tool path.

        Typing is already paced per character, so the cue (one bounded page
        read) runs beside it rather than in front of it.
        """
        from ascended_browser._app import browser_agent_cursor

        sequence = browser_agent_cursor.sequence(workspace_id, tab_id)

        async def cue() -> None:
            point, _secret = await self._agent_cursor_focus_point(page)
            if typed:
                # Never the typed text itself: whether a field holds a secret
                # is not reliably knowable (the focus read can fail, and a
                # token pasted into a plain field is still a secret), and
                # the tool result does not disclose the value either.
                detail = "Typing"
            else:
                detail = f"Pressing {str(key or '').strip() or 'a key'}"[:browser_agent_cursor.CAPTION_LIMIT]
            if point is None:
                await browser_agent_cursor.emit(
                    page, "status", detail=detail, workspace_id=workspace_id,
                    tab_id=tab_id, if_sequence=sequence,
                )
                return
            if not await browser_agent_cursor.emit(
                page, "pointer_move", x=point[0], y=point[1], detail=detail,
                workspace_id=workspace_id, tab_id=tab_id, if_sequence=sequence,
            ):
                return
            await browser_agent_cursor.emit(
                page, "key", x=point[0], y=point[1], detail=detail,
                workspace_id=workspace_id, tab_id=tab_id,
            )

        try:
            task = asyncio.create_task(cue(), name=f"browser-cursor-key:{workspace_id}:{tab_id}")
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(lambda done: (
            self._tasks.discard(done), done.cancelled() or done.exception(),
        ))

    async def _agent_cursor_scroll_cue(
        self, page, root, selector: str, *, detail: str, workspace_id: str, tab_id: str,
    ) -> None:
        """Bring the pointer over what is being scrolled, then show the wheel.

        A targeted scroll aims at its container; a page scroll at the middle
        of the viewport, where a person would rest the mouse to scroll.
        """
        from ascended_browser._app import browser_agent_cursor

        if root is not None and selector:
            await self._agent_cursor_move_for_dispatch(
                page, root, selector, detail=detail, follow=("wheel",),
                workspace_id=workspace_id, tab_id=tab_id,
            )
            return
        try:
            width, height = await asyncio.wait_for(browser_agent_cursor._page_size(page), timeout=0.3)
            if width <= 0 or height <= 0:
                return
            for cue in ("pointer_move", "wheel"):
                await asyncio.wait_for(browser_agent_cursor.emit(
                    page, cue, x=width / 2.0, y=height / 2.0, detail=detail,
                    workspace_id=workspace_id, tab_id=tab_id,
                ), timeout=0.3)
        except Exception:
            pass

    async def _agent_cursor_focus_point(
        self, page, root=None, selector: str = "",
    ) -> tuple[tuple[float, float] | None, bool]:
        """Center of the element keys will go to, and whether it is a password.

        Bounded to one short page round trip; on any failure the cue is
        simply not positioned.
        """
        script = (
            "(el) => { el = el || document.activeElement;"
            " if (!el || el === document.body) return null;"
            " const r = el.getBoundingClientRect();"
            " if (!r.width && !r.height) return null;"
            " return [r.left + r.width / 2, r.top + r.height / 2,"
            " (el.type || '').toLowerCase() === 'password']; }"
        )
        try:
            if root is not None and selector:
                found = await asyncio.wait_for(
                    root.locator(selector).first.evaluate(script), timeout=0.25,
                )
            else:
                found = await asyncio.wait_for(
                    page.evaluate(f"({script})(null)"), timeout=0.25,
                )
        except Exception:
            return None, False
        if not isinstance(found, (list, tuple)) or len(found) < 3:
            return None, False
        try:
            return (float(found[0]), float(found[1])), bool(found[2])
        except (TypeError, ValueError):
            return None, False

    def _headless_mode(self) -> bool | str:
        """Decide the display mode from the host, not from a mutated environment.

        Camoufox's ``headless="virtual"`` starts an Xvfb server and exports its
        DISPLAY (e.g. ``:4``) into this process. Tearing the browser down kills the
        server but leaves DISPLAY set, so a later launch that re-read os.environ
        concluded a real display existed and tried to attach to a dead one --
        ``Error: cannot open display: :4``. Every workspace wake after the first
        failed for the life of the process.
        """
        if os.name == "nt":
            return False
        return False if self._host_display else "virtual"

    @staticmethod
    def workspace_id(owner: str, session_id: str) -> str:
        import hashlib
        return "ws_" + hashlib.sha256(f"{owner}\0{session_id}".encode()).hexdigest()[:24]

    @staticmethod
    def _user_control_deadline() -> float:
        try:
            seconds = int(get_setting("browser_workspace_user_control_lease_seconds", 300) or 300)
        except (TypeError, ValueError):
            seconds = 300
        return time.time() + max(60, min(3600, seconds))

    @staticmethod
    def _return_tab_lease_to_agent(tab: TabRecord) -> str:
        resume_actor = str(tab.takeover_resume_actor or "parent")
        resume_actor_id = str(tab.takeover_resume_actor_id or "parent")
        worker_id = resume_actor_id if resume_actor == "worker" else ""
        next_generation = _next_lease_epoch(tab)
        if worker_id:
            tab.lease = TabLease(
                actor="worker", actor_id=worker_id, generation=next_generation,
            )
            tab.owner_kind = "worker"
            tab.owner_id = worker_id
        else:
            tab.lease = TabLease(
                actor="parent", actor_id="parent", generation=next_generation,
            )
            tab.owner_kind = "parent"
            tab.owner_id = "parent"
        tab.takeover_resume_actor = ""
        tab.takeover_resume_actor_id = ""
        tab.takeover_group_id = ""
        return worker_id

    @staticmethod
    def _same_takeover_group(first: TabRecord, second: TabRecord) -> bool:
        """Whether two tabs belong to the same live human takeover."""
        group_id = str(getattr(first, "takeover_group_id", "") or "")
        return bool(
            group_id
            and secrets.compare_digest(
                group_id, str(getattr(second, "takeover_group_id", "") or ""),
            )
            and first.lease is not None
            and second.lease is not None
            and first.lease.actor == second.lease.actor == "user"
            and first.lease.actor_id == second.lease.actor_id
        )

    def _takeover_group_tabs(
        self, record: WorkspaceRecord, tab: TabRecord,
    ) -> list[TabRecord]:
        """Return the durable tabs that must hand back as one takeover."""
        group_id = str(getattr(tab, "takeover_group_id", "") or "")
        if not group_id:
            return [tab]
        grouped = [
            candidate
            for candidate in record.tabs.values()
            if self._same_takeover_group(tab, candidate)
        ]
        return grouped or [tab]

    def _takeover_group_has_connected_view(
        self, record: WorkspaceRecord, tab: TabRecord,
    ) -> bool:
        runtime = self.backend.runtime(record.owner)
        runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
        if not runtime_id:
            return False
        members = {member.tab_id for member in self._takeover_group_tabs(record, tab)}
        return any(
            candidate.workspace_id == record.workspace_id
            and candidate.tab_id in members
            and self.live_views.current_connected(candidate)
            for candidate in self.live_views.for_runtime(runtime_id)
        )

    def _capture_live_transition(
        self,
        record: WorkspaceRecord,
        tab: TabRecord,
        *,
        runtime_id: str,
    ) -> dict[str, Any]:
        """Capture the durable and ephemeral owners changed by handoff."""
        selection_key = owner_key(record.owner)
        clock_prefix = record.workspace_id
        return {
            "record": copy.deepcopy(record),
            "tab": tab,
            "runtime_id": runtime_id,
            "controller_present": runtime_id in self._liveview_controller_by_runtime,
            "controller": self._liveview_controller_by_runtime.get(runtime_id, ""),
            "viewport_owner_present": runtime_id in self._liveview_viewport_owner_by_runtime,
            "viewport_owner": self._liveview_viewport_owner_by_runtime.get(runtime_id, ""),
            "selection_key": selection_key,
            "selection_present": selection_key in self._liveview_selected,
            "selection": self._liveview_selected.get(selection_key),
            "worker_clocks": {
                key: copy.deepcopy(value)
                for key, value in self._worker_clocks_by_key.items()
                if key[0] == clock_prefix
            },
        }

    def _restore_live_transition(self, snapshot: dict[str, Any]) -> None:
        """Restore a failed lease commit in memory and in the manifest."""
        saved: WorkspaceRecord = snapshot["record"]
        current = self._records.get(saved.workspace_id)
        if current is None:
            current = saved
            self._records[saved.workspace_id] = current
        else:
            fields = copy.deepcopy(saved.__dict__)
            original_tab: TabRecord = snapshot["tab"]
            restored_tab = fields["tabs"].get(original_tab.tab_id)
            if restored_tab is not None:
                original_tab.__dict__.clear()
                original_tab.__dict__.update(copy.deepcopy(restored_tab.__dict__))
                fields["tabs"][original_tab.tab_id] = original_tab
            current.__dict__.clear()
            current.__dict__.update(fields)

        runtime_id = str(snapshot.get("runtime_id") or "")
        if runtime_id:
            if snapshot.get("controller_present"):
                self._liveview_controller_by_runtime[runtime_id] = str(
                    snapshot.get("controller") or ""
                )
            else:
                self._liveview_controller_by_runtime.pop(runtime_id, None)
            if snapshot.get("viewport_owner_present"):
                self._liveview_viewport_owner_by_runtime[runtime_id] = str(
                    snapshot.get("viewport_owner") or ""
                )
            else:
                self._liveview_viewport_owner_by_runtime.pop(runtime_id, None)
        selection_key = str(snapshot.get("selection_key") or "")
        if snapshot.get("selection_present"):
            self._liveview_selected[selection_key] = snapshot.get("selection")
        else:
            self._liveview_selected.pop(selection_key, None)

        for key in [
            key for key in self._worker_clocks_by_key
            if key[0] == saved.workspace_id
        ]:
            self._worker_clocks_by_key.pop(key, None)
        self._worker_clocks_by_key.update(snapshot.get("worker_clocks") or {})
        # WorkspaceStore.save is atomic. Retrying the pre-image also repairs a
        # failure injected after an otherwise successful replacement.
        self.store.save(current)

    def record(self, owner: str, session_id: str) -> WorkspaceRecord:
        workspace_id = self.workspace_id(owner, session_id)
        cached = self._records.get(workspace_id)
        record = cached or self.store.load(workspace_id)
        if record is None:
            record = WorkspaceRecord(workspace_id=workspace_id, owner=owner, session_id=session_id)
            self.store.save(record)
        if record.owner != owner or record.session_id != session_id:
            raise WorkspaceError("Browser workspace ownership mismatch")
        if cached is None:
            # A process restart loses all iframe connections. Honor the durable
            # user-lease deadline immediately on load instead of waiting for a
            # lifecycle sweep before the agent can use its tab again.
            now = time.time()
            expired = False
            for tab in record.tabs.values():
                lease = tab.lease
                if (
                    lease and lease.actor == "user"
                    and (not lease.expires_at or lease.expires_at <= now)
                ):
                    tab.user_note = "User-control lease expired while the app was offline."
                    self._return_tab_lease_to_agent(tab)
                    expired = True
            if expired:
                self.store.save(record)
        self._records[workspace_id] = record
        return record

    async def ensure_awake(self, owner: str, session_id: str) -> WorkspaceRecord:
        record = self.record(owner, session_id)
        if record.status == "active" and self.backend.runtime(owner) is not None:
            return record
        prewarm = self._prewarm_tasks.get(record.workspace_id)
        if prewarm is not None and prewarm is not asyncio.current_task():
            return await asyncio.shield(prewarm)
        return await self._wake_workspace(record)

    # --- Display-backed capture recovery -------------------------------------
    # One bounded, manager-owned decision: when a viewer asks for a tab whose
    # browser was born on a virtual display, a safe relaunch may restore real
    # streaming. Active workers, connected viewers, and user leases on other
    # or meaningful pages defer the relaunch. Only the requested clean blank
    # user tab may be recreated under its existing identity.

    CAPTURE_RELAUNCH_MAX_ATTEMPTS = 2
    CAPTURE_RELAUNCH_COOLDOWN_SECONDS = 90.0

    async def _recover_capture_for_owner(
        self, owner: str, record: WorkspaceRecord, *, requested_tab_id: str = "",
    ) -> None:
        workspace = record.workspace_id
        if self._shutting_down:
            return
        if getattr(self.backend.runtime(owner), "presentation", "") == "embedded":
            return  # desktop-rendered guests: there is no capture to relaunch
        task = self._capture_relaunch_tasks.get(workspace)
        if task is not None and not task.done():
            return
        health = await self.liveview.health()
        if not bool(health.get("available")):
            # Warming the daemon first means a later mint either succeeds with
            # an existing runtime or finds a healthy daemon ready to justify a
            # relaunch. Do not begin a browser relaunch under a broken daemon.
            await self.liveview.start()
            return
        if self._capture_relaunch_blocked(
            record, safe_user_tab_id=requested_tab_id,
        ):
            return
        attempts = int(self._capture_relaunch_failures.get(workspace) or 0)
        last = float(self._capture_relaunch_attempts.get(workspace) or 0)
        if attempts >= self.CAPTURE_RELAUNCH_MAX_ATTEMPTS:
            return
        if last and time.time() - last < self.CAPTURE_RELAUNCH_COOLDOWN_SECONDS:
            return
        self._capture_relaunch_attempts[workspace] = time.time()
        self._capture_relaunch_failures[workspace] = attempts + 1
        self._capture_relaunch_requested_tab()[workspace] = requested_tab_id
        task = asyncio.create_task(self._capture_relaunch(record))
        self._capture_relaunch_tasks[workspace] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _capture_relaunch_requested_tab(self) -> dict[str, str]:
        requested = getattr(self, "_capture_relaunch_tabs", None)
        if requested is None:
            requested = self._capture_relaunch_tabs = {}
        return requested

    async def _capture_relaunch(self, record: WorkspaceRecord) -> None:
        """Restore capture by one full sleep/wake of the owner's browser.

        The relaunch runs after the viewer's transition has released the owner
        barrier, so without taking it again an agent ``browser_open`` admitted
        in between created pages on a runtime being closed underneath it
        ("Owner browser runtime is not active", then a rolled-back batch and
        tabs left uncertain). Draining shared effects first lets in-flight work
        finish; work queued behind it lands on the relaunched runtime.
        """
        workspace = record.workspace_id
        requested_tab_id = self._capture_relaunch_requested_tab().pop(workspace, "")
        try:
            async with self._runtime_effect_barrier(record.owner).exclusive(
                label="capture_relaunch",
            ):
                # Pages may have gained unsaved state while effects drained.
                if self._capture_relaunch_blocked(record, safe_user_tab_id=requested_tab_id):
                    log.info("Live view relaunch for %s skipped: browser now holds work", workspace)
                    self._capture_relaunch_failures.pop(workspace, None)
                    return
                log.warning(
                    "Relaunching browser for %s display-backed to restore live view",
                    workspace,
                )
                await self.backend.close_owner(record.owner)
                await self._wake_workspace(record)
        except Exception:
            log.warning(
                "Live view relaunch failed for workspace %s", workspace, exc_info=True,
            )
        else:
            self._capture_relaunch_failures.pop(workspace, None)
        finally:
            if self._capture_relaunch_tasks.get(workspace) is asyncio.current_task():
                self._capture_relaunch_tasks.pop(workspace, None)

    def _capture_relaunch_blocked(
        self, record: WorkspaceRecord, *, safe_user_tab_id: str = "",
    ) -> bool:
        """Keep active work protected while allowing an editor's empty tab to recover.

        The editor creates a fresh ``about:blank`` tab with a user lease before
        it requests the first live-view capability. If that browser was started
        without display capture, the lease is the only thing preventing the
        manager's bounded relaunch that would make the stream possible. That
        exact blank tab is safe to recreate under its existing identity; any
        other user-owned page remains a hard relaunch barrier.
        """
        # The runtime belongs to the authenticated owner, not this one chat.
        # Relaunching it closes every sibling chat's resident browser page.
        # A clean requested tab cannot authorize losing another agent's draft,
        # worker action, user lease, or connected viewer.
        records = self._owner_records(record.owner)
        if not any(item.workspace_id == record.workspace_id for item in records):
            records.append(record)
        for owner_record in records:
            if any(
                str(worker.get("status") or "") not in {"completed", "cancelled"}
                for worker in owner_record.workers.values()
            ):
                return True
            for tab in owner_record.tabs.values():
                # A restart reloads pages with GET. A draft, a POST result, or
                # a page whose creation was still in flight cannot come back.
                if tab.dirty or tab.uncertain or tab.restorability == "non_restorable":
                    return True
                lease = tab.lease
                if lease is None or lease.actor != "user":
                    continue
                safe_requested_blank = (
                    owner_record.workspace_id == record.workspace_id
                    and tab.tab_id == safe_user_tab_id
                    and lease.actor_id == record.owner
                    and tab.restorability == "restorable_get"
                    and not tab.dirty
                    and not tab.uncertain
                    and self._is_revivable_blank_tab(tab)
                )
                if not safe_requested_blank:
                    return True
            if self.live_views.connected_count(owner_record.workspace_id):
                return True
        return False

    def _capture_unavailable_reason(self, record: WorkspaceRecord) -> tuple[str, str]:
        """Return the calm, typed reason a capture cannot exist right now."""
        task = self._capture_relaunch_tasks.get(record.workspace_id)
        if task is not None and not task.done():
            return "Waking browser display…", "warming"
        attempts = int(self._capture_relaunch_failures.get(record.workspace_id) or 0)
        if attempts >= self.CAPTURE_RELAUNCH_MAX_ATTEMPTS:
            return (
                "Live view could not be restored after repeated attempts. "
                "Browser agents are unaffected.",
                "capture_unavailable",
            )
        if self._capture_relaunch_blocked(record):
            return (
                "The live stream will start when this browser is next awake.",
                "capture_unavailable",
            )
        if bool(getattr(self.liveview, "available", True)):
            return "Waking browser display…", "warming"
        health_reason = str(getattr(self.liveview, "_reason", "") or "")
        return (
            health_reason or "Live view is unavailable on this host.",
            "unavailable_host",
        )



    async def _wake_workspace(self, record: WorkspaceRecord) -> WorkspaceRecord:
        record.status = "waking"
        self.store.save(record)
        await self._ensure_auth_migrated(record)
        try:
            _runtime, _created = await self.backend.ensure_owner(record.owner, self._on_owner_page)
            canonical = self.auth_store.read(record.owner)
            record.auth_base_version = int(canonical.get("version") or 0)
            for tab in list(record.tabs.values()):
                # Starting the owner runtime is enough to wake a workspace.
                # Retained GET pages revive only when that exact tab is used;
                # recreating every page here consumed the resident-page budget
                # and made unrelated background tabs part of the wake outcome.
                parent_owned = tab.lease is None or tab.lease.actor == "parent"
                # Without authentication the owner is "" and the open route's
                # lease falls back to actor_id "user" (``actor_id or actor``).
                user_owned = bool(
                    tab.lease
                    and tab.lease.actor == "user"
                    and tab.lease.actor_id == (record.owner or "user")
                )
                if tab.restorability == "non_restorable" or tab.uncertain:
                    tab.owner_kind = "needs_user"
                elif parent_owned or user_owned:
                    # A process restart has no resident Playwright pages. A
                    # clean GET tab is safe to revive lazily. Preserve a live
                    # authenticated user's lease so recovery cannot transfer
                    # authority, while parent-owned tabs return to their
                    # ordinary ambient ownership.
                    tab.owner_kind = "sleeping"
                    tab.owner_id = ""
                    if parent_owned:
                        tab.lease = None
                else:
                    # A worker-held page cannot be reconstructed from a user
                    # surface without changing its authority.
                    tab.owner_kind = "needs_user"
            record.status = "active"
            record.recovery_warning = ""
            # Waking is runtime activity. Without refreshing this timestamp, a
            # workspace whose checkpoint is older than the soft-sleep window is
            # put back to sleep by the next lifecycle sweep while its first tool
            # call is still using the newly launched browser.
            record.last_runtime_activity_at = time.time()
            self.store.save(record)
            return record
        except BaseException as exc:
            # CancelledError is a BaseException: leaving status=waking made the
            # next browser_open look busy forever after a client disconnect.
            if record.status == "waking":
                cancelled = isinstance(exc, asyncio.CancelledError)
                record.status = "sleeping" if cancelled else "error"
                record.recovery_warning = (
                    "Browser wake was cancelled"
                    if cancelled
                    else f"Browser wake failed: {type(exc).__name__}"
                )
                self.store.save(record)
            raise

    def prewarm(self, owner: str, session_id: str) -> asyncio.Task | None:
        """Begin a workspace wake without weakening browser_open semantics.

        The task performs the same ensure-awake path browser_open would use.
        browser_open discovers and awaits it through ``ensure_awake``; no page
        is created or navigated until the caller supplies its URL.
        """
        if self._shutting_down:
            return None
        record = self.record(owner, session_id)
        if record.status == "active" and self.backend.runtime(owner) is not None:
            return None
        existing = self._prewarm_tasks.get(record.workspace_id)
        if existing is not None and not existing.done():
            return existing

        task = asyncio.create_task(self._wake_workspace(record))
        self._prewarm_tasks[record.workspace_id] = task

        def completed(done: asyncio.Task) -> None:
            if self._prewarm_tasks.get(record.workspace_id) is done:
                self._prewarm_tasks.pop(record.workspace_id, None)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                log.warning(
                    "Browser prewarm failed for workspace %s: %s",
                    record.workspace_id,
                    error,
                )

        task.add_done_callback(completed)
        return task

    async def _ensure_auth_migrated(
        self, record: WorkspaceRecord, *, force: bool = False, owner_lock_held: bool = False,
    ) -> dict:
        from ascended_browser._app.browser_profile_coord import master_profile_dir
        status = self.auth_store.migration_status(record.owner)
        if status["claimed_by_another_owner"]:
            record.recovery_warning = (
                "A legacy browser profile exists but was claimed by another owner; "
                "this workspace starts with empty auth state. Administrative re-import is available."
            )
            self.store.save(record)
            return status
        profile_path = self.backend.profile_dir(record.owner) if isinstance(self.backend, PersistentOwnerBackend) else None
        def installed(path: Path | None) -> bool:
            return bool(path and path.is_dir() and any(
                item.name not in {".odysseus-owner-runtime.lock", ".odysseus-owner-runtime.json"}
                for item in path.iterdir()
            ))
        profile_installed = installed(profile_path)
        if status["migrated"] and profile_installed and not force:
            return status
        profile = Path(master_profile_dir())
        has_state = any((profile / name).exists() for name in ("cookies.sqlite", "webappsstore.sqlite", "storage"))
        if not has_state:
            return {"migrated": False, "empty_legacy_profile": True}
        async with self._migration_lock:
            status = self.auth_store.migration_status(record.owner)
            profile_installed = installed(profile_path)
            if status["migrated"] and profile_installed and not force:
                return status
            from ascended_browser._app.browser_workspace.auth_state import AuthMigrator
            handle = None
            if isinstance(self.backend, PersistentOwnerBackend) and not owner_lock_held:
                handle = self.backend.acquire_owner_install_lock(record.owner)
            try:
                result = await AuthMigrator(self.auth_store).migrate(
                    record.owner,
                    profile=str(profile),
                    install_profile=str(profile_path) if profile_path else None,
                    replace_existing=force,
                )
            finally:
                if handle is not None:
                    self.backend.release_owner_install_lock(handle)
            self.auth_store.mark_migrated(record.owner, result)
            return result

    async def migration_status(self, owner: str, session_id: str) -> dict:
        record = self.record(owner, session_id)
        return {
            **self.auth_store.migration_status(owner),
            "warning": record.recovery_warning,
            "reimport_eligible": self.backend.runtime(owner) is None,
            **self.backend.health(owner),
        }

    async def retry_migration(self, owner: str, session_id: str, *, confirmed: bool = False) -> dict:
        record = self.record(owner, session_id)
        if not confirmed:
            raise WorkspaceError("Administrative re-import requires explicit confirmation")
        if self.backend.runtime(owner) is not None:
            raise WorkspaceError("Close all of this owner's browser workspaces before re-importing legacy authentication")
        handle = None
        if isinstance(self.backend, PersistentOwnerBackend):
            handle = self.backend.acquire_owner_install_lock(owner)
        try:
            return await self._ensure_auth_migrated(record, force=True, owner_lock_held=handle is not None)
        finally:
            if handle is not None:
                self.backend.release_owner_install_lock(handle)

    async def reset_owner_profile(self, owner: str, session_id: str, *, confirmed: bool = False) -> dict:
        self.record(owner, session_id)
        if not confirmed:
            raise WorkspaceError("Owner profile reset requires explicit confirmation")
        if not isinstance(self.backend, PersistentOwnerBackend):
            raise WorkspaceError("The selected browser backend has no persistent owner profile")
        if self.backend.runtime(owner) is not None:
            raise WorkspaceError("Close all of this owner's browser workspaces before resetting the profile")
        return self.backend.reset_profile(owner)

    def _owner_scope(self, workspace_id: str) -> str:
        record = self._records.get(workspace_id)
        return owner_key(record.owner) if record else workspace_id

    def _owner_records(self, owner: str) -> list[WorkspaceRecord]:
        return [record for record in self._records.values() if record.owner == owner]

    @staticmethod
    def _tab_activity_at(tab: TabRecord) -> float:
        """Return the newest durable use signal, including old manifests.

        ``last_active_at`` was added after browser workspaces already existed
        on disk.  Falling back to ``updated_at`` prevents an old record from
        being treated as six-hours idle merely because it has not yet been
        written by the new code.
        """
        active = float(getattr(tab, "last_active_at", 0) or 0)
        return active or float(getattr(tab, "updated_at", 0) or 0)

    def _touch_tab_activity(
        self,
        record: WorkspaceRecord,
        tab: TabRecord,
        *,
        human: bool = False,
        agent: bool = False,
        persist: bool = False,
    ) -> None:
        """Record meaningful tab use without making pointer motion durable I/O."""
        now = time.time()
        tab.last_active_at = now
        # LRU should also prefer a truly inactive tab over one the user just
        # selected, while the separate field remains the sleeping-policy owner.
        tab.updated_at = now
        record.last_runtime_activity_at = now
        if human:
            record.last_human_activity_at = now
        if agent:
            # Which tab the agent last acted on, for the interface's tab-strip
            # agent-focus marker. Human touches deliberately do not set it.
            tab.last_agent_act_at = now
        if not persist:
            return
        key = (record.workspace_id, tab.tab_id)
        previous = self._tab_activity_last_saved.get(key, 0.0)
        if now - previous < 60.0:
            return
        self.store.save(record)
        self._tab_activity_last_saved[key] = now

    def _runtime_effect_barrier(self, owner: str) -> _RuntimeEffectBarrier:
        """Return the canonical owner-runtime barrier, including lean test managers."""
        barriers = getattr(self, "_runtime_effect_barriers", None)
        if barriers is None:
            barriers = self._runtime_effect_barriers = {}
        return barriers.setdefault(owner_key(owner), _RuntimeEffectBarrier(owner_key(owner)))

    def _log_act_timing(
        self, tab_id: str, kind: str, lock_wait: float, effect: float,
    ) -> None:
        """Attribute a slow act to lock contention or to the effect itself.

        The act holds the owner barrier shared while it runs, so the live view's
        exclusive transition waits for it. Without this split a viewer-visible
        stall cannot be traced to either side.
        """
        try:
            threshold = float(get_setting("browser_workspace_lock_slow_seconds", 2) or 2)
        except Exception:
            threshold = 2.0
        if lock_wait < threshold and effect < threshold:
            return
        log.warning(
            "browser act %s on %s: lock wait %.1fs, effect %.1fs",
            kind or "?", tab_id, lock_wait, effect,
        )

    @staticmethod
    def _actor_owns_tab(tab: TabRecord, actor: str, actor_id: str) -> bool:
        lease = getattr(tab, "lease", None)
        expected_id = actor_id or actor
        # LRU suspension intentionally clears the ambient parent lease. The
        # durable tab remains a parent resumable tab, not an unowned capability.
        if actor == "parent" and lease is None:
            if tab.owner_kind in {"parent", "sleeping"}:
                return True
            # A crashed parent draft deliberately drops its lease and becomes
            # needs_user/uncertain. It is still the parent's durable tab: the
            # missing page needs recovery, not a new authority grant.
            if (
                tab.owner_kind == "needs_user"
                and tab.uncertain
                and tab.url.startswith(("http://", "https://"))
            ):
                return True
        # Shared control (cua-style): a person driving this tab's native input
        # never fences the agent whose page it is. The lease records who holds
        # the native grant; the displaced agent keeps acting and reads fresh
        # state before every action (human_input_revision). Only the displaced
        # agent continues — another worker's tab stays foreign.
        if (
            lease
            and lease.actor == "user"
            and actor in {"parent", "worker"}
            and str(tab.takeover_resume_actor or "") == actor
            and str(tab.takeover_resume_actor_id or "") == expected_id
        ):
            return True
        return bool(
            lease
            and lease.actor == actor
            and lease.actor_id == expected_id
        )

    @staticmethod
    def _foreign_tab_message(tab: TabRecord) -> str:
        """Say who holds the tab and how control comes back.

        A refusal without the holder reads as a dead end, so a caller retries
        or abandons work that is merely paused. The lease already records the
        holder, the expiry and the actor control returns to; surface them.
        """
        lease = getattr(tab, "lease", None)
        holder = str(getattr(lease, "actor", "") or tab.owner_kind or "another actor")
        detail = f"currently held by {holder}"
        if holder == "user":
            detail += " (a person took control of this tab in the live view)"
        expires_at = getattr(lease, "expires_at", None)
        if expires_at:
            remaining = int(max(0.0, float(expires_at) - time.time()))
            detail += f"; that hold lapses in about {remaining}s unless renewed"
        resume_actor = str(getattr(tab, "takeover_resume_actor", "") or "")
        if resume_actor:
            detail += f"; control is scheduled to return to {resume_actor}"
        return (
            f"Tab belongs to another browser actor — {detail}. Do not retry this "
            "action now; wait for control to return (call sleep, then observe the "
            "tab again), use a different tab, or ask the user to hand the tab back."
        )

    def assert_tab_access(
        self, owner: str, session_id: str, tab_id: str, *,
        actor: str = "parent", actor_id: str = "parent",
    ) -> TabRecord:
        """Enforce tab leases at the manager boundary, not in model prose."""
        record = self.record(owner, session_id)
        tab = record.tabs.get(tab_id)
        if tab is None:
            raise WorkspaceError("Unknown tab")
        if not self._actor_owns_tab(tab, actor, actor_id):
            raise WorkspaceError(self._foreign_tab_message(tab))
        return tab

    def status_for_actor(
        self, owner: str, session_id: str, *,
        actor: str = "parent", actor_id: str = "parent",
    ) -> dict:
        """Return only tabs the calling actor is allowed to drive."""
        status = self.status(owner, session_id)
        if actor == "user":
            return status
        tabs = [
            tab for tab in status.get("tabs") or []
            if (
                isinstance(tab, dict)
                and (
                    (
                        isinstance(tab.get("lease"), dict)
                        and tab["lease"].get("actor") == actor
                        and tab["lease"].get("actor_id") == (actor_id or actor)
                    )
                    or (
                        # Shared control: the person is driving the native
                        # input on this agent's tab; the agent still sees and
                        # acts on its page.
                        isinstance(tab.get("lease"), dict)
                        and tab["lease"].get("actor") == "user"
                        and str(tab.get("takeover_resume_actor") or "") == actor
                        and str(tab.get("takeover_resume_actor_id") or "") == (actor_id or actor)
                    )
                    or (
                        actor == "parent"
                        and tab.get("lease") is None
                        and (
                            tab.get("owner_kind") in {"parent", "sleeping"}
                            or (
                                tab.get("owner_kind") == "needs_user"
                                and tab.get("uncertain") is True
                                and str(tab.get("url") or "").startswith(("http://", "https://"))
                            )
                        )
                    )
                )
            )
        ]
        status["tabs"] = tabs
        status["tab_count"] = len(tabs)
        status["visible_tab_count"] = len(tabs)
        return status

    def _owner_live_tab_count(self, owner: str) -> int:
        workspace_ids = {item.workspace_id for item in self._owner_records(owner)}
        return sum(1 for workspace_id, _tab_id in self._pages if workspace_id in workspace_ids)

    @asynccontextmanager
    async def reserve_tab_capacity(
        self, owner: str, count: int, *, actor: str = "parent", timeout: float = 120.0,
    ):
        """Atomically wait for enough resident-tab capacity for one open batch."""
        count = max(0, int(count))
        # Parent/user creation is already serialized with LRU eviction by the
        # runtime page-creation lock. It must not queue behind a worker's slow
        # network navigation; worker admission deliberately leaves two live
        # slots available for it.
        if not count or actor != "worker":
            yield
            return
        lock = self._owner_capacity_locks.setdefault(owner, asyncio.Lock())
        deadline = time.monotonic() + max(0.0, float(timeout))
        reserved = False
        while True:
            async with lock:
                # Preserve two slots for the human/parent while workers fan
                # out. Pending batches count too; otherwise simultaneous
                # children all observe the same free capacity before creating.
                effective_limit = max(1, self._owner_tab_limit - 2)
                pending = self._owner_capacity_reserved.get(owner, 0)
                if self._owner_live_tab_count(owner) + pending + count <= effective_limit:
                    self._owner_capacity_reserved[owner] = pending + count
                    reserved = True
                    break
            if time.monotonic() >= deadline:
                raise WorkspaceError(
                    f"Owner browser lacks capacity for {count} tab(s); "
                    f"{self._owner_live_tab_count(owner)}/{self._owner_tab_limit} are resident"
                )
            await asyncio.sleep(0.1)
        try:
            yield
        finally:
            if reserved:
                async with lock:
                    remaining = self._owner_capacity_reserved.get(owner, 0) - count
                    if remaining > 0:
                        self._owner_capacity_reserved[owner] = remaining
                    else:
                        self._owner_capacity_reserved.pop(owner, None)

    def subscribe_browser_events(self, owner: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._browser_event_subs.setdefault(owner, set()).add(queue)
        return queue

    def unsubscribe_browser_events(self, owner: str, queue: asyncio.Queue) -> None:
        subs = self._browser_event_subs.get(owner)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._browser_event_subs.pop(owner, None)

    def _emit_browser_event(self, record: WorkspaceRecord, reason: str, tab_id: str = "", *, site_icon_host: str = "") -> None:
        subs = self._browser_event_subs.get(record.owner)
        if not subs:
            return
        payload = {
            "type": "browser_workspace_changed",
            "session_id": record.session_id,
            "workspace_id": record.workspace_id,
            "tab_id": tab_id,
            "reason": reason,
            **({"site_icon_host": site_icon_host} if site_icon_host else {}),
            "at": time.time(),
        }
        for queue in list(subs):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A slow client misses nothing semantic: events only say
                # "re-fetch", and the next event (or the 4s poll) covers it.
                pass
            except RuntimeError:
                # No running loop in this thread (teardown, tests).
                pass

    def _tab_activity_projection(self, workspace_id: str, tab_id: str) -> dict[str, Any]:
        operations = self._agent_tab_operations.get((workspace_id, tab_id), {})
        actors = sorted({
            str(item.get("actor") or "") for item in operations.values()
            if str(item.get("actor") or "")
        })
        return {
            "agent_active": bool(operations),
            "agent_activity_count": len(operations),
            "agent_activity_actors": actors,
        }

    async def wait_for_runtime_transition_quiet(self, owner: str) -> bool:
        """Let a safe open retry start after the selected-view burst settles."""
        return await self._runtime_effect_barrier(owner).wait_until_quiet()

    def _public_tab(self, record: WorkspaceRecord, tab: TabRecord) -> dict[str, Any]:
        """Project durable tab state with process-local operation presence."""
        projection = {
            **tab.public(),
            **self._tab_activity_projection(record.workspace_id, tab.tab_id),
        }
        # Desktop-embedded runtimes: which <webview> guest renders this tab.
        page_identity = getattr(self.backend, "page_identity", None)
        page = self._pages.get((record.workspace_id, tab.tab_id))
        if page is not None and callable(page_identity):
            identity = page_identity(record.owner, page)
            if isinstance(identity, dict):
                projection.update(identity)
                if getattr(self.backend.runtime(record.owner), "presentation", "") == "embedded":
                    sizing_key = "embedded:" + str(identity.get("target_id") or id(page))
                    projection["size_mode"] = self._size_mode(sizing_key)
        return projection

    @asynccontextmanager
    async def agent_tab_operation(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        actor: str = "parent",
        actor_id: str = "parent",
        tool: str = "",
    ):
        """Expose one real browser operation for truthful per-tab presence.

        Authority and locking remain with the normal dispatch path. This
        context owns only a live projection and always clears it on errors or
        cancellation. Tokens preserve correctness when parallel calls overlap.
        """
        record = self.record(owner, session_id)
        tab_id = str(tab_id or "")
        tab = record.tabs.get(tab_id)
        if tab is None or not self._actor_owns_tab(tab, actor, actor_id):
            yield
            return
        key = (record.workspace_id, tab_id)
        token = uuid.uuid4().hex
        operations = self._agent_tab_operations.setdefault(key, {})
        was_active = bool(operations)
        operations[token] = {
            "actor": str(actor or "parent"),
            "actor_id": str(actor_id or actor or "parent"),
            "tool": str(tool or ""),
        }
        if not was_active:
            self._emit_browser_event(record, "agent_activity_started", tab_id)
        status = _AGENT_CURSOR_TOOL_STATUS.get(str(tool or ""))
        if status:
            # Reads move nothing on the page; without a caption the watcher
            # saw a still stream and could not tell the agent was working.
            await self._agent_cursor_status(
                None, status, workspace_id=record.workspace_id, tab_id=tab_id,
            )
        try:
            yield
        finally:
            current = self._agent_tab_operations.get(key)
            if current is not None:
                current.pop(token, None)
                if not current:
                    self._agent_tab_operations.pop(key, None)
                    self._emit_browser_event(record, "agent_activity_ended", tab_id)
            # Playwright can focus the target's native Firefox window even
            # when the action was for a different tab. The daemon remains
            # bound to the user's selected window; once that window drops
            # behind another one, X capture can keep encoding black frames
            # while both decoders report healthy paint cadence. Re-front the
            # authoritative selected page at the operation boundary — only for
            # a daemon without `bound_window_on_top`; one that has it keeps
            # the bound window up itself (see _restore_liveview_selection).
            # The switch lock and selection recheck prevent an older operation
            # from undoing a newer user tab choice.
            selected = self._liveview_selected.get(owner_key(owner))
            if selected and self.live_views.connected_count_for_tab(*selected):
                await self._restore_liveview_selection(owner)

    async def _new_managed_page(
        self,
        record: WorkspaceRecord,
        tab: TabRecord,
        *,
        runtime: Any | None = None,
        restore_live_selection: bool = True,
    ) -> Any:
        runtime = runtime or self.backend.runtime(record.owner)
        if runtime is None:
            raise WorkspaceError("Owner browser runtime is not active")
        room_deadline = time.monotonic() + self._tab_room_wait_seconds()
        while True:
            room = await self._wait_for_tab_room(record.owner, room_deadline)
            had_room = bool(room)
            try:
                return await self._create_managed_page(
                    record, tab, runtime, restore_live_selection=restore_live_selection,
                    allow_growth=room == "grow",
                )
            except TabRoomExhausted:
                # Retry only when room had appeared and another wake took it.
                if not had_room or time.monotonic() >= room_deadline:
                    raise

    async def _create_managed_page(
        self, record: WorkspaceRecord, tab: TabRecord, runtime: Any, *, restore_live_selection: bool,
        allow_growth: bool = False,
    ) -> Any:
        stage = "page_creation_lock"
        started = time.monotonic()
        try:
            async with asyncio.timeout(_PAGE_CREATION_TIMEOUT_SECONDS):
                async with runtime.page_creation_lock:
                    # Capacity and creation are one atomic owner operation.
                    stage = "capacity"
                    await self._enforce_owner_tab_limit(record.owner, allow_growth=allow_growth)
                    future = asyncio.get_running_loop().create_future()
                    ticket = PageCreationTicket(record.workspace_id, tab.tab_id, future)
                    runtime.pending_page_claim = ticket
                    try:
                        stage = "new_page"
                        page = await self.backend.create_page(record.owner)
                        # The native response identifies this request's page.
                        # An unrelated or late context event must never consume
                        # the current ticket just because it has no opener.
                        self._attach_page(record, tab, page)
                        future.set_result(page)
                    finally:
                        if not future.done():
                            future.set_result(None)
                        if runtime.pending_page_claim is ticket:
                            runtime.pending_page_claim = None
            # Restoration is best effort and has its own bounded wait. Once
            # the page exists, a slow old page must not fail this creation.
            if restore_live_selection:
                await self._restore_liveview_selection(record.owner)
            return page
        except TimeoutError as exc:
            log.warning("Browser page creation timed out: stage=%s elapsed=%.2fs tab=%s",
                        stage, time.monotonic() - started, tab.tab_id)
            raise TabLifecycleError(
                "Browser tab creation timed out; the browser did not respond. Retry opening the tab.",
                error_kind="page_creation_timeout",
                failure_stage=stage,
            ) from exc

    def _on_owner_page(self, runtime_key: str, page: Any) -> asyncio.Task:
        task = asyncio.create_task(self._resolve_owner_page(runtime_key, page))
        self._tasks.add(task); task.add_done_callback(self._tasks.discard)
        return task

    def _runtime_by_key(self, runtime_key: str) -> Any | None:
        for record in self._records.values():
            runtime = self.backend.runtime(record.owner)
            if runtime is not None and runtime.key == runtime_key:
                return runtime
        return None

    async def _resolve_owner_page(self, runtime_key: str, page: Any) -> None:
        if any(value is page for value in self._pages.values()) or id(page) in self._adopting_pages:
            return
        opener = None
        try:
            opener = await page.opener()
        except Exception:
            pass
        if opener is not None:
            for (workspace_id, tab_id), candidate in list(self._pages.items()):
                record = self._records.get(workspace_id)
                if candidate is opener and record and owner_key(record.owner) == runtime_key:
                    await self._adopt_popup_async(workspace_id, tab_id, page)
                    return
        # opener() awaited above, so the popup path may have claimed it meanwhile.
        if any(value is page for value in self._pages.values()) or id(page) in self._adopting_pages:
            return
        runtime = self._runtime_by_key(runtime_key)
        ticket = runtime.pending_page_claim if runtime else None
        if ticket is not None:
            requested_page = await asyncio.shield(ticket.future)
            if requested_page is page:
                return
        if any(value is page for value in self._pages.values()):
            return
        await self._adopt_or_quarantine_owner_page(runtime_key, page, opener_checked=True)

    def attach_diagnostics(self, record: WorkspaceRecord, tab: TabRecord, page: Any) -> Any:
        """Start recording what this page reports; idempotent per page.

        Public so a harness that hands the manager a page directly (bypassing
        ``_attach_page``) can still get the evidence production gets.
        """
        from ascended_browser._app.browser_diagnostics import TabDiagnostics

        key = (record.workspace_id, tab.tab_id)
        current = self._diagnostics.get(key)
        if current is not None and current.page is page:
            return current
        if current is not None:
            current.detach()
        owner = record.owner
        diagnostics = TabDiagnostics(
            page, is_noise=lambda url, document_url="": self._adblock_blocked(owner, url, document_url),
        ).attach()
        self._diagnostics[key] = diagnostics
        return diagnostics

    def _discard_diagnostics(self, key: tuple[str, str], page: Any | None = None) -> None:
        current = self._diagnostics.get(key)
        if current is None or (page is not None and current.page is not page):
            return
        self._diagnostics.pop(key, None)
        current.detach()
        # The page's picture and its media emulation went with the page.
        self._last_captures.pop(key, None)
        self._emulation.pop(key, None)

    def _adblock_blocked(self, owner: str, url: str, document_url: str = "") -> bool:
        """Whether our own request blocker aborted this URL for this page just now (policy, not a page failure)."""
        try:
            runtime = self.backend.runtime(owner)
            adblock = getattr(runtime, "adblock", None)
            return bool(adblock is not None and adblock.was_blocked(url, document_url))
        except Exception:
            return False

    def _mark_diagnostics_action(self, workspace_id: str, tab_id: str) -> None:
        diagnostics = self._diagnostics.get((workspace_id, tab_id))
        if diagnostics is not None:
            diagnostics.mark_action()

    def _attach_diagnostics_summary(self, record: WorkspaceRecord, tab: TabRecord, result: dict[str, Any]) -> dict[str, Any]:
        diagnostics = self._diagnostics.get((record.workspace_id, tab.tab_id))
        if diagnostics is None or "diagnostics" in result:
            return result
        try:
            summary = diagnostics.take_summary()
        except Exception:
            log.debug("Browser diagnostics summary failed", exc_info=True)
            return result
        if not summary:
            return result
        return {**result, "diagnostics": summary}

    def _attach_page(self, record: WorkspaceRecord, tab: TabRecord, page: Any) -> None:
        self._pages[(record.workspace_id, tab.tab_id)] = page
        workspace_id, tab_id = record.workspace_id, tab.tab_id
        self.attach_diagnostics(record, tab, page)
        from ascended_browser.runtime.noop import watch_page_icons

        def icon_changed(host: str) -> None:
            self._emit_browser_event(record, "site_icon_changed", tab_id, site_icon_host=host)

        try:
            icon_loop = asyncio.get_running_loop()
            icon_task = icon_loop.create_task(watch_page_icons(
                record.owner, page,
                is_current=lambda: self._pages.get((workspace_id, tab_id)) is page,
                on_changed=icon_changed,
            ))
            self._tasks.add(icon_task)
            icon_task.add_done_callback(self._tasks.discard)
        except RuntimeError:
            pass
        # Every handler must absorb Playwright's payload argument — see _launch_shared.
        page.on(
            "crash",
            lambda _page=None: self._on_page_crashed(workspace_id, tab_id, page),
        )
        page.on("close", lambda _page=None: self._on_page_closed(workspace_id, tab_id, page))
        page.on(
            "framenavigated",
            lambda frame: self._on_main_frame_navigated(
                workspace_id, tab_id, page, frame,
            ),
        )
        page.on(
            "response",
            lambda response: self._note_navigation_method(
                workspace_id, tab_id, response, page=page,
            ),
        )
        page.on("popup", lambda popup: self._schedule_popup_adoption(workspace_id, tab_id, popup))
        try:
            task = asyncio.create_task(self._preinstall_live_wheel_executor(page))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            task = asyncio.create_task(
                self._install_live_form_dirty_observer(record, tab, page)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except RuntimeError:
            # A synthetic/unit page can be attached outside a running loop.
            # The first wheel installs and applies atomically in that case.
            pass

    def _on_main_frame_navigated(
        self, workspace_id: str, tab_id: str, page: Any, frame: Any,
    ) -> None:
        """Keep the durable tab projection aligned with its resident page.

        User-followed links and redirects navigate the existing Playwright page
        without going through a browser tool or the live-view omnibox. The
        browser-tab strip projects the durable URL, so it must be updated at
        this page lifecycle boundary. Subframes never define tab identity.
        """
        key = (workspace_id, tab_id)
        if self._pages.get(key) is not page:
            return
        try:
            if frame is not page.main_frame:
                return
            current_url = str(getattr(page, "url", "") or "")
        except Exception:
            return
        record = self._records.get(workspace_id)
        tab = record.tabs.get(tab_id) if record else None
        if tab is not None and current_url:
            self._record_native_history(record, current_url, visit=True)
        if tab is None or not current_url or current_url == tab.url:
            return
        tab.url = current_url
        tab.last_observation_id = ""
        self._invalidate_observation(workspace_id, tab_id)
        self.store.save(record)
        self._emit_browser_event(record, "page_navigated", tab_id)
        if callable(getattr(page, "wait_for_load_state", None)) and callable(
            getattr(page, "title", None),
        ):
            try:
                task = asyncio.get_running_loop().create_task(
                    self._refresh_tab_title_after_navigation(
                        workspace_id, tab_id, page, current_url,
                    ),
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            except RuntimeError:
                # Synthetic pages may be attached outside the manager loop.
                pass

    async def _refresh_tab_title_after_navigation(
        self, workspace_id: str, tab_id: str, page: Any, navigated_url: str,
    ) -> None:
        """Refresh the paired title after the new main document settles."""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            title = str(await asyncio.wait_for(page.title(), timeout=3.0) or "")
            current_url = str(getattr(page, "url", "") or "")
        except Exception:
            return
        if not title or current_url != navigated_url:
            return
        key = (workspace_id, tab_id)
        if self._pages.get(key) is not page:
            return
        record = self._records.get(workspace_id)
        tab = record.tabs.get(tab_id) if record else None
        if tab is None or tab.url != navigated_url or tab.title == title:
            return
        tab.title = title
        self._record_native_history(record, navigated_url, title)
        self.store.save(record)
        self._emit_browser_event(record, "page_navigated", tab_id)

    async def _preinstall_live_wheel_executor(self, page: Any) -> None:
        """Install the fixed pixel executor for this document and navigations."""
        try:
            add_init_script = getattr(page, "add_init_script", None)
            if callable(add_init_script):
                await add_init_script(script=f"({_LIVE_WHEEL_INSTALL_JS})()")
            await page.evaluate(_LIVE_WHEEL_INSTALL_JS)
            self._liveview_wheel_installed_pages.add(id(page))
        except Exception:
            # Navigation races are expected. The effect-time call below checks
            # presence and atomically reinstalls before applying the batch.
            log.debug("Could not preinstall the live wheel executor", exc_info=True)

    def _mark_live_form_dirty(self, record: WorkspaceRecord, tab: TabRecord) -> bool:
        """Persist the first real form edit before a later owner-wide recovery."""
        key = (record.workspace_id, tab.tab_id)
        if self._records.get(record.workspace_id) is not record or record.tabs.get(tab.tab_id) is not tab:
            return False
        if (
            tab.dirty and tab.restorability == "non_restorable"
            and key not in self._live_form_dirty_pending_save
        ):
            return True
        tab.dirty = True
        tab.restorability = "non_restorable"
        self._live_form_dirty_pending_save.add(key)
        try:
            self.store.save(record)
        except Exception:
            log.warning("Could not persist browser form edit for tab %s", tab.tab_id, exc_info=True)
            return False
        self._live_form_dirty_pending_save.discard(key)
        self._emit_browser_event(record, "form_changed", tab.tab_id)
        return True

    async def _install_live_form_dirty_observer(
        self, record: WorkspaceRecord, tab: TabRecord, page: Any,
    ) -> None:
        """Observe trusted form edits in either Live View input transport."""
        if id(page) in self._live_form_dirty_installed_pages:
            return
        expose_binding = getattr(page, "expose_binding", None)
        add_init_script = getattr(page, "add_init_script", None)
        evaluate = getattr(page, "evaluate", None)
        if not all(callable(value) for value in (expose_binding, add_init_script, evaluate)):
            return

        async def edited(source: dict[str, Any]) -> bool:
            # Binding callbacks can be delivered after handback, tab close,
            # owner retirement, or a replacement page with the same tab id.
            if source.get("page") is not page:
                return False
            if self._pages.get((record.workspace_id, tab.tab_id)) is not page:
                return False
            return self._mark_live_form_dirty(record, tab)

        try:
            await expose_binding("__odysseusLiveFormDirtyV1", edited)
            await add_init_script(script=f"({_LIVE_FORM_DIRTY_INSTALL_JS})()")
            await evaluate(_LIVE_FORM_DIRTY_INSTALL_JS)
            self._live_form_dirty_installed_pages.add(id(page))
        except Exception:
            # A navigation may retire the current execution context between
            # exposure and evaluate. The installed init script still protects
            # future documents; accepted text is the conservative fallback.
            log.debug("Could not install the live form edit observer", exc_info=True)

    def _on_page_closed(self, workspace_id: str, tab_id: str, page: Any | None = None) -> None:
        """A page closed by the site or the user must not stay routable."""
        key = (workspace_id, tab_id)
        if page is not None:
            self._liveview_wheel_installed_pages.discard(id(page))
            self._live_form_dirty_installed_pages.discard(id(page))
        self._discard_diagnostics(key, page)
        current_page = self._pages.get(key)
        if page is not None and current_page is not None and current_page is not page:
            # Page close events are dispatched asynchronously. A sleep/wake or
            # document recovery can attach a replacement beneath the same
            # stable tab id before the old callback arrives; that stale event
            # must never evict or downgrade the replacement.
            return
        if key in self._replacing_page_closes:
            self._replacing_page_closes.discard(key)
            if page is None or self._pages.get(key) is page:
                self._pages.pop(key, None)
            return
        record = self._records.get(workspace_id)
        tab = (record.tabs.get(tab_id) if record else None)
        intentional = key in self._intentional_page_closes
        if intentional:
            self._intentional_page_closes.discard(key)
        controller_view: LiveViewSession | None = None
        if not intentional and record is not None and tab is not None:
            runtime = self.backend.runtime(record.owner)
            runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
            controller_id = self._liveview_controller_by_runtime.get(runtime_id, "")
            candidate = self.live_views.find(controller_id)
            if (
                candidate is not None
                and candidate.workspace_id == workspace_id
                and (
                    candidate.tab_id == tab_id
                )
                and tab.lease is not None
                and tab.lease.actor == "user"
                and tab.lease.actor_id == record.owner
            ):
                controller_view = candidate

        # Remove the dead Playwright object and invalidate every capability
        # immediately. This is the manager-side fail-closed boundary while the
        # asynchronous daemon revocation is being committed.
        self._pages.pop(key, None)
        self.live_views.invalidate_tab(workspace_id, tab_id)
        for runtime_key, selected in list(self._liveview_selected.items()):
            if selected == (workspace_id, tab_id):
                self._liveview_selected.pop(runtime_key, None)
        if tab is not None:
            if intentional:
                return
            if controller_view is not None:
                self._schedule_controlled_page_loss(
                    record, tab, controller_view,
                    reason="Controlled browser page closed unexpectedly before handback.",
                )
                return
            # A clean GET page can be reconstructed on demand beneath the same
            # stable tab id. Treating every unexpected close as non-restorable
            # made one page-process crash permanently kill both extract and the
            # selected live view even though its URL was safe to revisit.
            clean_blank = self._is_revivable_blank_tab(tab)
            restorable = clean_blank or (
                tab.restorability == "restorable_get"
                and not tab.dirty and not tab.uncertain
                and tab.url.startswith(("http://", "https://"))
            )
            if restorable:
                # A blank page has no remote effect or in-page draft to
                # reconcile.  It is safe to recreate under its stable tab id,
                # including for older records that were marked uncertain by
                # the previous all-non-HTTP fail-closed rule.
                if clean_blank:
                    tab.restorability = "restorable_get"
                    tab.uncertain = False
                tab.owner_kind = "sleeping"
                log.warning("Browser page closed unexpectedly; tab %s will revive on demand", tab_id)
            else:
                tab.restorability = "non_restorable"
                tab.uncertain = True
                tab.owner_kind = "needs_user" if tab.disposition == "handoff" else tab.owner_kind
                log.warning("Non-restorable browser page closed unexpectedly: tab %s", tab_id)
            if record is not None:
                self.store.save(record)
                self._emit_browser_event(record, "page_closed", tab_id)

    @staticmethod
    def _is_revivable_blank_tab(tab: TabRecord) -> bool:
        """Whether a lost page can safely be recreated as a blank document.

        This is intentionally stricter than "not an HTTP URL": a dirty page,
        a non-GET result, or a held action may carry user work or an unresolved
        effect and must still require recovery.  ``about:blank`` with no such
        state is the one document whose replacement cannot replay an external
        action or discard meaningful page contents.
        """
        return (
            str(tab.url or "").strip() == "about:blank"
            and not tab.dirty
            and str(tab.last_method or "GET").upper() == "GET"
            and tab.hold is None
        )

    def _on_page_crashed(
        self, workspace_id: str, tab_id: str, page: Any | None = None,
    ) -> None:
        self._on_page_closed(workspace_id, tab_id, page)
        self._schedule_recovery([workspace_id])

    def _schedule_controlled_page_loss(
        self,
        record: WorkspaceRecord,
        tab: TabRecord,
        view: LiveViewSession,
        *,
        reason: str,
    ) -> None:
        """Revoke a dead controlled page, then durably return its lease."""
        task_key = (record.workspace_id, tab.tab_id)
        pending = self._liveview_page_loss_tasks.get(task_key)
        if pending is not None and not pending.done():
            return
        lease_epoch = int(getattr(tab.lease, "generation", 0) or 0)

        async def cleanup() -> None:
            try:
                async with self._liveview_transition(
                    record.owner, record.workspace_id, tab.tab_id,
                ):
                    current_tab = record.tabs.get(tab.tab_id)
                    if current_tab is None:
                        self._liveview_controller_by_runtime.pop(view.runtime_id, None)
                        self._liveview_viewport_owner_by_runtime.pop(
                            view.runtime_id, None,
                        )
                        return
                    controller = self._liveview_controller_by_runtime.get(view.runtime_id)
                    if not controller or not secrets.compare_digest(controller, view.view_id):
                        return
                    try:
                        await self.fence_live_control(
                            view, current_tab, enabled=False, lease_epoch=lease_epoch,
                        )
                    except Exception:
                        # The closed capability makes the Python proxy reject
                        # all further input even when liveviewd is unreachable.
                        log.warning(
                            "Could not revoke control after browser page loss",
                            exc_info=True,
                        )
                    current_lease = current_tab.lease
                    if (
                        current_lease is not None
                        and current_lease.actor == "user"
                        and current_lease.actor_id == record.owner
                        and int(current_lease.generation or 0) == lease_epoch
                    ):
                        self.release_control(
                            record.owner,
                            record.session_id,
                            current_tab.tab_id,
                            note=reason,
                            preserve_hold=True,
                            _fence_daemon=False,
                            _page_lost=True,
                        )
                    else:
                        self._liveview_controller_by_runtime.pop(view.runtime_id, None)
                        self._liveview_viewport_owner_by_runtime.pop(
                            view.runtime_id, None,
                        )
                    self._emit_browser_event(record, "page_closed", current_tab.tab_id)
            finally:
                self._liveview_page_loss_tasks.pop(task_key, None)

        try:
            task = asyncio.get_running_loop().create_task(cleanup())
        except RuntimeError:
            log.error("Controlled browser page was lost outside an event loop")
            return
        self._liveview_page_loss_tasks[task_key] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _note_navigation_method(
        self, workspace_id: str, tab_id: str, response: Any, *, page: Any | None = None,
    ) -> None:
        """Record the real HTTP method of the committed top-level navigation.

        Restorability is decided by re-navigating with a GET. A POST result page
        (checkout step, search POST, "add to cart") cannot be reproduced that way,
        so the method has to come from the wire rather than being assumed GET.
        """
        record = self._records.get(workspace_id)
        tab = (record.tabs.get(tab_id) if record else None)
        if tab is None or (page is not None and self._pages.get((workspace_id, tab_id)) is not page):
            return
        try:
            request = response.request
            if not request.is_navigation_request():
                return
            if getattr(response.frame, "parent_frame", None) is not None:
                return  # sub-frame navigation does not define the tab's own method
            method = str(request.method or "GET").upper()
        except Exception:
            # An unreadable response must fail closed, never silently claim GET.
            tab.last_method = "UNKNOWN"
            return
        tab.last_method = method
        # A committed top-level navigation discards the previous document, so
        # the unsaved form state that set `dirty` no longer exists. Leaving it
        # set made one filled field mark the tab non-restorable for the rest of
        # its life, so wake produced needs_user for tabs that were plain GETs.
        tab.dirty = False
        tab.restorability = (
            "restorable_get" if tab.last_method == "GET" and not tab.uncertain
            else "non_restorable"
        )
        self._live_form_dirty_pending_save.discard((workspace_id, tab_id))
        self.store.save(record)

    def _on_unattributed_page(self, workspace_id: str, page: Any) -> None:
        """Compatibility entrypoint for tests/older callers; attribution is deterministic."""
        record = self._records.get(workspace_id)
        if record:
            self._on_owner_page(owner_key(record.owner), page)

    def _schedule_popup_adoption(self, workspace_id: str, opener_tab_id: str, page: Any) -> None:
        task = asyncio.create_task(self._adopt_popup_async(workspace_id, opener_tab_id, page))
        self._tasks.add(task); task.add_done_callback(self._tasks.discard)

    async def _adopt_popup_async(self, workspace_id: str, opener_tab_id: str, page: Any) -> None:
        if any(value is page for value in self._pages.values()) or id(page) in self._adopting_pages:
            return
        self._adopting_pages.add(id(page))
        try:
            record = self._records.get(workspace_id)
            if record is None or opener_tab_id not in record.tabs:
                return
            # Popup attribution changes the durable ownership graph. Serialize
            # it with takeover, handback and tab selection so a child cannot be
            # published from a stale snapshot of its opener's user lease.
            async with self._liveview_transition(
                record.owner, workspace_id, opener_tab_id,
            ):
                if any(value is page for value in self._pages.values()):
                    return
                await self._adopt_popup_locked(workspace_id, opener_tab_id, page)
        finally:
            self._adopting_pages.discard(id(page))
        if record is not None:
            await self._restore_liveview_selection(record.owner)

    async def _adopt_popup_locked(self, workspace_id: str, opener_tab_id: str, page: Any) -> None:
        record = self._records.get(workspace_id)
        opener_tab = record.tabs.get(opener_tab_id) if record else None
        if not record or not opener_tab:
            return
        # A popup can close while its opener/context events are queued. A
        # closed Playwright page cannot be selected, and its close event has
        # already fired before we can attach our handler.
        if callable(getattr(page, "is_closed", None)) and page.is_closed():
            return
        try:
            await self._enforce_owner_tab_limit(record.owner)
        except WorkspaceError as exc:
            try: await page.close()
            except Exception: pass
            self.raise_attention(record, audience="parent", reason=f"Popup closed: {exc}")
            return
        opener_snapshot = copy.deepcopy(opener_tab)
        opener_lease = opener_tab.lease
        if opener_lease is not None and opener_lease.actor == "user":
            # Older adoption lost the deadline and return address, producing an
            # immortal user-owned popup that no viewer controlled. Every human
            # child joins one bounded, durable handback group instead.
            if opener_lease.expires_at is None or opener_lease.expires_at <= time.time():
                opener_lease.expires_at = self._user_control_deadline()
            if not opener_tab.takeover_group_id:
                opener_tab.takeover_group_id = "control_" + uuid.uuid4().hex
        child_lease = (
            TabLease(
                actor=opener_lease.actor,
                actor_id=opener_lease.actor_id,
                mode=opener_lease.mode,
                generation=opener_lease.generation,
                acquired_at=opener_lease.acquired_at,
                expires_at=opener_lease.expires_at,
            )
            if opener_lease is not None else None
        )
        tab = TabRecord(
            url=str(getattr(page, "url", "about:blank") or "about:blank"),
            disposition=opener_tab.disposition,
            owner_kind=opener_tab.owner_kind,
            owner_id=opener_tab.owner_id,
            lease=child_lease,
            takeover_resume_actor=opener_tab.takeover_resume_actor,
            takeover_resume_actor_id=opener_tab.takeover_resume_actor_id,
            takeover_group_id=opener_tab.takeover_group_id,
        )
        record.tabs[tab.tab_id] = tab
        self._attach_page(record, tab, page)
        if callable(getattr(page, "is_closed", None)) and page.is_closed():
            self._on_page_closed(workspace_id, tab.tab_id, page)
        try:
            self.store.save(record)
        except BaseException:
            opener_tab.__dict__.clear()
            opener_tab.__dict__.update(copy.deepcopy(opener_snapshot.__dict__))
            record.tabs.pop(tab.tab_id, None)
            self._pages.pop((workspace_id, tab.tab_id), None)
            try:
                await page.close()
            except Exception:
                pass
            raise
        self.store.append_journal(workspace_id, {
            "at": time.time(), "event": "popup_adopted", "tab_id": tab.tab_id,
            "opener_tab_id": opener_tab_id,
        })
        self._emit_browser_event(record, "popup_adopted", tab.tab_id)

    async def _adopt_or_quarantine_owner_page(
        self, runtime_key: str, page: Any, *, opener_checked: bool = False,
    ) -> None:
        if any(value is page for value in self._pages.values()):
            return
        # No creation ticket and no opener: this is genuinely unattributed. Pick
        # the most recently active workspace for this owner and quarantine there.
        records = [record for record in self._records.values() if owner_key(record.owner) == runtime_key]
        if not records:
            try: await page.close()
            except Exception: pass
            return
        runtime = self._runtime_by_key(runtime_key)
        if runtime is not None and page is getattr(runtime, "anchor_page", None):
            # The anchor holds the Firefox window open; it is never a workspace tab.
            return
        if str(getattr(page, "url", "") or "") == "about:blank" and not any(
            candidate.status == "active" for candidate in records
        ):
            # A blank page with no ticket and no opener, before any workspace is
            # active, is Firefox's own startup noise rather than a real popup.
            return
        record = max(records, key=lambda item: item.last_runtime_activity_at)
        self._quarantine_if_unowned(record.workspace_id, page)
        await self._restore_liveview_selection(record.owner)

    @staticmethod
    def _is_wedged_error(exc: BaseException) -> bool:
        """Firefox stopped serving navigation for the whole context, not one page.

        Measured on camoufox 0.4.11: a persistent context stops navigating after
        a nondeterministic number of open pages (as low as 6). Once it happens,
        retrying the same page, a fresh page, and an older working page all time
        out — only relaunching the browser recovers. So a navigation timeout is
        an owner-level fault here, not a slow site.
        """
        text = f"{type(exc).__name__}: {exc}"
        # Only signatures that are definitively context-wide. A bare navigation
        # timeout must fail its own call: classifying that as a wedge restarted
        # the owner's browser for one slow page and marked every sibling tab
        # needs_user.
        return "window is null" in text or "Browser.newPage" in text

    async def _relaunch_wedged_owner(self, owner: str, reason: str) -> None:
        """Tear down a non-navigating browser and bring the owner back up once."""
        prior_runtime = self.backend.runtime(owner)
        prior_runtime_id = str(
            getattr(prior_runtime, "liveview_runtime_id", "") or ""
        )
        if prior_runtime_id:
            self._liveview_controller_by_runtime.pop(prior_runtime_id, None)
            self._liveview_viewport_owner_by_runtime.pop(prior_runtime_id, None)
            self.live_views.invalidate_runtime(prior_runtime_id)
        self.backend.mark_wedged(owner)
        for record in self._owner_records(owner):
            for key in [key for key in self._pages if key[0] == record.workspace_id]:
                self._pages.pop(key, None)
            for tab in record.tabs.values():
                lease = getattr(tab, "lease", None)
                parent_owned = lease is None or lease.actor == "parent"
                if (
                    tab.restorability == "restorable_get"
                    and not tab.dirty and not tab.uncertain
                    and parent_owned
                ):
                    tab.owner_kind = "sleeping"
                    tab.owner_id = ""
                    tab.lease = None
                else:
                    tab.uncertain = True
                    tab.owner_kind = "needs_user"
            record.recovery_warning = f"Browser stopped responding ({reason}); restarting."
            record.status = "waking"
            self.store.save(record)
        await self.backend.relaunch_owner(owner, self._on_owner_page)
        for record in self._owner_records(owner):
            record.status = "active"
            record.recovery_warning = f"Browser stopped responding ({reason}); it was restarted."
            self.store.save(record)

    async def _recover_unresponsive_page_creation(
        self, record: WorkspaceRecord, failed_runtime: Any,
    ) -> bool:
        """Retry only a proved owner-wide stall with safe, drained state.

        A slow newPage alone cannot authorize closing sibling pages. Reuse the
        existing owner-wide relaunch guard and require a separate native
        context command to time out after shared effects have drained.
        """
        if self._capture_relaunch_blocked(record):
            return False
        try:
            async with asyncio.timeout(12):
                async with self._runtime_effect_barrier(record.owner).exclusive(
                    label="page_creation_recovery",
                ):
                    if self.backend.runtime(record.owner) is not failed_runtime:
                        return True  # Another recovery already replaced it.
                    if self._capture_relaunch_blocked(record):
                        return False
                    context = getattr(failed_runtime, "context", None)
                    if context is None or not hasattr(context, "cookies"):
                        return False
                    try:
                        await _bounded(context.cookies(), 1.0)
                    except TimeoutError:
                        log.warning("Recovering unresponsive browser after tab creation: workspace=%s",
                                    record.workspace_id)
                        await self._relaunch_wedged_owner(record.owner, "native commands timed out")
                        return True
                    except Exception:
                        return False
                    return False
        except TimeoutError:
            log.warning("Browser tab creation recovery exceeded its bounded budget: workspace=%s",
                        record.workspace_id)
            return False

    def _owner_tab_room(self, owner: str) -> tuple[list[tuple[Any, Any, Any]], list[tuple[Any, Any, Any]]]:
        """The owner's resident tabs and those that may be slept to make room."""
        live = [
            (record, tab, self._pages.get((record.workspace_id, tab.tab_id)))
            for record in self._owner_records(owner)
            for tab in record.tabs.values()
            if self._pages.get((record.workspace_id, tab.tab_id)) is not None
        ]
        candidates = [
            item for item in live
            if item[1].restorability == "restorable_get"
            and not item[1].dirty and not item[1].uncertain
            and item[1].disposition != "deliverable"
            and self._operation_pins.get((item[0].workspace_id, item[1].tab_id), 0) == 0
            # A parent lease is ambient ownership: it says which actor may
            # resume the tab, not that an action or takeover is in flight.
            # Worker and user leases remain hard eviction barriers.
            and (item[1].lease is None or item[1].lease.actor == "parent")
        ]
        return live, candidates

    def _owner_may_grow(
        self, owner: str, live: list[tuple[Any, Any, Any]], *, waited: float = 0.0,
    ) -> bool:
        """Whether a full owner may exceed its soft tab limit.

        When nothing resident will free up by waiting (every tab that could
        sleep holds work), or once a wake has waited briefly for busy tabs:
        twelve sessions with drafts in progress used to block every other
        session's wake forever, and same-site navigations queued behind the
        per-site lock hold their tabs for as long as that queue.
        """
        if len(live) >= max(self._owner_tab_limit, getattr(self, "_owner_tab_hard_limit", 0)):
            return False
        if waited >= _TAB_ROOM_GROW_AFTER_SECONDS:
            return True
        for record, tab, _page in live:
            sleepable_when_idle = (
                tab.restorability == "restorable_get" and not tab.dirty and not tab.uncertain
                and tab.disposition != "deliverable"
                and (tab.lease is None or tab.lease.actor == "parent")
            )
            if sleepable_when_idle and self._operation_pins.get((record.workspace_id, tab.tab_id), 0):
                return False  # it will come free when its action ends
        return True

    async def _wait_for_tab_room(self, owner: str, deadline: float) -> str:
        """Return "free" (room or a sleepable tab), "grow" (may exceed the soft
        limit), or "" (no room before the deadline, or none will come)."""
        started = time.monotonic()
        """Wait, until ``deadline``, for a free tab slot or a tab that may sleep.

        With many sessions acting at once every resident tab can be mid-action
        for a moment. A wake or open that needs room used to fail at once
        ("tab wake failed"); it now waits for one of those actions to finish.
        """
        while True:
            live, candidates = self._owner_tab_room(owner)
            waited = time.monotonic() - started
            if len(live) < self._owner_tab_limit or candidates:
                return "free"
            if self._owner_may_grow(owner, live, waited=waited):
                return "grow"
            if not any(self._operation_pins.get((record.workspace_id, tab.tab_id), 0) for record, tab, _ in live):
                return ""  # only drafts and leases hold the room at the hard limit
            if time.monotonic() >= deadline:
                return ""
            await asyncio.sleep(0.1)

    @staticmethod
    def _tab_room_wait_seconds() -> float:
        """How long a wake/open may wait for room: the configured cap, but
        never so long that the operation's own budget has no time left.

        Same-site navigations queue behind the per-site lock and keep their
        tabs pinned while they wait, so with many sessions on one site room
        can take as long as that queue to appear.
        """
        try:
            cap = max(0.0, float(get_setting("browser_workspace_tab_room_wait_seconds", 45) or 0))
        except (TypeError, ValueError):
            cap = 45.0
        from ascended_browser._app.browser_deadline import remaining_seconds

        budget = remaining_seconds()
        return cap if budget is None else min(cap, max(0.0, budget - 15.0))

    async def _enforce_owner_tab_limit(self, owner: str, *, allow_growth: bool = False) -> None:
        live, _ = self._owner_tab_room(owner)
        while len(live) >= self._owner_tab_limit:
            _, candidates = self._owner_tab_room(owner)
            candidates = [item for item in candidates if any(item[2] is page for _, _, page in live)]
            if not candidates:
                if self._owner_may_grow(owner, live, waited=_TAB_ROOM_GROW_AFTER_SECONDS if allow_growth else 0.0):
                    log.info("Owner browser grows past its %d-tab limit: every resident tab holds work",
                             self._owner_tab_limit)
                    return
                raise TabRoomExhausted(
                    f"Owner browser reached its {self._owner_tab_limit}-tab limit and every tab is in use; "
                    "close or sleep a tab, or retry when an action finishes"
                )
            record, tab, page = min(candidates, key=lambda item: item[1].updated_at)
            key = (record.workspace_id, tab.tab_id)
            await self._capture_sleep_scroll(tab, page)
            # The scroll read yielded: this tab's own session may have started
            # an action on the page meanwhile. Leave it and choose again.
            if self._operation_pins.get(key, 0) or self._pages.get(key) is not page:
                live, _ = self._owner_tab_room(owner)
                continue
            # Sleep it before the close yields, so an action arriving now wakes
            # a fresh page instead of acting on a closing one or finding none.
            tab.owner_kind = "sleeping"
            tab.owner_id = ""
            tab.lease = None
            self._pages.pop(key, None)
            self._intentional_page_closes.add(key)
            try:
                await _bounded(page.close(), _PAGE_CLOSE_TIMEOUT_SECONDS)
            except (TimeoutError, asyncio.TimeoutError):
                # Same rule as closing a tab: a page Firefox never answers for
                # must not stall every wake behind it. It is already detached.
                log.warning("Sleeping tab %s did not confirm close within %.0fs; forgetting it",
                            tab.tab_id, _PAGE_CLOSE_TIMEOUT_SECONDS)
                self._discard_diagnostics(key, page)
            finally:
                self._intentional_page_closes.discard(key)
            self.store.append_journal(record.workspace_id, {
                "at": time.time(), "event": "tab_lru_evicted", "tab_id": tab.tab_id,
            })
            self.store.save(record)
            live = [item for item in live if item[2] is not page]

    @staticmethod
    def _background_tab_sleep_seconds() -> int:
        """Return the single policy deadline for automatic clean-tab sleep."""
        try:
            configured = int(
                get_setting("browser_workspace_tab_sleep_seconds", 21600) or 21600
            )
        except (TypeError, ValueError):
            configured = 21600
        # Do not permit an accidental empty/zero setting to bring back the
        # immediate-close behavior that this policy replaces.  Memory pressure
        # is handled separately by _relieve_memory_pressure and the owner tab
        # limit, both observable in the journal.
        return max(300, configured)

    @staticmethod
    def _dirty_tab_sleep_seconds() -> int:
        """Idle lifetime of a tab holding unsaved form input (default 12 h)."""
        try:
            configured = int(
                get_setting("browser_workspace_dirty_tab_sleep_seconds", 43200) or 43200
            )
        except (TypeError, ValueError):
            configured = 43200
        return max(3600, configured)

    def _tab_sleep_blocked(
        self,
        record: WorkspaceRecord,
        tab: TabRecord,
        *,
        protected: tuple[str, str] | None = None,
    ) -> bool:
        """Barriers every automatic sleep respects: live work and watched pages."""
        key = (record.workspace_id, tab.tab_id)
        if protected == key or self._pages.get(key) is None:
            return True
        if key in self._intentional_page_closes:
            # Another sleep or close of this page is already in flight.
            return True
        # An uncertain effect (a submit whose outcome is unknown) needs a
        # person or agent to reconcile it; it never expires on a timer.
        if tab.uncertain:
            return True
        if tab.disposition == "deliverable" or tab.hold is not None:
            return True
        if self._operation_pins.get(key, 0) != 0:
            return True
        # Parent is ambient resume authority.  Worker and user ownership signal
        # live work that must never be evicted behind their backs.
        if tab.lease is not None and tab.lease.actor != "parent":
            return True
        if self.live_views.has_usable_view(record.workspace_id, tab.tab_id):
            # Includes connection/reconnect grace: closing it would invalidate
            # an iframe which is still allowed to recover.
            return True
        return False

    def _tab_can_sleep_in_background(
        self,
        record: WorkspaceRecord,
        tab: TabRecord,
        *,
        now: float,
        protected: tuple[str, str] | None = None,
        min_idle: float | None = None,
    ) -> bool:
        """Whether closing this clean resident page is safe and overdue.

        ``min_idle`` replaces the six-hour deadline; memory pressure uses a
        short one so only pages nobody touched recently are released.
        """
        if self._tab_sleep_blocked(record, tab, protected=protected):
            return False
        if tab.restorability != "restorable_get" or tab.dirty:
            return False
        idle = self._background_tab_sleep_seconds() if min_idle is None else min_idle
        return now - self._tab_activity_at(tab) >= idle

    def _tab_draft_expired(
        self,
        record: WorkspaceRecord,
        tab: TabRecord,
        *,
        now: float,
        protected: tuple[str, str] | None = None,
    ) -> bool:
        """Whether a tab with unsaved form input went unused past its lifetime.

        Any use by the person or the agent refreshes the activity clock, so a
        draft only expires after a full lifetime with nobody touching it.
        """
        if not tab.dirty or self._tab_sleep_blocked(record, tab, protected=protected):
            return False
        return now - self._tab_activity_at(tab) >= self._dirty_tab_sleep_seconds()

    @staticmethod
    async def _capture_sleep_scroll(tab: TabRecord, page: Any) -> None:
        """Remember where the page was scrolled so its wake can return there."""
        try:
            position = await asyncio.wait_for(
                page.evaluate("() => [Math.round(scrollX), Math.round(scrollY)]"),
                timeout=2,
            )
            tab.sleep_scroll_x, tab.sleep_scroll_y = int(position[0]), int(position[1])
        except Exception:
            # A hung or crashed page must not block its own release.
            tab.sleep_scroll_x = tab.sleep_scroll_y = 0

    @staticmethod
    async def _restore_sleep_scroll(tab: TabRecord, page: Any) -> None:
        x, y = tab.sleep_scroll_x, tab.sleep_scroll_y
        tab.sleep_scroll_x = tab.sleep_scroll_y = 0
        if not (x or y):
            return
        try:
            await asyncio.wait_for(
                page.evaluate("([x, y]) => window.scrollTo(x, y)", [x, y]), timeout=2,
            )
        except Exception:
            pass

    async def _sleep_resident_tab(
        self,
        record: WorkspaceRecord,
        tab: TabRecord,
        *,
        now: float,
        event: str,
        detail: dict[str, Any] | None = None,
        discard_draft: bool = False,
    ) -> bool:
        """Close one resident page and record its tab as sleeping.

        Returns whether the record changed. ``discard_draft`` is for a tab
        whose unsaved input expired: the input lived only in the closed
        document, so the tab wakes as a fresh GET and the loss is reported to
        the agent on wake.
        """
        key = (record.workspace_id, tab.tab_id)
        page = self._pages.get(key)
        if page is None:
            return False
        await self._capture_sleep_scroll(tab, page)
        self._intentional_page_closes.add(key)
        try:
            await page.close()
        except Exception:
            if self._pages.get(key) is page:
                return False
            # The close callback won the race: the page (and any draft in
            # it) is gone either way, so record the same sleeping state.
        finally:
            self._intentional_page_closes.discard(key)
        if discard_draft:
            tab.dirty = False
            tab.restorability = "restorable_get"
            tab.draft_discarded_at = now
        tab.owner_kind = "sleeping"
        tab.owner_id = ""
        tab.lease = None
        self.store.append_journal(record.workspace_id, {
            "at": now, "event": event, "tab_id": tab.tab_id,
            "inactive_seconds": round(now - self._tab_activity_at(tab), 3),
            **(detail or {}),
        })
        self._emit_browser_event(record, "suspended", tab.tab_id)
        return True

    async def _suspend_background_tabs(
        self,
        owner: str,
        watched_workspace_id: str = "",
        watched_tab_id: str = "",
        *,
        now: float | None = None,
    ) -> int:
        """Sleep genuinely inactive background tabs.

        Live-view connection used to call this method and close every eligible
        background page immediately.  That made opening one viewer look like
        tabs had slept after seconds.  The capability now merely protects the
        watched page; the lifecycle sweep supplies the deadlines: six hours for
        a clean page, twelve for one holding unsaved form input.
        """
        if not get_setting("browser_workspace_suspend_background_tabs_on_live_view", True):
            return 0
        current = time.time() if now is None else float(now)
        protected = (
            (watched_workspace_id, watched_tab_id)
            if watched_workspace_id and watched_tab_id else None
        )
        suspended = 0
        for record in self._owner_records(owner):
            changed = False
            for tab in list(record.tabs.values()):
                if self._tab_can_sleep_in_background(
                    record, tab, now=current, protected=protected,
                ):
                    slept = await self._sleep_resident_tab(
                        record, tab, now=current, event="tab_slept_after_inactivity",
                    )
                elif self._tab_draft_expired(
                    record, tab, now=current, protected=protected,
                ):
                    slept = await self._sleep_resident_tab(
                        record, tab, now=current,
                        event="tab_draft_discarded_after_inactivity",
                        discard_draft=True,
                    )
                else:
                    continue
                if slept:
                    changed = True
                    suspended += 1
            if changed:
                self.store.save(record)
        return suspended

    async def _relieve_memory_pressure(self, *, now: float | None = None) -> int:
        """Sleep least-recently-used clean background tabs while memory is short.

        Without pressure this does nothing, so tabs keep their ordinary
        lifetime. Under pressure it releases the pages nobody has used for a
        minute, oldest first, before the kernel has to kill a process. Drafts,
        uncertain effects, held or leased tabs and watched pages keep theirs.
        """
        if not get_setting("browser_workspace_memory_pressure_sleep", True):
            return 0
        pressure = read_memory_pressure()
        if pressure is None:
            return 0
        self._memory_pressure_active = pressure.is_low(
            already_low=self._memory_pressure_active,
        )
        if not self._memory_pressure_active:
            return 0
        current = time.time() if now is None else float(now)
        candidates: list[tuple[float, WorkspaceRecord, TabRecord]] = []
        for record in list(self._records.values()):
            if record.status == "sleeping" or self.backend.runtime(record.owner) is None:
                continue
            for tab in list(record.tabs.values()):
                if self._tab_can_sleep_in_background(
                    record, tab, now=current, min_idle=MEMORY_PRESSURE_MIN_IDLE_SECONDS,
                ):
                    candidates.append((self._tab_activity_at(tab), record, tab))
        candidates.sort(key=lambda item: item[0])
        slept = 0
        started = time.monotonic()
        for _, record, tab in candidates[:MEMORY_PRESSURE_MAX_SLEEPS_PER_PASS]:
            # Earlier closes yielded: the person or agent may have resumed this
            # tab, taken a lease, opened a viewer or pinned an operation since
            # the candidate list was built. Re-check right before closing.
            if record.tabs.get(tab.tab_id) is not tab or not self._tab_can_sleep_in_background(
                record, tab, now=current + (time.monotonic() - started),
                min_idle=MEMORY_PRESSURE_MIN_IDLE_SECONDS,
            ):
                continue
            if await self._sleep_resident_tab(
                record, tab, now=current, event="tab_slept_for_memory_pressure",
                detail=pressure.journal(),
            ):
                self.store.save(record)
                slept += 1
            # A closed page's content process takes a moment to exit.
            await asyncio.sleep(MEMORY_PRESSURE_SETTLE_SECONDS)
            pressure = read_memory_pressure()
            if pressure is None or not pressure.is_low(already_low=True):
                break
        if slept:
            log.info(
                "Slept %d browser tab(s) for memory pressure (%s)",
                slept, pressure.journal() if pressure else {},
            )
        return slept

    async def _memory_pressure_loop(self) -> None:
        while True:
            await asyncio.sleep(MEMORY_PRESSURE_POLL_SECONDS)
            try:
                refresh = getattr(self.backend, "refresh_oom_priority", None)
                if refresh is not None:
                    refresh()
                await self._relieve_memory_pressure()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Browser memory-pressure pass failed", exc_info=True)

    @asynccontextmanager
    async def _pin_tab(self, workspace_id: str, tab_id: str):
        key = (workspace_id, tab_id)
        if not hasattr(self, "_operation_pins"):
            self._operation_pins = {}
        self._operation_pins[key] = self._operation_pins.get(key, 0) + 1
        try:
            yield
        finally:
            remaining = self._operation_pins.get(key, 1) - 1
            if remaining > 0:
                self._operation_pins[key] = remaining
            else:
                self._operation_pins.pop(key, None)

    def _quarantine_if_unowned(self, workspace_id: str, page: Any) -> None:
        if any(value is page for (wid, _), value in self._pages.items() if wid == workspace_id):
            return
        record = self._records.get(workspace_id)
        if not record:
            return
        tab = TabRecord(url=str(getattr(page, "url", "about:blank") or "about:blank"), owner_kind="quarantined", owner_id="parent")
        record.tabs[tab.tab_id] = tab
        self._attach_page(record, tab, page)
        self.raise_attention(record, audience="parent", reason="An unattributed popup opened and was quarantined.")

    def begin_shutdown(self) -> None:
        """Stop treating browser exits as crashes; safe from a signal handler."""
        self._shutting_down = True

    def _schedule_recovery(self, workspace_ids: list[str]) -> None:
        if self._shutting_down:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._recover(workspace_ids))
            self._tasks.add(task); task.add_done_callback(self._tasks.discard)
        except RuntimeError:
            pass

    def _schedule_owner_recovery(self, runtime_key: str) -> None:
        for persisted in self.store.load_owner_key(runtime_key):
            self._records.setdefault(persisted.workspace_id, persisted)
        workspace_ids = [
            record.workspace_id for record in self._records.values()
            if owner_key(record.owner) == runtime_key and record.status != "sleeping"
        ]
        if workspace_ids:
            self._schedule_recovery(workspace_ids)

    async def _recover(self, workspace_ids: list[str]) -> None:
        if self._shutting_down:
            return
        for workspace_id in workspace_ids:
            record = self._records.get(workspace_id)
            if not record:
                continue
            for view in self.live_views.for_runtime(
                str(getattr(self.backend.runtime(record.owner), "liveview_runtime_id", "") or "")
            ):
                if view.workspace_id == workspace_id:
                    self._liveview_controller_by_runtime.pop(view.runtime_id, None)
                    self._liveview_viewport_owner_by_runtime.pop(view.runtime_id, None)
                    self.live_views.invalidate_runtime(view.runtime_id)
            record.status = "needs_recovery"
            record.recovery_warning = "Browser disconnected; recovery is using the last committed checkpoint."
            for tab in record.tabs.values():
                # A parent holds a write lease on every tab it has used, so the
                # lease alone is not evidence of lost work. Only a draft, a
                # non-GET result, a hold, or an action in flight at disconnect
                # is uncertain; a clean GET revives lazily like any sleeping tab.
                if tab.lease and tab.lease.mode == "write" and (
                    tab.dirty
                    or tab.hold is not None
                    or str(tab.last_method or "GET").upper() != "GET"
                    or self._operation_pins.get((workspace_id, tab.tab_id), 0) != 0
                ):
                    tab.uncertain = True
                    tab.restorability = "non_restorable"
            self.store.save(record)
        for attempt in range(3):
            await asyncio.sleep((attempt + 1) * 2)
            if self._shutting_down:
                # Relaunching here launched a browser the exiting process then
                # killed mid-wake, persisting "waking" for the next process.
                return
            failed = []
            for workspace_id in workspace_ids:
                record = self._records.get(workspace_id)
                if not record:
                    continue
                try:
                    await self.ensure_awake(record.owner, record.session_id)
                except Exception:
                    failed.append(workspace_id)
            if not failed:
                return
            workspace_ids = failed
        for workspace_id in workspace_ids:
            record = self._records.get(workspace_id)
            if record:
                record.status = "error"
                record.recovery_warning = "Browser recovery failed after three attempts; partial results remain available."
                self.store.save(record)

    async def _click_page_state(self, page: Any) -> str | None:
        try:
            return str(await _bounded(page.evaluate(
                "() => {" + SHADOW_DOM_JS + " return location.href + '\\n' + odyInnerText(document.body); }"
            ), 1.0))
        except Exception:
            return None

    async def _start_click_effect_probe(self, root: Any, selector: str) -> bool:
        """Arm a bounded semantic mutation recorder before click dispatch.

        Status regions may live anywhere in the target document, but generic
        control mutations only prove this click when they occur on the clicked
        element itself.  Otherwise unrelated timers or hidden bookkeeping
        fields can mint false effect evidence.
        """
        script = """selector => {
          const key = Symbol.for('odysseus.browser.clickEffect');
          try { window[key]?.observer?.disconnect(); } catch (e) {}
          const target = document.querySelector(selector);
          const state = {events: [], targetFound: !!target};
          const push = event => {
            state.events.push(event);
            if (state.events.length > 24) state.events.shift();
          };
          const observer = new MutationObserver(records => {
            for (const mutation of records) {
              const raw = mutation.target;
              const el = raw && raw.nodeType === 1 ? raw : raw?.parentElement;
              if (!el) continue;
              const region = el.closest?.('[role="status"],[role="alert"],[aria-live]');
              if (region) {
                const text = (region.innerText || region.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 240);
                if (text) push({kind: 'status', text});
              }
              if (mutation.type === 'attributes' && el === target) {
                const name = mutation.attributeName || '';
                if (['aria-checked','aria-pressed','aria-expanded','aria-busy','checked','disabled','value'].includes(name)) {
                  push({kind: 'target_state', attribute: name,
                    value: el.getAttribute(name) || (name === 'checked' ? String(!!el.checked) : ''),
                    role: el.getAttribute('role') || '',
                    label: el.getAttribute('aria-label') || ''});
                }
              }
            }
          });
          observer.observe(document.documentElement, {subtree: true, childList: true,
            characterData: true, attributes: true,
            attributeFilter: ['aria-checked','aria-pressed','aria-expanded','aria-busy','checked','disabled','value']});
          window[key] = {state, observer};
          return true;
        }"""
        try:
            return bool(await _bounded(root.evaluate(script, selector), 1.0))
        except Exception:
            return False

    async def _read_click_effect_probe(self, root: Any, *, finish: bool = False) -> list[dict]:
        script = """finish => {
          const holder = window[Symbol.for('odysseus.browser.clickEffect')];
          if (!holder) return [];
          if (finish) { try { holder.observer.disconnect(); } catch (e) {} }
          return Array.isArray(holder.state?.events) ? holder.state.events.slice(-24) : [];
        }"""
        try:
            value = await _bounded(root.evaluate(script, finish), 1.0)
            return [item for item in value or [] if isinstance(item, dict)]
        except Exception:
            return []

    def _start_click_network_probe(self, page: Any) -> tuple[list[dict], Any]:
        """Capture bounded, body-free evidence of click-triggered HTTP effects.

        This records only same-origin, non-read requests observed after the
        probe is armed. Bodies and query strings never enter the receipt. A
        successful response proves an effect occurred, not that the user's
        whole objective is complete.
        """
        events: list[dict] = []
        page_url = urlparse(str(getattr(page, "url", "") or ""))
        origin = (page_url.scheme.casefold(), page_url.netloc.casefold())

        def on_response(response: Any) -> None:
            try:
                request = response.request
                method = str(request.method or "").upper()
                if method in {"", "GET", "HEAD", "OPTIONS"}:
                    return
                parsed = urlparse(str(response.url or ""))
                if (parsed.scheme.casefold(), parsed.netloc.casefold()) != origin:
                    return
                events.append({
                    "method": method,
                    "status": int(response.status),
                    "path": parsed.path or "/",
                    "resource_type": str(request.resource_type or ""),
                })
                del events[:-8]
            except Exception:
                return

        try:
            page.on("response", on_response)
        except Exception:
            return events, None
        return events, on_response

    def _start_click_dialog_probe(self, page: Any) -> tuple[list[dict], Any]:
        """Record JS dialogs a click opens, and dismiss them as before.

        With no listener Playwright dismisses every alert/confirm/prompt
        silently: a confirm() answered Cancel and the click still read as a
        plain page change. Dismissal stays the policy (accepting a "Delete?"
        confirm is not the agent's call to make implicitly); what changes is
        that the result now says a dialog appeared, what it asked, and that
        the page received Cancel.
        """
        events: list[dict] = []

        async def on_dialog(dialog: Any) -> None:
            try:
                events.append({
                    "type": str(getattr(dialog, "type", "") or ""),
                    "message": str(getattr(dialog, "message", "") or "")[:200],
                    "handled": "dismissed",
                })
                del events[:-4]
            except Exception:
                pass
            try:
                await dialog.dismiss()
            except Exception:
                pass

        try:
            page.on("dialog", on_dialog)
        except Exception:
            return events, None
        return events, on_dialog

    def _finish_click_dialog_probe(self, page: Any, listener: Any) -> None:
        if listener is None:
            return
        try:
            page.remove_listener("dialog", listener)
        except Exception:
            pass

    def _finish_click_network_probe(self, page: Any, listener: Any) -> None:
        if listener is None:
            return
        try:
            page.remove_listener("response", listener)
        except Exception:
            pass

    async def _expectation_state(
        self, page: Any, expectation: dict | None,
    ) -> tuple[dict | None, str]:
        """Read a postcondition once: (evidence, "met" | "absent" | "unreadable").

        A page that cannot be read right now — mid-navigation, its execution
        context destroyed, or simply busy past the bound — has not answered the
        question. Reporting that as "the expectation is not there" is what made
        a sign-in whose error banner was already on the page fail its own step.
        """
        if not isinstance(expectation, dict) or not expectation:
            return None, "absent"
        try:
            # The separator must stay escaped: as a real newline Python folds
            # it into the JS source, the string literal breaks across a line,
            # and every evaluate throws "unescaped line break" — which is how
            # this check silently reported every postcondition as absent.
            state = await _bounded(page.evaluate("expected => {" + SHADOW_DOM_JS + r""" return ({
              url: location.href,
              text: [
                odyInnerText(document.body),
                // Rendered names only: a hidden toast template's aria-label
                // is not text the page shows, and it satisfied the guard.
                ...odyQueryAll(document, '[aria-label], img[alt]')
                  .filter(el => el.getClientRects().length
                    && getComputedStyle(el).visibility !== 'hidden')
                  .map(el => el.getAttribute('aria-label') || el.getAttribute('alt') || ''),
                ...Array.from(document.querySelectorAll('frame, iframe')).map(frame => {
                  try {
                    const doc = frame.contentDocument;
                    return doc && doc.body ? doc.body.innerText : '';
                  } catch (error) { return ''; }
                })
              ].join('\n')
            }); }""", expectation), 1.0)
        except Exception:
            return None, "unreadable"
        if not isinstance(state, dict):
            return None, "unreadable"
        wanted_text = str(expectation.get("text") or "").strip()
        wanted_url = str(expectation.get("url_contains") or "").strip()
        # "The error banner went away" is as common an outcome as one
        # appearing (Playwright MCP's browser_wait_for textGone).
        gone_text = str(expectation.get("text_gone") or "").strip()
        if not wanted_text and not wanted_url and not gone_text:
            return None, "absent"
        # Whitespace-collapsed and case-insensitive, the way Playwright's own
        # text matchers compare by default: a page that renders the expected
        # sentence across two lines still contains it.
        page_text = _fold_text(state.get("text"))
        if wanted_text and _fold_text(wanted_text) not in page_text:
            return None, "absent"
        if gone_text and _fold_text(gone_text) in page_text:
            return None, "absent"
        if wanted_url and wanted_url not in str(state.get("url") or ""):
            return None, "absent"
        evidence = {"text": wanted_text or None, "url_contains": wanted_url or None}
        if gone_text:
            evidence["text_gone"] = gone_text
        return evidence, "met"

    async def _click_expectation_met(self, page: Any, expectation: dict | None) -> dict | None:
        evidence, _state = await self._expectation_state(page, expectation)
        return evidence

    async def _ref_presence(self, page: Any, ref: str, *, workspace_id: str, tab_id: str) -> tuple[bool | None, bool]:
        """(present, visible) for an observed ref, read from the live DOM.

        ``present`` is None when the ref cannot be resolved at all (unknown,
        or invalidated by a resize): that is not the same as the element
        having left the page, and a wait must never report it as gone.
        """
        from ascended_browser._app.browser_click_helpers import resolve_target

        try:
            root, selector = resolve_target(page, ref, workspace_id=workspace_id, tab_id=tab_id)
        except Exception:
            return None, False
        try:
            count = await asyncio.wait_for(root.locator(selector).count(), timeout=1.5)
        except Exception:
            return None, False
        if count == 0:
            return False, False
        try:
            state = await _bounded(root.locator(selector).first.evaluate(
                """el => ({present: !!el && el.isConnected,
                           visible: !!el && el.getClientRects().length > 0
                             && getComputedStyle(el).visibility !== 'hidden'})""",
                timeout=1000,
            ), 1.5)
        except Exception:
            return None, False
        if not isinstance(state, dict):
            return None, False
        return bool(state.get("present")), bool(state.get("visible"))

    async def _await_until(
        self, record: WorkspaceRecord, tab: TabRecord, page: Any, conditions: dict[str, Any], *,
        kind: str, effect_barrier: Any | None = None,
    ) -> dict[str, Any]:
        """Poll until every `until` condition holds, the budget ends, or an error ends it.

        Re-queries on every poll instead of holding handles: a page that
        re-renders between polls still answers truthfully.
        """
        from ascended_browser._app.browser_click_helpers import ref_identity
        from ascended_browser._app.browser_reliability import classify_blocker
        from ascended_browser._app.browser_wait_conditions import describe_condition, describe_conditions, unmet_detail

        workspace_id, tab_id = record.workspace_id, tab.tab_id
        started = time.monotonic()
        budget_s = float(conditions.get("timeout_ms") or 5000) / 1000.0
        deadline = started + budget_s
        expectation = {k: conditions[k] for k in ("text", "text_gone", "url_contains") if k in conditions}
        diagnostics = self._diagnostics.get((workspace_id, tab_id))
        error_mark = diagnostics.action_mark if diagnostics is not None and diagnostics.action_mark is not None else (
            diagnostics.seq if diagnostics is not None else 0
        )
        activity = ActionActivity(page).start() if conditions.get("network_idle") else None
        state = "unmet"
        unmet: list[str] = []
        observed = ""
        replaced_by = ""
        try:
            while True:
                unmet = []
                readable = True
                if expectation:
                    evidence, read_state = await self._expectation_state(page, expectation)
                    if evidence is None:
                        unmet.extend(describe_condition(k, v) for k, v in expectation.items())
                        readable = read_state != "unreadable"
                for key in ("ref_visible", "ref_gone"):
                    ref = conditions.get(key)
                    if not ref:
                        continue
                    present, visible = await self._ref_presence(page, ref, workspace_id=workspace_id, tab_id=tab_id)
                    if present is None:
                        # Unknown or invalidated: cannot be judged either way.
                        unmet.append(f"{describe_condition(key, ref)} (ref not known on this tab; observe again)")
                        continue
                    if key == "ref_visible" and not (present and visible):
                        unmet.append(describe_condition(key, ref))
                    if key == "ref_gone" and present:
                        unmet.append(describe_condition(key, ref))
                if activity is not None:
                    # The fresh listener sees only requests started after it;
                    # the tab's log sees the ones a click or navigation already
                    # had in flight when the wait began.
                    earlier = diagnostics.inflight() if diagnostics is not None else []
                    pending = activity.pending() or earlier
                    if pending or time.monotonic() - activity.last_change < 0.5:
                        unmet.append("network idle" + (f" ({len(pending)} in flight)" if pending else ""))
                if conditions.get("no_new_errors") and diagnostics is not None:
                    errors = diagnostics.errors_since(error_mark)
                    if errors:
                        state, observed = "errored", "; ".join(errors[:2])
                        unmet.append("no new page errors")
                        break
                if unmet and expectation and kind != "wait" and diagnostics is not None:
                    # The action's own save came back as an error: the text it
                    # was waiting for is not coming. Say so now instead of
                    # running the budget out on it.
                    failed_writes = diagnostics.failed_writes_since(error_mark)
                    if failed_writes:
                        state, observed = "errored", "; ".join(failed_writes[:2])
                        break
                if conditions.get("wall_cleared"):
                    try:
                        head = await _bounded(page.evaluate(
                            "() => ({url: location.href, title: document.title, text: (document.body ? document.body.innerText : '').slice(0, 4000)})"
                        ), 1.0)
                    except Exception:
                        head, readable = None, False
                    if not isinstance(head, dict) or classify_blocker(head):
                        unmet.append("bot wall cleared")
                if not unmet:
                    state = "met"
                    break
                if not readable:
                    state = "unreadable"
                if effect_barrier is not None and getattr(effect_barrier, "transition_pending", False):
                    state = "deferred"
                    break
                if time.monotonic() >= deadline:
                    if state == "met":
                        state = "unmet"
                    break
                await asyncio.sleep(0.15)
        finally:
            if activity is not None:
                activity.stop()
        waited_ms = round((time.monotonic() - started) * 1000)
        if state == "met" and conditions.get("ref_gone"):
            # Gone, or rebuilt? A same-named element that now exists is the
            # usual React remount; say so instead of letting "gone" mislead.
            identity = ref_identity(conditions["ref_gone"], workspace_id=workspace_id, tab_id=tab_id) or {}
            try:
                replaced_by = await asyncio.wait_for(self._reresolve_by_label(
                    record, tab, page, "", lock_held=True, identity=identity,
                ), timeout=8.0)
            except Exception:
                replaced_by = ""
        result: dict[str, Any] = {
            "met": state == "met",
            "state": state,
            "conditions": describe_conditions(conditions),
            "waited_ms": waited_ms,
            "timeout_ms": conditions.get("timeout_ms"),
        }
        if state == "met":
            result["detail"] = (
                f"{'Waited' if kind == 'wait' else kind.capitalize() + ' dispatched and the browser waited'} {waited_ms} ms until "
                + "; ".join(result["conditions"]) + "."
            )
        else:
            result["unmet"] = unmet
            result["detail"] = unmet_detail(kind, unmet, waited_ms, observed=observed, state=state)
        if replaced_by:
            result["replaced_by"] = replaced_by
            result["detail"] += f" The element was rebuilt: a same-named element now exists as ref={replaced_by}."
        if observed and state != "errored":
            result["observed"] = observed
        return result

    async def _await_expectation(
        self, page: Any, expectation: dict | None, *, budget_s: float,
    ) -> tuple[dict | None, str, int]:
        """Wait for a postcondition the way an assertion does, not a sample.

        The effect an action asks for is rarely present the instant the action
        returns: a sign-in POSTs, redirects and then renders; an async control
        enables itself a second later with no request at all. Playwright's
        assertions retry until a timeout (its MCP server uses 5 s) instead of
        reading once, and that is what this does. Returns the evidence, the
        last state, and how long it waited.
        """
        started = time.monotonic()
        deadline = started + max(0.0, budget_s)
        state = "absent"
        while True:
            evidence, state = await self._expectation_state(page, expectation)
            if evidence is not None:
                return evidence, "met", round((time.monotonic() - started) * 1000)
            if time.monotonic() >= deadline:
                return None, state, round((time.monotonic() - started) * 1000)
            await asyncio.sleep(0.15)

    async def _wait_for_click_change(
        self, page: Any, before: str | None, *, probe_started: bool = False,
        probe_root: Any | None = None, expectation: dict | None = None,
        network_events: list[dict] | None = None,
        effect_barrier: _RuntimeEffectBarrier | None = None,
        dialog_events: list[dict] | None = None,
    ) -> dict:
        # Event-driven evidence returns as soon as it becomes useful. An
        # unchanged page is still uncertain, but it no longer pays the old five
        # second whole-page polling delay plus another settle window.
        budget = max(0.1, min(5.0, float(
            get_setting("browser_workspace_click_effect_seconds", 1.5) or 1.5
        )))
        deadline = time.monotonic() + budget
        changed = False
        verification_deferred = False
        expectation_evidence = None
        semantic_events: list[dict] = []
        while before is not None and time.monotonic() < deadline:
            if effect_barrier is not None and effect_barrier.transition_pending:
                verification_deferred = True
                break
            expectation_evidence = await self._click_expectation_met(page, expectation)
            if expectation_evidence:
                break
            # With a requested postcondition, other effects are not the answer
            # the caller asked for: keep waiting for it rather than returning
            # on the first status message, and record what did happen.
            if probe_started and probe_root is not None:
                semantic_events = await self._read_click_effect_probe(probe_root)
                if not expectation and any(item.get("kind") in {"status", "target_state"} for item in semantic_events):
                    break
            if not expectation and network_events and any(200 <= int(item.get("status") or 0) < 400 for item in network_events):
                break
            after = await self._click_page_state(page)
            if after is not None and after != before:
                changed = True
                if not expectation:
                    break
            remaining = max(0.0, deadline - time.monotonic())
            if effect_barrier is not None:
                verification_deferred = await effect_barrier.wait_for_transition(
                    min(0.1, remaining),
                )
                if verification_deferred:
                    break
            else:
                await asyncio.sleep(min(0.1, remaining))
        if probe_started and probe_root is not None:
            semantic_events = await self._read_click_effect_probe(probe_root, finish=True)
        status_events = [item for item in semantic_events if item.get("kind") == "status"]
        target_events = [item for item in semantic_events if item.get("kind") == "target_state"]
        successful_network_events = [
            item for item in (network_events or [])
            if 200 <= int(item.get("status") or 0) < 400
        ]
        failed_network_events = [
            item for item in (network_events or [])
            if int(item.get("status") or 0) >= 400
        ]
        blocking_dialogs = [
            item for item in (dialog_events or []) if item.get("type") != "alert"
        ]
        if verification_deferred:
            state, ok = "verification_deferred", None
            detail = (
                "Click dispatched; final page verification yielded to a pending browser "
                "control or tab-selection transition. Inspect the selected page before retrying."
            )
        elif expectation_evidence:
            state, ok = "postcondition_met", True
            detail = "Click dispatched and the requested visible postcondition was observed."
        elif blocking_dialogs:
            # The page asked a question and was answered Cancel; whatever it
            # did next is the cancel path, not the click's intended effect.
            dialog = blocking_dialogs[-1]
            state, ok = "dialog_dismissed", None
            detail = (
                f"Click opened a {dialog.get('type') or 'JS'} dialog "
                f"({dialog.get('message')!r}); it was dismissed, so the page received "
                "Cancel. Nothing here can accept it; observe the page before retrying."
            )
        elif expectation:
            state, ok = "postcondition_unmet", False
            detail = _unmet_postcondition_detail(
                expectation, status_events=status_events, changed=changed,
            )
        elif status_events:
            state, ok = "status_changed", True
            detail = "Click dispatched and a semantic status/alert region reported the resulting state."
        elif target_events:
            state, ok = "target_state_changed", True
            detail = "Click dispatched and a semantic control state changed."
        elif failed_network_events and not successful_network_events:
            # The click's own request came back as an error. The page may
            # show one, may not; either way this is not a success to retry.
            failed = failed_network_events[-1]
            state, ok = "request_failed", None
            detail = (
                f"Click dispatched and its same-origin {failed.get('method')} "
                f"{failed.get('path')} returned {failed.get('status')}. Read the page's "
                "error (or browser_extract read=network for the response) before retrying."
            )
        elif successful_network_events:
            state, ok = "network_effect_observed", True
            detail = (
                "Click dispatched and a same-origin state-changing request completed successfully. "
                "This verifies the request effect, not the entire user objective."
            )
        elif changed:
            state, ok = "page_changed", True
            detail = "Click dispatched and the page changed; inspect the returned state for task completion."
        else:
            state, ok = "outcome_unknown", None
            detail = (
                "Click dispatched but no causal page, status, or control-state evidence was observed. "
                "The outcome is unknown; inspect destination state before retrying."
            )
        return {
            "state": state,
            "ok": ok,
            "detail": detail,
            "postcondition": expectation_evidence,
            "status_events": status_events[-3:],
            "target_state_events": target_events[-3:],
            "network_events": (network_events or [])[-3:],
            "verification_deferred": verification_deferred,
            **({"failed_requests": failed_network_events[-3:]} if failed_network_events else {}),
            **({"dialogs": list(dialog_events)} if dialog_events else {}),
        }

    async def _settle_page(self, page: Any) -> None:
        """Wait for a just-navigated page to finish rendering, within a budget.

        `wait_until="domcontentloaded"` returns before a client-rendered page has
        drawn anything, so an observe immediately after a navigate saw 0
        interactive elements and the text "Loading…". The agent then reasonably
        concludes the page is empty — the single largest source of "it worked
        last time" nondeterminism, since whether you win the race depends on
        network and CPU.

        Deliberately not `wait_until="networkidle"`: pages with websockets,
        polling or analytics beacons never reach it, and Playwright discourages
        it for exactly that reason. Instead this samples a cheap DOM signature
        and returns as soon as it stops changing, capped either way.
        """
        budget = float(get_setting("browser_workspace_settle_seconds", 5) or 5)
        if budget <= 0:
            return
        deadline = time.monotonic() + budget
        probe = (
            "() => document.querySelectorAll("
            "'a[href],button,input,select,textarea,[role],[tabindex]').length"
            " + ':' + (document.body ? document.body.innerText.length : 0)"
        )
        previous = None
        while time.monotonic() < deadline:
            try:
                signature = str(await _bounded(page.evaluate(probe), min(1.0, deadline - time.monotonic())))
            except Exception:
                return  # navigating, closed, or hostile — not worth stalling for
            if signature == previous and not signature.startswith("0:"):
                return  # two identical samples and the page is not still empty
            previous = signature
            await asyncio.sleep(0.25)

    async def _navigate_until_runtime_transition(
        self,
        page: Any,
        operation: Any,
        effect_barrier: _RuntimeEffectBarrier,
        *,
        wait_seconds: float = 65.0,
    ) -> Any:
        """Run a safe navigation while allowing queued authority to stop it.

        A GET/history navigation can otherwise hold takeover behind Firefox's
        45-60 second timeout while the UI itself gives up after ten seconds.
        Once a transition queues, request ``window.stop()``, cancel only the
        still-pending Playwright wait, and report interruption truthfully.
        Click/submission effects do not use this path because their remote
        consequence must finish verification rather than be guessed at.
        """
        navigation = asyncio.create_task(operation)
        transition = asyncio.create_task(
            effect_barrier.wait_for_transition(max(0.1, wait_seconds)),
        )
        try:
            done, _pending = await asyncio.wait(
                {navigation, transition}, return_when=asyncio.FIRST_COMPLETED,
            )
            if navigation in done:
                transition.cancel()
                return await navigation
            if not await transition:
                return await navigation
            try:
                await _bounded(page.evaluate("() => window.stop()"), 2.0)
            except Exception:
                # Cancelling Playwright's pending navigation waiter remains
                # necessary even when the changing document cannot run JS.
                pass
            if not navigation.done():
                navigation.cancel()
            await asyncio.gather(navigation, return_exceptions=True)
            raise RuntimeTransitionRequested(
                "Browser navigation yielded to a pending control or viewport transition"
            )
        except asyncio.CancelledError:
            navigation.cancel()
            transition.cancel()
            await asyncio.gather(navigation, transition, return_exceptions=True)
            raise
        finally:
            if not transition.done():
                transition.cancel()
            await asyncio.gather(transition, return_exceptions=True)

    async def _read_until_runtime_transition(
        self,
        operation: Any,
        effect_barrier: _RuntimeEffectBarrier,
        *,
        operation_name: str,
        wait_seconds: float,
    ) -> Any:
        """Cancel a safe read when shared runtime authority needs the window.

        Observations, extraction, and policy-gated evaluation have no remote
        consequence to verify. Once takeover or resize queues, keeping their
        Playwright wait alive only delays the authoritative transition and may
        publish refs derived from geometry that is about to change.
        """
        read = asyncio.create_task(operation)
        transition = asyncio.create_task(
            effect_barrier.wait_for_transition(max(0.1, wait_seconds)),
        )
        try:
            done, _pending = await asyncio.wait(
                {read, transition}, return_when=asyncio.FIRST_COMPLETED,
            )
            # Give an observed transition priority when both tasks settle in
            # the same loop turn. A completed read is safe to discard; a resize
            # immediately after it would otherwise make its refs stale before
            # the caller can use them.
            if transition in done and await transition:
                if not read.done():
                    read.cancel()
                await asyncio.gather(read, return_exceptions=True)
                raise RuntimeTransitionRequested(
                    f"Browser {operation_name} yielded to a pending control or viewport transition"
                )
            transition.cancel()
            return await read
        except asyncio.CancelledError:
            read.cancel()
            transition.cancel()
            await asyncio.gather(read, transition, return_exceptions=True)
            raise
        finally:
            if not transition.done():
                transition.cancel()
            await asyncio.gather(transition, return_exceptions=True)

    async def _post_dispatch_read_until_runtime_transition(
        self,
        operation: Any,
        effect_barrier: _RuntimeEffectBarrier,
        *,
        operation_name: str,
        wait_seconds: float,
    ) -> tuple[Any | None, bool]:
        """Yield a safe read after dispatch when human selection is pending.

        The dispatched browser effect remains exactly where it is; this helper
        never cancels, retries, or reports it as absent.  It only cuts off
        passive activity settling, DOM stability waiting, or readback once a
        live-view selection/takeover has queued.  Returning the explicit
        deferred bit lets the action receipt stay truthful while releasing the
        shared native-window barrier promptly.
        """
        try:
            return await self._read_until_runtime_transition(
                operation,
                effect_barrier,
                operation_name=operation_name,
                wait_seconds=wait_seconds,
            ), False
        except RuntimeTransitionRequested:
            return None, True

    async def _settle_activity_after_dispatch(
        self,
        activity: ActionActivity,
        effect_barrier: _RuntimeEffectBarrier,
        *,
        operation_name: str,
    ) -> dict[str, Any]:
        state, deferred = await self._post_dispatch_read_until_runtime_transition(
            activity.settle(),
            effect_barrier,
            operation_name=operation_name,
            wait_seconds=float(get_setting("browser_workspace_settle_seconds", 5) or 5) + 3.0,
        )
        if deferred:
            return {
                "state": "verification_deferred",
                "navigated": False,
                "transition_pending": True,
            }
        return dict(state or {})

    async def _settle_page_after_dispatch(
        self, page: Any, effect_barrier: _RuntimeEffectBarrier,
    ) -> bool:
        """Settle rendering until a pending live-view transition needs focus.

        Returns ``True`` only when the settle read was deferred.  The caller
        has already dispatched and persisted the action boundary, so this is
        intentionally not an error or a reason to redo it.
        """
        _result, deferred = await self._post_dispatch_read_until_runtime_transition(
            self._settle_page(page),
            effect_barrier,
            operation_name="post-dispatch page settle",
            wait_seconds=float(get_setting("browser_workspace_settle_seconds", 5) or 5) + 3.0,
        )
        return deferred

    @staticmethod
    def _is_page_navigation_reset(exc: BaseException) -> bool:
        """A restored Firefox document failed, while its context may be fine."""
        return "NS_ERROR_FAILURE" in f"{type(exc).__name__}: {exc}"

    @classmethod
    def _site_unreachable_error(cls, exc: BaseException, page: Any) -> str:
        """The site's network error when the browser kept its own error page.

        A refused connection, unknown host or TLS failure still commits the
        browser's error document, exactly as a person's tab would show it.
        Discarding that page left the tab unviewable over an outage the user
        can see and retry from the page itself.
        """
        if isinstance(exc, (asyncio.CancelledError, TimeoutError)) or cls._is_page_navigation_reset(exc):
            return ""
        if type(exc).__name__ == "TimeoutError":
            return ""
        try:
            if page.is_closed():
                return ""
        except Exception:
            return ""
        match = re.search(
            r"\b(NS_ERROR_[A-Z_]+|net::ERR_[A-Z_]+|SSL_ERROR_[A-Z_]+|SEC_ERROR_[A-Z_]+)",
            str(exc),
        )
        return match.group(1) if match else ""

    async def _replace_managed_page(
        self, record: WorkspaceRecord, tab: TabRecord, page: Any,
    ) -> Any:
        """Replace a broken Playwright page without creating another UI tab."""
        key = (record.workspace_id, tab.tab_id)
        self._replacing_page_closes.add(key)
        try:
            await page.close()
        finally:
            self._replacing_page_closes.discard(key)
            if self._pages.get(key) is page:
                self._pages.pop(key, None)
        return await self._new_managed_page(record, tab)

    async def _open_existing_tab(
        self,
        record: WorkspaceRecord,
        tab: TabRecord,
        url: str,
        *,
        navigate: bool,
        discard_lost_draft: bool = False,
    ) -> dict:
        """Settle/navigate an existing tab behind an LRU operation pin."""
        async with self._pin_tab(record.workspace_id, tab.tab_id):
            # A navigation in a reused tab is the same load as one in a new tab
            # and needs the same wait. Without the network settle a heavy SPA
            # (a Stripe dashboard list, reused across a whole session) came
            # back after 13 s as its boot shell: 0 elements, then 10, then
            # the 140 the agent needed, one observe round each.
            activity: ActionActivity | None = None
            navigation_state: dict | None = None
            for attempt in range(2):
                try:
                    _, page = await self._get_tab(
                        record, tab.tab_id, discard_lost_draft=discard_lost_draft,
                    )
                    if navigate:
                        try:
                            self._invalidate_observation(record.workspace_id, tab.tab_id)
                            activity = ActionActivity(page).start()
                            await self._navigate_until_runtime_transition(
                                page,
                                page.goto(url, wait_until="domcontentloaded", timeout=45000),
                                self._runtime_effect_barrier(record.owner),
                                wait_seconds=47,
                            )
                        except Exception as exc:
                            if activity is not None:
                                activity.stop()
                                activity = None
                            if not self._is_page_navigation_reset(exc):
                                raise
                            page = await self._replace_managed_page(record, tab, page)
                            activity = ActionActivity(page).start()
                            await self._navigate_until_runtime_transition(
                                page,
                                page.goto(url, wait_until="domcontentloaded", timeout=45000),
                                self._runtime_effect_barrier(record.owner),
                                wait_seconds=47,
                            )
                    break
                except Exception as exc:
                    if activity is not None:
                        activity.stop()
                        activity = None
                    if attempt or not self._is_wedged_error(exc):
                        raise
                    await self._relaunch_wedged_owner(record.owner, type(exc).__name__)
            if self._open_waits_for_render(tab):
                await self._settle_page(page)
            if activity is not None:
                try:
                    # Only a page whose own requests are still running pays for
                    # the network wait; a page that has drawn and gone quiet
                    # returns as quickly as before.
                    if self._open_waits_for_render(tab) and activity.pending():
                        navigation_state = await activity.settle()
                        await self._settle_page(page)
                finally:
                    # Still loading at the cap: the tab's next observe finishes
                    # this wait instead of reporting the half-drawn page.
                    self._keep_or_stop_activity(
                        record.workspace_id, tab.tab_id, activity, navigation_state,
                    )
            await self._refresh_tab(record, tab, page)
            self.store.save(record)
            result = {**tab.public(), "reused": True}
            if navigate:
                result["navigated"] = True
            _attach_still_loading(result, navigation_state)
            if self._open_returns_page(tab):
                # Opening a tab shows the whole page: no delta baseline survives it.
                self.__dict__.get("_last_shown_projection", {}).pop((record.workspace_id, tab.tab_id), None)
                result["page"] = await self._observe_for_result(record, tab, page)
            return self._attach_blocker(record, tab, result)

    async def _open_new_tab(
        self,
        record: WorkspaceRecord,
        tab: TabRecord,
        url: str,
    ) -> dict:
        """Create, navigate and observe one tab as a single pinned operation."""
        navigation_state: dict | None = None
        async with self._pin_tab(record.workspace_id, tab.tab_id):
            for attempt in range(2):
                try:
                    page = await self._new_managed_page(record, tab)
                    if url != "about:blank":
                        self._invalidate_observation(record.workspace_id, tab.tab_id)
                        activity = ActionActivity(page).start()
                        try:
                            await self._navigate_until_runtime_transition(
                                page,
                                page.goto(url, wait_until="domcontentloaded", timeout=45000),
                                self._runtime_effect_barrier(record.owner),
                                wait_seconds=47,
                            )
                            if self._open_waits_for_render(tab):
                                navigation_state = await activity.settle()
                        finally:
                            self._keep_or_stop_activity(
                                record.workspace_id, tab.tab_id, activity, navigation_state,
                            )
                        if self._open_waits_for_render(tab):
                            await self._settle_page(page)
                    break
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError) or attempt or not self._is_wedged_error(exc):
                        record.tabs.pop(tab.tab_id, None)
                        failed_page = self._pages.pop((record.workspace_id, tab.tab_id), None)
                        if failed_page is not None:
                            self._intentional_page_closes.add((record.workspace_id, tab.tab_id))
                            try:
                                await _bounded(failed_page.close(), 1.0)
                            except Exception:
                                pass
                            finally:
                                self._intentional_page_closes.discard((record.workspace_id, tab.tab_id))
                        self._invalidate_observation(record.workspace_id, tab.tab_id)
                        self.__dict__.get("_observation_cache_miss_reasons", {}).pop(
                            (record.workspace_id, tab.tab_id), None,
                        )
                        self.store.save(record)
                        raise
                    await self._relaunch_wedged_owner(record.owner, type(exc).__name__)
                    record.tabs[tab.tab_id] = tab
            await self._refresh_tab(record, tab, page)
            self.store.save(record)
            result = tab.public()
            _attach_still_loading(result, navigation_state)
            if self._open_returns_page(tab):
                # Opening a tab shows the whole page: no delta baseline survives it.
                self.__dict__.get("_last_shown_projection", {}).pop((record.workspace_id, tab.tab_id), None)
                result["page"] = await self._observe_for_result(record, tab, page)
            return self._attach_blocker(record, tab, result)

    @staticmethod
    def _open_waits_for_render(tab: TabRecord) -> bool:
        """Whether an open holds its result until the page has rendered.

        The network and DOM settles exist so an agent's result describes a
        drawn page. A person's tab is shown through the live view, which
        displays the page as it renders; the settles only delayed the tab and
        its stream (up to their full 5 s budget on pages with no controls).
        """
        return tab.lease is None or tab.lease.actor != "user"

    @classmethod
    def _open_returns_page(cls, tab: TabRecord) -> bool:
        """Whether an open result carries the agent's page observation.

        The editor reads only the tab metadata; building the agent snapshot
        (~0.1-0.3 s) for a person's tab just delayed the tab and its stream.
        """
        return cls._open_waits_for_render(tab) and bool(
            get_setting("browser_workspace_open_returns_page", True)
        )

    async def open(
        self, owner: str, session_id: str, url: str = "about:blank", *,
        disposition: str = "handoff", actor: str = "parent", actor_id: str = "parent",
        reuse: bool = True, operation_id: str = "", read: Any = False,
        reuse_same_url: bool = False,
        _capacity_reserved: bool = False, _operation_locked: bool = False,
    ) -> dict:
        """Open ``url`` in a tab this actor owns.

        ``reuse_same_url`` (with ``reuse=False``) returns an owned tab already
        at this URL instead of opening a copy, but never navigates a different
        tab: a batch's tabs stay independent pages.
        """
        # Opening a page in order to read it is one intent, and it was two
        # calls: nine of a browsing session's opens were followed straight by
        # an extract of the same tab.
        budget = read_text_budget(read)
        read_token = _READ_TEXT_CHARS.set(budget) if budget else None
        try:
            return await self._open(
                owner, session_id, url, disposition=disposition, actor=actor,
                actor_id=actor_id, reuse=reuse, operation_id=operation_id,
                reuse_same_url=reuse_same_url,
                _capacity_reserved=_capacity_reserved, _operation_locked=_operation_locked,
            )
        finally:
            if read_token is not None:
                _READ_TEXT_CHARS.reset(read_token)

    async def _open(
        self, owner: str, session_id: str, url: str = "about:blank", *,
        disposition: str = "handoff", actor: str = "parent", actor_id: str = "parent",
        reuse: bool = True, operation_id: str = "", _capacity_reserved: bool = False,
        _operation_locked: bool = False, reuse_same_url: bool = False,
    ) -> dict:
        operation_id = str(operation_id or "").strip()
        if operation_id and (len(operation_id) > 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", operation_id)):
            raise ValueError("invalid browser-tab operation id")
        # `operation_id` is accepted only for an explicit fresh tab. Agent
        # reuse retains its established semantics and never consumes it.
        if operation_id and not reuse and not _operation_locked:
            key = (owner, session_id, operation_id)
            lock, entrants = self._manual_open_locks.get(key, (asyncio.Lock(), 0))
            self._manual_open_locks[key] = (lock, entrants + 1)
            try:
                async with lock:
                    existing = self.record(owner, session_id)
                    tab = next(
                        (item for item in existing.tabs.values()
                         if item.open_operation_id == operation_id),
                        None,
                    )
                    if tab is not None:
                        result = tab.public()
                        result["reused"] = True
                        result["idempotent"] = True
                        return result
                    return await self.open(
                        owner, session_id, url,
                        disposition=disposition, actor=actor, actor_id=actor_id,
                        reuse=False, operation_id=operation_id,
                        _capacity_reserved=_capacity_reserved, _operation_locked=True,
                    )
            finally:
                # Keep the shared lock until every request that observed it
                # has drained. A failed first attempt may be retried, but a
                # new arrival must not bypass an already-queued same-id retry.
                current = self._manual_open_locks.get(key)
                if current and current[0] is lock:
                    if current[1] <= 1:
                        self._manual_open_locks.pop(key, None)
                    else:
                        self._manual_open_locks[key] = (lock, current[1] - 1)
        if not _capacity_reserved:
            existing = self.record(owner, session_id)
            if reuse and any(
                self._actor_owns_tab(tab, actor, actor_id)
                for tab in existing.tabs.values()
            ):
                return await self._open(
                    owner, session_id, url,
                    disposition=disposition, actor=actor, actor_id=actor_id,
                    reuse=True, operation_id=operation_id, _capacity_reserved=True,
                    _operation_locked=_operation_locked,
                )
            async with self.reserve_tab_capacity(owner, 1, actor=actor):
                return await self._open(
                    owner, session_id, url,
                    disposition=disposition, actor=actor, actor_id=actor_id,
                    reuse=reuse, operation_id=operation_id, _capacity_reserved=True,
                    _operation_locked=_operation_locked, reuse_same_url=reuse_same_url,
                )
        record = await self.ensure_awake(owner, session_id)
        if actor not in {"parent", "worker", "user"}:
            raise ValueError(f"unknown tab lease actor: {actor}")
        # Shared control: a person holding the browser's native input no longer
        # fences agent tab opens. Opening a tab is a page-level action on the
        # shared browser, not a focus theft the person must authorize; the
        # agent cursor and truthful receipts keep it legible.
        effect_barrier = self._runtime_effect_barrier(owner)
        for attempt in range(2):
            failed_runtime = self.backend.runtime(owner)
            try:
                async with effect_barrier.shared():
                    result = await self._open_after_effect_barrier(
                        record, url, disposition=disposition, actor=actor,
                        actor_id=actor_id, reuse=reuse, operation_id=operation_id,
                        reuse_same_url=reuse_same_url,
                    )
                    if actor in {"parent", "worker"}:
                        opened = record.tabs.get(str(result.get("tab_id") or ""))
                        if opened is not None:
                            self._touch_tab_activity(record, opened, agent=True)
                    return result
            except RuntimeTransitionRequested:
                # A user's fresh GET tab can collide with the old viewer's
                # queued fit/selection. The failed new-page path already
                # removes its provisional tab. Release the shared barrier,
                # let that transition drain, then retry this same operation
                # once; agent effects retain their existing interruption.
                if attempt or actor != "user" or reuse or not operation_id:
                    raise
                if not await effect_barrier.wait_until_quiet():
                    raise
            except TabLifecycleError as exc:
                if (attempt or exc.error_kind != "page_creation_timeout"
                        or exc.failure_stage != "new_page"
                        or not await self._recover_unresponsive_page_creation(record, failed_runtime)):
                    raise

    async def _open_after_effect_barrier(
        self,
        record: WorkspaceRecord,
        url: str,
        *,
        disposition: str,
        actor: str,
        actor_id: str,
        reuse: bool,
        operation_id: str = "",
        reuse_same_url: bool = False,
    ) -> dict:
        """Open/reuse a tab while the owner runtime effect barrier is shared."""
        owner = record.owner
        # Default browser work is sequential in one actor-owned page. Resume
        # prompts often start at a different URL ("start from Google again");
        # limiting reuse to exact URL equality opened a duplicate on every such
        # continuation. Prefer an exact match, otherwise navigate this actor's
        # most recently used page. `reuse=false` remains the explicit request
        # for a genuinely independent second tab.
        if reuse or reuse_same_url:
            candidates: list[TabRecord] = []
            for existing in record.tabs.values():
                lease = getattr(existing, "lease", None)
                if lease and (lease.actor, lease.actor_id) == (actor, actor_id or actor):
                    candidates.append(existing)
                # The exact match also covers this actor's lease-less tabs:
                # sleeping ones and drafts a browser restart lost. Matching on
                # the lease alone reopened all nine such pages of one session
                # (722b3c33) as copies beside the originals.
                if not _same_url(existing.url, url) or not self._actor_owns_tab(existing, actor, actor_id):
                    continue
                try:
                    # Asking for this URL again is a restart of that page: a
                    # draft lost with the old browser is gone either way.
                    return await self._open_existing_tab(
                        record, existing, url, navigate=False, discard_lost_draft=True,
                    )
                except RuntimeTransitionRequested:
                    raise
                except WorkspaceError as exc:
                    if isinstance(exc, TabLifecycleError) and exc.error_kind == "page_creation_timeout":
                        raise
                    break  # tab is gone; fall through and open a fresh one
        if reuse:
            selected = self._liveview_selected.get(owner_key(owner))
            preferred = next(
                (item for item in candidates if selected == (record.workspace_id, item.tab_id)),
                None,
            ) or (max(candidates, key=lambda item: item.updated_at) if candidates else None)
            if preferred is not None:
                try:
                    return await self._open_existing_tab(
                        record,
                        preferred,
                        url,
                        navigate=url != "about:blank",
                    )
                except RuntimeTransitionRequested:
                    raise
                except WorkspaceError as exc:
                    if isinstance(exc, TabLifecycleError) and exc.error_kind == "page_creation_timeout":
                        raise
                    # A dead recovery record must not prevent a fresh page.
                    pass
        tab = TabRecord(
            url=url,
            disposition=disposition if disposition in {"discard", "deliverable", "handoff"} else "handoff",
            open_operation_id=operation_id,
        )
        # A worker lease is a hard eviction barrier in _enforce_owner_tab_limit,
        # unlike a parent lease which is only ambient ownership. That is what
        # keeps a sub-agent's tab alive while it is parked at a hand-off waiting
        # for the parent to answer.
        tab.owner_kind = actor
        tab.owner_id = actor_id or actor
        # A person-opened tab is their seat for a while, but it never fences
        # the agent forever: give person leases the standard user-control
        # expiry so the agent regains the page automatically.
        tab.lease = TabLease(
            actor=actor, actor_id=actor_id or actor,
            expires_at=self._user_control_deadline() if actor == "user" else None,
        )
        record.tabs[tab.tab_id] = tab
        key = (record.workspace_id, tab.tab_id)
        self._opening_tabs[key] = asyncio.current_task()
        try:
            self._emit_browser_event(record, "opened", tab.tab_id)
            return await self._open_new_tab(record, tab, url)
        finally:
            self._opening_tabs.pop(key, None)

    @asynccontextmanager
    async def _tab_action_lock(self, workspace_id: str, tab_id: str):
        """Serialize every operation that touches one tab.

        A parent tool batch may deliberately run actions on separate tabs at the
        same time.  It therefore takes this lock directly, while ordinary
        operations keep their broader owner/process scope as well.  Takeover,
        live-view selection, reads, and close all use ``_action_lock`` below,
        so they wait for the same tab action before changing authority or page
        ownership.
        """
        lock = self._tab_locks.setdefault((workspace_id, tab_id), asyncio.Lock())
        async with lock:
            yield

    def _read_lock(self, workspace_id: str, tab_id: str):
        """Tab-only guard for a parallel parent read, else the ordinary guard."""
        if _PARALLEL_TAB_READ.get():
            return self._tab_action_lock(workspace_id, tab_id)
        return self._action_lock(workspace_id, tab_id)

    @asynccontextmanager
    async def _action_lock(self, workspace_id: str, tab_id: str):
        """Take the configured broad guard, then the universal tab guard."""
        if self.lock_scope == "tab":
            async with self._tab_action_lock(workspace_id, tab_id):
                yield
            return
        scope_lock = (
            self._context_locks.setdefault(self._owner_scope(workspace_id), asyncio.Lock())
            if self.lock_scope == "context" else self._process_lock
        )
        # The order is intentionally broad scope then tab. Parallel parent
        # actions only take the tab guard, so they can never form a cycle with
        # this path, and a normal action remains serialized as before.
        async with scope_lock:
            async with self._tab_action_lock(workspace_id, tab_id):
                yield

    @asynccontextmanager
    async def _liveview_transition(
        self, owner: str, workspace_id: str, tab_id: str, *, label: str = "",
        timeout: float = 8.0,
    ):
        """Drain page effects, lock the tab, then serialize shared focus/authority.

        The order matters. A live navigation owns the tab/effect boundary while
        Stop deliberately bypasses it and uses the owner transition lock. No
        takeover, handback, selection, or close may own that Stop lock while it
        is still waiting for the navigation lock it needs to transition.
        """
        key = owner_key(owner)
        effect_barrier = self._runtime_effect_barrier(owner)
        owner_lock = self._liveview_switch_locks.setdefault(key, asyncio.Lock())
        stage = "effect_barrier"
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                async with effect_barrier.exclusive(
                    priority=True, label=label or "liveview_transition",
                ):
                    stage = "action_lock"
                    async with self._action_lock(workspace_id, tab_id):
                        stage = "owner_lock"
                        async with owner_lock:
                            stage = "transition_body"
                            yield
        except TimeoutError as exc:
            log.warning(
                "Browser live-view transition timed out: label=%s stage=%s elapsed=%.2fs",
                label or "liveview_transition", stage, time.monotonic() - started,
            )
            raise TabLifecycleError(
                "Browser control transition timed out; check the current view before retrying.",
                error_kind="browser_transition_timeout",
            ) from exc

    @staticmethod
    def _domain(url: str) -> str:
        # Browser navigation and stateless fetches must agree on what an
        # origin bucket means; otherwise subdomains can multiply the limit in
        # one path while remaining grouped in the other.
        from ascended_browser.runtime.admission import registrable_domain

        return registrable_domain(url)

    @staticmethod
    def _domain_limit(key: str, default: int) -> int:
        try:
            return max(1, int(get_setting(key, default) or default))
        except (TypeError, ValueError):
            return default

    async def _rate_limit_domain(self, domain: str, owner: str = "") -> None:
        """Pace navigations to one site for this owner and across all owners.

        The window used to be per site for the whole server, so one user's
        sessions spent every other user's budget on a shared site.
        """
        if not domain: return
        buckets = [(domain, self._domain_limit("browser_workspace_domain_global_actions_per_minute", 120))]
        if owner:
            buckets.insert(0, ((owner_key(owner), domain), self._domain_limit("browser_workspace_domain_actions_per_minute", 30)))
        for key, limit in buckets:
            now = time.monotonic(); recent = [stamp for stamp in self._domain_tokens.get(key, []) if now - stamp < 60]
            if len(recent) >= limit:
                await asyncio.sleep(max(0.0, 60 - (now - recent[0])))
        now = time.monotonic()
        for key, _ in buckets:
            recent = [stamp for stamp in self._domain_tokens.get(key, []) if now - stamp < 60]
            recent.append(now); self._domain_tokens[key] = recent

    async def _acquire_domain_slots(self, domain: str, owner: str) -> Callable[[], None]:
        """Hold one of this owner's slots for the site, then a server-wide one."""
        owner_slots = self._domain_slots.setdefault(
            (owner_key(owner), domain),
            asyncio.Semaphore(self._domain_limit("browser_workspace_domain_concurrency", 2)),
        )
        global_slots = self._domain_slots.setdefault(
            domain, asyncio.Semaphore(self._domain_limit("browser_workspace_domain_global_concurrency", 6)),
        )
        await owner_slots.acquire()
        try:
            await global_slots.acquire()
        except BaseException:
            owner_slots.release()
            raise

        def release() -> None:
            global_slots.release()
            owner_slots.release()
        return release

    def _backoff_key(self, workspace_id: str, domain: str) -> tuple[str, str]:
        return (self._owner_scope(workspace_id), domain)

    async def _wait_challenge_backoff(self, workspace_id: str, domain: str) -> None:
        if not domain:
            return
        _, until = self._challenge_backoff.get(self._backoff_key(workspace_id, domain), (0, 0.0))
        if until > time.monotonic():
            await asyncio.sleep(until - time.monotonic())

    def _record_domain_challenge(self, workspace_id: str, domain: str) -> None:
        if not domain:
            return
        key = self._backoff_key(workspace_id, domain)
        count, _ = self._challenge_backoff.get(key, (0, 0.0))
        count = min(count + 1, 8)
        self._challenge_backoff[key] = (count, time.monotonic() + min(300.0, float(2 ** count)))

    async def _auth_signature(self, page: Any, domain: str) -> str:
        if not domain:
            return ""
        import hashlib
        import re

        try:
            # Called twice per act and twice per login, all inside the action
            # lock, so an unbounded cookie read stalls every other tab.
            cookies = await _bounded(
                page.context.cookies([str(getattr(page, "url", "") or "")]),
                float(get_setting("browser_workspace_probe_timeout_seconds", 5) or 5),
            )
        except Exception:
            return ""
        sensitive = [
            cookie for cookie in cookies
            if cookie.get("httpOnly") or re.search(
                r"(?:auth|session|token|jwt|oauth|account|identity)",
                str(cookie.get("name") or ""), re.I,
            )
        ]
        normalized = sorted(
            (str(c.get("name")), str(c.get("domain")), str(c.get("path")), str(c.get("value")))
            for c in sensitive
        )
        return hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode()).hexdigest()

    def _record_domain_disruption(self, workspace_id: str, domain: str, reason: str) -> None:
        if not domain:
            return
        key = (self._owner_scope(workspace_id), domain)
        previous = self._domain_session_state.get(key) or {}
        self._domain_session_state[key] = {
            "version": int(previous.get("version") or 0) + 1,
            "source_workspace_id": workspace_id,
            "reason": reason,
            "at": time.time(),
        }

    def _acknowledge_worker_session(self, workspace_id: str, tab_id: str, domain: str) -> None:
        state = self._domain_session_state.get((self._owner_scope(workspace_id), domain)) or {}
        self._tab_session_versions[(workspace_id, tab_id)] = int(state.get("version") or 0)

    def _assert_worker_session(self, workspace_id: str, tab_id: str, domain: str) -> None:
        state = self._domain_session_state.get((self._owner_scope(workspace_id), domain)) or {}
        current = int(state.get("version") or 0)
        baseline = self._tab_session_versions.get((workspace_id, tab_id), current)
        if current > baseline and state.get("source_workspace_id") != workspace_id:
            raise SessionChangedError(
                "session_changed_under_me: another chat changed login, 2FA, or challenge state "
                f"for {domain}; parent handoff is required"
            )
        self._tab_session_versions[(workspace_id, tab_id)] = current

    async def _get_tab(
        self, record: WorkspaceRecord, tab_id: str, *, revive: bool = True,
        settle: bool = True, discard_lost_draft: bool = False,
        preserve_lease: bool = False,
    ) -> tuple[TabRecord, Any]:
        """Return a tab and its resident page, reviving a clean sleeping GET.

        ``settle=False`` skips the post-revive DOM settle. That wait exists so
        an agent's next observation is not of a half-rendered page; a live
        viewer watches the page render instead, and the settle (up to its full
        budget on pages with no interactive elements) only delayed the stream.

        ``discard_lost_draft`` lets a person selecting the tab reload a lost
        parent page even when it held a draft. That draft died with the old
        browser; the agent path still stops so it cannot keep filling a form
        it believes is intact, and it is told to refill on its next call.

        ``preserve_lease`` is presentation recovery only: keep an absent lease
        absent instead of installing the ordinary default parent lease.
        """
        tab = record.tabs.get(tab_id)
        page = self._pages.get((record.workspace_id, tab_id))
        if tab is None:
            raise TabLifecycleError("Browser tab is closed or unknown", error_kind="unknown_tab")
        if (page is None and self._is_revivable_blank_tab(tab)
                and (record.workspace_id, tab_id) not in self._opening_tabs
                and (tab.owner_kind != "sleeping" or tab.uncertain
                     or tab.restorability != "restorable_get")):
            # Repair records written before clean blank documents had a
            # distinct recovery path.  This only normalizes durable metadata;
            # the actual page is still recreated through the ordinary sleeping
            # tab path below.
            tab.restorability = "restorable_get"
            tab.uncertain = False
            tab.owner_kind = "sleeping"
            tab.owner_id = ""
            self.store.save(record)
            self._emit_browser_event(record, "blank_page_revivable", tab_id)
        lease = tab.lease
        lost_parent_page = (
            page is None
            and (tab.owner_kind == "needs_user" or tab.uncertain)
            and (lease is None or lease.actor == "parent")
            and tab.url.startswith(("http://", "https://"))
        )
        if revive and lost_parent_page:
            if tab.dirty and not discard_lost_draft:
                raise TabLifecycleError(
                    "Dirty browser draft is unavailable; automatic reload is disabled. "
                    "Reconcile any uncertain effect and explicitly restart the page before continuing.",
                    error_kind="page_lost",
                )
            reload = await self._reload_lost_parent_tab_record(
                record, tab_id, settle=settle, preserve_lease=preserve_lease,
            )
            tab = record.tabs[tab_id]
            page = self._pages.get((record.workspace_id, tab_id))
            if page is None and reload is not None and not reload.get("reloaded"):
                # The tab stays a lost parent page, so the next access retries
                # the reload. Saying "requires user recovery" here sent people
                # looking for a recovery step that does not exist.
                raise TabLifecycleError(
                    "Browser tab could not be reloaded after its page state was lost "
                    f"({reload.get('reason') or 'reload failed'}); retry to reload it again.",
                    error_kind="page_reload_failed",
                )
        if revive and page is None and tab.owner_kind == "sleeping":
            if tab.restorability != "restorable_get" or tab.dirty or tab.uncertain:
                raise TabLifecycleError(
                    "Browser tab cannot be restored because its page state may have changed; "
                    "user recovery is required",
                    error_kind="page_lost",
                )
            key = (record.workspace_id, tab_id)
            lock = self._tab_revive_locks.setdefault(key, asyncio.Lock())
            async with lock:
                page = self._pages.get(key)
                if page is None:
                    # Revisit a clean GET under the same durable identity and
                    # invalidate every element ref from its previous document.
                    try:
                        page = await self._new_managed_page(record, tab)
                        # Reviving is use. A stale timestamp let the
                        # inactivity sweep close this page during goto,
                        # leaving a lease on a tab with no page.
                        self._touch_tab_activity(record, tab)
                        self._invalidate_observation(record.workspace_id, tab_id)
                        from ascended_browser._app.browser_click_helpers import invalidate_refs
                        invalidate_refs(workspace_id=record.workspace_id, tab_id=tab_id)
                        tab.last_observation_id = ""
                        if tab.url.startswith(("http://", "https://")):
                            try:
                                await page.goto(tab.url, wait_until="domcontentloaded", timeout=45000)
                            except Exception as navigation_exc:
                                # An unreachable site still leaves the
                                # browser's error page, which the viewer can
                                # retry from; only other failures abort the wake.
                                if not self._site_unreachable_error(navigation_exc, page):
                                    raise
                            else:
                                if settle:
                                    await self._settle_page(page)
                    except BaseException as exc:
                        failed_page = self._pages.pop(key, None)
                        if failed_page is not None:
                            self._intentional_page_closes.add(key)
                            try:
                                await failed_page.close()
                            except Exception:
                                pass
                            finally:
                                self._intentional_page_closes.discard(key)
                        self.store.save(record)
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        if isinstance(exc, BrowserRuntimeUnavailable):
                            raise
                        if isinstance(exc, TabRoomExhausted):
                            # Say why: the browser is full of work in progress,
                            # which a retry after other sessions finish resolves.
                            raise TabLifecycleError(
                                f"Browser tab is sleeping and could not wake: {exc}",
                                error_kind="tab_limit_busy",
                            ) from exc
                        raise TabLifecycleError(
                            "Browser tab wake failed; it remains sleeping and can be retried",
                            error_kind="page_reload_failed",
                        ) from exc
                    await self._restore_sleep_scroll(tab, page)
                    if tab.lease is None and not preserve_lease:
                        tab.lease = TabLease(actor="parent", actor_id="parent")
                    tab.owner_kind = tab.lease.actor if tab.lease is not None else "parent"
                    tab.owner_id = tab.lease.actor_id if tab.lease is not None else "parent"
                    self.store.save(record)
                    self._emit_browser_event(record, "revived", tab_id)
        if page is None:
            if tab.owner_kind == "needs_user" or tab.uncertain:
                raise TabLifecycleError(
                    "Browser tab page state was lost and requires user recovery",
                    error_kind="page_lost",
                )
            if tab.owner_kind == "sleeping":
                raise TabLifecycleError(
                    "Browser tab is sleeping and could not be restored",
                    error_kind="page_lost",
                )
            raise TabLifecycleError("Browser tab is not currently available", error_kind="page_unavailable")
        if tab.draft_discarded_at:
            self._queue_draft_discard_notice(record, tab)
        return tab, page

    def _queue_draft_discard_notice(self, record: WorkspaceRecord, tab: TabRecord) -> None:
        """Tell the agent, on its next look, that an expired draft was dropped.

        The durable ``draft_discarded_at`` marker stays set until the notice is
        delivered (see _attach_blocker), so a restart between wake and the
        agent's next observation re-queues it instead of losing it.
        """
        key = (record.workspace_id, tab.tab_id)
        if key in self._pending_recovery_notices:
            return
        hours = max(1, round(self._dirty_tab_sleep_seconds() / 3600))
        self._pending_recovery_notices[key] = {
            "tab_id": tab.tab_id,
            "url": tab.url,
            "reloaded": True,
            "reason": (
                "Unsaved form input on this tab was discarded after the tab went "
                f"unused for {hours} hours. The page was reloaded fresh; observe it "
                "and refill any fields or uploads still needed."
            ),
        }

    async def reload_lost_parent_tab(
        self, owner: str, session_id: str, tab_id: str, *, settle: bool = True,
    ) -> dict[str, Any] | None:
        """Discard unavailable parent draft state and reload its durable URL.

        A browser restart has already destroyed an in-page draft by the time a
        tab is marked ``needs_user``.  Requiring the human to reopen that URL
        creates an unnecessary dead end: a GET can restore the page for the
        agent, provided we record the loss and never replay the old POST,
        upload, consent, or submission.  This deliberately applies only to the
        parent lease selected by ``browser_resume``.  A user or worker still
        controls their own handoff.
        """
        record = await self.ensure_awake(owner, session_id)
        return await self._reload_lost_parent_tab_record(record, tab_id, settle=settle)

    async def _reload_lost_parent_tab_record(
        self, record: WorkspaceRecord, tab_id: str, *, settle: bool = True,
        preserve_lease: bool = False,
    ) -> dict[str, Any] | None:
        """Record-scoped implementation shared by resume and ordinary access."""
        tab = record.tabs.get(tab_id)
        if tab is None:
            raise WorkspaceError("Browser tab is closed or unknown")
        key = (record.workspace_id, tab_id)
        lease = tab.lease
        if (
            self._pages.get(key) is not None
            or not (tab.owner_kind == "needs_user" or tab.uncertain)
            or (lease is not None and lease.actor != "parent")
        ):
            return None
        if not tab.url.startswith(("http://", "https://")):
            return {
                "tab_id": tab_id,
                "reloaded": False,
                "reason": "The lost tab has no reloadable HTTP URL.",
            }

        restore_selection = False
        try:
            async with self._pin_tab(record.workspace_id, tab_id):
                async with self._action_lock(record.workspace_id, tab_id):
                    # A queued page operation may have restored or reassigned the
                    # tab while this recovery waited for its lifecycle lock.
                    tab = record.tabs.get(tab_id)
                    page = self._pages.get(key)
                    lease = tab.lease if tab is not None else None
                    if tab is None:
                        raise WorkspaceError("Browser tab is closed or unknown")
                    if (
                        page is not None
                        or not (tab.owner_kind == "needs_user" or tab.uncertain)
                        or (lease is not None and lease.actor != "parent")
                    ):
                        return None
                    revive_lock = self._tab_revive_locks.setdefault(key, asyncio.Lock())
                    async with revive_lock:
                        page = self._pages.get(key)
                        if page is not None:
                            return None
                        try:
                            # This recovery already owns the tab action lock. Restore
                            # after releasing it so the selection transaction uses
                            # the canonical effect -> action -> owner lock order.
                            page = await self._new_managed_page(
                                record, tab, restore_live_selection=False,
                            )
                            restore_selection = True
                            # Reloading is use. A tab stale past the sleep
                            # deadline was otherwise eligible for the
                            # inactivity sweep the moment its reload cleared
                            # `uncertain`, closing the page just revived.
                            self._touch_tab_activity(record, tab)
                            self._invalidate_observation(record.workspace_id, tab_id)
                            from ascended_browser._app.browser_click_helpers import invalidate_refs
                            invalidate_refs(workspace_id=record.workspace_id, tab_id=tab_id)
                            tab.last_observation_id = ""
                            unreachable = ""
                            try:
                                await page.goto(tab.url, wait_until="domcontentloaded", timeout=45000)
                            except Exception as navigation_exc:
                                unreachable = self._site_unreachable_error(navigation_exc, page)
                                if not unreachable:
                                    raise
                            if settle and not unreachable:
                                await self._settle_page(page)
                        except BaseException as exc:
                            failed_page = self._pages.pop(key, None)
                            if failed_page is not None:
                                self._intentional_page_closes.add(key)
                                try:
                                    await failed_page.close()
                                except Exception:
                                    pass
                                finally:
                                    self._intentional_page_closes.discard(key)
                            self.store.save(record)
                            if isinstance(exc, asyncio.CancelledError):
                                raise
                            if isinstance(exc, BrowserRuntimeUnavailable):
                                raise
                            log.warning(
                                "Lost browser tab %s could not be reloaded", tab_id,
                                exc_info=True,
                            )
                            return {
                                "tab_id": tab_id,
                                "reloaded": False,
                                "reason": f"Fresh reload failed: {type(exc).__name__}",
                            }

                        # The original document is gone.  This is a *new GET
                        # document*, never a reconstruction of its draft or an
                        # assertion about an earlier remote effect.
                        tab.dirty = False
                        tab.uncertain = False
                        tab.last_method = "GET"
                        tab.owner_kind = "parent"
                        tab.owner_id = "parent"
                        tab.lease = lease if preserve_lease else lease or TabLease(actor="parent", actor_id="parent")
                        intended_url = tab.url
                        await self._refresh_tab(record, tab, page)
                        if unreachable:
                            # Keep the address the browser's error page is
                            # for, so a reload retries the site, not the error.
                            tab.url = intended_url
                            tab.restorability = "restorable_get"
                        self.store.append_journal(record.workspace_id, {
                            "at": time.time(),
                            "event": "lost_page_state_discarded",
                            "tab_id": tab_id,
                            "url": tab.url,
                        })
                        self.store.save(record)
                        self._emit_browser_event(record, "lost_state_reloaded", tab_id)
                        notice = {
                            "tab_id": tab_id,
                            "url": tab.url,
                            "reloaded": True,
                            "reason": (
                                "Browser restart discarded unsaved page state. The tab was "
                                "reloaded with GET; inspect it and refill any draft fields or "
                                "uploads that are still needed."
                            ),
                        }
                        if unreachable:
                            notice["site_error"] = unreachable
                            notice["reason"] += (
                                f" The site did not load ({unreachable}); the tab shows the "
                                "browser's error page until the site is reachable again."
                            )
                        self._pending_recovery_notices[key] = notice
                        return notice
        finally:
            if restore_selection:
                await self._restore_liveview_selection(record.owner)

    async def page_for(self, owner: str, session_id: str, tab_id: str) -> Any:
        """Live page for a tab, reviving it if the resident-page ceiling evicted it.

        Callers must not read self._pages directly: at this ceiling an idle tab
        can be evicted between two calls, and only this path brings it back.
        """
        record = await self.ensure_awake(owner, session_id)
        _, page = await self._get_tab(record, tab_id)
        return page

    async def restore_embedded_tab(self, owner: str, session_id: str, tab_id: str) -> dict[str, Any]:
        """Reattach a native guest after frontend reload without taking control."""
        record = self.record(owner, session_id)
        tab = record.tabs.get(tab_id)
        if tab is None:
            raise TabLifecycleError("Browser tab is closed or unknown", error_kind="unknown_tab")
        runtime = self.backend.runtime(owner)
        if getattr(runtime, "presentation", "") != "embedded":
            raise TabLifecycleError(
                "This browser tab uses a streamed view; reconnect its live view instead.",
                error_kind="embedded_presentation_required",
            )
        awake_record = await self.ensure_awake(owner, session_id)
        if (awake_record is not record or record.tabs.get(tab_id) is not tab
                or self.backend.runtime(owner) is not runtime):
            raise TabLifecycleError(
                "The native browser changed while restoration was waiting; retry restoration.",
                error_kind="embedded_runtime_changed",
            )
        key = (record.workspace_id, tab_id)
        page = self._pages.get(key)
        if page is not None and callable(getattr(page, "is_closed", None)) and page.is_closed():
            self._on_page_closed(record.workspace_id, tab_id, page)
            page = self._pages.get(key)
        if page is None and not (
            self._is_revivable_blank_tab(tab)
            or (tab.restorability == "restorable_get" and not tab.dirty and not tab.uncertain
                and tab.hold is None and str(tab.last_method or "GET").upper() == "GET"
                and tab.url.startswith(("http://", "https://")))
        ):
            raise TabLifecycleError(
                "The browser page state is unavailable and cannot be restored safely. "
                "Reconcile any uncertain effect and explicitly restart the page; automatic draft discard is disabled.",
                error_kind="page_lost",
            )
        _, page = await self._get_tab(record, tab_id, settle=False, preserve_lease=True)
        if (self.backend.runtime(owner) is not runtime
                or self._records.get(record.workspace_id) is not record
                or record.tabs.get(tab_id) is not tab or self._pages.get(key) is not page):
            raise TabLifecycleError(
                "The native browser changed while restoration was waiting; retry restoration.",
                error_kind="embedded_runtime_changed",
            )
        identity = self.backend.page_identity(owner, page)
        if not isinstance(identity, dict) or not identity.get("guest_key") or not identity.get("target_id"):
            raise TabLifecycleError(
                "The native browser guest is not attached yet; retry restoration.",
                error_kind="embedded_guest_unavailable",
            )
        return {**self.status(owner, session_id), "status": "attached", "tab_id": tab_id,
                "guest_key": identity["guest_key"], "target_id": identity["target_id"]}

    async def credential_request_target(
        self, owner: str, session_id: str, tab_id: str,
    ) -> dict[str, str]:
        """Capture a credential request only from a proven live login form."""
        from ascended_browser._app.browser_login_broker import credential_request_target
        from ascended_browser._app.browser_click_helpers import build_enhanced_snapshot
        from ascended_browser._app.browser_reliability import classify_auth_state

        record = await self.ensure_awake(owner, session_id)
        async with self._action_lock(record.workspace_id, tab_id):
            tab, page = await self._get_tab(record, tab_id)
            snapshot = await asyncio.wait_for(
                build_enhanced_snapshot(
                    page, workspace_id=record.workspace_id, tab_id=tab_id,
                ),
                timeout=float(get_setting("browser_workspace_observe_timeout_seconds", 90) or 90),
            )
            auth_state = classify_auth_state(snapshot)
            if auth_state.get("state") != "login_form_visible":
                raise WorkspaceError(
                    "Credential request refused: the current tab does not contain a visible login form. "
                    "Observe the page and continue without asking for credentials."
                )
            await self._refresh_tab(record, tab, page, agent=True)
            self.store.save(record)
            target = credential_request_target(str(getattr(page, "url", "") or tab.url))
        return {"tab_id": tab_id, **target}

    async def prepare_login_continuation(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        url: str,
        origin: str,
    ) -> dict[str, Any]:
        """Recover the reviewed login page without making tab loss fatal."""
        from ascended_browser._app.browser_login_broker import credential_request_target

        try:
            record = await self.ensure_awake(owner, session_id)
            if tab_id in record.tabs:
                try:
                    tab, page = await self._get_tab(record, tab_id)
                    current = credential_request_target(
                        str(getattr(page, "url", "") or tab.url)
                    )
                    if current["origin"] == origin:
                        self._assert_may_mutate(tab, "parent", "parent", "login")
                        return {"ready": True, "tab_id": tab_id, "recovery": "existing_tab"}
                except Exception:
                    # Closed, sleeping/non-restorable, and lost pages all fall
                    # through to a fresh tab at the reviewed safe GET URL.
                    pass
            opened = await self.open(
                owner,
                session_id,
                url,
                disposition="handoff",
                actor="parent",
                actor_id="parent",
                reuse=False,
            )
            return {
                "ready": True,
                "tab_id": str(opened.get("tab_id") or ""),
                "recovery": "new_tab",
            }
        except Exception as exc:
            return {
                "ready": False,
                "tab_id": "",
                "recovery": "retry_required",
                "error": f"{type(exc).__name__}: browser page could not be restored"[:200],
            }

    @staticmethod
    def _attach_login_available(owner: str, result: dict[str, Any]) -> dict[str, Any]:
        from ascended_browser._app.browser_login_broker import login_available_for_url

        output = dict(result)
        output.pop("login_available", None)
        available = login_available_for_url(str(output.get("url") or ""), owner=owner)
        if available:
            output["login_available"] = available
        return output

    def browser_proxy(self, owner: str) -> str:
        """The proxy URL the owner's browser was launched with, "" when direct.

        Sidecars that must present the browser's exit IP (Cloudflare clearance
        is IP-bound) read it from here instead of a parallel setting, so a
        proxied browser and its sidecar can never drift apart.
        """
        runtime = self.backend.runtime(owner)
        return str(getattr(runtime, "proxy", "") or "")

    async def observe(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        actor: str = "parent",
        actor_id: str = "parent",
        view: dict | None = None,
        parallel_tab: bool = False,
    ) -> dict:
        _parallel_token = _PARALLEL_TAB_READ.set(bool(parallel_tab))
        try:
            return await self._observe_entry(
                owner, session_id, tab_id, actor=actor, actor_id=actor_id, view=view,
            )
        finally:
            _PARALLEL_TAB_READ.reset(_parallel_token)

    async def _observe_entry(
        self, owner: str, session_id: str, tab_id: str, *,
        actor: str, actor_id: str, view: dict | None,
    ) -> dict:
        async with self._pin_tab(self.workspace_id(owner, session_id), tab_id):
            effect_barrier = self._runtime_effect_barrier(owner)
            async with effect_barrier.shared():
                self.assert_tab_access(
                    owner, session_id, tab_id, actor=actor, actor_id=actor_id,
                )
                return await self._read_until_runtime_transition(
                    self._observe(owner, session_id, tab_id, view=view),
                    effect_barrier,
                    operation_name="observation",
                    wait_seconds=float(
                        get_setting("browser_workspace_observe_timeout_seconds", 90) or 90
                    ) + 15.0,
                )

    async def _observe(self, owner: str, session_id: str, tab_id: str, *, view: dict | None = None) -> dict:
        from ascended_browser._app.browser_click_helpers import build_enhanced_snapshot
        started = time.monotonic()
        record = await self.ensure_awake(owner, session_id)
        tab, page = await self._get_tab(record, tab_id)
        view = dict(view or {})
        # The previous action returned while the page was still loading: finish
        # waiting on that same activity, then report whether it completed.
        loading = await self._finish_loading(record.workspace_id, tab_id)
        if loading is not None:
            self._invalidate_observation(record.workspace_id, tab_id)
            await self._settle_page(page)
        # The cache holds the default, unscoped projection. A scoped view must
        # never be answered from it, or `within` would silently return the
        # whole page it was meant to narrow.
        cache_entry = self._observation_cache.get((record.workspace_id, tab_id))
        cache_age_ms = round((started - cache_entry[0]) * 1000) if cache_entry else None
        if view:
            cache_status = "scoped_bypass"
            cached = None
        else:
            cached, cache_status = self._cached_observation(
                record.workspace_id, tab, page, with_reason=True,
            )
        if cached is not None and cached.get("viewport_revision"):
            # TTL alone cannot make a viewport reading current: a nested scroll,
            # append or rerender can leave the URL unchanged. A cheap live
            # revision probe retains warm hits without a second DOM collection.
            try:
                from ascended_browser._app.browser_viewport_content import VIEWPORT_SIGNATURE_PROBE
                revision = await _bounded(page.evaluate(VIEWPORT_SIGNATURE_PROBE), 1.0)
            except Exception:
                revision = None
            if revision != cached["viewport_revision"] or int(cached.get("frames_observed") or 1) > 1:
                self._invalidate_observation(record.workspace_id, tab_id, reason="viewport_changed")
                cached, cache_status = None, "viewport_changed"
        if cached is not None:
            self._record_observation_cache_event(
                tab_id, "hit", age_ms=cache_age_ms,
                total_ms=round((time.monotonic() - started) * 1000), collect_ms=0,
            )
            cached = self._attach_login_available(owner, cached)
            note = self.take_note(record, tab)
            if note:
                cached["user_note"] = (
                    f"The user took control of this tab and says: {note!r}. "
                    "Treat that as what happened; do not redo it."
                )
            return self._attach_blocker(record, tab, cached)
        collect_started = time.monotonic()
        async with self._read_lock(record.workspace_id, tab_id):
            # Backstop over the per-probe bounds in build_enhanced_snapshot. This
            # lock is per-owner at the default context scope, so an unbounded
            # observe wedged every tab in every chat for this owner and simply
            # never returned to the model.
            snapshot = await asyncio.wait_for(
                build_enhanced_snapshot(page, workspace_id=record.workspace_id, tab_id=tab_id),
                timeout=float(get_setting("browser_workspace_observe_timeout_seconds", 90) or 90),
            )
        collect_ms = round((time.monotonic() - collect_started) * 1000)
        tab.last_observation_id = str(snapshot.get("observation_id") or (snapshot.get("elements") or [{}])[0].get("observation_id") or "")
        await self._refresh_tab(record, tab, page, agent=True)
        self.store.save(record)
        within = str(view.get("within") or "")
        if within.startswith("e"):
            # An element ref scopes to that element and everything the page
            # renders inside it. Containment is read from the live DOM, where
            # every collected element carries its ref marker.
            view["_within_refs"] = await self._descendant_refs(
                page, within, workspace_id=record.workspace_id, tab_id=tab_id,
            )
        if view.get("filter") == "all":
            view["_headings"] = await self._page_headings(page)
        projected = self._project_observation(
            record, tab, snapshot, text=await self._page_digest(page), view=view or None,
        )
        projected = self._attach_login_available(owner, projected)
        if loading is not None:
            from ascended_browser._app.browser_action_settle import still_loading_note

            projected["navigation_state"] = {**loading, **(
                {"next_step": still_loading_note(loading)} if still_loading_note(loading) else {}
            )}
        if not view:
            self._cache_observation(record.workspace_id, tab, page, projected)
        self._record_observation_cache_event(
            tab_id, cache_status, age_ms=cache_age_ms,
            total_ms=round((time.monotonic() - started) * 1000), collect_ms=collect_ms,
        )
        note = self.take_note(record, tab)
        if note:
            projected["user_note"] = (
                f"The user took control of this tab and says: {note!r}. "
                "Treat that as what happened; do not redo it."
            )
        return self._attach_blocker(record, tab, projected)

    def _keep_or_stop_activity(
        self, workspace_id: str, tab_id: str, activity: ActionActivity, state: dict | None,
    ) -> None:
        """Keep a still-loading action's activity for the tab's next call."""
        store = self.__dict__.setdefault("_loading_activity", {})
        previous = store.pop((workspace_id, tab_id), None)
        if previous is not None and previous is not activity:
            previous.stop()
        if isinstance(state, dict) and state.get("state") == "still_loading":
            store[(workspace_id, tab_id)] = activity
        else:
            activity.stop()

    async def _finish_loading(self, workspace_id: str, tab_id: str) -> dict | None:
        """Wait out an action that returned while its page was still loading."""
        activity = self.__dict__.get("_loading_activity", {}).pop((workspace_id, tab_id), None)
        if activity is None:
            return None
        try:
            return await activity.settle(cap=8.0, from_now=True)
        finally:
            activity.stop()

    def _invalidate_observation(
        self, workspace_id: str, tab_id: str, *, reason: str = "invalidated",
    ) -> None:
        key = (workspace_id, tab_id)
        if self._observation_cache.pop(key, None) is not None:
            self.__dict__.setdefault("_observation_cache_miss_reasons", {})[key] = reason
        watcher = self.__dict__.get("_observation_cache_watchers", {}).pop(key, None)
        if watcher is not None:
            page, handler = watcher
            try:
                page.remove_listener("framenavigated", handler)
            except Exception:
                pass

    def _record_observation_cache_event(
        self, tab_id: str, status: str, *, age_ms: int | None,
        total_ms: int, collect_ms: int,
    ) -> None:
        """Log cache timing without page text, URL, form values, or prompt churn."""
        counts = self.__dict__.setdefault("_observation_cache_counts", {})
        counts[status] = counts.get(status, 0) + 1
        log.info(
            "browser observation tab=%s cache=%s age_ms=%s total_ms=%s collect_ms=%s",
            tab_id, status, age_ms, total_ms, collect_ms,
        )

    def _project_observation(
        self, record, tab, snapshot: dict, *, text: str = "", view: dict | None = None,
    ) -> dict:
        """Common presentation boundary for explicit and post-action snapshots."""
        projected = _project_snapshot(
            {**snapshot, "workspace_id": record.workspace_id, "tab_id": tab.tab_id},
            text=text, view=view,
        )
        # Shared control: which human-input generation this snapshot saw. An
        # agent action compares its target's revision against the tab's current
        # one to prove its snapshot is still fresh before overwriting state.
        projected["human_input_revision"] = int(
            getattr(tab, "human_input_revision", 0) or 0
        )
        # Region ids (r…) are named in observations only; remember them so a
        # design read can scope to "that form" by the id the model was shown.
        if projected.get("regions"):
            tables = self.__dict__.setdefault("_region_tables", {})
            table = tables.setdefault((record.workspace_id, tab.tab_id), {})
            table.update(projected["regions"])
            while len(table) > 400:
                table.pop(next(iter(table)))
        return projected

    @staticmethod
    async def _descendant_refs(
        page: Any, ref: str, *, workspace_id: str = "", tab_id: str = "",
    ) -> list[str]:
        """Refs inside the element ``ref`` names, including itself. Bounded, read-only.

        The DOM marker carries the element's node token, not the opaque ref;
        the ref the model holds may come from an earlier generation. Translate
        ref -> token through the registry's retained identity, read containment
        from the live document, and translate the tokens found back to this
        generation's refs.
        """
        from ascended_browser._app.browser_click_helpers import current_ref_candidates, frame_for_ref, ref_identity

        identity = ref_identity(ref, workspace_id=workspace_id or None, tab_id=tab_id or None) or {}
        token = str(identity.get("node_token") or "")
        if not token:
            return []
        try:
            root = frame_for_ref(page, ref, workspace_id=workspace_id or None, tab_id=tab_id or None)
        except Exception:
            root = page
        try:
            found = await _bounded(root.evaluate(
                "(token) => {" + SHADOW_DOM_JS + """
                  const root = odyQueryAll(document, `[data-odysseus-browser-ref="${CSS.escape(token)}"]`)[0];
                  if (!root) return [];
                  const out = [token];
                  odyQueryAll(root, '[data-odysseus-browser-ref]').forEach(el => {
                    out.push(el.getAttribute('data-odysseus-browser-ref'));
                  });
                  return out.slice(0, 2000);
                }""", token), 3.0)
        except Exception:
            return []
        by_token = {
            str(candidate.get("node_token") or ""): str(candidate.get("ref") or "")
            for candidate in current_ref_candidates(workspace_id=workspace_id, tab_id=tab_id)
            if candidate.get("node_token")
        }
        refs = [by_token[str(item)] for item in found or [] if str(item) in by_token]
        return list(dict.fromkeys([ref, *refs]))

    @staticmethod
    async def _page_headings(page: Any) -> list[dict]:
        """The page's heading outline — what page scripts most often went looking for."""
        try:
            found = await _bounded(page.evaluate(
                "() => {" + SHADOW_DOM_JS + r""" return odyQueryAll(document, 'h1,h2,h3,[role="heading"]')
                  .filter(el => el.getClientRects().length)
                  .map(el => ({
                    level: Number(el.getAttribute('aria-level')) || Number((el.tagName.match(/^H(\d)$/) || [])[1]) || 2,
                    text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 100),
                  }))
                  .filter(h => h.text)
                  .slice(0, 40); }"""), 3.0)
        except Exception:
            return []
        return [h for h in found or [] if isinstance(h, dict)]

    def _cache_observation(
        self, workspace_id: str, tab: TabRecord, page: Any, snapshot: dict[str, Any],
    ) -> None:
        key = (workspace_id, tab.tab_id)
        self._invalidate_observation(workspace_id, tab.tab_id)
        self.__dict__.setdefault("_observation_cache_miss_reasons", {}).pop(key, None)
        self._observation_cache[key] = (
            time.monotonic(), str(getattr(page, "url", "") or tab.url), dict(snapshot),
        )
        # What the model was last shown of this tab, whole. Survives the cache
        # invalidation every mutation does, so an action's result can be a delta.
        self.__dict__.setdefault("_last_shown_projection", {})[key] = dict(snapshot)
        # Which URL the model last saw on this tab. Survives cache
        # invalidation (every mutation invalidates), so an action can say
        # "unchanged since what you saw" only about a page that was shown.
        self.__dict__.setdefault("_last_observed_url", {})[key] = str(getattr(page, "url", "") or tab.url)
        # A reload or iframe navigation can keep the same page URL and arrive
        # within the TTL. A frame navigation retires the affected DOM refs;
        # invalidating the whole small projection costs no warm-hit round trip.
        if callable(getattr(page, "on", None)):
            def on_navigated(frame: Any) -> None:
                try:
                    reason = "document_changed" if frame is page.main_frame else "frame_changed"
                    self._invalidate_observation(workspace_id, tab.tab_id, reason=reason)
                except Exception:
                    pass

            try:
                page.on("framenavigated", on_navigated)
            except Exception:
                pass
            else:
                self.__dict__.setdefault("_observation_cache_watchers", {})[key] = (
                    page, on_navigated,
                )

    def _cached_observation(
        self, workspace_id: str, tab: TabRecord, page: Any, *, with_reason: bool = False,
    ) -> dict[str, Any] | None | tuple[dict[str, Any] | None, str]:
        cached = self._observation_cache.get((workspace_id, tab.tab_id))
        if cached is None:
            reason = self.__dict__.setdefault("_observation_cache_miss_reasons", {}).pop(
                (workspace_id, tab.tab_id), "cold_miss",
            )
            return (None, reason) if with_reason else None
        at, url, snapshot = cached
        ttl = max(0.0, min(10.0, float(
            get_setting("browser_workspace_observation_cache_seconds", 3) or 0
        )))
        current_url = str(getattr(page, "url", "") or tab.url)
        reason = (
            "disabled" if ttl <= 0 else
            "url_changed" if current_url != url else
            "ttl_expired" if time.monotonic() - at > ttl else ""
        )
        if reason:
            self._invalidate_observation(workspace_id, tab.tab_id)
            return (None, reason) if with_reason else None
        result = {**snapshot, "cached": True}
        return (result, "hit") if with_reason else result

    def _attach_blocker(
        self, record: WorkspaceRecord, tab: TabRecord, result: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach page evidence without making a run-control decision.

        Authentication pages are exposed through ``auth_state``. Challenges
        are exposed through ``blocker``. Neither creates attention, transfers
        control, or suspends work here: the model decides whether the user's
        goal calls for another route, a direct explanation, or an explicit
        question. Durable state records what happened; it does not choose what
        the assistant should do about it.
        """
        notice = self._pending_recovery_notices.pop(
            (record.workspace_id, tab.tab_id), None,
        )
        if notice is not None and "lost_page_state" not in result:
            result = {**result, "lost_page_state": notice}
        if notice is not None and tab.draft_discarded_at:
            # Delivered: the durable marker has done its job.
            tab.draft_discarded_at = 0.0
            self.store.save(record)
        # What the page reported since the last result (console errors,
        # uncaught exceptions, failed requests), as a delta: evidence beside
        # auth_state, not a verdict.
        result = self._attach_diagnostics_summary(record, tab, result)
        snapshot = result.get("page") if isinstance(result.get("page"), dict) else result
        try:
            from ascended_browser._app.browser_reliability import classify_blocker
            blocker = classify_blocker(snapshot)
        except Exception:
            log.debug("Browser blocker classification failed", exc_info=True)
            blocker = None
        if not blocker:
            return result
        kind = str(blocker.get("type") or "challenge")
        reason = str(blocker.get("reason") or f"Browser {kind} requires the user")
        return {
            **result,
            "blocked": True,
            "kind": kind,
            "tab_id": tab.tab_id,
            "reason": reason,
            "blocker": blocker,
        }

    async def _reresolve_by_label(
        self, record: Any, tab: Any, page: Any, label: str, *, lock_held: bool = False,
        identity: dict | None = None,
    ) -> str:
        """Re-observe and find the element the caller named, or give up.

        This is Stagehand's self-heal (`actHandler.ts`, `selfHeal`) adapted to
        an architecture with no inference step: Stagehand re-runs the LLM to
        pick an element again, whereas the caller here already told us what it
        was aiming at, so the visible label plays that role.

        Deliberately refuses an ambiguous match. A page with eight "Try it"
        buttons must not have one picked for it — silently acting on a
        different element than intended is far worse than one failed step.
        """
        from ascended_browser._app.browser_click_helpers import build_enhanced_snapshot

        # The caller's label, or else the name the stale ref was observed with
        # — kept in the registry for exactly this. Without it a model that
        # sends only a ref (the common case) was told the browser would
        # re-resolve its target, and then it did not.
        identity = identity if isinstance(identity, dict) else {}
        wanted_role = str(identity.get("role") or "").strip().casefold()
        # An author id survives a page rebuild (a tab restored from sleep
        # reloads it, so every node and every ref is new): three radios all
        # named "Radio button" are still radio-button-1, -2 and -3.
        wanted_id = str(identity.get("id") or "").strip()
        # The name the ref was observed with is the element's own, so it
        # decides, as in agent-browser (re-query by the ref's recorded
        # role+name) and Stagehand (re-infer from the recorded description).
        # The caller's label is a paraphrase ("Next button", "Submit Profile
        # (real form button)") that matched nothing in live runs; it is used
        # only for a ref whose identity is not retained, never to pick some
        # other element when the observed name is ambiguous.
        names = {
            _state_free_name(identity.get(field))
            for field in ("label", "aria_label", "text", "placeholder")
        } - {""}
        if not names:
            # A control with no name of its own: the text beside it.
            names = {_state_free_name(identity.get("nearby"))} - {""}
        if not names:
            names = {_state_free_name(label)} - {""}
        wanted_type = str(identity.get("type") or "").strip().casefold()
        if not names and not wanted_id and not wanted_type:
            return ""
        async def collect():
            return await asyncio.wait_for(
                build_enhanced_snapshot(
                    page, workspace_id=record.workspace_id, tab_id=tab.tab_id,
                ),
                timeout=float(get_setting("browser_workspace_observe_timeout_seconds", 90) or 90),
            )
        if lock_held:
            snapshot = await collect()
        else:
            async with self._action_lock(record.workspace_id, tab.tab_id):
                snapshot = await collect()
        tab.last_observation_id = str(
            snapshot.get("observation_id") or (snapshot.get("elements") or [{}])[0].get("observation_id") or ""
        )
        # Only on the page the ref was observed on: after a navigation, a
        # same-id or same-named element elsewhere is a different element.
        observed_on = _page_key(identity.get("frame_url")) if int(identity.get("frame_index") or 0) == 0 else ""
        if observed_on and observed_on != _page_key(getattr(page, "url", "")):
            return ""
        visible = [element for element in (snapshot.get("elements") or []) if element.get("visible")]
        if wanted_type == "file":
            # A file input is almost always hidden behind a styled button, so
            # a stale one heals to a file input, hidden or not, and to nothing
            # else. Matching visible elements only refused Jobvite's labelled,
            # hidden "file-input-0" on four tabs at once (session 722b3c33).
            visible = [
                element for element in (snapshot.get("elements") or [])
                if str(element.get("type") or "").strip().casefold() == "file"
            ]
        same_role = lambda element: not wanted_role or str(element.get("role") or "").strip().casefold() == wanted_role
        if wanted_id:
            by_id = [e for e in visible if str(e.get("id") or "").strip() == wanted_id and same_role(e)]
            if len(by_id) == 1:
                return str(by_id[0].get("ref") or "")
        if not names:
            # Nothing names it (Formy's hidden file input: no id, name or
            # label): on the same page, the only element of its kind is it.
            # File inputs are usually hidden behind a styled button.
            pool = (snapshot.get("elements") or []) if wanted_type == "file" else visible
            of_kind = [e for e in pool if same_role(e)
                       and str(e.get("type") or "").strip().casefold() == wanted_type]
            return str(of_kind[0].get("ref") or "") if wanted_type and len(of_kind) == 1 else ""
        matches = [
            element for element in visible
            if any(
                _state_free_name(element.get(field)) in names
                for field in ("text", "label", "aria_label", "placeholder", "nearby")
            ) and same_role(element)
        ]
        if len(matches) > 1 and identity.get("nearby"):
            # Same name, told apart by the text beside each.
            wanted_nearby = _state_free_name(identity.get("nearby"))
            matches = [e for e in matches if _state_free_name(e.get("nearby")) == wanted_nearby]
        if len(matches) != 1:
            return ""
        return str(matches[0].get("ref") or "")

    async def _recover_form_fields(self, record: Any, tab: Any, page: Any, action: dict) -> dict:
        """Resolve missing batch targets once, before any field is written."""
        from ascended_browser._app.browser_click_helpers import (
            StaleRefError, build_enhanced_snapshot, parse_ref, ref_identity,
        )

        fields = [dict(entry) if isinstance(entry, dict) else entry
                  for entry in action.get("fields") or []]
        missing: list[tuple[dict, set[str]]] = []
        for entry in fields:
            if not isinstance(entry, dict):
                continue
            try:
                parse_ref(str(entry.get("ref") or ""), workspace_id=record.workspace_id,
                          tab_id=tab.tab_id,
                          observation_id=entry.get("observation_id") or action.get("observation_id"),
                          allow_selector=False)
            except StaleRefError:
                # The name the ref was observed with decides; the caller's
                # label, which drops the required marker or paraphrases
                # ("ZIP" for "Zip/Postal Code *"), only stands in when no
                # identity is retained. See _reresolve_by_label.
                identity = ref_identity(
                    str(entry.get("ref") or ""), workspace_id=record.workspace_id, tab_id=tab.tab_id,
                ) or {}
                wanted = {_state_free_name(identity.get(key))
                          for key in ("label", "aria_label", "placeholder")} - {""}
                wanted = wanted or {_state_free_name(entry.get("label"))} - {""}
                if wanted:
                    missing.append((entry, wanted))
        if not missing:
            return action
        snapshot = await asyncio.wait_for(
            build_enhanced_snapshot(page, workspace_id=record.workspace_id, tab_id=tab.tab_id),
            timeout=float(get_setting("browser_workspace_observe_timeout_seconds", 90) or 90),
        )
        elements = snapshot.get("elements") or []
        tab.last_observation_id = str((elements or [{}])[0].get("observation_id") or "")
        fields_on_page = [element for element in elements
                          if element.get("visible")
                          and element.get("tag") in {"input", "textarea", "select"}
                          and element.get("type") not in {"file", "submit", "button", "hidden"}]
        for entry, wanted in missing:
            matches = [element for element in fields_on_page
                       if any(_state_free_name(element.get(key)) in wanted
                              for key in ("label", "aria_label", "placeholder"))]
            if len(matches) == 1:
                entry.update(ref=matches[0]["ref"], observation_id=tab.last_observation_id,
                             self_healed=True)
        return {**action, "fields": fields}

    async def _refresh_pending_form_fields(
        self,
        record: Any,
        tab: Any,
        page: Any,
        entries: list[dict[str, Any]],
        *,
        start_index: int,
    ) -> None:
        """Rebase pending targets after a composite control changes the DOM.

        React-style selects commonly replace sibling input nodes when a value
        commits.  Continuing with every ref from the initial observation makes
        the final field fail even though its semantic identity is unchanged.
        Refresh once inside the executor boundary and conservatively rebase
        only uniquely matched pending controls; ambiguous entries remain stale
        and fail closed through the normal recovery path.
        """
        if start_index >= len(entries):
            return
        from ascended_browser._app.browser_click_helpers import build_enhanced_snapshot
        from ascended_browser._app.browser_form_runtime import rebase_pending_field

        snapshot = await build_enhanced_snapshot(
            page,
            workspace_id=record.workspace_id,
            tab_id=tab.tab_id,
        )
        observation_id = str(snapshot.get("observation_id") or "")
        if observation_id:
            tab.last_observation_id = observation_id
        for pending_index in range(start_index, len(entries)):
            pending = entries[pending_index]
            if not isinstance(pending, dict):
                continue
            rebased, _diagnostic = rebase_pending_field(pending, snapshot)
            entries[pending_index] = rebased

    async def _page_needs_reread(
        self, record: Any, tab: Any, page: Any, kind: str, state_before: str | None, result: dict,
    ) -> bool:
        """Should an action's result carry a fresh page, or say it is unchanged?

        Of 69 act→observe pairs in the 2026-10 sessions, 51 re-read a page
        whose URL had not changed and 49 followed an act that carried no page
        at all (fill, check, upload). The URL-plus-visible-text signature taken
        before the action says whether anything the model can see moved: typed
        values are not innerText, so a fill leaves it alone, while a validation
        message, a revealed section or a navigation changes it. A composed
        failure (stalled scroll, unmet wait) and an action the caller asked to
        read always get the page; so does a tab the model has no observation of.
        """
        if result.get("success") is False or kind in _RESTATE_ALWAYS or _read_budget():
            return True
        state_after = await self._click_page_state(page)
        if state_before is None or state_after is None or state_after != state_before:
            return True
        seen_url = self.__dict__.get("_last_observed_url", {}).get((record.workspace_id, tab.tab_id))
        current_url = str(getattr(page, "url", "") or tab.url)
        if not seen_url or seen_url != current_url:
            return True
        result["page_unchanged"] = True
        result["page_note"] = (
            "Page unchanged: same URL and visible text as your last observation, so its "
            "refs still apply. Observe again only for a different view."
        )
        return False

    async def _observe_for_result(
        self, record: Any, tab: Any, page: Any, *, lock_held: bool = False,
    ) -> dict:
        """A fresh projected snapshot to attach to an action's result.

        Best-effort: an action that worked must not be reported as failed just
        because the follow-up look at the page did not come back in time.
        """
        from ascended_browser._app.browser_click_helpers import build_enhanced_snapshot

        try:
            async def _snapshot():
                return await asyncio.wait_for(
                    build_enhanced_snapshot(
                        page, workspace_id=record.workspace_id, tab_id=tab.tab_id,
                    ),
                    timeout=float(get_setting("browser_workspace_observe_timeout_seconds", 90) or 90),
                )

            if lock_held:
                snapshot = await _snapshot()
            else:
                async with self._action_lock(record.workspace_id, tab.tab_id):
                    snapshot = await _snapshot()
            tab.last_observation_id = str(
                snapshot.get("observation_id") or (snapshot.get("elements") or [{}])[0].get("observation_id") or ""
            )
            self.store.save(record)
            text = await self._page_digest(page)
            projected = self._project_observation(record, tab, snapshot, text=text)
            projected = self._attach_login_available(record.owner, projected)
            if _read_budget():
                # The call asked to read this page: its text is the answer, and
                # the presentation budgets it as such rather than as the
                # 1,200-char digest every observation carries. The cache serves
                # later plain observations, so it keeps the ordinary digest.
                digest = int(get_setting("browser_workspace_observe_text_chars", 1200) or 1200)
                self._cache_observation(
                    record.workspace_id, tab, page, {**projected, "text": text[:max(0, digest)]},
                )
                return {**projected, "text_read": True}
            previous = self.__dict__.get("_last_shown_projection", {}).get((record.workspace_id, tab.tab_id))
            self._cache_observation(record.workspace_id, tab, page, projected)
            delta = (
                _delta_projection(previous, projected)
                if get_setting("browser_act_result_delta", True) is not False else None
            )
            return delta if delta is not None else projected
        except Exception as exc:
            return {"unavailable": f"{type(exc).__name__}: {str(exc)[:160]}"}

    async def _page_digest(self, page: Any) -> str:
        """A short readable summary of the page, folded into every observe.

        Every turn of a real browsing session opened with
        observe → extract → evaluate, because nothing said which of the three
        answered the question. Carrying a digest here means `extract` is only
        needed when the full text genuinely is the answer.

        An action that asked to read the page it lands on raises the budget for
        that one call: browsing was a click and then an extract, twice per page
        visited, and the click already had the page open in front of it.
        """
        limit = _read_budget() or int(
            get_setting("browser_workspace_observe_text_chars", 1200) or 1200
        )
        if limit <= 0:
            return ""
        try:
            text = str(await _bounded(deep_inner_text(page, timeout=4.0), 5.0) or "")
        except Exception:
            text = ""
        if not text.strip():
            # A frameset has no body of its own, so the page read as empty:
            # six frames observed, nothing shown, and the run fell back to
            # browser_evaluate to find out what was on screen.
            text = await self._frame_digest(page)
        collapsed = re.sub(r"\n{2,}", "\n", re.sub(r"[ \t]+", " ", str(text))).strip()
        return collapsed[:limit]

    @staticmethod
    async def _frame_digest(page: Any) -> str:
        """What a document without a body of its own is showing, by frame."""
        parts: list[str] = []
        for frame in list(getattr(page, "frames", []) or [])[1:12]:
            try:
                body = str(await _bounded(
                    deep_inner_text(frame, timeout=1.5), 2.0,
                ) or "").strip()
            except Exception:
                continue
            if not body:
                continue
            name = str(getattr(frame, "name", "") or getattr(frame, "url", "") or "")[:80]
            parts.append(f"[frame {name}] {body}" if name else body)
        return "\n".join(parts)

    def _assert_may_mutate(self, tab, actor: str, actor_id: str, what: str) -> None:
        """A tab may be mutated by its own lease holder, and by nobody else.

        Comparing against the literal "parent" locked a browser sub-agent out of
        the very tab it had just opened: `open` leases the tab to the worker, so
        every subsequent act/login from that same worker read as an unauthorized
        parent mutation.

        Shared control: a person holding the native-input lease on this agent's
        tab never fences that agent's page actions — the lease records who
        drives native input, not page authority. Only the displaced agent
        continues; another actor's lease stays a hard refusal.
        """
        lease = getattr(tab, "lease", None)
        if not lease:
            return
        if (lease.actor, lease.actor_id) == (actor, actor_id):
            return
        if (
            lease.actor == "user"
            and actor in {"parent", "worker"}
            and str(tab.takeover_resume_actor or "") == actor
            and str(tab.takeover_resume_actor_id or "") == (actor_id or actor)
        ):
            return
        raise WorkspaceError(
            f"Tab is currently leased to {lease.actor}; {actor} {what} is not authorized"
        )

    async def _check_target_identity(
        self, *, record: Any, tab_id: str, kind: str, action: dict, locator: Any,
    ) -> None:
        """Refuse a click/key on a ref whose control turned into a final submit.

        On Workday one button read "Save and Continue" and later "Submit", and
        the model pressed Enter on that ref three more times while stepping the
        wizard (2026-10-04). A ref is the element as it was observed; when its
        live name now reads as a final submission and did not then, nothing is
        sent and the model is told to look again. A refusal, never a pause.
        """
        from ascended_browser._app.browser_submit_gate import TARGET_SCRIPT, activating_key, turned_submit
        from ascended_browser._app.browser_click_helpers import ref_identity

        ref = str(action.get("ref") or "").strip()
        if not ref or kind not in {"click", "press"}:
            return
        if kind == "press" and not activating_key(action.get("key")):
            return
        try:
            live = await _bounded(locator.evaluate(TARGET_SCRIPT), 2.0)
        except Exception:
            return
        if not isinstance(live, dict):
            return
        observed = ref_identity(
            ref, workspace_id=record.workspace_id, tab_id=tab_id,
            observation_id=action.get("observation_id"),
        )
        before = turned_submit(observed, str(live.get("name") or ""))
        if before:
            raise _untouched(WorkspaceError(
                f"{ref} now reads \"{live.get('name')}\" (it read \"{before}\" when observed); "
                "nothing was sent. Observe the page before acting on it."
            ))

    async def act(self, owner: str, session_id: str, tab_id: str, action: dict,
                  *, actor: str = "parent", actor_id: str = "parent",
                  parallel_tab: bool = False) -> dict:
        from ascended_browser._app.browser_deadline import browser_deadline
        from ascended_browser._app.browser_execution import BrowserAttempt, CURRENT_ATTEMPT

        workspace_id = self.workspace_id(owner, session_id)
        attempt = BrowserAttempt(
            lambda event: self.store.append_journal(workspace_id, event),
            str(normalize_browser_action(action).get("kind") or ""), tab_id,
        )
        attempt.event("browser_action_attempt", dispatch_status="not_dispatched")
        token = CURRENT_ATTEMPT.set(attempt)
        # An action that says it is going to read the page it lands on gets the
        # page's text back with its own result, instead of the click-then-
        # extract pair that every page visit used to cost.
        read_token = _READ_TEXT_CHARS.set(
            read_text_budget(action.get("read")) or None
        )
        try:
            async with browser_deadline(action_budget_seconds(action)):
                async with self._pin_tab(workspace_id, tab_id):
                    result = await self._act(
                        owner, session_id, tab_id, action, actor=actor, actor_id=actor_id,
                        parallel_tab=parallel_tab,
                    )
            receipt = attempt.receipt(result.get("receipt"), failed=result.get("success") is False)
            attempt.event("browser_action_outcome", receipt=receipt)
            return {**result, "receipt": receipt}
        except BaseException as exc:
            receipt = attempt.receipt(failed=True)
            record = self._records.get(workspace_id)
            if record is not None and attempt.dispatch_possible:
                self.store.save(record)
            attempt.event("browser_action_outcome", receipt=receipt, error_type=type(exc).__name__)
            # Preserve existing exception types for authority/stale-ref callers,
            # while giving the facade and tool boundary truthful effect data.
            exc.browser_receipt = receipt
            raise
        finally:
            _READ_TEXT_CHARS.reset(read_token)
            CURRENT_ATTEMPT.reset(token)

    async def _act(self, owner: str, session_id: str, tab_id: str, action: dict,
                   *, actor: str = "parent", actor_id: str = "parent",
                   parallel_tab: bool = False) -> dict:
        # StaleRefError is imported here, not inside a branch: a conditional
        # import makes the name local to the whole function, so fill_form's
        # stale-field handler raised UnboundLocalError instead of healing.
        from ascended_browser._app.browser_click_helpers import (
            StaleRefError, click_with_fallbacks, invalidate_refs, parse_ref,
            ref_identity, resolve_live_target,
        )
        from ascended_browser._app.browser_execution import action_stage, before_dispatch
        record = await self.ensure_awake(owner, session_id)
        tab, page = await self._get_tab(record, tab_id)
        # Never act on a page the previous action left mid-load.
        await self._finish_loading(record.workspace_id, tab_id)
        page_state_before: str | None = None
        if (record.workspace_id, tab_id) in self._pending_recovery_notices:
            raise WorkspaceError(
                "Browser tab was reloaded after its page state was lost. Observe the fresh "
                "page before acting; prior element refs, fields, and uploads were discarded."
            )
        self._assert_may_mutate(tab, actor, actor_id, "mutation")
        action = normalize_browser_action(action)
        # Shared control: which human-input generation the model's last
        # observation saw. A newer tab revision proves the person changed the
        # page after that snapshot, so state-writing actions yield to their
        # edits instead of overwriting them (cua's snapshot-before invariant).
        # fill_form batches carry "_model_observed_revision" captured before
        # the wrapper's auto-observe refreshes the cache; other kinds read the
        # cached projection directly.
        cached = self._observation_cache.get((record.workspace_id, tab_id))
        cached_projection = cached[2] if isinstance(cached, tuple) and len(cached) == 3 else None
        if isinstance(action.get("_model_observed_revision"), int):
            observed_human_revision = action["_model_observed_revision"]
        elif isinstance(cached_projection, dict):
            observed_human_revision = int(
                cached_projection.get("human_input_revision") or 0
            )
        else:
            observed_human_revision = -1
        # -1 means the snapshot's freshness is unknowable (scoped view, evicted
        # cache); yielding only triggers when a newer human input is provable.
        yield_to_human = 0 <= observed_human_revision < int(
            tab.human_input_revision or 0
        )
        human_revision_at_entry = int(tab.human_input_revision or 0)
        if isinstance(action.get("_model_observed_controls"), dict):
            observed_controls = action["_model_observed_controls"]
        else:
            observed_controls = _observed_control_states(cached_projection)
        self_healed = False
        scroll_evidence: dict | None = None
        click_evidence = None
        navigation_state: dict[str, Any] | None = None
        kind = str(action.get("kind") or "")
        if not kind and str(action.get("ref") or "").strip():
            identity = ref_identity(
                str(action.get("ref") or ""), workspace_id=record.workspace_id,
                tab_id=tab_id,
                observation_id=action.get("observation_id") or tab.last_observation_id,
            )
            kind = _infer_missing_kind_from_identity(action, identity)
            if kind:
                action["kind"] = kind
        if kind not in _SUPPORTED_ACTION_KINDS:
            # date and sequence are dispatched by the manager before this
            # point; name them too, or the model learns they do not exist.
            supported = ", ".join(sorted(_SUPPORTED_ACTION_KINDS | {"date", "sequence"}))
            raise WorkspaceError(
                f"Unsupported browser action: {kind or '(missing)'}. "
                f"Use action.kind one of: {supported}."
            )
        if kind not in _RESTATE_ALWAYS:
            # URL plus visible text before the action; compared at the tail so
            # the result can say truthfully whether the page moved. Kinds that
            # always carry the page (scroll, wait, navigate) skip the read.
            page_state_before = await self._click_page_state(page)
        from ascended_browser._app.browser_wait_conditions import describe_conditions, normalize_until

        try:
            until_conditions = normalize_until(action.get("until"), kind=kind)
        except ValueError as exc:
            raise _untouched(WorkspaceError(f"{exc} Nothing was dispatched.")) from None
        effect_barrier = self._runtime_effect_barrier(record.owner)
        action_stage("resolve")
        # Resolve the ref before taking the writer lock, so a ref that no longer
        # resolves can be healed by re-observing — which needs that same
        # non-reentrant lock and would deadlock from inside. This is Stagehand's
        # self-heal (actHandler.ts `selfHeal`) minus its inference step: it
        # re-asks an LLM which element to use, while the caller here already
        # named one, so the visible label stands in for the instruction.
        if kind in {"click", "fill", "select", "check", "upload"} and get_setting(
            "browser_workspace_act_self_heal", True
        ):
            try:
                parse_ref(
                    str(action.get("ref") or ""), workspace_id=record.workspace_id,
                    tab_id=tab_id, observation_id=action.get("observation_id"),
                    allow_selector=False,
                )
            except StaleRefError:
                current = None
                if action.get("observation_id"):
                    # A ref names one DOM node for the life of the tab, so a
                    # ref live in the latest observation is that node whatever
                    # generation the caller attached (models paired refs with
                    # the wrong, or an invented, observation_id).
                    try:
                        current = parse_ref(
                            str(action.get("ref") or ""), workspace_id=record.workspace_id,
                            tab_id=tab_id, allow_selector=False,
                        )
                    except StaleRefError:
                        current = None
                if current:
                    # Resolve against the latest generation, as a ref sent
                    # without an observation_id would be. Not a heal: the
                    # element is the one the caller named.
                    action = {k: v for k, v in action.items() if k != "observation_id"}
                else:
                    async with effect_barrier.shared():
                        # A queued takeover/resize gets writer priority. Recheck
                        # before publishing a healed ref derived from page geometry.
                        self._assert_may_mutate(tab, actor, actor_id, "mutation")
                        recovered = await self._reresolve_by_label(
                            record, tab, page, str(action.get("label") or ""),
                            identity=ref_identity(
                                str(action.get("ref") or ""), workspace_id=record.workspace_id,
                                tab_id=tab_id,
                            ),
                        )
                    if not recovered:
                        raise  # unknown ref and no unambiguous name — say so
                    action = {
                        **action, "ref": recovered,
                        "observation_id": tab.last_observation_id,
                    }
                    self_healed = True

        target_url = str(action.get("url") or tab.url)
        domain = self._domain(target_url)
        sign_in = _can_change_sign_in(kind, action)
        domain_lock = (
            self._domain_locks.setdefault((owner_key(record.owner), domain), asyncio.Lock())
            if sign_in else nullcontext()
        )
        # Throttling waits happen BEFORE any writer lock is taken, so one
        # rate-limited domain does not hold the owner's context lock while idle.
        release_domain_slots = None
        detail = ""
        field_results: list[dict] = []
        upload_evidence: dict[str, Any] | None = None
        from ascended_browser._app.browser_deadline import credit_queue_wait

        if kind in {"navigate", "click"}:
            action_stage("queue")
            throttle_started = time.monotonic()
            await self._rate_limit_domain(domain, record.owner)
            release_domain_slots = await self._acquire_domain_slots(domain, record.owner)
            credit_queue_wait(time.monotonic() - throttle_started)
        lock_wait = 0.0
        effect_started: float | None = None
        try:
            self._invalidate_observation(record.workspace_id, tab_id)
            owner_slots = self._owner_action_slots.setdefault(
                self._owner_scope(record.workspace_id),
                asyncio.Semaphore(max(1, min(4, int(
                    get_setting("browser_parallel_tab_concurrency", 3) or 3
                )))),
            )
            action_lock = (
                self._tab_action_lock(record.workspace_id, tab_id)
                if parallel_tab
                else self._action_lock(record.workspace_id, tab_id)
            )
            lock_wait_started = time.monotonic()
            action_stage("queue")
            # Queue for the owner's slots and the site's sign-in lock *before*
            # joining the effect barrier: an action waiting behind other tabs
            # on the same site held the shared barrier the whole time, and the
            # user's take-control (exclusive barrier) timed out at 30 s behind
            # a select that had not started (2026-10-04). The tab lock stays
            # after the barrier, the same order the live-view transition uses.
            async with owner_slots, domain_lock, effect_barrier.shared("act"), action_lock:
                lock_wait = time.monotonic() - lock_wait_started
                # Time spent behind other tabs is not this action's work.
                credit_queue_wait(lock_wait)
                effect_started = time.monotonic()
                # Takeover waits on this same lock. Re-check after acquiring it
                # so an agent that queued just before takeover cannot mutate
                # the page using the lease it observed before it waited.
                self._assert_may_mutate(tab, actor, actor_id, "mutation")
                expectation_before = action.get("expect_before")
                if isinstance(expectation_before, dict) and expectation_before:
                    action_stage("resolve")
                    if not await self._click_expectation_met(page, expectation_before):
                        raise WorkspaceError(
                            "Browser action precondition was not present on the current page; "
                            "nothing was dispatched."
                        )
                before_auth = await self._auth_signature(page, domain) if sign_in else ""
                self._assert_may_mutate(tab, actor, actor_id, "mutation")
                # From here on, what the page reports belongs to this action.
                self._mark_diagnostics_action(record.workspace_id, tab_id)
                if kind == "navigate":
                    url = str(action.get("url") or "")
                    activity = ActionActivity(page).start()
                    await self._agent_cursor_status(
                        page, _agent_cursor_navigate_caption(url),
                        workspace_id=record.workspace_id, tab_id=tab_id,
                    )
                    before_dispatch()
                    try:
                        await self._navigate_until_runtime_transition(
                            page,
                            page.goto(url, wait_until="domcontentloaded", timeout=60000),
                            effect_barrier,
                            wait_seconds=62,
                        )
                        # domcontentloaded is before a client-rendered app has
                        # fetched what it shows; follow its requests too.
                        navigation_state = await self._settle_activity_after_dispatch(
                            activity, effect_barrier, operation_name="post-navigation activity settle",
                        )
                    finally:
                        self._keep_or_stop_activity(record.workspace_id, tab_id, activity, navigation_state)
                    await self._settle_page_after_dispatch(page, effect_barrier)
                    invalidate_refs(workspace_id=record.workspace_id, tab_id=tab_id)
                    tab.last_method = "GET"
                elif kind in {"click", "fill"}:
                    ref = str(action.get("ref") or "")
                    if kind == "click":
                        action_stage("resolve")
                        click_root, click_selector = await resolve_live_target(
                            page, ref, workspace_id=record.workspace_id, tab_id=tab_id,
                            observation_id=action.get("observation_id"),
                        )
                        await self._check_target_identity(
                            record=record, tab_id=tab_id, kind=kind, action=action,
                            locator=click_root.locator(click_selector).first,
                        )
                        before_click = await self._click_page_state(page)
                        probe_started = await self._start_click_effect_probe(click_root, click_selector)
                        expectation = action.get("expect")
                        expectation_preexisting = bool(
                            await self._click_expectation_met(page, expectation)
                        )
                        network_events, network_listener = self._start_click_network_probe(page)
                        dialog_events, dialog_listener = self._start_click_dialog_probe(page)
                        activity = ActionActivity(page).start()
                        self._assert_may_mutate(tab, actor, actor_id, "mutation")
                        before_dispatch()
                        try:
                            detail, click_method = await click_with_fallbacks(
                                page, ref=ref, label=str(action.get("label") or ref),
                                workspace_id=record.workspace_id, tab_id=tab_id,
                                observation_id=action.get("observation_id"),
                                allow_selector=False,
                            )
                            # A click is the usual way a route change or a lazy panel
                            # renders, so the same race applies as after a navigate.
                            click_evidence = await self._wait_for_click_change(
                                page, before_click, probe_started=probe_started,
                                probe_root=click_root,
                                expectation=None if expectation_preexisting else expectation,
                                network_events=network_events,
                                effect_barrier=effect_barrier,
                                dialog_events=dialog_events,
                            )
                            # The first change is not the end: a sign-in closes
                            # its dialog, then requests and a redirect follow.
                            navigation_state = await self._settle_activity_after_dispatch(
                                activity, effect_barrier, operation_name="post-click activity settle",
                            )
                        finally:
                            self._finish_click_network_probe(page, network_listener)
                            self._finish_click_dialog_probe(page, dialog_listener)
                            self._keep_or_stop_activity(record.workspace_id, tab_id, activity, navigation_state)
                        if expectation_preexisting:
                            click_evidence["postcondition_preexisting"] = True
                        elif click_evidence.get("state") == "postcondition_unmet":
                            # A redirect or async render can land after the
                            # short effect window; ask once more, as an
                            # assertion would, before calling the guard unmet.
                            late, late_state, _waited = await self._await_expectation(
                                page, expectation,
                                budget_s=max(0.0, min(15.0, float(
                                    get_setting("browser_workspace_expectation_seconds", 5.0) or 5.0
                                ))),
                            )
                            if late:
                                click_evidence.update(
                                    state="postcondition_met", ok=True, postcondition=late,
                                    detail="Click dispatched and the requested visible postcondition was observed.",
                                )
                            elif late_state == "unreadable":
                                # Mid-navigation: the question was not answered,
                                # which is not the same as "absent".
                                click_evidence.update(
                                    state="postcondition_unreadable", ok=None,
                                    detail=(
                                        "Click dispatched; the page could not be read to check the "
                                        "requested postcondition. Observe the tab to confirm."
                                    ),
                                )
                        if (
                            click_evidence.get("ok") is not True
                            and click_evidence.get("state") not in {
                                "postcondition_unmet", "postcondition_unreadable", "dialog_dismissed",
                            }
                            and (navigation_state or {}).get("navigated")
                        ):
                            # Nothing changed in the first moments, then the page
                            # navigated: that is the click's effect.
                            click_evidence.update(
                                state="navigated", ok=True,
                                detail="Click dispatched and the page navigated.",
                            )
                        click_evidence["click_method"] = click_method
                        if click_evidence.get("ok") is True:
                            # A framework can emit its first semantic mutation
                            # while a dialog/list is still replacing children.
                            # Qualify the returned ref generation only after the
                            # existing cheap DOM-stability boundary.
                            deferred = await self._settle_page_after_dispatch(page, effect_barrier)
                            if deferred:
                                click_evidence["settlement_deferred"] = True
                    else:
                        action_stage("resolve")
                        root, selector = await resolve_live_target(
                            page, ref, workspace_id=record.workspace_id, tab_id=tab_id,
                            observation_id=action.get("observation_id"),
                        )
                        tab.dirty = True
                        self._assert_may_mutate(tab, actor, actor_id, "mutation")
                        if yield_to_human and await _control_holds_unrequested_value(
                            root, selector, _requested_control_state(action),
                            observed_controls.get(ref),
                        ):
                            raise WorkspaceError(
                                "The person edited this field after your last "
                                "observation and it now holds a different value. "
                                "Nothing was written: re-observe and decide "
                                "whether to keep their edit or overwrite it "
                                "deliberately."
                            )
                        before_dispatch()
                        await self._agent_cursor_move_for_dispatch(
                            page, root, selector,
                            workspace_id=record.workspace_id, tab_id=tab_id,
                            detail=_agent_cursor_control_caption("fill", action),
                            follow=("key",),
                        )
                        detail = await _fill_control(root, selector, action.get("value"))
                elif kind in {"select", "check"}:
                    action_stage("resolve")
                    root, selector = await resolve_live_target(
                        page, str(action.get("ref") or ""), workspace_id=record.workspace_id,
                        tab_id=tab_id, observation_id=action.get("observation_id"),
                    )
                    if kind == "select" and action.get("list_options") is True:
                        # Read before guessing. This opens the control and
                        # reports what it offers without committing anything,
                        # so a filtered combobox does not cost one round trip
                        # per guessed label. Not a mutation: no dirty flag and
                        # no mutation authority check.
                        from ascended_browser._app.browser_semantic_controls import list_options_semantic

                        # root, not page: a control inside an iframe (the
                        # Greenhouse form embedded in Stripe's page) is not in
                        # the top document, and every lookup there waited out
                        # the whole budget.
                        listing = await list_options_semantic(
                            root, selector=selector,
                            candidate={"ref": str(action.get("ref") or ""),
                                       "label": str(action.get("label") or "")},
                            query=action.get("query"),
                        )
                        action_stage("observation")
                        return await self._finish_act(
                            record, tab, page, tab_id=tab_id, kind=kind, domain=domain,
                            before_auth=before_auth, page_state_before=page_state_before,
                            after_auth=await self._auth_signature(page, domain) if sign_in else "",
                            detail=(
                                f"listed {len(listing['options'])} option(s); nothing selected"
                                if listing["options"] else
                                # One next step: a list that will not open on
                                # demand is usually a search box.
                                "opening it showed no list, so nothing was listed; to choose a "
                                "value, use kind 'select' with the option's text — it types the "
                                "text, runs the search and verifies the choice"
                            ),
                            field_results=[], upload_evidence=None,
                            self_healed=False, options_listing=listing,
                        )
                    value = action.get("value")
                    if kind == "check" and isinstance(action.get("checked"), bool):
                        # The boolean is the contract; a stray `value` (the
                        # model sent value="Other" with checked=true) is not.
                        value = action["checked"]
                    elif kind == "check" and value is None:
                        value = True
                    tab.dirty = True
                    self._assert_may_mutate(tab, actor, actor_id, "mutation")
                    if yield_to_human and await _control_holds_unrequested_value(
                        root, selector, _requested_control_state(action),
                        observed_controls.get(str(action.get("ref") or "")),
                    ):
                        raise WorkspaceError(
                            "The person changed this control after your last "
                            "observation and it now holds a different value. "
                            "Nothing was written: re-observe and decide "
                            "whether to keep their choice or overwrite it "
                            "deliberately."
                        )
                    before_dispatch()
                    await self._agent_cursor_move_for_dispatch(
                        page, root, selector,
                        workspace_id=record.workspace_id, tab_id=tab_id,
                        detail=_agent_cursor_control_caption(kind, {**action, "value": value}),
                        follow=("pointer_down", "pointer_up"),
                    )
                    if kind == "select":
                        options = action.get("options")
                        if isinstance(options, list):
                            detail, select_evidence = await _select_multiple_control(
                                root, selector, options,
                            )
                            from ascended_browser._app.browser_live_verification import publish_select_evidence
                            publish_select_evidence(select_evidence)
                        else:
                            detail = await _select_control(
                                root, selector, value,
                                query=action.get("query"), option=action.get("option"),
                            )
                    else:
                        detail = await _fill_control(root, selector, value)
                    tab.dirty = True
                elif kind == "fill_form":
                    if get_setting("browser_workspace_act_self_heal", True):
                        action = await self._recover_form_fields(record, tab, page, action)
                    # One round trip for a whole form. Each field is reported
                    # separately so a single bad ref does not discard the rest,
                    # and so the model learns exactly which field to retry.
                    observation_id = action.get("observation_id")
                    entries = [entry for entry in (action.get("fields") or []) if isinstance(entry, dict)]
                    progress = action.get("_batch_progress")
                    field_states: dict[str, str] = {}
                    final_probes: list[tuple[int, dict[str, Any]]] = []
                    dispatched_targets: set[tuple[Any, ...]] = set()
                    for entry_index, entry in enumerate(entries):
                        if not isinstance(entry, dict):
                            continue
                        # An entire batch is one model call, not one
                        # uninterruptible ownership of the native window. Once
                        # a user selection/takeover queues, preserve every
                        # completed field and report the rest as untouched so
                        # the action can release the barrier at this safe
                        # between-field boundary.
                        if effect_barrier.transition_pending:
                            for pending_index, pending_entry in enumerate(
                                entries[entry_index:], start=entry_index,
                            ):
                                pending_ref = str(pending_entry.get("ref") or "")
                                field_results.append({
                                    "id": str(
                                        pending_entry.get("id") or pending_entry.get("label")
                                        or f"field-{pending_index + 1}"
                                    ).strip()[:120],
                                    "ref": pending_ref,
                                    "label": str(pending_entry.get("label") or "")[:120],
                                    "kind": str(
                                        pending_entry.get("kind")
                                        or pending_entry.get("type") or "fill"
                                    )[:24],
                                    "status": "unattempted",
                                    "verified": False,
                                    "effect_state": "not_dispatched",
                                    "dispatch_status": "not_dispatched",
                                    "retry_safe": True,
                                    "error": "Not attempted because browser control or tab selection was pending.",
                                    "failure_reason": "transition_pending",
                                    "timing_ms": {"total": 0},
                                })
                            if isinstance(progress, dict):
                                progress.update(
                                    fields=list(field_results), attempted=entry_index,
                                    remaining=len(entries) - entry_index,
                                )
                            break
                        field_started = time.monotonic()
                        field_id = str(
                            entry.get("id") or entry.get("label") or f"field-{entry_index + 1}"
                        ).strip()[:120]
                        raw_dependencies = entry.get("depends_on")
                        dependencies = [
                            str(value).strip()[:120]
                            for value in (raw_dependencies if isinstance(raw_dependencies, list) else [])
                            if str(value or "").strip()
                        ]
                        if isinstance(progress, dict):
                            progress["attempted"] = entry_index + 1
                            progress["remaining"] = max(0, len(entries) - entry_index - 1)
                        entry_ref = str(entry.get("ref") or "")
                        entry_healed = bool(entry.get("self_healed"))
                        unsatisfied = [
                            dependency for dependency in dependencies
                            if field_states.get(dependency) != "verified"
                        ]
                        if unsatisfied:
                            # A batch runs in the order it was written. A field
                            # that depends on one listed after it can only fail,
                            # and "dependency did not verify" reads as a tool
                            # fault — a live run reported it as one. Name the
                            # ordering instead, since that is the whole fix.
                            later = [
                                name for name in unsatisfied
                                if any(
                                    str(other.get("id") or "") == name
                                    for other in entries[entry_index + 1:]
                                    if isinstance(other, dict)
                                )
                            ]
                            field_states[field_id] = "blocked_dependency"
                            field_results.append({
                                "id": field_id,
                                "ref": entry_ref,
                                "label": str(entry.get("label") or "")[:120],
                                "kind": str(entry.get("kind") or entry.get("type") or "fill")[:24],
                                "status": "failed",
                                "error": (
                                    f"{field_id!r} depends on {', '.join(later)}, which this "
                                    f"batch lists after it. Fields run in the order given, so "
                                    f"put a dependency before the field that needs it."
                                    if later else
                                    "dependency did not verify: " + ", ".join(unsatisfied)
                                ),
                                "verified": False,
                                "effect_state": "not_dispatched",
                                "dispatch_status": "not_dispatched",
                                "retry_safe": True,
                                "failure_reason": "dependency_failed",
                                "depends_on": dependencies,
                                "timing_ms": {"total": round((time.monotonic() - field_started) * 1000)},
                            })
                            if isinstance(progress, dict):
                                progress.update(
                                    fields=list(field_results), attempted=len(field_results),
                                    remaining=max(0, len(entries) - len(field_results)),
                                )
                            continue
                        try:
                            resolve_started = time.monotonic()
                            try:
                                entry_root, entry_selector = await resolve_live_target(
                                    page, entry_ref, workspace_id=record.workspace_id, tab_id=tab_id,
                                    observation_id=entry.get("observation_id") or observation_id,
                                )
                            except StaleRefError:
                                recovered = await self._reresolve_by_label(
                                    record, tab, page, str(entry.get("label") or ""), lock_held=True,
                                    identity=ref_identity(
                                        entry_ref, workspace_id=record.workspace_id, tab_id=tab_id,
                                    ),
                                )
                                if not recovered:
                                    raise
                                entry_ref = recovered
                                entry_healed = True
                                entry_root, entry_selector = await resolve_live_target(
                                    page, entry_ref, workspace_id=record.workspace_id, tab_id=tab_id,
                                )
                            resolved_at = time.monotonic()
                            target_identity = ref_identity(
                                entry_ref, workspace_id=record.workspace_id, tab_id=tab_id,
                            ) or {}
                            node_token = str(target_identity.get("node_token") or "")
                            target_key: tuple[Any, ...] = (
                                "node", target_identity.get("frame_index"),
                                target_identity.get("frame_url"), node_token,
                            ) if node_token else ("ref", entry_ref)
                            if target_key in dispatched_targets:
                                field_states[field_id] = "duplicate_target"
                                field_results.append({
                                    "id": field_id,
                                    "ref": entry_ref,
                                    "label": str(entry.get("label") or "")[:120],
                                    "kind": str(entry.get("kind") or entry.get("type") or "fill")[:24],
                                    "status": "failed",
                                    "verified": False,
                                    "effect_state": "not_dispatched",
                                    "dispatch_status": "not_dispatched",
                                    "retry_safe": True,
                                    "failure_reason": "duplicate_target",
                                    "error": "This control was already edited in this batch; observe and use its correct question ref.",
                                    "timing_ms": {"total": round((time.monotonic() - field_started) * 1000)},
                                })
                                if isinstance(progress, dict):
                                    progress.update(
                                        fields=list(field_results), attempted=len(field_results),
                                        remaining=max(0, len(entries) - len(field_results)),
                                    )
                                continue
                            tab.dirty = True
                            self._assert_may_mutate(tab, actor, actor_id, "mutation")
                            if yield_to_human and await _control_holds_unrequested_value(
                                entry_root, entry_selector, _requested_control_state(entry),
                                observed_controls.get(str(entry.get("ref") or "")),
                            ):
                                # cua's snapshot-before invariant: the person
                                # changed this control after the model last saw
                                # the page, so the batch does not overwrite
                                # their edit. Other fields continue; the model
                                # re-observes and decides for this one.
                                field_states[field_id] = "human_edited"
                                field_results.append({
                                    "id": field_id,
                                    "ref": entry_ref,
                                    "label": str(entry.get("label") or "")[:120],
                                    "kind": str(
                                        entry.get("kind") or entry.get("type") or "fill"
                                    )[:24],
                                    "status": "yielded",
                                    "verified": False,
                                    "human_edited": True,
                                    "self_healed": entry_healed,
                                    "effect_state": "not_dispatched",
                                    "dispatch_status": "not_dispatched",
                                    "retry_safe": True,
                                    "error": (
                                        "The person edited this field after your "
                                        "last observation; their value was kept. "
                                        "Re-observe and decide."
                                    ),
                                    "failure_reason": "human_edited",
                                    "timing_ms": {
                                        "total": round((time.monotonic() - field_started) * 1000)
                                    },
                                })
                                if isinstance(progress, dict):
                                    progress.update(
                                        fields=list(field_results),
                                        attempted=len(field_results),
                                        remaining=max(0, len(entries) - len(field_results)),
                                    )
                                continue
                            before_dispatch()
                            dispatched_targets.add(target_key)
                            entry_cue_kind = str(entry.get("kind") or entry.get("type") or "fill")
                            await self._agent_cursor_move_for_dispatch(
                                page, entry_root, entry_selector,
                                workspace_id=record.workspace_id, tab_id=tab_id,
                                detail=_agent_cursor_control_caption(entry_cue_kind, entry),
                                follow=(
                                    ("pointer_down", "pointer_up")
                                    if entry_cue_kind in {"select", "check", "radio", "checkbox"}
                                    else ("key",)
                                ),
                            )
                            field_probe: list[dict[str, Any]] = []
                            entry_kind, outcome, evidence = await _apply_form_control(
                                entry_root, entry_selector, entry, final_probes=field_probe,
                            )
                            completed_at = time.monotonic()
                            verified = bool(evidence and evidence.get("verified") is True)
                            effect_state = (
                                "verified" if verified else
                                "contradicted" if evidence and evidence.get("failure_reason") == "state_mismatch"
                                else "uncertain"
                            )
                            field_states[field_id] = effect_state
                            field_result = {
                                "id": field_id,
                                "ref": entry_ref,
                                "label": str(entry.get("label") or "")[:120],
                                "kind": entry_kind,
                                "status": outcome if verified else "failed",
                                "verified": verified,
                                "self_healed": entry_healed,
                                "effect_state": effect_state,
                                "dispatch_status": "dispatched",
                                "retry_safe": False,
                                "timing_ms": {
                                    "resolve": round((resolved_at - resolve_started) * 1000),
                                    "dispatch_verify": round((completed_at - resolved_at) * 1000),
                                    "total": round((completed_at - field_started) * 1000),
                                },
                            }
                            if dependencies:
                                field_result["depends_on"] = dependencies
                            if evidence:
                                field_result["verification"] = str(evidence.get("verification") or "")
                                field_result["requested"] = evidence.get("requested")
                                field_result["observed"] = evidence.get("observed")
                                if isinstance(evidence.get("timing_ms"), dict):
                                    field_result["control_timing_ms"] = dict(evidence["timing_ms"])
                                if evidence.get("failure_reason"):
                                    field_result["failure_reason"] = evidence["failure_reason"]
                            if not verified:
                                field_result["error"] = "field effect could not be verified"
                            field_results.append(field_result)
                            if verified and field_probe:
                                final_probes.append((len(field_results) - 1, field_probe[0]))
                            if isinstance(progress, dict):
                                progress.update(
                                    fields=list(field_results), attempted=len(field_results),
                                    remaining=max(0, len(entries) - len(field_results)),
                                )
                            if (
                                verified
                                and entry_kind in {"select", "date"}
                                and entry_index + 1 < len(entries)
                            ):
                                await self._refresh_pending_form_fields(
                                    record, tab, page, entries,
                                    start_index=entry_index + 1,
                                )
                            # A contradicted readback proves the requested value
                            # did not stick. Continue unrelated fields and skip
                            # only declared dependents. An uncertain effect can
                            # still have happened, so it remains terminal.
                            if effect_state == "uncertain":
                                break
                        except Exception as exc:
                            # A refusal that inspected the control and declined
                            # left the page as it was, so the fields that do not
                            # depend on it are still worth doing: one unmatched
                            # option label used to abandon six untouched fields
                            # and cost a round trip each. Anything that may have
                            # touched the page stays terminal.
                            touched = bool(getattr(exc, "dispatched", True))
                            field_states[field_id] = "uncertain" if touched else "not_dispatched"
                            field_results.append({
                                "id": field_id,
                                "ref": entry_ref,
                                "label": str(entry.get("label") or "")[:120],
                                "kind": str(entry.get("kind") or entry.get("type") or "fill")[:24],
                                "status": "failed",
                                "error": describe_exception(exc),
                                "verified": False,
                                "effect_state": "uncertain" if touched else "not_dispatched",
                                "dispatch_status": "possible" if touched else "not_dispatched",
                                "retry_safe": not touched,
                                "timing_ms": {"total": round((time.monotonic() - field_started) * 1000)},
                                **({"depends_on": dependencies} if dependencies else {}),
                                # Evidence a failing control captured while its
                                # state still existed. The prose is truncated;
                                # this must survive as structured data.
                                **_failure_evidence(exc),
                            })
                            if isinstance(progress, dict):
                                progress.update(
                                    fields=list(field_results), attempted=len(field_results),
                                    remaining=max(0, len(entries) - len(field_results)),
                                )
                            if touched:
                                break
                    tab.dirty = True
                elif kind == "upload":
                    from ascended_browser._app.browser_upload import UploadError, choose_files_verified, set_input_files_verified

                    ref = str(action.get("ref") or "")
                    root, selector = await resolve_live_target(
                        page, ref, workspace_id=record.workspace_id, tab_id=tab_id,
                        observation_id=action.get("observation_id"),
                    )
                    try:
                        tab.dirty = True
                        self._assert_may_mutate(tab, actor, actor_id, "mutation")
                        before_dispatch()
                        await self._agent_cursor_move_for_dispatch(
                            page, root, selector,
                            workspace_id=record.workspace_id, tab_id=tab_id,
                            detail=_agent_cursor_upload_caption(_upload_paths(action)),
                            follow=("pointer_down", "pointer_up"),
                        )
                        is_file_input = await _bounded(root.locator(selector).first.evaluate(
                            "el => el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'file'"), 2.0)
                        if action.get("file_chooser") is True or not is_file_input:
                            # A button or link that opens the page's own file
                            # picker (Workday's "Select file"): capture the
                            # chooser it opens rather than refusing the target.
                            upload_result = await choose_files_verified(
                                page, root, selector=selector, paths=_upload_paths(action), target_ref=ref,
                            )
                        else:
                            upload_result = await set_input_files_verified(
                                root, selector=selector, paths=_upload_paths(action), target_ref=ref,
                            )
                    except UploadError as exc:
                        raise WorkspaceError(str(exc)) from exc
                    detail = "uploaded " + ", ".join(
                        upload_result.get("expected_filenames") or []
                    )
                    upload_evidence = upload_result
                    tab.dirty = True
                elif kind == "wait":
                    # Nothing is dispatched: the tail below waits for `until`
                    # and the page that results is the answer.
                    await self._agent_cursor_status(
                        page, "Waiting for the page",
                        workspace_id=record.workspace_id, tab_id=tab_id,
                    )
                    detail = "waited for " + "; ".join(describe_conditions(until_conditions or {}))
                elif kind == "press":
                    typed = _typed_text(action.get("key"))
                    target_ref = str(action.get("ref") or "").strip()
                    if target_ref:
                        # Aim the key at the named element. Focus needs no
                        # pointer hit, so this reaches controls a click cannot
                        # (a date segment parked outside the viewport).
                        action_stage("resolve")
                        root, selector = await resolve_live_target(
                            page, target_ref, workspace_id=record.workspace_id,
                            tab_id=tab_id, observation_id=action.get("observation_id"),
                        )
                        target = root.locator(selector).first
                        await self._check_target_identity(
                            record=record, tab_id=tab_id, kind=kind, action=action, locator=target,
                        )
                        before_dispatch()
                        await target.focus(timeout=5000)
                    else:
                        before_dispatch()
                    self._agent_cursor_key_cue_soon(
                        page, typed=typed, key=action.get("key"),
                        workspace_id=record.workspace_id, tab_id=tab_id,
                    )
                    activity = ActionActivity(page).start()
                    try:
                        if typed:
                            await page.keyboard.type(typed, delay=40)
                            detail = f"typed {len(typed)} character(s)"
                        else:
                            # Enter in a form is a submission; follow it like a click.
                            await page.keyboard.press(_normalize_key(action.get("key")))
                        navigation_state = await self._settle_activity_after_dispatch(
                            activity, effect_barrier, operation_name="post-key activity settle",
                        )
                    finally:
                        self._keep_or_stop_activity(record.workspace_id, tab_id, activity, navigation_state)
                    if navigation_state.get("navigated") or navigation_state.get("state") != "settled":
                        await self._settle_page_after_dispatch(page, effect_barrier)
                elif kind == "scroll":
                    from ascended_browser._app.browser_scroll import scroll_once
                    from ascended_browser._app.browser_click_helpers import current_ref_candidates

                    delta_y, interval_s, duration_s = scroll_action_settings(action)
                    deadline = time.monotonic() + duration_s
                    target_ref = str(action.get("ref") or "")
                    if target_ref:
                        root, selector = await resolve_live_target(
                            page, target_ref, workspace_id=record.workspace_id,
                            tab_id=tab_id, observation_id=action.get("observation_id"),
                        )
                    else:
                        root, selector = page, ":root"
                    scroll_caption = "Scrolling up" if delta_y < 0 else "Scrolling down"
                    await self._agent_cursor_scroll_cue(
                        page, root if target_ref else None, selector if target_ref else "",
                        detail=scroll_caption,
                        workspace_id=record.workspace_id, tab_id=tab_id,
                    )
                    scroll_evidence = None
                    pulses = 0
                    yielded_for_transition = False
                    while True:
                        before_dispatch()
                        if pulses:
                            await self._agent_cursor_move_for_dispatch(
                                page, root, selector, kind="wheel", detail=scroll_caption,
                                workspace_id=record.workspace_id, tab_id=tab_id,
                            )
                        pulse = await scroll_once(
                            root, selector, delta_y=delta_y, targeted=bool(target_ref),
                        )
                        pulses += 1
                        token = pulse.pop("node_token", "")
                        container_ref = next((item["ref"] for item in current_ref_candidates(
                            workspace_id=record.workspace_id, tab_id=tab_id,
                        ) if item.get("node_token") == token), "")
                        if scroll_evidence is None:
                            scroll_evidence = {**pulse, "container_ref": container_ref}
                        else:
                            scroll_evidence.update(after=pulse["after"], state=pulse["state"])
                            scroll_evidence["moved"] += pulse["moved"]
                            scroll_evidence["changed"] |= pulse["changed"]
                        scroll_evidence["pulses"] = pulses
                        if pulse["state"] != "moved" or duration_s <= 0 or time.monotonic() >= deadline:
                            break
                        yielded_for_transition = await effect_barrier.wait_for_transition(
                            min(interval_s, max(0.0, deadline - time.monotonic())),
                        )
                        if yielded_for_transition:
                            break
                    detail = (
                        f"Scroll {scroll_evidence['state']}: measured "
                        f"{scroll_evidence['moved']:g}px movement; "
                        f"position {scroll_evidence['after']['top']:g}/"
                        f"{scroll_evidence['after']['max_top']:g}."
                    )
                    if yielded_for_transition:
                        detail += " yielded for browser control transition."
                invalid_fields = (
                    await _leave_field_and_read_invalid(page)
                    if kind in {"fill", "fill_form", "select", "check"}
                    # Blur-then-read costs up to a second of waiting on the
                    # page. A queued selection or takeover outranks it: the
                    # fields keep their state, and the next observe reports
                    # the same aria-invalid.
                    and not effect_barrier.transition_pending else []
                )
                if kind == "fill_form" and final_probes and not effect_barrier.transition_pending:
                    from ascended_browser._app.browser_live_verification import verify_fill_probe_final

                    for field_index, probe in final_probes:
                        final_evidence = await verify_fill_probe_final(probe)
                        if final_evidence.get("verified") is True:
                            continue
                        field_result = field_results[field_index]
                        field_result.update(
                            verified=False,
                            status="failed",
                            effect_state=(
                                "contradicted" if final_evidence.get("failure_reason") == "state_mismatch"
                                else "uncertain"
                            ),
                            verification="final_batch_control_readback",
                            observed=final_evidence.get("observed"),
                            failure_reason=final_evidence.get("failure_reason") or "readback_unavailable",
                            error="field no longer holds its requested value after the batch",
                        )
                    if isinstance(progress, dict):
                        progress["fields"] = list(field_results)
                until_evidence = None
                if until_conditions:
                    action_stage("verify")
                    until_evidence = await self._await_until(
                        record, tab, page, until_conditions, kind=kind,
                        effect_barrier=effect_barrier,
                    )
                after_auth = await self._auth_signature(page, domain) if sign_in else ""
                action_stage("observation")
                result = await self._finish_act(
                    record, tab, page,
                    tab_id=tab_id,
                    kind=kind,
                    domain=domain,
                    before_auth=before_auth,
                    after_auth=after_auth,
                    detail=detail,
                    field_results=field_results,
                    upload_evidence=upload_evidence,
                    self_healed=self_healed,
                    click_evidence=click_evidence,
                    scroll_evidence=scroll_evidence,
                    navigation_state=navigation_state,
                    invalid_fields=invalid_fields,
                    until_evidence=until_evidence,
                    page_state_before=page_state_before,
                )
                if int(tab.human_input_revision or 0) > human_revision_at_entry:
                    # The person typed, scrolled, or clicked while this action
                    # ran. The receipt stays truthful: report it so the model
                    # re-observes before its next state assumption.
                    result["human_input_during_action"] = True
                    result["next_action"] = (
                        "The person interacted with the page during this "
                        "action. Re-observe before assuming page state."
                    )
                return result
        finally:
            if release_domain_slots is not None:
                release_domain_slots()
            if effect_started is not None:
                self._log_act_timing(
                    tab_id, kind, lock_wait, time.monotonic() - effect_started,
                )

    async def _finish_act(
        self, record: WorkspaceRecord, tab: TabRecord, page: Any, *,
        tab_id: str, kind: str, domain: str,
        before_auth: str, after_auth: str, detail: str,
        field_results: list[dict], upload_evidence: dict[str, Any] | None,
        self_healed: bool,
        click_evidence: dict | None = None,
        scroll_evidence: dict | None = None,
        options_listing: dict[str, Any] | None = None,
        navigation_state: dict[str, Any] | None = None,
        invalid_fields: list[dict[str, Any]] | None = None,
        until_evidence: dict[str, Any] | None = None,
        page_state_before: str | None = None,
    ) -> dict:
        """Persist and observe an action while its tab lock is still held."""
        if before_auth != after_auth:
            self._record_domain_disruption(record.workspace_id, domain, "authentication state changed in parent tab")
        await self._refresh_tab(record, tab, page, agent=True)
        self.store.append_journal(record.workspace_id, {"at": time.time(), "tab_id": tab_id, "action": kind, "url": tab.url})
        self.store.save(record)
        result: dict[str, Any] = {"success": True, "tab": tab.public()}
        if scroll_evidence is not None:
            result["scroll"] = scroll_evidence
            if scroll_evidence["state"] == "stalled":
                result.update(success=False, error="Scroll did not move and the requested boundary was not reached. Observe the tab and choose its scroll_containers ref.")
        if click_evidence is not None:
            result["verification"] = click_evidence
        if until_evidence is not None:
            # What the action waited for, and whether the page got there. A
            # bare wait that did not is a failed step; an action whose own
            # effect landed keeps its success and says the condition is unmet.
            result["until"] = until_evidence
            if not until_evidence.get("met"):
                result["postcondition_met"] = False
                if kind == "wait":
                    result.update(success=False, error=str(until_evidence.get("detail") or "wait condition not observed"))
        if invalid_fields:
            # The page's own verdict on what was just entered. A fill that read
            # back exactly can still be rejected (Workday's phone check); the
            # model spent four extractions finding out which field and why.
            result["invalid_fields"] = invalid_fields
        if navigation_state is not None:
            from ascended_browser._app.browser_action_settle import still_loading_note

            # Whether the page had finished responding when this result was
            # taken, and whether it navigated: the model's only way to tell a
            # finished sign-in from a page caught mid-redirect.
            result["navigation_state"] = dict(navigation_state)
            if still_loading_note(navigation_state):
                result["navigation_state"]["next_step"] = still_loading_note(navigation_state)
        if options_listing is not None:
            # A read, not a mutation: report the options and say plainly that
            # nothing was committed, so this is never mistaken for a selection.
            result["observed_options"] = options_listing.get("options") or []
            result["popup_strategy"] = str(options_listing.get("popup_strategy") or "")
            result["options_truncated"] = bool(options_listing.get("truncated"))
            result["committed"] = False
        note = self.take_note(record, tab)
        if note:
            result["user_note"] = (
                f"The user took control of this tab and says: {note!r}. "
                "Treat that as what happened; do not redo it."
            )
        if self_healed:
            # Say so: the model should learn its ref had gone stale, not believe
            # the one it sent is still good.
            result["self_healed"] = (
                "The ref you sent no longer resolved (the page was rebuilt); "
                "re-observed and found the one element it named, by its id, "
                "name or the text beside it. Use refs from the newest observation."
            )
        if detail:
            result["detail"] = detail
        if upload_evidence is not None:
            # Ahead of the snapshot for the same key-order reason as field
            # results below: this is the evidence that the attach really landed.
            result["upload"] = upload_evidence
        # Per-field outcomes go in BEFORE the page snapshot. Key order is what
        # the result formatter truncates on, and the snapshot is thousands of
        # characters, so a form that failed reported "Error: unknown" with the
        # one thing needed to fix it cut off the end.
        if field_results:
            failed = [item for item in field_results if item["status"] == "failed"]
            unattempted = [item for item in field_results if item["status"] == "unattempted"]
            result["filled"] = len(field_results) - len(failed) - len(unattempted)
            result["partial_failure"] = bool(failed and result["filled"])
            if failed:
                # Not an exception: the rest of the form did land, and saying so
                # is what lets the model fix one field instead of redoing all.
                result["success"] = False
                result["failed"] = len(failed)
                # An explicit message, or the formatter has nothing to render
                # and every partial form failure reads as "Error: unknown".
                result["error"] = "; ".join(
                    f"{item['ref']}: {item.get('error') or 'failed'}" for item in failed[:6]
                )
            if unattempted:
                # A user-requested browser transition won at a field boundary.
                # This is incomplete work, never a failed/no-effect batch: the
                # model receives exact untouched fields and must observe the
                # selected page before deciding whether to continue.
                result["success"] = False
                result["incomplete"] = True
                result["unattempted"] = len(unattempted)
                if not result.get("error"):
                    result["error"] = (
                        "Browser control or tab selection was pending; remaining fields were not attempted."
                    )
            result["fields"] = field_results
        # Hand back the page the action produced. Without this the model must
        # call browser_observe after almost every act just to learn what
        # changed — 18 observes for 30 acts in one measured session — and each
        # one is a whole extra round trip that re-sends the conversation.
        if (
            (kind in _RESTATE_AFTER or kind in _RESTATE_WHEN_CHANGED)
            and get_setting("browser_workspace_act_returns_page", True)
            and await self._page_needs_reread(record, tab, page, kind, page_state_before, result)
        ):
            effect_barrier = self._runtime_effect_barrier(record.owner)
            try:
                result["page"] = await self._read_until_runtime_transition(
                    self._observe_for_result(
                        record, tab, page, lock_held=True,
                    ),
                    effect_barrier,
                    operation_name="post-action observation",
                    wait_seconds=float(
                        get_setting("browser_workspace_observe_timeout_seconds", 90) or 90
                    ) + 15.0,
                )
            except RuntimeTransitionRequested:
                # The action and its bounded verification already completed.
                # Preserve that truthful success while dropping only the rich
                # DOM read that would keep takeover/resize waiting behind it.
                result["observation_deferred"] = (
                    "A browser control or viewport transition was pending; "
                    "inspect the page after the transition or handback."
                )
        return self._attach_blocker(record, tab, result)

    async def extract(
        self, owner: str, session_id: str, tab_id: str, *, instruction: str = "",
        target_ref: str = "", find: str = "", from_end: bool = False,
        actor: str = "parent", actor_id: str = "parent", parallel_tab: bool = False,
    ) -> dict:
        _parallel_token = _PARALLEL_TAB_READ.set(bool(parallel_tab))
        try:
            return await self._extract_entry(
                owner, session_id, tab_id, instruction=instruction, target_ref=target_ref,
                find=find, from_end=from_end, actor=actor, actor_id=actor_id,
            )
        finally:
            _PARALLEL_TAB_READ.reset(_parallel_token)

    async def _extract_entry(
        self, owner: str, session_id: str, tab_id: str, *, instruction: str,
        target_ref: str, find: str, from_end: bool, actor: str, actor_id: str,
    ) -> dict:
        async with self._pin_tab(self.workspace_id(owner, session_id), tab_id):
            effect_barrier = self._runtime_effect_barrier(owner)
            async with effect_barrier.shared():
                self.assert_tab_access(
                    owner, session_id, tab_id, actor=actor, actor_id=actor_id,
                )
                return await self._read_until_runtime_transition(
                    self._extract(
                        owner, session_id, tab_id, instruction=instruction,
                        target_ref=target_ref, find=find, from_end=from_end,
                    ),
                    effect_barrier,
                    operation_name="extraction",
                    wait_seconds=float(
                        get_setting("browser_workspace_observe_timeout_seconds", 90) or 90
                    ) + 30.0,
                )

    async def query_elements(
        self, owner: str, session_id: str, tab_id: str, *, selector: str,
        attributes: Any = None, max_results: Any = None, include_text: Any = True,
        cursor: Any = 0,
        actor: str = "parent", actor_id: str = "parent",
    ) -> dict:
        """Count and list elements by CSS selector: no snapshot, no model call."""
        from ascended_browser._app.browser_element_query import normalize_query_args

        try:
            args = normalize_query_args(selector, attributes, max_results, include_text, cursor=cursor)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "tab_id": tab_id}
        async with self._pin_tab(self.workspace_id(owner, session_id), tab_id):
            effect_barrier = self._runtime_effect_barrier(owner)
            async with effect_barrier.shared():
                self.assert_tab_access(
                    owner, session_id, tab_id, actor=actor, actor_id=actor_id,
                )
                return await self._read_until_runtime_transition(
                    self._query_elements(owner, session_id, tab_id, args),
                    effect_barrier,
                    operation_name="element query",
                    wait_seconds=30.0,
                )

    async def _query_elements(
        self, owner: str, session_id: str, tab_id: str, args: dict[str, Any],
    ) -> dict:
        from ascended_browser._app.browser_click_helpers import current_ref_candidates, frame_is_rendered
        from ascended_browser._app.browser_element_query import QUERY_SCRIPT, attach_refs, format_query_result

        record = await self.ensure_awake(owner, session_id)
        tab, page = await self._get_tab(record, tab_id)
        async with self._action_lock(record.workspace_id, tab_id):
            candidates = current_ref_candidates(
                workspace_id=record.workspace_id, tab_id=tab_id,
            )
            try:
                data = await _bounded(page.evaluate(QUERY_SCRIPT, args), 10.0)
            except Exception as exc:
                return {"success": False, "source": "element_query", "tab_id": tab_id,
                        "selector": args["selector"],
                        "error": f"The page did not answer the query: {type(exc).__name__}"}
            if not isinstance(data, dict):
                return {"success": False, "source": "element_query", "tab_id": tab_id,
                        "selector": args["selector"], "error": "The page returned no query result."}
            if data.get("error"):
                return {"success": False, "source": "element_query", "tab_id": tab_id,
                        "selector": args["selector"], "error": str(data["error"])}
            elements = [item for item in data.get("elements") or [] if isinstance(item, dict)]
            attach_refs(elements, candidates)
            total = int(data.get("total") or 0)
            # A form or listing inside an embedded application lives in an
            # iframe that document.querySelectorAll never reaches (see extract).
            for frame in list(getattr(page, "frames", []) or [])[1:12]:
                room = args["maxResults"] - len(elements)
                try:
                    if not await _bounded(frame_is_rendered(frame), 3.0):
                        continue
                    found = await _bounded(frame.evaluate(
                        QUERY_SCRIPT, {**args, "maxResults": max(room, 0), "start": max(0, args["start"] - total)},
                    ), 5.0)
                except Exception:
                    continue
                if not isinstance(found, dict) or found.get("error") or not found.get("total"):
                    continue
                frame_url = str(getattr(frame, "url", "") or "")
                inner = [item for item in found.get("elements") or [] if isinstance(item, dict)]
                attach_refs(inner, candidates, frame_url=frame_url, main_frame=False)
                for item in inner:
                    item["index"] = args["start"] + len(elements)
                    item["frame"] = frame_url[:200]
                    elements.append(item)
                total += int(found.get("total") or 0)
        result = {
            "success": True,
            "source": "element_query",
            "tab_id": tab_id,
            "url": str(getattr(page, "url", "") or tab.url),
            "title": tab.title,
            "selector": args["selector"],
            "total": total,
            "showing": len(elements),
            "cursor": args["start"],
            "next_cursor": args["start"] + len(elements) if args["start"] + len(elements) < total else None,
            # Not "elements": that key is observed controls to every consumer
            # of an extraction, and these are query matches of any kind.
            "matches": elements,
        }
        result["text"] = format_query_result(result, args["selector"])
        result["summary"] = (
            f'Found {total} element{"s" if total != 1 else ""} matching "{args["selector"]}".'
        )
        return result

    async def read_log(
        self, owner: str, session_id: str, tab_id: str, *, read: str,
        level: str = "", pattern: str = "", types: Any = None, failed_only: bool = False,
        navigations: Any = 1, cursor: Any = 0, max_results: Any = None,
        actor: str = "parent", actor_id: str = "parent",
    ) -> dict:
        """The tab's console or network log: no snapshot, no model call, no page script."""
        from ascended_browser._app.browser_diagnostics import format_console_log, format_network_log

        read = str(read or "").strip().casefold()
        if read not in {"console", "network"}:
            return {"success": False, "error": "read must be console or network", "tab_id": tab_id}
        try:
            start = max(0, int(cursor or 0))
            limit = max(1, min(200, int(max_results or (60 if read == "console" else 40))))
            epochs = max(1, min(3, int(navigations or 1)))
        except (TypeError, ValueError):
            return {"success": False, "error": "cursor, max_results and navigations must be integers", "tab_id": tab_id}
        if isinstance(types, str):
            types = [part.strip() for part in types.split(",") if part.strip()]
        try:
            re.compile(str(pattern or ""))
        except re.error as exc:
            return {"success": False, "error": f"filter is not a valid regular expression: {exc}", "tab_id": tab_id}
        workspace_id = self.workspace_id(owner, session_id)
        async with self._pin_tab(workspace_id, tab_id):
            self.assert_tab_access(owner, session_id, tab_id, actor=actor, actor_id=actor_id)
            record = await self.ensure_awake(owner, session_id)
            tab, page = await self._get_tab(record, tab_id)
            diagnostics = self._diagnostics.get((workspace_id, tab_id))
            if diagnostics is None:
                diagnostics = self.attach_diagnostics(record, tab, page)
            if read == "console":
                rows = diagnostics.console_entries(level=str(level or ""), epochs=epochs)
            else:
                rows = diagnostics.network_entries(
                    pattern=str(pattern or ""), types=types or (), failed_only=bool(failed_only), epochs=epochs,
                )
            total = len(rows)
            page_rows = rows[start:start + limit]
            result = {
                "success": True,
                "source": f"{read}_log",
                "read": read,
                "tab_id": tab_id,
                "url": str(getattr(page, "url", "") or tab.url),
                "title": tab.title,
                "navigations": epochs,
                "total": total,
                "showing": len(page_rows),
                "cursor": start,
                "next_cursor": start + len(page_rows) if start + len(page_rows) < total else None,
                "entries": page_rows,
            }
            if read == "console":
                result["text"] = format_console_log(result, level=str(level or ""))
            else:
                result["text"] = format_network_log(result, pattern=str(pattern or ""), types=types or (), failed_only=bool(failed_only))
            # Reading the log is reading it: the ambient delta on the next
            # result need not repeat what this call just showed in full.
            diagnostics.take_summary()
            return result

    async def inspect_ref(
        self, owner: str, session_id: str, tab_id: str, *, ref: str,
        actor: str = "parent", actor_id: str = "parent",
    ) -> dict:
        """Box, hit target, hiding/clipping ancestors and styles of one observed element."""
        from ascended_browser._app.browser_click_helpers import (
            StaleRefError, current_ref_candidates, ref_identity, resolve_live_target,
        )
        from ascended_browser._app.browser_inspect import _is_sensitive, format_inspection, inspect_locator

        ref = str(ref or "").strip()
        if not ref:
            return {"success": False, "error": "read=inspect requires target_ref: a ref from the current observation.", "tab_id": tab_id}
        workspace_id = self.workspace_id(owner, session_id)
        async with self._pin_tab(workspace_id, tab_id):
            effect_barrier = self._runtime_effect_barrier(owner)
            async with effect_barrier.shared():
                self.assert_tab_access(owner, session_id, tab_id, actor=actor, actor_id=actor_id)
                record = await self.ensure_awake(owner, session_id)
                tab, page = await self._get_tab(record, tab_id)
                base = {
                    "source": "element_inspect", "read": "inspect", "tab_id": tab_id, "ref": ref,
                    "url": str(getattr(page, "url", "") or tab.url), "title": tab.title,
                }
                try:
                    root, selector = await resolve_live_target(
                        page, ref, workspace_id=workspace_id, tab_id=tab_id,
                    )
                except StaleRefError as exc:
                    return {**base, "success": False, "error": f"{exc} Nothing was inspected."}
                except ValueError as exc:
                    return {**base, "success": False, "error": str(exc)}
                identity = ref_identity(ref, workspace_id=workspace_id, tab_id=tab_id) or {}
                try:
                    inspection = await inspect_locator(
                        root.locator(selector).first,
                        candidates=current_ref_candidates(workspace_id=workspace_id, tab_id=tab_id),
                        sensitive=_is_sensitive(identity),
                    )
                except Exception as exc:
                    return {**base, "success": False, "error": f"The page did not answer the inspection: {type(exc).__name__}"}
                result = {**base, "success": True, **inspection}
                result["text"] = format_inspection(result)
                return result

    async def read_audit(
        self, owner: str, session_id: str, tab_id: str, *, checks: Any = None,
        actor: str = "parent", actor_id: str = "parent",
    ) -> dict:
        """Accessibility, performance, page basics, broken links and console of the page."""
        from ascended_browser._app.browser_audit import format_audit, normalize_checks, run_audit
        from ascended_browser._app.browser_click_helpers import current_ref_candidates

        try:
            wanted = normalize_checks(checks)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "tab_id": tab_id}
        workspace_id = self.workspace_id(owner, session_id)
        async with self._pin_tab(workspace_id, tab_id):
            effect_barrier = self._runtime_effect_barrier(owner)
            async with effect_barrier.shared():
                self.assert_tab_access(owner, session_id, tab_id, actor=actor, actor_id=actor_id)
                record = await self.ensure_awake(owner, session_id)
                tab, page = await self._get_tab(record, tab_id)
                base = {
                    "source": "page_audit", "read": "audit", "tab_id": tab_id,
                    "url": str(getattr(page, "url", "") or tab.url), "title": tab.title,
                }
                diagnostics = self._diagnostics.get((workspace_id, tab_id))
                if diagnostics is None:
                    diagnostics = self.attach_diagnostics(record, tab, page)
                async with self._read_lock(workspace_id, tab_id):
                    audit = await run_audit(
                        page, checks=wanted,
                        candidates=current_ref_candidates(workspace_id=workspace_id, tab_id=tab_id),
                        diagnostics=diagnostics,
                    )
                # The link check's own HEAD requests and the audit's reads are
                # not the page's news: the next result's ambient delta starts
                # after them, as it does after read=console.
                try:
                    diagnostics.take_summary()
                except Exception:
                    pass
                result = {**base, "success": True, **audit}
                result["text"] = format_audit(result)
                return result

    async def read_design(
        self, owner: str, session_id: str, tab_id: str, *, ref: str = "",
        actor: str = "parent", actor_id: str = "parent",
    ) -> dict:
        """Colors, type scale, radii, spacing, layout and icons of the page or one element."""
        from ascended_browser._app.browser_click_helpers import StaleRefError, resolve_live_target
        from ascended_browser._app.browser_design import format_design, read_design

        ref = str(ref or "").strip()
        workspace_id = self.workspace_id(owner, session_id)
        async with self._pin_tab(workspace_id, tab_id):
            effect_barrier = self._runtime_effect_barrier(owner)
            async with effect_barrier.shared():
                self.assert_tab_access(owner, session_id, tab_id, actor=actor, actor_id=actor_id)
                record = await self.ensure_awake(owner, session_id)
                tab, page = await self._get_tab(record, tab_id)
                base = {
                    "source": "design_read", "read": "design", "tab_id": tab_id,
                    "url": str(getattr(page, "url", "") or tab.url), "title": tab.title,
                }
                if ref.startswith("r") and not ref.startswith("ref"):
                    from ascended_browser._app.browser_design import locate_region

                    base["ref"] = ref
                    entry = self.__dict__.get("_region_tables", {}).get((workspace_id, tab_id), {}).get(ref)
                    if not entry:
                        return {**base, "success": False, "error": (
                            f"{ref} is not a region id from this tab's observations. Pass an element ref (e…) "
                            "inside it, or observe this tab for its region ids. Nothing was read.")}
                    target = await locate_region(page, entry)
                    if target is None:
                        return {**base, "success": False, "error": (
                            f"Region {ref} ({entry.get('role')} {str(entry.get('label') or '')[:60]!r}) is no longer "
                            "on the page. Pass an element ref (e…) inside it. Nothing was read.")}
                elif ref:
                    base["ref"] = ref
                    try:
                        root, selector = await resolve_live_target(
                            page, ref, workspace_id=workspace_id, tab_id=tab_id,
                        )
                    except StaleRefError as exc:
                        return {**base, "success": False, "error": f"{exc} Nothing was read."}
                    except ValueError as exc:
                        return {**base, "success": False, "error": str(exc)}
                    target = root.locator(selector).first
                else:
                    target = page
                try:
                    design = await read_design(target, scoped=bool(ref))
                except Exception as exc:
                    return {**base, "success": False, "error": f"The page did not answer the design read: {type(exc).__name__}"}
                return {**base, "success": True, "design": design, "text": format_design(design)}

    async def _extract(
        self, owner: str, session_id: str, tab_id: str, *, instruction: str = "",
        target_ref: str = "", find: str = "", from_end: bool = False,
    ) -> dict:
        from ascended_browser._app.browser_act import extract_snapshot, visible_errors, visible_status
        from ascended_browser._app.browser_click_helpers import (
            build_enhanced_snapshot, frame_is_rendered, ref_identity,
            resolve_live_target,
        )

        record = await self.ensure_awake(owner, session_id)
        tab, page = await self._get_tab(record, tab_id)
        async with self._read_lock(record.workspace_id, tab_id):
            scope_meta: dict[str, Any] | None = None
            scoped_control_ids: set[str] = set()
            if target_ref:
                identity = ref_identity(
                    target_ref, workspace_id=record.workspace_id, tab_id=tab_id,
                ) or {}
                root, selector = await resolve_live_target(
                    page, target_ref, workspace_id=record.workspace_id,
                    tab_id=tab_id, allow_selector=False,
                )
                scoped = await _bounded(root.locator(selector).evaluate(
                    r"""el => {
                      const ids = (el.getAttribute('aria-controls') || '')
                        .split(/\s+/).filter(Boolean);
                      const owned = ids.map(id => el.ownerDocument.getElementById(id))
                        .filter(node => node && node.getClientRects().length);
                      const targets = owned.length ? owned : [el];
                      return {
                        text: targets.map(node => String(node.innerText || node.textContent || '').trim())
                          .filter(Boolean).join('\n\n'),
                        strategy: owned.length ? 'aria_controls' : 'target',
                        controlled_ids: ids,
                      };
                    }""",
                ), 5.0)
                text = str((scoped or {}).get("text") or "")
                scoped_control_ids = {
                    str(value) for value in ((scoped or {}).get("controlled_ids") or [])
                    if str(value or "")
                }
                scope_meta = {
                    "target_ref": target_ref,
                    "strategy": str((scoped or {}).get("strategy") or "target"),
                    "label": str(identity.get("label") or identity.get("aria_label") or "")[:120],
                }
            else:
                try:
                    text = str(await deep_inner_text(page, timeout=4.0))
                except Exception:
                    # A frameset document has no body at all, so this waited out
                    # its whole timeout and failed the extraction, sending the
                    # run to browser_evaluate. The page's content is in its
                    # frames; read those instead of reporting nothing.
                    text = ""
            own_text = text
            # The visible content of an embedded application or checkout form
            # lives in an iframe, and `page.locator` never descends into one.
            # Extract on such a page returned the host site's marketing chrome,
            # so the agent could fill a form and then find no way to check that
            # anything had landed. Best-effort and bounded: an ad frame that
            # will not answer must not fail the extraction.
            for frame in ([] if target_ref else list(getattr(page, "frames", []) or [])[1:12]):
                try:
                    if not await _bounded(frame_is_rendered(frame), 3.0):
                        continue
                    embedded = str(await _bounded(
                        deep_inner_text(frame, timeout=4.0), 5.0,
                    )).strip()
                except Exception:
                    continue
                if not embedded or embedded in text:
                    continue  # chrome, pixels and empty frames
                if len(embedded) < 40 and len(own_text) >= 40:
                    # A short frame beside a page that reads on its own is
                    # chrome. Measured against the page's own text, not the
                    # text so far, or the first frame kept hides its siblings.
                    continue
                text += (
                    f"\n\n--- embedded frame: {str(getattr(frame, 'url', '') or '')[:200]} ---\n"
                    + embedded
                )
            # Keep enough private source for instruction-focused extraction.
            # Model-facing text remains independently bounded, and the existing
            # evidence archive retains the allowed complete payload.
            full_text = text[:250000]
            observation_id = ""
            # Extract is a high-level page read, not an observe delta. Build the
            # field/action state on every call even when observe has just cached
            # the same revision. The raw body read above remains authoritative
            # for prose because accessibility snapshots can omit ordinary text.
            try:
                snapshot = await asyncio.wait_for(
                    build_enhanced_snapshot(
                        page, workspace_id=record.workspace_id, tab_id=tab_id,
                    ),
                    timeout=float(
                        get_setting("browser_workspace_observe_timeout_seconds", 90) or 90
                    ),
                )
            except Exception:
                snapshot = {}
            if target_ref and isinstance(snapshot.get("elements"), list):
                def in_scope(element: dict[str, Any]) -> bool:
                    if str(element.get("ref") or "") == target_ref:
                        return True
                    region = element.get("containing_region")
                    region_id = str(region.get("id") or "") if isinstance(region, dict) else ""
                    return bool(region_id and region_id in scoped_control_ids)
                snapshot["elements"] = [
                    element for element in snapshot["elements"]
                    if isinstance(element, dict) and in_scope(element)
                ]
            try:
                snapshot["visible_errors"] = await visible_errors(page)
            except Exception:
                snapshot["visible_errors"] = []
            try:
                # What the page says about itself, kept apart from what went
                # wrong on it.
                status = await visible_status(page)
                if status:
                    snapshot["page_status"] = status
            except Exception:
                pass
            observation_id = str(
                snapshot.get("observation_id") or (snapshot.get("elements") or [{}])[0].get("observation_id") or ""
            )
            if observation_id:
                # The extract's field refs are now the newest valid refs. Seed
                # observe's cache from the same scan so a follow-up observe does
                # not immediately replace them merely to restate the controls.
                tab.last_observation_id = observation_id
                observe_text_limit = int(
                    get_setting("browser_workspace_observe_text_chars", 1200) or 1200
                )
                projected = self._project_observation(
                    record, tab, snapshot, text=full_text[:max(0, observe_text_limit)],
                )
                self._cache_observation(record.workspace_id, tab, page, projected)
                self.store.save(record)

        snapshot.update({
            "url": str(getattr(page, "url", "") or tab.url),
            "title": snapshot.get("title") or tab.title,
            "text": full_text,
            "visible_text": full_text,
        })
        result = extract_snapshot(snapshot, instruction=instruction)
        if find:
            # What a long page says about one thing, with enough either side to
            # read it. Live runs matched their own regex over innerText to get
            # this, because a page's answer is often below what fits.
            result["passages"] = _passages_for(full_text, find)
            result["find"] = find
            result["passage_count"] = len(result["passages"])
            if not result["passages"]:
                result["note"] = f"No passage on this page contains {find!r}."
        if from_end:
            # A deadline, a total, a footer: the end of a long page is content,
            # and forward paging charges for everything before it.
            window = int(get_setting("browser_workspace_read_text_chars", 6000) or 6000)
            tail = full_text[-window:]
            result["text"] = tail
            result["visible_text"] = tail
            # relevant_text is what the model reads. Left as the page head, a
            # from_end read returned the top of the page (or all of it).
            normalized_tail = re.sub(r"\s+", " ", tail).strip()
            result["relevant_text"] = normalized_tail
            result["text_coverage"] = {
                "strategy": "tail",
                "source_chars": len(re.sub(r"\s+", " ", full_text).strip()),
                "returned_chars": len(normalized_tail),
                "complete": len(tail) >= len(full_text),
            }
            result["read_from"] = "end"
            result["total_chars"] = len(full_text)
            return self._attach_blocker(record, tab, self._attach_login_available(owner, result))
        result["tab_id"] = tab_id
        result["observation_id"] = observation_id
        # Keep the historical `text` key for callers while exposing the richer
        # contract used by the high-level MCP extractor.
        result["text"] = full_text
        result["visible_text"] = full_text
        if scope_meta is not None:
            result["scope"] = scope_meta
        result = self._attach_login_available(owner, result)
        return self._attach_blocker(record, tab, result)

    async def evaluate(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        function: str,
        arg: Any = None,
        *,
        actor: str = "parent",
        actor_id: str = "parent",
    ) -> dict:
        async with self._pin_tab(self.workspace_id(owner, session_id), tab_id):
            effect_barrier = self._runtime_effect_barrier(owner)
            async with effect_barrier.shared():
                # A few narrow unit probes construct the manager via __new__ to
                # test only the evaluate timeout. Real managers always have the
                # durable records required for the authority recheck.
                if hasattr(self, "_records"):
                    self.assert_tab_access(
                        owner, session_id, tab_id, actor=actor, actor_id=actor_id,
                    )
                return await self._read_until_runtime_transition(
                    self._evaluate(owner, session_id, tab_id, function, arg),
                    effect_barrier,
                    operation_name="evaluation",
                    wait_seconds=float(
                        get_setting("browser_workspace_evaluate_timeout_seconds", 15) or 15
                    ) + 2.0,
                )

    async def _evaluate(self, owner: str, session_id: str, tab_id: str, function: str, arg: Any = None) -> dict:
        from ascended_browser._app.browser_evaluate_policy import validate_read_only_evaluate
        validate_read_only_evaluate(function)
        record = await self.ensure_awake(owner, session_id)
        _, page = await self._get_tab(record, tab_id)
        budget = float(get_setting("browser_workspace_evaluate_timeout_seconds", 15) or 15)
        async with self._action_lock(record.workspace_id, tab_id):
            # page.evaluate has no timeout of its own, and this runs under the
            # per-owner lock: a script that never settles would hang every
            # browser tool in every one of this owner's chats, not just this tab.
            try:
                result = await _bounded(page.evaluate(function, arg), budget)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                raise WorkspaceError(
                    f"browser_evaluate exceeded {budget:g}s and was abandoned. "
                    f"Return a small value instead of walking the whole page, "
                    f"or use browser_extract for page text."
                ) from exc
        return {"result": result}

    async def screenshot(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        purpose: str = "inspect",
        scope: str = "viewport",
        ref: str = "",
        state: str = "",
        actor: str = "parent",
        actor_id: str = "parent",
        parallel_tab: bool = False,
    ) -> dict:
        """Picture an agent-accessible tab, stamped with the geometry it had.

        A read: it changes no page state, holds the tab's action lock only for
        the capture, and yields to a pending takeover or resize rather than
        return a picture of a window that is about to change size. ``state``
        hovers or focuses ``ref`` for the capture and puts the pointer/focus
        back afterwards: a hover style is otherwise something only a person
        with a mouse can check.
        """
        _parallel_token = _PARALLEL_TAB_READ.set(bool(parallel_tab))
        try:
            return await self._screenshot_entry(
                owner, session_id, tab_id, purpose=purpose, scope=scope, ref=ref,
                state=state, actor=actor, actor_id=actor_id,
            )
        finally:
            _PARALLEL_TAB_READ.reset(_parallel_token)

    def previous_capture(self, owner: str, session_id: str, tab_id: str) -> dict[str, Any] | None:
        """The last picture taken of this tab in this process, if any."""
        entry = self._last_captures.get((self.workspace_id(owner, session_id), tab_id))
        return dict(entry) if entry else None

    async def _screenshot_entry(
        self, owner: str, session_id: str, tab_id: str, *, purpose: str, scope: str,
        ref: str, actor: str, actor_id: str, state: str = "",
    ) -> dict:
        from ascended_browser._app.browser_capture import CAPTURE_TIMEOUT_SECONDS

        async with self._pin_tab(self.workspace_id(owner, session_id), tab_id):
            effect_barrier = self._runtime_effect_barrier(owner)
            async with effect_barrier.shared():
                self.assert_tab_access(
                    owner, session_id, tab_id, actor=actor, actor_id=actor_id,
                )
                return await self._read_until_runtime_transition(
                    self._screenshot(
                        owner, session_id, tab_id,
                        purpose=purpose, scope=scope, ref=ref, state=state,
                    ),
                    effect_barrier,
                    operation_name="screenshot",
                    wait_seconds=CAPTURE_TIMEOUT_SECONDS + 10.0,
                )

    async def _screenshot(
        self, owner: str, session_id: str, tab_id: str, *,
        purpose: str, scope: str, ref: str, state: str = "",
    ) -> dict:
        from ascended_browser._app.browser_capture import CaptureError, capture_page, encode_capture
        from ascended_browser._app.browser_click_helpers import resolve_live_target

        record = await self.ensure_awake(owner, session_id)
        tab, page = await self._get_tab(record, tab_id)
        state = str(state or "").strip().lower()
        async with self._read_lock(record.workspace_id, tab_id):
            element = None
            target = None
            if scope == "element" or state:
                root, selector = await resolve_live_target(
                    page, ref, workspace_id=record.workspace_id, tab_id=tab_id,
                    allow_selector=False,
                )
                target = root.locator(selector).first
                if scope == "element":
                    element = target
            undo = None
            if state == "hover":
                try:
                    await asyncio.wait_for(target.hover(timeout=5000), timeout=6.0)
                except Exception as exc:
                    raise CaptureError(
                        f"The pointer could not be placed over {ref} for a hover capture: "
                        f"{type(exc).__name__}. Use browser_extract read=inspect on it to see why."
                    ) from exc

                async def undo():
                    try:
                        await asyncio.wait_for(page.mouse.move(0, 0), timeout=2.0)
                    except Exception:
                        pass
            elif state == "focus":
                try:
                    # Remember what had focus so the capture leaves it as it found it.
                    await asyncio.wait_for(root.evaluate(
                        "() => { window.__odysseusFocusBefore = document.activeElement; }"
                    ), timeout=2.0)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(target.focus(timeout=5000), timeout=6.0)
                except Exception as exc:
                    raise CaptureError(
                        f"{ref} could not take focus for a focus capture: {type(exc).__name__}. "
                        "Use browser_extract read=inspect on it to see why."
                    ) from exc

                async def undo():
                    try:
                        await asyncio.wait_for(root.evaluate("""() => {
                          const before = window.__odysseusFocusBefore; delete window.__odysseusFocusBefore;
                          if (before && before !== document.body && before.isConnected && typeof before.focus === 'function') before.focus();
                          else if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
                        }"""), timeout=3.0)
                    except Exception:
                        pass
            try:
                if state:
                    # Let :hover/:focus transitions and tooltips settle.
                    await asyncio.sleep(0.2)
                capture = await capture_page(page, scope=scope, element=element)
            finally:
                if undo is not None:
                    await undo()
        self._last_captures[(record.workspace_id, tab_id)] = {
            "scope": scope, "state": state, "ref": ref if (scope == "element" or state) else "",
            "data": capture.data, "mime_type": capture.mime_type,
            "width": capture.image_width, "height": capture.image_height,
            "viewport": (capture.metrics.get("viewport_width"), capture.metrics.get("viewport_height")),
            "url": str(getattr(page, "url", "") or tab.url), "at": time.time(),
        }
        runtime = self.backend.runtime(owner)
        runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
        geometry_revision = None
        if runtime is not None:
            try:
                geometry_revision = self._viewport_state(owner, runtime).get("geometry_revision")
            except Exception:
                geometry_revision = None
        return {
            "tab_id": tab_id,
            "url": str(getattr(page, "url", "") or tab.url),
            "title": tab.title,
            "purpose": purpose,
            "scope": scope,
            **({"ref": ref} if scope == "element" or state else {}),
            **({"state": state} if state else {}),
            "capture": {
                **capture.metrics,
                "geometry_revision": geometry_revision,
                "image_width": capture.image_width,
                "image_height": capture.image_height,
                "masked_fields": capture.masked_fields,
                "truncated": capture.truncated,
                # A person holding the live view can resize the window between
                # this picture and the agent's next step.
                "shared_with_person": self._human_native_input_present(owner, runtime_id),
                # Resizes and navigations clear the tab's observation id, so an
                # empty one means earlier refs no longer describe this layout.
                "refs_current": bool(tab.last_observation_id),
            },
            "image": {
                "data": encode_capture(capture),
                "mimeType": capture.mime_type,
                "width": capture.image_width,
                "height": capture.image_height,
            },
        }

    async def emulate(
        self, owner: str, session_id: str, tab_id: str, *,
        color_scheme: str | None = None, reduced_motion: str | None = None,
        forced_colors: str | None = None, offline: bool | None = None, clear: bool = False,
        actor: str = "parent", actor_id: str = "parent",
    ) -> dict[str, Any]:
        """Emulate what a page asks the browser about: color scheme, motion, colors, network.

        Page-level media emulation and context-level offline mode, the parts
        of a device profile Playwright can change on a live persistent
        context. Device scale, locale and timezone are set when the context
        is created and are deliberately not offered here: changing them means
        a new context and the loss of its session. ``clear`` (and viewport
        restore) returns everything to the real values; do that before
        continuing on a third-party site, since a forced scheme is one more
        thing a fingerprint can read.
        """
        workspace_id = self.workspace_id(owner, session_id)
        key = (workspace_id, tab_id)
        async with self._pin_tab(workspace_id, tab_id):
            effect_barrier = self._runtime_effect_barrier(owner)
            async with effect_barrier.shared():
                self.assert_tab_access(owner, session_id, tab_id, actor=actor, actor_id=actor_id)
                record = await self.ensure_awake(owner, session_id)
                tab, page = await self._get_tab(record, tab_id)
                async with self._action_lock(workspace_id, tab_id):
                    wanted = {} if clear else dict(self._tab_media_emulation(key, page))
                    if not clear:
                        for name, value in (
                            ("color_scheme", color_scheme), ("reduced_motion", reduced_motion),
                            ("forced_colors", forced_colors),
                        ):
                            if value is None:
                                continue
                            value = str(value).strip().lower()
                            if value in {"", "none", "no-preference", "null", "default"} and name != "forced_colors":
                                wanted.pop(name, None)
                            elif value in {"", "none", "null", "default"}:
                                wanted.pop(name, None)
                            else:
                                wanted[name] = value
                    try:
                        # `contrast="null"` is load-bearing: left unset, the
                        # Playwright 1.58 driver fills Firefox's
                        # Page.setEmulatedMedia with contrast "no-preference",
                        # a field Camoufox's older Juggler does not know, and
                        # every emulation (dark mode, the capture matrix) failed
                        # with a protocol error. "null" maps to no-override,
                        # which the driver omits from the call.
                        await asyncio.wait_for(page.emulate_media(
                            color_scheme=wanted.get("color_scheme") or "null",
                            reduced_motion=wanted.get("reduced_motion") or "null",
                            forced_colors=wanted.get("forced_colors") or "null",
                            contrast="null",
                        ), timeout=5.0)
                    except Exception as exc:
                        raise WorkspaceError(
                            f"The browser did not accept the media emulation: {describe_exception(exc)}"
                        ) from exc
                    if clear or offline is not None:
                        # Always apply: a context that was relaunched or woken is
                        # online whatever this process remembers, so the memory is
                        # never a reason to skip the call.
                        wanted_offline = bool(offline) and not clear
                        try:
                            await asyncio.wait_for(page.context.set_offline(wanted_offline), timeout=5.0)
                        except Exception as exc:
                            raise WorkspaceError(
                                f"The browser did not accept offline={wanted_offline}: {describe_exception(exc)}"
                            ) from exc
                        if wanted_offline:
                            self._offline_owners[owner] = self._runtime_generation(owner)
                        else:
                            self._offline_owners.pop(owner, None)
                    if wanted:
                        self._emulation[key] = {"page_id": id(page), "media": wanted}
                    else:
                        self._emulation.pop(key, None)
                    # The DOM and its refs are unchanged; what the page shows is not.
                    self._invalidate_observation(workspace_id, tab_id, reason="emulation_changed")
                    emulation = {**wanted, **({"offline": True} if self._offline_for(owner) else {})}
                    result: dict[str, Any] = {
                        "action": "emulate",
                        "emulation": emulation,
                        "cleared": bool(clear),
                        "message": (
                            "Emulation cleared; the page sees the real browser again." if clear and not emulation
                            else "Emulating " + ", ".join(f"{k}={v}" for k, v in emulation.items())
                            + ". Clear it with browser_viewport action=restore (or emulate with clear) when done,"
                            " before continuing on a third-party site."
                        ),
                    }
                    result["page"] = await self._observe_for_result(record, tab, page, lock_held=True)
                    return result

    def _runtime_generation(self, owner: str) -> int:
        try:
            return int(getattr(self.backend.runtime(owner), "generation", 0) or 0)
        except Exception:
            return 0

    def _offline_for(self, owner: str) -> bool:
        """Offline only counts for the context it was applied to."""
        applied = self._offline_owners.get(owner)
        if applied is None:
            return False
        if applied != self._runtime_generation(owner):
            self._offline_owners.pop(owner, None)
            return False
        return True

    def _tab_media_emulation(self, key: tuple[str, str], page: Any | None = None) -> dict[str, str]:
        """Media emulation in force on the tab's current page; a replaced page has none."""
        record = self._emulation.get(key)
        if not record:
            return {}
        current = page if page is not None else self._pages.get(key)
        if current is not None and record.get("page_id") != id(current):
            # The page was replaced (sleep/wake, recovery): its emulation went with it.
            self._emulation.pop(key, None)
            return {}
        return dict(record.get("media") or {})

    def emulation_for(self, owner: str, session_id: str, tab_id: str) -> dict[str, Any]:
        key = (self.workspace_id(owner, session_id), tab_id)
        emulation = self._tab_media_emulation(key)
        if self._offline_for(owner):
            emulation["offline"] = True
        return emulation

    async def screenshot_matrix(
        self, owner: str, session_id: str, tab_id: str, *, presets: list[str],
        color_schemes: list[str] | None = None, actor: str = "parent", actor_id: str = "parent",
    ) -> dict[str, Any]:
        """One viewport capture per preset (and per color scheme), then put everything back."""
        tiles: list[dict[str, Any]] = []
        failures: list[str] = []
        original = self.emulation_for(owner, session_id, tab_id)
        original_offline = bool(original.pop("offline", False))
        schemes = [str(item) for item in (color_schemes or []) if item] or [""]
        restore_error = ""
        # The size the window had: a preset or explicit size the agent set
        # earlier must come back, not the launch size or dynamic sizing.
        before_size: dict[str, Any] = {}
        try:
            runtime = self.backend.runtime(owner)
            runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
            before_size = self._size_mode(runtime_id) if runtime_id else {}
        except Exception:
            before_size = {}
        try:
            for preset in presets:
                sized = await self.viewport(
                    owner, session_id, tab_id, "set", preset=preset, actor=actor, actor_id=actor_id,
                )
                for scheme in schemes:
                    if scheme:
                        await self.emulate(
                            owner, session_id, tab_id, color_scheme=scheme, actor=actor, actor_id=actor_id,
                        )
                    try:
                        shot = await self._screenshot_entry(
                            owner, session_id, tab_id, purpose="inspect", scope="viewport", ref="",
                            actor=actor, actor_id=actor_id,
                        )
                    except Exception as exc:
                        failures.append(f"{preset}{' ' + scheme if scheme else ''}: {describe_exception(exc)}")
                        continue
                    import base64 as _b64
                    tiles.append({
                        "preset": preset,
                        "color_scheme": scheme or None,
                        "width": int(sized.get("width") or 0),
                        "height": int(sized.get("height") or 0),
                        "label": f"{preset} {sized.get('width')}x{sized.get('height')}" + (f" {scheme}" if scheme else ""),
                        "data": _b64.b64decode(shot["image"]["data"]),
                        "mime_type": shot["image"]["mimeType"],
                        "masked_fields": shot.get("capture", {}).get("masked_fields", 0),
                    })
        finally:
            try:
                if before_size.get("mode") == "fixed" and int(before_size.get("width") or 0) > 0:
                    await self.viewport(
                        owner, session_id, tab_id, "set",
                        width=int(before_size["width"]), height=int(before_size["height"]),
                        **({"preset": str(before_size["preset"])} if before_size.get("preset") else {}),
                        actor=actor, actor_id=actor_id,
                    )
                else:
                    await self.viewport(owner, session_id, tab_id, "restore", actor=actor, actor_id=actor_id)
            except Exception as exc:
                restore_error = describe_exception(exc)
            # Whatever the loop changed and whatever restore cleared, the
            # emulation that was in force before comes back exactly.
            # Emulate merges, so clear first: a scheme the loop set must not
            # survive when the original had other media settings but none.
            try:
                wanted = {**original, **({"offline": True} if original_offline else {})}
                if self.emulation_for(owner, session_id, tab_id) != wanted:
                    await self.emulate(owner, session_id, tab_id, clear=True, actor=actor, actor_id=actor_id)
                    if original:
                        await self.emulate(owner, session_id, tab_id, actor=actor, actor_id=actor_id, **original)
                    if original_offline:
                        await self.emulate(owner, session_id, tab_id, actor=actor, actor_id=actor_id, offline=True)
            except Exception as exc:
                restore_error = (restore_error + "; " if restore_error else "") + describe_exception(exc)
        record = self._records.get(self.workspace_id(owner, session_id))
        tab = record.tabs.get(tab_id) if record else None
        return {
            "tab_id": tab_id,
            "url": tab.url if tab else "",
            "title": tab.title if tab else "",
            "scope": "matrix",
            "tiles": tiles,
            "failures": failures,
            "restored": not restore_error,
            **({"restore_error": restore_error} if restore_error else {}),
        }

    async def login(self, owner: str, session_id: str, tab_id: str, *, account_hint: str = "",
                    submit: bool = False, actor: str = "parent", actor_id: str = "parent") -> dict:
        async with self._pin_tab(self.workspace_id(owner, session_id), tab_id):
            effect_barrier = self._runtime_effect_barrier(owner)
            async with effect_barrier.shared():
                self.assert_tab_access(
                    owner, session_id, tab_id, actor=actor, actor_id=actor_id,
                )
                return await self._login(
                    owner, session_id, tab_id,
                    account_hint=account_hint,
                    submit=submit,
                    actor=actor,
                    actor_id=actor_id,
                )

    async def _login(self, owner: str, session_id: str, tab_id: str, *, account_hint: str = "",
                     submit: bool = False, actor: str = "parent", actor_id: str = "parent") -> dict:
        from ascended_browser._app.browser_login_broker import login_fill_page
        record = await self.ensure_awake(owner, session_id)
        tab, page = await self._get_tab(record, tab_id)
        self._assert_may_mutate(tab, actor, actor_id, "login")
        domain = self._domain(tab.url)
        before_url = str(getattr(page, "url", "") or tab.url)
        # With per-tab locking, the per-site lock is what keeps another
        # session's action on this site out of the login cookie comparison.
        domain_lock = self._domain_locks.setdefault((owner_key(record.owner), domain), asyncio.Lock())
        # Site lock before tab lock: the order act and the worker use.
        async with domain_lock, self._action_lock(record.workspace_id, tab_id):
            self._assert_may_mutate(tab, actor, actor_id, "login")
            before_auth = await self._auth_signature(page, domain)
            result = await login_fill_page(page, submit=submit, account_hint=account_hint, owner=owner)
            after_auth = await self._auth_signature(page, domain)
        auth_changed = before_auth != after_auth
        navigated = str(getattr(page, "url", "") or tab.url) != before_url
        submitted = bool(result.get("submitted"))
        if submit and result.get("filled") and not result.get("message"):
            # The result said only "filled": a run could not tell a login that
            # never submitted from one the site refused, and filled again into
            # the rate limit (session 722b3c33, Ciena). Every fill counts toward
            # that limit, since a person-style Enter can follow any of them.
            if not submitted:
                result["message"] = (
                    f"Filled the login fields but did not submit them "
                    f"({result.get('submit_reason') or 'no sign-in control found'}). "
                    "Press the page's sign-in control with browser_act; calling "
                    "browser_login again uses another of the 2 fills allowed per 10 minutes."
                )
            elif not navigated and not auth_changed:
                result["message"] = (
                    "Submitted, but the page and its sign-in state did not change. "
                    "Look for an error on the page before trying again; another "
                    "browser_login uses one of the 2 fills allowed per 10 minutes."
                )
        if auth_changed or submit:
            self._record_domain_disruption(record.workspace_id, domain, "login state changed in another chat")
        # Filling fields alone usually leads directly to another targeted action
        # and does not justify a full DOM walk. Once login submits, navigates, or
        # changes authentication, the new page is precisely what the model needs
        # next; attach it here and eliminate a separate observe round.
        if (
            get_setting("browser_workspace_login_returns_page", True)
            and (submitted or navigated or auth_changed)
        ):
            async with self._action_lock(record.workspace_id, tab_id):
                await self._settle_page(page)
                await self._refresh_tab(record, tab, page, agent=True)
            self.store.save(record)
            result["page"] = await self._observe_for_result(record, tab, page)
        return self._attach_blocker(record, tab, result)

    async def inject_clearance_cookies(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        cookies: list[dict[str, Any]],
        *,
        url: str = "",
        actor: str = "parent",
        actor_id: str = "parent",
    ) -> dict[str, Any]:
        """Transplant harvested Cloudflare clearance cookies and reload the tab.

        The clearance sidecar solves the challenge in its own TLS-matched
        session and returns ``cf_clearance`` / ``__cf_bm`` cookies. Inject them
        into this tab's Camoufox context and reload so the site re-evaluates
        with them present. Best effort: a clearance cookie is bound to the
        sidecar's user-agent and IP, so a rejected transplant is a normal miss
        — the caller re-observes to confirm whether the wall actually cleared.
        """
        record = await self.ensure_awake(owner, session_id)
        tab, page = await self._get_tab(record, tab_id)
        self.assert_tab_access(
            owner, session_id, tab_id, actor=actor, actor_id=actor_id,
        )
        domain = self._domain(tab.url)
        target_url = url or str(getattr(page, "url", "") or tab.url)
        play_cookies: list[dict[str, Any]] = []
        for cookie in cookies or []:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if not name or not value:
                continue
            item: dict[str, Any] = {
                "name": name,
                "value": value,
                "domain": str(cookie.get("domain") or domain or ""),
                "path": str(cookie.get("path") or "/"),
            }
            if cookie.get("expires"):
                item["expires"] = int(cookie["expires"])
            if item["domain"]:
                play_cookies.append(item)
        if not play_cookies:
            return {"injected": 0, "url": target_url}
        effect_barrier = self._runtime_effect_barrier(owner)
        async with effect_barrier.shared("act"):
            # A takeover queued behind this barrier wins: recheck the lease
            # before writing cookies and reloading the user's page.
            self.assert_tab_access(
                owner, session_id, tab_id, actor=actor, actor_id=actor_id,
            )
            self._assert_may_mutate(tab, actor, actor_id, "mutation")
            await page.context.add_cookies(play_cookies)
            try:
                await _bounded(
                    page.goto(target_url, wait_until="domcontentloaded"),
                    float(get_setting("browser_workspace_observe_timeout_seconds", 90) or 90),
                )
            except Exception:
                try:
                    await _bounded(_reload_page(page, timeout=15_000), 15.0)
                except Exception:
                    pass
        self._invalidate_observation(record.workspace_id, tab.tab_id)
        self._record_domain_disruption(record.workspace_id, domain, "cloudflare clearance injected")
        self._acknowledge_worker_session(record.workspace_id, tab_id, domain)
        await self._refresh_tab(record, tab, page, agent=True)
        self.store.save(record)
        return {"injected": len(play_cookies), "url": target_url}

    async def challenge_widget_state(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        actor: str = "parent",
        actor_id: str = "parent",
    ) -> dict[str, Any]:
        """Read embedded Turnstile/reCAPTCHA/hCaptcha widget state. Read-only."""
        from ascended_browser._app.browser_challenge_widget import public_state, widget_state

        record = await self.ensure_awake(owner, session_id)
        _, page = await self._get_tab(record, tab_id)
        async with self._runtime_effect_barrier(owner).shared("observe"):
            self.assert_tab_access(
                owner, session_id, tab_id, actor=actor, actor_id=actor_id,
            )
            return public_state(await widget_state(page))

    async def press_challenge_widget(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        actor: str = "parent",
        actor_id: str = "parent",
    ) -> dict[str, Any]:
        """Press an embedded widget's checkbox once; never touches a puzzle."""
        from ascended_browser._app.browser_challenge_widget import press_checkbox

        record = await self.ensure_awake(owner, session_id)
        tab, page = await self._get_tab(record, tab_id)
        async with self._runtime_effect_barrier(owner).shared("act"):
            self.assert_tab_access(
                owner, session_id, tab_id, actor=actor, actor_id=actor_id,
            )
            self._assert_may_mutate(tab, actor, actor_id, "mutation")
            result = await press_checkbox(page)
        if result.get("pressed"):
            self._invalidate_observation(record.workspace_id, tab.tab_id)
        return result

    def _view_has_control(self, view: LiveViewSession, tab: TabRecord) -> bool:
        lease = getattr(tab, "lease", None)
        if not lease or lease.actor != "user" or lease.actor_id != view.owner:
            return False
        if lease.expires_at is not None and lease.expires_at <= time.time():
            return False
        controller = self._liveview_controller_by_runtime.get(view.runtime_id)
        runtime = self.backend.runtime(view.owner)
        if self._live_capabilities(runtime).get("control_fencing"):
            # A modern daemon grant is the point-of-effect authority. Never infer
            # a controller from the durable account lease when that grant has not
            # been bound to a particular viewer yet.
            return bool(controller) and secrets.compare_digest(controller, view.view_id)
        if controller:
            return secrets.compare_digest(controller, view.view_id)
        # A protocol-v1 daemon cannot fence already-forwarded input during a
        # viewer transfer. Preserve its historical takeover only when exactly one
        # usable viewer exists; ambiguous legacy viewers all remain read-only.
        candidates = self.live_views.for_runtime(view.runtime_id)
        return len(candidates) == 1 and secrets.compare_digest(
            candidates[0].view_id, view.view_id,
        )

    @staticmethod
    def _liveview_liveness_key(
        view_id: str, connection_generation: int,
    ) -> tuple[str, int]:
        """Identify one ephemeral transport without making it durable state."""
        return (str(view_id or ""), int(connection_generation or 0))

    def _cancel_liveview_liveness(
        self,
        view_id: str,
        *,
        connection_generation: int | None = None,
    ) -> None:
        """Forget liveness work for a view or one superseded socket generation."""
        target_view_id = str(view_id or "")
        target_generation = (
            None if connection_generation is None else int(connection_generation)
        )
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        keys = [
            key for key in self._liveview_liveness_tasks
            if key[0] == target_view_id
            and (target_generation is None or key[1] == target_generation)
        ]
        # A route may have recorded activity before an event loop was present,
        # so include orphaned stamps too rather than letting them accumulate.
        keys.extend(
            key for key in self._liveview_client_activity
            if key not in keys
            and key[0] == target_view_id
            and (target_generation is None or key[1] == target_generation)
        )
        keys.extend(
            key for key in self._liveview_liveness_expiring
            if key not in keys
            and key[0] == target_view_id
            and (target_generation is None or key[1] == target_generation)
        )
        for key in keys:
            task = self._liveview_liveness_tasks.pop(key, None)
            if task is not None and task is not current_task:
                task.cancel()
            self._liveview_client_activity.pop(key, None)
            self._liveview_liveness_expiring.discard(key)

    def note_live_view_client_activity(
        self,
        view: LiveViewSession,
        *,
        connection_generation: int,
    ) -> bool:
        """Record one authenticated current-socket signal for half-open expiry.

        This is intentionally a manager-owned, non-durable liveness signal.
        Routes call it only after WebSocket authentication and parsing a known
        protocol message; stale sockets cannot extend the current controller.
        """
        generation = int(connection_generation or 0)
        if (
            generation <= 0
            or generation != int(getattr(view, "connection_generation", 0) or 0)
            or not self.live_views.current_connected(view)
            or self._liveview_controller_by_runtime.get(view.runtime_id) != view.view_id
        ):
            return False
        key = self._liveview_liveness_key(view.view_id, generation)
        # Once the timeout has committed to fencing this exact generation, a
        # late message cannot resurrect it halfway through native revocation.
        # A reconnect creates a new generation and remains fully eligible.
        if key in self._liveview_liveness_expiring:
            return False
        self._liveview_client_activity[key] = time.monotonic()
        task = self._liveview_liveness_tasks.get(key)
        if task is not None and not task.done():
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous test/bootstrap callers have no lifecycle task to
            # own. Production WebSocket routes always run in an event loop.
            return True
        self._liveview_liveness_tasks[key] = loop.create_task(
            self._expire_unresponsive_live_view(
                view.view_id, generation,
            )
        )
        return True

    async def _expire_unresponsive_live_view(
        self,
        view_id: str,
        connection_generation: int,
    ) -> None:
        """Fence and hand back a current controller after missed heartbeats.

        A normal WebSocket close uses ``live_view_disconnected`` and retains
        reconnect grace. This task only handles the distinct case in which the
        registry still considers the *current* socket connected but no
        authenticated route signal has arrived for the bounded interval.
        """
        key = self._liveview_liveness_key(view_id, connection_generation)
        try:
            while True:
                activity_at = self._liveview_client_activity.get(key)
                if activity_at is None:
                    return
                remaining = _LIVE_VIEW_CLIENT_LIVENESS_SECONDS - (
                    time.monotonic() - activity_at
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                # This synchronous transition is the liveness cutoff. A
                # current-generation message after it has begun must reconnect
                # rather than race native revocation and a durable handback.
                self._liveview_liveness_expiring.add(key)
                await self._expire_unresponsive_live_controller(
                    view_id,
                    connection_generation,
                    activity_at=activity_at,
                )
                return
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning(
                "Could not reconcile an unresponsive browser live-view controller",
                exc_info=True,
            )
        finally:
            current_task = asyncio.current_task()
            if self._liveview_liveness_tasks.get(key) is current_task:
                self._liveview_liveness_tasks.pop(key, None)
                self._liveview_client_activity.pop(key, None)
            self._liveview_liveness_expiring.discard(key)

    async def _expire_unresponsive_live_controller(
        self,
        view_id: str,
        connection_generation: int,
        *,
        activity_at: float,
    ) -> bool:
        """Run the half-open handback inside the normal authority transition."""
        view = self.live_views.find(view_id)
        if view is None:
            return False
        try:
            async with self._liveview_transition(
                view.owner,
                view.workspace_id,
                view.tab_id,
                label="liveview_liveness_expiry",
            ):
                current = self.live_views.find(view_id)
                if current is None:
                    return False
                record = self._records.get(current.workspace_id) or self.record(
                    current.owner, current.session_id,
                )
                tab = record.tabs.get(current.tab_id)
                if tab is None:
                    return False
                return await self._fence_then_return_live_user_lease_locked(
                    current,
                    record,
                    tab,
                    expected_connection_generation=connection_generation,
                    expected_lease_epoch=int(
                        getattr(tab.lease, "generation", 0) or 0
                    ),
                    note=(
                        "Live browser connection became unresponsive; control "
                        "returned to the agent."
                    ),
                    liveness_activity_at=activity_at,
                )
        except (LiveViewUnavailable, TabLifecycleError):
            # A competing close/restart owns its own reconciliation path.
            return False

    async def _fence_then_return_live_user_lease_locked(
        self,
        view: LiveViewSession,
        record: WorkspaceRecord,
        tab: TabRecord,
        *,
        expected_connection_generation: int,
        expected_lease_epoch: int,
        note: str,
        require_expired_lease: bool = False,
        liveness_activity_at: float | None = None,
    ) -> bool:
        """Fence native input, then use the canonical durable handback path.

        The caller already owns ``_liveview_transition``. Every condition is
        checked immediately before the await and again after it, so an old
        liveness worker or expired-controller recovery cannot release a newer
        socket/controller that arrived while the daemon request was in flight.
        """
        current = self.live_views.find(view.view_id)
        if (
            current is None
            or current.closed
            or current.owner != view.owner
            or current.runtime_id != view.runtime_id
            or current.workspace_id != record.workspace_id
            or current.tab_id != tab.tab_id
            or int(current.connection_generation) != int(expected_connection_generation)
            or self._liveview_controller_by_runtime.get(current.runtime_id) != current.view_id
        ):
            return False
        lease = tab.lease
        if (
            lease is None
            or lease.actor != "user"
            or lease.actor_id != current.owner
            or int(getattr(lease, "generation", 0) or 0) != int(expected_lease_epoch)
            or (
                require_expired_lease
                and (lease.expires_at is None or lease.expires_at > time.time())
            )
        ):
            return False
        if liveness_activity_at is not None:
            key = self._liveview_liveness_key(
                current.view_id, expected_connection_generation,
            )
            if (
                key not in self._liveview_liveness_expiring
                or not self.live_views.current_connected(current)
                or self._liveview_client_activity.get(key) != liveness_activity_at
                or time.monotonic() - liveness_activity_at
                < _LIVE_VIEW_CLIENT_LIVENESS_SECONDS
            ):
                return False
        await self.fence_live_control(
            current,
            tab,
            enabled=False,
            lease_epoch=expected_lease_epoch,
            connection_generation=expected_connection_generation,
        )
        # A new socket can connect while the daemon revocation awaits. Its
        # reconnect transaction will re-establish native authority; never turn
        # that successor's durable lease back into an agent lease.
        current = self.live_views.find(view.view_id)
        lease = tab.lease
        if (
            current is None
            or int(current.connection_generation) != int(expected_connection_generation)
            or self._liveview_controller_by_runtime.get(view.runtime_id) != view.view_id
            or lease is None
            or lease.actor != "user"
            or lease.actor_id != view.owner
            or int(getattr(lease, "generation", 0) or 0) != int(expected_lease_epoch)
            or (
                require_expired_lease
                and (lease.expires_at is None or lease.expires_at > time.time())
            )
        ):
            return False
        self.release_control(
            view.owner,
            record.session_id,
            tab.tab_id,
            note=note,
            preserve_hold=True,
            _fence_daemon=False,
        )
        current.pressed_keys.clear()
        self._liveview_wheel_states.pop(current.view_id, None)
        if self._liveview_viewport_owner_by_runtime.get(view.runtime_id) == view.view_id:
            # A half-open controller cannot retain a resize claim merely
            # because the registry has not seen TCP teardown yet.
            self._liveview_viewport_owner_by_runtime.pop(view.runtime_id, None)
        return True

    async def _reconcile_expired_live_controller_locked(
        self,
        runtime_id: str,
        *,
        owner: str,
        target_record: WorkspaceRecord,
        target_tab: TabRecord,
        note: str,
    ) -> bool:
        """Return one expired *other* controller before it blocks a transition.

        This is intentionally narrower than a lease sweep: it only acts on the
        manager's current controller and it still requires native fencing before
        the canonical handback. A live, non-expired controller remains a hard
        cross-tab conflict.
        """
        controller_id = self._liveview_controller_by_runtime.get(runtime_id, "")
        controller = self.live_views.find(controller_id) if controller_id else None
        if (
            controller is None
            or controller.closed
            or controller.owner != owner
            or controller.runtime_id != runtime_id
        ):
            return False
        record = self._records.get(controller.workspace_id) or self.record(
            owner, controller.session_id,
        )
        tab = record.tabs.get(controller.tab_id)
        if tab is None:
            return False
        same_tab = (
            record.workspace_id == target_record.workspace_id
            and tab.tab_id == target_tab.tab_id
        )
        same_group = (
            record.workspace_id == target_record.workspace_id
            and self._same_takeover_group(tab, target_tab)
        )
        lease = tab.lease
        if (
            same_tab
            or same_group
            or lease is None
            or lease.actor != "user"
            or lease.actor_id != owner
            or lease.expires_at is None
            or lease.expires_at > time.time()
        ):
            return False
        return await self._fence_then_return_live_user_lease_locked(
            controller,
            record,
            tab,
            expected_connection_generation=int(controller.connection_generation),
            expected_lease_epoch=int(getattr(lease, "generation", 0) or 0),
            note=note,
            require_expired_lease=True,
        )

    async def _reconcile_expired_uncontrolled_lease_locked(
        self,
        requester: LiveViewSession,
        record: WorkspaceRecord,
        tab: TabRecord,
        *,
        target_record: WorkspaceRecord,
        target_tab: TabRecord,
        note: str,
    ) -> bool:
        """Safely clear an expired lease whose controller entry has vanished.

        A missing manager controller is not a reason to treat the old durable
        user lease as harmless. The authenticated requesting view fences the
        runtime first; the normal handback then clears the stale durable state.
        Do not do this while another known controller exists.
        """
        lease = tab.lease
        if (
            lease is None
            or lease.actor != "user"
            or lease.actor_id != requester.owner
            or lease.expires_at is None
            or lease.expires_at > time.time()
        ):
            return False
        same_tab = (
            record.workspace_id == target_record.workspace_id
            and tab.tab_id == target_tab.tab_id
        )
        same_group = (
            record.workspace_id == target_record.workspace_id
            and self._same_takeover_group(tab, target_tab)
        )
        if same_tab or same_group:
            return False
        runtime_id = requester.runtime_id
        controller_id = self._liveview_controller_by_runtime.get(runtime_id, "")
        if controller_id and self.live_views.find(controller_id) is not None:
            return False
        if not self._live_capabilities(self.backend.runtime(requester.owner)).get(
            "control_fencing",
        ):
            return False
        expected_generation = int(requester.connection_generation)
        expected_epoch = int(getattr(lease, "generation", 0) or 0)
        await self.fence_live_control(
            requester,
            tab,
            enabled=False,
            lease_epoch=expected_epoch,
            connection_generation=expected_generation,
        )
        lease = tab.lease
        if (
            int(requester.connection_generation) != expected_generation
            or self._liveview_controller_by_runtime.get(runtime_id, "") != controller_id
            or lease is None
            or lease.actor != "user"
            or lease.actor_id != requester.owner
            or int(getattr(lease, "generation", 0) or 0) != expected_epoch
            or lease.expires_at is None
            or lease.expires_at > time.time()
        ):
            return False
        self.release_control(
            requester.owner,
            record.session_id,
            tab.tab_id,
            note=note,
            preserve_hold=True,
            _fence_daemon=False,
        )
        if self._liveview_viewport_owner_by_runtime.get(runtime_id) == requester.view_id:
            self._liveview_viewport_owner_by_runtime.pop(runtime_id, None)
        return True

    async def _recover_orphaned_live_lease_for_explicit_switch(
        self, view: LiveViewSession, record: WorkspaceRecord, tab: TabRecord,
    ) -> bool:
        """Return a provably unowned durable user lease before a tab switch.

        The manager lease intentionally outlives a websocket briefly so a
        transient reconnect cannot let the agent type under the user.  It must
        not, however, become permanent when fencing has already lost its
        controller entry.  An explicit selection from the same authenticated
        viewer is the narrow recovery point: no connected controller exists,
        native input is revoked first, then the normal recorded return path is
        used.  A real controller (including one in reconnect grace) remains a
        hard conflict and is never displaced by a spectator's tab click.
        """
        lease = tab.lease
        if (
            lease is None or lease.actor != "user" or lease.actor_id != view.owner
            or not self._live_capabilities(self.backend.runtime(view.owner)).get("control_fencing")
        ):
            return False
        controller_id = self._liveview_controller_by_runtime.get(view.runtime_id, "")
        if controller_id and self.live_views.find(controller_id) is not None:
            return False
        try:
            await self.fence_live_control(view, tab, enabled=False)
        except Exception as exc:
            # Do not convert a failed native revocation into an apparent
            # handback. The old daemon grant could still accept input.
            raise WorkspaceError(
                "Could not safely recover stale browser control; reopen the live view before switching"
            ) from exc
        # A dangling map entry has no capability behind it. Clear it before
        # using the canonical handback path so its viewport claim cannot be
        # resurrected by release_control's compatibility bookkeeping.
        if controller_id:
            self._liveview_controller_by_runtime.pop(view.runtime_id, None)
        self.release_control(
            view.owner, record.session_id, tab.tab_id,
            note="Recovered stale live control while switching browser tabs.",
            preserve_hold=True,
            _fence_daemon=False,
        )
        log.warning(
            "Recovered orphaned live user-control lease for workspace=%s tab=%s during explicit tab switch",
            record.workspace_id, tab.tab_id,
        )
        return True

    def _viewport_owner_id(self, runtime_id: str) -> str:
        """Return the current resize owner, clearing stale spectator claims."""
        runtime_id = str(runtime_id or "")
        if not runtime_id:
            return ""
        controller = self._liveview_controller_by_runtime.get(runtime_id, "")
        if controller:
            # Input authority is stronger than a read-only resize claim and
            # remains authoritative throughout the existing reconnect grace.
            self._liveview_viewport_owner_by_runtime[runtime_id] = controller
            return controller
        owner_id = self._liveview_viewport_owner_by_runtime.get(runtime_id, "")
        if not owner_id:
            return ""
        owner_view = self.live_views.find(owner_id)
        if (
            owner_view is None
            or owner_view.runtime_id != runtime_id
            or not self.live_views.current_connected(owner_view)
        ):
            self._liveview_viewport_owner_by_runtime.pop(runtime_id, None)
            return ""
        return owner_id

    def _view_has_viewport_control(self, view: LiveViewSession) -> bool:
        owner_id = self._viewport_owner_id(view.runtime_id)
        return bool(owner_id) and secrets.compare_digest(owner_id, view.view_id)

    def _live_capabilities(self, runtime: Any | None) -> dict[str, bool]:
        runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
        live_runtime = self.liveview.runtime(runtime_id) if runtime_id else None
        daemon = dict(getattr(live_runtime, "capabilities", {}) or {})
        return {
            "navigation": bool(runtime_id),
            "resize_window": bool(
                getattr(runtime, "dynamic_viewport", False)
                and daemon.get("resize_window", False)
                and daemon.get("window_binding", False)
            ),
            "control_fencing": bool(daemon.get("control_fencing", False)),
            "geometry_revision": bool(daemon.get("geometry_revision", False)),
            "control_revision": bool(daemon.get("control_revision", False)),
            "release_all": bool(daemon.get("release_all", False)),
            "direct_input": bool(daemon.get("direct_input", False)),
            # The manager relays after_sequence and the daemon enforces it; the
            # client may skip its round-trip fence only when both are true, so
            # this is a manager-owned name distinct from the daemon's own flag.
            "causal_input_relay": bool(
                daemon.get("direct_input", False) and daemon.get("causal_input", False)
            ),
            # Xvfb cannot consume a host /dev/uinput high-resolution wheel.
            # Route wheel alone through the already-authorized browser
            # protocol path for exact DOM pixels (with trusted-wheel fallback);
            # pointer/buttons stay on liveviewd's direct RTC lane. The manager
            # still revalidates page and lease at effect.
            "pixel_wheel": bool(
                runtime_id
                and get_setting("browser_liveview_pixel_wheel", True) is True
            ),
            "window_binding": bool(daemon.get("window_binding", False)),
        }

    def _adopt_liveview_geometry_revision(self, runtime_id: str) -> None:
        live_runtime = self.liveview.runtime(runtime_id)
        if live_runtime is not None:
            self._liveview_geometry_revision[runtime_id] = int(
                getattr(live_runtime, "geometry_revision", 0) or 0
            )

    def _frame_meta(
        self,
        tab: TabRecord,
        *,
        view: LiveViewSession | None = None,
        runtime: Any | None = None,
    ) -> dict:
        """Everything the live view needs beside the WebRTC media track."""
        lease = getattr(tab, "lease", None)
        live_runtime = self.liveview.runtime(view.runtime_id) if view is not None else None
        control = self._view_has_control(view, tab) if view is not None else False
        viewport_owner = self._viewport_owner_id(view.runtime_id) if view is not None else ""
        viewport_control = bool(
            view is not None
            and viewport_owner
            and secrets.compare_digest(viewport_owner, view.view_id)
        )
        return {
            "session_id": view.session_id if view is not None else "",
            "tab_id": tab.tab_id,
            "selection_revision": int(getattr(view, "selection_revision", 0) or 0),
            "selection_pending": bool(getattr(view, "selection_pending", False)),
            "title": tab.title,
            "url": tab.url,
            "owner_kind": tab.owner_kind,
            "owner_id": tab.owner_id,
            **(
                self._tab_activity_projection(
                    self.workspace_id(view.owner, view.session_id), tab.tab_id,
                )
                if view is not None
                else {"agent_active": False, "agent_activity_count": 0, "agent_activity_actors": []}
            ),
            # Why a human was pulled in, if they were. The takeover view shows
            # this beside the frame; without it the user has to go hunting in
            # the transcript for what they are being asked to approve.
            "hold": asdict(tab.hold) if tab.hold else None,
            "driver": getattr(lease, "actor", "") or "",
            "lease_epoch": int(getattr(lease, "generation", 0) or 0),
            "control": control,
            "viewport_control": viewport_control,
            "viewport_owner_present": bool(viewport_owner),
            "controller_id": view.view_id if control and view is not None else "",
            "connection_generation": int(getattr(view, "connection_generation", 0) or 0),
            "runtime_generation": int(getattr(view, "runtime_generation", 0) or 0),
            # The daemon's effect-boundary generation. It is separate from
            # the durable lease epoch and travels with direct input so a late
            # packet from a revoked grant cannot be mistaken for a current one.
            "control_revision": int(getattr(live_runtime, "control_revision", 0) or 0),
            "geometry_revision": int(getattr(live_runtime, "geometry_revision", 0) or 0),
            "viewport": {
                "width": int(getattr(runtime, "viewport_width", 0) or 0),
                "height": int(getattr(runtime, "viewport_height", 0) or 0),
                "outer_width": int(getattr(live_runtime, "window_width", 0) or 0),
                "outer_height": int(getattr(live_runtime, "window_height", 0) or 0),
            "geometry_revision": int(getattr(live_runtime, "geometry_revision", 0) or 0),
            "audio": bool(getattr(live_runtime, "audio_source", "") or ""),
            },
            "capabilities": self._live_capabilities(runtime),
            "resize_supported": self._live_capabilities(runtime)["resize_window"],
            "size_mode": self._size_mode(view.runtime_id) if view is not None else None,
        }

    def _retire_live_spectator(self, view: LiveViewSession) -> None:
        """Retire one non-controller capability and its ephemeral claims."""
        controller = self._liveview_controller_by_runtime.get(view.runtime_id, "")
        if controller and secrets.compare_digest(controller, view.view_id):
            return
        pending = self._liveview_disconnect_tasks.pop(view.view_id, None)
        if pending:
            pending.cancel()
        self._cancel_liveview_liveness(view.view_id)
        self._liveview_wheel_states.pop(view.view_id, None)
        viewport_owner = self._liveview_viewport_owner_by_runtime.get(
            view.runtime_id, "",
        )
        if viewport_owner and secrets.compare_digest(viewport_owner, view.view_id):
            self._liveview_viewport_owner_by_runtime.pop(view.runtime_id, None)
        self.live_views.retire(view)

    def _retire_own_blockers_and_move_blockers(
        self, owner: str, blockers: list[LiveViewSession],
    ) -> list[LiveViewSession]:
        """Same-owner spectators move with the person, not against them.

        A stale connected capability of this person's own browser session
        (a browser tab they moved away from but whose socket lingers) must
        not turn a routine selection into "another viewer is watching".
        Retire their capabilities like stale previous selections; only a
        genuinely foreign viewer keeps the raise.
        """
        remaining: list[LiveViewSession] = []
        for candidate in blockers:
            if str(getattr(candidate, "owner", "") or "") == owner:
                self._retire_live_spectator(candidate)
            else:
                remaining.append(candidate)
        return remaining


    def _other_live_selection_blockers(
        self,
        runtime_id: str,
        *,
        ignore_view_ids: set[str] | None = None,
    ) -> list[LiveViewSession]:
        """Connected spectators and the controller can hold shared selection."""
        ignored = ignore_view_ids or set()
        controller = self._liveview_controller_by_runtime.get(runtime_id, "")
        blockers = []
        for candidate in self.live_views.for_runtime(runtime_id):
            if candidate.view_id in ignored:
                continue
            if (
                self.live_views.current_connected(candidate)
                or self.live_views.pending_connection(candidate)
                or (controller and secrets.compare_digest(controller, candidate.view_id))
            ):
                blockers.append(candidate)
        return blockers

    async def create_live_view(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        replace_view_id: str = "",
    ) -> dict:
        """Select one page on the owner's real display and mint an iframe capability."""
        # A cold start kicks the canonical browser wake (same task browser_open
        # awaits; deduped and reused) and rides it for a bounded time. Only a
        # wake that outlasts that answers a calm "warming" for the viewer to
        # poll: blocking the mint for a whole slow launch outran the client's
        # request deadline and turned a calm wait into retry churn.
        record = self.record(owner, session_id)
        if record.status != "active" or self.backend.runtime(owner) is None:
            # prewarm dedupes its own in-flight task. Do not skip it on a
            # stored "waking": a manifest saved by a process that stopped
            # mid-wake loads as "waking" with no task here, and the viewer
            # then polled "Waking browser tab…" forever.
            self.prewarm(owner, session_id)
            wake = self._prewarm_tasks.get(record.workspace_id)
            if wake is not None:
                await asyncio.wait({wake}, timeout=_LIVE_VIEW_WAKE_WAIT_SECONDS)
            record = self.record(owner, session_id)
            if record.status != "active" or self.backend.runtime(owner) is None:
                raise LiveViewUnavailable("Waking browser tab…", error_kind="warming")
        record = await self.ensure_awake(owner, session_id)
        # Reviving a missing page may itself create/focus a native tab and then
        # restore the current selection. Do that before entering the
        # non-reentrant selection transaction; the resident page is revalidated
        # under its action lock below.
        await self._revive_for_viewer(record, tab_id)
        key = owner_key(owner)
        # Selection changes one shared native Firefox window. Keep the check,
        # activation, selected-tab publication, and capability minting in the
        # same owner transaction so a concurrently opening spectator cannot
        # move X11 focus after a controller grant commits for another tab.
        async with self._liveview_transition(
            owner, record.workspace_id, tab_id, label="create_live_view",
        ):
            runtime = self.backend.runtime(owner)
            if getattr(runtime, "presentation", "") == "embedded":
                raise LiveViewUnavailable(
                    "This browser is shown in the Ascended desktop app, not as a live stream.",
                    error_kind="embedded_presentation",
                )
            runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "") if runtime else ""
            if not runtime_id:
                await self._recover_capture_for_owner(
                    owner, record, requested_tab_id=tab_id,
                )
                runtime = self.backend.runtime(owner)
                runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "") if runtime else ""
            if not runtime_id:
                reason, kind = self._capture_unavailable_reason(record)
                raise LiveViewUnavailable(reason, error_kind=kind)
            self._capture_relaunch_failures.pop(record.workspace_id, None)
            capabilities = self._live_capabilities(runtime)
            controller_id = self._liveview_controller_by_runtime.get(runtime_id, "")
            controller_view = self.live_views.find(controller_id) if controller_id else None
            replacement_view = self.live_views.find(replace_view_id) if replace_view_id else None
            if replacement_view is not None and (
                replacement_view.closed
                or not secrets.compare_digest(replacement_view.owner, owner)
                or replacement_view.runtime_id != runtime_id
            ):
                replacement_view = None
            reconnect_view = (
                controller_view
                if (
                    controller_view is not None
                    and controller_view.usable()
                    and not self.live_views.current_connected(controller_view)
                    and secrets.compare_digest(controller_view.owner, owner)
                    and controller_view.session_id == session_id
                    and controller_view.workspace_id == record.workspace_id
                    and controller_view.tab_id == tab_id
                    and controller_view.runtime_id == runtime_id
                    and int(controller_view.runtime_generation)
                    == int(getattr(runtime, "generation", 0) or 0)
                )
                else None
            )
            # An explicit remount of the current controller is the same viewer,
            # even if its old WebSocket teardown has not reached the server yet.
            # Reusing the capability preserves the durable takeover and rotates
            # connection generation when the replacement iframe connects.
            if (
                reconnect_view is None
                and replacement_view is not None
                and controller_view is replacement_view
                and replacement_view.session_id == session_id
                and replacement_view.workspace_id == record.workspace_id
                and replacement_view.tab_id == tab_id
                and int(replacement_view.runtime_generation)
                == int(getattr(runtime, "generation", 0) or 0)
            ):
                reconnect_view = replacement_view
            existing_views = self.live_views.for_runtime(runtime_id)
            if not capabilities.get("control_fencing"):
                replaceable = [
                    candidate for candidate in existing_views
                    if (
                        candidate.workspace_id == record.workspace_id
                        and candidate.tab_id == tab_id
                        and candidate.connections == 0
                    )
                ]
                if len(replaceable) != len(existing_views):
                    raise WorkspaceError(
                        "This live-view runtime supports only one viewer until its daemon is upgraded"
                    )
                controller = self._liveview_controller_by_runtime.get(runtime_id, "")
                if controller and any(
                    secrets.compare_digest(controller, candidate.view_id)
                    for candidate in replaceable
                ) and reconnect_view is None:
                    # Registry.create below replaces these unused capabilities.
                    # Do not leave their stale controller id blocking the one
                    # legacy viewer that is safe to admit.
                    self._liveview_controller_by_runtime.pop(runtime_id, None)
            selected = self._liveview_selected.get(key)
            if (
                selected is not None
                and selected != (record.workspace_id, tab_id)
            ):
                ignored = {
                    replacement_view.view_id
                    for replacement_view in [replacement_view]
                    if replacement_view is not None
                    and replacement_view is not controller_view
                }
                blockers = self._retire_own_blockers_and_move_blockers(
                    owner,
                    self._other_live_selection_blockers(
                        runtime_id, ignore_view_ids=ignored,
                    ),
                )
                if blockers:
                    raise WorkspaceError(
                        "This shared browser is already being watched in another tab"
                    )
                # Capabilities for the previous selection that never connected
                # or have already disconnected must not block a new device for
                # five minutes or reconnect later to the newly selected page.
                for candidate in existing_views:
                    if candidate is reconnect_view:
                        continue
                    self._retire_live_spectator(candidate)
            if replacement_view is not None and replacement_view is not reconnect_view:
                self._retire_live_spectator(replacement_view)
            tab, page = await self._get_tab(record, tab_id, revive=False)
            await self.backend.prepare_user_control(owner, page)
            self._adopt_liveview_geometry_revision(runtime_id)
            self._liveview_selected[key] = (record.workspace_id, tab_id)
            # Reopening the same controlled tab during transport grace must use
            # the existing capability. Minting a replacement closes the old
            # view, causing its grace task to exit while its durable user lease
            # and controller mapping remain stranded indefinitely.
            view = reconnect_view or self.live_views.create(
                owner=owner, session_id=session_id, workspace_id=record.workspace_id,
                tab_id=tab_id, runtime_id=runtime_id,
                runtime_generation=int(getattr(runtime, "generation", 0) or 0),
            )
            if (
                tab.lease and tab.lease.actor == "user" and tab.lease.actor_id == owner
                and view.runtime_id not in self._liveview_controller_by_runtime
                and not capabilities.get("control_fencing")
            ):
                # A live fixed-size takeover created before controller fencing
                # is adopted by its first replacement viewer during rolling upgrade.
                self._liveview_controller_by_runtime[view.runtime_id] = view.view_id
            self._touch_tab_activity(record, tab, human=True)
            self.store.save(record)
            self._emit_browser_event(record, "selection", tab_id)
            control = self._view_has_control(view, tab)
            return {
                "view_id": view.view_id,
                "embed_url": f"/browser-live/embed/{view.view_id}",
                "expires_at": view.connect_expires_at,
                "state": "live-user-control" if control else "live-readonly",
                **self._frame_meta(tab, view=view, runtime=runtime),
            }

    async def _revive_for_viewer(self, record: WorkspaceRecord, tab_id: str) -> None:
        """Bring a selected tab's page back for a viewer, healing on its own.

        A person selecting a tab asks to see it, so a lost page is reloaded
        (including a draft the old browser already destroyed). The load runs
        in its own task: a slow site answers "warming" and the viewer's retry
        joins the same load instead of cancelling and restarting it, and a
        failed load is retried by the next poll rather than parked as an
        error the person has to clear by hand.
        """
        tab = record.tabs.get(tab_id)
        key = (record.workspace_id, tab_id)
        opening = self._opening_tabs.get(key)
        if opening is not None and not opening.done() and self._pages.get(key) is None:
            finished, _pending = await asyncio.wait({opening}, timeout=_LIVE_VIEW_REVIVE_WAIT_SECONDS)
            if not finished:
                raise LiveViewUnavailable("Opening browser tab…", error_kind="warming")
            # The original open owns creation; never launch a duplicate page.
            opening.result()
            tab = record.tabs.get(tab_id)
        task = self._viewer_revive_tasks.get(key)
        # Page creation registers its identity before the wake navigation
        # finishes. A retry must still join that pending load; otherwise it
        # can bind a blank page that the first load may later retire on failure.
        if (tab is not None and self._pages.get(key) is not None
                and (task is None or task.done())):
            return
        if task is None or task.done():
            task = asyncio.create_task(self._get_tab(
                record, tab_id, settle=False, discard_lost_draft=True,
            ))
            self._viewer_revive_tasks[key] = task

            def _finished(done: asyncio.Task, key=key) -> None:
                if self._viewer_revive_tasks.get(key) is done:
                    self._viewer_revive_tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(_finished)
        finished, _pending = await asyncio.wait({task}, timeout=_LIVE_VIEW_REVIVE_WAIT_SECONDS)
        if not finished:
            raise LiveViewUnavailable("Loading browser tab…", error_kind="warming")
        try:
            task.result()
        except TabLifecycleError as exc:
            if exc.error_kind != "page_reload_failed":
                raise
            # A failed load leaves the tab revivable; the viewer's warming
            # poll starts a fresh attempt instead of stopping at an error.
            raise LiveViewUnavailable(
                f"Reloading browser tab… {exc}", error_kind="warming",
            ) from exc

    async def switch_live_view(
        self, view_id: str, owner: str, session_id: str, tab_id: str,
        *, selection_revision: int = 0, connection_generation: int = 0,
    ) -> dict:
        """Select a tab on the same fingerprint-pinned display, keeping WebRTC warm."""
        view, _record, _tab = self.resolve_live_view(view_id, owner)
        if connection_generation and int(connection_generation) != view.connection_generation:
            raise WorkspaceError("Live-view connection generation is stale")
        revision = int(selection_revision or 0)
        if revision < 0:
            raise WorkspaceError("Browser selection revision is invalid")
        if revision and revision == view.selection_revision and (
            session_id != view.selection_requested_session_id
            or tab_id != view.selection_requested_tab_id
        ):
            raise WorkspaceError("Browser selection revision targets another tab")
        if revision > view.selection_revision:
            view.selection_revision = revision
            view.selection_requested_session_id = session_id
            view.selection_requested_tab_id = tab_id
            view.selection_pending = True
        if view.selection_revision and revision < view.selection_revision:
            return {
                "view_id": view.view_id,
                "selection_superseded": True,
                **await self.live_view_state(view_id, owner),
            }
        try:
            async with asyncio.timeout(8.0):
                return await self._switch_live_view_selected(
                    view_id, owner, session_id, tab_id,
                    selection_revision=revision,
                    connection_generation=connection_generation,
                )
        except TimeoutError as exc:
            raise TabLifecycleError(
                "Browser selection timed out; check the current view before retrying.",
                error_kind="browser_transition_timeout",
            ) from exc
        finally:
            if revision == view.selection_revision:
                view.selection_pending = False

    async def live_view_state(self, view_id: str, owner: str) -> dict[str, Any]:
        """Reconcile actual selection after a lost transition response."""
        view, record, tab = self.resolve_live_view(view_id, owner)
        async with self._liveview_transition(
            owner, record.workspace_id, tab.tab_id, label="live_view_state",
        ):
            view, record, tab = self.resolve_live_view(view_id, owner)
            return self._frame_meta(tab, view=view, runtime=self.backend.runtime(owner))

    async def _switch_live_view_selected(
        self, view_id: str, owner: str, session_id: str, tab_id: str,
        *, selection_revision: int, connection_generation: int,
    ) -> dict:
        record = await self.ensure_awake(owner, session_id)
        # Page revival restores the prior live selection and therefore takes
        # this same owner transaction. Complete it before acquiring the
        # transition locks, then require the page to remain resident below.
        await self._revive_for_viewer(record, tab_id)
        key = owner_key(owner)
        # Page activation changes one shared Firefox window. Serialize rapid UI
        # clicks so an older slow activation cannot finish after a newer one.
        async with self._liveview_transition(
            owner, record.workspace_id, tab_id, label="switch_live_view",
        ):
            view, _old_record, old_tab = self.resolve_live_view(view_id, owner)
            if connection_generation and int(connection_generation) != view.connection_generation:
                raise WorkspaceError("Live-view connection generation is stale")
            if view.selection_revision and selection_revision < view.selection_revision:
                return {
                    "view_id": view.view_id,
                    "selection_superseded": True,
                    **self._frame_meta(old_tab, view=view, runtime=self.backend.runtime(owner)),
                }
            target_preview = record.tabs.get(tab_id)
            same_control_group = bool(
                target_preview is not None
                and self._same_takeover_group(old_tab, target_preview)
            )
            if (
                old_tab.tab_id != tab_id
                and target_preview is not None
                and old_tab.lease is not None
                and old_tab.lease.actor == "user"
                and not same_control_group
            ):
                await self._reconcile_expired_live_controller_locked(
                    view.runtime_id,
                    owner=owner,
                    target_record=record,
                    target_tab=target_preview,
                    note=(
                        "Expired live browser control was returned to the "
                        "agent before switching tabs."
                    ),
                )
            was_control = self._view_has_control(view, old_tab)
            if old_tab.tab_id != tab_id and old_tab.lease and old_tab.lease.actor == "user":
                if not was_control:
                    recovered = await self._recover_orphaned_live_lease_for_explicit_switch(
                        view, record, old_tab,
                    )
                    if recovered:
                        # release_control is the durable authority change;
                        # the target now opens read-only rather than claiming
                        # that a vanished viewer's input grant moved with it.
                        was_control = False
                if not same_control_group:
                    if old_tab.lease and old_tab.lease.actor == "user":
                        # Same person, new tab: presence moves with them. Return
                        # the controlled lease through the canonical release and
                        # open the switched-to tab read-only; no manual handback.
                        was_control = False
                        await self._relinquish_same_owner_control(
                            owner,
                            note="Switched tabs; control returned to the agent.",
                            moving_view_id=view.view_id,
                        )
                        old_record = self._records.get(
                            self.workspace_id(view.owner, view.session_id)
                        )
                        old_tab = (
                            (old_record.tabs.get(view.tab_id) if old_record is not None else None)
                            or self.record(view.owner, view.session_id).tabs.get(view.tab_id)
                        )
                        if old_tab is None:
                            raise TabLifecycleError(
                                "The controlled tab disappeared during the switch",
                                error_kind="control_group_conflict",
                            )
                if old_tab.lease and old_tab.lease.actor == "user" and not was_control:
                    raise WorkspaceError("Only the controlling viewer can switch controlled tabs")
            if old_tab.tab_id != tab_id:
                blockers = self._retire_own_blockers_and_move_blockers(
                    owner,
                    self._other_live_selection_blockers(
                        view.runtime_id, ignore_view_ids={view.view_id},
                    ),
                )
                if blockers:
                    raise WorkspaceError(
                        "Another device is watching this shared browser tab"
                    )
                for candidate in self.live_views.for_runtime(view.runtime_id):
                    if candidate.view_id != view.view_id:
                        self._retire_live_spectator(candidate)
            runtime = self.backend.runtime(owner)
            if (
                runtime is None
                or str(getattr(runtime, "liveview_runtime_id", "") or "") != view.runtime_id
                or int(getattr(runtime, "generation", 0) or 0) != view.runtime_generation
            ):
                raise LiveViewUnavailable(
                    "The browser restarted; reopen the live view",
                    error_kind="capability_expired",
                )
            tab, page = await self._get_tab(record, tab_id, revive=False)
            old_session_id = view.session_id
            old_workspace_id = view.workspace_id
            old_tab_id = view.tab_id
            old_selection = self._liveview_selected.get(key)
            old_page = self._pages.get((old_workspace_id, old_tab_id))
            try:
                await self.backend.prepare_user_control(owner, page)
                self._adopt_liveview_geometry_revision(view.runtime_id)
                view.session_id = session_id
                view.workspace_id = record.workspace_id
                view.tab_id = tab_id
                view.pressed_keys.clear()
                self._liveview_selected[key] = (record.workspace_id, tab_id)
                if was_control and old_tab_id != tab_id:
                    # Move the daemon's native input grant with the view. The
                    # target shares the group's epoch and durable return path.
                    await self.fence_live_control(view, tab, enabled=True)
            except BaseException:
                view.session_id = old_session_id
                view.workspace_id = old_workspace_id
                view.tab_id = old_tab_id
                if old_selection is None:
                    self._liveview_selected.pop(key, None)
                else:
                    self._liveview_selected[key] = old_selection
                if old_page is not None and old_page is not page:
                    try:
                        await self.backend.prepare_user_control(owner, old_page)
                        self._adopt_liveview_geometry_revision(view.runtime_id)
                        if was_control:
                            await self.fence_live_control(view, old_tab, enabled=True)
                    except Exception:
                        log.exception("Could not restore live control after tab switch failed")
                raise
            self._touch_tab_activity(record, tab, human=True)
            self.store.save(record)
            self._emit_browser_event(record, "selection", tab_id)
            if selection_revision == view.selection_revision:
                view.selection_pending = False
            return {
                "view_id": view.view_id,
                "state": "live-user-control" if self._view_has_control(view, tab) else "live-readonly",
                **self._frame_meta(tab, view=view, runtime=runtime),
            }

    async def _restore_liveview_selection(self, owner: str) -> None:
        key = owner_key(owner)
        requested = self._liveview_selected.get(key)
        if requested is None:
            return
        # Every page is its own top-level window and Juggler raises the
        # agent's page on each pointer command. A daemon that keeps the bound
        # window on top undoes that raise within one X round trip, so
        # re-activating here would only queue bring_to_front behind the
        # agents' mouse events on Juggler's global activation chain (measured
        # 2026-10-04: 3-tab batch p50 2.92 s -> 2.45 s without it, 0
        # blackouts either way).
        keeps_on_top = getattr(self.backend, "keeps_bound_window_on_top", None)
        if callable(keeps_on_top) and keeps_on_top(owner):
            return
        owner_lock = self._liveview_switch_locks.setdefault(key, asyncio.Lock())
        try:
            async with asyncio.timeout(CONTROL_PREPARATION_TIMEOUT_SECONDS):
                async with owner_lock:
                    # A newer switch may have completed while this callback
                    # waited. Never restore the previous selected page then.
                    if self._liveview_selected.get(key) != requested:
                        return
                    page = self._pages.get(requested)
                    if page is None:
                        return
                    await self.backend.activate(owner, page)
        except Exception:
            log.debug("Could not restore the selected live-view page", exc_info=True)

    def resolve_live_view(self, view_id: str, owner: str) -> tuple[LiveViewSession, WorkspaceRecord, TabRecord]:
        view = self.live_views.get(view_id, owner)
        record = self._records.get(view.workspace_id) or self.record(owner, view.session_id)
        tab = record.tabs.get(view.tab_id)
        runtime = self.backend.runtime(owner)
        if tab is None or runtime is None:
            view.closed = True
            raise LiveViewUnavailable("The selected browser tab is no longer active")
        if (
            str(getattr(runtime, "liveview_runtime_id", "") or "") != view.runtime_id
            or int(getattr(runtime, "generation", 0) or 0) != view.runtime_generation
        ):
            view.closed = True
            raise LiveViewUnavailable(
                "The browser restarted; reopen the live view",
                error_kind="capability_expired",
            )
        return view, record, tab

    async def capture_live_still(self, view_id: str, owner: str) -> tuple[bytes, str, int, int, str]:
        """Grab the native-resolution content rectangle for a resting viewer.

        The embed shows this above the video once the picture stops moving and
        drops it on the first input. It never takes a browser action lock or
        touches the encoder, so input and video latency cannot wait on it.
        Identity is rechecked after the asynchronous grab so a tab switch or
        geometry change never returns a picture of another page.
        """
        view, record, tab = self.resolve_live_view(view_id, owner)
        native = self.liveview.runtime(view.runtime_id)
        selected = self._liveview_selected.get(owner_key(owner))
        page = self._pages.get((record.workspace_id, tab.tab_id))
        runtime = self.backend.runtime(owner)
        if (native is None or page is None or runtime is None
                or selected != (record.workspace_id, tab.tab_id)
                or getattr(runtime, "bound_liveview_page", None) is not page
                or not self.live_views.current_connected(view)):
            raise LiveViewUnavailable("The selected live page is unavailable")
        revision = int(native.geometry_revision)
        window_id = int(native.window_id)
        x, y = int(native.capture_x), int(native.capture_y)
        width, height = int(native.capture_width), int(native.capture_height)
        offset_x = x - int(native.window_x)
        offset_y = y - int(native.window_y)
        if (width < 2 or height < 2 or width * height > 4_000_000
                or window_id <= 0 or offset_x < 0 or offset_y < 0
                or offset_x + width > int(native.window_width)
                or offset_y + height > int(native.window_height)):
            raise LiveViewUnavailable("The live page exceeds the still-image bounds")
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "DISPLAY": native.display, "XAUTHORITY": native.auth_file}
        command = (
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "x11grab", "-window_id", str(window_id),
            "-draw_mouse", "0", "-video_size", f"{width}x{height}",
            "-grab_x", str(offset_x), "-grab_y", str(offset_y),
            "-i", native.display, "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=environment,
            )
            try:
                raw, error = await asyncio.wait_for(process.communicate(), timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise LiveViewUnavailable("The still image took too long") from None
        except OSError as exc:
            raise LiveViewUnavailable("The still-image capture is unavailable") from exc
        if process.returncode or len(raw) != width * height * 3:
            log.debug("Still-image capture failed: %s", error[:300] if error else process.returncode)
            raise LiveViewUnavailable("The still image could not be captured")
        picture, media_type = await asyncio.to_thread(_encode_live_still, raw, width, height)
        current_view, current_record, current_tab = self.resolve_live_view(view_id, owner)
        current_native = self.liveview.runtime(current_view.runtime_id)
        if (current_view is not view or current_record.workspace_id != record.workspace_id
                or current_tab.tab_id != tab.tab_id or current_native is not native
                or int(native.geometry_revision) != revision or int(native.window_id) != window_id
                or self._liveview_selected.get(owner_key(owner)) != selected
                or self._pages.get(selected) is not page
                or not self.live_views.current_connected(view)):
            raise LiveViewUnavailable("The live page changed during still-image capture")
        return picture, media_type, int(view.runtime_generation), revision, str(tab.tab_id)

    def live_view_connected(self, view: LiveViewSession) -> int:
        pending = self._liveview_disconnect_tasks.pop(view.view_id, None)
        if pending:
            pending.cancel()
        # A replacement socket establishes a new current generation. No task
        # from an older transport may fence or hand back that successor.
        self._cancel_liveview_liveness(view.view_id)
        # A new transport generation cannot inherit modifier state from the
        # connection it supersedes. The daemon releases its native state when
        # the grant generation changes; clear the manager's shortcut mirror too.
        view.pressed_keys.clear()
        connection_generation = self.live_views.connected(view)
        self.note_live_view_client_activity(
            view, connection_generation=connection_generation,
        )
        # Viewing a tab is real use.  It refreshes that tab's durable activity
        # clock but never treats the connection itself as permission to evict
        # every other resident page.
        record = self._records.get(view.workspace_id)
        tab = record.tabs.get(view.tab_id) if record else None
        if record is not None and tab is not None:
            self._touch_tab_activity(record, tab, human=True, persist=True)
        return connection_generation

    def _schedule_suspend_background(self, _view: LiveViewSession) -> None:
        """Compatibility shim for integrations from before per-tab sleeping.

        It intentionally does nothing: viewer connection is activity, not an
        eviction signal.  Keep the private seam for old test/deployment glue
        until those callers age out rather than forcing a parallel policy path.
        """
        return None

    def live_view_disconnected(
        self,
        view: LiveViewSession,
        *,
        connection_generation: int | None = None,
    ) -> None:
        disconnected_generation = int(
            view.connection_generation
            if connection_generation is None else connection_generation
        )
        current_lost = self.live_views.disconnected(
            view, connection_generation=connection_generation,
        )
        # A clean close owns reconnect grace below; liveness work only models
        # an *unreported* half-open current socket and must not outlive either
        # kind of teardown. This also clears a superseded socket's task.
        self._cancel_liveview_liveness(
            view.view_id, connection_generation=disconnected_generation,
        )
        # A superseded socket often closes after its replacement is already
        # driving the view. Its teardown must not start handback grace for the
        # current controller.
        if not current_lost:
            return
        view.pressed_keys.clear()
        # The wheel pump state is per-viewer-session; a reconnect starts clean
        # (the bound page may have changed while this viewer was away).
        self._liveview_wheel_states.pop(view.view_id, None)
        controller = self._liveview_controller_by_runtime.get(view.runtime_id)
        if not controller:
            viewport_owner = self._liveview_viewport_owner_by_runtime.get(
                view.runtime_id, "",
            )
            if viewport_owner and secrets.compare_digest(
                viewport_owner, view.view_id,
            ):
                # Read-only viewport authority has no durable lease and no
                # reconnect entitlement. Transfer is available immediately.
                self._liveview_viewport_owner_by_runtime.pop(view.runtime_id, None)
            record = self._records.get(view.workspace_id)
            tab = record.tabs.get(view.tab_id) if record is not None else None
            if not (
                tab is not None
                and tab.lease is not None
                and tab.lease.actor == "user"
                and tab.lease.actor_id == view.owner
            ):
                return
        if controller and controller != view.view_id:
            return
        prior = self._liveview_disconnect_tasks.pop(view.view_id, None)
        if prior:
            prior.cancel()

        async def release_after_grace() -> None:
            try:
                await asyncio.sleep(RECONNECT_GRACE_SECONDS)
                async with self._liveview_transition(
                    view.owner, view.workspace_id, view.tab_id,
                ):
                    # A reconnect capability becomes unusable at the same
                    # deadline that wakes this task. Cleanup must therefore
                    # use the already-authenticated lifecycle object rather
                    # than Registry.get(), which correctly rejects it as
                    # expired and would leave the durable user lease stranded.
                    current = self.live_views.find(view.view_id) or view
                    if (
                        current.disconnected_at is None
                        or int(current.connection_generation) != disconnected_generation
                        or self.live_views.current_connected(current)
                    ):
                        return
                    record = self._records.get(view.workspace_id) or self.record(
                        view.owner, view.session_id,
                    )
                    tab = record.tabs.get(view.tab_id) if record is not None else None
                    controller = self._liveview_controller_by_runtime.get(view.runtime_id)
                    if controller and controller != view.view_id:
                        return
                    if tab is None:
                        if controller == view.view_id:
                            self._liveview_controller_by_runtime.pop(view.runtime_id, None)
                            self._liveview_viewport_owner_by_runtime.pop(
                                view.runtime_id, None,
                            )
                        return
                    if tab.lease and tab.lease.actor == "user":
                        try:
                            await self.fence_live_control(current, tab, enabled=False)
                        except Exception:
                            # The current proxy connection is already gone and
                            # the capability is expired. Mirror failed-reconnect
                            # cleanup: revoke manager authority even when the
                            # daemon cannot acknowledge its stale grant.
                            log.warning(
                                "Could not fence expired live control at liveviewd",
                                exc_info=True,
                            )
                        self.release_control(
                            view.owner, view.session_id, view.tab_id,
                            note="Live control disconnected before handback.",
                            preserve_hold=True,
                            _fence_daemon=False,
                        )
            except (asyncio.CancelledError, LiveViewUnavailable):
                return
            finally:
                self._liveview_disconnect_tasks.pop(view.view_id, None)

        task = asyncio.create_task(release_after_grace())
        self._liveview_disconnect_tasks[view.view_id] = task

    async def fail_live_reconnect(
        self,
        view: LiveViewSession,
        *,
        connection_generation: int,
        reason: str = "Live control reconnect could not be fenced safely.",
    ) -> None:
        """Fail a reconnect closed and return any stranded durable lease."""
        async with self._liveview_transition(
            view.owner, view.workspace_id, view.tab_id,
        ):
            if int(view.connection_generation) != int(connection_generation):
                return
            record = self._records.get(view.workspace_id)
            tab = record.tabs.get(view.tab_id) if record else None
            if tab is None:
                self._liveview_controller_by_runtime.pop(view.runtime_id, None)
                self._liveview_viewport_owner_by_runtime.pop(view.runtime_id, None)
                return
            controller = self._liveview_controller_by_runtime.get(view.runtime_id)
            if not controller or not secrets.compare_digest(controller, view.view_id):
                return
            try:
                await self.fence_live_control(view, tab, enabled=False)
            except Exception:
                # The proxy's manager-side authority is still revoked below,
                # so an unreachable/ambiguous daemon cannot receive new input.
                log.warning("Could not revoke failed reconnect at liveviewd", exc_info=True)
            if tab.lease and tab.lease.actor == "user" and tab.lease.actor_id == view.owner:
                try:
                    self.release_control(
                        view.owner,
                        view.session_id,
                        view.tab_id,
                        note=reason,
                        preserve_hold=True,
                        _fence_daemon=False,
                    )
                except Exception:
                    # release_control mutates the lease before its atomic save.
                    # Retry that forward handback once; regardless of storage
                    # health, clearing the controller keeps this process closed
                    # to further native input.
                    self._liveview_controller_by_runtime.pop(view.runtime_id, None)
                    try:
                        self.store.save(record)
                    except Exception:
                        log.exception("Could not persist failed-reconnect handback")
            else:
                self._liveview_controller_by_runtime.pop(view.runtime_id, None)
                self._liveview_viewport_owner_by_runtime.pop(view.runtime_id, None)
            view.pressed_keys.clear()

    def _assert_live_user_control(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        view_id: str = "",
        lease_epoch: int = 0,
        runtime_generation: int = 0,
        controller_id: str = "",
        connection_generation: int = 0,
    ) -> tuple[WorkspaceRecord, TabRecord, LiveViewSession | None]:
        record = self.record(owner, session_id)
        tab = record.tabs.get(tab_id)
        if tab is None:
            raise WorkspaceError("Unknown tab")
        lease = tab.lease
        if not lease or lease.actor != "user" or lease.actor_id != owner:
            raise WorkspaceError("User-control lease is stale")
        if lease.expires_at is not None and lease.expires_at <= time.time():
            raise WorkspaceError("User-control lease is expired")
        if lease_epoch and int(lease.generation or 0) != int(lease_epoch):
            raise WorkspaceError("User-control lease epoch is stale")
        if not view_id:
            if self._live_capabilities(self.backend.runtime(owner)).get("control_fencing"):
                raise WorkspaceError("A controlling live-view capability is required")
            return record, tab, None
        view, view_record, view_tab = self.resolve_live_view(view_id, owner)
        if view_record.workspace_id != record.workspace_id or view_tab.tab_id != tab_id:
            raise WorkspaceError("Live-view capability does not target this tab")
        if not self._view_has_control(view, tab):
            raise WorkspaceError("This viewer does not control the browser")
        runtime = self.backend.runtime(owner)
        if self._live_capabilities(runtime).get("control_fencing"):
            if (
                int(lease_epoch or 0) <= 0
                or int(runtime_generation or 0) <= 0
                or not str(controller_id or "")
                or int(connection_generation or 0) <= 0
            ):
                raise WorkspaceError("Live-view controller authority is incomplete")
        if runtime_generation and int(runtime_generation) != int(view.runtime_generation):
            raise WorkspaceError("Live-view runtime generation is stale")
        if controller_id and not secrets.compare_digest(str(controller_id), view.view_id):
            raise WorkspaceError("Live-view controller is stale")
        if (
            connection_generation
            and int(connection_generation) != int(view.connection_generation)
        ):
            raise WorkspaceError("Live-view connection generation is stale")
        return record, tab, view

    def _assert_live_viewport_authority(
        self,
        view_id: str,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        lease_epoch: int = 0,
        runtime_generation: int = 0,
        controller_id: str = "",
        connection_generation: int = 0,
        claim: bool = False,
    ) -> tuple[WorkspaceRecord, TabRecord, LiveViewSession]:
        """Fence a resize to one connected viewer without granting input."""
        if not view_id:
            raise WorkspaceError("A live-view capability is required to resize")
        view, view_record, view_tab = self.resolve_live_view(view_id, owner)
        record = self.record(owner, session_id)
        tab = record.tabs.get(tab_id)
        if (
            tab is None
            or view_record.workspace_id != record.workspace_id
            or view_tab.tab_id != tab_id
        ):
            raise WorkspaceError("Live-view capability does not target this tab")
        runtime = self.backend.runtime(owner)
        if runtime is None:
            raise BrowserViewportError(
                "The browser restarted; reopen the live view",
                error_kind="runtime_restarted",
            )
        if int(runtime_generation or 0) <= 0 or int(connection_generation or 0) <= 0:
            raise BrowserViewportError(
                "Live-view viewport authority is incomplete",
                error_kind="viewport_authority_incomplete",
            )
        if int(runtime_generation) != int(view.runtime_generation):
            raise BrowserViewportError(
                "Live-view runtime generation is stale",
                error_kind="runtime_restarted",
            )
        if int(connection_generation) != int(view.connection_generation):
            raise BrowserViewportError(
                "Live-view connection generation is stale",
                error_kind="viewport_authority_stale",
            )
        if not self.live_views.current_connected(view):
            raise BrowserViewportError(
                "Live-view connection is not active",
                error_kind="viewport_authority_stale",
            )
        if not self._live_capabilities(runtime).get("resize_window"):
            raise BrowserViewportError(
                "Dynamic browser sizing is unavailable",
                error_kind="viewport_unavailable",
                requires_new_runtime=not bool(
                    getattr(runtime, "dynamic_viewport", False)
                ),
            )
        lease = tab.lease
        if lease_epoch and int(lease_epoch) != int(getattr(lease, "generation", 0) or 0):
            raise BrowserViewportError(
                "Browser tab lease epoch is stale",
                error_kind="viewport_authority_stale",
            )

        controller = self._liveview_controller_by_runtime.get(view.runtime_id, "")
        if controller:
            if not secrets.compare_digest(controller, view.view_id):
                raise BrowserViewportError(
                    "Another live viewer controls the shared browser viewport",
                    error_kind="viewport_owner_conflict",
                )
            # A controller keeps the stronger, durable authority checks. This
            # also catches an expired lease before any geometry can change.
            self._assert_live_user_control(
                owner,
                session_id,
                tab_id,
                view_id=view_id,
                lease_epoch=lease_epoch,
                runtime_generation=runtime_generation,
                controller_id=controller_id,
                connection_generation=connection_generation,
            )
            self._liveview_viewport_owner_by_runtime[view.runtime_id] = view.view_id
            return record, tab, view

        if controller_id:
            raise BrowserViewportError(
                "Live-view controller is stale",
                error_kind="viewport_authority_stale",
            )
        viewport_owner = self._viewport_owner_id(view.runtime_id)
        if viewport_owner and not secrets.compare_digest(viewport_owner, view.view_id):
            raise BrowserViewportError(
                "Another live viewer controls the shared browser viewport",
                error_kind="viewport_owner_conflict",
            )
        if claim:
            self._liveview_viewport_owner_by_runtime[view.runtime_id] = view.view_id
        return record, tab, view

    async def live_omnibox_matches(
        self, owner: str, session_id: str, query: str = "", active_tab_id: str = "",
    ) -> dict[str, list[dict[str, Any]]]:
        """Project owner-scoped open tabs and browser-owned visit history."""
        record = self.record(owner, session_id)
        text = str(query or "").strip()[:180]
        folded = text.casefold()
        active_tab_id = str(active_tab_id or "").strip()[:128]
        open_tabs: list[dict[str, Any]] = []
        for tab in record.tabs.values():
            # A native “Switch to tab” row is only useful for another tab. The
            # focused page remains the current navigation target and may be
            # one metadata commit ahead of the durable tab summary.
            if tab.tab_id == active_tab_id:
                continue
            url = str(tab.url or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                continue
            title = str(tab.title or "").strip()
            if folded and folded not in f"{title} {url}".casefold():
                continue
            open_tabs.append({
                "tab_id": str(tab.tab_id),
                "url": url[:2048],
                "title": title[:200],
                "last_active_at": float(tab.last_active_at or tab.updated_at or 0),
            })
        open_tabs.sort(key=lambda item: item["last_active_at"], reverse=True)
        open_tabs = open_tabs[:6]

        profile_dir = ""
        profile_resolver = getattr(self.backend, "profile_dir", None)
        if callable(profile_resolver):
            try:
                profile_dir = str(profile_resolver(owner) or "")
            except Exception:
                profile_dir = ""
        if (callable(getattr(self.backend, "runtime", None))
              and getattr(self.backend.runtime(owner), "presentation", "") == "embedded"):
            try:
                history = await asyncio.to_thread(self.store.native_history_matches, owner, text)
            except (OSError, ValueError):
                log.warning("Could not read native browser history", exc_info=True)
                history = []
        elif profile_dir:
            from ascended_browser._app.browser_workspace.omnibox import firefox_history_matches

            history = await asyncio.to_thread(
                firefox_history_matches, profile_dir, text,
            )
        else:
            history = []
        open_urls = {item["url"] for item in open_tabs}
        history = [item for item in history if item.get("url") not in open_urls]
        return {"tabs": open_tabs, "history": history}

    async def live_navigate(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        action: str,
        *,
        url: str = "",
        view_id: str = "",
        lease_epoch: int = 0,
        runtime_generation: int = 0,
        controller_id: str = "",
        connection_generation: int = 0,
    ) -> dict[str, Any]:
        """Drive ordinary browser chrome through the workspace authority owner."""
        action = str(action or "").strip().lower()
        if action not in {"back", "forward", "reload", "stop", "goto"}:
            raise WorkspaceError("Unsupported browser navigation action")
        record, tab, _view = self._assert_live_user_control(
            owner, session_id, tab_id, view_id=view_id, lease_epoch=lease_epoch,
            runtime_generation=runtime_generation, controller_id=controller_id,
            connection_generation=connection_generation,
        )
        target = ""
        if action == "goto":
            target = _browser_omnibox_target(str(url or ""))
        _, page = await self._get_tab(record, tab_id)
        if action == "stop":
            # Stop must be able to interrupt page.goto/reload while that request
            # owns the tab action lock. Controller transitions use this
            # owner-scoped lock, so authority remains stable from the final
            # check through dispatching window.stop().
            owner_lock = self._liveview_switch_locks.setdefault(
                owner_key(owner), asyncio.Lock(),
            )
            async with owner_lock:
                self._assert_live_user_control(
                    owner, session_id, tab_id, view_id=view_id,
                    lease_epoch=lease_epoch, runtime_generation=runtime_generation,
                    controller_id=controller_id,
                    connection_generation=connection_generation,
                )
                self._invalidate_observation(record.workspace_id, tab_id)
                tab.last_observation_id = ""
                try:
                    await asyncio.wait_for(
                        page.evaluate("() => window.stop()"), timeout=3.0,
                    )
                except asyncio.TimeoutError as exc:
                    raise WorkspaceError("The page did not acknowledge Stop") from exc
                tab.url = str(getattr(page, "url", "") or tab.url)
                try:
                    tab.title = str(
                        await asyncio.wait_for(page.title(), timeout=1.0)
                        or tab.title
                    )
                except Exception:
                    pass
                self._touch_tab_activity(record, tab, human=True)
                # A stopped load still changed what the page shows; agent
                # snapshots taken before this are stale.
                tab.human_input_revision = int(tab.human_input_revision or 0) + 1
                self.store.save(record)
            return {
                "action": action,
                "url": tab.url,
                "title": tab.title,
                "can_go_back": None,
                "can_go_forward": None,
            }
        effect_barrier = self._runtime_effect_barrier(owner)
        navigation_result = None
        async with effect_barrier.shared(), self._action_lock(record.workspace_id, tab_id):
            # Authority may have transferred while this request waited.
            self._assert_live_user_control(
                owner, session_id, tab_id, view_id=view_id, lease_epoch=lease_epoch,
                runtime_generation=runtime_generation, controller_id=controller_id,
                connection_generation=connection_generation,
            )
            previous_url = str(getattr(page, "url", "") or tab.url)
            self._invalidate_observation(record.workspace_id, tab_id)
            tab.last_observation_id = ""
            if action == "goto":
                navigation_result = await self._navigate_until_runtime_transition(
                    page,
                    page.goto(target, wait_until="domcontentloaded", timeout=60000),
                    effect_barrier,
                    wait_seconds=62,
                )
                tab.last_method = "GET"
            elif action == "back":
                navigation_result = await self._navigate_until_runtime_transition(
                    page,
                    page.go_back(wait_until="domcontentloaded", timeout=60000),
                    effect_barrier,
                    wait_seconds=62,
                )
            elif action == "forward":
                navigation_result = await self._navigate_until_runtime_transition(
                    page,
                    page.go_forward(wait_until="domcontentloaded", timeout=60000),
                    effect_barrier,
                    wait_seconds=62,
                )
            elif action == "reload":
                navigation_result = await self._navigate_until_runtime_transition(
                    page,
                    _reload_page(page, timeout=60_000),
                    effect_barrier,
                    wait_seconds=62,
                )
            await self._settle_page(page)
            await self._refresh_tab(record, tab, page)
            try:
                history_state = await page.evaluate(
                    "() => ({"
                    "can_go_back: window.navigation && 'canGoBack' in navigation "
                    "? navigation.canGoBack : history.length > 1, "
                    "can_go_forward: window.navigation && 'canGoForward' in navigation "
                    "? navigation.canGoForward : null})"
                )
            except Exception:
                history_state = {}
            if not isinstance(history_state, dict):
                history_state = {}
            current_url = str(getattr(page, "url", "") or tab.url)
            moved = navigation_result is not None or current_url != previous_url
            # Camoufox deliberately masks parts of the web-exposed session
            # history surface. In particular, history.length can report one
            # after Playwright has committed a second, traversable entry. The
            # manager knows more at this boundary: a completed cross-document
            # goto creates a Back entry, while a successful traversal creates
            # the inverse direction. Project those facts over the masked JS
            # hint so the toolbar does not disable a control that works.
            if action == "goto" and moved:
                history_state["can_go_back"] = True
                history_state["can_go_forward"] = False
            elif action == "back":
                if moved:
                    history_state["can_go_back"] = True
                    history_state["can_go_forward"] = True
                else:
                    history_state["can_go_back"] = False
                    history_state["can_go_forward"] = None
            elif action == "forward":
                if moved:
                    history_state["can_go_back"] = True
                    history_state["can_go_forward"] = True
                else:
                    history_state["can_go_back"] = None
                    history_state["can_go_forward"] = False
            if moved:
                # The person moved the page (goto/back/forward/reload); agent
                # snapshots taken before this are stale.
                tab.human_input_revision = int(tab.human_input_revision or 0) + 1
            self.store.save(record)
        return {
            "action": action,
            "url": str(getattr(page, "url", "") or tab.url),
            "title": tab.title,
            "can_go_back": (
                (history_state or {}).get("can_go_back")
                if isinstance((history_state or {}).get("can_go_back"), bool)
                else None
            ),
            "can_go_forward": (
                (history_state or {}).get("can_go_forward")
                if isinstance((history_state or {}).get("can_go_forward"), bool)
                else None
            ),
        }

    async def live_find_in_page(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        query: str,
        action: str,
        *,
        sequence: int = 0,
        view_id: str = "",
        lease_epoch: int = 0,
        runtime_generation: int = 0,
        controller_id: str = "",
        connection_generation: int = 0,
    ) -> dict[str, Any]:
        """Run browser-shell Find against the controlled remote page.

        Firefox's page-side ``window.find`` supplies native selection and
        scrolling. Counts and cursor position are transient and scoped to the
        workspace, tab, and live-view capability.
        """
        action = str(action or "").strip().lower()
        if action not in {"search", "next", "previous", "clear"}:
            raise WorkspaceError("Unsupported Find in page action")
        query = str(query or "")[:500]
        if action == "search":
            query = query.strip()
        find_runtime = self.backend.runtime(owner)
        embedded = getattr(find_runtime, "presentation", "") == "embedded"
        if not view_id and not embedded:
            raise WorkspaceError("A controlling live-view capability is required")
        if embedded and not view_id and int(lease_epoch or 0) <= 0:
            raise WorkspaceError("A current desktop user-control lease epoch is required")
        record, _tab, _view = self._assert_live_user_control(
            owner, session_id, tab_id, view_id=view_id, lease_epoch=lease_epoch,
            runtime_generation=runtime_generation, controller_id=controller_id,
            connection_generation=connection_generation,
        )
        key = (record.workspace_id, tab_id, view_id)
        sequence = max(0, int(sequence or 0))
        latest_sequence = self._live_find_revisions.get(key, 0)
        if sequence and sequence < latest_sequence:
            return {"action": action, "query": query, "count": 0, "index": 0, "stale": True}
        if sequence:
            self._live_find_revisions[key] = sequence
            if len(self._live_find_revisions) > 1000:
                oldest = next(iter(self._live_find_revisions))
                self._live_find_revisions.pop(oldest, None)
        script = r"""({ query, operation, backwards }) => {
          const selection = window.getSelection?.();
          if (operation === 'clear') {
            if (selection && query
                && selection.toString().toLocaleLowerCase() === query.toLocaleLowerCase()) {
              selection.removeAllRanges();
            }
            return { found: false, count: 0, supported: true };
          }
          if (typeof window.find !== 'function') {
            return { found: false, count: 0, supported: false };
          }
          if (operation === 'step') {
            return {
              found: !!window.find(query, false, !!backwards, false, false, false, false),
              count: 0, supported: true,
            };
          }
          const root = document.body || document.documentElement;
          if (!selection || !root || !query) {
            selection?.removeAllRanges();
            return { found: false, count: 0, supported: true };
          }
          selection.removeAllRanges();
          const start = document.createRange();
          start.selectNodeContents(root);
          start.collapse(!backwards);
          selection.addRange(start);
          let count = 0;
          let chosen = null;
          while (count < 1000 && window.find(query, false, !!backwards, false, false, false, false)) {
            count += 1;
            if (!chosen && selection.rangeCount) chosen = selection.getRangeAt(0).cloneRange();
          }
          selection.removeAllRanges();
          if (chosen) {
            selection.addRange(chosen);
            const node = chosen.startContainer.nodeType === Node.ELEMENT_NODE
              ? chosen.startContainer : chosen.startContainer.parentElement;
            node?.scrollIntoView?.({ block: 'center', inline: 'nearest' });
          }
          return { found: count > 0, count, supported: true };
        }"""
        effect_barrier = self._runtime_effect_barrier(owner)
        async with effect_barrier.shared(), self._action_lock(record.workspace_id, tab_id):
            if sequence and sequence < self._live_find_revisions.get(key, 0):
                return {"action": action, "query": query, "count": 0, "index": 0, "stale": True}
            state = self._live_find_states.get(key)
            empty_search = action == "search" and not query
            if action == "clear" and not query and state:
                query = str(state.get("query") or "")
            elif empty_search and state:
                query = str(state.get("query") or "")
            self._assert_live_user_control(
                owner, session_id, tab_id, view_id=view_id, lease_epoch=lease_epoch,
                runtime_generation=runtime_generation, controller_id=controller_id,
                connection_generation=connection_generation,
            )
            _, page = await self._get_tab(record, tab_id)
            def assert_current_find_target() -> None:
                self._assert_live_user_control(
                    owner, session_id, tab_id, view_id=view_id, lease_epoch=lease_epoch,
                    runtime_generation=runtime_generation, controller_id=controller_id,
                    connection_generation=connection_generation,
                )
                if (
                    self.backend.runtime(owner) is not find_runtime
                    or self._records.get(record.workspace_id) is not record
                    or record.tabs.get(tab_id) is not _tab
                    or self._pages.get((record.workspace_id, tab_id)) is not page
                ):
                    raise WorkspaceError("The browser page changed; reopen Find in page")

            assert_current_find_target()
            frames = list(getattr(page, "frames", []) or [page])
            signature = [
                (str(getattr(frame, "url", "") or ""), str(getattr(frame, "name", "") or ""))
                for frame in frames
            ]

            async def frame_call(frame: Any, operation: str, *, backwards: bool = False) -> dict[str, Any]:
                assert_current_find_target()
                try:
                    result = await asyncio.wait_for(
                        frame.evaluate(script, {
                            "query": query, "operation": operation,
                            "backwards": backwards,
                        }),
                        timeout=4.0,
                    )
                except Exception as exc:
                    raise WorkspaceError("Find in page could not inspect the current page") from exc
                assert_current_find_target()
                if not isinstance(result, dict) or result.get("supported") is not True:
                    raise WorkspaceError("Find in page is unavailable in this browser runtime")
                return result

            state_is_current = (
                bool(state)
                and str(state.get("query") or "").casefold() == query.casefold()
                and state.get("signature") == signature
                and len(state.get("counts") or []) == len(frames)
            )
            if action in {"next", "previous"} and not state_is_current:
                action = "search"

            if action == "clear":
                if query:
                    for frame in frames:
                        await frame_call(frame, "clear")
                self._live_find_states.pop(key, None)
                return {"action": "clear", "query": "", "count": 0, "index": 0}

            if empty_search:
                if query:
                    for frame in frames:
                        await frame_call(frame, "clear")
                self._live_find_states.pop(key, None)
                return {"action": "search", "query": "", "count": 0, "index": 0}

            if not query:
                for frame in frames:
                    await frame_call(frame, "clear")
                self._live_find_states.pop(key, None)
                return {"action": "search", "query": "", "count": 0, "index": 0}

            if action == "search":
                counts: list[int] = []
                first_frame = -1
                for index, frame in enumerate(frames):
                    result = await frame_call(frame, "count")
                    count = max(0, int(result.get("count") or 0))
                    counts.append(count)
                    if count and first_frame < 0:
                        first_frame = index
                    elif count:
                        # Count every frame, but expose only the active result.
                        await frame_call(frame, "clear")
                self._live_find_states[key] = {
                    "query": query, "signature": signature, "counts": counts,
                    "frame": first_frame, "match": 1 if first_frame >= 0 else 0,
                }
                if len(self._live_find_states) > 1000:
                    oldest = next(iter(self._live_find_states))
                    self._live_find_states.pop(oldest, None)
                total = sum(counts)
                return {
                    "action": "search", "query": query,
                    "count": min(total, 1000), "count_capped": total >= 1000,
                    "index": 1 if first_frame >= 0 else 0,
                }

            direction = -1 if action == "previous" else 1
            current_frame = int(state.get("frame", -1))
            current_match = int(state.get("match", 0))
            counts = [max(0, int(value or 0)) for value in state.get("counts", [])]
            if current_frame >= 0:
                result = await frame_call(
                    frames[current_frame], "step", backwards=direction < 0,
                )
                if result.get("found"):
                    current_match += direction
                    if current_match < 1:
                        current_match = counts[current_frame]
                    elif current_match > counts[current_frame]:
                        current_match = 1
                else:
                    next_frame = current_frame
                    for _ in range(len(frames)):
                        next_frame = (next_frame + direction) % len(frames)
                        if counts[next_frame]:
                            break
                    else:
                        next_frame = -1
                    if next_frame >= 0:
                        await frame_call(
                            frames[next_frame], "count", backwards=direction < 0,
                        )
                        current_frame = next_frame
                        current_match = counts[next_frame] if direction < 0 else 1
                    else:
                        current_frame = -1
                        current_match = 0
            total = sum(counts)
            state["frame"] = current_frame
            state["match"] = current_match
            prefix = sum(counts[:current_frame]) if current_frame >= 0 else 0
            return {
                "action": action, "query": query,
                "count": min(total, 1000), "count_capped": total >= 1000,
                "index": prefix + current_match if current_frame >= 0 else 0,
            }

    async def live_screenshot(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        view_id: str = "",
        lease_epoch: int = 0,
        runtime_generation: int = 0,
        controller_id: str = "",
        connection_generation: int = 0,
    ) -> tuple[bytes, str]:
        """Capture the active page viewport as a PNG under live-view authority."""
        record, _tab, _view = self._assert_live_user_control(
            owner, session_id, tab_id, view_id=view_id, lease_epoch=lease_epoch,
            runtime_generation=runtime_generation, controller_id=controller_id,
            connection_generation=connection_generation,
        )
        effect_barrier = self._runtime_effect_barrier(owner)
        async with effect_barrier.shared(), self._action_lock(record.workspace_id, tab_id):
            self._assert_live_user_control(
                owner, session_id, tab_id, view_id=view_id, lease_epoch=lease_epoch,
                runtime_generation=runtime_generation, controller_id=controller_id,
                connection_generation=connection_generation,
            )
            tab, page = await self._get_tab(record, tab_id)
            try:
                png = await asyncio.wait_for(
                    page.screenshot(type="png", full_page=False, animations="disabled"),
                    timeout=15.0,
                )
            except Exception as exc:
                raise WorkspaceError("The browser page could not be captured") from exc
            self._assert_live_user_control(
                owner, session_id, tab_id, view_id=view_id, lease_epoch=lease_epoch,
                runtime_generation=runtime_generation, controller_id=controller_id,
                connection_generation=connection_generation,
            )
            return bytes(png), str(getattr(page, "url", "") or tab.url)

    async def live_clear_site_cookies(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        confirmed: bool = False,
        expected_domain: str = "",
        view_id: str = "",
        lease_epoch: int = 0,
        runtime_generation: int = 0,
        controller_id: str = "",
        connection_generation: int = 0,
    ) -> dict[str, Any]:
        """Clear cookie scopes applying to the user's confirmed current host.

        The persistent owner context remains authoritative. Read every path,
        including HttpOnly cookies, and use an exact domain filter rather than
        clearing/rebuilding the jar (which loses unrelated or concurrent state).
        No reload or other origin-storage mutation is implicit in this action.
        """
        if not confirmed:
            raise WorkspaceError("Clearing site cookies requires confirmation")
        cookie_runtime = self.backend.runtime(owner)
        embedded = getattr(cookie_runtime, "presentation", "") == "embedded"
        if not view_id and not embedded:
            raise WorkspaceError("A controlling live-view capability is required")
        if embedded and not view_id and int(lease_epoch or 0) <= 0:
            raise WorkspaceError("A current desktop user-control lease epoch is required")
        authority = dict(
            view_id=view_id, lease_epoch=lease_epoch,
            runtime_generation=runtime_generation, controller_id=controller_id,
            connection_generation=connection_generation,
        )
        record, tab, _view = self._assert_live_user_control(
            owner, session_id, tab_id, **authority,
        )
        async with self._liveview_transition(
            owner, record.workspace_id, tab_id, label="clear_site_cookies", timeout=12.0,
        ):
            self._assert_live_user_control(owner, session_id, tab_id, **authority)
            tab, page = await self._get_tab(record, tab_id)

            def current_host() -> str:
                if embedded and self.backend.runtime(owner) is not cookie_runtime:
                    raise WorkspaceError("The desktop browser restarted; reopen the tab")
                url = urlparse(str(getattr(page, "url", "") or ""))
                if url.scheme not in {"http", "https"} or not url.hostname:
                    raise WorkspaceError("Open a website to clear its cookies")
                host = url.hostname.lower()
                if host != str(expected_domain or "").lower():
                    raise WorkspaceError("The site changed. Reopen the menu to clear its cookies.")
                return host

            host = current_host()

            def applies(domain: str, hostname: str = host) -> bool:
                value = str(domain or "").lower()
                return value == hostname or (
                    value.startswith(".")
                    and (hostname == value[1:] or hostname.endswith(value))
                )

            cookie_effect_started = False
            domains: set[str] = set()
            try:
                before = await page.context.cookies()
                domains = {cookie["domain"] for cookie in before if applies(cookie.get("domain"))}
                # Cookie reads yield to the browser. Recheck both target and
                # authority at the actual effect boundary, after that await.
                self._assert_live_user_control(owner, session_id, tab_id, **authority)
                current_host()
                if domains:
                    pattern = re.compile("^(?:" + "|".join(re.escape(d) for d in sorted(domains)) + ")$")
                    cookie_effect_started = True
                    if embedded:
                        cdp = await page.context.new_cdp_session(page)
                        try:
                            for cookie in before:
                                if not applies(cookie.get("domain")):
                                    continue
                                self._assert_live_user_control(owner, session_id, tab_id, **authority)
                                current_host()
                                await cdp.send("Network.deleteCookies", {
                                    "name": cookie["name"], "domain": cookie["domain"],
                                    "path": cookie.get("path", "/"),
                                })
                        finally:
                            await cdp.detach()
                    else:
                        await page.context.clear_cookies(domain=pattern)
                after = await page.context.cookies()
            except WorkspaceError:
                raise
            except Exception as exc:
                raise WorkspaceError("Site cookie clearing could not be confirmed. Check the site before retrying.") from exc
            finally:
                # A lost response can follow a completed deletion. Invalidate
                # shared-cookie observations even when readback is uncertain.
                if cookie_effect_started:
                    self._touch_tab_activity(record, tab, human=True)
                    for affected_record in self._owner_records(owner):
                        changed = False
                        for affected_tab in affected_record.tabs.values():
                            affected_page = self._pages.get((affected_record.workspace_id, affected_tab.tab_id))
                            url = str(getattr(affected_page, "url", "") or affected_tab.url)
                            hostname = urlparse(url).hostname or ""
                            if affected_tab is tab or any(applies(d, hostname.lower()) for d in domains):
                                self._invalidate_observation(affected_record.workspace_id, affected_tab.tab_id)
                                affected_tab.last_observation_id = ""
                                affected_tab.human_input_revision = int(affected_tab.human_input_revision or 0) + 1
                                changed = True
                        if changed:
                            self.store.save(affected_record)
            remaining = sum(1 for cookie in after if applies(cookie.get("domain")))
            removed = sum(1 for cookie in before if applies(cookie.get("domain"))) - remaining
            return {"domain": host, "cleared": max(0, removed), "remaining": remaining}

    def _viewport_state(
        self,
        owner: str,
        runtime: Any,
        *,
        geometry_revision: int | None = None,
    ) -> dict[str, Any]:
        """Project the manager/backend's current shared owner-window geometry."""
        runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
        live_runtime = self.liveview.runtime(runtime_id) if runtime_id else None
        revision = (
            int(geometry_revision)
            if geometry_revision is not None
            else self._liveview_geometry_revision.get(
                runtime_id,
                int(getattr(live_runtime, "geometry_revision", 0) or 0),
            )
        )
        return {
            "width": int(getattr(runtime, "viewport_width", 0) or 0),
            "height": int(getattr(runtime, "viewport_height", 0) or 0),
            "outer_width": int(
                getattr(runtime, "window_width", 0)
                or getattr(live_runtime, "window_width", 0)
                or 0
            ),
            "outer_height": int(
                getattr(runtime, "window_height", 0)
                or getattr(live_runtime, "window_height", 0)
                or 0
            ),
            "geometry_revision": revision,
            "screen_width": int(getattr(runtime, "display_width", 0) or 0),
            "screen_height": int(getattr(runtime, "display_height", 0) or 0),
            "dynamic": bool(getattr(runtime, "dynamic_viewport", False)),
            "scope": "owner_browser_window",
            "mobile_emulation": False,
        }

    def _size_mode(self, runtime_id: str) -> dict[str, Any]:
        mode = self._liveview_size_mode_by_runtime.get(str(runtime_id or ""))
        return dict(mode) if mode else {"mode": "dynamic", "source": "", "revision": 0}

    def _record_size_mode(
        self,
        runtime_id: str,
        mode: str,
        *,
        source: str,
        width: int = 0,
        height: int = 0,
        preset: str = "",
    ) -> dict[str, Any]:
        """Record who last chose the shared window's size mode.

        Revisions are wall-clock milliseconds, kept strictly increasing, so a
        viewer that outlives a server restart never mistakes a new choice for
        one it has already applied.
        """
        runtime_id = str(runtime_id or "")
        if not runtime_id:
            return self._size_mode(runtime_id)
        previous = int(self._size_mode(runtime_id).get("revision") or 0)
        entry: dict[str, Any] = {
            "mode": "fixed" if mode == "fixed" else "dynamic",
            "source": source,
            "revision": max(previous + 1, int(time.time() * 1000)),
        }
        if entry["mode"] == "fixed":
            entry.update(width=int(width), height=int(height), preset=str(preset or ""))
        self._liveview_size_mode_by_runtime[runtime_id] = entry
        return dict(entry)

    def _sizing_view_page(self, runtime_id: str) -> tuple[Any | None, Any | None]:
        """The watching viewer that sizes the window, and the page it shows."""
        view_id = self._viewport_owner_id(runtime_id)
        view = self.live_views.find(view_id) if view_id else None
        if view is None or view.runtime_id != runtime_id:
            return None, None
        return view, self._pages.get((view.workspace_id, view.tab_id))

    async def _measured_viewport_state(
        self,
        owner: str,
        runtime: Any,
        page: Any,
        *,
        geometry_revision: int | None = None,
    ) -> dict[str, Any]:
        """The shared window geometry, with the page's size read live.

        ``_viewport_state`` mirrors what the last resize transaction accepted.
        A live viewer, a window manager, or anything outside that transaction
        can change the window without updating the mirror, so a report of the
        page's current size must come from the page. ``measured`` says which
        one the caller got.
        """
        from ascended_browser._app.browser_workspace.backend import _page_geometry

        state = self._viewport_state(owner, runtime, geometry_revision=geometry_revision)
        try:
            geometry = await _bounded(_page_geometry(page), 3.0)
        except Exception:
            state["measured"] = False
            return state
        state.update({
            key: int(geometry[key])
            for key in ("width", "height", "outer_width", "outer_height")
        })
        state["measured"] = True
        return state

    def _human_native_input_present(self, owner: str, runtime_id: str = "") -> bool:
        """Whether a person currently holds the shared browser's native input.

        Shared control keeps this as a *presence* signal, not a fence: agent
        page actions proceed, but window-level mutations (sizing) defer to the
        person while they are here, and agent preconditions re-resolve against
        fresh state (human_input_revision) instead of trusting stale snapshots.
        """
        if runtime_id:
            if self._liveview_controller_by_runtime.get(runtime_id):
                return True
            if self._viewport_owner_id(runtime_id):
                return True
        return any(
            candidate.lease and candidate.lease.actor == "user"
            for owner_record in self._owner_records(owner)
            for candidate in owner_record.tabs.values()
        )

    def _assert_agent_viewport_authority(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        actor: str,
        actor_id: str,
        runtime_id: str,
    ) -> tuple[WorkspaceRecord, TabRecord]:
        """Recheck agent sizing authority at the manager's effect boundary."""
        if actor not in {"parent", "worker"}:
            raise BrowserViewportError(
                "Only an agent tab lease may change the browser viewport",
                error_kind="browser_actor_conflict",
            )
        try:
            tab = self.assert_tab_access(
                owner,
                session_id,
                tab_id,
                actor=actor,
                actor_id=actor_id,
            )
        except WorkspaceError as exc:
            raise BrowserViewportError(
                str(exc), error_kind="browser_actor_conflict",
            ) from exc
        record = self.record(owner, session_id)
        return record, tab

    def _invalidate_owner_geometry_observations(self, owner: str) -> None:
        """Fence every cached coordinate/ref derived from the old window size."""
        from ascended_browser._app.browser_click_helpers import invalidate_refs

        for owner_record in self._owner_records(owner):
            for owner_tab in owner_record.tabs.values():
                self._invalidate_observation(owner_record.workspace_id, owner_tab.tab_id)
                invalidate_refs(
                    workspace_id=owner_record.workspace_id,
                    tab_id=owner_tab.tab_id,
                )
                owner_tab.last_observation_id = ""
            self.store.save(owner_record)

    async def _resize_owner_viewport_locked(
        self,
        owner: str,
        record: WorkspaceRecord,
        tab: TabRecord,
        page: Any,
        *,
        runtime_id: str,
        width: int,
        height: int,
        previous_revision: int,
    ) -> dict[str, Any]:
        """Apply and commit one resize while effect/action/owner locks are held."""
        try:
            geometry = await self.backend.resize_user_view(owner, page, width, height)
        except BaseException:
            # The daemon may have committed native/capture geometry before a
            # later Playwright content readback failed or this request was
            # cancelled. Adopt its advanced revision and fence every old ref
            # even though the caller must receive a truthful failed outcome.
            failed_runtime = self.liveview.runtime(runtime_id)
            failed_revision = int(
                getattr(failed_runtime, "geometry_revision", 0) or 0
            )
            if failed_revision != previous_revision:
                if failed_revision > previous_revision:
                    self._liveview_geometry_revision[runtime_id] = failed_revision
                self._invalidate_owner_geometry_observations(owner)
            raise
        refreshed_runtime = self.liveview.runtime(runtime_id)
        daemon_revision = int(
            getattr(refreshed_runtime, "geometry_revision", 0) or 0
        )
        daemon_revisions = bool(
            dict(getattr(refreshed_runtime, "capabilities", {}) or {}).get(
                "geometry_revision", False,
            )
        )
        # From this point the native resize may have changed layout. Invalidate
        # before validating the daemon's revision so even a broken/backwards
        # response cannot leave coordinate or element evidence reusable.
        if not daemon_revisions or daemon_revision != previous_revision:
            self._invalidate_owner_geometry_observations(owner)
        if daemon_revisions and daemon_revision < previous_revision:
            raise LiveViewUnavailable("The live-view geometry state moved backwards")
        # A native no-op keeps the daemon revision unchanged. Echo that exact
        # revision so viewers do not wait for a frame boundary that will never
        # be published. Legacy/fake runtimes retain the monotonic fallback.
        next_revision = daemon_revision if daemon_revisions else previous_revision + 1
        self._liveview_geometry_revision[runtime_id] = next_revision
        return {
            "width": int(geometry.get("width") or 0),
            "height": int(geometry.get("height") or 0),
            "outer_width": int(geometry.get("outer_width") or 0),
            "outer_height": int(geometry.get("outer_height") or 0),
            "geometry_revision": next_revision,
            "resized": next_revision != previous_revision,
        }

    async def viewport(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        action: str,
        *,
        width: int | None = None,
        height: int | None = None,
        preset: str = "",
        actor: str = "parent",
        actor_id: str = "parent",
    ) -> dict[str, Any]:
        """Read or change responsive layout without changing browser identity.

        ``set`` takes a ``preset`` (the live view's Phone / Tablet / Desktop
        profiles) or explicit dimensions and holds that size as the shared
        window's fixed mode, including while a person watches: their panel
        shows it the way it shows their own profile choice, and their own
        panel resize returns the window to dynamic. ``restore`` returns to
        dynamic: a watching panel fits the window to itself again; with nobody
        watching, the window returns to its launch size.
        """
        action = str(action or "").strip().lower()
        if action not in {"get", "set", "restore"}:
            raise BrowserViewportError(
                "Viewport action must be get, set, or restore",
                error_kind="invalid_request",
            )
        record = await self.ensure_awake(owner, session_id)
        try:
            self.assert_tab_access(
                owner, session_id, tab_id, actor=actor, actor_id=actor_id,
            )
        except WorkspaceError as exc:
            raise BrowserViewportError(
                str(exc), error_kind="browser_actor_conflict",
            ) from exc
        runtime = self.backend.runtime(owner)
        if runtime is None:
            raise BrowserViewportError(
                "Owner browser runtime is not active",
                error_kind="runtime_restarted",
            )
        runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
        emulation_key = (record.workspace_id, tab_id)
        cleared_emulation: dict[str, Any] | None = None
        if action == "restore" and self.emulation_for(owner, session_id, tab_id):
            # Restore means "back to the real thing": size and emulation both.
            cleared_emulation = await self.emulate(
                owner, session_id, tab_id, clear=True, actor=actor, actor_id=actor_id,
            )

        def _emulation_only(message: str) -> dict[str, Any]:
            return {
                **cleared_emulation, "action": "restore", "available": False, "resized": False,
                "emulation_cleared": True,
                "message": f"Emulation cleared. {message}",
            }

        if action == "get":
            effect_barrier = self._runtime_effect_barrier(owner)
            async with effect_barrier.shared(), self._action_lock(record.workspace_id, tab_id):
                try:
                    self.assert_tab_access(
                        owner, session_id, tab_id, actor=actor, actor_id=actor_id,
                    )
                except WorkspaceError as exc:
                    raise BrowserViewportError(
                        str(exc), error_kind="browser_actor_conflict",
                    ) from exc
                current = self.backend.runtime(owner)
                if current is None or int(getattr(current, "generation", 0) or 0) != int(
                    getattr(runtime, "generation", 0) or 0
                ):
                    raise BrowserViewportError(
                        "The browser restarted while viewport state was requested",
                        error_kind="runtime_restarted",
                    )
                current_runtime_id = str(
                    getattr(current, "liveview_runtime_id", "") or ""
                )
                # A read must not wake, revive or reload the tab: measure the
                # page only if it is already open.
                page = self._pages.get((record.workspace_id, tab_id))
                live_runtime = (
                    self.liveview.runtime(current_runtime_id)
                    if current_runtime_id else None
                )
                available = bool(
                    getattr(current, "dynamic_viewport", False)
                    and supports_dynamic_viewport(live_runtime)
                )
                return {
                    "action": action,
                    "available": available,
                    "requires_new_runtime": not bool(getattr(current, "dynamic_viewport", False)),
                    **(
                        await self._measured_viewport_state(owner, current, page)
                        if page is not None
                        else {**self._viewport_state(owner, current), "measured": False}
                    ),
                    "size_mode": self._size_mode(current_runtime_id),
                    "emulation": self.emulation_for(owner, session_id, tab_id),
                }

        if not bool(getattr(runtime, "dynamic_viewport", False)):
            if cleared_emulation is not None:
                return _emulation_only("The window size is not dynamic on this runtime, so it was left as it is.")
            raise BrowserViewportError(
                "Dynamic viewport is unavailable for this runtime; enable it before the next safe browser launch",
                error_kind="viewport_unavailable",
                requires_new_runtime=True,
            )
        live_runtime = self.liveview.runtime(runtime_id) if runtime_id else None
        if not runtime_id or not supports_dynamic_viewport(live_runtime):
            if cleared_emulation is not None:
                return _emulation_only("The window size is not dynamic on this runtime, so it was left as it is.")
            raise BrowserViewportError(
                "Dynamic viewport is unavailable for this browser runtime",
                error_kind="viewport_unavailable",
            )
        if action == "restore":
            requested_width = int(getattr(runtime, "launch_viewport_width", 0) or 0)
            requested_height = int(getattr(runtime, "launch_viewport_height", 0) or 0)
            if requested_width <= 0 or requested_height <= 0:
                if cleared_emulation is not None:
                    return _emulation_only("This runtime has no verified launch viewport to restore, so the size was left as it is.")
                raise BrowserViewportError(
                    "This runtime has no verified launch viewport to restore",
                    error_kind="viewport_unavailable",
                )
        else:
            preset = str(preset or "").strip().lower()
            if preset:
                if preset not in VIEWPORT_PRESETS:
                    raise BrowserViewportError(
                        "Viewport preset must be phone, tablet, or desktop",
                        error_kind="invalid_request",
                    )
                width, height = VIEWPORT_PRESETS[preset]
            if width is None or height is None:
                raise BrowserViewportError(
                    "Viewport set requires a preset or both width and height",
                    error_kind="invalid_dimensions",
                )
            try:
                requested_width = int(width)
                requested_height = int(height)
            except (TypeError, ValueError) as exc:
                raise BrowserViewportError(
                    "Browser dimensions must be integers",
                    error_kind="invalid_dimensions",
                ) from exc
            if requested_width <= 0 or requested_height <= 0:
                raise BrowserViewportError(
                    "Browser dimensions must be positive integers",
                    error_kind="invalid_dimensions",
                )

        generation = int(getattr(runtime, "generation", 0) or 0)
        self._assert_agent_viewport_authority(
            owner,
            session_id,
            tab_id,
            actor=actor,
            actor_id=actor_id,
            runtime_id=runtime_id,
        )
        effect_barrier = self._runtime_effect_barrier(owner)
        resize_lock = self._liveview_resize_locks.setdefault(runtime_id, asyncio.Lock())
        owner_lock = self._liveview_switch_locks.setdefault(owner_key(owner), asyncio.Lock())
        async with (
            effect_barrier.exclusive(label="set_viewport"),
            resize_lock,
            self._action_lock(record.workspace_id, tab_id),
            owner_lock,
        ):
            record, tab = self._assert_agent_viewport_authority(
                owner,
                session_id,
                tab_id,
                actor=actor,
                actor_id=actor_id,
                runtime_id=runtime_id,
            )
            current = self.backend.runtime(owner)
            if (
                current is None
                or current is not runtime
                or int(getattr(current, "generation", 0) or 0) != generation
                or str(getattr(current, "liveview_runtime_id", "") or "") != runtime_id
            ):
                raise BrowserViewportError(
                    "The browser restarted while the viewport change waited",
                    error_kind="runtime_restarted",
                )
            tab, page = await self._get_tab(record, tab_id, revive=False)
            viewer, viewer_page = self._sizing_view_page(runtime_id)
            if action == "restore" and viewer is not None:
                # Dynamic means "follow the watching panel". The panel fits
                # the window to itself once it sees this choice; resizing to
                # the launch size first would only make it jump twice.
                size_mode = self._record_size_mode(runtime_id, "dynamic", source="agent")
                accepted = await self._measured_viewport_state(owner, current, page)
                result = {
                    "action": action,
                    "available": True,
                    **accepted,
                    "resized": False,
                    "clamped": False,
                    "size_mode": size_mode,
                    "emulation": self.emulation_for(owner, session_id, tab_id),
                    **({"emulation_cleared": True} if cleared_emulation is not None else {}),
                    "message": (
                        "Back to dynamic sizing: the live view panel a person is "
                        "watching now fits the window to itself."
                        + (" Emulation cleared." if cleared_emulation is not None else "")
                    ),
                }
                result["page"] = await self._observe_for_result(
                    record, tab, page, lock_held=True,
                )
                return result
            if viewer is not None:
                # The window is shared by every tab. Resize it through the page
                # the person is watching so their view is not switched to the
                # agent's tab; it is already bound for capture.
                if viewer_page is None:
                    raise BrowserViewportError(
                        "The live view is showing a page that is not open here; "
                        "the window was not resized",
                        error_kind="watched_tab_conflict",
                    )
                resize_page = viewer_page
            else:
                # Bind/select the page through the same backend path as human
                # live view without granting native input or changing the lease.
                try:
                    await self.backend.prepare_user_control(owner, page)
                except BrowserBackendError as exc:
                    raise BrowserViewportError(
                        str(exc), error_kind="native_resize_failed",
                    ) from exc
                resize_page = page
            live_runtime = self.liveview.runtime(runtime_id)
            previous_revision = self._liveview_geometry_revision.get(
                runtime_id,
                int(getattr(live_runtime, "geometry_revision", 0) or 0),
            )
            try:
                result = await self._resize_owner_viewport_locked(
                    owner,
                    record,
                    tab,
                    resize_page,
                    runtime_id=runtime_id,
                    width=requested_width,
                    height=requested_height,
                    previous_revision=previous_revision,
                )
            except LiveViewUnavailable as exc:
                raise BrowserViewportError(
                    str(exc), error_kind="geometry_conflict",
                ) from exc
            except BrowserBackendError as exc:
                raise BrowserViewportError(
                    str(exc), error_kind="native_resize_failed",
                ) from exc
            accepted = self._viewport_state(
                owner,
                current,
                geometry_revision=int(result["geometry_revision"]),
            )
            # Prefer the transaction's immediate readback over possibly older
            # supervisor/runtime mirrors.
            accepted.update({
                key: result[key]
                for key in ("width", "height", "outer_width", "outer_height")
            })
            result = {
                "action": action,
                "available": True,
                "requested_width": requested_width,
                "requested_height": requested_height,
                **accepted,
                "resized": bool(result["resized"]),
                "clamped": (
                    int(result["width"]) != requested_width
                    or int(result["height"]) != requested_height
                ),
                "size_mode": (
                    self._record_size_mode(
                        runtime_id, "fixed", source="agent",
                        width=requested_width, height=requested_height, preset=preset,
                    )
                    if action == "set"
                    else self._record_size_mode(runtime_id, "dynamic", source="agent")
                ),
                "emulation": self.emulation_for(owner, session_id, tab_id),
                **({"emulation_cleared": True} if cleared_emulation is not None else {}),
            }
            result["page"] = await self._observe_for_result(
                record, tab, page, lock_held=True,
            )
            return result

    async def _resize_embedded_view(
        self, owner: str, session_id: str, tab_id: str, width: int, height: int,
        *, lease_epoch: int, size_mode: str,
    ) -> dict[str, Any]:
        """Apply desktop guest geometry under the existing tab authority owner."""
        if int(lease_epoch or 0) <= 0:
            raise WorkspaceError("A current desktop user-control lease epoch is required")
        mode = str(size_mode or "fixed").strip().lower()
        if mode not in {"fixed", "dynamic"}:
            raise WorkspaceError("Unsupported browser sizing mode")
        width, height = int(width), int(height)
        if mode == "fixed" and not (320 <= width <= 5120 and 240 <= height <= 2560):
            raise WorkspaceError("Browser dimensions must be within 320–5120 by 240–2560")
        authority = {"lease_epoch": lease_epoch}
        record, tab, _ = self._assert_live_user_control(owner, session_id, tab_id, **authority)
        runtime = self.backend.runtime(owner)
        async with self._liveview_transition(
            owner, record.workspace_id, tab_id, label="resize_embedded_view", timeout=12.0,
        ):
            self._assert_live_user_control(owner, session_id, tab_id, **authority)
            _, page = await self._get_tab(record, tab_id)
            if self.backend.runtime(owner) is not runtime:
                raise WorkspaceError("The desktop browser restarted; reopen the tab")
            self._assert_live_user_control(owner, session_id, tab_id, **authority)
            cdp = await page.context.new_cdp_session(page)
            try:
                self._assert_live_user_control(owner, session_id, tab_id, **authority)
                if self.backend.runtime(owner) is not runtime:
                    raise WorkspaceError("The desktop browser restarted; reopen the tab")
                if mode == "fixed":
                    await cdp.send("Emulation.setDeviceMetricsOverride", {
                        "width": width, "height": height,
                        "deviceScaleFactor": 1, "mobile": False,
                    })
                else:
                    await cdp.send("Emulation.clearDeviceMetricsOverride")
                geometry = await page.evaluate("({width:innerWidth,height:innerHeight})")
                accepted_width, accepted_height = int(geometry["width"]), int(geometry["height"])
                if accepted_width <= 0 or accepted_height <= 0:
                    raise WorkspaceError("The desktop browser returned invalid viewport geometry")
                if mode == "fixed" and (accepted_width != width or accepted_height != height):
                    raise WorkspaceError("The desktop viewport change could not be confirmed")
            except WorkspaceError:
                raise
            except Exception as exc:
                raise WorkspaceError("The desktop viewport change could not be confirmed") from exc
            finally:
                await cdp.detach()
                from ascended_browser._app import browser_agent_cursor
                browser_agent_cursor.invalidate_viewport_size(page)
                self._invalidate_observation(record.workspace_id, tab_id)
                tab.last_observation_id = ""
                tab.human_input_revision = int(tab.human_input_revision or 0) + 1
                self.store.save(record)
            self._touch_tab_activity(record, tab, human=True)
            identity = self.backend.page_identity(owner, page)
            sizing_key = "embedded:" + str(identity.get("target_id") or id(page))
            if sizing_key not in self._liveview_size_mode_by_runtime:
                page.once("close", lambda _page=None, key=sizing_key:
                          self._liveview_size_mode_by_runtime.pop(key, None))
            result_mode = self._record_size_mode(
                sizing_key, mode, source="user", width=accepted_width, height=accepted_height,
            )
            return {"width": accepted_width, "height": accepted_height,
                    "available": True, "resized": True, "measured": True,
                    "viewport_control": True, "size_mode": result_mode}

    async def resize_live_view(
        self,
        view_id: str,
        owner: str,
        session_id: str,
        tab_id: str,
        width: int,
        height: int,
        *,
        geometry_revision: int = 0,
        lease_epoch: int = 0,
        runtime_generation: int = 0,
        controller_id: str = "",
        connection_generation: int = 0,
        size_mode: str = "",
    ) -> dict[str, Any]:
        """Resize one shared browser window, then invalidate all affected refs.

        ``size_mode`` is the viewer's mode for this request: "fixed" for a
        profile button or custom size, "dynamic" for a panel fit (including a
        person's panel resize breaking a fixed size). Older clients omit it
        and leave the recorded mode unchanged.
        """
        if getattr(self.backend.runtime(owner), "presentation", "") == "embedded" and not view_id:
            return await self._resize_embedded_view(
                owner, session_id, tab_id, width, height,
                lease_epoch=lease_epoch, size_mode=size_mode,
            )
        record, tab, view = self._assert_live_viewport_authority(
            view_id,
            owner,
            session_id,
            tab_id,
            lease_epoch=lease_epoch,
            runtime_generation=runtime_generation,
            controller_id=controller_id,
            connection_generation=connection_generation,
        )
        runtime = self.backend.runtime(owner)
        if runtime is None or view.runtime_id != str(getattr(runtime, "liveview_runtime_id", "") or ""):
            raise LiveViewUnavailable("The browser restarted; reopen the live view")
        live_runtime = self.liveview.runtime(view.runtime_id)
        current_revision = self._liveview_geometry_revision.get(
            view.runtime_id, int(getattr(live_runtime, "geometry_revision", 0) or 0),
        )
        if int(geometry_revision) != current_revision:
            raise WorkspaceError(
                f"Browser geometry revision is stale (current {current_revision})"
            )
        try:
            requested_width = int(width)
            requested_height = int(height)
        except (TypeError, ValueError) as exc:
            raise WorkspaceError("Browser dimensions must be integers") from exc
        resize_lock = self._liveview_resize_locks.setdefault(view.runtime_id, asyncio.Lock())
        effect_barrier = self._runtime_effect_barrier(owner)
        owner_lock = self._liveview_switch_locks.setdefault(
            owner_key(owner), asyncio.Lock(),
        )
        async with (
            effect_barrier.exclusive(priority=True, label="resize_live_view"),
            resize_lock,
            self._action_lock(record.workspace_id, tab_id),
            owner_lock,
        ):
            prior_viewport_owner = self._viewport_owner_id(view.runtime_id)
            record, tab, view = self._assert_live_viewport_authority(
                view_id,
                owner,
                session_id,
                tab_id,
                lease_epoch=lease_epoch,
                runtime_generation=runtime_generation,
                controller_id=controller_id,
                connection_generation=connection_generation,
                claim=True,
            )
            claimed_here = not prior_viewport_owner
            latest = self._liveview_geometry_revision.get(
                view.runtime_id, int(getattr(live_runtime, "geometry_revision", 0) or 0),
            )
            if int(geometry_revision) != latest:
                raise WorkspaceError(
                    f"Browser geometry revision is stale (current {latest})"
                )
            try:
                _, page = await self._get_tab(record, tab_id)
                result = await self._resize_owner_viewport_locked(
                    owner,
                    record,
                    tab,
                    page,
                    runtime_id=view.runtime_id,
                    width=requested_width,
                    height=requested_height,
                    previous_revision=latest,
                )
            except BaseException:
                if (
                    claimed_here
                    and not self._liveview_controller_by_runtime.get(view.runtime_id)
                    and self._liveview_viewport_owner_by_runtime.get(view.runtime_id)
                    == view.view_id
                ):
                    self._liveview_viewport_owner_by_runtime.pop(view.runtime_id, None)
                raise
            self._touch_tab_activity(record, tab, human=True)
            self.store.save(record)
            result.update({
                "viewport_control": True,
                "viewport_owner_present": True,
            })
            mode = str(size_mode or "").strip().lower()
            if mode in {"fixed", "dynamic"}:
                result["size_mode"] = self._record_size_mode(
                    view.runtime_id, mode, source="user",
                    width=requested_width, height=requested_height,
                )
            else:
                result["size_mode"] = self._size_mode(view.runtime_id)
        return result

    def authorize_live_input(
        self,
        view: LiveViewSession,
        message: dict[str, Any],
        *,
        connection_generation: int | None = None,
    ) -> dict[str, Any]:
        """Validate one remote input before committing its local consequences.

        Input reaches the native daemon only after this method returns.  A
        rejected packet therefore must be observationally inert here too: it
        must not consume the sequence used by a later valid packet, refresh a
        user-control lease/activity clock, or change the shortcut-state mirror.
        Keep all coercion and authority/geometry checks above the short,
        synchronous commit tail below.  That makes the manager's state match
        the daemon's effect boundary even when delayed packets arrive after a
        resize, reconnect, or control handoff.
        """
        _view, record, tab = self.resolve_live_view(view.view_id, view.owner)
        lease = tab.lease
        if not self._view_has_control(view, tab):
            raise WorkspaceError("User-control lease is stale")
        if lease.expires_at is not None and lease.expires_at <= time.time():
            raise WorkspaceError("User-control lease is expired")
        lease_epoch = int(lease.generation or 0)
        if int(message.get("lease_epoch") or 0) != lease_epoch:
            raise WorkspaceError("User-control lease epoch is stale")
        if (
            connection_generation is not None
            and int(connection_generation) != int(view.connection_generation)
        ):
            raise WorkspaceError("Live-view connection generation is stale")
        sequence = int(message.get("sequence") or 0)
        if sequence <= view.input_sequence:
            raise WorkspaceError("Input sequence is stale")
        kind = str(message.get("kind") or "")
        if kind not in {
            "pointer_move", "pointer_down", "pointer_up", "wheel",
            "key_down", "key_up", "text", "release_all",
        }:
            raise WorkspaceError("Unsupported live-view input")
        key = str(message.get("key") or "")[:64]
        lowered = key.lower()
        control_keys = {"control_l", "control_r"}
        meta_keys = {"super_l", "super_r", "meta_l", "meta_r"}
        shift_keys = {"shift_l", "shift_r"}
        alt_keys = {"alt_l", "alt_r"}
        # Do shortcut-policy evaluation against a proposed mirror.  A blocked
        # key must never make a later key look like a held modifier.
        next_pressed_keys = set(view.pressed_keys)
        if kind == "key_down":
            if lowered in meta_keys or lowered in {"f6", "f11", "f12"}:
                raise WorkspaceError("Browser or operating-system shortcut is blocked")
            if lowered in {"l", "t", "w", "n", "q"} and next_pressed_keys.intersection(control_keys):
                raise WorkspaceError("Browser chrome shortcut is blocked")
            if lowered in {"i", "j", "c", "k"} and next_pressed_keys.intersection(control_keys) and next_pressed_keys.intersection(shift_keys):
                raise WorkspaceError("Developer-tools shortcut is blocked")
            if lowered == "f4" and next_pressed_keys.intersection(control_keys | alt_keys):
                raise WorkspaceError("Window or tab close shortcut is blocked")
            next_pressed_keys.add(lowered)
        elif kind == "key_up":
            next_pressed_keys.discard(lowered)
        elif kind == "release_all":
            next_pressed_keys.clear()
        text = str(message.get("text") or "")
        if len(text) > 4096:
            raise WorkspaceError("Text input is too large")

        def unit(name: str) -> float:
            try:
                return max(0.0, min(1.0, float(message.get(name) or 0.0)))
            except (TypeError, ValueError):
                return 0.0

        # Coerce the complete daemon message before changing manager state.
        # Keep the established bounded/defaulting behavior, but make malformed
        # numeric payloads reject before they can consume a sequence.
        x = unit("x")
        y = unit("y")
        delta_x = max(-2000.0, min(2000.0, float(message.get("delta_x") or 0.0)))
        delta_y = max(-2000.0, min(2000.0, float(message.get("delta_y") or 0.0)))
        button = max(0, min(2, int(message.get("button") or 0)))
        location = max(0, min(3, int(message.get("location") or 0)))
        live_runtime = self.liveview.runtime(view.runtime_id)
        geometry_revision = int(getattr(live_runtime, "geometry_revision", 0) or 0)
        runtime = self.backend.runtime(view.owner)
        control_revision = int(getattr(live_runtime, "control_revision", 0) or 0)
        if (
            self._live_capabilities(runtime).get("control_revision")
            and int(message.get("control_revision") or 0) != control_revision
        ):
            raise WorkspaceError("Live-view control revision is stale")
        if (
            kind in {"pointer_move", "pointer_down", "pointer_up", "wheel"}
            and getattr(runtime, "dynamic_viewport", False)
            and int(message.get("geometry_revision") if message.get("geometry_revision") is not None else -1)
            != geometry_revision
        ):
            raise WorkspaceError("Browser geometry changed; retry input against the current frame")
        active_connection = int(
            view.connection_generation if connection_generation is None else connection_generation
        )
        # An earlier direct-lane (RTC) input this one must not overtake. The
        # daemon holds the input until that sequence is resolved; anything but
        # a strictly earlier sequence is dropped rather than trusted.
        try:
            after_sequence = int(message.get("after_sequence") or 0)
        except (TypeError, ValueError):
            after_sequence = 0
        if not 0 < after_sequence < sequence:
            after_sequence = 0
        authorized = {
            "type": "input", "sequence": sequence, "kind": kind,
            "x": x, "y": y,
            "delta_x": delta_x,
            "delta_y": delta_y,
            "button": button,
            "key": key, "code": str(message.get("code") or "")[:64],
            "location": location,
            "repeat": bool(message.get("repeat")),
            "shift_key": bool(message.get("shift_key")),
            "control_key": bool(message.get("control_key")),
            "alt_key": bool(message.get("alt_key")),
            "meta_key": bool(message.get("meta_key")),
            "positioned": bool(message.get("positioned")),
            "text": text, "lease_epoch": lease_epoch,
            **({"after_sequence": after_sequence} if after_sequence else {}),
            "runtime_generation": int(view.runtime_generation),
            "controller_id": view.view_id,
            "connection_generation": active_connection,
            "tab_id": view.tab_id,
            "control_revision": control_revision,
            "geometry_revision": geometry_revision,
        }

        # Commit only an accepted input.  This method has no await points, so
        # the sequence, key mirror, group lease refresh, and activity update
        # form one event-loop transaction after all validation above succeeds.
        view.input_sequence = sequence
        view.pressed_keys.clear()
        view.pressed_keys.update(next_pressed_keys)
        deadline = self._user_control_deadline()
        for member in self._takeover_group_tabs(record, tab):
            if (
                member.lease is not None
                and member.lease.actor == "user"
                and member.lease.actor_id == view.owner
            ):
                member.lease.expires_at = deadline
        # Pointer motion is high-frequency transport feedback, not evidence of
        # deliberate continued use. Other accepted input types constitute
        # activity; persist them at a bounded cadence so the six-hour policy
        # survives a restart without turning a scroll into an fsync storm.
        if kind != "pointer_move":
            # Shared control: state-changing human input invalidates every
            # agent snapshot taken before it. Actions capture this revision at
            # observation time and re-resolve when it moved, so a person
            # typing, scrolling, or clicking between observe and act is never
            # overwritten from stale page state.
            tab.human_input_revision = int(tab.human_input_revision or 0) + 1
            self._touch_tab_activity(record, tab, human=True, persist=True)
        # The page observer reports the actual DOM change for both signaling
        # and direct RTC input. If it has not installed yet (or cannot install
        # on this page), conservatively protect accepted editing keystrokes.
        # This is the exceptional path: ordinary per-key input never waits on
        # a manifest fsync, and a daemon rejection cannot dirty an observed
        # page because its event never fires.
        page = self._pages.get((record.workspace_id, tab.tab_id))
        if page is None or id(page) not in self._live_form_dirty_installed_pages:
            editing_key = kind == "key_down" and (
                lowered in {"backspace", "delete", "return", "enter", "space"}
                or (
                    len(key) == 1
                    and not next_pressed_keys.intersection(alt_keys | meta_keys)
                    and (
                        not next_pressed_keys.intersection(control_keys)
                        or lowered in {"v", "x", "z", "y"}
                    )
                )
            )
            if (kind == "text" and bool(text)) or editing_key:
                self._mark_live_form_dirty(record, tab)
        return authorized

    def authorize_live_clipboard(
        self,
        view: LiveViewSession,
        message: dict[str, Any],
        *,
        connection_generation: int | None = None,
    ) -> dict[str, Any]:
        """Authorize one clipboard read for the current controller.

        Copying inside the remote page lands on the isolated X clipboard, which
        the user's own machine can never see; this read is the bridge that
        makes remote copy work at all. Authority matches native input exactly
        (current controller, live lease, current connection), because clipboard
        text is page data. The content itself never passes through here — this
        returns only the authorized wire request for the daemon's effect
        boundary, which revalidates the same grant before reading.
        """
        _view, _record, tab = self.resolve_live_view(view.view_id, view.owner)
        lease = tab.lease
        if not self._view_has_control(view, tab):
            raise WorkspaceError("User-control lease is stale")
        if lease.expires_at is not None and lease.expires_at <= time.time():
            raise WorkspaceError("User-control lease is expired")
        if int(message.get("lease_epoch") or 0) != int(lease.generation or 0):
            raise WorkspaceError("User-control lease epoch is stale")
        if (
            connection_generation is not None
            and int(connection_generation) != int(view.connection_generation)
        ):
            raise WorkspaceError("Live-view connection generation is stale")
        return {
            "type": "clipboard_read",
            "clipboard_request_id": max(0, min(2**53 - 1, int(message.get("clipboard_request_id") or 0))),
            "lease_epoch": int(lease.generation or 0),
            "runtime_generation": int(view.runtime_generation),
            "controller_id": view.view_id,
            "connection_generation": int(
                view.connection_generation if connection_generation is None else connection_generation
            ),
            "tab_id": view.tab_id,
        }

    async def live_text_target(
        self,
        view: LiveViewSession,
        message: dict[str, Any],
        *,
        connection_generation: int | None = None,
    ) -> dict[str, Any]:
        """Inspect an authorized touch target without exposing page content.

        The live frame uses a transparent textarea to receive mobile IME and
        composition events. A touch-start probe also distinguishes a native
        slider/media surface from ordinary scroll content before moving it.
        """
        _view, record, tab = self.resolve_live_view(view.view_id, view.owner)
        lease = tab.lease
        if not self._view_has_control(view, tab):
            raise WorkspaceError("User-control lease is stale")
        if lease.expires_at is not None and lease.expires_at <= time.time():
            raise WorkspaceError("User-control lease is expired")
        if int(message.get("lease_epoch") or 0) != int(lease.generation or 0):
            raise WorkspaceError("User-control lease epoch is stale")
        if (
            connection_generation is not None
            and int(connection_generation) != int(view.connection_generation)
        ):
            raise WorkspaceError("Live-view connection generation is stale")
        page = self._pages.get((record.workspace_id, tab.tab_id))
        runtime = self.backend.runtime(view.owner)
        bound_page = getattr(runtime, "bound_liveview_page", None) if runtime else None
        probe_drag = message.get("probe_drag") is True
        unavailable = {"drag_target": False} if probe_drag else {"editable": False, "input_mode": "none"}
        if page is None or (bound_page is not None and bound_page is not page):
            return unavailable
        try:
            result = await page.evaluate(
                """({x, y, probeDrag}) => {
                  const descend = (candidate) => {
                    let el = candidate;
                    while (el?.shadowRoot?.activeElement) el = el.shadowRoot.activeElement;
                    return el;
                  };
                  const px = Math.max(0, Math.min(innerWidth - 1, x * innerWidth));
                  const py = Math.max(0, Math.min(innerHeight - 1, y * innerHeight));
                  const active = descend(document.activeElement);
                  let pointed = descend(document.elementFromPoint(
                    px, py
                  ));
                  if (probeDrag) {
                    const slider = pointed?.closest?.('input[type="range"], [role="slider"], video');
                    return {drag_target: !!slider && !slider.disabled};
                  }
                  const label = pointed?.closest?.('label');
                  if (label?.control) pointed = descend(label.control);
                  const activeRect = active?.getBoundingClientRect?.();
                  const activeAtPoint = active && active !== document.body
                    && active !== document.documentElement && activeRect
                    && px >= activeRect.left && px <= activeRect.right
                    && py >= activeRect.top && py <= activeRect.bottom;
                  const candidate = activeAtPoint ? active : pointed;
                  const editable = candidate?.closest?.('input,textarea,[contenteditable]') || null;
                  if (!editable || editable.disabled || editable.readOnly
                      || editable.getAttribute?.('contenteditable') === 'false') {
                    return {editable: false, input_mode: 'none'};
                  }
                  const type = String(editable.type || '').toLowerCase();
                  if (['button','checkbox','color','file','hidden','image','radio',
                       'range','reset','submit'].includes(type)) {
                    return {editable: false, input_mode: 'none'};
                  }
                  const declared = String(editable.inputMode || '').toLowerCase();
                  const modes = new Set(['text','search','email','tel','url','numeric','decimal']);
                  const fallback = ['search','email','tel','url'].includes(type) ? type
                    : (type === 'number' ? 'decimal' : 'text');
                  return {editable: true, input_mode: modes.has(declared) ? declared : fallback};
                }""",
                {
                    "x": max(0.0, min(1.0, float(message.get("x") or 0.0))),
                    "y": max(0.0, min(1.0, float(message.get("y") or 0.0))),
                    "probeDrag": probe_drag,
                },
            )
        except Exception:
            log.debug("Could not inspect the live text target", exc_info=True)
            return unavailable
        if probe_drag:
            return {"drag_target": isinstance(result, dict) and result.get("drag_target") is True}
        if not isinstance(result, dict) or not result.get("editable"):
            return {"editable": False, "input_mode": "none"}
        mode = str(result.get("input_mode") or "text")
        if mode not in {"text", "search", "email", "tel", "url", "numeric", "decimal"}:
            mode = "text"
        return {"editable": True, "input_mode": mode}

    async def apply_live_wheel(
        self,
        view: LiveViewSession,
        authorized: dict[str, Any],
        notify: Any = None,
        fallback: Any = None,
    ) -> bool:
        """Queue one authorized wheel for exact browser CSS-pixel scrolling.

        XTEST can only fake discrete wheel notches, and Firefox's automation
        wheel command applies a platform multiplier even when given pixel
        values. Both make a short phone drag jump by many times its distance.
        The browser protocol instead scrolls the nearest scrollable ancestor
        at the cursor by the exact CSS-pixel delta. Custom/non-DOM wheel
        surfaces retain Playwright's trusted wheel command as a fallback.

        Returns True when the wheel was accepted into the pixel pipeline (the
        relay then skips the daemon and the pump acknowledges once applied);
        False means the relay must fall back to the daemon's notch path. If an
        accepted asynchronous batch later fails, ``fallback`` receives that
        exact merged, authorized batch so the gesture is not discarded. The
        pump merges deltas that arrive while a round trip is in flight.
        Pointer positioning stays on the native low-latency channel. The DOM
        path uses the authorized coordinates directly; only the custom-surface
        fallback updates Playwright's cached mouse position with zero steps,
        avoiding Camoufox's deliberate motion humanization.

        Every fallback decision is logged at warning level: a silent fallback
        reads identically to a broken scroll path from the outside, and this
        path has already hidden one failure behind debug logging.
        """
        if get_setting("browser_liveview_pixel_wheel", True) is not True:
            return False
        runtime = self.backend.runtime(view.owner)
        # The manager's selected tab mapping is the canonical page identity.
        # ``bound_liveview_page`` is a backend binding cache and can be empty
        # after startup/recovery even though takeover already selected and
        # fenced this page. Requiring that cache made the smooth path
        # unreachable in the real app, leaving small desktop/phone deltas to
        # the quantized native fallback. A conflicting non-empty binding still
        # fails closed rather than scrolling a different browser tab.
        page = self._pages.get((view.workspace_id, view.tab_id))
        if page is None:
            self._log_wheel_fallback(view, "selected tab has no active page")
            return False
        bound_page = getattr(runtime, "bound_liveview_page", None) if runtime else None
        if bound_page is not None and bound_page is not page:
            self._log_wheel_fallback(view, "backend binding targets a different page")
            return False
        width = int(getattr(runtime, "viewport_width", 0) or 0)
        height = int(getattr(runtime, "viewport_height", 0) or 0)
        if width <= 0 or height <= 0:
            self._log_wheel_fallback(view, "no observed viewport geometry")
            return False
        state = self._liveview_wheel_states.get(view.view_id)
        if state is None or state.get("page") is not page:
            state = {
                "page": page, "active": False, "pending": None,
                "failed_until": 0.0,
                "task": None,
            }
            self._liveview_wheel_states[view.view_id] = state
        if state["failed_until"] > time.monotonic():
            self._log_wheel_fallback(view, "automation channel recently failed; cooling down")
            return False
        try:
            authorized_at = time.monotonic()
            x = max(0.0, min(float(width) - 1.0, float(authorized.get("x") or 0.0) * width))
            y = max(0.0, min(float(height) - 1.0, float(authorized.get("y") or 0.0) * height))
            delta_x = float(authorized.get("delta_x") or 0.0)
            delta_y = float(authorized.get("delta_y") or 0.0)
            sequence = int(authorized.get("sequence") or 0)
            runtime_generation = int(authorized.get("runtime_generation") or 0)
        except (TypeError, ValueError):
            self._log_wheel_fallback(view, "malformed wheel authorization")
            return False
        pending = state["pending"]
        if pending is None:
            state["pending"] = {
                "x": x, "y": y, "delta_x": delta_x, "delta_y": delta_y,
                "sequence": sequence, "runtime_generation": runtime_generation,
                "notify": notify, "fallback": fallback,
                "authorized": dict(authorized),
                "authorized_at": authorized_at,
            }
        else:
            # Merge into the in-flight batch: the total scroll is preserved,
            # the cursor targets the newest position, and one round trip
            # covers everything that arrived while the last was applying.
            pending["delta_x"] += delta_x
            pending["delta_y"] += delta_y
            pending["x"], pending["y"] = x, y
            pending["sequence"] = max(pending["sequence"], sequence)
            pending["runtime_generation"] = runtime_generation
            if notify is not None:
                pending["notify"] = notify
            if fallback is not None:
                pending["fallback"] = fallback
            # Use the newest authority envelope while preserving the complete
            # accumulated delta. The daemon revalidates it immediately before
            # applying a fallback effect, including after transfer or resize.
            merged_authorized = dict(authorized)
            merged_authorized.update({
                "x": x, "y": y,
                "delta_x": pending["delta_x"],
                "delta_y": pending["delta_y"],
                "sequence": pending["sequence"],
            })
            pending["authorized"] = merged_authorized
        if state["active"] and state.get("task") is None:
            # A pump vanished without clearing its flag (the pre-fix bug, or
            # any future lifecycle mistake). Recover instead of leaving every
            # future wheel queued behind a pump that will never run.
            log.warning("Live wheel pump for view %s vanished mid-flight; respawning", view.view_id)
            state["active"] = False
        if not state["active"]:
            state["active"] = True
            # Hold a strong reference for the pump's whole lifetime: the event
            # loop keeps only weak references to tasks, and a pump collected
            # mid-await would leave `active` stuck True with pending wheels
            # that nothing will ever apply — scroll dies silently.
            task = asyncio.create_task(self._pump_live_wheel(view, state))
            state["task"] = task

            def clear_finished_task(
                finished: asyncio.Task,
                wheel_state: dict[str, Any] = state,
            ) -> None:
                # Do not let an older task's completion callback erase a
                # replacement pump that was installed in the meantime.
                if wheel_state.get("task") is finished:
                    wheel_state["task"] = None

            task.add_done_callback(clear_finished_task)
        return True

    def _log_wheel_fallback(self, view: LiveViewSession, reason: str) -> None:
        """Rate-limit one visible line per fallback cause per viewer."""
        key = view.view_id
        now = time.monotonic()
        last = self._liveview_wheel_fallback_logs.get(key, (reason, 0.0))
        if last[0] == reason and now - last[1] < 10.0:
            return
        self._liveview_wheel_fallback_logs[key] = (reason, now)
        log.warning(
            "Live pixel wheel unavailable for view %s: %s; using daemon notch path",
            key, reason,
        )

    def _live_wheel_authority_current(
        self,
        view: LiveViewSession,
        authorized: dict[str, Any],
        page: Any,
    ) -> bool:
        """Revalidate asynchronous pixel-wheel authority at effect time."""
        try:
            current_view, record, tab = self.resolve_live_view(view.view_id, view.owner)
            runtime = self.backend.runtime(view.owner)
            live_runtime = self.liveview.runtime(view.runtime_id)
            lease = tab.lease
            return bool(
                current_view is view
                and self._pages.get((record.workspace_id, tab.tab_id)) is page
                and tab.tab_id == str(authorized.get("tab_id") or "")
                and self._view_has_control(view, tab)
                and secrets.compare_digest(
                    self._liveview_controller_by_runtime.get(view.runtime_id, ""),
                    view.view_id,
                )
                and int(getattr(lease, "generation", 0) or 0)
                == int(authorized.get("lease_epoch") or 0)
                and int(getattr(runtime, "generation", 0) or 0)
                == int(authorized.get("runtime_generation") or 0)
                and int(view.connection_generation)
                == int(authorized.get("connection_generation") or 0)
                and int(getattr(live_runtime, "geometry_revision", 0) or 0)
                == int(authorized.get("geometry_revision") or 0)
            )
        except (LiveViewUnavailable, WorkspaceError, TypeError, ValueError):
            return False

    async def _pump_live_wheel(self, view: LiveViewSession, state: dict[str, Any]) -> None:
        """Apply queued wheel batches in order, one juggler round trip each.

        The read-pending and exit steps are synchronous, so a wheel accepted
        between pump iterations either finds the pump alive (it loops and
        picks the batch up) or finds it fully exited (a new pump is spawned).
        No batch is ever dropped or applied twice.
        """
        try:
            while True:
                pending = state["pending"]
                state["pending"] = None
                if pending is None:
                    return
                page = state["page"]
                if not self._live_wheel_authority_current(
                    view, pending["authorized"], page,
                ):
                    log.warning(
                        "Discarding stale live pixel wheel for view %s before effect",
                        view.view_id,
                    )
                    return
                try:
                    if pending["delta_x"] or pending["delta_y"]:
                        playwright_started_at = time.monotonic()
                        argument = {
                            "x": pending["x"], "y": pending["y"],
                            "dx": pending["delta_x"], "dy": pending["delta_y"],
                        }
                        exact = await page.evaluate(
                            _LIVE_WHEEL_APPLY_JS, argument,
                        )
                        if exact is None:
                            exact = await page.evaluate(
                                _LIVE_WHEEL_INSTALL_AND_APPLY_JS, argument,
                            )
                        self._liveview_wheel_installed_pages.add(id(page))
                        if not exact:
                            # Canvas/maps and wheel-handler-only controls have
                            # no native DOM scroll range. Preserve their trusted
                            # event semantics as the narrow fallback.
                            await page.mouse.move(pending["x"], pending["y"], steps=0)
                            await page.mouse.wheel(pending["delta_x"], pending["delta_y"])
                        effect_at = time.monotonic()
                    else:
                        playwright_started_at = effect_at = time.monotonic()
                except Exception as exc:
                    # The relay already accepted this batch, so falling back
                    # only on a later gesture loses the current scroll. Send
                    # this exact authorized batch through the daemon now; its
                    # effect boundary rejects stale control or geometry.
                    log.warning(
                        "Live pixel wheel failed for view %s (batch dy=%.0f); "
                        "falling back to the daemon notch path: %s: %s",
                        view.view_id, pending["delta_y"], type(exc).__name__, exc,
                    )
                    state["failed_until"] = time.monotonic() + 2.0
                    # More deltas may have arrived while the failed wheel was
                    # awaiting the browser. They were already accepted by the
                    # relay too, so drain every queued batch through its own
                    # daemon fallback before stopping this pump.
                    failed = pending
                    while failed is not None:
                        callback = failed.get("fallback")
                        if callable(callback):
                            try:
                                result = callback(dict(failed["authorized"]))
                                if inspect.isawaitable(result):
                                    await result
                            except Exception:
                                log.warning(
                                    "Failed live wheel could not reach the daemon fallback for view %s",
                                    view.view_id,
                                    exc_info=True,
                                )
                        failed = state["pending"]
                        state["pending"] = None
                    return
                callback = pending.get("notify")
                if callable(callback):
                    try:
                        result = callback(
                            pending["sequence"], pending["runtime_generation"],
                            {
                                "authorization_to_schedule_ms": max(
                                    0.0, (playwright_started_at - pending["authorized_at"]) * 1000,
                                ),
                                "playwright_to_effect_ms": max(
                                    0.0, (effect_at - playwright_started_at) * 1000,
                                ),
                                "authorization_to_effect_ms": max(
                                    0.0, (effect_at - pending["authorized_at"]) * 1000,
                                ),
                            },
                        )
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        log.warning(
                            "Wheel acknowledgement could not be delivered for view %s",
                            view.view_id, exc_info=True,
                        )
        finally:
            state["active"] = False

    def renew_live_control(
        self, view: LiveViewSession, *, connection_generation: int | None = None,
    ) -> None:
        """Keep an actively connected human lease alive without rotating it."""
        if (
            connection_generation is not None
            and int(connection_generation) != int(view.connection_generation)
        ):
            raise WorkspaceError("Live-view connection generation is stale")
        _view, record, tab = self.resolve_live_view(view.view_id, view.owner)
        lease = tab.lease
        if not self._view_has_control(view, tab):
            raise WorkspaceError("User-control lease is stale")
        if lease.expires_at is not None and lease.expires_at <= time.time():
            raise WorkspaceError("User-control lease is expired")
        if lease and lease.actor == "user" and lease.actor_id == view.owner:
            deadline = self._user_control_deadline()
            for member in self._takeover_group_tabs(record, tab):
                if (
                    member.lease is not None
                    and member.lease.actor == "user"
                    and member.lease.actor_id == view.owner
                ):
                    member.lease.expires_at = deadline

    def update_live_metrics(self, view: LiveViewSession, raw: Any) -> None:
        """Keep one bounded, ephemeral WebRTC health snapshot for diagnostics."""
        if not isinstance(raw, dict):
            return
        numeric_allowed = {
            "framesDecoded", "framesDropped", "freezeCount", "totalFreezesDuration",
            "bytesReceived", "packetsReceived", "packetsLost", "jitter", "jitterMs",
            "retransmittedPacketsReceived", "nackCount", "pliCount", "framesPerSecond",
            "frameWidth", "frameHeight", "currentRoundTripTime", "sampled_at",
            "rttMs", "availableIncomingBitrate", "sampleWindowMs",
            "packetsReceivedDelta", "packetsLostDelta",
            "retransmittedPacketsReceivedDelta", "nackCountDelta", "pliCountDelta",
            "freezeCountDelta", "packetLossPercent", "kilobitsPerSecond", "rttDeltaMs",
            "keyFramesDecoded", "keyFramesDecodedDelta", "framesDecodedDelta",
            "qpSum", "qpPerFrame", "qpP50", "qpP95", "bitsPerPixel",
            "totalDecodeTime", "decodeWindowMs",
            "presentationHoldMs",
            "inputApplyMs",
            "inputAckP50", "inputAckP95", "pointerMoveAckP50", "pointerMoveAckP95",
            "inputQueueP50", "inputQueueP95", "inputNativeP50", "inputNativeP95",
            "encoderReconfigurations", "keyframeRequests", "keyframeToRTPP50",
            "keyframeToRTPP95", "pacerQueueBytes", "pacerQueuePackets",
            "pacerOldestMs", "pacerDroppedPackets", "pacerGOPResets",
            "pacerCatchupActive", "pacerCatchupActivations",
            "targetBitrate", "liveBitrate",
        }
        metrics: dict[str, float | str] = {}
        for key in numeric_allowed:
            try:
                value = float(raw[key])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value) and abs(value) < 1e16:
                metrics[key] = value
        enum_allowed = {
            "networkCondition": {"clean", "jittery", "cutting"},
            "iceProtocol": {"udp", "tcp"},
            "localCandidateType": {"host", "srflx", "prflx", "relay"},
            "remoteCandidateType": {"host", "srflx", "prflx", "relay"},
            "relayProtocol": {"udp", "tcp", "tls"},
            "codec": {"vp8", "h264"},
            "encoderBackend": {"vp8", "nvenc_h264", "vaapi_h264", "x264_h264", "gst-launch"},
        }
        for key, choices in enum_allowed.items():
            value = raw.get(key)
            if isinstance(value, str) and value in choices:
                metrics[key] = value
        reason = raw.get("networkReason")
        if (
            isinstance(reason, str)
            and re.fullmatch(r"[a-z0-9+%=., -]{1,160}", reason)
        ):
            metrics["networkReason"] = reason
        reconfiguration_reason = raw.get("encoderReconfigurationReason")
        if isinstance(reconfiguration_reason, str) and re.fullmatch(
            r"[a-z0-9_-]{1,80}", reconfiguration_reason,
        ):
            metrics["encoderReconfigurationReason"] = reconfiguration_reason
        view.metrics = metrics
        view.metrics_at = time.time()
        # Field trials need the delivered stream, not just the configured one:
        # the viewer's path (e.g. a relay) decides what the encoder may spend.
        if view.metrics_at - self._live_metrics_logged_at.get(view.view_id, 0.0) >= 10.0:
            self._live_metrics_logged_at[view.view_id] = view.metrics_at
            log.info(
                "Live view %s delivered: %s", view.view_id[:8],
                " ".join(f"{key}={metrics[key]:g}" if isinstance(metrics[key], float) else f"{key}={metrics[key]}"
                         for key in ("kilobitsPerSecond", "targetBitrate", "liveBitrate",
                                     "frameWidth", "frameHeight", "framesPerSecond",
                                     "qpP50", "qpP95", "rttMs", "packetLossPercent", "availableIncomingBitrate",
                                     "freezeCountDelta", "pacerOldestMs", "localCandidateType",
                                     "remoteCandidateType", "networkCondition")
                         if key in metrics),
            )

    async def _refresh_tab(
        self, record: WorkspaceRecord, tab: TabRecord, page: Any, *,
        agent: bool = False,
    ) -> None:
        tab.url = str(getattr(page, "url", "") or tab.url)
        # Runs on every observe and act; unbounded it makes both hang on a page
        # that has stopped answering.
        try:
            metadata = await asyncio.wait_for(self.backend.metadata(record.owner, page), timeout=10)
            tab.url = str(metadata.get("url") or tab.url)
            tab.title = str(metadata.get("title") or tab.title)
        except Exception: pass
        self._record_native_history(record, tab.url, tab.title)
        self._touch_tab_activity(record, tab, agent=agent)
        tab.restorability = "restorable_get" if tab.last_method == "GET" and not tab.dirty and not tab.uncertain else "non_restorable"

    def _record_native_history(self, record, url: str, title: str = "", *, visit: bool = False) -> None:
        if getattr(record, "_native_history_retired", False):
            return
        if not callable(getattr(self.backend, "runtime", None)):
            return
        try:
            if getattr(self.backend.runtime(record.owner), "presentation", "") == "embedded":
                self.store.record_native_visit(record.owner, url, title, visit=visit)
        except Exception:
            # History failure must be diagnosable without turning a successful
            # page navigation into a false failure or replacing damaged data.
            log.warning("Could not save native browser history", exc_info=True)

    async def quiesce_native_history_owner(self, owner: str) -> list[WorkspaceRecord]:
        """Fence captured native-page metadata before an account ownership change."""
        runtime = self.backend.runtime(owner)
        records = [record for record in self._records.values() if record.owner == owner]
        for record in records:
            record._native_history_retired = True
        try:
            if runtime is not None and getattr(runtime, "presentation", "") == "embedded":
                for record in records:
                    if record.status != "sleeping":
                        await self.sleep(owner, record.session_id)
                await self.backend.close_owner(owner)
        except BaseException:
            self.resume_native_history_records(records)
            raise
        return records

    @staticmethod
    def resume_native_history_records(records: list[WorkspaceRecord]) -> None:
        """Rejected account mutations retain the existing owner's history."""
        for record in records:
            record._native_history_retired = False

    def _worker_clocks(self, record: WorkspaceRecord, worker_id: str) -> ParkingClocks | None:
        """Lease clocks for one worker, kept separate from its compute budget.

        BudgetTracker already suspends the worker's own budget. These are the
        *lease* deadlines, which are a second, independent wall clock — without
        pausing them too, a two-hour user park burns a 29-minute idle lease and the
        worker is dead the moment the user finally answers.
        """
        if not worker_id or worker_id not in record.workers:
            return None
        key = (record.workspace_id, worker_id)
        clocks = self._worker_clocks_by_key.get(key)
        if clocks is None:
            worker = record.workers[worker_id]
            now = time.time()
            clocks = ParkingClocks(
                started_at=float(worker.get("started_at") or now),
                last_progress_at=float(worker.get("updated_at") or now),
            )
            self._worker_clocks_by_key[key] = clocks
        return clocks

    def _publish_worker_clocks(self, record: WorkspaceRecord, worker_id: str) -> None:
        clocks = self._worker_clocks_by_key.get((record.workspace_id, worker_id))
        worker = record.workers.get(worker_id)
        if clocks is None or worker is None:
            return
        worker["started_at"] = clocks.started_at
        worker["updated_at"] = clocks.last_progress_at

    def raise_attention(
        self, record: WorkspaceRecord, *, audience: str, reason: str,
        worker_id: str = "", kind: str = "", tab_id: str = "",
    ) -> Attention:
        # Polling or re-observing an unchanged challenge must not create a stack
        # of identical attention cards.
        for existing in record.attentions.values():
            if (
                not existing.resolved_at
                and existing.audience == audience
                and str(getattr(existing, "kind", "") or "") == str(kind or "")
                and str(getattr(existing, "tab_id", "") or "") == str(tab_id or "")
            ):
                return existing
        item = Attention(
            audience=audience, reason=reason, worker_id=worker_id,
            kind=kind, tab_id=tab_id,
        )
        record.attentions[item.attention_id] = item
        clocks = self._worker_clocks(record, worker_id)
        if clocks is not None:
            clocks.pause(item.raised_at)
            self._publish_worker_clocks(record, worker_id)
        self.store.save(record)
        return item

    def _unpark_worker(self, record: WorkspaceRecord, worker_id: str, at: float) -> None:
        """Resume lease clocks exactly once, whatever ended the park."""
        if not worker_id:
            return
        if any(
            other.worker_id == worker_id and not other.resolved_at
            for other in record.attentions.values()
        ):
            return  # still parked on another unresolved request for the same worker
        clocks = self._worker_clocks_by_key.get((record.workspace_id, worker_id))
        if clocks is None:
            return
        clocks.resume(at)
        self._publish_worker_clocks(record, worker_id)

    def deliver_attention(self, owner: str, session_id: str, attention_id: str) -> dict:
        record = self.record(owner, session_id); item = record.attentions.get(attention_id)
        if not item: raise WorkspaceError("Unknown attention request")
        if item.delivered_at is None:
            item.delivered_at = time.time()
            item.expires_at = item.delivered_at + (600 if item.audience == "parent" else 7200)
            self.store.save(record)
        return item.__dict__.copy()

    def resolve_attention(self, owner: str, session_id: str, attention_id: str, revision: int, resolution: str) -> dict:
        record = self.record(owner, session_id); item = record.attentions.get(attention_id)
        if not item: raise WorkspaceError("Unknown attention request")
        if item.revision != int(revision): raise WorkspaceError("Attention revision conflict")
        if item.resolved_at: return item.__dict__.copy()
        item.resolution = resolution; item.resolved_at = time.time(); item.revision += 1
        self._unpark_worker(record, item.worker_id, item.resolved_at)
        self.store.save(record); return item.__dict__.copy()

    def expire_attention(self, owner: str, session_id: str, attention_id: str, *, reason: str) -> dict:
        record = self.record(owner, session_id)
        item = record.attentions.get(attention_id)
        if not item or item.resolved_at:
            return item.__dict__.copy() if item else {}
        item.resolution = reason
        item.resolved_at = time.time()
        item.revision += 1
        if item.worker_id and item.worker_id in record.workers:
            worker = record.workers[item.worker_id]
            worker.update({"status": "blocked", "error": reason, "partial": True})
        self._unpark_worker(record, item.worker_id, item.resolved_at)
        self._settle_group(record)
        self.store.save(record)
        return item.__dict__.copy()

    def mark_group_claimed(self, owner: str, session_id: str, group_id: str, claimed: bool = True) -> bool:
        record = self.record(owner, session_id)
        group = record.active_group or {}
        if group.get("group_id") != group_id or group.get("status") != "terminal":
            return False
        group["completion_claimed"] = bool(claimed)
        group["claimed_at"] = time.time() if claimed else None
        self.store.save(record)
        return True

    def _settle_group(self, record: WorkspaceRecord) -> None:
        """Mark the notification group terminal only once it is delegation-quiescent.

        A group must not go terminal in the window where the parent is mid-way
        through appending a second cohort, or the completion wake fires against a
        batch that is still growing and the appended work gets its own second wake.
        """
        group = record.active_group or {}
        assignments = group.get("assignments") or []
        if not assignments:
            return
        if not all(item.get("status") in {"completed", "blocked", "cancelled"} for item in assignments):
            group.pop("terminal_pending_since", None)
            group["status"] = "active"
            return
        now = time.time()
        quiescence = float(get_setting("browser_workspace_group_quiescence_seconds", 1.0) or 1.0)
        since = group.get("terminal_pending_since")
        if since is None:
            group["terminal_pending_since"] = now
            self._schedule_resettle(record, quiescence)
            return
        if now - float(since) >= quiescence and now - float(group.get("updated_at") or 0) >= quiescence:
            group["status"] = "terminal"
            group["terminal_at"] = now
        else:
            self._schedule_resettle(record, quiescence)

    def _schedule_resettle(self, record: WorkspaceRecord, delay: float) -> None:
        """Re-evaluate the quiescence window; nothing else would wake it in time."""

        async def later() -> None:
            await asyncio.sleep(max(0.05, delay))
            self._settle_group(record)
            self.store.save(record)

        try:
            task = asyncio.get_running_loop().create_task(later())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except RuntimeError:
            pass

    def _update_links_index(self, record: WorkspaceRecord, worker_id: str, urls: list[str], artifact: Path) -> None:
        path = self.store.directory(record.workspace_id) / "links.json"
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"links": [], "artifacts": []}
        except Exception:
            current = {"links": [], "artifacts": []}
        known = {str(item.get("url") or "") for item in current.get("links") or []}
        for url in urls:
            if url not in known:
                current.setdefault("links", []).append({"url": url, "worker_id": worker_id, "observed_at": time.time()})
                known.add(url)
        current.setdefault("artifacts", []).append({"worker_id": worker_id, "path": str(artifact), "created_at": time.time()})
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    async def close_live_tab(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        view_id: str,
        lease_epoch: int = 0,
        runtime_generation: int = 0,
        controller_id: str = "",
        connection_generation: int = 0,
    ) -> dict:
        """Close a controlled page after revoking its native input grant."""
        record = self.record(owner, session_id)
        async with self._liveview_transition(owner, record.workspace_id, tab_id):
            _record, tab, view = self._assert_live_user_control(
                owner,
                session_id,
                tab_id,
                view_id=view_id,
                lease_epoch=lease_epoch,
                runtime_generation=runtime_generation,
                controller_id=controller_id,
                connection_generation=connection_generation,
            )
            assert view is not None
            await self.fence_live_control(view, tab, enabled=False)
            # Closing any member ends the one human takeover first. Otherwise
            # sibling popups retain orphan user leases after this capability is
            # invalidated by the close.
            self.release_control(
                owner,
                session_id,
                tab_id,
                note="Controlled browser tab closed; control returned to the agent.",
                preserve_hold=True,
                _fence_daemon=False,
            )
            try:
                result = await self._close_tab_locked(record, tab_id)
            except BaseException:
                # The page may have closed before persistence failed. Keep no
                # durable user lease whose controlling capability was revoked.
                record.tabs.setdefault(tab_id, tab)
                if tab.lease and tab.lease.actor == "user":
                    try:
                        self.release_control(
                            owner,
                            session_id,
                            tab_id,
                            note="Browser close did not complete cleanly; control returned to the agent.",
                            preserve_hold=True,
                            _fence_daemon=False,
                            _page_lost=(record.workspace_id, tab_id) not in self._pages,
                        )
                    except Exception:
                        log.exception("Could not persist controlled-close recovery")
                raise
            if self._liveview_viewport_owner_by_runtime.get(view.runtime_id) == view.view_id:
                self._liveview_viewport_owner_by_runtime.pop(view.runtime_id, None)
            pending = self._liveview_disconnect_tasks.pop(view.view_id, None)
            if pending is not None:
                pending.cancel()
            return result

    async def close_tab(
        self, owner: str, session_id: str, tab_id: str, *,
        actor: str | None = None, actor_id: str | None = None,
    ) -> dict:
        record = self.record(owner, session_id)
        if tab_id not in record.tabs:
            raise WorkspaceError("Unknown tab")
        if actor is not None:
            self.assert_tab_access(
                owner, session_id, tab_id,
                actor=actor, actor_id=actor_id or actor,
            )
        key = (record.workspace_id, tab_id)
        # Closing invalidates the page and its capability. Wait for any
        # in-flight parallel action on this tab before making that transition.
        effect_barrier = self._runtime_effect_barrier(owner)
        async with effect_barrier.shared(), self._action_lock(record.workspace_id, tab_id):
            if actor is not None:
                self.assert_tab_access(
                    owner, session_id, tab_id,
                    actor=actor, actor_id=actor_id or actor,
                )
            return await self._close_tab_locked(record, tab_id)

    async def close_tab_for_user(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        view_id: str = "",
        lease_epoch: int = 0,
        runtime_generation: int = 0,
        controller_id: str = "",
        connection_generation: int = 0,
    ) -> dict:
        """Close a tab on the authenticated workspace owner's authority.

        Closing a tab is a lifecycle action, not a controller-level input
        effect: the owner may close any tab in their workspace, including one
        the agent is driving and one a different viewer of the same account
        controls. When the tab sits under an active user lease, the close ends
        that takeover gracefully — the daemon grant is revoked at the effect
        boundary and the whole takeover group returns to its saved owner —
        instead of being rejected for a missing or stale input envelope. The
        caller's envelope is still honored when it does name the current
        controller, because that path can fence and clean up by identity.
        """
        record = self.record(owner, session_id)
        if tab_id not in record.tabs:
            raise TabLifecycleError("Unknown tab", error_kind="unknown_tab")
        tab = record.tabs[tab_id]
        lease = getattr(tab, "lease", None)
        active_user_lease = bool(
            lease
            and lease.actor == "user"
            and lease.actor_id == owner
            and (lease.expires_at is None or lease.expires_at > time.time())
        )
        if not active_user_lease:
            return await self.close_tab(owner, session_id, tab_id)
        if view_id:
            try:
                _record, _tab, caller_view = self._assert_live_user_control(
                    owner,
                    session_id,
                    tab_id,
                    view_id=view_id,
                    lease_epoch=lease_epoch,
                    runtime_generation=runtime_generation,
                    controller_id=controller_id,
                    connection_generation=connection_generation,
                )
            except (WorkspaceError, LiveViewUnavailable):
                # An unknown, stale, or expired capability is not an obstacle
                # for an owner close; the registry-driven unwind below still
                # revokes whichever grant is currently registered.
                caller_view = None
            if caller_view is not None:
                return await self.close_live_tab(
                    owner,
                    session_id,
                    tab_id,
                    view_id=view_id,
                    lease_epoch=lease_epoch,
                    runtime_generation=runtime_generation,
                    controller_id=controller_id,
                    connection_generation=connection_generation,
                )
        # Owner authority without a matching controller envelope: unwind the
        # takeover through the registry so the daemon grant is still revoked
        # before the page disappears.
        async with self._liveview_transition(owner, record.workspace_id, tab_id):
            runtime = self.backend.runtime(owner)
            runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
            registered_controller = self._liveview_controller_by_runtime.get(
                runtime_id, "",
            )
            controller_view = (
                self.live_views.find(registered_controller)
                if registered_controller
                else None
            )
            if (
                controller_view is not None
                and controller_view.workspace_id == record.workspace_id
                and controller_view.tab_id == tab_id
                and self._view_has_control(controller_view, tab)
            ):
                try:
                    await self.fence_live_control(
                        controller_view, tab, enabled=False,
                    )
                except Exception:
                    log.exception(
                        "Could not revoke the live grant before an owner close"
                    )
            try:
                self.release_control(
                    owner,
                    session_id,
                    tab_id,
                    note="Browser tab closed; control returned to the agent.",
                    preserve_hold=True,
                    _fence_daemon=False,
                )
            except WorkspaceError:
                # The lease may have expired between the check and the
                # transition. The close itself remains authorized, so continue.
                log.debug("Takeover lease expired during owner close of %s", tab_id)
            try:
                return await self._close_tab_locked(record, tab_id)
            except BaseException:
                # Mirror the controlled-close recovery: the page may have
                # closed before persistence failed, and no orphan user lease
                # may survive whose grant was just revoked.
                record.tabs.setdefault(tab_id, tab)
                if tab.lease and tab.lease.actor == "user":
                    try:
                        self.release_control(
                            owner,
                            session_id,
                            tab_id,
                            note="Browser close did not complete cleanly; control returned to the agent.",
                            preserve_hold=True,
                            _fence_daemon=False,
                            _page_lost=(record.workspace_id, tab_id) not in self._pages,
                        )
                    except Exception:
                        log.exception("Could not persist owner-close recovery")
                raise

    async def _close_tab_locked(
        self, record: WorkspaceRecord, tab_id: str,
    ) -> dict:
        """Close a tab while its action lock is already held."""
        key = (record.workspace_id, tab_id)
        page = self._pages.get(key)
        if page is not None:
            self._intentional_page_closes.add(key)
            try:
                await _bounded(page.close(), _PAGE_CLOSE_TIMEOUT_SECONDS)
            except (TimeoutError, asyncio.TimeoutError):
                # The close request went out and Firefox never answered. Every
                # later call on this owner would queue behind a page nobody
                # can use; drop it as closed.
                log.warning(
                    "Browser tab %s did not confirm close within %.0fs; forgetting it",
                    tab_id, _PAGE_CLOSE_TIMEOUT_SECONDS,
                )
                self._liveview_wheel_installed_pages.discard(id(page))
                self._live_form_dirty_installed_pages.discard(id(page))
                self._discard_diagnostics(key, page)
            except Exception:
                self._intentional_page_closes.discard(key)
                raise
            finally:
                # Playwright normally fires the close event synchronously, but
                # this also prevents a stale marker if an adapter does not.
                self._intentional_page_closes.discard(key)
        record.tabs.pop(tab_id, None)
        self._pages.pop(key, None)
        self._tab_revive_locks.pop(key, None)
        self._invalidate_observation(record.workspace_id, tab_id)
        self.__dict__.get("_observation_cache_miss_reasons", {}).pop(key, None)
        self.live_views.invalidate_tab(record.workspace_id, tab_id)
        runtime = self.backend.runtime(record.owner)
        runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
        if runtime_id:
            viewport_owner = self._liveview_viewport_owner_by_runtime.get(
                runtime_id, "",
            )
            viewport_view = self.live_views.find(viewport_owner) if viewport_owner else None
            if (
                viewport_view is None
                or (
                    viewport_view.workspace_id == record.workspace_id
                    and viewport_view.tab_id == tab_id
                )
            ):
                self._liveview_viewport_owner_by_runtime.pop(runtime_id, None)
        if self._liveview_selected.get(owner_key(record.owner)) == (
            record.workspace_id, tab_id,
        ):
            self._liveview_selected.pop(owner_key(record.owner), None)
        self.store.save(record)
        self._emit_browser_event(record, "closed", tab_id)
        return {"closed": True, "tab_id": tab_id}

    async def reconcile_worker_tabs(
        self, owner: str, session_id: str, worker_id: str, *, terminal_state: str,
    ) -> dict:
        """Release every lease owned by a terminal browser child.

        Deliverables become parent tabs after successful/partial work. Handoffs
        retain their worker lease so followup_task can resume them. Everything
        else is closed, preventing dead workers from permanently consuming the
        owner's resident-tab budget.
        """
        record = self.record(owner, session_id)
        owned = [
            tab for tab in list(record.tabs.values())
            if self._actor_owns_tab(tab, "worker", worker_id)
        ]
        if terminal_state == "handed_off":
            return {"preserved": len(owned), "transferred": 0, "closed": 0}
        transferred = 0
        closed = 0
        for tab in owned:
            if tab.disposition == "deliverable" and terminal_state in {"completed", "partial"}:
                tab.owner_kind = "parent"
                tab.owner_id = "parent"
                tab.lease = TabLease(actor="parent", actor_id="parent")
                transferred += 1
                self._emit_browser_event(record, "lease", tab.tab_id)
                continue
            await self.close_tab(
                owner, session_id, tab.tab_id,
                actor="worker", actor_id=worker_id,
            )
            closed += 1
        self.store.save(record)
        return {"preserved": 0, "transferred": transferred, "closed": closed}

    def record_hold(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        action: dict | None = None,
        refusal: dict | None = None,
    ) -> None:
        """Remember why an action was stopped, so the takeover view can say so.

        The gate's refusal already names the site, the target and the class; it
        just went to the model and nowhere else, which left a user staring at a
        picture of a page with no idea what they had been pulled in for.
        Best-effort by construction — failing to annotate must never turn a
        clean refusal into a tool error.
        """
        try:
            refusal = refusal or {}
            action = action or {}
            record = self.record(owner, session_id)
            tab = record.tabs.get(tab_id)
            if not tab:
                return
            tab.hold = TabHold(
                action_class=str(refusal.get("action_class") or ""),
                target=str(
                    action.get("_target_text") or action.get("label")
                    or action.get("ref") or ""
                )[:200],
                domain=str(refusal.get("domain") or ""),
                kind=str(action.get("kind") or action.get("action") or action.get("type") or ""),
                # The first line of the refusal is the human-readable "why";
                # the rest is routing instructions aimed at the model.
                reason=str(refusal.get("error") or "").strip().split("\n")[0][:300],
            )
            self.store.save(record)
            self._emit_browser_event(record, "hold", tab_id)
        except Exception:
            log.debug("could not record browser hold", exc_info=True)

    def clear_hold(self, owner: str, session_id: str, tab_id: str) -> None:
        record = self.record(owner, session_id)
        tab = record.tabs.get(tab_id)
        if tab and tab.hold is not None:
            tab.hold = None
            self.store.save(record)
            self._emit_browser_event(record, "hold", tab_id)

    def take_note(self, record: WorkspaceRecord, tab: TabRecord) -> str:
        """Pop what the user said they did while holding this tab.

        Once only: it is a message about a moment, and repeating it on every
        later observe would read as a fresh event each time.
        """
        note = str(getattr(tab, "user_note", "") or "")
        if note:
            tab.user_note = ""
            self.store.save(record)
        return note

    async def _relinquish_same_owner_control(
        self, owner: str, *, note: str, keep: tuple[str, str] | None = None,
        moving_view_id: str = "",
    ) -> list[str]:
        """Freeform presence: this person's other user leases return to the agent.

        Selecting or acting on one tab supersedes presence elsewhere, so the
        leases the person is leaving behind return through the canonical
        durable release (holds preserved; the superseded native grant is
        fenced here because the caller's transition already owns media
        authority). Only the same person's leases are touched — never another
        actor's — and no second authority owner is created: release_control
        remains the single handback implementation.
        """
        released: list[str] = []
        runtime = self.backend.runtime(owner)
        runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "") if runtime else ""
        controller_id = (
            self._liveview_controller_by_runtime.get(runtime_id, "") if runtime_id else ""
        )
        # Same person does not mean the same device: only the lease whose
        # native grant belongs to the moving view (or one left uncontrolled
        # by an idle bump) may be displaced. A different connected controller
        # of the same account keeps its seat.
        for owner_record in self._owner_records(owner):
            for candidate in list(owner_record.tabs.values()):
                lease = candidate.lease
                if lease is None or lease.actor != "user" or lease.actor_id != owner:
                    continue
                if keep and (owner_record.workspace_id, candidate.tab_id) == keep:
                    continue
                if moving_view_id and not self._lease_controls_owned_view(
                    owner_record, candidate, runtime_id, moving_view_id,
                ):
                    continue
                try:
                    self.release_control(
                        owner,
                        owner_record.session_id,
                        candidate.tab_id,
                        note=note,
                        preserve_hold=True,
                        _fence_daemon=False,
                    )
                    released.append(candidate.tab_id)
                except WorkspaceError:
                    continue  # a raced release; the caller revalidates again
        if released and controller_id:
            # The runtime's single grant was pointing at a released tab; fence
            # it so its input is dead before the caller's grant is placed.
            controller_view = self.live_views.find(controller_id)
            controller_tab = self.record(controller_view.owner, controller_view.session_id) \
                .tabs.get(controller_view.tab_id) if controller_view else None
            try:
                if controller_view is not None and controller_tab is not None:
                    await self.fence_live_control(controller_view, controller_tab, enabled=False)
            except Exception:
                # The takeover's own grant replacement re-fences this device;
                # a failed revoke here must not strand the durable release.
                log.warning(
                    "Could not fence the superseded controller for %s",
                    controller_view.view_id if controller_view else "",
                    exc_info=True,
                )
        return released

    def _lease_controls_owned_view(
        self, owner_record: WorkspaceRecord, tab: TabRecord, runtime_id: str,
        moving_view_id: str,
    ) -> bool:
        """Whether this tab's native grant belongs to the moving person's view.

        Another connected controller of the same account is a different seat
        and must keep its lease and grant; only the moving view's own grant
        and grants left idle by an auto-handback are superseded by presence.
        """
        controller = self._liveview_controller_by_runtime.get(runtime_id or "", "")
        if not controller:
            # No live grant: this lease is presence without native input
            # (an idle handback). It follows the person.
            return True
        if secrets.compare_digest(controller, moving_view_id):
            return True
        prior = next(
            (
                candidate for candidate in self.live_views.for_runtime(runtime_id)
                if secrets.compare_digest(candidate.view_id, controller)
            ),
            None,
        )
        return (
            prior is None
            or prior.workspace_id != owner_record.workspace_id
            or prior.tab_id != tab.tab_id
        )

    async def take_live_control(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        view_id: str,
        lease_epoch: int = 0,
        runtime_generation: int = 0,
        controller_id: str = "",
        connection_generation: int = 0,
    ) -> dict:
        """Persist authority, fence its native grant, then publish the transition."""
        record = self.record(owner, session_id)
        async with self._liveview_transition(
            owner, record.workspace_id, tab_id, label="take_live_control", timeout=30.0,
        ):
            view, view_record, tab = self.resolve_live_view(view_id, owner)
            if view_record.workspace_id != record.workspace_id or tab.tab_id != tab_id:
                raise WorkspaceError("Live-view capability does not target this tab")
            runtime = self.backend.runtime(owner)
            runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
            controller = self._liveview_controller_by_runtime.get(runtime_id)
            control_fencing = self._live_capabilities(runtime).get("control_fencing")
            if control_fencing:
                if int(runtime_generation or 0) <= 0 or int(connection_generation or 0) <= 0:
                    raise WorkspaceError("Live-view takeover authority is incomplete")
                if int(runtime_generation) != int(view.runtime_generation):
                    raise WorkspaceError("Live-view runtime generation is stale")
                if int(connection_generation) != int(view.connection_generation):
                    raise WorkspaceError("Live-view connection generation is stale")
                current_epoch = int(getattr(tab.lease, "generation", 0) or 0)
                if int(lease_epoch or 0) > current_epoch:
                    # The client's epoch is advisory. Only an impossible claim
                    # (newer than anything the manager ever minted) is forged.
                    # Any older or zero epoch — a first gesture, a viewer that
                    # missed an idle handback bump, a fresh reload — proceeds:
                    # identity and freshness are proven by the runtime,
                    # connection, and controller fencing above, and the mint
                    # below rotates the durable epoch so every older native
                    # packet is fenced at the daemon boundary regardless.
                    raise WorkspaceError("User-control lease epoch is stale")
                if int(lease_epoch or 0) != current_epoch:
                    log.debug(
                        "take_live_control accepted a lagging client epoch "
                        "(sent %s, current %s, view %s)",
                        int(lease_epoch or 0), current_epoch, view.view_id,
                    )
                # A spectator is not told another device's controller token;
                # its view id and WebSocket generation identify the claimant.
                # A retry from an existing controller may echo its own id.
                if controller_id and not secrets.compare_digest(
                    str(controller_id), view.view_id,
                ):
                    raise WorkspaceError("Live-view controller is stale")
            # A normal controller remains exclusive. An already-expired one is
            # different: it cannot keep a new authenticated takeover blocked
            # merely because its half-open connection never delivered close.
            await self._reconcile_expired_live_controller_locked(
                runtime_id,
                owner=owner,
                target_record=record,
                target_tab=tab,
                note=(
                    "Expired live browser control was returned to the agent "
                    "before another tab took control."
                ),
            )
            controller = self._liveview_controller_by_runtime.get(runtime_id)
            if not control_fencing and controller and not secrets.compare_digest(controller, view_id):
                raise WorkspaceError(
                    "This live-view daemon cannot safely transfer control between viewers"
                )
            # Freeform movement: a same-owner takeover of another tab supersedes
            # this person's presence elsewhere instead of demanding a manual
            # handback. Canonical release keeps held input fenced and durable.
            await self._relinquish_same_owner_control(
                owner,
                note="Switched tabs; control returned to the agent.",
                keep=(record.workspace_id, tab_id),
                moving_view_id=view_id,
            )
            for owner_record in self._owner_records(owner):
                for candidate in owner_record.tabs.values():
                    candidate_lease = candidate.lease
                    same_tab = (
                        owner_record.workspace_id == record.workspace_id
                        and candidate.tab_id == tab_id
                    )
                    same_group = (
                        owner_record.workspace_id == record.workspace_id
                        and self._same_takeover_group(tab, candidate)
                    )
                    if (
                        not same_tab and not same_group
                        and candidate_lease is not None
                        and candidate_lease.actor == "user"
                        and candidate_lease.actor_id == owner
                    ):
                        if (
                            candidate_lease.expires_at is not None
                            and candidate_lease.expires_at <= time.time()
                            and await self._reconcile_expired_uncontrolled_lease_locked(
                                view,
                                owner_record,
                                candidate,
                                target_record=record,
                                target_tab=tab,
                                note=(
                                    "Expired browser control with no current "
                                    "controller was returned to the agent."
                                ),
                            )
                        ):
                            continue
                        # Relinquish applies to the runtime's current grant,
                        # covering the same-owner lease that was being refused.
                        await self._relinquish_same_owner_control(
                            owner,
                            note="Switched tabs; control returned to the agent.",
                            keep=(record.workspace_id, tab_id),
                            moving_view_id=view_id,
                        )
                        controller = self._liveview_controller_by_runtime.get(runtime_id)
                        break

            # Recovery above is itself a durable authority transition. Capture
            # rollback state only after it, so a failed new takeover never
            # resurrects the expired controller we intentionally fenced.
            transition = self._capture_live_transition(
                record, tab, runtime_id=runtime_id,
            )
            page = self._pages.get((record.workspace_id, tab_id))
            if page is None:
                raise LiveViewUnavailable("The selected browser tab is no longer active")
            lease = tab.lease
            if lease and lease.actor == "user" and lease.actor_id == owner:
                next_epoch = int(lease.generation or 0)
                if (
                    (controller and not secrets.compare_digest(controller, view_id))
                    or (lease.expires_at is not None and lease.expires_at <= time.time())
                ):
                    next_epoch += 1
            else:
                next_epoch = _next_lease_epoch(tab)
            grant_connection_generation = int(
                connection_generation or view.connection_generation
            )
            # A view can have been minted before another device selected a
            # tab. Activate this target immediately before its grant becomes
            # valid so X11 input and the grant's tab id cannot diverge.
            try:
                await self.backend.prepare_user_control(owner, page)
                self._adopt_liveview_geometry_revision(runtime_id)
                self._liveview_selected[owner_key(owner)] = (
                    record.workspace_id, tab_id,
                )
                if connection_generation and int(connection_generation) != view.connection_generation:
                    raise WorkspaceError("Live-view connection generation is stale")

                # Commit the durable manager authority before exposing the
                # native grant. The daemon broadcasts its complete grant tuple,
                # so enabling it first would let a crafted RTC peer act in the
                # gap before take_control made that authority durable. Suppress
                # the lease event until the native effect boundary confirms;
                # the captured transition restores this pre-image on failure.
                result = self.take_control(
                    owner,
                    session_id,
                    tab_id,
                    view_id=view_id,
                    _emit_event=False,
                )
                if int(result.get("lease_epoch") or 0) != next_epoch:
                    raise WorkspaceError("Browser control changed during takeover")
                fenced = await self.fence_live_control(
                    view, tab, enabled=True, lease_epoch=next_epoch,
                    connection_generation=grant_connection_generation,
                )
                if (
                    connection_generation
                    and int(connection_generation) != int(view.connection_generation)
                ):
                    # A replacement socket arrived while the native grant was
                    # awaiting its effect boundary. The rollback below revokes
                    # this captured generation before it can become durable;
                    # the replacement will acquire this transition next.
                    raise WorkspaceError("Live-view connection generation is stale")
                # Join the daemon's effect-boundary revision with the durable
                # lease before replying. A lost-response retry may fence a newer
                # revision, so the receipt must be refreshed on every attempt.
                result.update(
                    self._frame_meta(
                        tab,
                        view=view,
                        runtime=self.backend.runtime(owner),
                    )
                )
                # The optional timestamp proves that a source picture was made
                # after this exact grant. It is never cached or reused across
                # an authority, connection, tab, or geometry change.
                if isinstance(fenced, dict):
                    boundary = fenced.get("control_frame_rtp_timestamp")
                    grant = fenced.get("control") or {}
                    if (
                        type(boundary) is int and 0 <= boundary < 2**32
                        and int(fenced.get("runtime_generation") or 0)
                        == int(view.runtime_generation)
                        and grant.get("enabled") is True
                        and grant.get("controller_id") == view.view_id
                        and grant.get("tab_id") == tab.tab_id
                        and int(grant.get("lease_epoch") or 0) == next_epoch
                        and int(grant.get("connection_generation") or 0)
                        == int(view.connection_generation)
                    ):
                        result["control_frame_rtp_timestamp"] = boundary
                        result["geometry_revision"] = int(
                            fenced.get("geometry_revision") or 0
                        )
            except BaseException:
                # A manifest failure after the daemon grant is an ambiguous
                # partial commit. Revoke native effects, then restore both the
                # in-memory owner graph and its durable pre-image.
                try:
                    await asyncio.shield(
                        self.fence_live_control(
                            view,
                            tab,
                            enabled=False,
                            lease_epoch=next_epoch,
                            connection_generation=grant_connection_generation,
                        )
                    )
                except Exception:
                    log.exception("Could not revoke a failed live takeover grant")
                try:
                    self._restore_live_transition(transition)
                except Exception:
                    log.exception("Could not persist live takeover rollback")
                prior_selection = transition.get("selection")
                prior_page = self._pages.get(prior_selection) if prior_selection else None
                if prior_page is not None and prior_page is not page:
                    try:
                        await self.backend.prepare_user_control(owner, prior_page)
                        self._adopt_liveview_geometry_revision(runtime_id)
                    except Exception:
                        log.debug("Could not restore browser focus after failed takeover", exc_info=True)
                raise
            self._emit_browser_event(record, "lease", tab_id)
            self._emit_browser_event(record, "selection", tab_id)
            self.note_live_view_client_activity(
                view,
                connection_generation=int(view.connection_generation),
            )
            return result

    def take_control(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        view_id: str = "",
        _emit_event: bool = True,
    ) -> dict:
        record = self.record(owner, session_id)
        tab = record.tabs.get(tab_id)
        if not tab:
            raise WorkspaceError("Unknown tab")
        view: LiveViewSession | None = None
        runtime = self.backend.runtime(owner)
        if view_id:
            view, view_record, view_tab = self.resolve_live_view(view_id, owner)
            if view_record.workspace_id != record.workspace_id or view_tab.tab_id != tab_id:
                raise WorkspaceError("Live-view capability does not target this tab")
        runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "") if runtime else ""
        controller = self._liveview_controller_by_runtime.get(runtime_id) if runtime_id else ""
        if (
            view_id and controller
            and not secrets.compare_digest(controller, view_id)
            and not self._live_capabilities(runtime).get("control_fencing")
        ):
            raise WorkspaceError(
                "This live-view daemon cannot safely transfer control between viewers"
            )
        if tab.lease and tab.lease.actor == "user" and tab.lease.actor_id == owner:
            expired = bool(tab.lease.expires_at is not None and tab.lease.expires_at <= time.time())
            if not expired and (not view_id or not controller or secrets.compare_digest(controller, view_id)):
                # A response can be lost after the server commits. Retrying the
                # same takeover must not rotate its epoch or erase the worker
                # return address.
                changed = False
                if tab.lease.expires_at is None:
                    tab.lease.expires_at = self._user_control_deadline()
                    changed = True
                if not tab.takeover_group_id:
                    tab.takeover_group_id = "control_" + uuid.uuid4().hex
                    changed = True
                if view_id and runtime_id:
                    self._liveview_controller_by_runtime[runtime_id] = view_id
                    self._liveview_viewport_owner_by_runtime[runtime_id] = view_id
                if changed:
                    self.store.save(record)
                return {
                    "lease": tab.lease.__dict__, "lease_epoch": tab.lease.generation,
                    "hold": asdict(tab.hold) if tab.hold else None,
                    "already_controlled": True,
                    "control": True,
                    "viewport_control": True,
                    "viewport_owner_present": True,
                    "controller_id": view_id,
                    "connection_generation": int(getattr(view, "connection_generation", 0) or 0),
                    "runtime_generation": int(getattr(view, "runtime_generation", 0) or 0),
                    "capabilities": self._live_capabilities(runtime),
                }
            # The same user explicitly took control from another connected
            # device. Preserve the original agent return address but rotate the
            # durable epoch so old device messages are fenced immediately.
            next_generation = _next_lease_epoch(tab)
            deadline = self._user_control_deadline()
            for member in self._takeover_group_tabs(record, tab):
                member.lease = TabLease(
                    actor="user", actor_id=owner, generation=next_generation,
                    expires_at=deadline,
                )
                member.owner_kind = "user"
        else:
            resume_actor = "worker" if tab.owner_kind == "worker" else "parent"
            resume_actor_id = tab.owner_id if resume_actor == "worker" else "parent"
            # Taking the lease is the whole mechanism: a sub-agent whose tab has
            # been taken finds its next action rejected and hands off.
            next_generation = _next_lease_epoch(tab)
            tab.lease = TabLease(
                actor="user", actor_id=owner, generation=next_generation,
                expires_at=self._user_control_deadline(),
            )
            tab.owner_kind = "user"
            tab.takeover_resume_actor = resume_actor
            tab.takeover_resume_actor_id = resume_actor_id
            tab.takeover_group_id = "control_" + uuid.uuid4().hex
        if view_id and runtime_id:
            self._liveview_controller_by_runtime[runtime_id] = view_id
            self._liveview_viewport_owner_by_runtime[runtime_id] = view_id
            for candidate in self.live_views.for_runtime(runtime_id):
                if candidate.runtime_id == runtime_id and candidate.view_id != view_id:
                    candidate.pressed_keys.clear()
        self._touch_tab_activity(record, tab, human=True)
        self.store.save(record)
        if _emit_event:
            self._emit_browser_event(record, "lease", tab_id)
        return {
            "lease": tab.lease.__dict__, "lease_epoch": tab.lease.generation,
            "hold": asdict(tab.hold) if tab.hold else None,
            "control": True,
            "viewport_control": True,
            "viewport_owner_present": True,
            "controller_id": view_id,
            "connection_generation": int(getattr(view, "connection_generation", 0) or 0),
            "runtime_generation": int(getattr(view, "runtime_generation", 0) or 0),
            "capabilities": self._live_capabilities(runtime),
        }

    async def fence_live_control(
        self,
        view: LiveViewSession,
        tab: TabRecord,
        *,
        enabled: bool,
        lease_epoch: int | None = None,
        connection_generation: int | None = None,
    ) -> dict[str, Any] | None:
        """Commit the current controller grant at the daemon's effect boundary."""
        runtime = self.backend.runtime(view.owner)
        capabilities = self._live_capabilities(runtime)
        if not capabilities.get("control_fencing"):
            return
        live_runtime = self.liveview.runtime(view.runtime_id)
        revision = max(
            self._liveview_control_revision.get(view.runtime_id, 0),
            int(getattr(live_runtime, "control_revision", 0) or 0),
        ) + 1
        # Allocate before awaiting transport; delayed requests retain their old
        # revision and cannot overwrite a later revoke at the native boundary.
        self._liveview_control_revision[view.runtime_id] = revision
        lease = tab.lease
        return await self.liveview.update_runtime(
            view.runtime_id,
            control={
                "enabled": bool(enabled),
                "controller_id": view.view_id if enabled else "",
                "connection_generation": int(
                    view.connection_generation if connection_generation is None
                    else connection_generation
                ),
                "control_revision": revision,
                "lease_epoch": int(
                    getattr(lease, "generation", 0) if lease_epoch is None else lease_epoch
                ),
                "tab_id": view.tab_id if enabled else "",
            },
        )

    async def reconnect_live_control(
        self, view: LiveViewSession, *, connection_generation: int,
    ) -> dict[str, Any]:
        """Bind a reconnect through the same authority transaction as handback."""
        async with self._liveview_transition(
            view.owner, view.workspace_id, view.tab_id, label="reconnect_live_control",
        ):
            current, record, tab = self.resolve_live_view(view.view_id, view.owner)
            if int(connection_generation) != int(current.connection_generation):
                raise WorkspaceError("Live-view connection generation is stale")
            lease = tab.lease
            controller = self._liveview_controller_by_runtime.get(view.runtime_id, "")
            if (
                controller == view.view_id and lease is not None
                and lease.actor == "user" and lease.actor_id == view.owner
                and lease.expires_at is not None and lease.expires_at <= time.time()
            ):
                await self.fence_live_control(
                    view, tab, enabled=False, connection_generation=connection_generation,
                )
                self.release_control(
                    view.owner, record.session_id, tab.tab_id,
                    note="User-control lease expired before reconnect.",
                    preserve_hold=True, _fence_daemon=False,
                )
            elif self._view_has_control(view, tab):
                await self.fence_live_control(
                    view, tab, enabled=True, connection_generation=connection_generation,
                )
                if int(connection_generation) != int(current.connection_generation):
                    # A newer socket arrived while the native update awaited.
                    # Revoke this obsolete grant; its replacement will acquire
                    # this same transaction and establish a higher revision.
                    await self.fence_live_control(
                        view, tab, enabled=False, connection_generation=connection_generation,
                    )
                    raise WorkspaceError("Live-view connection generation is stale")
            self.note_live_view_client_activity(
                view, connection_generation=connection_generation,
            )
            return self._frame_meta(tab, view=view, runtime=self.backend.runtime(view.owner))

    async def release_live_control(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        view_id: str,
        lease_epoch: int = 0,
        runtime_generation: int = 0,
        controller_id: str = "",
        connection_generation: int = 0,
        note: str = "",
        approve: bool = False,
        preserve_hold: bool = False,
    ) -> dict:
        """Revoke native effects before returning the durable tab lease."""
        record = self.record(owner, session_id)
        async with self._liveview_transition(owner, record.workspace_id, tab_id):
            retry_view, retry_record, retry_tab = self.resolve_live_view(view_id, owner)
            receipt = retry_view.last_handback
            request_identity = {
                "session_id": session_id, "tab_id": tab_id,
                "lease_epoch": int(lease_epoch),
                "runtime_generation": int(runtime_generation),
                "controller_id": str(controller_id),
                "connection_generation": int(connection_generation),
            }
            if (
                receipt is not None and receipt["request"] == request_identity
                and retry_record.workspace_id == record.workspace_id
                and retry_tab.tab_id == tab_id
                and int(retry_view.connection_generation) == int(connection_generation)
                and int(getattr(retry_tab.lease, "generation", 0) or 0) == receipt["result"]["lease_epoch"]
                and not self._liveview_controller_by_runtime.get(retry_view.runtime_id)
            ):
                return {**receipt["result"], "already_released": True}
            _record, tab, view = self._assert_live_user_control(
                owner,
                session_id,
                tab_id,
                view_id=view_id,
                lease_epoch=lease_epoch,
                runtime_generation=runtime_generation,
                controller_id=controller_id,
                connection_generation=connection_generation,
            )
            assert view is not None
            runtime_id = str(view.runtime_id or "")
            transition = self._capture_live_transition(
                record, tab, runtime_id=runtime_id,
            )
            await self.fence_live_control(view, tab, enabled=False)
            try:
                result = self.release_control(
                    owner,
                    session_id,
                    tab_id,
                    note=note,
                    approve=approve,
                    preserve_hold=preserve_hold,
                    _fence_daemon=False,
                )
                view.last_handback = {"request": request_identity, "result": dict(result)}
                return result
            except BaseException:
                # Native effects are already disabled. Restore the durable
                # user lease and its worker/parent return address so a
                # failed fsync cannot publish a half-handback.
                try:
                    self._restore_live_transition(transition)
                except Exception:
                    log.exception("Could not persist live handback rollback")
                raise

    def release_control(
        self,
        owner: str,
        session_id: str,
        tab_id: str,
        *,
        note: str = "",
        approve: bool = False,
        preserve_hold: bool = False,
        _fence_daemon: bool = True,
        _page_lost: bool = False,
    ) -> dict:
        record = self.record(owner, session_id)
        tab = record.tabs.get(tab_id)
        if not tab:
            raise WorkspaceError("Unknown tab")
        if tab.lease is not None and (
            tab.lease.actor != "user" or tab.lease.actor_id != owner
        ):
            # Idempotent retry after a lost success response. In particular, do
            # not turn a resumed worker lease into a parent lease on retry.
            return {
                "released": True,
                "already_released": True,
                "resumed_worker_id": tab.owner_id if tab.owner_kind == "worker" else "",
                "owner_kind": tab.owner_kind,
                "owner_id": tab.owner_id,
                "driver": getattr(tab.lease, "actor", "") or "",
                "note": str(getattr(tab, "user_note", "") or ""),
                "approved": False,
                "lease_epoch": int(getattr(tab.lease, "generation", 0) or 0),
            }
        group_tabs = self._takeover_group_tabs(record, tab)
        note_text = str(note or "").strip()[:500]
        # Returning a challenged/login group to the agent is the user's durable
        # signal that the prerequisite was handled (or consciously abandoned).
        resolved_at = time.time()
        worker_ids: list[str] = []
        released_tab_ids: list[str] = []
        for member in group_tabs:
            member.user_note = note_text
            for item in record.attentions.values():
                if (
                    not item.resolved_at
                    and str(getattr(item, "tab_id", "") or "") == member.tab_id
                ):
                    item.resolution = note_text or "user returned browser control"
                    item.resolved_at = resolved_at
                    item.revision += 1
                    self._unpark_worker(record, item.worker_id, resolved_at)
            if not preserve_hold:
                member.hold = None
            worker_id = self._return_tab_lease_to_agent(member)
            if worker_id:
                worker_ids.append(worker_id)
            released_tab_ids.append(member.tab_id)
            if _page_lost and member.tab_id == tab_id and (
                member.restorability == "restorable_get"
                and not member.dirty
                and not member.uncertain
                and (
                    member.url.startswith(("http://", "https://"))
                    or self._is_revivable_blank_tab(member)
                )
            ):
                # Keep the returned worker/parent lease while marking the
                # missing clean page for lazy reconstruction.
                member.owner_kind = "sleeping"
                member.owner_id = ""
        self._touch_tab_activity(record, tab, human=True)
        self.store.save(record)
        for released_tab_id in released_tab_ids:
            self._emit_browser_event(record, "lease", released_tab_id)
        runtime = self.backend.runtime(owner)
        runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "") if runtime else ""
        controller_id = self._liveview_controller_by_runtime.pop(runtime_id, "") if runtime_id else ""
        controller_view = next(
            (item for item in self.live_views.for_runtime(runtime_id) if item.view_id == controller_id),
            None,
        ) if runtime_id and controller_id else None
        if controller_id:
            self._cancel_liveview_liveness(controller_id)
        if runtime_id and controller_id:
            if (
                controller_view is not None
                and self.live_views.current_connected(controller_view)
            ):
                # Handback removes input authority, while the still-present
                # viewer keeps its independent window-sizing ownership.
                self._liveview_viewport_owner_by_runtime[runtime_id] = controller_id
            else:
                self._liveview_viewport_owner_by_runtime.pop(runtime_id, None)
        for member in group_tabs:
            page = self._pages.get((record.workspace_id, member.tab_id))
            if page is None:
                continue
            try:
                if (
                    member.tab_id == tab_id
                    and controller_view is not None
                    and _fence_daemon
                ):
                    task = asyncio.get_running_loop().create_task(
                        self.fence_live_control(controller_view, member, enabled=False)
                    )
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                task = asyncio.get_running_loop().create_task(self.backend.release_user_control(owner, page))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            except RuntimeError:
                pass
        return {
            "released": True,
            "resumed_worker_id": worker_ids[0] if worker_ids else "",
            "resumed_worker_ids": list(dict.fromkeys(worker_ids)),
            "released_tab_ids": released_tab_ids,
            "owner_kind": tab.owner_kind,
            "owner_id": tab.owner_id,
            "driver": getattr(tab.lease, "actor", "") or "",
            "note": tab.user_note,
            "approved": bool(approve),
            "hold": asdict(tab.hold) if tab.hold else None,
            "hold_preserved": bool(preserve_hold and tab.hold),
            "lease_epoch": tab.lease.generation,
        }

    async def sleep_for_archive(self, owner: str, session_id: str) -> dict:
        """Release this chat's safe resident pages during an archive request.

        Archiving must not discard unsaved form input or interrupt a worker,
        user takeover, hold, or in-flight effect. A clean GET can be restored
        from its durable tab record; a protected page stays resident and is
        counted so the archive receipt cannot imply complete memory cleanup.
        """
        record = self.record(owner, session_id)
        slept = 0
        retained = 0
        running_worker = any(
            worker.get("status") in {"queued", "running", "waiting"}
            for worker in record.workers.values()
        )
        try:
            from ascended_browser.runtime.runs import is_active as agent_run_active
            running_agent = agent_run_active(session_id)
        except Exception:
            # An unreadable run state cannot authorize closing a live page.
            log.exception("Could not determine agent-run state during session archive")
            running_agent = True
        # Open/close/effect operations share this barrier. Hold it across the
        # final owner shutdown so a new page cannot appear after our safety
        # check and then be closed by sleep().
        async with self._runtime_effect_barrier(owner).exclusive(
            priority=True, label="session_archive_sleep",
        ):
            for tab in list(record.tabs.values()):
                key = (record.workspace_id, tab.tab_id)
                if key not in self._pages:
                    continue
                async with self._action_lock(record.workspace_id, tab.tab_id):
                    if key not in self._pages:
                        continue
                    lease = tab.lease
                    protected = (
                        running_agent or running_worker or tab.dirty or tab.uncertain
                        or tab.restorability != "restorable_get"
                        or tab.hold is not None
                        or (lease is not None and lease.actor not in {"parent", "user"})
                        or (lease is not None and lease.actor == "user" and tab.takeover_group_id)
                        or self._operation_pins.get(key, 0) != 0
                        or key in self._intentional_page_closes
                    )
                    if protected:
                        retained += 1
                        continue
                    if await self._sleep_resident_tab(
                        record, tab, now=time.time(), event="tab_slept_on_session_archive",
                    ):
                        self.live_views.invalidate_tab(record.workspace_id, tab.tab_id)
                        selected_key = owner_key(owner)
                        if self._liveview_selected.get(selected_key) == key:
                            self._liveview_selected.pop(selected_key, None)
                        slept += 1
                    else:
                        retained += 1
            if retained:
                if slept:
                    self.store.save(record)
                return {"slept_tabs": slept, "retained_tabs": retained, "status": "partial"}
            # sleep() checkpoints the owner profile and shuts down Firefox
            # when this was its last active workspace.
            await self.sleep(owner, session_id)
        return {"slept_tabs": slept, "retained_tabs": 0, "status": "complete"}

    async def sleep(self, owner: str, session_id: str) -> dict:
        record = self.record(owner, session_id)
        others_active = any(
            item.workspace_id != record.workspace_id
            and item.status in {"active", "waking", "draining", "needs_recovery"}
            for item in self._owner_records(owner)
        )
        runtime = self.backend.runtime(owner)
        state = None
        if runtime is not None and not others_active:
            # storage_state(indexed_db=True) may create a helper page internally;
            # capture it while Firefox still has a live window, then close.
            state = await runtime.context.storage_state(indexed_db=True)
        for key in [key for key in self._pages if key[0] == record.workspace_id]:
            page = self._pages.get(key)
            tab = record.tabs.get(key[1])
            if tab is not None:
                if (
                    tab.restorability == "restorable_get"
                    and not tab.dirty and not tab.uncertain
                ):
                    tab.owner_kind = "sleeping"
                else:
                    # Closing this document discards state that cannot be
                    # reconstructed with a safe GET. Preserve the tab as a
                    # truthful recovery item instead of advertising it as an
                    # ordinary sleeping page.
                    tab.owner_kind = "needs_user"
                    tab.uncertain = True
                    tab.restorability = "non_restorable"
            self._intentional_page_closes.add(key)
            if page is not None and (runtime is None or others_active):
                try: await page.close()
                except Exception: self._intentional_page_closes.discard(key)
            self._pages.pop(key, None)
        record.status = "sleeping"; record.checkpoint_version += 1; self.store.save(record)
        if runtime is not None and not others_active:
            runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
            if runtime_id:
                self.live_views.invalidate_runtime(runtime_id)
                self._liveview_controller_by_runtime.pop(runtime_id, None)
                self._liveview_viewport_owner_by_runtime.pop(runtime_id, None)
            self._liveview_selected.pop(owner_key(owner), None)
            await self.backend.close_owner(owner)
            checkpoint = await self.auth_store.checkpoint(owner, state or {})
            record.auth_base_version = int(checkpoint.get("version") or 0)
            self.store.save(record)
        return record.public()

    def status(self, owner: str, session_id: str) -> dict:
        record = self.record(owner, session_id)
        live = sum(1 for key in self._pages if key[0] == record.workspace_id)
        public = record.public()
        public["tabs"] = [self._public_tab(record, tab) for tab in record.tabs.values()]
        return {
            **public,
            **self.backend.health(owner),
            # Supervision needs to show what is actually resident, not just what
            # the record remembers: evicted tabs stay in the record but hold no page.
            "live_tab_count": live,
            "live_viewer_count": self.live_views.connected_count(record.workspace_id),
            "owner_live_tab_count": sum(
                1 for key in self._pages
                if key[0] in {item.workspace_id for item in self._owner_records(owner)}
            ),
            "owner_tab_limit": self._owner_tab_limit,
        }

    def list_owner_workspaces(self, owner: str) -> dict:
        """Cross-chat projection for the global Browser watcher rail panel."""
        by_id: dict[str, WorkspaceRecord] = {}
        try:
            for record in self.store.load_owner(owner or ""):
                by_id[record.workspace_id] = record
        except Exception:
            pass
        for record in self._owner_records(owner or ""):
            by_id[record.workspace_id] = record

        workspaces = []
        attention_total = 0
        tab_total = 0
        for record in by_id.values():
            tabs = [self._public_tab(record, tab) for tab in record.tabs.values()]
            attention = [
                asdict(item) for item in record.attentions.values() if not item.resolved_at
            ]
            if not tabs and not attention and record.status in {"sleeping", "error"}:
                continue
            attention_total += len(attention)
            tab_total += len(tabs)
            live = sum(1 for key in self._pages if key[0] == record.workspace_id)
            workspaces.append({
                "workspace_id": record.workspace_id,
                "session_id": record.session_id,
                "status": record.status,
                "tabs": tabs,
                "tab_count": len(tabs),
                "live_tab_count": live,
                "attention": attention,
                "attention_count": len(attention),
                "resumable_count": record.resumable_count,
                "recovery_warning": record.recovery_warning,
                "updated_at": record.updated_at,
            })
        workspaces.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
        return {
            "workspaces": workspaces,
            "tab_count": tab_total,
            "attention_count": attention_total,
            "owner_tab_limit": self._owner_tab_limit,
        }

    @staticmethod
    def _digest_tab_row(tab: dict) -> dict:
        """One open tab as the agent needs it to find it again: id, page, state."""
        row = {
            "tab_id": tab.get("tab_id"),
            "title": str(tab.get("title") or "")[:100],
            "url": str(tab.get("url") or "")[:240],
            "owner_kind": tab.get("owner_kind"),
        }
        if tab.get("owner_kind") == "worker":
            row["owner_id"] = tab.get("owner_id")
        if tab.get("restorability") not in (None, "", "restorable_get"):
            row["restorability"] = tab.get("restorability")
        for flag in ("dirty", "uncertain"):
            if tab.get(flag):
                row[flag] = True
        if tab.get("user_note"):
            row["user_note"] = str(tab["user_note"])[:200]
        return row

    def digest(
        self,
        owner: str,
        session_id: str,
        *,
        model: str = "",
        max_tokens: int = 2000,
        max_chars: int | None = None,
    ) -> str:
        record = self.record(owner, session_id); public = record.public()
        active = [worker for worker in public["workers"] if worker.get("status") not in {"completed", "cancelled"}]
        completed = [worker for worker in public["workers"] if worker.get("status") == "completed"]
        completed.sort(key=lambda worker: float(worker.get("updated_at") or 0), reverse=True)
        core = {
            "status": public["status"],
            "attention": public["attention"],
            "resumable_count": public["resumable_count"],
            "recovery_warning": public["recovery_warning"],
            "active_workers": active,
            "tabs": [self._digest_tab_row(tab) for tab in public["tabs"]],
            "completed_workers": completed,
            "artifact_manifest": self.store.artifact_manifest(record.workspace_id),
        }
        # This is the agent's every-turn record of what is open, after older
        # tool results have aged out of its history. Whole tab records (~840
        # chars each) fit four tabs in the budget, so session 722b3c33 saw 4 of
        # 10 and reopened the rest. Rows say what a tab is; trimming drops the
        # least recently active first and says how many it dropped.
        activity = {
            str(tab.get("tab_id") or ""): float(tab.get("last_active_at") or tab.get("updated_at") or 0)
            for tab in public["tabs"]
        }
        tab_total = len(core["tabs"])

        def least_active(rows: list[dict]) -> list[dict]:
            return sorted(rows, key=lambda row: activity.get(str(row.get("tab_id") or ""), 0))

        def render() -> str:
            return json.dumps(core, ensure_ascii=False, separators=(",", ":"))

        def tokens(value: str) -> int:
            if max_chars is not None:
                return 0 if len(value) <= max_chars else max_tokens + 1
            try:
                from litellm import token_counter
                return int(token_counter(model=model or None, text=value))
            except Exception:
                return (len(value) + 3) // 4

        text = render()
        while tokens(text) > max_tokens and core["completed_workers"]:
            core["completed_workers"].pop()
            text = render()
        while tokens(text) > max_tokens and core["tabs"]:
            removable = next((tab for tab in least_active(core["tabs"]) if tab.get("owner_kind") not in {"user", "worker"}), None)
            if removable is None:
                break
            core["tabs"].remove(removable)
            core["tabs_omitted"] = tab_total - len(core["tabs"])
            text = render()
        if tokens(text) > max_tokens:
            core["active_workers"] = [
                {
                    key: (str(worker.get(key))[:240] if key in {"objective", "error"} else worker.get(key))
                    for key in ("worker_id", "status", "objective", "error", "artifact")
                    if worker.get(key) not in (None, "")
                }
                for worker in core["active_workers"]
            ]
            text = render()
        if tokens(text) > max_tokens:
            newest = {id(tab) for tab in least_active(core["tabs"])[-12:]}
            core["tabs"] = [
                {key: tab[key] for key in ("tab_id", "url", "owner_kind", "owner_id", "uncertain") if key in tab}
                for tab in core["tabs"] if id(tab) in newest
            ]
            core["tabs_omitted"] = tab_total - len(core["tabs"])
            text = render()
        if tokens(text) > max_tokens:
            for worker in core["active_workers"]:
                if worker.get("objective"):
                    worker["objective"] = str(worker["objective"])[:80]
                if worker.get("error"):
                    worker["error"] = str(worker["error"])[:120]
            text = render()
        if tokens(text) > max_tokens:
            # recovery_warning and attention prose are operator-authored and were
            # never in the drop ladder, so a long one survived every stage and hit
            # the raw slice below. The parent then received a JSON fragment.
            core["recovery_warning"] = str(core["recovery_warning"])[:200]
            for item in core["attention"]:
                if isinstance(item, dict) and item.get("reason"):
                    item["reason"] = str(item["reason"])[:200]
            text = render()
        if tokens(text) > max_tokens:
            # Last resort: a *valid, honest* minimal object beats a truncated one.
            core = {
                "status": core["status"],
                "attention_count": len(core["attention"]),
                "active_worker_count": len(core["active_workers"]),
                "tab_count": len(core["tabs"]),
                "artifact_manifest": core["artifact_manifest"],
                "truncated": True,
            }
            text = render()
        return text

    def start_monitor(self) -> asyncio.Task:
        if self._monitor_task and not self._monitor_task.done():
            return self._monitor_task
        liveview_start = asyncio.create_task(self.liveview.start())
        self._tasks.add(liveview_start)
        liveview_start.add_done_callback(self._tasks.discard)
        warm_imports = asyncio.create_task(asyncio.to_thread(_warm_browser_launch_imports))
        self._tasks.add(warm_imports)
        warm_imports.add_done_callback(self._tasks.discard)
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._memory_pressure_task = asyncio.create_task(self._memory_pressure_loop())
        return self._monitor_task

    def _reconcile_liveview_health(self, liveview_ready: bool) -> None:
        """Project capture loss without treating it as browser-process loss."""
        if not liveview_ready:
            return
        warning = "Live view unavailable; browser automation continues."
        for record in list(self._records.values()):
            runtime = self.backend.runtime(record.owner)
            runtime_id = str(getattr(runtime, "liveview_runtime_id", "") or "")
            if (
                record.status != "sleeping" and runtime is not None
                and (not runtime_id or not self.liveview.has_runtime(runtime_id))
            ):
                # Capture is optional and is not browser health. A valid
                # virtual-display fallback has no runtime id; restarting it here
                # destroyed healthy dirty forms between agent calls. Actual
                # browser disconnects and context-wide protocol failures have
                # their own recovery paths.
                if not record.recovery_warning:
                    record.recovery_warning = warning
                    self.store.save(record)
                    self._emit_browser_event(record, "live_view_unavailable")
            elif (
                record.recovery_warning == warning and runtime_id
                and self.liveview.has_runtime(runtime_id)
            ):
                record.recovery_warning = ""
                self.store.save(record)
                self._emit_browser_event(record, "live_view_available")

    async def _monitor_loop(self) -> None:
        while True:
            try:
                now = time.time()
                # This sweep is an observer. Calling start() here turns every
                # app process into a daemon owner: a stale/secondary process
                # then reaps the healthy shared container on its next 30-second
                # pass, and competing processes alternate killing the display
                # underneath Firefox. Runtime creation remains the explicit
                # recovery/start path; supervision only projects current health.
                liveview_health = await self.liveview.health()
                liveview_ready = bool(liveview_health.get("available"))
                self._reconcile_liveview_health(liveview_ready)
                tab_sleep_seconds = self._background_tab_sleep_seconds()
                swept_owners: set[str] = set()
                for record in list(self._records.values()):
                    if record.status == "sleeping" or self.backend.runtime(record.owner) is None:
                        continue
                    for tab in list(record.tabs.values()):
                        lease = tab.lease
                        if (
                            lease and lease.actor == "user" and lease.expires_at
                            and lease.expires_at <= now
                            and not self._takeover_group_has_connected_view(record, tab)
                        ):
                            self.release_control(
                                record.owner, record.session_id, tab.tab_id,
                                note="User-control lease expired while disconnected.",
                                preserve_hold=True,
                            )
                    # Background management is deliberately per tab.  A live
                    # viewer is no longer an eviction trigger: each owner is
                    # swept at most once per lifecycle pass and only clean tabs
                    # whose own activity clock is at least six hours old can
                    # be closed.
                    if record.owner not in swept_owners:
                        await self._suspend_background_tabs(record.owner, now=now)
                        swept_owners.add(record.owner)
                    workers_running = any(w.get("status") in {"queued", "running", "waiting"} for w in record.workers.values())
                    user_control = any(t.lease and t.lease.actor == "user" for t in record.tabs.values())
                    viewer_connected = self.live_views.connected_count(record.workspace_id) > 0
                    try:
                        from ascended_browser.runtime.runs import is_active as agent_run_active
                        parent_running = agent_run_active(record.session_id)
                    except Exception:
                        parent_running = False
                    resident = any(
                        self._pages.get((record.workspace_id, tab.tab_id)) is not None
                        for tab in record.tabs.values()
                    )
                    # Never turn an in-page draft, a challenge, a held action,
                    # or a merely recent clean tab into a workspace-wide close.
                    # Once the per-tab sweep has safely slept every resident
                    # page, the empty workspace may release its shared owner
                    # runtime on the same six-hour policy boundary.
                    if (
                        not resident and not workers_running and not parent_running
                        and not user_control and not viewer_connected
                        and now - max(
                            float(record.last_human_activity_at or 0),
                            float(record.last_runtime_activity_at or 0),
                        ) >= tab_sleep_seconds
                    ):
                        await self.sleep(record.owner, record.session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Browser workspace lifecycle sweep failed", exc_info=True)
            for owner in {record.owner for record in self._records.values()}:
                self.backend.heartbeat(owner)
            self._evict_idle_records(now)
            self._evict_stale_domain_tokens()
            await asyncio.sleep(30)

    def _evict_idle_records(self, now: float) -> None:
        """Drop sleeping, browser-less workspace records that have been idle
        longer than ``_WORKSPACE_EVICT_SECONDS``. Rehydrated from the store on
        next access, so this only bounds the in-memory cache.
        """
        if not self._records:
            return
        for workspace_id, record in list(self._records.items()):
            if record.status != "sleeping":
                continue
            if self.backend.runtime(record.owner) is not None:
                continue
            last = max(
                float(getattr(record, "last_human_activity_at", 0) or 0),
                float(getattr(record, "last_runtime_activity_at", 0) or 0),
            )
            if last and (now - last) > _WORKSPACE_EVICT_SECONDS:
                self._records.pop(workspace_id, None)
                # Drop any live page references for this workspace.
                for key in list(self._pages):
                    if key[0] == workspace_id:
                        self._pages.pop(key, None)
                log.debug("Evicted idle browser workspace %s from RAM", workspace_id)

    def _evict_stale_domain_tokens(self) -> None:
        """Prune ``_domain_tokens`` lists whose entries are all outside the
        rate-limit window — the keys otherwise accumulate per distinct domain
        ever visited for the process lifetime.
        """
        if not self._domain_tokens:
            return
        now = time.monotonic()
        for domain in list(self._domain_tokens.keys()):
            recent = [s for s in self._domain_tokens[domain] if now - s < _DOMAIN_TOKEN_WINDOW]
            if recent:
                self._domain_tokens[domain] = recent
            else:
                self._domain_tokens.pop(domain, None)

    async def close_all(self) -> None:
        self.begin_shutdown()
        for key in list(self._diagnostics):
            self._discard_diagnostics(key)
        if self._monitor_task:
            self._monitor_task.cancel()
        if self._memory_pressure_task:
            self._memory_pressure_task.cancel()
        for task in list(self._liveview_disconnect_tasks.values()):
            task.cancel()
        self._liveview_disconnect_tasks.clear()
        for task in list(self._liveview_liveness_tasks.values()):
            task.cancel()
        self._liveview_liveness_tasks.clear()
        self._liveview_client_activity.clear()
        self._liveview_liveness_expiring.clear()
        for task in list(self._liveview_page_loss_tasks.values()):
            task.cancel()
        self._liveview_page_loss_tasks.clear()
        prewarms = list(self._prewarm_tasks.values())
        relaunches = [task for task in self._capture_relaunch_tasks.values() if not task.done()]
        recoveries = [
            task for task in self._tasks
            if not task.done() and getattr(task.get_coro(), "__name__", "") == "_recover"
        ]
        for task in (*prewarms, *relaunches, *recoveries):
            task.cancel()
        if prewarms or relaunches or recoveries:
            await asyncio.gather(*prewarms, *relaunches, *recoveries, return_exceptions=True)
        self._prewarm_tasks.clear()
        # ``sleep`` records the durable recovery classification before the
        # backend closes its persistent contexts.  Those context closes emit
        # ordinary Playwright ``close`` events a moment later.  They are a
        # planned application shutdown, not a browser/page crash; without this
        # marker the callback races the checkpoint and can turn a clean GET
        # page into a spurious uncertain/non-restorable draft.  Dirty state is
        # still classified by ``sleep`` and is never made revivable here.
        self._intentional_page_closes.update(self._pages)
        for record in list(self._records.values()):
            if record.status != "sleeping":
                try: await self.sleep(record.owner, record.session_id)
                except Exception: log.warning("Could not checkpoint browser workspace %s", record.workspace_id, exc_info=True)
        await self.backend.close_all()
        cursor_probes = [task for task in self._tasks
                         if task.get_name().startswith("browser-cursor-geometry:")]
        if cursor_probes:
            _, pending = await asyncio.wait(cursor_probes, timeout=0.5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*cursor_probes, return_exceptions=True)
        await self.liveview.stop()
