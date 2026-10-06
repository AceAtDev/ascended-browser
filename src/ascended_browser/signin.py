"""``ascended-browser signin URL``: sign in by hand, in the agent's own browser profile.

For sites a saved login cannot fill (single sign-on, passkeys, a CAPTCHA, an
emailed code): the browser opens visibly on the saved profile, you sign in,
press Enter here, and the cookies stay in the profile every later agent session
starts from. Nothing typed here passes through an agent.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys


async def _signin(url: str) -> int:
    os.environ["ASCENDED_BROWSER_WINDOW"] = "show"
    from ._app.browser_workspace.auth_state import AuthStateStore
    from ._app.browser_workspace.manager import BrowserWorkspaceManager
    from ._app.browser_workspace.store import WorkspaceStore
    from ._app.dispatch import do_browser_workspace
    from .browser_build import pin
    from .profiles import claim
    from .runtime import agent_tools
    from .runtime.paths import data_dir
    from .server import OWNER
    from .window import BrowserWindow

    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("signin needs a screen: run it in a desktop session (no DISPLAY is set).", file=sys.stderr)
        return 1
    root = data_dir()
    profile = claim(root)
    if not profile.primary:
        profile.release()
        print("An agent session has the saved browser profile open. Close it (or the agent "
              "client) and run signin again, so the sign-in lands in the saved profile.", file=sys.stderr)
        return 1
    pin(download=True)
    from .logins import install as install_logins

    install_logins()
    window = BrowserWindow()

    class Manager(BrowserWorkspaceManager):
        def _headless_mode(self):  # noqa: D401 - the app's hook for the display choice
            return window.headless

    manager = Manager(store=WorkspaceStore(str(profile.root)), auth_store=AuthStateStore(str(root / "auth")))
    manager.auth_store.mark_migrated(OWNER, {"migrated": True})
    window.install(manager)
    agent_tools.set_browser_workspace_manager(manager)
    try:
        result = await do_browser_workspace("browser_open", json.dumps({"url": url}),
                                            owner=OWNER, session_id="signin")
        if not isinstance(result, dict) or result.get("error"):
            print(f"Could not open {url}: {(result or {}).get('error') if isinstance(result, dict) else result}",
                  file=sys.stderr)
            return 1
        print(f"Sign in to {url} in the browser window, then press Enter here to save it.", file=sys.stderr)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        try:
            await asyncio.wait_for(manager.close_all(), timeout=30)
        except Exception:
            pass
        window.close()
        profile.release()
    print("Saved. Agent sessions started from now on begin signed in.", file=sys.stderr)
    return 0


def signin(url: str) -> int:
    if "://" not in url:
        url = "https://" + url
    return asyncio.run(_signin(url))
