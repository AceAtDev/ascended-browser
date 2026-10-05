"""Deterministic observe/act/extract helpers for the live Camoufox MCP browser."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ascended_browser._app.browser_shadow_dom import SHADOW_DOM_JS
from ascended_browser._app.browser_click_helpers import build_enhanced_snapshot, click_with_fallbacks, parse_ref, ref_identity
from ascended_browser._app.browser_navigation_hints import capture_page_state, classify_navigation
from ascended_browser._app.browser_reliability import (
    classify_auth_state,
    classify_blocker,
    form_success_result,
    no_match_result,
    objective_requirements,
    recommended_login_route,
    snapshot_state_fingerprint,
    verify_browser_action,
)


_SUBMIT_WORDS = re.compile(
    r"\b(submit|send|apply|create\s+(?:profile|account)|register|sign\s*up|checkout|purchase|place\s+order|finish)\b",
    re.I,
)
_CLICK_WORDS = re.compile(r"\b(click|press|tap|open|choose|select)\b", re.I)
_PRESS_WORDS = re.compile(r"\b(?:press|hit|send)\s+(?:the\s+)?(?P<key>escape|esc|enter|return|tab|backspace|delete|space)\b", re.I)
_SECRET_KEYS = re.compile(r"password|passcode|secret|token", re.I)
_UPLOAD_OBJECTIVE_RE = re.compile(r"\b(upload|attach|attachment|resume|cover\s+letter)\b", re.I)
_DESTRUCTIVE_ATTACHMENT_RE = re.compile(r"\b(remove|delete|clear|detach|replace)\b.{0,80}\b(attachment|file|resume|cover\s+letter)\b|\b(attachment|file|resume|cover\s+letter)\b.{0,80}\b(remove|delete|clear|detach|replace)\b", re.I)


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _norm_key(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _redact(field: str, value: Any) -> str:
    if _SECRET_KEYS.search(str(field or "")):
        return "••••••••"
    return str(value or "")


def _snapshot_text(snapshot: dict[str, Any]) -> str:
    return _norm(
        snapshot.get("text")
        or snapshot.get("visible_text")
        or snapshot.get("accessibility_snapshot")
        or ""
    )


def _normalize_snapshot(snapshot: dict[str, Any] | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    base = dict(fallback or {})
    if isinstance(snapshot, dict):
        base.update(snapshot)
    if "text" not in base:
        base["text"] = _snapshot_text(base)
    if "visible_text" not in base:
        base["visible_text"] = base.get("text", "")
    viewport = base.get("viewport") if isinstance(base.get("viewport"), dict) else {}
    if viewport:
        base.setdefault("viewport_width", viewport.get("width") or 0)
        base.setdefault("viewport_height", viewport.get("height") or 0)
    return base


async def _verification_snapshot(page, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return _normalize_snapshot(await build_enhanced_snapshot(page, register_refs=False), fallback)
    except Exception:
        return _normalize_snapshot({}, fallback)


def _element_label(el: dict[str, Any]) -> str:
    parts = [
        el.get("text"),
        el.get("label"),
        el.get("aria_label"),
        el.get("placeholder"),
        el.get("name"),
        el.get("id"),
        el.get("title_attr"),
        el.get("legacy_ref"),
    ]
    return _norm(" ".join(str(p or "") for p in parts if p))


def _element_kind(el: dict[str, Any]) -> str:
    tag = str(el.get("tag") or "").lower()
    role = str(el.get("role") or "").lower()
    typ = str(el.get("type") or "").lower()
    text = _norm_key(_element_label(el))
    if tag == "select" or role == "combobox":
        return "select"
    if tag in {"input", "textarea"} or role in {"textbox", "searchbox"}:
        if tag == "input" and typ == "file":
            return "upload"
        if typ in {"submit", "button", "reset"}:
            return "click"
        if typ in {"checkbox", "radio"} or role in {"checkbox", "radio"}:
            return "click"
        return "fill"
    if role in {"button", "link", "menuitem", "tab"} or tag in {"button", "a"}:
        return "click"
    if _SUBMIT_WORDS.search(text):
        return "click"
    return "unknown"


def _action_candidate(el: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    center = el.get("center") or {}
    label = _element_label(el)
    candidate = {
        "id": str(el.get("ref") or ""),
        "kind": _element_kind(el),
        "label": label,
        "role": el.get("role") or "",
        "ref": el.get("ref") or "",
        "css_selector": el.get("css_selector") or el.get("selector") or "",
        "text": el.get("text") or "",
        "center": center,
        "confidence": 0.75 if reason else 0.55,
        "reason": reason or "visible interactive element",
        "href": el.get("href") or "",
        "document_order": el.get("document_order", 0),
        "frame_index": el.get("frame_index"),
        "frame_name": el.get("frame_name") or "",
        "frame_url": el.get("frame_url") or "",
    }
    context = str(el.get("context") or "").strip()
    if context:
        candidate["context"] = context[:220]
    if el.get("in_form") not in (None, ""):
        candidate["in_form"] = bool(el.get("in_form"))
    form_action = str(el.get("form_action") or "").strip()
    if form_action:
        candidate["form_action"] = form_action[:500]
    return candidate


def _is_structured_control(element: dict[str, Any]) -> bool:
    """Identify controls from DOM semantics, independent of task wording."""
    tag = str(element.get("tag") or "").casefold()
    role = str(element.get("role") or "").casefold()
    return tag in {"button", "input", "select", "textarea"} or role in {
        "button", "checkbox", "combobox", "radio", "searchbox", "slider",
        "spinbutton", "switch", "textbox",
    }


def _rank_candidates(candidates: list[dict[str, Any]], *, requirements: dict[str, Any], instruction: str = "") -> list[dict[str, Any]]:
    login_goal = bool(requirements.get("login"))
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    instruction_tokens = [
        token for token in _norm_key(instruction).split()
        if len(token) > 2 or token.isdigit()
    ]
    for index, candidate in enumerate(candidates):
        hay = _norm_key(" ".join(str(candidate.get(k) or "") for k in ("label", "text", "href", "role")))
        score = int(float(candidate.get("confidence") or 0) * 100)
        if candidate.get("kind") in {"fill", "select", "upload"}:
            score += 80
        if candidate.get("in_form"):
            score += 50
        score += min(96, sum(12 for token in instruction_tokens if token in hay))
        if login_goal:
            if re.search(r"\bpassword\b", hay):
                score += 120
            if re.search(r"\b(?:username|email|identifier)\b", hay):
                score += 90
            if re.search(r"\b(?:dashboard|my account|account|profile|portal)\b", hay):
                score += 80
            if re.search(r"\b(?:log in|login|sign in|signin)\b", hay):
                score += 70
            if re.search(r"\b(?:sign up|register|search|browse|featured|discover|forgot)\b", hay):
                score -= 60
        ranked.append((score, int(candidate.get("document_order") or index), candidate))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    out = []
    for score, _, candidate in ranked:
        item = dict(candidate)
        item["rank_score"] = score
        out.append(item)
    return out


def observe_snapshot(
    snapshot: dict[str, Any],
    *,
    instruction: str = "",
    max_actions: int = 30,
    visible_error_text: list[str] | None = None,
) -> dict[str, Any]:
    """Rank actions from an existing enhanced snapshot without scanning the DOM."""
    snapshot = _normalize_snapshot(snapshot)
    max_actions = max(1, min(int(max_actions or 30), 80))
    query = _norm_key(instruction)
    requirements = objective_requirements(instruction)
    blocker = classify_blocker(snapshot)
    auth_state = classify_auth_state(snapshot)
    candidates: list[dict[str, Any]] = []
    for el in snapshot.get("elements") or []:
        if not isinstance(el, dict):
            continue
        label_key = _norm_key(_element_label(el))
        reason = ""
        if query and label_key and any(token in label_key for token in query.split() if len(token) > 2):
            reason = f"matches instruction: {instruction}"
        candidates.append(_action_candidate(el, reason=reason))
    candidates = _rank_candidates(candidates, requirements=requirements, instruction=instruction)
    candidates = candidates[:max_actions]
    route_hint = recommended_login_route(snapshot) if requirements.get("login") else None
    return {
        "success": True,
        "url": snapshot.get("url") or "",
        "title": snapshot.get("title") or "",
        "objective": requirements,
        "auth_state": auth_state,
        "state_fingerprint": snapshot_state_fingerprint(snapshot),
        "blocker": blocker,
        "recommended_next_action": route_hint,
        "actions": candidates,
        "blocking_overlays": snapshot.get("blocking_overlays") or [],
        "visible_errors": list(visible_error_text or snapshot.get("visible_errors") or []),
    }


async def observe_page(page, *, instruction: str = "", max_actions: int = 30) -> dict[str, Any]:
    """Return structured candidate actions for the current page."""
    snapshot = _normalize_snapshot(await build_enhanced_snapshot(page))
    return observe_snapshot(
        snapshot,
        instruction=instruction,
        max_actions=max_actions,
        visible_error_text=await visible_errors(page),
    )


async def visible_status(page) -> list[str]:
    """What the page is saying about itself, politely.

    role=status and aria-live=polite are how a page narrates progress — a
    result count, "page is loaded", "saved". That is worth reading and is not
    an error, which is what it used to be reported as.
    """
    script = "() => {" + SHADOW_DOM_JS + """
      function visible(el) {
        const s = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
      }
      const out = [];
      for (const sel of ['[role="status"]', '[aria-live="polite"]', '[role="log"]']) {
        try {
          odyQueryAll(document, sel).forEach(el => {
            if (!visible(el)) return;
            if (el.matches('[role="alert"], [aria-live="assertive"], [aria-invalid="true"]')) return;
            const text = (el.innerText || el.getAttribute('aria-label') || '')
              .replace(/\\s+/g, ' ').trim();
            if (text) out.push(text.slice(0, 220));
          });
        } catch (e) {}
      }
      return [...new Set(out)].slice(0, 8);
    }"""
    try:
        data = await page.evaluate(script)
        if isinstance(data, list):
            return [_norm(x) for x in data if _norm(x)]
    except Exception:
        pass
    return []


async def visible_errors(page) -> list[str]:
    """Best-effort collection of visible validation/error text."""
    script = "() => {" + SHADOW_DOM_JS + """
      function visible(el) {
        const s = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
      }
      // ARIA separates the two: assertive regions and alerts interrupt because
      // something is wrong; polite regions and role=status narrate progress.
      // Listing every live region as an error reported "Search for Jobs page is
      // loaded" and "1 - 20 of 871 jobs" as failures on a page where nothing
      // had failed.
      const selectors = [
        '[role="alert"]', '[aria-live="assertive"]', '.error', '.errors', '.invalid-feedback',
        '.validation-error', '[class*="error" i]', '[id*="error" i]',
        'input:invalid', 'textarea:invalid', 'select:invalid', '[aria-invalid="true"]'
      ];
      const out = [];
      for (const sel of selectors) {
        try {
          odyQueryAll(document, sel).forEach(el => {
            if (!visible(el)) return;
            let text = (el.innerText || el.validationMessage || el.getAttribute('aria-label') || '')
              .replace(/\\s+/g, ' ').trim();
            if (!text && el.labels && el.labels[0]) text = el.labels[0].innerText || '';
            if (text) out.push(text.slice(0, 220));
          });
        } catch (e) {}
      }
      return [...new Set(out)].slice(0, 12);
    }"""
    try:
        data = await page.evaluate(script)
        if isinstance(data, list):
            return [_norm(x) for x in data if _norm(x)]
    except Exception:
        pass
    return []


_FOCUS_STOP_WORDS = frozenset({
    "and", "are", "for", "from", "into", "page", "read", "show", "that",
    "the", "this", "with", "find", "extract", "visible", "current",
})


def _focused_text(text: str, instruction: str, *, limit: int = 12000) -> tuple[str, dict[str, Any]]:
    """Return bounded source windows around task terms, not just the page head."""
    source = _norm(text)
    if len(source) <= limit:
        return source, {
            "strategy": "complete", "source_chars": len(source),
            "returned_chars": len(source), "complete": True,
        }
    terms = list(dict.fromkeys(
        token for token in _norm_key(instruction).split()
        if (len(token) > 2 or token.isdigit()) and token not in _FOCUS_STOP_WORDS
    ))[:24]
    folded = source.casefold()
    terms.sort(key=lambda term: (not term.isdigit(), folded.count(term.casefold())))
    def term_positions(term: str) -> list[int]:
        positions: list[int] = []
        cursor = 0
        numeric = term.isdigit()
        while len(positions) < 8:
            position = folded.find(term, cursor)
            if position < 0:
                break
            before = folded[position - 1] if position else ""
            after_at = position + len(term)
            after = folded[after_at] if after_at < len(folded) else ""
            # Rendered inline elements can concatenate text ("510Directory").
            # Digits still have an exact boundary there as long as an adjacent
            # character is not another digit; words retain alphanumeric bounds.
            bounded_before = before.isdigit() if numeric else before.isalnum()
            bounded_after = after.isdigit() if numeric else after.isalnum()
            if not bounded_before and not bounded_after:
                positions.append(position)
            cursor = position + max(1, len(term))
        return positions

    def merge_spans(spans: list[tuple[int, int]]) -> list[list[int]]:
        merged: list[list[int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1] + 80:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return merged

    selected: list[tuple[int, int]] = []
    matched_terms: set[str] = set()
    for term in terms:
        for position in term_positions(term.casefold()):
            candidate = (max(0, position - 420), min(len(source), position + 780))
            proposed = merge_spans(selected + [candidate])
            proposed_chars = sum(end - start for start, end in proposed)
            proposed_chars += max(0, len(proposed) - 1) * len(" … [next match] … ")
            if proposed_chars > limit and selected:
                continue
            selected = [(start, end) for start, end in proposed]
            matched_terms.add(term)
    if not selected:
        # next_cursor is an offset into this same whitespace-normalized text,
        # which is what an evidence_ref continuation pages over.
        return source[:limit], {
            "strategy": "head", "source_chars": len(source),
            "returned_chars": limit, "complete": False, "matched_terms": [],
            "next_cursor": limit,
        }
    pieces: list[str] = []
    remaining = limit
    for start, end in selected:
        if remaining <= 0:
            break
        piece = source[start:end]
        if len(piece) > remaining:
            piece = piece[:remaining]
        pieces.append(piece)
        remaining -= len(piece) + 19
    focused = " … [next match] … ".join(pieces)[:limit]
    return focused, {
        "strategy": "instruction_windows", "source_chars": len(source),
        "returned_chars": len(focused), "complete": False,
        "matched_terms": sorted(matched_terms),
    }


def extract_snapshot(snapshot: dict[str, Any], *, instruction: str = "") -> dict[str, Any]:
    """Extract task-focused information from a cached rendered snapshot."""
    snapshot = _normalize_snapshot(snapshot)
    body_text = _snapshot_text(snapshot)
    observed = observe_snapshot(snapshot, instruction=instruction, max_actions=80)
    elements_by_ref = {
        str(element.get("ref") or ""): element
        for element in snapshot.get("elements") or []
        if isinstance(element, dict) and element.get("ref")
    }
    all_actions = [
        _action_candidate(element)
        for element in snapshot.get("elements") or []
        if isinstance(element, dict)
    ]
    ranked_actions = _rank_candidates(
        all_actions,
        requirements=observed.get("objective") or objective_requirements(instruction),
        instruction=instruction,
    )
    control_actions = []
    related_actions = []
    for action in ranked_actions:
        source = elements_by_ref.get(str(action.get("ref") or ""), {})
        if _is_structured_control(source) and len(control_actions) < 80:
            control_actions.append(action)
        elif len(related_actions) < 12:
            # A small relevance-ranked supplement keeps requested links/tabs
            # without allowing a repetitive directory to bury real controls.
            related_actions.append(action)
    selected_actions = sorted(
        control_actions + related_actions,
        key=lambda action: int(action.get("document_order") or 0),
    )
    fields = []
    for action in selected_actions:
        if action.get("kind") not in {"fill", "select", "click", "upload"}:
            continue
        source = elements_by_ref.get(str(action.get("ref") or ""), {})
        semantic_label = _norm(
            source.get("label")
            or source.get("aria_label")
            or source.get("placeholder")
            or source.get("text")
            or action.get("label")
        )
        field = {
            "kind": action.get("kind"),
            "tag": source.get("tag") or "",
            "type": source.get("type") or "",
            "role": source.get("role") or action.get("role") or "",
            "label": semantic_label,
            "ref": action.get("ref"),
            "observation_id": source.get("observation_id"),
            "text": action.get("text"),
        }
        # Extract is also the read-back tool after a form action. Preserve the
        # state needed to verify what landed, while never echoing secret input.
        secret_hint = " ".join(str(source.get(key) or "") for key in (
            "type", "name", "id", "label", "aria_label", "placeholder",
        ))
        if source.get("value") not in (None, ""):
            field["value"] = _redact(secret_hint, source.get("value"))[:1000]
        for key in ("selected_text", "checked", "disabled", "required", "href", "file_names"):
            if key in source and source[key] not in (None, ""):
                field[key] = source[key]
        options = source.get("options")
        if isinstance(options, list) and options:
            field["options"] = [
                str(option.get("text") if isinstance(option, dict) else option)[:120]
                for option in options[:40]
            ]
        fields.append(field)
    no_match = no_match_result(instruction, body_text, fields=fields)
    blocker = observed.get("blocker") or classify_blocker({
        "url": snapshot.get("url") or "",
        "title": snapshot.get("title") or "",
        "text": body_text,
    })
    relevant_text, text_coverage = _focused_text(body_text, instruction)
    return {
        "success": True,
        "url": snapshot.get("url") or "",
        "title": snapshot.get("title") or "",
        "instruction": instruction or "",
        "objective": objective_requirements(instruction),
        "visible_text": _norm(body_text)[:12000],
        "relevant_text": relevant_text,
        "text_coverage": text_coverage,
        "fields": fields,
        "controls_total": sum(
            1 for element in snapshot.get("elements") or []
            if isinstance(element, dict) and _is_structured_control(element)
        ),
        "controls_returned": len(control_actions),
        "controls_complete": len(control_actions) == sum(
            1 for element in snapshot.get("elements") or []
            if isinstance(element, dict) and _is_structured_control(element)
        ),
        "visible_errors": observed.get("visible_errors") or [],
        "blocker": blocker,
        "auth_state": observed.get("auth_state") or classify_auth_state({
            "url": snapshot.get("url") or "",
            "title": snapshot.get("title") or "",
            "text": body_text,
        }),
        "no_match": bool(no_match.get("no_match")),
        "no_match_reason": no_match.get("no_match_reason", ""),
    }


async def extract_page(page, *, instruction: str = "") -> dict[str, Any]:
    """Return task-focused visible page information without mutating the page."""
    snapshot = _normalize_snapshot(await build_enhanced_snapshot(page))
    snapshot["visible_errors"] = await visible_errors(page)
    return extract_snapshot(snapshot, instruction=instruction)


def _token_hit(token: str, hay: str, hay_tokens: set[str]) -> bool:
    """Prefer whole-token hits so target 'name' does not match 'username'."""
    if not token:
        return False
    if token in hay_tokens:
        return True
    # Longer tokens may appear inside compound ids (contact-email); keep light.
    return len(token) >= 5 and token in hay


def _score_candidate(
    candidate: dict[str, Any],
    target: str,
    allowed_kinds: set[str],
    *,
    section: str = "",
) -> int:
    if candidate.get("kind") not in allowed_kinds:
        return -1
    context = str(candidate.get("context") or "")
    label_key = _norm_key(candidate.get("label") or "")
    hay = _norm_key(" ".join([
        candidate.get("label") or "",
        candidate.get("text") or "",
        candidate.get("css_selector") or "",
        candidate.get("role") or "",
        context,
    ]))
    hay_tokens = set(hay.split())
    target_key = _norm_key(target)
    if not target_key:
        return 0
    if candidate.get("ref") == target:
        return 100
    score = 0
    if label_key == target_key:
        score += 50
    for token in target_key.split():
        if len(token) < 2:
            continue
        if _token_hit(token, hay, hay_tokens):
            score += 20
    if target_key and (target_key == label_key or target_key in hay_tokens or f" {target_key} " in f" {hay} "):
        score += 40
    section_key = _norm_key(section)
    context_key = _norm_key(context)
    if section_key and context_key:
        if section_key == context_key or section_key in context_key or context_key in section_key:
            score += 55
        else:
            section_tokens = [t for t in section_key.split() if len(t) > 2]
            if section_tokens and all(token in context_key for token in section_tokens):
                score += 40
            elif any(token in context_key for token in section_tokens):
                score += 20
    return score


def _select_candidate(
    candidates: list[dict[str, Any]],
    target: str,
    allowed_kinds: set[str],
    *,
    section: str = "",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    scored = [(c, _score_candidate(c, target, allowed_kinds, section=section)) for c in candidates]
    scored = [(c, s) for c, s in scored if s > 0]
    scored.sort(key=lambda item: item[1], reverse=True)
    if not scored:
        return None, []
    top_score = scored[0][1]
    top = [c for c, s in scored if s == top_score]
    if len(top) > 1 and top_score < 80:
        # Same label score across different sections → ambiguous unless section boost won.
        return None, top[:5]
    if len(top) > 1 and not section:
        contexts = {str(c.get("context") or "") for c in top}
        if len(contexts) > 1:
            return None, top[:5]
    return scored[0][0], [c for c, _ in scored[:5]]


def _field_section(field: dict[str, Any], sticky: str = "") -> str:
    explicit = _norm(
        field.get("section")
        or field.get("form_hint")
        or field.get("context")
        or ""
    )
    return explicit or sticky


def _extract_quoted(text: str) -> str:
    m = re.search(r"['\"]([^'\"]+)['\"]", text)
    return m.group(1).strip() if m else ""


def _parse_fill(action: str) -> tuple[str, str] | None:
    text = _norm(action)
    quoted = _extract_quoted(text)
    patterns = [
        r"(?:fill|enter|type|set)\s+(?:the\s+)?(?P<target>[a-z0-9 _-]{2,40})\s+(?:field\s+)?(?:with|as|to)\s+(?P<value>.+)$",
        r"(?:fill|enter|type)\s+(?P<value>.+?)\s+(?:into|in)\s+(?:the\s+)?(?P<target>[a-z0-9 _-]{2,40})(?:\s+field)?$",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            target = _norm(m.group("target"))
            value = quoted or _norm(m.group("value")).strip(" .")
            return target, value
    if quoted:
        before = text.split(quoted, 1)[0]
        m = re.search(r"(email|password|first name|last name|name|phone|address|city|zip|postal|country)", before, re.I)
        if m:
            return m.group(1), quoted
    return None


def _parse_select(action: str) -> tuple[str, str] | None:
    text = _norm(action)
    m = re.search(r"(?:select|choose|set)\s+(?P<value>.+?)\s+(?:for|in|as)\s+(?:the\s+)?(?P<target>[a-z0-9 _-]{2,40})(?:\s+dropdown|\s+select)?$", text, re.I)
    if m:
        return _norm(m.group("target")), _extract_quoted(text) or _norm(m.group("value")).strip(" .")
    m = re.search(r"(?:select|choose)\s+(?:the\s+)?(?P<target>[a-z0-9 _-]{2,40})\s+(?:dropdown\s+)?(?:value\s+)?(?P<value>.+)$", text, re.I)
    if m:
        return _norm(m.group("target")), _extract_quoted(text) or _norm(m.group("value")).strip(" .")
    return None


def _parse_click_target(action: str) -> str:
    text = _norm(action)
    quoted = _extract_quoted(text)
    if quoted:
        return quoted
    m = re.search(r"(?:click|press|tap|open)\s+(?:the\s+)?(?P<target>.+)$", text, re.I)
    if m:
        return _norm(re.sub(r"\b(button|link|cta)\b", "", m.group("target"), flags=re.I)).strip(" .")
    return text


def _parse_press_key(action: str) -> str:
    match = _PRESS_WORDS.search(_norm(action))
    if not match:
        return ""
    key = match.group("key").lower()
    return {
        "esc": "Escape",
        "escape": "Escape",
        "enter": "Enter",
        "return": "Enter",
        "tab": "Tab",
        "backspace": "Backspace",
        "delete": "Delete",
        "space": "Space",
    }.get(key, key)


def resolve_semantic_action(snapshot: dict[str, Any], instruction: str) -> dict[str, Any]:
    """Resolve a semantic instruction to one frame-aware primitive action."""
    action_text = _norm(instruction)
    observed = observe_snapshot(snapshot, instruction=action_text, max_actions=80)
    candidates = list(observed.get("actions") or [])
    if _UPLOAD_OBJECTIVE_RE.search(action_text) and not _DESTRUCTIVE_ATTACHMENT_RE.search(action_text):
        upload_candidates = [candidate for candidate in candidates if candidate.get("kind") == "upload"]
        return {
            "success": False,
            "error": "upload_requires_file_input_ref",
            "message": "Use browser_set_input_files with a fresh file-input ref; upload controls must not be clicked.",
            "candidates": upload_candidates[:8],
        }
    press_key = _parse_press_key(action_text)
    if press_key:
        return {"success": True, "action": {"action": "press", "key": press_key}, "candidates": []}
    select_spec = _parse_select(action_text)
    fill_spec = None if select_spec else _parse_fill(action_text)
    if fill_spec:
        target, value = fill_spec
        candidate, close = _select_candidate(candidates, target, {"fill"})
        if not candidate:
            return {"success": False, "error": "ambiguous_target" if close else "target_not_found", "candidates": close}
        return {"success": True, "action": _candidate_primitive("input", candidate, text=value), "candidates": close}
    if select_spec:
        target, value = select_spec
        candidate, close = _select_candidate(candidates, target, {"select"})
        if not candidate:
            return {"success": False, "error": "ambiguous_target" if close else "target_not_found", "candidates": close}
        return {"success": True, "action": _candidate_primitive("select", candidate, value=value), "candidates": close}
    if _CLICK_WORDS.search(action_text) or _submit_like(action_text):
        target = _parse_click_target(action_text)
        candidate, close = _select_candidate(candidates, target, {"click"})
        if not candidate:
            return {"success": False, "error": "ambiguous_target" if close else "target_not_found", "candidates": close}
        return {"success": True, "action": _candidate_primitive("click", candidate), "candidates": close}
    return {"success": False, "error": "unsupported_action", "candidates": candidates[:8]}


def resolve_form_field(snapshot: dict[str, Any], target: str, value: Any, kind: str = "") -> dict[str, Any]:
    """Resolve a form field by label and infer the primitive from its control kind."""
    candidates = list(observe_snapshot(snapshot, instruction=target, max_actions=80).get("actions") or [])
    requested = _norm_key(kind)
    allowed = {"select"} if requested == "select" else {"click"} if requested in {"checkbox", "radio"} else {"upload"} if requested in {"upload", "file"} else {"fill", "select"}
    candidate, close = _select_candidate(candidates, target, allowed)
    if not candidate:
        return {"success": False, "error": "ambiguous_target" if close else "target_not_found", "candidates": close}
    candidate_kind = str(candidate.get("kind") or "")
    if requested in {"checkbox", "radio"}:
        action = _candidate_primitive("check", candidate, checked=bool(value))
    elif requested in {"upload", "file"}:
        action = _candidate_primitive("upload", candidate)
    elif requested == "select" or candidate_kind == "select":
        action = _candidate_primitive("select", candidate, value=str(value or ""))
    else:
        action = _candidate_primitive("input", candidate, text=str(value or ""))
    return {"success": True, "action": action, "candidates": close}


def _candidate_primitive(name: str, candidate: dict[str, Any], **params: Any) -> dict[str, Any]:
    action = {"action": name, "ref": candidate.get("ref") or "", **params}
    for key in ("frame_index", "frame_name", "frame_url"):
        if candidate.get(key) not in (None, ""):
            action[key] = candidate[key]
    return action


async def _fill(page, candidate: dict[str, Any], value: str) -> None:
    selector = candidate.get("css_selector") or parse_ref(candidate["ref"], allow_selector=True)
    locator = page.locator(selector).first
    await locator.fill(value, timeout=8000)


async def _select(page, candidate: dict[str, Any], value: str) -> None:
    selector = candidate.get("css_selector") or parse_ref(candidate["ref"], allow_selector=True)
    locator = page.locator(selector).first
    try:
        await locator.select_option(label=value, timeout=8000)
    except Exception:
        await locator.select_option(value=value, timeout=8000)


async def _set_checked(locator, checked: bool) -> None:
    """Set checkbox/radio state; fall back to native setter for visually-hidden styled inputs."""
    try:
        if checked:
            if hasattr(locator, "check"):
                await locator.check(timeout=8000)
            else:
                await locator.click(timeout=8000)
        else:
            if hasattr(locator, "uncheck"):
                await locator.uncheck(timeout=8000)
            else:
                await locator.click(timeout=8000)
        return
    except Exception:
        pass
    await locator.evaluate(
        """(el, desired) => {
            const input = el;
            const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked');
            if (desc && desc.set) desc.set.call(input, desired);
            else input.checked = desired;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        checked,
    )


def _candidate_selector(candidate: dict[str, Any]) -> str:
    return str(candidate.get("css_selector") or candidate.get("selector") or parse_ref(str(candidate.get("ref") or ""), allow_selector=True))


def _field_target(field: dict[str, Any]) -> str:
    return _norm(
        field.get("label")
        or field.get("target")
        or field.get("name")
        or field.get("placeholder")
        or field.get("ref")
        or field.get("selector")
        or ""
    )


def _field_action_kind(field: dict[str, Any], candidate: dict[str, Any] | None = None) -> str:
    explicit = _norm_key(field.get("kind") or field.get("type") or "")
    if explicit in {"select", "checkbox", "radio", "upload", "file"}:
        return "upload" if explicit == "file" else explicit
    if field.get("file_path") or field.get("path"):
        return "upload"
    if isinstance(field.get("checked"), bool):
        return "checkbox"
    if candidate:
        kind = str(candidate.get("kind") or "").lower()
        role = str(candidate.get("role") or "").lower()
        if kind == "select":
            return "select"
        if role in {"checkbox", "radio"}:
            return "checkbox"
    return "fill"


def _field_allowed_kinds(field: dict[str, Any]) -> set[str]:
    """Kinds allowed when resolving a fill_form field by label (exclude submit buttons)."""
    kind = _field_action_kind(field, None)
    if kind in {"checkbox", "radio"}:
        return {"click"}
    if kind == "select":
        return {"select"}
    if kind == "upload":
        return {"upload"}
    return {"fill", "select"}


async def _apply_field(page, field: dict[str, Any], candidate: dict[str, Any] | None) -> dict[str, Any]:
    selector = str(field.get("selector") or field.get("css_selector") or "")
    ref = str(field.get("ref") or "")
    if not selector and ref:
        selector = parse_ref(ref, allow_selector=True)
    if not selector and candidate:
        selector = _candidate_selector(candidate)
        ref = str(candidate.get("ref") or ref)
    if not selector:
        raise ValueError("field requires selector/ref/label")

    locator = page.locator(selector).first
    kind = _field_action_kind(field, candidate)
    target = _field_target(field) or (candidate or {}).get("label") or selector
    value = field.get("value")
    if kind == "upload":
        from ascended_browser._app.browser_upload import set_input_files_verified

        path = str(field.get("file_path") or field.get("path") or value or "").strip()
        if not path:
            raise ValueError("upload field requires file_path")
        if not ref:
            raise ValueError("upload field requires a fresh file-input ref")
        upload_result = await set_input_files_verified(
            page,
            selector=selector,
            paths=[path],
            target_ref=ref,
            expected_identity=ref_identity(ref),
            timeout_ms=10000,
        )
        return {
            "action": "upload", "target": target, "ref": ref, "success": True,
            "uploaded": True, "upload_verification": upload_result,
        }
    if kind == "select":
        text = str(value if value is not None else field.get("text") or "").strip()
        try:
            await locator.select_option(label=text, timeout=8000)
        except Exception:
            await locator.select_option(value=text, timeout=8000)
        return {"action": "select", "target": target, "ref": ref, "value": text, "success": True}
    if kind in {"checkbox", "radio"}:
        checked = bool(field.get("checked", True))
        await _set_checked(locator, checked)
        return {"action": "click", "target": target, "ref": ref, "checked": checked, "success": True}
    text = str(value if value is not None else field.get("text") or "")
    await locator.fill(text, timeout=8000)
    return {"action": "fill", "target": target, "ref": ref, "value": _redact(target, text), "success": True}


def _submit_like(action: str, candidate: dict[str, Any] | None = None) -> bool:
    hay = action
    if candidate:
        hay += " " + str(candidate.get("label") or "") + " " + str(candidate.get("text") or "")
    return bool(_SUBMIT_WORDS.search(hay))


def _blocks_before_acting(blocker: dict[str, Any] | None) -> bool:
    """Only an interstitial that owns the page is worth refusing to act on.

    A rendered captcha widget still leaves the rest of the form usable, so the
    action runs and the caller learns from no_progress_after_click instead.
    """
    if not blocker or blocker.get("type") != "challenge":
        return False
    return str(blocker.get("state") or "") == "auto_verifying"


async def _act_page_inner(page, *, action: str, expect: str = "", submit_intent: str = "none") -> dict[str, Any]:
    observed = await observe_page(page, instruction=action, max_actions=80)
    candidates = list(observed.get("actions") or [])
    before = await capture_page_state(page)
    before_snapshot = await _verification_snapshot(page, before)
    action_text = _norm(action)
    submit_intent = _norm_key(submit_intent or "none") or "none"
    blocker = observed.get("blocker") or classify_blocker(before_snapshot)
    if _blocks_before_acting(blocker):
        return _failure(
            "challenge_detected",
            before,
            candidates=[],
            needs_user=False,
            blocker=blocker,
        )

    if _UPLOAD_OBJECTIVE_RE.search(action_text) and not _DESTRUCTIVE_ATTACHMENT_RE.search(action_text):
        upload_candidates = [candidate for candidate in candidates if candidate.get("kind") == "upload"]
        return _failure(
            "upload_requires_file_input_ref",
            before,
            candidates=upload_candidates[:8],
            needs_user=False,
            blocker=blocker,
        )

    press_key = _parse_press_key(action_text)
    if press_key:
        try:
            await page.keyboard.press(press_key)
            await page.wait_for_timeout(250)
        except Exception:
            await asyncio.sleep(0.25)
        after = await capture_page_state(page)
        after_snapshot = await _verification_snapshot(page, after)
        verification = verify_browser_action("press", {"key": press_key}, before_snapshot, after_snapshot, True)
        return _success(
            before,
            after,
            [{"kind": "press", "key": press_key}],
            visible=await visible_errors(page),
            verification=verification,
            task_description=action,
            blocker=classify_blocker(after_snapshot),
        )

    select_spec = _parse_select(action_text)
    fill_spec = None if select_spec else _parse_fill(action_text)
    if fill_spec:
        target, value = fill_spec
        candidate, close = _select_candidate(candidates, target, {"fill"})
        if not candidate:
            return _failure("ambiguous_target" if close else "target_not_found", before, candidates=close, needs_user=True, blocker=blocker)
        await _fill(page, candidate, value)
        after = await capture_page_state(page)
        after_snapshot = await _verification_snapshot(page, after)
        action_step = {
            "kind": "fill",
            "action": "fill",
            "target": candidate.get("label") or target,
            "ref": candidate.get("ref"),
            "selector": candidate.get("css_selector") or "",
            "value": _redact(target, value),
            "success": True,
        }
        verification = verify_browser_action("fill", {**action_step, "value": value}, before_snapshot, after_snapshot, True)
        return _success(before, after, [{
            k: v for k, v in action_step.items() if k != "selector"
        }], visible=await visible_errors(page), verification=verification, task_description=action, blocker=classify_blocker(after_snapshot))

    if select_spec:
        target, value = select_spec
        candidate, close = _select_candidate(candidates, target, {"select"})
        if not candidate:
            return _failure("ambiguous_target" if close else "target_not_found", before, candidates=close, needs_user=True, blocker=blocker)
        await _select(page, candidate, value)
        after = await capture_page_state(page)
        after_snapshot = await _verification_snapshot(page, after)
        action_step = {
            "kind": "select",
            "action": "select",
            "target": candidate.get("label") or target,
            "ref": candidate.get("ref"),
            "selector": candidate.get("css_selector") or "",
            "value": value,
            "success": True,
        }
        verification = verify_browser_action("select", action_step, before_snapshot, after_snapshot, True)
        return _success(before, after, [{
            k: v for k, v in action_step.items() if k != "selector"
        }], visible=await visible_errors(page), verification=verification, task_description=action, blocker=classify_blocker(after_snapshot))

    if _CLICK_WORDS.search(action_text) or _submit_like(action_text):
        target = _parse_click_target(action_text)
        candidate, close = _select_candidate(candidates, target, {"click"})
        if not candidate:
            return _failure("ambiguous_target" if close else "target_not_found", before, candidates=close, needs_user=True, blocker=blocker)
        message, method = await click_with_fallbacks(page, ref=str(candidate["ref"]), label=candidate.get("label") or target)
        try:
            await page.wait_for_timeout(500)
        except Exception:
            await asyncio.sleep(0.5)
        after = await capture_page_state(page)
        after_snapshot = await _verification_snapshot(page, after)
        action_step = {
            "kind": "click",
            "action": "click",
            "target": candidate.get("label") or target,
            "ref": candidate.get("ref"),
            "method": method,
            "message": message,
            "success": True,
            "submit_intent": submit_intent,
        }
        verification = verify_browser_action("click", action_step, before_snapshot, after_snapshot, True)
        out = _success(before, after, [{
            k: v for k, v in action_step.items() if k not in {"action", "success", "submit_intent"}
        }], visible=await visible_errors(page), verification=verification, task_description=action, submit_intent=submit_intent, blocker=classify_blocker(after_snapshot))
        if not out["changed"]:
            return _failure("no_progress_after_click", after, candidates=[candidate], needs_user=False, visible=out["visible_errors"], verification=verification, blocker=out.get("blocker"))
        return out

    return _failure("unsupported_action", before, candidates=candidates[:8], needs_user=True, blocker=blocker)


def _failure(
    error: str,
    state: dict[str, Any],
    *,
    candidates: list[dict[str, Any]] | None = None,
    needs_user: bool = True,
    visible: list[str] | None = None,
    verification: dict[str, Any] | None = None,
    blocker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "error": error,
        "current_url": state.get("url") or "",
        "title": state.get("title") or "",
        "candidates": candidates or [],
        "visible_errors": visible or [],
        "needs_user": needs_user,
        "verified": bool((verification or {}).get("checked")),
        "verification": verification or {},
        "blocker": blocker,
        "no_match": False,
        "no_match_reason": "",
        "form_success": False,
        "form_result": {},
    }


def _success(
    before: dict[str, Any],
    after: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    visible: list[str],
    verification: dict[str, Any] | None = None,
    task_description: str = "",
    submit_intent: str = "none",
    blocker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    nav_type = classify_navigation(before, after)
    changed = nav_type != "none" or bool(actions and actions[0].get("kind") in {"fill", "select"})
    steps = []
    for item in actions:
        step = {
            "action": item.get("kind") or item.get("action"),
            "kind": item.get("kind") or item.get("action"),
            "success": True,
            "verification": verification or {},
        }
        if submit_intent not in {"", "none", "false", "no"}:
            step["submit_intent"] = submit_intent
        steps.append(step)
    form_result = form_success_result(
        task_description,
        steps,
        {"observed_evidence": {"post_submission_confirmation": _looks_like_confirmation(after)}},
        "done",
    )
    return {
        "success": True,
        "changed": changed,
        "current_url": after.get("url") or "",
        "title": after.get("title") or "",
        "actions": actions,
        "visible_errors": visible,
        "needs_user": False,
        "reason": None,
        "navigation_type": nav_type,
        "verified": bool((verification or {}).get("checked")),
        "verification": verification or {},
        "blocker": blocker,
        "no_match": False,
        "no_match_reason": "",
        "form_success": bool(form_result),
        "form_result": form_result,
    }


def _looks_like_confirmation(state: dict[str, Any]) -> bool:
    hay = _norm(" ".join(str(state.get(key) or "") for key in ("title", "text", "visible_text"))).lower()
    return bool(re.search(r"\b(?:thank you|submitted|success|received|confirmation|application complete)\b", hay))


async def fill_form_page(
    page,
    *,
    fields: list[dict[str, Any]],
    submit_intent: str = "none",
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Fill multiple visible form fields in one bounded live-browser action."""
    timeout = max(1000, min(int(timeout_ms or 30000), 90000)) / 1000
    try:
        return await asyncio.wait_for(
            _fill_form_page_inner(page, fields=fields, submit_intent=submit_intent),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        state = await capture_page_state(page)
        return _failure("action_timeout", state, needs_user=True, visible=await visible_errors(page))


async def _fill_form_page_inner(page, *, fields: list[dict[str, Any]], submit_intent: str = "none") -> dict[str, Any]:
    if not isinstance(fields, list) or not fields:
        state = await capture_page_state(page)
        return _failure("fields_required", state, needs_user=True)
    observed = await observe_page(page, instruction="fill form", max_actions=80)
    candidates = list(observed.get("actions") or [])
    before = await capture_page_state(page)
    before_snapshot = await _verification_snapshot(page, before)
    blocker = observed.get("blocker") or classify_blocker(before_snapshot)
    if _blocks_before_acting(blocker):
        return _failure(
            "challenge_detected",
            before,
            needs_user=False,
            blocker=blocker,
        )

    field_results: list[dict[str, Any]] = []
    action_steps: list[dict[str, Any]] = []
    sticky_section = ""
    for index, raw_field in enumerate(fields[:80]):
        field = raw_field if isinstance(raw_field, dict) else {"value": raw_field}
        target = _field_target(field)
        candidate = None
        close: list[dict[str, Any]] = []
        if not (field.get("selector") or field.get("css_selector") or field.get("ref")):
            allowed = _field_allowed_kinds(field)
            section = _field_section(field, sticky_section)
            candidate, close = _select_candidate(candidates, target, allowed, section=section)
            if not candidate:
                field_results.append({
                    "index": index,
                    "target": target,
                    "success": False,
                    "error": "ambiguous_target" if close else "target_not_found",
                    "candidates": close,
                    "section": section or None,
                })
                continue
        try:
            result = await _apply_field(page, field, candidate)
            result["index"] = index
            field_results.append(result)
            action_steps.append({
                **result,
                "verification": result.get("upload_verification") or {"form_control": True},
            })
            # Sticky section: once a label resolves inside a form/section, prefer it for later fields.
            if candidate and not sticky_section:
                sticky_section = str(candidate.get("context") or "").strip()
        except Exception as exc:
            field_results.append({
                "index": index,
                "target": target,
                "success": False,
                "error": str(exc),
            })

    after = await capture_page_state(page)
    after_snapshot = await _verification_snapshot(page, after)
    verification = verify_browser_action(
        "batch_form_fill",
        {"fields": fields, "field_results": field_results},
        before_snapshot,
        after_snapshot,
        any(item.get("success") for item in field_results),
    )
    visible = await visible_errors(page)
    form_result = form_success_result(
        "fill form" + (" and upload file" if any(r.get("uploaded") for r in field_results) else ""),
        action_steps,
        {"observed_evidence": {"post_submission_confirmation": False}},
        "done" if any(item.get("success") for item in field_results) else "error",
    )
    nav_type = classify_navigation(before, after)
    success = bool(field_results) and all(item.get("success") for item in field_results)
    first_error = next(
        (str(item.get("error") or "") for item in field_results if not item.get("success")),
        "",
    )
    return {
        "success": success,
        "changed": nav_type != "none" or bool(field_results),
        "current_url": after.get("url") or "",
        "title": after.get("title") or "",
        "actions": [{"kind": "batch_form_fill", "fields": field_results}],
        "field_results": field_results,
        "visible_errors": visible,
        "needs_user": any(not item.get("success") for item in field_results),
        "error": None if success else (first_error or "form_fields_failed"),
        "reason": None,
        "navigation_type": nav_type,
        "verified": bool(verification.get("checked")),
        "verification": verification,
        "blocker": classify_blocker(after_snapshot),
        "no_match": False,
        "no_match_reason": "",
        "form_success": bool(form_result),
        "form_result": form_result,
        "submit_intent": submit_intent or "none",
    }


async def act_page(page, *, action: str, expect: str = "", submit_intent: str = "none", timeout_ms: int = 20000) -> dict[str, Any]:
    """Execute one deterministic browser action with bounded runtime."""
    timeout = max(1000, min(int(timeout_ms or 20000), 60000)) / 1000
    try:
        return await asyncio.wait_for(
            _act_page_inner(page, action=action, expect=expect, submit_intent=submit_intent),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        state = await capture_page_state(page)
        return _failure("action_timeout", state, needs_user=True, visible=await visible_errors(page))
