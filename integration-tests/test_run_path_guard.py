"""The run-path executor guard — layer (b) of "real ⇒ jailed" (PD7).

The dialect run-composition layer invokes an executor ONLY via
``launch_confined``; the only raw spawn lives in the containment backends
(which ARE ``launch_confined``'s implementation). "No raw subprocess" is
deliberately NOT the invariant — the framework spawns ``sandbox-exec`` by
design and ``ruff`` waives S603 — so this is an AST *call* scan (styled on
``test_d2_boundary.py``'s full-tree import scan), graduated move-not-build
from ``spikes/260609-run-path-guard`` (11/11) per the execplan's PD7 row.

The guard runs live against the production dialect package run path (landed at
PD5). It currently holds — the in-process driver spawns nothing —
so this is a plain green invariant rather than the signposted xfail the plan
anticipated for a not-yet-landed run path; B3c-1's ``launch_confined``
composition lands *inside* the sanctioned verb and stays green by
construction.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_PATH = REPO_ROOT / "shepherd" / "packages" / "dialect" / "src" / "shepherd_dialect"

#: The containment backends — the jail IS the sanctioned spawn; raw subprocess
#: is its mechanism. Everything else scanned is run-path.
IMPL_FILES = frozenset(
    {
        "_containment.py",
        "_seatbelt_containment.py",
        "_landlock_containment.py",
    }
)

# Executor-spawning APIs. A run-path module must reach these only *through*
# launch_confined.
_BANNED: dict[str, frozenset[str]] = {
    "subprocess": frozenset({"run", "call", "check_call", "check_output", "Popen", "getoutput", "getstatusoutput"}),
    "os": frozenset(
        {
            "system",
            "popen",
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "posix_spawn",
            "posix_spawnp",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
        }
    ),
    "pty": frozenset({"spawn", "fork"}),
    "asyncio": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
    "multiprocessing": frozenset({"Process"}),
}

#: The one sanctioned verb. A call to it is never a violation, on any receiver.
_SANCTIONED = "launch_confined"


@dataclass(frozen=True)
class Violation:
    """One banned executor call: where, which API, and the enclosing symbol."""

    filename: str
    lineno: int
    api: str
    symbol: str  # innermost def/class enclosing the call, or "<module>"


# Each entry is a deliberate, reviewed grant of parent-side executor use on the
# run path. A key pins (filename, api, symbol); if the pinned symbol or api
# changes, the entry goes stale and the guard FAILS — re-review, don't rename.
# Target size is ZERO: every entry is a debt the credential/egress broker seam
# (W3.2 / g07) is meant to retire.
RATIFIED_PARENT_EFFECTS: dict[tuple[str, str, str], str] = {
    ("providers.py", "subprocess.run", "_read_host_claude_login"): (
        "D1 2026-07-04: subscription-auth credential seeding (public PR#7). Reads the macOS "
        "keychain (`security find-generic-password`) in the PARENT and copies raw credential "
        "bytes into the jailed run's scratch CLAUDE_CONFIG_DIR so a signed-in `claude` CLI works "
        "like an env-carried key. Fail-soft by design: each source is a recorded miss, and a "
        "keyless resolution makes the public headless provider refuse before launch (unless "
        "SHEPHERD_ALLOW_KEYLESS_CLAUDE is set). Migrates to the credential-broker seam (W3.2/g07); "
        "retire this entry when it does."
    ),
    ("providers.py", "subprocess.run", "probe_claude_auth"): (
        "D2 2026-07-06: `shepherd doctor claude --probe` auth preflight. Runs a minimal `claude -p` "
        "in the PARENT to verify the signed-in CLI can actually authenticate, classifying the "
        "outcome with the run path's own envelope parser. This is a diagnostic health check, not a "
        "task run: it launches no user task body and carries no workspace authority, so it is "
        "deliberately outside `launch_confined`. Never raises (a probe that cannot launch is a "
        "failed probe). Migrates to the credential-broker seam (W3.2/g07); retire this entry when "
        "it does."
    ),
}


def _import_maps(tree: ast.AST) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Full-tree (incl. function-local) import resolution.

    Deliberately scope-flat — a conservative over-approximation appropriate
    for a deny-guard.
    """
    amap: dict[str, str] = {}
    fmap: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound = a.asname or a.name.split(".")[0]
                amap[bound] = (a.asname and a.name) or a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                fmap[a.asname or a.name] = (node.module, a.name)
    return amap, fmap


def _resolve(call: ast.Call, amap: dict[str, str], fmap: dict[str, tuple[str, str]]) -> str | None:
    """Return the banned 'module.attr' a call resolves to, or None."""
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr == _SANCTIONED:
            return None
        base = func.value
        if isinstance(base, ast.Name):
            mod = amap.get(base.id)
            if mod and func.attr in _BANNED.get(mod, frozenset()):
                return f"{mod}.{func.attr}"
    elif isinstance(func, ast.Name):
        if func.id == _SANCTIONED:
            return None
        if func.id in fmap:
            mod, orig = fmap[func.id]
            if orig in _BANNED.get(mod, frozenset()):
                return f"{mod}.{orig}"
    return None


def _symbol_spans(tree: ast.AST) -> list[tuple[int, int, str]]:
    """Return (start, end, name) for every def/class in the tree.

    A call line maps to its innermost enclosing symbol. Widest-first; the last
    match at a line wins.
    """
    spans: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            spans.append((node.lineno, node.end_lineno or node.lineno, node.name))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    return spans


def _enclosing_symbol(spans: list[tuple[int, int, str]], lineno: int) -> str:
    """Innermost def/class containing ``lineno`` ("<module>" if none)."""
    found = "<module>"
    for start, end, name in spans:
        if start <= lineno <= end:
            found = name  # spans are outer-first at a given start; inner ones come later
    return found


def find_violations(source: str, filename: str, *, role: str) -> list[Violation]:
    """Scan one module; containment_impl is the sanctioned-spawn role."""
    if role == "containment_impl":
        return []  # the jail IS the sanctioned spawn; raw subprocess is its mechanism.
    tree = ast.parse(source, filename=filename)
    amap, fmap = _import_maps(tree)
    spans = _symbol_spans(tree)
    return [
        Violation(filename, node.lineno, api, _enclosing_symbol(spans, node.lineno))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (api := _resolve(node, amap, fmap)) is not None
    ]


def partition_ratified(
    violations: list[Violation],
) -> tuple[list[Violation], list[tuple[str, str, str]]]:
    """Split scanned violations against RATIFIED_PARENT_EFFECTS.

    Returns (unratified, stale_keys):
      - unratified: violations with no ratifying table entry — hard failures;
      - stale_keys: table keys that matched NOTHING this scan — the pin drifted
        (symbol/api renamed or the call removed), also a hard failure so the
        table can never rot into a rubber stamp.
    """
    matched: set[tuple[str, str, str]] = set()
    unratified: list[Violation] = []
    for v in violations:
        key = (v.filename, v.api, v.symbol)
        if key in RATIFIED_PARENT_EFFECTS:
            matched.add(key)
        else:
            unratified.append(v)
    stale = [k for k in RATIFIED_PARENT_EFFECTS if k not in matched]
    return unratified, stale


def scan_paths(paths: list[Path], *, impl_files: frozenset[str]) -> list[Violation]:
    """Scan trees: every .py is run_path unless its basename is in impl_files."""
    out: list[Violation] = []
    for p in paths:
        for f in sorted(p.rglob("*.py")) if p.is_dir() else [p]:
            role = "containment_impl" if f.name in impl_files else "run_path"
            out.extend(find_violations(f.read_text(encoding="utf-8"), f.name, role=role))
    return out


# --- The live invariant -------------------------------------------------------


def test_run_path_invokes_no_executor_outside_launch_confined() -> None:
    """The live invariant against the production dialect run path.

    Every executor call on the run path must go through ``launch_confined``,
    OR be a deliberate, reviewed entry in ``RATIFIED_PARENT_EFFECTS``. An
    unratified call fails; a ratified entry that no longer matches anything
    (a stale pin) also fails — so the table stays honest.
    """
    assert RUN_PATH.is_dir(), f"run path missing: {RUN_PATH}"
    violations = scan_paths([RUN_PATH], impl_files=IMPL_FILES)
    unratified, stale = partition_ratified(violations)
    assert unratified == [], (
        "Executor call(s) outside launch_confined in the dialect run path "
        f"(real ⇒ jailed, layer b), not in RATIFIED_PARENT_EFFECTS: {unratified!r}"
    )
    assert stale == [], (
        "Stale RATIFIED_PARENT_EFFECTS entries — the pinned call was renamed or "
        f"removed; re-review and update the table: {stale!r}"
    )


# --- The guard guards itself (the spike's self-test corpus, carried over) ------


def test_guard_accepts_the_sanctioned_verb() -> None:
    """launch_confined is never a violation, on any receiver."""
    clean = (
        "def prepare_bound(ctx, req, execution):\n"
        "    spec = map_may_to_spec(req.may)\n"
        "    return execution.launch_confined([req.entrypoint, *req.args], spec)\n"
    )
    assert find_violations(clean, "clean.py", role="run_path") == []


def test_guard_catches_executor_bypasses() -> None:
    """Every spawn family the spike pinned still trips the guard."""
    violating = {
        "raw-subprocess-run": "import subprocess\ndef f(cmd):\n    return subprocess.run(cmd, check=True)\n",
        "aliased-Popen": "from subprocess import Popen as P\ndef f(cmd):\n    return P(cmd)\n",
        "os-system": "import os\ndef f(cmd):\n    os.system(cmd)\n",
        "os-execv": "import os\ndef f(p, a):\n    os.execv(p, a)\n",
        "asyncio-exec": "import asyncio\nasync def f(cmd):\n    return await asyncio.create_subprocess_exec(*cmd)\n",
        "local-import": "def f(cmd):\n    import subprocess\n    return subprocess.Popen(cmd)\n",
    }
    for name, source in violating.items():
        assert find_violations(source, f"{name}.py", role="run_path"), f"guard missed: {name}"


def test_containment_backends_are_impl_not_run_path() -> None:
    """The jail IS the sanctioned spawn; its raw subprocess is allowed."""
    raw = "import subprocess\ndef launch(profile, root, cmd):\n    return subprocess.run(cmd)\n"
    assert find_violations(raw, "_seatbelt_containment.py", role="containment_impl") == []


# --- The ratification mechanism (W1b.1) ---------------------------------------


def test_violation_captures_enclosing_symbol() -> None:
    """A violation records the innermost def enclosing the call."""
    src = "import subprocess\ndef outer():\n    def inner():\n        subprocess.run(['x'])\n"
    (v,) = find_violations(src, "m.py", role="run_path")
    assert (v.api, v.symbol) == ("subprocess.run", "inner")


def test_ratified_entry_is_accepted_and_matches_the_live_pin() -> None:
    """The two reviewed parent-side executor calls: the PR#7 keychain read and the doctor probe."""
    violations = scan_paths([RUN_PATH], impl_files=IMPL_FILES)
    unratified, stale = partition_ratified(violations)
    assert unratified == []
    assert stale == []
    # exactly these reviewed entries: the PR#7 keychain read and the `doctor claude` auth probe
    assert set(RATIFIED_PARENT_EFFECTS) == {
        ("providers.py", "subprocess.run", "_read_host_claude_login"),
        ("providers.py", "subprocess.run", "probe_claude_auth"),
    }


def test_unratified_call_still_fails() -> None:
    """A second, unlisted parent-side spawn is reported even amid ratified ones."""
    src = (
        "import subprocess\n"
        "def _read_host_claude_login():\n"
        "    return subprocess.run(['security'])\n"  # would-be ratified symbol...
        "def _sneaky():\n"
        "    return subprocess.run(['curl'])\n"  # ...but this one is not
    )
    violations = find_violations(src, "providers.py", role="run_path")
    unratified, _ = partition_ratified(violations)
    assert [v.symbol for v in unratified] == ["_sneaky"]


def test_ratification_is_symbol_specific_not_file_wide() -> None:
    """Same file + same api but a DIFFERENT symbol is not covered by the pin."""
    src = "import subprocess\ndef some_other_fn():\n    return subprocess.run(['security'])\n"
    violations = find_violations(src, "providers.py", role="run_path")
    unratified, _ = partition_ratified(violations)
    assert [(v.filename, v.api, v.symbol) for v in unratified] == [("providers.py", "subprocess.run", "some_other_fn")]


def test_stale_ratified_entry_is_a_failure() -> None:
    """A table key matching nothing in a scan is surfaced as stale."""
    violations = find_violations("def unrelated():\n    return 1\n", "providers.py", role="run_path")
    _, stale = partition_ratified(violations)
    assert ("providers.py", "subprocess.run", "_read_host_claude_login") in stale
