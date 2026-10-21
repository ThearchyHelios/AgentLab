English | [Simplified Chinese](README.zh-CN.md)

# AgentLab

**A visual agent-orchestration workbench**: define workflows on a canvas, watch execution node by node at run time, and put verifiable constraints on what comes out. Templates fix the skeleton, and agents are free only at the joints they are authorized for; the system fills the numbers in a report from snapshots, and every number opens to its provenance. The user interface is currently available in Simplified Chinese only.

![Canvas during a run](docs/images/canvas-running.png)

<sub>The Research Assistant template running on Mock: finished nodes get a check mark, and running nodes show elapsed time and streaming output. The track along the bottom is a live timeline; the right column shows the run steps, driven by the same event stream.</sub>

## Why use it

- **Numbers you can check**: all arithmetic happens in SQL and in caliber cards (controlled metric sets), and the narrative layer has no right to create numbers. The report-writer node writes only citation markers, the system fills in the numbers from snapshots, and every number opens all the way to the cell in the query result it came from.
- **Constrained orchestration**: the engine is compiled, so at run time the model can't change the graph structure; its freedom is confined to the inside of agent nodes and to branch decisions. Fully dynamic multi-agent collaboration stays in the exploration layer.
- **Pinned, reproducible versions**: formal runs accept only published immutable versions (pinned by graph hash). The complete evidence of every execution goes into a content-addressed artifact store and is sealed, and one click verifies that nothing was altered.
- **Human control**: approvals, joint sign-off, time limits, and Reject exits; nothing is ever approved automatically, and a process restart loses no state.
- **Honest issuance**: the issuance contract decides between three tiers, formal / degraded / withheld; when data is missing, the output is downgraded rather than forced through.

## Main features

**Orchestration and runs**

- Canvas: 18 node types, drag and connect; live node highlighting, streaming output, and a timeline track at run time → [Workflows](docs/en/workflows.md)
- Copilot: generate or rewrite a whole graph from one sentence; it looks up schemas on demand and checks its work before delivering → [Copilot](docs/en/ask-data.md#copilot)
- Human-in-the-loop: approvals, joint sign-off, time limits, Reject exits → [Human nodes](docs/en/workflows.md#human-nodes)
- Agent guardrails: repeated calls, spending, and context each have a guard; the step count is only a backstop → [Guardrails](docs/en/workflows.md#guardrails)
- Runs and resuming: failed runs continue from the checkpoint; per-node duration, token usage, and cost → [Runs](docs/en/workflows.md#runs)
- Publishing and evidence gates: published and governed levels, gates G1–G5, pre-publish check and fixes → [Publishing and versions](docs/en/workflows.md#publishing-and-versions)
- Tools and the capability registry: built-in tools, custom tools, MCP, approval levels, registered endpoints → [Tools](docs/en/tools.md)
- Knowledge base, memory, and skills: hybrid retrieval, memory across runs, reusable methodology → [Knowledge base](docs/en/workflows.md#knowledge-base-memory-and-skills)
- Multi-model access: Anthropic, OpenAI, OpenAI-compatible services, and Mock → [Model access](docs/en/configuration.md#model-access)
- Code sandbox: two isolation tiers, the OS sandbox (Seatbelt / bubblewrap) or microVM → [Code sandbox](docs/en/identity-and-security.md#code-sandbox)

**Data**

- Ask Data: ask in natural language, and the system builds a graph, runs it, reviews it, and gives a conclusion → [Ask Data](docs/en/ask-data.md)
- Data sources and spreadsheet import: read-only access to business databases, Excel / CSV turned into tables you can query with SQL, cross-database merges → [Data sources](docs/en/data-sources.md)
- Data catalog: grain, business keys, valid-record conditions, code values, relationships; drafting, review, profiling, and a change log → [Data catalog](docs/en/data-catalog.md)
- SQL checks: the catalog catches seven kinds of queries that compute wrong numbers → [SQL checks](docs/en/data-catalog.md#sql-checks)

**Evidence and issuance**

- Clickable evidence: numbers, table and field names, and verbatim knowledge-base quotes all open to their provenance → [Evidence and Issuance](docs/en/evidence.md)
- Three-tier issuance: caliber cards plus issuance contracts, checked by value or by citation → [Issuance contracts](docs/en/evidence.md#issuance-contracts-and-tiers)
- Sealing and exports: one-click seal verification; self-contained report exports and verifiable reports → [Exports](docs/en/evidence.md#exports)
- Review suite: double-blind recomputation, adversarial review, approval inquiry, the what-if sandbox, run comparison → [Review suite](docs/en/evidence.md#the-review-suite)

**Quality**

- Regression evaluation: test suites, background evaluations, accuracy with intervals, comparisons and A/B, scheduled evaluations → [Regression evaluation](docs/en/evaluation-and-ledger.md#regression-evaluation)
- Caliber ledger: each number in an answer gets a caliber label, with issuance by caliber, reconciliation, and recall → [Caliber ledger](docs/en/evaluation-and-ledger.md#caliber-ledger)

**Apps and integration**

- Apps: entry forms, views, tasks, and actions derived from a published version → [Apps](docs/en/apps.md)
- Writing to external systems: confirmation cards, idempotency keys, no automatic resend when the result is unknown → [Write to external systems](docs/en/apps.md#write-to-external-systems)
- External integration: inbound hooks, access keys, share links, embedding in portals → [External integration](docs/en/apps.md#external-integration)
- Schedules, event triggers, and notifications → [Schedules and notifications](docs/en/schedules.md)

**Operations and security**

- Identity and permissions: local accounts or OIDC, three roles plus app grants, masking boundaries → [Identity, Permissions, and Security](docs/en/identity-and-security.md)
- PostgreSQL and multiple processes: import, connection pools, PgBouncer, leases and takeover → [PostgreSQL and Scaling](docs/en/postgresql-and-scaling.md)

## Quick start

You need conda ([miniforge](https://conda-forge.org/miniforge/) is enough), Node 20+, and pnpm; Docker isn't needed.

```bash
conda env create -f environment.yml   # first time: create the agentlab environment and install dependencies
./scripts/dev.sh                      # after that: one command starts the frontend and backend
```

Open http://localhost:5273 . You can try everything without an API key: the built-in Mock provider runs the whole orchestration path end to end; to connect a real model, see [Model access](docs/en/configuration.md#model-access). The 9 built-in templates run as they are.

**Login is off by default**: anyone on the same network who can open the page can use every feature. Turn on login before multiple people use it; see [Login modes](docs/en/identity-and-security.md#login-modes). For more startup options, see [Development](docs/en/development.md#start).

## Deployment

- Without Docker: `./scripts/prod.sh` builds the frontend and has the backend serve the pages too; one process by default, at http://127.0.0.1:8000 .
- Offline environments: build a Docker offline bundle with `./scripts/package-docker.sh`, copy it to a machine that has only Docker Engine, and run `./deploy.sh`.

See [Deployment](docs/en/deployment.md) and [deploy/README.md](deploy/README.md) (written in Chinese). The app database defaults to SQLite with a single process; for multiple processes, switch to PostgreSQL (see [PostgreSQL and Scaling](docs/en/postgresql-and-scaling.md)). Read the [Upgrade Notes](docs/en/upgrade-notes.md) before upgrading.

## Documentation

| Document | What it covers |
|---|---|
| **Using and building** | |
| [Ask Data and Copilot](docs/en/ask-data.md) | What happens after you ask, output review, record retention, how Copilot builds graphs and looks things up |
| [Workflows](docs/en/workflows.md) | The canvas, node types, passing data, human nodes and rejection, agent guardrails, subworkflows, publishing and gates, runs and resuming |
| [Evidence and Issuance](docs/en/evidence.md) | Caliber cards, issuance contracts and tiers, citation markers, claim sentences, no data in the queried range, sealing and verification, report exports and verifiable reports, the review suite |
| [Data Sources and Spreadsheet Import](docs/en/data-sources.md) | Connecting databases, read-only guarantees, uploading spreadsheets and recipes, accumulating by period, cross-database merges, masked columns |
| [Data Catalog](docs/en/data-catalog.md) | What the catalog records, drafting and review, export and import, data profiling, SQL checks, introspection scope for large databases, schema and value changes |
| [Tools, MCP, and the Capability Registry](docs/en/tools.md) | Kinds of tools, approval levels, capability governance, limits on write capabilities, registered endpoints, network rules |
| [Apps](docs/en/apps.md) | Creating apps, views and stamps, processes and tasks, writing to external systems, form submissions, personal apps, external integration |
| [Evaluation and the Caliber Ledger](docs/en/evaluation-and-ledger.md) | Test suites and evaluation results, caliber labels per number, issuance by caliber, reconciliation, distilling, recall |
| [Schedules, Event Triggers, and Notifications](docs/en/schedules.md) | Schedules, event triggers, notification channels and email, subscribable events |
| **Deployment and operations** | |
| [Deployment](docs/en/deployment.md) | `prod.sh`, the Docker offline bundle, the data directory, backups, upgrades |
| [Configuration](docs/en/configuration.md) | Model access, the public URL, environment variables, UI settings, the sandbox backend and microVM |
| [PostgreSQL and Scaling](docs/en/postgresql-and-scaling.md) | Switching databases, importing, connection pools, PgBouncer, leases and takeover across processes, multiple machines, upgrades |
| [Identity, Permissions, and Security](docs/en/identity-and-security.md) | Login modes, roles and the permission table, masking boundaries, audit, the code sandbox, security boundaries |
| [Upgrade Notes](docs/en/upgrade-notes.md) | How each update affects users and operators, and what to do when upgrading |
| **Development** | |
| [Architecture and Design Decisions](docs/en/architecture.md) | The overall architecture, constrained dynamic orchestration, and the reasons behind key design decisions |
| [Development](docs/en/development.md) | Development environment, directory layout, dependencies and the lock file, tests, frontend checks, documentation checks |
| [FAQ](docs/en/faq.md) | What to do about common errors and symptoms |

## Development and testing

Before running backend tests, remove model keys from the environment; otherwise the tests might really call the API:

```bash
cd backend && env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
  python -m pytest -n auto
cd frontend && npx tsc -b
node scripts/check-all.mjs   # frontend checks; start ./scripts/dev.sh first
```

For tests on PostgreSQL, the 43 frontend checks, and the documentation checks, see [Development](docs/en/development.md).

## License

This project is **source-available** and is licensed under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0).

### Noncommercial Use

You may use, study, modify, experiment with, and develop upon this software for purposes permitted under the PolyForm Noncommercial License 1.0.0.

Typical permitted noncommercial uses include:

* personal research;
* learning and education;
* experimentation;
* evaluation and testing;
* hobby projects;
* noncommercial software development.

See the [`LICENSE`](./LICENSE) file for the applicable license terms.

### Commercial Use

**Commercial use is not permitted without prior written authorization from Yilun JIANG.**

If you intend to use this software in a commercial product, commercial service, SaaS offering, customer project, commercial deployment, or other commercial context, you must obtain a separate commercial license before doing so.

For commercial licensing, see [`COMMERCIAL-LICENSE.md`](./COMMERCIAL-LICENSE.md) or contact:

**Yilun JIANG**
**[me@thearchyhelios.com](mailto:me@thearchyhelios.com)**

### Copyright

Copyright (c) 2026 Yilun JIANG.

All rights reserved except as expressly granted under the applicable license.
