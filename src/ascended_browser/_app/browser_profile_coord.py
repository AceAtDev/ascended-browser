"""Coordinate Camoufox/Firefox profile cloning and ephemeral sessions.

Master profile (data/browser_work/persistent-profile) is read-only for automation:
users sign in manually, then Browser Work / Agent MCP clone it into per-task
directories under data/browser_work/camoufox-sessions/.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List

from ascended_browser.runtime.platform import try_lock_exclusive, unlock as unlock_file

logger = logging.getLogger(__name__)

_LOCK_NAME = ".odysseus-profile.lock"
_SESSIONS_DIR_NAME = "camoufox-sessions"
_ORPHANED_AGENT_MCP_PROFILE_MIN_AGE_SECONDS = 60 * 60

# Auth-relevant paths copied from master into ephemeral clones.
_PROFILE_CLONE_FILES = (
    "cookies.sqlite",
    "cookies.sqlite-wal",
    "cookies.sqlite-shm",
    "key4.db",
    "cert9.db",
    "storage.sqlite",
    "storage.sqlite-wal",
    "storage.sqlite-shm",
    "webappsstore.sqlite",
    "webappsstore.sqlite-wal",
    "webappsstore.sqlite-shm",
    "prefs.js",
    "xulstore.json",
    "compatibility.ini",
    "containers.json",
    "extension-preferences.json",
    "extension-settings.json",
    "extensions.json",
    "handlers.json",
    "permissions.sqlite",
    "formhistory.sqlite",
    "places.sqlite",
    "places.sqlite-wal",
    "favicons.sqlite",
    "favicons.sqlite-wal",
)

_PROFILE_CLONE_GLOBS = ("sessionstore*",)
_PROFILE_CLONE_DIRS = (
    "storage",
)
_PROFILE_EXCLUDE_NAMES = frozenset({
    "lock",
    ".parentlock",
    _LOCK_NAME,
    "cache2",
    "startupCache",
})


def valid_profile_directory_setting(value: str) -> bool:
    """Reject commands/URLs accidentally pasted into a profile path field."""
    raw = str(value or "").strip()
    if not raw:
        return True
    lowered = raw.lower()
    if any(token in raw for token in ("\n", "\r", "&&", "||", ";", "|", "`", "$(")):
        return False
    if lowered.startswith(("http://", "https://", "cd ", "python ", "python3 ", "bash ", "sh ")):
        return False
    return True


def master_profile_dir() -> str:
    """Canonical read-only sign-in profile directory."""
    return shared_persistent_profile_dir()


def shared_persistent_profile_dir() -> str:
    from ascended_browser.runtime.constants import DATA_DIR
    from ascended_browser.runtime.settings import get_setting

    custom = str(get_setting("browser_work_user_data_dir", "") or "").strip()
    if custom and valid_profile_directory_setting(custom):
        return os.path.abspath(os.path.expanduser(custom))
    if custom:
        logger.warning("Ignoring invalid Browser Work profile directory setting; using the canonical persistent profile")
    return os.path.abspath(os.path.join(DATA_DIR, "browser_work", "persistent-profile"))


def ephemeral_sessions_root() -> str:
    from ascended_browser.runtime.constants import DATA_DIR

    return os.path.abspath(os.path.join(DATA_DIR, "browser_work", _SESSIONS_DIR_NAME))


def auth_storage_state_path() -> str:
    from ascended_browser.runtime.constants import DATA_DIR

    root = os.path.abspath(os.path.join(DATA_DIR, "browser_work"))
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, "auth-state.json")


async def save_playwright_storage_state(page) -> dict:
    """Persist Playwright storage_state as a sidecar for session-cookie restore."""
    context = getattr(page, "context", None)
    if context is None or not hasattr(context, "storage_state"):
        return {"saved": False, "path": auth_storage_state_path(), "reason": "no browser context"}
    path = auth_storage_state_path()
    state = await context.storage_state(path=path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return {
        "saved": True,
        "path": path,
        "cookies": len(state.get("cookies") or []),
        "origins": len(state.get("origins") or []),
    }


async def apply_playwright_storage_state(context) -> dict:
    """Load saved cookies/localStorage into an already-created browser context."""
    path = auth_storage_state_path()
    if not os.path.isfile(path):
        return {"applied": False, "path": path, "reason": "missing"}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception as exc:
        return {"applied": False, "path": path, "reason": str(exc)}

    cookies = state.get("cookies") if isinstance(state, dict) else []
    if cookies and hasattr(context, "add_cookies"):
        await context.add_cookies(cookies)

    origins = state.get("origins") if isinstance(state, dict) else []
    local_storage_by_origin = {}
    if isinstance(origins, list):
        for origin in origins:
            if not isinstance(origin, dict):
                continue
            origin_url = str(origin.get("origin") or "")
            entries = origin.get("localStorage") or []
            if origin_url and isinstance(entries, list):
                local_storage_by_origin[origin_url] = [
                    {"name": str(item.get("name") or ""), "value": str(item.get("value") or "")}
                    for item in entries
                    if isinstance(item, dict) and item.get("name") is not None
                ]
    if local_storage_by_origin and hasattr(context, "add_init_script"):
        payload = json.dumps(local_storage_by_origin, ensure_ascii=False)
        script = (
            "(() => {"
            f"const s = {payload};"
            "const entries = s[location.origin];"
            "if (!entries || !window.localStorage) return;"
            "for (const item of entries) { try { localStorage.setItem(item.name, item.value); } catch (e) {} }"
            "})()"
        )
        await context.add_init_script(script)

    return {
        "applied": True,
        "path": path,
        "cookies": len(cookies or []),
        "origins": len(local_storage_by_origin),
    }


def agent_should_share_browser_work_cookies() -> bool:
    from ascended_browser.runtime.settings import get_setting

    if not bool(get_setting("browser_mcp_share_browser_work_cookies", True)):
        return False
    if not bool(get_setting("browser_work_use_persistent_profile", False)):
        return False
    if str(get_setting("browser_mcp_user_data_dir", "") or "").strip():
        return False
    return True


def resolve_agent_camoufox_profile_dir() -> str:
    from ascended_browser.runtime.constants import DATA_DIR
    from ascended_browser.runtime.settings import get_setting

    custom = str(get_setting("browser_mcp_user_data_dir", "") or "").strip()
    if custom:
        return os.path.abspath(os.path.expanduser(custom))
    if agent_should_share_browser_work_cookies():
        return master_profile_dir()
    return os.path.abspath(os.path.join(DATA_DIR, "mcp", "camoufox-agent-profile"))


def allocate_task_clone_dir(task_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", task_id or uuid.uuid4().hex).strip("-")
    if not safe_id:
        safe_id = uuid.uuid4().hex
    dest = Path(ephemeral_sessions_root()) / safe_id
    dest.mkdir(parents=True, exist_ok=True)
    return str(dest)


def allocate_agent_mcp_clone_dir() -> str:
    cleanup_orphaned_agent_mcp_profiles()
    dest = Path(ephemeral_sessions_root()) / f"agent-mcp-{os.getpid()}"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    return str(dest)


def cleanup_orphaned_agent_mcp_profiles(
    *, min_age_seconds: float = _ORPHANED_AGENT_MCP_PROFILE_MIN_AGE_SECONDS,
) -> list[str]:
    """Remove session clones left behind by MCP processes that no longer exist.

    Agent MCP clone directories are named after their owning server PID. A
    process crash can leave Firefox's ``lock`` and ``.parentlock`` files in
    such a directory. Live PIDs are deliberately left untouched, even when
    their browser is temporarily not running. Dead profiles are retained for
    an hour before deletion so an interrupted active run has a recovery window.
    """
    root = Path(ephemeral_sessions_root())
    if not root.is_dir():
        return []

    removed: list[str] = []
    for profile in root.glob("agent-mcp-*"):
        if not profile.is_dir():
            continue
        try:
            pid = int(profile.name.removeprefix("agent-mcp-"))
        except ValueError:
            continue
        try:
            age_seconds = max(0.0, time.time() - profile.stat().st_mtime)
        except OSError:
            continue
        if age_seconds < max(0.0, min_age_seconds):
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            if destroy_ephemeral_profile(str(profile)):
                removed.append(str(profile))
        except PermissionError:
            # A live process owned by another user must never be disturbed.
            continue
        except OSError:
            continue
    if removed:
        logger.info("Removed %d orphaned Camoufox MCP profile(s)", len(removed))
    return removed


def profile_lock_path(profile_dir: str) -> str:
    return os.path.join(os.path.abspath(os.path.expanduser(profile_dir)), _LOCK_NAME)


def _pids_holding_profile(profile_dir: str) -> List[int]:
    abs_profile = os.path.abspath(os.path.expanduser(profile_dir))
    needle = f"-profile {abs_profile}"
    pids: List[int] = []
    proc_root = "/proc"
    if not os.path.isdir(proc_root):
        return pids
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        cmdline_path = os.path.join(proc_root, entry, "cmdline")
        try:
            with open(cmdline_path, "rb") as fh:
                cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except OSError:
            continue
        if needle in cmdline and ("camoufox-bin" in cmdline or "firefox" in cmdline):
            pids.append(int(entry))
    return pids


def profile_process_ids(profile_dir: str) -> List[int]:
    """Return live Firefox/Camoufox processes using an exact profile path."""
    return _pids_holding_profile(profile_dir)


def master_profile_in_use(profile_dir: str) -> bool:
    abs_profile = os.path.abspath(os.path.expanduser(profile_dir))
    if _pids_holding_profile(abs_profile):
        return True
    for name in ("lock", ".parentlock"):
        path = os.path.join(abs_profile, name)
        if os.path.lexists(path):
            return True
    return False


def _assert_master_available_for_clone(master_dir: str) -> None:
    abs_master = os.path.abspath(os.path.expanduser(master_dir))
    if _pids_holding_profile(abs_master):
        raise RuntimeError(
            "Master browser profile is still open. Close the sign-in browser window "
            f"({abs_master}) before starting Browser Work or Agent Camoufox MCP."
        )
    clear_stale_profile_lock_files(abs_master)
    if master_profile_in_use(abs_master):
        raise RuntimeError(
            "Master browser profile is still open. Close the sign-in browser window "
            f"({abs_master}) before starting Browser Work or Agent Camoufox MCP."
        )


def _copy_profile_entry(src_root: Path, dest_root: Path, rel_path: str) -> None:
    src = src_root / rel_path
    if not src.exists():
        return
    dest = dest_root / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest)


def _selective_clone(master_dir: str, dest_dir: str) -> bool:
    master = Path(master_dir)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in _PROFILE_CLONE_FILES:
        if (master / name).exists():
            _copy_profile_entry(master, dest, name)
            copied += 1
    for pattern in _PROFILE_CLONE_GLOBS:
        for src_path in master.glob(pattern):
            if src_path.name in _PROFILE_EXCLUDE_NAMES:
                continue
            rel = src_path.relative_to(master).as_posix()
            _copy_profile_entry(master, dest, rel)
            copied += 1
    for name in _PROFILE_CLONE_DIRS:
        if (master / name).is_dir():
            _copy_profile_entry(master, dest, name)
            copied += 1
    return copied > 0 or (dest / "cookies.sqlite").is_file()


def _full_clone_minus_cache(master_dir: str, dest_dir: str) -> None:
    master = Path(master_dir)
    dest = Path(dest_dir)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    def _ignore(directory: str, names: List[str]) -> List[str]:
        ignored = []
        for name in names:
            if name in _PROFILE_EXCLUDE_NAMES:
                ignored.append(name)
            elif name == "cache2" or name == "startupCache":
                ignored.append(name)
        return ignored

    for entry in master.iterdir():
        if entry.name in _PROFILE_EXCLUDE_NAMES or entry.name in {"cache2", "startupCache"}:
            continue
        target = dest / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, ignore=_ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


def clone_profile_from_master(master_dir: str, dest_dir: str) -> str:
    """Snapshot master profile into dest_dir for an ephemeral browser session."""
    master = os.path.abspath(os.path.expanduser(master_dir))
    dest = os.path.abspath(os.path.expanduser(dest_dir))
    os.makedirs(master, exist_ok=True)

    _assert_master_available_for_clone(master)

    with master_profile_lock(master, timeout_seconds=30.0):
        if Path(dest).exists():
            shutil.rmtree(dest, ignore_errors=True)
        Path(dest).mkdir(parents=True, exist_ok=True)
        if not _selective_clone(master, dest):
            logger.info("Selective profile clone empty; falling back to full copy for %s", master)
            _full_clone_minus_cache(master, dest)
        clear_stale_profile_lock_files(dest)

    logger.info("Cloned master profile %s -> %s", master, dest)
    return dest


def promote_auth_state_to_master(source_dir: str, master_dir: str | None = None) -> dict:
    """Copy auth-relevant state from a task clone back into the master profile.

    The source must be a child of the ephemeral sessions directory. This keeps
    profile write-back explicit and prevents arbitrary filesystem paths from
    being copied into the long-lived sign-in profile.
    """
    source = os.path.abspath(os.path.expanduser(source_dir or ""))
    if not source or not os.path.isdir(source):
        raise ValueError("Source browser profile does not exist")
    if not _is_under_ephemeral_sessions(source):
        raise ValueError("Only ephemeral Browser Work profiles can be promoted")

    master = os.path.abspath(os.path.expanduser(master_dir or master_profile_dir()))
    if source == master:
        raise ValueError("Refusing to promote the master profile into itself")

    copied: list[str] = []
    skipped: list[str] = []
    os.makedirs(master, exist_ok=True)
    if master_profile_in_use(master):
        raise RuntimeError("Master browser profile is open; close it before saving login state")

    with master_profile_lock(master, timeout_seconds=30.0):
        src_root = Path(source)
        dst_root = Path(master)
        for name in _PROFILE_CLONE_FILES:
            if (src_root / name).exists():
                _copy_profile_entry(src_root, dst_root, name)
                copied.append(name)
            else:
                skipped.append(name)
        for pattern in _PROFILE_CLONE_GLOBS:
            for src_path in src_root.glob(pattern):
                if src_path.name in _PROFILE_EXCLUDE_NAMES:
                    continue
                rel = src_path.relative_to(src_root).as_posix()
                _copy_profile_entry(src_root, dst_root, rel)
                copied.append(rel)
        for name in _PROFILE_CLONE_DIRS:
            if (src_root / name).is_dir():
                _copy_profile_entry(src_root, dst_root, name)
                copied.append(name)
            else:
                skipped.append(name)
        clear_stale_profile_lock_files(master)

    logger.info("Promoted browser auth state %s -> %s (%s entries)", source, master, len(copied))
    return {
        "source": source,
        "master": master,
        "copied": copied,
        "skipped": skipped,
        "copied_count": len(copied),
    }


def _is_under_ephemeral_sessions(path: str) -> bool:
    abs_path = os.path.abspath(os.path.expanduser(path))
    root = ephemeral_sessions_root()
    try:
        common = os.path.commonpath([abs_path, root])
    except ValueError:
        return False
    return common == root and abs_path != root


def destroy_ephemeral_profile(dest_dir: str) -> bool:
    """Delete a task-scoped clone. Refuses to delete master or other roots."""
    if not dest_dir:
        return False
    abs_dest = os.path.abspath(os.path.expanduser(dest_dir))
    master = master_profile_dir()
    if abs_dest == master or abs_dest == os.path.abspath(os.path.expanduser(shared_persistent_profile_dir())):
        logger.warning("Refusing to destroy master profile: %s", abs_dest)
        return False
    if not _is_under_ephemeral_sessions(abs_dest):
        logger.warning("Refusing to destroy non-ephemeral profile path: %s", abs_dest)
        return False
    try:
        shutil.rmtree(abs_dest, ignore_errors=True)
        logger.info("Destroyed ephemeral browser profile: %s", abs_dest)
        return True
    except OSError as exc:
        logger.warning("Failed to destroy ephemeral profile %s: %s", abs_dest, exc)
        return False


def release_profile_holders(profile_dir: str, *, grace_seconds: float = 2.0) -> int:
    """Terminate browser processes currently bound to profile_dir."""
    released = 0
    for pid in _pids_holding_profile(profile_dir):
        try:
            os.kill(pid, signal.SIGTERM)
            released += 1
        except ProcessLookupError:
            continue
        except OSError as exc:
            logger.warning("Could not terminate profile holder pid=%s: %s", pid, exc)
    if released and grace_seconds > 0:
        deadline = time.time() + grace_seconds
        while time.time() < deadline:
            if not _pids_holding_profile(profile_dir):
                break
            time.sleep(0.1)
    for pid in _pids_holding_profile(profile_dir):
        try:
            os.kill(pid, signal.SIGKILL)
            released += 1
        except ProcessLookupError:
            continue
        except OSError as exc:
            logger.warning("Could not SIGKILL profile holder pid=%s: %s", pid, exc)
    clear_stale_profile_lock_files(profile_dir)
    return released


def clear_stale_profile_lock_files(profile_dir: str) -> None:
    root = os.path.abspath(os.path.expanduser(profile_dir))
    for name in ("lock", ".parentlock"):
        path = os.path.join(root, name)
        try:
            if os.path.islink(path) or os.path.isfile(path):
                os.unlink(path)
        except OSError:
            pass


_INCOMPATIBLE_PLAYWRIGHT_VERSION = (1, 60)


def playwright_camoufox_compat_error() -> str | None:
    """Return a user-facing error when Playwright is too new for Camoufox's Juggler."""
    try:
        import importlib.metadata

        raw = importlib.metadata.version("playwright")
    except Exception:
        return None
    parts = raw.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None
    if (major, minor) >= _INCOMPATIBLE_PLAYWRIGHT_VERSION:
        return (
            f"Playwright {raw} is incompatible with Camoufox: pages that throw JavaScript "
            "errors can crash the browser driver (daijro/camoufox#617). "
            "Fix: pip install 'playwright==1.58.0' (also pinned in requirements-optional.txt)."
        )
    return None


def assert_playwright_camoufox_compat() -> None:
    msg = playwright_camoufox_compat_error()
    if msg:
        raise RuntimeError(msg)


@contextmanager
def master_profile_lock(profile_dir: str, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Brief exclusive lock on master while cloning or signing in."""
    abs_profile = os.path.abspath(os.path.expanduser(profile_dir))
    os.makedirs(abs_profile, exist_ok=True)
    lock_path = profile_lock_path(abs_profile)
    lock_fh = open(lock_path, "a+", encoding="utf-8")
    deadline = time.time() + max(1.0, timeout_seconds)
    acquired = False
    try:
        while time.time() < deadline:
            try:
                try_lock_exclusive(lock_fh.fileno())
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.1)
        if not acquired:
            raise TimeoutError(
                f"Timed out waiting for master profile lock: {abs_profile}. "
                "Another process (Browser Work or Camoufox MCP) may be using the shared sign-in profile — "
                "cancel the other browser task, wait a moment, or disable cookie sharing in Settings."
            )
        yield
    finally:
        if acquired:
            try:
                unlock_file(lock_fh.fileno())
            except OSError:
                pass
        lock_fh.close()


@contextmanager
def exclusive_profile(profile_dir: str, *, timeout_seconds: float = 120.0) -> Iterator[None]:
    """Legacy: acquire exclusive profile lease, preempting other holders."""
    abs_profile = os.path.abspath(os.path.expanduser(profile_dir))
    os.makedirs(abs_profile, exist_ok=True)
    lock_path = profile_lock_path(abs_profile)
    release_profile_holders(abs_profile)
    lock_fh = open(lock_path, "a+", encoding="utf-8")
    deadline = time.time() + max(1.0, timeout_seconds)
    acquired = False
    try:
        while time.time() < deadline:
            try:
                try_lock_exclusive(lock_fh.fileno())
                acquired = True
                break
            except BlockingIOError:
                release_profile_holders(abs_profile)
                time.sleep(0.25)
        if not acquired:
            raise TimeoutError(
                f"Timed out waiting for shared browser profile: {abs_profile}. "
                "Cancel other Browser Work or Camoufox MCP sessions using this profile, then retry."
            )
        yield
    finally:
        if acquired:
            try:
                unlock_file(lock_fh.fileno())
            except OSError:
                pass
        lock_fh.close()
