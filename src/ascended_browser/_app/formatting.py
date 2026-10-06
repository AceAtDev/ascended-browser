"""Generated from tool_execution.py by scripts/sync_from_ascended.py: the definitions
reachable from format_tool_result, browser_result_archive, browser_result_context. Do not edit; edit Ascended and re-sync."""


from __future__ import annotations


import json

import logging

import re

from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from ascended_browser._app.user_labels import AUTH_STATE_WORDS, page_label

from ascended_browser.runtime.constants import MAX_OUTPUT_CHARS, MAX_READ_CHARS, MAX_DIFF_LINES, DATA_DIR

logger = logging.getLogger(__name__)

_COMPACT_DEDUPE_MIN_CHARS = 200

def _compact_model_data(value: Any) -> Any:
    """Drop empty fields and repeated subtrees from the model-facing data dump.

    Null/empty values change no decision, and a receipt that nests the same
    object twice (present_artifact's ``deliverable.workspace_item`` and
    ``workspace_item``) cost about 2 KB per call. ``False`` and ``0`` are kept:
    they carry meaning. The raw result is untouched; only this copy shrinks.
    """
    seen: Dict[str, str] = {}

    def walk(item: Any, path: str) -> Any:
        if isinstance(item, dict):
            out = {}
            for key, child in item.items():
                if child is None or child == "" or child == [] or child == {}:
                    continue
                out[key] = walk(child, f"{path}.{key}" if path else str(key))
            return out
        if isinstance(item, list):
            return [walk(child, f"{path}[{index}]") for index, child in enumerate(item)]
        return item

    def dedupe(item: Any, path: str) -> Any:
        if isinstance(item, dict):
            encoded = json.dumps(item, sort_keys=True, default=str)
            if len(encoded) >= _COMPACT_DEDUPE_MIN_CHARS:
                if encoded in seen:
                    return f"(same as {seen[encoded]})"
                seen[encoded] = path or "data"
            return {key: dedupe(child, f"{path}.{key}" if path else str(key))
                    for key, child in item.items()}
        if isinstance(item, list):
            return [dedupe(child, f"{path}[{index}]") for index, child in enumerate(item)]
        return item

    if not isinstance(value, dict):
        return value
    return dedupe(walk(value, ""), "")

_FORMATTER_HANDLED_KEYS = {
    "stdout", "stderr", "exit_code", "content", "size",
    "response", "results", "session_id", "name", "model", "session_name",
    "success", "path", "action", "title", "doc_id", "version", "applied",
    "skipped", "count", "diagnostics", "excerpt", "error_code", "content_revision",
    "expected_revision",
    "error", "output", "images", "vision_required", "vision_note", "vision_prompt",
    # Host routing flag: marks images as model-vision-only so they are never
    # promoted to durable media. Nothing for the model to read.
    "inspection_only",
    "inspection", "files", "next_cursor", "coverage_complete",
    "full_file_coverage", "inspection_status", "warnings",
    "range_suggestions", "error_code",
    "_inspection_session_id", "_inspection_delivery_blocks",
    "pending_operation", "bw_handoff", "terminal", "terminal_sessions",
    "workspace_path", "sandbox_path", "saved_to", "workspace_notice", "capability_notice",
    # MCP normalization output. `mcp_content` restates, block for block, the
    # text already joined into `stdout`, so echoing it as raw JSON shipped every
    # MCP result to the model twice (and put base64 resource blobs in context).
    # `mcp_meta` is host plumbing, and `selection_required` is the same payload
    # as `ask_user`. The rest get compact branches below instead of a 25-field
    # dump per asset.
    "mcp_content", "mcp_meta", "selection_required",
    "structured_content", "media_artifacts", "presentation_artifacts", "presentation", "design_cards", "mcp_media_job",
    # Normalized lifecycle envelope (DS-001). `tool_status`/`error_message`/
    # `retryable`/`timing` are harness metadata for the UI and metrics; the
    # model already reads the error itself in its dedicated branch, and
    # `exit_code` stays process metadata only.
    "tool_status", "error_message", "retryable", "timing",
    # UI-only summary of a fetched page (title, description, shape).
    "page_view",
}

_BROWSER_MODEL_MAX_CHARS = 6_800

_BROWSER_SAVED_EVIDENCE_KEYS = (
    "saved_to", "saved_format", "saved_scope", "saved_bytes", "saved_sha256",
)

_BROWSER_ELEMENT_QUERY_KEYS = (
    "selector", "total", "showing", "cursor", "next_cursor",
)

_BROWSER_MODEL_TOP_LEVEL_KEYS = (
    # Outcomes and continuation precede page detail, including on partial failure.
    "success", "error", "error_code", "error_kind", "retry_with", "observed_options", "popup_strategy",
    "options_truncated", "committed",
    # A refusal that names no alternative is a dead end. `message` carries the
    # recovery instruction and `matches` the choices to pick from; dropping
    # them left "multiple logins matched" with no way to say which.
    "message", "matches",
    "tool_status", "status", "kind", "index", "exit_code",
    "scroll", "receipt", "verification_failed", "verification", "blocked", "attention_required",
    "blocker", "user_note", "remaining", "stopped_early", "stop_reason", "skipped_fields",
    "unattempted_fields",
    "attempted", "filled", "submitted", "submit_reason", "failed", "partial_failure", "uncertain",
    "reconcile_required",
    "request_index", "requested_url", "workspace_id", "tab_id", "observation_id",
    "page_revision", "url", "title", "login_available", "reused", "navigated",
    "page_unchanged", "page_note",
    "navigation_state", "auth_state", "disposition", "restorable", "restore_status",
    "resumed", "selected_tab_id", "lost_page_state", "detail", "upload", "self_healed",
    "completed", "attempted", "remaining", "stopped_early", "partial_failure", "results",
    "stop_reason", "unattempted_steps", "duration_ms", "postcondition_met", "resolution",
    "semantic_recovery", "semantic_replay", "delta", "observation_deferred",
    "action", "viewport", "available", "requires_new_runtime", "requested_width",
    "requested_height", "width", "height", "outer_width", "outer_height",
    "screen_width", "screen_height", "geometry_revision", "dynamic", "scope",
    "mobile_emulation", "resized", "restored", "clamped", "measured", "size_mode",
    "rolled_back", "closed",
    "evidence_ref", "observation_ref", "managed_output_ref", *_BROWSER_SAVED_EVIDENCE_KEYS,
    "evidence_archive_failed", "context_note", "tab_note", "invalid_fields",
    # Session-owned browser evidence continuation. These values are the paging
    # contract: dropping them makes the model guess offsets and can revive an
    # already-consumed prefix indefinitely.
    "source", "cursor", "next_cursor", "eof", "total_chars",
    # browser_extract answering one question about a long page.
    "passages", "passage_count", "find", "read_from",
    # What the page narrates about itself (role=status / aria-live=polite),
    # which is not the same as what went wrong on it.
    "page_status",
    # What the page reported since the last result: console errors, uncaught
    # exceptions, failed requests, as a few lines (src/browser_diagnostics.py).
    "diagnostics",
    # Why a refused click could not land (src/browser_inspect.py).
    "inspect",
    # What the action waited for and whether the page got there.
    "until",
    # Media/network emulation in force on the tab (browser_viewport emulate/restore).
    "emulation", "emulation_cleared", "cleared",
)

_BROWSER_MODEL_PAGE_KEYS = (
    "workspace_id", "tab_id", "observation_id", "page_revision", "url", "title",
    "login_available", "navigation_state", "blocked", "attention_required", "blocker",
    "auth_state", "element_count", "elements_omitted", "budget_exhausted", "complete",
    "incomplete", "coverage", "coverage_complete", "snapshot_complete", "truncated",
    "note", "user_note", "delta", "viewport", "blocking_overlays", "dismiss_candidates",
    "overlay_count", "frames_omitted", "frame_count", "projection_truncated",
    "elements_complete", "controls_observed", "controls_total", "frames_observed", "frames_total",
    "collection_diagnostics", "identity_omitted",
    "regions", "frames", "omitted_examples", "next_cursor", "view", "headings",
    # filter:"links" answers "where can this page take me" as text and URL.
    "content_blocks", "scroll_containers", "content_complete", "content_note", "content_blocks_omitted",
    "links", "link_count", "links_complete", "links_omitted",
)

_BROWSER_MODEL_ELEMENT_KEYS = (
    "ref", "observation_id", "role", "name", "text", "tag", "type", "value", "checked",
    "disabled", "href", "frame_id", "frame_name", "frame_url", "frame_path", "frame_index", "frame_scope",
    "file_names", "id", "label", "aria_label", "placeholder", "required", "options",
    "aria_controls", "aria_expanded", "aria_activedescendant", "aria_autocomplete",
    "controlled_regions", "containing_region",
    "selected_text", "context", "in_form", "hidden", "visible", "in_viewport",
    "name_attr", "form_id", "form_name", "fieldset", "nearby_question", "aria_checked", "identity_omitted",
    # Shared context stated once in the page's regions/frames tables, by id
    # (browser-observation-system-design.md). Dropping these would strand an
    # element from the region and frame it belongs to.
    "region", "controls", "frame", "on_screen",
    # Appeared since the previous observation of this tab (an opened popup's
    # options, a revealed follow-up field): what the last action changed.
    "new",
    # Visible text beside a control whose own name is missing or shared
    # (a radio labelled only "Radio button" beside the word "College").
    "nearby",
    # The page rejected the value (aria-invalid) and why.
    "invalid", "error",
)

_BROWSER_MODEL_EXTRACT_KEYS = (
    "instruction", "objective", "fields", "visible_errors", "no_match", "no_match_reason",
    "text_coverage", "controls_total", "controls_returned", "controls_complete",
)

_BROWSER_IDENTITY_KEYS = frozenset({
    "ref", "target_ref", "old_ref", "new_ref", "observation_id", "tab_id", "workspace_id",
    "href",
    "id", "name", "name_attr", "frame_id", "frame_path", "frame_name", "frame_url", "frame_scope",
    "evidence_ref", "observation_ref", "managed_output_ref",
})

_BROWSER_PRIVATE_KEYS = frozenset({
    "password", "secret", "access_token", "refresh_token", "authorization", "cookie",
    "cookies", "headers", "storage_state", "html", "css", "outer_html", "inner_html",
    "raw_dom", "screenshot", "screenshot_base64", "images", "image_data",
})

_BROWSER_FLOW_KEYS = (
    "success", "error", "source", "tab_id", "status", "saved", "deleted", "available",
    "flow_id", "name", "site", "start_url", "step_count", "variables", "skipped_sensitive",
    "runs", "recorded", "flows", "count", "total", "completed", "skipped", "steps",
    "remaining_steps", "next", "duration_ms", "model_calls",
)

_ELEMENT_QUERY_TEXT_CHARS = 7_000

_STRUCTURED_EXTRACT_KEYS = (
    "items_returned", "unverified_count", "unverified", "unverified_note", "strings_checked",
    "truncated", "next_cursor", "processed", "source_chars", "model",
)

_STRUCTURED_EXTRACT_TEXT_CHARS = 12_000

def _bounded_browser_text(value: Any, limit: int = 2_000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    # Keep late failures as well as the opening context. This is presentation,
    # never control value evidence for verification.
    marker = f" … [{len(text) - limit:,} chars omitted] … "
    head = _bounded_browser_text_head(text, limit)
    return text[:head] + marker + text[-max(1, limit - head - len(marker)):]

def _bounded_browser_text_head(text: str, limit: int) -> int:
    """How many leading characters ``_bounded_browser_text`` keeps verbatim."""
    if len(text) <= limit:
        return len(text)
    marker = f" … [{len(text) - limit:,} chars omitted] … "
    return max(0, (limit - len(marker)) * 2 // 3)

_BROWSER_READ_TEXT_CHARS = 5_000

_BROWSER_READ_TEXT_FLOOR = 800

def _has_form_state(fields: Any) -> bool:
    """An extraction reading back a form: field values are its evidence."""
    return isinstance(fields, list) and any(
        isinstance(field, dict) and any(
            field.get(key) not in (None, "", [], False)
            for key in ("value", "selected_text", "checked", "file_names")
        )
        for field in fields
    )

def _browser_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Bound structured presentation without serializing private runtime objects."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if key in _BROWSER_IDENTITY_KEYS:
            # Never manufacture a different, truncated target identifier.
            return value if len(value) <= 1_024 else None
        return _bounded_browser_text(value)
    if depth >= 6:
        return {"projection_truncated": True}
    if isinstance(value, dict):
        sensitive = value.get("sensitive") is True or str(value.get("type") or "").casefold() == "password"
        output = {}
        for child_key, child in list(value.items())[:80]:
            if not isinstance(child_key, str) or child_key.startswith("_") or child_key.casefold() in _BROWSER_PRIVATE_KEYS:
                continue
            if sensitive and child_key in {"value", "text", "selected_text", "requested", "observed"}:
                output[child_key] = "[masked]"
            else:
                safe = _browser_value(child, key=child_key, depth=depth + 1)
                if safe is None and child is not None:
                    output["identity_omitted"] = True
                else:
                    output[child_key[:120]] = safe
                if isinstance(child, (list, tuple)) and len(child) > 80:
                    output[child_key[:120] + "_omitted"] = len(child) - 80
        if len(value) > 80:
            output["projection_truncated"] = True
        return output
    if isinstance(value, (list, tuple)):
        return [_browser_value(item, depth=depth + 1) for item in value[:80]]
    return "[unsupported browser value]"

def _browser_keys(value: dict, keys: tuple) -> dict:
    output = {}
    for key in keys:
        if key not in value or value[key] in (None, "", [], {}):
            continue
        safe = _browser_value(value[key], key=key)
        if safe is None:
            output["identity_omitted"] = True
            continue
        if key in {"error", "user_note", "stop_reason", "observation_deferred", "detail", "note"} and not isinstance(safe, str):
            safe = _bounded_browser_text(json.dumps(safe, ensure_ascii=False))
        output[key] = safe
        if isinstance(value[key], (list, tuple)) and len(value[key]) > 80:
            output[key + "_omitted"] = len(value[key]) - 80
    return output

def _project_browser_page(page: Any) -> Dict[str, Any]:
    """Build a bounded observation; raw state remains available to its owner."""
    from ascended_browser._app.browser_viewport_content import CONTENT_BLOCK_MAX_CHARS
    if not isinstance(page, dict):
        return {}
    projected = _browser_keys(page, _BROWSER_MODEL_PAGE_KEYS)
    if isinstance(page.get("content_blocks"), list):
        for source, block in zip(page["content_blocks"], projected.get("content_blocks") or []):
            block["text"] = str(source.get("text") or "")[:CONTENT_BLOCK_MAX_CHARS]
            if len(str(source.get("text") or "")) > CONTENT_BLOCK_MAX_CHARS:
                block["truncated"] = True
                projected["content_complete"] = False
    elements = page.get("relevant_elements")
    if not isinstance(elements, list):
        elements = page.get("elements")
    if isinstance(elements, list):
        from ascended_browser._app.browser_observation_outline import observe_format
        # The outline's selection was already budgeted in rendered characters
        # by _project_snapshot; a second count cap would silently re-cut it.
        limit = 400 if observe_format() == "outline" else 80
        bounded_elements = []
        for element in elements[:limit]:
            if not isinstance(element, dict):
                continue
            safe = _browser_value(element)
            bounded_elements.append(_browser_keys(safe, _BROWSER_MODEL_ELEMENT_KEYS))
        if bounded_elements:
            projected["relevant_elements"] = bounded_elements
        omitted = max(0, int(page.get("elements_omitted") or 0)) + len(elements) - len(bounded_elements)
        if omitted:
            projected["elements_omitted"] = omitted
            projected["coverage_complete"] = False
    evidence_ref = page.get("evidence_ref") or page.get("observation_ref")
    if evidence_ref:
        projected["evidence_ref"] = _browser_value(evidence_ref, key="evidence_ref")
    relevant_text = page.get("relevant_text") or page.get("text") or page.get("visible_text") or page.get("accessibility_snapshot")
    if page.get("content_blocks") and page.get("text_read") is not True:
        relevant_text = ""
    if relevant_text and page.get("text_read") is True:
        # Private source for _fit_browser_projection, popped before rendering.
        projected["_text_source"] = str(relevant_text)
        projected["relevant_text"] = _bounded_browser_text(relevant_text, _BROWSER_READ_TEXT_CHARS)
    elif relevant_text:
        projected["relevant_text"] = _bounded_browser_text(relevant_text)
    return projected

def _fit_browser_projection(projected: dict, *, preserve_text: bool = False) -> dict:
    """Keep the JSON complete under the downstream managed-output preview cap.

    Outcomes stay ahead of page detail. Omitted controls are counted, and whole
    entries/identities are removed rather than clipped into invalid references.
    Text the call asked to read keeps its budget ahead of controls, and a read
    that still could not show everything says where the rest is.
    """
    pages = [projected]
    if isinstance(projected.get("page"), dict):
        pages.append(projected["page"])
    sources = {id(page): page.pop("_text_source") for page in pages if "_text_source" in page}
    projected = _fit_browser_projection_body(projected, preserve_text=preserve_text, sources=sources)
    for page in pages:
        source = sources.get(id(page))
        shown = page.get("relevant_text")
        if source is None or not isinstance(shown, str) or shown == source:
            continue
        head = _bounded_browser_text_head(source, len(shown))
        coverage = projected.get("text_coverage") if page is projected else None
        if (
            isinstance(coverage, dict)
            and coverage.get("strategy") in {"complete", "head"}
            and projected.get("evidence_ref")
        ):
            # An extraction's text is the whitespace-normalised page text that
            # an evidence_ref continuation pages over, so the first character
            # not shown is exactly where the next slice starts. The collector's
            # own cursor pointed past text this projection never showed.
            coverage.update({"complete": False, "returned_chars": head, "next_cursor": head})
            page["text_note"] = (
                f"Showing {head:,} of {len(source):,} characters of page text (and its end). "
                "Continue with browser_extract evidence_ref and cursor="
                f"{head} rather than re-reading the page."
            )
        else:
            page["text_note"] = (
                f"Showing {head:,} of {len(source):,} characters of page text (and its end). "
                "For the rest of this tab use browser_extract with find (words to look "
                "for) or from_end, not a fresh read."
            )
    for page in pages:
        elements = page.get("relevant_elements")
        coverage = page.get("coverage")
        if isinstance(elements, list) and isinstance(coverage, dict):
            originally_shown = coverage.get("shown")
            coverage["shown"] = len(elements)
            if not elements or any("on_screen" in e or "in_viewport" in e for e in elements):
                coverage["on_screen_shown"] = sum(bool(e.get("on_screen", e.get("in_viewport"))) for e in elements)
            elif originally_shown != len(elements):
                # Compact JSON controls omit per-node visibility. Preserve the
                # collector's count only while its selection is unchanged;
                # after trimming we cannot honestly recompute that count.
                coverage.pop("on_screen_shown", None)
    return projected

def browser_projection_char_limit(projected: dict) -> int:
    """Formatter-owned model budget for controls, reading evidence and outcomes."""
    from ascended_browser._app.browser_observation_outline import observe_format, outline_budget
    page = projected.get("page") if isinstance(projected.get("page"), dict) else projected
    reading = bool(page.get("content_blocks"))
    if observe_format() == "outline":
        return outline_budget() + (12_000 if reading else 4_000)
    return 24_000 if reading else _BROWSER_MODEL_MAX_CHARS

def _fit_browser_projection_body(projected: dict, *, preserve_text: bool, sources: dict) -> dict:
    from ascended_browser._app.browser_observation_outline import observe_format, render_projection

    limit = browser_projection_char_limit(projected)
    if observe_format() == "outline":
        def size():
            return len(render_projection(projected))
    else:
        def size():
            return len(json.dumps(projected, ensure_ascii=False, separators=(", ", ": ")))

    if size() <= limit:
        return projected
    projected["projection_truncated"] = True
    pages = [projected]
    if isinstance(projected.get("page"), dict):
        pages.append(projected["page"])
    # Deltas/diagnostics repeat state already represented by current controls.
    # Sacrifice them before dropping every actionable reference on a long page.
    for page in pages:
        for key in ("delta", "collection_diagnostics", "semantic_recovery", "semantic_replay"):
            if key in page and size() > limit:
                page.pop(key)
                page.setdefault("projection_omitted", []).append(key)
    for page in pages:
        if size() <= limit:
            break
        if "relevant_text" in page and not preserve_text and id(page) not in sources:
            page["relevant_text"] = _bounded_browser_text(page["relevant_text"], 240)
    for page in pages:
        blocks = page.get("content_blocks")
        while isinstance(blocks, list) and len(blocks) > 1 and size() > limit:
            blocks.pop()
            page["content_blocks_omitted"] = int(page.get("content_blocks_omitted") or 0) + 1
            page["content_complete"] = False
        if isinstance(blocks, list) and size() > limit:
            for block in blocks:
                if len(block.get("text") or "") > 800:
                    block["text"] = block["text"][:800]
                    block["truncated"] = True
                    page["content_complete"] = False
    retained_elements: list[tuple[dict, list]] = []
    for page in pages:
        elements = page.get("relevant_elements")
        while isinstance(elements, list) and len(elements) > 3 and size() > limit:
            elements.pop()
            page["elements_omitted"] = int(page.get("elements_omitted") or 0) + 1
            page["coverage_complete"] = False
        if isinstance(elements, list):
            retained_elements.append((page, elements))
    # A large form keeps failures/uncertainty before successful old fields.
    fields = projected.get("fields")
    while isinstance(fields, list) and fields and size() > limit:
        removable = next((i for i in range(len(fields) - 1, -1, -1)
                          if isinstance(fields[i], dict) and fields[i].get("status") != "failed"
                          and not fields[i].get("error") and fields[i].get("verified") is not False), len(fields) - 1)
        fields.pop(removable)
        projected["fields_omitted"] = int(projected.get("fields_omitted") or 0) + 1
    # Text the call asked to read gives way only now, and only down to a floor:
    # the next action's few controls and every outcome still come first.
    for page in pages:
        source = sources.get(id(page))
        shown = page.get("relevant_text")
        if source is None or not isinstance(shown, str):
            continue
        allowance = len(shown)
        while size() > limit and allowance > _BROWSER_READ_TEXT_FLOOR:
            allowance = max(_BROWSER_READ_TEXT_FLOOR, allowance - (size() - limit) - 64)
            page["relevant_text"] = _bounded_browser_text(source, allowance)
    # If outcome compaction still cannot fit, the final three controls may be
    # reduced now that successful field receipts had their chance to shrink.
    # This ordering prevents a large verified batch from erasing every normal
    # next action (Save/Next/Review) before dropping repetitive success detail.
    for page, elements in retained_elements:
        while elements and size() > limit:
            elements.pop()
            page["elements_omitted"] = int(page.get("elements_omitted") or 0) + 1
            page["coverage_complete"] = False
    # Bound nested diagnostic/result strings before dropping any outcome key.
    def shorten(value):
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if key == "relevant_text" and id(value) in sources:
                    continue
                if isinstance(child, str) and key not in _BROWSER_IDENTITY_KEYS:
                    value[key] = _bounded_browser_text(child, 240)
                else:
                    shorten(child)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, str):
                    value[index] = _bounded_browser_text(child, 240)
                else:
                    shorten(child)
    if size() > limit:
        shorten(projected)
    # Diagnostic arrays can be huge even with small members. Keep omission
    # explicit; scalar truth (false, remaining counts, verification) survives.
    def trim_lists(value):
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if isinstance(child, list):
                    while child and size() > limit:
                        child.pop()
                        value[key + "_omitted"] = int(value.get(key + "_omitted") or 0) + 1
                elif isinstance(child, dict):
                    trim_lists(child)
    if size() > limit:
        trim_lists(projected)
    if size() > limit:
        for key in reversed(list(projected)):
            if key in {"success", "error", "receipt", "verification_failed", "verification", "remaining", "stopped_early", "stop_reason", "user_note", "projection_truncated"}:
                continue
            projected.pop(key)
            projected.setdefault("projection_omitted", []).append(key)
            if size() <= limit:
                break
    # Only hostile/unbounded nested outcome dictionaries reach here. Preserve
    # the receipt's actual verdict and known scalar evidence, never a raw dump.
    if size() > limit:
        for key in ("receipt", "verification"):
            if isinstance(projected.get(key), dict):
                projected[key] = {k: v for k, v in projected[key].items()
                                  if k in {"action", "verified", "ok", "verification", "sensitive", "target_ref", "target_label", "requested", "observed", "observed_length",
                                           "operation_id", "effect_state", "dispatch_status", "retry_safe", "failed_stage"}
                                  and not isinstance(v, (dict, list))}
    return projected

def _project_browser_result(result: Any, *, _depth: int = 0, preserve_text: bool = False) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {"error": "Browser returned a non-object result"}
    if result.get("source") == "browser_evidence":
        # This is already a caller-requested, bounded slice from immutable
        # session-owned evidence. Preserve it contiguously and keep the exact
        # cursor contract. Running it through the ordinary page projection
        # would head/tail-truncate the bytes and then advance past evidence the
        # model never received.
        projected = _browser_keys(result, (
            "success", "error", "error_code", "source", "evidence_ref",
            "cursor", "next_cursor", "eof", "total_chars",
            *_BROWSER_SAVED_EVIDENCE_KEYS,
        ))
        text = result.get("text")
        if isinstance(text, str):
            projected["relevant_text"] = text
        return projected
    if result.get("source") == "browser_flow":
        # A flow result is its own contract: the id and variables to run it,
        # and after a stopped run, what is done, what remains and how to
        # resume. The page projection keeps none of those keys.
        projected = _browser_keys(result, _BROWSER_FLOW_KEYS)
        for key in ("steps", "remaining_steps", "flows"):
            if isinstance(projected.get(key), list) and len(projected[key]) > 80:
                projected[key + "_omitted"] = len(projected[key]) - 80
                projected[key] = projected[key][:80]
        return projected
    if result.get("source") == "structured_extraction":
        # The answer is the data in the caller's shape; the page projection
        # keeps none of it. Verification and continuation lead, so a clipped
        # list still says which values are unconfirmed and where to resume.
        projected = _browser_keys(result, (
            "success", "error", "source", "tab_id", "url", "title",
            *_STRUCTURED_EXTRACT_KEYS,
        ))
        data_text = json.dumps(result.get("data"), ensure_ascii=False, indent=1)
        if len(data_text) > _STRUCTURED_EXTRACT_TEXT_CHARS:
            data_text = (
                data_text[:_STRUCTURED_EXTRACT_TEXT_CHARS].rsplit("\n", 1)[0]
                + "\n[Data clipped by the output budget; pass already_collected with the "
                "items above, or ask for fewer fields.]"
            )
            projected["projection_truncated"] = True
        projected["relevant_text"] = data_text
        return projected
    if result.get("source") in {"console_log", "network_log", "element_inspect", "design_read", "page_audit"}:
        # A log or an inspection is its text: the lines are already bounded
        # per entry and paged by cursor, and the page projection keeps none of
        # the keys that page it.
        projected = _browser_keys(result, (
            "success", "error", "source", "read", "tab_id", "url", "title", "ref",
            "navigations", "total", "showing", "cursor", "next_cursor",
            "hittable", "reasons", "next_step", "notes", "checks", "summary", *_BROWSER_SAVED_EVIDENCE_KEYS,
        ))
        text = result.get("text")
        if isinstance(text, str) and text:
            if len(text) > _ELEMENT_QUERY_TEXT_CHARS:
                # Whole entries only: an entry's continuation lines (a
                # response body) belong to it, so the cut lands before an
                # entry, never between an entry and its body, and the cursor
                # names exactly the entries shown.
                kept_lines: list[str] = []
                size = 0
                listed = 0
                entry: list[str] = []
                for line in text.splitlines():
                    if line.startswith("[") and entry:
                        kept_lines.extend(entry)
                        entry = []
                    if line.startswith("["):
                        entry = [line]
                        if size + sum(len(item) + 1 for item in entry) > _ELEMENT_QUERY_TEXT_CHARS:
                            entry = []
                            break
                        listed += 1
                        size += len(line) + 1
                    elif entry:
                        if size + len(line) + 1 > _ELEMENT_QUERY_TEXT_CHARS:
                            # The body does not fit: drop the whole entry so a
                            # continuation can show it complete.
                            size -= sum(len(item) + 1 for item in entry)
                            listed -= 1
                            entry = []
                            break
                        entry.append(line)
                        size += len(line) + 1
                    else:
                        kept_lines.append(line)
                        size += len(line) + 1
                kept_lines.extend(entry)
                kept = "\n".join(kept_lines).rstrip()
                next_cursor = int(result.get("cursor") or 0) + listed
                projected.update(showing=listed, next_cursor=next_cursor)
                kept += (
                    f"\n\n[Listed {listed} entries before the output budget; continue "
                    f"browser_extract with the same read and cursor={next_cursor}, or narrow the filter.]"
                )
                projected["projection_truncated"] = True
                text = kept
            projected["relevant_text"] = text
        return projected
    if result.get("source") == "element_query":
        # A selector query's answer is its list. The page projection would
        # clip it to a 2,000-char digest and repeat each match as a control, so
        # a 50-link listing reached the model as a head and a tail. The list
        # is already bounded by max_results and per-match clipping.
        projected = _browser_keys(result, (
            "success", "error", "source", "tab_id", "url", "title",
            *_BROWSER_ELEMENT_QUERY_KEYS, *_BROWSER_SAVED_EVIDENCE_KEYS,
        ))
        text = result.get("text")
        if isinstance(text, str) and text:
            if len(text) > _ELEMENT_QUERY_TEXT_CHARS:
                # Whole matches only, and say where the list stopped: a
                # mid-list elision would read as matches that do not exist.
                kept = text[:_ELEMENT_QUERY_TEXT_CHARS].rsplit("\n", 1)[0]
                listed = sum(1 for line in kept.splitlines() if line.startswith("["))
                next_cursor = int(result.get("cursor") or 0) + listed
                projected.update(showing=listed, next_cursor=next_cursor)
                kept += (
                    f"\n\n[Listed {listed} matches before the output budget; "
                    f"continue browser_extract with the same selector and cursor={next_cursor}, or narrow the selector.]"
                )
                projected["projection_truncated"] = True
                text = kept
            projected["relevant_text"] = text
        return projected
    projected = _browser_keys(result, _BROWSER_MODEL_TOP_LEVEL_KEYS)
    for key in _BROWSER_MODEL_EXTRACT_KEYS:
        if key in result and result[key] not in (None, "", [], {}):
            value = result[key]
            if key == "fields" and isinstance(value, list) and len(value) > 80:
                # Failures beyond the display cap must not disappear behind a
                # long prefix of successful fields. Indexes retain source order.
                indexed = [dict(item, field_index=i) if isinstance(item, dict) else item
                           for i, item in enumerate(value)]
                failed = [item for item in indexed if isinstance(item, dict) and (
                    item.get("error") or item.get("verified") is False or item.get("status") == "failed")]
                other = [item for item in indexed if not (isinstance(item, dict) and (
                    item.get("error") or item.get("verified") is False or item.get("status") == "failed"))]
                value = sorted((failed + other)[:80], key=lambda item: item.get("field_index", 0) if isinstance(item, dict) else 0)
                projected["fields_omitted"] = len(result[key]) - len(value)
            projected[key] = _browser_value(value, key=key)
            if isinstance(result[key], list) and len(result[key]) > 80 and key != "fields":
                projected[key + "_omitted"] = len(result[key]) - 80
    if "result" in result:
        projected["result"] = _browser_value(result["result"])
    # Observation coverage also matters when the current page has zero elements.
    projected.update(_project_browser_page({key: value for key, value in result.items()
                                            if key not in {"text", "visible_text"}}))
    page = _project_browser_page(result.get("page"))
    if page:
        if isinstance(result.get("fields"), list) and isinstance(page.get("relevant_elements"), list):
            # A completed batch already carries field-level outcomes. Put the
            # next normal actions first so the bounded projection does not make
            # the model observe again merely to find Save/Next/Review.
            def next_action_rank(element: dict) -> tuple[int, str]:
                role = str(element.get("role") or "").casefold()
                typ = str(element.get("type") or "").casefold()
                label = " ".join(str(element.get(key) or "") for key in ("label", "text", "name")).casefold()
                if role == "button" or typ in {"button", "submit", "reset"}:
                    if any(word in label for word in ("save", "next", "review", "back", "check")):
                        return (0, label)
                    if typ == "submit" or "submit" in label:
                        return (1, label)
                    return (2, label)
                return (3, label)
            page["relevant_elements"] = sorted(page["relevant_elements"], key=next_action_rank)
        projected["page"] = page
    tab = result.get("tab")
    if isinstance(tab, dict):
        for key in ("workspace_id", "tab_id", "observation_id", "page_revision", "url", "title", "navigation_state"):
            if key not in projected and tab.get(key) not in (None, ""):
                projected[key] = _browser_value(tab[key], key=key)
    rows = result.get("results")
    if isinstance(rows, list) and _depth < 2:
        compact_rows = []
        for row in rows[:5]:
            compact = _project_browser_result(row, _depth=_depth + 1)
            compact.pop("page", None)
            compact_rows.append(compact)
        projected["results"] = compact_rows
        if len(rows) > 5:
            projected["results_omitted"] = len(rows) - 5
        for key in ("opened", "failed", "partial_failure", "status", "failure_class"):
            if key in result:
                projected[key] = _browser_value(result[key])
    tabs = result.get("tabs")
    # A resume's page is the selected tab; its tab list is still the only
    # place the model learns what else is open (session 722b3c33 resumed with
    # observe_selected=true and reopened nine open pages as copies).
    if isinstance(tabs, list) and (not page or result.get("resumed")) and not isinstance(rows, list):
        tabs = [tab for tab in tabs if isinstance(tab, dict)]
        if len(tabs) > 20:
            # Workspace order is oldest first, so a plain head dropped exactly
            # the pages the agent was working on. Keep the selected tab and the
            # most recently active ones, in workspace order.
            selected = str(result.get("selected_tab_id") or "")
            by_activity = sorted(range(len(tabs)), key=lambda index: (
                str(tabs[index].get("tab_id") or "") == selected or bool(tabs[index].get("selected")),
                float(tabs[index].get("last_active_at") or tabs[index].get("updated_at") or 0),
            ), reverse=True)
            keep = set(by_activity[:20])
            projected["tabs_omitted"] = len(tabs) - 20
            projected["tabs_omitted_note"] = "The least recently active tabs are omitted."
            tabs = [tab for index, tab in enumerate(tabs) if index in keep]
        projected["tabs"] = [_browser_keys(tab, (
            "tab_id", "url", "title", "status", "navigation_state", "selected", "restorable",
            "restore_status", "owner_kind", "restorability", "dirty", "uncertain",
        )) for tab in tabs]
    relevant_text = result.get("relevant_text") or result.get("visible_text") or result.get("text")
    if result.get("content_blocks") and not preserve_text:
        relevant_text = ""
    if relevant_text and preserve_text and not result.get("passages") and not _has_form_state(result.get("fields")):
        # A reading extraction: the page text is what was asked for. A form
        # read-back keeps its field values first, and a `find` its passages.
        projected["_text_source"] = str(relevant_text)
        projected["relevant_text"] = _bounded_browser_text(relevant_text, _BROWSER_READ_TEXT_CHARS)
    elif relevant_text:
        projected["relevant_text"] = _bounded_browser_text(relevant_text)
    return _fit_browser_projection(projected, preserve_text=preserve_text)

def _browser_transport_payload(result: Any) -> Any:
    """Unwrap the existing MCP transport after tool identity was established.

    Parsing a whole JSON result is protocol decoding, not heuristic detection of
    a browser tool from a page body. Plain legacy text remains bounded data.
    """
    if not isinstance(result, dict):
        return result
    structured = result.get("structured_content", result.get("structuredContent"))
    if not isinstance(structured, dict) and ("mcp_content" in result or "stdout" in result):
        text = result.get("stdout") or result.get("stderr") or ""
        try:
            structured = json.loads(text)
        except (TypeError, ValueError):
            structured = {"text": text} if text else {}
        if not isinstance(structured, dict):
            structured = {"result": structured}
    if not isinstance(structured, dict):
        return result
    payload = dict(structured)
    for key in ("exit_code", "tool_status", "evidence_ref", "managed_output_ref",
                "evidence_archive_failed", "context_note"):
        if key in result:
            payload[key] = result[key]
    if result.get("exit_code") not in (None, 0):
        payload["success"] = False
        payload["error"] = result.get("error") or result.get("stderr") or payload.get("error") or "Browser transport failed"
    return payload

_EXTRACT_READING_ARGS = ("instruction", "find", "target_ref", "selector", "schema", "read", "cursor")

_SAVED_RECEIPT_KEYS = (
    "success", "error", "error_code", "error_kind", "exit_code", "tool_status",
    "workspace_id", "tab_id", "observation_id", "url", "title",
    "blocked", "attention_required", "blocker", "diagnostics", "context_note",
    "evidence_ref", "managed_output_ref", *_BROWSER_SAVED_EVIDENCE_KEYS,
)

def _saved_extract_receipt(projected: dict, args: dict) -> dict:
    """A save-only extraction shows its receipt, not the content it saved.

    The file holds the content; repeating it inline doubled the cost. One
    session made 118 such calls and carried ~760K chars it had already
    written to disk (722b3c33). browser-use does the same: saved extracts
    stay in history as their file name.
    """
    if (not args.get("save_to") or not projected.get("saved_to")
            or any(args.get(key) not in (None, "", False, [], {}) for key in _EXTRACT_READING_ARGS)):
        return projected
    if "fields" not in projected and str(projected.get("message") or "").startswith("Saved to "):
        return projected  # already a receipt (history replays the stored one)
    receipt = {key: projected[key] for key in _SAVED_RECEIPT_KEYS if key in projected}
    fields = [field for field in projected.get("fields") or [] if isinstance(field, dict)]
    empty = [
        str(field.get("label") or field.get("text") or field.get("ref") or "")[:60]
        for field in fields
        if field.get("required") and not (
            field.get("value") or field.get("checked") or field.get("file_names")
            or field.get("selected_text")
        )
    ]
    summary = f"Saved to {projected['saved_to']} ({projected.get('saved_bytes', 0)} bytes"
    if fields:
        summary += f", {len(fields)} controls"
    if empty:
        summary += f"; required and empty: {', '.join(empty[:10])}"
        if len(empty) > 10:
            summary += f" and {len(empty) - 10} more"
    receipt["message"] = summary + "). Content not repeated here; read the file if you need it."
    return receipt

def browser_result_context(tool_name: str, result: Any, arguments: Any = None) -> dict | None:
    """Typed browser context from actual tool identity, never result prose.

    Versioned optional metadata is safe for legacy history. It carries no
    authority and no independent task state; the bounded projection is exactly
    the one rendered to the model by this module.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            arguments = {}
    args = arguments if isinstance(arguments, dict) else {}
    name = str(tool_name or "").strip()
    if name.startswith("mcp: "):
        name = name[5:].strip()
    else:
        name = name.split(":", 1)[0].strip()
    if name == "mcp_call":
        name = str(args.get("name") or args.get("tool_name") or "")
        args = args.get("arguments") if isinstance(args.get("arguments"), dict) else args
    if name.startswith("mcp__"):
        name = name.rsplit("__", 1)[-1]
    from ascended_browser.runtime.tool_registry import browser_tool_names
    # Only the canonical supervised browser contract is safe to reduce this
    # way. Browser-worker reports and unknown extension payloads can themselves
    # be the task deliverable. The MCP form operation uses this same contract.
    if name not in browser_tool_names() and name != "browser_fill_form":
        return None
    kind = ("extraction" if name in {"browser_extract", "browser_evaluate"}
            else "capture" if name == "browser_screenshot"
            else "roster" if name in {"browser_tabs", "browser_workspace_status"}
            else "observation" if name == "browser_observe" else "action")
    try:
        projected = _project_browser_result(
            _browser_transport_payload(result), preserve_text=kind == "extraction",
        )
        if name == "browser_extract":
            projected = _saved_extract_receipt(projected, args)
    except (TypeError, ValueError, RecursionError):
        # A malformed remote result must not kill a run or bypass redaction by
        # falling back to raw JSON. The operation outcome stays unknown unless
        # the owning runtime explicitly supplied a boolean verdict.
        projected = {
            "context_projection_failed": True,
            "context_note": "Browser result could not be projected; raw evidence remains in run history.",
        }
        if isinstance(result, dict) and isinstance(result.get("success"), bool):
            projected["success"] = result["success"]
    context = {"version": 1, "tool": name, "kind": kind, "projection": projected}
    # The execution owner supplies this identity outside the browser payload.
    # Equal result bodies can describe separate attempts; carry/replay must
    # deduplicate the execution, not the text of its outcome. Keep it private.
    outcome_id = result.get("_browser_outcome_id") if isinstance(result, dict) else None
    if isinstance(outcome_id, str) and outcome_id.strip() and len(outcome_id) <= 512:
        context["_browser_outcome_id"] = outcome_id
    page = projected.get("page") if isinstance(projected.get("page"), dict) else {}
    scope = projected.get("scope") if isinstance(projected.get("scope"), dict) else {}
    for key in ("workspace_id", "tab_id", "observation_id", "page_revision"):
        value = next((source[key] for source in (projected, page, scope, args)
                      if key in source and source[key] is not None), None)
        if value is not None:
            context[key] = _browser_value(value, key=key)
    return context

def browser_result_archive(tool_name: str, result: Any, arguments: Any = None) -> str | None:
    """Full retrievable semantic output for the existing managed-output store.

    The archive is not another live context projection. It retains complete
    extracted task evidence and form outcomes behind an owner-scoped reference;
    current DOM internals stay in the browser runtime. Values explicitly marked
    sensitive are masked in the archive exactly as in model presentation.
    """
    context = browser_result_context(tool_name, {}, arguments)
    if context is None or not isinstance(result, dict):
        return None
    result = _browser_transport_payload(result)
    # A continuation is a view into an existing immutable archive, not fresh
    # evidence. Re-archiving the formatted slice creates a recursive chain of
    # references and teaches the model to page the wrapper instead of the
    # original evidence.
    if result.get("source") == "browser_evidence":
        return None

    def safe(value, depth=0):
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        if depth >= 20:
            return {"archive_truncated": True}
        if isinstance(value, (list, tuple)):
            return [safe(child, depth + 1) for child in value]
        if not isinstance(value, dict):
            return "[unsupported browser value]"
        sensitive = value.get("sensitive") is True or str(value.get("type") or "").casefold() == "password"
        output = {}
        for key, child in value.items():
            if not isinstance(key, str) or key.startswith("_") or key.casefold() in _BROWSER_PRIVATE_KEYS:
                continue
            output[key] = ("[masked]" if sensitive and key in {
                "value", "text", "selected_text", "requested", "observed",
            } else safe(child, depth + 1))
        return output

    keys = _BROWSER_MODEL_TOP_LEVEL_KEYS + _BROWSER_MODEL_EXTRACT_KEYS
    if result.get("source") == "element_query":
        keys += _BROWSER_ELEMENT_QUERY_KEYS
    archive = {}
    if context["kind"] == "extraction":
        # Put focused evidence first for the immediate model projection while
        # retaining the complete source text for lossless continuation and
        # existing evidence consumers. Projection and archive have separate
        # budgets; the latter must not silently discard allowed page content.
        for key in ("result", "relevant_text", "text", "visible_text"):
            if key in result:
                archive[key] = safe(result[key])
    archive.update({key: safe(result[key]) for key in keys if key in result})
    if not archive:
        return None
    return json.dumps(archive, ensure_ascii=False, separators=(",", ":"))

def _browser_observation_heading(result: Dict[str, Any]) -> str:
    """One human-readable line naming the page and its plain state facts.

    Sits above the structured observation so the model's first reference to a
    browser outcome is a human label (page title / host), not a raw id. Facts
    are joined with " · " and drawn from exact enum vocabulary in user_labels;
    the structured keys below remain untouched for tool-call addressing.
    """
    tab = result.get("tab") if isinstance(result.get("tab"), dict) else {}
    page_state = result.get("page") if isinstance(result.get("page"), dict) else {}
    # A sequence reports its page on the last step, not at the top level.
    steps = [item for item in result.get("results") or [] if isinstance(item, dict)]
    last_step = steps[-1] if steps and "index" in steps[-1] else {}
    url = _bounded_browser_text(
        tab.get("url") or result.get("url") or page_state.get("url") or last_step.get("url") or "", 320,
    )
    title = _bounded_browser_text(
        tab.get("title") or result.get("title") or page_state.get("title") or last_step.get("title") or "", 160,
    )
    page = page_label(title or None, url or None)
    if not url and not title:
        # A batch open, a tab roster or a viewport change is about the browser,
        # not one page; "an unnamed page" misdescribed every one of them.
        opened = [item for item in steps if item.get("url") or item.get("requested_url")]
        tabs = [item for item in result.get("tabs") or [] if isinstance(item, dict)]
        if opened:
            page = f"{len(opened)} page{'s' if len(opened) != 1 else ''}"
        elif tabs:
            page = f"{len(tabs)} tab{'s' if len(tabs) != 1 else ''}"
        else:
            page = "the browser"
    facts: list = []
    if url:
        facts.append(url)
    auth = result.get("auth_state") if isinstance(result.get("auth_state"), dict) else {}
    plain = str(auth.get("state") or "").strip()
    if not plain and isinstance(result.get("auth_state"), str):
        plain = result["auth_state"]
    known_auth = AUTH_STATE_WORDS.get(plain)
    if known_auth:
        facts.append(known_auth)
    if result.get("resumed") is True:
        facts.append("reopened")
    if isinstance(result.get("page"), dict):
        summary = str(result["page"].get("summary") or "").strip()
        if summary:
            facts.append(_bounded_browser_text(summary, 160))
    if not facts:
        return f"**page:** {page}"
    return f"**page:** {page} · " + " · ".join(facts)

_AGENT_RESULT_SOURCES_SHOWN = 25

_AGENT_RESULT_TRACE_CHARS = 1_000

def _format_agent_result(description: str, result: Dict) -> str:
    """Render a child's result with its report as prose, not escaped JSON.

    The generic branch put the report inside an 8,000-char JSON dump behind
    thread metadata, so a parent saw roughly the first third of a real report
    and re-asked the child for what it had already delivered. Here the report
    is plain text after a compact header; managed output keeps the lossless
    copy when the whole block is larger than the model-facing projection.
    """
    parts = [f"### {description}"]
    header = [
        f"thread_id={result.get('thread_id')}",
        f"task={result.get('task_name')}",
        f"mode={result.get('capability_mode')}",
        f"status={result.get('status')}",
        f"generation={result.get('generation')}",
    ]
    if result.get("terminal_reason"):
        header.append(f"reason={result.get('terminal_reason')}")
    header.append(f"tokens={int(result.get('token_total') or 0):,}")
    header.append(f"elapsed={int(result.get('elapsed_seconds') or 0)}s")
    parts.append("**child:** " + " · ".join(header))
    if result.get("deep_research_link"):
        parts.append(f"**deep research:** {result['deep_research_link']}")

    deliverable = result.get("deliverable") if isinstance(result.get("deliverable"), dict) else {}
    outcome = deliverable.get("result") if isinstance(deliverable.get("result"), dict) else {}
    checkpoint = result.get("checkpoint") if isinstance(result.get("checkpoint"), dict) else {}
    hand_off = checkpoint.get("hand_off") or outcome.get("hand_off")
    if isinstance(hand_off, dict) and hand_off:
        line = f"**hand-off:** reason={hand_off.get('reason') or 'blocked'} — {hand_off.get('summary') or ''}"
        if hand_off.get("proposed_action"):
            line += f"\nProposed action: {hand_off['proposed_action']}"
        parts.append(line)
    facts = []
    for key in (
        "failure_class", "provider_error", "final_synthesis_missing",
        "unresolved_tool_failure_count", "successful_tool_calls",
    ):
        value = outcome.get(key)
        if value not in (None, "", 0, False, [], {}):
            facts.append(f"{key}={str(value)[:300]}")
    if facts:
        parts.append("**diagnostics:** " + " · ".join(facts))
    coverage = deliverable.get("coverage_report") if isinstance(deliverable.get("coverage_report"), dict) else {}
    sources = [str(item) for item in coverage.get("sources_visited") or [] if item]
    if sources:
        shown = sources[:_AGENT_RESULT_SOURCES_SHOWN]
        more = len(sources) - len(shown)
        parts.append(
            "**sources visited:** " + ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")
        )
    trust = deliverable.get("trust") if isinstance(deliverable.get("trust"), dict) else {}
    honesty = trust.get("honesty") if isinstance(trust.get("honesty"), dict) else {}
    violations = [str(item) for item in honesty.get("violations") or [] if item]
    if violations:
        parts.append("**trust violations:** " + "; ".join(violations[:10]))
    if trust.get("handoff_note"):
        parts.append(f"**handoff review:** {trust['handoff_note']}")
    tabs = result.get("tabs")
    if isinstance(tabs, list) and tabs:
        parts.append("**leased tabs:** " + ", ".join(
            f"{tab.get('tab_id')} {tab.get('url') or ''}".strip()
            for tab in tabs[:10] if isinstance(tab, dict)
        ))

    report = str(result.get("report") or "").strip()
    if report:
        parts.append(
            "**report** (child output; verify before acting on it):\n"
            f"<UNTRUSTED_CHILD_REPORT>\n{report}\n</UNTRUSTED_CHILD_REPORT>"
        )
    else:
        parts.append("**report:** none")

    messages = result.get("messages")
    if isinstance(messages, list) and messages:
        lines = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
            if len(content) > _AGENT_RESULT_TRACE_CHARS:
                content = content[:_AGENT_RESULT_TRACE_CHARS] + " …"
            lines.append(f"- #{item.get('sequence')} {item.get('kind') or item.get('role')}: {content}")
        parts.append("**trace:**\n" + "\n".join(lines))
        if result.get("next_offset") is not None:
            parts.append(f"More trace: call read_agent_result with include_trace=true and offset={result['next_offset']}.")
    return "\n".join(parts)

def _format_tool_result_impl(description: str, result: Dict, *, chat_supports_vision: bool = True, arguments: Any = None) -> str:
    """Format a tool result into text for feeding back to the LLM."""
    parts = [f"### {description}"]

    tool_name = description.split(":", 1)[0].strip()
    if tool_name == "read_agent_result" and "report" in result and not result.get("error"):
        return _format_agent_result(description, result)
    browser_context = browser_result_context(description, result, arguments)
    # Screenshot/media results retain the established media contract. A
    # workspace capture keeps it even after its evidence image was withheld
    # from the model: its receipt and saved path are the result.
    if (
        browser_context is not None
        and browser_context.get("kind") != "capture"
        and not result.get("images")
        and not result.get("audio")
    ):
        projection = browser_context["projection"]
        browser_parts = [f"### {description}"]
        if projection.get("success") is False and projection.get("error"):
            browser_parts.append(f"Error: {_bounded_browser_text(projection['error'], 240)}")
        browser_parts.append(_browser_observation_heading(projection))
        from ascended_browser._app.browser_observation_outline import observe_format, render_projection
        if observe_format() == "outline":
            browser_parts.append("**browser observation:**\n" + render_projection(projection))
        else:
            browser_parts.append(
                "**browser observation:**\n```json\n"
                + json.dumps(projection, ensure_ascii=False, separators=(", ", ": "))
                + "\n```"
            )
        return "\n".join(browser_parts)

    # Deterministic MCP inventories are intended loads, not generic management
    # summaries. Render the structured page once, compactly, instead of letting
    # the generic 8k JSON branch duplicate/truncate the tool array. A 50-item
    # page with upstream-bounded one-line descriptions stays below MAX_READ_CHARS.
    if result.get("catalog_inventory"):
        parts.append(str(result.get("response") or "MCP tool inventory"))
        if result.get("error"):
            if result.get("error_code"):
                parts.append(f"**error_code:** `{result['error_code']}`")
            return "\n".join(parts)
        lines = []
        for item in result.get("tools") or []:
            if not isinstance(item, dict):
                continue
            qualified = str(item.get("qualified_name") or item.get("name") or "")
            desc = re.sub(r"\s+", " ", str(item.get("description") or "")).strip()
            disabled = " [disabled]" if item.get("is_disabled") else ""
            line = f"- `{qualified}`{disabled}"
            if desc:
                line += f" — {desc}"
            lines.append(line)
        if lines:
            parts.append("**tools:**\n" + "\n".join(lines))
        parts.append(
            "**inventory:** "
            f"returned={result.get('returned', len(lines))}, "
            f"total={result.get('total', len(lines))}, "
            f"offset={result.get('offset', 0)}, "
            f"has_more={bool(result.get('has_more'))}, "
            f"next_offset={result.get('next_offset')}, "
            f"exhaustive={bool(result.get('exhaustive'))}"
        )
        formatted = "\n".join(parts)
        if len(formatted) > MAX_READ_CHARS:
            formatted = (
                formatted[:MAX_READ_CHARS]
                + f"\n... [MCP inventory truncated at {MAX_READ_CHARS:,} chars]"
            )
        return formatted

    if "stdout" in result:
        if result["stdout"]:
            parts.append(f"**stdout:**\n```\n{result['stdout']}\n```")
        if result["stderr"]:
            parts.append(f"**stderr:**\n```\n{result['stderr']}\n```")
        parts.append(f"**exit_code:** {result.get('exit_code', 'unknown')}")
    elif "output" in result:
        # bash / python canonical result shape: {"output": ..., "exit_code": ...}
        parts.append(f"```\n{result['output']}\n```")
        if result.get("exit_code") not in (0, None):
            parts.append(f"**exit_code:** {result['exit_code']}")
        if result.get("process_status"):
            parts.append(f"**process_status:** {result['process_status']}")
            if result.get("session_id"):
                parts.append(f"**session_id:** `{result['session_id']}`")
            elif result.get("exit_code") == 0:
                parts.append("**exit_code:** 0")
    elif "action" in result:
        action = result["action"]
        title = result.get("title", "")
        doc_id = result.get("doc_id", "")
        version = result.get("version", "?")
        revision = result.get("content_revision", version)
        if action == "create":
            parts.append(
                f'Document created: "{title}" (id: {doc_id}, v{version}, r{revision})'
            )
        elif action == "update":
            parts.append(
                f'Document updated: "{title}" (id: {doc_id}, v{version}, r{revision})'
            )
            if result.get("media_action") in {"attach", "update"} and "heading" in result:
                location = str(result.get("heading") or "")[:160] or "(before the first heading)"
                parts.append(f"Media saved under heading: {location}")
        elif action == "edit":
            parts.append(
                f'Document edited: "{title}" (id: {doc_id}, v{version}, r{revision}, '
                f'applied: {result.get("applied", 0)}, skipped: {result.get("skipped", 0)})'
            )
        elif action == "suggest":
            count = result.get("count", len(result.get("suggestions") or []))
            if result.get("review_rendered"):
                parts.append(
                    f'Document suggested: "{title}" '
                    f'(id: {doc_id}, r{revision}, suggestions: {count}). '
                    'The review is visible in the editor.'
                )
            else:
                parts.append(
                    f'Document suggestions were prepared but the editor did not confirm '
                    f'that the review rendered: "{title}" '
                    f'(id: {doc_id}, r{revision}, suggestions: {count}). '
                    'Do not claim that the review is visible or applied.'
                )
        elif action == "read" and description.split(":", 1)[0].strip() == "document_media":
            parts.append(result.get("response") or "Document media read")
        elif action == "open":
            # `response` already carries the anchor, the re-attach note and a
            # bounded preview. This branch wins over the generic "content" one
            # below, which is what keeps the full document body (sent to the
            # editor panel over SSE) out of the model's context.
            parts.append(
                result.get("response")
                or f'Document opened: "{title}" (id: {doc_id}, v{version}, r{revision})'
            )
    elif "content" in result:
        parts.append(f"**content ({result.get('size', '?')} chars):**\n```\n{result['content']}\n```")
    elif "response" in result:
        model = result.get("model", result.get("session_name", ""))
        if model:
            parts.append(f"**{model} responded:**\n{result['response']}")
        else:
            parts.append(result["response"])
    elif "results" in result:
        # Batched tools return structured rows here. Appending the list object
        # directly made the final ``"\n".join(parts)`` raise TypeError after a
        # successful tool call, taking down the entire agent loop. Tool output
        # formatting is a trust boundary: every supported shape must become
        # text before it can reach a provider message or the transcript.
        rows = result["results"]
        if isinstance(rows, str):
            parts.append(rows)
        else:
            parts.append(json.dumps(rows, ensure_ascii=False, default=str))
    elif "session_id" in result and "name" in result:
        parts.append(f"Session created: **{result['name']}** (id: `{result['session_id']}`, model: {result.get('model', 'unknown')})")
    elif "success" in result:
        if not result["success"]:
            parts.append(f"Error: {result.get('error', 'unknown')}")
        elif "path" in result:
            parts.append(
                f"File written: {result['path']} ({result.get('size', '?')} bytes)"
            )
        # Any other successful result falls through to the generic data block
        # below. `success` is a common key, not a file-write signature: every
        # successful browser_act returns {"success": True, "tab": {...}} and
        # used to land here, raising KeyError('path') out of format_tool_result
        # — which is not caught per-tool, so it killed the whole agent run.
    elif "error" in result:
        parts.append(f"**Error:** {result['error']}")

    if result.get("error_code") == "document_revision_conflict":
        parts.append(
            "**revision conflict:** "
            f"expected r{result.get('expected_revision', '?')}, "
            f"current r{result.get('content_revision', '?')}"
        )

    deliverable_sync = result.get("deliverable_sync") if isinstance(result, dict) else None
    if isinstance(deliverable_sync, dict) and deliverable_sync.get("status") != "not_linked":
        parts.append("**deliverable_sync:** " + json.dumps(
            deliverable_sync, ensure_ascii=False, sort_keys=True,
        ))
        if deliverable_sync.get("status") == "diverged":
            parts.append("The editor document was updated; the linked file was left untouched because reconciliation was unsafe. Reconcile before claiming both copies agree.")
        elif deliverable_sync.get("status") == "error":
            parts.append("The editor document was updated, but linked-file synchronization is not fully verified. Check the reported file state before claiming both copies agree.")

    diagnostics = result.get("diagnostics") if isinstance(result, dict) else None
    if isinstance(diagnostics, list) and diagnostics:
        diag_lines = []
        for diag in diagnostics:
            if not isinstance(diag, dict):
                continue
            idx = diag.get("index", "?")
            status = diag.get("status", "unknown")
            count = diag.get("match_count")
            line_bits = diag.get("matching_lines") or []
            detail = f"- edit {idx}: {status}"
            if count is not None:
                detail += f", exact matches={count}"
            if line_bits:
                detail += ", lines=" + ",".join(str(n) for n in line_bits[:20])
            if diag.get("candidate_line"):
                detail += f", diagnostic candidate line={diag['candidate_line']}"
            if diag.get("reason"):
                detail += f" — {diag['reason']}"
            diag_lines.append(detail)
        if diag_lines:
            parts.append("**edit diagnostics:**\n" + "\n".join(diag_lines))
    excerpt = result.get("excerpt") if isinstance(result, dict) else None
    # manage_documents action=search already renders its window inside
    # `response`; re-rendering it here doubled an inlined result that is only
    # supposed to be bounded at MAX_READ_CHARS.
    if excerpt and not any(str(excerpt) in part for part in parts):
        parts.append(f"**current numbered excerpt:**\n```\n{excerpt}\n```")

    terminal = result.get("terminal") if isinstance(result, dict) else None
    if isinstance(terminal, dict):
        parts.append(
            "**terminal:** "
            f"id=`{terminal.get('session_id')}`, status={terminal.get('status')}, "
            f"pid={terminal.get('pid')}, lines={terminal.get('total_lines')}, "
            f"remaining={terminal.get('remaining', 0)}"
        )
    sessions = result.get("terminal_sessions") if isinstance(result, dict) else None
    if isinstance(sessions, list) and sessions:
        parts.append("**terminal sessions:**\n" + "\n".join(
            f"- `{s.get('session_id')}` {s.get('status')} pid={s.get('pid')} {s.get('title')}"
            for s in sessions[:20] if isinstance(s, dict)
        ))

    if result.get("workspace_path"):
        parts.append(f"**workspace_path:** `{result['workspace_path']}`")
    if result.get("sandbox_path"):
        parts.append(f"**session_sandbox_path:** `{result['sandbox_path']}`")
    if result.get("workspace_notice"):
        parts.append(f"**workspace note:** {result['workspace_notice']}")
    if result.get("capability_notice"):
        parts.append(f"**capability note:** {result['capability_notice']}")

    images = result.get("images") if isinstance(result, dict) else None
    if images and not result.get("inspection_only"):
        image_lines = []
        for idx, img in enumerate(images[:3], 1):
            mime = img.get("mimeType") or "image/*"
            dims = ""
            if img.get("width") and img.get("height"):
                dims = f", {img['width']}x{img['height']}"
            if chat_supports_vision:
                image_lines.append(f"- image {idx}: {mime}{dims}, attached to the next model turn")
            else:
                image_lines.append(
                    f"- image {idx}: {mime}{dims}, converted to a text description "
                    f"for this text-only chat model (see the following user message)"
                )
        if len(images) > 3:
            image_lines.append(f"- ... {len(images) - 3} more image(s) omitted from this text summary")
        parts.append("**visual attachments:**\n" + "\n".join(image_lines))
        if result.get("vision_note") and chat_supports_vision:
            parts.append(f"**vision note:** {result['vision_note']}")
        elif not chat_supports_vision:
            parts.append(
                "**vision note:** This chat model cannot view images directly; "
                "a Settings → Vision model produced a text description of the screenshot."
            )

    inspection = result.get("inspection") if isinstance(result, dict) else None
    if isinstance(inspection, dict):
        delivered = int(inspection.get("delivered_count") or 0)
        remaining = int(inspection.get("remaining_count") or 0)
        parts.append(
            "**private visual inspection:** "
            f"delivered={delivered}, remaining={remaining}, "
            f"selection_complete={bool(result.get('coverage_complete'))}, "
            f"full_file_coverage={bool(result.get('full_file_coverage'))}"
        )
        labels = [
            str(label) for label in (inspection.get("delivered_units") or [])
            if label
        ]
        if labels:
            parts.append("**delivered units:** " + "; ".join(labels[:12]))
        if result.get("next_cursor"):
            parts.append(f"**next cursor:** `{result['next_cursor']}`")
        file_lines = []
        for item in result.get("files") or []:
            if not isinstance(item, dict):
                continue
            selected = len(set(item.get("selected_units") or []))
            delivered_for_file = len(set(item.get("delivered_units") or []))
            file_lines.append(
                f"- {item.get('path') or item.get('source_id') or 'file'}: "
                f"{delivered_for_file}/{selected} selected "
                f"{item.get('unit_kind') or 'unit'}(s) delivered"
            )
        if file_lines:
            parts.append("**inspection files:**\n" + "\n".join(file_lines))
        parts.append(
            "Judge the currently delivered batch now. If a defect needs an edit, "
            "fix the file and restart inspection for the new hash before continuing."
        )

    inspection_status = (
        result.get("inspection_status") if isinstance(result, dict) else None
    )
    if isinstance(inspection_status, dict):
        parts.append(
            "**visual inspection status:** "
            f"{inspection_status.get('state') or 'unknown'} "
            f"({inspection_status.get('delivered_units') or 0}/"
            f"{inspection_status.get('total_units') if inspection_status.get('total_units') is not None else '?'})"
        )

    # Some MCP servers answer only in structured content and leave stdout empty.
    # Render it just for that case — otherwise it restates what stdout already
    # said.
    structured = result.get("structured_content")
    if structured and not result.get("stdout"):
        try:
            structured_json = json.dumps(structured, indent=2, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            structured_json = ""
        if structured_json:
            if len(structured_json) > 8000:
                structured_json = (
                    structured_json[:8000]
                    + f"\n... (truncated, {len(structured_json)} chars total)"
                )
            parts.append(f"**structured content:**\n```json\n{structured_json}\n```")

    media_artifacts = result.get("media_artifacts")
    if isinstance(media_artifacts, list) and media_artifacts:
        media_lines = []
        for item in media_artifacts[:8]:
            if not isinstance(item, dict):
                continue
            if item.get("group_id"):
                candidates = item.get("candidates") or []
                media_lines.append(
                    f"- candidate group `{item['group_id']}` ({item.get('selection_status') or 'waiting'}), "
                    f"{len(candidates)} option(s): "
                    + ", ".join(
                        (
                            f"`{c.get('asset_id')}`"
                            + (f" at `{c.get('saved_path')}`" if c.get("saved_path") else "")
                        )
                        for c in candidates[:8] if isinstance(c, dict)
                    )
                )
            elif item.get("asset_id"):
                dims = ""
                if item.get("width") and item.get("height"):
                    dims = f", {item['width']}x{item['height']}"
                media_lines.append(
                    f"- `{item['asset_id']}` {item.get('kind') or 'media'} "
                    f"({item.get('status') or 'unknown'}{dims})"
                    + (f" — {item['title']}" if item.get("title") else "")
                    + (f"; saved at `{item['saved_path']}`" if item.get("saved_path") else "")
                )
        if len(media_artifacts) > 8:
            media_lines.append(f"- ... {len(media_artifacts) - 8} more not listed")
        if media_lines:
            parts.append(
                "**media artifacts:**\n" + "\n".join(media_lines)
                + "\nThe media is already displayed to the user. Reference it by "
                "`asset_id:` or /api/media/{id}/content. To use it in the active "
                "workspace, copy from `saved_path` and preserve the Gallery original "
                "unless deletion was explicitly requested."
            )

    job = result.get("mcp_media_job")
    if isinstance(job, dict) and job.get("id"):
        parts.append(
            f"**media job:** `{job['id']}` {job.get('status') or 'pending'} "
            f"({job.get('kind') or 'image'})"
            + (f" — {job['title']}" if job.get("title") else "")
        )

    design_cards = result.get("design_cards")
    if isinstance(design_cards, list) and design_cards:
        design_lines = []
        for card in design_cards[:5]:
            if not isinstance(card, dict):
                continue
            links = card.get("links") if isinstance(card.get("links"), dict) else {}
            design_lines.append(
                f"- {card.get('title') or 'Design'} ({card.get('provider') or 'unknown'})"
                + (f" — {', '.join(f'{k}: {v}' for k, v in links.items())}" if links else "")
            )
        if design_lines:
            parts.append("**designs:**\n" + "\n".join(design_lines))

    # Surface any additional structured payload (events, tasks, notes, calendars,
    # documents, attachments, etc.) that the dedicated branches above don't show.
    # Without this, tools that return {"response": "...", "events": [...]} would
    # silently drop the events list and the model would only see the summary line.
    extra = {k: v for k, v in result.items() if k not in _FORMATTER_HANDLED_KEYS}
    # Full discovery contracts are retained in the raw tool receipt and sent
    # in provider tool definitions. Repeating them in model-facing generic
    # JSON wastes context and previously clipped schemas mid-document.
    if description in {"search_tools", "get_tool_details"}:
        def discovery_receipt(value):
            if isinstance(value, dict):
                return {
                    key: (
                        str(item)[:200] if key == "description" else discovery_receipt(item)
                    ) for key, item in value.items()
                    if key not in {"loadable_tools", "tool_schema", "parameters", "schema", "schema_chars", "truncated"}
                }
            if isinstance(value, list):
                return [discovery_receipt(item) for item in value]
            return value
        extra = discovery_receipt(extra)
    if result.get("capability_notice"):
        # The note above already says which capability loaded; the name list
        # is loop plumbing. search_tools keeps its activated_tools listing.
        extra.pop("activated_tools", None)

    # A Browser Work result's payoff is `extracted_content` — the page text the
    # agent asked for. Left inside `extra`, it competes with two dozen metadata
    # fields for the 8000-char JSON budget below, so a page-sized answer that had
    # survived every producer bound still arrived clipped. Render it as its own
    # text block and drop it from the JSON dump so it is neither truncated by
    # that budget nor shipped twice.
    # Same duplication for manage_documents action=read: the bounded preview is
    # already rendered in `response`, so echoing it again inside the JSON dump
    # added up to another 8k of the very same document text.
    doc_payload = extra.get("document") if isinstance(extra.get("document"), dict) else None
    if doc_payload and "content" in doc_payload:
        extra = {
            **extra,
            "document": {k: v for k, v in doc_payload.items() if k != "content"},
        }

    bw_result = extra.get("result") if isinstance(extra.get("result"), dict) else None
    if bw_result and "extracted_content" in bw_result:
        content = str(bw_result.get("extracted_content") or "")
        if content:
            parts.append(f"**extracted_content:**\n```\n{content}\n```")
        trimmed = {k: v for k, v in bw_result.items() if k != "extracted_content"}
        extra = {**extra, "result": trimmed}

    if description == "update_plan":
        # The plan is the model's own argument; echoing it back is pure cost.
        extra.pop("plan_update", None)
    extra = _compact_model_data(extra)

    if extra:
        try:
            extra_json = json.dumps(extra, indent=2, default=str, ensure_ascii=False)
            # Cap to avoid blowing the context window on huge payloads.
            if description in {"search_tools", "get_tool_details"} and len(extra_json) > 8000:
                # Bound receipts structurally, preserving all activation names
                # and recovery fields. Never emit a partial JSON document.
                extra_json = json.dumps(extra, default=str, ensure_ascii=False)
                while len(extra_json) > 8000:
                    rows = extra.get("tools")
                    if not isinstance(rows, list) or not rows:
                        break
                    rows.pop()
                    extra["omitted_details"] = extra.get("omitted_details", 0) + 1
                    extra_json = json.dumps(extra, default=str, ensure_ascii=False)
            elif len(extra_json) > 8000:
                extra_json = extra_json[:8000] + f"\n... (truncated, {len(extra_json)} chars total)"
            parts.append(f"**data:**\n```json\n{extra_json}\n```")
        except (TypeError, ValueError):
            pass

    formatted = "\n".join(parts)
    # Every document tool in managed_tool_output._INLINE_FULL_TOOLS bypasses the
    # generic 4k managed preview on the promise that its result is already
    # bounded here. manage_documents read/search must be in this set or that
    # promise is false for exactly the tool the partial-index recovery path uses.
    doc_tools = {
        "create_document", "edit_document", "update_document", "suggest_document",
        "manage_documents", "document_media",
    }
    if description.split(":", 1)[0].strip() in doc_tools and len(formatted) > MAX_READ_CHARS:
        formatted = (
            formatted[:MAX_READ_CHARS]
            + f"\n... [document result truncated at {MAX_READ_CHARS:,} chars]"
        )
    return formatted

def format_tool_result(
    description: str,
    result: Dict,
    *,
    chat_supports_vision: bool = True,
    arguments: Any = None,
) -> str:
    """Contain malformed tool payloads so rendering can never kill a run."""
    try:
        return _format_tool_result_impl(
            description,
            result,
            chat_supports_vision=chat_supports_vision,
            arguments=arguments,
        )
    except Exception as exc:
        logger.exception("Failed to format tool result for %s", description)
        if browser_result_context(description, {}, arguments) is not None:
            # A malformed browser payload must not bypass the privacy boundary
            # by falling back to a dump of raw snapshots or private evidence.
            return (f"### {description}\n**result rendering warning:** {type(exc).__name__}\n"
                    "Browser result could not be projected; the raw result remains in run history.")
        try:
            fallback = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            fallback = repr(result)
        if len(fallback) > 8_000:
            fallback = fallback[:8_000] + "\n... [fallback truncated]"
        return (
            f"### {description}\n"
            f"**result rendering warning:** {type(exc).__name__}: {exc}\n"
            f"```json\n{fallback}\n```"
        )
