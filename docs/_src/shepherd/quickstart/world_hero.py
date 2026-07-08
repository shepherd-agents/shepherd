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


# A task is a signature + docstring: the contract a sandboxed agent fulfils.
# The `repo` parameter is the whole permission surface — a writable workspace
# handle. Narrow it with sp.May[sp.GitRepo, sp.ReadOnly] when writes aren't needed.
@sp.task
def write_note(repo: sp.GitRepo, topic: str, output_path: str, output_text: str) -> None:
    """Write one note about `topic` into the repository."""


with sp.open(".") as workspace:  # run `shepherd init` here first
    workspace.tasks.register(write_note)
    run = workspace.run(
        write_note,
        repo=workspace.git_repo(),
        topic="shepherd",
        output_path="NOTE.txt",
        output_text="Hello from a Shepherd retained output.\n",
        runtime={"provider": "static"},  # deterministic, offline; "claude" = live agent
    )

    output = run.output()                          # a proposal, held to one side
    print(list(output.changeset().changed_paths))  # what it wants to change
    print(output.read_text("NOTE.txt"), end="")    # read it before deciding
    output.select()                                # record your decision — or .discard()
# --8<-- [end:hero]
