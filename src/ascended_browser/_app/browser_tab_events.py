"""Build SSE payloads that surface leased browser pages in the session tab strip."""
from __future__ import annotations

from typing import Any, Iterable

# Tools whose target tab should be surfaced while the operation is running.
# This is activity projection only; user-facing tab selection belongs to the
# shared editor surface and is never encoded by a tool-name allowlist.
_TAB_ACTIVITY_TOOLS = frozenset({
    "browser_open",
    "browser_viewport",
    "browser_resume",
    "browser_observe",
    "browser_act",
    "browser_extract",
    "browser_evaluate",
    "browser_screenshot",
    "browser_flow",
    "browser_login",
    "wait_for_bot_wall",
    "browser_wait",
    "browser_attention_resolve",
})

# Quiet reconcile — update chips without stealing focus from a document.
_QUIET_TOOLS = frozenset({
    "browser_workspace_status",
})

_ALL_BROWSER_TOOLS = _TAB_ACTIVITY_TOOLS | _QUIET_TOOLS | frozenset({"browser_tabs"})


def is_browser_workspace_tool(tool: str) -> bool:
    return str(tool or "") in _ALL_BROWSER_TOOLS


def browser_activity_tab_id(
    tool: str,
    workspace: dict[str, Any] | None,
    args: dict[str, Any] | None = None,
) -> str:
    """Resolve the page whose operation presence should be projected."""
    tool = str(tool or "")
    if tool not in _TAB_ACTIVITY_TOOLS:
        return ""
    args = args if isinstance(args, dict) else {}
    if tool in {"browser_wait", "browser_attention_resolve"}:
        return ""
    if tool == "browser_flow" and str(args.get("action") or "").lower() != "run":
        return ""
    if tool == "browser_open" and args.get("urls") not in (None, []):
        # A batch always creates independent pages; it never reuses the
        # previously selected parent tab.
        return ""
    target = str(args.get("tab_id") or "")
    if target:
        return target
    if tool not in {"browser_resume", "browser_open"} or args.get("reuse", True) is False:
        return ""
    tabs = [
        tab for tab in (workspace or {}).get("tabs", [])
        if isinstance(tab, dict)
    ] if isinstance(workspace, dict) else []
    return _preferred_parent_tab_id(tabs)


def _tab_payload(
    *,
    session_id: str,
    tab: dict[str, Any],
    tool: str,
    status: str = "open",
    phase: str = "",
    created: bool | None = None,
) -> dict[str, Any]:
    payload = {
        "type": "browser_tab_update",
        "session_id": session_id,
        "tab_id": str(tab.get("tab_id") or ""),
        "url": str(tab.get("url") or ""),
        "title": str(tab.get("title") or ""),
        "owner_kind": str(tab.get("owner_kind") or ""),
        "owner_id": str(tab.get("owner_id") or ""),
        "status": status,
        "tool": tool,
    }
    # Durable activity is useful for ordering/history; it is not presence.
    raw_stamp = tab.get("last_agent_act_at")
    if isinstance(raw_stamp, (int, float)) and raw_stamp > 0:
        payload["last_agent_act_at"] = raw_stamp
    if phase:
        payload["phase"] = phase
    if created is not None:
        payload["created"] = bool(created)
    return payload


def _preferred_parent_tab_id(tabs: list[dict[str, Any]]) -> str:
    parents = [
        tab for tab in tabs
        if tab.get("owner_kind") == "parent"
        or (tab.get("lease") or {}).get("actor") == "parent"
    ]
    selected = max(
        parents or tabs,
        key=lambda tab: float(tab.get("updated_at") or 0),
        default={},
    )
    return str(selected.get("tab_id") or "")


def browser_tab_start_events(
    tool: str,
    workspace: dict[str, Any] | None,
    *,
    session_id: str,
    args: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Surface the page being worked before a browser tool finishes."""
    tool = str(tool or "")
    if tool not in _TAB_ACTIVITY_TOOLS or not isinstance(workspace, dict):
        return []
    tabs = [tab for tab in workspace.get("tabs", []) if isinstance(tab, dict)]
    args = args if isinstance(args, dict) else {}
    target = browser_activity_tab_id(tool, workspace, args)
    events = []
    for tab in tabs:
        tab_id = str(tab.get("tab_id") or "")
        if not tab_id:
            continue
        # Resume repopulates the complete strip; other tools need only their
        # target page. This lets a closed pane mount before ensure_awake runs.
        if tool != "browser_resume" and tab_id != target:
            continue
        events.append(_tab_payload(
            session_id=session_id,
            tab=tab,
            tool=tool,
            phase="running" if tab_id == target else "",
        ))
    return events


def browser_tab_update_events(
    tool: str,
    result: dict[str, Any] | None,
    *,
    session_id: str,
    args: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract zero or more browser_tab_update SSE payloads from a tool result."""
    tool = str(tool or "")
    if not is_browser_workspace_tool(tool):
        return []
    if not isinstance(result, dict) or result.get("error"):
        return []
    exit_code = int(result.get("exit_code") or 0)
    partial_batch = bool(
        tool == "browser_open"
        and isinstance(result.get("results"), list)
        and any(
            isinstance(row, dict) and row.get("success") and row.get("tab_id")
            for row in result["results"]
        )
    )
    if exit_code != 0 and not partial_batch:
        return []
    session_id = str(session_id or result.get("session_id") or "")
    if not session_id:
        return []
    args = args if isinstance(args, dict) else {}

    events: list[dict[str, Any]] = []

    if tool == "browser_tabs":
        action = str(args.get("action") or result.get("action") or "list")
        if action == "close" and result.get("tab_id"):
            return [{
                "type": "browser_tab_update",
                "session_id": session_id,
                "tab_id": str(result.get("tab_id") or ""),
                "url": "",
                "title": "",
                "owner_kind": "",
                "owner_id": "",
                "status": "closed",
                "tool": tool,
            }]

    if tool == "browser_open" and result.get("tab_id"):
        events.append(_tab_payload(
            session_id=session_id,
            tab=result,
            tool=tool,
            created=not bool(result.get("reused")),
        ))
        return events

    nested = result.get("tab")
    if isinstance(nested, dict) and nested.get("tab_id"):
        events.append(_tab_payload(session_id=session_id, tab=nested, tool=tool))
        return events

    # Viewport mutations return the authoritative post-resize observation in
    # `page`. Preserve its current URL/title when the completion upserts the
    # existing chip; falling through to args would replace both with blanks.
    page = result.get("page")
    if tool == "browser_viewport" and isinstance(page, dict):
        page_tab = {**page}
        page_tab.setdefault("tab_id", str(args.get("tab_id") or ""))
        if page_tab.get("tab_id"):
            events.append(_tab_payload(
                session_id=session_id,
                tab=page_tab,
                tool=tool,
            ))
            return events

    tabs = result.get("tabs")
    if not isinstance(tabs, list) and tool == "browser_open":
        # Batch opens deliberately expose one authoritative ordered `results`
        # mapping instead of duplicating tab ids under `tabs`. Live View still
        # needs an upsert for every successful tab.
        rows = result.get("results")
        if isinstance(rows, list):
            tabs = [
                row for row in rows
                if isinstance(row, dict) and row.get("success") and row.get("tab_id")
            ]
    if isinstance(tabs, list) and tabs:
        for tab in tabs:
            if not isinstance(tab, dict) or not tab.get("tab_id"):
                continue
            events.append(_tab_payload(
                session_id=session_id,
                tab=tab,
                tool=tool,
                created=(not bool(tab.get("reused"))) if tool == "browser_open" else None,
            ))
        return events

    # observe / extract / evaluate / login often omit tab in the body — use args.
    arg_tab = str(args.get("tab_id") or "")
    if (
        tool == "browser_viewport"
        and str(args.get("action") or result.get("action") or "").lower() == "get"
        and arg_tab
    ):
        # A read-only geometry result has no page snapshot. Emit the identity
        # without empty metadata so the existing chip keeps its URL and title.
        return [{
            "type": "browser_tab_update",
            "session_id": session_id,
            "tab_id": arg_tab,
            "status": "open",
            "tool": tool,
        }]
    if arg_tab:
        events.append(_tab_payload(
            session_id=session_id,
            tab={
                "tab_id": arg_tab,
                "url": result.get("url") or "",
                "title": result.get("title") or "",
                "owner_kind": "",
                "owner_id": "",
            },
            tool=tool,
        ))
    return events


def iter_browser_tab_sse(
    tool: str,
    result: dict[str, Any] | None,
    *,
    session_id: str,
    args: dict[str, Any] | None = None,
) -> Iterable[str]:
    import json
    for payload in browser_tab_update_events(tool, result, session_id=session_id, args=args):
        if not payload.get("tab_id") and payload.get("status") != "closed":
            continue
        yield f"data: {json.dumps(payload)}\n\n"
