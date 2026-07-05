"""Demonstration that the autouse env-isolation fixture actually isolates (W0.2).

Two tests in definition order: the first leaks a process-env var without
cleaning up; the second asserts a clean environment. If the ``conftest.py``
``_isolate_process_environment`` fixture regressed, the second would see the
leak and fail. This is the pair that proves the belt-and-braces is live.
"""

from __future__ import annotations

import os

_PROBE = "_SHEPHERD_ENV_HYGIENE_PROBE"


def test_env_hygiene_a_poisoner_leaks_a_var() -> None:
    os.environ[_PROBE] = "leaked"  # deliberately no cleanup
    assert os.environ[_PROBE] == "leaked"


def test_env_hygiene_b_checker_sees_clean_env() -> None:
    assert _PROBE not in os.environ, (
        "env isolation regressed: the poisoner's var leaked past its test — "
        "the autouse _isolate_process_environment fixture is not restoring env"
    )
