"""Shared facade introspection for the prototype generators (DESIGN §5.3).

Reads the REAL repo facade statically — ast for the export/import map (never
imports product code; the repo-root exec shim makes imports unsafe), griffe
static loading for kinds/signatures/docstrings. Read-only on the main repo.

Retarget knobs (DESIGN S7): flip FACADE_IMPORT/FACADE_INIT/SEARCH_PATHS when
the `shepherd` package lands.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

PROTO_ROOT = Path(__file__).resolve().parents[3] # repo root: docs_system/scripts/ ->.. ->.. -> repo root
REPO_ROOT = PROTO_ROOT # trees (docs/shepherd, docs/_src/shepherd, _generated) + shepherd/ all live at the repo root

FACADE_IMPORT = "shepherd"
FACADE_INIT = REPO_ROOT / "shepherd/packages/meta/src/shepherd/__init__.py"
SEARCH_PATHS = [
    REPO_ROOT / "shepherd/packages/meta/src",
    REPO_ROOT / "shepherd/packages/runtime/src",
    REPO_ROOT / "shepherd/packages/core/src",
]

# The lazy substrate-handle surface (``shepherd_dialect.workspace_control``,
# resolved through the facade's PEP 562 ``__getattr__``) pulls ``vcs_core`` and
# ``pygit2`` on import. The docs build is deliberately import-light and offline,
# so those symbols cannot be rendered here; the generated reference documents the
# nucleus that resolves from SEARCH_PATHS. See the facade's own module docstring.
DEFERRED_SOURCE_PREFIX = "shepherd_dialect"

API_DIR = PROTO_ROOT / "docs/shepherd/reference/api"
MAP_FILE = API_DIR / "_map.yml"
SNAPSHOT = PROTO_ROOT / "docs/_generated/shepherd/python-api/public-symbols.json"


def facade_map() -> tuple[list[str], dict[str, str]]:
    """(exports, {name: one-hop 'module.name' target}) — parsed with ast."""
    tree = ast.parse(FACADE_INIT.read_text(encoding="utf-8"))
    exports: list[str] = []
    source: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                source[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            exports = [e.value for e in getattr(node.value, "elts", []) if isinstance(e, ast.Constant)]
    exports = [
        e
        for e in exports
        if not e.startswith("__")
        and not source.get(e, "").startswith(DEFERRED_SOURCE_PREFIX)
    ]
    return exports, source


def deferred_exports() -> tuple[list[str], dict[str, str]]:
    """(names, {name: 'module.name'}) for __all__ exports whose source is the
    lazily-imported handle surface (``shepherd_dialect``). These are real public
    symbols; the import-light docs build cannot griffe-render them (importing
    ``shepherd_dialect`` pulls ``vcs_core``/``pygit2``), so the inventory lists
    them as runtime-resolved rather than dropping them."""
    tree = ast.parse(FACADE_INIT.read_text(encoding="utf-8"))
    exports: list[str] = []
    source: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                source[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            exports = [e.value for e in getattr(node.value, "elts", []) if isinstance(e, ast.Constant)]
    deferred = [
        e for e in exports
        if not e.startswith("__") and source.get(e, "").startswith(DEFERRED_SOURCE_PREFIX)
    ]
    return deferred, source


def _fmt_sig(obj) -> str:
    kind = obj.kind.value
    if kind == "function":
        parts = []
        for p in obj.parameters:
            s = p.name
            if p.annotation is not None:
                s += f": {p.annotation}"
            if p.default is not None:
                s += f" = {p.default}"
            parts.append(s)
        ret = f" -> {obj.returns}" if obj.returns is not None else ""
        return f"{obj.name}({', '.join(parts)}){ret}"
    if kind == "class":
        bases = ", ".join(str(b) for b in obj.bases)
        return f"class {obj.name}({bases})" if bases else f"class {obj.name}"
    ann = f": {obj.annotation}" if getattr(obj, "annotation", None) is not None else ""
    return f"{obj.name}{ann}"


def symbol_info() -> list[dict]:
    """Per-symbol {name, source, kind, signature, doc_hash}, griffe-static."""
    import griffe

    exports, source = facade_map()
    roots: dict[str, object] = {}

    def load_root(pkg: str):
        if pkg not in roots:
            roots[pkg] = griffe.load(
                pkg, search_paths=[str(p) for p in SEARCH_PATHS], allow_inspection=False
            )
        return roots[pkg]

    out = []
    for name in exports:
        target = source.get(name, f"{FACADE_IMPORT}.{name}")
        pkg, _, inner = target.partition(".")
        info = {"name": name, "source": target, "target": target, "kind": "unresolved", "signature": "", "doc_hash": ""}
        try:
            obj = load_root(pkg)[inner]
            if obj.is_alias:
                obj = obj.final_target
            # Disambiguate module/function name shadowing (e.g. nucleus.workspace
            # is a submodule AND re-exports its same-named function): descend.
            if obj.kind.value == "module" and name in obj.members:
                obj = obj[name]
                if obj.is_alias:
                    obj = obj.final_target
                info["target"] = obj.canonical_path
            doc = obj.docstring.value if obj.docstring else ""
            info.update(
                kind=obj.kind.value,
                signature=_fmt_sig(obj),
                doc_hash=hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16],
            )
        except Exception as exc: # keep generation total; surface in the page/snapshot
            info["signature"] = f"<unresolved: {type(exc).__name__}>"
        out.append(info)
    # The lazily-imported handle/grant surface (per-binding grants, workspace
    # run/output/settlement nouns). Real public exports; listed here as
    # runtime-resolved because the offline docs build cannot import them.
    deferred, dsource = deferred_exports()
    for name in deferred:
        target = dsource.get(name, f"{FACADE_IMPORT}.{name}")
        out.append({
            "name": name,
            "source": target,
            "target": target,
            "kind": "handle-surface (runtime-resolved)",
            "signature": "",
            "doc_hash": "",
        })
    return out


def page_filename(name: str, all_names: list[str]) -> str:
    """NTFS-safe: case-insensitive collisions suffix the capitalized one."""
    twins = [n for n in all_names if n.lower() == name.lower()]
    if len(twins) > 1 and name[:1].isupper():
        return f"{name}-class.md"
    return f"{name}.md"
