# Original citation-checker candidate

[tasks.py](tasks.py) is the first registered task recovered from Shepherd's task
artifact store: `tasks.check_references@v1`, run `run-d295776ebbb2`, September 5,
2026 at 09:19:08 UTC. The source is preserved verbatim.

One Opus task reviewed all references supplied in a batch, using web tools and
six result categories within a four-minute budget. Its first worker received an
empty workspace: [first-report.json](first-report.json) records `missing_inputs`
and zero audited citations. This execution failure is not a 0% accuracy baseline.

The archive retains the [task artifact](task-artifact.json), [policy](policy.md),
[inputs](input.json), [references](references.json), [evidence](evidence/) and
[provenance](provenance.json). Operational launch records are omitted and machine
paths are normalized for publication. The outer controller was not recovered.

Compare this task with the current [five-stage package](../../README.md) and its
[measured results](../../evaluation/README.md). See the evaluation integrity
manifest for hashes of public result files; provenance records hashes of this archive.
