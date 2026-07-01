from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from vcs_core._dirty_flag import read_dirty_flag
from vcs_core._materialization_run import read_materialization_run
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.materialization import MaterializationPreflightError
from vcs_core.sqlite_substrate import SQLiteSubstrate
from vcs_core.store import Store
from vcs_core.substrates import MarkerSubstrate
from vcs_core.vcscore import VcsCore

from ..support.builders import make_marker_filesystem_substrates


def _fetch_all(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        return cursor.fetchall()
    finally:
        conn.close()


def _vcscore_with_sqlite(workspace: Path, db_path: Path) -> tuple[VcsCore, SQLiteSubstrate]:
    store = Store(str(workspace / ".vcscore"))
    marker = MarkerSubstrate(build_builtin_substrate_context(store))
    sqlite_substrate = SQLiteSubstrate(build_builtin_substrate_context(store), db_path=db_path)
    mg = VcsCore(str(workspace), substrates=[marker, sqlite_substrate], store=store)
    mg.activate()
    return mg, sqlite_substrate


def _vcscore_with_two_sqlite(
    workspace: Path,
    db_path_a: Path,
    db_path_b: Path,
) -> tuple[VcsCore, SQLiteSubstrate, SQLiteSubstrate]:
    store = Store(str(workspace / ".vcscore"))
    marker = MarkerSubstrate(build_builtin_substrate_context(store))
    sqlite_a = SQLiteSubstrate(build_builtin_substrate_context(store), db_path=db_path_a)
    sqlite_b = SQLiteSubstrate(build_builtin_substrate_context(store), db_path=db_path_b)
    mg = VcsCore(str(workspace), substrates=[marker, sqlite_a, sqlite_b], store=store)
    mg.activate()
    return mg, sqlite_a, sqlite_b


def _vcscore_with_filesystem_and_sqlite(workspace: Path, db_path: Path) -> tuple[VcsCore, SQLiteSubstrate]:
    store = Store(str(workspace / ".vcscore"))
    marker, filesystem = make_marker_filesystem_substrates(store)
    sqlite_substrate = SQLiteSubstrate(build_builtin_substrate_context(store), db_path=db_path)
    mg = VcsCore(str(workspace), substrates=[marker, filesystem, sqlite_substrate], store=store)
    mg.activate()
    return mg, sqlite_substrate


def _record_buffered_sql(
    mg: VcsCore,
    *,
    scope_name: str,
    sqlite_substrate: SQLiteSubstrate,
    statements: list[tuple[str, str]],
) -> None:
    task = mg.fork(mg.ground, scope_name)
    for carrier_seq, (sql, kind) in enumerate(statements):
        mg.store._emit_effect(
            task,
            "SqlStatementBuffered",
            {
                "target_id": sqlite_substrate._target_id,
                "basis_token": sqlite_substrate.current_basis_token(),
                "carrier_scope": task.name,
                "carrier_seq": carrier_seq,
                "materializer_key": sqlite_substrate.materializer_key,
                "sql": sql,
                "kind": kind,
                "params": None,
            },
            substrate="sqlite",
        )
    mg.merge(task, mg.ground)


def test_sqlite_query_sees_buffered_write_in_same_scope(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-sql")
        mg.exec("sqlite", "execute", scope=task, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('alpha')")

        outcome = mg.exec("sqlite", "query", scope=task, sql="SELECT name FROM items ORDER BY name")

        assert outcome.value == {
            "columns": ["name"],
            "rows": [["alpha"]],
            "row_count": 1,
        }
        assert not db_path.exists()
    finally:
        mg.deactivate()


@pytest.mark.parametrize(
    ("command", "sql", "message"),
    [
        ("query", "INSERT INTO items (name) VALUES ('alpha')", "read-only"),
        ("execute", "SELECT name FROM sqlite_master", "mutating"),
        ("query", "SELECT 1; SELECT 2", "multi-statement"),
        ("execute", "BEGIN", "reject BEGIN"),
        ("execute", "ATTACH DATABASE 'other.db' AS other", "reject ATTACH"),
        ("execute", "PRAGMA journal_mode=WAL", "PRAGMA"),
        ("query", "PRAGMA journal_mode", "PRAGMA"),
        ("query", "PRAGMA wal_checkpoint(FULL)", "PRAGMA"),
        ("query", "PRAGMA incremental_vacuum(10)", "PRAGMA"),
        ("query", "PRAGMA optimize", "PRAGMA"),
        ("query", "WITH c AS (SELECT 1)", "ambiguous WITH"),
        ("execute", "CREATE TEMP TABLE temp_items (name TEXT)", "unsupported CREATE"),
        ("execute", "DROP VIEW IF EXISTS named_view", "unsupported DROP"),
    ],
)
def test_sqlite_rejects_invalid_command_shapes_before_execution(
    workspace: Path,
    command: str,
    sql: str,
    message: str,
) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-invalid-sql")

        with pytest.raises(ValueError, match=message):
            mg.exec("sqlite", command, scope=task, sql=sql)

        assert task.name not in sqlite_substrate._carriers
        assert mg.store.filter_effects(substrate="sqlite", ref=task.ref) == []
    finally:
        mg.deactivate()


def test_sqlite_query_accepts_read_only_pragma(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-pragma-query")
        outcome = mg.exec("sqlite", "query", scope=task, sql="PRAGMA table_info(items)")

        assert outcome.value == {
            "columns": ["cid", "name", "type", "notnull", "dflt_value", "pk"],
            "rows": [],
            "row_count": 0,
        }
    finally:
        mg.deactivate()


def test_sqlite_query_accepts_with_select(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-with-query")
        outcome = mg.exec("sqlite", "query", scope=task, sql="WITH c(value) AS (SELECT 1) SELECT value FROM c")

        assert outcome.value == {
            "columns": ["value"],
            "rows": [[1]],
            "row_count": 1,
        }
    finally:
        mg.deactivate()


@pytest.mark.parametrize(
    ("command", "sql", "message"),
    [
        ("query", "INSERT INTO items (name) VALUES ('alpha')", "read-only"),
        ("execute", "SELECT 1", "mutating"),
    ],
)
def test_sqlite_direct_execute_rejects_invalid_command_shapes_before_carrier_creation(
    workspace: Path,
    command: str,
    sql: str,
    message: str,
) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-direct-invalid-sql")

        with pytest.raises(ValueError, match=message):
            sqlite_substrate.execute(command, task, sql=sql)

        assert task.name not in sqlite_substrate._carriers
        assert mg.store.filter_effects(substrate="sqlite", ref=task.ref) == []
    finally:
        mg.deactivate()


def test_sqlite_direct_query_still_allows_valid_read_only_statement(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-direct-query")

        outcome = sqlite_substrate.execute("query", task, sql="SELECT 1 AS value")

        assert outcome.value == {
            "columns": ["value"],
            "rows": [[1]],
            "row_count": 1,
        }
        assert task.name in sqlite_substrate._carriers
        assert outcome.effects[-1].effect_type == "SqlQueryObserved"
        assert mg.store.filter_effects(substrate="sqlite", ref=task.ref) == []
    finally:
        mg.deactivate()


def test_sqlite_registers_claims_for_db_and_runtime_sidecars(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        for path in (db_path, *SQLiteSubstrate._sidecar_paths(db_path)):
            claim = sqlite_substrate._runtime.lookup_claim(path)
            assert claim is not None
            assert claim.policy == "exclusive"

        task = mg.fork(mg.ground, "task-claims")
        mg.exec("sqlite", "execute", scope=task, sql="CREATE TABLE items (name TEXT)")
        runtime_path = sqlite_substrate._carriers[task.name].runtime_path

        for path in (runtime_path, *SQLiteSubstrate._sidecar_paths(runtime_path)):
            claim = sqlite_substrate._runtime.lookup_claim(path)
            assert claim is not None
            assert claim.policy == "authoritative_suppress_fs"
    finally:
        mg.deactivate()


def test_sqlite_exec_rejects_substrate_type_selection_for_multiple_instances(workspace: Path) -> None:
    db_path_a = workspace / "a.db"
    db_path_b = workspace / "b.db"
    mg, _sqlite_a, _sqlite_b = _vcscore_with_two_sqlite(workspace, db_path_a, db_path_b)
    try:
        task = mg.fork(mg.ground, "task-ambiguous")

        with pytest.raises(ValueError, match="Unknown binding 'sqlite'"):
            mg.exec("sqlite", "query", scope=task, sql="SELECT 1")
        outcome = mg.exec("sqlite-1", "query", scope=task, sql="SELECT 1")
        assert outcome.value == {"columns": ["1"], "rows": [[1]], "row_count": 1}
    finally:
        mg.deactivate()


def test_sqlite_multiple_instances_do_not_conflict_in_push_materializers(workspace: Path) -> None:
    db_path_a = workspace / "a.db"
    db_path_b = workspace / "b.db"
    mg, sqlite_a, sqlite_b = _vcscore_with_two_sqlite(workspace, db_path_a, db_path_b)
    try:
        _record_buffered_sql(
            mg,
            scope_name="task-a",
            sqlite_substrate=sqlite_a,
            statements=[
                ("CREATE TABLE items (name TEXT)", "CREATE"),
                ("INSERT INTO items (name) VALUES ('alpha')", "INSERT"),
            ],
        )
        _record_buffered_sql(
            mg,
            scope_name="task-b",
            sqlite_substrate=sqlite_b,
            statements=[
                ("CREATE TABLE items (name TEXT)", "CREATE"),
                ("INSERT INTO items (name) VALUES ('beta')", "INSERT"),
            ],
        )

        plan = mg.push()

        assert plan.total_operations == 4
        assert _fetch_all(db_path_a, "SELECT name FROM items ORDER BY name") == [("alpha",)]
        assert _fetch_all(db_path_b, "SELECT name FROM items ORDER BY name") == [("beta",)]
    finally:
        mg.deactivate()


def test_sqlite_effects_record_instance_scoped_materializer_key(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-materializer-key")
        outcome = mg.exec("sqlite", "execute", scope=task, sql="CREATE TABLE items (name TEXT)")

        assert outcome.value == {"rowcount": -1}
        buffered = mg.store.filter_effects(effect_type="SqlStatementBuffered", substrate="sqlite", ref=task.ref)
        assert len(buffered) == 1
        assert buffered[0].metadata["materializer_key"] == sqlite_substrate.materializer_key
        assert buffered[0].metadata["materializer_key"] != "builtin:sqlite"
    finally:
        mg.deactivate()


def test_sqlite_push_clears_internal_materialization_state_under_filesystem_patches(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, _sqlite = _vcscore_with_filesystem_and_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-filesystem-push")
        mg.exec("sqlite", "execute", scope=task, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('alpha')")
        mg.merge(task, mg.ground)

        plan = mg.push()

        assert plan.total_operations == 2
        assert _fetch_all(db_path, "SELECT name FROM items ORDER BY name") == [("alpha",)]
        assert read_materialization_run(str(workspace / ".vcscore")) is None
        assert read_dirty_flag(str(workspace / ".vcscore")) is None
    finally:
        mg.deactivate()


def test_sqlite_exec_on_existing_db_uses_control_plane_guard_under_filesystem_patches(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE items (name TEXT)")
        conn.execute("INSERT INTO items VALUES ('alpha')")
        conn.commit()
    finally:
        conn.close()

    mg, _sqlite = _vcscore_with_filesystem_and_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "sql-stale")

        outcome = mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items VALUES ('pending')")

        assert outcome.value == {"rowcount": 1}
        assert _fetch_all(db_path, "SELECT name FROM items ORDER BY name") == [("alpha",)]
    finally:
        mg.deactivate()


def test_sqlite_wal_mode_bootstrap_reads_live_db_snapshot(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE items (name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES ('alpha')")
        conn.commit()

        mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
        try:
            task = mg.fork(mg.ground, "task-wal-bootstrap", hints={"isolated": True})
            outcome = mg.exec("sqlite", "query", scope=task, sql="SELECT name FROM items ORDER BY name")

            assert outcome.value == {
                "columns": ["name"],
                "rows": [["alpha"]],
                "row_count": 1,
            }
        finally:
            mg.deactivate()
    finally:
        conn.close()


def test_sqlite_nonisolated_child_reuses_parent_carrier(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "tool-child", hints={"isolated": False})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('shared')")

        outcome = mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items")

        parent_carrier = sqlite_substrate._carriers[parent.name]
        assert outcome.value == {"columns": ["name"], "rows": [["shared"]], "row_count": 1}
        assert sqlite_substrate._runtime.nearest_carrier_scope("sqlite", sqlite_substrate._target_id, child) == parent
        assert parent_carrier.scope.name == parent.name
    finally:
        mg.deactivate()


def test_sqlite_isolated_child_gets_independent_carrier_after_parent_activity(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "task-child", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('inherited')")

        child_query = mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items ORDER BY name")
        parent_query_before = mg.exec("sqlite", "query", scope=parent, sql="SELECT name FROM items ORDER BY name")

        assert child_query.value == {"columns": ["name"], "rows": [["inherited"]], "row_count": 1}
        assert parent_query_before.value == {"columns": ["name"], "rows": [["inherited"]], "row_count": 1}
        assert set(sqlite_substrate._carriers) == {parent.name, child.name}

        fork_markers = mg.store.filter_effects(effect_type="SqlCarrierForked", substrate="sqlite", ref=child.ref)
        assert len(fork_markers) == 1
        assert fork_markers[0].metadata["parent_carrier_scope"] == parent.name
        assert fork_markers[0].metadata["child_carrier_scope"] == child.name
        assert fork_markers[0].metadata["parent_scope_ref"] == parent.ref
        assert fork_markers[0].metadata["parent_creation_oid"] == parent.creation_oid
        assert isinstance(fork_markers[0].metadata["parent_visible_frontier"], str)
        assert fork_markers[0].metadata["base_seq"] == 1

        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('parent-late')")
        mg.exec("sqlite", "execute", scope=child, sql="INSERT INTO items (name) VALUES ('child-only')")

        parent_query_after = mg.exec("sqlite", "query", scope=parent, sql="SELECT name FROM items ORDER BY name")
        child_query_after = mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items ORDER BY name")

        assert parent_query_after.value == {
            "columns": ["name"],
            "rows": [["inherited"], ["parent-late"]],
            "row_count": 2,
        }
        assert child_query_after.value == {
            "columns": ["name"],
            "rows": [["child-only"], ["inherited"]],
            "row_count": 2,
        }
    finally:
        mg.deactivate()


def test_sqlite_isolated_child_runtime_rebuild_uses_lineage_cutoff(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "task-child", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('inherited')")
        mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items ORDER BY name")
        mg.exec("sqlite", "execute", scope=child, sql="INSERT INTO items (name) VALUES ('child-only')")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('parent-late')")

        child_runtime = sqlite_substrate._carriers[child.name].runtime_path
        child_runtime.unlink()

        rebuilt = mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items ORDER BY name")

        assert rebuilt.value == {
            "columns": ["name"],
            "rows": [["child-only"], ["inherited"]],
            "row_count": 2,
        }
    finally:
        mg.deactivate()


def test_sqlite_nonisolated_child_reuses_parent_carrier_after_reactivation(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "tool-child", hints={"isolated": False})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('shared')")
    finally:
        runtime_root = sqlite_substrate._runtime_root
        mg.deactivate()

    shutil.rmtree(runtime_root, ignore_errors=True)

    mg2, sqlite2 = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent2 = mg2.restore_scope(
            name=parent.name,
            ref=parent.ref,
            instance_id=parent.instance_id,
            creation_oid=parent.creation_oid,
            world_id=parent.world_id,
            parent=mg2.ground,
            isolated=True,
        )
        child2 = mg2.restore_scope(
            name=child.name,
            ref=child.ref,
            instance_id=child.instance_id,
            creation_oid=child.creation_oid,
            world_id=child.world_id,
            parent=parent2,
            isolated=False,
        )

        outcome = mg2.exec("sqlite", "query", scope=child2, sql="SELECT name FROM items ORDER BY name")

        assert outcome.value == {"columns": ["name"], "rows": [["shared"]], "row_count": 1}
        assert sqlite2._runtime.nearest_carrier_scope("sqlite", sqlite2._target_id, child2) == parent2
    finally:
        mg2.deactivate()


def test_sqlite_read_only_isolated_root_refreshes_runtime_cache_after_reactivation(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.execute("INSERT INTO items (name) VALUES ('base')")
    conn.commit()
    conn.close()

    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-read-only", hints={"isolated": True})
        outcome = mg.exec("sqlite", "query", scope=task, sql="SELECT name FROM items ORDER BY name")
        assert outcome.value == {
            "columns": ["name"],
            "rows": [["base"]],
            "row_count": 1,
        }
    finally:
        mg.deactivate()

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO items (name) VALUES ('external')")
    conn.commit()
    conn.close()

    mg2, _sqlite2 = _vcscore_with_sqlite(workspace, db_path)
    try:
        task2 = mg2.restore_scope(
            name=task.name,
            ref=task.ref,
            instance_id=task.instance_id,
            creation_oid=task.creation_oid,
            world_id=task.world_id,
            parent=mg2.ground,
            isolated=True,
        )

        outcome = mg2.exec("sqlite", "query", scope=task2, sql="SELECT name FROM items ORDER BY name")

        assert outcome.value == {
            "columns": ["name"],
            "rows": [["base"], ["external"]],
            "row_count": 2,
        }
    finally:
        mg2.deactivate()


def test_sqlite_isolated_child_lineage_rebuild_survives_reactivation(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "task-child", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('inherited')")
        mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items ORDER BY name")
        mg.exec("sqlite", "execute", scope=child, sql="INSERT INTO items (name) VALUES ('child-only')")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('parent-late')")
    finally:
        runtime_root = sqlite_substrate._runtime_root
        mg.deactivate()

    shutil.rmtree(runtime_root, ignore_errors=True)

    mg2, sqlite2 = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent2 = mg2.restore_scope(
            name=parent.name,
            ref=parent.ref,
            instance_id=parent.instance_id,
            creation_oid=parent.creation_oid,
            world_id=parent.world_id,
            parent=mg2.ground,
            isolated=True,
        )
        child2 = mg2.restore_scope(
            name=child.name,
            ref=child.ref,
            instance_id=child.instance_id,
            creation_oid=child.creation_oid,
            world_id=child.world_id,
            parent=parent2,
            isolated=True,
        )

        child_outcome = mg2.exec("sqlite", "query", scope=child2, sql="SELECT name FROM items ORDER BY name")
        parent_outcome = mg2.exec("sqlite", "query", scope=parent2, sql="SELECT name FROM items ORDER BY name")

        assert child_outcome.value == {
            "columns": ["name"],
            "rows": [["child-only"], ["inherited"]],
            "row_count": 2,
        }
        assert parent_outcome.value == {
            "columns": ["name"],
            "rows": [["inherited"], ["parent-late"]],
            "row_count": 2,
        }
        assert set(sqlite2._carriers) == {parent2.name, child2.name}
    finally:
        mg2.deactivate()


def test_sqlite_isolated_child_restore_parent_first_preserves_child_lineage(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "task-child", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('inherited')")
        mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items ORDER BY name")
        mg.exec("sqlite", "execute", scope=child, sql="INSERT INTO items (name) VALUES ('child-only')")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('parent-late')")
    finally:
        runtime_root = sqlite_substrate._runtime_root
        mg.deactivate()

    shutil.rmtree(runtime_root, ignore_errors=True)

    mg2, sqlite2 = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent2 = mg2.restore_scope(
            name=parent.name,
            ref=parent.ref,
            instance_id=parent.instance_id,
            creation_oid=parent.creation_oid,
            world_id=parent.world_id,
            parent=mg2.ground,
            isolated=True,
        )
        child2 = mg2.restore_scope(
            name=child.name,
            ref=child.ref,
            instance_id=child.instance_id,
            creation_oid=child.creation_oid,
            world_id=child.world_id,
            parent=parent2,
            isolated=True,
        )

        parent_outcome = mg2.exec("sqlite", "query", scope=parent2, sql="SELECT name FROM items ORDER BY name")
        child_outcome = mg2.exec("sqlite", "query", scope=child2, sql="SELECT name FROM items ORDER BY name")

        assert parent_outcome.value == {
            "columns": ["name"],
            "rows": [["inherited"], ["parent-late"]],
            "row_count": 2,
        }
        assert child_outcome.value == {
            "columns": ["name"],
            "rows": [["child-only"], ["inherited"]],
            "row_count": 2,
        }
        assert set(sqlite2._carriers) == {parent2.name, child2.name}
    finally:
        mg2.deactivate()


def test_sqlite_isolated_child_rebuild_survives_reactivation_without_restoring_parent(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "task-child", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('inherited')")
        mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items ORDER BY name")
        mg.exec("sqlite", "execute", scope=child, sql="INSERT INTO items (name) VALUES ('child-only')")
    finally:
        runtime_root = sqlite_substrate._runtime_root
        mg.deactivate()

    shutil.rmtree(runtime_root, ignore_errors=True)

    mg2, _sqlite2 = _vcscore_with_sqlite(workspace, db_path)
    try:
        child2 = mg2.restore_scope(
            name=child.name,
            ref=child.ref,
            instance_id=child.instance_id,
            creation_oid=child.creation_oid,
            world_id=child.world_id,
            parent=mg2.ground,
            isolated=True,
        )

        child_outcome = mg2.exec("sqlite", "query", scope=child2, sql="SELECT name FROM items ORDER BY name")

        assert child_outcome.value == {
            "columns": ["name"],
            "rows": [["child-only"], ["inherited"]],
            "row_count": 2,
        }
    finally:
        mg2.deactivate()


def test_sqlite_parent_query_after_child_merge_rebuilds_from_merged_history(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('parent')")

        child = mg.fork(parent, "task-child", hints={"isolated": True})
        mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items ORDER BY name")
        mg.exec("sqlite", "execute", scope=child, sql="INSERT INTO items (name) VALUES ('child')")

        mg.merge(child, parent)
        outcome = mg.exec("sqlite", "query", scope=parent, sql="SELECT name FROM items ORDER BY name")

        assert outcome.value == {
            "columns": ["name"],
            "rows": [["child"], ["parent"]],
            "row_count": 2,
        }
        assert set(sqlite_substrate._carriers) == {parent.name}
    finally:
        mg.deactivate()


def test_sqlite_parent_rebuilds_from_merged_child_history_without_parent_writes(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "task-child", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=child, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=child, sql="INSERT INTO items (name) VALUES ('child')")

        mg.merge(child, parent)
        sqlite_substrate._invalidate_runtime_caches()

        outcome = mg.exec("sqlite", "query", scope=parent, sql="SELECT name FROM items ORDER BY name")

        assert outcome.value == {
            "columns": ["name"],
            "rows": [["child"]],
            "row_count": 1,
        }
    finally:
        mg.deactivate()


def test_sqlite_merged_child_history_rebuilds_after_reactivation(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('parent')")

        child = mg.fork(parent, "task-child", hints={"isolated": True})
        mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items ORDER BY name")
        mg.exec("sqlite", "execute", scope=child, sql="INSERT INTO items (name) VALUES ('child')")
        mg.merge(child, parent)
    finally:
        runtime_root = sqlite_substrate._runtime_root
        mg.deactivate()

    shutil.rmtree(runtime_root, ignore_errors=True)

    mg2, _sqlite2 = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent2 = mg2.restore_scope(
            name=parent.name,
            ref=parent.ref,
            instance_id=parent.instance_id,
            creation_oid=parent.creation_oid,
            world_id=parent.world_id,
            parent=mg2.ground,
            isolated=True,
        )

        outcome = mg2.exec("sqlite", "query", scope=parent2, sql="SELECT name FROM items ORDER BY name")

        assert outcome.value == {
            "columns": ["name"],
            "rows": [["child"], ["parent"]],
            "row_count": 2,
        }
    finally:
        mg2.deactivate()


def test_sqlite_merged_child_only_history_rebuilds_after_reactivation(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "task-child", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=child, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=child, sql="INSERT INTO items (name) VALUES ('child')")
        mg.merge(child, parent)
    finally:
        runtime_root = sqlite_substrate._runtime_root
        mg.deactivate()

    shutil.rmtree(runtime_root, ignore_errors=True)

    mg2, _sqlite2 = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent2 = mg2.restore_scope(
            name=parent.name,
            ref=parent.ref,
            instance_id=parent.instance_id,
            creation_oid=parent.creation_oid,
            world_id=parent.world_id,
            parent=mg2.ground,
            isolated=True,
        )

        outcome = mg2.exec("sqlite", "query", scope=parent2, sql="SELECT name FROM items ORDER BY name")

        assert outcome.value == {
            "columns": ["name"],
            "rows": [["child"]],
            "row_count": 1,
        }
    finally:
        mg2.deactivate()


def test_sqlite_second_generation_child_cold_rebuild_preserves_merged_inherited_state(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('parent')")

        child1 = mg.fork(parent, "task-child-1", hints={"isolated": True})
        mg.exec("sqlite", "query", scope=child1, sql="SELECT name FROM items ORDER BY name")
        mg.exec("sqlite", "execute", scope=child1, sql="INSERT INTO items (name) VALUES ('child1')")
        mg.merge(child1, parent)

        child2 = mg.fork(parent, "task-child-2", hints={"isolated": True})
        warm = mg.exec("sqlite", "query", scope=child2, sql="SELECT name FROM items ORDER BY name")
        assert warm.value == {
            "columns": ["name"],
            "rows": [["child1"], ["parent"]],
            "row_count": 2,
        }

        fork_markers = [
            marker
            for marker in mg.store.filter_effects(effect_type="SqlCarrierForked", substrate="sqlite", ref=child2.ref)
            if marker.metadata.get("child_carrier_scope") == child2.name
        ]
        assert len(fork_markers) == 1
        assert fork_markers[0].metadata["parent_carrier_scope"] == parent.name
        assert fork_markers[0].metadata["parent_scope_ref"] == parent.ref
        assert fork_markers[0].metadata["parent_creation_oid"] == parent.creation_oid
        assert isinstance(fork_markers[0].metadata["parent_visible_frontier"], str)

        runtime_path = sqlite_substrate._carriers[child2.name].runtime_path
        runtime_path.unlink()
        cold = mg.exec("sqlite", "query", scope=child2, sql="SELECT name FROM items ORDER BY name")

        assert cold.value == warm.value

        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('parent-late')")
        child_after_parent = mg.exec("sqlite", "query", scope=child2, sql="SELECT name FROM items ORDER BY name")
        assert child_after_parent.value == warm.value
    finally:
        mg.deactivate()


def test_sqlite_second_generation_child_rebuild_survives_reactivation(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('parent')")

        child1 = mg.fork(parent, "task-child-1", hints={"isolated": True})
        mg.exec("sqlite", "query", scope=child1, sql="SELECT name FROM items ORDER BY name")
        mg.exec("sqlite", "execute", scope=child1, sql="INSERT INTO items (name) VALUES ('child1')")
        mg.merge(child1, parent)

        child2 = mg.fork(parent, "task-child-2", hints={"isolated": True})
        mg.exec("sqlite", "query", scope=child2, sql="SELECT name FROM items ORDER BY name")
    finally:
        runtime_root = sqlite_substrate._runtime_root
        mg.deactivate()

    shutil.rmtree(runtime_root, ignore_errors=True)

    mg2, sqlite2 = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent2 = mg2.restore_scope(
            name=parent.name,
            ref=parent.ref,
            instance_id=parent.instance_id,
            creation_oid=parent.creation_oid,
            world_id=parent.world_id,
            parent=mg2.ground,
            isolated=True,
        )
        child2_restored = mg2.restore_scope(
            name=child2.name,
            ref=child2.ref,
            instance_id=child2.instance_id,
            creation_oid=child2.creation_oid,
            world_id=child2.world_id,
            parent=parent2,
            isolated=True,
        )

        outcome = mg2.exec("sqlite", "query", scope=child2_restored, sql="SELECT name FROM items ORDER BY name")

        assert outcome.value == {
            "columns": ["name"],
            "rows": [["child1"], ["parent"]],
            "row_count": 2,
        }

        mg2.exec("sqlite", "execute", scope=parent2, sql="INSERT INTO items (name) VALUES ('parent-late')")
        child_after_parent = mg2.exec(
            "sqlite",
            "query",
            scope=child2_restored,
            sql="SELECT name FROM items ORDER BY name",
        )
        assert child_after_parent.value == outcome.value
        assert set(sqlite2._carriers) == {parent2.name, child2_restored.name}
    finally:
        mg2.deactivate()


def test_sqlite_second_generation_child_restore_parent_first_preserves_child_lineage(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('parent')")

        child1 = mg.fork(parent, "task-child-1", hints={"isolated": True})
        mg.exec("sqlite", "query", scope=child1, sql="SELECT name FROM items ORDER BY name")
        mg.exec("sqlite", "execute", scope=child1, sql="INSERT INTO items (name) VALUES ('child1')")
        mg.merge(child1, parent)

        child2 = mg.fork(parent, "task-child-2", hints={"isolated": True})
        mg.exec("sqlite", "query", scope=child2, sql="SELECT name FROM items ORDER BY name")
    finally:
        runtime_root = sqlite_substrate._runtime_root
        mg.deactivate()

    shutil.rmtree(runtime_root, ignore_errors=True)

    mg2, sqlite2 = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent2 = mg2.restore_scope(
            name=parent.name,
            ref=parent.ref,
            instance_id=parent.instance_id,
            creation_oid=parent.creation_oid,
            world_id=parent.world_id,
            parent=mg2.ground,
            isolated=True,
        )
        child2_restored = mg2.restore_scope(
            name=child2.name,
            ref=child2.ref,
            instance_id=child2.instance_id,
            creation_oid=child2.creation_oid,
            world_id=child2.world_id,
            parent=parent2,
            isolated=True,
        )

        parent_outcome = mg2.exec("sqlite", "query", scope=parent2, sql="SELECT name FROM items ORDER BY name")
        child_outcome = mg2.exec("sqlite", "query", scope=child2_restored, sql="SELECT name FROM items ORDER BY name")

        assert parent_outcome.value == {
            "columns": ["name"],
            "rows": [["child1"], ["parent"]],
            "row_count": 2,
        }
        assert child_outcome.value == {
            "columns": ["name"],
            "rows": [["child1"], ["parent"]],
            "row_count": 2,
        }

        mg2.exec("sqlite", "execute", scope=parent2, sql="INSERT INTO items (name) VALUES ('parent-late')")
        child_after_parent = mg2.exec(
            "sqlite",
            "query",
            scope=child2_restored,
            sql="SELECT name FROM items ORDER BY name",
        )
        assert child_after_parent.value == child_outcome.value
        assert set(sqlite2._carriers) == {parent2.name, child2_restored.name}
    finally:
        mg2.deactivate()


def test_sqlite_push_replays_committed_buffered_statements(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-push")
        mg.exec("sqlite", "execute", scope=task, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('pushed')")
        mg.merge(task, mg.ground)

        plan = mg.push()

        assert plan.total_operations == 2
        assert _fetch_all(db_path, "SELECT name FROM items ORDER BY name") == [("pushed",)]
    finally:
        mg.deactivate()


def test_sqlite_push_fails_before_side_effects_when_basis_is_stale(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.commit()
    conn.close()

    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-stale")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('pending')")
        mg.merge(task, mg.ground)

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO items (name) VALUES ('external')")
        conn.commit()
        conn.close()

        with pytest.raises(MaterializationPreflightError, match="status='conflicted'"):
            mg.push()

        reconcile = mg.store.filter_effects(effect_type="SqlReconcileRecorded", substrate="sqlite")
        assert len(reconcile) == 1
        assert reconcile[0].metadata["outcome"] == "conflicted"
        assert _fetch_all(db_path, "SELECT name FROM items ORDER BY name") == [("external",)]
    finally:
        mg.deactivate()


def test_sqlite_push_dry_run_reports_stale_basis_without_recording(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.commit()
    conn.close()

    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-stale-dry-run")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('pending')")
        mg.merge(task, mg.ground)

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO items (name) VALUES ('external')")
        conn.commit()
        conn.close()

        with pytest.raises(MaterializationPreflightError, match="status='conflicted'"):
            mg.push(dry_run=True)

        reconcile = mg.store.filter_effects(effect_type="SqlReconcileRecorded", substrate="sqlite")
        assert reconcile == []
        assert _fetch_all(db_path, "SELECT name FROM items ORDER BY name") == [("external",)]
    finally:
        mg.deactivate()


def test_sqlite_assessment_reports_stale_basis_without_recording(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.commit()
    conn.close()

    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-stale-assess")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('pending')")
        mg.merge(task, mg.ground)

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO items (name) VALUES ('external')")
        conn.commit()
        conn.close()

        assessment = mg.assess_push()

        assert [blocker.result.status for blocker in assessment.preflight_blockers] == ["conflicted"]
        reconcile = mg.store.filter_effects(effect_type="SqlReconcileRecorded", substrate="sqlite")
        assert reconcile == []
    finally:
        mg.deactivate()


def test_sqlite_stale_basis_reconcile_remains_blocked_after_restart(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.commit()
    conn.close()

    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-stale-restart")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('pending')")
        mg.merge(task, mg.ground)

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO items (name) VALUES ('external')")
        conn.commit()
        conn.close()

        with pytest.raises(MaterializationPreflightError, match="status='conflicted'"):
            mg.push()

        reconcile = mg.store.filter_effects(effect_type="SqlReconcileRecorded", substrate="sqlite")
        assert len(reconcile) == 1
    finally:
        mg.deactivate()

    mg2, _sqlite2 = _vcscore_with_sqlite(workspace, db_path)
    try:
        with pytest.raises(MaterializationPreflightError, match="status='conflicted'"):
            mg2.push()

        reconcile = mg2.store.filter_effects(effect_type="SqlReconcileRecorded", substrate="sqlite")
        assert len(reconcile) == 1
        assert reconcile[0].metadata["outcome"] == "conflicted"
    finally:
        mg2.deactivate()


def test_sqlite_stale_basis_reconcile_does_not_block_after_basis_restored(workspace: Path) -> None:
    db_path = workspace / "app.db"
    basis_path = workspace.parent / "basis.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.commit()
    conn.close()
    shutil.copyfile(db_path, basis_path)

    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-stale-restored")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('pending')")
        mg.merge(task, mg.ground)

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO items (name) VALUES ('external')")
        conn.commit()
        conn.close()

        with pytest.raises(MaterializationPreflightError, match="status='conflicted'"):
            mg.push()

        shutil.copyfile(basis_path, db_path)

        plan = mg.push()

        assert plan.total_operations == 1
        assert _fetch_all(db_path, "SELECT name FROM items ORDER BY name") == [("pending",)]
        reconcile = mg.store.filter_effects(effect_type="SqlReconcileRecorded", substrate="sqlite")
        assert len(reconcile) == 1
    finally:
        mg.deactivate()


def test_sqlite_wal_mode_push_detects_live_basis_advance(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE items (name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES ('base')")
        conn.commit()

        mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
        try:
            task = mg.fork(mg.ground, "task-wal-stale")
            mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('pending')")
            mg.merge(task, mg.ground)

            conn.execute("INSERT INTO items (name) VALUES ('external')")
            conn.commit()

            with pytest.raises(MaterializationPreflightError, match="preflight failed"):
                mg.push()

            assert _fetch_all(db_path, "SELECT name FROM items ORDER BY name") == [
                ("base",),
                ("external",),
            ]
        finally:
            mg.deactivate()
    finally:
        conn.close()


def test_sqlite_push_reports_base_unavailable_when_required_db_is_missing(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.commit()
    conn.close()

    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-missing-base")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('pending')")
        mg.merge(task, mg.ground)

        db_path.unlink()

        with pytest.raises(MaterializationPreflightError, match="status='unsupported'"):
            mg.push()

        reconcile = mg.store.filter_effects(effect_type="SqlReconcileRecorded", substrate="sqlite")
        assert len(reconcile) == 1
        assert reconcile[0].metadata["outcome"] == "unsupported"
        assert not db_path.exists()
    finally:
        mg.deactivate()


def test_sqlite_base_unavailable_reconcile_remains_blocked_after_restart(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.commit()
    conn.close()

    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-missing-base-restart")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('pending')")
        mg.merge(task, mg.ground)

        db_path.unlink()

        with pytest.raises(MaterializationPreflightError, match="status='unsupported'"):
            mg.push()

        reconcile = mg.store.filter_effects(effect_type="SqlReconcileRecorded", substrate="sqlite")
        assert len(reconcile) == 1
    finally:
        mg.deactivate()

    mg2, _sqlite2 = _vcscore_with_sqlite(workspace, db_path)
    try:
        with pytest.raises(MaterializationPreflightError, match="status='unsupported'"):
            mg2.push()

        reconcile = mg2.store.filter_effects(effect_type="SqlReconcileRecorded", substrate="sqlite")
        assert len(reconcile) == 1
        assert reconcile[0].metadata["outcome"] == "unsupported"
    finally:
        mg2.deactivate()


def test_sqlite_replay_plan_base_availability_matches_substrate_for_live_basis(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.commit()
    conn.close()

    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-live-basis")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('pending')")
        mg.merge(task, mg.ground)

        plan = sqlite_substrate.build_pending_replay_plan()

        assert plan.base_availability == sqlite_substrate.base_availability(plan.basis_token)
        assert plan.base_availability.base_available is True
        assert plan.base_availability.source == "live-upstream"
    finally:
        mg.deactivate()


def test_sqlite_replay_plan_base_availability_matches_substrate_for_missing_basis(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (name TEXT)")
    conn.commit()
    conn.close()

    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-missing-basis-alignment")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('pending')")
        mg.merge(task, mg.ground)
        db_path.unlink()

        plan = sqlite_substrate.build_pending_replay_plan()

        assert plan.base_availability == sqlite_substrate.base_availability(plan.basis_token)
        assert plan.base_availability.base_available is False
        assert plan.base_availability.source == "none"
    finally:
        mg.deactivate()


def test_sqlite_replay_plan_base_availability_matches_substrate_for_forked_child_carrier(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        child = mg.fork(parent, "task-child", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('inherited')")
        mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items ORDER BY name")
        mg.exec("sqlite", "execute", scope=child, sql="INSERT INTO items (name) VALUES ('child-only')")

        plan = sqlite_substrate.build_replay_plan(child)

        assert plan.base_availability == sqlite_substrate.base_availability(plan.basis_token)
        assert plan.base_availability.base_available is True
        assert plan.base_availability.source == "live-upstream"
    finally:
        mg.deactivate()


def test_sqlite_runtime_rebuild_fails_closed_when_live_base_advanced(workspace: Path) -> None:
    db_path = workspace / "app.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO items (id, name) VALUES (1, 'base')")
    conn.commit()
    conn.close()

    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-runtime-stale", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=task, sql="UPDATE items SET name = 'local' WHERE id = 1")

        runtime_path = sqlite_substrate._carriers[task.name].runtime_path
        runtime_path.unlink()

        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE items SET name = 'external' WHERE id = 1")
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="runtime rebuild requires the original basis"):
            mg.exec("sqlite", "query", scope=task, sql="SELECT name FROM items ORDER BY id")
    finally:
        mg.deactivate()


def test_sqlite_runtime_db_loss_does_not_erase_pending_replay_intent(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        task = mg.fork(mg.ground, "task-runtime-loss")
        mg.exec("sqlite", "execute", scope=task, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('recoverable')")
        runtime_path = sqlite_substrate._carriers[task.name].runtime_path
        mg.merge(task, mg.ground)

        runtime_path.unlink(missing_ok=True)
        mg.push()

        assert _fetch_all(db_path, "SELECT name FROM items ORDER BY name") == [("recoverable",)]
    finally:
        mg.deactivate()


def test_sqlite_runtime_rebuild_does_not_replay_materialized_ancestor_history(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    try:
        setup = mg.fork(mg.ground, "task-setup")
        mg.exec("sqlite", "execute", scope=setup, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=setup, sql="INSERT INTO items (name) VALUES ('baseline')")
        mg.merge(setup, mg.ground)
        mg.push()

        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (name) VALUES ('pending')")

        runtime_path = sqlite_substrate._carriers[parent.name].runtime_path
        runtime_path.unlink()

        rebuilt = mg.exec("sqlite", "query", scope=parent, sql="SELECT name FROM items ORDER BY name")

        assert rebuilt.value == {
            "columns": ["name"],
            "rows": [["baseline"], ["pending"]],
            "row_count": 2,
        }
    finally:
        mg.deactivate()


def test_sqlite_push_matches_runtime_after_child_merge_then_later_parent_update(workspace: Path) -> None:
    db_path = workspace / "app.db"
    mg, _sqlite = _vcscore_with_sqlite(workspace, db_path)
    try:
        parent = mg.fork(mg.ground, "task-parent", hints={"isolated": True})
        mg.exec("sqlite", "execute", scope=parent, sql="CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        mg.exec("sqlite", "execute", scope=parent, sql="INSERT INTO items (id, name) VALUES (1, 'base')")

        child = mg.fork(parent, "task-child", hints={"isolated": True})
        mg.exec("sqlite", "query", scope=child, sql="SELECT name FROM items ORDER BY id")
        mg.exec("sqlite", "execute", scope=child, sql="UPDATE items SET name = 'child' WHERE id = 1")
        mg.merge(child, parent)

        runtime_outcome = mg.exec(
            "sqlite", "execute", scope=parent, sql="UPDATE items SET name = 'parent-late' WHERE id = 1"
        )
        assert runtime_outcome.value == {"rowcount": 1}

        runtime_query = mg.exec("sqlite", "query", scope=parent, sql="SELECT name FROM items ORDER BY id")
        assert runtime_query.value == {
            "columns": ["name"],
            "rows": [["parent-late"]],
            "row_count": 1,
        }

        mg.merge(parent, mg.ground)
        mg.push()

        assert _fetch_all(db_path, "SELECT name FROM items ORDER BY id") == [("parent-late",)]
    finally:
        mg.deactivate()


def test_sqlite_verify_recovery_succeeds_without_runtime_db_files(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    runtime_root = sqlite_substrate._runtime_root
    try:
        task = mg.fork(mg.ground, "task-verify-recover")
        mg.exec("sqlite", "execute", scope=task, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('verified')")
        mg.merge(task, mg.ground)

        def _crash_after_apply() -> None:
            raise RuntimeError("simulated crash after materialization")

        monkeypatch.setattr(mg.store, "advance_materialized", _crash_after_apply)

        with pytest.raises(RuntimeError, match="simulated crash after materialization"):
            mg.push()

        run = read_materialization_run(str(workspace / ".vcscore"))
        assert run is not None
        assert run.planned_unit_ids == (f"sqlite:sqlite:{db_path}",)
        assert run.completed_unit_ids == run.planned_unit_ids
        assert _fetch_all(db_path, "SELECT name FROM items ORDER BY name") == [("verified",)]
    finally:
        mg.deactivate()

    shutil.rmtree(runtime_root, ignore_errors=True)

    store = Store(str(workspace / ".vcscore"))
    marker = MarkerSubstrate(build_builtin_substrate_context(store))
    sqlite_recovery = SQLiteSubstrate(build_builtin_substrate_context(store), db_path=db_path)
    recovered = VcsCore(str(workspace), substrates=[marker, sqlite_recovery], store=store)
    recovered.activate(recover="verify")
    try:
        assert recovered.status().commits_ahead == 0
        assert read_dirty_flag(str(workspace / ".vcscore")) is None
        assert read_materialization_run(str(workspace / ".vcscore")) is None
        assert _fetch_all(db_path, "SELECT name FROM items ORDER BY name") == [("verified",)]
    finally:
        recovered.deactivate()


def test_sqlite_verify_recovery_fails_closed_on_divergence(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = workspace / "app.db"
    mg, sqlite_substrate = _vcscore_with_sqlite(workspace, db_path)
    runtime_root = sqlite_substrate._runtime_root
    try:
        task = mg.fork(mg.ground, "task-verify-diverged")
        mg.exec("sqlite", "execute", scope=task, sql="CREATE TABLE items (name TEXT)")
        mg.exec("sqlite", "execute", scope=task, sql="INSERT INTO items (name) VALUES ('expected')")
        mg.merge(task, mg.ground)

        def _crash_after_apply() -> None:
            raise RuntimeError("simulated crash after materialization")

        monkeypatch.setattr(mg.store, "advance_materialized", _crash_after_apply)

        with pytest.raises(RuntimeError, match="simulated crash after materialization"):
            mg.push()
    finally:
        mg.deactivate()

    shutil.rmtree(runtime_root, ignore_errors=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO items (name) VALUES ('diverged')")
        conn.commit()
    finally:
        conn.close()

    store = Store(str(workspace / ".vcscore"))
    marker = MarkerSubstrate(build_builtin_substrate_context(store))
    sqlite_recovery = SQLiteSubstrate(build_builtin_substrate_context(store), db_path=db_path)
    recovered = VcsCore(str(workspace), substrates=[marker, sqlite_recovery], store=store)

    with pytest.raises(RuntimeError, match="diverged from expected materialized replay"):
        recovered.recover_dirty_push(mode="verify")

    assert read_dirty_flag(str(workspace / ".vcscore")) is not None
    assert read_materialization_run(str(workspace / ".vcscore")) is not None
    assert _fetch_all(db_path, "SELECT name FROM items ORDER BY name") == [("diverged",), ("expected",)]
