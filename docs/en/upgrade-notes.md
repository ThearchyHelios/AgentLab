English | [Simplified Chinese](../zh-CN/upgrade-notes.md)

# Upgrade Notes

This page lists, newest first, the changes in each update that affect users and operators, and what to do when upgrading. It is for operators and admins. The features themselves are described in the topic documents. The user interface is currently available in Simplified Chinese only.

## 2026-10, orchestration improvements wave 2: PostgreSQL and multiple processes

**What to do**: in a multi-process deployment, stop all worker processes before upgrading; pass values that code nodes need explicitly; don't delete `.agentlab-data` in the data directory.

- The default is still SQLite in the data directory with a single process, so deployments that don't change their configuration behave the same after upgrading. The new columns and indexes of this wave are added automatically at startup. Run events' `(run_id, seq)` becomes a unique index; if an existing database already has duplicate numbers, a log line is written and the index isn't built this time, without affecting startup.
- The data directory gains a `.agentlab-data` marker file, which `deploy.sh` uses (or else `agentlab.db` or `.secret_key`) to recognize the data directory; don't delete it.
- The app database can be switched to PostgreSQL, and existing data imported with `python -m app.db copy-to-postgres`; Docker deployments can use a built-in or an external PostgreSQL, but not both (see [PostgreSQL and Scaling](postgresql-and-scaling.md) and [Deployment](deployment.md#where-the-app-database-goes)).
- With PostgreSQL, you can run multiple worker processes (`AGENTLAB_WORKERS`). SQLite supports a single process only, and a second process refuses to start. Upgrading requires stopping all worker processes first: while the database still has live processes of a different version, the new version refuses to start (`deploy.sh upgrade` does exactly this).
- Runs queue by class: interactive, app, scheduled, and system. The site-wide limit is still `AGENTLAB_MAX_CONCURRENT_RUNS`; by default a quarter of it is reserved for interactive runs, adjustable under Settings → Preferences → Run Concurrency. Under full load, evaluations and recalls are slower than before, in exchange for Ask Data always having a slot; an evaluation question's time limit doesn't include time spent queuing.
- `deploy.sh upgrade` backs up automatically before upgrading: SQLite is copied to `backups/sqlite/`, and the built-in PostgreSQL is saved with `pg_dump` to `backups/postgres/`; each keeps the latest 5 copies. `restart` restarts only the service by default; `restart --postgres` restarts the database too.
- The bubblewrap sandbox no longer inherits the backend's environment variables: code nodes can no longer read variables such as model API keys or database addresses. Pass any values the code needs explicitly.
- When a subworkflow runs several times inside a loop, idempotency keys for writes to external systems now use the global id within the parent run: previously, a write in the loop's second iteration would get the first iteration's receipt and not be sent; now it is sent as usual. Runs paused inside a subworkflow before the upgrade still use the old keys when resumed, and writes that already took effect aren't resent.
- Ask Data heartbeats are no longer stored, and event logs are purged for turns that ended more than 14 days ago (the turn's questions and answer stay in the conversation). When a turn is taken over after a service restart, it is checked again against the account's current role, and turns of accounts that were disabled or lost permission are honestly marked interrupted.
- When a multi-row query has no `ORDER BY`, citing one of its cells no longer counts as "from a caliber"; recall and reconciliation mark it "Row order undefined", and the E1 statistic changes accordingly. A recall whose metrics all have undefined row order concludes "Cannot verify" and opens a task, instead of saying "Unchanged". When reconciliation needs a person to choose the metrics to compare, the columns verified number by number are preselected.
- Tasks, notifications, and webhooks state plainly where a rejection went ("Rejected, routed to X", "This path ends here"); inside a subworkflow they say "Subworkflow › x". When a step fails inside a subworkflow, automatic review reports it only once. User-visible lists break ties between entries with the same name and time by id, so both kinds of database give the same order.

## 2026-10, orchestration improvements wave 1

- A whole Ask Data turn (building the graph, starting the run, review, redoing per the review, saving) now runs in a background job on the server, and the page only watches; stopping goes through the `/api/chat/turns` API. On a service restart, a turn still building its graph is marked interrupted and can be retried. A conversation runs only one turn at a time. Ask Data for users is now reviewed too; continuing a run is still limited to builders and above (see [Ask Data and Copilot](ask-data.md#what-happens-in-a-turn)).
- Every step inside a subworkflow now goes into the same run's event stream; fields at the subworkflow's exit return to the parent graph with their provenance (up to 3 levels), and if something inside the subworkflow can't be traced back to raw data, the parent graph is downgraded just the same (see [Subworkflows](workflows.md#subworkflows)).
- Approvals, supplementary input, draft editing, action confirmations, code and tool approvals, and double-blind adjudication can take an extra Reject exit. Without it, behavior is unchanged (recorded as "Cancelled · Rejected"). The pre-publish check blocks reject paths that can never reach the end (see [Rejection](workflows.md#rejection)).
- Schedules can choose Event trigger, which follows the `AGENTLAB_SCHEDULER` switch: when it is off, events are kept but not dispatched (see [Event triggers](schedules.md#event-triggers)).

## 2026-10, next round wave 4

**What to do**: external systems that read events should tell rejections apart from failures by the `reason` of `node.failed`.

- The navigation gains Evaluations (visible to builders and above, ⌥0, placed before Settings).
- After a new version of a governed caliber is published, the system recalls and recomputes, in the background, the answers that used the old version, and opens a Recall task only when a number really changed; each publish recomputes at most `ledger.recall_limit` groups (default 50). The automatic batch after publishing and the continuation at startup follow the `AGENTLAB_SCHEDULER` switch (see [Recall](evaluation-and-ledger.md#recall)).
- When a review changes the catalog items that affect numbers, or the schema drops a table, drops a column, or changes a column type, the answers that used it are marked "Basis changed"; a task is opened only for answers that newly have error-level issues under the new catalog.
- For a rejected step, the `node.failed` event additionally carries `reason: "rejected"` (the same field as on `run.cancelled`), and the canvas, track, timeline, and Ask Data progress all show "Rejected". Old runs are unchanged.
- The graph-building usage of each Ask Data turn is now recorded after the model stream has been read to the end: output tokens reported only at the end of the stream used to go unrecorded, so a turn costs somewhat more than before; the difference is what used to be missed.
- Subscribable notification events gain "Data source has changes to handle" and "Caliber version bumped, answer numbers changed"; neither message includes numbers.
- Caliber labels are now judged number by number, and the matching algorithm moves to version 4; old matching records remain readable and aren't recomputed automatically. Evaluation scoring rules move to version 2, and old evaluations can be re-scored under the new rules from the evaluation details (reading only the sealed records, without calling models).
- When Copilot returns only thinking and no operations, both Ask Data and evaluation automatically try again once. Cancelled and timed-out model calls are also recorded as calls (marked "Incomplete").
- Drafting and profiling progress on the data catalog page moves to a background job bar, and the page header keeps only the entry points. Scheduled Maintenance can be set on the data source card. Links to a particular data source card are now always `/data/databases#source-<id>`.

## 2026-10, next round wave 3

**What to do**: nothing is required. The first introspection after upgrading only builds a schema baseline for each table and reports no changes.

- Schema introspection records a schema version for every table and compares it with the previous one, recording changes in the data source's change log. For a formal run started from a published version, if the schema it references changed after publishing, the run gets a "Schema change" reminder (`schema.drift`, inside the sealed scope).
- Profiling gains value observation; when a code table marked "Complete" shows a new value, the system revokes "Complete" (see [Value observation](data-catalog.md#value-observation)).
- Data sources can have scheduled maintenance, and a schedule's target can also be a test suite. These two target types don't accept webhooks (they return 422); notifications go through channel subscriptions under Settings → Notifications.
- In this wave, the change log, evaluation, and caliber issuance and reconciliation were API-only (`/api/datasources/{id}/changes`, `/api/evals/…`, `/api/ledger/answers/{run_id}/…`); the UI shipped with wave 4.
- When an Ask Data answer as a whole is equivalent to a published governed caliber, the system automatically starts a formal run by that caliber. The `ledger` group in Settings can turn this off (`auto_issue`); automatic and one-click issuance together are capped at 20 per day.
- When app views, trends, share links, and the view data API take "the latest sealed run", runs from issuance by caliber or from backfills never replace the numbers on a dashboard (see [Views and stamps](apps.md#views-and-stamps)).
- With login on, the only raw run records, events, and live streams a user can read are those of their own Ask Data runs.
- Notifications gain email (SMTP), the daily task digest (`notify.digest_at`), and access key expiry reminders; the last two aren't sent when login is off.
- Share links are off by default (see [Share links](apps.md#share-links)).

## 2026-10, next round wave 2

**What to do**: after upgrading, click Introspect Schema once for each data source to build the object index; for databases with more than 500 tables, set an introspection scope first.

- Schema introspection becomes a background job, queued with drafting and profiling for the same data source. The old schema stays as it is until introspection finishes reading.
- For a database with more than 500 tables and no introspection scope, the first introspection is refused and says how many tables matched. Set rules under Introspection Scope first, or raise the limit (at most 2,000).
- Formal introspection no longer accepts `?schema=` for switching schemas temporarily (one schema per data source); previews still do.
- At run time, `db_schema__` without a table name lists only the first 50 tables; naming a table outside the introspection scope reads its schema on the spot and adds it to the scope (at most 8 tables per run, with an audit trail).
- After an exploratory run is sealed, it is matched against published calibers in the background, and a post-seal event `ledger.matched` is appended, outside the sealed scope. At startup, runs from the last 7 days are matched retroactively, following the `AGENTLAB_SCHEDULER` switch.
- Report exports and verifiable report downloads each record an audit entry, `report.exported`. Runs started from Ask Data carry their conversation and turn (`origin_ref`), and workflows created with Open in Canvas record which turn they came from.

## 2026-10, next round wave 1

**What to do**: portals that already embed AgentLab in an iframe must add the portal's address to `AGENTLAB_FRAME_ANCESTORS`; deployments that filled in the public URL in settings should check the OIDC callback registered with the identity provider; after upgrading, click Introspect Schema once for each data source.

- Runs rejected by a person are recorded as "Cancelled · Rejected" and no longer count as failures: notifications go out as "Instance finished", schedules push "Not issued (rejected)", and the run can't be continued. Gates configured with retry on failure or Continue on error no longer ask again after a rejection and don't carry on downstream. Old runs that are already sealed aren't rewritten.
- With login on, people pickers in app entry forms and task forms are checked against a candidate pool, and a name outside the pool gets a 422 (`principal`).
- By default, pages can only be embedded by the same site (`frame-ancestors 'self'`, plus `X-Frame-Options`).
- The public URL is now a single setting (Settings → General; `AGENTLAB_PUBLIC_URL` takes precedence, and a malformed value fails at startup). For deployments that filled in the address in settings, the OIDC callback is now built from it; if it differs from what is registered with the identity provider, update the registration. With login on, `/docs` and `/openapi.json` require a builder login.
- Schema introspection now stores column types in each database's full notation (`VARCHAR2(20 CHAR)`, `ENUM('a','b')`). Schema snapshot hashes of new runs change; old runs are unaffected. On MySQL / MariaDB, the first re-profiling after the upgrade bumps the catalog of these tables up one version (text columns gain a sentence about the collation, ENUM columns gain values that don't appear in the data, and date columns state how many invalid dates were excluded). The profiling setting "Sampled key count" is renamed "Checked key count".

## App platform

**What to do**: where subworkflows or caliber cards pin an old version without formal-run credentials, follow the prompt and pin a published version instead; from now on, a new internal-network MCP must first be registered by an admin under Tools → Endpoints.

The first startup after the upgrade does these things automatically, all idempotent:

- Existing pending approvals get tasks created for them; plaintext in custom tools' request headers and in MCP environment variables gets encrypted; the capability registry is synced once.
- Old versions that were published before levels were recorded and that have formal-run credentials are retroactively marked Published. Versions pinned by subworkflows and caliber cards must be published and not retired; when an old version without credentials is pinned, formal runs are refused.
- HTTP MCP services that point at the local machine or the internal network automatically get an endpoint registered with internal network access enabled (`mcp-<service-name>`) and are bound to it, with an audit entry.
- When `/api/copilot/generate` edits a graph, it outputs only the change operations instead of a whole new graph. Every run gets an extra `run.origin` event (inside the sealed scope).
