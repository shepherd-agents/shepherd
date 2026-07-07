"""Homepage / Getting Started hero: the retained-run quickstart.

This is the condensed form of ``examples/quickstart/world_channel.py`` (the
script ``shepherd demo write quickstart`` emits). Unlike the rest of this
directory it is NOT exercised against the ``_sim`` shim — the sim does not
model the retained-run surface. ``test_world_hero.py`` runs it against the
installed real wheel in a fresh ``shepherd init`` workspace.

Run it yourself:
    mkdir demo && cd demo
    shepherd init
    python world_hero.py
"""

# --8<-- [start:hero]
import shepherd as sp

workspace = sp.open(".")  # run `shepherd init` here first

# A task is a signature + docstring: the contract a sandboxed agent fulfils.
# The grant on `repo` is what lets it write the repository.
workspace.tasks.register_source(
    task_id="quickstart.write_note",
    module="quickstart_tasks",
    source_text='''
import shepherd as sp

def write_note(repo: sp.May[sp.GitRepo, sp.ReadWrite], topic: str,
               output_path: str, output_text: str):
    """Write one note about `topic` into the repository."""
''',
    entrypoint="write_note",
    may_default="ReadWrite",
)

run = workspace.run(
    "quickstart.write_note",
    repo=workspace.git_repo(),
    args={"topic": "shepherd", "output_path": "NOTE.txt",
          "output_text": "Hello from a Shepherd retained output.\n"},
    runtime={"provider": "static"},  # deterministic, offline; "claude" = live agent
)

output = run.output()                              # a proposal, held to one side
print(output.changeset().inspect()["changed_paths"])  # what it wants to change
print(output.read_text("NOTE.txt"), end="")           # read it before deciding
output.select()                                    # record your decision — or .discard()
workspace.close()
# --8<-- [end:hero]
