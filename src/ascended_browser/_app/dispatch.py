"""Generated from tool_implementations.py by scripts/sync_from_ascended.py: the definitions
reachable from do_browser_workspace. Do not edit; edit Ascended and re-sync."""


from __future__ import annotations


import asyncio

import base64

import hashlib

import json

import logging

import os

import re

import time

from contextlib import asynccontextmanager

from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

@asynccontextmanager
async def _browser_capacity_scope(manager, owner: str, count: int, actor: str):
    """Use real capacity reservations while keeping lightweight test doubles valid."""
    reserve = getattr(manager, "reserve_tab_capacity", None)
    if callable(reserve):
        async with reserve(owner, count, actor=actor):
            yield
        return
    yield

def _assert_browser_tab_access(
    manager, owner: str, session_id: str, tab_id: str, actor: str, actor_id: str,
) -> None:
    checker = getattr(manager, "assert_tab_access", None)
    if callable(checker):
        checker(owner, session_id, tab_id, actor=actor, actor_id=actor_id)

def _resolve_upload_paths(paths: List, session_id: str) -> List[str]:
    """Resolve model-supplied upload paths against the session's workspace.

    Absolute paths are kept as given — the user may deliberately attach
    something from outside the workspace — but a relative path is resolved
    inside the workspace root and must stay there.
    """
    from ascended_browser.runtime.sandbox import workspace_root

    root = workspace_root(session_id)
    out: List[str] = []
    for item in paths:
        raw = str(item or "").strip()
        if not raw:
            continue
        expanded = os.path.expanduser(raw)
        if os.path.isabs(expanded) or root is None:
            out.append(expanded)
            continue
        resolved = os.path.realpath(os.path.join(str(root), expanded))
        if os.path.commonpath([resolved, os.path.realpath(str(root))]) != os.path.realpath(str(root)):
            raise ValueError(f"upload path {raw!r} is outside the active workspace")
        out.append(resolved)
    return out

def _scope_browser_result(result: Any, manager: Any, owner: str, session_id: str,
                          args: Dict | None = None) -> Dict:
    """Attach the authoritative local workspace, even without an observation.

    Browser schemas expose tab IDs, not model-chosen workspace IDs. Only this
    local dispatch boundary can derive their namespace from the authenticated
    owner and effective session (including a browser child's parent session).
    External MCP results never pass this boundary.
    """
    from ascended_browser._app.browser_workspace.manager_core import BrowserWorkspaceManager

    workspace = getattr(manager, "workspace_id", None)
    workspace_id = (workspace if callable(workspace) else BrowserWorkspaceManager.workspace_id)(owner, session_id)
    output = dict(result) if isinstance(result, dict) else {"result": result}
    output["workspace_id"] = workspace_id
    tab = output.get("tab") if isinstance(output.get("tab"), dict) else {}
    page = output.get("page") if isinstance(output.get("page"), dict) else {}
    request = args if isinstance(args, dict) else {}
    tab_id = next((value.strip() for value in (
        output.get("tab_id"), tab.get("tab_id"), page.get("tab_id"), request.get("tab_id"),
    ) if isinstance(value, str) and value.strip()), None)
    if tab_id is not None:
        output["tab_id"] = tab_id
    for key in ("tab", "page"):
        if isinstance(output.get(key), dict):
            output[key] = {**output[key], "workspace_id": workspace_id}
    # Batch-open items and the tab roster use this same owner/session. Keep
    # their per-tab scope exact without assigning one sibling's tab to another.
    for key in ("results", "tabs"):
        if isinstance(output.get(key), list):
            output[key] = [
                _scope_browser_result(item, manager, owner, session_id)
                if isinstance(item, dict) else item for item in output[key]
            ]
    from ascended_browser._app.browser_result_summary import browser_result_summary
    output["result_summary"] = browser_result_summary(output)
    return output

async def _read_browser_evidence(
    session_id: str, reference: str, *, cursor: Any = 0, limit: Any = 8000,
) -> Dict[str, Any]:
    """Page one session-owned browser archive without exposing its file path."""
    from ascended_browser.runtime.evidence import resolve_managed_output_reference

    try:
        start = max(0, int(cursor or 0))
        size = max(512, min(12_000, int(limit or 8000)))
    except (TypeError, ValueError):
        return {
            "success": False,
            "error": "browser_extract evidence cursor and limit must be integers",
            "evidence_ref": str(reference or ""),
            "cursor": 0,
        }
    path = resolve_managed_output_reference(session_id, str(reference or ""))
    if path is None:
        return {
            "success": False,
            "error": "Browser evidence is missing, expired, or belongs to another session.",
            "evidence_ref": str(reference or ""),
            "cursor": start,
        }

    def read_slice() -> tuple[str, int, int, str]:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            complete = stream.read()
        content, unit = _browser_evidence_page_text(complete)
        end = min(len(content), start + size)
        return content[start:end], end, len(content), unit

    text, end, total, unit = await asyncio.to_thread(read_slice)
    if start and start >= total:
        # A cursor past the end came from somewhere else (another page, or a
        # raw-archive offset); an empty success would read as "nothing more".
        return {
            "success": False,
            "error": (
                f"cursor {start} is past the end of this evidence ({total} chars); "
                "follow next_cursor from the previous slice, or start at cursor 0"
            ),
            "evidence_ref": str(reference),
            "cursor": start,
            "total_chars": total,
        }
    eof = end >= total
    return {
        "success": True,
        "source": "browser_evidence",
        "evidence_ref": str(reference),
        "cursor": start,
        "text": text,
        "next_cursor": None if eof else end,
        "eof": eof,
        "total_chars": total,
        "content": unit,
    }

async def _save_browser_extraction(result: Dict, args: Dict, session_id: str) -> Dict:
    """Export canonical evidence through the ordinary workspace file writer.

    Save the source, not a model projection or a continuation slice. The browser
    serializer owns redaction; the managed-output resolver owns archive access;
    the filesystem tool owns path policy and the write lifecycle.
    """
    if result.get("error") or result.get("success") is False or result.get("exit_code", 0):
        return result
    from ascended_browser.runtime.tool_execution import browser_result_archive
    reference = str(args.get("evidence_ref") or "").strip()
    if reference:
        from ascended_browser.runtime.evidence import resolve_managed_output_reference
        path = resolve_managed_output_reference(session_id, reference)
        if path is None:
            raise ValueError("Browser evidence is missing, expired, or belongs to another session.")
        def read_archive():
            with open(path, "r", encoding="utf-8") as stream:
                return stream.read()
        content = await asyncio.to_thread(read_archive)
        scope = "original_evidence_archive"
    elif result.get("source") == "design_read":
        # The whole read, icon markup and font sources included: the inline
        # text is a brief and cannot be retyped into a stylesheet.
        content = json.dumps(result.get("design") or {}, ensure_ascii=False, indent=1)
        scope = "design_read"
    elif result.get("source") == "structured_extraction":
        # The records are the evidence. The page archive keeps none of them,
        # so the file came back 158 bytes long and a live run retyped all 25
        # rows by hand from the tool result.
        content = json.dumps({
            key: result[key]
            for key in ("url", "title", "data", "unverified", "unverified_count", "truncated", "next_cursor")
            if key in result
        }, ensure_ascii=False, indent=1)
        scope = "structured_extraction"
    else:
        content = browser_result_archive("browser_extract", result, args)
        scope = "current_extraction_result"
    if content is None:
        raise ValueError("This extraction has no exportable semantic evidence.")
    from ascended_browser.runtime.files import WriteFileTool
    saved = await WriteFileTool().execute(str(args["save_to"]) + "\n" + content, {})
    if saved.get("error") or saved.get("exit_code", 0):
        return {**result, "success": False, "error": saved.get("error") or "Evidence file write failed.", "exit_code": 1}
    try:
        json.loads(content)
        saved_format = "json"
    except ValueError:
        saved_format = "text"
    encoded = content.encode("utf-8")
    return {**result, "saved_to": args["save_to"], "saved_format": saved_format,
            "saved_scope": scope, "saved_bytes": len(encoded),
            "saved_sha256": hashlib.sha256(encoded).hexdigest()}

def _browser_evidence_page_text(archive: str) -> tuple[str, str]:
    """What an evidence cursor counts: the page's text, not the archive's JSON.

    The first extraction reports offsets into the page text (whitespace
    collapsed, as its ``relevant_text`` is). Paging the serialized archive
    instead returned escaped JSON from an unrelated offset and a total three
    times the page's length, so the two calls disagreed about the same page.
    """
    import json
    import re

    try:
        document = json.loads(archive)
    except ValueError:
        return archive, "archive"
    if isinstance(document, dict):
        # The archive may wrap the tool payload; look one level down too.
        candidates = [document] + [
            value for value in document.values() if isinstance(value, dict)
        ]
        for candidate in candidates:
            for key in ("text", "visible_text", "relevant_text"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return re.sub(r"\s+", " ", value).strip(), "page_text"
    return archive, "archive"

@asynccontextmanager
async def _null_async_context():
    yield

async def do_browser_workspace(tool: str, content: str, owner: Optional[str] = None,
                               session_id: Optional[str] = None, run_id: Optional[str] = None,
                               progress_cb=None, parallel_browser_tab: bool = False) -> Dict:
    """Dispatch the native chat-scoped browser workspace surface."""
    owner = owner or ""
    # A browser sub-agent runs its own agent loop with session_id=None, so it
    # cannot write to the parent's chat history. Its *browser* is the parent's
    # though — the workspace is keyed (owner, session_id) — so the child's
    # identity supplies the session here and marks its tabs as worker-leased.
    # This must resolve BEFORE the session guard below: a child always arrives
    # with session_id=None, so checking first refuses every call it makes.
    from ascended_browser.runtime.subagents import current_child_execution
    if current_child_execution():
        return {"error": "Sub-agents do not receive browser control. Ask the parent to perform this action.", "exit_code": 1}
    from ascended_browser.runtime.subagents import current_browser_child
    child = current_browser_child()
    actor = "worker" if child else "parent"
    if child:
        session_id = child.get("session_id") or session_id
        owner = owner or child.get("owner") or ""
    if not session_id:
        return {"error": f"{tool} requires an active chat session", "exit_code": 1}
    try:
        args = json.loads(content or "{}")
    except Exception as exc:
        return _scope_browser_result(
            {"error": f"{tool}: invalid JSON: {exc}", "exit_code": 1}, None, owner, session_id,
        )
    if not isinstance(args, dict):
        return _scope_browser_result(
            {"error": f"{tool} requires a JSON object", "exit_code": 1}, None, owner, session_id,
        )
    from ascended_browser.runtime.agent_tools import get_browser_workspace_manager
    manager = get_browser_workspace_manager()
    if manager is None:
        return _scope_browser_result(
            {"error": "Browser workspace runtime is not initialized", "exit_code": 1},
            None, owner, session_id, args,
        )
    started_at = time.monotonic()
    try:
        if progress_cb:
            try:
                await progress_cb({
                    "tail": f"{tool.replace('_', ' ').capitalize()} in progress…",
                    "phase": "browser_operation",
                })
            except Exception:
                pass
        tab_note = _correct_tab_id(manager, owner, session_id, args)
        from ascended_browser._app.browser_tab_events import browser_activity_tab_id
        workspace_status = {}
        if tool in {"browser_open", "browser_resume"} and not args.get("tab_id"):
            try:
                workspace_status = manager.status(owner, session_id)
            except Exception:
                pass
        activity_tab_id = browser_activity_tab_id(tool, workspace_status, args)
        actor_id = child.get("thread_id") or "parent"
        # A read or a plain page load that yielded to a live-view or viewport
        # transition did nothing; retry it behind the transition, as open does.
        # With parallel tabs the live view moves often, and every yield reached
        # the model as an error it had to retry by hand.
        # browser_open already absorbs a transition burst inside the dispatcher
        # and stops at three on purpose, so a user who keeps resizing cannot
        # hold an agent navigation open forever. Retrying it again here would
        # turn that bound into nine.
        operation_context = getattr(manager, "agent_tab_operation", None)
        if callable(operation_context) and activity_tab_id:
            activity_context = operation_context(
                owner, session_id, activity_tab_id,
                actor=actor, actor_id=actor_id, tool=tool,
            )
        else:
            activity_context = _null_async_context()
        async with activity_context:
            for attempt in range(1 if tool == "browser_open" else 3):
                result = await _dispatch_browser_workspace(
                    tool, args, manager, owner, session_id, run_id,
                    actor=actor, actor_id=actor_id,
                    parallel_browser_tab=parallel_browser_tab,
                )
                if not (isinstance(result, dict) and result.get("error_kind") == "browser_transition"
                        and "RuntimeTransitionRequested" in str(result.get("error") or "")):
                    break
                logger.info("browser %s yielded to a runtime transition; retrying (attempt %d)", tool, attempt + 1)
        if tab_note and isinstance(result, dict):
            result["tab_note"] = tab_note
        # A browser-engine switch (desktop browser gone for a while, so this
        # host's own browser took over) is reported once, with the result that
        # first ran on the new engine, so the agent can tell the user.
        pop_notice = getattr(getattr(manager, "backend", None), "pop_engine_notice", None)
        if callable(pop_notice) and isinstance(result, dict):
            notice = pop_notice(owner)
            if notice:
                result["engine_notice"] = notice
        if progress_cb:
            try:
                await progress_cb({
                    "tail": f"{tool.replace('_', ' ').capitalize()} completed.",
                    "phase": "browser_operation_complete",
                })
            except Exception:
                pass
        return result
    finally:
        # Timing lives here because every layer of the browser tools measures fast
        # in isolation; when a call is slow in the live app this is the only place
        # that says which tool and how long, without a repro harness.
        from ascended_browser.runtime.settings import get_setting as _get_setting

        elapsed = time.monotonic() - started_at
        if elapsed >= float(_get_setting("browser_workspace_slow_tool_seconds", 2) or 2):
            logger.warning("browser workspace tool %s took %.1fs (session %s)", tool, elapsed, session_id)
        else:
            logger.debug("browser workspace tool %s took %.2fs", tool, elapsed)

def _correct_tab_id(manager: Any, owner: str, session_id: str, args: Dict) -> str:
    """Resolve a garbled tab id to the one tab it can only mean; say so.

    Tab ids are 36 characters of hex, and weaker models clip or mistype them
    ("tab_44c9301e0c454a090c..." for tab_44c9301e0c454ac090ec3b70237dc060).
    Playwright MCP sidesteps this with short tab indexes; here an id within two
    edits of exactly one open tab of this workspace, or a clear unique prefix,
    is corrected. Anything else is left for the ordinary unknown-tab error.
    """
    wanted = str(args.get("tab_id") or "").strip()
    if not wanted:
        return ""
    try:
        record = manager.record(owner, session_id)
        known = list(getattr(record, "tabs", {}) or {})
    except Exception:
        return ""
    if not known or wanted in known:
        return ""
    stem = wanted.rstrip(".…").strip()
    if len(stem) < 12:
        return ""

    def distance(a: str, b: str) -> int:
        row = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            prev, row[0] = row[0], i
            for j, cb in enumerate(b, 1):
                prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
        return row[-1]

    clipped = stem != wanted  # "…" or "..." marks a deliberately shortened id
    matches = [
        tab for tab in known
        if (tab.startswith(stem) if clipped else False)
        or distance(stem, tab[:len(stem)] if clipped else tab) <= 2
    ]
    if len(matches) != 1:
        tabs = getattr(record, "tabs", {}) or {}
        listing = "; ".join(
            f"{tab_id} ({str(getattr(tab, 'title', '') or getattr(tab, 'url', ''))[:50]})"
            for tab_id, tab in list(tabs.items())[:8]
        )
        return f"tab_id {wanted!r} is not an open tab. Open tabs: {listing}."
    args["tab_id"] = matches[0]
    return f"tab_id {wanted!r} is not an open tab; used {matches[0]!r}, the only close match. Copy tab ids exactly."

def _requested_tab_ids(args: Dict) -> list[str]:
    """The tabs a browser_tabs call names, in order, without repeats.

    ``tab_ids`` is the batch form and ``tab_id`` the single one; a model that
    sends both means the union, and an empty list is the caller's problem to
    hear about rather than a silent no-op.
    """
    raw = args.get("tab_ids")
    if raw is not None and not isinstance(raw, list):
        raise ValueError("browser_tabs tab_ids must be an array of tab ids")
    values = [*(raw or []), args.get("tab_id")]
    wanted: list[str] = []
    for value in values:
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError("browser_tabs tab ids must be strings")
        one = value.strip()
        if one and one not in wanted:
            wanted.append(one)
    if not wanted:
        raise ValueError("browser_tabs close requires tab_id, or tab_ids for several")
    if len(wanted) > 20:
        raise ValueError("browser_tabs closes at most 20 tabs per call")
    return wanted

def _browser_navigation_error_kind(exc: BaseException) -> str:
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_request"
    typed_kind = str(getattr(exc, "error_kind", "") or "").strip()
    if typed_kind:
        return typed_kind
    text = f"{type(exc).__name__}: {exc}".casefold()
    if any(marker in text for marker in (
        "dns", "err_name_not_resolved", "ns_error_unknown_host", "name or service not known",
        "temporary failure in name resolution", "getaddrinfo",
    )):
        return "dns"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if any(marker in text for marker in (
        "connection refused", "connection reset", "err_connection",
        "network is unreachable",
    )):
        return "connection"
    return "navigation"

def _browser_operation_failure(exc: BaseException) -> Dict[str, Any]:
    """Return one truthful, typed browser failure contract."""
    # Evidence a control captured while its state still existed (an open
    # combobox popup closes as the failure unwinds). Keep it structured so the
    # caller can act on it instead of re-deriving what is already gone.
    from ascended_browser._app.browser_workspace.manager_core import _failure_evidence, describe_exception

    error_kind = _browser_navigation_error_kind(exc)
    result: Dict[str, Any] = {
        "error": describe_exception(exc),
        "error_kind": error_kind,
        "exit_code": 1,
    }
    result.update(_failure_evidence(exc))
    if error_kind == "browser_runtime_unavailable":
        result.update({
            "blocked": True,
            "retry_safe": False,
            "failed_stage": "runtime_launch",
            "next_action": (
                "Stop browser work for this turn and report that the managed "
                "browser runtime is unavailable. Do not substitute shell HTTP "
                "requests for the requested browser interaction."
            ),
        })
    return result

async def _open_browser_after_runtime_transition(
    manager,
    owner: str,
    session_id: str,
    url: str,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Retry a safe open through a bounded burst of runtime transitions.

    ``BrowserWorkspaceManager.open`` deliberately stops GET navigation when a
    resize or takeover is waiting.  The manager has already unwound its shared
    effect barrier (and removed a partially created tab) by the time this
    helper sees ``RuntimeTransitionRequested``.  Retrying here therefore waits
    behind that transition instead of racing it, while keeping arbitrary
    navigation failures and repeated transitions visible to the caller.
    """
    from ascended_browser._app.browser_workspace.manager_core import RuntimeTransitionRequested

    # A newly mounted Live View can queue two independent authoritative
    # transitions in succession: selection/control setup, then its accepted
    # viewport.  Three total attempts absorb that bounded transition burst.
    # A fourth request still escapes as ``browser_transition`` so ongoing
    # user resizing or takeover cannot keep an agent navigation alive forever.
    for attempt in range(3):
        try:
            return await manager.open(owner, session_id, url, **kwargs)
        except RuntimeTransitionRequested:
            if attempt == 2:
                raise
            logger.info(
                "browser open yielded to a runtime transition; retrying after "
                "authority settles (session %s)",
                session_id,
            )
            settle = getattr(manager, "wait_for_runtime_transition_quiet", None)
            if callable(settle):
                await settle(owner)
    raise AssertionError("browser open transition retry exhausted")

async def _browser_screenshot(
    manager, owner: str, session_id: str, args: Dict, actor: str, actor_id: str,
    *, parallel_tab: bool = False,
) -> Dict[str, Any]:
    """Validate a capture request and shape its result for the loop.

    Evidence is saved as a workspace file under deliverables/, which the agent
    may present with present_artifact; it is never published to the chat on
    its own. The image travels in ``images`` so the tool row can preview it,
    and ``visual_audience`` decides whether it also reaches the model:
    evidence does not, which is what stops a picture becoming a way to
    navigate.

    ``state`` captures with a ref hovered or focused; ``matrix`` captures the
    live view's presets (and optionally light and dark) into one labelled
    grid; ``compare_with`` diffs the new picture against the previous one of
    this tab or a saved evidence file and reports the changed regions.
    """
    from ascended_browser._app.browser_capture import CAPTURE_PURPOSES, CAPTURE_SCOPES, capture_receipt

    tab_id = str(args.get("tab_id") or "").strip()
    purpose = str(args.get("purpose") or "").strip().lower()
    scope = str(args.get("scope") or "viewport").strip().lower()
    ref = str(args.get("ref") or "").strip()
    state = str(args.get("state") or "").strip().lower()
    compare_with = str(args.get("compare_with") or "").strip()
    matrix = args.get("matrix")
    color_schemes = args.get("color_schemes")
    if not tab_id:
        raise ValueError("browser_screenshot requires tab_id")
    if purpose not in CAPTURE_PURPOSES:
        raise ValueError("browser_screenshot purpose must be evidence or inspect")
    if scope not in CAPTURE_SCOPES:
        raise ValueError("browser_screenshot scope must be viewport, full_page, or element")
    if scope == "element" and not ref:
        raise ValueError("browser_screenshot scope=element requires a ref from browser_observe")
    if state and state not in {"hover", "focus"}:
        raise ValueError("browser_screenshot state must be hover or focus")
    if state and not ref:
        raise ValueError(f"browser_screenshot state={state} requires ref: the element to {state}")
    if isinstance(matrix, str):
        matrix = [part.strip() for part in matrix.split(",") if part.strip()]
    if isinstance(color_schemes, str):
        color_schemes = [part.strip() for part in color_schemes.split(",") if part.strip()]
    if matrix is not None and matrix != []:
        if not isinstance(matrix, list) or not all(str(item).strip().lower() in {"phone", "tablet", "desktop"} for item in matrix):
            raise ValueError("browser_screenshot matrix must list presets from phone, tablet, desktop")
        matrix = list(dict.fromkeys(str(item).strip().lower() for item in matrix))
        if state or compare_with or scope != "viewport":
            raise ValueError("browser_screenshot matrix captures the viewport of each preset; it cannot be combined with state, compare_with or another scope")
    else:
        matrix = None
    if color_schemes:
        if matrix is None:
            raise ValueError("browser_screenshot color_schemes needs matrix (the presets to capture in each scheme)")
        if not all(str(item).strip().lower() in {"light", "dark"} for item in color_schemes):
            raise ValueError("browser_screenshot color_schemes must be light and/or dark")
        color_schemes = list(dict.fromkeys(str(item).strip().lower() for item in color_schemes))
    _assert_browser_tab_access(manager, owner, session_id, tab_id, actor, actor_id)
    label = " ".join(str(args.get("label") or "").split())[:120]

    if matrix is not None:
        from ascended_browser._app.browser_capture_compare import compose_grid

        taken = await manager.screenshot_matrix(
            owner, session_id, tab_id, presets=matrix, color_schemes=color_schemes,
            actor=actor, actor_id=actor_id,
        )
        tiles = taken.get("tiles") or []
        if not tiles:
            return {
                **{k: v for k, v in taken.items() if k != "tiles"},
                "success": False,
                "error": "No preset could be captured: " + "; ".join(taken.get("failures") or ["unknown failure"]),
            }
        # Two schemes: one row per preset, light beside dark.
        per_row = len(color_schemes) if color_schemes and len(color_schemes) > 1 else len(tiles)
        grid, layout = compose_grid([(tile["label"], tile["data"]) for tile in tiles], columns=per_row)
        image = {"data": base64.b64encode(grid).decode("ascii"), "mimeType": "image/png",
                 "width": layout["width"], "height": layout["height"]}
        captured: Dict[str, Any] = {
            **{k: v for k, v in taken.items() if k != "tiles"},
            "purpose": purpose,
            "matrix": [{k: v for k, v in tile.items() if k not in {"data", "mime_type"}} for tile in tiles],
            "capture": {"image_width": layout["width"], "image_height": layout["height"],
                        "masked_fields": sum(int(tile.get("masked_fields") or 0) for tile in tiles)},
        }
        lines = [
            f"Captured {len(tiles)} view{'s' if len(tiles) != 1 else ''} of {taken.get('url') or 'the tab'} in one picture, row by row: "
            + "; ".join(tile["label"] for tile in tiles) + "."
        ]
        if taken.get("failures"):
            lines.append("Not captured: " + "; ".join(taken["failures"]) + ".")
        lines.append("The window is back to the size and emulation it had before." if taken.get("restored")
                     else f"The window could not be restored: {taken.get('restore_error')}. Call browser_viewport action=restore.")
        if purpose == "evidence":
            from ascended_browser._app.browser_capture import save_evidence

            captured["saved_path"] = save_evidence(session_id, grid, "image/png", label=label or f"{taken.get('title') or 'page'} responsive")
            lines.insert(0, f"Saved at {captured['saved_path']}; show it to the user with present_artifact if they asked to see it. It is not in your context.")
        else:
            lines.append("It carries no element refs; to act on the page, use refs from browser_observe. Earlier refs were invalidated by the resizes: observe again before acting.")
        image["label"] = label or f"Responsive views: {taken.get('title') or taken.get('url') or tab_id}"[:120]
        return {
            **captured,
            "success": True,
            "output": "\n".join(lines),
            "images": [image],
            "visual_audience": "user" if purpose == "evidence" else "model",
        }

    previous = manager.previous_capture(owner, session_id, tab_id) if compare_with == "previous" else None
    # Another tab as the baseline: the reference site beside the local build.
    other_tab = compare_with[4:].strip() if compare_with.startswith("tab:") else (
        compare_with if compare_with.startswith("tab_") else ""
    )
    if other_tab:
        if other_tab == tab_id:
            raise ValueError("browser_screenshot compare_with names this same tab; name the other tab, or use 'previous'")
        if scope == "element" or state:
            raise ValueError("browser_screenshot compare_with=tab:<id> captures both tabs whole; use scope viewport or full_page without state")
        _assert_browser_tab_access(manager, owner, session_id, other_tab, actor, actor_id)
    captured = await manager.screenshot(
        owner, session_id, tab_id,
        purpose=purpose, scope=scope, ref=ref if (scope == "element" or state) else "",
        state=state,
        actor=actor, actor_id=actor_id, **({"parallel_tab": True} if parallel_tab else {}),
    )
    image = dict(captured.pop("image"))
    receipt_lines = [capture_receipt(captured)]
    if state:
        receipt_lines.insert(0, (
            f"Captured with the pointer resting on {ref} (its :hover state), then moved away."
            if state == "hover" else f"Captured with {ref} focused (its :focus state), then blurred."
        ))
    if compare_with:
        from ascended_browser._app.browser_capture_compare import describe_diff, diff_captures

        baseline: bytes | None = None
        against = compare_with
        compare_error = ""
        if other_tab:
            try:
                reference = await manager.screenshot(
                    owner, session_id, other_tab, purpose="inspect", scope=scope,
                    actor=actor, actor_id=actor_id,
                )
                baseline = base64.b64decode(reference["image"]["data"])
                against = f"tab {other_tab} ({reference.get('url') or 'no url'})"
            except Exception as exc:
                compare_error = f"tab {other_tab} could not be captured: {exc}"
        elif compare_with == "previous":
            if previous is None:
                compare_error = "no earlier picture of this tab exists in this browser session; take one first, then compare_with='previous'"
            else:
                baseline = previous["data"]
                against = f"the previous picture of this tab ({previous.get('scope')}{', ' + previous['state'] if previous.get('state') else ''}, {previous.get('width')}x{previous.get('height')})"
        else:
            try:
                from ascended_browser.runtime.tool_execution import _resolve_tool_path

                path = _resolve_tool_path(compare_with)
                baseline = open(path, "rb").read()
                against = compare_with
            except Exception as exc:
                compare_error = f"compare_with file could not be read: {exc}"
        if baseline is not None:
            try:
                composite, stats = diff_captures(baseline, base64.b64decode(image["data"]))
            except ValueError as exc:
                if not other_tab:
                    compare_error = str(exc)
                else:
                    # Two pages rarely share a full-page height; side by side
                    # is still the comparison wanted, without a pixel diff.
                    from ascended_browser._app.browser_capture_compare import compose_grid

                    composite, _layout = compose_grid([
                        ("compare_with tab", baseline), ("this tab", base64.b64decode(image["data"])),
                    ])
                    captured["compare"] = {"against": against, "side_by_side": True, "reason": str(exc)}
                    image = {**image, "data": base64.b64encode(composite).decode("ascii"), "mimeType": "image/png",
                             "width": _layout["width"], "height": _layout["height"]}
                    receipt_lines.insert(0, f"Side by side with {against} (left) and this tab (right); {exc}, so no pixel diff.")
            else:
                captured["compare"] = {"against": against, **stats}
                image = {**image, "data": base64.b64encode(composite).decode("ascii"), "mimeType": "image/png"}
                try:
                    from ascended_browser._app.browser_capture import image_dimensions

                    dims = image_dimensions(composite)
                    if dims:
                        image["width"], image["height"] = dims
                except Exception:
                    pass
                receipt_lines.insert(0, describe_diff(stats, against=against))
        if compare_error:
            captured["compare"] = {"against": compare_with, "error": compare_error}
            receipt_lines.insert(0, f"Not compared: {compare_error}. The new picture is attached on its own.")
    if purpose == "evidence":
        from ascended_browser._app.browser_capture import save_evidence

        captured["saved_path"] = save_evidence(
            session_id, base64.b64decode(image["data"]), str(image.get("mimeType") or ""),
            label=label or str(captured.get("title") or ""),
        )
        receipt_lines[-1] = capture_receipt(captured)
    image["label"] = label or (
        f"Browser evidence: {captured.get('title') or captured.get('url') or tab_id}"
        if purpose == "evidence"
        else f"Browser screenshot: {captured.get('title') or captured.get('url') or tab_id}"
    )[:120]
    return {
        **captured,
        "success": True,
        "output": "\n".join(receipt_lines),
        "images": [image],
        "visual_audience": "user" if purpose == "evidence" else "model",
    }

def _browser_viewport_failure(exc: BaseException) -> Dict[str, Any]:
    """Project viewport failures without mislabeling them as navigation."""
    message = str(exc)
    lowered = message.casefold()
    typed_kind = str(getattr(exc, "error_kind", "") or "").strip()
    requires_new_runtime = bool(getattr(exc, "requires_new_runtime", False)) or (
        "next safe browser launch" in lowered
    )

    if typed_kind:
        error_kind = typed_kind
    elif "browser restarted" in lowered or "runtime restarted" in lowered:
        error_kind = "runtime_restarted"
    elif "in user control" in lowered or "after handback" in lowered:
        error_kind = "browser_in_user_control"
    elif "another tab" in lowered and "watched" in lowered:
        error_kind = "watched_tab_conflict"
    elif (
        "another browser actor" in lowered
        or "agent tab lease" in lowered
        or "lease mismatch" in lowered
    ):
        error_kind = "browser_actor_conflict"
    elif ("dimension" in lowered or "width and height" in lowered) and (
        "integer" in lowered or "positive" in lowered or "invalid" in lowered
    ):
        error_kind = "invalid_dimensions"
    elif (
        "requires tab_id" in lowered
        or "action must" in lowered
        or "viewport action" in lowered
        or "set requires width and height" in lowered
        or "requires a preset or width and height" in lowered
        or "preset must be" in lowered
        or "unknown tab" in lowered
    ):
        error_kind = "invalid_request"
    elif (
        "dynamic viewport is unavailable" in lowered
        or "dynamic browser sizing is unavailable" in lowered
        or "dynamic browser sizing is not enabled" in lowered
        or "runtime is not active" in lowered
        or "no verified launch viewport" in lowered
    ):
        error_kind = "viewport_unavailable"
    elif "geometry" in lowered and any(
        marker in lowered for marker in ("stale", "revision", "backwards", "changed")
    ):
        error_kind = "geometry_conflict"
    elif "geometry" in lowered or "usable dimensions" in lowered:
        error_kind = "geometry_unavailable"
    elif "resize" in lowered or "window is not bound" in lowered:
        error_kind = "native_resize_failed"
    else:
        error_kind = "viewport_failed"

    result: Dict[str, Any] = {
        "error": f"{type(exc).__name__}: {message}",
        "error_kind": error_kind,
        "exit_code": 1,
    }
    if requires_new_runtime:
        result["requires_new_runtime"] = True
    return result

async def _dispatch_browser_workspace(tool: str, args: Dict, manager, owner: str,
                                      session_id: str, run_id: Optional[str],
                                      *, actor: str = "parent", actor_id: str = "parent",
                                      parallel_browser_tab: bool = False) -> Dict:
    try:
        # A parent per-tab wave (agent_loop) reads several tabs at once; the
        # manager then takes only each tab's lock, as a parallel browser_act does.
        _parallel_parent_read = bool(parallel_browser_tab) and actor == "parent"
        tab_tools = {"browser_observe", "browser_evaluate", "browser_screenshot", "browser_close", "browser_login", "browser_extract"}
        evidence_read = tool == "browser_extract" and args.get("evidence_ref")
        if tool in tab_tools and not evidence_read and not str(args.get("tab_id") or "").strip():
            raise ValueError(f"{tool} requires tab_id. Use browser_tabs to find this session's tab ID, then resend with tab_id. No browser action was dispatched.")
        if tool == "browser_evaluate" and not any(args.get(k) for k in ("function", "expression", "code", "js", "script")):
            raise ValueError("browser_evaluate requires function and tab_id. No script was dispatched.")
        if tool == "browser_extract" and args.get("save_to"):
            if actor != "parent":
                raise PermissionError("browser_extract save_to is available to the main agent only.")
            from ascended_browser.runtime.tool_execution import get_active_workspace, _resolve_tool_path
            if not get_active_workspace():
                raise ValueError("browser_extract save_to requires an active workspace.")
            if not isinstance(args["save_to"], str) or any(c in args["save_to"] for c in "\r\n\0"):
                raise ValueError("browser_extract save_to must be a file path without line breaks.")
            # Validate before reading; WriteFileTool repeats authoritative checks
            # at the effect, including read-only job context and path grants.
            _resolve_tool_path(args["save_to"])
        if tool == "browser_workspace_status":
            visible = manager.status_for_actor(
                owner, session_id, actor=actor, actor_id=actor_id,
            )
            digest = (
                manager.digest(owner, session_id)
                if actor == "user"
                else json.dumps(visible, ensure_ascii=False, separators=(",", ":"))[:8000]
            )
            return _scope_browser_result(
                {**visible, "digest": digest, "exit_code": 0}, manager, owner, session_id, args,
            )
        if tool == "browser_open":
            raw_urls = args.get("urls")
            # Several OpenAI-compatible providers materialize every optional
            # schema property, so a perfectly valid single-URL call arrives as
            # {"url": "https://…", "urls": []}.  Empty alternatives are not
            # a second input.  Treat them as omitted while still rejecting two
            # genuinely populated forms.
            single_url = str(args.get("url") or "").strip()
            has_batch_urls = raw_urls not in (None, [])
            if has_batch_urls:
                if single_url:
                    raise ValueError("Use either url or urls, not both")
                if not isinstance(raw_urls, list):
                    raise ValueError("urls must be an array")
                urls = [str(item or "").strip() for item in raw_urls]
                if not urls or any(not item for item in urls):
                    raise ValueError("urls must contain at least one non-empty URL")
                if len(urls) > 5:
                    raise ValueError(
                        f"browser_open accepts at most 5 URLs per batch; got {len(urls)}. "
                        "Open the first 5, read them, then open the rest in another batch."
                    )
                # Independent by default: "check these sites" with one dead host
                # must keep the tabs that loaded. Two live batches were destroyed
                # by a single DNS miss each under the old atomic default.
                # All-or-nothing is an explicit opt-in (atomic=true). A false
                # boolean is not a request: several providers materialize every
                # optional property, so `allow_partial: false` arrives unasked.
                allow_partial = args.get("atomic") is not True
                semaphore = asyncio.Semaphore(3)

                async def _open_one(index: int, url: str) -> dict:
                    try:
                        async with semaphore:
                            item = await asyncio.wait_for(
                                _open_browser_after_runtime_transition(
                                    manager,
                                    owner, session_id, url,
                                    disposition=str(args.get("disposition") or "handoff"),
                                    actor=actor, actor_id=actor_id,
                                    # A batch promises independent tabs and
                                    # atomic rollback. Reusing/navigating an
                                    # existing tab would make rollback unable
                                    # to restore its previous page.
                                    reuse=False,
                                    _capacity_reserved=True,
                                ),
                                timeout=60,
                            )
                        return {
                            "request_index": index,
                            "requested_url": url,
                            "success": True,
                            **item,
                        }
                    except Exception as exc:
                        return {
                            "request_index": index,
                            "requested_url": url,
                            "url": url,
                            "success": False,
                            **_browser_operation_failure(exc),
                        }

                async with _browser_capacity_scope(manager, owner, len(urls), actor):
                    tasks = [
                        asyncio.create_task(_open_one(index, url))
                        for index, url in enumerate(urls)
                    ]
                    try:
                        opened = list(await asyncio.gather(*tasks))
                    except asyncio.CancelledError:
                        for task in tasks:
                            task.cancel()
                        completed = await asyncio.gather(*tasks, return_exceptions=True)
                        for item in completed:
                            if (
                                isinstance(item, dict)
                                and item.get("success")
                                and item.get("tab_id")
                                and not item.get("reused")
                            ):
                                try:
                                    await manager.close_tab(
                                        owner, session_id, str(item["tab_id"]),
                                        actor=actor, actor_id=actor_id,
                                    )
                                except Exception:
                                    pass
                        raise
                tabs = [item for item in opened if item.get("success")]
                failed = len(urls) - len(tabs)
                if failed and not allow_partial:
                    for item in tabs:
                        if item.get("tab_id") and not item.get("reused"):
                            try:
                                await manager.close_tab(
                                    owner, session_id, str(item["tab_id"]),
                                    actor=actor, actor_id=actor_id,
                                )
                            except Exception:
                                pass
                    # The batch is atomic by default, so one unrelated failure
                    # (a DNS miss, a 404) discards tabs that opened correctly.
                    # Name the escape hatch in the failure itself: without it a
                    # caller cannot tell a retry of the same batch from the one
                    # flag that would preserve the work.
                    for item in opened:
                        if item.get("success"):
                            item["success"] = False
                            item["rolled_back"] = True
                            item["error_kind"] = "batch_rolled_back"
                            item["error"] = (
                                f"Batch rolled back: {failed} of {len(urls)} URLs failed, so "
                                "every tab that opened was closed because atomic=true. Retry "
                                "without atomic to keep the tabs that succeed, or reopen the "
                                "working URLs alone."
                            )
                            item["retry_with"] = {"atomic": False}
                    tabs = []
                failed_rows = [item for item in opened if not item.get("success")]
                batch_error = ""
                if failed:
                    names = ", ".join(
                        f"{item.get('requested_url') or item.get('url')} ({item.get('error_kind') or 'failed'})"
                        for item in failed_rows if not item.get("rolled_back")
                    )
                    batch_error = (
                        f"{failed} of {len(urls)} URLs did not open: {names}. "
                        + (f"The other {len(tabs)} tabs are open; use their tab_ids from results."
                           if tabs else "No tab from this batch is open.")
                    )
                result = {
                    # Diagnostics first: projections and truncation keep key order.
                    **({"error": batch_error} if batch_error else {}),
                    "results": opened,
                    # `results` is the single authoritative ordered mapping.
                    # Do not duplicate the same tab ids under a second `tabs`
                    # collection: models copied ids across the two shapes and
                    # occasionally invented a hybrid id that never existed.
                    "requested": len(urls),
                    "opened": len(tabs),
                    "failed": len(urls) - len(tabs),
                    "partial_failure": bool(failed and allow_partial and tabs),
                    "status": "complete" if not failed else ("partial" if allow_partial and tabs else "failed"),
                    "failure_class": (
                        "" if not failed
                        else "browser_batch_partial" if allow_partial and tabs
                        else "browser_batch_failed"
                    ),
                    "exit_code": 0 if not failed else 1,
                }
            else:
                result = await _open_browser_after_runtime_transition(
                    manager,
                    owner, session_id, single_url or "about:blank",
                    disposition=str(args.get("disposition") or "handoff"),
                    actor=actor, actor_id=actor_id,
                    reuse=bool(args.get("reuse", True)),
                    read=args.get("read", False),
                )
        elif tool == "browser_tabs":
            action = str(args.get("action") or "list")
            if action == "list":
                result = manager.status_for_actor(
                    owner, session_id, actor=actor, actor_id=actor_id,
                )
            elif action == "close":
                # Cleanup is the one place a run closes many tabs at once, and
                # one call per tab was fourteen round trips in a single live
                # session. Closing is independent per tab, so a list closes
                # them in order and reports each outcome on its own.
                from ascended_browser._app.browser_workspace.manager_core import describe_exception

                wanted = _requested_tab_ids(args)
                if len(wanted) == 1:
                    result = await manager.close_tab(
                        owner, session_id, wanted[0], actor=actor, actor_id=actor_id,
                    )
                else:
                    closed: list[dict] = []
                    for one in wanted:
                        try:
                            outcome = await manager.close_tab(
                                owner, session_id, one, actor=actor, actor_id=actor_id,
                            )
                            closed.append({"tab_id": one, "closed": True,
                                           **({"tab": outcome} if isinstance(outcome, dict) else {})})
                        except Exception as exc:  # one bad id must not strand the rest
                            closed.append({"tab_id": one, "closed": False,
                                           "error": describe_exception(exc)})
                    failed = [item for item in closed if not item["closed"]]
                    result = {
                        "results": closed,
                        "requested": len(wanted),
                        "closed": len(closed) - len(failed),
                        "failed": len(failed),
                        "exit_code": 0 if not failed else 1,
                    }
                    if failed:
                        result["error"] = "; ".join(
                            f"{item['tab_id']}: {item['error']}" for item in failed
                        )
            elif action == "sleep":
                result = (
                    await manager.reconcile_worker_tabs(
                        owner, session_id, actor_id, terminal_state="sleeping",
                    )
                    if actor == "worker" else await manager.sleep(owner, session_id)
                )
            else: raise ValueError(f"Unsupported browser_tabs action: {action}")
        elif tool == "browser_viewport":
            tab_id = str(args.get("tab_id") or "").strip()
            action = str(args.get("action") or "").strip().lower()
            if not tab_id:
                raise ValueError("browser_viewport requires tab_id")
            if action not in {"get", "set", "restore", "emulate"}:
                raise ValueError("browser_viewport action must be get, set, restore, or emulate")
            width = height = None
            preset = str(args.get("preset") or "").strip().lower()
            if action == "emulate":
                emulate_kwargs: Dict[str, Any] = {}
                for name, allowed in (
                    ("color_scheme", {"light", "dark", "no-preference"}),
                    ("reduced_motion", {"reduce", "no-preference"}),
                    ("forced_colors", {"active", "none"}),
                ):
                    value = args.get(name)
                    if value is None or value == "":
                        continue
                    value = str(value).strip().lower()
                    if value not in allowed:
                        raise ValueError(f"browser_viewport {name} must be one of {', '.join(sorted(allowed))}")
                    emulate_kwargs[name] = value
                if args.get("offline") is not None:
                    if not isinstance(args.get("offline"), bool):
                        raise ValueError("browser_viewport offline must be true or false")
                    emulate_kwargs["offline"] = bool(args["offline"])
                clear = bool(args.get("clear"))
                if not emulate_kwargs and not clear:
                    raise ValueError(
                        "browser_viewport action=emulate needs at least one of color_scheme, reduced_motion, "
                        "forced_colors, offline, or clear=true"
                    )
                result = await manager.emulate(
                    owner, session_id, tab_id, clear=clear, actor=actor, actor_id=actor_id, **emulate_kwargs,
                )
            elif action == "set" and preset:
                # The live view's profile buttons; the manager owns the sizes.
                if preset not in {"phone", "tablet", "desktop"}:
                    raise ValueError("browser_viewport preset must be phone, tablet, or desktop")
            elif action == "set":
                if args.get("width") is None or args.get("height") is None:
                    raise ValueError("browser_viewport set requires a preset or width and height")
                if isinstance(args.get("width"), bool) or isinstance(args.get("height"), bool):
                    raise ValueError("browser_viewport width and height must be positive integers")
                if any(
                    isinstance(value, float) and not value.is_integer()
                    for value in (args.get("width"), args.get("height"))
                ):
                    raise ValueError("browser_viewport width and height must be positive integers")
                try:
                    width = int(args["width"])
                    height = int(args["height"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "browser_viewport width and height must be positive integers"
                    ) from exc
                if width <= 0 or height <= 0:
                    raise ValueError("browser_viewport width and height must be positive integers")
            if action != "emulate":
                result = await manager.viewport(
                    owner,
                    session_id,
                    tab_id=tab_id,
                    action=action,
                    width=width,
                    height=height,
                    actor=actor,
                    actor_id=actor_id,
                    **({"preset": preset} if action == "set" and preset else {}),
                )
        elif tool == "browser_screenshot":
            result = await _browser_screenshot(
                manager, owner, session_id, args, actor, actor_id,
                parallel_tab=_parallel_parent_read,
            )
        elif tool == "browser_observe":
            _assert_browser_tab_access(manager, owner, session_id, str(args.get("tab_id") or ""), actor, actor_id)
            view = {
                key: str(args[key]).strip() for key in ("query", "within", "cursor", "filter")
                if isinstance(args.get(key), (str, int)) and str(args[key]).strip()
            }
            if view.get("filter") == "interactive":
                view.pop("filter")  # the default; not a narrowing
            # Only a narrowed view is passed on: a plain observe keeps the exact
            # call every manager already supports.
            result = await manager.observe(
                owner, session_id, str(args.get("tab_id") or ""),
                actor=actor, actor_id=actor_id, **({"view": view} if view else {}),
                **({"parallel_tab": True} if _parallel_parent_read else {}),
            )
        elif tool == "browser_act":
            from ascended_browser._app.browser_workspace.manager import coerce_browser_act_action
            action = coerce_browser_act_action(args if isinstance(args, dict) else {})
            # Some providers nest the target ID alongside action.kind. Resolve
            # only an explicit, unambiguous ID; never guess the selected tab.
            candidates = (args.get("tab_id"), action.pop("tab_id", None))
            if any(value is not None and not isinstance(value, str) for value in candidates):
                raise ValueError("browser_act tab_id must be a non-empty string")
            tab_ids = {value.strip() for value in candidates if isinstance(value, str) and value.strip()}
            if len(tab_ids) != 1:
                raise ValueError("browser_act requires one unambiguous tab_id; provide it at the top level and do not supply conflicting IDs")
            tab_id = tab_ids.pop()
            args = {**args, "tab_id": tab_id}
            _assert_browser_tab_access(manager, owner, session_id, tab_id, actor, actor_id)
            resolved_upload_paths: list[str] | None = None
            if action.get("kind") == "upload":
                if actor == "worker":
                    # Browser children never choose arbitrary filesystem paths.
                    # The durable supervisor resolves the parent's explicit file
                    # list up front and exposes only opaque, child-local IDs.
                    if action.get("paths"):
                        raise PermissionError(
                            "browser sub-agents must upload with upload_ids from their assignment, not paths"
                        )
                    from ascended_browser.runtime.subagents import resolve_browser_upload_ids

                    resolved_upload_paths = resolve_browser_upload_ids(action.get("upload_ids") or [])
                else:
                    # Parent browser calls retain the ordinary workspace-relative
                    # path contract.
                    resolved_upload_paths = _resolve_upload_paths(action.get("paths") or [], session_id)
            if resolved_upload_paths is not None:
                # Resolve opaque worker grants before passing host paths to the
                # browser backend. The capability boundary remains enforced even
                # though contextual action authority belongs to the agent.
                action["paths"] = resolved_upload_paths
            act_kwargs = {"actor": actor, "actor_id": actor_id}
            if parallel_browser_tab and actor == "parent":
                act_kwargs["parallel_tab"] = True
            result = await manager.act(
                owner, session_id, tab_id, action,
                **act_kwargs,
            )
        elif tool == "browser_extract":
            evidence_ref = str(args.get("evidence_ref") or "").strip()
            if evidence_ref:
                # The opaque reference is already session-owned authority. A
                # repeated natural-language objective cannot change immutable
                # archived bytes, so accept and ignore it instead of forcing a
                # repair round. A supplied tab still must belong to the caller.
                tab_id = str(args.get("tab_id") or "").strip()
                if tab_id:
                    _assert_browser_tab_access(
                        manager, owner, session_id, tab_id, actor, actor_id,
                    )
                result = await _read_browser_evidence(
                    session_id, evidence_ref,
                    cursor=args.get("cursor", 0), limit=args.get("limit", 8000),
                )
            elif str(args.get("read") or "").strip().casefold() in {"console", "network", "inspect", "design", "audit"}:
                # Evidence the tab already holds (what the page logged, what it
                # requested) or one element's hit test: no snapshot, no page
                # script written by the model.
                read = str(args.get("read") or "").strip().casefold()
                tab_id = str(args.get("tab_id") or "")
                _assert_browser_tab_access(manager, owner, session_id, tab_id, actor, actor_id)
                if read == "audit":
                    result = await manager.read_audit(
                        owner, session_id, tab_id, checks=args.get("checks"),
                        actor=actor, actor_id=actor_id,
                    )
                elif read == "design":
                    result = await manager.read_design(
                        owner, session_id, tab_id, ref=str(args.get("target_ref") or ""),
                        actor=actor, actor_id=actor_id,
                    )
                elif read == "inspect":
                    result = await manager.inspect_ref(
                        owner, session_id, tab_id, ref=str(args.get("target_ref") or ""),
                        actor=actor, actor_id=actor_id,
                    )
                else:
                    result = await manager.read_log(
                        owner, session_id, tab_id, read=read,
                        level=str(args.get("level") or ""),
                        pattern=str(args.get("filter") or ""),
                        types=args.get("types"),
                        failed_only=bool(args.get("failed_only")),
                        navigations=args.get("navigations", 1),
                        cursor=args.get("cursor", 0),
                        max_results=args.get("max_results"),
                        actor=actor, actor_id=actor_id,
                    )
            elif args.get("schema") not in (None, "", {}):
                # Records in the caller's shape from the whole page's text (see
                # browser_structured_extract). The page read is the ordinary
                # extract, so tab access, frames and locking are unchanged.
                from ascended_browser._app.browser_structured_extract import extract_structured, normalize_schema

                tab_id = str(args.get("tab_id") or "")
                _assert_browser_tab_access(manager, owner, session_id, tab_id, actor, actor_id)
                schema = normalize_schema(args.get("schema"))
                page_read = await manager.extract(
                    owner, session_id, tab_id, instruction="", actor=actor, actor_id=actor_id,
                    **({"parallel_tab": True} if _parallel_parent_read else {}),
                )
                if not page_read.get("success", True) or page_read.get("error"):
                    result = page_read
                else:
                    structured = await extract_structured(
                        str(page_read.get("text") or ""),
                        schema=schema,
                        instruction=str(args.get("instruction") or ""),
                        already_collected=args.get("already_collected"),
                        cursor=args.get("cursor", 0),
                        owner=owner, session_id=session_id,
                    )
                    result = {
                        "source": "structured_extraction",
                        "tab_id": tab_id,
                        "url": page_read.get("url"),
                        "title": page_read.get("title"),
                        **structured,
                    }
            elif str(args.get("selector") or "").strip():
                _assert_browser_tab_access(
                    manager, owner, session_id, str(args.get("tab_id") or ""), actor, actor_id,
                )
                result = await manager.query_elements(
                    owner, session_id, str(args.get("tab_id") or ""),
                    selector=str(args.get("selector") or ""),
                    attributes=args.get("attributes"),
                    max_results=args.get("max_results"),
                    include_text=args.get("include_text", True),
                    **({"cursor": args["cursor"]} if args.get("cursor") not in (None, "", 0, "0") else {}),
                    actor=actor, actor_id=actor_id,
                )
            else:
                _assert_browser_tab_access(
                    manager, owner, session_id, str(args.get("tab_id") or ""), actor, actor_id,
                )
                extract_kwargs = {
                    "instruction": str(args.get("instruction") or ""),
                    "actor": actor,
                    "actor_id": actor_id,
                }
                if str(args.get("target_ref") or "").strip():
                    extract_kwargs["target_ref"] = str(args["target_ref"]).strip()
                if str(args.get("find") or "").strip():
                    extract_kwargs["find"] = str(args["find"]).strip()[:200]
                if args.get("from_end"):
                    extract_kwargs["from_end"] = True
                if _parallel_parent_read:
                    extract_kwargs["parallel_tab"] = True
                result = await manager.extract(
                    owner, session_id, str(args.get("tab_id") or ""),
                    **extract_kwargs,
                )
            if args.get("save_to"):
                result = await _save_browser_extraction(result, args, session_id)
        elif tool == "browser_flow":
            flow_action = str(args.get("action") or "").strip().lower()
            tab_id = str(args.get("tab_id") or "").strip()
            flow_id = str(args.get("flow_id") or "").strip()
            if actor != "parent":
                # Flows are the owner's saved work and a run replays
                # consequential steps. A browser child shares the owner
                # identity but not that authority: it may not read, change or
                # replay them.
                result = {"success": False,
                          "error": "browser_flow is available to the main agent only, not to browser sub-agents."}
            elif flow_action in {"recorded", "save", "run"} and not tab_id:
                result = {"success": False, "error": f"browser_flow action={flow_action} needs tab_id."}
            elif flow_action in {"show", "run", "delete"} and not flow_id:
                result = {"success": False,
                          "error": f"browser_flow action={flow_action} needs flow_id; action=list shows them."}
            elif flow_action == "recorded":
                _assert_browser_tab_access(manager, owner, session_id, tab_id, actor, actor_id)
                result = manager.flow_recorded(owner, session_id, tab_id)
            elif flow_action == "save":
                _assert_browser_tab_access(manager, owner, session_id, tab_id, actor, actor_id)
                result = manager.flow_save(
                    owner, session_id, tab_id, name=str(args.get("name") or ""),
                    from_step=args.get("from_step"), to_step=args.get("to_step"),
                )
            elif flow_action == "list":
                result = manager.flow_list(owner)
            elif flow_action == "show":
                result = manager.flow_show(owner, flow_id)
            elif flow_action == "delete":
                result = manager.flow_delete(owner, flow_id)
            elif flow_action == "run":
                _assert_browser_tab_access(manager, owner, session_id, tab_id, actor, actor_id)
                result = await manager.flow_run(
                    owner, session_id, tab_id, flow_id,
                    variables=args.get("variables"),
                    start_url=str(args.get("start_url") or ""),
                    from_step=args.get("from_step"),
                    actor=actor, actor_id=actor_id,
                )
            else:
                result = {"success": False,
                          "error": "browser_flow action must be recorded, save, list, show, run or delete."}
            if isinstance(result, dict):
                result = {**result, "source": "browser_flow"}
        elif tool == "browser_evaluate":
            _assert_browser_tab_access(manager, owner, session_id, str(args.get("tab_id") or ""), actor, actor_id)
            function = (
                args.get("function") or args.get("expression") or args.get("code")
                or args.get("js") or args.get("script") or ""
            )
            result = await manager.evaluate(
                owner, session_id, str(args.get("tab_id") or ""), str(function), args.get("arg"),
                actor=actor, actor_id=actor_id,
            )
        elif tool == "browser_login":
            _assert_browser_tab_access(manager, owner, session_id, str(args.get("tab_id") or ""), actor, actor_id)
            result = await manager.login(
                owner, session_id, str(args.get("tab_id") or ""),
                account_hint=str(args.get("account_hint") or ""),
                submit=bool(args.get("submit", False)),
                actor=actor, actor_id=actor_id,
            )
        elif tool == "wait_for_bot_wall":
            _assert_browser_tab_access(manager, owner, session_id, str(args.get("tab_id") or ""), actor, actor_id)
            from ascended_browser._app.browser_wall import wait_for_bot_wall
            result = await wait_for_bot_wall(
                manager, owner, session_id, str(args.get("tab_id") or ""),
                max_wait_ms=args.get("max_wait_ms", 60_000),
                poll_interval_ms=args.get("poll_interval_ms", 5_000),
                apply_solver=bool(args.get("apply_solver", True)),
                actor=actor, actor_id=actor_id,
            )
        elif tool == "browser_wait":
            result = manager.status_for_actor(
                owner, session_id, actor=actor, actor_id=actor_id,
            )
        elif tool == "browser_resume":
            record = await manager.ensure_awake(owner, session_id)
            record.status = "active"; record.last_human_activity_at = time.time(); manager.store.save(record)
            public = record.public()
            # Tell the UI which parent-owned page represents the resumed work.
            # Dict order is creation order, not activity order, so focusing the
            # first tab can show an unrelated old page in a multi-tab session.
            parent_tabs = [
                tab for tab in public.get("tabs", [])
                if tab.get("owner_kind") == "parent"
                or (tab.get("lease") or {}).get("actor") == "parent"
            ]
            selected = max(
                parent_tabs or public.get("tabs", []),
                key=lambda tab: float(tab.get("updated_at") or 0),
                default={},
            )
            lost_state = None
            selected_tab_id = str(selected.get("tab_id") or "")
            if selected_tab_id:
                # Restart destroys browser-memory drafts.  Reload the selected
                # parent page instead of handing routine recovery back to the
                # user; the manager records the loss and returns it to the
                # agent so it can inspect and refill the new GET document.
                lost_state = await manager.reload_lost_parent_tab(
                    owner, session_id, selected_tab_id,
                )
                if lost_state is not None:
                    public = record.public()
            result = {
                "resumed": True,
                "selected_tab_id": selected_tab_id,
                **public,
            }
            if lost_state is not None:
                result["lost_page_state"] = lost_state
            if bool(args.get("observe_selected", False)) and result["selected_tab_id"]:
                result["page"] = await manager.observe(
                    owner, session_id, result["selected_tab_id"],
                )
                # Observe refreshes tab metadata/activity; return that current
                # projection rather than the pre-observe checkpoint fields.
                refreshed = record.public()
                result.update(refreshed)
        elif tool == "browser_attention_resolve":
            result = manager.resolve_attention(owner, session_id, str(args.get("attention_id") or ""), int(args.get("revision") or 0), str(args.get("resolution") or ""))
        else:
            raise ValueError(f"Unknown browser workspace tool: {tool}")
        result = _scope_browser_result(result, manager, owner, session_id, args)
        result.setdefault("exit_code", 0)
        return result
    except Exception as exc:
        if tool == "browser_viewport":
            result = _browser_viewport_failure(exc)
        else:
            result = _browser_operation_failure(exc)
        return _scope_browser_result(result, manager, owner, session_id, args)
