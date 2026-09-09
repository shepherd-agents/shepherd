# Validation scope

The package regression suite checks extraction, evidence matching, source coverage,
claim scope, partial-output recovery, frozen inputs and native benchmark scoring.
Run it with `make test-citation-checker` from a development checkout.

Release packaging also requires a clean base install and a citation-extra install,
CLI and package discovery, exact-match execution, frozen resume, parser operation
inside the review workspace, and a bounded real Claude Code review. See
[release packaging](../../../../packaging/shepherd-ai/README.md).

The [recorded evaluations](../evaluation/README.md) describe development performance
and its limitations; passing packaging checks is not a new accuracy measurement.
