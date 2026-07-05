"""Regenerate the kernel-v3 golden fixtures whose canonical bytes changed.

Run from repo root:

    uv run python shepherd/packages/kernel-v3-reference/tests/rebake_goldens.py

Rebuilds each fixture from the SAME builder the byte-stability test compares
against, so a regenerate-then-verify round-trip is exact. Preserves every
non-generated field (names, ordering, schema_version). Run deliberately when a
projection/serializer/canonical-domain change intentionally alters the bytes —
here, the agentic->shepherd / metagit->vcscore canonical-domain rename — then
review the fixture diff before committing.

Covers three fixtures:
  - fixtures/golden_conformance_artifacts.json      (conformance/test_golden_artifact.py)
  - fixtures/golden_run_trace_identity_artifacts.json (conformance/test_run_trace_identity_golden.py)
  - fixtures/golden_traces.json                     (trace/test_golden_corpus.py)

The v0_lite positive batch corpus has its own regenerate.py; run that separately.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_TESTS = Path(__file__).resolve().parent
_FIXTURES = _TESTS / "fixtures"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _rebake_conformance_artifact(test_rel: str, fixture_name: str, builder_attr: str, module_name: str) -> None:
    """golden_conformance_artifacts + golden_run_trace_identity share a shape:
    one `artifacts` entry whose `artifact` == conformance_artifact_to_json(builder())."""
    mod = _load(_TESTS / test_rel, module_name)
    from shepherd_kernel_v3_reference.conformance import conformance_artifact_to_json

    fixture_path = _FIXTURES / fixture_name
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    builder = getattr(mod, builder_attr)
    data["artifacts"][0]["artifact"] = conformance_artifact_to_json(builder())
    fixture_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"rebaked {fixture_name}")


def _rebake_golden_traces() -> None:
    mod = _load(_TESTS / "trace" / "test_golden_corpus.py", "kv3_golden_corpus_regen")
    from shepherd_kernel_v3_reference.kernel import elaborate, run_trace
    from shepherd_kernel_v3_reference.trace.serde import trace_to_json

    fixture_path = _FIXTURES / "golden_traces.json"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    cases_by_name = {case.name: case for case in mod.golden_cases()}

    for entry in data["cases"]:
        case = cases_by_name[entry["case"]]
        traced = run_trace(elaborate(case.term), include_debug_evidence=True)
        evidence = traced.require_debug_evidence()
        entry["program_ref"] = evidence.program_ref
        entry["outcome"] = mod._outcome_summary(traced.outcome)
        entry["trace"] = trace_to_json(traced.trace)
        entry["continuation_root_refs"] = list(evidence.continuation_root_refs)
        entry["continuation_objects"] = mod._continuation_objects_to_json(traced)
    fixture_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print("rebaked golden_traces.json")


def main() -> int:
    _rebake_conformance_artifact(
        "conformance/test_golden_artifact.py",
        "golden_conformance_artifacts.json",
        "_golden_artifact",
        "kv3_golden_artifact_regen",
    )
    _rebake_conformance_artifact(
        "conformance/test_run_trace_identity_golden.py",
        "golden_run_trace_identity_artifacts.json",
        "_identity_artifact",
        "kv3_identity_artifact_regen",
    )
    _rebake_golden_traces()
    print("\ndone — review the fixture diff, then run the golden tests to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
