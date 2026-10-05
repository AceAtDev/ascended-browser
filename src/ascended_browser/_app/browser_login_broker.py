"""Password-safe browser login fill broker.

The broker is the only layer that may retrieve built-in vault login secrets
for browser automation. Agent-visible callers receive redacted status only.
"""

from __future__ import annotations

import json
import re
import uuid
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import pyotp

from ascended_browser.runtime.platform import safe_chmod
from ascended_browser.runtime.database import BrowserLoginCredential, SessionLocal
from ascended_browser._app.browser_click_helpers import commit_edit


RATE_LIMIT_FILE = Path("data/browser_work/login-fill-rate-limit.json")

MATCH_DOMAIN = 0
MATCH_HOST = 1
MATCH_STARTS_WITH = 2
MATCH_EXACT = 3
MATCH_REGEX = 4
MATCH_NEVER = 5

MATCH_LABELS = {
    MATCH_DOMAIN: "domain",
    MATCH_HOST: "host",
    MATCH_STARTS_WITH: "starts_with",
    MATCH_EXACT: "exact",
    MATCH_REGEX: "regex",
    MATCH_NEVER: "never",
}
MATCH_VALUES = {v: k for k, v in MATCH_LABELS.items()}

SECRET_PATTERNS = [
    re.compile(r"(password|totp|secret|token|session|key)(['\"\s:=]+)[^'\"\s,;}]+", re.I),
    re.compile(r"otpauth://[^\s'\"<>]+", re.I),
]


def scrub_secret_text(text: Any, max_chars: Optional[int] = 800) -> str:
    value = "" if text is None else str(text)
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(lambda m: f"{m.group(1) if m.lastindex and m.lastindex >= 1 else 'secret'}[redacted]", value)
    return value if max_chars is None else value[:max_chars]


def redacted_item(item: dict) -> dict:
    login = item.get("login") if isinstance(item.get("login"), dict) else {}
    uris = login.get("uris") if isinstance(login.get("uris"), list) else []
    return {
        "id": item.get("id", ""),
        "name": item.get("name", ""),
        "username": login.get("username", ""),
        "uris": [
            {
                "uri": u.get("uri", ""),
                "match": u.get("match"),
                "match_label": MATCH_LABELS.get(u.get("match"), "default"),
            }
            for u in uris
            if isinstance(u, dict)
        ],
        "notes": bool(item.get("notes")),
        "has_password": bool(login.get("password")),
        "has_totp": bool(login.get("totp")),
        "revisionDate": item.get("revisionDate", ""),
        "creationDate": item.get("creationDate", ""),
        "type": item.get("type"),
    }


def item_for_ui(item: dict) -> dict:
    login = item.get("login") if isinstance(item.get("login"), dict) else {}
    payload = redacted_item(item)
    payload.update(
        {
            "password": login.get("password", ""),
            "totp": login.get("totp", ""),
            "notes_text": item.get("notes", "") or "",
        }
    )
    return payload


def _owner_value(owner: str | None) -> str | None:
    owner = (owner or "").strip().lower()
    return owner or None


def credential_to_item(row: BrowserLoginCredential, *, include_secrets: bool = False) -> dict:
    try:
        uris = json.loads(row.uris_json or "[]")
        if not isinstance(uris, list):
            uris = []
    except Exception:
        uris = []
    login = {
        "username": row.username or "",
        "uris": [u for u in uris if isinstance(u, dict)],
    }
    if include_secrets:
        login["password"] = row.password or ""
        login["totp"] = row.totp or ""
    return {
        "id": row.id,
        "type": 1,
        "name": row.name or "",
        "notes": row.notes or "",
        "login": login,
        "revisionDate": row.updated_at.isoformat() if getattr(row, "updated_at", None) else "",
        "creationDate": row.created_at.isoformat() if getattr(row, "created_at", None) else "",
        "expiresAt": row.expires_at.isoformat() if getattr(row, "expires_at", None) else "",
    }


def _expire_local_items(db, *, owner: str | None = None) -> None:
    """Apply user-selected retention before any vault item can be observed."""
    query = db.query(BrowserLoginCredential).filter(
        BrowserLoginCredential.is_deleted == False,  # noqa: E712
        BrowserLoginCredential.expires_at.is_not(None),
        BrowserLoginCredential.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None),
        BrowserLoginCredential.owner == owner,
    )
    if query.update({BrowserLoginCredential.is_deleted: True}, synchronize_session=False):
        db.commit()


def list_local_items(*, owner: str | None, query: str = "", include_secrets: bool = False) -> list[dict]:
    owner_key = _owner_value(owner)
    q = (query or "").strip().lower()
    db = SessionLocal()
    try:
        _expire_local_items(db, owner=owner_key)
        rows = (
            db.query(BrowserLoginCredential)
            .filter(BrowserLoginCredential.owner == owner_key, BrowserLoginCredential.is_deleted == False)  # noqa: E712
            .order_by(BrowserLoginCredential.updated_at.desc())
            .all()
        )
        items = [credential_to_item(row, include_secrets=include_secrets) for row in rows]
        if q:
            items = [
                item for item in items
                if q in str(item.get("name") or "").lower()
                or q in str((item.get("login") or {}).get("username") or "").lower()
                or any(q in str(uri.get("uri") or "").lower() for uri in ((item.get("login") or {}).get("uris") or []))
            ]
        return items
    finally:
        db.close()


def get_local_item(item_id: str, *, owner: str | None, include_secrets: bool = True) -> Optional[dict]:
    owner_key = _owner_value(owner)
    db = SessionLocal()
    try:
        _expire_local_items(db, owner=owner_key)
        row = (
            db.query(BrowserLoginCredential)
            .filter(
                BrowserLoginCredential.id == item_id,
                BrowserLoginCredential.owner == owner_key,
                BrowserLoginCredential.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        return credential_to_item(row, include_secrets=include_secrets) if row else None
    finally:
        db.close()


def save_local_item(
    data: dict,
    *,
    owner: str | None,
    item_id: str | None = None,
    expires_at: datetime | None = None,
) -> dict:
    owner_key = _owner_value(owner)
    payload = build_login_item_payload(data)
    login = payload.get("login") if isinstance(payload.get("login"), dict) else {}
    db = SessionLocal()
    try:
        row = None
        if item_id:
            row = (
                db.query(BrowserLoginCredential)
                .filter(
                    BrowserLoginCredential.id == item_id,
                    BrowserLoginCredential.owner == owner_key,
                    BrowserLoginCredential.is_deleted == False,  # noqa: E712
                )
                .first()
            )
        if row is None:
            row = BrowserLoginCredential(id=item_id or uuid.uuid4().hex, owner=owner_key)
            db.add(row)
        row.name = payload.get("name") or ""
        row.username = login.get("username") or ""
        row.password = login.get("password") or ""
        row.totp = login.get("totp") or ""
        row.notes = payload.get("notes") or ""
        row.uris_json = json.dumps(
            [
                {"uri": str(uri.get("uri") or ""), "match": MATCH_HOST}
                for uri in (login.get("uris") or [])
                if isinstance(uri, dict) and str(uri.get("uri") or "").strip()
            ],
            ensure_ascii=False,
        )
        row.expires_at = expires_at
        row.is_deleted = False
        db.commit()
        db.refresh(row)
        return credential_to_item(row, include_secrets=False)
    finally:
        db.close()


def delete_local_item(item_id: str, *, owner: str | None) -> bool:
    owner_key = _owner_value(owner)
    db = SessionLocal()
    try:
        row = (
            db.query(BrowserLoginCredential)
            .filter(
                BrowserLoginCredential.id == item_id,
                BrowserLoginCredential.owner == owner_key,
                BrowserLoginCredential.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if not row:
            return False
        row.is_deleted = True
        db.commit()
        return True
    finally:
        db.close()


def _normalize_url(raw: str) -> str:
    value = (raw or "").strip()
    if value and "://" not in value:
        value = "https://" + value
    return value


def _origin(url: str) -> str:
    parsed = urlparse(_normalize_url(url))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    netloc = parsed.hostname.lower().rstrip(".")
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((parsed.scheme.lower(), netloc, "", "", "", ""))


def _host(url: str) -> str:
    parsed = urlparse(_normalize_url(url))
    return (parsed.hostname or "").lower().rstrip(".")


def _pathless(url: str) -> str:
    parsed = urlparse(_normalize_url(url))
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return ""
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    return urlunparse((parsed.scheme.lower() or "https", netloc, parsed.path or "/", "", parsed.query or "", ""))


def credential_request_target(raw_url: str) -> dict[str, str]:
    """Return the safe, durable site identity used by credential requests.

    The live browser URL is authoritative. Query strings and fragments often
    contain OAuth state, redirect targets, or other transient data, and are not
    needed by the vault's host matcher, so they never enter interaction state.
    """
    value = str(raw_url or "").strip()
    parsed = urlparse(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("The selected browser tab does not have a safe HTTP(S) URL.")
    origin = _origin(value)
    if not origin:
        raise ValueError("The selected browser tab does not have a usable website origin.")
    path = parsed.path if str(parsed.path or "").startswith("/") else "/"
    safe_url = f"{origin}{path or '/'}"
    return {
        "url": safe_url,
        "origin": origin,
        "display_host": str(parsed.hostname or "").lower().rstrip("."),
    }


def login_available_for_url(page_url: str, *, owner: str | None) -> dict[str, Any] | None:
    """Secret-free vault availability metadata for a browser observation."""
    matches = [
        item for item in list_local_items(owner=owner, include_secrets=False)
        if isinstance(item, dict) and item.get("type") == 1 and item_matches_url(item, page_url)
    ]
    if not matches:
        return None
    return {
        "available": True,
        "count": len(matches),
        "accounts": [
            {"id": str(item.get("id") or ""), "label": _safe_label(item)}
            for item in matches[:10]
        ],
        "hint": "Saved login available for this website. Open its login page, then use browser_login.",
    }


def _registered_domain(host: str) -> str:
    host = (host or "").lower().rstrip(".")
    if not host:
        return ""
    try:
        import tldextract

        ext = tldextract.extract(host)
        return ext.registered_domain or ext.top_domain_under_public_suffix or host
    except Exception:
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _uri_match_type(uri: dict) -> int:
    raw = uri.get("match")
    if raw is None:
        return MATCH_HOST
    if isinstance(raw, int):
        return raw
    label = str(raw or "").strip().lower().replace("-", "_")
    return MATCH_VALUES.get(label, MATCH_NEVER)


def _strict_uri_matches(page_url: str, item_uri: dict) -> bool:
    match_type = _uri_match_type(item_uri)
    if match_type in {MATCH_NEVER, MATCH_STARTS_WITH, MATCH_REGEX}:
        return False
    candidate = str(item_uri.get("uri") or "").strip()
    if not candidate:
        return False
    page_origin = _origin(page_url)
    item_origin = _origin(candidate)
    page_host = _host(page_url)
    item_host = _host(candidate)
    if not page_origin or not item_origin or not page_host or not item_host:
        return False
    if match_type == MATCH_EXACT:
        return _pathless(page_url).rstrip("/") == _pathless(candidate).rstrip("/")
    if match_type == MATCH_HOST:
        return page_host == item_host
    if match_type == MATCH_DOMAIN:
        return _registered_domain(page_host) == _registered_domain(item_host)
    return False


def item_matches_url(item: dict, page_url: str) -> bool:
    login = item.get("login") if isinstance(item.get("login"), dict) else {}
    uris = login.get("uris") if isinstance(login.get("uris"), list) else []
    return any(isinstance(uri, dict) and _strict_uri_matches(page_url, uri) for uri in uris)


def _safe_label(item: dict) -> str:
    login = item.get("login") if isinstance(item.get("login"), dict) else {}
    name = str(item.get("name") or "Unnamed login")
    username = str(login.get("username") or "")
    return f"{name} ({username})" if username else name


def _rate_key(page_url: str, item_id: str) -> str:
    return f"{_origin(page_url)}|{item_id}"


def _load_rate_data() -> dict:
    try:
        data = json.loads(RATE_LIMIT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_rate_data(data: dict) -> None:
    RATE_LIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RATE_LIMIT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    safe_chmod(str(RATE_LIMIT_FILE), 0o600)


def _check_rate_limit(page_url: str, item_id: str, *, cap: int = 2, window_seconds: int = 600) -> bool:
    now = time.time()
    data = _load_rate_data()
    key = _rate_key(page_url, item_id)
    attempts = [float(ts) for ts in data.get(key, []) if now - float(ts) < window_seconds]
    if len(attempts) >= cap:
        data[key] = attempts
        _save_rate_data(data)
        return False
    attempts.append(now)
    data[key] = attempts
    _save_rate_data(data)
    return True


def _totp_code(secret: str) -> str:
    raw = (secret or "").strip()
    if not raw:
        return ""
    try:
        if raw.lower().startswith("otpauth://"):
            return pyotp.parse_uri(raw).now()
        cleaned = re.sub(r"\s+", "", raw)
        return pyotp.TOTP(cleaned).now()
    except Exception:
        return ""


def _hint_tokens(hint: str) -> list[str]:
    """Split an account hint into the words it is actually made of.

    A caller naturally names an account the way it was shown to it — including
    the form ``_safe_label`` emits, "Name (username)". Matching that whole
    string as a substring of ``name`` can never succeed, because ``name`` does
    not contain the parenthesised username. Compare word by word instead.
    """
    return [token for token in re.split(r"[^\w@.+-]+", str(hint or "").lower()) if token]


def _hint_matches(item: dict, tokens: list[str]) -> bool:
    """Every word of the hint must appear somewhere in this item's identity."""
    login = item.get("login") if isinstance(item.get("login"), dict) else {}
    haystack = " ".join([
        str(item.get("name") or ""),
        str(login.get("username") or ""),
        str(item.get("id") or ""),
        _safe_label(item),
    ]).lower()
    return all(token in haystack for token in tokens)


async def _select_item_for_url(page_url: str, *, account_hint: str = "", owner: str | None = None) -> tuple[Optional[dict], dict]:
    items = list_local_items(owner=owner, include_secrets=True)
    candidates = [item for item in items if isinstance(item, dict) and item.get("type") == 1 and item_matches_url(item, page_url)]
    for_url = list(candidates)
    tokens = _hint_tokens(account_hint) if account_hint else []
    if tokens:
        candidates = [item for item in candidates if _hint_matches(item, tokens)]
        if not candidates and for_url:
            # The site has stored logins; this hint excluded all of them. That
            # is a different problem from having no credentials, and the caller
            # can only act on it if we say which one happened and what was
            # actually available.
            return None, {
                "status": "hint_excluded_all",
                "message": (
                    f"{len(for_url)} stored login(s) match this site, but none match "
                    f"account_hint {account_hint!r}. Retry without account_hint, or use "
                    "one of the labels below exactly."
                ),
                "available": [
                    {"id": item.get("id", ""), "label": _safe_label(item)} for item in for_url[:10]
                ],
            }
    if not candidates:
        return None, {
            "status": "no_match",
            "message": (
                "No stored login is registered for this site. Do not retry; ask the "
                "user to add one, or sign in another way."
            ),
        }
    if len(candidates) > 1:
        return None, {
            "status": "ambiguous_match",
            "message": "Multiple safe matching login items found.",
            "matches": [{"id": item.get("id", ""), "label": _safe_label(item)} for item in candidates[:10]],
        }
    return candidates[0], {}


@dataclass
class LoginFields:
    username: str = ""
    password: str = ""
    totp: str = ""


_LOGIN_TARGET_ATTR = "data-odysseus-login-fill-target"


_DISCOVER_LOGIN_TARGETS = r"""({marker, hasUsername, hasPassword, hasTotp}) => {
  const textTypes = new Set(['', 'text', 'email', 'tel', 'number']);
  const visible = el => {
    if (!el || el.hidden || (el.type || '').toLowerCase() === 'hidden') return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  };
  const roots = [];
  const visit = root => {
    roots.push(root);
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) visit(el.shadowRoot);
    }
  };
  visit(document);
  const all = roots.flatMap(root => Array.from(root.querySelectorAll('input, textarea')))
    .filter(el => visible(el) && !el.disabled && !el.readOnly);
  const index = new Map(all.map((el, i) => [el, i]));
  const labels = el => Array.from(el.labels || []).map(label => label.textContent || '').join(' ');
  const norm = el => [
    el.getAttribute('autocomplete'), el.name, el.id, el.placeholder,
    el.getAttribute('aria-label'), el.getAttribute('title'), labels(el),
  ].filter(Boolean).join(' ').toLowerCase();
  const scopeOf = el => el.closest('form,[role=dialog],[aria-modal=true],.modal,.popup,.login,.signin,.sign-in') || document.body;
  const scopeNorm = el => {
    const scope = scopeOf(el);
    return [scope.id, scope.getAttribute?.('name'), scope.getAttribute?.('aria-label'), scope.className]
      .filter(value => typeof value === 'string').join(' ').toLowerCase();
  };
  const autocomplete = el => (el.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/);
  const hasToken = (el, token) => autocomplete(el).includes(token);
  const registration = el => /sign.?up|register|registration|create.?account|join/.test(scopeNorm(el));
  const recovery = el => /backup|recovery/.test(norm(el));
  const newPassword = el => hasToken(el, 'new-password') || /new.?pass|confirm|repeat|verify.?pass/.test(norm(el));
  const passwordScore = el => {
    const type = (el.type || '').toLowerCase();
    const words = norm(el);
    if (newPassword(el) || recovery(el) || registration(el)) return -1;
    if (type !== 'password' && !/pass(word)?|pwd/.test(words)) return -1;
    return 80 + (hasToken(el, 'current-password') ? 60 : 0) + (type === 'password' ? 20 : 0);
  };
  const usernameScore = el => {
    const type = (el.type || '').toLowerCase();
    const words = norm(el);
    if (!textTypes.has(type) || type === 'number' || recovery(el)) return -1;
    if (/search|coupon|promo|invite|first.?name|last.?name/.test(words)) return -1;
    let score = 0;
    if (hasToken(el, 'username')) score += 100;
    if (hasToken(el, 'email')) score += 70;
    if (/user|email|e-mail|login|account|identifier/.test(words)) score += 50;
    if (type === 'email') score += 35;
    if (type === 'tel') score += 5;
    return score || -1;
  };
  const totpScore = el => {
    const type = (el.type || '').toLowerCase();
    const words = norm(el);
    if (!textTypes.has(type) || recovery(el)) return -1;
    if (hasToken(el, 'one-time-code')) return 140;
    if (/totp|2fa|mfa|authenticator|one.?time|verification.?code|security.?code/.test(words)) return 100;
    if (/\botp\b/.test(words)) return 80;
    return -1;
  };
  const rankedPasswords = all.map(el => ({el, score: passwordScore(el)})).filter(x => x.score >= 0)
    .sort((a, b) => b.score - a.score || index.get(a.el) - index.get(b.el));
  let passwordEl = hasPassword && rankedPasswords.length ? rankedPasswords[0].el : null;
  let ambiguous = false;
  if (rankedPasswords.length > 1 && rankedPasswords[0].score === rankedPasswords[1].score &&
      scopeOf(rankedPasswords[0].el) !== scopeOf(rankedPasswords[1].el)) ambiguous = true;
  const loginScope = passwordEl ? scopeOf(passwordEl) : null;
  let users = all.map(el => ({el, score: usernameScore(el)})).filter(x => x.score >= 0);
  if (loginScope) users = users.filter(x => scopeOf(x.el) === loginScope && index.get(x.el) < index.get(passwordEl));
  users.sort((a, b) => b.score - a.score || index.get(b.el) - index.get(a.el));
  const userEl = hasUsername && users.length ? users[0].el : null;
  if (!passwordEl && users.length > 1 && users[0].score === users[1].score &&
      scopeOf(users[0].el) !== scopeOf(users[1].el)) ambiguous = true;
  let totps = all.map(el => ({el, score: totpScore(el)})).filter(x => x.score >= 0 && x.el !== userEl && x.el !== passwordEl);
  if (loginScope) {
    const sameScope = totps.filter(x => scopeOf(x.el) === loginScope);
    if (sameScope.length) totps = sameScope;
  }
  totps.sort((a, b) => b.score - a.score || index.get(a.el) - index.get(b.el));
  let totpEls = [];
  if (hasTotp && totps.length) {
    const top = totps[0];
    const split = totps.filter(x => x.score === top.score && scopeOf(x.el) === scopeOf(top.el) &&
      Number(x.el.maxLength || 0) === 1);
    totpEls = split.length > 1 ? split.map(x => x.el) : [top.el];
  }
  const marked = [];
  const mark = (el, role, part = 0) => {
    if (!el) return '';
    const id = `${marker}:${role}:${part}`;
    el.setAttribute(marker.split(':')[0], id);
    marked.push(id);
    return id;
  };
  const usernameId = mark(userEl, 'username');
  const passwordId = mark(passwordEl, 'password');
  const totpIds = totpEls.map((el, i) => mark(el, 'totp', i));
  return {
    username_id: usernameId, password_id: passwordId, totp_ids: totpIds,
    ambiguous,
    score: (passwordEl ? 200 : 0) + (userEl ? 80 : 0) + (totpEls.length ? 40 : 0),
    marked,
  };
}"""


async def _discover_login_targets(frame: Any, fields: LoginFields, marker: str) -> dict:
    return await frame.evaluate(
        _DISCOVER_LOGIN_TARGETS,
        {
            "marker": f"{_LOGIN_TARGET_ATTR}:{marker}",
            "hasUsername": bool(fields.username),
            "hasPassword": bool(fields.password),
            "hasTotp": bool(fields.totp),
        },
    )


async def _remove_login_markers(frame: Any, marker: str) -> None:
    try:
        await frame.evaluate(
            """({attr, prefix}) => {
              const roots = [];
              const visit = root => {
                roots.push(root);
                for (const el of root.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
              };
              visit(document);
              for (const root of roots) for (const el of root.querySelectorAll(`[${attr}]`)) {
                if ((el.getAttribute(attr) || '').startsWith(prefix)) el.removeAttribute(attr);
              }
            }""",
            {"attr": _LOGIN_TARGET_ATTR, "prefix": f"{_LOGIN_TARGET_ATTR}:{marker}:"},
        )
    except Exception:
        pass


async def _fill_marked_field(frame: Any, target_id: str, value: str) -> bool:
    """Fill one non-secret marker and verify that the intended value stuck."""
    if not target_id or not value:
        return False
    locator = frame.locator(f'[{_LOGIN_TARGET_ATTR}="{target_id}"]').first
    try:
        # locator.fill already waits for and focuses an editable target. An
        # explicit click + focus duplicated that work and could pay Camoufox's
        # first-action delay twice before any credential was inserted.
        await locator.fill(value, timeout=5000)
        # Leave the field with its change event: Camoufox fires none for
        # fill(), and sign-in forms that validate on change stayed empty.
        await commit_edit(locator, refocus=False)
        if await locator.input_value(timeout=5000) == value:
            return True
        # Masked/controlled inputs sometimes reject bulk insertion. A real
        # keystroke sequence is slower but is a useful, bounded fallback.
        await locator.focus(timeout=5000)
        await locator.press("ControlOrMeta+A", timeout=5000)
        await locator.press_sequentially(value, delay=10, timeout=5000)
        await locator.blur(timeout=5000)
        return await locator.input_value(timeout=5000) == value
    except Exception:
        # Never return Playwright's call log: fill errors may include the secret.
        return False


async def _marked_field_matches(frame: Any, target_id: str, value: str) -> bool:
    if not target_id or not value:
        return False
    try:
        locator = frame.locator(f'[{_LOGIN_TARGET_ATTR}="{target_id}"]').first
        return await locator.input_value(timeout=5000) == value
    except Exception:
        return False


async def _fill_fields(page, fields: LoginFields, *, item: Optional[dict] = None) -> dict:
    """Discover, fill, and verify login controls without exposing field values."""
    frames = list(getattr(page, "frames", None) or [page])
    main_frame = getattr(page, "main_frame", None)
    marker = uuid.uuid4().hex
    candidates: list[tuple[Any, dict]] = []
    touched: list[Any] = []
    try:
        for frame in frames:
            frame_url = str(getattr(frame, "url", "") or "")
            is_main = frame is main_frame or (main_frame is None and frame is page)
            if not is_main and item is not None and not item_matches_url(item, frame_url):
                continue
            try:
                found = await _discover_login_targets(frame, fields, marker)
            except Exception:
                continue
            touched.append(frame)
            if found.get("marked"):
                candidates.append((frame, found))
        candidates.sort(key=lambda pair: int(pair[1].get("score") or 0), reverse=True)
        if not candidates:
            return {"username_filled": False, "password_filled": False, "totp_filled": False}
        if candidates[0][1].get("ambiguous") or (
            len(candidates) > 1
            and int(candidates[0][1].get("score") or 0) == int(candidates[1][1].get("score") or 0)
        ):
            return {
                "username_filled": False, "password_filled": False, "totp_filled": False,
                "ambiguous": True,
            }
        frame, targets = candidates[0]
        username_id = str(targets.get("username_id") or "")
        password_id = str(targets.get("password_id") or "")
        username_filled = await _fill_marked_field(frame, username_id, fields.username)
        password_filled = await _fill_marked_field(frame, password_id, fields.password)
        totp_ids = [str(value) for value in targets.get("totp_ids") or [] if value]
        totp_results = []
        if len(totp_ids) > 1 and len(totp_ids) == len(fields.totp):
            for target_id, character in zip(totp_ids, fields.totp):
                totp_results.append(await _fill_marked_field(frame, target_id, character))
        elif totp_ids:
            totp_results.append(await _fill_marked_field(frame, totp_ids[0], fields.totp))
        if username_filled:
            username_filled = await _marked_field_matches(frame, username_id, fields.username)
        if password_filled:
            password_filled = await _marked_field_matches(frame, password_id, fields.password)
        if totp_results and all(totp_results):
            if len(totp_ids) > 1 and len(totp_ids) == len(fields.totp):
                totp_results = [
                    await _marked_field_matches(frame, target_id, character)
                    for target_id, character in zip(totp_ids, fields.totp)
                ]
            else:
                totp_results = [await _marked_field_matches(frame, totp_ids[0], fields.totp)]
        final_target = ""
        if totp_results and all(totp_results):
            final_target = totp_ids[-1]
        elif password_filled:
            final_target = password_id
        elif username_filled:
            final_target = username_id
        if final_target:
            try:
                await frame.locator(f'[{_LOGIN_TARGET_ATTR}="{final_target}"]').first.focus(timeout=5000)
            except Exception:
                pass
        return {
            "username_filled": username_filled,
            "password_filled": password_filled,
            "totp_filled": bool(totp_results) and all(totp_results),
            "_target_frame": frame,
            "_target_url": str(getattr(frame, "url", "") or ""),
        }
    finally:
        for frame in touched:
            await _remove_login_markers(frame, marker)


async def _safe_submit(page, matched_url: str) -> tuple[bool, str]:
    matched_origin = _origin(matched_url)
    script = """(matchedOrigin) => {
      const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
      const norm = el => ((el.getAttribute('autocomplete') || '') + ' ' + (el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
      const labels = /^(log\\s*in|login|sign\\s*in|signin|continue|next|submit)$/i;
      const allInputs = Array.from(document.querySelectorAll('input, textarea')).filter(visible);
      const passwordEl = allInputs.find(el => (el.type || '').toLowerCase() === 'password' || (/pass(word)?|pwd/.test(norm(el)) && !/forgot|reset/.test(norm(el))));
      const loginScope = passwordEl && (passwordEl.closest('form,[role=dialog],[aria-modal=true],.modal,.popup,.login,.signin,.sign-in') || document.body);
      const selectors = [
        'button',
        'input[type=submit]',
        'input[type=button]',
        'a',
        '[role=button]',
        '[onclick]',
        '.btn',
        '.button'
      ].join(',');
      const candidates = Array.from(document.querySelectorAll(selectors))
        .filter(el => visible(el) && !el.disabled)
        .sort((a, b) => {
          const aIn = loginScope && loginScope !== document.body && loginScope.contains(a) ? 0 : 1;
          const bIn = loginScope && loginScope !== document.body && loginScope.contains(b) ? 0 : 1;
          return aIn - bIn;
        });
      for (const el of candidates) {
        const text = ((el.innerText || el.value || el.getAttribute('aria-label') || '') + '').trim();
        if (!labels.test(text)) continue;
        if (loginScope && loginScope !== document.body && !loginScope.contains(el)) continue;
        const form = el.closest('form');
        if (form && form.getAttribute('action')) {
          try {
            const action = new URL(form.getAttribute('action'), location.href);
            if (action.origin !== matchedOrigin) return { ok: false, reason: 'cross_origin_form_action' };
          } catch (e) {
            return { ok: false, reason: 'unresolvable_form_action' };
          }
        }
        el.click();
        return { ok: true, reason: 'clicked' };
      }
      return { ok: false, reason: 'no_obvious_submit' };
    }"""
    result = await page.evaluate(script, matched_origin)
    return bool(result.get("ok")), str(result.get("reason") or "")


async def login_fill_page(page, *, submit: bool = False, account_hint: str = "", wait_ms: int = 500, owner: str | None = None) -> dict:
    try:
        page_url = getattr(page, "url", "") or ""
        item, status = await _select_item_for_url(page_url, account_hint=account_hint, owner=owner)
        if item is None:
            return {**status, "filled": False, "submitted": False}
        item_id = str(item.get("id") or "")
        if not _check_rate_limit(page_url, item_id):
            return {"status": "rate_limited", "filled": False, "submitted": False, "message": "Login fill rate limit reached."}
        login = item.get("login") if isinstance(item.get("login"), dict) else {}
        fields = LoginFields(
            username=str(login.get("username") or ""),
            password=str(login.get("password") or ""),
            totp=_totp_code(str(login.get("totp") or "")),
        )
        filled = await _fill_fields(page, fields, item=item)
        fields_status = {
            "username": bool(filled.get("username_filled")),
            "password": bool(filled.get("password_filled")),
            "totp": bool(filled.get("totp_filled")),
        }
        any_field_filled = any(fields_status.values())
        required_field_filled = bool(fields_status["password"] or fields_status["username"])
        if not any_field_filled:
            ambiguous = bool(filled.get("ambiguous"))
            return {
                "status": "ambiguous_login_fields" if ambiguous else "no_visible_login_fields",
                "filled": False,
                "submitted": False,
                "submit_reason": "ambiguous_fields" if ambiguous else "fields_not_found",
                "origin": _origin(page_url),
                "item_id": item_id,
                "label": _safe_label(item),
                "owner": _owner_value(owner) or "",
                "fields": fields_status,
                "next_action_hint": (
                    "Focus the intended login field or open a page with only one login form, then retry."
                    if ambiguous else
                    "Open the login page/form first, then retry browser_login_fill."
                ),
            }
        submitted = False
        submit_reason = ""
        if submit and required_field_filled:
            target_frame = filled.get("_target_frame") or page
            target_url = str(filled.get("_target_url") or page_url)
            submitted, submit_reason = await _safe_submit(target_frame, target_url)
            if wait_ms:
                await page.wait_for_timeout(max(0, min(int(wait_ms), 5000)))
        return {
            "status": "filled",
            "filled": required_field_filled,
            "submitted": submitted,
            "submit_reason": submit_reason,
            "origin": _origin(page_url),
            "item_id": item_id,
            "label": _safe_label(item),
            "owner": _owner_value(owner) or "",
            "fields": fields_status,
        }
    except Exception as exc:
        return {"status": "error", "filled": False, "submitted": False, "message": scrub_secret_text(exc)}


def build_login_item_payload(data: dict, *, existing: Optional[dict] = None) -> dict:
    item = dict(existing or {})
    item["type"] = 1
    item["name"] = str(data.get("name") or item.get("name") or "").strip()
    item["notes"] = str(data.get("notes") or data.get("notes_text") or item.get("notes") or "")
    login = dict(item.get("login") if isinstance(item.get("login"), dict) else {})
    login["username"] = str(data.get("username") if data.get("username") is not None else login.get("username") or "")
    if data.get("password") is not None:
        login["password"] = str(data.get("password") or "")
    if data.get("totp") is not None:
        login["totp"] = str(data.get("totp") or "")
    raw_uris = data.get("uris") if isinstance(data.get("uris"), list) else []
    uris = []
    for uri in raw_uris:
        if not isinstance(uri, dict):
            continue
        value = str(uri.get("uri") or "").strip()
        if not value:
            continue
        match = _uri_match_type(uri)
        if match not in {MATCH_DOMAIN, MATCH_HOST, MATCH_EXACT, MATCH_STARTS_WITH, MATCH_REGEX, MATCH_NEVER}:
            match = MATCH_HOST
        uris.append({"uri": value, "match": match})
    login["uris"] = uris
    item["login"] = login
    return item
