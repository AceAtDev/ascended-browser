"""Top-level definitions of a module reachable from root names, by name reference."""
import ast
from pathlib import Path


def _defined(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    return set()


def _used(node):
    return ({n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)})


def closure(path, roots):
    """(source lines, kept top-level nodes in file order)."""
    source = Path(path).read_text()
    tree = ast.parse(source)
    defs = {}
    for node in tree.body:
        for name in _defined(node):
            defs.setdefault(name, []).append(node)
    keep, todo = set(), list(roots)
    while todo:
        for node in defs.get(todo.pop(), []):
            if id(node) not in keep:
                keep.add(id(node))
                todo.extend(_used(node) & defs.keys())
    return source.splitlines(), [n for n in tree.body if id(n) in keep]
