"""Where the package keeps its state."""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """``ASCENDED_DATA_DIR``, else ``$XDG_DATA_HOME/ascended/browser`` (``~/.local/share/...``)."""
    configured = os.environ.get("ASCENDED_DATA_DIR", "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        base = os.environ.get("XDG_DATA_HOME", "").strip() or str(Path.home() / ".local" / "share")
        root = Path(base) / "ascended" / "browser"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root
