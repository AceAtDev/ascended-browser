"""Advisory browser-action classification kept for compatibility and telemetry.

Browser tools deliberately do not gate actions from these text heuristics. The
web is too contextual for labels and reasoning prose to reliably decide user
authority; the bundled browser-use skill supplies that judgment before action.
"""

from __future__ import annotations

import re
from typing import Any

from ascended_browser.runtime.permission import ActionClass

# Phrasing mirrors the browser's own submit-intent judgement, kept together here
# so a change lands on every tool at once rather than one handler at a time.
_DANGEROUS_RE = re.compile(
    r"\b(pay|payment|purchase|buy now|place order|checkout|"
    r"accept contract|sign contract|agree to terms|delete account)\b",
    re.I,
)
_FINAL_SUBMIT_RE = re.compile(
    r"\b(submit application|send application|submit form|submit|finalize|"
    r"finish(?: application)?|complete application|confirm and send|post)\b",
    re.I,
)
# Visible "Continue"/"Next" semantics win over a model describing the underlying
# HTML operation as a submit. Without this every wizard step reads as final.
_SAFE_PROGRESS_RE = re.compile(
    r"\b(search|find|filter|load more|next|continue|review|save|"
    r"apply now|start application|sign in|log in)\b",
    re.I,
)

#: Human-readable rationale per class, used in the refusal a tool returns.
_WHY = {
    ActionClass.CONSEQUENTIAL: (
        "This looks like a final submit or an upload. Sending data to a third "
        "party is not a decision you can make on your own."
    ),
    ActionClass.DANGEROUS: (
        "This looks like a payment, purchase, or contract acceptance. Only a "
        "human can authorize it."
    ),
}


def _target_of(action: dict[str, Any]) -> str:
    """The visible text of whatever is being acted on."""
    return str(
        action.get("_target_text") or action.get("label") or action.get("ref") or ""
    )


def classify_browser_action(action: dict[str, Any]) -> ActionClass:
    """Classify one browser action.

    payment / purchase / contract          -> DANGEROUS
    final form submit, upload, login_fill  -> CONSEQUENTIAL
    everything else                        -> SAFE

    `_target_text` is the visible label of what is being acted on and carries
    far more signal than the model's own description of its intent, so a visible
    "Continue" beats a reasoning string that says "submitting the form".
    """
    # Workspace tools use kind/type; the older MCP path uses action=<verb>.
    name = str(
        action.get("action") or action.get("kind") or action.get("type") or ""
    ).lower()
    target_text = _target_of(action)
    reasoning = str(action.get("reasoning") or "")
    combined = f"{target_text} {reasoning}"

    if _DANGEROUS_RE.search(combined):
        return ActionClass.DANGEROUS
    if name in {"upload", "file_upload", "set_input_files", "login_fill"}:
        return ActionClass.CONSEQUENTIAL
    if name in {"click", "press", "type", "act", "fill_form"}:
        if _SAFE_PROGRESS_RE.search(target_text) and not _FINAL_SUBMIT_RE.search(target_text):
            return ActionClass.SAFE
        if _FINAL_SUBMIT_RE.search(combined):
            return ActionClass.CONSEQUENTIAL
    return ActionClass.SAFE


#: Consent recorded this run, keyed (owner, session_id, domain). In-process on
#: purpose: "this session" should mean this session, and a restart is a
#: reasonable point to ask again.
_CONSENTED: set[tuple[str, str, str]] = set()


def consent_key(owner: str, session_id: str, domain: str) -> tuple[str, str, str]:
    return (str(owner or ""), str(session_id or ""), str(domain or "").lower())


def record_consent(owner: str, session_id: str, domain: str) -> None:
    """Remember that the user has approved consequential actions on this site."""
    _CONSENTED.add(consent_key(owner, session_id, domain))


def has_consent(owner: str, session_id: str, domain: str) -> bool:
    return consent_key(owner, session_id, domain) in _CONSENTED


def reset_consent() -> None:
    """Test hook; also what a session teardown would call."""
    _CONSENTED.clear()


def gate_browser_action(
    action: dict[str, Any],
    *,
    owner: str = "",
    session_id: str = "",
    domain: str = "",
) -> dict[str, Any] | None:
    """Compatibility no-op. Browser authority is evaluated from context."""
    return None


def _is_confirmed(action: dict[str, Any]) -> bool:
    value = action.get("user_confirmed", action.get("confirmed"))
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "1"}


def refusal_for(action: dict[str, Any]) -> dict[str, Any] | None:
    """Back-compat wrapper retained for callers during rolling upgrades."""
    return gate_browser_action(action)
