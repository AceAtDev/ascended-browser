"""Sensitive browser-state helpers.

The browser runtime may need to inspect a secret locally to prove an action
worked, but model-facing snapshots and receipts must never echo that secret.
Keep that distinction explicit and shared by observation/receipt code.
"""
from __future__ import annotations

from typing import Any


def is_password_element(element: dict[str, Any] | None) -> bool:
    """Whether a control's value must never be shown: passwords, codes, card data."""
    if not isinstance(element, dict):
        return False
    if element.get("sensitive") is True:
        return True
    if str(element.get("type") or "").strip().casefold() in {"password", "hidden"}:
        return True
    # The same tokens the live-view capture masks (_SENSITIVE_AUTOCOMPLETE):
    # one-time codes and card data are secrets even outside type=password.
    tokens = str(element.get("autocomplete") or "").casefold().split()
    return any(token in _SENSITIVE_AUTOCOMPLETE for token in tokens)


def names_a_secret(*texts: Any) -> bool:
    """Whether a control's name, id, label or placeholder says it holds a secret.

    The same broad words the capture masks. Over-matching only leaves a field
    unrecorded; under-matching stores a secret.
    """
    for text in texts:
        folded = str(text or "").casefold()
        if not folded:
            continue
        if any(word in folded for word in _SENSITIVE_WORDS) or any(
            label in folded for label in _SENSITIVE_LABELS
        ) or "social security" in folded or "one-time" in folded:
            return True
    return False


def redact_password_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Redact password values/text in-place and return *snapshot*.

    Label/placeholder/context remain available so agents can still identify the
    control. ``text`` is cleared because raw DOM collection can derive it from
    ``el.value`` before the ordinary ``value`` field is separately redacted.
    """
    if not isinstance(snapshot, dict):
        return snapshot
    for element in snapshot.get("elements") or []:
        if not is_password_element(element):
            continue
        element["value"] = ""
        element["text"] = ""
        element.pop("selected_text", None)
    return snapshot


# Page regions a browser picture must never show. A picture has no field-level
# redaction, so the capture masks these before the pixels leave the browser;
# cleaning the image afterwards cannot reliably find them. Over-masking costs a
# less informative picture; under-masking publishes a secret, so matching is
# deliberately broad:
# - a "show password" toggle turns a password input into type=text, usually
#   with no autocomplete token, so names, ids and labels are matched too;
# - card fields are commonly cross-origin payment iframes, covered whole.
_SENSITIVE_WORDS = (
    "password", "passwd", "pwd", "passcode", "cardnumber", "card_number",
    "card-number", "cvv", "cvc", "csc", "securitycode", "security_code",
    "security-code", "ssn",
)
_SENSITIVE_LABELS = ("password", "passcode", "card number", "security code", "cvv", "cvc")
#: For controls named by a separate <label> or aria-labelledby, which no
#: attribute selector can see. Used with Playwright's label matching.
SENSITIVE_LABEL_PATTERN = "|".join(_SENSITIVE_LABELS)
_SENSITIVE_AUTOCOMPLETE = (
    "current-password", "new-password", "one-time-code", "cc-number", "cc-csc",
    "cc-exp", "cc-exp-month", "cc-exp-year",
)
SENSITIVE_CAPTURE_SELECTOR = ", ".join((
    'input[type="password" i]',
    *(f'input[autocomplete~="{token}" i]' for token in _SENSITIVE_AUTOCOMPLETE),
    *(f'input[{attr}*="{word}" i]' for word in _SENSITIVE_WORDS for attr in ("name", "id")),
    *(f'input[{attr}*="{label}" i]' for label in _SENSITIVE_LABELS
      for attr in ("aria-label", "placeholder")),
    'iframe[src*="js.stripe.com" i]',
    'iframe[title*="card" i]',
    'iframe[title*="payment" i]',
))
