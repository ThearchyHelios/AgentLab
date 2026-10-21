English | [Simplified Chinese](../zh-CN/tools.md)

# Tools, MCP, and the Capability Registry

This page covers the tools workflows can call (built-in tools, custom tools, and MCP), their approval levels, how admins govern them in the capability registry, and how to register endpoints for external systems. It is for builders and admins. The user interface is currently available in Simplified Chinese only.

## Kinds of tools

- **Built-in tools**: 15 of them.
- **Custom tools**: an HTTP template, or Python in the sandbox.
- **MCP**: connect MCP servers (stdio or http).
- **Data source tools**: each data source's query tool, `db_query__<source>`, plus `db_schema__` for looking up schemas at run time (see [Data Sources](data-sources.md)).

Who can configure what: MCP stdio, endpoints and credentials, and capability governance can be changed only by admins; builders can read them, and users can't see them (see the [permission table](identity-and-security.md#permission-table)).

## Approval levels

The platform can't tell what MCP and custom tools do, so by default every call to them waits for approval. There are three approval levels: `ask`, `gated`, and `always`.

- On the tools page, each tool can be switched to Always allow · gated: every call first has its arguments reviewed by a gating model, and anything uncertain, erroring, or timing out goes back to a person.
- A tool can also be switched to Always allow. Always allow on an approval card is the same as Always allow · gated.
- The level is snapshotted when the run starts, and resuming or continuing uses the same snapshot.
- Formal runs ignore the level and always wait for approval; the exception is capabilities declared read-only with approval set to no approval in the capability registry (see below).

## Capability registry

Tools → Capabilities registers built-in tools, data sources, custom tools, and MCP tools in one place, and each entry's name is the tool name used in graphs.

- It syncs automatically at startup and whenever an object is created, updated, or deleted. When the source is deleted, the entry is marked "Missing" rather than deleted.
- Admins govern each entry: effect (read-only / write / depends on arguments), approval (follow trust settings / always confirm / no approval), whether unattended writes are allowed, where the returned table is located, field semantics (shaped like the data catalog's items, with inferred items going to Pending review), masked fields, credential mode, and query capability.
- Every change increments the version number. A formal run pins the version when it starts, publishing records the version, and later changes only trigger a reminder.

### No approval in formal runs

Capabilities declared read-only with approval set to no approval execute directly in formal runs, so Agents with MCP or custom tools no longer stop halfway to wait for approval. Write statements on a writable data source don't skip approval because of a declaration.

### Writes go only through Call Tool nodes

Write capabilities can only be placed in Call Tool nodes:

- When the tool list of an Agent or a multi-agent collaboration includes a write capability, the pre-publish check reports an error at every level, and the runtime refuses it too.
- Trial runs on the tools page and executions from the tool library always do a dry run for write capabilities: nothing is sent, and the method, URL, header names, and body that would have been sent are returned.
- When trial-running an unsaved draft, the request is assembled from the draft's configuration, and read vs. write is decided by whichever is stricter of the saved governance and the draft's method.
- Trial runs and endpoint test connections report success or failure by HTTP status: only 2xx counts as success, and when the connection works but returns 404 or 401, the status code is reported as it is.
- A query capability (used to verify write results) can only be a read-only capability on the same endpoint, bound to that endpoint.

For the full rules on writing to external systems, see [Apps](apps.md#write-to-external-systems).

## Registered endpoints

Admins register external systems' addresses and auth headers for two environments, test and production. Auth headers are stored encrypted, and the UI only ever shows them masked.

- Only registered endpoints with Allow internal network access turned on can reach the internal network. Metadata addresses and high-risk ports are still blocked.
- An HTTP tool bound to an endpoint fills in only the method, path template, query parameters, and body; path parameters are encoded segment by segment when substituted. The model can't reach arbitrary addresses, and redirects aren't followed.
- Formal runs use the production environment. In exploratory runs, writes use the test environment (a dry run if there isn't one), and reads use production.
- When the credential mode is Service credential + identity assertion, requests additionally carry `X-AgentLab-Assertion`: an EdDSA JWT valid for 5 minutes, where `aud` is the endpoint name, `sub` is the run's initiator, and the person who confirmed the action is in `act`. The public key is at `/api/auth/jwks`.
- An http MCP server that points at the local machine or the internal network must first be registered by an admin under Tools → Endpoints.

### Endpoint reads can serve as evidence

Once a read-only, endpoint-bound tool declares a table (where the rows are and where each column is), its results land as endpoint snapshots shaped like query snapshots:

- reports number them Q1… as usual, and cells open to their provenance;
- run comparison compares them as usual, by request;
- masking applies by column name.

Python tools and MCP are not included.

## Network and credentials

- HTTP tools resolve the domain and then check every IP, blocking internal, loopback, and cloud metadata addresses. They don't follow redirects; each hop is validated again. Addresses given by the model still get the full SSRF check.
- When a local proxy has fake-ip on, DNS resolution returns fake addresses in the 198.18 range. Once that range is set in `AGENTLAB_HTTP_TOOL_FAKE_IP_RANGES` (for example `198.18.0.0/15`), such results are verified against public DNS (DoH) to find the real address before deciding; anything that can't be verified is treated as blocked. The DoH URLs are set by `AGENTLAB_HTTP_TOOL_DOH_URLS`, comma-separated (see the [FAQ](faq.md)).
- File tool paths must still be inside the workspace after `resolve()`.
- Custom tool request headers, MCP environment variables and request headers, and endpoint auth headers are all stored encrypted and shown only masked.
