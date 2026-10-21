English | [Simplified Chinese](../zh-CN/ask-data.md)

# Ask Data and Copilot

This page covers the Ask Data page and Copilot: what happens after you ask a question, how to check the numbers in an answer, and how Copilot looks at tables and checks its own work. Copilot is the model that generates and edits workflows from natural language. It is for people who use Ask Data and for builders. The user interface is currently available in Simplified Chinese only.

## Ask a question

1. Open the Ask Data page and ask in natural language.
2. The system connects to data sources on its own, generates a workflow, runs it, and gives a conclusion.
3. When the answer contains numbers, it goes out through a report-writer node, and each segment opens to its provenance (see [Evidence and Issuance](evidence.md)).

Conversations are saved and can be shared by link.

Without a real model, the built-in Mock provider still runs the whole pipeline, but it writes no citation markers, so every number in the report is marked "No evidence". To get clickable provenance, connect a real model under Settings → Model Access (see [Configuration](configuration.md#model-access)).

## What happens in a turn

A turn covers: Copilot builds the graph, the run starts, the result is reviewed, the run is redone if the review asks for it, and the result is saved. All of this runs in a background job on the server; the page only watches.

- After a disconnect, a refresh, or closing and reopening the page, it picks up where it left off by sequence number. Stopping goes through the `/api/chat/turns` API.
- On a service restart, a turn that is still building its graph is marked interrupted and can be retried with Retry This Turn. A turn that has already started its run keeps waiting for the run, then reviews and wraps up as usual.
- When a turn is taken over after a service restart, it is checked again against the account's current role. If the account was disabled or lost permission, the turn is marked interrupted.
- A conversation runs only one turn at a time. A turn waiting for a person at an approval doesn't count.
- When Copilot returns only thinking and no operations, the turn is retried once automatically.
- The cost of each turn includes the model calls for Copilot's graph building, table selection, and review.

## Output review

After a run ends, rules scan the event stream first, and a model review runs only when they find an anomaly. The review may restructure the answer, but it **must not introduce numbers that aren't in the original answer**.

- Ask Data for users (the user role) is reviewed too.
- When a step fails inside a subworkflow, automatic review reports it only once.
- Continue Run in Ask Data is limited to builders and above.

## Calibers in an answer

A caliber is the agreed definition of how a metric is fetched and computed. It lives in a caliber card, with a name and a version. After an answer is sealed, the system compares every number in it with the published calibers in the background and labels each number with a caliber; each turn gets a one-line summary. When the whole answer is equivalent to a governed caliber, the system automatically starts a formal run by that caliber. See [Evaluation and the Caliber Ledger](evaluation-and-ledger.md#caliber-ledger).

## From an answer to a workflow or an app

- Open in Canvas puts this turn's graph on the canvas. The new workflow records which turn it came from.
- Make App first extracts the run into a workflow (keeping only the steps that actually ran). Publish it on the canvas, then continue from the publish-success notice (see [Apps](apps.md#create-an-app)).
- Runs started from Ask Data carry their conversation and turn (`origin_ref`).

## Who can use it

Ask Data is available to builders and admins by default. An admin can enable it for users under Settings → Identity. Users can only start exploratory runs, and the nodes and tools they can use are allowlisted; see [Identity, Permissions, and Security](identity-and-security.md#ask-data-for-users). Conversations are isolated by the person who created them.

## How long records are kept

- The event log of each turn (the `chat_turn_events` table in `agentlab.db`: Copilot's operations, phases, runs, reviews, final outcome) only serves turns in progress, so the page can replay it before switching to live.
- Turns that ended (succeeded, failed, cancelled, interrupted) more than 14 days ago are deleted as whole turns by the scheduler loop, which cleans up every hour.
- Reopening an ended turn reads the row saved in the conversation (answer, graph, review, usage, number of attempts), which the cleanup doesn't touch.
- Copilot's heartbeats while it waits for the model are sent only on the live stream and never stored.

## Copilot

Copilot generates or rewrites a whole graph from one sentence and lays it out automatically. The canvas, Ask Data, the `/generate` API, pre-publish fixes, the evidence upgrade, and app polishing all use the same generation process: draft → look → keep writing → self-check fixes.

- When it edits a graph, it outputs only the change operations instead of rewriting the whole graph.
- Before delivering, it checks its work with the same rules the runtime uses. Problems that would make the run fail go back to it to fix first; if they can't be fixed, nothing runs automatically.
- While building graphs, it records facts worth remembering in long-term memory.

### Looking on demand

Copilot's system prompt has only a one-line overview per data source (number of tables, how many have catalog entries, whether there is masking). The canvas and Ask Data also list capabilities from the capability registry; Ask Data lists only read-only ones. When Copilot needs a table schema, capability parameters, or a workflow version, it "looks" for itself, and the server returns fields, catalog entries, and join conditions in one go. Small talk or a prompt edit calls the model only once.

- To look, Copilot outputs one line: `{"op":"look","need":…,"targets":[{"kind":"table","source":…,"name":…}]}`. As soon as the server reads it, it stops that output, performs the look, and returns the result as the next message. Copilot then carries on writing without repeating operations it already applied.
- Three kinds of objects can be looked at: tables; capabilities (name, parameters, returned fields, read/write, whether confirmation is required; no addresses or credentials); and workflow versions (only published, non-retired ones are listed).
- Named targets are fetched directly. When only a need is given: if there are no more than 12 candidate tables, all of them are returned; otherwise tables are selected by the need (thinking off, 20-second limit), intermediate tables within two hops along relationships are added, and one look returns at most 16 tables and 12,000 characters.
- When nothing can be selected, or the selection comes out empty, the server says so plainly and hands back the list of tables for Copilot to name.
- Each turn allows at most 3 looks, plus 1 more for self-check fixes.
- Looks done by the system: pre-publish fixes and the evidence upgrade look up, by name, the tables used by the SQL being fixed; app polishing first looks at the workflow version the app pins.
- Every look emits a look event (what was looked at, how it was found, what wasn't found, catalog version, elapsed time), stored in the turn's `meta.looks`. The next turn says "Looked at in the previous turn: …", and follow-up questions look things up by name directly.

### Ask before acting

When Copilot must clarify something before it can act, it outputs `{"op":"ask","question":…}` and the turn ends there. Changes already applied are delivered as usual but not run automatically.
