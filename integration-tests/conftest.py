"""Shared fixtures for the top-level integration suite."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_process_environment():
    """Snapshot and restore ``os.environ`` around every test — belt-and-braces.

    Protects against any test that writes process env directly leaking state
    into later in-process ``CliRunner`` calls. A d2 guard
    (test_meta_cli_mutates_no_ambient_process_env) keeps the CLI itself honest.
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
