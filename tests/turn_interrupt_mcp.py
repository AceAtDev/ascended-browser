"""A turn that ends while the browser is mid-wait: interrupt, process exit, SSH hangup.

    python tests/turn_interrupt_mcp.py .venv/bin/ascended-browser

A local app whose "Place order" click takes 6 s on its server (server-side
state: an order counter). The agent clicks and waits for "Order placed"; once
the app has the order request and the page is waiting, the turn ends one of
four ways:

  A1 interrupt, client sends notifications/cancelled (server aborts the call)
  A2 interrupt, client just stops waiting (server finishes, result discarded)
  B  SIGTERM to the server (how `claude -p` ends its MCP servers)
  C  SIGHUP to the server's process group (an SSH session dropping)

Checks: the next call answers promptly on the same tab (A), or a new server
starts cleanly on the same data directory (B, C); the next turn is told, once,
that the click may already have taken effect (A1, B, C); no browser or Xvfb
process outlives the server; no journal file is left. No DISPLAY is passed, as
on a remote box. Fails on any check.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

BIN = sys.argv[1]
ORDERS = {"n": 0, "inflight": 0}
PAGE = """<!doctype html><title>Shop</title><h1>Checkout</h1>
<p>Orders on record: <b id=count>{n}</b></p>
<button id=buy>Place order</button><p id=state>Ready</p>
<script>
const el = id => document.getElementById(id);
el('buy').onclick = async () => {{
  el('state').textContent = 'Placing order...'; el('buy').disabled = true;
  const r = await fetch('/order', {{method: 'POST'}}); const j = await r.json();
  el('state').textContent = 'Order placed #' + j.n; el('count').textContent = j.n; el('buy').disabled = false;
}};
</script>"""


class App(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self._send("text/html", PAGE.format(n=ORDERS["n"]).encode())

    def do_POST(self):
        ORDERS["inflight"] += 1
        time.sleep(6)
        ORDERS["n"] += 1
        self._send("application/json", json.dumps({"n": ORDERS["n"]}).encode())

    def _send(self, kind, data):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def status_of(t: str) -> str:
    found = re.findall(r"(Order placed #\d+|Placing order\.\.\.|Ready)", t)
    return found[-1] if found else "?"


DUMP: list = []


def keep(label: str, t: str) -> str:
    DUMP.append((label, t))
    return t


async def until_inflight(target: int, limit: float = 15.0) -> float:
    """Wait for the app to receive order request number target: the click is now mid-flight."""
    started = time.monotonic()
    while ORDERS["inflight"] < target and time.monotonic() - started < limit:
        await asyncio.sleep(0.05)
    return round(time.monotonic() - started, 1)


def procs() -> dict[int, str]:
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout.splitlines()[1:]
    return {int(l.split(None, 1)[0]): l.split(None, 1)[1] for l in out if l.strip()}


def survivors(before: dict[int, str], data: Path) -> list[str]:
    now = procs()
    left = []
    for pid, args in now.items():
        if pid in before:
            continue
        if str(data) in args or (args.startswith("Xvfb") or "/Xvfb " in args):
            left.append(f"{pid} {args[:110]}")
    return left


async def open_and_click(session, url):
    opened = text(await session.call_tool("browser_open", {"url": url}))
    tab = re.search(r"\btab_[0-9a-f]{32}\b", opened).group(0)
    ref = re.search(r'"ref": "(e[0-9a-f]+)", "role": "button", "name": "Place order"', opened)
    if not ref:
        ref = re.search(r'"ref": "(e[0-9a-f]+)"[^}]*"Place order"', opened)
    return tab, ref.group(1)


def click_args(tab, ref):
    return {"tab_id": tab, "action": {"kind": "click", "ref": ref,
                                      "until": {"text": "Order placed", "timeout_ms": 20000}}}


def env_for(data: Path) -> dict:
    return {"ASCENDED_DATA_DIR": str(data), "PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}


async def scenario_interrupt(url: str, data: Path, send_cancel: bool) -> dict:
    ORDERS["n"] = 0
    report: dict = {}
    async with stdio_client(StdioServerParameters(command=BIN, env=env_for(data))) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tab, ref = await open_and_click(s, url)
            request_id = s._request_id  # the id the next request will carry
            ORDERS["inflight"] = 0
            click = asyncio.ensure_future(s.call_tool("browser_act", click_args(tab, ref)))
            report["interrupted_after_s"] = await until_inflight(1) + 0.3
            await asyncio.sleep(0.3)  # mid-wait: the request is at the app, the page is waiting
            click.cancel()
            if send_cancel:
                await s.send_notification(types.ClientNotification(types.CancelledNotification(
                    params=types.CancelledNotificationParams(requestId=request_id, reason="user interrupted"))))
            report["orders_at_interrupt"] = ORDERS["n"]
            # The next turn: look at the tab.
            started = time.monotonic()
            observed = await asyncio.wait_for(s.call_tool("browser_observe", {"tab_id": tab}), 60)
            keep("next_turn_observe", text(observed))
            report["next_call_s"] = round(time.monotonic() - started, 1)
            report["next_call_error"] = observed.isError
            report["next_turn_sees"] = status_of(text(observed))
            report["next_turn_warned"] = ("interrupted before its result came back" in text(observed)
                                          and "click on 'Place order'" in text(observed))
            await asyncio.sleep(7)
            later = keep("after_7s", text(await s.call_tool("browser_observe", {"tab_id": tab})))
            report["after_7s_sees"] = status_of(later)
            report["warned_again"] = "interrupted before its result came back" in later
            report["orders_after"] = ORDERS["n"]
            # A next turn that does not know and simply clicks again:
            started = time.monotonic()
            again = await asyncio.wait_for(s.call_tool("browser_act", click_args(tab, ref)), 60)
            report["reclick_s"] = round(time.monotonic() - started, 1)
            report["reclick_sees"] = status_of(keep("reclick", text(again)))
            report["orders_after_reclick"] = ORDERS["n"]
    return report


async def scenario_kill(url: str, data: Path, how: str) -> dict:
    ORDERS["n"] = 0
    report: dict = {}
    pidfile = data / "server.pid"
    data.mkdir(parents=True, exist_ok=True)
    before = procs()
    params = StdioServerParameters(command="setsid", args=["sh", "-c", f'echo $$ > {pidfile}; exec {BIN}'],
                                   env=env_for(data))
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tab, ref = await open_and_click(s, url)
                pid = int(pidfile.read_text())
                ORDERS["inflight"] = 0
                click = asyncio.ensure_future(s.call_tool("browser_act", click_args(tab, ref)))
                report["killed_after_s"] = await until_inflight(1) + 0.3
                await asyncio.sleep(0.3)
                if how == "SIGTERM":
                    os.kill(pid, signal.SIGTERM)
                else:
                    os.killpg(pid, signal.SIGHUP)
                report["orders_at_kill"] = ORDERS["n"]
                raise SystemExit  # leave the client; the server is gone
    except (SystemExit, Exception, BaseExceptionGroup):
        pass
    await asyncio.sleep(10)
    report["server_alive_10s_later"] = Path(f"/proc/{int(pidfile.read_text())}").exists()
    report["left_running_10s_later"] = survivors(before, data)
    report["orders_after"] = ORDERS["n"]
    # The next turn: a fresh server on the same data directory.
    started = time.monotonic()
    async with stdio_client(StdioServerParameters(command=BIN, env=env_for(data))) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            opened = await asyncio.wait_for(s.call_tool("browser_open", {"url": url}), 120)
            t = keep(f"restart_{data.name}", text(opened))
            report["restart_open_s"] = round(time.monotonic() - started, 1)
            report["restart_error"] = opened.isError
            report["restart_on_copy"] = "runs on a copy" in t
            m = re.search(r"Orders on record:?\D{0,60}?(\d+)", t)
            report["next_turn_sees_orders"] = m.group(1) if m else "?"
            report["restart_warned"] = ("An earlier browser session ended" in t
                                        and "click on 'Place order'" in t and url in t)
            again = text(await s.call_tool("browser_observe", {"tab_id": re.search(r"\btab_[0-9a-f]{32}\b", t).group(0)}))
            report["warned_again"] = "An earlier browser session ended" in again
    report["journal_files_left"] = sorted(p.name for p in (data / "inflight").glob("*.json"))
    for pid_line in survivors(before, data):  # tidy anything this scenario leaked
        try:
            os.kill(int(pid_line.split()[0]), signal.SIGKILL)
        except OSError:
            pass
    return report


async def main() -> None:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), App)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    root = Path(tempfile.mkdtemp(prefix="ab-turn-", dir="/var/tmp"))
    results = {}
    for name, coro in (
        ("A1 interrupt + cancel notice", lambda: scenario_interrupt(url, root / "a1", True)),
        ("A2 interrupt, client stops waiting", lambda: scenario_interrupt(url, root / "a2", False)),
        ("B  SIGTERM (claude -p exit)", lambda: scenario_kill(url, root / "b", "SIGTERM")),
        ("C  SIGHUP to group (SSH drop)", lambda: scenario_kill(url, root / "c", "SIGHUP")),
    ):
        try:
            results[name] = await coro()
        except Exception as exc:
            results[name] = {"HARNESS_ERROR": f"{type(exc).__name__}: {exc}"}
        print(name, json.dumps(results[name], indent=1), flush=True)
    httpd.shutdown()
    failures = []
    for name, r in results.items():
        def need(ok, what):
            if not ok:
                failures.append(f"{name}: {what}")
        need("HARNESS_ERROR" not in r, r.get("HARNESS_ERROR", ""))
        if name.startswith("A"):
            need(not r.get("next_call_error"), "the next call failed")
            need(r.get("next_call_s", 99) < 10, f"the next call took {r.get('next_call_s')} s")
            need(r.get("after_7s_sees", "").startswith("Order placed"), "the page never showed the order")
            need(not r.get("warned_again"), "the warning was repeated")
        if name.startswith("A1"):
            need(r.get("next_turn_warned"), "the next turn was not told the click was interrupted")
        if name[0] in "BC":
            need(not r.get("server_alive_10s_later"), "the server outlived the signal")
            need(not r.get("left_running_10s_later"), f"left running: {r.get('left_running_10s_later')}")
            need(not r.get("restart_error") and not r.get("restart_on_copy"), "the next server did not start cleanly")
            need(r.get("restart_warned"), "the next session was not told about the unfinished click")
            need(not r.get("warned_again"), "the warning was repeated")
            need(not r.get("journal_files_left"), f"journal files left: {r.get('journal_files_left')}")
    shutil.rmtree(root, ignore_errors=True)
    print("PASS" if not failures else "FAIL\n  " + "\n  ".join(failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
