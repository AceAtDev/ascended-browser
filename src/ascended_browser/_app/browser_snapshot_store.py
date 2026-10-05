"""Session-scoped browser snapshot store for listing vs detail comparison.

One browser MCP process serves every chat, so labels are kept per chat: two
sessions saving "listing" used to overwrite each other, and a compare could
diff one chat's page against another's. The chat id arrives as MCP request
metadata (``odysseus_session_id``); calls without one share the "" scope.
"""

from __future__ import annotations

import json
import re
from typing import Any

_MAX_SNAPSHOTS = 20  # per chat
_MAX_SESSIONS = 200  # chats with saved snapshots, least recently saved evicted first
_store: dict[str, dict[str, dict[str, Any]]] = {}


def _labels(session: str, *, create: bool = False) -> dict[str, dict[str, Any]] | None:
    key = str(session or "")
    labels = _store.get(key)
    if labels is None and create:
        while len(_store) >= _MAX_SESSIONS:
            del _store[next(iter(_store))]
        labels = _store[key] = {}
    elif labels is not None and create:
        _store[key] = _store.pop(key)  # most recently used chat last
    return labels


def save_snapshot(label: str, *, url: str = "", title: str = "", content: str = "", session: str = "") -> str:
    """Save a labeled page snapshot (evaluate result or snapshot text)."""
    key = (label or "").strip()
    if not key:
        return "Error: label is required"
    labels = _labels(session, create=True)
    if key not in labels:
        while len(labels) >= _MAX_SNAPSHOTS:
            del labels[next(iter(labels))]
    labels[key] = {
        "url": (url or "").strip(),
        "title": (title or "").strip(),
        "content": (content or "")[:50000],
    }
    return f"Saved snapshot '{key}' ({len(content)} chars, url={url or 'n/a'})"


def list_snapshots(session: str = "") -> list[str]:
    return list(_labels(session) or {})


def get_snapshot(label: str, session: str = "") -> dict[str, Any] | None:
    return (_labels(session) or {}).get((label or "").strip())


def _try_parse_json(content: str) -> Any | None:
    text = (content or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _diff_dicts(a: dict, b: dict) -> list[str]:
    lines: list[str] = []
    all_keys = sorted(set(a.keys()) | set(b.keys()))
    for key in all_keys:
        va = a.get(key)
        vb = b.get(key)
        if va == vb:
            continue
        lines.append(f"- **{key}**: `{va}` → `{vb}`")
    return lines


def compare_snapshots(label_a: str, label_b: str, session: str = "") -> str:
    """Return side-by-side diff of two saved snapshots."""
    a = get_snapshot(label_a, session)
    b = get_snapshot(label_b, session)
    saved = ', '.join(list_snapshots(session)) or '(none)'
    if a is None:
        return f"Error: snapshot '{label_a}' not found. Saved: {saved}"
    if b is None:
        return f"Error: snapshot '{label_b}' not found. Saved: {saved}"

    lines = [
        f"## Snapshot compare: `{label_a}` vs `{label_b}`",
        "",
        f"| | {label_a} | {label_b} |",
        f"|---|---|---|",
        f"| URL | {a.get('url', '')} | {b.get('url', '')} |",
        f"| Title | {a.get('title', '')} | {b.get('title', '')} |",
        "",
    ]

    json_a = _try_parse_json(a.get("content", ""))
    json_b = _try_parse_json(b.get("content", ""))

    if isinstance(json_a, dict) and isinstance(json_b, dict):
        diffs = _diff_dicts(json_a, json_b)
        if diffs:
            lines.append("### Field differences")
            lines.extend(diffs)
        else:
            lines.append("No field differences in JSON objects.")
    elif isinstance(json_a, list) and isinstance(json_b, list):
        lines.append(f"List lengths: {len(json_a)} vs {len(json_b)}")
    else:
        text_a = re.sub(r"\s+", " ", (a.get("content") or ""))[:2000]
        text_b = re.sub(r"\s+", " ", (b.get("content") or ""))[:2000]
        if text_a == text_b:
            lines.append("Text content matches (within preview window).")
        else:
            lines.append(f"**{label_a}** preview: {text_a[:500]}")
            lines.append("")
            lines.append(f"**{label_b}** preview: {text_b[:500]}")

    return "\n".join(lines)
