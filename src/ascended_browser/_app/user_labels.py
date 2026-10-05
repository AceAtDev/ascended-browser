"""Single owner of human-readable labels for internal browser vocabulary.

The browser subsystem addresses pages and elements with durable identifiers
(tab ids, observation ids, element refs) and reports lifecycle values as
internal enums. The model may echo those verbatim into user-facing prose, so
both the model-facing observation and every user-visible projection should
source their human phrasing from here rather than re-deriving it per call
site. One owner, one vocabulary (DESIGN_PHILOSOPHY "one capability has one
owner and one contract").

Exact values only: label lookups match known enum strings; there is no
pattern scanning anywhere in this module.
"""

from urllib.parse import urlparse
from typing import Optional

# Internal lifecycle / status vocabulary -> plain statements a user can skim.
# Keys are exact enum string values; a key added here must be mirrored by the
# frontend's output moments map (tests pin the sync of the render-relevant set).
SERVICE_VOCABULARY: dict = {
    "restorable_get": "safe to reopen",
    "non_restorable": "cannot be reopened",
    "manual": "closed by you",
    "dirty": "changed since it was last checked",
    "uncertain": "state could not be fully confirmed",
}

# Auth/classifier states from browser_reliability.classify_auth_state, rendered
# as the plain fact a user would skim for.
AUTH_STATE_WORDS: dict = {
    "authenticated": "signed in",
    "login_link_visible": "not signed in — a sign-in link is on the page",
    "login_form_visible": "showing a login form",
    "auto_verifying": "browser verification in progress",
    "user_required": "needs a human check",
    "passive": "no human check needed",
    "none": "no sign-in needed",
    "unknown": "sign-in state unconfirmed",
}

_BOOL_WORDS = {
    True: "yes",
    False: "no",
    None: "unknown",
}


def host_of(url: object) -> str:
    """Lowercased hostname of a URL string, without any pattern scanning."""
    try:
        parsed = urlparse(str(url or ""))
        return (parsed.hostname or "").lower()
    except (TypeError, ValueError):
        return ""


def page_label(title: object, url: object) -> str:
    """A short human name for a browser page: page title or site host."""
    clean_title = " ".join(str(title or "").split())
    host = host_of(url)
    if clean_title:
        bare = clean_title.replace(f"{host} - ", "").replace(f" - {host}", "")
        return bare or clean_title
    if host:
        return host
    return "an unnamed page"


def describe_tab(tab: object, *, fallback: str = "the browser tab") -> str:
    """Reference a tab the way a user would: by site, never by internal id."""
    if not isinstance(tab, dict) or not (tab.get("url") or tab.get("title")):
        return fallback
    label = page_label(tab.get("title"), tab.get("url"))
    return f"the {label} tab"


def element_label(element: object, *, page: object = None) -> Optional[str]:
    """Human phrase for one observed element; None when no label is known."""
    if not isinstance(element, dict):
        return None
    # Observed elements carry their visible wording as `text` (or `label`);
    # `name` is the accessible-name key and is often absent. Reading only
    # `name` collapsed every control to its role, so each one rendered as the
    # identical phrase and the user-visible text named nothing at all.
    name = " ".join(
        str(element.get("name") or element.get("label") or element.get("text") or "").split()
    )
    role = " ".join(str(element.get("role") or "").split())
    text = name or role
    if not text:
        return None
    return f"the \"{text}\" field on the page"


def word_for(value: object) -> str:
    """Plain-language rendering for a scalar status value if one is known."""
    if isinstance(value, bool):
        return _BOOL_WORDS[value]
    text = str(value or "").strip()
    if not text:
        return ""
    known = SERVICE_VOCABULARY.get(text)
    if known is not None:
        return known
    return text


def humanize_enum_text(value: object) -> str:
    """Replace exact enum tokens with their plain word (ids untouched)."""
    known = SERVICE_VOCABULARY.get(str(value or "").strip())
    return known if known is not None else str(value or "")
