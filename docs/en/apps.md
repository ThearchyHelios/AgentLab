English | [Simplified Chinese](../zh-CN/apps.md)

# Apps

This page covers turning a published workflow into an app: creating and publishing it, views and stamps, processes and tasks, writing to external systems, and external integration (inbound hooks, access keys, share links). It is for builders, admins, and people who use apps. The user interface is currently available in Simplified Chinese only.

## What an app is

Once a workflow is published, the people who read its numbers and the people who get work done use it through Apps.

- An app is not a new engine, nor an embedded low-code platform. Every number comes from a sealed formal run of the pinned version, and every start is a formal run of the pinned version.
- The interface layer holds no logic: bindings only take existing values; visibility conditions can only compare, combine booleans, read values, and use `in`, with no arithmetic. All evaluation happens on the server, and invisible components and masked values never leave the server.
- Apps have three grant levels of their own: Use, View all instances, and Manage (see [Identity, Permissions, and Security](identity-and-security.md#roles-and-grants)).

## Create an app

1. Publish the workflow on the canvas and click Make App in the publish-success notice. You can also click Make App on an Ask Data answer: this first extracts the run into a workflow (keeping only the steps that actually ran); publish it on the canvas, then continue from the publish-success notice.
2. The system derives an app draft from this version deterministically, without calling a model:
   - entry fields become forms;
   - caliber cards become metric blocks, reports become report blocks, and data-fetch nodes become tables;
   - human nodes become tasks, with positions left empty for someone to bind;
   - Call Tool nodes that write to external systems become action confirmations and action status;
   - views prefer to bind to existing schedules.
3. Adjust it in the app editor: the page tree and properties panel, a preview based on the draft (no run is started), and checks. The Copilot on the right can only polish layout, copy, and controls; it can't change the pinned version, the entry mappings, or the forms' data constraints.
4. Bind positions to people or groups (required for process apps), set grants, inbound hooks, and notification subscriptions, then publish.

When the pinned workflow version is newer than the one in the previous app version, publishing requires choosing a disposition for it: Recompute with the new caliber, Both versions side by side, or Before and after not comparable.

## Kinds of apps

| Type | Capabilities used |
|---|---|
| Data dashboard | Views read a schedule's latest sealed formal run: stamp, metric blocks, multi-period trends (drawn according to the version-bump disposition), tables, line / bar charts, reports |
| Parameterized report | An entry form starts a formal run, and you see the result on the instance page; or a parameter form on the page runs on demand and caches the result |
| Approval process | Tasks assigned to a position / the initiator / people computed by an expression, with any one / all / at least m people, time limits and escalation, an inbox, and Started by Me |
| Write back to external systems | Call Tool nodes write to registered endpoints; the confirmation card shows a masked preview and a content hash; unknown results go to a person for verification |
| External system trigger / callback | Signed inbound hooks: start an instance, or call back the Wait for External System step |
| Hybrid | Start a handling process from a dashboard, and finally write back to the system |

## Views and stamps

Every view carries a stamp that states:

- as of when (including the reporting period and the data version);
- which caliber version (workflow name vN · first 8 characters of the hash, plus the caliber card's caliber name and version);
- which issuance tier;
- where the data came from (schedule "…" run n / on-demand run / this instance).

When the schedule has already moved on to a new version while the app is still pinned to the old one, the stamp says so plainly.

When a view takes "the latest sealed run", it counts only formal runs started by schedules, app entries, on-demand views, the Formal Run button on the canvas or version page, inbound hooks, and access keys. Runs from issuance by caliber or from backfills never replace the numbers on a dashboard.

On-demand views:

- run only when you click Refresh, by default;
- run only once at a time per set of parameters within a process;
- must set a freshness period when they query a live database;
- have a daily cap per app (the setting `apps.on_demand_daily_limit`, default 200).

## Processes and tasks

- Each interrupt corresponds to one approval (for the engine) and one task (for people). Everyone's decisions are aggregated by the task service, and the run resumes once, only when they're all in.
- The graph names only positions; positions are bound to people or groups in the app. Runs without an app (canvas, schedules) are assigned to anyone.
- The old APIs `/api/approvals/{id}/decide` and `/api/runs/{id}/resume` still work.
- The scheduler loop checks time limits every 15 seconds. When the scheduler is off (`AGENTLAB_SCHEDULER=0`), there are no reminders, no escalations, and no automatic verification.
- With login on, people pickers in app entry forms and task forms are checked against a candidate pool (by default, the people who can use the app); a name outside the pool gets a 422 (`principal`).
- For how rejections and Reject exits work, see [Workflows](workflows.md#rejection).

## Write to external systems

Write capabilities can only be placed in Call Tool nodes.

- An effect record is written first, and then the request is sent. Within one execution, the idempotency key for the n-th send is `{action_key}:{n}`.
- The confirmation card shows a masked preview and a content hash. The hash is recomputed and compared before sending; if anything upstream changed after confirmation, sending is refused.
- When the result is unknown, the run is suspended and not sealed. If a query capability is registered, the result is verified automatically by idempotency key; if not, a verification task is opened (took effect, enter the external order number / didn't take effect, send again / didn't take effect, give up).
- A write whose result is unknown is never resent automatically.
- Formal runs write to the production environment; exploratory runs write to the test environment, or do a dry run without sending anything when there is no test environment.
- Runs with effect records can't be deleted.

For registering endpoints, see [Tools](tools.md#registered-endpoints). For acceptance testing, a fake external system can simulate the different outcomes (see [Development](development.md#a-fake-external-system-for-acceptance-tests)).

## Form submissions and attachments

Each form submission is one row in a record table managed by the platform (`data/records/`; one SQLite file per table version, append-only).

- Writes go only through the platform capability `records_append` of a Call Tool node, and one submission lands as exactly one row.
- A correction appends a new row that points at the original row; the original row is not changed. The correcting row still records the original submitter (who can still see it), and the person who made the correction is recorded in the action record and the audit log.
- Dashboards query with `db_query__<record_table>`. A run sees only the records that existed when it started.
- Users see, row by row, only what they submitted themselves (the Record List component); summaries in formal runs cover everything.
- When a form only adds optional fields, the columns are added within the same version; changing a type, deleting a field, or adding a required field produces a new version, and old records are kept as they are. When a dashboard queries the new version, old versions are read-only and merged into the same `records` table (fields an old version lacks, or whose type no longer matches, are null), and every version is fixed to the records as of the run's start.
- Instances started before the new version was published and still in progress finish writing to the old version; instances started afterwards write only to the new one.
- Writes to record tables are serialized site-wide (with a lock in the database when running multiple processes). They are sized for manual data entry, not for high-frequency writes many times per second.
- Attachments in forms are uploaded first (`data/files/`, stored by content hash, read-only) and bound to the run on submission. Only people who can read that run or that record row can download them. Uploads not submitted within 24 hours are cleaned up automatically.

## Backfill with the new caliber

When the pinned version is bumped and the chosen disposition is Recompute with the new caliber, a manager can click Backfill history with the new caliber:

- It takes the old version's inputs from the most recent few periods on the same origin and starts a formal run with the new version for each; the origin is recorded as a backfill with the new caliber.
- Inputs that were already computed are skipped.
- Workflows with external side effects (processes, human nodes, writes to external systems) aren't backfilled.

## Process instances across upgrades

Process instances may wait for days, so upgrading in the meantime is normal.

- Instances paused at a human node, or at the start of a write node waiting for confirmation, can all continue after an upgrade.
- Writes with unknown results continue after verification and aren't resent.
- Instances paused at an approval in the middle of an Agent can't continue after an upgrade. That is why publishing warns with `process.wait_mid_agent`, and why an app that references such a workflow as a process is blocked outright.

## Personal apps

Click Save as My App on a blueprint to make a personal app that only its owner can see.

- Users can create them only after an admin enables this in the Apps section of Settings → Preferences.
- The graph of a personal app uses the same allowlist as Ask Data for users. The data-fetch nodes of templates such as Periodic Dashboard and Exception Handling (list only) are simply Call Tool nodes that query data sources.
- Each refresh is an exploratory run: query results are masked at the tool boundary first, data sources are always read-only, and refreshes share a per-person daily cap with on-demand views.
- Only the owner can read refreshes and trial runs of personal blueprints: admins and builders can't see these runs, evidence, artifacts, or attachments either, and they aren't listed in run lists.
- In the app list, admins see only the name, owner, and created and updated times, and can archive the app; they can't see pages or blueprints.
- Personal apps have no entry form, and refreshes carry no attachments. If attachments must be submitted with a form, use Submit for Review so a builder can turn it into a regular app.

Shared blueprints can be read, edited, trial-run, and used to create publish plans: admins can do this for all of them; builders, for blueprints they created and blueprints that come from apps they manage (see the [permission table](identity-and-security.md#permission-table)).

## External integration

### Inbound hooks

- Declare hooks by name in the spec: start an instance, or call back a task (the Wait for External System step).
- Generate the secret in the app editor; it is shown only once.
- The external system sends a signed request to `/api/hooks/{hook_id}` with `X-AgentLab-Timestamp` and `X-AgentLab-Signature: sha256=HMAC-SHA256(<secret>, "<timestamp>." + <request_body>)`. The time window is 5 minutes, and `Idempotency-Key` is required.
- What a hook does follows the declaration in the app's currently published version. Requests to hooks that aren't published or have been disabled are rejected.

### Access keys

External systems (portals, OA, BI) call the API with access keys. Keys take effect only when login is on.

**Who can hold one**

- Service accounts: created by an admin under Settings → Access Keys, starting with `svc-`; they have no password, can't log in, and can't be assigned tasks.
- Personal access keys: an admin must turn on a switch (off by default), and then people create their own on the same page.
- A key is shown only once, when it is created or rotated, and the database stores only its hash. An expiry is required: at most 365 days, default 90 days.
- On rotation, the old key expires after 24 hours; revocation takes effect immediately; keys stop working once their owner is disabled.

**What a key can do**

- It is always evaluated with the user role's permissions: it sees only apps granted to the owner (or to their groups, or to everyone), and the data it sees is always masked, even for an admin's key.
- Permissions follow an API allowlist: read apps and views, start entries, refresh views, read tasks, reply to tasks, read reports. A new key has only the read permissions ticked by default, and APIs outside the allowlist always return 403.
- Starting entries and refreshing views require an `Idempotency-Key`, so an external system retrying after a timeout won't start an extra run.
- Replying to tasks is only for personal keys, and only for approvals, supplementary input, and draft editing.
- Each key is limited to 120 requests per minute.

**Audit trail**

- Creating, revoking, rotating, and toggling are all written to the audit log; every request is logged and kept for 90 days.
- Runs started with a key record which key in `run.origin`.
- Access keys get a reminder 7 days and again 1 day before they expire (when login is on).

```bash
curl -H "Authorization: Bearer agl_…" https://agentlab.example/api/auth/me
curl -H "Authorization: Bearer agl_…" -H "Idempotency-Key: order-20261016-001" \
     -H "Content-Type: application/json" -d '{"input": {…}}' \
     https://agentlab.example/api/apps/<app_id>/entries/<entry_id>
```

### View data API

`GET /api/apps/{app_id}/views/{view_id}/data` returns the stamp, the manifest hash, and the raw values of the caliber card metrics (masked ones are `null`). You can check the manifest hash against the verifiable report. The full API list and error codes are under Settings → Access Keys → API Reference.

### Share links

To show a dashboard to people who don't have an account, use a share link.

1. An admin turns on Allow creating share links (off by default) in the Apps section of Settings → Preferences.
2. Someone who can manage the app creates the share link under Positions & Grants in the app editor.

- **What it shows**: only views fed by schedules (the latest sealed formal run). Choose Always latest or Pin this period (the period that was current when the share was created). On-demand views, instances, and entry forms aren't shown, and opening the link never starts a run. Process components and report bodies are replaced with explanation blocks; the body and provenance are in Download report.
- **Link**: `/s#<key>`. The key comes after `#`, so it never enters server logs and isn't sent out in the Referer header; the database stores only its hash. An expiry is required, at most 90 days, and links can be revoked. When a link is revoked or expired, or the app is no longer published, opening it returns 410 with the reason stated. Once an admin turns the switch off, existing share links stop opening too.
- **Audit trail**: creating and revoking are written to the audit log; opens are only counted. Access keys can't call the share API.

### Embedding in a portal

- By default, pages can only be embedded by the same site: `frame-ancestors 'self'`, plus `X-Frame-Options: SAMEORIGIN`.
- A same-site portal (under the same registrable domain) that wants to embed the whole site adds the portal's address to `AGENTLAB_FRAME_ANCESTORS`; login works as usual.
- Embedding the whole site in a cross-site portal isn't supported (browsers don't send the session cookie into a cross-site iframe); use share links instead.
- Which sites may embed share pages is set by `AGENTLAB_EMBED_ORIGINS`. A share created without ticking Allow embedding in other sites shows no content inside an iframe.

### Error codes

Errors from the APIs integrators use (apps, tasks, artifacts, role denials) always carry a machine-readable `code`; `detail` is human-readable wording and may change.
