"""The process-wide browser workspace manager, set by the server at startup."""
_manager = None


def set_browser_workspace_manager(manager) -> None:
    global _manager
    _manager = manager


def get_browser_workspace_manager():
    return _manager
