"""Find the values in a recorded browser flow that change from run to run.

Ported from browser-use's ``browser_use/agent/variable_detector.py`` (MIT,
commit d8110c5). The detection rules are upstream's, in upstream's priority
order: the element's own attributes first (type, then id/name/placeholder/
aria-label keywords), the value's shape as the fallback. Ascended adds the
field's visible label to the keyword text, since its observations carry one,
returns plain dicts rather than a pydantic model, and matches short keywords
as whole words (see ``detect_from_attributes``).
"""
from __future__ import annotations

import re
from typing import Any


def detect_variable_type(
    value: str, attributes: dict[str, Any] | None = None,
) -> tuple[str, str | None] | None:
    """(variable name, format) for a value, element context first; else None."""
    if attributes:
        found = detect_from_attributes(attributes)
        if found:
            return found
    return detect_from_value_pattern(value)


def detect_from_attributes(attributes: dict[str, Any]) -> tuple[str, str | None] | None:
    input_type = str(attributes.get("type") or "").lower()
    if input_type == "email":
        return ("email", "email")
    if input_type == "tel":
        return ("phone", "phone")
    if input_type == "date":
        return ("date", "date")
    if input_type == "number":
        return ("number", "number")
    if input_type == "url":
        return ("url", "url")

    combined_text = " ".join(
        str(attributes.get(key) or "")
        for key in ("id", "name", "placeholder", "aria-label", "label")
    )
    # Upstream matches keywords as substrings, so "Cover Letter" held "tel"
    # and became a phone number, and "Statement" a state. Short keywords
    # match whole words (camelCase and snake_case split); long ones, which
    # do not occur inside unrelated words, still match anywhere.
    words = set(re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", combined_text).lower()))
    combined_text = combined_text.lower()

    def has(*keywords: str) -> bool:
        return any(keyword in words if len(keyword) < 6 else keyword in combined_text for keyword in keywords)

    if has("address", "street", "addr"):
        if has("billing"):
            return ("billing_address", None)
        if has("shipping"):
            return ("shipping_address", None)
        return ("address", None)
    if has("comment", "note", "message", "description"):
        return ("comment", None)
    if has("email") or "e-mail" in combined_text:
        return ("email", "email")
    if has("phone", "tel", "mobile", "cell"):
        return ("phone", "phone")
    if has("first") and has("name"):
        return ("first_name", None)
    if has("last") and has("name"):
        return ("last_name", None)
    if has("full") and has("name"):
        return ("full_name", None)
    if has("name"):
        return ("name", None)
    if has("date", "dob", "birth"):
        return ("date", "date")
    if has("city"):
        return ("city", None)
    if has("state", "province"):
        return ("state", None)
    if has("country"):
        return ("country", None)
    if has("zip", "postal", "postcode"):
        return ("zip_code", "postal_code")
    if has("company", "organization"):
        return ("company", None)
    return None


def detect_from_value_pattern(value: str) -> tuple[str, str | None] | None:
    if "@" in value and "." in value and re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", value):
        return ("email", "email")
    if re.match(r"^[\d\s\-\(\)\+]+$", value):
        if len(re.sub(r"[\s\-\(\)\+]", "", value)) >= 10:
            return ("phone", "phone")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return ("date", "date")
    if value and value[0].isupper() and value.replace(" ", "").replace("-", "").isalpha() and 2 <= len(value) <= 30:
        words = value.split()
        if len(words) == 1:
            return ("first_name", None)
        if len(words) == 2:
            return ("full_name", None)
        return ("name", None)
    if value.isdigit() and 1 <= len(value) <= 9:
        return ("number", "number")
    return None


def ensure_unique_name(base_name: str, existing: dict[str, Any]) -> str:
    """first_name -> first_name_2 -> first_name_3 when names collide."""
    if base_name not in existing:
        return base_name
    counter = 2
    while f"{base_name}_{counter}" in existing:
        counter += 1
    return f"{base_name}_{counter}"


def target_attributes(target: dict[str, Any]) -> dict[str, str]:
    """The upstream attribute view of a recorded Odysseus target."""
    return {
        "type": str(target.get("type") or ""),
        "id": str(target.get("element_id") or target.get("id") or ""),
        "name": str(target.get("name_attr") or ""),
        "placeholder": str(target.get("placeholder") or ""),
        "aria-label": str(target.get("aria_label") or target.get("name") or ""),
        "label": str(target.get("label") or ""),
    }
