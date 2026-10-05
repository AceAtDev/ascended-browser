"""Lifecycle, capabilities, and authorization state for WebRTC browser live view."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import signal
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

try:  # ``start`` is unavailable on Windows, but imports must remain safe there.
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - Windows import compatibility
    fcntl = None  # type: ignore[assignment]

import httpx
import websockets

from ascended_browser.runtime.constants import DATA_DIR
from ascended_browser.runtime.settings import get_setting

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CONNECT_TTL_SECONDS = 300
RECONNECT_GRACE_SECONDS = 30
# A freshly minted iframe capability needs a short admission window before its
# WebSocket opens. Treating it as a viewer for the full five-minute token TTL
# turns an abandoned mint into a long-lived false selection conflict.
PENDING_CONNECT_GRACE_SECONDS = 10
HEALTH_REUSE_SECONDS = 2.0
HEALTH_PROBE_TIMEOUT_SECONDS = 1.0
# A connect() on a listening unix socket completes locally and at once;
# anything slower is a full backlog, which still means a listener.
SOCKET_PROBE_TIMEOUT_SECONDS = 0.5
# A surviving display can retain an inherited copy of the daemon's stderr.
# After process exit, collect buffered diagnostics without waiting for EOF.
STDERR_DRAIN_TIMEOUT_SECONDS = 0.25
START_TIMEOUT_SECONDS = 10.0
RESTART_FAILURE_COUNT = 3
RESTART_SUSPECT_SECONDS = 1.0
# Linux allows at most 107 pathname bytes for an AF_UNIX socket; other
# supported Unix hosts can be tighter.  Leave headroom rather than discovering
# the limit only after a browser has already launched without a live view.
UNIX_SOCKET_SAFE_PATH_BYTES = 96


def _socket_path_bytes(path: Path) -> int:
    return len(os.fsencode(str(path)))


def _secure_liveview_runtime_dir() -> Path | None:
    """Return a short, user-owned directory for an overlong daemon socket.

    The nominal data-root socket remains the identity when it fits.  Only its
    transport address moves here, deterministically keyed by that canonical
    identity, so independently started app processes still find the same
    daemon without exposing a predictable socket directly in a shared temp
    directory.
    """
    uid = getattr(os, "getuid", lambda: 0)()
    candidate_roots: list[Path] = []
    xdg_runtime = str(os.getenv("XDG_RUNTIME_DIR") or "").strip()
    if xdg_runtime:
        candidate_roots.append(Path(xdg_runtime))
    candidate_roots.append(Path(tempfile.gettempdir()))
    # A custom TMPDIR can itself be too deep for AF_UNIX.  This conventional
    # POSIX fallback is only used through a private uid-scoped subdirectory.
    if Path("/tmp") not in candidate_roots:
        candidate_roots.append(Path("/tmp"))

    directory_name = f"odysseus-liveview-{uid}"
    for root in candidate_roots:
        directory = root / directory_name
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = directory.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                continue
            if metadata.st_uid != uid:
                continue
            # An existing directory may predate this invariant.  It is safe
            # to tighten permissions only after confirming same-user ownership.
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                directory.chmod(0o700)
            return directory
        except OSError:
            continue
    return None


def compact_liveview_socket_path(path: str | Path) -> Path:
    """Keep an AF_UNIX daemon endpoint usable when its data root is deep.

    A long isolated QA root, a deeply nested workspace, or a user-selected
    data directory must not make ordinary browser viewing unavailable.  The
    digest is based on the full canonical requested path, making the compact
    address collision-resistant while retaining stable shared-daemon discovery
    for the same workspace root.
    """
    requested = Path(path)
    if _socket_path_bytes(requested) <= UNIX_SOCKET_SAFE_PATH_BYTES:
        return requested
    runtime_dir = _secure_liveview_runtime_dir()
    if runtime_dir is None:
        # Start will return the daemon's meaningful bind failure instead of
        # changing its state owner or guessing at an unsafe path.
        return requested
    canonical = str(requested.expanduser().resolve(strict=False))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    compact = runtime_dir / f"{digest}.sock"
    return compact if _socket_path_bytes(compact) <= UNIX_SOCKET_SAFE_PATH_BYTES else requested


def configured_ice_servers() -> list[dict[str, Any]]:
    raw: Any = os.getenv("ODYSSEUS_LIVEVIEW_ICE_SERVERS")
    if raw is None:
        raw = get_setting("browser_liveview_ice_servers", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError:
            log.warning("Ignoring malformed ODYSSEUS_LIVEVIEW_ICE_SERVERS")
            return []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        urls = item.get("urls")
        values = [urls] if isinstance(urls, str) else urls if isinstance(urls, list) else []
        values = [
            str(value)[:500] for value in values[:8]
            if str(value).lower().startswith(("stun:", "stuns:", "turn:", "turns:"))
        ]
        if not values:
            continue
        server: dict[str, Any] = {"urls": values[0] if isinstance(urls, str) else values}
        if item.get("username") is not None:
            server["username"] = str(item["username"])[:500]
        if item.get("credential") is not None:
            server["credential"] = str(item["credential"])[:1000]
        result.append(server)
    return result



def _utf8_prefix(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes of `text`, never splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")

class LiveViewUnavailable(RuntimeError):
    """A live-view capability or runtime cannot currently serve a request.

    The browser routes need a stable reason code here.  Parsing an error
    message in the UI would make capability recovery depend on incidental
    wording, and could cause a healthy viewer to be replaced after an
    unrelated tab-lifecycle failure.
    """

    def __init__(self, message: str, *, error_kind: str = "unavailable") -> None:
        super().__init__(message)
        self.error_kind = error_kind


@dataclass
class LiveViewRuntime:
    runtime_id: str
    display: str
    auth_file: str
    width: int
    height: int
    fps: int
    capture_x: int = 0
    capture_y: int = 0
    capture_width: int = 0
    capture_height: int = 0
    output_height: int = 1080
    xvfb_pid: int = 0
    openbox_pid: int = 0
    runtime_generation: int = 0
    geometry_revision: int = 0
    control_revision: int = 0
    media_generation: int = 0
    window_id: int = 0
    window_x: int = 0
    window_y: int = 0
    window_width: int = 0
    window_height: int = 0
    encoded_width: int = 0
    encoded_height: int = 0
    cursor_embedded: bool = True
    # Monitor source the daemon captures browser audio from. Empty means the
    # runtime streams video only.
    audio_source: str = ""
    bitrate_cap: int = 0
    xid_capture: bool = False
    repaint_capture: bool = False
    pipeline_mode: str = "inprocess"
    encoder: str = "auto"
    vp8_profile: str = "current"
    freshness_pacer: bool = True
    pacer_max_age_ms: int = 150
    zero_playout_delay: bool = False
    playout_delay_ms: int | None = None
    playout_delay_min_ms: int = 0
    capabilities: dict[str, bool] = field(default_factory=dict)


@dataclass
class LiveViewSession:
    view_id: str
    owner: str
    session_id: str
    workspace_id: str
    tab_id: str
    runtime_id: str
    runtime_generation: int
    created_at: float = field(default_factory=time.time)
    connect_expires_at: float = field(default_factory=lambda: time.time() + CONNECT_TTL_SECONDS)
    connected_at: float | None = None
    disconnected_at: float | None = None
    connections: int = 0
    input_sequence: int = 0
    closed: bool = False
    pressed_keys: set[str] = field(default_factory=set)
    connection_generation: int = 0
    # Every WebSocket gets a monotonically increasing generation. An older
    # transport can remain open briefly after a reconnect, so a plain count
    # cannot tell whether the authoritative (latest) socket is still present.
    active_connection_generations: set[int] = field(default_factory=set)
    metrics: dict[str, float | str] = field(default_factory=dict)
    metrics_at: float | None = None
    # Selection intent belongs to this ephemeral viewer, never durable tab state.
    # Reserve before waiting for native focus so reordered HTTP requests cannot
    # apply an older tab click after a newer one has already been accepted.
    selection_revision: int = 0
    selection_requested_session_id: str = ""
    selection_requested_tab_id: str = ""
    selection_pending: bool = False
    last_handback: dict[str, Any] | None = None

    def usable(self, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self.closed:
            return False
        if self.connected_at is None:
            return now <= self.connect_expires_at
        return self.disconnected_at is None or now - self.disconnected_at <= RECONNECT_GRACE_SECONDS


class LiveViewRegistry:
    """Short-lived, owner-bound iframe capabilities; never persisted."""

    def __init__(self) -> None:
        self._sessions: dict[str, LiveViewSession] = {}

    def create(
        self, *, owner: str, session_id: str, workspace_id: str, tab_id: str,
        runtime_id: str, runtime_generation: int,
    ) -> LiveViewSession:
        self.prune()
        # Each browser window/device gets an independent capability. A second
        # viewer can finish minting before the first iframe opens its WebSocket;
        # invalidating "unused" capabilities here therefore creates a race in
        # the supported many-spectator journey. Expiry/prune bounds abandoned
        # mints, while controller authority remains fenced separately by the
        # BrowserWorkspaceManager and daemon grant.
        view = LiveViewSession(
            view_id=secrets.token_urlsafe(32), owner=owner, session_id=session_id,
            workspace_id=workspace_id, tab_id=tab_id, runtime_id=runtime_id,
            runtime_generation=runtime_generation,
        )
        self._sessions[view.view_id] = view
        return view

    def get(self, view_id: str, owner: str) -> LiveViewSession:
        view = self._sessions.get(str(view_id or ""))
        if view is None or not secrets.compare_digest(view.owner, owner) or not view.usable():
            raise LiveViewUnavailable(
                "Live-view capability is unknown or expired",
                error_kind="capability_expired",
            )
        return view

    def find(self, view_id: str) -> LiveViewSession | None:
        """Internal lifecycle lookup, including a just-invalidated session."""
        return self._sessions.get(str(view_id or ""))

    def retire(self, view: LiveViewSession) -> None:
        """Make an explicitly superseded spectator unusable immediately."""
        view.closed = True
        view.active_connection_generations.clear()
        view.connections = 0

    def connected(self, view: LiveViewSession) -> int:
        view.connected_at = view.connected_at or time.time()
        view.connection_generation += 1
        view.active_connection_generations.add(view.connection_generation)
        view.connections = len(view.active_connection_generations)
        view.disconnected_at = None
        return view.connection_generation

    def disconnected(
        self, view: LiveViewSession, connection_generation: int | None = None,
    ) -> bool:
        """Forget one socket and report whether the current socket was lost.

        Reconnects supersede older sockets even when TCP/WebSocket teardown for
        the old transport arrives later. Only losing the current generation
        starts reconnect grace; a stale teardown can never disconnect a newer
        controller.
        """
        generation = int(
            view.connection_generation
            if connection_generation is None else connection_generation
        )
        view.active_connection_generations.discard(generation)
        view.connections = len(view.active_connection_generations)
        current_lost = generation == int(view.connection_generation)
        if current_lost:
            view.disconnected_at = time.time()
        return current_lost

    @staticmethod
    def current_connected(view: LiveViewSession) -> bool:
        return (
            not view.closed
            and int(view.connection_generation) in view.active_connection_generations
        )

    @staticmethod
    def pending_connection(
        view: LiveViewSession,
        *,
        now: float | None = None,
    ) -> bool:
        now = time.time() if now is None else now
        return (
            not view.closed
            and view.connected_at is None
            and now - view.created_at <= PENDING_CONNECT_GRACE_SECONDS
        )

    def invalidate_tab(self, workspace_id: str, tab_id: str) -> None:
        for view in self._sessions.values():
            if view.workspace_id == workspace_id and view.tab_id == tab_id:
                view.closed = True

    def invalidate_runtime(self, runtime_id: str) -> None:
        for view in self._sessions.values():
            if view.runtime_id == runtime_id:
                view.closed = True

    def connected_count(self, workspace_id: str) -> int:
        """Current iframe viewers for one workspace.

        A selected tab is not enough: minimized/hidden live views remove their
        iframe and disconnect. Only a real signaling connection pins browser
        compute against soft sleep.
        """
        return sum(
            1 for view in self._sessions.values()
            if view.workspace_id == workspace_id and self.current_connected(view)
        )

    def connected_count_for_tab(self, workspace_id: str, tab_id: str) -> int:
        return sum(
            1 for view in self._sessions.values()
            if (
                view.workspace_id == workspace_id and view.tab_id == tab_id
                and self.current_connected(view)
            )
        )

    def has_usable_view(self, workspace_id: str, tab_id: str) -> bool:
        """Any live, connecting, or grace-period session bound to this tab.

        Connected viewers are the obvious case, but a session that was minted
        and whose iframe has not finished connecting (or that dropped inside
        the reconnect grace window) breaks just as permanently if the tab's
        page is closed underneath it.
        """
        return any(
            view.workspace_id == workspace_id and view.tab_id == tab_id and view.usable()
            for view in self._sessions.values()
        )

    def for_runtime(self, runtime_id: str) -> list[LiveViewSession]:
        self.prune()
        return [
            view for view in self._sessions.values()
            if view.runtime_id == runtime_id and not view.closed
        ]

    def prune(self) -> None:
        self._sessions = {key: value for key, value in self._sessions.items() if value.usable()}


class LiveViewSupervisor:
    """Supervise the private Go daemon and speak its Unix-socket protocol."""

    def __init__(self, *, socket_path: str | Path | None = None, binary: str | None = None) -> None:
        self.requested_socket_path = Path(
            socket_path or (Path(DATA_DIR) / "run" / "camoufox-liveview.sock")
        )
        self.socket_path = compact_liveview_socket_path(self.requested_socket_path)
        if self.socket_path != self.requested_socket_path:
            log.info("Using compact private live-view socket address for a deep data root")
        configured = str(os.getenv("ODYSSEUS_LIVEVIEW_BINARY") or get_setting("browser_liveview_binary", "") or "").strip()
        self.binary = binary or configured or shutil.which("liveviewd") or str(
            Path(__file__).resolve().parents[2] / "custom_libraries" / "camoufox_liveview" / "bin" / "liveviewd"
        )
        self.media_port = int(os.getenv("ODYSSEUS_LIVEVIEW_WEBRTC_PORT") or get_setting("browser_liveview_webrtc_port", 59000) or 59000)
        self.ice_servers = configured_ice_servers()
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail = ""
        self._start_lock = asyncio.Lock()
        self._runtimes: dict[str, LiveViewRuntime] = {}
        # A healthy daemon on our stable data-root socket may outlive an app
        # process. It is a valid shared service, but it is never ours to tear
        # down: this supervisor has no process handle or foreign runtime
        # inventory.
        self._adopted_daemon = False
        self._reason = "not started"
        self._healthy_until = 0.0
        self._health_failures = 0
        self._suspect_since: float | None = None

    @property
    def available(self) -> bool:
        return bool(
            self.socket_path.exists()
            and (
                self._adopted_daemon
                or (self._process and self._process.returncode is None)
            )
        )

    async def start(self) -> bool:
        configured_enabled = str(os.getenv("ODYSSEUS_LIVEVIEW_ENABLED") or "").strip().lower()
        enabled = configured_enabled not in {"0", "false", "no", "off"} if configured_enabled else bool(get_setting("browser_liveview_enabled", True))
        if os.name == "nt" or not enabled:
            self._reason = "live view requires an enabled Linux host"
            return False
        if self.available and time.monotonic() < self._healthy_until:
            return True
        try:
            async with asyncio.timeout(START_TIMEOUT_SECONDS):
                async with self._start_lock:
                    if self.available and time.monotonic() < self._healthy_until:
                        return True
                    lock_fd = await self._acquire_startup_lock()
                    try:
                        return await self._start_with_process_lock()
                    finally:
                        self._release_startup_lock(lock_fd)
        except TimeoutError:
            self._reason = "live-view management did not become ready before the deadline"
            return False

    async def _acquire_startup_lock(self) -> int:
        """Serialize startup across app processes sharing this data root."""
        assert fcntl is not None
        lock_path = Path(f"{self.socket_path}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return lock_fd
                except BlockingIOError:
                    await asyncio.sleep(0.05)
        except BaseException:
            os.close(lock_fd)
            raise

    @staticmethod
    def _release_startup_lock(lock_fd: int) -> None:
        assert fcntl is not None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    async def _start_with_process_lock(self) -> bool:
        owned_daemon = self._process is not None and self._process.returncode is None
        # The daemon is explicitly a shared Pion mux: one healthy daemon can
        # serve many browser runtimes and peers. A second app process using the
        # same data root must not unlink that daemon's socket and race it for
        # the host-wide media port. We can use its protocol, while leaving
        # process/runtime teardown with its actual owner.
        if owned_daemon or self.socket_path.exists():
            if await self._probe_health():
                self._adopted_daemon = not owned_daemon
                return True
            # A refused socket proves the endpoint has no listener at all:
            # there is no media to preserve and no process to strand, whoever
            # started it. Only a daemon that still answers can be the wedged
            # owner of another app process's displays, and that one is never
            # ours to kill -- however often its management probe fails.
            if await self._management_socket_has_listener():
                # A single management timeout does not mean media is dead. Let
                # the caller fail boundedly while later requests confirm it.
                if self._adopted_daemon or (
                    self._health_failures < RESTART_FAILURE_COUNT
                    or time.monotonic() - (
                        self._suspect_since if self._suspect_since is not None else time.monotonic()
                    )
                    < RESTART_SUSPECT_SECONDS
                ):
                    self._reason = "live-view management is temporarily unavailable; existing media is preserved"
                    return False
            else:
                # Adoption described a daemon that answered. It no longer does,
                # so this process is free to start its own replacement.
                self._adopted_daemon = False
        # A wedged daemon must not run its graceful display teardown: the owner
        # browser is still attached to those X sockets. SIGKILL it, then
        # reattach the orphaned displays in the replacement process.
        if self._process is not None and self._process.returncode is None:
            self._process.kill()
            await self._process.wait()
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
        executable = Path(self.binary)
        if not (executable.is_file() and os.access(executable, os.X_OK)):
            found = shutil.which(self.binary)
            if not found:
                self._reason = f"liveviewd not found at {self.binary}"
                return False
            self.binary = found
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._reap_foreign_docker_daemon()
            await self._reap_orphaned_native_daemon()
            # ``liveviewd`` owns reclamation of a proven-stale management
            # socket.  Do not unlink here: another process can have bound the
            # endpoint after our bounded health probe, and removing it would
            # strand that still-live daemon from its displays.
            self._stderr_tail = ""
            self._reason = ""
            self._adopted_daemon = False
            self._process = await asyncio.create_subprocess_exec(
                self.binary, "--socket", str(self.socket_path),
                "--webrtc-port", str(self.media_port),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._stderr_task = asyncio.create_task(self._drain_stderr(self._process))
            for _ in range(80):
                if await self._probe_health():
                    await self._restore_runtimes()
                    self._reason = ""
                    return True
                if self._process.returncode is not None:
                    break
                await asyncio.sleep(0.05)
            if self._process.returncode is None:
                self._process.kill()
                await self._process.wait()
            if self._stderr_task is not None:
                await asyncio.gather(self._stderr_task, return_exceptions=True)
                self._stderr_task = None
            self._reason = self._stderr_tail or self._reason or "liveviewd failed its startup health check"
            return False
        except Exception as exc:
            self._reason = f"{type(exc).__name__}: {exc}"
            return False

    async def _reap_foreign_docker_daemon(self) -> None:
        """Reap only the confirmed-unreachable container for this data root.

        The wrapper uses the same socket-scoped digest. A QA instance must never
        remove another workspace's container merely because both have our UID.
        """
        digest = hashlib.sha256(str(self.socket_path.resolve(strict=False)).encode()).hexdigest()[:12]
        name = f"odysseus-camoufox-liveview-{os.getuid()}-{digest}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass

    def _orphaned_native_daemon_pids(self) -> list[int]:
        """Return same-user native daemons that were launched for this socket.

        This only runs after the management socket has failed its health check.
        Matching the executable name and exact ``--socket`` argument avoids
        touching other live-view roots or unrelated user processes.
        """
        target = str(self.socket_path.resolve(strict=False))
        pids: list[int] = []
        try:
            proc_entries = list(Path("/proc").iterdir())
        except OSError:
            return pids
        for entry in proc_entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                status = (entry / "status").read_text(errors="replace")
                uid_line = next(
                    (line for line in status.splitlines() if line.startswith("Uid:")), "",
                )
                uid_fields = uid_line.split()
                if len(uid_fields) < 2 or int(uid_fields[1]) != os.getuid():
                    continue
                arguments = (entry / "cmdline").read_bytes().split(b"\0")
            except (OSError, ValueError):
                continue
            decoded = [argument.decode(errors="replace") for argument in arguments if argument]
            if not decoded or Path(decoded[0]).name not in {"liveviewd", "liveviewd-native"}:
                continue
            if any(
                argument == "--socket" and index + 1 < len(decoded) and decoded[index + 1] == target
                for index, argument in enumerate(decoded)
            ):
                pids.append(pid)
        return pids

    async def _reap_orphaned_native_daemon(self) -> None:
        """Remove an unreachable native daemon before reserving its media port."""
        for pid in self._orphaned_native_daemon_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except OSError:
                log.warning("Could not stop orphaned liveviewd process %s", pid, exc_info=True)
                continue
            for _ in range(40):
                if not Path(f"/proc/{pid}").exists():
                    break
                await asyncio.sleep(0.05)

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        stream = process.stderr
        if stream is None:
            return

        async def read_tail() -> None:
            while True:
                chunk = await stream.read(1024)
                if not chunk:
                    return
                self._stderr_tail = (self._stderr_tail + chunk.decode(errors="replace"))[-2000:].strip()

        reader = asyncio.create_task(read_tail())
        try:
            # uvloop can leave an extra stderr descriptor in Xvfb. That
            # display intentionally outlives the daemon, so EOF cannot own
            # process-exit detection or block the supervisor's restart path.
            returncode = await process.wait()
            try:
                async with asyncio.timeout(STDERR_DRAIN_TIMEOUT_SECONDS):
                    await asyncio.shield(reader)
            except TimeoutError:
                pass
            if self._process is process:
                log.warning(
                    "liveviewd exited (code %s); last stderr: %s",
                    returncode, self._stderr_tail[-500:] or "(none)",
                )
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    def _client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
        return httpx.AsyncClient(transport=transport, base_url="http://liveviewd", timeout=5)

    async def _healthy(self) -> bool:
        if not self.socket_path.exists():
            return False
        try:
            async with self._client() as client:
                response = await client.get("/healthz")
                payload = response.json()
            compatible = response.status_code == 200 and int(payload.get("protocol_version") or 0) == PROTOCOL_VERSION
            if compatible and payload.get("ok") is not True:
                missing = [name for name, present in (payload.get("dependencies") or {}).items() if not present]
                missing += [f"GStreamer:{name}" for name, present in (payload.get("gstreamer_plugins") or {}).items() if not present]
                self._reason = "missing live-view runtime support: " + ", ".join(missing)
            return compatible and payload.get("ok") is True
        except Exception:
            return False

    async def _management_socket_has_listener(self) -> bool:
        """Is anything still accepting connections on the management socket?

        Only an explicit refusal (or a vanished path) proves the endpoint is
        dead; every other answer fails closed, so an unreadable socket is
        never mistaken for permission to take a live daemon's place.
        """
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)),
                SOCKET_PROBE_TIMEOUT_SECONDS,
            )
        except (ConnectionRefusedError, FileNotFoundError, NotADirectoryError):
            return False
        except (OSError, TimeoutError):
            return True
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    async def _probe_health(self) -> bool:
        try:
            healthy = await asyncio.wait_for(self._healthy(), HEALTH_PROBE_TIMEOUT_SECONDS)
        except TimeoutError:
            healthy = False
        now = time.monotonic()
        if healthy:
            self._healthy_until = now + HEALTH_REUSE_SECONDS
            self._health_failures = 0
            self._suspect_since = None
            self._reason = ""
        else:
            self._healthy_until = 0.0
            self._health_failures += 1
            if self._suspect_since is None:
                self._suspect_since = now
        return healthy

    async def health(self) -> dict[str, Any]:
        healthy = await self._healthy()
        payload: dict[str, Any] = {
            "available": healthy,
            "reason": "" if healthy else self._reason,
            "socket": str(self.socket_path),
            "webrtc_port": self.media_port,
            "engine": "camoufox",
            "future_engines": {"chromium": False},
            "advertise_ip": str(os.getenv("ODYSSEUS_LIVEVIEW_ADVERTISE_IP") or ""),
        }
        if healthy:
            try:
                async with self._client() as client:
                    payload.update((await client.get("/healthz")).json())
            except Exception:
                pass
        return payload

    async def create_runtime(
        self, runtime_id: str, width: int, height: int, *, fps: int = 25,
        capture_x: int = 0, capture_y: int = 0,
        capture_width: int = 0, capture_height: int = 0, output_height: int = 1080,
        runtime_generation: int = 0,
        local_cursor: bool = False,
        audio_source: str = "",
        bitrate_cap: int = 0,
        xid_capture: bool = False,
        repaint_capture: bool = False,
        pipeline_mode: str = "inprocess",
        encoder: str = "auto",
        vp8_profile: str = "current",
        freshness_pacer: bool = True,
        pacer_max_age_ms: int = 150,
        zero_playout_delay: bool = False,
        playout_delay_ms: int | None = None,
        playout_delay_min_ms: int = 0,
    ) -> LiveViewRuntime:
        cached = self._runtimes.get(runtime_id)
        if cached is not None and await self.start() and runtime_id in self._runtimes:
            return self._runtimes[runtime_id]
        if not await self.start():
            raise LiveViewUnavailable(self._reason)
        async with self._client() as client:
            runtime_request = {
                "runtime_id": runtime_id, "width": width, "height": height, "fps": fps,
                "capture_x": capture_x, "capture_y": capture_y,
                "capture_width": capture_width, "capture_height": capture_height,
                "output_height": output_height,
                "runtime_generation": runtime_generation,
                "local_cursor": bool(local_cursor),
                "audio_source": str(audio_source or ""),
                "bitrate_cap": max(0, int(bitrate_cap or 0)),
                "xid_capture": bool(xid_capture),
                "repaint_capture": bool(repaint_capture),
                "pipeline_mode": str(pipeline_mode or "inprocess"),
                "encoder": str(encoder or "auto"),
                "vp8_profile": str(vp8_profile or "current"),
                "freshness_pacer": bool(freshness_pacer),
                "pacer_max_age_ms": max(50, min(2000, int(pacer_max_age_ms or 150))),
                "zero_playout_delay": bool(zero_playout_delay),
            }
            if playout_delay_ms is not None:
                runtime_request["playout_delay_ms"] = max(0, min(40950, int(playout_delay_ms)))
                if playout_delay_min_ms:
                    runtime_request["playout_delay_min_ms"] = max(0, min(40950, int(playout_delay_min_ms)))
            response = await client.post("/v1/runtimes", json=runtime_request)
        if response.status_code not in {200, 201}:
            try: detail = str(response.json().get("error") or response.text)
            except Exception: detail = response.text
            raise LiveViewUnavailable(detail or "liveviewd rejected the display")
        data = response.json()
        runtime = LiveViewRuntime(
            runtime_id=str(data["runtime_id"]), display=str(data["display"]),
            auth_file=str(data["auth_file"]),
            width=int(data["width"]), height=int(data["height"]), fps=int(data["fps"]),
            capture_x=int(data.get("capture_x") or 0), capture_y=int(data.get("capture_y") or 0),
            capture_width=int(data.get("capture_width") or data["width"]),
            capture_height=int(data.get("capture_height") or data["height"]),
            output_height=int(data.get("output_height") or 1080),
            xvfb_pid=int(data.get("xvfb_pid") or 0), openbox_pid=int(data.get("openbox_pid") or 0),
            runtime_generation=int(data.get("runtime_generation") or runtime_generation),
            geometry_revision=int(data.get("geometry_revision") or 0),
            control_revision=int(data.get("control_revision") or 0),
            media_generation=int(data.get("media_generation") or 0),
            window_id=int(data.get("window_id") or 0),
            window_x=int(data.get("window_x") or 0), window_y=int(data.get("window_y") or 0),
            window_width=int(data.get("window_width") or data.get("capture_width") or capture_width or width),
            window_height=int(data.get("window_height") or data.get("capture_height") or capture_height or height),
            encoded_width=int(data.get("encoded_width") or 0),
            encoded_height=int(data.get("encoded_height") or 0),
            cursor_embedded=bool(data.get("cursor_embedded", True)),
            audio_source=str(audio_source or ""),
            bitrate_cap=max(0, int(bitrate_cap or 0)),
            xid_capture=bool(xid_capture),
            repaint_capture=bool(repaint_capture),
            pipeline_mode=str(pipeline_mode or "inprocess"),
            encoder=str(encoder or "auto"),
            vp8_profile=str(vp8_profile or "current"),
            freshness_pacer=bool(freshness_pacer),
            pacer_max_age_ms=max(50, min(2000, int(pacer_max_age_ms or 150))),
            zero_playout_delay=bool(zero_playout_delay),
            playout_delay_ms=(
                max(0, min(40950, int(playout_delay_ms)))
                if playout_delay_ms is not None else None
            ),
            playout_delay_min_ms=(
                max(0, min(40950, int(playout_delay_min_ms or 0)))
                if playout_delay_ms is not None else 0
            ),
            capabilities={
                str(key): bool(value)
                for key, value in (data.get("capabilities") or {}).items()
            } if isinstance(data.get("capabilities"), dict) else {},
        )
        self._runtimes[runtime_id] = runtime
        return runtime

    def runtime(self, runtime_id: str) -> LiveViewRuntime | None:
        return self._runtimes.get(str(runtime_id or ""))

    @staticmethod
    def _apply_runtime_payload(runtime: LiveViewRuntime, data: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "capture_x", "capture_y", "capture_width", "capture_height",
            "output_height", "runtime_generation", "geometry_revision", "control_revision",
            "media_generation", "window_id", "window_x", "window_y",
            "window_width", "window_height", "encoded_width", "encoded_height",
        )
        for name in fields:
            if data.get(name) is None:
                continue
            try:
                setattr(runtime, name, int(data[name]))
            except (TypeError, ValueError):
                continue
        if isinstance(data.get("capabilities"), dict):
            runtime.capabilities = {
                str(key): bool(value) for key, value in data["capabilities"].items()
            }
        if data.get("cursor_embedded") is not None:
            runtime.cursor_embedded = bool(data["cursor_embedded"])
        return data

    async def update_runtime(
        self,
        runtime_id: str,
        *,
        geometry: dict[str, Any] | None = None,
        control: dict[str, Any] | None = None,
        expected_geometry_revision: int | None = None,
    ) -> dict[str, Any]:
        runtime = self.runtime(runtime_id)
        if runtime is None:
            raise LiveViewUnavailable("Unknown live-view runtime")
        if not await self.start():
            raise LiveViewUnavailable(self._reason)
        payload: dict[str, Any] = {
            "runtime_generation": runtime.runtime_generation,
        }
        if expected_geometry_revision is not None:
            payload["expected_geometry_revision"] = int(expected_geometry_revision)
        if geometry is not None:
            payload["geometry"] = geometry
        if control is not None:
            payload["control"] = control
        async with self._client() as client:
            response = await client.patch(
                f"/v1/runtimes/{quote(runtime_id, safe='')}", json=payload,
            )
        try:
            data = response.json()
        except Exception:
            data = {}
        if response.status_code not in {200, 201}:
            detail = str(data.get("error") or response.text or "liveviewd rejected the update")
            if response.status_code == 409:
                current = data.get("current")
                if isinstance(current, dict):
                    current_generation = int(current.get("runtime_generation") or 0)
                    if current_generation == runtime.runtime_generation:
                        self._apply_runtime_payload(runtime, current)
                    elif current_generation:
                        # Never adopt state from a different browser/display
                        # incarnation under the same id. Existing views and
                        # positional input must fail closed until the owner
                        # runtime is reconciled.
                        self._runtimes.pop(runtime_id, None)
                        self._reason = "live-view runtime generation changed"
            raise LiveViewUnavailable(detail)
        return self._apply_runtime_payload(runtime, data)

    async def resize_runtime(
        self, runtime_id: str, *, width: int, height: int,
        content_rect: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        runtime = self.runtime(runtime_id)
        if runtime is None:
            raise LiveViewUnavailable("Unknown live-view runtime")
        geometry: dict[str, Any] = {
            "window_id": int(runtime.window_id),
            "window_width": int(width),
            "window_height": int(height),
        }
        if content_rect is not None:
            geometry["content_rect"] = {
                name: int(content_rect.get(name, 0) or 0)
                for name in ("x", "y", "width", "height")
            }
        return await self.update_runtime(
            runtime_id,
            expected_geometry_revision=runtime.geometry_revision,
            geometry=geometry,
        )

    async def bind_runtime_window(
        self, runtime_id: str, *, title: str,
        width: int, height: int,
        content_rect: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Bind capture to the selected browser top-level identified by title."""
        runtime = self.runtime(runtime_id)
        if runtime is None:
            raise LiveViewUnavailable("Unknown live-view runtime")
        geometry: dict[str, Any] = {
            "bind_window_title": str(title),
            "window_width": int(width),
            "window_height": int(height),
        }
        if content_rect is not None:
            geometry["content_rect"] = {
                name: int(content_rect.get(name, 0) or 0)
                for name in ("x", "y", "width", "height")
            }
        return await self.update_runtime(
            runtime_id,
            expected_geometry_revision=runtime.geometry_revision,
            geometry=geometry,
        )

    async def _restore_runtimes(self) -> None:
        """Re-register displays orphaned by an abrupt daemon exit."""
        if not self._runtimes:
            return
        stale: list[str] = []
        async with self._client() as client:
            for runtime_id, runtime in list(self._runtimes.items()):
                try:
                    payload = {
                        "runtime_id": runtime_id, "width": runtime.width, "height": runtime.height,
                        "fps": runtime.fps, "display": runtime.display,
                        "capture_x": runtime.capture_x, "capture_y": runtime.capture_y,
                        "capture_width": runtime.capture_width,
                        "capture_height": runtime.capture_height,
                        "output_height": runtime.output_height,
                        "runtime_generation": runtime.runtime_generation,
                        "control_revision": runtime.control_revision,
                        "local_cursor": not runtime.cursor_embedded,
                        "auth_file": runtime.auth_file,
                        "xvfb_pid": runtime.xvfb_pid, "openbox_pid": runtime.openbox_pid,
                        "audio_source": runtime.audio_source,
                        "bitrate_cap": runtime.bitrate_cap,
                        "xid_capture": runtime.xid_capture,
                        "repaint_capture": runtime.repaint_capture,
                        "pipeline_mode": runtime.pipeline_mode,
                        "encoder": runtime.encoder,
                        "vp8_profile": runtime.vp8_profile,
                        "freshness_pacer": runtime.freshness_pacer,
                        "pacer_max_age_ms": runtime.pacer_max_age_ms,
                        "zero_playout_delay": runtime.zero_playout_delay,
                    }
                    if runtime.playout_delay_ms is not None:
                        payload["playout_delay_ms"] = runtime.playout_delay_ms
                        if runtime.playout_delay_min_ms:
                            payload["playout_delay_min_ms"] = runtime.playout_delay_min_ms
                    # A daemon restart must not reset the coordinate CAS while
                    # the browser/runtime generation and existing viewers stay
                    # alive. New daemons accept this restore-only snapshot;
                    # older daemons ignore the extra JSON fields and the
                    # response below still becomes our authoritative state.
                    if (
                        runtime.geometry_revision > 0
                        and runtime.window_width > 0
                        and runtime.window_height > 0
                    ):
                        payload.update({
                            "geometry_revision": runtime.geometry_revision,
                            "media_generation": runtime.media_generation,
                            "window_id": runtime.window_id,
                            "window_x": runtime.window_x,
                            "window_y": runtime.window_y,
                            "window_width": runtime.window_width,
                            "window_height": runtime.window_height,
                        })
                    response = await client.post("/v1/runtimes", json=payload)
                    if response.status_code not in {200, 201}:
                        stale.append(runtime_id)
                        continue
                    try:
                        data = response.json()
                    except Exception:
                        data = {}
                    if not isinstance(data, dict):
                        stale.append(runtime_id)
                        continue
                    restored_generation = int(data.get("runtime_generation") or 0)
                    if restored_generation and restored_generation != runtime.runtime_generation:
                        stale.append(runtime_id)
                        continue
                    self._apply_runtime_payload(runtime, data)
                except Exception:
                    stale.append(runtime_id)
        for runtime_id in stale:
            runtime = self._runtimes.pop(runtime_id, None)
            if runtime is not None:
                self._terminate_orphan(runtime)

    @staticmethod
    def _terminate_orphan(runtime: LiveViewRuntime) -> None:
        expected = ((runtime.openbox_pid, "openbox"), (runtime.xvfb_pid, "Xvfb"))
        for pid, marker in expected:
            if pid <= 1:
                continue
            try:
                command = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
                if marker in command:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass
        try:
            Path(runtime.auth_file).unlink(missing_ok=True)
        except OSError:
            pass

    def has_runtime(self, runtime_id: str) -> bool:
        return runtime_id in self._runtimes

    async def agent_cursor(
        self,
        runtime_id: str,
        *,
        kind: str,
        x: float,
        y: float,
        visible: bool = True,
        label: str = "",
        tone: int = 0,
        detail: str = "",
    ) -> bool:
        """Publish one agent tool-pointer event as cursor metadata.

        Spectator decoration only: this never injects input, never decides
        authority, and any failure here simply means the cursor is not drawn.
        `detail` is the action caption. A daemon without the
        `agent_cursor_actions` capability knows only the four pointer kinds
        and drops unknown fields, so for it a typing pulse is sent as a move,
        narration with no position is skipped, and the caption takes the
        (24-byte) label so the watcher still reads what is happening; the
        tone keeps a sub-agent's pointer distinct.
        """
        if not await self.start() or runtime_id not in self._runtimes:
            return False
        runtime = self._runtimes.get(runtime_id)
        capabilities = getattr(runtime, "capabilities", None) or {}
        payload = {
            "kind": str(kind or ""), "x": float(x), "y": float(y),
            "visible": bool(visible), "label": str(label or ""),
            "tone": int(tone),
        }
        if capabilities.get("agent_cursor_actions"):
            if detail:
                payload["detail"] = str(detail)
        else:
            if payload["kind"] == "status":
                return False
            if payload["kind"] == "key":
                payload["kind"] = "pointer_move"
            if detail:
                # The caption says what is happening; who is acting is still
                # carried by the tone (a sub-agent's pointer has its own).
                payload["label"] = _utf8_prefix(str(detail), 24)
        try:
            async with self._client() as client:
                response = await client.post(
                    f"/v1/runtimes/{quote(runtime_id, safe='')}/agent-cursor",
                    json=payload,
                )
            return response.status_code == 204
        except Exception:
            log.debug("agent cursor publication failed for %s", runtime_id, exc_info=True)
            return False

    async def delete_runtime(self, runtime_id: str) -> None:
        self._runtimes.pop(runtime_id, None)
        if not self.socket_path.exists():
            return
        try:
            async with self._client() as client:
                await client.delete(f"/v1/runtimes/{quote(runtime_id, safe='')}")
        except Exception:
            log.debug("Could not delete live-view runtime %s", runtime_id, exc_info=True)

    @asynccontextmanager
    async def peer_connection(
        self,
        runtime_id: str,
        peer_id: str,
        advertise_ip: str = "",
        *,
        controller_id: str = "",
        connection_generation: int = 0,
        codecs: str = "",
        native_codecs: str = "",
        resume_peer_session_id: str = "",
    ):
        if not await self.start() or runtime_id not in self._runtimes:
            raise LiveViewUnavailable(self._reason or "live-view runtime is unavailable")
        def supported_codecs(raw: str) -> str:
            values = []
            for value in str(raw or "").lower().split(","):
                value = value.strip()
                if value in {"vp8", "h264"} and value not in values:
                    values.append(value)
            return ",".join(values)

        query_values: dict[str, str] = {}
        if advertise_ip:
            query_values["advertise_ip"] = advertise_ip
        worker_codecs = supported_codecs(codecs)
        native_video_codecs = supported_codecs(native_codecs)
        if worker_codecs:
            query_values["codecs"] = worker_codecs
        if native_video_codecs:
            query_values["native_codecs"] = native_video_codecs
        # This is deliberately an opaque, bounded reconnect hint.  It never
        # confers authority (the manager-issued controller/generation headers
        # still fence every effect), but lets a capable daemon retain a healthy
        # media peer across a brief signaling reconnect.  Older daemons safely
        # ignore the additive query parameter.
        resume_peer = str(resume_peer_session_id or "")
        if resume_peer:
            query_values["resume_peer_session_id"] = resume_peer[:256]
        query = f"?{urlencode(query_values)}" if query_values else ""
        uri = f"ws://liveviewd/v1/runtimes/{quote(runtime_id, safe='')}/peers/{quote(peer_id, safe='')}{query}"
        async with websockets.unix_connect(
            str(self.socket_path), uri=uri, max_size=1_000_000, open_timeout=5,
            # The browser-facing socket already owns reconnect grace. Letting
            # the private daemon socket spend its default multi-second close
            # handshake keeps the old Pion ICE agent registered in the shared
            # mux and delays its replacement by roughly five seconds.
            close_timeout=0.25,
            additional_headers={
                "X-Liveview-Ice-Servers": json.dumps(self.ice_servers, separators=(",", ":")),
                # The public client cannot choose these bindings. liveviewd
                # uses them to source-bind its direct RTC input channel before
                # revalidating the complete manager-issued grant at effect time.
                "X-Liveview-Controller-ID": str(controller_id or "")[:256],
                "X-Liveview-Connection-Generation": str(max(0, int(connection_generation or 0))),
            },
        ) as connection:
            yield connection

    async def stop(self) -> None:
        self._runtimes.clear()
        self._healthy_until = 0.0
        self._health_failures = 0
        self._suspect_since = None
        process, self._process = self._process, None
        owns_live_child = process is not None and process.returncode is None
        self._adopted_daemon = False
        # Capture the endpoint before stopping our child.  If another daemon
        # replaces it while shutdown is in progress, its device/inode differs
        # and this supervisor must leave that replacement reachable.
        owned_socket_identity = self._socket_identity() if owns_live_child else None
        if owns_live_child:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
        # A no-process supervisor may have merely observed the shared endpoint;
        # it has no authority to remove it.  A child we own normally unlinks
        # its listener on graceful close, but clean up only the exact endpoint
        # that was present when that child was stopped.  This avoids unlinking
        # a replacement daemon which won the startup race.
        if (
            owns_live_child
            and owned_socket_identity is not None
            and owned_socket_identity == self._socket_identity()
        ):
            self.socket_path.unlink(missing_ok=True)

    def _socket_identity(self) -> tuple[int, int] | None:
        """Return the stable identity of a socket path without following links."""
        try:
            metadata = self.socket_path.lstat()
        except OSError:
            return None
        if not stat.S_ISSOCK(metadata.st_mode):
            return None
        return metadata.st_dev, metadata.st_ino


def liveview_audio_sink_name(owner_key: str) -> str:
    """Deterministic private sink name for one owner's browser runtime."""
    sanitized = "".join(
        character if character.isalnum() or character == "-" else "-"
        for character in str(owner_key or "")[:32].lower()
    ).strip("-")
    return f"odysseus-liveview-{sanitized or 'default'}"


def _pactl(*arguments: str) -> subprocess.CompletedProcess[str] | None:
    if shutil.which("pactl") is None:
        return None
    try:
        return subprocess.run(
            ["pactl", *arguments], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def ensure_liveview_audio_sink(owner_key: str) -> tuple[str, int]:
    """Create (or reuse) the private null sink that receives browser audio.

    The browser is launched with ``PULSE_SINK`` pointed at this sink, so its
    mixed output never reaches the machine's real output devices; the daemon
    captures the sink's monitor for the WebRTC audio track. Returns the sink
    name and the owning module index (0 when reusing an existing sink or when
    audio is unavailable — both degrade to video-only, never to speakers).
    """
    sink = liveview_audio_sink_name(owner_key)
    listed = _pactl("list", "sinks", "short")
    if listed is None or listed.returncode != 0:
        return "", 0
    for line in listed.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) > 1 and fields[1] == sink:
            return sink, 0
    loaded = _pactl(
        "load-module", "module-null-sink",
        f"sink_name={sink}",
        "sink_properties=device.description=Ascended.LiveView",
    )
    if loaded is None or loaded.returncode != 0:
        log.warning("Could not create the live-view audio sink %s: %s",
                    sink, (loaded.stderr if loaded is not None else "") or "unavailable")
        return "", 0
    try:
        return sink, int(loaded.stdout.strip())
    except ValueError:
        return sink, 0


def release_liveview_audio_sink(module_index: int) -> None:
    if not module_index:
        return
    _pactl("unload-module", str(module_index))


def advertised_ip(host_header: str) -> str:
    """Resolve a safe ICE address, preferring explicit/Tailscale request hosts."""
    explicit = str(os.getenv("ODYSSEUS_LIVEVIEW_ADVERTISE_IP") or "").strip()
    if explicit:
        try:
            return str(ipaddress.ip_address(explicit))
        except ValueError:
            log.warning("Ignoring invalid ODYSSEUS_LIVEVIEW_ADVERTISE_IP")
    allowed_hosts = {socket.gethostname().lower(), socket.getfqdn().lower()}
    for origin in str(os.getenv("ALLOWED_ORIGINS") or "").split(","):
        try:
            if origin.strip():
                allowed_hosts.add(str(urlparse(origin.strip()).hostname or "").lower())
        except Exception:
            pass
    host = str(host_header or "").strip()
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    if host:
        try:
            address = ipaddress.ip_address(host)
            tailscale = address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10")
            if host.lower() in allowed_hosts or address.is_private or address.is_loopback or address.is_link_local or tailscale:
                return str(address)
        except ValueError:
            trusted_name = host.lower() in allowed_hosts or host.lower().endswith(".ts.net")
            if trusted_name and len(host) <= 253 and all(part and part.replace("-", "").isalnum() for part in host.split(".")):
                try:
                    for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
                        address = ipaddress.ip_address(info[4][0])
                        tailscale = address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10")
                        if not address.is_unspecified and (
                            host.lower() in allowed_hosts or address.is_private or address.is_loopback
                            or address.is_link_local or tailscale
                        ):
                            return str(address)
                except OSError:
                    pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("1.1.1.1", 53))
        value = probe.getsockname()[0]
        probe.close()
        return str(ipaddress.ip_address(value))
    except Exception:
        return ""
