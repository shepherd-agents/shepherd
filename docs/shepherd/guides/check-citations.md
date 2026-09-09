# Check a paper’s citations

> Page status: release-ready
> Source state: checked-example
> Applies to: Shepherd v0.3.1
> Owner: Shepherd maintainers
> Validation: packaging/shepherd-ai/smoke.py; shepherd/extras/citation-checker/tests

*How-to guide. New to Shepherd? Start with the quickstart. For exact APIs, see the reference.*

Check whether bibliography entries identify real works and describe their metadata
correctly. The checker retains evidence, explains confirmed errors and proposes
supported corrections. It does not assess whether a cited paper supports a claim.

## Install and run

Use Python 3.11+, Git, Claude Code signed in with a subscription, and a supported
native jail: macOS Seatbelt or Linux Landlock. Live review uses Claude Code's
headless CLI; API-key and token overrides are not supported by this checker.

```bash
python -m pip install --upgrade "shepherd-ai[citation-checker]==0.3.1"
claude auth login
shepherd-check-citations paper paper.pdf --output citation-report
```

Choose a fresh output directory. Ordinary code extracts references with pdfplumber,
collects evidence and verifies complete exact matches. Unresolved references go
to Opus in batches, then all results pass through shared validation and reporting:

```text
Extract → Collect evidence → Match → Review difficult batches → Report
                              └───────────────────────────────→ Report
```

Defaults are six citations per batch and five concurrent workers. Each review
has a five-minute provider budget, six searches and twelve fetched URLs. Successful
citations survive a partial batch failure; a timeout remains visible in the report.

## Read the results

Open `citation-report/report.md` for findings and source links, `report.json` for
structured results, and `corrections.bib` for supported citation corrections.
`task-graph.json` records the actual stages and links reviewer runs. A citation
can be verified, have confirmed metadata problems, or remain uncertain because
evidence is incomplete. Uncertainty is not a claim that a work is fabricated.

Runs preserve input hashes, evidence, code and parser dependencies. To resume
with the saved code and completed results:

```bash
shepherd-check-citations run citation-report
```

## Recorded evaluation

The frozen development selection has 150 citations: 50 each from Manual, HALLMARK
and DeLTA. Ten ambiguous DeLTA labels are excluded from scoring. The final result
is **116/140 (82.9%)**, with abstentions and the one unfinished citation counted
as errors. This selection was screened during development and is not held out.

The recorded 58-citation Shepherd manuscript run completed in 2m19, flagged the
Sutton/Barto and OpenHands entries, and retained five uncertain entries. It is a
demonstration with known controls, not an independently labeled paper benchmark.
The full citation benchmark took 18m57 with cached initial evidence and fresh
reviewer discovery; fresh retrieval can change runtime.

See the [evaluation data and reproduction commands](https://github.com/shepherd-agents/shepherd/tree/v0.3.1/shepherd/extras/citation-checker/evaluation)
and the [original task](https://github.com/shepherd-agents/shepherd/tree/v0.3.1/shepherd/extras/citation-checker/docs/original-candidate).
