# Citation-checker evaluation and demo

These repository tools and artifacts are separate from the installed checker.
The runtime imports no scoring code, gold labels or experimental runners.

The final evaluation, recorded September 9, 2026, uses a frozen, runtime- and
label-screened **development selection of 150 citations**, not 150 papers or a
held-out benchmark. Each dataset has 50 citations; ten ambiguous DeLTA labels
remain unscored. All abstentions and the one unfinished citation count as errors.
Selections and labels were unchanged between the runs below.

| Dataset | Previous full run | First packaged run | Corrected package |
|---|---:|---:|---:|
| Manual | 32/50 | 34/50 | 41/50 |
| HALLMARK | 45/50 | 34/50 | 42/50 |
| DeLTA, non-ambiguous gold | 39/40 | 24/40 | 33/40 |
| **Total** | **116/140 (82.9%)** | **92/140 (65.7%)** | **116/140 (82.9%)** |

The corrected run took **1136.996 seconds (18m57s)**, or 1139.326 seconds including
preparation, with cached extraction and initial evidence and fresh reviewer
discovery. Of 150 outputs, 149 completed; batch 015 hit its 300-second provider
deadline after saving five valid neighbors, leaving citation 262 unfinished.
There are 22 scored abstentions, including that unfinished citation, and two
native false positives (Manual 109 and 210). These are not relabeled as gold errors.
Usage is **at least $39.824941** in CLI list-price equivalents, not subscription
charges: only 24 of 25 sessions have terminal usage reports.

The 58-reference Shepherd manuscript completed in **139.414 seconds (2m19s)**,
or 141.487 seconds including preparation. Thirty exact matches bypassed Opus;
28 references used five successful sessions. Sutton/Barto (35) and OpenHands
(41) were flagged, SWE-bench (15) was verified, and author-name gaps in 3 and 5
remained uncertain. The report has 51 verified references, three core metadata
gaps, two optional metadata gaps and two confirmed issues. All 58 outputs validate.
Reported usage is $4.847559 with all five terminal reports available. These are
known controls, not exhaustive independent manuscript gold.

## Retained evidence for the numbers

- Benchmark: [report](results/benchmark/report.md), [raw JSON](results/benchmark/report.json),
  [native evaluation](results/benchmark/evaluation.json), [all failures and abstentions](results/benchmark/failures.json),
  [performance](results/benchmark/performance.json), [validation/usage](results/benchmark/validation-and-usage.json).
- Manuscript: [report](results/shepherd/report.md), [corrections](results/shepherd/corrections.bib),
  [control assertions](results/shepherd/controls.json), [performance](results/shepherd/performance.json),
  [actual task graph](results/shepherd/task-graph.json), [validation/usage](results/shepherd/validation-and-usage.json).
- History: [earlier batched measurement](results/baselines/earlier-batched.json),
  [previous full measurement](results/baselines/previous-full.json),
  [packaged discovery regression](results/discovery-regression/performance.json),
  [six-case recovery pilot](results/discovery-control/evaluation.json).
- Checks: [16-output parity](results/saved-output-parity.json),
  [174 additional saved decisions](results/full-saved-output-parity.json),
  [identical full-run inputs/budgets](results/full-run-input-parity.json),
  [58-reference extraction parity](results/extraction-smoke.json),
  [artifact hashes](results/integrity.json).

Public artifacts preserve citation judgments, native labels, scores and timings.
Machine-specific paths are normalized, and Markdown trailing blank lines are
trimmed. The integrity manifest records public hashes and hashes of the original
source files; run IDs identify the recorded executions. Raw web-response caches,
provider streams and full execution workspaces remain local; a fresh clone can
rescore the reports but cannot replay those exact live HTTP responses without
the retained run directory. The source snapshot hashes are retained in each
result directory's `manifest.json`.

The first packaged run exposed a stale `retrieval_rounds_remaining: 0` signal
beside positive tool budgets. Several abstentions explicitly cited no retrieval
rounds. A preceding prompt simplification also removed the instruction to attempt
discovery before giving up on identity. Removing the obsolete field, exposing the
actual budget and restoring bounded discovery recovered five of six selected
abstentions before the full rerun. No new service, model stage or scoring exception
was added. The final aggregate score matches the previous run, with changed
per-dataset performance and higher latency; packaging alone did not improve accuracy.

## Reproduce scoring or run the checker

From the repository root, rescore the retained report with no network or model:

```bash
python shepherd/extras/citation-checker/evaluation/score.py \
  shepherd/extras/citation-checker/evaluation/results/benchmark/report.json
make test-citation-checker
```

For a new live evaluation, install the package and authenticate as described in
its [README](../README.md), then use a fresh output directory:

```bash
python shepherd/extras/citation-checker/evaluation/run.py --output .runs/new-citation-evaluation
```

This defaults to **fresh initial retrieval**, so its latency is not directly
comparable with the recorded cached-evidence runs. To use the same original
initial evidence, pass `--prepared-run PATH_TO_RETAINED_PACKAGE_RUN`; the driver
verifies its hashes and citation population, copies only initial evidence, and
permits fresh reviewer discovery. Add `--evidence-only` to disable discovery
for a fixed-evidence diagnostic. Prior decisions and labels never enter the checker.
Generated runs go under ignored `.runs/`; do not add new scratch results here.

## Frozen selection and demo

[inputs.json](frozen/inputs.json) and [labels.json](frozen/labels.json) preserve the
150 citations and native labels. [Upstream URLs/revisions](frozen/upstream-manifest.json)
identify Manual, HALLMARK and DeLTA sources. The [selection manifest](frozen/manifest.json),
[replacement protocol](frozen/replacement-protocol.json), [selection](frozen/selection.json)
and [screening audit](frozen/label-screen-audit.json) document screening. Historical
paths in those records are provenance, not files required by the current driver.

For the demo, show the [original recovered candidate](../docs/original-candidate/README.md),
the current five-stage graph, one expanded difficult review and the results above.
The earlier-batched-to-final comparison is **74/140 (52.9%) → 116/140 (82.9%)**,
with latency **8m26s → 18m57s**. It is a whole-workflow development comparison;
the original missing-input run is not a 0% accuracy baseline.
