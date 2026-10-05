from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ascended_browser._app.browser_profile_coord import (
    clear_stale_profile_lock_files,
    master_profile_dir,
    master_profile_in_use,
    master_profile_lock,
    profile_process_ids,
)
from ascended_browser.runtime.constants import DATA_DIR

_AUTH_KEY = re.compile(r"(?:auth|session|token|jwt|oauth|account|identity)", re.I)


def _owner_key(owner: str) -> str:
    return hashlib.sha256((owner or "default").encode()).hexdigest()[:24]


def _cookie_key(item: dict) -> tuple:
    return (item.get("name"), item.get("domain"), item.get("path"), item.get("partitionKey"))


def _origins(state: dict) -> dict[str, dict]:
    return {str(item.get("origin") or ""): item for item in state.get("origins") or [] if item.get("origin")}


def _index_local(origin: dict) -> dict[str, str]:
    return {str(item.get("name")): str(item.get("value", "")) for item in origin.get("localStorage") or []}


def _registrable(value: str) -> str:
    """Normalize a cookie domain or an origin URL onto one comparable key.

    Cookie conflicts are keyed by domain (".example.com") while localStorage and
    IndexedDB are keyed by origin ("https://www.example.com"). Comparing those two
    shapes directly meant a cookie identity conflict never blocked the matching
    localStorage merge, so half of a conflicting identity was written anyway.
    """
    host = str(value or "").strip().lower()
    host = host.removeprefix("https://").removeprefix("http://").split("/", 1)[0]
    host = host.split(":", 1)[0].lstrip(".")
    if not host:
        return ""
    try:
        import tldextract

        found = tldextract.TLDExtract(suffix_list_urls=None)(host)
        registrable = ".".join(part for part in (found.domain, found.suffix) if part)
        return registrable or host
    except Exception:
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) > 2 else host


def _idb_identity_names(origin: dict) -> set[str]:
    """Names of IndexedDB databases that plausibly carry identity.

    Hashing the whole IndexedDB payload treated ordinary application churn (a mail
    client's message cache, a video site's watch history) as an identity conflict,
    which quarantined the origin on every checkpoint and stopped auth ever merging.
    Only database *names* that look identity-bearing participate in the decision.
    """
    names: set[str] = set()
    for database in origin.get("indexedDB") or []:
        name = str((database or {}).get("name") or "")
        if _AUTH_KEY.search(name):
            names.add(name)
    return names


def _sensitive_cookie(cookie: dict) -> bool:
    return bool(cookie.get("httpOnly")) or bool(_AUTH_KEY.search(str(cookie.get("name") or "")))


def merge_storage_state(base: dict, canonical: dict, current: dict) -> tuple[dict, list[dict]]:
    """Three-way auth merge. Conflicting identity origins fail closed."""
    base_cookies = {_cookie_key(c): c for c in base.get("cookies") or [] if c.get("expires") not in (None, -1, 0)}
    canonical_cookies = {_cookie_key(c): c for c in canonical.get("cookies") or [] if c.get("expires") not in (None, -1, 0)}
    current_cookies = {_cookie_key(c): c for c in current.get("cookies") or [] if c.get("expires") not in (None, -1, 0)}
    conflicts: list[dict] = []
    blocked_origins: set[str] = set()
    for key in set(base_cookies) | set(canonical_cookies) | set(current_cookies):
        before, latest, candidate = base_cookies.get(key), canonical_cookies.get(key), current_cookies.get(key)
        if latest != before and candidate != before and latest != candidate and _sensitive_cookie(candidate or latest or {}):
            domain = str((candidate or latest or {}).get("domain") or "")
            blocked_origins.add(_registrable(domain))
            conflicts.append({"origin": domain.lstrip("."), "kind": "cookie_identity_conflict", "key": str(key[0])})
    base_origins, canonical_origins, current_origins = _origins(base), _origins(canonical), _origins(current)
    for origin in set(base_origins) | set(canonical_origins) | set(current_origins):
        b, c, w = base_origins.get(origin, {}), canonical_origins.get(origin, {}), current_origins.get(origin, {})
        bl, cl, wl = _index_local(b), _index_local(c), _index_local(w)
        sensitive_conflict = any(
            cl.get(key) != bl.get(key) and wl.get(key) != bl.get(key) and cl.get(key) != wl.get(key)
            and _AUTH_KEY.search(key)
            for key in set(bl) | set(cl) | set(wl)
        )
        bi, ci, wi = _idb_identity_names(b), _idb_identity_names(c), _idb_identity_names(w)
        idb_conflict = ci != bi and wi != bi and ci != wi
        if sensitive_conflict or idb_conflict:
            blocked_origins.add(_registrable(origin))
            conflicts.append({"origin": origin, "kind": "indexeddb_identity_conflict" if idb_conflict else "local_storage_identity_conflict"})

    merged_cookies = dict(canonical_cookies)
    for key in set(base_cookies) | set(current_cookies):
        domain = str((current_cookies.get(key) or base_cookies.get(key) or {}).get("domain") or "")
        if _registrable(domain) in blocked_origins:
            continue
        if key not in current_cookies:
            merged_cookies.pop(key, None)
        elif current_cookies.get(key) != base_cookies.get(key):
            merged_cookies[key] = current_cookies[key]

    merged_origins = dict(canonical_origins)
    for origin in set(base_origins) | set(current_origins):
        if _registrable(origin) in blocked_origins:
            continue
        if origin not in current_origins:
            merged_origins.pop(origin, None)
        elif current_origins.get(origin) != base_origins.get(origin):
            merged_origins[origin] = current_origins[origin]
    return {"cookies": list(merged_cookies.values()), "origins": list(merged_origins.values())}, conflicts


class AuthStateStore:
    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or Path(DATA_DIR) / "browser_workspaces" / "auth")
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, owner: str) -> Path:
        return self.root / f"{_owner_key(owner)}.json"

    @property
    def migration_marker_path(self) -> Path:
        return self.root / "legacy-migration.json"

    def migration_status(self, owner: str) -> dict:
        marker: dict = {}
        if self.migration_marker_path.is_file():
            try:
                loaded = json.loads(self.migration_marker_path.read_text(encoding="utf-8"))
                marker = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                # A marker truncated by a crash must not make every later browser
                # call raise. Treat it as "not migrated" so the retry path can run.
                marker = {}
        claimed = str(marker.get("owner") or "")
        result = marker.get("result") if isinstance(marker.get("result"), dict) else {}
        return {
            "migrated": bool(marker.get("migrated_at")),
            "claimed_by_current_owner": bool(claimed and claimed == (owner or "")),
            "claimed_by_another_owner": bool(claimed and claimed != (owner or "")),
            "installed_profile_version": int(result.get("profile_generation") or 0),
            "previous_profile_backup": bool(result.get("previous_profile_backup")),
            "staging": False,
        }

    def mark_migrated(self, owner: str, result: dict) -> None:
        payload = {"owner": owner or "", "migrated_at": time.time(), "result": result}
        path = self.migration_marker_path
        fd, tmp = tempfile.mkstemp(prefix="migration-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False); fh.flush(); os.fsync(fh.fileno())
            os.chmod(tmp, 0o600); os.replace(tmp, path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def read(self, owner: str) -> dict:
        path = self._path(owner)
        if not path.is_file():
            return {"version": 0, "cookies": [], "origins": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, owner: str, state: dict) -> None:
        path = self._path(owner)
        fd, tmp = tempfile.mkstemp(prefix="auth-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False)
                fh.flush(); os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def quarantine(self, owner: str, workspace_id: str, current: dict, conflicts: list[dict]) -> str:
        """Persist conflicted auth privately without exposing values to the model/UI."""
        path = self.root / f"{_owner_key(owner)}-{workspace_id}-quarantine.json"
        payload = {"created_at": time.time(), "workspace_id": workspace_id, "conflicts": conflicts, "state": current}
        fd, tmp = tempfile.mkstemp(prefix="auth-quarantine-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
                fh.flush(); os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        return str(path)

    async def reconcile(self, owner: str, base: dict, current: dict) -> tuple[dict, list[dict]]:
        lock = self._locks.setdefault(_owner_key(owner), asyncio.Lock())
        async with lock:
            canonical = self.read(owner)
            merged, conflicts = merge_storage_state(base, canonical, current)
            merged["version"] = int(canonical.get("version") or 0) + 1
            self._write(owner, merged)
            return merged, conflicts

    async def checkpoint(self, owner: str, current: dict) -> dict:
        """Write a derived backup from the authoritative owner profile/context."""
        lock = self._locks.setdefault(_owner_key(owner), asyncio.Lock())
        async with lock:
            previous = self.read(owner)
            state = {
                "version": int(previous.get("version") or 0) + 1,
                "cookies": [cookie for cookie in current.get("cookies") or [] if cookie.get("expires") not in (None, -1, 0)],
                "origins": list(current.get("origins") or []),
                "checkpointed_at": time.time(),
                "source": "persistent_owner_profile",
            }
            self._write(owner, state)
            return state


def read_firefox_local_storage(profile: str) -> dict[str, list[dict[str, str]]]:
    """Read legacy Firefox localStorage without visiting any site."""
    path = Path(profile) / "webappsstore.sqlite"
    if not path.is_file():
        return {}
    result: dict[str, list[dict[str, str]]] = {}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for _attrs, _origin_key, scope, key, value in connection.execute(
            "SELECT originAttributes, originKey, scope, key, value FROM webappsstore2"
        ):
            origin = str(scope or "").rstrip("/")
            if origin.startswith(("http://", "https://")):
                result.setdefault(origin, []).append({"name": str(key), "value": str(value)})
    finally:
        connection.close()
    return result


def firefox_storage_origins(profile: str) -> tuple[set[str], set[str]]:
    origins, indexed = set(read_firefox_local_storage(profile)), set()
    root = Path(profile) / "storage" / "default"
    if root.is_dir():
        for entry in root.iterdir():
            match = re.match(r"^(https?)\+\+\+([^\^]+)", entry.name)
            if not match:
                continue
            origin = f"{match.group(1)}://{match.group(2).replace('+', ':')}"
            origins.add(origin)
            if any((entry / "idb").glob("*.sqlite")):
                indexed.add(origin)
    cookies_db = Path(profile) / "cookies.sqlite"
    if cookies_db.is_file():
        connection = sqlite3.connect(f"file:{cookies_db}?mode=ro", uri=True)
        try:
            for host, secure in connection.execute("SELECT DISTINCT host, isSecure FROM moz_cookies"):
                hostname = str(host or "").lstrip(".")
                if hostname:
                    origins.add(f"{'https' if secure else 'http'}://{hostname}")
        finally:
            connection.close()
    places_db = Path(profile) / "places.sqlite"
    if places_db.is_file():
        connection = sqlite3.connect(f"file:{places_db}?mode=ro", uri=True)
        try:
            for (url,) in connection.execute("SELECT DISTINCT url FROM moz_places WHERE url LIKE 'http%' LIMIT 10000"):
                parsed = urlparse(str(url or ""))
                if parsed.scheme in {"http", "https"} and parsed.netloc:
                    origins.add(f"{parsed.scheme}://{parsed.netloc}")
        finally:
            connection.close()
    return origins, indexed


class AuthMigrationError(RuntimeError):
    pass


def _fsync_directory(path: Path) -> None:
    """Make a profile rename durable before the migration marker can be written."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class AuthMigrator:
    """Networkless migration of cookies, localStorage and IndexedDB."""

    def __init__(self, store: AuthStateStore) -> None:
        self.store = store

    async def migrate(
        self,
        owner: str,
        *,
        profile: str | None = None,
        install_profile: str | None = None,
        replace_existing: bool = False,
    ) -> dict:
        source = profile or master_profile_dir()
        install_path = Path(install_profile).resolve() if install_profile else None
        if install_path:
            install_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = tempfile.mkdtemp(
            prefix="odysseus-auth-migration-",
            dir=str(install_path.parent) if install_path else None,
        )
        real_requests: list[str] = []
        try:
            # Coordinate with the legacy sign-in/profile-cloning surfaces, reject
            # a genuinely live Firefox, and clean only stale Firefox lock files.
            # The source lock is needed only for this immutable staging copy.
            with master_profile_lock(source):
                if profile_process_ids(source):
                    raise AuthMigrationError("Legacy browser profile is open; close it and retry migration.")
                clear_stale_profile_lock_files(source)
                if master_profile_in_use(source):
                    raise AuthMigrationError("Legacy browser profile is open; close it and retry migration.")
                shutil.copytree(
                    source, snapshot, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("lock", ".parentlock", "cache2", "startupCache"),
                )
            # Firefox refuses a profile whose compatibility.ini names a newer
            # application build. It is derived launch metadata, not auth state;
            # removing it lets the pinned Camoufox build regenerate a compatible
            # copy without touching cookies or origin storage.
            try:
                (Path(snapshot) / "compatibility.ini").unlink()
            except FileNotFoundError:
                pass
            origins, indexed_origins = firefox_storage_origins(snapshot)
            local = read_firefox_local_storage(snapshot)
            from camoufox.async_api import AsyncCamoufox
            # Numeric form avoids Camoufox 0.4.11 serializing Python ``True``
            # into the double-only humanize:maxTime browser property.
            cm = AsyncCamoufox(headless=True, humanize=1.5, user_data_dir=snapshot, persistent_context=True)
            context = await cm.__aenter__()
            intercepted: set[str] = set()

            async def route_handler(route, request):
                intercepted.add(str(request.url))
                if request.is_navigation_request():
                    await route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>migration</title>")
                else:
                    await route.abort()

            await context.route("**/*", route_handler)

            def audit(request: Any) -> None:
                # Routing is the control; this is the audit. Any request that
                # completed without passing through the handler reached the network,
                # which would mean the migration silently touched the user's live
                # accounts. Record it so the run fails closed below.
                url = str(getattr(request, "url", "") or "")
                if url and not url.startswith(("data:", "about:", "blob:")) and url not in intercepted:
                    real_requests.append(url)

            context.on("requestfinished", audit)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                unreachable: list[str] = []
                for origin in sorted(origins):
                    try:
                        await page.goto(origin, wait_until="commit", timeout=10000)
                    except Exception:
                        # One dead or renamed origin in a years-old profile must not
                        # cost the user every other login. Record it and keep going;
                        # the IndexedDB completeness check below still fails closed
                        # for any origin that actually carried identity state.
                        unreachable.append(origin)
                state = await context.storage_state(indexed_db=True)
            finally:
                await cm.__aexit__(None, None, None)
            exported = _origins(state)
            for origin, entries in local.items():
                exported.setdefault(origin, {"origin": origin})["localStorage"] = entries
            missing = sorted(origin for origin in indexed_origins if not (exported.get(origin) or {}).get("indexedDB"))
            if missing:
                raise AuthMigrationError(f"IndexedDB migration incomplete for {len(missing)} HTTP(S) origin(s).")
            state["cookies"] = [c for c in state.get("cookies") or [] if c.get("expires") not in (None, -1, 0)]
            state["origins"] = list(exported.values())
            # The legacy sidecar can contain state captured after the profile files were
            # last flushed. It only fills gaps; the complete profile export wins.
            from ascended_browser._app.browser_profile_coord import auth_storage_state_path
            sidecar_path = Path(auth_storage_state_path())
            if sidecar_path.is_file():
                try:
                    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    sidecar = {}
                cookie_keys = {_cookie_key(cookie) for cookie in state["cookies"]}
                for cookie in sidecar.get("cookies") or []:
                    if cookie.get("expires") not in (None, -1, 0) and _cookie_key(cookie) not in cookie_keys:
                        state["cookies"].append(cookie)
                        cookie_keys.add(_cookie_key(cookie))
                origin_map = _origins(state)
                for origin, payload in _origins(sidecar).items():
                    if origin not in origin_map:
                        state["origins"].append(payload)
            if local and not state["origins"]:
                raise AuthMigrationError("Local storage existed but migration exported no origins.")
            if real_requests:
                raise AuthMigrationError(
                    f"Migration attempted network access to {len(real_requests)} URL(s)."
                )
            state["version"] = 1
            backup = ""
            installed_new_profile = False
            if install_path:
                staging = Path(snapshot)
                for lock_name in ("lock", ".parentlock", ".odysseus-owner-runtime.lock"):
                    try: (staging / lock_name).unlink()
                    except FileNotFoundError: pass
                if install_path.exists() and any(install_path.iterdir()):
                    if not replace_existing:
                        raise AuthMigrationError("Owner profile already exists; explicit administrative replacement is required.")
                    backup_path = install_path.with_name(f"{install_path.name}.backup-{int(time.time())}")
                    os.replace(install_path, backup_path)
                    backup = str(backup_path)
                elif install_path.exists():
                    install_path.rmdir()
                try:
                    os.replace(staging, install_path)
                    installed_new_profile = True
                    snapshot = ""
                    _fsync_directory(install_path.parent)
                    self.store._write(owner, state)
                except Exception:
                    # Re-import must never strand the owner between profiles. Keep
                    # the rejected candidate for diagnosis, then put the previous
                    # authoritative profile back at its original path.
                    if installed_new_profile and install_path.exists():
                        failed_path = install_path.with_name(
                            f"{install_path.name}.failed-install-{int(time.time())}"
                        )
                        os.replace(install_path, failed_path)
                    if backup and Path(backup).exists():
                        os.replace(backup, install_path)
                        _fsync_directory(install_path.parent)
                    raise
            else:
                self.store._write(owner, state)
            return {
                "migrated": True,
                "cookies": len(state["cookies"]),
                "origins": len(state["origins"]),
                "indexed_origins": len(indexed_origins),
                "unreachable_origins": unreachable,
                "network_requests": len(real_requests),
                "profile_generation": 1,
                "previous_profile_backup": backup,
            }
        finally:
            if snapshot:
                shutil.rmtree(snapshot, ignore_errors=True)
