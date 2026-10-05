"""Full copies of long tool results, readable again with browser_extract evidence_ref.

The server clips a long result for the client and stores the whole of it
here; ``browser_extract {"evidence_ref": ..., "cursor": n}`` pages through it.
References are opaque and scoped to one server session.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Optional

from .sandbox import ensure_session_sandbox, get_session_sandbox

SCHEME = "evidence://"
_NAME = re.compile(r"[a-f0-9]{32}\.txt")


def store_text(session_id: str, text: str) -> str:
    folder = ensure_session_sandbox(session_id) / "evidence"
    folder.mkdir(exist_ok=True)
    name = f"{uuid.uuid4().hex}.txt"
    (folder / name).write_text(text, encoding="utf-8")
    return SCHEME + name


def resolve_managed_output_reference(session_id: str, reference: str) -> Optional[Path]:
    name = str(reference or "").strip()
    name = name[len(SCHEME):] if name.startswith(SCHEME) else ""
    if not _NAME.fullmatch(name):
        return None
    root = get_session_sandbox(session_id)
    path = (root / "evidence" / name) if root else None
    return path if path and path.is_file() else None
