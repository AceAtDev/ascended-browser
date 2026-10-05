"""Small transaction runtime for multi-field browser forms.

The model still makes one ``fill_form`` call, but the browser executes fields as
verified semantic transactions.  Dependent controls can be proactively rebased
from the page returned by a preceding select, and a failed/unverified field
stops the remaining batch instead of compounding uncertainty.
"""
from __future__ import annotations

from typing import Any

from ascended_browser._app.browser_semantic_target import SemanticTarget, recover_action_from_snapshot


def _snapshot_elements(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return []
    return [item for item in snapshot.get("elements") or [] if isinstance(item, dict)]


def field_ref_is_current(field: dict[str, Any], snapshot: dict[str, Any] | None) -> bool:
    ref = str(field.get("ref") or "")
    return bool(ref) and any(str(item.get("ref") or "") == ref for item in _snapshot_elements(snapshot))


def rebase_pending_field(
    field: dict[str, Any],
    snapshot: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Use an already-returned page snapshot to refresh a replaced pending ref.

    No extra browser observation is performed here. If the old ref remains in
    the page it is reused. If not, semantic resolution must be uniquely
    confident or the original field is left untouched so the normal stale-ref
    path can fail/re-observe safely.
    """
    if not isinstance(snapshot, dict) or field_ref_is_current(field, snapshot):
        return dict(field), None
    target = SemanticTarget.from_action(field)
    if not target.signature():
        return dict(field), None
    recovered, diagnostic = recover_action_from_snapshot(field, snapshot, target=target)
    if not recovered:
        return dict(field), diagnostic
    recovered = dict(recovered)
    recovered["pre_rebased"] = True
    return recovered, diagnostic


def should_stop_form_batch(result: dict[str, Any]) -> tuple[bool, str]:
    if not bool(result.get("success")):
        return True, str(result.get("error") or "field action failed")
    if result.get("blocked"):
        return True, str(result.get("reason") or "browser blocker appeared")
    receipt = result.get("receipt") if isinstance(result.get("receipt"), dict) else None
    if receipt is not None and receipt.get("verified") is False:
        return True, "field postcondition was not verified"
    return False, ""


def field_summary(
    original: dict[str, Any],
    executed: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    ok = bool(result.get("success"))
    receipt = result.get("receipt") if isinstance(result.get("receipt"), dict) else {}
    summary: dict[str, Any] = {
        "ref": str(executed.get("ref") or original.get("ref") or ""),
        "original_ref": str(original.get("ref") or ""),
        "label": str(executed.get("label") or original.get("label") or "")[:120],
        "kind": str(executed.get("kind") or original.get("kind") or original.get("type") or "fill")[:24],
        "status": str(result.get("detail") or ("done" if ok else "failed"))[:240],
        "verified": receipt.get("verified") is True,
        "self_healed": bool(result.get("self_healed")),
        "pre_rebased": bool(executed.get("pre_rebased")),
    }
    for key in ("operation_id", "effect_state", "dispatch_status", "retry_safe", "failed_stage"):
        if key in receipt:
            summary[key] = receipt[key]
    if result.get("semantic_recovery"):
        summary["semantic_recovery"] = result["semantic_recovery"]
    if not ok:
        summary["error"] = str(result.get("error") or "field action failed")[:240]
    return summary
