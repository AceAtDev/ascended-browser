"""Several MCP servers on one data directory at once.

    ASCENDED_BROWSER_BIN=/path/to/venv/bin/ascended-browser python tests/concurrent_sessions.py

Session A signs in to a local fixture (a persistent cookie) and stays open.
Sessions B and C start while A runs: each must open pages instead of failing,
say once that it runs on a copy, arrive signed in with A's cookie, and keep
its own sign-in to itself. After they close, their copies are gone and A
still works on the saved profile.
"""
from __future__ import annotations

import asyncio
import http.server
import os
import shutil
import sys
import tempfile
import threading
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.environ.get("ASCENDED_BROWSER_BIN", "ascended-browser")
COPY = "runs on a copy"


def serve_fixture() -> tuple[str, http.server.ThreadingHTTPServer]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body, headers = "", []
            if self.path.startswith("/login/"):
                user = self.path.rsplit("/", 1)[1]
                headers.append(("Set-Cookie", f"user={user}; Max-Age=86400; Path=/"))
                body = f"<h1>Signed in as {user}</h1>"
            else:
                cookie = self.headers.get("Cookie") or ""
                user = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.startswith("user=")), "nobody")
                body = f"<h1>Hello {user}</h1>"
            data = f"<!doctype html><title>who</title><body>{body}</body>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def start(stack: AsyncExitStack, env: dict) -> ClientSession:
    read, write = await stack.enter_async_context(stdio_client(StdioServerParameters(command=SERVER, env=env)))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


async def visit(session: ClientSession, url: str) -> tuple[str, bool]:
    result = await session.call_tool("browser_open", {"url": url})
    return text_of(result), bool(result.isError)


async def main() -> int:
    base, httpd = serve_fixture()
    data = Path(tempfile.mkdtemp(prefix="ab-concurrent-", dir="/var/tmp"))
    env = {"ASCENDED_DATA_DIR": str(data), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(Path.home())}
    env.update({k: v for k, v in os.environ.items() if k.startswith("ASCENDED_BROWSER_") or k == "XDG_CACHE_HOME"})
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("PASS " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    try:
        async with AsyncExitStack() as a_stack:
            a = await start(a_stack, env)
            text, err = await visit(a, f"{base}/login/alice")
            check(not err and "Signed in as alice" in text and COPY not in text, "A signs in on the saved profile")
            await asyncio.sleep(3)  # Firefox writes cookies to disk shortly after they are set

            async with AsyncExitStack() as others:
                b = await start(others, env)
                c = await start(others, env)
                (b_text, b_err), (c_text, c_err) = await asyncio.gather(visit(b, f"{base}/"), visit(c, f"{base}/"))
                check(not b_err and not c_err, "B and C open pages while A holds the profile")
                check(COPY in b_text and COPY in c_text, "B and C each say they run on a copy")
                check("Hello alice" in b_text and "Hello alice" in c_text, "B and C arrive signed in with A's cookie")
                text, err = await visit(b, f"{base}/login/bob")
                check(not err and COPY not in text, "the copy notice is said once")
                (a_text, _), (c_text, _) = await asyncio.gather(visit(a, f"{base}/"), visit(c, f"{base}/"))
                check("Hello alice" in a_text and "Hello alice" in c_text, "B's sign-in stays in B")
                copies = [p for p in (data / "sessions").glob("*") if p.is_dir()]
                check(len(copies) == 2, f"two copies while B and C run ({len(copies)})")

            text, err = await visit(a, f"{base}/")
            check(not err and "Hello alice" in text, "A still works after B and C end")

        async with AsyncExitStack() as d_stack:
            d = await start(d_stack, env)
            text, err = await visit(d, f"{base}/")
            check(not err and COPY not in text and "Hello alice" in text,
                  "the next lone session gets the saved profile back")
            left = list((data / "sessions").glob("*"))
            check(not left, f"copies of ended sessions are removed ({len(left)} left)")
    finally:
        httpd.shutdown()
        shutil.rmtree(data, ignore_errors=True)
    print("OK" if not failures else f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
