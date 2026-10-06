"""The ascended-browser MCP server (stdio).

One server process is one browser session: its tabs and element refs live
until the client disconnects. The browser profile lives in the data directory,
so a site you sign in to stays signed in across sessions; a server started while
another one has it open runs on a copy (see profiles.py).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import sys
from importlib import resources
from typing import Any

from . import __version__

log = logging.getLogger("ascended_browser")

OWNER = "local"
# A result longer than this goes to the client clipped, with the whole of it
# stored for browser_extract evidence_ref. About 6k tokens: inside every MCP
# client's default output limit, with room for several results in one turn.
CLIP_CHARS = int(os.environ.get("ASCENDED_RESULT_MAX_CHARS", "24000") or 24000)

INSTRUCTIONS = (
    "A real browser (Camoufox, a hardened Firefox) driven through verified actions. "
    "browser_open loads a page and returns what is on it, each element with a ref; act with "
    "browser_act using those refs (click, fill, fill_form, select, check, date, press, upload, "
    "scroll). Results say what changed, so you rarely need browser_observe after an action. "
    "Use browser_extract to read content, find a phrase, or list every match of a CSS selector, "
    "browser_viewport and browser_screenshot for responsive and visual checks, and "
    "browser_extract read=console|network|inspect to debug a page you are building. On a sign-in "
    "form, browser_login fills the user's saved login for that site (you never see its values; "
    "they read as [redacted]); without one, ask the user to save it with `ascended-browser login "
    "add`. For public pages you only need to read, a fetch tool is faster than the browser."
)


def _window_mode() -> bool | str:
    """The display choice reported by ``doctor`` (see window.BrowserWindow)."""
    from .window import wants_visible

    if wants_visible():
        return False
    if sys.platform.startswith("linux") and shutil.which("Xvfb"):
        return "virtual"
    return True


async def _viewport(window, manager, session: str, args: dict, dispatch) -> dict:
    """browser_viewport: set/restore resize the real window; get/emulate go to the app code."""
    from ._app.browser_workspace.manager_core import VIEWPORT_PRESETS

    action = str(args.get("action") or "").strip().lower()
    tab_id = str(args.get("tab_id") or "")
    if action not in {"set", "restore"} or not window.resizable:
        return await dispatch("browser_viewport", json.dumps(args), owner=OWNER, session_id=session)
    width, height = args.get("width"), args.get("height")
    preset = str(args.get("preset") or "").strip().lower()
    if action == "set":
        if preset:
            if preset not in VIEWPORT_PRESETS:
                return {"error": "Viewport preset must be phone, tablet, or desktop", "exit_code": 1}
            width, height = VIEWPORT_PRESETS[preset]
        try:
            width, height = int(width), int(height)
        except (TypeError, ValueError):
            return {"error": "browser_viewport set needs a preset or integer width and height", "exit_code": 1}
        if not (240 <= width <= 3840 and 240 <= height <= 2400):
            return {"error": "Viewport size must be within 240-3840 by 240-2400", "exit_code": 1}
    else:
        width = height = None
    try:
        sized = await window.resize(manager, OWNER, session, tab_id, width, height)
    except Exception as exc:
        return {"error": f"browser_viewport: {exc}", "exit_code": 1}
    cleared = {}
    if action == "restore" and manager.emulation_for(OWNER, session, tab_id):
        cleared = await manager.emulate(OWNER, session, tab_id, clear=True, actor="parent", actor_id="parent")
    observed = await dispatch("browser_observe", json.dumps({"tab_id": tab_id}), owner=OWNER, session_id=session)
    return {
        "action": action, "available": True, "tab_id": tab_id,
        "requested_width": sized["requested"][0], "requested_height": sized["requested"][1],
        "width": sized["width"], "height": sized["height"], "clamped": sized["clamped"],
        "resized": [sized["width"], sized["height"]] != sized["before"],
        **({"preset": preset} if preset else {}),
        "size_mode": "fixed" if action == "set" else "launch",
        "emulation": manager.emulation_for(OWNER, session, tab_id),
        **({"emulation_cleared": True} if cleared else {}),
        "message": (f"The window now shows the page at {sized['width']}x{sized['height']} CSS pixels; "
                    "the page reflowed and earlier element positions are stale.")
                   if action == "set" else "Back to the launch size" + (" and emulation cleared." if cleared else "."),
        "page": observed,
        "exit_code": 0, "success": True,
    }


def _tool_schemas() -> list[dict]:
    text = resources.files("ascended_browser._app").joinpath("tool_schemas.json").read_text()
    return json.loads(text)


def _content(name: str, args: dict, result: dict, session: str) -> tuple[list, bool]:
    from mcp import types

    from ._app.formatting import browser_result_archive, format_tool_result
    from .logins import scrub, widen_masks
    from .runtime.evidence import store_text

    # No saved login value reaches the agent, whichever tool read it.
    text = scrub(format_tool_result(name, result, arguments=args))
    # An evidence page is already bounded (12k characters at most) and is itself
    # the way to read a clipped result, so it is never clipped again.
    if len(text) > CLIP_CHARS and result.get("source") != "browser_evidence":
        full = scrub(browser_result_archive(name, result, args) or text)
        reference = store_text(session, full)
        text = (
            text[:CLIP_CHARS]
            + f"\n\n[Result clipped at {CLIP_CHARS:,} of {len(text):,} characters. The complete "
            + f'result: browser_extract {{"evidence_ref": "{reference}", "cursor": 0}}.]'
        )
    content: list[Any] = [types.TextContent(type="text", text=text)]
    for image in result.get("images") or []:
        if isinstance(image, dict) and image.get("data"):
            kind = image.get("mimeType") or "image/png"
            content.append(types.ImageContent(type="image", data=widen_masks(image["data"], kind), mimeType=kind))
    failed = result.get("exit_code") not in (None, 0) or result.get("success") is False
    return content, bool(failed)


async def serve(protocol_out: io.TextIOWrapper) -> None:
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    from ._app.browser_workspace.auth_state import AuthStateStore
    from ._app.browser_workspace.manager import BrowserWorkspaceManager
    from ._app.browser_workspace.store import WorkspaceStore
    from ._app.dispatch import do_browser_workspace
    from .runtime import agent_tools
    from .runtime.paths import data_dir

    from .window import (BrowserWindow, apply_frame_size, demo_image, demo_log, install_demo_events, log_boxes,
                         log_find, log_geometry, refs_in_result)

    from .browser_build import prefetch
    from .inflight import Journal, interrupted_note, recovered_note
    from .logins import install as install_logins
    from .logins import scrub
    from .profiles import NOTICE, claim

    prefetch()  # first run: start the browser download now, not at the first tool call
    install_logins()
    window = BrowserWindow()

    class Manager(BrowserWorkspaceManager):
        def _headless_mode(self):  # noqa: D401 - the app's hook for the display choice
            return window.headless

    root = data_dir()
    profile = claim(root)
    notice = [] if profile.primary else [NOTICE]
    journal = Journal(root)
    unfinished = journal.recover()  # actions an ended session never reported back
    manager = Manager(store=WorkspaceStore(str(profile.root)),
                      auth_store=AuthStateStore(str(root / "auth")))
    manager.auth_store.mark_migrated(OWNER, {"migrated": True})
    window.install(manager)
    install_demo_events(window, manager)
    log.info("browser window: %s", window.describe())
    agent_tools.set_browser_workspace_manager(manager)
    session = "mcp-" + os.urandom(4).hex()

    schemas = _tool_schemas()
    tools = [types.Tool(**schema) for schema in schemas]
    known = {schema["name"] for schema in schemas}
    server = Server("ascended-browser", version=__version__, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict | None):
        if name not in known:
            raise ValueError(f"Unknown tool: {name}")
        args = dict(arguments or {})
        if name == "browser_screenshot":
            args["purpose"] = "inspect"
        demo_log({"type": "tool", "phase": "start", "tool": name, "args": args})
        action = args.get("action") if isinstance(args.get("action"), dict) else {}
        if name == "browser_act" and action.get("ref") and action.get("kind") in {"type", "fill", "select", "press"}:
            await log_boxes(manager, OWNER, session, str(args.get("tab_id") or ""), "field",
                            [(str(action["ref"]), "field")])
        pending = journal.begin(name, args)
        try:
            if name == "browser_viewport":
                result = await _viewport(window, manager, session, args, do_browser_workspace)
            else:
                result = await do_browser_workspace(name, json.dumps(args), owner=OWNER, session_id=session)
        except anyio.get_cancelled_exc_class():
            journal.interrupted(pending)  # the turn stopped mid-action; tell the next one
            raise
        finally:
            journal.end(pending)
        if isinstance(result, dict) and result.get("tab_id") and name in {"browser_open", "browser_viewport"}:
            if name == "browser_open":
                await apply_frame_size(window)
            await log_geometry(manager, OWNER, session, str(result["tab_id"]))
        if not isinstance(result, dict):
            result = {"error": f"{name} returned no result", "exit_code": 1}
        content, failed = _content(name, args, result, session)
        tab_id = str(result.get("tab_id") or args.get("tab_id") or "")
        journal.learn(tab_id, content[0].text if content else "")
        if notice and name == "browser_open" and not failed:  # said once, with the first page this session opens
            content.insert(0, types.TextContent(type="text", text=notice.pop()))
        told = journal.notes_for(tab_id)
        if told:
            content.insert(0, types.TextContent(type="text", text=scrub(interrupted_note(told))))
        if unfinished:
            content.insert(0, types.TextContent(type="text", text=scrub(recovered_note(unfinished))))
            unfinished.clear()
        text = "\n".join(getattr(c, "text", "") for c in content)
        if name == "browser_extract" and not failed and (args.get("read") == "audit" or args.get("selector")):
            audit = args.get("read") == "audit"
            await log_boxes(manager, OWNER, session, str(result.get("tab_id") or args.get("tab_id") or ""),
                            "audit" if audit else "query", refs_in_result(text, audit))
        if name == "browser_extract" and not failed and isinstance(args.get("find"), str):
            await log_find(manager, OWNER, session, str(result.get("tab_id") or args.get("tab_id") or ""), args["find"])
        image = next((c.data for c in content if getattr(c, "type", "") == "image"), "")
        demo_log({"type": "tool", "phase": "end", "tool": name, "ok": not failed, "text": text[:8000],
                  **({"image": shot} if (shot := demo_image(image)) else {})})
        return types.CallToolResult(content=content, isError=failed)

    stdin = anyio.wrap_file(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"))
    stdout = anyio.wrap_file(protocol_out)
    try:
        async with stdio_server(stdin=stdin, stdout=stdout) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        try:
            await asyncio.wait_for(manager.close_all(), timeout=20)
        except Exception:
            log.debug("browser close at exit failed", exc_info=True)
        window.close()
        profile.release()


def run() -> None:
    # stdout carries the MCP protocol; everything the browser stack prints goes to stderr.
    protocol_out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr,
                        level=os.environ.get("ASCENDED_LOG_LEVEL", "WARNING").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve(protocol_out))
