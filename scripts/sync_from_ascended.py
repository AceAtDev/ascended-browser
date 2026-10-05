"""Regenerate src/ascended_browser/_app from an Ascended checkout.

    python scripts/sync_from_ascended.py --source ~/odysseus

``_app`` is generated, never edited by hand: the browser modules are copied
as they are, the browser tool dispatcher and the result formatter are
extracted (only the definitions reachable from their entry points), and every
import of an Ascended module outside the browser code is rewritten to the
standalone ``ascended_browser.runtime`` package. Any import that has no
mapping stops the sync, so the package can never quietly reach back into the
app.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _closure import closure  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "ascended_browser" / "_app"
PKG = "ascended_browser"

# Ascended module -> standalone module. Longest prefixes are applied first.
MODULE_MAP = {
    "src.browser_workspace": f"{PKG}._app.browser_workspace",
    "src.user_labels": f"{PKG}._app.user_labels",
    "src.tool_implementations": f"{PKG}._app.dispatch",
    "src.tool_execution": f"{PKG}.runtime.tool_execution",
    "src.settings": f"{PKG}.runtime.settings",
    "src.constants": f"{PKG}.runtime.constants",
    "core.platform_compat": f"{PKG}.runtime.platform",
    "core.database": f"{PKG}.runtime.database",
    "routes.desktop_routes": f"{PKG}.runtime.desktop",
    "src.agent_runs": f"{PKG}.runtime.runs",
    "src.agent_tools.filesystem_tools": f"{PKG}.runtime.files",
    "src.agent_tools": f"{PKG}.runtime.agent_tools",
    "src.endpoint_resolver": f"{PKG}.runtime.llm",
    "src.llm_core": f"{PKG}.runtime.llm",
    "src.managed_tool_output": f"{PKG}.runtime.evidence",
    "src.session_sandbox": f"{PKG}.runtime.sandbox",
    "src.site_icons": f"{PKG}.runtime.noop",
    "src.tool_image_delivery": f"{PKG}.runtime.noop",
    "src.subagents.browser": f"{PKG}.runtime.subagents",
    "src.subagents.capabilities": f"{PKG}.runtime.subagents",
    "src.subagents.permission": f"{PKG}.runtime.permission",
    "src.text_helpers": f"{PKG}.runtime.text",
    "src.tool_registry": f"{PKG}.runtime.tool_registry",
    "src.url_security": f"{PKG}.runtime.url_security",
    "services.search.admission": f"{PKG}.runtime.admission",
}
APP_ROOTS = {"src", "core", "services", "routes"}

# (source module, entry points) extracted into one generated module each.
EXTRACTS = {
    "dispatch.py": ("src/tool_implementations.py", ["do_browser_workspace"]),
    "formatting.py": ("src/tool_execution.py", ["format_tool_result", "browser_result_archive",
                                                 "browser_result_context"]),
}


def rewrite(text: str) -> str:
    text = re.sub(r"\bfrom src import (browser_\w+)", rf"from {PKG}._app import \1", text)
    text = re.sub(r"(?<![\w.])src\.browser_", f"{PKG}._app.browser_", text)
    for old in sorted(MODULE_MAP, key=len, reverse=True):
        text = re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w])", MODULE_MAP[old], text)
    return text


def extract(source: Path, roots: list[str]) -> str:
    lines, nodes = closure(source, roots)
    head = '"""Generated from {} by scripts/sync_from_ascended.py: the definitions\nreachable from {}. Do not edit; edit Ascended and re-sync."""\n'.format(
        source.name, ", ".join(roots))
    parts = [head, "from __future__ import annotations\n"]
    for node in nodes:
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
        parts.append("\n".join(lines[start:node.end_lineno]))
    return "\n\n".join(parts) + "\n"


def settings_defaults(source: Path, generated: str) -> dict:
    """The app's DEFAULT_SETTINGS entries whose keys the generated code mentions.

    Read from the literal, not by importing the app (which would open its data).
    """
    tree = ast.parse((source / "src" / "settings.py").read_text())
    table = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "DEFAULT_SETTINGS" for t in node.targets))
    found = {}
    for key_node, value_node in zip(table.keys, table.values):
        try:
            key = ast.literal_eval(key_node)
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError, SyntaxError):
            continue
        if isinstance(key, str) and (f'"{key}"' in generated or f"'{key}'" in generated):
            found[key] = value
    return dict(sorted(found.items()))


TOOLS = [
    "browser_open", "browser_tabs", "browser_observe", "browser_act", "browser_extract",
    "browser_screenshot", "browser_viewport", "browser_evaluate", "browser_flow", "wait_for_bot_wall",
]
# Properties that need the app around them: a chat workspace to save into.
# Screenshots always come back to the client as a picture.
DROP = {"browser_extract": {"save_to", "schema", "already_collected"},
        # matrix captures several sizes through the app's live view, which this package lacks.
        "browser_screenshot": {"purpose", "matrix", "color_schemes"}}
ACTIONS: dict[str, list[str]] = {}
DESCRIBE = {
    "browser_viewport": (
        "Check a responsive layout in the current agent-owned tab. action=set with preset phone "
        "(390x844), tablet (768x1024) or desktop (1365x768), or width and height in CSS pixels, "
        "resizes the real browser window so the page reflows as it would on that screen (Linux; "
        "elsewhere the window keeps its size). action=restore returns to the launch size and clears "
        "emulation; call it when the check is done. action=get reads the current size. "
        "action=emulate sets color_scheme (light or dark), reduced_motion, forced_colors or offline. "
        "A successful set or restore includes a fresh page snapshot; earlier element positions are "
        "stale after a resize, so use the new refs."
    ),
}
# Wording that names a dropped property.
# Sentences about a dropped property, removed after REWORD (the calling agent
# is itself a model, so schema extraction by a second model is left out).
DROP_SENTENCES_WITH = {"browser_extract": ("save_to", "schema", "already_collected", "Use it before guessing CSS",
                                          "it saves the original archive", "Saved query/extraction scope")}
REWORD = {"browser_extract": [
    ("; target_ref limits it to one component, save_to adds icon SVGs and font sources",
     "; target_ref limits it to one component"),
], "browser_screenshot": [
    ("purpose=inspect only for", "Use it only for"),
    ("take purpose=inspect with compare_with", "take one with compare_with"),
    (", instead of saving evidence and stitching it yourself", ""),
    (", or checking a responsive layout after browser_viewport set)", ")"),
    ("; matrix captures presets (and schemes) into one labelled grid", ""),
]}
# Sentences that send the model to tools an MCP client does not have.
APP_ONLY = ("present_artifact", "ask_user", "browser_login", "bundled browser-use skill",
            "workspace file", "spawn_agent", "trigger_research", "deliverables/")


def tool_schemas(source: Path, python: Path) -> list[dict]:
    """The app's own function schemas for TOOLS, read in a throwaway process.

    The app is imported with a temporary data directory so the sync never
    touches a real install's settings or database.
    """
    script = (
        "import json, os, sys, tempfile\n"
        "d = tempfile.mkdtemp(); os.environ.update(DATA_DIR=d, ODYSSEUS_DATA_DIR=d, DATABASE_URL=f'sqlite:///{d}/x.db')\n"
        f"sys.path.insert(0, {str(source)!r}); os.chdir({str(source)!r})\n"
        "import src.agent_tools\n"
        "from src.tool_schemas import FUNCTION_TOOL_SCHEMAS\n"
        "print(json.dumps([s['function'] for s in FUNCTION_TOOL_SCHEMAS]))\n"
    )
    out = subprocess.run([str(python), "-c", script],
                         capture_output=True, text=True, check=True).stdout
    by_name = {fn["name"]: fn for fn in json.loads(out.strip().splitlines()[-1])}
    schemas = []
    for name in TOOLS:
        fn = by_name[name]
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        for prop in DROP.get(name, ()):
            params.get("properties", {}).pop(prop, None)
            if prop in params.get("required", []):
                params["required"].remove(prop)
        if name in ACTIONS and "action" in params.get("properties", {}):
            params["properties"]["action"]["enum"] = ACTIONS[name]
            params["properties"]["action"]["description"] = " ".join(ACTIONS[name])
        sentences = re.split(r"(?<=[.!?])\s+", DESCRIBE.get(name) or fn.get("description", ""))
        description = " ".join(s for s in sentences if not any(term in s for term in APP_ONLY)).strip()
        for old, new in REWORD.get(name, ()):
            description = description.replace(old, new)
        terms = DROP_SENTENCES_WITH.get(name, ())
        if terms:
            description = " ".join(s for s in re.split(r"(?<=[.!?])\s+", description)
                                   if not any(term in s for term in terms))
        for prop in params.get("properties", {}).values():
            if isinstance(prop, dict) and terms:
                prop["description"] = " ".join(
                    s for s in re.split(r"(?<=[.!?])\s+", str(prop.get("description", "")))
                    if not any(term in s for term in terms)) or prop.get("description", "")
        schemas.append({"name": name, "description": description, "inputSchema": params})
    return schemas


def leftover_app_imports(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        module = ""
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            module = node.module
        elif isinstance(node, ast.Import):
            module = next((a.name for a in node.names if a.name.split(".")[0] in APP_ROOTS), "")
        if module.split(".")[0] in APP_ROOTS:
            found.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source", type=Path, required=True, help="Ascended checkout")
    parser.add_argument("--ref", default="HEAD",
                        help="commit to generate from (default HEAD); the working tree is never read")
    options = parser.parse_args()
    checkout = options.source.expanduser().resolve()
    if not (checkout / "src" / "tool_implementations.py").is_file():
        sys.exit(f"{checkout} is not an Ascended checkout")
    commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", options.ref],
                            capture_output=True, text=True, check=True).stdout.strip()
    # Generate from a commit, not the working tree: someone may be mid-edit.
    export = Path(tempfile.mkdtemp(prefix="ascended-sync-"))
    # The app packages the schema export imports; the rest of the repo is not needed.
    archive = subprocess.run(["git", "-C", str(checkout), "archive", commit, "src", "core", "services", "routes"],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(export)], input=archive, check=True)
    source = export
    venv_python = checkout / "venv" / "bin" / "python"

    if APP.exists():
        shutil.rmtree(APP)
    (APP / "browser_workspace").mkdir(parents=True)
    copied = sorted((source / "src").glob("browser_*.py")) + sorted((source / "src" / "browser_workspace").glob("*.py"))
    copied.append(source / "src" / "user_labels.py")
    for path in copied:
        target = APP / path.relative_to(source / "src")
        target.write_text(rewrite(path.read_text()))
    shutil.copytree(source / "src" / "browser_vendor", APP / "browser_vendor")
    for name, (module, roots) in EXTRACTS.items():
        (APP / name).write_text(rewrite(extract(source / module, roots)))
    (APP / "__init__.py").write_text('"""Generated from Ascended; see scripts/sync_from_ascended.py."""\n')
    generated = "\n".join(path.read_text() for path in APP.rglob("*.py"))
    defaults = settings_defaults(source, generated)
    (APP / "settings_defaults.json").write_text(json.dumps(defaults, indent=1) + "\n")
    (APP / "tool_schemas.json").write_text(json.dumps(tool_schemas(source, venv_python if venv_python.exists() else Path(sys.executable)), indent=1) + "\n")

    problems = [line for path in APP.rglob("*.py") for line in leftover_app_imports(path)]
    if problems:
        sys.exit("Unmapped Ascended imports (add them to MODULE_MAP and runtime/):\n  " + "\n  ".join(problems))

    (APP / "SOURCE.json").write_text(json.dumps({
        "ascended_commit": commit, "files": len(list(APP.rglob("*.py"))),
    }, indent=2) + "\n")
    shutil.rmtree(export, ignore_errors=True)
    print(f"synced {len(copied)} modules + {len(EXTRACTS)} extracts from {commit[:10]}")


if __name__ == "__main__":
    main()
