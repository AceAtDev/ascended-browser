"""Record the saved-login demo: an agent signs in and never sees the login.

    ASCENDED_BROWSER_BIN=.venv/bin/ascended-browser python demo/login_demo.py --out videos/

Serves a small local site ("Acme Cloud", a sign-in form and a dashboard that
prints the account email), saves a throwaway demo login with
`ascended-browser login add` in the recording's own data directory, and has
Claude Code sign in with browser_login and take a screenshot. The transcript
panel shows what the agent got back ([redacted]) and the screenshot print is
the masked picture the tool returned.
"""
from __future__ import annotations

import argparse
import http.server
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs

PORT = 8765
USERNAME = "ada.tester@example.org"
PASSWORD = "Acme-demo-Pa55!"  # a throwaway value for this local fixture only
STYLE = """<style>
*{box-sizing:border-box} body{margin:0;font:16px/1.5 system-ui,sans-serif;background:#f4f5fb;color:#1b1d29}
header{background:#1b1d29;color:#fff;padding:14px 32px;font-weight:700;letter-spacing:.3px}
.card{max-width:420px;margin:72px auto;background:#fff;border-radius:14px;padding:32px;box-shadow:0 8px 28px #1b1d2920}
h1{margin:0 0 20px;font-size:26px} label{display:block;margin:14px 0 6px;font-weight:600;font-size:14px}
input{width:100%;padding:11px 12px;font-size:16px;border:1px solid #c9ccda;border-radius:8px}
button{margin-top:22px;width:100%;padding:12px;font-size:16px;font-weight:700;color:#fff;background:#4f46e5;border:0;border-radius:8px}
.wide{max-width:720px} .row{display:flex;justify-content:space-between;padding:12px 0;border-bottom:1px solid #eceef5}
.muted{color:#6b6f85}
</style>"""
SIGN_IN = f"""<!doctype html><html><head><title>Sign in · Acme Cloud</title>{STYLE}</head><body>
<header>Acme Cloud</header>
<form class="card" method="post" action="/session">
  <h1>Sign in</h1>
  <label for="email">Email</label><input id="email" type="email" name="email" autocomplete="username">
  <label for="password">Password</label><input id="password" type="password" name="password" autocomplete="current-password">
  <button type="submit">Sign in</button>
</form></body></html>"""
DASHBOARD = """<!doctype html><html><head><title>Dashboard · Acme Cloud</title>{style}</head><body>
<header>Acme Cloud</header>
<div class="card wide"><h1>Welcome back</h1>
<p class="muted">Signed in as <b>{email}</b></p>
<div class="row"><span>Projects</span><b>3</b></div>
<div class="row"><span>Plan</span><b>Team</b></div>
<div class="row"><span>Account email</span><b>{email}</b></div>
</div></body></html>"""


class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        cookie = self.headers.get("Cookie") or ""
        if self.path.startswith("/dashboard") and "acme=1" in cookie:
            return self._page(DASHBOARD.format(style=STYLE, email=USERNAME))
        return self._page(SIGN_IN)

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
        if form.get("email", [""])[0] == USERNAME and form.get("password", [""])[0] == PASSWORD:
            self.send_response(303)
            self.send_header("Set-Cookie", "acme=1; Path=/")
            self.send_header("Location", "/dashboard")
            self.end_headers()
            return
        self._page(SIGN_IN.replace("<h1>Sign in</h1>", "<h1>Sign in</h1><p style='color:#c0263b'>Wrong email or password.</p>"))

    def _page(self, body: str):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, default=Path("videos"))
    parser.add_argument("--model", default="")
    parser.add_argument("--recompose", action="store_true")
    args, rest = parser.parse_known_args()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    binary = os.environ.get("ASCENDED_BROWSER_BIN", "ascended-browser")
    setup = (f"printf '%s\\n' {shlex.quote(PASSWORD)} | {shlex.quote(binary)} login add http://127.0.0.1:{PORT} "
             f"--name 'Acme Cloud' --username {USERNAME} --password-stdin")
    task = (f"Sign in to my Acme Cloud account at http://127.0.0.1:{PORT}/ with my saved login: fill it "
            "first and take a screenshot of the filled form, then submit it and take a screenshot of the "
            "dashboard. End your reply with one line: ANSWER: <what you could see of my email and "
            "password, in under 10 words>.")
    command = [sys.executable, str(Path(__file__).with_name("record_demo.py")), "--name", "login",
               "--out", str(args.out), "--task", task, "--setup", setup, "--pace", "1.6", *rest]
    if args.model:
        command += ["--model", args.model]
    if args.recompose:
        command.append("--recompose")
    try:
        sys.exit(subprocess.run(command).returncode)
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
