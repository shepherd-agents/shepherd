"""Shared fixtures for the top-level integration suite."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_process_environment():
    """Snapshot and restore ``os.environ`` around every test — belt-and-braces.

    The original leak (the CLI entrypoint's ``os.environ.setdefault`` for
    VCS_CORE_SEAL_AND_SELECT) was removed at the source in W1c: the CLI now
    scopes the flag with ``scoped_seal_and_select()`` and restores it when the
    click context tears down, so in-process ``CliRunner`` calls no longer leak.
    This fixture is kept anyway — it protects against a *different* source (any
    test that writes process env directly), which the CLI fix does not cover.
    A d2 guard (test_meta_cli_mutates_no_ambient_process_env) keeps the CLI
    itself honest.
    """
    saved = dict(os.environ)
    try:
        yield
    finally:
        for key in set(os.environ) - set(saved):
            del os.environ[key]
        for key, value in saved.items():
            if os.environ.get(key) != value:
                os.environ[key] = value
