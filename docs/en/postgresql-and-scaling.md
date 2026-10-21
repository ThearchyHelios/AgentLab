English | [Simplified Chinese](../zh-CN/postgresql-and-scaling.md)

# PostgreSQL and Scaling

This page covers switching the app database to PostgreSQL, importing data from SQLite, backups, and the rules for running several worker processes or several machines. It is for operators. The user interface is currently available in Simplified Chinese only.

## When to switch to PostgreSQL

The default app database is SQLite in the data directory: one machine, one process, nothing to install. To run several worker processes, you need PostgreSQL.

What moves to PostgreSQL is the app database (workflows, runs, events, approvals, tasks, data catalog, evaluations, and so on) and the execution checkpoints. Artifacts, the version databases of uploaded spreadsheets, original files, attachments, record tables, sandbox workspaces, and `.secret_key` remain files in the data directory, so you still need to set `AGENTLAB_DATA_DIR`.

## Use PostgreSQL

Set one environment variable:

```bash
AGENTLAB_DATABASE_URL=postgresql+asyncpg://agentlab:<password>@127.0.0.1:5432/agentlab   # postgresql:// is accepted too
```

- It needs the `[postgres]` extra (already included in `environment.yml` and the Docker image). If you set this variable without installing the extra, startup fails right away and says what to install: `pip install -e 'backend[postgres]' -c backend/requirements.lock`.
- Create the database first (`CREATE DATABASE agentlab`). The tables are created when the service starts; for a database created by an older version, missing columns and indexes are added at startup, the same as with SQLite.
- The first line of the startup log states which kind of database is in use, and which host and which database (without the password).

### SSL and the checkpoint database

- SSL: append `?sslmode=require` to the URL (or `verify-ca`, `verify-full`; asyncpg's own `?ssl=require` is accepted too); at startup it is converted into the parameter asyncpg understands.
- Certificate files don't go in the URL (asyncpg doesn't accept parameters such as `sslrootcert`, and startup fails if you include them). Set the environment variables `PGSSLROOTCERT`, `PGSSLCERT`, and `PGSSLKEY` instead; both the app database's asyncpg and the checkpoint library's libpq honor them.
- asyncpg doesn't accept other libpq parameters in the URL either (`application_name`, `connect_timeout`, and the like); these are likewise reported at startup.
- Checkpoints are LangGraph's four tables: checkpoints, checkpoint_blobs, checkpoint_writes, and checkpoint_migrations, whose names don't clash with the app's tables. They go in the same database by default; to put them elsewhere, set `AGENTLAB_CHECKPOINT_URL=postgresql://…` (this one uses libpq syntax, so `sslrootcert` and the like can be written in it directly).

### Connection pool and connection count

- Each process keeps `AGENTLAB_DB_POOL_SIZE` (default 10) connections open and opens up to `AGENTLAB_DB_MAX_OVERFLOW` (default 20) more at peak. Connections are checked for liveness before use and replaced every 30 minutes. The checkpoint library has a separate small pool.
- One process uses at most `AGENTLAB_DB_POOL_SIZE + AGENTLAB_DB_MAX_OVERFLOW` app database connections (default 10 + 20 = 30), and the checkpoint pool at most `min(AGENTLAB_MAX_CONCURRENT_RUNS, 20)` (at least 4, default 20), for a total of at most 50 per process. The import command takes a few more.
- PostgreSQL defaults to `max_connections=100`, and you also need to leave a few for `psql`, backups, and superusers (`superuser_reserved_connections` defaults to 3). One process fits; N processes need N times as many connections. Choose one of three fixes:
  - raise `max_connections` to roughly "number of processes × 50 + 20";
  - shrink both pools, for example `POOL_SIZE=5`, `MAX_OVERFLOW=10`, `MAX_CONCURRENT_RUNS=10`, for at most 25 per process;
  - put PgBouncer in front (see [Direct connections and PgBouncer](#direct-connections-and-pgbouncer)).
- With multiple processes, each process has one extra connection dedicated to inter-process notifications (LISTEN / NOTIFY), outside the pool, and a cross-process lock (a session-level advisory lock) occupies one connection while it is held.

### Behavior that matches SQLite

After switching to PostgreSQL, behavior matches SQLite:

- String columns have no length limit (SQLite never checks length anyway).
- Text is compared and sorted by bytes: columns carry `COLLATE "C"`, as in SQLite, regardless of the locale chosen when the database was created.
- Integer columns are BIGINT.
- JSON columns store the raw text (PostgreSQL json, not JSONB: JSONB reorders the keys of objects).
- Times are stored as UTC without a time zone.
- When sorting, NULLs in nullable columns take SQLite's positions: first in ascending order, last in descending. This covers only sorting directly by a column, not by expressions such as aggregates or coalesce. Lists all carry a tie-breaker such as id, so ties between entries with the same name and time sort the same on both databases.
- Filtering by a key inside JSON compares by JSON type (true doesn't equal 1).
- Two kinds of values that SQLite stores but PostgreSQL rejects are replaced: NaN / Infinity in JSON are written as null, and NUL characters in text (inside JSON or in ordinary text columns) are removed.

Case-insensitive search (the run list by name, the object index by comment): text columns are `COLLATE "C"`, and ILIKE on such a column only handles ASCII case, so case folding follows the locale chosen when the database was created. With a locale such as `en_US.UTF-8` or `C.UTF-8`, accented letters such as É/é and fullwidth Latin letters both match; a database created with the plain `C` locale handles only ASCII.

### Waiting for the database at startup

When the service starts together with the database's container while the database is still initializing, startup waits for it:

- It retries with backoff (1, 2, 4, 5, 5… seconds), logging "App database not ready yet" each time, and waits at most `AGENTLAB_DB_STARTUP_TIMEOUT` (default 90) seconds before exiting with an error.
- Errors that waiting won't fix, such as a wrong password or a database that doesn't exist (SQLSTATE classes 28, 3D, 42), fail immediately.
- Creating tables and adding columns and indexes happen under an advisory lock in the database, and default data is filled in under a cross-process lock. When several worker processes start at once, each of these is done only once.

## Start together with the database

- **Deployment**: the offline bundle's `./deploy.sh install --postgres` comes with a built-in PostgreSQL; `migrate-to-postgres` switches an SQLite deployment over (see the PostgreSQL section of [deploy/README.md](../../deploy/README.md), written in Chinese).
- **Development**: `./scripts/dev.sh --postgres` starts (or reuses, if it already exists) a local `postgres:16` container named `agentlab-dev-postgres`:
  - it listens only on `127.0.0.1:55432`, with its data in the docker volume `agentlab-dev-pgdata`;
  - the password is generated automatically the first time in `data/.postgres-dev/` and read by the backend via `PGPASSFILE`; the backend's `AGENTLAB_DATABASE_URL` points at this container;
  - Ctrl-C stops only the frontend and backend, and the database container is kept for reuse next time;
  - change the container name, port, volume, and image with `AGENTLAB_DEV_PG_CONTAINER`, `AGENTLAB_DEV_PG_PORT`, `AGENTLAB_DEV_PG_VOLUME`, and `AGENTLAB_DEV_PG_IMAGE` (as environment variables or in `.env`, with environment variables taking precedence).
- Without `--postgres`, everything is as before: SQLite, no docker.

## Move over from SQLite

`python -m app.db copy-to-postgres` imports `agentlab.db` and `checkpoints.db` from the data directory into PostgreSQL. Runs paused at an approval, or interrupted, can still continue after the import.

```bash
# 1. Stop the service (the import checks this: if agentlab.db-wal / checkpoints.db-wal is still next to the source, or a service is already connected to the target database, it refuses)
# 2. Back up the data directory (copy it while the service is stopped)
# 3. Verify only: count what the source has and what the target already has, writing nothing
cd backend && python -m app.db copy-to-postgres ../data postgresql+asyncpg://agentlab:<password>@127.0.0.1:5432/agentlab --dry-run
# 4. Import: create the tables and insert table by table in batches, in foreign-key order; at the end, verify each table by primary key and the checkpoints by count; the exit code is non-zero if anything wasn't imported
cd backend && python -m app.db copy-to-postgres ../data postgresql+asyncpg://agentlab:<password>@127.0.0.1:5432/agentlab
# 5. Set AGENTLAB_DATABASE_URL (with AGENTLAB_DATA_DIR still pointing at the original data directory) and start the service
```

- Without a target URL, `AGENTLAB_DATABASE_URL` is used; `--checkpoint-url` imports the checkpoints into a different database; `--work-dir` holds the snapshot (leave as much space as the two databases take up; it defaults to the system temp directory).
- The import reads a snapshot copied out through SQLite's backup API and first brings the snapshot up to the current schema; the source's contents are never changed.
- Rerunning is idempotent: rows the target already has with the same primary key (or that collide with another unique constraint) are left untouched, and only the missing ones are added, with no duplicates and no overwrites. So rows changed on SQLite after the import won't be carried over by a rerun; to redo everything, empty the target database and import again. Checkpoints work the same way: existing ones aren't rewritten.
- Only the two conditions in step 1 can be checked. A service connected to a different data directory, or a service on another machine connected to the target database, can't be detected; you have to make sure of that yourself. When the service stops normally it closes all its connections, and the -wal files go away with them; any still present were left by an earlier force-kill. After confirming the service is stopped, add `--assume-stopped`.
- Data in old databases that SQLite can store but PostgreSQL won't accept is handled on the snapshot first and listed plainly in the report:
  - NOT NULL columns left holding NULL, because an old version added the column without a default, are filled with the ORM model's default (the startup migration fills them the same way on SQLite, so both sides store the same thing);
  - **orphan rows** (rows whose foreign key points at a row that no longer exists, left over because SQLite didn't enforce foreign keys at the time): where the foreign key is SET NULL, the foreign key is set to null; otherwise (CASCADE, non-nullable) the rows aren't imported. Their counts are listed per table, and they don't count as verification failures (they should have been deleted along with their parent rows anyway).
- Auto-increment primary key sequences continue after the largest number SQLite has used; deleted numbers aren't reused.
- Leaving `agentlab.db` in the data directory after the import is fine; it can serve as a fallback. It still counts as AgentLab's own file and can't be registered as a data source.

## Backups

- Back up PostgreSQL with `pg_dump` (if the app tables and the checkpoints share a database, one dump covers both), and copy the whole data directory as before.
- Ideally, do both together while the service is stopped. If you don't stop the service, dump the database first and then copy the data directory: artifacts and original files are content-addressed and only ever added, so every file the database references is there when you copy the data directory.
- The exception is the form-submission record tables, `records/*.db`: they are SQLite files appended in place, so copying them while the service is running may capture a half-written file. Either copy them with the service stopped, or copy them one by one with SQLite's backup API:

```bash
sqlite3 data/records/x.v1.db ".backup /backup-dir/x.v1.db"
```

## Multiple processes

When the app database is PostgreSQL, you can start several worker processes pointing at the same database and the same data directory:

```bash
AGENTLAB_DATABASE_URL=postgresql+asyncpg://… ./scripts/prod.sh --workers 4      # one machine: uvicorn starts 4 processes sharing one port
AGENTLAB_WORKERS=4 …                                                             # Docker: add this line to agentlab.env (deploy/README.md)
```

- Running one copy each in several containers or on several machines also works: put a load balancer in front; WebSocket and SSE don't need sticky sessions.
- **SQLite supports a single process only**: there is a file lock on the data directory, so a second process refuses to start and says so plainly; it likewise refuses when `AGENTLAB_WORKERS` is greater than 1 while the app database is SQLite.
- The connection count multiplies with the number of processes; see [Connection pool and connection count](#connection-pool-and-connection-count).

| Setting | Default | What it means |
| --- | --- | --- |
| `AGENTLAB_WORKERS` | 1 | How many worker processes to start (`prod.sh` and the Docker entrypoint pass it to uvicorn as `--workers`) |
| `AGENTLAB_WORKER_HEARTBEAT_S` | 5 | How often each process renews its lease and picks up requests sent to it (such as stops) |
| `AGENTLAB_WORKER_LEASE_S` | 30 | How long a lease lasts (at least 3 times the heartbeat): how long after a process dies (force-killed, power loss) another process takes over |
| `AGENTLAB_SCHEDULER_TICK_S` | 15 | Interval between scheduler loop ticks |

### Direct connections and PgBouncer

A few kinds of connections depend on session state: the one that sends and receives inter-process notifications (LISTEN), cross-process locks (session-level advisory locks, which must be acquired and released on the same session), and the checkpoint library (prepared statements, plus a session lock when creating tables).

- Ordinary queries go through `AGENTLAB_DATABASE_URL`.
- When `AGENTLAB_DATABASE_DIRECT_URL` is set (same syntax), the connections above go through the direct-connection URL; the checkpoint library uses it by default too, unless `AGENTLAB_CHECKPOINT_URL` is given separately.
- When it isn't set, everything uses the same URL.

Three deployments:

| Deployment | How to configure |
| --- | --- |
| Direct PostgreSQL | Set only `AGENTLAB_DATABASE_URL`. The simplest, and the recommended setup for multiple processes |
| PgBouncer session mode (`pool_mode=session`) | Just point `AGENTLAB_DATABASE_URL` at PgBouncer: in session mode each client connection has a server connection to itself, so all session state is there |
| PgBouncer transaction mode (`pool_mode=transaction`) | Point `AGENTLAB_DATABASE_URL` at PgBouncer, and also set `AGENTLAB_DB_PGBOUNCER=1` and `AGENTLAB_DATABASE_DIRECT_URL` (direct to PostgreSQL, or to PgBouncer's session-mode database). Startup is refused without a direct URL |

What `AGENTLAB_DB_PGBOUNCER=1` does (only for the ordinary URL):

- It doesn't use the client-side connection pool, leaving pooling to PgBouncer.
- It turns off the prepared statement caches of SQLAlchemy and asyncpg: in transaction mode, the next statement may land on a different server connection, where the cached prepared statement doesn't exist.
- asyncpg still prepares each statement before executing it (within the same transaction, on the same server connection), using random names so they don't collide with ones left by other clients. On the PgBouncer side, configure `server_reset_query = DISCARD ALL` plus `server_reset_query_always = 1` to clean up leftover prepared statements, or use version 1.21 or later with `max_prepared_statements` turned on.
- Transaction-level advisory locks (used for handing out slots, for same-source queues, and when creating tables) still hold in transaction mode: the lock is acquired and released within the same transaction.

These points were checked against the documentation and code of SQLAlchemy, asyncpg, and psycopg, but haven't been run against a real PgBouncer.

### Claims, leases, and takeover

- Each process registers a row in the database (host, pid, a random string generated at startup) and renews its lease on every heartbeat tick.
- Runs, Ask Data turns, background jobs (introspection, drafting, profiling, evaluation, shadow drafting), documents being processed, and publish plans being executed all record the process that claimed them. Leases are per process: when a process dies, all the rows it claimed expire together.
- A process that shuts down gracefully hands over and deletes its own registration, and other processes take over immediately; for a force-killed process, they wait for its lease to expire.
- Cleanup at startup only touches rows whose owner is gone, never those of other live processes.

After the owner is gone:

| What | How it is handled |
| --- | --- |
| Running runs | Marked interrupted (can continue from the checkpoint), with an added note "The service did not shut down cleanly last time". Steps with side effects aren't rerun automatically; a person clicks Continue Run |
| Queued runs | Marked failed (they never started executing and have no checkpoint, so "resuming" could only fabricate a result), with a failure reason added |
| Runs paused at an approval | Belong to no process: whoever receives the decision carries on, and the same decision is executed by only one process (approvals and runs are both updated conditionally) |
| Ask Data turns | Turns still building the graph are marked interrupted (Retry This Turn is available); for turns that already started a run, the process that takes over keeps waiting for that run, then reviews and wraps up as usual |
| Drafting, profiling, introspection, evaluation, shadow drafting | Marked interrupted (unfinished tables and questions are recorded as stopped); click Retry in the UI to carry on |
| Documents being processed, publish plans being executed | Documents are marked failed (upload them again); publish plans are changed to stopped, halted at the step that didn't finish, and can be continued |
| Pending approvals, writes with unknown results | Missing tasks are created; for unknown results with no verification scheduled yet, verification is added (writes whose run is still executing in another live process are left alone) |

### Stopping

When a run, an Ask Data turn, or a background job is running in another process, Stop writes a request for the owner (on PostgreSQL, NOTIFY wakes it up, within one heartbeat tick at the latest). The owner stops and wraps up under its own process's rules, and `run.cancelled` records who stopped it. In the UI it looks the same as with a single process. If the owner is gone, takeover follows the table above.

### Run concurrency

Each execution segment (a start, carrying on after an approval, continuing a run) needs a slot before it begins. Slots are divided by origin into four classes, counted across all processes together:

| Class | Includes | Default priority |
|---|---|---|
| Interactive | Ask Data, canvas trial runs, formal runs started by hand, blueprint trial runs, personal app refreshes | 40 |
| App | App entries, on-demand views, hooks, the API | 30 |
| Scheduled | Schedules, event triggers | 20 |
| System | Evaluation, caliber recall, reconciliation, backfill, formal issuance by caliber | 10 |

- The site-wide limit is `AGENTLAB_MAX_CONCURRENT_RUNS` (default 20).
- Part of it is **reserved for interactive runs**: by default 1/4 of the site-wide limit, at least 1 and at most the site-wide limit minus 1. The app, scheduled, and system classes combined can never use the reserve, so there is always a slot when someone uses Ask Data or does a trial run.
- Outside the reserve, the other three classes can **borrow**: each class's limit defaults to "site-wide limit − reserve", so evaluations can use all of those slots when nobody is using Ask Data; you can lower these limits in settings. The interactive class's own limit defaults to the site-wide limit.
- While waiting for a slot, runs are ordered by priority, first come first served within the same level; what is already executing isn't interrupted.
- Change these under Settings → Preferences → Run Concurrency (admins). After a change, every process hands out slots by the new values within a few seconds at most.
- A single process hands out slots by the same rules. Under full load, evaluations and recalls are slower, in exchange for Ask Data always having a slot.

### Scheduler leader

Only one process is on duty (a single lease in the database). These things happen only in the process on duty: claiming schedules, dispatching events, delivering webhooks, process time limits and verification, the daily digest, all kinds of periodic cleanup, retroactive caliber matching and continuing automatic recalls, and taking over rows left by processes that are gone.

- The other processes stand by; when the process on duty is gone, a standby takes over. A graceful shutdown hands over immediately.
- Nothing runs twice during the switchover: schedule claims are conditional updates, and trigger and delivery records have unique constraints.

### Live events

- Run events and Ask Data turn events are stored first. When the process a page's WebSocket / SSE is connected to isn't the one executing the run, it follows along from the database by sequence number.
- The executing process sends a NOTIFY (carrying only the run or turn id and the sequence number) for every batch of events it commits, and other processes read from the database when they receive it. If a NOTIFY is lost, there is a fallback: run streams check the database every 2 seconds, and Ask Data streams every heartbeat tick.
- The two things that aren't stored (body tokens and thinking deltas, and the Ask Data Copilot's keep-alive heartbeats) are forwarded only to processes where someone is watching, packed in short batches into a NOTIFY.
- Replay, reconnection, and heartbeat behavior is unchanged. Sequence numbers are assigned by the executing process; other processes hand events for an executing run over to it to record; appends to a paused run continue numbering from the last row in the database (a conditional update prevents number collisions).

### State shared across processes

- **Consistent site-wide and kept in the database**: the daily spending accounts (judge, verification, evaluation), which lock their row before reading and writing; per-minute rate limiting for access keys; the backoff after failed logins; same-source queues (drafting, profiling, and introspection on the same data source, one after another).
- **Guarded by cross-process locks (PostgreSQL advisory locks)**: on-demand introspection, adding tables to the introspection scope, version storage for uploaded spreadsheets, record table writes, single-flight and daily caps for on-demand views, personal app refreshes, issuance, recall, backfill, and scheduled starts.
- **In each process's memory, with change notifications**: login settings, the public URL, the embedding model, the capability registry's governance snapshot, data source connection pools, and app page caches. The process that makes a change refreshes immediately; the others reload once they get a notification (or within one heartbeat tick at the latest).
- **Pure performance caches** stay in each process, where staleness is harmless: Ask Data vectors, value guesses, schema snapshots, the masking sets of data-fetch snapshots, OIDC metadata, DoH resolutions, xlsx column scans, and the SQL shapes used in caliber matching.
- Access key request logs accumulate in each process's memory first and are written to the database on every heartbeat tick.
- Each process's own concurrency gates (caliber matching, automatic comparison, drafting model calls, upload parsing) count per process, so the total grows with the number of processes.

### Multiple machines

When several machines share one data directory:

- Put the data directory on shared storage (NFS or the like), mounted on every machine at **the same absolute path** (the database records the absolute paths of uploaded spreadsheets' version database files).
- Keep the machines' clocks synchronized (NTP). Leases go by the database's clock, so clock differences between machines don't affect who counts as alive; but judgments such as "stopped for two lease durations" look at times each machine wrote using its own local clock.
- When the database server's own clock jumps forward by more than one lease (rare), every process finds its lease already expired when it renews, and they all fence themselves off together: runs halfway through execution are marked interrupted, and clicking Continue Run in run details continues from the checkpoint; background jobs are handled under the "owner is gone" rules; each process automatically re-registers with a new identity, after which new starts proceed as usual. A backward jump causes no fencing; a process that really is gone just takes that much longer to be taken over.
- A jump in a machine's own wall clock (an NTP step correction, a manual time change) causes no fencing: whether a renewal was late is judged by a monotonic clock that counts sleep time, and when the two disagree, the lease in the database wins.
- Files are always written to a temporary file first and then renamed, so readers never see a half-written file; writes to uploaded spreadsheets' version databases and to record tables go one after another under a lock in the database.

To be honest about the limits: the data directory still contains SQLite files (the version databases of uploaded spreadsheets, read-only after publishing; the form-submission record tables `records/*.db`, appended in place; and databases created by code inside the sandbox). For SQLite files on a network file system, read/write consistency relies on file locks, and file locks on NFS are unreliable: if one machine reads while another is appending, it may read inconsistent pages. So with multiple machines, deployments that write a lot to record tables are better off running multiple processes on one machine, or putting the data directory on storage with reliable file locking.

Sandbox sessions (microVM) belong to each process and aren't shared across processes.

### Upgrades

Processes of an old and a new version connected to one database at the same time aren't supported, and the code blocks it:

- Each process registers with its code version (package version, build number or git commit, and a source fingerprint) and its schema version. The source fingerprint includes the dependency lock file, so upgrading only a dependency also counts as a different version.
- While the database still has live processes of a different version, a new-version process refuses to start before adding any columns; the error lists every such process, the version of each, and whether its heartbeat is still going. Processes of the same version join as usual.

Upgrade steps:

1. Stop all old-version processes. A graceful shutdown deletes its own registration immediately; for a force-killed process, its lease has to expire, and the new process waits for that at startup.
2. Back up: `pg_dump` plus the data directory (see [Backups](#backups)).
3. Start the new version: the first process adds the columns and indexes, and the ones after it join as usual.

If you start the new version after stopping only some of the old processes, the new version won't start and the old version keeps running as usual, so the two never write the same rows each by its own rules. For Docker deployments, `./deploy.sh upgrade` follows exactly this order.

## Tests

- `scripts/test-postgres.sh` starts a temporary container from the `postgres:16` image already present in local docker (bound only to a random port on 127.0.0.1 and deleted afterwards) and runs the full backend suite against it; arguments are passed to pytest unchanged. Tests that hold only on SQLite are marked `sqlite_only` and skipped on PostgreSQL.
- `scripts/test-multiprocess.sh` starts a temporary database from `postgres:16` in local docker, starts two worker processes on it, and runs end to end with the demo model (`backend/tests/test_multiprocess_e2e.py`, skipped by default and run when `AGENTLAB_MULTIPROCESS_E2E=1`). It verifies that: a run can be claimed by either process; a stream on the other process receives live events; the same decision is executed only once; stopping works across processes; a schedule fires only once; priorities take effect when evaluation and Ask Data run concurrently; the daily spending cap isn't exceeded; and after one process is force-killed, its work is taken over or honestly marked interrupted.
- The frontend check scripts' `--stacks` option (isolated replica services) supports SQLite only.
