"""Public runtime scope surface."""

from __future__ import annotations

from shepherd_runtime._scope.scope import Scope as _Scope
from shepherd_runtime._scope.scope import ScopeProxy as _ScopeProxy
from shepherd_runtime._scope.scope import current_scope as _current_scope
from shepherd_runtime._scope.scope import require_scope as _require_scope
from shepherd_runtime.scope_bindings import (
    AmbiguousBindingError,
    NoBindingForTypeError,
    current_binding,
)

Scope = _Scope
ScopeProxy = _ScopeProxy
current_scope = _current_scope
require_scope = _require_scope

Scope.__module__ = __name__
ScopeProxy.__module__ = __name__
current_scope.__module__ = __name__
require_scope.__module__ = __name__

__all__ = [
    "AmbiguousBindingError",
    "NoBindingForTypeError",
    "Scope",
    "ScopeProxy",
    "current_binding",
    "current_scope",
    "require_scope",
]
