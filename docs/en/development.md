English | [Simplified Chinese](../zh-CN/development.md)

# Development

This page covers local development: the environment, starting the app, the directory layout, dependencies and the lock file, tests, and the check scripts. It is for people who change the code. For why the system is designed this way, see [Architecture and Design Decisions](architecture.md). The user interface is currently available in Simplified Chinese only.

## Development environment

- You need conda ([miniforge](https://conda-forge.org/miniforge/) is enough), Node 20+, and pnpm.
- **No Docker needed**: the code sandbox is built on isolation primitives that ship with the operating system, with an optional upgrade to microVM (see [Configuration](configuration.md#enable-microvm)).
- You can try everything without an API key: the built-in Mock provider runs the whole orchestration path end to end (see [Configuration](configuration.md#model-access)).

## Start

```bash
conda env create -f environment.yml   # first time: create the agentlab environment and install dependencies
./scripts/dev.sh                      # after that: one command starts the frontend and backend
```

- Open http://localhost:5273 .
- `dev.sh` finds the `agentlab` conda environment automatically and creates it from `environment.yml` when it doesn't exist. To use a different environment name, set `AGENTLAB_CONDA_ENV`.
- Ports: the frontend defaults to 5273 and the backend to 8000; change them with `AGENTLAB_WEB_PORT` / `AGENTLAB_PORT`.
- `--host`: the frontend listens on all network interfaces and prints the LAN address at startup, so phones and other computers on the same network can open it. The backend still listens only on 127.0.0.1, and the frontend dev server forwards the page's `/api` locally. Login is off by default, so only do this on a network you trust (see [Login modes](identity-and-security.md#login-modes)).
- `--postgres`: use PostgreSQL for the app database; a development database is started in local docker (see [Start together with the database](postgresql-and-scaling.md#start-together-with-the-database)).
- Unrecognized arguments fail with an error; run `./scripts/dev.sh --help` for usage.
- `dev.sh` has hot reload on: changing a backend file means a service restart, and runs in progress show as interrupted with their checkpoints intact, ready to resume (see [Resume from a checkpoint](workflows.md#resume-from-a-checkpoint)).

## Directory layout

```
backend/                  FastAPI + LangGraph
  app/engine/             compiles and executes canvas JSON → StateGraph
    schema.py             graph definition and static validation
    compiler.py           compilation: node wrapping, events, retries, conditional routing
    runner.py             execution scheduling, event bus, interrupts and resumption
    issuance.py           issuance contract: number trace-back and the three-tier verdict
    evidence.py           clickable evidence: evidence ledger, citation markers, chunking and verifying report documents (pure functions)
    judge.py              claim-sentence judge: prefiltering, cost cap, writing verdicts back
    governance.py         publishing gates: governed lint and evidence gates G1–G5
    autofix.py            automatic pre-publish fixes; upgrade.py does the one-click upgrade to a traceable structure
    guards.py             agent guardrails: repeated calls, budgets, context compression
    review.py             output review: event-stream rule scan + model review
    expressions.py        template interpolation + AST-allowlist expression evaluation
    nodes/                executors for each node type (report.py is the report writer)
  app/data/               data source access: connections, SQL guard, schema introspection, spreadsheet import
  app/providers/          multi-model access and cost estimation
  app/sandbox/            four backends: microVM / Seatbelt / bubblewrap / bare subprocess
  app/tools/              tool registry, built-in tools, MCP, custom tools
  app/memory/             memory, knowledge base, inverted index, hybrid retrieval, document parsing
  app/api/                REST + WebSocket (the evidence API is in evidence.py)
frontend/                 React 19 + React Flow + Zustand
  src/canvas/nodeDefs.ts  node metadata: drives the properties panel, the node library, and the connection handles
  src/canvas/routing.ts   edge routing: staggered ports, corridor lanes, detours for long edges
  src/run/decode.ts       the single translation layer from the event stream to human-readable steps
  src/run/EvidenceDoc.tsx renders the report segment by segment: numbers, names, and quotes are all clickable fragments; EvidencePanel.tsx is the provenance chain
  src/store/studio.ts     canvas state + mapping from the event stream to highlights
environment.yml           conda environment definition
scripts/dev.sh            one-command startup
scripts/check-all.mjs     main entry for the frontend checks: runs every check-*.mjs in parallel lanes and summarizes them in one table
```

## Dependencies and the lock file

- `backend/pyproject.toml` specifies version ranges and `backend/requirements.lock` specifies exact versions: all backend dependencies (including transitive dependencies and all extras) are locked to the set the tests ran against, and the lock works across platforms.
- `environment.yml` uses the lock file as a constraint when installing packages, so another machine gets the same set too, not whatever is newest that day.
- The LangGraph / LangChain family is allowed only up to the next minor version in `pyproject.toml`: the engine depends directly on their checkpoint format, interrupt / resume, and streaming events, and these have changed behavior even in minor releases.
- The checkpoint format itself lives in `langgraph-checkpoint` (langgraph only requires it to be <5, so it is pinned separately). Both checkpoint libraries, for SQLite and for PostgreSQL (`langgraph-checkpoint-sqlite`, `langgraph-checkpoint-postgres`), read and write according to it, and `copy-to-postgres` can move checkpoints only because both sides use the same format version.
- From 3.1.3 on, `langgraph-checkpoint-postgres` requires `langgraph-checkpoint` 4.3, so it is locked at 3.1.2; upgrade the three together.
- The same goes for aiosqlite and SQLAlchemy: `db/base.py` patches aiosqlite, and the connection pool runs into that patch when it closes connections. After the app database moves to PostgreSQL, the patch stays in place, because uploaded spreadsheets and manually registered SQLite data sources still go through aiosqlite.
- `[postgres]` holds what the app database needs on PostgreSQL: on the SQLAlchemy side it goes through asyncpg (the same package `[db]` uses to connect to business databases), and the checkpoint library is the official `langgraph-checkpoint-postgres`, which only works with psycopg 3 and `psycopg-pool`. psycopg comes with `[binary]`: libpq is bundled in the wheel, so neither the image nor the local machine needs a separately installed PostgreSQL client.

### Optional dependencies

`environment.yml` installs all of them by default. When installing extras separately, pass the lock file so the versions match what was tested:

```bash
pip install -e 'backend[docs]' -c backend/requirements.lock     # PDF / Word / PowerPoint / Excel parsing
pip install -e 'backend[db]' -c backend/requirements.lock       # MySQL / PostgreSQL / Oracle drivers
pip install -e 'backend[microvm]' -c backend/requirements.lock  # microVM sandbox
pip install -e 'backend[postgres]' -c backend/requirements.lock # PostgreSQL for the app database (asyncpg, psycopg 3, the official checkpoint library)
pip install -e 'backend[dev]' -c backend/requirements.lock      # pytest
```

### Upgrading dependencies

Upgrading dependencies is something you do deliberately; after changing them, run the tests once before committing:

```bash
./scripts/lock-deps.sh --upgrade-package langgraph   # upgrade one package (plus whatever has to move with it)
./scripts/lock-deps.sh --upgrade                     # upgrade everything to the latest that pyproject allows
./scripts/lock-deps.sh                               # only added a new dependency: add it and leave the locked ones alone
pip install -e 'backend[microvm,dev,db,docs,postgres]' -c backend/requirements.lock   # install into the current environment
```

Regenerating the lock file requires [uv](https://docs.astral.sh/uv/); installing the dependencies themselves uses only pip.

## Tests

```bash
# Backend: about 14300 tests; with -n auto they run in parallel in about 1–2 minutes
cd backend && env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
  python -m pytest -n auto

# Frontend type check
cd frontend && npx tsc -b

# Run the same backend suite with the app database on PostgreSQL (starts a temporary postgres:16 container and deletes it afterwards)
PYTHON=$(conda run -n agentlab which python) ./scripts/test-postgres.sh
```

- Don't leave out the `env -u` in front of pytest: providers without a configured key fall back to keys in environment variables, and the seed at startup would also use them to create a provider. With a real key left in the environment, the tests might really call the API.
- For details on the PostgreSQL and multi-process tests, see [PostgreSQL and Scaling](postgresql-and-scaling.md#tests).

### A fake external system for acceptance tests

```bash
python scripts/fake-erp.py --port 8765
```

It can switch between "normal / time out after a successful write / 500 / 4xx", for acceptance-testing the different outcomes of writing to external systems; its API is described at the top of the script.

## Frontend checks

```bash
# 43 items, 8 lanes in parallel by default. They connect to 5273 / 8000, so start ./scripts/dev.sh first
node scripts/check-all.mjs
# Start 2 extra "backend + vite" stacks used only by the checks, each with its own copy of the data, about 6.5 minutes; editing code while they run doesn't interrupt them
CHECK_DATA_SRC=<data-dir> node scripts/check-all.mjs --stacks 2 --lanes 12
# Run only some of the items (names without the check- prefix)
node scripts/check-all.mjs evidence publish
```

- Full output is written to `/tmp/agentlab-checks/<timestamp>/`, and the terminal gets only the summary. Items that failed during the parallel run are rerun on their own at the end; ones that pass only on the rerun are listed separately as "Flaky".
- With `--stacks`, each stack copies the data and picks free ports automatically; the backend runs without hot reload and vite doesn't watch files; everything is cleaned up after a finish, an error, or an interruption. `--stacks` supports SQLite only.
- `scripts/e2e-check.mjs` really starts a run (Mock, costs nothing) and leaves records in the database, so it isn't part of check-all.
- `scripts/check-auth.mjs` (login, navigation collapsed by role, 401 redirecting to login) has to run on its own against a separate service with local-account login turned on, so it isn't part of check-all either.

The 43 `check-all` items, in the order of `ORDER` in `scripts/check-all.mjs`:

| Category | Checks |
|---|---|
| No browser | `tokens` (static design-token scan) `copy` (UI copy) `trace` (pure functions of the run-state core) `decode` (event translation layer) |
| Routing, components, and guardrails | `canvas-layout` `ui-kit` `guards` |
| Shell, answer stream, and run states | `shell` `stream` `evidence` (clickable evidence) `run-states` (node cards) `canvas-fx` (canvas surface layer) |
| Pages | `studio` `publish` (pre-publish check and autofix) `chat` `runs` `manage` `versions` (uploaded spreadsheet versions) `catalog` (data catalog) `catalog-loop` (Table Copilot, review queue, export and import) `catalog-profile` (per-table data profiling jobs) `catalog-changes` (changes, the changes section in table details, scheduled maintenance, the background job bar) `ui` (end to end, with real data) |
| Review suite and exports | `schedules` (schedules) `compare` (run comparison) `verify` (double-blind recomputation, adversarial review) `inquiry` (approval inquiry) `challenge` (adversarial review) `bundle` (verifiable report) `whatif` (what-if sandbox) `report-export` (report export) |
| Evaluation and caliber ledger | `evals` (test suites, adding questions, starting and progress, results, comparison and A/B, caliber hits and repetition) `ledger` (the Caliber step in evidence, turn summaries, issuance and reconciliation, distillable questions) |
| App platform | `apps` (app pages, stamps, clicking numbers to see provenance, entry validation, instance pages) `inbox` (joint sign-off progress, timeouts, the hash card for action confirmation, verification) `app-editor` (app editing, derivation, checks, choosing a disposition at publish time, Copilot) `capabilities` (capabilities, endpoints, identity, notifications, audit) `access-keys` (access keys) `share` (share links and the /s page) `blueprint` (creating apps, blueprint cards, trial runs, publish summaries) `personal` (personal apps) `records` (record list) `files` (file controls) |

### What the three verification layers cover

- `check-decode` guards "is the translation right", `check-stream` guards "is it drawn right", and `check-ui` guards "does the whole path work". The three can each pass while the whole is broken: for example, every Step field is correct, yet the component renders it as a screen full of escaped JSON.
- `check-canvas-fx` guards against "the events are right, but the UI didn't keep up": it pours scripted events into the store (going through the same applyEvent / NodeCard / FlowEdge the page uses; only the source of the events is a script) and asserts that the collaboration matrix really appears, the degree of parallelism is computed correctly, bar lengths are proportional to durations, the exit a branch took lights up, and light effects really aren't drawn when motion is turned off at the system level. Actually running a multi-agent graph needs a model connection and a wait of a dozen or more seconds, and still may not hit the moment when three members are running at once; a check like that would become a flaky failure.
- `check-canvas-layout` alone guards the only computable part of "does the graph look good": it hands fixture graphs to the backend's `/api/copilot/layout` for layout, then directly calls `import('/src/canvas/routing.ts')` to compute paths with the very routing code running on the page, and makes geometric assertions: no two edges overlap on the same segment, no edge passes through the middle of someone else's card, and both ends of every edge attach to a node's border. When edges pile up in a tangle, `check-ui` and `e2e-check` won't say a word.

## Documentation checks

```bash
python3 scripts/check-docs.py
```

It checks three things: `README.md` and `docs/en/` must contain no Chinese characters or full-width symbols (code blocks included); relative links and anchors in `README.md`, `README.zh-CN.md`, every document under `docs/`, and `deploy/README.md` must all resolve; and `docs/zh-CN/` and `docs/en/` must have the same files, with the same heading levels in each pair. Run it after changing the docs.
