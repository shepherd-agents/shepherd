"""The Hermes Agent headless CLI execution provider — the multi-model lane.

Lifecycle: active. Backed by the ``hermes`` CLI (Nous Research, PyPI
``hermes-agent``) in oneshot mode (``-z``). Routes to Anthropic, OpenAI, or
OpenRouter models through one execution provider; the claude lanes are
Anthropic-only. Spiked against ``HERMES_TESTED_VERSION`` below
(``260709-hermes-provider-execplan.md`` r4/r5).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from vcs_core.spi import ExecutionAuthorityRequired

from shepherd_dialect.provider_capabilities import (
    BASH,
    EDIT_FILE,
    READ_FILE,
    SEARCH_CONTENT,
    SEARCH_FILES,
    WRITE_FILE,
    AgentProviderCapabilities,
    canonical_tool_payload,
)
from shepherd_dialect.provider_runtime import (
    MODEL_CALL,
    MODEL_TURN,
    PROVIDER_INVOCATION_COMPLETED,
    PROVIDER_INVOCATION_FAILED,
    PROVIDER_INVOCATION_STARTED,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_STARTED,
    ExecutionProviderResult,
    ProviderEvent,
    ProviderInvocationError,
    ProviderInvocationResult,
    digest_jsonable,
    provider_invocation_outcome,
    redacted_text_payload,
)
from shepherd_dialect.providers._common import (
    SCRATCH_RESIDUE_ERROR_TYPE,
    _invocation_id,
    _provider_prompt,
    hard_stop_prefix,
    scratch_residue_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vcs_core.runtime_substrate import HandlerStack
    from vcs_core.spi import DriverContext

# The supported model providers, by how the jailed run authenticates:
#   - env-key providers (anthropic/openai/openrouter): a key passes through the
#     jail's env block;
#   - hermes-native OAuth providers (openai-codex = a ChatGPT subscription): no
#     env key exists — the host's `~/.hermes/auth.json` login is seeded
#     access-token-only into the scratch (Phase 2, §subscription).
# anthropic additionally accepts a signed-in Claude login (Phase 1). An unknown
# provider fails construction rather than at runtime with an opaque envelope.
_HERMES_ENV_PROVIDERS = ("anthropic", "openai", "openrouter")
_HERMES_OAUTH_PROVIDERS = ("openai-codex",)
HERMES_SUPPORTED_MODEL_PROVIDERS = (*_HERMES_ENV_PROVIDERS, *_HERMES_OAUTH_PROVIDERS)

# The hermes-agent version this lane was spiked and reviewed against (the
# module docstring's claim, as data), plus what to re-audit on a bump. Owned
# here — not in the doctor CLI — because a version bump happens in this module
# and its tests; the doctor's warn-only pin imports these so there is exactly
# one place to update (r5 review).
HERMES_TESTED_VERSION = "0.18.2"
HERMES_REAUDIT_ON_BUMP = "hermes_cli/oneshot.py, tools/approval.py, agent/models_dev.py, tools/lazy_deps.py"

_HERMES_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# hermes `-t file,terminal` tools, mapped to canonical names: read_file and
# write_file are shared vocabulary, `patch` is the edit tool, `terminal` is
# bash, and `search_files` is a ripgrep-backed search whose *default* target is
# file contents (regex), with a glob mode for names — so it executes both
# search claims.
_HERMES_WORKSPACE_TOOLS = frozenset({READ_FILE, WRITE_FILE, EDIT_FILE, SEARCH_FILES, SEARCH_CONTENT, BASH})

# The usage-envelope fields lifted into ProviderInvocationResult.usage. The
# envelope is the run's cost evidence: hermes reports per-run token counts and
# an estimated cost, which is richer than the claude lanes' envelopes.
_HERMES_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
    "api_calls",
    "estimated_cost_usd",
    "cost_status",
    "cost_source",
)

_KEYLESS_ESCAPE = (
    "If a `hermes` wrapper authenticates outside the env-key routes, set "
    "SHEPHERD_ALLOW_KEYLESS_HERMES=1 to launch anyway."
)


def _host_claude_subscription_blob() -> bytes | None:
    """A seedable host Claude Code login, access-token-only, or ``None`` (Phase 1, §subscription).

    Reuses the claude lane's D1-ratified host-login reader — the single
    credential-read site — so no new parent-side effect is introduced. Gated to
    the conditions under which the access-token-only mitigation is *sound*:

    - **Linux only.** The safety guarantee (a jailed hermes with no refresh
      token cannot rotate the host login) relies on the jail leaving *no other*
      credential source reachable. On Linux the jail redirects HOME/HERMES_HOME
      and there is no keychain, so the seeded access-token-only file is the only
      source (spiked). The macOS keychain is reachable and could supply a
      refresh token, so subscription seeding is gated off there until spiked.
    - **Not expired.** A jailed run cannot refresh, so an already-expired login
      is refused rather than seeded (mirrors the claude lane's preflight).
    - **``SHEPHERD_NO_CREDENTIAL_SEEDING`` opts out**, shared with the claude lane.
    """
    if not sys.platform.startswith("linux") or os.environ.get("SHEPHERD_NO_CREDENTIAL_SEEDING"):
        return None
    from shepherd_dialect.providers.claude_auth import _claude_blob_expiry, _read_host_claude_login

    blob = _read_host_claude_login().blob
    if blob is None or _claude_blob_expiry(blob) is True:
        return None
    return _access_token_only(blob)


def _strip_refresh_capable(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Drop every refresh-capable / identity field — the one safety primitive, fail-closed.

    The whole subscription-seed mechanism rests on a single invariant: *a jailed
    hermes given only an access token cannot rotate/invalidate the host login*
    (the refresh call needs a refresh token). This is the sole place that
    invariant is enforced, shared by both seed shapes (Claude blob, Hermes
    ``auth.json`` entry) so it can never drift between them.

    Fail-closed **by pattern, not by an exact key name**: any key whose name
    contains ``refresh`` (case-insensitive) is dropped, so a renamed or added
    refresh field (``refreshTokenV2``, ``refresh_token_expires_at``) is stripped
    too rather than silently carried into the jail — the failure mode a
    ``k != "refreshToken"`` denylist would miss. ``id_token`` (a host identity
    secret the jailed run has no need for) is dropped on the same principle.
    Over-stripping a benign refresh-named telemetry field (e.g. ``last_refresh``)
    is harmless — the access token, ``auth_type`` and ``base_url`` a run needs
    all survive.
    """
    return {k: v for k, v in mapping.items() if "refresh" not in k.lower() and k.lower() != "id_token"}


def _access_token_only(blob: bytes) -> bytes | None:
    """A Claude Code credential blob, refresh-capable fields stripped, or ``None``.

    Applies the shared :func:`_strip_refresh_capable` invariant to the
    ``claudeAiOauth`` object (hermes refreshes in-process; spiked). Returns
    ``None`` if the blob has no usable access token.
    """
    try:
        oauth = json.loads(blob).get("claudeAiOauth")
    except (ValueError, AttributeError):
        return None
    if not isinstance(oauth, Mapping) or not oauth.get("accessToken"):
        return None
    stripped = {"claudeAiOauth": _strip_refresh_capable(oauth)}
    return json.dumps(stripped).encode()


def _host_hermes_oauth_authstore(model_provider: str) -> bytes | None:
    """A seedable host hermes OAuth login for ``model_provider``, access-token-only, or ``None``.

    The Phase 2 path (§subscription): a ChatGPT subscription (``openai-codex``,
    and future hermes-native OAuth providers) is signed into Hermes itself and
    stored in the host ``~/.hermes/auth.json`` ``credential_pool`` — the jail
    redirects ``HERMES_HOME`` away from it, so it is seeded in. Returns a
    minimal ``auth.json`` (just this provider's entries) with **every refresh
    token stripped** — the same safety invariant as Phase 1: a jailed hermes
    with no refresh token cannot rotate the host login (spiked live against a
    real ChatGPT login; host store byte-identical after). Linux-gated and
    ``SHEPHERD_NO_CREDENTIAL_SEEDING``-aware, as in Phase 1.
    """
    if not sys.platform.startswith("linux") or os.environ.get("SHEPHERD_NO_CREDENTIAL_SEEDING"):
        return None
    host_home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    try:
        store = json.loads((Path(host_home) / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = (store.get("credential_pool") or {}).get(model_provider) if isinstance(store, Mapping) else None
    if not isinstance(entries, list):
        return None
    stripped = [_strip_refresh_capable(e) for e in entries if isinstance(e, Mapping) and e.get("access_token")]
    if not stripped:
        return None
    minimal = {"version": store.get("version", 1), "credential_pool": {model_provider: stripped}}
    return json.dumps(minimal).encode()


# Where each subscription seed lands *inside the scratch* (segments joined onto
# the scratch root). The Claude blob → the redirected HOME's ~/.claude (Phase 1);
# the Hermes auth.json → the redirected HERMES_HOME (Phase 2).
_CLAUDE_SEED_RELPATH = ("home", ".claude", ".credentials.json")
_HERMES_SEED_RELPATH = ("hermes", "auth.json")


@dataclass(frozen=True)
class _CredentialSeed:
    """A host subscription login, already refresh-stripped, ready to write into the scratch.

    Carried on :class:`HermesAuthStatus` so the host credential is read exactly
    once — at status time — rather than re-read at seed time. That single read
    closes the TOCTOU window where a login rotated between the verdict and the
    seed would launch the jailed run uncredentialed.
    """

    relpath: tuple[str, ...]
    blob: bytes


@dataclass(frozen=True)
class HermesAuthStatus:
    """The offline readiness verdict for the jailed Hermes lane."""

    mode: str | None
    ok: bool
    detail: str
    seed: _CredentialSeed | None = None


def hermes_auth_status(model_provider: str) -> HermesAuthStatus:
    """Offline verdict on whether the jailed Hermes lane can authenticate.

    Cheap and network-free. Resolved against ``model_provider``: an **env key**
    (passes through the jail's env block); for anthropic, a **signed-in Claude
    login** (Phase 1); or, for a hermes-native OAuth provider like
    ``openai-codex``, a **signed-in Hermes login** from ``~/.hermes/auth.json``
    (Phase 2). Subscription logins are seeded access-token-only. A set key /
    seedable login is ``ok`` but *unverified*: ``probe_hermes_auth`` is the
    authoritative check.
    """
    if model_provider not in HERMES_SUPPORTED_MODEL_PROVIDERS:
        supported = ", ".join(HERMES_SUPPORTED_MODEL_PROVIDERS)
        return HermesAuthStatus(None, False, f"unsupported model_provider {model_provider!r} — supported: {supported}")
    env_key = _HERMES_ENV_KEYS.get(model_provider)
    if env_key and os.environ.get(env_key):
        return HermesAuthStatus("env_key", True, f"{env_key} set")
    if model_provider == "anthropic":
        blob = _host_claude_subscription_blob()
        if blob is not None:
            return HermesAuthStatus(
                "subscription_login",
                True,
                "signed-in Claude login (seeded access-token-only)",
                seed=_CredentialSeed(_CLAUDE_SEED_RELPATH, blob),
            )
    if model_provider in _HERMES_OAUTH_PROVIDERS:
        authstore = _host_hermes_oauth_authstore(model_provider)
        if authstore is not None:
            return HermesAuthStatus(
                "subscription_login",
                True,
                f"signed-in Hermes {model_provider} login (seeded access-token-only)",
                seed=_CredentialSeed(_HERMES_SEED_RELPATH, authstore),
            )
    if env_key:
        return HermesAuthStatus(
            None, False, f"{env_key} is not set — the jailed hermes lane authenticates via env keys"
        )
    return HermesAuthStatus(
        None,
        False,
        f"no signed-in Hermes {model_provider} login found — run `hermes auth add {model_provider} --type oauth`",
    )


def _hermes_preflight_refusal(status: HermesAuthStatus) -> tuple[str, str, str] | None:
    """``(classification, error_type, message)`` if a jailed launch is known-doomed, else ``None``.

    The provider redirects ``HOME``/``HERMES_HOME`` into an empty scratch, so a
    body with no env key authenticates against nothing — a guaranteed envelope
    failure (hermes exits 0 and reports ``failed: true``). Refuse before
    spending a confined launch, unless ``SHEPHERD_ALLOW_KEYLESS_HERMES`` opts a
    wrapper in.
    """
    if status.mode is None:
        return (
            "auth_missing",
            "HermesAuthMissing",
            f"hermes auth is not available for a jailed run ({status.detail}). {_KEYLESS_ESCAPE}",
        )
    return None


def _hermes_seeded_config(model: str, model_provider: str) -> str:
    """The scratch ``HERMES_HOME/config.yaml`` — pure, so a keyless test pins it.

    The seeding is load-bearing, not a nicety: a scrubbed ``HERMES_HOME`` has no
    account default — this file *is* the model selection, and it is what routes
    env-key auth. The compression line is the fourth disarm, the one the toolset
    gate cannot reach: hermes auto-compresses at 50% context usage by default,
    which fires an auxiliary LLM call on the same credentials and rewrites the
    ``state.db`` rows the event harvest reads. JSON string literals are valid
    YAML scalars, so values are embedded via ``json.dumps``.
    """
    return (
        "model:\n"
        f"  default: {json.dumps(model)}\n"
        f"  provider: {json.dumps(model_provider)}\n"
        "compression:\n"
        "  enabled: false\n"
    )


@dataclass(frozen=True)
class _HermesCliFailureDiagnosis:
    """A typed reading of a failed hermes run (nonzero exit or failed envelope)."""

    classification: str
    summary: str
    remedy: str | None


_AUTH_FAILURE_MARKS = ("invalid x-api-key", "unauthorized", "authenticationerror", "permissiondeniederror")


def _diagnose_hermes_cli_failure(
    returncode: int,
    stdout: str | None,
    stderr: str | None,
    *,
    model_provider: str,
    envelope_failure: str | None = None,
) -> _HermesCliFailureDiagnosis:
    """Classify a failed run: structured ``failure`` key first, reply text second.

    Hermes exits 0 even on failure — the usage envelope's ``failed`` flag is the
    outcome authority. When an exception escaped the agent, the envelope carries
    ``failure`` (``str(exc)``, exception class included): a machine signal the
    model's reply cannot spoof, so it wins. The text fallback anchors on the
    *first line* of output (hermes prints its error as the whole reply — spiked:
    ``HTTP 401: invalid x-api-key`` on stdout, rc 0); free substring matching
    over the full reply would misclassify prose that merely mentions
    authentication.
    """
    env_key = _HERMES_ENV_KEYS.get(model_provider, "the provider API key")
    auth_remedy = (
        f"the jailed run authenticates via {env_key} — check it is set, current, and valid for {model_provider}"
    )
    if envelope_failure:
        lowered_failure = envelope_failure.lower()
        summary = envelope_failure.splitlines()[0][:300]
        if any(mark in lowered_failure for mark in _AUTH_FAILURE_MARKS) or "401" in lowered_failure:
            return _HermesCliFailureDiagnosis("auth_failure", summary, auth_remedy)
        return _HermesCliFailureDiagnosis("provider_error", summary, None)
    text = f"{stderr or ''}\n{stdout or ''}".strip()
    first_line = text.splitlines()[0][:300] if text else ""
    lowered_first = first_line.lower()
    summary = first_line or f"hermes exited rc={returncode} with no output"
    if lowered_first.startswith(("http 401", "http 403")) or any(mark in lowered_first for mark in _AUTH_FAILURE_MARKS):
        return _HermesCliFailureDiagnosis("auth_failure", summary, auth_remedy)
    if lowered_first.startswith("http 4") or "could not resolve" in lowered_first:
        return _HermesCliFailureDiagnosis("provider_rejection", summary, None)
    return _HermesCliFailureDiagnosis("unknown", summary, None)


def _read_usage_envelope(path: Path) -> Mapping[str, Any] | None:
    """The ``--usage-file`` JSON envelope, or ``None`` when absent/unreadable.

    No envelope is written on an alarm kill (spiked), so the rc -14 check must
    precede interpreting a missing envelope as a contract violation; a missing
    envelope on an ordinary zero exit is one, and the caller fails loudly.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _harvest_hermes_session(db_path: Path, session_id: str | None) -> tuple[Mapping[str, Any], ...] | None:
    """Message rows for the run's session from the scratch ``state.db``, or ``None``.

    Read-only URI open, stdlib ``sqlite3`` (no hermes import — the dialect's
    dependency posture). Failed envelopes carry ``session_id: null`` (spiked on
    both an auth failure and the no-model 400), so a null key falls back to the
    sole session in the db — each run gets a fresh ``HERMES_HOME``, so more than
    one session is unexpected. Degrade to ``None`` (bookends-only evidence)
    when the db holds zero sessions (a refusal can fail before the session row
    exists — early-failure diagnostics are ``request_dump_*.json`` files, not
    rows), more than one, or on any sqlite error: an alarm-killed run leaves
    live ``-wal``/``-shm`` sidecars and WAL recovery under a read-only open is
    environment-sensitive, so degrade quietly rather than fail the harvest.
    """
    try:
        # SQLite URI filenames percent-decode %xx and stop at ?/# — a raw
        # f-string breaks (or misdirects) on working paths containing those
        # characters, so the path rides percent-encoded (verified live: '%20',
        # '#', and '?' dirs all harvest correctly encoded, all fail raw).
        con = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        con.row_factory = sqlite3.Row
        if session_id is None:
            ids = [row[0] for row in con.execute("SELECT id FROM sessions")]
            if len(ids) != 1:
                return None
            session_id = ids[0]
        rows = con.execute(
            "SELECT role, content, tool_call_id, tool_calls, tool_name FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return tuple(dict(row) for row in rows)
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _provider_events_from_hermes_rows(
    rows: tuple[Mapping[str, Any], ...],
    *,
    provider_id: str,
    invocation_id: str,
    model: str,
    sequence_start: int,
) -> tuple[ProviderEvent, ...]:
    """Project harvested message rows into tool-call provider events.

    Assistant rows carry ``tool_calls`` (a JSON array of function calls with
    ids); ``tool`` rows carry the result keyed by ``tool_call_id`` with the
    native tool name in ``tool_name``. Mirrors the claude stream projection:
    started/completed pairs with canonical tool names and digested params.

    Ids can be absent on either side (hermes's object-message persistence path
    drops call ids), so open calls queue up and an id-less result row pairs
    with the oldest open call — synthesized ids then match across the pair
    instead of drifting apart on a shared counter.
    """
    events: list[ProviderEvent] = []
    sequence = sequence_start
    fallback_index = 0
    open_calls: list[tuple[str, str]] = []  # (tool_call_id, tool_name) awaiting a result row
    for row in rows:
        role = row.get("role")
        if role == "assistant" and row.get("tool_calls"):
            try:
                calls = json.loads(str(row["tool_calls"]))
            except ValueError:
                continue
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function")
                function = function if isinstance(function, Mapping) else {}
                raw_call_id = call.get("id")
                if raw_call_id:
                    tool_call_id = str(raw_call_id)
                else:
                    fallback_index += 1
                    tool_call_id = f"hermes-tool-{fallback_index}"
                tool_name = str(function.get("name") or "tool")
                open_calls.append((tool_call_id, tool_name))
                raw_arguments = function.get("arguments")
                try:
                    params = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                except ValueError:
                    params = raw_arguments
                events.append(
                    ProviderEvent(
                        kind=TOOL_CALL_STARTED,
                        provider_id=provider_id,
                        invocation_id=invocation_id,
                        sequence=sequence,
                        event_id=f"{invocation_id}:tool-start:{sequence}",
                        model=model,
                        tool_call_id=tool_call_id,
                        payload={
                            **canonical_tool_payload(tool_name),
                            "params_digest": digest_jsonable(params if isinstance(params, Mapping) else {}),
                        },
                    )
                )
                sequence += 1
        elif role == "tool":
            raw_row_id = row.get("tool_call_id")
            if raw_row_id:
                tool_call_id = str(raw_row_id)
                matched = next((call for call in open_calls if call[0] == tool_call_id), None)
                if matched is not None:
                    open_calls.remove(matched)
                tool_name = str(row.get("tool_name") or (matched[1] if matched else "tool"))
            elif open_calls:
                tool_call_id, opened_name = open_calls.pop(0)
                tool_name = str(row.get("tool_name") or opened_name)
            else:
                fallback_index += 1
                tool_call_id = f"hermes-tool-{fallback_index}"
                tool_name = str(row.get("tool_name") or "tool")
            events.append(
                ProviderEvent(
                    kind=TOOL_CALL_COMPLETED,
                    provider_id=provider_id,
                    invocation_id=invocation_id,
                    sequence=sequence,
                    event_id=f"{invocation_id}:tool-complete:{sequence}",
                    model=model,
                    tool_call_id=tool_call_id,
                    payload={
                        **canonical_tool_payload(tool_name),
                        **redacted_text_payload(str(row.get("content") or ""), field="output"),
                    },
                )
            )
            sequence += 1
    return tuple(events)


@dataclass(frozen=True)
class HermesHeadlessProvider:
    """Hermes headless CLI executor for the VcsCore-native run path.

    Runs **inside the jail** via ``launch_confined``; its file/terminal tools
    create real files in the carrier's working copy, and VcsCore captures the
    delta at merge. Nondeterministic and auth-needing, so it never gates CI —
    the runbook (package README) is its home.

    The argv composes the S1-proven blocks, outermost first:

    - **the hard stop** — perl ``alarm``+``exec``. The alarm kill surfaces as
      rc -14 → ``BudgetExhausted``, and it is the *only* stop: oneshot has no
      reachable turn-cap surface (spiked — neither ``HERMES_MAX_ITERATIONS``
      nor seeded ``agent.max_turns`` reaches ``-z``), so there is no
      ``max_turns`` field.
    - **the env redirect + hardening** — ``HOME``/``HERMES_HOME``/``TMPDIR``
      into ``<working_path>/.hermes-scratch``, plus the disarm trio:
      ``HERMES_SAFE_MODE`` (no plugin discovery, no shell hooks, no unsafe
      MCP), ``HERMES_DISABLE_LAZY_INSTALLS`` and ``HERMES_SKIP_NODE_BOOTSTRAP``
      (no runtime self-mutation of the install).
    - **the body** — ``hermes --ignore-rules --yolo -t file,terminal
      --usage-file … -m … --provider … -z <prompt>``. The toolset pin is
      triple-load-bearing: it disarms the learning loop (skills/memory review
      gates on their tools being present), excludes default-on ``delegate_task``
      fan-out, and keeps the browser sidecar's ~300 MB pull unreachable.
      ``--yolo`` is belt-and-braces (oneshot force-sets it today); the jail is
      the boundary, and interactive approval prompts would hang a headless run.
      No ``--ignore-user-config``: under the redirected home it is a no-op, and
      its documented semantics name exactly the file the seeding writes.

    Outcome authority is the ``--usage-file`` envelope, not the exit code —
    hermes exits 0 on failure (spiked). Event evidence is harvested from the
    scratch ``state.db`` before the D3 scrub.
    """

    provider_id: str = "hermes-headless"
    prompt: str = ""
    budget_seconds: int = 240
    # Both are REQUIRED (None defaults keep construction symmetry with the
    # claude lanes; __post_init__ enforces). A scrubbed HERMES_HOME has no
    # default anything: the seeded config is the model selection, and there is
    # nothing to seed it from but these two fields. model_provider is the
    # multi-model knob (`--provider`), constrained to the v1 auth set.
    model: str | None = None
    model_provider: str | None = None

    _SCRATCH = ".hermes-scratch"

    def __post_init__(self) -> None:
        if not self.model or not self.model_provider:
            raise ValueError(
                "HermesHeadlessProvider requires both model and model_provider: a scrubbed "
                "HERMES_HOME has no account default, so the seeded config is the model selection "
                "(model without provider fails auth resolution; neither fails as an opaque HTTP 400)"
            )
        if self.model_provider not in HERMES_SUPPORTED_MODEL_PROVIDERS:
            supported = ", ".join(HERMES_SUPPORTED_MODEL_PROVIDERS)
            raise ValueError(
                f"model_provider {self.model_provider!r} is outside the v1 auth set ({supported}) — "
                "the jailed lane can only resolve env keys for these"
            )

    @property
    def capabilities(self) -> AgentProviderCapabilities:
        return AgentProviderCapabilities(
            provider_id=self.provider_id,
            transport="headless_cli",
            confined=True,
            network_required=True,
            # No schema flag upstream (verified across CLI/AIAgent/ACP at
            # 0.18.2); structured results are a deferred-ladder item.
            structured_output=False,
            # Resume works mechanically (`--resume <sid> -z`, spiked) but the
            # scrubbed scratch makes the claim non-executable across runs —
            # the same honesty rule as claude-api.
            session_resume=False,
            workspace_tools=_HERMES_WORKSPACE_TOOLS,
            custom_tools=False,
            mcp=False,
        )

    def command_argv(self, working_path: Path | str, cli: str, prompt: str | None = None) -> list[str]:
        """The full jailed argv — pure, so the shape is pinned by a keyless test."""
        prompt = self.prompt if prompt is None else prompt
        scratch = Path(working_path) / self._SCRATCH
        return [
            *hard_stop_prefix(self.budget_seconds),
            "/usr/bin/env",
            f"HOME={scratch / 'home'}",
            f"HERMES_HOME={scratch / 'hermes'}",
            f"TMPDIR={scratch / 'tmp'}",
            "HERMES_SAFE_MODE=1",
            "HERMES_DISABLE_LAZY_INSTALLS=1",
            "HERMES_SKIP_NODE_BOOTSTRAP=1",
            cli,
            "--ignore-rules",
            "--yolo",
            "-t",
            "file,terminal",
            "--usage-file",
            str(scratch / "usage.json"),
            "-m",
            str(self.model),
            "--provider",
            str(self.model_provider),
            "-z",
            prompt,
        ]

    def _seed_scratch(self, working_path: Path | str) -> Path:
        """Create the scratch layout and write the seeded config; return the scratch root."""
        scratch = Path(working_path) / self._SCRATCH
        for sub in ("home", "tmp", "hermes"):
            (scratch / sub).mkdir(parents=True, exist_ok=True)
        config_path = scratch / "hermes" / "config.yaml"
        config_path.write_text(_hermes_seeded_config(str(self.model), str(self.model_provider)))
        return scratch

    @staticmethod
    def _write_credential_seed(scratch: Path, seed: _CredentialSeed) -> None:
        """Write a subscription credential into the scratch, atomically at mode ``0o600``.

        ``os.open(O_CREAT | O_EXCL, 0o600)`` creates the file already private — no
        world-readable window between a ``write_bytes`` and a later ``chmod`` for a
        co-tenant on a shared host to read the seeded access token through. Called
        *inside* ``execute``'s ``try``/``finally`` so a seed-time failure is still
        scrubbed, never leaving a credential on disk (the fail-closed contract).
        """
        dest = scratch.joinpath(*seed.relpath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, seed.blob)
        finally:
            os.close(fd)

    def execute(
        self,
        task_body: Callable[..., Any] | None,
        stack: HandlerStack,
        context: DriverContext,
        args: Mapping[str, Any],
        *,
        execution: Any = None,
        confinement: Any = None,
    ) -> ExecutionProviderResult:
        del stack, context
        if execution is None or confinement is None:
            raise ExecutionAuthorityRequired(
                "the Hermes headless provider runs only jailed: it needs the per-run "
                "ExecutionCapability and a lowered ConfinementSpec."
            )
        prompt = _provider_prompt(self.prompt, task_body, args, "HermesHeadlessProvider")
        cli = shutil.which("hermes")
        if cli is None:
            raise RuntimeError("hermes CLI not found on PATH — see the package README runbook note")
        invocation_id = _invocation_id(self.provider_id, execution)
        sequence = count()
        auth = hermes_auth_status(str(self.model_provider))
        auth_payload = {"auth_mode": auth.mode or "none", "auth_status": "ok" if auth.ok else auth.detail}
        started = ProviderEvent(
            kind=PROVIDER_INVOCATION_STARTED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=next(sequence),
            event_id=f"{invocation_id}:started",
            model=str(self.model),
            payload={
                "prompt_digest": digest_jsonable({"prompt": prompt}),
                "toolsets": "file,terminal",
                "model_provider": str(self.model_provider),
                **auth_payload,
            },
        )
        # Pre-launch: a jailed body with no env key authenticates against
        # nothing (HOME/HERMES_HOME are redirected into an empty scratch), so
        # refuse the known-doomed run *before* spending a confined launch —
        # unless SHEPHERD_ALLOW_KEYLESS_HERMES opts a wrapper in.
        preflight = _hermes_preflight_refusal(auth)
        if preflight is not None and not os.environ.get("SHEPHERD_ALLOW_KEYLESS_HERMES"):
            classification, error_type, message = preflight
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next(sequence),
                event_id=f"{invocation_id}:failed",
                model=str(self.model),
                payload={
                    "error_type": error_type,
                    "failure_classification": classification,
                    "launch_attempted": False,
                    **auth_payload,
                    **redacted_text_payload(message, field="error"),
                },
            )
            raise ProviderInvocationError(message, provider_events=(started, failed))
        scratch = self._seed_scratch(execution.working_path)
        envelope: Mapping[str, Any] | None = None
        harvest: tuple[Mapping[str, Any], ...] | None = None
        try:
            if auth.seed is not None:
                # Seed the host login access-token-only, fresh each run, from the
                # blob already read at status time (single read — no TOCTOU). The
                # refresh token is stripped, so the run cannot rotate the host
                # login; seeding is *inside* this try so the finally scrub covers a
                # seed-time failure too — the host stays the source of truth.
                self._write_credential_seed(scratch, auth.seed)
            proc = execution.launch_confined(self.command_argv(execution.working_path, cli, prompt), confinement)
            # Read the envelope and harvest event evidence BEFORE the D3 scrub
            # deletes the scratch. No envelope is written on an alarm kill.
            envelope = _read_usage_envelope(scratch / "usage.json")
            raw_session_id = envelope.get("session_id") if envelope else None
            harvest = _harvest_hermes_session(
                scratch / "hermes" / "state.db",
                raw_session_id if isinstance(raw_session_id, str) else None,
            )
        finally:
            # D3 scrub: before prepare_bound's supervised after-scan and before the
            # wrap merges — the captured delta is the agent's writes, not housekeeping.
            shutil.rmtree(scratch, ignore_errors=True)
        sequence_start = next(sequence)
        harvested_events = _provider_events_from_hermes_rows(
            harvest or (),
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            model=str(self.model),
            sequence_start=sequence_start,
        )
        next_sequence = sequence_start + len(harvested_events)
        if scratch.exists():
            # Fail closed, before any outcome: scratch residue that survived the
            # scrub would ride the captured delta as retained output — and the
            # hermes scratch holds the unredacted state.db transcript. Mirrors
            # the workspace lane's loud post-scrub verification (shared across
            # the jailed lanes — §4.7).
            message = scratch_residue_message(self._SCRATCH)
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next_sequence,
                event_id=f"{invocation_id}:failed",
                model=str(self.model),
                payload={
                    "returncode": proc.returncode,
                    "error_type": SCRATCH_RESIDUE_ERROR_TYPE,
                    "failure_classification": "scrub_residue",
                    **redacted_text_payload(message, field="error"),
                },
            )
            raise ProviderInvocationError(message, provider_events=(started, *harvested_events, failed))
        if proc.returncode == -14:
            from shepherd_dialect.nucleus import BudgetExhausted

            # The partial transcript rides the exception (the events channel
            # BudgetExhausted shares with ProviderInvocationError) — an
            # exhausted run's evidence is not discarded with its budget.
            raise BudgetExhausted(
                f"budget exceeded ({self.budget_seconds}s): SIGALRM hard stop",
                provider_events=(started, *harvested_events),
            )
        # One evaluation, ordered: process refusal, then the envelope contract
        # (hermes exits 0 on failure — the envelope is the outcome authority),
        # then explicit failure, then non-completion (partial/interrupted runs
        # report completed != true with failed: false and rc 0 — spiked against
        # oneshot's thinking-budget-exhausted path).
        failure_leg: tuple[str, str] | None = None
        if proc.returncode != 0:
            failure_leg = ("ConfinedProcessRefused", f"confined body refused (rc={proc.returncode})")
        elif envelope is None:
            # rc 0 with no envelope: the --usage-file contract was violated;
            # an empty success would be misread as "the agent did nothing".
            failure_leg = ("UsageEnvelopeMissing", "hermes wrote no usage envelope (--usage-file contract violated)")
        elif envelope.get("failed"):
            failure_leg = ("EnvelopeReportedFailure", "hermes envelope reported failure (failed: true)")
        elif envelope.get("completed") is not True:
            failure_leg = (
                "EnvelopeNotCompleted",
                "hermes envelope reported an incomplete run (completed is not true — partial/interrupted)",
            )
        if failure_leg is not None:
            error_type, cause = failure_leg
            raw_failure = envelope.get("failure") if envelope else None
            diagnosis = _diagnose_hermes_cli_failure(
                proc.returncode,
                proc.stdout,
                proc.stderr,
                model_provider=str(self.model_provider),
                envelope_failure=raw_failure if isinstance(raw_failure, str) and raw_failure else None,
            )
            message = f"{cause}: {diagnosis.summary}"
            if diagnosis.remedy:
                message += f"\n  → {diagnosis.remedy}"
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next_sequence,
                event_id=f"{invocation_id}:failed",
                model=str(self.model),
                payload={
                    "returncode": proc.returncode,
                    "error_type": error_type,
                    "failure_classification": diagnosis.classification,
                    "envelope_present": envelope is not None,
                    "envelope_failed": bool(envelope.get("failed")) if envelope else False,
                    "envelope_completed": bool(envelope.get("completed")) if envelope else False,
                    **redacted_text_payload(message, field="error"),
                    **redacted_text_payload(proc.stdout or "", field="stdout"),
                    **redacted_text_payload(proc.stderr or "", field="stderr"),
                },
            )
            # The harvested partial transcript rides the failure — it is most
            # diagnostic exactly on failed runs (sole-session fallback: failed
            # envelopes carry session_id: null).
            raise ProviderInvocationError(message, provider_events=(started, *harvested_events, failed))
        output_text = (proc.stdout or "").strip()
        usage = {key: envelope[key] for key in _HERMES_USAGE_KEYS if envelope.get(key) is not None}
        raw_session_id = envelope.get("session_id")
        session_id = raw_session_id if isinstance(raw_session_id, str) else None
        served_model = str(envelope.get("model") or self.model)
        metadata: dict[str, object] = {
            "model": served_model,
            "model_provider": str(envelope.get("provider") or self.model_provider),
        }
        cost = envelope.get("estimated_cost_usd")
        if isinstance(cost, (int, float)):
            metadata["cost_usd"] = float(cost)
        events: tuple[ProviderEvent, ...] = harvested_events
        if output_text or usage:
            events = (
                *events,
                ProviderEvent(
                    kind=MODEL_CALL,
                    provider_id=self.provider_id,
                    invocation_id=invocation_id,
                    sequence=next_sequence,
                    event_id=f"{invocation_id}:model-call:{next_sequence}",
                    model=served_model,
                    payload={
                        "usage": dict(usage),
                        **redacted_text_payload(output_text, field="output_text"),
                    },
                ),
            )
            next_sequence += 1
        if output_text:
            events = (
                *events,
                ProviderEvent(
                    kind=MODEL_TURN,
                    provider_id=self.provider_id,
                    invocation_id=invocation_id,
                    sequence=next_sequence,
                    event_id=f"{invocation_id}:model-turn:{next_sequence}",
                    model=served_model,
                    payload=redacted_text_payload(output_text, field="text"),
                ),
            )
            next_sequence += 1
        completed = ProviderEvent(
            kind=PROVIDER_INVOCATION_COMPLETED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=next_sequence,
            event_id=f"{invocation_id}:completed",
            model=served_model,
            payload={
                "returncode": proc.returncode,
                "session_id": session_id or "",
                **redacted_text_payload(proc.stdout or "", field="stdout"),
                **redacted_text_payload(proc.stderr or "", field="stderr"),
            },
        )
        result = ProviderInvocationResult(
            output_text=output_text,
            structured_output={},
            session_id=session_id,
            usage=usage,
            events=(started, *events, completed),
            metadata=metadata,
        )
        return ExecutionProviderResult(
            outcome=provider_invocation_outcome(
                result,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
            ),
            provider_events=result.events,
        )


def probe_hermes_auth(*, model: str, model_provider: str, budget_seconds: int = 30) -> tuple[bool, str]:
    """Check Hermes auth under Shepherd's scrubbed-home conditions; return ``(ok, detail)``.

    Runs a minimal ``hermes -z`` in the **parent** (not through the jail — no
    ``launch_confined``) under the provider's scrubbed-home + seeded-config
    conditions (the auth-relevant half of a real run) and classifies the outcome
    with the run path's own envelope reading — the authoritative counterpart to
    the offline ``hermes_auth_status``. Runs from a throwaway temp cwd: the
    prompt requests no tools, but nothing *stops* a model from calling
    ``write_file``, and a temp cwd caps that blast radius for free. Reaches the
    network and briefly calls the model. Never raises.
    """
    cli = shutil.which("hermes")
    if cli is None:
        return False, "`hermes` not found on PATH"
    status = hermes_auth_status(model_provider)
    if not status.ok:
        return False, status.detail
    if not model:
        # The construction invariant would raise ValueError; a probe reports.
        return False, "probe needs a model id (got an empty string)"
    try:
        provider = HermesHeadlessProvider(
            prompt="Reply with the single word: ok",
            model=model,
            model_provider=model_provider,
            budget_seconds=budget_seconds,
        )
        with tempfile.TemporaryDirectory(prefix="shepherd-hermes-probe-") as tmp:
            working = Path(tmp)
            scratch = provider._seed_scratch(working)
            argv = provider.command_argv(working, cli)
            # Narrow the probe below the run argv's toolsets: `-t file` (the
            # probe arms no terminal); the pinned run shape is untouched.
            argv[argv.index("-t") + 1] = "file"
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=budget_seconds + 15,
                check=False,
                cwd=working,
            )
            envelope = _read_usage_envelope(scratch / "usage.json")
    except subprocess.TimeoutExpired:
        return False, f"probe timed out after ~{budget_seconds}s"
    except Exception as exc:  # noqa: BLE001 — a probe that cannot run is a failed probe, not a crash
        return False, f"probe could not run: {exc}"
    if proc.returncode == 0 and envelope is not None and envelope.get("completed") is True:
        return True, f"authenticated ({model_provider} via env key)"
    raw_failure = envelope.get("failure") if envelope else None
    diagnosis = _diagnose_hermes_cli_failure(
        proc.returncode,
        proc.stdout,
        proc.stderr,
        model_provider=model_provider,
        envelope_failure=raw_failure if isinstance(raw_failure, str) and raw_failure else None,
    )
    return False, diagnosis.summary
