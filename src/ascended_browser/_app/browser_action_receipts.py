"""Postcondition receipts for browser mutations.

The browser should distinguish "the input event was dispatched" from "the
requested state is now observable". This module is intentionally small and
Playwright-shaped: actions execute elsewhere, then this layer reads the target
back and proves the semantic postcondition before the manager reports success.

Design references:
- Playwright actionability/assertions: execution and state assertions are
  separate, auto-waited concerns.
- Skyvern's public ActionResult/commit-evidence design inspired the *shape* of
  the receipt, but no AGPL source is copied here.
"""
from __future__ import annotations

import re
from typing import Any

from ascended_browser._app.browser_live_verification import (
    consume_fill_evidence,
    consume_select_evidence,
)
from ascended_browser._app.browser_sensitive_state import is_password_element, redact_password_snapshot


_VERIFY_KINDS = frozenset({"fill", "select", "check", "upload", "scroll"})


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _fold(value: Any) -> str:
    return _norm(value).casefold()


def _textarea_norm(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _fold(value)
    if text in {"true", "1", "yes", "checked", "on"}:
        return True
    if text in {"false", "0", "no", "unchecked", "off"}:
        return False
    return None


def _target_from_page_snapshot(page_snapshot: dict[str, Any] | None, action: dict[str, Any]) -> dict[str, Any]:
    """Resolve post-action evidence from a projected observation, fail-closed."""
    snapshot = page_snapshot if isinstance(page_snapshot, dict) else {}
    elements = [item for item in snapshot.get("elements") or [] if isinstance(item, dict)]
    ref = str(action.get("ref") or "").strip()
    if ref:
        exact = [item for item in elements if str(item.get("ref") or "") == ref]
        if len(exact) == 1:
            return exact[0]

    wanted = _fold(action.get("label"))
    if not wanted:
        return {}
    matches = [
        item for item in elements
        if any(_fold(item.get(key)) == wanted for key in ("label", "aria_label", "placeholder", "text"))
    ]
    return matches[0] if len(matches) == 1 else {}


def _state_values(target: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("value", "selected_text", "text", "aria_value", "aria_valuetext"):
        value = target.get(key)
        if value not in (None, ""):
            values.append(_norm(value))
    return values


def _select_matches(expected: str, observed: list[str]) -> bool:
    wanted = _fold(expected)
    if not wanted:
        return False
    for item in observed:
        folded = _fold(item)
        if folded == wanted:
            return True
    return False


def build_action_receipt(
    action: dict[str, Any],
    result: dict[str, Any],
    *,
    page_snapshot: dict[str, Any] | None = None,
    select_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact, falsifiable receipt from independent post-action state."""
    kind = str(action.get("kind") or "").strip().lower()
    receipt: dict[str, Any] = {
        "action": kind,
        "target_ref": str(action.get("ref") or ""),
        "target_label": str(action.get("label") or "")[:160],
        "verified": None,
        "verification": "not_applicable",
    }

    # The projection itself is model-facing. Redact all password controls even
    # if this particular receipt is for another field in the same form.
    redact_password_snapshot(page_snapshot)

    if kind == "select" and action.get("list_options") is True and result.get("committed") is False:
        # A read, not a selection. Judged as a select it failed its own
        # postcondition (requested='') and a correct listing reached the
        # model as an error, which it then distrusted and worked around.
        receipt.update({
            "action": "list_options",
            "verification": "read_only",
            "option_count": len(result.get("observed_options") or []),
        })
        return receipt

    if kind == "scroll":
        evidence = result.get("scroll")
        if isinstance(evidence, dict):
            receipt.update(verified=evidence.get("state") in {"moved", "at_start", "at_end"},
                           verification="scroll_position", observed=evidence)
        else:
            receipt.update(verified=False, verification="scroll_position", failure_reason="scroll_evidence_missing")
        return receipt

    if kind not in _VERIFY_KINDS:
        if kind == "fill_form":
            fields = [item for item in result.get("fields") or [] if isinstance(item, dict)]
            requested_fields = [item for item in action.get("fields") or [] if isinstance(item, dict)]
            if not requested_fields:
                # Nothing was ever asked for, so nothing was dispatched. A
                # verification verdict would claim an attempt that never
                # happened and reads as a flaky engine; name the malformed
                # envelope instead so the caller can fix the call rather than
                # retry it unchanged.
                receipt.update({
                    "verified": False,
                    "verification": "not_attempted",
                    "field_count": 0,
                    "verified_fields": 0,
                    "failure_reason": "fill_form_missing_fields",
                    "error": (
                        "fill_form requires `fields`: [{ref, value|option|options|checked, ...}]. "
                        "`steps` belongs to kind:'sequence'. No field was dispatched; "
                        "resend this call with the fields key rather than retrying it unchanged."
                    ),
                })
                return receipt
            all_verified = bool(fields) and all(item.get("verified") is True for item in fields)
            # The core receipt is built before the public wrapper attaches its
            # convenience ``remaining`` count.  Prove completeness against the
            # immutable request instead of allowing an early-stopped batch to
            # look complete merely because that later field is not present yet.
            complete = (
                bool(requested_fields)
                and len(fields) == len(requested_fields)
                and not result.get("blocked")
                and int(result.get("remaining") or 0) == 0
            )
            contradicted = any(item.get("effect_state") == "contradicted" for item in fields)
            receipt.update({
                "verified": True if all_verified and complete and result.get("success") is True else False,
                "verification": "batch_control_readback",
                "field_count": len(fields),
                "verified_fields": sum(item.get("verified") is True for item in fields),
            })
            if contradicted:
                receipt["failure_reason"] = "state_mismatch"
            elif receipt["verified"] is False:
                receipt["failure_reason"] = "batch_incomplete_or_uncertain"
            return receipt
        if kind == "click" and isinstance(result.get("verification"), dict):
            evidence = dict(result["verification"])
            # Dispatch/transport success is not effect evidence. In particular,
            # an unchanged page must not become verified through a default True.
            verified = evidence.get("ok") if isinstance(evidence.get("ok"), bool) else None
            if evidence.get("state") == "outcome_unknown":
                verified = None
            elif evidence.get("state") == "page_changed" and verified is None:
                verified = True
            receipt.update({
                "verified": verified,
                "verification": "click_effect",
                "evidence": evidence,
            })
        return receipt

    if kind == "upload":
        upload = result.get("upload") if isinstance(result.get("upload"), dict) else {}
        expected = [str(v) for v in upload.get("expected_filenames") or []]
        observed = [str(v) for v in upload.get("observed_input_filenames") or []]
        visible = [str(v) for v in upload.get("visible_filename_matches") or []]
        # The names the exact input held at its own change event: a site that
        # reads the files and clears the input leaves nothing to read back.
        witnessed = [str(v) for v in upload.get("witnessed_input_filenames") or []]
        verified = bool(upload.get("verified")) and bool(expected) and all(
            value in observed or value in visible or value in witnessed for value in expected
        )
        receipt.update({
            "verified": verified,
            "verification": "input_change_event" if witnessed and not observed else "file_list_readback",
            "requested": expected,
            "observed": observed or visible or witnessed,
            "file_selection_verified": upload.get("file_selection_verified") is True,
            "attachment_accepted": upload.get("attachment_accepted"),
            "submission_observed": upload.get("submission_observed") is True,
        })
        return receipt

    if kind in {"fill", "check"}:
        private = consume_fill_evidence()
        if private is not None:
            receipt.update({
                "verified": private.get("verified") is True,
                "verification": str(private.get("verification") or "live_control_value"),
                "requested": private.get("requested"),
                "observed": private.get("observed"),
            })
            # A check is proved by a checked-state read, or — when the option
            # closed with its menu — by the chosen item that now names it.
            if kind == "check" and private.get("verification") not in {
                "live_checked_state", "chosen_item_readback",
            }:
                receipt["verified"] = False
                receipt["failure_reason"] = "control_not_checkable"
            for key in (
                "requested_length", "observed_length", "sensitive",
                "values_truncated", "selector_strategy", "failure_reason", "settle_ms",
            ):
                if key in private:
                    receipt[key] = private[key]
            return receipt

    target = _target_from_page_snapshot(page_snapshot, action)
    observed = _state_values(target)

    if kind == "fill":
        expected = str(action.get("value") if action.get("value") is not None else action.get("text") or "")
        actual = str(target.get("value") or "") if target else ""
        tag = _fold(target.get("tag"))
        verified = bool(target) and (
            _textarea_norm(actual) == _textarea_norm(expected) if tag == "textarea" else actual == expected
        )
        sensitive = is_password_element(target)
        if sensitive:
            verified = False  # Redacted page text can never prove a password.
        receipt.update({
            "verified": verified,
            "verification": "control_value_readback",
            "requested": "[masked]" if sensitive else expected,
            "observed": "[masked]" if sensitive else actual,
        })
        if sensitive:
            receipt["sensitive"] = True
        return receipt

    if kind == "check":
        expected = action.get("value")
        if expected is None:
            expected = action.get("checked", True)
        expected_bool = _boolish(expected)
        if expected_bool is None:
            expected_bool = bool(expected)

        actual_bool = None
        if target:
            aria_value = target.get("aria_checked")
            if aria_value not in (None, ""):
                actual_bool = _boolish(aria_value)
            if actual_bool is None:
                actual_bool = _boolish(target.get("checked"))
        verified = bool(target) and actual_bool is expected_bool
        receipt.update({
            "verified": verified,
            "verification": "checked_state_readback",
            "requested": expected_bool,
            "observed": actual_bool,
        })
        return receipt

    if kind == "select":
        private_select = consume_select_evidence()
        if select_evidence is None:
            select_evidence = private_select
        requested_options = action.get("options")
        expected: str | list[str]
        if isinstance(requested_options, list):
            expected = [
                str(value).strip() for value in requested_options
                if str(value or "").strip()
            ]
        else:
            expected = str(
                action.get("option")
                if action.get("option") not in (None, "")
                else action.get("value") or ""
            )
        if select_evidence and select_evidence.get("verified"):
            evidence_values = [str(v) for v in select_evidence.get("observed") or []]
            observed = list(dict.fromkeys(observed + evidence_values))
        # The caller may have named part of the label ("Canada" of "Canada +1").
        # The select layer resolved it against the live popup, so the committed
        # label is what this readback is about; the request is kept beside it.
        committed = str((select_evidence or {}).get("value") or "")
        requested_label = expected
        if committed and not isinstance(expected, list):
            expected = committed
        matches = (
            bool(expected) and all(_select_matches(value, observed) for value in expected)
            if isinstance(expected, list)
            else _select_matches(expected, observed)
        )
        # A composite control displays what it stores, not the label that was
        # clicked: a country field with its own dial-code input reads back
        # "+1". The select layer saw the popup and the click, so its verdict —
        # and the weaker evidence class it names — stands here too.
        part_display = "control_displays_option_part" in {
            str((select_evidence or {}).get("readback") or ""),
            str((select_evidence or {}).get("strategy") or ""),
        }
        verified = bool(select_evidence and select_evidence.get("verified")) and (
            matches or part_display
        )
        if not select_evidence:
            # Backward compatibility for a select path that did not yet emit a
            # private receipt: a fresh page snapshot may still prove the state.
            verified = matches
        receipt.update({
            "verified": verified,
            "verification": (
                "control_displays_option_part" if verified and part_display and not matches
                else "selected_value_readback"
            ),
            "requested": expected,
            "observed": observed,
        })
        if committed and requested_label and _fold(committed) != _fold(str(requested_label)):
            receipt["requested_option"] = requested_label
        if select_evidence and select_evidence.get("selector_strategy"):
            receipt["selector_strategy"] = select_evidence["selector_strategy"]
        if select_evidence and select_evidence.get("recovered_from"):
            receipt["recovered_from"] = select_evidence["recovered_from"]
        return receipt

    return receipt


def enforce_receipt(result: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    """Attach a receipt and fail closed for mutations whose state was not proven."""
    output = dict(result)
    output["receipt"] = receipt
    kind = str(receipt.get("action") or "")
    if receipt.get("failure_reason") == "fill_form_missing_fields":
        # A malformed envelope dispatched nothing. fill_form stays outside
        # _VERIFY_KINDS because a partial batch is a legitimate outcome, but a
        # batch that never ran is not partial — reporting success for it is how
        # this reached a live session five times without a single tool error.
        output["success"] = False
        output["verification_failed"] = True
        if not output.get("error"):
            output["error"] = receipt.get("error") or "fill_form dispatched no fields"
        return output
    evidence = receipt.get("evidence") if isinstance(receipt.get("evidence"), dict) else {}
    if kind == "click" and evidence.get("state") in {"postcondition_unmet", "postcondition_unreadable"}:
        # The caller made this click conditional on a visible outcome. Any
        # other effect is not that outcome, and an outcome that could not be
        # read was not observed either, so neither can read as success.
        output["success"] = False
        output["verification_failed"] = True
        if not output.get("error"):
            output["error"] = str(evidence.get("detail") or "click postcondition was not observed")
        return output
    if kind in _VERIFY_KINDS and receipt.get("verified") is not True:
        output["success"] = False
        output["verification_failed"] = True
        if not output.get("error"):
            requested = receipt.get("requested")
            observed = receipt.get("observed")
            output["error"] = (
                f"{kind} executed but its postcondition was not verified; "
                f"requested={requested!r}, observed={observed!r}"
            )
    return output
