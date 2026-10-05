from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from ascended_browser._app.browser_workspace.models import Attention, TabHold, TabLease, TabRecord, WorkspaceRecord
from ascended_browser.runtime.constants import DATA_DIR


class WorkspaceStore:
    """Atomic durable manifests; live Playwright objects never enter persistence."""

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or Path(DATA_DIR) / "browser_workspaces")
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def directory(self, workspace_id: str) -> Path:
        path = self.root / workspace_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, record: WorkspaceRecord) -> None:
        record.updated_at = __import__("time").time()
        path = self.directory(record.workspace_id) / "manifest.json"
        payload = asdict(record)
        # Live JPEG frames are cached in memory and re-derived on wake. Persisting
        # them turned every browser action into a multi-megabyte fsync, because the
        # capture loop refreshes last_screenshot roughly once a second.
        for tab in (payload.get("tabs") or {}).values():
            tab.pop("last_screenshot", None)
        with self._lock:
            fd, temp_path = tempfile.mkstemp(prefix="manifest-", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(temp_path, path)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

    def load(self, workspace_id: str) -> WorkspaceRecord | None:
        path = self.root / workspace_id / "manifest.json"
        if not path.is_file():
            return None
        with self._lock, path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["tabs"] = {
            key: TabRecord(**{
                **value,
                # The first activity-aware build must interpret an older
                # manifest's most recent durable update as its initial use
                # time.  Dataclass defaults would otherwise stamp it "now" on
                # every load and keep an already-idle legacy tab resident for
                # another full policy interval.
                "last_active_at": value.get("last_active_at", value.get("updated_at", 0)),
                "lease": TabLease(**value["lease"]) if value.get("lease") else None,
                "hold": TabHold(**value["hold"]) if value.get("hold") else None,
            })
            for key, value in (raw.get("tabs") or {}).items()
        }
        raw["attentions"] = {
            key: Attention(**value) for key, value in (raw.get("attentions") or {}).items()
        }
        return WorkspaceRecord(**raw)

    def load_owner(self, owner: str) -> list[WorkspaceRecord]:
        """Load every persisted logical workspace for crash fan-out recovery."""
        records: list[WorkspaceRecord] = []
        for manifest in self.root.glob("*/manifest.json"):
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(raw.get("owner") or "") != (owner or ""):
                continue
            record = self.load(str(raw.get("workspace_id") or manifest.parent.name))
            if record is not None:
                records.append(record)
        return records

    def load_owner_key(self, key: str) -> list[WorkspaceRecord]:
        records: list[WorkspaceRecord] = []
        for manifest in self.root.glob("*/manifest.json"):
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            owner = str(raw.get("owner") or "")
            if hashlib.sha256((owner or "default").encode()).hexdigest()[:24] != key:
                continue
            record = self.load(str(raw.get("workspace_id") or manifest.parent.name))
            if record is not None:
                records.append(record)
        return records

    def session_has_tabs(self, session_id: str) -> bool:
        """Does this chat own browser tabs on disk, ignoring the in-memory cache?

        The manager's _records is populated lazily, so right after a restart it
        is empty even for a chat with open tabs. Tool selection asks this to
        decide whether the browser tools must be offered, and reading the
        manifest is the only answer that survives a restart.
        """
        if not session_id:
            return False
        for manifest in self.root.glob("*/manifest.json"):
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(raw.get("session_id") or "") != session_id:
                continue
            if raw.get("tabs"):
                return True
            if str(raw.get("status") or "") in {"active", "waking", "draining", "needs_recovery"}:
                return True
        return False

    def load_session(self, session_id: str) -> list[WorkspaceRecord]:
        """Durably load every workspace manifest this chat session owns.

        Identity-hydration reads this instead of the manager's lazy in-memory
        cache, so a turn that starts after a restart still knows which tab ids
        this session's context can name.
        """
        if not session_id:
            return []
        records: list[WorkspaceRecord] = []
        for manifest in self.root.glob("*/manifest.json"):
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(raw.get("session_id") or "") != session_id:
                continue
            record = self.load(str(raw.get("workspace_id") or manifest.parent.name))
            if record is not None:
                records.append(record)
        return records

    def append_journal(self, workspace_id: str, event: dict) -> None:
        path = self.directory(workspace_id) / "journal.jsonl"
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            if str(event.get("event") or "").startswith("browser_action_"):
                fh.flush()
                os.fsync(fh.fileno())

    def incomplete_actions(self, workspace_id: str) -> list[dict]:
        """Reconstruct interrupted attempts without replaying a possible effect.

        Legacy journal entries contain no dispatch evidence and are not promoted
        to verified outcomes. A torn final line leaves its preceding dispatch
        unresolved; reading this method never changes external browser state.
        """
        path = self.root / workspace_id / "journal.jsonl"
        if not path.is_file():
            return []
        pending: dict[str, dict] = {}
        with self._lock, path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                operation_id = event.get("operation_id")
                if not operation_id:
                    continue
                kind = event.get("event")
                if kind == "browser_action_attempt":
                    pending[operation_id] = {**event, "effect_state": "not_dispatched", "retry_safe": True}
                elif kind == "browser_action_dispatch":
                    pending[operation_id] = {**pending.get(operation_id, {}), **event,
                                             "effect_state": "uncertain", "retry_safe": False}
                elif kind == "browser_action_outcome":
                    pending.pop(operation_id, None)
        return list(pending.values())

    def artifact_manifest(self, workspace_id: str) -> str:
        return str(self.directory(workspace_id) / "links.json")

    def _native_history_path(self, owner: str) -> Path:
        # Environment roots already isolate hosts. Hash the exact authenticated
        # owner, including the empty owner, rather than using it as a path.
        key = hashlib.sha256(owner.encode()).hexdigest()
        return self.root / "native_history" / f"{key}.json"

    def _native_history(self, owner: str) -> dict:
        path = self._native_history_path(owner)
        if not path.exists():
            return {"version": 1, "owner": owner, "visits": {}}
        return self._read_native_history(path, owner)

    @staticmethod
    def _read_native_history(path: Path, owner: str) -> dict:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(raw, dict) or raw.get("version") != 1
                or raw.get("owner") != owner or not isinstance(raw.get("visits"), dict)):
            raise ValueError("Native browser history ownership/format mismatch")
        for url, item in raw["visits"].items():
            if (not isinstance(url, str) or not isinstance(item, dict)
                    or not isinstance(item.get("title"), str)
                    or type(item.get("visit_count")) is not int or item["visit_count"] < 1
                    or type(item.get("last_visited")) is not int or item["last_visited"] < 0):
                raise ValueError("Native browser history ownership/format mismatch")
        return raw

    def record_native_visit(self, owner: str, url: str, title: str = "", *, visit: bool = True) -> None:
        """Persist observed native main-frame visits; metadata refresh is not a visit."""
        from ascended_browser._app.browser_workspace.omnibox import _safe_history_url

        url = _safe_history_url(url)
        if not url:
            return
        with self._lock:
            raw = self._native_history(owner)
            visits = raw["visits"]
            previous = visits.get(url)
            title = str(title or "")[:200]
            if previous and not visit and (not title or title == previous.get("title")):
                return
            visits[url] = {
                "title": title or (previous or {}).get("title", ""),
                "visit_count": int((previous or {}).get("visit_count", 0)) + int(visit or not previous),
                "last_visited": time.time_ns() // 1000 if visit or not previous else previous["last_visited"],
            }
            # History is bounded independently of long-running chat journals.
            raw["visits"] = dict(sorted(visits.items(), key=lambda pair: pair[1]["last_visited"], reverse=True)[:500])
            path = self._native_history_path(owner)
            path.parent.mkdir(exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix="history-", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(raw, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def native_history_matches(self, owner: str, query: str = "") -> list[dict]:
        from ascended_browser._app.browser_workspace.omnibox import history_matches_from_rows

        text = str(query or "").strip()[:180]
        with self._lock:
            visits = self._native_history(owner)["visits"]
        folded = text.casefold()
        rows = [(url, item["title"], item["visit_count"], item["last_visited"])
                for url, item in visits.items()
                if not folded or folded in f"{url} {item['title']}".casefold()]
        rows.sort(key=lambda row: row[3] if text else (row[2], row[3]), reverse=True)
        return history_matches_from_rows(rows, text)

    def stage_native_history(self, owner: str) -> Path | None:
        """Quarantine the reusable owner key before committing an account change."""
        with self._lock:
            source = self._native_history_path(owner)
            if not source.exists():
                return None
            quarantine = source.parent / "quarantine"
            quarantine.mkdir(exist_ok=True)
            fd, filename = tempfile.mkstemp(prefix="retired-", suffix=".json", dir=quarantine)
            os.close(fd)
            staged = Path(filename)
            try:
                os.replace(source, staged)
            except BaseException:
                staged.unlink(missing_ok=True)
                raise
            return staged

    def restore_native_history(self, owner: str, staged: Path | None) -> None:
        """Restore only when the original account mutation was rejected."""
        if staged is not None:
            with self._lock:
                os.replace(staged, self._native_history_path(owner))

    def discard_staged_native_history(self, staged: Path | None) -> None:
        if staged is not None:
            with self._lock:
                staged.unlink(missing_ok=True)

    def commit_native_history_rename(self, owner: str, new_owner: str, staged: Path | None) -> None:
        """Publish matching new-owner data; retain quarantine until finalization."""
        with self._lock:
            destination = self._native_history_path(new_owner)
            if staged is None:
                destination.unlink(missing_ok=True)
                return
            raw = self._read_native_history(staged, owner)
            raw["owner"] = new_owner
            fd, temporary = tempfile.mkstemp(prefix="history-", suffix=".tmp", dir=destination.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(raw, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def delete_native_history(self, owner: str) -> None:
        """Remove an owner key, including rollback of a published rename."""
        with self._lock:
            self._native_history_path(owner).unlink(missing_ok=True)

    def rename_native_history(self, owner: str, new_owner: str) -> None:
        if owner == new_owner:
            return
        staged = self.stage_native_history(owner)
        self.commit_native_history_rename(owner, new_owner, staged)
        self.discard_staged_native_history(staged)
