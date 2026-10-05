"""Record verified browser actions as a reusable flow, and plan its replay.

Ported in spirit from browser-use's history rerun (``Agent.rerun_history`` and
``_update_action_indices`` in ``browser_use/agent/service.py``, MIT, commit
d8110c5): after a task succeeds once, the same steps run again against the
live page with no model call, re-finding each element by what it was rather
than where it was, and swapping in new values for the fields that change.

The Ascended shape differs where its own contracts already decide:

- Only actions whose effect was verified are journaled, so a flow never
  carries the original run's failed attempts (upstream records everything
  and skips errored steps on replay).
- Targets are semantic identities (role, label, id, name, context, frame),
  resolved on replay by the same unique-match rule as stale-ref recovery
  (``browser_semantic_target.resolve_semantic_target``); an ambiguous or
  missing target stops the run rather than guessing.
- Secrets are never recorded: a password, one-time-code or card field is
  left out, and the flow says so.

The journal lives with the browser workspace (memory, per tab); saved flows
are per owner on disk so they outlive the chat that made them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ascended_browser._app.browser_sensitive_state import names_a_secret
from ascended_browser._app.browser_flow_variables import (
    detect_from_attributes, detect_variable_type, ensure_unique_name, target_attributes,
)

JOURNAL_LIMIT = 300
MAX_FLOWS_PER_OWNER = 100
MAX_FLOW_STEPS = 200

# What each recorded kind carries besides its target.
_PARAM_KEYS: dict[str, tuple[str, ...]] = {
    "navigate": ("url",),
    "click": ("expect", "button", "click_count", "modifiers"),
    "fill": ("value", "clear", "submit"),
    "select": ("option", "options", "query", "value"),
    "check": ("checked",),
    "date": ("value",),
    "press": ("key",),
    "upload": ("paths",),
}
RECORDED_KINDS = frozenset(_PARAM_KEYS)
_TARGET_KINDS = frozenset({"click", "fill", "select", "check", "date", "upload", "press"})
_MENU_ITEM_ROLES = frozenset({"menuitem", "menuitemcheckbox", "menuitemradio", "option", "treeitem"})
_GENERIC_VARIABLES = frozenset({"number", "name", "date", "comment", "url"})
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


def page_route(url: str) -> str:
    """host/path — the page a step ran on, without query or fragment."""
    parsed = urlparse(str(url or ""))
    if not parsed.hostname:
        return ""
    return f"{parsed.hostname.lower()}{parsed.path or '/'}"


def is_sensitive_target(target: dict[str, Any] | None) -> bool:
    """A field whose value must never be recorded.

    The collector's flag covers type and autocomplete; a plain text input
    named "cvv" or "ssn" has neither, so its names are checked too.
    """
    if not isinstance(target, dict):
        return False
    if target.get("sensitive") or str(target.get("type") or "").lower() in {"password", "hidden"}:
        return True
    return names_a_secret(*(target.get(key) for key in (
        "label", "aria_label", "name", "placeholder", "element_id", "name_attr",
    )))


def recordable(kind: str, action: dict[str, Any]) -> bool:
    """Actions that move a flow forward. Reads and listing are not steps."""
    if kind not in RECORDED_KINDS:
        return False
    if kind == "select" and action.get("list_options"):
        return False
    return True


class FlowJournal:
    """Verified actions per tab, oldest first, bounded."""

    def __init__(self, limit: int = JOURNAL_LIMIT) -> None:
        self._limit = limit
        self._entries: dict[tuple[str, str], deque] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def record(self, workspace_id: str, tab_id: str, entry: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            log = self._entries.setdefault((workspace_id, tab_id), deque(maxlen=self._limit))
            log.append({**entry, "seq": self._seq, "at": time.time()})

    def entries(self, workspace_id: str, tab_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._entries.get((workspace_id, tab_id), ())]

    def clear(self, workspace_id: str, tab_id: str) -> None:
        with self._lock:
            self._entries.pop((workspace_id, tab_id), None)


def journal_entry(kind: str, action: dict[str, Any], *, target: dict[str, Any] | None,
                  url: str, source: str = "agent") -> dict[str, Any] | None:
    """One verified action as a journal entry, or None when it must not be kept."""
    if not recordable(kind, action):
        return None
    if kind in _TARGET_KINDS and kind != "press" and not target:
        return None
    if is_sensitive_target(target):
        return {"kind": kind, "sensitive": True, "url": url, "source": source,
                "label": str((target or {}).get("label") or (target or {}).get("name") or "")[:120]}
    params = {key: action[key] for key in _PARAM_KEYS[kind] if key in action and action[key] not in (None, "")}
    entry = {"kind": kind, "url": url, "params": params, "source": source}
    if target:
        # The frame's URL is the page it was recorded on (job 101); on the
        # next run it is another page, and the matcher treats a differing
        # frame_url as a different element. A frame's name is stable.
        entry["target"] = {
            key: value for key, value in target.items()
            if value not in (None, "", [], {}) and key != "frame_url"
        }
    return entry


# What can tell one control from another of its kind. Role, tag and type
# cannot: a flow recorded "the file input" uploaded a work sample into the
# resume field on replay, because that was the one visible file input.
_NAME_KEYS = ("label", "aria_label", "name", "placeholder", "text", "element_id",
              "name_attr", "context", "nearby")


def has_identity(target: dict[str, Any] | None) -> bool:
    return any(str((target or {}).get(key) or "").strip() for key in _NAME_KEYS)


def _field_identity(target: dict[str, Any]) -> tuple:
    return tuple(str(target.get(key) or "") for key in (
        "element_id", "name_attr", "label", "aria_label", "placeholder", "context", "nearby",
    ))


def _step_label(target: dict[str, Any]) -> str:
    for key in ("label", "aria_label", "name", "placeholder", "text", "nearby"):
        value = " ".join(str(target.get(key) or "").split())
        if value:
            return value[:160]
    return ""


def compile_flow(entries: list[dict[str, Any]], *, name: str = "") -> dict[str, Any]:
    """Turn journal entries into a flow: steps, start URL and variables."""
    entries = [entry for entry in entries if isinstance(entry, dict)]
    if not entries:
        raise ValueError("Nothing to save: this tab has no verified actions yet.")
    skipped_sensitive = [entry.get("label") or "a sensitive field" for entry in entries if entry.get("sensitive")]
    entries = [entry for entry in entries if not entry.get("sensitive")]
    if not entries:
        raise ValueError("Nothing to save: every verified action on this tab was a secret field.")
    start_url = ""
    if entries[0].get("kind") == "navigate":
        start_url = str((entries[0].get("params") or {}).get("url") or "")
        entries = entries[1:]
    if not start_url:
        start_url = str(entries[0].get("url") or "") if entries else ""
    if not entries:
        raise ValueError("Nothing to save after the opening navigation.")
    if len(entries) > MAX_FLOW_STEPS:
        raise ValueError(f"A flow holds at most {MAX_FLOW_STEPS} steps; save a shorter range with from_step/to_step.")

    steps: list[dict[str, Any]] = []
    variables: dict[str, dict[str, Any]] = {}
    by_value: dict[tuple[str, str], str] = {}
    for entry in entries:
        kind = str(entry.get("kind") or "")
        target = dict(entry.get("target") or {})
        params = dict(entry.get("params") or {})
        step: dict[str, Any] = {"kind": kind, **params, "page": page_route(entry.get("url") or "")}
        if target:
            step["target"] = target
            if not has_identity(target):
                step["unnamed"] = True
        label = _step_label(target)
        value_key = {"fill": "value", "date": "value", "select": "option"}.get(kind)
        value = params.get(value_key) if value_key else None
        if isinstance(value, str) and value.strip():
            # A dropdown choice is named only by its field: by shape alone,
            # "Vue" or "Yes" would read as a first name. (Upstream detects in
            # typed text only.)
            found = (detect_from_attributes(target_attributes(target)) if kind == "select"
                     else detect_variable_type(value, target_attributes(target)))
            if found:
                # The same value typed into the same field again is one
                # variable; two different fields that happened to hold the
                # same value are two (changing one must not change the other).
                field_key = (found[0].split("_")[0], value, _field_identity(target))
                seen = by_value.get(field_key)
                if seen:
                    step[value_key] = "{{" + seen + "}}"
                else:
                    base = found[0]
                    if base in _GENERIC_VARIABLES and label:
                        # "number" or "date" says little about which one;
                        # the field's label does ("years_of_experience").
                        base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or base
                    var = ensure_unique_name(base, variables)
                    variables[var] = {"default": value, "format": found[1], "label": label,
                                      "step": len(steps)}
                    by_value[field_key] = var
                    step[value_key] = "{{" + var + "}}"
        if kind == "upload" and isinstance(params.get("paths"), list) and params["paths"]:
            base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "file"
            var = ensure_unique_name(base if base.endswith("file") else f"{base}_file", variables)
            variables[var] = {"default": list(params["paths"]), "format": "paths", "label": label,
                              "step": len(steps)}
            step["paths"] = "{{" + var + "}}"
        steps.append(step)

    site = urlparse(start_url).hostname or ""
    return {
        "name": (name or "").strip()[:120] or f"{site or 'browser'} flow",
        "site": site,
        "start_url": start_url,
        "steps": steps,
        "variables": variables,
        "skipped_sensitive": skipped_sensitive,
    }


def plan_steps(flow: dict[str, Any], values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Sequence steps for a run, variables substituted. Raises on unknown names."""
    declared = dict(flow.get("variables") or {})
    values = dict(values or {})
    unknown = sorted(set(values) - set(declared))
    if unknown:
        known = ", ".join(sorted(declared)) or "none"
        raise ValueError(f"Unknown flow variable(s): {', '.join(unknown)}. This flow's variables: {known}.")

    def substitute(raw: Any) -> Any:
        if not isinstance(raw, str):
            return raw
        whole = _PLACEHOLDER.fullmatch(raw.strip())
        if whole:
            name = whole.group(1)
            return values[name] if name in values else declared.get(name, {}).get("default", "")
        return _PLACEHOLDER.sub(
            lambda match: str(values.get(match.group(1), declared.get(match.group(1), {}).get("default", ""))),
            raw,
        )

    planned = []
    for index, raw in enumerate(flow.get("steps") or []):
        step = {key: substitute(value) for key, value in raw.items() if key not in {"target", "page"}}
        target = dict(raw.get("target") or {})
        if target:
            step["_semantic_target"] = target
            label = _step_label(target)
            if label:
                step["label"] = label
            if target.get("role"):
                step["role"] = target["role"]
        if step.get("kind") == "upload" and isinstance(step.get("paths"), str):
            step["paths"] = [step["paths"]]
        if raw.get("unnamed"):
            step["_unnamed"] = True
        step["_flow_step"] = index
        step["_page"] = raw.get("page") or ""
        planned.append(step)
    return planned


def same_target(first: dict[str, Any] | None, second: dict[str, Any] | None) -> bool:
    """Two steps aimed at the same element with the same verb."""
    if not first or not second or first.get("kind") != second.get("kind"):
        return False
    a, b = first.get("_semantic_target") or {}, second.get("_semantic_target") or {}
    return bool(a) and a == b


def opens_menu(step: dict[str, Any] | None) -> bool:
    """A click whose target opens a popup (upstream ``_is_menu_opener_step``)."""
    if not step or step.get("kind") != "click":
        return False
    target = step.get("_semantic_target") or {}
    haspopup = str(target.get("haspopup") or "").strip().lower()
    return bool(haspopup and haspopup != "false") or str(target.get("aria_expanded") or "") in {"true", "false"} \
        or str(target.get("role") or "").lower() in {"combobox", "menubutton"}


def is_menu_item(step: dict[str, Any] | None) -> bool:
    """A step aimed at an item inside a popup (upstream ``_is_menu_item_element``)."""
    target = (step or {}).get("_semantic_target") or {}
    return str(target.get("role") or "").lower() in _MENU_ITEM_ROLES


def describe_step(step: dict[str, Any]) -> str:
    """One readable line per step for the model and the user."""
    kind = str(step.get("kind") or "")
    label = _step_label(step.get("target") or step.get("_semantic_target") or {}) or str(step.get("label") or "")
    detail = ""
    for key in ("value", "option", "url", "key", "paths"):
        if step.get(key) not in (None, "", []):
            detail = f" = {step[key]}" if key != "url" else f" {step[key]}"
            break
    if kind == "check":
        detail = " = on" if step.get("checked", True) else " = off"
    kind_name = str((step.get("target") or step.get("_semantic_target") or {}).get("role") or "control")
    target = f' "{label}"' if label else (f" (unnamed {kind_name})" if kind in _TARGET_KINDS else "")
    return f"{kind}{target}{detail}"


class FlowStore:
    """Saved flows, one JSON file per owner, written atomically."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    def _path(self, owner: str) -> Path:
        digest = hashlib.sha256(str(owner or "").encode("utf-8")).hexdigest()[:24]
        return self.root / f"{digest}.json"

    def _load(self, owner: str) -> dict[str, Any]:
        path = self._path(owner)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"owner": owner, "flows": {}}
        if not isinstance(data, dict) or data.get("owner") != owner:
            return {"owner": owner, "flows": {}}
        data.setdefault("flows", {})
        return data

    def _write(self, owner: str, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix="flows-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=1)
            os.replace(temp, self._path(owner))
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def save(self, owner: str, flow: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._load(owner)
            flows = data["flows"]
            if len(flows) >= MAX_FLOWS_PER_OWNER and flow.get("flow_id") not in flows:
                raise ValueError(f"At most {MAX_FLOWS_PER_OWNER} saved flows; delete one first.")
            now = time.time()
            flow_id = str(flow.get("flow_id") or f"flow_{uuid.uuid4().hex[:10]}")
            stored = {**flow, "flow_id": flow_id, "updated_at": now}
            stored.setdefault("created_at", now)
            stored.setdefault("runs", {"count": 0})
            flows[flow_id] = stored
            self._write(owner, data)
            return dict(stored)

    def get(self, owner: str, flow_id: str) -> dict[str, Any] | None:
        with self._lock:
            flow = self._load(owner)["flows"].get(str(flow_id or ""))
            return dict(flow) if flow else None

    def list(self, owner: str) -> list[dict[str, Any]]:
        with self._lock:
            flows = list(self._load(owner)["flows"].values())
        return sorted(flows, key=lambda flow: flow.get("updated_at") or 0, reverse=True)

    def delete(self, owner: str, flow_id: str) -> bool:
        with self._lock:
            data = self._load(owner)
            if data["flows"].pop(str(flow_id or ""), None) is None:
                return False
            self._write(owner, data)
            return True

    def note_run(self, owner: str, flow_id: str, outcome: dict[str, Any]) -> None:
        with self._lock:
            data = self._load(owner)
            flow = data["flows"].get(flow_id)
            if not flow:
                return
            runs = dict(flow.get("runs") or {"count": 0})
            runs["count"] = int(runs.get("count") or 0) + 1
            runs["last_at"] = time.time()
            runs["last_status"] = outcome.get("status")
            runs["last_completed"] = outcome.get("completed")
            flow["runs"] = runs
            self._write(owner, data)


def flow_summary(flow: dict[str, Any], *, with_steps: bool = False) -> dict[str, Any]:
    """What the model is shown about a saved flow."""
    summary = {
        "flow_id": flow.get("flow_id"),
        "name": flow.get("name"),
        "site": flow.get("site"),
        "start_url": flow.get("start_url"),
        "step_count": len(flow.get("steps") or []),
        "variables": {
            name: {"default": spec.get("default"), "label": spec.get("label")}
            for name, spec in (flow.get("variables") or {}).items()
        },
        "runs": flow.get("runs") or {"count": 0},
    }
    if flow.get("skipped_sensitive"):
        summary["skipped_sensitive"] = list(flow["skipped_sensitive"])
    if with_steps:
        summary["steps"] = [f"{i + 1}. {describe_step(step)}" for i, step in enumerate(flow.get("steps") or [])]
    return summary
