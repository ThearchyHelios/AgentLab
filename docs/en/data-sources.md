English | [Simplified Chinese](../zh-CN/data-sources.md)

# Data Sources and Spreadsheet Import

This page covers connecting databases, uploading spreadsheets, and how AgentLab keeps queries read-only. It is for builders and admins. For the data catalog (recording the business meaning of tables and fields), see [Data Catalog](data-catalog.md). The user interface is currently available in Simplified Chinese only.

## Connect a database

A data source can be a business database (MySQL / MariaDB, PostgreSQL, or Oracle, with drivers in the `[db]` extra), an uploaded spreadsheet, or a manually registered SQLite file.

- After connecting, click Introspect Schema: it reads tables, columns, comments, foreign keys, and unique constraints. Column types are stored in each database's full notation, such as `VARCHAR2(20 CHAR)` or `ENUM('a','b')`.
- Introspection is a background job, queued with drafting and profiling for the same data source. For a large database with thousands of tables, it first builds an object index and then reads by introspection scope (see [Data Catalog](data-catalog.md#large-databases-introspection-scope-and-on-demand-inclusion)).
- One data source corresponds to one schema. Formal introspection doesn't accept `?schema=` for switching schemas temporarily; previews do.
- Each data source has a query tool, `db_query__<source>`; at run time, agents look up table schemas with `db_schema__`.
- The address of a data source card is `/data/databases#source-<id>`.
- AgentLab's own database files (such as the `agentlab.db` left in the data directory after switching to PostgreSQL) can't be registered as data sources.

Introspection failing and "Not yet introspected" are two different states. Clicking the button won't fix a failure; deal with the timeout or change the schema first. The data source card shows the failure reason and the databases available on that server; agents are told the facts too, including that they can query `information_schema` directly to confirm the table structure.

## Read-only guarantees

- The SQL guard combines allowlisted verb checks, body scanning, multi-statement blocking, and a result-set cap.
- `DROP` / `TRUNCATE` / `GRANT` and the like are always refused, even on writable data sources.
- Statements that begin as queries but write are treated as writes: `WITH … DELETE`, `SELECT … INTO`, `EXPLAIN ANALYZE`, `PRAGMA … =`, `set_config()`, and so on.
- Read-only data sources are also read-only at the connection layer, not just through the guard: SQLite is opened with `mode=ro`, and PostgreSQL / MySQL sessions use read-only transactions.
- **Oracle has no session-level read-only switch**, which leaves only the guard. Configure a read-only account for read-only Oracle data sources.
- On a writable data source, the same `db_query__<source>` executes queries directly, while a write needs confirmation first and is committed only after confirmation. A declaration in the capability registry can't make write statements skip approval.
- In Ask Data for users and in personal apps, data sources are always read-only (see [Identity, Permissions, and Security](identity-and-security.md#ask-data-for-users)).

## Upload spreadsheets

An uploaded Excel or CSV file becomes a SQLite file, registered as a data source of `kind=sqlite`, with a `db_query__<name>` tool generated automatically. Numbers are computed by SQL rather than read by the model from text fragments.

- Column types are inferred column by column (INTEGER / REAL / TEXT), and empty cells are stored as NULL. If an integer column were stored as TEXT, `ORDER BY` would fall back to lexicographic order (`'10' < '9'`) without any error.
- Why not the knowledge base: there a spreadsheet could only be chunked and retrieved, and the numbers would become whatever the model "reads" from fragments. Output review can catch numbers that appear out of nowhere, but not one cell misread from a pile of fragments.
- For data sources imported from spreadsheets, the system writes notes from its verification results; the data catalog neither duplicates nor rewrites them.
- An uploaded-spreadsheet cell cited in a report can be traced back to the cell in the original sheet (an inferred source).

### Complex reports: use an extraction recipe

- Complex reports are imported with an extraction recipe; CSV works too. Subtotals sandwiched between detail rows are checked and kept out of the data.
- AI drafting for recipes uses the Drafting model chosen under Settings → Preferences; when it is empty, it follows Copilot's model.

### Accumulating by period

- Monthly detail files can accumulate by period into one table. A new period can trigger a schedule (see [Event triggers](schedules.md#event-triggers)).
- Every historical version is a full copy, so disk usage grows with the number of periods and of referenced versions.
- Besides the current version and versions referenced by runs, each data source keeps the 3 most recently activated versions; the rest are reclaimed and can't be activated again.

## Cross-database merge

Cross-database questions don't use federated queries. Each database first aggregates to the same key, then the merge query node (`merge`) merges by key outside the databases (in-memory SQLite, with the same read-only guard).

- Truncated inputs are rejected.
- Mismatched key types, and row counts that grow after the merge, both trigger warnings.
- The numbers in the report trace all the way back to the raw queries on both sides.

## Masked columns

List the columns to mask in the data source settings (`mask_columns`); they apply by column name across the whole data source.

- For masked columns, the model gets only the column name and type: their comments, and the catalog's Chinese name, code values, and description for the column, are withheld.
- Profiling doesn't sample masked columns or observe their values.
- For users with login on, masking is a boundary; for builders, admins, and everyone when login is off, it only reduces exposure (see [Which roles can see raw values](identity-and-security.md#which-roles-can-see-raw-values)).
