"""``ascended-browser``: run the MCP server, or check / prepare this machine.

    ascended-browser            # MCP server on stdio (what a client launches)
    ascended-browser doctor     # what is installed, what is missing
    ascended-browser fetch      # download the Camoufox browser now (otherwise on first use)
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys

from . import __version__


def _camoufox_path() -> str:
    try:
        from camoufox.pkgman import camoufox_path

        return str(camoufox_path(download_if_missing=False))
    except Exception:
        return ""


def doctor() -> int:
    from .runtime.paths import data_dir
    from .server import _window_mode

    checks = {
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "data_dir": str(data_dir()),
        "camoufox_browser": _camoufox_path() or "missing: run `ascended-browser fetch`",
        "window": {False: "visible", True: "headless", "virtual": "virtual display (Xvfb)"}[_window_mode()],
        "xvfb": shutil.which("Xvfb") or ("not needed" if not sys.platform.startswith("linux") else "missing (headless instead)"),
    }
    print(json.dumps(checks, indent=2))
    return 0 if checks["camoufox_browser"] and not checks["camoufox_browser"].startswith("missing") else 1


def fetch() -> int:
    from camoufox.pkgman import CamoufoxFetcher

    CamoufoxFetcher().install()
    print(_camoufox_path() or "Camoufox fetch did not complete", file=sys.stderr)
    return 0 if _camoufox_path() else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="ascended-browser", description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", nargs="?", choices=["serve", "doctor", "fetch"], default="serve")
    parser.add_argument("--version", action="version", version=f"ascended-browser {__version__}")
    command = parser.parse_args().command
    if command == "doctor":
        sys.exit(doctor())
    if command == "fetch":
        sys.exit(fetch())
    from .server import run

    run()


if __name__ == "__main__":
    main()
