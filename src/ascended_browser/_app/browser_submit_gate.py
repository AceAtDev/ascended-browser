"""Recognise a ref whose control has turned into a final submission.

A ref names an element as it was observed. On Workday one button read "Save
and Continue" and later "Submit", and the model pressed Enter on that ref
three more times while stepping the wizard (2026-10-04). When a ref's live
visible name now reads as a final submission and did not when observed, the
action is refused before dispatch so the model looks again. It is a refusal,
never a pause: whether to submit at all is left to the classifier and to the
agent asking the user. The page's own name decides, not the model's prose.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ascended_browser._app.browser_safety import _DANGEROUS_RE, _FINAL_SUBMIT_RE, _SAFE_PROGRESS_RE

#: Keys that activate the focused control or submit its form.
_ACTIVATING_KEYS = frozenset({"enter", "return", "numpadenter", "space", " "})

#: The control a click or key press would activate, its visible name, and,
#: for a key pressed inside a form field, the form's own submit control.
TARGET_SCRIPT = """el => {
  const fold = s => String(s || '').replace(/\\s+/g, ' ').trim().slice(0, 160);
  const control = el.closest('button, a[href], input[type=submit], input[type=button], input[type=image], [role=button], [role=link], [role=menuitem]') || el;
  const name = c => fold(c.getAttribute('aria-label') || c.innerText || c.value || c.getAttribute('title') || c.getAttribute('alt'));
  const tag = control.tagName.toLowerCase();
  const form = el.form || el.closest('form');
  let formSubmit = '';
  if (form && ['input', 'textarea', 'select'].includes(el.tagName.toLowerCase())) {
    const submitter = form.querySelector('button[type=submit], button:not([type]), input[type=submit], input[type=image]');
    formSubmit = submitter ? name(submitter) : '';
  }
  return {name: name(control), tag, form_submit: formSubmit};
}"""


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def is_final_submit_name(name: str) -> bool:
    """A visible name that reads as a final submission or a payment."""
    text = _fold(name)
    if not text:
        return False
    if _DANGEROUS_RE.search(text):
        return True
    return bool(_FINAL_SUBMIT_RE.search(text)) and not _SAFE_PROGRESS_RE.search(text)


def activating_key(key: Any) -> bool:
    last = str(key or "").strip().lower().split("+")[-1]
    return last in _ACTIVATING_KEYS


def turned_submit(observed: dict[str, Any] | None, live_name: str) -> Optional[str]:
    """The name a ref was observed with, when the live control now reads as a submit and did not then."""
    if not observed:
        return None
    before = _fold(observed.get("text") or observed.get("label") or observed.get("aria_label") or "")
    if not before or not is_final_submit_name(live_name) or is_final_submit_name(before):
        return None
    return before
