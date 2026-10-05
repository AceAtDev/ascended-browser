"""Shared browser reliability helpers for Browser Work and live browser tools."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def objective_requirements(task_description: str) -> dict[str, Any]:
    """Classify the user's browser objective without tying it to one agent loop."""
    text = _text(task_description).lower()
    upload_info = bool(re.search(
        r"\b(?:documented|documentation|docs?|method|function|api|signature|example)\b.{0,100}"
        r"\b(?:upload|attach)(?:ing|ed)?\b"
        r"|\b(?:upload|attach)(?:ing|ed)?\b.{0,100}"
        r"\b(?:documented|documentation|docs?|method|function|api|signature|example)\b",
        text,
    ))
    upload = bool(re.search(
        r"\b(?:upload|attach)\s+(?:the\s+|a\s+|an\s+|this\s+|my\s+|supplied\s+|provided\s+|local\s+)?"
        r"(?:file|document|resume|image|photo|attachment|pdf|csv)\b"
        r"|\b(?:file control|file input|input\[type=file\])\b",
        text,
    ))
    if upload_info and not re.search(r"\b(?:upload|attach)\s+(?:the|my|supplied|provided|local)\b", text):
        upload = False

    form_control = r"(?:form|field|input|textarea|dropdown|select|option|checkbox|radio|control|application|essay|answer|information)"
    form_verb = r"(?:fill|enter|type|provide|complete|choose|select|check|uncheck|set|toggle)"
    website_search = bool(re.search(
        r"\b(?:use|using)\b.{0,60}\b(?:internal|website|site)(?:'s)?\s+(?:own\s+)?search\b"
        r"|\bsearch\s+(?:this|the)\s+(?:site|website)(?:'s)?\b"
        r"|\bfilter\s+(?:to|by|the|products?)\b"
        r"|\bapply\b.{0,50}\bfilter\b",
        text,
    ))
    form_interaction = bool(
        re.search(rf"\b{form_verb}\b.{{0,100}}\b{form_control}\b", text)
        or re.search(rf"\b{form_control}\b.{{0,100}}\b{form_verb}\b", text)
        or re.search(r"\bfill\b.{0,120}\b(?:with|using)\b", text)
        or upload
        or website_search
    )

    affirmative = re.sub(
        r"\b(?:do not|don't|never)\s+(?:click\s+|press\s+)?(?:submit|send|create|place|apply)\b[^.;!?]*"
        r"|\bstop\s+before\b[^.;!?]*"
        r"|\bwithout\s+(?:being\s+)?submitt(?:ed|ing)\b",
        " ",
        text,
    )
    submission_confirmation = bool(
        re.search(r"\b(?:submit|send|apply for|finish|complete)\b.{0,100}\b(?:application|form|entry|checkout|order|job)\b", affirmative)
        or re.search(r"\b(?:application|form|entry|checkout|order)\b.{0,100}\b(?:submit|send|finish|complete)\b", affirmative)
    )
    login = bool(re.search(r"\b(?:login|log in|sign in|authenticate|account)\b", text))
    read_only = bool(re.search(r"\b(?:extract|find|list|read|collect|scrape|return)\b", text)) and not form_interaction

    requested_output_fields = []
    for name, pattern in {
        "title": r"\btitle\b",
        "location": r"\blocation\b",
        "url": r"\burl\b|\blink\b",
        "summary": r"\bsummary\b",
        "skills": r"\bskills?\b",
        "remote_policy": r"\bremote\b|\bhybrid\b|\bonsite\b",
        "years_experience": r"\byears?\b.{0,20}\bexperience\b|\bexperience\b",
    }.items():
        if re.search(pattern, text):
            requested_output_fields.append(name)

    return {
        "form_interaction": form_interaction,
        "upload": upload,
        "website_search": website_search,
        "submission_confirmation": submission_confirmation,
        "login": login,
        "read_only": read_only,
        "requested_output_fields": requested_output_fields,
    }


def classify_challenge(
    *,
    url: str = "",
    title: str = "",
    text: str = "",
    visible_text: str | None = None,
    elements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw_text = str(text or "")
    normalized = _text(raw_text).lower()
    haystack = f"{url}\n{title}\n{normalized}".lower()
    visible_source = raw_text if visible_text is None else str(visible_text or "")
    visible = _text(visible_source).lower()

    passive_patterns = (
        "this site is protected by recaptcha",
        "this website is protected by recaptcha",
        "protected by recaptcha and the google privacy policy",
        "privacy policy and terms of service apply",
        "protected by hcaptcha",
    )
    if any(pattern in haystack for pattern in passive_patterns):
        active_terms = (
            "select all images",
            "complete the captcha",
            "captcha is required",
            "captcha verification failed",
            "please complete the security check",
            "please verify that you are human",
        )
        if not any(term in haystack for term in active_terms):
            return {"state": "passive", "reason": "passive captcha disclosure text", "evidence": ""}

    auto_terms = (
        "checking your browser",
        "checking if the site connection is secure",
        "just a moment",
        "please wait while we verify",
        "verifying you are human",
        "cf-browser-verification",
        "challenges.cloudflare.com",
    )
    if ("cloudflare" in haystack and any(term in haystack for term in auto_terms)) or (
        "just a moment" in haystack and "checking your browser" in haystack
    ):
        return {"state": "auto_verifying", "reason": "browser verification appears to be in progress", "evidence": "cloudflare"}

    element_text = " ".join(
        " ".join(str((el or {}).get(key) or "") for key in (
            "text", "legacy_ref", "aria_label", "placeholder", "title_attr",
            "id", "name", "selector", "frame_url", "frame_name",
        )).lower()
        for el in (elements or [])
        if isinstance(el, dict) and el.get("visible", True) and el.get("in_viewport", True)
    )
    visible_haystack = f"{title}\n{visible}\n{element_text}".lower()
    user_terms = (
        "verify you are human",
        "verify that you are human",
        "i am human",
        "i'm not a robot",
        "select all images",
        "complete the captcha",
        "captcha is required",
        "captcha challenge",
        "enable javascript and cookies",
    )
    matched = next((term for term in user_terms if term in visible_haystack), "")
    widget = challenge_widget_evidence(elements)
    if matched or widget:
        return {
            "state": "user_required",
            "reason": "visible human-verification control detected",
            "evidence": matched or widget,
        }
    if captcha_plumbing_present(elements):
        return {"state": "passive", "reason": "captcha attribution or idle widget only", "evidence": ""}
    return {"state": "none", "reason": "", "evidence": ""}


_CAPTCHA_PROVIDERS = (
    ("hcaptcha", "hcaptcha"),
    ("recaptcha", "recaptcha"),
    ("turnstile", "turnstile"),
    ("challenges.cloudflare.com", "cloudflare"),
    # DataDome (Yelp, many retail and travel sites). Unlike reCAPTCHA it has no
    # passive anchor frame: its captcha frame is only served to a blocked visitor.
    ("captcha-delivery.com", "datadome"),
)

# hCaptcha/reCAPTCHA always inject an anchor (checkbox) frame plus attribution links.
# Only the challenge popup frame means a human actually has to solve something.
_ACTIVE_CHALLENGE_FRAME_MARKERS = (
    "bframe", "/challenge", "challenge?", "challenge/", "captcha-delivery.com/captcha",
)


def _element_captcha_provider(element: dict[str, Any]) -> str:
    blob = " ".join(str(element.get(key) or "") for key in (
        "text", "aria_label", "title_attr", "id", "name", "selector",
        "frame_url", "frame_name", "href",
    )).lower()
    return next((label for marker, label in _CAPTCHA_PROVIDERS if marker in blob), "")


def _visible_elements(elements: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        element
        for element in (elements or [])
        if isinstance(element, dict)
        and element.get("visible", True)
        and element.get("in_viewport", True)
    ]


def challenge_widget_evidence(elements: list[dict[str, Any]] | None) -> str:
    """Evidence only when a challenge the user must clear is actually rendered.

    Footer attribution links and the always-present anchor/checkbox frame are
    normal on captcha-protected pages and must not read as a blocker.
    """
    for element in _visible_elements(elements):
        provider = _element_captcha_provider(element)
        if not provider:
            continue
        frame_url = str(element.get("frame_url") or "").lower()
        if any(marker in frame_url for marker in _ACTIVE_CHALLENGE_FRAME_MARKERS):
            return provider
    return ""


def captcha_plumbing_present(elements: list[dict[str, Any]] | None) -> bool:
    """True when captcha attribution/anchor frames exist but nothing blocks progress."""
    return any(_element_captcha_provider(element) for element in _visible_elements(elements))


def classify_blocker(snapshot: dict[str, Any] | None = None, *, text: str = "") -> dict[str, Any] | None:
    """Return only page states that mechanically require a human handoff.

    A login form is not such a state: ``browser_login`` exists and the agent
    may have been explicitly asked to sign in. Login remains available through
    ``auth_state`` in browser results so the model can decide what to do. The
    durable workspace must not turn a normal login page into an unconditional
    stop before the agent gets that choice.
    """
    snapshot = snapshot or {}
    visible_text = text or str(snapshot.get("text") or snapshot.get("visible_text") or "")
    challenge = classify_challenge(
        url=str(snapshot.get("url") or ""),
        title=str(snapshot.get("title") or ""),
        text=visible_text,
        visible_text=visible_text,
        elements=list(snapshot.get("elements") or []),
    )
    if challenge.get("state") in {"auto_verifying", "user_required"}:
        return {
            "type": "challenge",
            "state": challenge.get("state", ""),
            "reason": challenge.get("reason", ""),
            "evidence": challenge.get("evidence", ""),
        }

    return None


_LOGIN_TEXT = re.compile(r"\b(?:log\s*in|login|sign\s*in|signin)\b", re.I)
_ACCOUNT_MUTATION = re.compile(
    r"\b(?:delete|remove|deactivate|close|cancel|terminate|erase|unsubscribe|create|open|switch|upgrade)\b", re.I,
)
_ACCOUNT_ROUTE_LABEL = re.compile(
    r"^(?:(?:my|your|member|customer|client|student|employee|patient|user|partner)\s+)?"
    r"(?:account|accounts|dashboard|profile|portal|member area|members area)$"
    r"|\b(?:my|your)\s+(?:account|dashboard|profile|portal)\b"
    r"|\b(?:member|customer|client|student|employee|patient|partner)\s+(?:portal|login|area)\b",
    re.I,
)
# Path segments only: "/Portal:Energy" (a wiki namespace) or "/accounting" are not
# account routes, "/MyAccount/..." and "/account?next=" are.
_ACCOUNT_ROUTE_HREF = re.compile(r"/(?:myaccount|account|dashboard|profile|member|members|portal)(?=[/?#.]|$)", re.I)


def classify_auth_state(snapshot: dict[str, Any] | None = None, *, text: str = "") -> dict[str, Any]:
    """Classify auth state without treating every login-related word as a blocker."""
    snapshot = snapshot or {}
    visible_text = _text(text or snapshot.get("text") or snapshot.get("visible_text") or "")
    url = str(snapshot.get("url") or "")
    title = str(snapshot.get("title") or "")
    elements = [el for el in (snapshot.get("elements") or []) if isinstance(el, dict)]

    has_password = any(
        (
            str(el.get("type") or "").lower() == "password"
            or (
                str(el.get("tag") or "").lower() in {"input", "textarea"}
                and re.search(r"\bpassword\b", " ".join(str(el.get(k) or "") for k in ("text", "label", "aria_label", "placeholder", "name", "id")), re.I)
            )
        )
        for el in elements
    )
    has_username = any(
        re.search(r"\b(?:username|email|login|account|identifier)\b", " ".join(str(el.get(k) or "") for k in ("text", "label", "aria_label", "placeholder", "name", "id")), re.I)
        and str(el.get("tag") or "").lower() in {"input", "textarea"}
        for el in elements
    )
    if has_password:
        return {
            "state": "login_form_visible",
            "reason": "visible login form fields are present",
            "evidence": "password field" + (" and username field" if has_username else ""),
        }

    authenticated_terms = (
        "my dashboard",
        "my profile",
        "update profile",
        "matching scholarships",
        "saved scholarships",
        "subscription preferences",
        "sign out",
        "log out",
        "logout",
    )
    if re.search(r"/(?:myaccount|account|dashboard|profile)/", url, re.I) or any(term in visible_text.lower() for term in authenticated_terms):
        return {"state": "authenticated", "reason": "authenticated account area is visible", "evidence": "account page"}

    # An account-looking route is not evidence of being signed out: signed-in
    # pages show "My account" too, and "Delete account" is an action, not a
    # route. Only visible sign-in text says the session is signed out; the
    # route is then offered as the way to reach the login page.
    login_link = any(
        _LOGIN_TEXT.search(" ".join(str(el.get(k) or "") for k in ("text", "label", "aria_label", "placeholder", "name", "id")))
        for el in elements
    ) or _LOGIN_TEXT.search(visible_text)
    if not login_link:
        return {"state": "unknown", "reason": "", "evidence": ""}
    protected_route = recommended_login_route(snapshot)
    if protected_route:
        return {
            "state": "login_link_visible",
            "reason": "sign-in text and a protected account route are visible",
            "evidence": protected_route.get("label") or protected_route.get("text") or protected_route.get("href") or "",
            "recommended_next_action": protected_route,
        }
    return {"state": "login_link_visible", "reason": "login link text is visible", "evidence": "login text"}


def recommended_login_route(snapshot: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Find protected account/profile routes that commonly redirect to login."""
    snapshot = snapshot or {}
    candidates: list[tuple[int, dict[str, Any]]] = []
    for el in snapshot.get("elements") or []:
        if not isinstance(el, dict):
            continue
        label = _text(" ".join(str(el.get(k) or "") for k in ("text", "label", "aria_label", "title_attr", "name", "id"))).lower()
        name = _text(el.get("text") or el.get("label") or el.get("aria_label") or el.get("title_attr") or "").lower()
        short = 0 < len(name.split()) <= 4
        href = str(el.get("href") or el.get("form_action") or "").lower()
        role = str(el.get("role") or "").lower()
        tag = str(el.get("tag") or "").lower()
        if role not in {"link", "button", "menuitem"} and tag not in {"a", "button"}:
            continue
        # Controls that change an account are never a route to sign in, and
        # clicking one on a hint's say-so could be destructive.
        if _ACCOUNT_MUTATION.search(label):
            continue
        score = 0
        if short and _LOGIN_TEXT.search(name):
            score += 80
        # "Energy portal" or "Member benefits" are content links; the account
        # term must be the whole short name or be owned by the visitor.
        if short and _ACCOUNT_ROUTE_LABEL.search(name.rstrip(" .…▾▼")):
            score += 60
        if _ACCOUNT_ROUTE_HREF.search(href):
            score += 50
        if re.search(r"\b(?:sign up|register|search|browse|featured|discover|forgot|password)\b", label):
            score -= 40
        if score > 0:
            candidates.append((score, el))
    if not candidates:
        return None
    _, best = sorted(candidates, key=lambda item: (-item[0], int(item[1].get("document_order") or 0)))[0]
    label = _text(best.get("text") or best.get("label") or best.get("aria_label") or best.get("href") or "account route")
    return {
        "tool": "browser_act",
        "action": f"click {label}",
        "reason": (
            "Sign-in link opens the login page."
            if _LOGIN_TEXT.search(label)
            else "Protected account route is likely to open the login page."
        ),
        "ref": best.get("ref") or "",
        "label": label,
        "href": best.get("href") or "",
    }


def snapshot_state_fingerprint(snapshot: dict[str, Any] | None) -> str:
    snapshot = snapshot or {}
    normalized_text = _text(snapshot.get("text") or snapshot.get("visible_text") or "")[:6000]
    control_states: list[dict[str, Any]] = []
    for element in (snapshot.get("elements") or [])[:180]:
        if not isinstance(element, dict):
            continue
        tag = str(element.get("tag") or "").lower()
        role = str(element.get("role") or "").lower()
        if tag not in {"input", "textarea", "select"} and role not in {"checkbox", "radio", "combobox", "textbox", "searchbox"}:
            continue
        value_material = "\n".join((
            str(element.get("value") or ""),
            str(element.get("selected_text") or ""),
            json.dumps(element.get("file_names") or [], ensure_ascii=False),
        ))
        control_states.append({
            "identity": str(
                element.get("fallback_selector") or element.get("css_selector") or element.get("selector")
                or element.get("id") or element.get("name") or ""
            ),
            "tag": tag,
            "type": str(element.get("type") or "").lower(),
            "checked": bool(element.get("checked")),
            "value_hash": hashlib.sha256(value_material.encode("utf-8", errors="ignore")).hexdigest()[:16],
        })
    material = "\n".join((
        str(snapshot.get("url") or ""),
        str(snapshot.get("title") or ""),
        normalized_text,
        str(snapshot.get("scroll_x") or 0),
        str(snapshot.get("scroll_y") or 0),
        json.dumps(control_states, sort_keys=True, separators=(",", ":")),
    ))
    return hashlib.sha256(material.encode("utf-8", errors="ignore")).hexdigest()


def _target_element(snapshot: dict[str, Any], action_spec: dict[str, Any]) -> dict[str, Any]:
    selector = str((action_spec or {}).get("selector") or (action_spec or {}).get("css_selector") or "")
    ref = str((action_spec or {}).get("ref") or "")
    for element in (snapshot or {}).get("elements") or []:
        if not isinstance(element, dict):
            continue
        if ref and str(element.get("ref") or "") == ref:
            return element
        if selector and selector in {
            str(element.get("selector") or ""),
            str(element.get("css_selector") or ""),
            str(element.get("fallback_selector") or ""),
        }:
            return element
    return {}


def verify_browser_action(
    action: str,
    action_spec: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    success: bool,
) -> dict[str, Any]:
    if not success:
        return {"checked": False, "ok": False, "reason": "action raised an error"}
    action = (action or "").strip().lower()
    before_url = str((before or {}).get("url") or "")
    after_url = str((after or {}).get("url") or "")
    before_text = str((before or {}).get("text") or (before or {}).get("visible_text") or "")
    after_text = str((after or {}).get("text") or (after or {}).get("visible_text") or "")
    before_target = _target_element(before or {}, action_spec or {})
    after_target = _target_element(after or {}, action_spec or {})

    if action in {"click", "click_at", "press", "scroll", "wait"}:
        target_type = str((action_spec or {}).get("_target_type") or before_target.get("type") or "").lower()
        target_role = str((action_spec or {}).get("_target_role") or before_target.get("role") or "").lower()
        form_control = target_type in {"checkbox", "radio"} or target_role in {"checkbox", "radio"}
        checked_changed = bool(before_target) and bool(after_target) and (
            bool(before_target.get("checked")) != bool(after_target.get("checked"))
        )
        return {
            "checked": True,
            "ok": checked_changed if form_control and action in {"click", "click_at", "press"} else True,
            "url_changed": before_url != after_url,
            "text_changed": before_text[:2000] != after_text[:2000],
            "state_changed": snapshot_state_fingerprint(before) != snapshot_state_fingerprint(after),
            "form_control": form_control,
            "checked_changed": checked_changed,
            "checked_after": bool(after_target.get("checked")) if after_target else None,
        }

    if action in {"fill", "type", "select"}:
        value = str((action_spec or {}).get("value") or (action_spec or {}).get("text") or "")
        actual_value = str(after_target.get("value") or "")
        selected_text = str(after_target.get("selected_text") or "")
        value_matches = bool(after_target) and (
            not value
            or value == actual_value
            or value in actual_value
            or value.casefold() == selected_text.casefold()
        )
        return {
            "checked": True,
            "ok": value_matches,
            "form_control": True,
            "target_found_after_action": bool(after_target),
            "value_matches_control_state": value_matches,
        }

    if action == "upload":
        evidence = dict((action_spec or {}).get("upload_verification") or action_spec or {})
        expected = [str(name) for name in evidence.get("expected_filenames") or []]
        observed = [str(name) for name in evidence.get("observed_input_filenames") or []]
        visible = [str(name) for name in evidence.get("visible_filename_matches") or []]
        verified = bool(evidence.get("verified")) and bool(expected) and all(
            name in observed or name in visible for name in expected
        )
        return {
            "checked": True,
            "ok": verified,
            "verified": verified,
            "form_control": True,
            "expected_filenames": expected,
            "observed_input_filenames": observed,
            "visible_filename_matches": visible,
            "verification_method": str(evidence.get("verification_method") or "none"),
            "reason": "exact filename evidence observed" if verified else "exact filename evidence missing",
        }

    if action == "batch_form_fill":
        before_state = snapshot_state_fingerprint(before)
        after_state = snapshot_state_fingerprint(after)
        return {
            "checked": True,
            "ok": before_state != after_state or bool((action_spec or {}).get("field_results")),
            "form_control": True,
            "value_matches_control_state": before_state != after_state,
            "fields_requested": len((action_spec or {}).get("fields") or []),
            "fields_succeeded": len([r for r in (action_spec or {}).get("field_results") or [] if r.get("success")]),
        }

    if action == "login_fill":
        return {"checked": True, "ok": True, "reason": "broker returned redacted fill status"}

    return {"checked": False, "ok": True, "reason": "no verifier for action"}


def form_success_result(task_description: str, steps: list[dict[str, Any]], verification: dict[str, Any], terminal_status: str) -> dict[str, Any]:
    requirements = objective_requirements(task_description)
    formish = bool(requirements.get("form_interaction") or requirements.get("upload") or requirements.get("submission_confirmation"))
    if terminal_status != "done" or not formish:
        return {}
    successful_steps = [step for step in steps or [] if isinstance(step, dict) and step.get("success")]
    form_actions = [
        str(step.get("action") or step.get("kind") or "").lower()
        for step in successful_steps
        if str(step.get("action") or step.get("kind") or "").lower() in {"fill", "type", "select", "upload", "batch_form_fill", "login_fill"}
        or bool((step.get("verification") or {}).get("form_control"))
    ]
    submitted = any(
        str((step.get("submit_intent") or {}).get("kind") or step.get("submit_intent") or "") not in {"", "none", "false", "no"}
        and str(step.get("action") or step.get("kind") or "").lower() in {"click", "press", "submit"}
        for step in successful_steps
    )
    uploaded = any(
        str(step.get("action") or step.get("kind") or "").lower() == "upload"
        and bool((step.get("verification") or step.get("upload_verification") or {}).get("verified"))
        for step in successful_steps
    )
    observed = (verification or {}).get("observed_evidence") if isinstance(verification, dict) else {}
    confirmation_visible = bool(isinstance(observed, dict) and observed.get("post_submission_confirmation"))
    if not form_actions and not submitted and not uploaded:
        return {}
    if requirements.get("submission_confirmation") and not submitted:
        return {}
    details: list[str] = []
    if submitted:
        details.append("final submission was completed")
    if uploaded:
        details.append("file upload was completed")
    if confirmation_visible:
        details.append("visible confirmation was detected")
    if form_actions:
        details.append(f"{len(form_actions)} successful form action(s) were recorded")
    summary = "Form workflow succeeded."
    if details:
        summary = f"{summary} " + "; ".join(details) + "."
    return {
        "status": "succeeded",
        "summary": summary,
        "submitted": submitted,
        "uploaded": uploaded,
        "confirmation_visible": confirmation_visible,
        "form_actions_count": len(form_actions),
        "successful_actions": [
            str(step.get("action") or step.get("kind") or "").lower()
            for step in successful_steps
            if str(step.get("action") or step.get("kind") or "").strip()
        ],
    }


def no_match_result(instruction: str, visible_text: str, *, fields: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    text = _text(visible_text).lower()
    instruction_text = _text(instruction).lower()
    if not instruction_text or not re.search(r"\b(?:find|search|extract|list|collect|filter)\b", instruction_text):
        return {"no_match": False, "no_match_reason": ""}
    explicit = re.search(r"\b(?:no results|no matches|nothing found|0 results|we couldn't find|could not find)\b", text)
    if explicit:
        return {
            "no_match": True,
            "no_match_reason": "The page reports no visible results matching the requested goal/filter requirements.",
        }
    quoted_terms = [m.group(1).strip().lower() for m in re.finditer(r"['\"]([^'\"]{3,80})['\"]", instruction or "")]
    if quoted_terms and text and not any(term in text for term in quoted_terms):
        return {
            "no_match": True,
            "no_match_reason": "Could not find visible information matching the quoted requested filter terms.",
        }
    if fields is not None and not fields and re.search(r"\b(?:form|field|input|dropdown|select)\b", instruction_text):
        return {
            "no_match": True,
            "no_match_reason": "Could not find visible form controls matching the requested goal.",
        }
    return {"no_match": False, "no_match_reason": ""}
