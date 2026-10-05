"""Bounded hints between scoped observations; never a replacement for a snapshot."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re
from typing import Any

from ascended_browser._app.browser_sensitive_state import redact_password_snapshot


_IDENTITY_FIELDS = ("node_token", "id", "role", "label", "aria_label", "placeholder", "text", "context", "type", "frame_index", "frame_url", "frame_name")
_STATE_FIELDS = ("value", "selected_text", "checked", "disabled", "required", "text", "label", "context", "aria_checked")
_COVERAGE_FIELDS = ("url", "workspace_id", "tab_id", "observation_id", "elements_complete", "elements_omitted", "budget_exhausted", "unavailable", "collection_diagnostics")


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _key(element: dict[str, Any]) -> str:
    if element.get("_identity_key"):
        return str(element["_identity_key"])
    frame = json.dumps([element.get("frame_index", 0), element.get("frame_url", ""), element.get("frame_name", "")], ensure_ascii=False)
    token = _norm(element.get("node_token"))
    if token:
        return frame + "|node:" + token
    element_id = _norm(element.get("id"))
    if element_id:
        return frame + "|id:" + element_id
    parts = [_norm(element.get("role")), _norm(element.get("label") or element.get("aria_label") or element.get("placeholder") or element.get("text")), _norm(element.get("context")), _norm(element.get("type"))]
    return frame + "|semantic:" + "|".join(parts) if any(parts) else ""


def _state(element: dict[str, Any]) -> dict[str, Any]:
    return {key: (value[:160] if isinstance(value, str) else value)
            for key in _STATE_FIELDS if (value := element.get(key)) is not None and value != ""}


def _digest(element: dict[str, Any]) -> str:
    if element.get("_state_digest"):
        return str(element["_state_digest"])
    raw = {key: element.get(key) for key in _STATE_FIELDS if element.get(key) is not None and element.get(key) != ""}
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def delta_baseline(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Retain bounded comparison evidence in the existing observation history.

    Long values are hashed before clipping, so a late edit is still detected.
    Passwords never enter retained state or output.
    """
    redact_password_snapshot(snapshot)
    return {
        **{key: snapshot[key] for key in _COVERAGE_FIELDS if key in snapshot},
        "elements": [
            {**{key: element[key] for key in _IDENTITY_FIELDS if key in element},
             **_state(element), "_state_digest": _digest(element), "_identity_key": _key(element)}
            for element in snapshot.get("elements") or [] if isinstance(element, dict)
        ],
    }


def _complete(snapshot: dict[str, Any]) -> bool:
    return snapshot.get("elements_complete") is not False and not any(
        snapshot.get(key) for key in ("elements_omitted", "budget_exhausted", "unavailable", "collection_diagnostics")
    )


def observation_delta(previous: dict[str, Any] | None, current: dict[str, Any] | None, *, limit: int = 20) -> dict[str, Any]:
    redact_password_snapshot(previous)
    redact_password_snapshot(current)
    if not isinstance(current, dict):
        return {"changed": False, "comparison_complete": False, "added": [], "removed": [], "updated": []}
    baseline_id = str((previous or {}).get("observation_id") or "")
    metadata = {"baseline_observation_id": baseline_id, "observation_id": str(current.get("observation_id") or ""), "hint_only": True}
    scope_changed = isinstance(previous, dict) and any(previous.get(k) is not None and current.get(k) is not None and previous[k] != current[k] for k in ("workspace_id", "tab_id"))
    url_changed = isinstance(previous, dict) and previous.get("url") != current.get("url")
    if not isinstance(previous, dict) or scope_changed or url_changed:
        return {**metadata, "changed": True, "initial": True, "reset": bool(previous), "url_changed": bool(url_changed), "comparison_complete": False, "added_count": 0, "removed_count": 0, "updated_count": 0, "newly_observed_count": len(current.get("elements") or []), "added": [], "removed": [], "updated": []}

    def grouped(snapshot):
        groups = defaultdict(list)
        for element in snapshot.get("elements") or []:
            if isinstance(element, dict) and (key := _key(element)):
                groups[key].append(element)
        return groups

    before, after = grouped(previous), grouped(current)
    # Never silently overwrite repeated IDs/names. Without stable node identity,
    # changed duplicate groups are explicitly uncertain, not paired by position.
    duplicates = {key for key in before.keys() | after.keys() if len(before[key]) > 1 or len(after[key]) > 1}
    ambiguous_changes = sum(Counter(_digest(e) for e in before[key]) != Counter(_digest(e) for e in after[key]) for key in duplicates)
    unique_before = {key: value[0] for key, value in before.items() if value and key not in duplicates}
    unique_after = {key: value[0] for key, value in after.items() if value and key not in duplicates}
    newly_seen = [key for key in unique_after if key not in unique_before]
    not_seen = [key for key in unique_before if key not in unique_after]
    added = newly_seen if _complete(previous) else []
    removed = not_seen if _complete(current) else []
    updated = [key for key in unique_after if key in unique_before and _digest(unique_after[key]) != _digest(unique_before[key])]

    def label(element):
        return _norm(element.get("label") or element.get("aria_label") or element.get("placeholder") or element.get("text") or element.get("role"))[:120]

    return {
        **metadata,
        "changed": bool(newly_seen or not_seen or updated or ambiguous_changes),
        "url_changed": False,
        "comparison_complete": _complete(previous) and _complete(current) and not duplicates,
        "ambiguous_identity_count": len(duplicates), "ambiguous_changed_count": ambiguous_changes,
        "added_count": len(added), "removed_count": len(removed), "updated_count": len(updated),
        "newly_observed_count": len(newly_seen), "not_observed_count": len(not_seen),
        "added": [label(unique_after[key]) for key in added[:limit]],
        "removed": [label(unique_before[key]) for key in removed[:limit]],
        "updated": [{"target": label(unique_after[key]), "before": _state(unique_before[key]), "after": _state(unique_after[key])} for key in updated[:limit]],
        "truncated": any(len(items) > limit for items in (added, removed, updated)),
    }


def _semantic_key(element: dict[str, Any]) -> str:
    name = _norm(element.get("label") or element.get("aria_label") or element.get("placeholder") or element.get("text"))
    return "|".join([str(element.get("frame_index", 0)), _norm(element.get("frame_url")),
                     _norm(element.get("role")), name[:160]])


def newly_appeared_refs(previous: dict[str, Any] | None, current: dict[str, Any] | None) -> set[str]:
    """Refs of elements that were not on the page at the previous observation.

    Keyed by role and name, not DOM node: a framework re-render replaces nodes
    without changing what the user sees, and must not make everything "new".
    Only within one URL; a navigation makes the whole page new, which says
    nothing about what to look at first.
    """
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return set()
    if previous.get("url") != current.get("url"):
        return set()
    seen = {_semantic_key(e) for e in previous.get("elements") or [] if isinstance(e, dict)}
    if not seen:
        return set()
    return {
        str(e["ref"]) for e in current.get("elements") or []
        if isinstance(e, dict) and e.get("ref") and _semantic_key(e) not in seen
    }
