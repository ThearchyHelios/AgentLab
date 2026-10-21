English | [Simplified Chinese](../zh-CN/data-catalog.md)

# Data Catalog

This page covers the data catalog: what it records, how it is drafted and reviewed, data profiling, SQL checks, introspection scope for large databases, and schema and value changes. It is for builders and admins. For connecting data sources, see [Data Sources and Spreadsheet Import](data-sources.md). The user interface is currently available in Simplified Chinese only.

## Why a data catalog

Table and field names don't explain the business: does a row stand for a ticket or for one ticket check, can `visitor_count` be summed by day, which column joins two tables, and does the join turn one-to-many? When Copilot builds graphs and agents write SQL, they can only guess. A wrong guess still produces a query that runs, but the numbers are wrong.

The data catalog records these facts and hands them to three readers:

- **Copilot's graph building**: the system prompt has only a one-line overview per database (number of tables, how many have catalog entries, whether there is masking), with no table or field names. When Copilot needs a table, it looks for itself, and the server returns everything in one go (see [Looking on demand](ask-data.md#looking-on-demand)).
- **Schema lookups at run time**: `db_schema` attaches the catalog when it returns a table's schema. **Masked columns get only their name and type**: their comment, and the catalog's Chinese name, code values, and description for that column, are not given to the model. Copilot's looks, `db_schema`, and `/api/datasources/{id}/schema?table=` share one description; the cost is that the catalog agents see at run time lacks the masked-column part.
- **SQL checks**: the catalog is used to catch query patterns that compute wrong numbers (see below).

## What the catalog records

- For each table: a Chinese display name, grain, table kind, business key, business date, valid-record condition, and dedup rule.
- For each field: meaning, unit, measure type, and code values.
- Relationships between tables.

The catalog records only data facts, not calculation formulas. "Visitors currently in the park is a stock and can't be summed across days" goes into the catalog; "period-over-period = (this month − last month) ÷ last month" goes into a caliber card. This is also why there is no separate metric semantic layer: calibers are already pinned and governed by caliber cards and versions, and another layer would create a second set of calibers.

### Sources and statuses

Every catalog item carries a source and a status:

| | Values |
|---|---|
| Source | Database comment, foreign key, naming inference, data profiling, model drafting, a person |
| Status | Inferred, verified, confirmed, rejected |

- Inferred items are only hints; only verified and confirmed items feed error-level checks.
- Rejected items keep their ids, and the next drafting or inference won't bring them back.
- A code table can be marked "Complete".

## Drafting

- Introspection reads foreign keys and unique constraints, and infers relationships from `xxx_id` and `xxxId`, including when table names carry a business prefix.
- The model can draft: the Chinese display name, grain, table kind, business key, business date, and each column's meaning, unit, and measure type.
- Data sources imported from spreadsheets already have notes the system wrote from its verification results; the catalog neither duplicates nor rewrites them.
- Database comments that are just the table or column name itself, or that are written in identifier style, are not recorded in the catalog (neither as the display name nor as a description or meaning).
- The drafting instructions stored on the data source are given to the model together with the notes for this run, to help it understand the business and keep terms consistent. They can't override the "record only data facts" rule, and what comes back is still validated item by item.
- Which model: choose the Drafting model under Settings → Preferences; when it is empty, it follows Copilot's model.

### Model drafting

- Runs as a background job, table by table, recording each table's status and reason. Each table is merged into the catalog as soon as it is written; wide tables are merged after each segment.
- Closing the dialog doesn't stop it. You can stop it or retry only the failed tables; the list marks tables whose last drafting failed or only partly completed.
- On stop or service restart, segments already written to the catalog are kept. Jobs left unfinished by a service restart are marked interrupted and don't resume by themselves; click Retry to redraft those tables, and what was already written isn't lost.
- Synchronous drafting and background jobs are mutually exclusive.
- Each call covers at most 40 columns. For wide tables, the table-level items are drafted first, then the columns are split evenly into segments; work is queued in table order and doesn't jump ahead of narrow tables.
- The model's thinking is kept, with an output budget of 32768; when the provider won't accept that, it falls back to 8192.
- When redrafting, items the model left empty this time, or that failed validation, don't delete what was drafted before.
- Columns the model was unsure about and left empty (ones the catalog doesn't yet have a display name and meaning for) are recorded per table; you can add drafting instructions and redraft.

### Stop-loss rules for drafting

Drafting has no overall time limit; it relies on stop-loss rules and manual stopping:

- When the output budget runs out or a call times out, the segment is split smaller and asked again. Both halves of a timed-out segment are asked at the same time; once segments are down to 10 columns and still time out repeatedly, they are not split further.
- After two splits in total, newly started segments are halved, down to a minimum of 10 columns.
- Each segment, together with the pieces it was divided or split into, gets at most 8 calls. Progress shows how many pieces a segment was divided into.
- For missing tables and columns, only the missing ones are asked again; from a truncated reply, the fully written part is kept first.
- When 7 of the last 10 calls produced nothing, later segments are no longer split smaller; when 3 consecutive segments of one table produced nothing, the rest of that table's segments are skipped.
- When 4 of the last 6 calls produced nothing, it suggests switching models.
- When something wasn't produced, the reason names the segment and the columns; "Split and retried" appears only after segments that really were split.

### Proposals for error-preventing facts

While drafting, the model also proposes a valid-record condition (a SQL condition) and a dedup rule, each with its basis. For data sources with profiling on, the catalog's code values are given to the model as well (except for masked columns).

- Conditions are parsed with sqlglot in the data source's dialect. They may reference only this table's columns, may not contain subqueries, aggregates, or window functions, and may use only a few common functions; anything else is rejected, with the reason in the per-table notes.
- On data sources with profiling on, the share of rows excluded is counted through the read-only guard. Conditions that exclude 0% or 100% are rejected; accepted ones get "Profiling check: excludes x% of rows" in the notes.
- These are all recorded as inferred. Error-level SQL check rules accept only items a person has confirmed, and these proposals are ranked at the front of the review queue.

## Reviewing and editing

### Review queue

- Lists only inferred items, ordered by "how often runs used the table × how much the item affects numbers × source": valid-record conditions, dedup rules, business dates, and relationships come first; business keys and measure types next; display names, meanings, and units last. Each item says why it is ranked where it is.
- Filter by category and table, then multi-select to confirm, reject, or undo review in bulk. Optimistic locking applies per table, and conflicts are flagged one by one.
- You can see which runs used a table, and how many items have been confirmed for frequently used tables, for all tables, and for the items that affect numbers.

### Table Copilot

Chat with Copilot on the table details page to refine that one table:

- It can see the schema, the current catalog state (each item's source and status), relationships, profiling results, drafting instructions, and the columns left empty in the last drafting. Masked columns: name only.
- It proposes changes in batches following your instructions, each with before, after, and basis, validated by the same rules as the catalog (formulas are rejected; column names must exist in the schema). When it isn't sure, it asks you.
- Only on data sources with profiling on does it run verification queries through the read-only guard: at most 5 per request, with results masked first.
- Proposals accumulate in Pending changes and are saved in one go after you confirm (at most 200 items at a time), recorded as confirmed by a person. Each item's basis is saved separately as its Basis at confirmation, kept apart from notes and visible in the table details; it is removed when undoing review returns the item to inferred.
- Conversations aren't stored on the server. It changes only the catalog and never touches the business database.

### Corrections in conversation

When you say in a conversation "status=9 means voided", Copilot produces a catalog change proposal listing before, after, and the reason. It is saved only after a person confirms, and the reason is recorded as the item's Basis at confirmation. Formulas are not accepted; they belong in caliber cards.

## Export and import

Export the catalog as one JSON file (the catalog of every table, each item's status, and the drafting instructions) and import it into another instance or data source:

1. Preview first: which tables and columns match; whether each item will be added, updated, or kept; conflicts and skips listed one by one.
2. Items a person confirmed or rejected are carried over as they are. Where the target already has a confirmed item with a different value, the target's value is kept and recorded as a conflict. Inferred items in the file only fill gaps. Columns not in the schema are skipped, with the reason stated.
3. Drafting instructions are written only when the target has none.

Import isn't possible while drafting or profiling is in progress.

## Data profiling

Profiling runs read-only queries to check the catalog's inferences and fill in statistical facts. It is off by default and enabled per data source.

- Runs as a background job, table by table: for each table you can see check progress, query count, elapsed time, and which item is being checked. Closing the dialog doesn't stop it; you can stop it or retry only the unfinished tables. Jobs left unfinished by a service restart are marked interrupted and don't resume by themselves.
- The budget is per table: by default at most 40 queries and 60 seconds per table. When one table uses up its budget, only that table stops, and the next one starts with a full budget.
- There is no cap for the job as a whole; it relies on stop-loss rules and manual stopping: after 3 consecutive failed queries, or when 7 of the last 10 queries time out, it stops so as not to load the business database.
- By default it finishes one table before starting the next. In the data source settings you can let it profile 2–3 tables at once, which means at most that many queries hit the business database at any moment.
- Within each table, queries are sent one at a time, read-only, through the guard. Large tables use statistics to estimate row counts; masked columns aren't sampled.
- Every query sent leaves an audit trail, with values replaced by "?"; sampled key values never enter AgentLab's database.
- Sampling verifies inferred relationships and fills in coverage and cardinality; the ones that hold up are promoted to verified. Status columns get code-value candidates, with the meanings left for a person to fill in.
- The "Checked key count" profiling setting: `DISTINCT … LIMIT` doesn't take a random sample.
- On MySQL / MariaDB, profiling notes for text columns state the collation, ENUM columns gain the values that don't appear in the data, and date columns state how many invalid dates were excluded.
- The table list has a Profiling column, and the table details show the last profiling result.
- The date ranges profiling records aren't reliable for tables fully reloaded every day, so whether the queried range has data is checked live at run time; the ranges recorded by profiling and value observation serve only as a fallback when the live check fails (see [No data in the queried range](evidence.md#no-data-in-the-queried-range)).

By default, it profiles only "tables that need profiling": those never profiled, not finished last time, or that have since gained relationships awaiting verification or new code-value or date columns. On the same data source, drafting and profiling queue up one after the other, and the scope is set from the catalog as it stands when profiling starts, so you can click profile right after drafting finishes.

### Value observation

When the profiling scope is Value Observation, or during scheduled maintenance, profiling also observes values: row counts, code-value distributions (including code-value columns a person confirmed), the latest business date, and daily row counts over the last 35 days.

- These are compared with history taken on the same basis (the same sampling method) to find new code values, sharp shifts in proportions, gaps, missing days, volume drops, and large swings in row counts, which go into the change log (see below).
- When a code table marked "Complete" shows a value that isn't in the catalog, the system revokes "Complete", and the catalog goes up one version. This is the only thing the machine writes into the catalog by itself; the meaning of the new value still has to be filled in by a person.
- Masked columns aren't observed.

## Versions and reminders

- The catalog is frozen into the evidence along with the schema snapshot, so you can look up which version every run used.
- Publishing records the catalog versions of the tables a template references, and a formal run reminds you when the catalog has changed since. Where an agent writes SQL only at run time, there is a separate "possibly involved" reminder. These are reminders only; they never block the run.
- For a formal run started from a published version, if the schema that version references changed after publishing, the run gets a "Schema change" reminder (`schema.drift`, inside the sealed scope).
- When a review changes the items that affect numbers (valid-record condition, deduplication, business date, relationships, business key, measure type), or the schema drops a table, drops a column, or changes a column type, answers that used them are marked "Basis changed" (see [Caliber Ledger](evaluation-and-ledger.md#recall)).

## SQL checks

The catalog is used to check for seven kinds of problems:

1. Double counting after a one-to-many join;
2. Summing a stock across time;
3. Adding up or averaging ratios;
4. A missing valid-record condition;
5. Code values that don't exist;
6. Not using the agreed business date;
7. Join conditions that don't match known relationships.

The checks apply in four places: they go back to Copilot for correction during its self-check; they block publishing of governed templates; they are flagged on query snapshots at run time; and when the source query of a contract metric has errors, the issuance is downgraded.

Syntax that can't be parsed is always skipped: better to miss a problem than to raise a false alarm. Error-level rules use only verified and confirmed catalog items.

## Large databases: introspection scope and on-demand inclusion

A database with thousands of tables doesn't have to be read in full.

- Introspection first builds an object index of the whole database: one dictionary query per database type, recording name, type, comment, and estimated row count.
- It then reads the tables inside the Introspection Scope rules: wildcards are supported and exclusions take precedence; by default at most 500 tables, with an upper limit of 2,000.
- Tables with catalog entries, and tables used by SQL hard-coded in published templates, are always in scope.
- For a database with more than 500 tables and no introspection scope, the first introspection is refused and says how many tables matched. Set rules under Introspection Scope, or raise the limit, first.

At run time and during Copilot's looks, tables are searched first:

- Copilot's looks and the runtime `db_schema__` first search by the need (BM25; masked columns by name only).
- Without a table name, `db_schema__` lists only the first 50 tables and says how many more there are; you can search with `keyword`.
- Naming a table outside the scope reads its schema on the spot, read-only, and adds it to the scope with an audit trail (at most 8 tables per run).
- "Table no longer exists" is said only when the table really was dropped. Out of scope, schema unreadable, and never in the index are each reported separately.
- Row counts and date ranges from profiling are also given to Copilot and agents, with the note "for reference when writing queries only; not to be used as numbers in an answer". Masking is filtered again according to the settings at that moment.

## Schema changes and value changes

- **Schema changes**: introspection records a schema version for every table and compares it with the one read last time: added or dropped columns, type changes, nullability changes, comment changes, and added or removed primary keys, unique constraints, or foreign keys. A suspected rename is only reported as "possibly a rename", with no conclusion drawn. The first introspection of a table only builds a baseline and reports no changes.
- **Value changes**: value observations are compared with history taken on the same basis. For large tables, the basis is stated as it is (whole table, window, block sampling, first N rows), and observations on different bases aren't compared.

Both kinds of change go into the same change log.

### Where to look and how to handle them

- The data catalog's Changes view groups entries into Action needed, Reminder, and Note; the badge on a data source card is the number of entries that need action; the table details have a Schema & value changes section.
- Each entry states its impact: the catalog items that mention these columns and their status, the published templates that hard-code the column, and which SQL will now fail because the column doesn't exist.
- Actions: Clean up catalog items, Add code values, Redraft, Re-profile, Got it, and False positive (a false positive needs a reason). Every action goes through the catalog's existing write paths and records the catalog version or the job it created.
- The machine does only one thing by itself: when the data proves a code table is no longer complete, it revokes "Complete".
- Changes that need action can push the "Data source has changes to handle" notification, whose message has only the source name, the count, and a link, with no table names, column names, or values (see [Notifications](schedules.md#notifications)). They can also serve as a schedule's trigger event.

### Scheduled maintenance

Scheduled Maintenance on the data source card:

- Choose the steps (introspect the schema, profile the tables that need it, value observation; several can be chosen) and an off-peak time window; this is saved as a schedule whose target is the data source. There is none by default.
- It queues with drafting and profiling on the same data source, and steps that didn't run record the reason as it is.
- Schedules of this kind don't accept webhooks (they return 422); notifications go through channel subscriptions under Settings → Notifications.
