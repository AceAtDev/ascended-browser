"""Native supervised browser workspaces."""

from .manager import BrowserWorkspaceManager
from .backend import BrowserBackend, PersistentOwnerBackend
from .models import Attention, TabLease, TabRecord, WorkspaceRecord

__all__ = ["Attention", "BrowserBackend", "BrowserWorkspaceManager", "PersistentOwnerBackend", "TabLease", "TabRecord", "WorkspaceRecord"]
