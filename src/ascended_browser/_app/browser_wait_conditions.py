"""What an action waits for before it reports, said once as ``until``.

A developer edits a file, the dev server hot-reloads, and the page is either
fine, still rendering, or broken. The agent had three ways to find out, all
bad: observe at once and read a half-drawn page, sleep a fixed time, or poll
observe in a loop of model round trips. ``until`` puts the condition on the
action itself (navigate, click, press) or on a bare ``wait`` kind, and the
browser polls the page until it holds or the budget runs out, the way
Playwright's assertions and agent-browser's ``wait --text/--url/@ref`` do.

Conditions hold together (all of them must be true at once):

- ``text``, ``text_gone``, ``url_contains``: the existing postcondition reader.
- ``ref_visible``, ``ref_gone``: an observed element appears or leaves; a
  React remount counts as gone, and the result says ``replaced_by`` when a
  same-named element now exists so the model is not misled.
- ``network_idle``: no tracked request in flight for half a second.
- ``no_new_errors``: nothing new in the tab's diagnostics since the action
  began; one error ends the wait at once, naming it.
- ``wall_cleared``: the page is no longer a bot-wall challenge.
- ``timeout_ms``: budget, capped.

This module owns the vocabulary and the messages; the manager owns the loop.
"""
from __future__ import annotations

from typing import Any

CONDITION_KEYS = (
    "text", "text_gone", "url_contains", "ref_visible", "ref_gone",
    "network_idle", "no_new_errors", "wall_cleared",
)
DEFAULT_TIMEOUT_MS = 5_000
WAIT_DEFAULT_TIMEOUT_MS = 10_000
MAX_TIMEOUT_MS = 15_000
#: Kinds that accept ``until`` beside their own effect.
UNTIL_KINDS = frozenset({"navigate", "click", "press", "fill", "select", "check", "scroll", "wait"})


def normalize_until(until: Any, *, kind: str) -> dict[str, Any] | None:
    """A clean condition set, or None when the action carries none.

    Raises ValueError with the fix when the shape cannot be waited on.
    """
    if until in (None, "", {}, []):
        if kind == "wait":
            raise ValueError(
                "kind=wait requires until: an object with at least one of "
                + ", ".join(CONDITION_KEYS) + " (and an optional timeout_ms)."
            )
        return None
    if isinstance(until, str):
        # "until": "Saved" is the common shorthand for text.
        until = {"text": until}
    if not isinstance(until, dict):
        raise ValueError("until must be an object, e.g. {\"text\": \"Saved\"} or {\"network_idle\": true}.")
    unknown = sorted(key for key in until if key not in CONDITION_KEYS and key != "timeout_ms")
    if unknown:
        raise ValueError(
            f"until has no condition called {', '.join(unknown)}; use "
            + ", ".join(CONDITION_KEYS) + " or timeout_ms."
        )
    out: dict[str, Any] = {}
    for key in ("text", "text_gone", "url_contains", "ref_visible", "ref_gone"):
        value = until.get(key)
        if value in (None, "", False):
            continue
        value = str(value).strip()
        if not value:
            continue
        if key.startswith("ref_") and not value.startswith("e"):
            raise ValueError(f"until.{key} must be a ref (e…) from the current observation, not {value!r}.")
        out[key] = value[:500]
    for key in ("network_idle", "no_new_errors", "wall_cleared"):
        if until.get(key):
            out[key] = True
    if not out:
        raise ValueError(
            "until names no condition; set at least one of " + ", ".join(CONDITION_KEYS) + "."
        )
    timeout = until.get("timeout_ms")
    default = WAIT_DEFAULT_TIMEOUT_MS if kind == "wait" else DEFAULT_TIMEOUT_MS
    try:
        timeout = int(timeout) if timeout not in (None, "") else default
    except (TypeError, ValueError):
        raise ValueError("until.timeout_ms must be an integer number of milliseconds.") from None
    out["timeout_ms"] = max(250, min(MAX_TIMEOUT_MS, timeout))
    return out


def describe_condition(key: str, value: Any) -> str:
    if key == "text":
        return f"text {str(value)[:80]!r} visible"
    if key == "text_gone":
        return f"text {str(value)[:80]!r} gone"
    if key == "url_contains":
        return f"URL containing {str(value)[:80]!r}"
    if key == "ref_visible":
        return f"ref {value} visible"
    if key == "ref_gone":
        return f"ref {value} gone"
    if key == "network_idle":
        return "network idle"
    if key == "no_new_errors":
        return "no new page errors"
    if key == "wall_cleared":
        return "bot wall cleared"
    return key


def describe_conditions(conditions: dict[str, Any]) -> list[str]:
    return [describe_condition(key, value) for key, value in conditions.items() if key != "timeout_ms"]


def unmet_detail(kind: str, unmet: list[str], waited_ms: int, *, observed: str = "", state: str = "") -> str:
    """One sentence the model can act on."""
    what = "; ".join(unmet) or "the requested condition"
    head = f"Waited {waited_ms} ms" if kind == "wait" else f"{kind.capitalize()} dispatched and the browser then waited {waited_ms} ms"
    if state == "unreadable":
        return (
            f"{head}, but the page could not be read to check {what} (mid-navigation or busy). "
            "Observe the tab to see where it is now."
        )
    if state == "deferred":
        return f"{head}; the check yielded to a pending browser control or viewport transition. Inspect the page after it."
    if state == "errored":
        return f"{head} and stopped early: {observed}. Read the page's error before retrying."
    tail = f" {observed}" if observed else ""
    return f"{head}; {what} was not observed.{tail} Observe the tab before deciding the step failed."
