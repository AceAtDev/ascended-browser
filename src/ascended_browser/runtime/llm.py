"""Model calls. Schema-guided extraction asks a model in the app; here there is none."""
from __future__ import annotations


class NoModelConfigured(RuntimeError):
    pass


def resolve_endpoint(*_args, **_kwargs):
    """No endpoint: callers then report that no model is configured, in their own words."""
    return "", "", {}


async def llm_call_async(*_args, **_kwargs):
    raise NoModelConfigured("no model endpoint is configured in ascended-browser")
