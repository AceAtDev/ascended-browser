_BROWSER_TOOLS = frozenset({
    "browser_open", "browser_tabs", "browser_viewport", "browser_act", "browser_observe",
    "browser_extract", "browser_evaluate", "browser_login", "browser_screenshot", "browser_flow",
    "browser_wait", "browser_resume", "browser_attention_resolve", "browser_workspace_status",
    "wait_for_bot_wall",
})


def browser_tool_names() -> frozenset[str]:
    return _BROWSER_TOOLS
