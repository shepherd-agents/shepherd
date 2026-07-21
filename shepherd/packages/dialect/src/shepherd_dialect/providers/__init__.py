"""Execution providers the dialect ships.

Every provider in this package is an executor behind ``runtime.run``: the task
prompt/configuration is sent to a confined provider process, and VcsCore captures
workspace changes from the run working path after that process exits. Provider
events are semantic evidence only, not workspace authority.

Layout: one provider class per module; per-family infrastructure lives beside
its providers (``claude_auth``, ``claude_cli``); the only cross-provider helpers
are in ``_common``. Lifecycle status (active / deferred / legacy) is recorded in
each module's docstring, not in the package layout.
"""

from __future__ import annotations

from shepherd_dialect.providers.claude_api import ClaudeApiProvider
from shepherd_dialect.providers.claude_auth import (
    ClaudeAuthStatus,
    claude_auth_mode,
    claude_auth_status,
)
from shepherd_dialect.providers.claude_headless import ClaudeHeadlessProvider, probe_claude_auth
from shepherd_dialect.providers.claude_legacy import ClaudeAgentProvider
from shepherd_dialect.providers.codex import CodexAgentProvider
from shepherd_dialect.providers.codex_profile import (
    CODEX_REAUDIT_ON_BUMP,
    CODEX_TESTED_VERSION,
    CodexAuthStatus,
    CodexProfileError,
    adopt_existing_codex_login,
    codex_auth_status,
    login_codex_api_key,
    login_codex_chatgpt,
    logout_codex_profile,
    probe_codex_auth,
)
from shepherd_dialect.providers.fake import DeterministicFakeProvider
from shepherd_dialect.providers.hermes import (
    HERMES_REAUDIT_ON_BUMP,
    HERMES_SUPPORTED_MODEL_PROVIDERS,
    HERMES_TESTED_VERSION,
    HermesAuthStatus,
    HermesHeadlessProvider,
    hermes_auth_status,
    probe_hermes_auth,
)

__all__ = [
    "CODEX_REAUDIT_ON_BUMP",
    "CODEX_TESTED_VERSION",
    "HERMES_REAUDIT_ON_BUMP",
    "HERMES_SUPPORTED_MODEL_PROVIDERS",
    "HERMES_TESTED_VERSION",
    "ClaudeAgentProvider",
    "ClaudeApiProvider",
    "ClaudeAuthStatus",
    "ClaudeHeadlessProvider",
    "CodexAgentProvider",
    "CodexAuthStatus",
    "CodexProfileError",
    "DeterministicFakeProvider",
    "HermesAuthStatus",
    "HermesHeadlessProvider",
    "adopt_existing_codex_login",
    "claude_auth_mode",
    "claude_auth_status",
    "codex_auth_status",
    "hermes_auth_status",
    "login_codex_api_key",
    "login_codex_chatgpt",
    "logout_codex_profile",
    "probe_claude_auth",
    "probe_codex_auth",
    "probe_hermes_auth",
]
