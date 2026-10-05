import re

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def strip_think(text: str, *, prose: bool = False, prompt_echo: bool = True) -> str:
    """Drop <think>...</think> blocks from model output."""
    return _THINK.sub("", str(text or "")).strip()
