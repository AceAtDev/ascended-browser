"""Small user-facing browser outcome summary; never substitutes model evidence."""


def browser_result_summary(result: dict) -> str:
    if result.get("error"):
        return str(result["error"])[:500]
    if result.get("detail"):
        return str(result["detail"])[:500]
    if result.get("summary"):
        return str(result["summary"])[:500]
    if result.get("saved_to"):
        return f"Saved browser evidence to {result['saved_to']}."
    page = result.get("page") if isinstance(result.get("page"), dict) else result
    if page.get("content_blocks"):
        return f"Observed {len(page['content_blocks'])} visible content blocks and {len(page.get('elements') or [])} controls."
    if result.get("tabs") is not None:
        return f"Browser tabs: {len(result['tabs'])}."
    if result.get("text"):
        return f"Read {len(str(result['text'])):,} characters from the browser."
    if result.get("result") is not None:
        return "Browser evaluation completed."
    return "Browser state updated." if result.get("success", True) else "Browser action failed."
