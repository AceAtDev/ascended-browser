"""Read-only inputs for the streamed browser's address-bar suggestions.

The browser profile remains the owner of visited-page history. Search
completions come from the same DuckDuckGo destination already used for free
text in the address bar; failures intentionally degrade to local matches.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import sqlite3
import time
from tempfile import TemporaryDirectory
from pathlib import Path
from threading import RLock

import httpx


_HISTORY_LIMIT = 8
_TOP_SITE_LIMIT = 4
_SEARCH_LIMIT = 8
_SEARCH_CACHE_SECONDS = 90.0
_SEARCH_CACHE_LIMIT = 192
_SEARCH_TIMEOUT_SECONDS = 1.4
_SEARCH_CACHE: dict[str, tuple[float, list[str]]] = {}
_SEARCH_CACHE_LOCK = RLock()
_LIKE_ESCAPE = "\\"
_SEARCH_TITLE_OPERATOR = re.compile(
    r"(?:^|\s)(?:site|inurl|intitle|intext|filetype):|\s(?:OR|AND)\s",
    re.IGNORECASE,
)


def _history_rows(connection: sqlite3.Connection, text: str, cap: int):
    escaped = text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", _LIKE_ESCAPE + "%").replace("_", _LIKE_ESCAPE + "_")
    pattern = f"%{escaped}%"
    if text:
        return connection.execute(
            "SELECT url, title, visit_count, last_visit_date "
            "FROM moz_places "
            "WHERE url LIKE 'http%' AND visit_count > 0 AND hidden = 0 "
            "AND (url LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR COALESCE(title, '') LIKE ? ESCAPE '\\' COLLATE NOCASE) "
            "ORDER BY COALESCE(last_visit_date, 0) DESC LIMIT ?",
            (pattern, pattern, cap),
        ).fetchall()
    return connection.execute(
        "SELECT url, title, visit_count, last_visit_date "
        "FROM moz_places WHERE url LIKE 'http%' AND visit_count > 0 "
        "AND hidden = 0 ORDER BY visit_count DESC, "
        "COALESCE(last_visit_date, 0) DESC LIMIT ?",
        (min(64, cap * 8),),
    ).fetchall()


def _history_snapshot_rows(places: Path, text: str, cap: int):
    """Read a private throwaway copy when Firefox holds an exclusive DB lock."""
    with TemporaryDirectory(prefix="odysseus-omnibox-") as temporary:
        snapshot = Path(temporary) / "places.sqlite"
        suffixes = ("", "-wal", "-shm", "-journal")
        for attempt in range(2):
            before = []
            for suffix in suffixes:
                source = Path(f"{places}{suffix}")
                try:
                    stat = source.stat()
                    before.append((suffix, stat.st_size, stat.st_mtime_ns, stat.st_ino))
                    shutil.copyfile(source, Path(f"{snapshot}{suffix}"))
                except FileNotFoundError:
                    before.append((suffix, 0, 0, 0))
                    Path(f"{snapshot}{suffix}").unlink(missing_ok=True)
            after = []
            for suffix in suffixes:
                try:
                    stat = Path(f"{places}{suffix}").stat()
                    after.append((suffix, stat.st_size, stat.st_mtime_ns, stat.st_ino))
                except FileNotFoundError:
                    after.append((suffix, 0, 0, 0))
            if before == after or attempt:
                break
        connection = sqlite3.connect(str(snapshot), timeout=0.12)
        try:
            return _history_rows(connection, text, cap)
        finally:
            connection.close()


def _safe_history_url(value: object) -> str:
    url = str(value or "").strip()
    if len(url) > 2048 or not url.lower().startswith(("http://", "https://")):
        return ""
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username or parsed.password:
            return ""
        return url
    except (TypeError, ValueError):
        return ""


def firefox_history_matches(
    profile_dir: str | Path, query: str = "", *, limit: int = _HISTORY_LIMIT,
) -> list[dict[str, object]]:
    """Read a small, recent set of HTTP(S) visits from Firefox Places.

    SQLite is opened read-only and the history remains in the existing browser
    profile. Private/internal addresses are allowed here because this data is
    only returned to the authenticated owner and is never sent to search.
    """
    places = Path(profile_dir) / "places.sqlite"
    if not places.is_file():
        return []
    cap = max(1, min(int(limit or _HISTORY_LIMIT), _HISTORY_LIMIT))
    text = str(query or "").strip()[:180]
    try:
        rows = None
        connection = None
        try:
            connection = sqlite3.connect(
                f"{places.resolve().as_uri()}?mode=ro", uri=True, timeout=0.06,
            )
            rows = _history_rows(connection, text, cap)
        except sqlite3.OperationalError as exc:
            if (int(getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF) not in {
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
            }:
                raise
            rows = _history_snapshot_rows(places, text, cap)
        finally:
            if connection is not None:
                connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return []

    return history_matches_from_rows(rows, text, cap)


def history_matches_from_rows(rows, text: str, cap: int = _HISTORY_LIMIT):
    """Project trusted browser-owned rows with identical history/top-site rules."""
    matches: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_url, raw_title, visits, visited_at in rows:
        url = _safe_history_url(raw_url)
        if not url or url in seen:
            continue
        try:
            from urllib.parse import urlsplit, urlunsplit

            parsed = urlsplit(url)
            origin = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        except ValueError:
            continue
        is_origin_url = parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
        is_search_title = bool(_SEARCH_TITLE_OPERATOR.search(str(raw_title or "")))
        if text and is_origin_url and is_search_title:
            # The title says this was a search, but the recorded URL cannot
            # replay it. Do not offer the engine home page as a matching
            # history result for the query.
            continue
        if not text:
            # The idle omnibox shows familiar sites, not a stack of pages from
            # one host. Keep the URL/title from that host's most visited page,
            # but take the user to its origin root when selected.
            # The displayed title must describe the synthesized destination.
            # A deeper history page may be a search results page titled with
            # the user's query; pairing that title with the site root makes a
            # top-site row look like it will repeat the search when it cannot.
            if not is_origin_url or is_search_title:
                raw_title = (parsed.hostname or parsed.netloc).removeprefix("www.")
            if origin in seen:
                continue
            url = origin
            seen.add(origin)
        else:
            seen.add(url)
        matches.append({
            "url": url,
            "title": str(raw_title or "").strip()[:200],
            "visit_count": max(0, int(visits or 0)),
            "last_visited": max(0, int(visited_at or 0)),
        })
        if len(matches) >= (min(cap, _TOP_SITE_LIMIT) if not text else cap):
            break
    return matches


def looks_like_address(text: str) -> bool:
    """True when input is URL/host-shaped and should not leave for suggestions."""
    value = str(text or "").strip()
    if not value or any(char.isspace() for char in value):
        return False
    if "://" in value or "/" in value or value.startswith(("?", "#")):
        return True
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", value):
        return True
    host = value
    if ":" in host:
        base, port = host.rsplit(":", 1)
        if port.isdigit():
            return True
    else:
        base = host
    if base.casefold() in {"localhost", "localhost.localdomain"} or "." in base:
        return True
    try:
        ipaddress.ip_address(base.strip("[]"))
        return True
    except ValueError:
        return False


def _clean_search_suggestions(payload: object, query: str) -> list[str]:
    values: object = []
    if isinstance(payload, list) and len(payload) > 1:
        values = payload[1]
    elif isinstance(payload, dict):
        values = payload.get("suggestions") or payload.get("results") or []
    if not isinstance(values, list):
        return []

    needle = str(query or "").strip().casefold()
    result: list[str] = []
    seen: set[str] = {needle}
    for item in values:
        candidate = item.get("phrase") if isinstance(item, dict) else item
        candidate = re.sub(r"[\x00-\x1f\x7f]", " ", str(candidate or ""))
        candidate = re.sub(r"\s+", " ", candidate).strip()[:180]
        key = candidate.casefold()
        if not candidate or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
        if len(result) >= _SEARCH_LIMIT:
            break
    return result


def _cached_search(query: str) -> list[str] | None:
    now = time.monotonic()
    with _SEARCH_CACHE_LOCK:
        for key, (expires_at, _value) in list(_SEARCH_CACHE.items()):
            if expires_at <= now:
                _SEARCH_CACHE.pop(key, None)
        cached = _SEARCH_CACHE.get(query.casefold())
        return list(cached[1]) if cached else None


def _cache_search(query: str, suggestions: list[str]) -> None:
    now = time.monotonic()
    key = query.casefold()
    with _SEARCH_CACHE_LOCK:
        for stale, (expires_at, _value) in list(_SEARCH_CACHE.items()):
            if expires_at <= now:
                _SEARCH_CACHE.pop(stale, None)
        while key not in _SEARCH_CACHE and len(_SEARCH_CACHE) >= _SEARCH_CACHE_LIMIT:
            oldest = min(_SEARCH_CACHE, key=lambda item: _SEARCH_CACHE[item][0])
            _SEARCH_CACHE.pop(oldest, None)
        _SEARCH_CACHE[key] = (now + _SEARCH_CACHE_SECONDS, list(suggestions))


async def duckduckgo_search_suggestions(query: str, owner_id: str = "") -> list[str]:
    """Fetch bounded DDG completions; return [] for every provider failure."""
    text = re.sub(r"\s+", " ", str(query or "")).strip()[:180]
    if len(text) < 2 or looks_like_address(text):
        return []
    cached = _cached_search(text)
    if cached is not None:
        return cached

    def fetch() -> list[str]:
        with httpx.stream(
            "GET",
            "https://duckduckgo.com/ac/",
            params={"q": text, "type": "list"},
            headers={"Accept": "application/json", "User-Agent": "Ascended-Browser-Omnibox"},
            timeout=_SEARCH_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                return []
            media_type = str(response.headers.get("content-type") or "").split(";", 1)[0].lower()
            if media_type not in {"application/json", "application/javascript", "text/javascript"}:
                return []
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > 64 * 1024:
                    return []
                chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        return _clean_search_suggestions(payload, text)

    try:
        from ascended_browser.runtime.admission import (
            OutboundPriority,
            get_outbound_scheduler,
            provider_slot,
        )

        result = await get_outbound_scheduler().run_blocking(
            "search",
            provider_slot("duckduckgo-autocomplete", "https://duckduckgo.com"),
            fetch,
            owner_id=owner_id,
            priority=OutboundPriority.FOREGROUND,
            # A fresh query can arrive while the previous physical request
            # continues after its UI client cancels. Allow autocomplete's
            # 300ms pacing interval, while staying inside the client's
            # 2.2s budget with the bounded 1.65s execution window.
            admission_timeout=0.5,
            execution_timeout=_SEARCH_TIMEOUT_SECONDS + 0.25,
        )
        if isinstance(result, list):
            _cache_search(text, result)
            return result
    except Exception:
        # Autocomplete is optional: the local history and ordinary Go path
        # remain available during provider, scheduler, or network failures.
        return []
    return []
