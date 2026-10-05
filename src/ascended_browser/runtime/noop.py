"""App presentation features with nothing to present to here."""


async def watch_page_icons(*_args, **_kwargs) -> None:
    """Favicons are drawn by the Ascended app's tab strip."""
    return None


def materialize_tool_visual_previews(*_args, **_kwargs) -> list:
    """Chat-row screenshot previews belong to the Ascended app."""
    return []
