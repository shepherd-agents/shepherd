#!/usr/bin/env python3
"""Executed-evidence sentinel: prove a jailed acceptance gate actually ran, skip-free.

A green pytest summary is not release evidence when the load-bearing legs can silently *skip*
(no native jail on the host) or never *collect* (a marker-narrowed selection). "10 passed, 0
skipped" is satisfied by a run that only collected 3 tests. This sentinel closes both holes by
checking a JUnit-XML report against a named set of required test ids:

- every required id must be **present** in the report (catches a narrowed/non-collecting selection —
  zero-skips alone cannot detect this) AND **passed** (not failed/errored);
- a designated jailed subset must additionally be **skip-free** (catches a jail-less host silently
  skipping the syscall-enforcement legs).

Matching is on exact JUnit ids (``classname::name``), never bare test names.

The canonical required-id lists live here, in one place, so the release evidence packet and CI both
cite the same source and a test rename fails loudly in both. Profiles carry those lists; ad-hoc
callers may also pass ``--require``/``--jailed-require`` explicitly.

Run:
    uv run python scripts/check_executed_evidence.py --junitxml <report.xml> --profile lane-c

Exit 0 = every required id present+passed and every jailed id skip-free; exit 1 = otherwise, with
each failing id named.
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvidenceProfile:
    """A named set of required test ids and the jailed subset that must not skip."""

    name: str
    required: tuple[str, ...]
    jailed: tuple[str, ...] = field(default_factory=tuple)


# P-030 v0.2 Lane C acceptance gate (shepherd/packages/dialect/tests/test_lane_c_acceptance_gate.py).
# The 10 gate assertions; the 7 ``_JAIL_ONLY`` legs must additionally never skip on a release host.
_LANE_C_CLASS = "tests.test_lane_c_acceptance_gate"
_LANE_C_REQUIRED = (
    "test_a1_backend_write_lands_on_jailed_placement",
    "test_a2_readonly_root_raw_write_refused_at_the_syscall",
    "test_a2_readonly_handle_write_refused_at_the_handle_layer",
    "test_a3_unattributed_managed_write_fails_closed",
    "test_a4_overlapping_and_nested_binds_refused_at_bind_time",
    "test_a5_all_writable_advisory_run_records_advisory",
    "test_a5_readonly_binding_under_advisory_refused_at_start",
    "test_a6_any_writable_selects_once_then_consume_once_refuses",
    "test_a6_all_readonly_run_select_refused",
    "test_a7_per_binding_changeset_view",
)
_LANE_C_JAILED = (
    "test_a1_backend_write_lands_on_jailed_placement",
    "test_a2_readonly_root_raw_write_refused_at_the_syscall",
    "test_a2_readonly_handle_write_refused_at_the_handle_layer",
    "test_a3_unattributed_managed_write_fails_closed",
    "test_a6_any_writable_selects_once_then_consume_once_refuses",
    "test_a6_all_readonly_run_select_refused",
    "test_a7_per_binding_changeset_view",
)

# Beat-0 safety bar (0.3.0): the fabrication fence (both schema stacks + parity +
# registration carve-out), the signature-directed placement refusal (both call
# spellings, with and without a provider, both nuclei), and the W0 correctness
# fixes (dual-key shim in both nuclei, tri-state body guard, workspace re-entry).
# All legs are offline — no jailed subset; skips are forbidden via --forbid-skips
# at the call site (every leg must execute on every host).
_BEAT0_DIALECT_FENCE = "shepherd.packages.dialect.tests.test_handle_return_fence"
_BEAT0_RUNTIME_FENCE = "shepherd.packages.runtime.tests.unit.step.test_handle_return_fence"
_BEAT0_REFUSAL = "shepherd.packages.runtime.tests.unit.nucleus.test_placement_refusal"
_BEAT0_W0 = "shepherd.packages.runtime.tests.unit.nucleus.test_w0_correctness"
_BEAT0_SAFETY_REQUIRED = (
    f"{_BEAT0_DIALECT_FENCE}.TestTwoStackParity::test_refusals_are_identical[GitRepo]",
    f"{_BEAT0_DIALECT_FENCE}.TestTwoStackParity::test_refusals_are_identical[tuple0]",
    f"{_BEAT0_DIALECT_FENCE}.TestRegistrationCarveOut::test_task_input_model_accepts_handle_parameter",
    f"{_BEAT0_DIALECT_FENCE}.TestRegistrationCarveOut::test_task_prompt_refuses_handle_return",
    f"{_BEAT0_RUNTIME_FENCE}.TestReturnTypeToOutputSchemaFence::test_str_schema_byte_identical_to_prefence_shape",
    f"{_BEAT0_RUNTIME_FENCE}.TestStepExecutionRoutesThroughFence::test_mock_value_generation_refuses_handle_returns",
    f"{_BEAT0_REFUSAL}.TestHandleAnnotatedBodylessRefuses::test_direct_call_refuses_with_provider_installed",
    f"{_BEAT0_REFUSAL}.TestHandleAnnotatedBodylessRefuses::test_run_spelling_refuses_with_provider_installed",
    f"{_BEAT0_REFUSAL}.TestHandleAnnotatedBodylessRefuses::test_refuses_without_any_provider",
    f"{_BEAT0_REFUSAL}.TestHandleAnnotatedBodylessRefuses::test_refusal_keys_on_annotation_not_value",
    f"{_BEAT0_REFUSAL}.TestUnaffectedShapes::test_pure_value_bodyless_still_delivers",
    f"{_BEAT0_REFUSAL}.TestUnaffectedShapes::test_bodied_task_with_handle_params_untouched",
    "shepherd.packages.dialect.tests.test_placement_refusal_dialect_lane::test_dialect_ambient_lane_cannot_reach_bodyless_delivery",
    f"{_BEAT0_W0}.TestDualKeyShim::test_taught_spelling_resolves",
    "shepherd.packages.dialect.tests.test_model_call_key_shim_dialect::test_taught_spelling_resolves_in_dialect_nucleus",
    f"{_BEAT0_W0}.TestTriStateBodyClassification::test_exec_defined_return_none_task_raises_loud_not_silent",
    f"{_BEAT0_W0}.TestTriStateBodyClassification::test_ambiguous_handle_annotated_gets_placement_refusal",
    f"{_BEAT0_W0}.TestWorkspaceReentry::test_idle_reconfiguration_replaces_instead_of_trapping",
    f"{_BEAT0_W0}.TestWorkspaceReentry::test_reconfiguration_during_active_run_refuses",
)

PROFILES: dict[str, EvidenceProfile] = {
    "lane-c": EvidenceProfile(
        name="lane-c",
        required=tuple(f"{_LANE_C_CLASS}::{name}" for name in _LANE_C_REQUIRED),
        jailed=tuple(f"{_LANE_C_CLASS}::{name}" for name in _LANE_C_JAILED),
    ),
    "beat0-safety": EvidenceProfile(
        name="beat0-safety",
        required=_BEAT0_SAFETY_REQUIRED,
    ),
}


def _load_cases(path: str) -> dict[str, ET.Element]:
    cases: dict[str, ET.Element] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        cases[f"{classname}::{name}"] = case
    return cases


def check(
    junitxml: str,
    *,
    required: tuple[str, ...],
    jailed: tuple[str, ...],
    forbid_skips: bool,
) -> list[str]:
    """Return a list of failure reasons; empty means the evidence bar is met."""
    cases = _load_cases(junitxml)
    jailed_set = set(jailed)
    failures: list[str] = []
    for test_id in required:
        case = cases.get(test_id)
        if case is None:
            failures.append(f"MISSING (not collected): {test_id}")
            continue
        skipped = case.find("skipped") is not None
        broke = case.find("failure") is not None or case.find("error") is not None
        if broke:
            failures.append(f"FAILED/ERROR: {test_id}")
        if skipped and (forbid_skips or test_id in jailed_set):
            label = "SKIPPED jailed leg" if test_id in jailed_set else "SKIPPED"
            failures.append(f"{label}: {test_id}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--junitxml", required=True, help="Path to the JUnit-XML report to validate.")
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        help="Named required-id profile (e.g. 'lane-c'). Composes with explicit --require/--jailed-require.",
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="CLASS::NAME",
        help="Exact JUnit id that must be present and passed (repeatable).",
    )
    parser.add_argument(
        "--jailed-require",
        action="append",
        default=[],
        metavar="CLASS::NAME",
        help="Exact JUnit id that must additionally be skip-free (repeatable).",
    )
    parser.add_argument(
        "--forbid-skips",
        action="store_true",
        help="Require every --require id to be skip-free (not only the jailed subset).",
    )
    args = parser.parse_args(argv)

    required: list[str] = list(args.require)
    jailed: list[str] = list(args.jailed_require)
    if args.profile:
        profile = PROFILES[args.profile]
        required = [*profile.required, *required]
        jailed = [*profile.jailed, *jailed]

    if not required:
        parser.error("no required test ids: pass --profile and/or --require")

    failures = check(
        args.junitxml,
        required=tuple(dict.fromkeys(required)),
        jailed=tuple(dict.fromkeys(jailed)),
        forbid_skips=args.forbid_skips,
    )
    if failures:
        print("RED — executed-evidence bar not met:")
        for reason in failures:
            print(f"  {reason}")
        return 1
    print(
        f"GREEN — all {len(set(required))} required id(s) present and passed; "
        f"{len(set(jailed))} jailed id(s) skip-free."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
