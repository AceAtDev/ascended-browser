"""Explicit private live verification for high-level browser actions.

The model should not need ``browser_evaluate`` to prove that ``fill`` or
``select`` worked. The manager explicitly composes these functions with its primitives, reads the control back locally, and exposes only a
small receipt to the model-facing verifier.

Model-facing value previews are derived only after full private comparison.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import re
from typing import Any

from ascended_browser._app.browser_sensitive_state import is_password_element
from ascended_browser._app.browser_semantic_controls import (
    SemanticControlError,
    select_option_semantic,
    verify_selection_commit,
)


_FILL_EVIDENCE: ContextVar[dict[str, Any] | None] = ContextVar(
    "browser_private_fill_evidence", default=None,
)
_FILL_READBACK: ContextVar[dict[str, Any] | None] = ContextVar(
    "browser_private_fill_readback", default=None,
)
# Evidence a primitive proved itself, when the control it acted on no longer
# exists to be read back (a menu option that closes its menu on click).
_PRIMITIVE_EVIDENCE: ContextVar[dict[str, Any] | None] = ContextVar(
    "browser_primitive_fill_evidence", default=None,
)


def publish_primitive_evidence(evidence: dict[str, Any]) -> None:
    _PRIMITIVE_EVIDENCE.set(dict(evidence))


_SELECT_EVIDENCE: ContextVar[dict[str, Any] | None] = ContextVar(
    "browser_private_select_evidence", default=None,
)


def consume_fill_evidence() -> dict[str, Any] | None:
    value = _FILL_EVIDENCE.get()
    _FILL_EVIDENCE.set(None)
    _FILL_READBACK.set(None)
    return dict(value) if isinstance(value, dict) else None


def current_fill_probe() -> dict[str, Any] | None:
    """Private locator/readback state for a batch's final-state comparison."""
    probe = _FILL_READBACK.get()
    return dict(probe) if isinstance(probe, dict) else None


async def verify_fill_probe_final(probe: dict[str, Any]) -> dict[str, Any]:
    """Read one earlier batch field again after later fields have committed."""
    actual, strategy = await _read_pending_fill(probe)
    return _fill_evidence(probe, actual, strategy)


def consume_select_evidence() -> dict[str, Any] | None:
    value = _SELECT_EVIDENCE.get()
    _SELECT_EVIDENCE.set(None)
    return dict(value) if isinstance(value, dict) else None


def publish_select_evidence(value: dict[str, Any]) -> None:
    """Publish one aggregate select receipt for the enclosing action owner."""
    _SELECT_EVIDENCE.set(dict(value))


def _as_workspace_error(core: Any, exc: Exception) -> Exception:
    """Re-raise a semantic control failure without losing what it knows.

    Whether the page was touched travels with the error: a refusal that
    inspected the control and declined lets a batch carry on with the fields
    that have nothing to do with it. Flattening it to a bare WorkspaceError
    made every refusal look like it might have changed something.
    """
    error = core.WorkspaceError(str(exc))
    error.dispatched = bool(getattr(exc, "dispatched", True))
    # The evidence the control captured while its popup was open, and its
    # type: dropped here, a no-match select reported error_kind "navigation"
    # and lost its observed_options (live, 2026-10-05).
    for key in ("error_kind", "observed_options", "popup_strategy", "inspect"):
        value = getattr(exc, key, None)
        if value:
            setattr(error, key, value)
    return error


def _css_string(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _candidate(control: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": control.get("tag") or "",
        "role": control.get("role") or "",
        "label": identity.get("ariaLabel") or identity.get("placeholder") or control.get("text") or "",
        "aria_controls": control.get("ariaControls") or "",
        "aria_autocomplete": control.get("ariaAutocomplete") or "",
        "aria_expanded": control.get("ariaExpanded") or "",
        "multiple": bool(control.get("multiple")),
    }


def _verification_selector(original: str, control: dict[str, Any], identity: dict[str, Any]) -> str:
    """Prefer an author locator that survives framework node replacement."""
    element_id = str(identity.get("id") or "").strip()
    if element_id:
        return f'[id="{_css_string(element_id)}"]'
    name = str(identity.get("name") or "").strip()
    tag = str(control.get("tag") or "").strip().lower()
    if name and tag in {"input", "textarea", "select"}:
        return f'{tag}[name="{_css_string(name)}"]'
    aria_controls = str(control.get("ariaControls") or "").strip()
    if aria_controls and str(control.get("role") or "").strip().casefold() == "combobox":
        return f'[role="combobox"][aria-controls="{_css_string(aria_controls)}"]'
    return original


async def _describe(core: Any, root: Any, selector: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    locator = root.locator(selector).first
    try:
        control = await core._bounded(locator.evaluate(core._DESCRIBE_CONTROL), 5.0)
    except Exception:
        control = {}
    try:
        identity = await core._bounded(
            locator.evaluate(
                """el => ({
                  id: el.id || '',
                  name: el.getAttribute('name') || '',
                  ariaLabel: el.getAttribute('aria-label') || '',
                  placeholder: el.getAttribute('placeholder') || ''
                })"""
            ),
            3.0,
        )
    except Exception:
        identity = {}
    if not isinstance(control, dict):
        control = {}
    if not isinstance(identity, dict):
        identity = {}
    return control, identity, _verification_selector(selector, control, identity)


def _mask_normalized(value: str) -> str:
    """What the field stores, with the separators a mask inserts removed."""
    return re.sub(r"[^0-9A-Za-z]+", "", str(value or ""))


def _fill_actual_matches(expected: str, actual: str, tag: str) -> bool:
    if tag == "textarea":
        norm = lambda value: str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        return norm(actual) == norm(expected)
    if str(actual) == str(expected):
        return True
    # An input mask rewrites what was typed as it is typed: a phone field turns
    # 6135550100 into (613) 555-0100, a card field groups digits, a date field
    # inserts slashes. The characters that carry the value are unchanged, and
    # calling that a contradiction sent a live run hunting a field that was
    # already correct. A field that drops or changes characters still fails.
    normalized = _mask_normalized(expected)
    return bool(normalized) and _mask_normalized(actual) == normalized


async def _read_fill_value(root: Any, selector: str, control: dict[str, Any]) -> str | bool | None:
    """Read complete local state from exactly one control in its owning frame."""
    try:
        locator = root.locator(selector)
        # Author locators survive replacement, but duplicate ids/names must
        # never let .first mint evidence from an unrelated control.
        if await locator.count() != 1:
            return None
        locator = locator.first
        tag = str(control.get("tag") or "").casefold()
        typ = str(control.get("type") or "").casefold()
        role = str(control.get("role") or "").casefold()
        if role == "option" and tag not in {"input", "textarea"}:
            selected = await locator.get_attribute("aria-selected", timeout=500)
            return {"true": True, "false": False}.get(str(selected or "").strip().casefold())
        if str(control.get("ariaPressed") or "") in {"true", "false"} and tag not in {"input", "textarea"}:
            pressed = await locator.get_attribute("aria-pressed", timeout=500)
            return {"true": True, "false": False}.get(str(pressed or "").strip().casefold())
        if typ in {"checkbox", "radio"} or role in {
            "checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio",
        }:
            aria_checked = await locator.get_attribute("aria-checked", timeout=500)
            if aria_checked is not None:
                state = str(aria_checked).strip().casefold()
                return {"true": True, "false": False}.get(state)
            return await locator.is_checked(timeout=500)
        if tag in {"input", "textarea"}:
            return str(await locator.input_value(timeout=500))
        if bool(control.get("editable")):
            return str(await locator.inner_text(timeout=500))
    except Exception:
        return None
    return None


def _value_preview(value: str, *, limit: int = 160) -> str:
    if len(value) <= limit:
        return value
    # Evidence was already compared in full. This is presentation only.
    return value[:80] + "…[truncated]…" + value[-64:]


async def _read_pending_fill(probe: dict[str, Any]) -> tuple[str | bool | None, str]:
    root, original, author = probe["root"], probe["selector"], probe["verify_selector"]
    # Keep the exact observed identity while it exists. Only a stale observed
    # locator may fall back to a unique author locator within the same root.
    for selector in dict.fromkeys((original, author)):
        try:
            count = await asyncio.wait_for(root.locator(selector).count(), timeout=0.5)
            if count == 0:
                continue
            if count != 1:
                return None, "ambiguous"
            actual = await asyncio.wait_for(
                _read_fill_value(root, selector, probe["control"]), timeout=0.6,
            )
        except Exception:
            return None, "unresolved"
        return actual, "observed_ref" if selector == original else "stable_author_locator"
    return None, "unresolved"


def _fill_evidence(probe: dict[str, Any], actual: str | bool | None, strategy: str) -> dict[str, Any]:
    control, expected = probe["control"], probe["expected"]
    checked = probe["checkable"]
    sensitive = is_password_element(control)
    verified = actual is expected if checked else (
        isinstance(actual, str)
        and _fill_actual_matches(expected, actual, str(control.get("tag") or "").casefold())
    )
    reformatted = bool(
        verified and not checked and isinstance(actual, str) and actual != expected
    )
    evidence: dict[str, Any] = {
        "verified": verified,
        "verification": (
            "live_checked_state" if checked
            # Say which readback proved it: the field holds the value it was
            # given, displayed in the field's own format.
            else "live_control_value_reformatted" if reformatted
            else "live_control_value"
        ),
        "requested": expected if checked else "[masked]" if sensitive else _value_preview(expected),
        "observed": actual if checked else (
            None if actual is None else "[masked]" if sensitive else _value_preview(actual)
        ),
        "selector_strategy": strategy,
    }
    if not checked:
        evidence["requested_length"] = len(expected)
        if isinstance(actual, str):
            evidence["observed_length"] = len(actual)
        if sensitive:
            evidence["sensitive"] = True
        elif len(expected) > 160 or isinstance(actual, str) and len(actual) > 160:
            evidence["values_truncated"] = True
    if not verified:
        evidence["failure_reason"] = "readback_unavailable" if actual is None else "state_mismatch"
    return evidence


async def refresh_fill_evidence() -> bool:
    """Recheck after the manager's observation, without reading its projection.

    A framework can replace/rewrite the input while that observation is built.
    Keep the final private read under the same action/tab lock and fail closed
    if it no longer matches. No locator or full value enters the receipt.
    """
    probe = _FILL_READBACK.get()
    if not isinstance(probe, dict):
        return False
    actual, strategy = await _read_pending_fill(probe)
    evidence = _fill_evidence(probe, actual, strategy)
    previous = _FILL_EVIDENCE.get() or {}
    if "settle_ms" in previous:
        evidence["settle_ms"] = previous["settle_ms"]
    # A single final matching sample cannot erase a failed settling window.
    if previous.get("verified") is not True and evidence["verified"]:
        evidence["verified"] = False
        evidence["failure_reason"] = "state_not_settled"
    _FILL_EVIDENCE.set(evidence)
    return True


async def _settle_fill_evidence(probe: dict[str, Any]) -> dict[str, Any]:
    """Observe through a bounded 250 ms settling window, not just first match."""
    previous: str | bool | None = None
    stable = False
    for sample in range(3):
        if sample:
            # Playwright wait_for_timeout also works on a Frame; controllable
            # roots in contract tests advance their DOM clock here.
            wait = getattr(probe["root"], "wait_for_timeout", None)
            if wait is None:
                await asyncio.sleep(0.125)
            else:
                await asyncio.wait_for(wait(125), timeout=0.3)
        actual, strategy = await _read_pending_fill(probe)
        stable = actual is not None and actual == previous
        previous = actual
    evidence = _fill_evidence(probe, actual, strategy)
    if evidence["verified"] and not stable:
        evidence["verified"] = False
        evidence["failure_reason"] = "state_not_settled"
    evidence["settle_ms"] = 250
    return evidence


def _selection_commit_visibility_failure(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return (
        "did not expose a committed selection" in text
        or ("clicked option" in text and "committed selection" in text)
    )


async def verified_fill_control(
    core: Any,
    primitive: Any,
    root: Any,
    selector: str,
    value: Any,
    *,
    timeout_ms: int = 10000,
) -> str:
    _FILL_EVIDENCE.set(None)
    _FILL_READBACK.set(None)
    _PRIMITIVE_EVIDENCE.set(None)
    control, identity, verify_selector = await _describe(core, root, selector)
    detail = await primitive(root, selector, value, timeout_ms=timeout_ms)
    proved = _PRIMITIVE_EVIDENCE.get()
    if proved is not None:
        _PRIMITIVE_EVIDENCE.set(None)
        _FILL_EVIDENCE.set(proved)
        return detail

    tag = str(control.get("tag") or "").casefold()
    typ = str(control.get("type") or "").casefold()
    role = str(control.get("role") or "").casefold()
    if tag == "select":
        # Native selects produce their own private selection evidence.
        selected = _SELECT_EVIDENCE.get()
        if isinstance(selected, dict):
            _FILL_EVIDENCE.set({**selected, "verification": "live_selected_option",
                                "requested": str(value), "observed": selected.get("observed")})
            _SELECT_EVIDENCE.set(None)
        return detail
    checkable = typ in {"checkbox", "radio"} or role in {
        "checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio",
    } or (role == "option" and tag not in {"input", "textarea"}) or (
        str(control.get("ariaPressed") or "") in {"true", "false"} and tag not in {"input", "textarea"}
    )
    expected: str | bool = "" if value is None else str(value)
    if checkable:
        expected = expected.strip().casefold() not in {"false", "0", "no", "off", "unchecked", ""}
    probe = {
        "root": root, "selector": selector, "verify_selector": verify_selector,
        "control": control, "expected": expected, "checkable": checkable,
    }
    _FILL_READBACK.set(probe)
    from ascended_browser._app.browser_execution import action_stage
    action_stage("verify")
    try:
        evidence = await asyncio.wait_for(
            _settle_fill_evidence(probe), timeout=max(0.001, min(2.0, timeout_ms / 1000)),
        )
        # Set in the action's context, even on Python versions where
        # wait_for executes the coroutine in a separate asyncio task.
        _FILL_EVIDENCE.set(evidence)
    except Exception:
        # A successful mutation with unreadable/unstable evidence remains
        # an explicit uncertain result. Never fall back to a lossy snapshot.
        _FILL_EVIDENCE.set(_fill_evidence(probe, None, "unresolved"))
    return detail

async def verified_select_control(
    core: Any,
    primitive: Any,
    root: Any,
    selector: str,
    value: Any,
    *,
    query: Any = None,
    option: Any = None,
    timeout_ms: int = 10000,
) -> str:
    _SELECT_EVIDENCE.set(None)
    wanted = str(option if option not in (None, "") else value if value is not None else "").strip()
    control, identity, verify_selector = await _describe(core, root, selector)
    candidate = _candidate(control, identity)
    tag = str(control.get("tag") or "").casefold()
    role = str(control.get("role") or "").casefold()
    typ = str(control.get("type") or "").casefold()
    # A typeahead input that owns a popup but declares no role (Workday's
    # "How did you hear" search) is a select to the user. Refusing it made the
    # model switch verbs; the semantic path types the value as a filter and
    # fails with the popup's actual options if this is not a list at all.
    typeahead = tag == "input" and typ in {"", "text", "search"} and not role

    # Button-backed Workday/MUI-style selects are outside the legacy core's
    # native/role=combobox contract. Own them here so they share the same
    # exact-match + local-commit verification path and never hit the older
    # facade fallback that can accidentally toggle an open popup twice.
    if tag == "button" or typeahead or role in {"button", "combobox"} and tag != "select":
        try:
            evidence = await select_option_semantic(
                root,
                selector=selector,
                candidate=candidate,
                value=wanted,
                query=str(query) if query not in (None, "") else None,
                timeout_ms=timeout_ms,
                readback_selector=verify_selector,
            )
        except SemanticControlError as exc:
            raise _as_workspace_error(core, exc) from exc
        evidence = {
            **evidence,
            "selector_strategy": "stable_author_locator" if verify_selector != selector else "observed_ref",
        }
        _SELECT_EVIDENCE.set(evidence)
        shape = (
            "button-backed popup" if tag == "button" or role == "button"
            else "typeahead" if typeahead else "combobox"
        )
        return f"selected {wanted!r} from {shape}"

    try:
        detail = await primitive(
            root,
            selector,
            value,
            query=query,
            option=option,
            timeout_ms=timeout_ms,
        )
    except core.WorkspaceError as exc:
        if not _selection_commit_visibility_failure(exc):
            raise
        try:
            evidence = await verify_selection_commit(
                root,
                selector=verify_selector,
                candidate=candidate,
                value=wanted,
                timeout_ms=min(timeout_ms, 3500),
            )
        except SemanticControlError:
            raise exc
        evidence = {
            **evidence,
            "recovered_from": "legacy_commit_visibility_failure",
            "selector_strategy": "stable_author_locator" if verify_selector != selector else "observed_ref",
        }
        _SELECT_EVIDENCE.set(evidence)
        return f"selected {wanted!r}; verified from live committed control state"

    try:
        evidence = await verify_selection_commit(
            root,
            selector=verify_selector,
            candidate=candidate,
            value=wanted,
            timeout_ms=min(timeout_ms, 3500),
        )
    except SemanticControlError as exc:
        raise _as_workspace_error(core, exc) from exc
    evidence = {
        **evidence,
        "selector_strategy": "stable_author_locator" if verify_selector != selector else "observed_ref",
    }
    _SELECT_EVIDENCE.set(evidence)
    return detail
