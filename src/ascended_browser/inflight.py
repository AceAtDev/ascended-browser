"""Actions that never reported back: the next turn is told, so it does not repeat them blind.

A click whose call is interrupted (the user stops the turn) or whose server
dies (``claude -p`` ending its servers, an SSH session dropping) may already
have reached the site: an order placed, a form sent. The agent never got the
result, so the next turn would see a page it cannot explain, or click again.

``Journal`` records each state-changing call (browser_act other than waits and
scrolls, browser_login, a browser_flow run) when it starts and clears it when
its result comes back:

- interrupted (the client cancels the call): a note waits on that tab and is
  put before the next result for it; it stays in the journal file until then,
  since a client that hangs up cancels its calls just before the server exits;
- the process dies: the entry stays in ``<data>/inflight/<pid>.json``; the
  next server finds files whose process is gone and puts the list before its
  first result.

The note names the action, the element and the page from what the agent was
already shown, never typed values.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

PASSIVE_KINDS = {"wait", "scroll"}
_REF_NAME = re.compile(r'"ref": "(e[0-9a-f]+)"[^{}]*?"(?:name|text|label)": "([^"]{1,80})"')
_PAGE_URL = re.compile(r"\*\*page:\*\* [^\n]*?(https?://[^\s·]+)")
_MAX_LABELS = 400


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Journal:
    def __init__(self, root: Path) -> None:
        self.folder = root / "inflight"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.file = self.folder / f"{os.getpid()}.json"
        self.running: dict[int, dict] = {}
        self.waiting: dict[str, list[dict]] = {}   # tab_id -> interrupted actions not yet told
        self.urls: dict[str, str] = {}             # tab_id -> last page URL the agent was shown
        self.labels: dict[str, str] = {}           # ref -> element name the agent was shown
        self._next = 0

    # ------------------------------------------------------------ earlier sessions
    def recover(self) -> list[dict]:
        """Unfinished actions of servers that are gone; their files are removed."""
        found = []
        for path in self.folder.glob("*.json"):
            try:
                pid = int(path.stem)
            except ValueError:
                continue
            if pid == os.getpid() or _alive(pid):
                continue
            try:
                found.extend(json.loads(path.read_text()) or [])
            except (OSError, ValueError):
                pass
            path.unlink(missing_ok=True)
        return sorted(found, key=lambda e: e.get("started", 0))

    # ------------------------------------------------------------ this session
    def learn(self, tab_id: str, text: str) -> None:
        """What the agent was just shown: the tab's page and the names of its elements."""
        url = _PAGE_URL.search(text or "")
        if tab_id and url:
            self.urls[tab_id] = url.group(1)
        for ref, name in _REF_NAME.findall(text or ""):
            self.labels[ref] = name
        if len(self.labels) > _MAX_LABELS:
            for ref in list(self.labels)[: len(self.labels) - _MAX_LABELS]:
                del self.labels[ref]

    def begin(self, tool: str, args: dict) -> int | None:
        what = self.describe(tool, args)
        if what is None:
            return None
        self._next += 1
        tab_id = str(args.get("tab_id") or "")
        self.running[self._next] = {"tool": tool, "what": what, "tab_id": tab_id,
                                    "url": self.urls.get(tab_id, ""), "started": time.time()}
        self._save()
        return self._next

    def end(self, key: int | None) -> None:
        if key is not None and self.running.pop(key, None) is not None:
            self._save()

    def interrupted(self, key: int | None) -> None:
        entry = self.running.pop(key, None) if key is not None else None
        if entry is None:
            return
        entry["stopped_after_s"] = round(time.time() - entry["started"], 1)
        self.waiting.setdefault(entry["tab_id"], []).append(entry)
        self._save()

    def notes_for(self, tab_id: str) -> list[dict]:
        """Interrupted actions to tell about with this result (all of them when it names no tab)."""
        if tab_id:
            told = self.waiting.pop(tab_id, [])
        else:
            told = [entry for entries in self.waiting.values() for entry in entries]
            self.waiting.clear()
        if told:
            self._save()
        return told

    def describe(self, tool: str, args: dict) -> str | None:
        """A short description of a state-changing call, or None for a read."""
        if tool == "browser_login":
            return "sign-in with the saved login" + (" (submitted)" if args.get("submit") else "")
        if tool == "browser_flow":
            if str(args.get("action") or "") != "run":
                return None
            return f"run of saved flow {args.get('flow_id') or args.get('name') or ''}".rstrip()
        if tool != "browser_act":
            return None
        action = args.get("action") if isinstance(args.get("action"), dict) else {}
        kind = str(action.get("kind") or "")
        if kind == "sequence":
            steps = [s for s in action.get("steps") or [] if isinstance(s, dict)]
            active = [self._step(s) for s in steps if str(s.get("kind") or "") not in PASSIVE_KINDS]
            return f"sequence ({', '.join(active)})" if active else None
        if not kind or kind in PASSIVE_KINDS:
            return None
        return self._step(action)

    def _step(self, step: dict) -> str:
        kind = str(step.get("kind") or "action")
        if kind == "navigate":
            return f"navigate to {step.get('url') or ''}".rstrip()
        if kind == "press":
            return f"press {step.get('key') or ''}".rstrip()
        if kind == "fill_form":
            return "fill_form"
        ref = str(step.get("ref") or "")
        name = self.labels.get(ref)
        return f"{kind} on '{name}'" if name else (f"{kind} on {ref}" if ref else kind)

    def _save(self) -> None:
        untold = list(self.running.values()) + [e for entries in self.waiting.values() for e in entries]
        try:
            if untold:
                tmp = self.file.with_suffix(".tmp")
                tmp.write_text(json.dumps(untold))
                tmp.replace(self.file)
            else:
                self.file.unlink(missing_ok=True)
        except OSError:
            pass


def interrupted_note(entries: list[dict]) -> str:
    lines = [f"- {e['what']}" + (f" on {e['url']}" if e.get("url") else "")
             + f", stopped {e.get('stopped_after_s', 0):g} s after it started" for e in entries]
    return ("An earlier action on this tab was interrupted before its result came back, so it may "
            "already have taken effect (a form sent, an order placed):\n" + "\n".join(lines)
            + "\nCheck the page before repeating it.")


def recovered_note(entries: list[dict]) -> str:
    lines = [f"- {e['what']}" + (f" on {e['url']}" if e.get("url") else "")
             + f", started {time.strftime('%H:%M:%S', time.localtime(e.get('started', 0)))}" for e in entries]
    return ("An earlier browser session ended while these actions were still running, so each may "
            "already have taken effect (a form sent, an order placed):\n" + "\n".join(lines)
            + "\nCheck the site before repeating them.")
