"""Browser sub-agents are an Ascended app feature; every caller here is the main agent."""
from __future__ import annotations

from typing import Any


def current_browser_child() -> dict[str, Any]:
    return {}


def current_child_execution() -> dict[str, Any]:
    return {}


def resolve_browser_upload_ids(upload_ids: Any) -> list[str]:
    raise RuntimeError("upload_ids are only available to an Ascended browser sub-agent; pass file paths")
