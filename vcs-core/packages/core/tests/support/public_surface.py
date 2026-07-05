"""Shared baseline inventory for the public-looking package surface."""

from __future__ import annotations

PUBLIC_LOOKING_TOP_LEVEL_MODULES = {
    "authority",
    "cli",
    "commons_recording",
    "config",
    "discovery",
    "git_store",
    "git_substrate",
    "keyed_json_tree",
    "manifest",
    "materialization",
    "vcscore",
    "profiles",
    "recording",
    "readiness",
    # The consumer call API (PD5 — runtime-call-api.md §4; CALL_API_VERSION
    # versions it independently of the SPI).
    "runtime_api",
    # Public runtime substrate helper facade; implementation remains private.
    "runtime_substrate",
    "scope_stack",
    # Public Python facade over the private daemon transport for session/overlay
    # capture callers.
    "session_capture",
    # The substrate SPI — the implement-side stable surface (the twin of
    # runtime_api; decisions.md `spi-top-level-promotion`).
    "spi",
    "sqlite_substrate",
    "store",
    # Public owner path for first-cut active-surface profile helpers.
    "surface_profiles",
    "substrates",
    "types",
}
