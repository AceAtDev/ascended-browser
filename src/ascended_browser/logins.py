"""Saved logins: the vault the developer fills, and what keeps it from the agent.

The vault (``runtime/database.py``) holds a username, password and optional
TOTP secret per site. ``browser_login`` types them into the page; the agent
asks for a login by site or account name and never handles the values. Three
guards keep it that way:

- ``scrub`` replaces every vault value (and its URL-encoded, JSON-escaped and
  HTML-escaped forms) in each tool result the server returns, so neither
  ``browser_observe``, ``browser_evaluate``, ``browser_extract`` (network
  bodies included) nor an action receipt can read one back.
- ``install_capture_masks`` makes every screenshot paint over username and
  email fields and any vault value shown as page text, on top of the
  password, card and one-time-code fields the browser already masks.
- The CLI reads secrets with a hidden prompt (or stdin), never from argv,
  where they would land in shell history and ``ps``.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import quote, quote_plus

OWNER = "local"
REDACTED = "[redacted]"
# Shorter values are skipped: replacing every "abc" in every page would wreck
# what the agent reads, and a vault value that short is not much of a secret.
MIN_SECRET_CHARS = 4

# Account identifiers a picture must not show. Broad on purpose, like the
# password selector it extends: an over-masked field costs a little detail, an
# under-masked one shows the account.
USERNAME_CAPTURE_SELECTOR = ", ".join((
    'input[autocomplete~="username" i]',
    'input[autocomplete~="email" i]',
    'input[type="email" i]',
    *(f'input[{attr}*="{word}" i]' for word in ("user", "login", "email", "identifier", "account")
      for attr in ("name", "id")),
))
USERNAME_LABEL_RE = re.compile(r"user\s*name|e-?mail|phone or email", re.I)

_cache: dict[str, Any] = {"stamp": None, "values": ()}


# ---------------------------------------------------------------- vault
def _broker():
    from ._app import browser_login_broker

    return browser_login_broker


def items(*, include_secrets: bool = False) -> list[dict]:
    return _broker().list_local_items(owner=OWNER, include_secrets=include_secrets)


def sites(item: dict) -> list[str]:
    return [str(u.get("uri") or "") for u in (item.get("login") or {}).get("uris") or [] if isinstance(u, dict)]


def resolve(key: str) -> dict:
    """The one item whose id (or id prefix), name or site matches key."""
    key = key.strip()
    found = [item for item in items(include_secrets=True)
             if item["id"].startswith(key) or item["name"].lower() == key.lower()
             or any(_host(site) == _host(key) for site in sites(item))]
    if not found:
        raise LookupError(f"No saved login matches {key!r}; see `ascended-browser login list`.")
    if len(found) > 1:
        names = ", ".join(f"{i['id'][:8]} {i['name']!r}" for i in found)
        raise LookupError(f"{key!r} matches several logins ({names}); use the id.")
    return found[0]


def _host(value: str) -> str:
    from urllib.parse import urlparse

    value = value.strip()
    return (urlparse(value if "://" in value else "https://" + value).hostname or "").lower()


def save(*, sites_: list[str], username: str | None, password: str | None, totp: str | None = None,
         name: str = "", notes: str = "", existing: dict | None = None) -> dict:
    """Add a login, or update ``existing`` (None keeps that field's saved value)."""
    old = (existing or {}).get("login") or {}
    data = {
        "name": name or (existing or {}).get("name") or (_host(sites_[0]) if sites_ else ""),
        "notes": notes or (existing or {}).get("notes") or "",
        "username": username if username is not None else old.get("username", ""),
        "password": password if password is not None else old.get("password", ""),
        "totp": totp if totp is not None else old.get("totp", ""),
        "uris": [{"uri": _site_url(site)} for site in sites_] if sites_ else old.get("uris", []),
    }
    saved = _broker().save_local_item(data, owner=OWNER, item_id=(existing or {}).get("id"))
    _cache["stamp"] = None
    return saved


def _site_url(site: str) -> str:
    site = site.strip()
    return site if "://" in site else "https://" + site


def remove(item_id: str) -> bool:
    _cache["stamp"] = None
    return _broker().delete_local_item(item_id, owner=OWNER)


# ---------------------------------------------------------------- scrubbing
def _stamp() -> tuple:
    from .runtime.database import vault_path

    path = vault_path()
    parts = []
    for candidate in (path, path.with_name(path.name + "-wal")):
        try:
            stat = candidate.stat()
            parts.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            parts.append(None)
    return tuple(parts)


def secret_values() -> tuple[tuple[str, bool], ...]:
    """(value, case_sensitive) for every saved username, password and TOTP secret.

    Re-read only when the vault file changes, so a login added while a server
    runs is scrubbed from its next result.
    """
    stamp = _stamp()
    if stamp != _cache["stamp"]:
        values: set[tuple[str, bool]] = set()
        for item in items(include_secrets=True):
            login = item.get("login") or {}
            for field, exact in (("password", True), ("totp", True), ("username", False)):
                value = str(login.get(field) or "")
                if len(value) >= MIN_SECRET_CHARS:
                    values.add((value, exact))
        _cache.update(stamp=stamp, values=tuple(values), pattern=_pattern(values))
    return _cache["values"]


def _forms(value: str) -> set[str]:
    return {value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1], html.escape(value)}


def _pattern(values) -> re.Pattern | None:
    parts = []
    for value, exact in values:
        for form in _forms(value):
            if len(form) >= MIN_SECRET_CHARS:
                escaped = re.escape(form)
                parts.append((len(form), escaped if exact else f"(?i:{escaped})"))
    if not parts:
        return None
    return re.compile("|".join(p for _n, p in sorted(parts, reverse=True)))


def scrub(text: str) -> str:
    """text with every saved login value replaced by [redacted]."""
    if not text:
        return text
    secret_values()
    pattern = _cache.get("pattern")
    return pattern.sub(REDACTED, text) if pattern is not None else text


def install() -> None:
    """What a process that runs the browser needs before its first tool call."""
    from ._app import browser_login_broker
    from .runtime.paths import data_dir

    # The app keeps this next to its own data, relative to its working directory;
    # here that is the client's project, so it moves into the data directory.
    browser_login_broker.RATE_LIMIT_FILE = data_dir() / "browser_work" / "login-fill-rate-limit.json"
    install_capture_masks()


# ---------------------------------------------------------------- screenshots
def install_capture_masks() -> None:
    """Extend the browser's screenshot masks with usernames (see module docstring).

    ``capture_page`` looks ``sensitive_mask_locators`` up at call time, so
    wrapping the module attribute covers every picture the tools take.
    """
    from ._app import browser_capture

    original = browser_capture.sensitive_mask_locators
    if getattr(original, "_ascended_usernames", False):
        return

    async def with_usernames(page: Any) -> tuple[list[Any], int]:
        locators, count = await original(page)
        extra, more = await username_mask_locators(page)
        return locators + extra, count + more

    with_usernames._ascended_usernames = True  # type: ignore[attr-defined]
    browser_capture.sensitive_mask_locators = with_usernames


async def username_mask_locators(page: Any) -> tuple[list[Any], int]:
    """Username fields and any vault value shown as page text, per frame. Fails closed like the password probe.

    A site that echoes the password ("Debug: hunter2") or prints the account
    ("Signed in as ada@...") shows it as text, which no field selector covers.
    """
    from ._app.browser_capture import MAX_MASK_FRAMES, CaptureError, _bounded

    shown = [value for value, _exact in secret_values()]
    frames = list(getattr(page, "frames", None) or []) or [page]
    main = getattr(page, "main_frame", None) or frames[0]
    locators: list[Any] = []
    count = 0
    for index, frame in enumerate(frames):
        if index >= MAX_MASK_FRAMES:
            break  # the password probe has already masked unchecked frames whole
        locator = frame.locator(USERNAME_CAPTURE_SELECTOR).or_(frame.get_by_label(USERNAME_LABEL_RE))
        for value in shown:
            locator = locator.or_(frame.get_by_text(value))
        try:
            matched = int(await _bounded(locator.count(), 2.0))
        except Exception as exc:
            if frame is main:
                raise CaptureError("Could not check the page for account names, so no picture was taken",
                                   error_kind="mask_unavailable") from exc
            parent = getattr(frame, "parent_frame", None)
            if parent is not None:
                locators.append(parent.locator("iframe, frame"))
            continue
        if matched:
            locators.append(locator)
            count += matched
    return locators, count
