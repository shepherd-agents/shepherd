"""The jailed hard-stop supervisor — a budget alarm that reaps the process tree.

Replaces the bare ``perl -e 'alarm shift; exec @ARGV'`` prefix on Linux
(execplan 260709 §4.6). The perl form arms an alarm and *execs* the command,
so SIGALRM kills only that one process — an agent tool child spawned with
``setsid`` (hermes's terminal tool; the claude Bash tool) survives in its own
session and keeps writing after the provider returned (spiked: a post-kill
write landed 22 s later). Neither a process-group kill (``setsid`` is exactly
that escape) nor a plain PPID walk suffices: an agent typically backgrounds a
daemon whose intermediate shell exits at once, so the orphan **reparents to
init before the alarm fires** and a walk from the command is blind to it
(caught by the S3 jailed evidence run — the naive walk passed a keep-parent
unit test but missed the real reparent case).

So the supervisor marks itself a **child subreaper**
(``PR_SET_CHILD_SUBREAPER``): reparented descendants then reparent to *it*
instead of init, staying reachable. On the alarm it walks ``/proc`` for its
whole descendant set (the command plus every reparented orphan), SIGKILLs it,
then dies from SIGALRM itself so the returncode stays ``-14`` — the budget-stop
contract ``BudgetExhausted`` reads.

Run by file path (``python <this file> <budget_seconds> <cmd> …``) so it
triggers no package import inside the jail; stdlib only. Linux only — the
builder keeps the perl form on macOS, whose ``/proc`` this walk cannot use.
The child inherits stdout/stderr, so the command's output flows to the
launch pipes unchanged; this supervisor never writes to them.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import sys

_PR_SET_CHILD_SUBREAPER = 36  # <linux/prctl.h>: orphaned descendants reparent here, not to init


def _become_subreaper() -> None:
    """Best-effort ``PR_SET_CHILD_SUBREAPER``; a failure just weakens the reap."""
    with contextlib.suppress(OSError):
        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)


def _descendants(root: int) -> list[int]:
    """Every transitive child of ``root`` per ``/proc`` PPID links, deepest last.

    Called with the subreaper's own pid, so reparented orphans (which now point
    back at us, not init) are included. Unreadable/vanished entries are skipped
    — a racing exit is a reap that already happened.
    """
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")  # noqa: PTH208 — /proc pids are the natural string keys
    except OSError:
        # No readable /proc (a confinement that masks it): the descendant walk
        # is impossible, but the caller must still kill the direct child and
        # preserve the -14 signature. Return empty rather than letting the
        # OSError escape the signal handler (which would crash the reaper with
        # rc 1 and skip the kill entirely).
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as fh:
                # The comm field is parenthesized and may contain spaces/newlines;
                # split after the final ')' so field indexing is comm-safe.
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    collected: list[int] = []
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            collected.append(child)
            stack.append(child)
    return collected


def _kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def main() -> int:
    budget_seconds = int(sys.argv[1])
    command = sys.argv[2:]
    if not command:
        return 0

    # Before forking, so the child's descendants reparent to us, not init.
    _become_subreaper()
    child = os.fork()
    if child == 0:
        # New session so the child leads its own tree; exec the real command.
        os.setsid()
        try:
            os.execvp(command[0], command)  # noqa: S606 — shell-free exec is the point (the perl `exec`)
        except OSError as exc:  # exec failure mirrors the perl `die` path
            sys.stderr.write(f"exec: {exc}\n")
            os._exit(127)
        return 127  # unreachable

    def _on_alarm(_signum: int, _frame: object) -> None:
        # Walk from our own pid: as the subreaper we are the ancestor of both the
        # command and every orphan that reparented to us, so this one set is the
        # whole tree. Kill the command last, after the descendants it spawned.
        for pid in _descendants(os.getpid()):
            if pid != child:
                _kill(pid)
        _kill(child)
        # waitpid may race the SIGCHLD reaper; a missing child is already reaped.
        with contextlib.suppress(ChildProcessError):
            os.waitpid(child, 0)
        # Die from SIGALRM so the returncode is -14 — the budget-stop signature.
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGALRM)

    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(budget_seconds)
    _, status = os.waitpid(child, 0)
    signal.alarm(0)
    # Transparently propagate the child's own outcome (rc 0, rc 1 auth-fail, …):
    # only the alarm path overrides, and it never reaches here (it _exits above).
    if os.WIFSIGNALED(status):
        term = os.WTERMSIG(status)
        signal.signal(term, signal.SIG_DFL)
        os.kill(os.getpid(), term)
    return os.WEXITSTATUS(status)


if __name__ == "__main__":
    raise SystemExit(main())
