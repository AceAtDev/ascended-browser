"""Per-session folders under the data directory (screenshots saved as evidence)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .paths import data_dir


def _safe(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id or "default"))[:80] or "default"


def ensure_session_sandbox(session_id: str) -> Path:
    path = data_dir() / "sessions" / _safe(session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_session_sandbox(session_id: str) -> Optional[Path]:
    path = data_dir() / "sessions" / _safe(session_id)
    return path if path.is_dir() else None


def workspace_root(session_id: str) -> Optional[Path]:
    return None
