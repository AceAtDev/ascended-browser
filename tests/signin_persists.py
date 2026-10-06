"""``ascended-browser signin`` keeps the sign-in for later agent sessions.

    ASCENDED_BROWSER_BIN=.venv/bin/ascended-browser python tests/signin_persists.py

Runs signin against a local fixture whose page sets a persistent cookie (the
part a person would do by hand), presses Enter for them, then starts an MCP
session on the same data directory and checks the fixture sees the cookie.
On Linux it runs on its own Xvfb display, so no window appears on yours.
"""
from __future__ import annotations

import asyncio
import http.server
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.environ.get("ASCENDED_BROWSER_BIN", "ascended-browser")


def serve_fixture() -> tuple[str, http.server.ThreadingHTTPServer]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/signin"):
                body = b"<title>Sign in</title><h1>Signed in (cookie set)</h1>"
                cookie = "session=fixture-ok; Max-Age=86400; Path=/; SameSite=Lax"
            else:
                who = "signed in" if "session=fixture-ok" in (self.headers.get("Cookie") or "") else "signed out"
                body = f"<title>Account</title><h1>You are {who}</h1>".encode()
                cookie = ""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


async def main() -> int:
    base, httpd = serve_fixture()
    data = Path(tempfile.mkdtemp(prefix="ab-signin-data-"))
    env = {**os.environ, "ASCENDED_DATA_DIR": str(data)}
    env.pop("ASCENDED_BROWSER_WINDOW", None)
    xvfb = None
    if sys.platform.startswith("linux") and shutil.which("Xvfb"):
        display = f":{90 + os.getpid() % 900}"
        xvfb = subprocess.Popen(["Xvfb", display, "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
                                stderr=subprocess.DEVNULL, start_new_session=True)
        env["DISPLAY"] = display
        env.pop("WAYLAND_DISPLAY", None)
        await asyncio.sleep(1)
    failures = []
    try:
        signin = await asyncio.create_subprocess_exec(
            SERVER, "signin", f"{base}/signin", env=env, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        prompt = b""
        while b"press Enter" not in prompt:
            line = await asyncio.wait_for(signin.stderr.readline(), 240)
            if not line:
                break
            prompt += line
        signin.stdin.write(b"\n")
        await signin.stdin.drain()
        rest = await asyncio.wait_for(signin.stderr.read(), 120)
        code = await signin.wait()
        output = (prompt + rest).decode(errors="replace")
        if code or "Saved." not in output:
            failures.append(f"signin exited {code}: {output[-600:]}")
        print(f"signin exit {code}: {'Saved' if 'Saved.' in output else 'no confirmation'}")

        session_env = {k: v for k, v in env.items() if k != "DISPLAY"}
        async with stdio_client(StdioServerParameters(command=SERVER, env=session_env)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("browser_open", {"url": f"{base}/account"})
                text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
                state = re.search(r"You are (signed in|signed out)", text)
                print(f"next agent session: {state.group(0) if state else 'page not read'}")
                if not state or state.group(1) != "signed in":
                    failures.append("the agent session after signin was not signed in")
    finally:
        httpd.shutdown()
        if xvfb:
            xvfb.terminate()
        shutil.rmtree(data, ignore_errors=True)
    print("PASS" if not failures else "FAIL\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
