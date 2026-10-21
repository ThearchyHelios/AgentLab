English | [Simplified Chinese](../zh-CN/workflows.md)

# Workflows

This page covers building workflows on the canvas: how nodes connect, how data passes between them, the rules for human nodes and agents, how to publish, and how runs and resuming work. It is for builders. The user interface is currently available in Simplified Chinese only.

## The canvas

Drag nodes onto the canvas and connect them. During a run:

- Nodes highlight live, dots of light on the edges show where data is flowing, and streaming tokens appear inside the node card.
- The track along the bottom lays out each node's start, end, and degree of parallelism on one timeline. Live view and replay share it.
- The right column shows the run steps, read from the same event stream as the canvas.
- Multi-agent nodes expand a collaboration matrix inside the card: who is running, how many run in parallel, and how much time that saves.
- When a branch node finishes, it lights up the exit it took and dims the others. The edges actually traversed stay on the canvas: that is this run's execution path.

### What the motion means

Everything that moves on the canvas stands for something that is really happening:

| Motion | Meaning |
|---|---|
| An arc of light circling the border | The node is running |
| A scan line sweeping across | The node is producing output |
| A green sweep | The node just finished |
| A shake | The node just failed |
| Amber breathing | The node is waiting for a person |
| Dots of light on an edge | Data is flowing from one side to the other |

In the collaboration matrix, the roster is fixed and never reordered; members who have been dispatched light up and show their current task; bar lengths are normalized to the slowest member in the round, so the wall-clock time equals the longest bar; the footer shows how much time parallelism saved in total.

When motion is turned off at the system level (`prefers-reduced-motion`), the light effects are not drawn at all. A solid dot and the label "In progress" then show who is running.

### Automatic layout

- Layers follow the topology: parallel branches land in the same column, and nodes within a layer are ordered with a median heuristic to reduce crossings.
- A branch node's exits run top to bottom in their declared yes/no order.
- Loop back edges don't take part in layering; otherwise the loop body would be pushed to the left of the loop node.
- Each edge gets its own vertical lane: fan-out and converging edges don't stack on the same segment, and long edges that span layers route through gaps where there are no nodes (see [Architecture](architecture.md#edge-routing) for how).

### Built-in templates

There are 11 built-in templates, from Minimal Q&A to Anomaly Analysis (Traceable). They run as they are and also serve as executable documentation of how each kind of node is used. Templates 9, Traceable Weekly Report, and 11, Anomaly Analysis (Traceable), can be published at the governed level as they are: the first demonstrates caliber cards and issuance by citation; the second demonstrates the analysis card, which takes two queries, judges whether this week's change is within the usual fluctuation and which region it mainly came from, and leaves the report to cite only the verdict phrases (see [Analysis](analysis.md#analysis-cards)).

## Node types

| Category | Nodes | Description |
|---|---|---|
| Start/end | `input` `output` | Declare input fields / collect structured output |
| Model | `llm` `agent` `supervisor` `report` | Single call / loop with tools / multi-agent collaboration / report writer (writes only citation markers; the system fills in the numbers) |
| Execution | `tool` `code` `merge` | Call a tool directly / execute code in the sandbox / merge the results of several queries (aligned by key across databases) |
| Control | `branch` `loop` `subgraph` | Conditional or semantic branch / iteration and conditional loop / nested workflow |
| Context | `memory` `retrieve` `transform` | Long-term memory read/write / knowledge retrieval / data shaping |
| Gatekeeping | `human` `validate` `metrics` `analysis` | Human-in-the-loop / Schema validation and automatic rework / caliber card / analysis card (a fixed set of deterministic analyses on upstream query results) |

The `output` node validates structured output against a JSON Schema; when the output doesn't conform, the errors go back to the model for automatic rework. For `metrics` (caliber card) and `report` (report writer), see [Evidence and Issuance](evidence.md); for `analysis` (analysis card), see [Analysis](analysis.md#analysis-cards); for `merge` (merge query), see [Data Sources](data-sources.md#cross-database-merge).

## Passing data between nodes

Reference other nodes' output with template syntax:

```
{{ input.question }}        entry input
{{ vars.answer }}           variable written by some node's assign_to
{{ nodes.n1.text }}         output of the given node
{{ vars.items | json }}     filters: json / compact / upper / length / first …
```

### Conditions and expressions are not templates

Branch conditions, while-loop conditions, shaping expressions, caliber card metrics, and skip conditions are evaluated directly, so write variables bare: `vars.gate != 'ok'`, `len(vars.items) > 0`.

- An extra `{{ }}` wrapper leaves the meaning unambiguous, so it is read as `vars.gate` with just a hint.
- These are blocked at validation instead of failing when the step is reached: filters such as `| length` (write `len(x)` instead), `{'a', 'b'}` sets (use a list), unknown functions, `=` where `==` is meant, and `{{ }}` inside quotes (it isn't rendered, so the condition never holds).
- Validation, Copilot's self-check, and the runtime use the same parser, so "validation says it runs, but it fails when run" can't happen.
- The evaluator accepts only syntax on an AST allowlist; it doesn't use `eval`.

### Print code node results

`assign_to` receives stdout. A bare expression on the script's last line produces no output (showing it automatically is notebook behavior), so the variable will be empty.

- To hand an object downstream, write `print(json.dumps(result))`. When stdout is JSON, it is parsed into an object automatically.
- Validation blocks Python code that sets `assign_to` but has no output anywhere.
- Code runs in the sandbox. Each code node can choose its own isolation tier: strict is microVM, fast is the OS sandbox (see [Identity, Permissions, and Security](identity-and-security.md#code-sandbox)).
- The bubblewrap sandbox doesn't inherit the backend's environment variables, so code can't read variables such as model API keys or database addresses. Pass any values the code needs explicitly.
- `evidence_role` distinguishes `source` (data retrieval) from `compute` (computation, the default). Numbers computed in the sandbox can't be cited directly by a report; they must go through a caliber card (see [Evidence and Issuance](evidence.md#the-data-retrieval-chain)).

## Parallel branches and joins

- Several edges out of one node form a fan-out, and the branches run in parallel.
- When several edges flow into one node, it waits for them to converge: the join node waits until every other pending task in the graph has finished, then runs once. It isn't triggered twice when one branch is long and the other short.
- A join after an either-or branch doesn't wait for the path that wasn't taken.
- The cost: a join node also waits for branches that have nothing to do with it but happen to still be running.

## Human nodes

A `human` node pauses execution and saves its state to disk; once a person decides, execution continues along the matching exit.

![Human approval flow](../images/graph-human.png)

- A person can approve, reject, or edit the draft, and execution then continues from the checkpoint. A process restart loses no state.
- The step can be assigned to a position, to the initiator, or to people computed by an expression. A position is a name written in the graph and bound to people or groups in the app (see [Apps](apps.md#processes-and-tasks)).
- Joint sign-off: any one person agrees, everyone agrees (one veto rejects), or at least m people agree.
- A time limit can be set; on timeout the step can send a reminder, escalate, or be treated as rejected.
- An upstream Agent can draft the form.
- Nothing is ever approved automatically. Only an explicit approval counts (`true`, `approve`, or the Chinese word for "agree"); any reply that can't be understood is treated as a rejection.

Besides approvals, human steps include supplementary input, draft editing, action confirmation, code and tool approvals, and double-blind adjudication.

### Rejection

A rejection is a human decision, not a failure:

- The run is recorded as "Cancelled · Rejected". It doesn't count as a failure, isn't retried, and can't be continued. Notifications go out as "Instance finished", and schedules push "Not issued (rejected)".
- Gates configured with retry on failure or Continue on error don't ask again after a rejection and don't carry on downstream.
- For the rejected step, the `node.failed` event carries `reason: "rejected"` (the same field as on `run.cancelled`). The canvas, track, timeline, and Ask Data progress all show it as "Rejected". External systems that read events can tell rejections apart by `reason`.

Approvals, supplementary input, draft editing, action confirmations, code and tool approvals, and double-blind adjudication can each take an extra Reject exit:

- After a rejection (including a rejection on timeout), the step ends normally and execution continues along the Reject exit. The run ends along its path, and `run.finished` carries `rejected_via`.
- When an action confirmation takes the Reject exit, nothing is sent out.
- The instance page and notifications say "Rejected, routed to X". Tasks, notifications, and webhooks state plainly where a rejection went ("Rejected, routed to X", "This path ends here"); inside a subworkflow they say "Subworkflow › x".
- Without a Reject exit, the run is still recorded as "Cancelled · Rejected".
- The pre-publish check blocks reject paths that can never reach the end.

## Agent nodes

An `agent` node is a model loop with tools. An allowlist on the node decides which tools it can use (see [Tools](tools.md)). An agent with `output_schema` can turn on `cite_fields`, so each field records which cell of which query it came from (see [Evidence and Issuance](evidence.md#the-data-retrieval-chain)). An agent that queries data can also be given the analysis tools: breaking down changes and judging anomalies and trends is computed by the platform with fixed methods and thresholds, the system writes the verdict phrases, and reports cite them (see [Analysis tools for agents](analysis.md#analysis-tools-for-agents)).

### Guardrails

Guardrails replace a fixed step count and guard against four things:

- **Loops that don't converge**: the same tool with the same arguments isn't executed a second time; the previous result is handed back instead. After 3 consecutive steps with no new information, the agent wraps up.
- **Spending that keeps growing**: each node has a token budget (default 2 million) and a spending budget (unlimited by default); both can be set to unlimited in Settings.
- **Context filling up**: at 60% of the context window, early tool results are compressed; at 85%, the agent wraps up.
- **Genuinely runaway agents**: the step count remains only as a backstop, default 100, adjustable in Settings and capped by `AGENTLAB_MAX_AGENT_STEPS`.

The model gets one reminder before it reaches any of these limits. The limits are snapshotted when the run starts, and resuming or continuing uses the same snapshot: when a run resumes after an approval, the node replays in full, and if the limits had changed, the point where the loop stops would no longer match the checkpoint.

When downstream structured validation fails, the error names the upstream agent that wrapped up early. When an agent wraps up early, the timeline also shows a hint with the reason.

Tool approvals inside an agent aren't billed twice: when the node replays after an approval, earlier model calls and tool executions are read from the checkpoint (see [Architecture](architecture.md#why-langgraph)).

### Multi-agent collaboration

In a `supervisor` node, a coordinator assigns tasks to several specialists as the work progresses. This is an exploration-layer capability and is disabled for governed-level publishing.

![Multi-agent collaboration](../images/graph-supervisor.png)

Members of a collaboration team can't stop to wait for a person: calls that need confirmation aren't executed, and the member is told to try another approach.

## Subworkflows

A `subgraph` node nests another workflow.

- The version a subworkflow pins must be published and not retired.
- Every step inside the subworkflow goes into the same run's event stream: the timeline and the Records page show entries like "Subworkflow › that step", and the seal also covers the numbers the subworkflow queried.
- Fields at the subworkflow's exit return to the parent graph with their provenance, so the parent's caliber cards and reports can trace all the way to the cell inside the subworkflow, up to 3 levels deep. If something inside the subworkflow can't be traced back to raw data, the parent graph is downgraded just the same.
- Reports inside a subworkflow are recorded under prefixed node ids; the parent graph recognizes only its own reports.
- When a subworkflow runs several times inside a loop, the steps of each execution continue the numbering. Idempotency keys for writes to external systems use the global id within the parent run, so every iteration's writes are sent as usual.

## Knowledge base, memory, and skills

- **Knowledge base**: hybrid vector + BM25 retrieval, an inverted index, and optional model reranking. It parses PDF, Word, PowerPoint, HTML, and Markdown. Each score is normalized and then weighted; the weights are adjustable in the UI, and hits show the semantic score and the keyword score separately. For why retrieval is hybrid and measured results, see [Architecture](architecture.md#why-retrieval-is-hybrid).
- **Embedding model**: the default is local feature hashing, which needs no network, no model download, and no configuration, but generalizes poorly. You can connect any OpenAI-compatible endpoint (LM Studio, Ollama, vLLM, TEI). The dimension is found by probing, not inferred from the model name.
- **Long-term memory**: read and write across runs, with automatic deduplication. Copilot records facts worth remembering while it builds graphs.
- **Skills**: methodology packaged as reusable units, mounted on nodes and injected into the system prompt.

Excel and CSV files uploaded as data sources become tables you can query with SQL; put into the knowledge base, they could only be chunked and retrieved (see [Data Sources](data-sources.md#upload-spreadsheets)).

## Publishing and versions

### Publishing levels

A workflow can be published at two levels: published and governed.

- What you publish is an immutable version, pinned by graph hash. Editing the canvas after publishing doesn't affect published versions.
- Publishing at the published level: builders and above. Publishing at the governed level, demoting a governed workflow to published, and retiring versions: admins only.
- Versions pinned by subworkflows and caliber cards must be published and not retired.
- Governed-level publishing must pass the governance check: `supervisor` is disabled (fully dynamic collaboration belongs to the exploration layer); method cards must pin a version; agents may not turn off all approvals; `output` must have an issuance contract; and there are evidence gates G1–G5.

### Evidence gates

On top of the governance check, the governed level adds evidence gates. They look only at the graph and its configuration, and make "every number opens to its provenance" hold by construction:

- **G1** At an exit with a contract, the text in output fields may come only from report-writer nodes.
- **G2** Model-written text doesn't bypass the report-writer node to flow straight into the exit.
- **G3** The report-writer node sets `numbers: strict`, `on_violation: fail`, `claims: require_citation` (or `judge`, with a budget), and `analysis_claims: strict` (the analysis claim check set to strict; see [Analysis claim check](analysis.md#analysis-claim-check)).
- **G4** Caliber card inputs don't come from sandbox code in the compute role.
- **G5** Agents that feed a caliber card have `output_schema` set and `cite_fields` turned on.

At the governed level these are errors that block publishing; at the published level they are only warnings. Versions already published keep running as usual, and the next publish is checked against the new rules.

### Pre-publish check and fixes

- Opening the publish dialog first runs the pre-publish check (`POST /api/workflows/{id}/publish-check`, with the same criteria as the actual publish). The issues panel shows the results at other times too.
- Fixes (`/autofix`) come in three kinds: a one-click fix when there is only one right answer; options when a person has to decide, without choosing for them; and structural problems, handed to Copilot.
- Every fix is previewed first. Nothing is saved or published automatically.
- Changes that lower requirements are always refused: deleting nodes, deleting contracts, relaxing approvals, turning off strict, changing the level. Copilot's proposals must pass this check too. After a fix is applied, the check runs again; the errors must decrease, and no new ones may appear.
- The one-click upgrade of an old workflow to a traceable structure (`POST /api/copilot/upgrade-evidence`) goes through the same check (see [Evidence and Issuance](evidence.md#old-runs-and-the-one-click-upgrade)).
- The pre-publish check also blocks reject paths that can never reach the end, and Agents or multi-agent collaborations whose tool list includes a write capability. Instances paused at an approval in the middle of an Agent can't continue after an upgrade, so publishing warns with `process.wait_mid_agent`.

### From exploration to templates

- Canvas trial runs are automatically marked as exploratory runs, and their results stay out of formal archives.
- Extract Template turns a path that worked into a draft: it keeps only the nodes that actually ran and prunes the branches that weren't taken. Publish it after human review.
- Exploration question clustering suggests which follow-up questions should become a section of a template.
- The tool allowlist decay report (`/api/governance/tool-usage`) lists tools that are authorized but never used, as grounds for narrowing permissions.

## Runs

### Formal and exploratory runs

- Formal runs start only from published, non-retired versions and go into formal archives. Canvas trial runs are exploratory runs.
- Every run records its origin (Ask Data, canvas, schedule, app entry, external system, and so on) in a `run.origin` event, inside the sealed scope. Runs started with an access key record which key.
- When writing to external systems, formal runs use the production environment and exploratory runs use the test environment (see [Apps](apps.md#write-to-external-systems)).

### Resume from a checkpoint

- A failed run can continue from the failed node; completed nodes aren't executed again.
- An exploratory run can continue after its node configuration is changed, but the topology must stay the same. A formal run is pinned to its published version and can't continue after a configuration change.
- Continuing a run doesn't change its initiator.
- After a service restart (including the hot reload triggered by editing backend code during development), runs in progress show as interrupted with their checkpoints intact. Click Resume on the run details page: finished nodes don't run again, and later human approvals still stop and wait for a person.

### Time limits and step budgets

- A single execution (the stretch between two human interventions) is limited to 600 seconds by default (`AGENTLAB_MAX_RUN_SECONDS`).
- A single model call times out after 300 seconds without output by default (`AGENTLAB_MODEL_TIMEOUT_SECONDS`).
- The graph's step limit is `AGENTLAB_MAX_GRAPH_STEPS` (default 200). A loop gets its own step budget from the iteration count declared in its `max_iterations`, and that budget isn't bound by this limit.
- The current values are shown under Settings → Runtime Environment.

### Run records and cost

- Every event is stored and can be replayed.
- Each node records its duration, token usage, and cost at list prices. Cancelled and timed-out model calls are also recorded as calls, marked "Incomplete".
- Evaluations, caliber reconciliations, and caliber recalls are runs the system starts on someone's behalf. Their evidence can be inspected as usual, but they are left out of usage statistics (exploration question clustering, tool allowlists, catalog usage counts). The Records page doesn't list them by default and says how many it left out.
- The run details page can verify the seal with one click (see [Evidence and Issuance](evidence.md#sealing-and-verification)).
