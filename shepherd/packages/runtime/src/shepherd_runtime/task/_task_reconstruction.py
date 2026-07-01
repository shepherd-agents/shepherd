"""Runtime-private task reconstruction primitives."""

from __future__ import annotations

import ast
import uuid
from typing import TYPE_CHECKING, Any

from shepherd_runtime.task.reconstruction import ReconstructionError, reconstruct_task_class
from shepherd_runtime.task.source_validation import SourceValidationError, validate_task_source

from ._source_state import reconstruction_source

if TYPE_CHECKING:
    from types import ModuleType

try:
    from pydantic import ValidationError as PydanticValidationError
except ImportError:
    PydanticValidationError = None  # type: ignore[misc,assignment]

__all__ = [
    "SECURE_ALLOWED_DUNDERS",
    "SECURE_ALLOWED_MODULES",
    "SECURE_FORBIDDEN_NAMES",
    "SHEPHERD_IMPORTS",
    "STANDARD_IMPORTS",
    "ReconstructionError",
    "reconstruct_task_class",
    "secure_reconstruct_task_class",
]


STANDARD_IMPORTS = [
    "from __future__ import annotations",
    "from pydantic import BaseModel, Field",
    "from typing import Any, Literal, Optional, Union, Annotated",
]

SHEPHERD_IMPORTS = [
    "from shepherd_runtime.task.authoring import Artifact, Context, Input, Output, task",
]

SECURE_FORBIDDEN_NAMES = frozenset(
    {
        "__import__",
        "__builtins__",
        "__loader__",
        "__spec__",
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "globals",
        "locals",
        "vars",
        "dir",
    }
)

SECURE_ALLOWED_DUNDERS = frozenset(
    {
        "__init__",
        "__str__",
        "__repr__",
        "__eq__",
        "__hash__",
        "__name__",
        "__doc__",
        "__module__",
        "__qualname__",
        "__class__",
        "__dict__",
        "__annotations__",
    }
)

SECURE_ALLOWED_MODULES = frozenset(
    {
        "pydantic",
        "typing",
        "typing_extensions",
        "shepherd_core",
        "shepherd_runtime",
        "dataclasses",
        "enum",
        "abc",
        "functools",
        "collections",
        "re",
    }
)


def _check_restricted_python_available() -> bool:
    """Check if RestrictedPython is available."""
    import importlib.util

    return importlib.util.find_spec("RestrictedPython") is not None


def secure_reconstruct_task_class(
    source: str,
    imports: list[str] | None = None,
    extra_namespace: dict[str, Any] | None = None,
    *,
    allowed_imports: frozenset[str] = frozenset(),
) -> type:
    """Reconstruct a @task class with RestrictedPython sandboxing."""
    if not _check_restricted_python_available():
        raise ImportError(
            "RestrictedPython is required for secure reconstruction. Install with: pip install RestrictedPython"
        )

    from RestrictedPython import RestrictingNodeTransformer, compile_restricted_exec  # type: ignore[import-untyped]
    from RestrictedPython.Eval import default_guarded_getitem, default_guarded_getiter  # type: ignore[import-untyped]
    from RestrictedPython.Guards import guarded_iter_unpack_sequence, safer_getattr  # type: ignore[import-untyped]
    from RestrictedPython.transformer import INSPECT_ATTRIBUTES, copy_locations  # type: ignore[import-untyped]

    violations = validate_task_source(source)
    if violations:
        raise SourceValidationError(violations)

    class PydanticSafeNodeTransformer(RestrictingNodeTransformer):  # type: ignore[misc]
        """Extended transformer that supports Pydantic's annotated assignments."""

        def check_name(self, node, name, allow_magic_methods=False):  # type: ignore[no-untyped-def]  # noqa: ANN202
            if name is None:
                return
            if name in SECURE_FORBIDDEN_NAMES:
                self.error(node, f'"{name}" is a reserved/forbidden name.')
                return
            if name.endswith("__roles__"):
                self.error(node, f'"{name}" is invalid (ends with __roles__).')
                return
            if name.startswith("__") and name.endswith("__") and name not in SECURE_ALLOWED_DUNDERS:
                self.error(node, f'"{name}" is not in the allowed dunder methods.')

        def visit_Attribute(self, node):  # type: ignore[no-untyped-def]  # noqa: ANN202
            if node.attr.endswith("__roles__"):
                self.error(node, f'"{node.attr}" is invalid (ends with __roles__).')

            if node.attr in INSPECT_ATTRIBUTES:
                self.error(node, f'"{node.attr}" is a restricted inspection attribute.')

            if node.attr.startswith("__") and node.attr.endswith("__") and node.attr not in SECURE_ALLOWED_DUNDERS:
                self.error(node, f'"{node.attr}" is not in the allowed dunder attributes.')

            if isinstance(node.ctx, ast.Load):
                node = self.node_contents_visit(node)
                new_node = ast.Call(
                    func=ast.Name("_getattr_", ast.Load()),
                    args=[node.value, ast.Constant(node.attr)],
                    keywords=[],
                )
                copy_locations(new_node, node)
                return new_node

            if isinstance(node.ctx, (ast.Store, ast.Del)):
                node = self.node_contents_visit(node)
                new_value = ast.Call(func=ast.Name("_write_", ast.Load()), args=[node.value], keywords=[])
                copy_locations(new_value, node.value)
                node.value = new_value
                return node

            raise NotImplementedError(f"Unknown ctx type: {type(node.ctx)}")

        def visit_AnnAssign(self, node):  # type: ignore[no-untyped-def]  # noqa: ANN202
            return self.node_contents_visit(node)

        def visit_TypeAlias(self, node):  # type: ignore[no-untyped-def]  # noqa: ANN202
            return self.node_contents_visit(node)

        def visit_TypeVar(self, node):  # type: ignore[no-untyped-def]  # noqa: ANN202
            return self.node_contents_visit(node)

        def visit_TypeVarTuple(self, node):  # type: ignore[no-untyped-def]  # noqa: ANN202
            return self.node_contents_visit(node)

        def visit_ParamSpec(self, node):  # type: ignore[no-untyped-def]  # noqa: ANN202
            return self.node_contents_visit(node)

        def visit_AsyncFunctionDef(self, node):  # type: ignore[no-untyped-def]  # noqa: ANN202
            return self.node_contents_visit(node)

        def visit_AsyncFor(self, node):  # type: ignore[no-untyped-def]  # noqa: ANN202
            return self.node_contents_visit(node)

        def visit_AsyncWith(self, node):  # type: ignore[no-untyped-def]  # noqa: ANN202
            return self.node_contents_visit(node)

        def visit_Await(self, node):  # type: ignore[no-untyped-def]  # noqa: ANN202
            return self.node_contents_visit(node)

    byte_code = compile_restricted_exec(
        source,
        filename="<restricted_task>",
        policy=PydanticSafeNodeTransformer,
    )
    if byte_code.errors:
        raise SourceValidationError(list(byte_code.errors))

    from pydantic import BaseModel, Field

    from shepherd_runtime.task.authoring import Artifact, Context, Input, Output, task

    safe_builtins = {
        "True": True,
        "False": False,
        "None": None,
        "__build_class__": __build_class__,
        "isinstance": isinstance,
        "issubclass": issubclass,
        "type": type,
        "object": object,
        "super": super,
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "list": list,
        "dict": dict,
        "set": set,
        "tuple": tuple,
        "len": len,
        "range": range,
        "enumerate": enumerate,
        "zip": zip,
        "map": map,
        "filter": filter,
        "sorted": sorted,
        "min": min,
        "max": max,
        "sum": sum,
        "all": all,
        "any": any,
        "repr": repr,
        "hash": hash,
        "callable": callable,
        "getattr": getattr,
        "setattr": setattr,
        "hasattr": hasattr,
        "property": property,
        "staticmethod": staticmethod,
        "classmethod": classmethod,
        "Exception": Exception,
        "ValueError": ValueError,
        "TypeError": TypeError,
        "KeyError": KeyError,
        "AttributeError": AttributeError,
        "RuntimeError": RuntimeError,
    }

    effective_allowed_modules = SECURE_ALLOWED_MODULES | allowed_imports

    def restricted_import(name, globals=None, locals=None, fromlist=(), level=0) -> ModuleType:  # type: ignore[no-untyped-def]
        root_module = name.split(".")[0]
        if root_module in effective_allowed_modules or name in effective_allowed_modules:
            return __import__(name, globals, locals, fromlist, level)
        raise ImportError(f"Import of '{name}' is not allowed in restricted mode")

    safe_builtins["__import__"] = restricted_import

    module_name = f"shepherd_restricted_{uuid.uuid4().hex[:8]}"
    _globals = {
        "__builtins__": safe_builtins,
        "__name__": module_name,
        "__doc__": None,
        "__metaclass__": type,
        "_getattr_": safer_getattr,
        "_getitem_": default_guarded_getitem,
        "_getiter_": default_guarded_getiter,
        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        "_write_": lambda x: x,
        "BaseModel": BaseModel,
        "Field": Field,
        "task": task,
        "Input": Input,
        "Output": Output,
        "Context": Context,
        "Artifact": Artifact,
        "Any": Any,
        "Literal": __import__("typing").Literal,
        "Optional": __import__("typing").Optional,
        "Union": __import__("typing").Union,
        "Annotated": __import__("typing").Annotated,
    }

    if extra_namespace:
        _globals.update(extra_namespace)

    token = reconstruction_source.set(source)
    try:
        exec(byte_code.code, _globals)  # noqa: S102
    finally:
        reconstruction_source.reset(token)

    for obj in _globals.values():
        if isinstance(obj, type) and hasattr(obj, "_task_meta"):
            return obj

    raise ReconstructionError(
        error_type="MISSING_TASK_DECORATOR",
        message="No @task class found in restricted source",
        suggestion="Ensure the source includes a class decorated with @task.",
    )
