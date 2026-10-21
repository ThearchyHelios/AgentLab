English | [Simplified Chinese](../zh-CN/evaluation-and-ledger.md)

# Evaluation and the Caliber Ledger

This page covers two things: regression evaluation (whether Ask Data still answers correctly after you switch models, change prompts, or change the catalog) and the caliber ledger (whether the numbers in Ask Data answers match published calibers). It is for builders and admins. The user interface is currently available in Simplified Chinese only.

## Regression evaluation

Evaluations live under the Evaluations navigation item, visible to builders and above (shortcut ⌥0).

### Test suites

Each question in a test suite is a question plus an expected answer. The expectation can be:

- A SQL statement: select a cell or a column; it is recomputed on every evaluation rather than freezing a value; statements that query masked columns are rejected.
- A metric of a published caliber.
- A query identified from an Ask Data run.
- "No data", or a clarifying question back.
- The main source of a change (contribution): an expected SQL statement plus change decomposition parameters; the main source (one segment or the top few) and the direction are computed with the same analyses the answer uses.
- A verdict: an expected SQL statement plus parameters for a fluctuation baseline, trend test, difference test, or comparability check; the class of verdict is computed with the same analyses: usual fluctuation or unusual; rising, falling, or no clear trend; a difference that holds up or not enough to conclude; comparable or not comparable (see [Analysis](analysis.md#the-five-analyses)).

Rules:

- Numeric values may not be typed in by hand; nor may the segments, directions, and verdicts in contribution and verdict expectations, which are computed from the expected SQL with the same analyses, once before and once after the answer.
- Text key points are judged by the judge, listed separately, and not counted toward accuracy.
- After a caliber version bump, questions that use it show a notice at the top. Each question takes one of three options: switch to pinning the new version, compute one expectation for each of the old and new versions, or archive the old question and create a new one. Until then, the question is skipped in evaluations.
- Test suites can be imported and exported.

### Run an evaluation

1. Choose the variants: Copilot model, writing model, judge on or off, catalog (current / snapshot / verified only / off), and how many repetitions.
2. Check the estimated cost, then start. The Catalog contribution preset runs "current vs. off" in one go.

- Evaluations run in the background, and each question goes exactly the way a user's question would in Ask Data (graph building, run, review). Questions that stop at an approval aren't approved on anyone's behalf and are recorded as they are.
- When Copilot returns only thinking and no operations, the question is retried once automatically.
- Concurrency, per-question time limits, and per-evaluation and daily spending all have caps, set under Settings → Preferences → Evaluation. Evaluation spending is tallied separately.
- A question's time limit doesn't include time spent queuing. Under full load, evaluations queue behind Ask Data (see [Run concurrency](postgresql-and-scaling.md#run-concurrency)).
- An evaluation can be stopped, and you can retry only the unfinished questions. A service restart marks it interrupted, and it doesn't resume by itself.

### Read the results

- **Overview**: accuracy and traceable accuracy (with Wilson intervals), the distribution of paths and issuance tiers, usage and cost by role, and duration percentiles.
- **Per-question table**: verdict, the numbers in the expectation and in the answer, where the SQL differs, how many times tables were looked at, and cost; click through to the run.
- **Comparison**: against the previous evaluation or a baseline, with per-question transitions and attribution.
- **A/B paired table**: exact McNemar test; display only, not a gate.

Contribution and verdict expectations are scored like this: an answer that cites an analysis conclusion whose verdict matches is correct and traceable; body text that states it correctly by the lexicon without citing it is correct but uncited; a source or verdict that doesn't match, or the opposite answer, is wrong; an expectation that itself can't be computed (the expected SQL returns too little history, say) can't be scored. The input queries of analysis conclusions also count as queries the answer cited.

Evaluation runs are left out of usage statistics. The scoring rules are at version 3 (which adds contribution and verdict expectations); old evaluations can be re-scored under the new rules from the evaluation details (reading only the sealed records, without calling models).

### Scheduled evaluations

A schedule's target can be a test suite. When any question goes from pass to fail, it pushes "Scheduled evaluation regressed"; the message contains only the test suite name, the pass count, the number of regressed questions, and a link. Schedules that target a test suite don't accept webhooks (they return 422).

## Caliber ledger

The caliber ledger matches the numbers in Ask Data answers against published calibers. A caliber is the agreed definition of how a metric is fetched and computed, kept in a caliber card.

### A caliber label for each number

After an exploratory run is sealed, the system compares the answer with published calibers in the background and gives each number one of these labels:

| Label | Meaning |
|---|---|
| "Proven equivalent" | The SQL is proven equivalent to the caliber |
| "Chosen by Copilot" | Copilot assembled it from a caliber it chose |
| "Related caliber" | It uses the same table but isn't equivalent; the difference is stated |
| "This run's reasoning" | It has nothing to do with any caliber |

- The evidence panel's provenance chain gains a Caliber step at the top, and every Ask Data turn gets a one-line summary underneath. While matching has no result yet, it says "Matching calibers" and fetches again with backoff.
- The matching result is an event appended after sealing, `ledger.matched`. It appears in the timeline and in the verifiable report's "Appended after sealing" section, outside the sealed scope. At startup, runs from the last 7 days are matched retroactively (following the `AGENTLAB_SCHEDULER` switch).
- Judging is number by number: when a report cites several columns of one query (for example sales, order count, first and last day), each number is compared on its own column against the caliber's metric column, and extra queried columns don't affect it. This applies only when removing the other columns wouldn't change the number; queries with DISTINCT, truncation after sorting by another column, or extra grouping columns are still compared as a whole.
- When a multi-row query has no `ORDER BY`, citing one of its cells doesn't count as "from a caliber", and recall and reconciliation mark it "Row order undefined".
- An answer is issued automatically only when every number in it that comes from data is proven to come from the same caliber.
- Numbers in analysis conclusions (shares, changes, fluctuation ranges) are derived from query results, so they aren't compared or labeled with a caliber and aren't counted in the summary line.

When writing a caliber's data-fetch SQL, it is best not to round (`ROUND(SUM(x), 2)`) or wrap values in `COALESCE(…, 0)`. Leave rounding and defaults to the caliber card's decimal places and display settings; otherwise Ask Data queries written without rounding will never be recognized as equivalent (and where they differ is stated plainly).

### Formal issuance by caliber

When an answer as a whole is equivalent to a published governed caliber (the SQL is proven equivalent, the version is governed, and the graph has no human nodes and no writes), the system automatically starts a formal run by that caliber, with the origin recorded as "Formal issuance by caliber".

- The `ledger` group in Settings can turn off automatic issuance (`auto_issue`).
- Automatic and one-click issuance together are capped at 20 per day.
- Answers labeled "Chosen by Copilot" get one-click issuance.
- Runs that aren't Ask Data answers, such as canvas trial runs and personal app refreshes, aren't issued automatically.
- A user can issue by caliber only for caliber versions pinned by apps that user is allowed to use.

### Reconciliation

For a number labeled "Related caliber", click See the difference to start a read-only reconciliation run: this run's number, the caliber's number, and the difference each open to their provenance.

- You can give feedback on whether the reconciliation's explanation was useful.
- Users can reconcile too.
- When a person has to choose the metrics to compare, the columns that were verified number by number are preselected.

### Distill

When the same kind of question keeps coming back, it appears under Distillable questions in the workflow picker. The criteria: a query of the same shape at least 3 times from at least 2 conversations; or questions clustering into one group; or answers repeatedly exported or made into apps.

A distillable question can be codified into a caliber draft: the cited queries become data-fetch nodes, and literals that vary across turns automatically become parameters. Publish it after review.

### Recall

After a new version of a governed caliber is published, the system looks up, in the background, the Ask Data answers, formal runs, apps pinning it, and exports that used the old version, groups them by parameters, and recomputes the difference between the two versions on the same data (as exploratory runs with the origin "Caliber recall"; the difference is computed in a recall card).

- Only when a number really changed does the system open a Recall task for the answer's owner and push `ledger.recall`. In the task, the old value, the new value, the difference, and the original answer's value at the time are all clickable, and the answer can be reissued under the new version.
- Unchanged answers are only recorded in the ledger, annotated "Checked against vN, not affected".
- A recall whose metrics all have undefined row order concludes "Cannot verify" and opens a task.
- Each publish recomputes at most `ledger.recall_limit` groups (default 50; 0 means list without computing); for the rest, click Continue in the version history. The publish dialog previews the scale beforehand.
- The automatic batch after publishing and the continuation at startup both follow the `AGENTLAB_SCHEDULER` switch.
- When a number changes, the "Caliber version bumped, answer numbers changed" notification can be pushed (global channels and the answer owner's personal channels); the message contains no numbers.
- Catalog or schema changes can also mark answers "Basis changed" (see [Data Catalog](data-catalog.md#versions-and-reminders)); a task is opened only for answers that newly have error-level issues under the new catalog.

### Statistics

Four statistics are computed live by `GET /api/ledger/stats`: equivalence hits, reconciliation, question repetition, and recall (E1–E4). The Caliber hits & repetition tab on the evaluation page plots their trends.

Evaluations, caliber reconciliations, and caliber recalls are runs the system starts on someone's behalf; they are left out of usage statistics, and the Records page doesn't list them by default.
