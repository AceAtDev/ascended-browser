"""Which browser profile this server process uses.

The saved profile (cookies, sign-ins) can only be open in one Firefox at a
time. The first server process claims it; any other server started while it
runs (a second agent, a second editor window) gets its own copy instead of
failing: the sign-ins saved so far come along, and whatever that session
changes is dropped when it ends.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .runtime.platform import try_lock_exclusive, unlock

log = logging.getLogger("ascended_browser")

SESSIONS = "sessions"
NOTICE = (
    "Another ascended-browser session is using the saved browser profile, so this session "
    "runs on a copy of it: sites signed in before are still signed in, but sign-ins made "
    "here end with this session."
)


class Claim:
    """The workspace root this process should use, and whether it is a copy."""

    def __init__(self, root: Path, *, primary: bool, handle=None) -> None:
        self.root, self.primary, self._handle = root, primary, handle

    def release(self) -> None:
        if self._handle is not None:
            try:
                unlock(self._handle.fileno())
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        if not self.primary:
            shutil.rmtree(self.root, ignore_errors=True)


def _locked(path: Path):
    """An open, exclusively locked handle on path, or None when someone else holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try_lock_exclusive(handle.fileno())
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _profiles_busy(workspace: Path) -> bool:
    """A browser from another process (or an older release) has a profile here open."""
    for lock in (workspace / "profiles").glob(".*.owner-runtime.lock"):
        handle = _locked(lock)
        if handle is None:
            return True
        unlock(handle.fileno())
        handle.close()
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _sweep(sessions: Path) -> None:
    """Remove copies left behind by sessions that were killed."""
    for entry in sessions.glob("*"):
        if entry.is_dir() and entry.name.isdigit() and not _alive(int(entry.name)):
            shutil.rmtree(entry, ignore_errors=True)


def _copy_profiles(source: Path, dest: Path) -> None:
    from ._app.browser_profile_coord import _selective_clone, clear_stale_profile_lock_files
    from ._app.browser_workspace.fingerprint import SIDECAR_NAME

    for profile in source.glob("*"):
        if not profile.is_dir():
            continue
        target = dest / profile.name
        try:
            _selective_clone(str(profile), str(target))
            # Same fingerprint as the saved profile, so its cookies meet the identity they were issued to.
            if (profile / SIDECAR_NAME).is_file():
                shutil.copy2(profile / SIDECAR_NAME, target / SIDECAR_NAME)
            clear_stale_profile_lock_files(str(target))
        except OSError:
            log.warning("could not copy browser profile %s; this session starts signed out", profile, exc_info=True)
            shutil.rmtree(target, ignore_errors=True)


def claim(data: Path) -> Claim:
    """Claim the saved profile, or make this process a copy of it."""
    workspace = data / "workspace"
    sessions = data / SESSIONS
    _sweep(sessions)  # clients usually end a server with SIGTERM, so a copy outlives its session
    handle = _locked(data / ".profile-owner.lock")
    if handle is not None and not _profiles_busy(workspace):
        return Claim(workspace, primary=True, handle=handle)
    if handle is not None:
        unlock(handle.fileno())
        handle.close()
    root = sessions / str(os.getpid()) / "workspace"
    shutil.rmtree(root.parent, ignore_errors=True)
    (root / "profiles").mkdir(parents=True)
    _copy_profiles(workspace / "profiles", root / "profiles")
    log.warning("saved browser profile is in use by another session; using a copy at %s", root)
    return Claim(root, primary=False)
