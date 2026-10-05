"""The app's result formatter (extracted into _app.formatting) and workspace hooks."""
from __future__ import annotations

from .._app.formatting import browser_result_archive, format_tool_result  # noqa: F401


def get_active_workspace():
    """Chat workspaces belong to the Ascended app."""
    return None


def _resolve_tool_path(path):
    raise ValueError("save_to needs the Ascended app's workspace; it is not available here")
