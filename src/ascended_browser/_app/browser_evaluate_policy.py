"""Shared safety checks for read-only browser evaluation."""

from __future__ import annotations

import re


_UNSAFE_EVALUATE_PATTERNS = (
    (r"\b(?:form\.)?submit\s*\(", "form submission"),
    (r"\brequestSubmit\s*\(", "form submission"),
    (r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\b", "network access"),
    (r"\b(?:window\.)?open\s*\(", "opening windows"),
    (
        r"\b(?:history\.(?:back|forward|go|pushState|replaceState)|"
        r"(?:window\.|document\.)location\s*(?:=|\.(?:assign|replace|reload)\s*\())",
        "navigation",
    ),
    (r"\b(?:localStorage|sessionStorage|indexedDB|caches|document\.cookie)\b", "browser storage access"),
    (r"\b(?:dispatchEvent|click|focus|blur|scrollIntoView|scrollTo|scrollBy)\s*\(", "browser interaction"),
    # Treat every reference as unsafe, not just a direct ``window.resizeTo()``
    # call. JavaScript can invoke the same function through optional chaining,
    # bracket notation, ``call``/``apply``, or a local alias. Read-only
    # evaluation has no legitimate need to obtain these mutating capabilities.
    (r"\b(?:resizeTo|resizeBy)\b", "window resizing"),
    (r"\b(?:appendChild|removeChild|replaceChild|insertAdjacent(?:HTML|Element|Text)|setAttribute|removeAttribute)\s*\(", "DOM mutation"),
    (r"\.(?:innerHTML|outerHTML|textContent|innerText|value|checked|selectedIndex)\s*=", "DOM mutation"),
    (r"\.(?:remove|before|after|prepend|append|replaceWith)\s*\(", "DOM mutation"),
    (r"\.(?:classList\.(?:add|remove|toggle)|style\.)", "DOM mutation"),
)


def validate_read_only_evaluate(function: str) -> None:
    """Reject known browser-mutating or side-effecting evaluation code.

    This is intentionally conservative rather than a JavaScript sandbox. The
    evaluator remains useful for DOM reads and JSON shaping while keeping it
    out of navigation, input, persistence, network, and submission paths.
    """
    source = str(function or "").strip()
    if not source:
        raise ValueError("evaluate requires a JavaScript function")
    if len(source) > 12000:
        raise ValueError("evaluate function exceeds the 12,000 character limit")
    if not re.match(r"^(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", source, re.S):
        raise ValueError("evaluate must be a JavaScript arrow function")
    for pattern, reason in _UNSAFE_EVALUATE_PATTERNS:
        if re.search(pattern, source, re.I):
            raise ValueError(f"read-only evaluate blocks {reason}")
