English | [Simplified Chinese](../zh-CN/analysis.md)

# Analysis

This page covers where the analytical judgments in a report come from: how statements such as "mainly from East", "outside the usual fluctuation", "rising steadily", or "the difference holds up" are computed by the system, written into reports, and checked. It covers what each of the five analyses answers, the criteria it judges by, what it says when it can't be done, the two entry points (analysis tools for agents and analysis cards), how reports cite conclusions, the check on judgment words, the Analysis Scope section at the end of a report, and the settings and limits. It is for builders and for people who read reports. The user interface is currently available in Simplified Chinese only.

## What it is and why

The system fills the numbers in a report from snapshots, and the narrative layer does no arithmetic (see [Evidence and Issuance](evidence.md#the-basic-idea)). But a report says more than numbers; it also makes judgments: where a change mainly came from, whether this week counts as unusual, whether something keeps rising, whether a gap between two groups is real. These judgments used to be made by the writing model on its own; it could pick its evidence, and readers had no way to check.

Analysis brings judgments back into the system as well:

- **The system computes the verdicts**: the five analyses are deterministic platform code. They read only query snapshots returned during this run and compute verdicts with fixed methods and thresholds. The model decides only what to compute (which two periods to compare, which dimension to break down, which segment to drill into); it can't change the method or the thresholds.
- **The system writes the verdict phrases**: every analysis conclusion carries a verdict phrase written by the system from a template, and reports cite it (`[[a:A1]]`); like a number, it opens to its provenance. When the writer writes judgment words such as "mainly from" or "unusual" itself, the system checks sentence by sentence that the sentence cites a matching analysis conclusion and agrees with its verdict (see [Analysis claim check](#analysis-claim-check)).
- **Saying it as it is**: when an analysis can't be done, it says why (not enough history, the current period isn't over, the query was truncated, the two periods aren't comparable, too few samples) instead of forcing a conclusion. "Can't be done" is a conclusion too, and the writer reports it as such.
- **Leaving a trace**: every analysis conclusion, together with its method and version, parameters, the thresholds actually used, the input cells, the table ranges checked live, the results, and the verdict, is stored as a content-addressed artifact, recorded in the evidence ledger, and sealed with the run. The evidence panel, the report export, and the verifiable report all recompute it with the same function (see [What you see when you click a number](evidence.md#what-you-see-when-you-click-a-number)).

A breakdown shows only where a change came from, not why. A cause needs a verbatim knowledge-base quote or a run input as its basis; otherwise it is written as a conjecture to be verified.

## The five analyses

Each analysis produces one analysis conclusion, numbered A1, A2, … in order in the writing catalog (the same numbering scheme as Q1 for queries and K1 for knowledge-base chunks). A conclusion has a verdict (verdict code, the segments it names, a direction), a verdict phrase, a criterion (one sentence stating the thresholds actually used), named values (shares, the total change, and so on), a result table, and caveats (offsetting, Simpson's paradox, small samples, a record high, and so on; at most two are appended to the verdict phrase).

The thresholds in parentheses below are defaults, changed under Settings → Preferences → Analysis (see [Analysis settings](configuration.md#analysis-settings)).

### Change decomposition

Answers "where did the change come from". Three modes:

| Mode | Used for | How it breaks down |
|---|---|---|
| Additive | Quantities that add up, such as sales or order counts | By dimension (one or two columns), each segment's contribution to the total change and its share |
| Ratio | Ratios such as refund rate or average order value | Into a mix effect (segment weights changed) and each segment's own change; the two add up exactly to the change in the ratio |
| Factor | Sales = orders × average order value | Into each factor's contribution, by logarithmic mean (LMDI) |

- Verdicts: one segment accounting for at least half of the total change (0.5) is the main source ("mainly from East"); without such a segment, the top few segments together accounting for at least 80% (0.8) count as concentrated (at most a fifth of all segments, and at least 2); a total change under 0.5% of the base counts as essentially flat; anything else is dispersed. A ratio decomposition is judged as mainly from the mix, mainly from the segments' own changes, or both; a factor decomposition is judged by which factor it mainly comes from.
- When totals are given, the segments must add up to them (within a tolerance based on the precision the totals are written with) before the decomposition judges where the change came from. If they don't, it doesn't judge, and the verdict phrase states the gap. Without totals, the sum of the segments is the total, and the criterion says so.
- When the segments moving the other way add up to more than a quarter of the total change (0.25), a caveat says the changes partly offset each other. In a ratio decomposition, when most segments fall while the whole rises (or the other way round), a caveat explains Simpson's paradox. Segments with a denominator under 30 are marked as small and left out of that judgment.
- Drilling down: one segment of a decomposition can be broken down one level further (East by channel). The segments of that level must add up to the value of the segment above, at most 3 levels deep. The agent writes the drill-down query itself, querying only that segment, and the system checks that it closes against the value of the segment above.
- An example verdict phrase (translated): "Sales fell 194 (-9.2%), mainly from East (-120, 61.9% of the change)".

### Fluctuation baseline

Answers "is this change large, does it count as unusual": it puts the current period back into its own history.

- The input is one series ordered by period (day, week, month, quarter). By default it compares the rate of change against the previous period; daily data is compared with the same day last week, to avoid weekday effects. Stocks and ratios can compare the value itself. For year over year, compare with 52 weeks or 12 months earlier, with correspondingly longer history.
- It uses the median and the median absolute deviation of the history (robust standard deviation = 1.4826 × median absolute deviation): a current period within 2 robust standard deviations of the median is a usual fluctuation, 2 to 3 is outside, and 3 or more is far outside. It uses the median rather than the mean because extremes such as promotion weeks widen the mean and standard deviation, making normal weeks and unusual weeks alike look ordinary.
- History needs at least 8 periods and uses at most 52. When the current period is the highest or lowest in its history, a caveat says it is a record high or low.
- An example verdict phrase (translated): "Sales changed -15.8% this period, far outside the usual fluctuation of the past 24 weeks (-0.5% to +0.4%); this period's sales are the lowest of the past 24 weeks".

### Trend test

Answers "is it changing steadily, or was it a one-off".

- It takes the last contiguous stretch (at least 6 periods): when the two-sided p-value of the Mann–Kendall test is below 0.05, the sign of the Theil–Sen slope decides rising or falling; otherwise there is no clear trend. When the last 3 or more periods all move the same way, the verdict phrase adds "down 3 weeks in a row", or "but up the last 3 weeks in a row" when that runs against the trend.
- Mann–Kendall assumes the periods are independent, while business series are often autocorrelated, which makes p too small. So the verdict phrase says only "trending up", never "significantly up". Daily data gets a caveat about weekday effects suggesting weekly aggregation.
- An example verdict phrase (translated): "Online sales have trended up over the past 12 weeks (about +2.1% per week), up 4 weeks in a row".

### Difference test

Answers "are these two groups (or two periods of one group) really different, or is it sampling noise".

- For rates (conversion rate, refund rate), give the successes and the trials: each group gets a Wilson interval; when every cell's expected frequency is at least 5, it uses the pooled-proportion z-test, otherwise Fisher's exact test.
- For means, give each group's mean, standard deviation, and sample size, or its sum, sum of squares, and sample size (SQLite has no standard deviation function, so write it this way), and it uses Welch's t-test. You can also give the values row by row (at most 1000 rows, not truncated); the system computes the statistics itself and also reruns the test without the most distant value as a sensitivity check.
- A two-sided p-value below 0.05 means the difference holds up; several pairs in one call are corrected with Holm's method. A caveat about small samples is added when a group has fewer than 30 samples for means, or when Fisher's test was used for rates.
- Verdict phrases avoid terms such as "p-value" or "significance": a difference that holds up is described as such, and otherwise the phrase says the difference may be just sampling noise, with the sample sizes (230 and 40, say). P-values and intervals are in the result table and the evidence panel.

### Comparability check

Answers "can these two periods be compared directly". Each item passes, warns, fails, or can't be checked:

| Item | How it is judged |
|---|---|
| Same definition | The two queries differ only in their dates; when their structure differs, or they also filter something else (East versus South), it fails |
| Window length | Both periods have the same number of days; for an additive flow a mismatch fails, for ratios and stocks it only warns |
| Whole period | The current period is over and the table's data has arrived; for a flow, missing data fails ("the current period has only 3 days of data"), otherwise it warns |
| Weekday mix | For daily windows whose length isn't a multiple of 7, both periods have the same number of weekend days; otherwise it warns |
| Data problems | No data in the queried range fails; SQL check errors warn |

When everything passes (or can't be checked), the periods are comparable; with only warnings they are conditionally comparable; with any failure they aren't comparable. Items that can't be checked never make the periods incomparable, and the verdict phrase names them. The table's actual date range is checked live (read-only, time-limited, masked columns are never checked, falling back to the range recorded by profiling on failure); the range found is recorded in the conclusion, and recomputation uses the recorded values.

When a change decomposition is given two queries, it runs this check automatically first; when the fluctuation baseline, the trend test, or a change decomposition over a single query checks whether the current period is complete, it uses the whole-period item of this check. It also runs once automatically when a caliber card metric uses two queries of the same shape that differ only in their dates (this week versus last week), with the result attached to the metric (see [Caliber cards](evidence.md#caliber-cards)).

## When an analysis can't be done

An analysis that can't be done doesn't fail the agent or the node. It is recorded as a conclusion, with a verdict phrase along the lines of "Fluctuation baseline not done: only 5 periods of history, at least 8 needed", and it goes into the ledger and can be cited and checked:

| Reason | When |
|---|---|
| Truncated | The input query returned only its first rows, so the sum of the segments isn't the full sum |
| Too few segments | Fewer than 2 segments after the breakdown |
| History too short | Fewer valid periods of history than the minimum |
| Current period incomplete | The current period isn't over, data hasn't arrived, it isn't a whole week, or the query covered only part of it |
| Gap | A period is missing between the current period and its comparison period; the last periods used by the trend test aren't contiguous |
| Periods unreadable | The period column's format can't be read (times of day, mixed formats) |
| Zero base | The comparison period is 0, so no rate of change can be computed |
| Non-positive values | Factor decomposition requires every value to be positive |
| Not the same quantity | The two queries differ in structure, so they aren't two periods of the same quantity, and it doesn't break them down |
| No p-value | The difference test can't compute a p-value for any pair (each group has only 1 sample, or a sample size is 0) |
| Upper level not done, or missing from this data | In an analysis card, the level a drill-down sits under couldn't be done, or this period's data lacks a period, segment, group, or total cell that is needed |

A wrong call isn't recorded as a conclusion: a column that doesn't exist, a masked column used as a dimension or measure, a ratio treated as additive, several rows for the same segment and period, wrong parameters, or this agent having used up its analyses for this execution. The agent gets a sentence on how to fix the call and calls again; in an analysis card it is the template that is wrong, so the whole card fails and says which item.

Periods are read in these formats: dates, `YYYY-MM`, `YYYY-Www`, `YYYY-Qn`, and integer sequence numbers. For weekly aggregation, it is best to compute the period from a date column in the SQL, as the date of the first day of each week, or as an ISO week `YYYY-Www`. The same `2026-W42` lands on different days under SQLite's `%W`, MySQL's `%u`, and the ISO week; the system recognizes the scheme from the format string in the SQL and works out the real start and end. When the period is a ready-made week-number text column taken straight from the table, the scheme can't be recognized, and whether the current period is complete can't be checked.

## Two entry points

Both entry points share the same analyses, the same shape of conclusion, and the same table of verdict phrases.

### Analysis tools for agents

Agents in Ask Data and on the canvas can be given five analysis tools: `analysis_decompose` (change decomposition), `analysis_baseline` (fluctuation baseline), `analysis_trend` (trend test), `analysis_differ` (difference test), and `analysis_comparable` (comparability check). Use them when you only know which dimension to break down after seeing the numbers.

- The parameters cover only what to compute: which result this node queried (`Q3`, the same number as in the "Evidence Q3" handed back to the model), the role of each column, which two periods to compare, the two total cells, the level to drill down from, what to call the measure, and one sentence on what this call is testing (shown only in the evidence panel, never in the report body). There are no threshold parameters at all.
- Binding any one of them injects the platform's built-in analysis method into the agent's system prompt: first set the base period from the question, run the comparability check and the fluctuation baseline, then break down factors and mix, break down by dimension from coarse to fine, and drill down one level once the main source is found; a breakdown that finds no concentration is a conclusion too; make no causal claims; say plainly when something can't be done; finally, list the analyses done and their verdicts. The method is versioned with the code.
- What goes back to the agent is "Analysis A2" with the verdict, the verdict phrase, the key values, and caveats or the reason it couldn't be done; the full conclusion goes into the evidence ledger.
- Each agent can run at most 12 analyses per execution and drill down at most 3 levels.
- The analysis tools analyze only results that the agent they are bound to queried itself, and can't be put in a Call Tool node (for fixed analyses in a template, use an analysis card). Members of a collaboration team can use them too, but their conclusions don't go into the evidence ledger, so reports can't cite them.
- Users (the user role) can use them in Ask Data too: the analysis tools don't query the database; they read only query results already masked, and masked columns are never used as dimensions or measures.

### Analysis cards

An analysis card (the `analysis` node, a peer of the caliber card) runs a fixed set of analyses on the results of upstream query nodes. It is for analyses a template always does, such as the anomaly part of a weekly report: is this week's change within the usual fluctuation, and which region did it mainly come from.

- Each item states the analysis method and mode, which upstream query node to analyze, the parameters, and what the item is testing. Inputs can only be Call Tool nodes that call a database query tool, or Merge Query nodes, connected before the analysis card; an agent can't be an input, since it may have queried several times. The parameters are the same as the analysis tools', and `{{ input.x }}` can pick periods from run inputs.
- Thresholds aren't written in the items. When a template needs fixed thresholds, write them in the card's Threshold Overrides, pinned with the version; when they conflict with this run's analysis settings, the card fails and says where the conflict is.
- The output is `nodes.<card>.results.<item>`: status, verdict code, direction, named values, and verdict phrase. A downstream branch can route on the verdict, for example sending a run to a person when the change is outside the usual fluctuation.
- An item that can't be done doesn't fail the card: that item is recorded as not done, and the card moves on to the next one.
- Analysis card results are not caliber card inputs: a caliber card that reads them is treated as unable to trace back to raw data. Take metrics in the caliber card from query result cells, and cite analysis conclusions directly in the report.
- The governed level requires analysis card inputs to be Call Tool nodes that call a database query tool, or Merge Query nodes, so that it is settled at publish time that the card analyzes queries the platform executed.
- Run comparison aligns analysis cards by node and item and lists both runs' verdicts (see [Run comparison](evidence.md#run-comparison)).

The built-in template 11, Anomaly Analysis (Traceable), is built this way: fetch the weekly totals and two weeks by region → analysis card (fluctuation baseline, change decomposition with totals) → report writer → issuance. It can be published at the governed level as it is (see [Built-in templates](workflows.md#built-in-templates)).

## Citing in reports

```
[[a:A1]]                                    A1's verdict phrase, rendered by the system from the verdict; one clickable segment
[[a:A1.top_share|pct]]                      one named value of A1; unit conversion as for [[m:]]
[[v:A1.r0.share]]                           one cell of A1's result table
[[table:A1 cols=region,delta,share rows=0-4]]  A1's result table
[[see:A1]]                                  basis of the sentence; attached at the end of the sentence, not rendered
```

- The Analysis Conclusions section of the writing catalog lists each conclusion's number, verdict phrase, named values, the top 10 segments of its result table, and caveats or the reason it couldn't be done.
- The system shows shares and change rates in analysis conclusions as percentages (`[[a:A1.top_share]]` and `[[v:A1.r0.share]]` render as 62.0%), and changes in rates in percentage points (1.2 percentage points); writers cite them as usual and do not add a conversion. A conversion the writer does write is kept.
- Conclusions from analysis cards can always be cited. In formal issuance at the governed level, analyses an agent ran on the spot with analysis tools follow the same rule as the cells it queried: they can be cited only when the issuance contract turns on Cell Citations (they can still serve as the basis at the end of a sentence).
- The measure name in a verdict phrase comes from the agent or the template; the system first strips digits, percent signs, units, and citation markers from it and cuts it to 20 characters, so text written by the system never smuggles in numbers written by the model.

## Analysis claim check

The report-writer node's Analysis Claim Check (`analysis_claims`) checks judgment words sentence by sentence, with a fixed lexicon and binding rules, without calling a model.

| Class of judgment word | Examples (translated) | Analysis conclusion and verdict it needs |
|---|---|---|
| Source | mainly from, concentrated in, dragged down, drove, contributed most | Change decomposition judged a main source or concentrated; the segments named must be in the verdict, and a segment said to drag down must be falling |
| Dispersed, offset, mix | spread across regions, one up and one down, structural change | Change decomposition judged dispersed, offsetting, or mainly from the mix |
| Unusual, record, normal | unusual, sharply, surged, record high, normal fluctuation | Fluctuation baseline judged outside or far outside (direction matching), record high or low, or usual fluctuation |
| Trend, no trend | rising steadily, down three weeks in a row, no clear trend | Trend test, with matching direction and run length |
| Difference, no difference | significantly higher, really lower, little difference | Difference test judged as holding up (direction matching) or possibly sampling noise |
| Causal | because, due to, led to, affected by | A verbatim knowledge-base quote or a run input as the basis in the same sentence, or phrasing as a conjecture (possibly, to be verified) |

- A sentence's basis is the `[[a:…]]`, `[[v:A…]]`, and `[[table:A…]]` in it plus the `[[see:A…]]` at its end. Negation flips the class: "not unusual" is checked as normal.
- Not checked: statements about composition rather than change ("this week's orders came mainly from the online channel"), suggestions and plans, "abnormal" as a business status ("some orders are abnormal"), verbatim knowledge-base quotes, system-rendered verdict phrases, headings, and tables.
- The line between source and cause: "the decline was mainly caused by East", citing a breakdown by region and naming one of its segments, is checked as a source, the bookkeeping sense of "came from"; "the decline was due to the end of a promotion" names no segment and is checked as causation.
- Three kinds of violation: a judgment word without a matching analysis conclusion; a conclusion cited, but the verdict, the segments named, or the direction don't match (citing a conclusion that couldn't be done counts too); a cause without a basis.

| Value | What it does |
|---|---|
| `check` (default, also used by Ask Data) | Sends the report back to the writer for one rewrite; what remains is marked in the report and doesn't affect the issuance tier |
| `strict` | Rewrites up to the maximum number of repairs; what remains counts as an issuance gap and downgrades the issuance; required at the governed level (gate G3) |
| `off` | No check |

- When the writing catalog has analysis conclusions, the writer is told to follow the verdicts and preferably cite verdict phrases directly; when it has none, the writer is told not to make such judgments and to state only numbers and changes.
- An exit in citation mode checks again independently, with the same check used at assembly. The report document records which disposition and which version of the lexicon it was checked with: only documents that carry this record are checked for it on verification, so verification results for reports from before the upgrade don't change.
- It errs on the side of missing cases: only strong judgment words are collected. The finer points it misses are left to the claim-sentence judge (see [Claim sentences](evidence.md#claim-sentences)); when the judge's excerpt includes an analysis conclusion, the judge is also told that the verdict was computed by the system and to judge only whether the sentence states it correctly.

## Analysis scope

When a report uses analysis conclusions (when this run has any), the system appends an Analysis Scope section at the end of the report. The writer can't change it; it is sealed with the document, and its numbers are clickable:

- **Phenomena**: verdict phrases of comparability checks, fluctuation baselines, and trend tests.
- **Broken down**: each change decomposition, listed along its drill-down chain with its dimensions and verdict.
- **Ruled out**: decompositions judged dispersed, and difference tests where the difference may be just sampling noise. A breakdown that finds no concentration is a conclusion too.
- **Not done**: the reason for each analysis that couldn't be done.
- **Not broken down**: candidate dimensions that weren't broken down. Candidate dimensions are computed deterministically from the data catalog: columns on the fact table used by the decomposition's query that are marked as an attribute or status, aren't masked, and have a code-value table (at most 50 values, or marked complete), plus the same kind of columns on dimension tables related to it. When the columns used by the decomposition don't match the catalog, it says only that candidate dimensions can't be checked, rather than listing guesses.
- **Tests**: how many difference tests were run this time, and whether they were corrected for the number of tests.

The sentence an agent writes about what a call is testing doesn't go into this section; it appears only in the evidence panel. The report-writer node's Analysis Scope (`analysis_scope`) defaults to appending the section when there are analysis conclusions, and can be set not to.

## Settings

Under Settings → Preferences → Analysis you can change the thresholds used for verdicts, how many analyses an agent may run per execution, how many levels it may drill down, and how many segments are listed. For each item's label and default, see [Analysis settings](configuration.md#analysis-settings).

- The group is snapshotted into the run when the run starts (only when the graph has an analysis card or an agent with analysis tools), and resuming or continuing uses the same snapshot.
- Agents can't change thresholds: the analysis tools have no threshold parameters. A template can write threshold overrides on an analysis card.
- Every conclusion records the thresholds actually used, and its criterion is written from them, for example "a segment accounting for at least half of the total change is the main source".

## Limits

- Not in this round: forecasting; correlation analysis; dimension hierarchies in the data catalog; profiling of numeric columns; analysis tools for the verification agent of double-blind recomputation, adversarial review, and approval inquiry; analysis conclusions in caliber ledger matching and recalls (numbers in analysis conclusions get no caliber label; see [Caliber ledger](evaluation-and-ledger.md#caliber-ledger)).
- The trend test assumes independent periods; for autocorrelated series, p comes out too small.
- Candidate dimensions come only from code-value tables: profiling doesn't yet record each column's number of distinct values, so attribute columns without a code-value table aren't candidates.
- The lexicon collects only strong judgment words, so a different wording may slip through; what slips through is left to the claim-sentence judge and to the people reading the report.
- Analyses done by members of a collaboration team don't go into the evidence ledger, so reports can't cite them.
