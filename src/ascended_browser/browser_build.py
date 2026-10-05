"""The Camoufox build this package is tested on, and keeping to it.

`camoufox fetch` installs the newest Camoufox release, which can be a Firefox
version none of this package's test suites ran on. This package pins the build
they did run on, the way Playwright pins its browsers.

The pin is per process: camoufox's own config is shared with every other tool
on the machine (Ascended included), so its default version is left as it was.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

log = logging.getLogger("ascended_browser")

PINNED_VERSION, PINNED_BUILD = "135.0.1", "beta.24"
PINNED = f"{PINNED_VERSION}-{PINNED_BUILD}"

_lock = threading.Lock()


def installed_path() -> Path | None:
    from camoufox.multiversion import find_installed_version

    try:
        return find_installed_version(PINNED)
    except Exception:
        return None


def install() -> Path:
    """Download and install the pinned build (about 700 MB on Linux)."""
    from camoufox.multiversion import load_config, set_active
    from camoufox.pkgman import CamoufoxFetcher, list_available_versions

    wanted = next((v for v in list_available_versions()
                   if v.version.version == PINNED_VERSION and v.version.build == PINNED_BUILD), None)
    if wanted is None:
        raise RuntimeError(f"Camoufox {PINNED} is not offered for this platform on GitHub")
    previous = load_config().get("active_version")
    CamoufoxFetcher(selected_version=wanted).install()
    if previous:  # installing makes it the machine-wide default; put the old one back
        set_active(previous)
    path = installed_path()
    if path is None:
        raise RuntimeError(f"Camoufox {PINNED} did not install")
    return path


def pin(download: bool = True) -> Path | None:
    """Make this process use the pinned build, installing it first if asked."""
    with _lock:
        path = installed_path()
        if path is None and download:
            log.warning("downloading Camoufox %s (first run only)", PINNED)
            path = install()
        if path is None:
            return None
        from camoufox import multiversion, pkgman

        pkgman.camoufox_path = lambda download_if_missing=True: path
        multiversion.get_active_path = lambda: path
        return path


def prefetch() -> None:
    """Start downloading the pinned build in the background if it is missing."""
    if installed_path() is not None:
        pin(download=False)
        return

    def run() -> None:
        try:
            pin(download=True)
        except Exception:
            log.warning("background Camoufox download failed; it is retried at first use", exc_info=True)

    threading.Thread(target=run, name="camoufox-fetch", daemon=True).start()
