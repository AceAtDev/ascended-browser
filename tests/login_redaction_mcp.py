"""End-to-end: browser_login signs in, and no tool shows the agent the saved login.

    ASCENDED_BROWSER_BIN=.venv/bin/ascended-browser python tests/login_redaction_mcp.py [--shots DIR]

A local sign-in page that is as leaky as a site can be: it posts the email and
password, then echoes both into the page. A login is saved with the CLI, a real
MCP client signs in with browser_login, and every way an agent can read a page
(observe, evaluate of the input values, extract text and network, act receipts,
screenshots) is checked for the username and password in any encoding. Fails
if one appears, or if the sign-in did not happen. Screenshots are read with
tesseract when it is installed, so the masking is checked in pixels too;
--shots keeps the pictures so it can be looked at.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import html
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.environ.get("ASCENDED_BROWSER_BIN", "ascended-browser")
USERNAME = "ada.tester@example.org"
PASSWORD = "Hunter2-Sekr!t&x"
PAGE = """<!doctype html><html><head><title>Fixture Bank sign in</title>
<style>body{font:16px system-ui;margin:40px;max-width:420px} label{display:block;margin:12px 0}
input{display:block;width:100%;padding:8px;font-size:16px} button{padding:8px 16px;font-size:16px}</style>
</head><body>
<h1>Fixture Bank</h1>
<form id="f">
  <label>Email <input type="email" name="email" autocomplete="username"></label>
  <label>Password <input type="password" name="password" autocomplete="current-password"></label>
  <button type="submit">Sign in</button>
</form>
<div id="out"></div>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = await fetch('/api/login', {method: 'POST', body: new URLSearchParams(new FormData(e.target))});
  const j = await r.json();
  document.getElementById('f').hidden = true;
  document.getElementById('out').innerHTML =
    '<h2>Welcome back</h2><p>Signed in as <b>' + j.user + '</b></p><p>Debug echo: ' + j.echo + '</p>';
});
</script></body></html>"""


def serve_fixture() -> tuple[str, http.server.ThreadingHTTPServer]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self._send("text/html", PAGE.encode())

        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
            body = {"user": form.get("email", [""])[0], "echo": form.get("password", [""])[0]}
            self._send("application/json", json.dumps(body).encode())

        def _send(self, kind, data):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}/", httpd


# What OCR of a picture must not contain (fragments, since OCR drops punctuation).
PICTURE_FRAGMENTS = ("hunter2", "sekr", "ada.tester", "tester@")


def picture_leaks(png: bytes) -> list[str] | None:
    """Saved-login fragments tesseract reads in a screenshot; None without tesseract."""
    if not shutil.which("tesseract"):
        return None
    with tempfile.NamedTemporaryFile(suffix=".png") as image:
        image.write(png)
        image.flush()
        ocr = subprocess.run(["tesseract", image.name, "stdout"], capture_output=True, text=True)
    if ocr.returncode:
        return None  # e.g. no language data: TESSDATA_PREFIX=<dir with eng.traineddata>
    text = ocr.stdout.lower()
    if "fixture bank" not in text:
        return ["(OCR could not read the page at all)"]
    return [f for f in PICTURE_FRAGMENTS if f in text]


def leaks(text: str) -> list[str]:
    low = text.lower()
    forms = {PASSWORD, quote(PASSWORD, safe=""), quote_plus(PASSWORD), html.escape(PASSWORD),
             json.dumps(PASSWORD)[1:-1], USERNAME, quote(USERNAME, safe=""), quote_plus(USERNAME)}
    return sorted(f for f in forms if f.lower() in low)


async def main() -> int:
    options = argparse.ArgumentParser()
    options.add_argument("--shots", type=Path)
    options = options.parse_args()
    url, httpd = serve_fixture()
    data = Path(tempfile.mkdtemp(prefix="ab-login-data-"))
    env = {"ASCENDED_DATA_DIR": str(data), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(Path.home())}
    env.update({k: v for k, v in os.environ.items() if k.startswith("ASCENDED_BROWSER_") or k == "XDG_CACHE_HOME"})
    failures: list[str] = []
    seen: dict[str, str] = {}
    client_cwd = Path(tempfile.mkdtemp(prefix="ab-client-cwd-"))  # the agent's project: must stay untouched

    async def call(session, name, args, label=None):
        started = time.monotonic()
        result = await asyncio.wait_for(session.call_tool(name, args), 240)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        label = label or name
        seen[label] = text
        found = leaks(text)
        if found:
            failures.append(f"{label} showed the saved login: {found}")
        if result.isError:
            failures.append(f"{label} failed: {text[:300]}")
        print(f"  {'LEAK' if found else 'error' if result.isError else 'ok':<5} {label:<26} "
              f"{time.monotonic() - started:5.1f}s {len(text):>6}c")
        for image in (c for c in result.content if getattr(c, "type", "") == "image"):
            png = base64.b64decode(image.data)
            shown = picture_leaks(png)
            if shown:
                failures.append(f"{label} picture shows the saved login: {shown}")
            print(f"        picture: {'not OCR-checked (no tesseract)' if shown is None else 'LEAK ' + str(shown) if shown else 'no saved login visible'}")
            if options.shots:
                options.shots.mkdir(parents=True, exist_ok=True)
                (options.shots / f"{label}.png").write_bytes(png)
        return result, text

    try:
        added = subprocess.run([SERVER, "login", "add", url, "--name", "Fixture Bank", "--username", USERNAME,
                                "--password-stdin"], input=PASSWORD + "\n", text=True, capture_output=True, env=env)
        if added.returncode:
            print(added.stderr)
            return 1
        async with stdio_client(StdioServerParameters(command=SERVER, env=env, cwd=str(client_cwd))) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                if "browser_login" not in names:
                    failures.append("browser_login is not listed")
                _, text = await call(session, "browser_open", {"url": url})
                tab_id = re.search(r"\btab_[0-9a-f]{32}\b", text).group(0)
                if "Fixture Bank" not in text:
                    failures.append("browser_open did not name the saved login for this site")
                await call(session, "browser_login", {"tab_id": tab_id}, "browser_login fill")
                await call(session, "browser_observe", {"tab_id": tab_id})
                _, values = await call(session, "browser_evaluate", {
                    "tab_id": tab_id,
                    "function": "() => [...document.querySelectorAll('input')].map(i => i.name + '=' + i.value)"})
                if values.count("[redacted]") < 2:
                    failures.append("evaluate did not read two filled (redacted) inputs: the fill may not have happened")
                await call(session, "browser_extract", {"tab_id": tab_id}, "browser_extract fields")
                await call(session, "browser_screenshot", {"tab_id": tab_id}, "screenshot_filled")
                await call(session, "browser_login", {"tab_id": tab_id, "submit": True}, "browser_login submit")
                await asyncio.sleep(1)
                _, text = await call(session, "browser_extract", {"tab_id": tab_id, "read": "text"},
                                     "browser_extract text")
                if "Signed in as [redacted]" not in text:
                    failures.append("the page did not say 'Signed in as [redacted]': sign-in failed or text unscrubbed")
                await call(session, "browser_extract", {"tab_id": tab_id, "read": "network"},
                           "browser_extract network")
                await call(session, "browser_screenshot", {"tab_id": tab_id}, "screenshot_signed_in")
        stray = sorted(str(p.relative_to(client_cwd)) for p in client_cwd.rglob("*"))
        if stray:
            failures.append(f"the server wrote into the client's working directory: {stray}")
    finally:
        httpd.shutdown()
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(client_cwd, ignore_errors=True)
    print("PASS" if not failures else "FAIL\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
