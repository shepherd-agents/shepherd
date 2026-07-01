"""Reader for durable provider-neutral task-trace revisions."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from shepherd_trace_viewer.model import (
    TraceEdge,
    TraceLane,
    TraceNode,
    TraceResource,
    TraceRun,
    TraceSource,
    TraceView,
)

POINTER_KINDS = frozenset({"substrate.transition"})
EVENT_ROUTING_KEYS = frozenset({"id", "kind", "identity_domain", "record_digest", "body"})


class DurableTraceReadError(RuntimeError):
    """Raised when a durable trace payload cannot be read or projected."""


def read_trace_payload_file(path: str | Path) -> TraceView:
    """Read a durable trace revision JSON file into a ``TraceView``."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DurableTraceReadError(f"{p}: {exc}") from exc
    if not isinstance(data, dict):
        raise DurableTraceReadError(f"{p}: trace payload must be a JSON object")
    return read_trace_payload(data)


def read_trace_revision(workspace: str | Path, rev: str | None = None) -> TraceView:
    """Read a durable trace revision through VcsCore's public route.

    ``rev=None`` resolves the trace binding currently selected on the
    workspace's ground world, matching ``VcsCore.read_trace_revision(None)``.
    """
    workspace_path = Path(workspace).resolve()
    repo_path = workspace_path if workspace_path.name == ".vcscore" else workspace_path / ".vcscore"
    if not repo_path.exists():
        raise DurableTraceReadError(f"not a VcsCore repository: {repo_path}")
    try:
        try:
            from vcs_core.runtime_substrate import TaskTraceSubstrateDriver
        except ImportError:
            from vcs_core.experimental import TaskTraceSubstrateDriver
        from vcs_core.runtime_api import Store, VcsCore
    except ImportError as exc:
        raise DurableTraceReadError("vcs_core is required for --trace-rev") from exc

    root = repo_path.parent if repo_path.name == ".vcscore" else workspace_path
    mg = VcsCore(str(root), substrates=[TaskTraceSubstrateDriver()], store=Store(str(repo_path)))
    mg.activate()
    try:
        payload = mg.read_trace_revision(rev)
    except (KeyError, ValueError) as exc:
        raise DurableTraceReadError(f"cannot read trace revision {rev!r}: {exc}") from exc
    finally:
        mg.deactivate()
    if payload is None:
        if rev is None:
            raise DurableTraceReadError("no selected trace revision in workspace")
        raise DurableTraceReadError(f"no trace revision at {rev!r}")
    return read_trace_payload(payload)


def read_trace_payload(payload: Mapping[str, Any]) -> TraceView:
    """Project one durable trace revision payload into the viewer model."""
    _required_str(payload, "trace_runtime")
    _required_str(payload, "trace_owner_id")
    _required_str(payload, "frontier_id")
    events = _events(payload)
    known_ids = {str(event["id"]) for event in events}
    if len(known_ids) != len(events):
        raise DurableTraceReadError("duplicate event ids in trace payload")

    owner_paths = _owner_paths(payload, events)
    lanes = tuple(
        TraceLane(id=owner, label=owner, node_ids=tuple(path))
        for owner, path in owner_paths.items()
    )
    lane_ids_by_node: dict[str, list[str]] = defaultdict(list)
    for lane in lanes:
        for node_id in lane.node_ids:
            if node_id not in known_ids:
                raise DurableTraceReadError(f"owner path {lane.id!r} references unknown event {node_id!r}")
            lane_ids_by_node[node_id].append(lane.id)

    nodes = tuple(
        _node_from_event(event, sequence=index, lane_ids=tuple(lane_ids_by_node.get(str(event["id"]), ())))
        for index, event in enumerate(events)
    )
    edges = tuple(_owner_edges(lanes)) + tuple(_causal_edges(payload, known_ids))
    resources = tuple(_resources(events))
    run = _run(payload, events)
    source = TraceSource(
        trace_runtime=str(payload["trace_runtime"]),
        trace_owner_id=str(payload["trace_owner_id"]),
        frontier_id=str(payload["frontier_id"]),
        source_kind="hybrid_revision",
        identity_domain=_optional_str(payload.get("identity_domain")),
        schema=_optional_str(payload.get("schema")),
        kind=_optional_str(payload.get("kind")),
    )
    return TraceView(source=source, run=run, lanes=lanes, nodes=nodes, edges=edges, resources=resources)


def _required_str(payload: Mapping[str, Any], key: str) -> None:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DurableTraceReadError(f"trace payload requires non-empty string {key!r}")


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _events(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = payload.get("events")
    if not isinstance(raw, list) or not raw:
        raise DurableTraceReadError("trace payload requires non-empty events[]")
    events: list[Mapping[str, Any]] = []
    for index, event in enumerate(raw):
        if not isinstance(event, Mapping):
            raise DurableTraceReadError(f"event {index} is not an object")
        if not isinstance(event.get("id"), str) or not event["id"]:
            raise DurableTraceReadError(f"event {index} needs non-empty string id")
        if not isinstance(event.get("kind"), str) or not event["kind"]:
            raise DurableTraceReadError(f"event {event.get('id')!r} needs non-empty string kind")
        events.append(event)
    return events


def _owner_paths(payload: Mapping[str, Any], events: list[Mapping[str, Any]]) -> dict[str, list[str]]:
    raw = payload.get("owner_paths")
    if isinstance(raw, Mapping) and raw:
        paths: dict[str, list[str]] = {}
        for owner, path in raw.items():
            if not isinstance(owner, str) or not owner:
                raise DurableTraceReadError("owner path id must be a non-empty string")
            if not isinstance(path, list):
                raise DurableTraceReadError(f"owner path {owner!r} must be a list")
            paths[owner] = [str(node_id) for node_id in path]
        return paths
    owner = str(payload["trace_owner_id"])
    return {owner: [str(event["id"]) for event in events]}


def _node_from_event(event: Mapping[str, Any], *, sequence: int, lane_ids: tuple[str, ...]) -> TraceNode:
    kind = str(event["kind"])
    role = "pointer" if kind in POINTER_KINDS else "record"
    payload = {str(k): v for k, v in event.items() if k not in EVENT_ROUTING_KEYS}
    return TraceNode(
        id=str(event["id"]),
        kind=kind,
        family=_family(kind),
        role=role,
        lane_ids=lane_ids,
        sequence=sequence,
        timestamp=_number_or_none(event.get("timestamp")),
        label=_label(event),
        identity_domain=_optional_str(event.get("identity_domain")),
        record_digest=_optional_str(event.get("record_digest")),
        body=dict(event.get("body") or {}),
        payload=payload,
    )


def _family(kind: str) -> str:
    if "." in kind:
        return kind.split(".", 1)[0]
    if kind[:1].isupper():
        return "effect"
    return "event"


def _label(event: Mapping[str, Any]) -> str:
    kind = str(event["kind"])
    name = event.get("name")
    if isinstance(name, str) and name:
        return name
    body = event.get("body")
    if isinstance(body, Mapping) and isinstance(body.get("name"), str) and body["name"]:
        return str(body["name"])
    if kind == "task.invocation" and isinstance(body, Mapping) and body.get("task_id"):
        return str(body["task_id"])
    if kind == "model.call":
        suffix = _event_suffix(str(event["id"]), "concept")
        return f"model {suffix}".strip()
    if kind == "judge.score":
        score = event.get("score")
        max_score = event.get("max")
        suffix = _event_suffix(str(event["id"]), "judge")
        if isinstance(score, int | float) and isinstance(max_score, int | float):
            return f"judge {suffix} {score:g}/{max_score:g}".strip()
        return f"judge {suffix}".strip()
    if kind == "substrate.transition":
        binding = event.get("binding", "substrate")
        semantic_op = event.get("semantic_op", "transition")
        return f"{binding} {semantic_op}"
    if kind == "run.lifecycle":
        return str(event.get("terminal_status") or event.get("transition") or kind)
    if kind == "supervisor.decision":
        decision = str(event.get("decision") or "decision")
        op = event.get("op")
        path = event.get("path")
        if isinstance(op, str) and op and isinstance(path, str) and path:
            return f"{decision} {op} {path}"
        return decision
    if kind == "FileCreate" and isinstance(event.get("path"), str):
        return f"create {event['path']}"
    if "path" in event:
        return f"{kind} {event['path']}"
    return kind


def _event_suffix(event_id: str, prefix: str) -> str:
    normalized = event_id.replace("_", "-")
    if normalized.startswith(f"{prefix}-"):
        return normalized[len(prefix) + 1 :].replace("-", " ")
    return ""


def _number_or_none(value: Any) -> float | None:
    return value if isinstance(value, int | float) else None


def _owner_edges(lanes: tuple[TraceLane, ...]) -> list[TraceEdge]:
    edges: list[TraceEdge] = []
    for lane in lanes:
        for index, (source, target) in enumerate(zip(lane.node_ids, lane.node_ids[1:], strict=False)):
            edges.append(
                TraceEdge(
                    id=f"owner:{lane.id}:{index}:{source}->{target}",
                    kind="owner_path",
                    source=source,
                    target=target,
                    label=lane.label,
                )
            )
    return edges


def _causal_edges(payload: Mapping[str, Any], known_ids: set[str]) -> list[TraceEdge]:
    edges: list[TraceEdge] = []
    for index, raw in enumerate(payload.get("causal_edges") or ()):
        try:
            source, target = raw
        except (TypeError, ValueError) as exc:
            raise DurableTraceReadError(f"causal edge {raw!r} must have two entries") from exc
        source, target = str(source), str(target)
        if source not in known_ids or target not in known_ids:
            raise DurableTraceReadError(f"causal edge {raw!r} references unknown event")
        edges.append(
            TraceEdge(
                id=f"causal:{index}:{source}->{target}",
                kind="causal",
                source=source,
                target=target,
                label="causal",
            )
        )
    return edges


def _resources(events: list[Mapping[str, Any]]) -> list[TraceResource]:
    resources: dict[str, TraceResource] = {}
    for event in events:
        if event.get("kind") != "substrate.transition":
            continue
        for key in ("head_from", "head_to"):
            value = event.get(key)
            if isinstance(value, str) and value:
                resources[value] = TraceResource(
                    id=value,
                    kind="world_oid",
                    label=key,
                    detail={"event_id": event["id"], "field": key},
                )
    return list(resources.values())


def _run(payload: Mapping[str, Any], events: list[Mapping[str, Any]]) -> TraceRun:
    lifecycle = next((event for event in events if event.get("kind") == "run.lifecycle"), {})
    transition = next((event for event in events if event.get("kind") == "substrate.transition"), {})
    invocation = next((event for event in events if event.get("kind") == "task.invocation"), {})
    kinds = Counter(str(event["kind"]) for event in events)
    families = Counter(_family(str(event["kind"])) for event in events)
    return TraceRun(
        id=_optional_str(payload.get("run_ref")),
        terminal_status=_optional_str(lifecycle.get("terminal_status")),
        transition=_optional_str(lifecycle.get("transition")),
        summary={
            "events": len(events),
            "lanes": len(payload.get("owner_paths") or {payload.get("trace_owner_id"): []}),
            "edges": len(payload.get("causal_edges") or ())
            + sum(max(0, len(path) - 1) for path in (payload.get("owner_paths") or {}).values()),
            "kinds": dict(sorted(kinds.items())),
            "families": dict(sorted(families.items())),
            "invocation_digest": invocation.get("record_digest"),
            "head_from": transition.get("head_from"),
            "head_to": transition.get("head_to"),
        },
        detail={
            "trace_owner_id": payload.get("trace_owner_id"),
            "frontier_id": payload.get("frontier_id"),
        },
    )
