from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import json
import os
import secrets
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ascended_browser.runtime.platform import try_lock_exclusive, unlock as unlock_file
from ascended_browser.runtime.settings import get_setting
from ascended_browser._app.browser_workspace.liveview import (
    ensure_liveview_audio_sink,
    release_liveview_audio_sink,
)

log = logging.getLogger(__name__)

# Kernel OOM priority for the browser tree. Firefox spreads its memory over
# many processes, so under memory exhaustion the server — the largest single
# process — was the kernel's victim, taking every agent run, tab and live view
# with it. A killed content process costs one tab, which the manager already
# restores through its crash path; a killed browser main process costs the
# runtime, which relaunches. Content is preferred over the main process.
# Raising a value needs no privilege; Firefox may lower a content process back
# to its own 100-167 when priorities change, so every heartbeat re-applies it.
BROWSER_MAIN_OOM_SCORE_ADJ = 500
BROWSER_CHILD_OOM_SCORE_ADJ = 800


def _raise_oom_score_adj(pid: int, floor: int) -> None:
    path = f"/proc/{pid}/oom_score_adj"
    try:
        with open(path, "r+", encoding="ascii") as fh:
            if int(fh.read().strip() or 0) < floor:
                fh.seek(0)
                fh.write(str(floor))
    except (OSError, ValueError):
        pass


def prefer_browser_as_oom_victim(main_pids: list[int]) -> None:
    """Make the browser processes, not the server, the kernel's OOM victims."""
    if not main_pids or not os.path.isdir("/proc"):
        return
    children: dict[int, list[int]] = {}
    for item in os.listdir("/proc"):
        if not item.isdigit():
            continue
        try:
            with open(f"/proc/{item}/stat", encoding="ascii", errors="ignore") as fh:
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(item))
    for pid in main_pids:
        _raise_oom_score_adj(pid, BROWSER_MAIN_OOM_SCORE_ADJ)
        stack = list(children.get(pid, ()))
        while stack:
            child = stack.pop()
            _raise_oom_score_adj(child, BROWSER_CHILD_OOM_SCORE_ADJ)
            stack.extend(children.get(child, ()))
CONTROL_PREPARATION_TIMEOUT_SECONDS = 5.0
# Playwright's Firefox (Juggler) serializes every tab activation --
# bring_to_front, mouse dispatch, wheel, reload -- behind one global promise
# chain that waits for the browser's TabSwitchDone. One switch that never
# completes wedges that chain for the life of the process, and cancelling the
# Python call does not unblock it (2026-10-03: every live-view mint spent the
# whole 5 s budget in "activate" for 13 minutes, holding the owner's effect
# barrier exclusively each time, until Firefox was relaunched). Activation
# therefore gets a short budget of its own; a stall then falls through to a
# content-channel probe that does not touch the chain.
CONTROL_ACTIVATION_TIMEOUT_SECONDS = 1.5
CONTROL_VISIBILITY_PROBE_TIMEOUT_SECONDS = 0.75
_RUNTIME_SHUTDOWN_TIMEOUT_SECONDS = 5.0
# How long a pinned desktop browser may be missing before the owner's next
# launch falls back (announced) to this host's browser. Shorter outages are a
# truthful failure instead: lids close, links blip, apps restart.
DESKTOP_BROWSER_RECONNECT_GRACE_SECONDS = 120.0
TITLE_RESTORE_TIMEOUT_SECONDS = 0.25

_DYNAMIC_VIEWPORT_CAPABILITIES = frozenset({
    "runtime_update", "resize_window", "geometry_revision",
    "media_frame_boundary", "control_fencing", "release_all", "window_binding",
    "content_capture",
})

_CONTENT_CAPTURE_CAPABILITIES = frozenset({
    "runtime_update", "geometry_revision", "media_frame_boundary",
    "control_fencing", "release_all", "window_binding", "content_capture",
})

_PAGE_GEOMETRY_SCRIPT = """() => ({
    width: window.innerWidth,
    height: window.innerHeight,
    outer_width: window.outerWidth,
    outer_height: window.outerHeight,
    content_x: window.mozInnerScreenX,
    content_y: window.mozInnerScreenY,
})"""


def _mark_presented(page: Any) -> None:
    try:
        from ascended_browser._app.browser_presented_tab import mark_presented
    except ImportError:
        return
    mark_presented(page)


def supports_dynamic_viewport(live_runtime: Any | None) -> bool:
    capabilities = dict(getattr(live_runtime, "capabilities", {}) or {})
    return all(capabilities.get(name, False) for name in _DYNAMIC_VIEWPORT_CAPABILITIES)


def supports_content_capture(live_runtime: Any | None) -> bool:
    capabilities = dict(getattr(live_runtime, "capabilities", {}) or {})
    return all(capabilities.get(name, False) for name in _CONTENT_CAPTURE_CAPABILITIES)


async def _page_geometry(
    page: Any, *, require_content_origin: bool = False,
) -> dict[str, int]:
    raw = await page.evaluate(_PAGE_GEOMETRY_SCRIPT)
    if not isinstance(raw, dict):
        raise BrowserBackendError("Firefox did not report browser geometry")
    try:
        geometry: dict[str, int] = {
            name: int(round(float(raw.get(name))))
            for name in ("width", "height", "outer_width", "outer_height")
        }
        for name in ("content_x", "content_y"):
            value = raw.get(name)
            if value is None and not require_content_origin:
                geometry[name] = 0
            else:
                geometry[name] = int(round(float(value)))
    except (TypeError, ValueError, OverflowError) as exc:
        raise BrowserBackendError("Firefox reported invalid browser geometry") from exc
    if (
        geometry["width"] < 1 or geometry["height"] < 1
        or geometry["outer_width"] < geometry["width"]
        or geometry["outer_height"] < geometry["height"]
        or geometry["content_x"] < 0 or geometry["content_y"] < 0
    ):
        raise BrowserBackendError("Firefox reported invalid browser geometry")
    if require_content_origin:
        # liveviewd binds its selected native top-level at (0, 0). Normally
        # mozInnerScreen{X,Y} therefore names the capture rect directly. On
        # some Firefox/X11 tab switches it instead reports an old/global or
        # viewport-relative origin. Do not mix in window.screenY here: it is
        # not the daemon's native coordinate system. Normalize only values
        # that contradict the page's own outer/inner bounds, so a valid native
        # origin remains authoritative while stale coordinates cannot expose
        # Firefox chrome or reject the whole live view.
        horizontal_chrome = geometry["outer_width"] - geometry["width"]
        vertical_chrome = geometry["outer_height"] - geometry["height"]
        if (
            geometry["content_x"] + geometry["width"] > geometry["outer_width"]
            or geometry["content_x"] > horizontal_chrome
        ):
            log.warning("Firefox reported an inconsistent content X origin; using bound-window coordinates")
            geometry["content_x"] = 0
        if (
            geometry["content_y"] < vertical_chrome
            or geometry["content_y"] + geometry["height"] > geometry["outer_height"]
        ):
            log.warning("Firefox reported an inconsistent content Y origin; using bound-window coordinates")
            geometry["content_y"] = vertical_chrome
    return geometry


def _content_rect(geometry: dict[str, int]) -> dict[str, int]:
    return {
        "x": geometry["content_x"],
        "y": geometry["content_y"],
        "width": geometry["width"],
        "height": geometry["height"],
    }


def _bounded_liveview_window_size(
    runtime: Any, *, width: int, height: int,
) -> tuple[int, int]:
    """Keep an observed Firefox outer window inside its fixed live display.

    Firefox can report an outer size restored from its profile that is larger
    than the fingerprint-sized X display owned by liveviewd.  The DOM value is
    useful for deriving chrome insets, but it is not authority to bind an X11
    window outside that display.  Projecting the same insets onto this bounded
    outer size lets the native binding resize once, then re-observe and verify
    the actual content rectangle before admitting input.
    """
    display_width = int(getattr(runtime, "display_width", 0) or 0)
    display_height = int(getattr(runtime, "display_height", 0) or 0)
    if display_width < 320 or display_height < 240:
        raise BrowserBackendError("The live-view display is too small for a browser window")
    return (
        max(320, min(int(width), display_width)),
        max(240, min(int(height), display_height)),
    )


def _content_size_for_bound_window(
    geometry: dict[str, int], *, window_width: int, window_height: int,
) -> tuple[int, int]:
    """Project a page's total browser chrome onto a native bound window.

    The launch anchor can report a stale top-level size from Firefox session
    restore.  Its content origin is not needed to establish a restorable
    *size*: only the total horizontal and vertical chrome matters.  Keeping
    that calculation separate from a capture rectangle avoids accidentally
    turning a page-relative origin into native capture authority.
    """
    chrome_width = int(geometry["outer_width"]) - int(geometry["width"])
    chrome_height = int(geometry["outer_height"]) - int(geometry["height"])
    if chrome_width < 0 or chrome_height < 0:
        raise BrowserBackendError("Firefox reported inconsistent browser chrome geometry")
    content_width = int(window_width) - chrome_width
    content_height = int(window_height) - chrome_height
    if content_width < 320 or content_height < 240:
        raise BrowserBackendError("The accepted browser window is too small for its page content")
    return content_width, content_height


def _content_rect_for_bound_window(
    geometry: dict[str, int], *, window_width: int, window_height: int,
) -> dict[str, int]:
    """Apply observed Firefox chrome insets to a selected bound window.

    A Playwright page can name a different Firefox top-level than the live
    view's current one. Binding deliberately retains the accepted live-view
    window size, so the page's current screen-sized content rectangle cannot
    be passed through verbatim. Its chrome insets are the invariant; project
    those onto the requested native window, then re-observe after binding.
    """
    outer_width = int(geometry["outer_width"])
    outer_height = int(geometry["outer_height"])
    left = int(geometry["content_x"])
    top = int(geometry["content_y"])
    right = outer_width - left - int(geometry["width"])
    bottom = outer_height - top - int(geometry["height"])
    if min(left, top, right, bottom) < 0:
        raise BrowserBackendError("Firefox reported inconsistent browser chrome geometry")
    content_width, content_height = _content_size_for_bound_window(
        geometry, window_width=window_width, window_height=window_height,
    )
    return {
        "x": left,
        "y": top,
        "width": content_width,
        "height": content_height,
    }


def _content_rect_for_runtime_window(
    geometry: dict[str, int], *, window_width: int, window_height: int,
) -> dict[str, int]:
    """Return a capture rectangle in the daemon's current window coordinates.

    A selected Playwright tab can lag its newly-bound native window by one
    geometry sample.  Its raw content rectangle is only directly usable when
    the observed outer window is the retained window; otherwise preserve the
    observed Firefox chrome insets and project them onto the retained window.
    Treating a former full-screen rectangle as the new small window's crop is
    how a correct tab selection becomes a mostly black live surface.
    """
    direct = _content_rect(geometry)
    observed_outer_matches = (
        abs(int(geometry["outer_width"]) - int(window_width)) <= 2
        and abs(int(geometry["outer_height"]) - int(window_height)) <= 2
    )
    direct_fits = (
        direct["x"] >= 0 and direct["y"] >= 0
        and direct["width"] > 0 and direct["height"] > 0
        and direct["x"] + direct["width"] <= int(window_width)
        and direct["y"] + direct["height"] <= int(window_height)
    )
    if observed_outer_matches and direct_fits:
        return direct
    return _content_rect_for_bound_window(
        geometry, window_width=window_width, window_height=window_height,
    )


def _capture_alignment_matches(
    accepted: dict[str, Any], expected: dict[str, int], *, tolerance: int = 2,
) -> bool:
    """Whether an adapter explicitly confirmed the expected content crop.

    Older live-view daemons did not return capture fields on every geometry
    update; retain that compatibility.  When a daemon does claim a crop, never
    continue with a contradictory one — it is visible corruption, not a minor
    cosmetic difference.
    """
    keys = tuple(f"capture_{name}" for name in ("x", "y", "width", "height"))
    if not all(key in accepted and accepted[key] is not None for key in keys):
        return True
    return all(
        abs(int(accepted[f"capture_{name}"]) - int(expected[name])) <= tolerance
        for name in expected
    )


def _runtime_capture_matches(
    live_runtime: Any, expected: dict[str, int], *, tolerance: int = 2,
) -> bool:
    """Whether the supervisor's current crop already covers ``expected``.

    This is deliberately stricter than ``_capture_alignment_matches``.  The
    latter preserves compatibility with older daemon replies that omitted
    capture fields; a resize no-op, on the other hand, is safe only when the
    currently held runtime has explicit, matching crop bounds.  Treating a
    missing field as a match here could leave a black/chrome-cropped stream
    uncorrected merely because the page happened to have the requested size.
    """
    try:
        return all(
            abs(int(getattr(live_runtime, f"capture_{name}")) - int(expected[name]))
            <= tolerance
            for name in ("x", "y", "width", "height")
        )
    except (TypeError, ValueError, AttributeError):
        return False


class BrowserBackendError(RuntimeError):
    pass


class BrowserRuntimeUnavailable(BrowserBackendError):
    """The managed browser could not establish a usable runtime.

    This is deliberately distinct from page navigation failure.  The backend
    has already performed its bounded launch/fallback work by the time this
    escapes, so callers must not turn it into repeated URL opens.
    """

    error_kind = "browser_runtime_unavailable"


def owner_key(owner: str) -> str:
    return hashlib.sha256((owner or "default").encode()).hexdigest()[:24]


@dataclass
class PageCreationTicket:
    workspace_id: str
    tab_id: str
    future: asyncio.Future


@dataclass
class OwnerRuntime:
    key: str
    profile_dir: Path
    context_manager: Any
    context: Any
    lock_file: Any
    generation: int
    opened_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)
    closing: bool = False
    page_creation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_page_claim: PageCreationTicket | None = None
    # The context's first page owns the Firefox window. Closing it stalls every
    # later navigation, and letting the context reach zero pages makes new_page
    # fail permanently with "Protocol error (Browser.newPage): window is null".
    # So one page is held back from the workspace and closed only with the owner.
    anchor_page: Any = None
    wedged: bool = False
    adblock: Any = None
    liveview_runtime_id: str = ""
    display: str = ""
    xauthority: str = ""
    # Private null sink receiving the browser's audio ("" when audio
    # forwarding is unavailable). The daemon captures its monitor; the
    # machine's real output devices never play the stream.
    audio_sink: str = ""
    audio_module: int = 0
    display_width: int = 0
    display_height: int = 0
    dynamic_viewport: bool = False
    viewport_width: int = 0
    viewport_height: int = 0
    window_width: int = 0
    window_height: int = 0
    # Immutable geometry observed after this owner runtime launches. Agent
    # viewport `restore` returns to this baseline without consulting mutable
    # global settings or rewriting the fingerprint pin.
    launch_viewport_width: int = 0
    launch_viewport_height: int = 0
    launch_window_width: int = 0
    launch_window_height: int = 0
    # The daemon binding is window-scoped while Playwright exposes pages. Keep
    # the last successfully bound page identity so a same-tab spectator does
    # not release the active controller's held input or churn the media encoder.
    bound_liveview_page: Any = None
    # The proxy URL the browser was launched with ("" when direct). The bot-wall
    # sidecar reads this so its clearance harvest exits from the same IP —
    # Cloudflare binds cf_clearance to it.
    proxy: str = ""
    # Engine that launched this runtime (None: the backend's default engine).
    # An embedded runtime (desktop Electron guests) has no profile process,
    # display, anchor page or live-view capture.
    engine: Any = None

    @property
    def embedded(self) -> bool:
        return bool(getattr(self.engine, "embedded", False))

    @property
    def presentation(self) -> str:
        return str(getattr(self.engine, "presentation", "") or "")


class BrowserBackend(ABC):
    topology = "unknown"

    @abstractmethod
    async def ensure_owner(self, owner: str, on_page: Callable[[str, Any], None]) -> tuple[OwnerRuntime, bool]: ...

    @abstractmethod
    def runtime(self, owner: str) -> OwnerRuntime | None: ...

    @abstractmethod
    async def close_owner(self, owner: str) -> None: ...

    @abstractmethod
    async def close_all(self) -> None: ...

    @abstractmethod
    def health(self, owner: str) -> dict[str, Any]: ...

    @abstractmethod
    async def activate(self, owner: str, page: Any) -> None: ...

    async def prepare_user_control(self, owner: str, page: Any) -> None:
        await self.activate(owner, page)

    async def release_user_control(self, owner: str, page: Any) -> None:
        return None

    async def resize_user_view(
        self, owner: str, page: Any, width: int, height: int,
    ) -> dict[str, int]:
        raise BrowserBackendError("Dynamic browser sizing is unavailable")

    async def create_page(self, owner: str) -> Any:
        runtime = self.runtime(owner)
        if runtime is None:
            raise BrowserBackendError("Owner browser runtime is not active")
        return await runtime.context.new_page()

    async def metadata(self, owner: str, page: Any) -> dict[str, str]:
        return {"url": str(getattr(page, "url", "") or ""), "title": str(await page.title() or "")}


class PersistentOwnerBackend(BrowserBackend):
    """One persistent Camoufox process/profile/context for each authenticated owner."""

    topology = "process_per_owner"

    def __init__(
        self,
        root: str | Path,
        *,
        headless_mode: Callable[[], bool | str],
        on_disconnect: Callable[[str], None],
        liveview: Any | None = None,
        engine: Any | None = None,
        engine_selector: Callable[[str], Any | None] | None = None,
    ) -> None:
        self.root = Path(root) / "profiles"
        self.root.mkdir(parents=True, exist_ok=True)
        self._headless_mode = headless_mode
        self._on_disconnect = on_disconnect
        self.liveview = liveview
        if engine is None:
            from ascended_browser._app.browser_workspace.engine import CamoufoxEngine
            engine = CamoufoxEngine()
            if engine_selector is None:
                # Production default: the owner's connected desktop browser
                # (use_for_agent) replaces Camoufox for that owner only.
                from ascended_browser._app.browser_workspace.engine import desktop_engine_for_owner
                engine_selector = desktop_engine_for_owner
        self.engine = engine
        self._engine_selector = engine_selector
        self._engine_notices: dict[str, str] = {}
        self._runtimes: dict[str, OwnerRuntime] = {}
        self._launch_locks: dict[str, asyncio.Lock] = {}

    def profile_dir(self, owner: str) -> Path:
        return self.root / owner_key(owner)

    def _owner_lock_path(self, profile: Path) -> Path:
        # The lock must not live inside the directory migration/reset atomically
        # replaces, otherwise the rename moves the held inode out of the way and
        # another process can lock a newly-created file at the original path.
        return profile.parent / f".{profile.name}.owner-runtime.lock"

    def runtime(self, owner: str) -> OwnerRuntime | None:
        return self._runtimes.get(owner_key(owner))

    def _acquire_profile_lock(self, profile: Path):
        profile.mkdir(parents=True, exist_ok=True)
        lock_path = self._owner_lock_path(profile)
        handle = lock_path.open("a+")
        try:
            try_lock_exclusive(handle.fileno())
        except BlockingIOError as exc:
            handle.close()
            raise BrowserBackendError(
                "This owner's persistent browser profile is active in another server process."
            ) from exc
        handle.seek(0); handle.truncate()
        handle.write(json.dumps({"app_pid": os.getpid(), "locked_at": time.time()}))
        handle.flush(); os.fsync(handle.fileno())
        return handle

    @staticmethod
    def _release_profile_lock(handle: Any) -> None:
        try:
            unlock_file(handle.fileno())
            handle.close()
        except Exception:
            pass

    def acquire_owner_install_lock(self, owner: str) -> Any:
        if self.runtime(owner) is not None:
            raise BrowserBackendError("Close all of this owner's browser workspaces first.")
        return self._acquire_profile_lock(self.profile_dir(owner))

    def release_owner_install_lock(self, handle: Any) -> None:
        self._release_profile_lock(handle)

    def _prepare_profile_launch(self, profile: Path) -> None:
        # Firefox can leave these after SIGKILL. Only remove them when /proc says
        # no live Firefox/Camoufox process owns this exact profile.
        if self._process_ids(profile):
            return
        for name in ("lock", ".parentlock"):
            path = profile / name
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
            except OSError:
                pass

    def _generation(self, profile: Path) -> int:
        path = profile / ".odysseus-profile-generation"
        try:
            generation = int(path.read_text(encoding="utf-8").strip()) + 1
        except (OSError, ValueError):
            generation = 1
        fd, temp_path = tempfile.mkstemp(prefix="generation-", suffix=".tmp", dir=profile)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(generation)); fh.flush(); os.fsync(fh.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path): os.unlink(temp_path)
        return generation

    def _launch_timeout(self) -> float:
        value = float(get_setting("browser_workspace_launch_timeout_seconds", 25) or 25)
        if value == 90.0:
            # Former default, still sitting in some settings.json files.
            value = 25.0
        return max(5.0, min(value, 30.0))

    @staticmethod
    def _capture_display_request(
        runtime_id: str, generation: int,
        width: int, height: int,
        capture_width: int, capture_height: int,
        audio_sink: str,
    ) -> dict[str, Any]:
        """Keep the launch-time capture request in one place for retry."""
        return dict(
            runtime_id=str(runtime_id),
            width=int(width),
            height=int(height),
            fps=int(get_setting("browser_liveview_fps", 60) or 60),
            capture_width=int(capture_width),
            capture_height=int(capture_height),
            output_height=max(360, min(1440, int(get_setting("browser_liveview_output_height", 1080) or 1080))),
            runtime_generation=generation,
            local_cursor=True,
            audio_source=f"{audio_sink}.monitor" if audio_sink else "",
            bitrate_cap=max(0, int(get_setting("browser_liveview_start_bitrate", 4000000) or 0)),
            xid_capture=bool(get_setting("browser_liveview_xid_capture", True)),
            repaint_capture=bool(get_setting("browser_liveview_repaint_capture", True)),
            pipeline_mode=str(get_setting("browser_liveview_pipeline_mode", "inprocess") or "inprocess"),
            encoder=str(get_setting("browser_liveview_encoder", "auto") or "auto"),
            vp8_profile=str(get_setting("browser_liveview_vp8_profile", "current") or "current"),
            freshness_pacer=bool(get_setting("browser_liveview_freshness_pacer", True)),
            pacer_max_age_ms=max(50, min(2000, int(get_setting("browser_liveview_pacer_max_age_ms", 150) or 150))),
            playout_delay_ms=(
                max(0, min(1000, int(get_setting("browser_liveview_native_playout_delay_ms", 60) or 0)))
                if str(get_setting("browser_liveview_presentation_profile", "zero_hold") or "zero_hold") == "zero_hold"
                else None
            ),
            playout_delay_min_ms=max(0, min(1000, int(get_setting("browser_liveview_native_playout_min_ms", 10) or 0))),
        )

    async def _enter_browser_context(
        self, launch_options: dict[str, Any], *, timeout: float, engine: Any = None,
    ):
        engine = engine if engine is not None else self.engine
        cm = engine.context_manager(launch_options)
        try:
            context = await asyncio.wait_for(cm.__aenter__(), timeout=timeout)
            return cm, context
        except BaseException:
            try:
                await asyncio.wait_for(engine.shutdown(cm), timeout=5)
            except Exception:
                pass
            raise

    async def _abandon_profile_launch(
        self, profile: Path, liveview_runtime_id: str = "",
    ) -> None:
        """Stop a launch that timed out so a virtual retry can use this profile."""
        for pid in self._process_ids(profile):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        if self.liveview is not None and liveview_runtime_id:
            try:
                await asyncio.wait_for(self.liveview.delete_runtime(liveview_runtime_id), timeout=3)
            except Exception:
                pass
        self._prepare_profile_launch(profile)

    # -- engine selection (docs/desktop/ARCHITECTURE.md § Engine selection) --

    def _engine_pin_path(self, owner: str) -> Path:
        return self.root / f".{owner_key(owner)}.engine.json"

    def engine_pin(self, owner: str) -> str:
        try:
            data = json.loads(self._engine_pin_path(owner).read_text(encoding="utf-8"))
            return str(data.get("engine") or "")
        except (OSError, ValueError, AttributeError):
            return ""

    def _write_engine_pin(self, owner: str, engine_id: str) -> None:
        path = self._engine_pin_path(owner)
        if self.engine_pin(owner) == engine_id:
            return
        try:
            if not engine_id:
                path.unlink(missing_ok=True)
                return
            fd, temp_path = tempfile.mkstemp(prefix=".engine-", suffix=".tmp", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"engine": engine_id, "at": time.time()}, fh)
                os.replace(temp_path, path)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
        except OSError:
            log.warning("Could not record the browser engine choice", exc_info=True)

    def clear_engine_pin(self, owner: str) -> None:
        """Explicitly let this owner's next launch use the server browser."""
        self._write_engine_pin(owner, "")

    def _select_engine(self, owner: str, *, record: bool = True) -> Any:
        """The engine for this owner's next runtime.

        The desktop browser is used only while the owner's desktop host is
        connected with use_for_agent. The choice is recorded per owner; once
        recorded, a missing desktop host is a truthful failure, never a silent
        switch to Camoufox. A desktop host connected with use_for_agent false
        is the user's explicit opt-out and clears the record.
        """
        selector = self._engine_selector
        if selector is None:
            return self.engine
        chosen = selector(owner)
        if chosen is not None:
            if record:
                self._write_engine_pin(owner, str(getattr(chosen, "engine_id", "") or ""))
            return chosen
        from ascended_browser._app.browser_workspace.engine import (
            ElectronEmbeddedEngine, desktop_host_absent_for, desktop_host_connected,
        )

        if self.engine_pin(owner) == ElectronEmbeddedEngine.engine_id:
            if desktop_host_connected(owner):
                if record:
                    self._write_engine_pin(owner, "")
                return self.engine
            absent = desktop_host_absent_for(owner)
            if absent < DESKTOP_BROWSER_RECONNECT_GRACE_SECONDS:
                # Just dropped (lid closed, network blip, app restarting): a
                # silent switch would continue the task in another browser
                # profile, so fail truthfully and let the desktop come back.
                raise BrowserRuntimeUnavailable(
                    "This browser workspace uses the Ascended desktop browser, which just "
                    "disconnected. Open the desktop app for this host (or turn off 'Use desktop "
                    "browser for the agent' there) and retry. Do not retry browser actions in this turn."
                )
            # Gone for a while (e.g. the user is on the web app from another
            # device): use this host's own browser again, and say so once.
            if record:
                self._write_engine_pin(owner, "")
                self._engine_notices[owner] = (
                    "The Ascended desktop browser has not been connected for "
                    f"{int(absent // 60)} min, so this chat's browser now runs on this host. "
                    "Pages signed in only in the desktop browser need signing in again here; "
                    "tell the user if that matters for the task."
                )
        return self.engine

    def pop_engine_notice(self, owner: str) -> str:
        """A pending engine-switch notice for this owner, reported once."""
        return self._engine_notices.pop(owner, "")

    def _runtime_engine(self, runtime: OwnerRuntime | None) -> Any:
        engine = getattr(runtime, "engine", None) if runtime is not None else None
        return engine if engine is not None else self.engine

    async def _ensure_embedded_owner(
        self, owner: str, key: str, engine: Any, on_page: Callable[[str, Any], None],
    ) -> tuple[OwnerRuntime, bool]:
        """Attach to the owner's desktop browser: no profile, display or proxy."""
        cm = None
        started = time.monotonic()
        try:
            launch_options = engine.launch_options(profile=self.profile_dir(owner), fingerprint=None)
            cm, context = await self._enter_browser_context(
                launch_options, timeout=self._launch_timeout(), engine=engine,
            )
            runtime = OwnerRuntime(
                key=key,
                profile_dir=self.profile_dir(owner),
                context_manager=cm,
                context=context,
                lock_file=None,
                generation=0,
                engine=engine,
            )
            self._runtimes[key] = runtime
            log.info("Desktop browser attached for %s after %.1fs", key, time.monotonic() - started)
            identify = getattr(engine, "identify_soon", None)
            if callable(identify):
                # Popups and other pages the gateway reports: learn their guest key.
                context.on("page", identify)
            context.on("page", lambda page, runtime_key=key: on_page(runtime_key, page))
            context.on("close", lambda _context=None, runtime_key=key: self._disconnected(runtime_key))
            browser = getattr(context, "browser", None)
            if browser is not None:
                browser.on("disconnected", lambda _browser=None, runtime_key=key: self._disconnected(runtime_key))
            for page in list(context.pages):
                if callable(identify):
                    identify(page)
                reconciliation = on_page(key, page)
                if inspect.isawaitable(reconciliation):
                    await reconciliation
            return runtime, True
        except BaseException as exc:
            self._runtimes.pop(key, None)
            if cm is not None:
                try:
                    await asyncio.wait_for(engine.shutdown(cm), timeout=5)
                except Exception:
                    pass
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise BrowserRuntimeUnavailable(
                "The desktop browser could not be attached "
                f"({type(exc).__name__}: {str(exc)[:200]}). "
                "Do not retry browser actions in this turn."
            ) from exc

    async def ensure_owner(self, owner: str, on_page: Callable[[str, Any], None]) -> tuple[OwnerRuntime, bool]:
        key = owner_key(owner)
        existing = self._runtimes.get(key)
        if existing is not None:
            return existing, False
        lock = self._launch_locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self._runtimes.get(key)
            if existing is not None:
                return existing, False
            engine = self._select_engine(owner)
            if getattr(engine, "embedded", False):
                return await self._ensure_embedded_owner(owner, key, engine, on_page)
            profile = self.profile_dir(owner)
            lock_file = self._acquire_profile_lock(profile)
            cm = None
            liveview_runtime_id = ""
            started = time.monotonic()

            def stage(name: str) -> None:
                log.info("Browser launch %s for %s after %.1fs", name, key, time.monotonic() - started)

            try:
                self._prepare_profile_launch(profile)

                from ascended_browser._app.browser_workspace.fingerprint import load_or_create
                # Load the stable fingerprint before allocating a display: the
                # X screen must agree with the identity Camoufox presents.
                # Off the event loop: a new owner's generation takes ~0.4 s.
                fingerprint = await asyncio.to_thread(load_or_create, profile, os_name="windows")
                screen = getattr(fingerprint, "screen", None)
                width = int(getattr(screen, "width", 1366) or 1366)
                height = int(getattr(screen, "height", 768) or 768)
                # Camoufox deliberately exposes a fingerprint-sized browser
                # window inside the larger spoofed screen. Capture that window,
                # not the unused X desktop around it; maximizing Firefox would
                # change the viewport identity sites see.
                capture_width = max(640, min(width, int(getattr(screen, "outerWidth", width) or width)))
                capture_height = max(480, min(height, int(getattr(screen, "outerHeight", height) or height)))
                display = ""
                xauthority = ""
                audio_sink = ""
                audio_module = 0
                live_runtime = None
                generation = self._generation(profile)
                if self.liveview is not None:
                    try:
                        liveview_runtime_id = f"{key}-{generation}"
                        if get_setting("browser_liveview_audio", True) is True:
                            # pactl is a blocking subprocess; keep it off the
                            # event loop. A missing audio server degrades to
                            # video-only instead of failing the launch.
                            audio_sink, audio_module = await asyncio.to_thread(
                                ensure_liveview_audio_sink, key,
                            )
                        live_runtime = await asyncio.wait_for(
                            self.liveview.create_runtime(
                                **self._capture_display_request(
                                    liveview_runtime_id, generation, width, height,
                                    capture_width, capture_height, audio_sink,
                                )
                            ),
                            timeout=8,
                        )
                        display = live_runtime.display
                        xauthority = live_runtime.auth_file
                        stage("liveview-ready")
                    except Exception as first_failure:
                        # A cold daemon supply (Docker spawn, dependency probe)
                        # can outrun the first bounded attempt. Retake the
                        # daemon's own start() budget once before falling back
                        # to a launch without a capture display: a browser born
                        # virtual can never gain a live view later.
                        display = ""
                        xauthority = ""
                        audio_sink, audio_module = "", 0
                        # A browser launched without a capture display must
                        # never advertise the ID of the failed native runtime.
                        # create_live_view uses an empty ID to enter its
                        # bounded capture-relaunch path instead of minting a
                        # capability whose WebSocket can only close.
                        liveview_runtime_id = ""
                        log.warning(
                            "Live view unavailable for owner %s on first attempt; browser automation continues (%s: %s)",
                            key, type(first_failure).__name__, first_failure,
                        )
                        if self.liveview is not None and await self.liveview.start():
                            try:
                                liveview_runtime_id = f"{key}-{generation}"
                                live_runtime = await asyncio.wait_for(
                                    self.liveview.create_runtime(
                                        **self._capture_display_request(
                                            liveview_runtime_id, generation,
                                            width, height,
                                            capture_width, capture_height, "",
                                        )
                                    ),
                                    timeout=8,
                                )
                                liveview_runtime_id = f"{key}-{generation}"
                                display = live_runtime.display
                                xauthority = live_runtime.auth_file
                                stage("liveview-ready-retry")
                            except Exception:
                                liveview_runtime_id = ""
                                display = ""
                                xauthority = ""
                                audio_sink, audio_module = "", 0
                                log.warning(
                                    "Live view retry failed for owner %s; browser automation continues",
                                    key, exc_info=True,
                                )
                proxy = str(get_setting("browser_work_proxy", "") or "")
                dynamic_viewport = bool(
                    display
                    and get_setting("browser_liveview_dynamic_viewport", True) is True
                    and supports_dynamic_viewport(live_runtime)
                )
                timeout = self._launch_timeout()
                launch_options = self.engine.launch_options(
                    profile=profile, fingerprint=fingerprint, display=display,
                    xauthority=xauthority, proxy=proxy,
                    dynamic_viewport=dynamic_viewport,
                    audio_sink=audio_sink,
                )
                if not display:
                    # The engine removes inherited DISPLAY in this path. A
                    # headed host-mode decision cannot supply an X server to
                    # that child; keep the engine's virtual fallback instead.
                    launch_options["headless"] = self._headless_mode() or launch_options["headless"]
                try:
                    # A dead live-view X socket must not consume the full
                    # launch budget; automation can retry on virtual Xvfb.
                    first_timeout = min(12.0, timeout) if display else timeout
                    cm, context = await self._enter_browser_context(
                        launch_options, timeout=first_timeout,
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    if not display:
                        raise
                    # A dead or wedged live-view X socket makes Firefox wait out
                    # the whole launch budget. Automation does not need that
                    # display; retry on Camoufox's own virtual Xvfb once.
                    log.warning(
                        "Display-backed Camoufox launch timed out for %s after %.1fs; retrying virtual",
                        key, time.monotonic() - started,
                    )
                    await self._abandon_profile_launch(profile, liveview_runtime_id)
                    liveview_runtime_id = ""
                    display = ""
                    xauthority = ""
                    dynamic_viewport = False
                    launch_options = self.engine.launch_options(
                        profile=profile, fingerprint=fingerprint, display="",
                        xauthority="", proxy=proxy, dynamic_viewport=False,
                    )
                    launch_options["headless"] = self._headless_mode() or launch_options["headless"]
                    cm, context = await self._enter_browser_context(launch_options, timeout=timeout)
                stage("browser-ready")
                runtime = OwnerRuntime(
                    key=key,
                    profile_dir=profile,
                    context_manager=cm,
                    context=context,
                    lock_file=lock_file,
                    generation=generation,
                    liveview_runtime_id=liveview_runtime_id,
                    display=display,
                    xauthority=xauthority,
                    audio_sink=audio_sink,
                    audio_module=audio_module,
                    display_width=width,
                    display_height=height,
                    dynamic_viewport=dynamic_viewport,
                    window_width=capture_width if dynamic_viewport else 0,
                    window_height=capture_height if dynamic_viewport else 0,
                    proxy=proxy,
                )
                self._runtimes[key] = runtime
                # uBlock Origin is excluded above, so its job is done here at the
                # network layer: third-party ad/tracker hosts and third-party
                # media never load, which is what keeps tab memory bounded.
                try:
                    from ascended_browser._app.browser_adblock import build_from_settings

                    runtime.adblock = build_from_settings()
                    if runtime.adblock.enabled:
                        await runtime.adblock.attach(context)
                except Exception:
                    log.warning("Could not attach the ad blocker", exc_info=True)
                # Claim the anchor BEFORE any handler is attached, so it never
                # reaches a workspace and never fires a page event.
                runtime.anchor_page = context.pages[0] if context.pages else await self.engine.create_page(context)
                # The anchor keeps Firefox's native top-level alive, but it is
                # deliberately not a workspace tab and must never become the
                # first captured page.  Binding it starts a native capture
                # transaction only for `create_live_view`/`viewport` to bind
                # the actual selected page immediately afterwards.  Those
                # canonical transitions call `prepare_user_control` before
                # exposing a viewer or accepting a resize, so defer the first
                # bind until that visible page is known.  We still retain the
                # measured launch geometry below as the selected page's safe
                # initial outer-size baseline.
                try:
                    observed_geometry = await runtime.anchor_page.evaluate(
                        "() => ({width: innerWidth, height: innerHeight, "
                        "outer_width: outerWidth, outer_height: outerHeight})"
                    )
                    runtime.viewport_width = int(observed_geometry.get("width") or 0)
                    runtime.viewport_height = int(observed_geometry.get("height") or 0)
                    observed_outer_width = int(observed_geometry.get("outer_width") or 0)
                    observed_outer_height = int(observed_geometry.get("outer_height") or 0)
                    runtime.window_width, runtime.window_height = _bounded_liveview_window_size(
                        runtime,
                        width=observed_outer_width or runtime.window_width or runtime.display_width,
                        height=observed_outer_height or runtime.window_height or runtime.display_height,
                    )
                    # ``viewport_*`` remains the DOM observation until the
                    # selected page is bound and measured.  ``launch_*`` is
                    # the immutable restore target, so it must instead be the
                    # same chrome projected onto the bounded native window.
                    # Otherwise a profile restored taller than the fixed X
                    # display makes a later "restore" request impossible.
                    (
                        runtime.launch_viewport_width,
                        runtime.launch_viewport_height,
                    ) = _content_size_for_bound_window(
                        {
                            "width": runtime.viewport_width,
                            "height": runtime.viewport_height,
                            "outer_width": observed_outer_width,
                            "outer_height": observed_outer_height,
                        },
                        window_width=runtime.window_width,
                        window_height=runtime.window_height,
                    )
                    runtime.launch_window_width = runtime.window_width
                    runtime.launch_window_height = runtime.window_height
                except Exception:
                    log.debug("Could not read initial browser geometry for %s", key, exc_info=True)
                context.on("page", lambda page, runtime_key=key: on_page(runtime_key, page))
                context.on("close", lambda _context=None, runtime_key=key: self._disconnected(runtime_key))
                browser = getattr(context, "browser", None)
                if browser is not None:
                    browser.on("disconnected", lambda _browser=None, runtime_key=key: self._disconnected(runtime_key))
                # Persistent launch can return already-created pages (including
                # Firefox session restore); those do not emit a later page event.
                for page in list(context.pages):
                    if page is runtime.anchor_page:
                        continue
                    reconciliation = on_page(key, page)
                    if inspect.isawaitable(reconciliation):
                        await reconciliation
                self._write_heartbeat(runtime)
                return runtime, True
            except BaseException as exc:
                if cm is not None:
                    try:
                        await asyncio.wait_for(self.engine.shutdown(cm), timeout=5)
                    except Exception:
                        pass
                self._release_profile_lock(lock_file)
                if self.liveview is not None and liveview_runtime_id:
                    try:
                        await asyncio.wait_for(self.liveview.delete_runtime(liveview_runtime_id), timeout=3)
                    except Exception:
                        pass
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                # The cause was dropped here, so a failed first launch left no
                # trace in the log or the result (tool driver, 2026-10-05).
                log.warning("Managed browser runtime for %s could not start", key, exc_info=True)
                raise BrowserRuntimeUnavailable(
                    "The managed browser runtime could not start "
                    f"({type(exc).__name__}: {str(exc)[:200]}). "
                    "Do not retry browser actions in this turn."
                ) from exc

    async def resize_user_view(
        self, owner: str, page: Any, width: int, height: int,
    ) -> dict[str, int]:
        """Resize the real Camoufox window and its capture as one operation.

        ``width`` and ``height`` are requested page-content dimensions. Firefox
        owns its chrome decoration, so measure that delta before asking the
        daemon to resize the native outer X11 window and read the result back
        from the page afterwards. The live-view supervisor remains the owner of
        capture reconfiguration; this method never restarts the browser.
        """
        runtime = self.runtime(owner)
        if (
            runtime is None or not runtime.dynamic_viewport
            or not runtime.liveview_runtime_id or self.liveview is None
        ):
            raise BrowserBackendError("Dynamic browser sizing is not enabled for this runtime")
        live_runtime = self.liveview.runtime(runtime.liveview_runtime_id)
        if not supports_dynamic_viewport(live_runtime) or int(
            getattr(live_runtime, "window_id", 0) or 0
        ) <= 0:
            raise BrowserBackendError("The selected browser window is not bound for resizing")

        try:
            before = await _page_geometry(
                page, require_content_origin=supports_content_capture(live_runtime),
            )
        except Exception as exc:
            raise BrowserBackendError("Could not read the current browser geometry") from exc
        chrome_width = max(0, int(before.get("outer_width") or 0) - int(before.get("width") or 0))
        chrome_height = max(0, int(before.get("outer_height") or 0) - int(before.get("height") or 0))
        min_width = max(320, int(get_setting("browser_liveview_min_viewport_width", 320) or 320))
        min_height = max(240, int(get_setting("browser_liveview_min_viewport_height", 240) or 240))
        content_width = max(min_width, min(int(width), max(min_width, runtime.display_width - chrome_width)))
        content_height = max(min_height, min(int(height), max(min_height, runtime.display_height - chrome_height)))
        requested_outer_width = min(runtime.display_width, content_width + chrome_width)
        requested_outer_height = min(runtime.display_height, content_height + chrome_height)

        # A resize request is idempotent only after both sides of the native
        # boundary agree.  In particular, matching ``innerWidth`` alone is not
        # enough: a tab switch, capture recovery, or earlier partial resize can
        # leave liveviewd cropping a different rectangle while Firefox happens
        # to report the desired page dimensions.  Conversely, sending a
        # matching native resize through liveviewd reconfigures the encoder and
        # visibly interrupts a warm stream.  Return the measured geometry only
        # when its requested content size *and* the current explicit capture
        # bounds already agree; retain the normal resize/repair path for every
        # other state.
        expected_capture = _content_rect(before)
        current_page_matches_request = (
            int(before["width"]) == content_width
            and int(before["height"]) == content_height
        )
        if (
            current_page_matches_request
            and supports_content_capture(live_runtime)
            and _runtime_capture_matches(live_runtime, expected_capture)
        ):
            result = {
                "width": int(before["width"]),
                "height": int(before["height"]),
                "outer_width": int(before["outer_width"]),
                "outer_height": int(before["outer_height"]),
            }
            runtime.viewport_width = result["width"]
            runtime.viewport_height = result["height"]
            runtime.window_width = result["outer_width"]
            runtime.window_height = result["outer_height"]
            log.debug(
                "Skipping redundant live-view resize for %s at %dx%d",
                owner_key(owner), result["width"], result["height"],
            )
            return result

        try:
            updated = await self.liveview.resize_runtime(
                runtime.liveview_runtime_id,
                width=requested_outer_width,
                height=requested_outer_height,
            )
        except Exception as exc:
            raise BrowserBackendError(str(exc) or "The live browser could not be resized") from exc

        # Native X11 resize notification and Firefox reflow are asynchronous.
        # Wait for the native size the daemon actually accepted. Firefox and the
        # window manager may clamp a requested outer dimension (for example a
        # narrow desktop window), so waiting for the original content request
        # would consume the whole timeout even though resize already succeeded.
        accepted_outer_width = int(
            updated.get("window_width") or requested_outer_width
        )
        accepted_outer_height = int(
            updated.get("window_height") or requested_outer_height
        )
        accepted_content_width = max(1, int(
            updated.get("capture_width")
            or accepted_outer_width - chrome_width
        ))
        accepted_content_height = max(1, int(
            updated.get("capture_height")
            or accepted_outer_height - chrome_height
        ))
        try:
            await page.wait_for_function(
                "([w,h]) => Math.abs(innerWidth-w) <= 2 && Math.abs(innerHeight-h) <= 2",
                [accepted_content_width, accepted_content_height],
                timeout=1500,
            )
        except Exception:
            pass
        try:
            observed = await _page_geometry(
                page, require_content_origin=supports_content_capture(live_runtime),
            )
        except Exception as exc:
            raise BrowserBackendError(
                "The browser window changed size, but its content geometry "
                "could not be verified; inspect the page before continuing"
            ) from exc
        result = {
            "width": int(observed.get("width") or 0),
            "height": int(observed.get("height") or 0),
            "outer_width": int(observed.get("outer_width") or updated.get("window_width") or 0),
            "outer_height": int(observed.get("outer_height") or updated.get("window_height") or 0),
        }
        if result["width"] <= 0 or result["height"] <= 0:
            raise BrowserBackendError("The browser did not report usable dimensions after resize")
        if supports_content_capture(live_runtime):
            expected_capture = _content_rect(observed)
            accepted_capture = {
                "x": int(updated.get("capture_x") or 0),
                "y": int(updated.get("capture_y") or 0),
                "width": int(updated.get("capture_width") or 0),
                "height": int(updated.get("capture_height") or 0),
            }
            if any(
                abs(accepted_capture[name] - expected_capture[name]) > 2
                for name in expected_capture
            ):
                # Firefox normally keeps chrome insets stable across a native
                # resize. If a theme or minimum-size clamp changes them, apply
                # the observed content rectangle through the same fenced media
                # boundary before reporting the resize complete.
                try:
                    updated = await self.liveview.resize_runtime(
                        runtime.liveview_runtime_id,
                        width=int(updated.get("window_width") or requested_outer_width),
                        height=int(updated.get("window_height") or requested_outer_height),
                        content_rect=expected_capture,
                    )
                except Exception as exc:
                    raise BrowserBackendError(
                        "The browser resized, but its content capture could not be aligned"
                    ) from exc
        runtime.viewport_width = result["width"]
        runtime.viewport_height = result["height"]
        runtime.window_width = result["outer_width"]
        runtime.window_height = result["outer_height"]
        return result

    def _disconnected(self, key: str) -> None:
        runtime = self._runtimes.get(key)
        if runtime is None or runtime.closing:
            return
        self._runtimes.pop(key, None)
        self._release_profile_lock(runtime.lock_file)
        self._on_disconnect(key)

    def _process_ids(self, profile: Path) -> list[int]:
        needle = f"-profile {profile.resolve()}"
        result: list[int] = []
        proc = Path("/proc")
        if not proc.is_dir():
            return result
        for item in proc.iterdir():
            if not item.name.isdigit():
                continue
            try:
                command = (item / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
            except OSError:
                continue
            if needle in command and ("firefox" in command or "camoufox-bin" in command):
                result.append(int(item.name))
        return result

    def _write_heartbeat(self, runtime: OwnerRuntime) -> None:
        runtime.last_heartbeat_at = time.time()
        if runtime.embedded:
            return  # no local browser process or profile directory
        browser_pids = self._process_ids(runtime.profile_dir)
        prefer_browser_as_oom_victim(browser_pids)
        payload = {
            "topology": self.topology,
            "app_pid": os.getpid(),
            "browser_pids": browser_pids,
            "generation": runtime.generation,
            "opened_at": runtime.opened_at,
            "heartbeat_at": runtime.last_heartbeat_at,
        }
        path = runtime.profile_dir / ".odysseus-owner-runtime.json"
        fd, temp_path = tempfile.mkstemp(prefix="runtime-", suffix=".tmp", dir=runtime.profile_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":")); fh.flush(); os.fsync(fh.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path): os.unlink(temp_path)

    def heartbeat(self, owner: str) -> None:
        runtime = self.runtime(owner)
        if runtime is not None:
            self._write_heartbeat(runtime)

    def refresh_oom_priority(self) -> None:
        """Re-apply browser OOM priority between heartbeats.

        Firefox lowers a content process back to its own score whenever tab
        priorities change; left for the 30 s heartbeat, the browser main
        process could become the victim and take every tab with it.
        """
        for runtime in list(self._runtimes.values()):
            prefer_browser_as_oom_victim(self._process_ids(runtime.profile_dir))

    def anchor_alive(self, owner: str) -> bool:
        runtime = self.runtime(owner)
        if runtime is not None and runtime.embedded:
            # No anchor page: the desktop owns the window. Alive while the
            # CDP connection is.
            browser = getattr(runtime.context, "browser", None)
            try:
                return bool(browser is None or browser.is_connected())
            except Exception:
                return False
        anchor = getattr(runtime, "anchor_page", None) if runtime else None
        if anchor is None:
            return False
        try:
            return not anchor.is_closed()
        except Exception:
            return False

    def mark_wedged(self, owner: str) -> None:
        """Firefox stopped navigating; only a relaunch recovers it (see WEDGE_HINT)."""
        runtime = self.runtime(owner)
        if runtime is not None:
            runtime.wedged = True

    def is_wedged(self, owner: str) -> bool:
        runtime = self.runtime(owner)
        return bool(runtime is not None and (runtime.wedged or not self.anchor_alive(owner)))

    async def relaunch_owner(self, owner: str, on_page: Callable[[str, Any], None]) -> OwnerRuntime:
        try:
            await self.close_owner(owner)
        except Exception:
            self._runtimes.pop(owner_key(owner), None)
        runtime, _created = await self.ensure_owner(owner, on_page)
        return runtime

    async def close_owner(self, owner: str) -> None:
        runtime = self._runtimes.pop(owner_key(owner), None)
        if runtime is None:
            return
        runtime.closing = True
        try:
            await self._shutdown_runtime(runtime)
        finally:
            if not runtime.embedded:
                self._write_profile_health(runtime.profile_dir)
            self._release_profile_lock(runtime.lock_file)
            if self.liveview is not None and runtime.liveview_runtime_id:
                await self.liveview.delete_runtime(runtime.liveview_runtime_id)
            await asyncio.to_thread(release_liveview_audio_sink, runtime.audio_module)

    async def close_all(self) -> None:
        for key in list(self._runtimes):
            runtime = self._runtimes.get(key)
            if runtime is None:
                continue
            # close_owner hashes an owner, so close directly by runtime key here.
            runtime.closing = True
            self._runtimes.pop(key, None)
            try:
                await self._shutdown_runtime(runtime)
            finally:
                if not runtime.embedded:
                    self._write_profile_health(runtime.profile_dir)
                self._release_profile_lock(runtime.lock_file)
                if self.liveview is not None and runtime.liveview_runtime_id:
                    await self.liveview.delete_runtime(runtime.liveview_runtime_id)
                await asyncio.to_thread(release_liveview_audio_sink, runtime.audio_module)

    async def _shutdown_runtime(self, runtime: OwnerRuntime) -> None:
        """A hung native close must not prevent an authorized owner recovery."""
        try:
            await asyncio.wait_for(self._runtime_engine(runtime).shutdown(runtime.context_manager), timeout=_RUNTIME_SHUTDOWN_TIMEOUT_SECONDS)
        except TimeoutError:
            log.warning("Browser shutdown timed out; terminating only profile processes: runtime=%s",
                        runtime.key)
            for pid in self._process_ids(runtime.profile_dir):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
            # Let Playwright release its transport after the native process
            # dies, without allowing that cleanup to reintroduce the stall.
            try:
                await asyncio.wait_for(self._runtime_engine(runtime).shutdown(runtime.context_manager), timeout=1)
            except Exception:
                pass

    def keeps_bound_window_on_top(self, owner: str) -> bool:
        """Whether the owner's live-view daemon re-fronts the bound window itself.

        Such a daemon undoes an agent's page raise within one X round trip
        while a viewer watches, so re-activating the selected page after each
        agent operation only queues more work on Juggler's activation chain.
        """
        runtime = self.runtime(owner)
        runtime_id = getattr(runtime, "liveview_runtime_id", "") if runtime is not None else ""
        if self.liveview is None or not runtime_id:
            return False
        live_runtime = self.liveview.runtime(runtime_id)
        return bool((getattr(live_runtime, "capabilities", None) or {}).get("bound_window_on_top"))

    async def activate(self, owner: str, page: Any) -> None:
        runtime = self.runtime(owner)
        if runtime is None:
            raise BrowserBackendError("Owner browser runtime is not active")
        await self._runtime_engine(runtime).activate(page)

    async def prepare_user_control(self, owner: str, page: Any) -> None:
        """Bound page-main-thread work as part of the caller's transition.

        Outer manager deadlines can cancel this sooner. Cleanup must also be
        bounded so a blocked page cannot hold the runtime barrier indefinitely.
        """
        progress = {"stage": "activate"}
        started = time.monotonic()
        try:
            async with asyncio.timeout(CONTROL_PREPARATION_TIMEOUT_SECONDS):
                await self._prepare_user_control(owner, page, progress=progress)
        except BaseException as exc:
            runtime = self.runtime(owner)
            if runtime is not None:
                runtime.bound_liveview_page = None
            if isinstance(exc, (TimeoutError, asyncio.CancelledError)):
                # Name the step that ran out the budget; the caller only
                # reports a generic transition timeout.
                log.warning(
                    "Live-view control preparation stopped at stage=%s after %.2fs (%s)",
                    progress["stage"], time.monotonic() - started, type(exc).__name__,
                )
            raise

    async def _activate_for_control(
        self, runtime: OwnerRuntime, page: Any, progress: dict[str, str],
    ) -> None:
        """Raise the selected tab without letting a wedged activation own the mint.

        The engine's activation rides Juggler's global tab-switch chain. When
        it stalls, the page's content channel is still a separate path: a page
        that already reports itself visible *is* the selected tab, so the
        viewer gets its picture instead of a transition timeout. A hidden page
        re-raises the stall as the timeout the caller already maps to
        ``browser_transition_timeout``.
        """
        progress["stage"] = "activate"
        engine = self._runtime_engine(runtime)
        started = time.monotonic()
        try:
            async with asyncio.timeout(CONTROL_ACTIVATION_TIMEOUT_SECONDS):
                await engine.prepare_user_control(page)
            _mark_presented(page)
            return
        except TimeoutError as stall:
            activation_stall = stall
        live_runtime = (
            self.liveview.runtime(runtime.liveview_runtime_id)
            if self.liveview is not None and getattr(runtime, "liveview_runtime_id", "") else None
        )
        if (getattr(live_runtime, "capabilities", None) or {}).get("bound_window_on_top"):
            # Every page is its own top-level window. The title-marker bind
            # below finds this page's window whatever the stacking, and the
            # daemon keeps the bound window above the others while it has
            # viewers, so the picture does not depend on the stalled raise.
            # (The page cannot say whether it is on screen: Camoufox reports
            # every page as visible and focused.)
            log.warning(
                "Live-view tab activation stalled for %.2fs; binding without it "
                "(the live-view daemon keeps the bound window on top)",
                time.monotonic() - started,
            )
            return
        evaluate = getattr(page, "evaluate", None)
        visible = False
        progress["stage"] = "visibility_probe"
        if callable(evaluate):
            try:
                async with asyncio.timeout(CONTROL_VISIBILITY_PROBE_TIMEOUT_SECONDS):
                    visible = bool(await evaluate(
                        "() => document.visibilityState === 'visible'",
                    ))
            except (TimeoutError, Exception):
                visible = False
        if visible:
            log.warning(
                "Live-view tab activation stalled for %.2fs but the page is already "
                "visible; continuing without it (Juggler tab-switch chain may be wedged)",
                time.monotonic() - started,
            )
            return
        log.warning(
            "Live-view tab activation stalled for %.2fs and the page is not visible; "
            "Firefox's tab-switch chain is not completing (a browser relaunch clears it)",
            time.monotonic() - started,
        )
        raise activation_stall

    async def _prepare_user_control(
        self, owner: str, page: Any, *, progress: dict[str, str] | None = None,
    ) -> None:
        progress = progress if progress is not None else {}
        runtime = self.runtime(owner)
        if runtime is None:
            raise BrowserBackendError("Owner browser runtime is not active")
        await self._activate_for_control(runtime, page, progress)
        progress["stage"] = "check_existing_binding"
        live_runtime = (
            self.liveview.runtime(runtime.liveview_runtime_id)
            if self.liveview is not None and runtime.liveview_runtime_id else None
        )
        content_capture = supports_content_capture(live_runtime)
        if not runtime.dynamic_viewport and not content_capture:
            return
        if runtime.dynamic_viewport and not supports_dynamic_viewport(live_runtime):
            raise BrowserBackendError(
                "Dynamic browser sizing requires selected-window binding support"
            )
        if runtime.bound_liveview_page is page and int(
            getattr(live_runtime, "window_id", 0) or 0
        ) > 0:
            if not content_capture:
                return
            # Page identity alone does not prove the native crop is unchanged:
            # Firefox chrome, window geometry and external user resizing can
            # change while the Playwright page remains the same object.
            current_geometry = await _page_geometry(page, require_content_origin=True)
            expected = _content_rect_for_runtime_window(
                current_geometry,
                window_width=int(getattr(live_runtime, "window_width", 0) or runtime.window_width),
                window_height=int(getattr(live_runtime, "window_height", 0) or runtime.window_height),
            )
            if all(
                int(getattr(live_runtime, f"capture_{name}", -1)) == value
                for name, value in expected.items()
            ):
                return

        runtime.bound_liveview_page = None
        marker = f"odysseus-liveview-{secrets.token_hex(16)}"
        original_title = ""
        marker_installed = False
        geometry: dict[str, int] | None = None
        binding_content_rect: dict[str, int] | None = None
        try:
            # A page/tab activation normally stays inside the same Firefox
            # top-level. Preserve the accepted dynamic geometry in that case:
            # rebinding capture must not expand it to the full display and
            # interrupt a warm encoder just because the selected tab changed.
            requested_binding_width = int(
                runtime.window_width
                or getattr(live_runtime, "window_width", 0)
                or runtime.display_width
            )
            requested_binding_height = int(
                runtime.window_height
                or getattr(live_runtime, "window_height", 0)
                or runtime.display_height
            )
            binding_width, binding_height = _bounded_liveview_window_size(
                runtime, width=requested_binding_width, height=requested_binding_height,
            )
            if (binding_width, binding_height) != (
                requested_binding_width, requested_binding_height,
            ):
                log.info(
                    "Bounding selected Firefox outer window from %dx%d to fixed live display %dx%d",
                    requested_binding_width, requested_binding_height,
                    binding_width, binding_height,
                )
            if content_capture:
                progress["stage"] = "page_geometry"
                geometry = await _page_geometry(page, require_content_origin=True)
                binding_content_rect = _content_rect_for_bound_window(
                    geometry, window_width=binding_width, window_height=binding_height,
                )
            progress["stage"] = "title_marker"
            original_title = str(await page.evaluate(
                "marker => { const prior = document.title; document.title = marker; return prior; }",
                marker,
            ) or "")
            marker_installed = True
            progress["stage"] = "bind_window"
            updated = await self.liveview.bind_runtime_window(
                runtime.liveview_runtime_id, title=marker,
                width=binding_width,
                height=binding_height,
                content_rect=binding_content_rect,
            )
        except Exception as exc:
            log.warning(
                "Live-view window binding rejected (outer=%dx%d, content=%s, requested=%dx%d): %s",
                int((geometry or {}).get("outer_width") or 0),
                int((geometry or {}).get("outer_height") or 0),
                binding_content_rect,
                int(runtime.window_width or getattr(live_runtime, "window_width", 0) or runtime.display_width),
                int(runtime.window_height or getattr(live_runtime, "window_height", 0) or runtime.display_height),
                str(exc) or type(exc).__name__,
            )
            raise BrowserBackendError(
                str(exc) or "Could not bind the selected browser window"
            ) from exc
        finally:
            if marker_installed:
                try:
                    async with asyncio.timeout(TITLE_RESTORE_TIMEOUT_SECONDS):
                        await page.evaluate(
                            "([marker, prior]) => { if (document.title === marker) document.title = prior; }",
                            [marker, original_title],
                        )
                except Exception:
                    log.warning(
                        "Could not restore page title after live-view window binding",
                        exc_info=True,
                    )
        window_id = int(updated.get("window_id") or 0)
        if window_id <= 0:
            raise BrowserBackendError("Live-view daemon did not bind a browser window")
        runtime.bound_liveview_page = page
        _mark_presented(page)
        runtime.window_width = int(updated.get("window_width") or runtime.window_width or 0)
        runtime.window_height = int(updated.get("window_height") or runtime.window_height or 0)
        progress["stage"] = "align_capture"
        if geometry is not None and content_capture:
            # The native fit above can change Firefox's chrome/content insets.
            # Re-observe after reflow, then move capture to the exact viewport
            # through the same geometry CAS before input is admitted.
            try:
                await page.wait_for_function(
                    "([w,h]) => outerWidth <= w && outerHeight <= h",
                    [runtime.display_width, runtime.display_height],
                    timeout=1500,
                )
            except Exception:
                pass
            try:
                geometry = await _page_geometry(page, require_content_origin=True)
                expected_capture = _content_rect_for_runtime_window(
                    geometry,
                    window_width=runtime.window_width,
                    window_height=runtime.window_height,
                )
                accepted_capture = {
                    name: int(updated.get(f"capture_{name}") or 0)
                    for name in ("x", "y", "width", "height")
                }
                # A same-window tab activation commonly keeps the native and
                # content rectangles exact. Avoid a second geometry transaction
                # in that normal case; it otherwise restarts the capture after
                # the selection has already committed.
                if any(
                    abs(accepted_capture[name] - expected_capture[name]) > 2
                    for name in expected_capture
                ):
                    updated = await self.liveview.resize_runtime(
                        runtime.liveview_runtime_id,
                        width=runtime.window_width,
                        height=runtime.window_height,
                        content_rect=expected_capture,
                    )
                if not _capture_alignment_matches(updated, expected_capture):
                    raise BrowserBackendError(
                        "Live-view daemon confirmed content capture outside the selected browser viewport"
                    )
            except Exception as exc:
                runtime.bound_liveview_page = None
                raise BrowserBackendError(
                    "The browser window was bound, but its content capture could not be aligned"
                ) from exc
            runtime.viewport_width = geometry["width"]
            runtime.viewport_height = geometry["height"]
            runtime.window_width = int(updated.get("window_width") or runtime.window_width)
            runtime.window_height = int(updated.get("window_height") or runtime.window_height)

    async def release_user_control(self, owner: str, page: Any) -> None:
        await self._runtime_engine(self.runtime(owner)).release_user_control(page)

    async def create_page(self, owner: str) -> Any:
        runtime = self.runtime(owner)
        if runtime is None:
            raise BrowserBackendError("Owner browser runtime is not active")
        embedded = getattr(runtime, "presentation", "") == "embedded"
        def require_connected_desktop() -> None:
            if not embedded:
                return
            from ascended_browser._app.browser_workspace.engine import desktop_host_connected
            if not desktop_host_connected(owner):
                raise BrowserRuntimeUnavailable(
                    "The Ascended desktop browser disconnected. Open the desktop app "
                    "for this host and retry. Do not retry browser actions in this turn."
                )
        require_connected_desktop()
        try:
            page = await self._runtime_engine(runtime).create_page(runtime.context)
            if getattr(runtime, "bound_liveview_page", None) is None:
                # A new page's window opens on top. With a bound live view the
                # daemon puts the bound window back above it.
                _mark_presented(page)
            return page
        except Exception:
            # Transport failure may coincide with the host leaving after the
            # admission check. Preserve the availability boundary then too.
            require_connected_desktop()
            raise

    async def metadata(self, owner: str, page: Any) -> dict[str, str]:
        return await self._runtime_engine(self.runtime(owner)).metadata(page)

    @staticmethod
    def _profile_size(profile: Path) -> int:
        total = 0
        for root, _dirs, files in os.walk(profile):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
        return total

    def _write_profile_health(self, profile: Path) -> dict[str, Any]:
        size = self._profile_size(profile)
        limit = 2 * 1024 * 1024 * 1024
        payload = {"checked_at": time.time(), "size_bytes": size, "limit_bytes": limit, "over_limit": size > limit}
        path = profile / ".odysseus-profile-health.json"
        fd, temp_path = tempfile.mkstemp(prefix="profile-health-", suffix=".tmp", dir=profile)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":")); fh.flush(); os.fsync(fh.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path): os.unlink(temp_path)
        return payload

    def reset_profile(self, owner: str) -> dict[str, Any]:
        import shutil

        profile = self.profile_dir(owner)
        handle = self.acquire_owner_install_lock(owner)
        backup = profile.with_name(f"{profile.name}.reset-backup-{int(time.time())}")
        try:
            if profile.exists() and any(profile.iterdir()):
                os.replace(profile, backup)
            elif profile.exists():
                profile.rmdir()
            profile.mkdir(mode=0o700, parents=True, exist_ok=False)
            return {"reset": True, "previous_profile_backup": str(backup) if backup.exists() else ""}
        except Exception:
            if not profile.exists() and backup.exists():
                os.replace(backup, profile)
            raise
        finally:
            self.release_owner_install_lock(handle)

    def page_identity(self, owner: str, page: Any) -> dict[str, str]:
        """{guest_key, target_id} for a desktop-embedded page, else {}."""
        runtime = self.runtime(owner)
        if runtime is None or not runtime.embedded:
            return {}
        identity = getattr(runtime.engine, "identity", None)
        return dict(identity(page)) if callable(identity) else {}

    def _engine_capabilities(self, owner: str, runtime: OwnerRuntime | None) -> dict[str, Any]:
        """What the UI renders: the running engine, else the one a launch would use."""
        if runtime is not None:
            return self._runtime_engine(runtime).capabilities
        try:
            return self._select_engine(owner, record=False).capabilities
        except BrowserRuntimeUnavailable as exc:
            return {
                "engine": "electron-chromium", "presentation": "embedded",
                "supported": False, "reason": str(exc),
            }

    def health(self, owner: str) -> dict[str, Any]:
        runtime = self.runtime(owner)
        health_path = self.profile_dir(owner) / ".odysseus-profile-health.json"
        try:
            profile_health = json.loads(health_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            profile_health = {}
        return {
            "topology": self.topology,
            "browser_engine": self._engine_capabilities(owner, runtime),
            "runtime_health": ("wedged" if self.is_wedged(owner) else "active") if runtime else "stopped",
            "profile_generation": runtime.generation if runtime else 0,
            "heartbeat_at": runtime.last_heartbeat_at if runtime else None,
            "profile_size_bytes": int(profile_health.get("size_bytes") or 0),
            "profile_over_size_limit": bool(profile_health.get("over_limit")),
            "adblock": (runtime.adblock.stats() if runtime and runtime.adblock else {"enabled": False}),
            "liveview_runtime_id": runtime.liveview_runtime_id if runtime else "",
            "liveview_display": runtime.display if runtime else "",
        }
