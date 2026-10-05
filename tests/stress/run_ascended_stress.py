"""Run Ascended's browser stress harnesses against this package instead of the app.

    python tests/stress/run_ascended_stress.py web --source ~/odysseus --output runs/web
    python tests/stress/run_ascended_stress.py dev --source ~/odysseus --output runs/dev

The scenario files are read from an Ascended checkout (they are the bar the
app is held to), but every import of the app itself (``src``, ``core``,
``services``, ``routes``) is blocked: the harness's Runner gets this package's
manager, dispatcher and formatter, and a scenario that reaches for an app
module gets the package's copy or an ImportError. A pass here is a pass of
the packaged code, comparable line for line with the app's own runs.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.abc
import os
import sys
import tempfile
from pathlib import Path

APP_ROOTS = {"src", "core", "services", "routes"}
# App modules a scenario imports directly, and the packaged module that is the same code.
ALIASES = {"src.browser_capture_compare": "ascended_browser._app.browser_capture_compare"}


class _BlockApp(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in APP_ROOTS and name not in sys.modules:
            raise ImportError(f"{name}: the standalone package must not import the Ascended app")
        return None


def _install_aliases() -> None:
    """Empty stand-in parents, so only the aliased modules resolve under them."""
    import types

    for name, target in ALIASES.items():
        parts = name.split(".")
        for depth in range(1, len(parts)):
            parent = ".".join(parts[:depth])
            if parent not in sys.modules:
                stub = types.ModuleType(parent)
                stub.__path__ = []
                sys.modules[parent] = stub
        sys.modules[name] = importlib.import_module(target)


def _use_package(runner_module) -> None:
    """Give the harness Runner this package's browser stack."""
    Runner = runner_module.Runner

    async def __aenter__(self):
        scratch = str(self.data) if self.data else tempfile.mkdtemp(prefix="ab-stress-")
        os.environ["ASCENDED_DATA_DIR"] = scratch
        from ascended_browser._app.browser_workspace.auth_state import AuthStateStore
        from ascended_browser._app.browser_workspace.manager import BrowserWorkspaceManager
        from ascended_browser._app.browser_workspace.store import WorkspaceStore
        from ascended_browser._app.dispatch import do_browser_workspace
        from ascended_browser._app.formatting import format_tool_result
        from ascended_browser.runtime import agent_tools
        from ascended_browser.window import BrowserWindow

        window = BrowserWindow()

        class Manager(BrowserWorkspaceManager):
            def _headless_mode(self):
                return window.headless

        self.manager = Manager(store=WorkspaceStore(os.path.join(scratch, "ws")),
                               auth_store=AuthStateStore(os.path.join(scratch, "auth")))
        self.manager.auth_store.mark_migrated(self.owner, {"migrated": True})
        window.install(self.manager)
        self._window = window
        if self.trust_ca:
            self._trust_ca(self.trust_ca)
        agent_tools.set_browser_workspace_manager(self.manager)
        self._format = format_tool_result
        self._dispatch = do_browser_workspace
        self.scratch = scratch
        return self

    Runner.__aenter__ = __aenter__


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("harness", choices=["web", "dev"])
    parser.add_argument("--source", type=Path, required=True, help="Ascended checkout holding the scenarios")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenarios", default="all", help="web only: comma-separated subset")
    parser.add_argument("--loops", type=int, default=12, help="dev only")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    sys.meta_path.insert(0, _BlockApp())
    _install_aliases()
    sys.path.insert(0, str(source))  # for manual_tests.* only; app packages are blocked above
    runner_module = importlib.import_module("manual_tests.browser_stress.runner")
    _use_package(runner_module)
    os.chdir(source)  # the harnesses read their fixtures relative to the checkout
    if args.harness == "web":
        harness = importlib.import_module("manual_tests.browser_stress.web_browsing_stress")
        return asyncio.run(harness.run(output, None, None, args.scenarios))
    harness = importlib.import_module("manual_tests.browser_stress.dev_loop_stress")
    return asyncio.run(harness.run(output, None, args.loops, None))


if __name__ == "__main__":
    sys.exit(main())
