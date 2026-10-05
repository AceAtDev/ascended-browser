"""Give real agent CLIs live-website tasks with only ascended-browser attached.

    python tests/agents/agent_tasks.py --agent claude --tasks all
    python tests/agents/agent_tasks.py --agent codex --tasks login
    python tests/agents/agent_tasks.py --agent opencode --tasks select --model opencode-go/glm-5

Each run uses a fresh working directory and data directory, attaches only
this MCP server, and records the agent's tool calls and final answer to
runs/agents/. A task passes when the answer contains what the live site
actually says (checked against a known value, or fetched independently).
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs" / "agents"
SERVER = os.environ.get("ASCENDED_BROWSER_BIN") or shutil.which("ascended-browser") or "ascended-browser"


def _hn_top_title() -> str:
    import httpx

    html = httpx.get("https://news.ycombinator.com/", timeout=20).text
    match = re.search(r'<span class="titleline"><a [^>]*>([^<]+)</a>', html)
    return html_lib.unescape(match.group(1)).strip() if match else ""


@dataclass
class Task:
    name: str
    prompt: str
    expected: Callable[[], list[str]]


TASKS = {
    "nav": Task(
        "nav",
        "Use the browser tools. Open https://news.ycombinator.com and tell me the exact title of the "
        "top story. Reply with the title on the last line, prefixed 'ANSWER: '.",
        lambda: [_hn_top_title()],
    ),
    "login": Task(
        "login",
        "Use the browser tools. Go to https://the-internet.herokuapp.com/login, sign in with username "
        "tomsmith and password SuperSecretPassword!, and tell me the confirmation message the site "
        "shows. Reply with the message on the last line, prefixed 'ANSWER: '.",
        lambda: ["You logged into a secure area!"],
    ),
    "select": Task(
        "select",
        "Use the browser tools. On https://demoqa.com/select-menu, choose 'Group 2, option 1' in the "
        "'Select Value' dropdown and 'Mrs.' in the 'Select One' dropdown (both are custom React "
        "dropdowns), then tell me what both dropdowns now show. Reply on the last line as "
        "'ANSWER: <first> | <second>'.",
        lambda: ["Group 2, option 1", "Mrs."],
    ),
    "shop": Task(
        "shop",
        "Use the browser tools. On https://www.saucedemo.com sign in as standard_user with password "
        "secret_sauce, add the cheapest product to the cart, go through checkout with first name "
        "Ada, last name Lovelace and postal code 12345 up to the overview page (do not finish the "
        "order), and tell me the Total shown there. Reply on the last line as 'ANSWER: <total>'.",
        lambda: ["$8.63"],
    ),
}


def _env(work: Path) -> dict:
    """The agent's environment, with PWD matching its working directory.

    opencode resolves its project from $PWD, not the process working
    directory: with a stale PWD it silently loads another directory's config
    (no MCP server, default permissions).
    """
    return {**os.environ, "PWD": str(work)}


def _mcp_json(data: Path) -> dict:
    return {"command": SERVER, "args": [], "env": {"ASCENDED_DATA_DIR": str(data)}}


def run_claude(task: Task, work: Path, data: Path, model: str) -> tuple[str, list[str], str]:
    config = work / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"ascended-browser": _mcp_json(data)}}))
    cmd = ["claude", "-p", task.prompt, "--mcp-config", str(config), "--strict-mcp-config",
           "--allowedTools", "mcp__ascended-browser", "--output-format", "stream-json", "--verbose"]
    if model:
        cmd += ["--model", model]
    out = subprocess.run(cmd, cwd=work, env=_env(work), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900).stdout
    calls, answer = [], ""
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        for part in (event.get("message") or {}).get("content") or []:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                calls.append(f"{part.get('name', '').split('__')[-1]} {json.dumps(part.get('input'))[:160]}")
        if event.get("type") == "result":
            answer = str(event.get("result") or "")
    return answer, calls, out


def run_codex(task: Task, work: Path, data: Path, model: str) -> tuple[str, list[str], str]:
    server = _mcp_json(data)
    cmd = ["codex", "exec", "--skip-git-repo-check", "--json",
           "-c", f'mcp_servers.ascended-browser.command="{server["command"]}"',
           "-c", f'mcp_servers.ascended-browser.env={{ASCENDED_DATA_DIR="{data}"}}',
           "-c", "mcp_servers.ascended-browser.tool_timeout_sec=240",
           # exec never asks, so the server's tools must be pre-approved.
           "-c", 'mcp_servers.ascended-browser.default_tools_approval_mode="approve"']
    if model:
        cmd += ["-m", model]
    # A private CODEX_HOME so only this server is attached (no user MCP servers
    # or plugins); the login is linked, never copied.
    home = work / ".codex-home"
    home.mkdir()
    real = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if (real / "auth.json").exists():
        (home / "auth.json").symlink_to(real / "auth.json")
    env = {**_env(work), "CODEX_HOME": str(home)}
    out = subprocess.run(cmd + [task.prompt], cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, timeout=900).stdout
    calls, answer = [], ""
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        if item.get("type") == "mcp_tool_call" and event.get("type") == "item.started":
            calls.append(f"{item.get('server')}.{item.get('tool')} {json.dumps(item.get('arguments'))[:160]}")
        if item.get("type") == "agent_message" and event.get("type") == "item.completed":
            answer = str(item.get("text") or "")
    return answer, calls, out


def run_opencode(task: Task, work: Path, data: Path, model: str) -> tuple[str, list[str], str]:
    server = _mcp_json(data)
    (work / "opencode.json").write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"ascended-browser": {"type": "local", "command": [server["command"]],
                                     "environment": server["env"], "enabled": True}},
        # The test measures the MCP: built-in substitutes are off (left on, a
        # model may curl the site or pip-install its own Playwright instead).
        "permission": {"bash": "deny", "webfetch": "deny", "edit": "deny"},
    }))
    cmd = ["opencode", "run", "--format", "json"] + (["-m", model] if model else []) + [task.prompt]
    out = subprocess.run(cmd, cwd=work, env=_env(work), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900).stdout
    calls, texts = [], []
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part") or {}
        if part.get("type") == "tool" and (part.get("state") or {}).get("status") == "completed":
            calls.append(f"{part.get('tool')} {json.dumps((part.get('state') or {}).get('input'))[:160]}")
        if part.get("type") == "text" and part.get("text"):
            texts.append(part["text"])
    return (texts[-1] if texts else ""), calls, out


AGENTS = {"claude": run_claude, "codex": run_codex, "opencode": run_opencode}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--agent", choices=sorted(AGENTS), required=True)
    parser.add_argument("--tasks", default="all", help="comma-separated: " + ",".join(TASKS))
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    names = list(TASKS) if args.tasks == "all" else [n.strip() for n in args.tasks.split(",")]
    RUNS.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name in names:
        task = TASKS[name]
        work = Path(tempfile.mkdtemp(prefix=f"ab-{args.agent}-{name}-"))
        data = work / "data"
        started = time.monotonic()
        try:
            answer, calls, raw = AGENTS[args.agent](task, work, data, args.model)
        except subprocess.TimeoutExpired:
            answer, calls, raw = "", [], "timeout"
        seconds = round(time.monotonic() - started, 1)
        final = answer.strip().splitlines()[-1] if answer.strip() else ""
        expected = [e for e in task.expected() if e]
        ok = bool(expected) and all(e.casefold() in final.casefold() for e in expected)
        failed += not ok
        record = {"agent": args.agent, "task": name, "model": args.model, "ok": ok, "seconds": seconds,
                  "expected": expected, "answer": answer[-2000:], "tool_calls": calls}
        (RUNS / f"{args.agent}-{name}.json").write_text(json.dumps(record, indent=1))
        (RUNS / f"{args.agent}-{name}.raw.jsonl").write_text(raw)
        print(f"{'PASS' if ok else 'FAIL'} {args.agent:<8} {name:<7} {seconds:6.1f}s {len(calls):>3} calls  "
              f"answer={final[:90]!r}  expected={expected}")
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
