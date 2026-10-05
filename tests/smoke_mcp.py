"""End-to-end smoke test: a real MCP client drives the installed server.

    ASCENDED_BROWSER_BIN=/path/to/venv/bin/ascended-browser python tests/smoke_mcp.py

Opens a local form, observes it, fills a field by ref, reads the value back,
takes a picture, and checks that a long result comes back clipped with an
evidence reference that browser_extract can page. Fails on any tool error or
protocol fault (a stray print on stdout breaks the stream).
"""
from __future__ import annotations

import asyncio
import http.server
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.environ.get("ASCENDED_BROWSER_BIN", "ascended-browser")
FORM = """<!doctype html><html><head><title>Signup fixture</title></head><body>
<h1>Join the waitlist</h1>
<form onsubmit="event.preventDefault(); document.getElementById('done').hidden = false">
  <label>Email <input id="email" type="email" name="email"></label>
  <label>Plan <select name="plan"><option>Free</option><option>Founder</option></select></label>
  <button type="submit">Join</button>
</form>
<p id="done" hidden>Thanks, you're on the list.</p>
</body></html>"""
LONG = "<!doctype html><title>Long page</title><body>" + "".join(
    f"<p>Paragraph {i}: {'lorem ipsum dolor sit amet ' * 12}</p>" for i in range(900)) + "</body>"


def serve_fixture() -> tuple[str, http.server.ThreadingHTTPServer]:
    root = Path(tempfile.mkdtemp(prefix="ab-fixture-"))
    (root / "index.html").write_text(FORM)
    (root / "long.html").write_text(LONG)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}/", httpd


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def call(session, failures, name, args, timeout=240):
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(session.call_tool(name, args), timeout)
    except Exception as exc:
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"  FAIL  {name:<18} {time.monotonic() - started:5.1f}s {exc}")
        return None, ""
    text = text_of(result)
    if result.isError:
        failures.append(f"{name}: {text[:400]}")
    print(f"  {'error' if result.isError else 'ok':<5} {name:<18} {time.monotonic() - started:5.1f}s "
          f"{len(text):>6}c {sorted({c.type for c in result.content})}")
    return result, text


async def main() -> int:
    url, httpd = serve_fixture()
    data = Path(tempfile.mkdtemp(prefix="ab-data-"))
    failures: list[str] = []
    env = {"ASCENDED_DATA_DIR": str(data), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(Path.home()), "ASCENDED_RESULT_MAX_CHARS": "6000"}
    # Launcher settings (the npm wrapper's package override, cache dir) pass through.
    env.update({k: v for k, v in os.environ.items() if k.startswith("ASCENDED_BROWSER_") or k == "XDG_CACHE_HOME"})
    try:
        async with stdio_client(StdioServerParameters(command=SERVER, env=env)) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                print(f"server {init.serverInfo.name} {init.serverInfo.version}: {len(tools.tools)} tools")
                _, text = await call(session, failures, "browser_open", {"url": url})
                tab = re.search(r"\btab_[0-9a-f]{32}\b", text)
                if not tab:
                    failures.append("browser_open returned no tab_id")
                    return 1
                tab_id = tab.group(0)
                _, text = await call(session, failures, "browser_observe", {"tab_id": tab_id})
                ref = re.search(r'"text": "Email"[^}]*?"control_refs": \["(e[0-9a-f]+)"', text)
                if not ref:
                    failures.append("observe showed no ref for the email field")
                    return 1
                await call(session, failures, "browser_act", {
                    "tab_id": tab_id, "action": {"kind": "fill", "ref": ref.group(1), "value": "probe@example.com"}})
                _, text = await call(session, failures, "browser_extract", {"tab_id": tab_id})
                if "probe@example.com" not in text:
                    failures.append("extract did not read back the filled email")
                result, _ = await call(session, failures, "browser_screenshot", {"tab_id": tab_id})
                if result is not None and not any(c.type == "image" for c in result.content):
                    failures.append("screenshot returned no image")
                # A long result is clipped and pageable.
                await call(session, failures, "browser_act", {
                    "tab_id": tab_id, "action": {"kind": "navigate", "url": url + "long.html"}})
                _, text = await call(session, failures, "browser_extract", {"tab_id": tab_id, "read": "text"})
                evidence = re.search(r'"evidence_ref": "([^"]+)"', text)
                if "Result clipped" in text and evidence:
                    _, page = await call(session, failures, "browser_extract",
                                         {"evidence_ref": evidence.group(1), "cursor": 0})
                    if "Paragraph" not in page:
                        failures.append("evidence_ref page did not contain the stored text")
                elif "Result clipped" in text:
                    failures.append("clipped result named no evidence_ref")
    finally:
        httpd.shutdown()
        shutil.rmtree(data, ignore_errors=True)
    print("PASS" if not failures else "FAIL\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
