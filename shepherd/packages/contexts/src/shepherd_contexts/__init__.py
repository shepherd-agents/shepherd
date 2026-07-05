"""Shepherd Contexts - Generic Execution Contexts for Shepherd Framework.

This package provides generic execution contexts that can be used with
the shepherd-core framework. Each context is a complete implementation
of the ExecutionContext protocol.

Available Contexts
------------------
- WorkspaceRef: Git-backed workspace with capability-based access control
- SessionState: Invisible context for multi-turn conversation continuity
- MCPServerContext: External MCP server integration
- DatabaseContext: Read-only SQL database access
- KVStoreContext: Simple key-value store
- AppStoreContext: App Store Connect API access

Quick Start
-----------
    from shepherd_contexts.workspace import WorkspaceRef
    from shepherd_contexts.session import SessionState
    from shepherd_contexts.mcp import MCPServerContext
    from shepherd_contexts.database import DatabaseContext
    from shepherd_contexts.kvstore import KVStoreContext
    from shepherd_contexts.appstore import AppStoreContext

    # Create a workspace context
    workspace = WorkspaceRef.from_path("/path/to/repo")

    # Create a session context
    session = SessionState()

    # Create an MCP server context
    mcp = MCPServerContext(
        name="filesystem",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem", "/projects"),
    )

Effects
-------
Each context module also exports its domain-specific effects:

- workspace: WorkspacePatchCaptured, BashCommand
- session: SessionCreated, SessionForked, SessionResumed
- mcp: MCPServerConnected, MCPToolCalled
- database: QueryExecuted
- kvstore: KeySet, KeyDeleted
- appstore: AppStoreAPICall
"""

from __future__ import annotations

__version__ = "0.2.0"

# Workspace context
# App Store context
from shepherd_contexts.appstore import (
    AppStoreAPICall,
    AppStoreContext,
)

# Database context
from shepherd_contexts.database import (
    DatabaseContext,
    QueryExecuted,
)

# KVStore context
from shepherd_contexts.kvstore import (
    KeyDeleted,
    KeySet,
    KVStoreContext,
)

# MCP Server context
from shepherd_contexts.mcp import (
    MCPServerConnected,
    MCPServerContext,
    MCPToolCalled,
)

# Session context
from shepherd_contexts.session import (
    SessionCreated,
    SessionForked,
    SessionResumed,
    SessionState,
)
from shepherd_contexts.workspace import (
    BashCommand,
    DiffPatch,
    WorkspacePatchCaptured,
    WorkspaceRef,
)

__all__ = [
    "AppStoreAPICall",
    # App Store
    "AppStoreContext",
    "BashCommand",
    # Database
    "DatabaseContext",
    "DiffPatch",
    # KVStore
    "KVStoreContext",
    "KeyDeleted",
    "KeySet",
    "MCPServerConnected",
    # MCP
    "MCPServerContext",
    "MCPToolCalled",
    "QueryExecuted",
    "SessionCreated",
    "SessionForked",
    "SessionResumed",
    # Session
    "SessionState",
    "WorkspacePatchCaptured",
    # Workspace
    "WorkspaceRef",
    # Version
    "__version__",
]
