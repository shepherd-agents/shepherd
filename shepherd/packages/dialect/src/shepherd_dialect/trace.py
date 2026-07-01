"""The dialect's trace component — hybrid run-trace revision payloads (B4b slice 1, W1).

Builds the hybrid shape of `trace-substrate-hybrid.md`: substrate-touching
effects as **pointers** (world-OID citations), pure-semantic events as
**records**, causal edges + owner paths as structure, every non-pointer record
under an explicit `identity_domain` (hoisted to the revision header; per-event
override for fourth-row records).

Identity is the ratified dual-domain model (`trace-identity-dual-domain.md`):

- `run.lifecycle` housekeeping stays `vcscore.canonical.v2` (the header default).
- The **fourth-row** record — `task.invocation`, the cross-run fact
  `{task_id, args_digest, may_profile}` — digests under the harvested
  `shepherd.kernel.canonical.v2` profile (D1: the `shepherd2.kernel` ring,
  runtime-free by the retention-facade guarantee). `may_profile` is the
  **resolved** surface: an omitted declaration and an explicit `Permissive`
  are the same semantic contract, hence the same cross-run fact; the
  declared-vs-defaulted provenance is run housekeeping (`run.lifecycle`'s
  `may_source`), not identity (refines the slice-1 D2 pinned shape).
- `substrate.transition` entries are pointers and carry **no** record digest.

The digest is the **body digest** (`canonical_digest(<body>)`) — the execplan's
pinned shape (D2): full ABI record identity (witness refs + causality) would
fold per-run facts in and destroy exactly the cross-run stability this record
exists to provide. Slice 2 owns the witness question.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from shepherd2.kernel.canonical import CANONICAL_VERSION, canonical_digest

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "CHILD_LAUNCH_REFUSED",
    "CHILD_RUN_COMPLETED",
    "CHILD_VALUE_COMPLETED",
    "SHEPHERD_KERNEL_DOMAIN",
    "TRACE_RUNTIME",
    "VCSCORE_DOMAIN",
    "RunTrace",
    "append_run_trace",
    "build_run_trace_revision",
    "read_run_trace",
    "task_invocation_record",
]


CHILD_RUN_COMPLETED = "child.run.completed"
CHILD_VALUE_COMPLETED = "child.value.completed"
CHILD_LAUNCH_REFUSED = "child.launch.refused"


def append_run_trace(mg: Any, revision: Mapping[str, Any], *, scope: Any = None) -> str:
    """Append one hybrid revision through the canonical route (W3 consumer sugar).

    Rides ``mg.exec("trace", "append", …)`` — the selectable arm of the
    dispatch bridge — and returns the selected revision head.
    """
    target_scope = scope if scope is not None else mg.ground
    outcome = mg.exec("trace", "append", scope=target_scope, payload=dict(revision))
    return outcome.oids[0]


TRACE_RUNTIME = "shepherd.trace.provider-neutral.v1"
VCSCORE_DOMAIN = "vcscore.canonical.v2"
SHEPHERD_KERNEL_DOMAIN = CANONICAL_VERSION # "shepherd.kernel.canonical.v2"


def _lift_selector(selector: Any) -> Callable[[Mapping[str, Any]], bool]:
    """The auto-lift point (triage D1 / forward-compat).

    v1 lifts three forms — a kind string (``"task.invocation"``), an effect
    type (``FileCreate`` — matches recorded-effect entries by type name), or a
    predicate over the event mapping. When the spec's ``Match`` algebra lands
    it compiles to the predicate form and slots into this same parameter;
    the ``Pattern.event`` lifts from exactly these values. No pattern
    machinery lives here — this is selection, not matching structure.
    """
    if isinstance(selector, str):
        return lambda event: event.get("kind") == selector
    if isinstance(selector, type):
        return lambda event: event.get("kind") == selector.__name__
    if callable(selector):
        return selector
    raise TypeError(
        f"trace selector must be a kind string, an effect type, or a predicate; got {type(selector).__name__}"
    )


@dataclass(frozen=True)
class RunTrace:
    """One durable trace revision, read back as a value (B4b slice 3).

    A *view* over the selected revision payload — the store stays
    authoritative; ``RunTrace`` never writes, caches, or re-keys. Verbs are
    pure and O(revision read): ``filter`` (the D1 re-pin of the legacy stream
    *views* question), ``summary`` (the ``debug_summary`` question), and
    ``compare`` (the ``compare_streams`` question, keyed on real cross-run
    identity — the fourth-row digest).
    """

    payload: Mapping[str, Any]

    @property
    def events(self) -> tuple[Mapping[str, Any],...]:
        return tuple(self.payload.get("events") or ())

    @property
    def run_ref(self) -> str | None:
        return self.payload.get("run_ref")

    def filter(self, selector: Any) -> tuple[Mapping[str, Any],...]:
        """Events matching ``selector`` (kind string | effect type | predicate)."""
        predicate = _lift_selector(selector)
        return tuple(event for event in self.events if predicate(event))

    def invocation(self) -> Mapping[str, Any] | None:
        """The fourth-row ``task.invocation`` record, if present."""
        return next((e for e in self.events if e.get("kind") == "task.invocation"), None)

    def summary(self) -> dict[str, Any]:
        """The debug-summary question: counts by kind, terminal, pointers, decisions."""
        kinds: dict[str, int] = {}
        for event in self.events:
            kind = str(event.get("kind"))
            kinds[kind] = kinds.get(kind, 0) + 1
        lifecycle = next((e for e in self.events if e.get("kind") == "run.lifecycle"), None)
        transition = next((e for e in self.events if e.get("kind") == "substrate.transition"), None)
        invocation = self.invocation()
        child_runs = tuple(
            {
                "child_run_ref": event.get("child_run_ref"),
                "child_lifecycle": event.get("child_lifecycle"),
                "child_world_disposition": event.get("child_world_disposition"),
                "child_scope_terminal_status": event.get("child_scope_terminal_status"),
                "child_trace_head": event.get("child_trace_head"),
                "child_operation_id": event.get("child_operation_id"),
                "child_logical_scope_ref": event.get("child_logical_scope_ref"),
                "child_execution_scope_ref": event.get("child_execution_scope_ref"),
                "terminal_status": event.get("terminal_status"),
            }
            for event in self.filter(CHILD_RUN_COMPLETED)
        )
        value_children = tuple(
            {
                "child_run_ref": event.get("child_run_ref"),
                "child_lifecycle": event.get("child_lifecycle"),
                "child_trace_token": event.get("child_trace_token"),
                "evidence_level": event.get("evidence_level"),
                "trace_materialized": event.get("trace_materialized"),
                "ledger_visible": event.get("ledger_visible"),
                "operation_identity_kind": event.get("operation_identity_kind"),
                "terminal_status": event.get("terminal_status"),
            }
            for event in self.filter(CHILD_VALUE_COMPLETED)
        )
        return {
            "run_ref": self.run_ref,
            "kinds": kinds,
            "terminal_status": (lifecycle or {}).get("terminal_status"),
            "head_from": (transition or {}).get("head_from"),
            "head_to": (transition or {}).get("head_to"),
            "supervision": tuple(self.filter("supervisor.decision")),
            "child_runs": child_runs,
            "value_children": value_children,
            "invocation_digest": (invocation or {}).get("record_digest"),
        }

    def compare(self, other: RunTrace) -> dict[str, Any]:
        """The cross-run question, keyed on the fourth row: same fact, or not?

        ``same_invocation=True`` means the two runs are the *same cross-run
        fact* (task+args+effect surface) — diff their lifecycle/decision
        events meaningfully. ``False`` means different invocations; the diff
        is not a re-run comparison and says so first.
        """
        mine, theirs = self.summary(), other.summary()
        kinds = set(mine["kinds"]) | set(theirs["kinds"])
        return {
            "same_invocation": (
                mine["invocation_digest"] is not None
                and mine["invocation_digest"] == theirs["invocation_digest"]
            ),
            "invocation_digest": (mine["invocation_digest"], theirs["invocation_digest"]),
            "terminal_status": (mine["terminal_status"], theirs["terminal_status"]),
            "kind_count_delta": {
                kind: theirs["kinds"].get(kind, 0) - mine["kinds"].get(kind, 0)
                for kind in sorted(kinds)
                if theirs["kinds"].get(kind, 0) != mine["kinds"].get(kind, 0)
            },
            "supervision": (mine["supervision"], theirs["supervision"]),
        }


def read_run_trace(mg: Any, head: str | None = None) -> RunTrace | None:
    """Read one durable trace revision as a ``RunTrace`` (W1's public route).

    Rides ``VcsCore.read_trace_revision`` — the read-only Group E query —
    replacing the demo's spike-tier ``_world_storage()`` private reach.
    ``head=None`` reads the trace binding's currently selected head.
    """
    payload = mg.read_trace_revision(head)
    return None if payload is None else RunTrace(payload)


def task_invocation_record(
    *,
    task_id: str,
    args: Mapping[str, Any] | None,
    may_profile: str,
) -> dict[str, Any]:
    """The fourth-row record: the run's cross-run identity (the pattern-cache key).

    Same task + args + effect surface in two runs ⇒ the same `record_digest`
    — the profile is included deliberately (the capability surface is part of
    the semantic contract; execplan D2). It is the **resolved** profile: how
    the surface was arrived at (declared vs defaulted) is provenance, carried
    by `run.lifecycle.may_source`, never folded into the cross-run key.
    """
    body = {
        "task_id": task_id,
        "args_digest": canonical_digest({"args": dict(args or {})}),
        "may_profile": may_profile,
    }
    return {
        "id": "task-invocation",
        "kind": "task.invocation",
        "identity_domain": SHEPHERD_KERNEL_DOMAIN,
        "record_digest": canonical_digest(body),
        "body": body,
    }


def build_run_trace_revision(
    *,
    run_ref: str,
    trace_owner_id: str,
    frontier_id: str,
    task_id: str,
    args: Mapping[str, Any] | None,
    may_profile: str,
    may_source: str = "declared",
    terminal_status: str,
    input_world_oid: str | None,
    output_world_oid: str | None,
    operation_id: str | None = None,
    extra_events: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """One run's hybrid trace-revision payload, for both terminal outcomes.

    ``terminal_status`` is ``"merged"`` or ``"discarded"``; on a discarded run
    ``output_world_oid`` is ``None`` (the `VcsCoreExecutionLink` convention:
    no output world was produced). The revision is appended *post-run* either
    way — trace records outlive discarded workspace state (v1 invariant 3).

    ``may_profile`` is the resolved effect surface (keys the fourth-row
    record); ``may_source`` (``"declared"``/``"defaulted"``) is recorded on
    the lifecycle event so the defaulted-Permissive population stays
    countable in the durable trace.
    """
    invocation = task_invocation_record(task_id=task_id, args=args, may_profile=may_profile)
    transition = {
        "id": "workspace-transition",
        "kind": "substrate.transition", # pointer half: cites world OIDs, no record digest
        "binding": "workspace",
        "head_from": input_world_oid,
        "head_to": output_world_oid,
        "semantic_op": "run",
        "operation_id": operation_id,
    }
    lifecycle = {
        "id": "run-lifecycle",
        "kind": "run.lifecycle",
        "transition": _lifecycle_transition_for_terminal_status(terminal_status),
        "terminal_status": terminal_status,
        "may_profile": may_profile,
        "may_source": may_source,
    }
    middle: list[dict[str, Any]] = []
    for index, event in enumerate(extra_events):
        entry = dict(event)
        entry.setdefault("id", f"e{index + 1}")
        middle.append(entry)
    events = [invocation, transition, *middle, lifecycle]
    ids = [event["id"] for event in events]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate trace event ids: {ids!r}")
    return {
        "trace_runtime": TRACE_RUNTIME,
        "trace_owner_id": trace_owner_id,
        "frontier_id": frontier_id,
        "run_ref": run_ref,
        "identity_domain": VCSCORE_DOMAIN, # hoisted header; fourth-row events override
        "events": events,
        "causal_edges": [[a, b] for a, b in pairwise(ids)],
        "owner_paths": {trace_owner_id: ids},
    }


def _lifecycle_transition_for_terminal_status(terminal_status: str) -> str:
    if terminal_status == "merged":
        return "finished"
    if terminal_status == "retained":
        return "retained"
    return "failed"
