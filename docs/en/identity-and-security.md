English | [Simplified Chinese](../zh-CN/identity-and-security.md)

# Identity, Permissions, and Security

This page covers login modes, roles and permissions, who masking is a boundary for, how strongly the code sandbox isolates code, and the other security boundaries. It is for admins and operators. The user interface is currently available in Simplified Chinese only.

## Login modes

There are three login modes. The environment variable `AGENTLAB_AUTH_MODE` (`off` / `local` / `oidc`) takes precedence over Settings → Identity.

- **off** (default): `X-Actor` carries a self-reported name, everyone is equivalent to an admin, and the app home page says so. Anyone on the same network who can open the page can use every feature: run workflows, spend the model quota you configured, read data sources, and execute sandbox code; approvals and run starts record whatever name each person typed, unverified. Only use this mode on a network you trust; `dev.sh --host` and `prod.sh --host` listen on all network interfaces.
- **local**: username + password (`hashlib.scrypt`). When switching to it in settings from a deployment where login was never on, the first admin must be created in the same save. For a new deployment whose environment variable specifies `local` directly, the first admin is created on the login page (available only while there is no admin yet).
- **oidc**: generic OIDC with authorization code + PKCE. Non-standard protocols such as Feishu and WeCom aren't supported for now. Configure the identity provider's address, the client ID and secret, and at least one admin mapping (an admin username or admin group). Roles are recomputed from the mapping at every login, and the `groups` claim is synced into read-only groups. The callback URL is `<public-url>/api/auth/oidc/callback`; behind a reverse proxy, set `AGENTLAB_PUBLIC_URL` (see [Configuration](configuration.md#public-url)).

### Sessions

- Sessions live in an HttpOnly, SameSite=Lax cookie, and the database stores only the token's hash.
- Sessions expire after 12 hours idle or after 7 days at most (`AGENTLAB_AUTH_SESSION_IDLE_HOURS`, `AGENTLAB_AUTH_SESSION_MAX_DAYS`).
- Whether the cookie carries Secure depends on whether the request is https; `AGENTLAB_AUTH_COOKIE_SECURE` can force it.
- With login on, non-GET requests must carry the `X-AgentLab-Request` header, and requests with an `Origin` must come from this site or from an address in `AGENTLAB_CORS_ORIGINS`.
- After consecutive failed logins, the wait time grows each time.

### Forgotten passwords

If you forget the password or misconfigure single sign-on, run this on the server:

```bash
cd backend && python -m app.auth reset-admin --username admin   # without --password, a random one is generated and printed once
```

## Roles and grants

Roles: admin ⊇ builder ⊇ user.

- Apps have three grant levels of their own: Use, View all instances, and Manage. Manage granted to a user counts as View all instances.
- When builders bind positions and grant access, they choose from a read-only people directory (`GET /api/directory`: username, display name, whether disabled, group names). Names that aren't in the directory, or that are disabled, can't be added.
- Single-sign-on people and groups enter the directory only after their first login; those not in it yet can be added only after confirmation.
- With login on, confirmations of publishing, starting runs, and data imports are always recorded as the logged-in person, and the interface no longer has a name field.

## Permission table

| What you can do | Admin | Builder | User |
|---|---|---|---|
| Ask Data, Copilot | ✓ | ✓ | Not by default; an admin can enable it under Settings → Identity |
| Orchestration, exploratory runs, publishing at the published level, data sources, data catalog, schedules, knowledge base, custom tools | ✓ | ✓ | ✗ |
| Publishing at the governed level, demoting a governed workflow to published, retiring versions | ✓ | ✗ | ✗ |
| Records page, live run streaming | ✓ (except other people's personal apps) | ✓ (except other people's personal apps) | Only their own Ask Data runs |
| MCP stdio, endpoints and credentials, capability governance, model access | ✓ | Read-only | ✗ |
| Apps: use / view all instances / build and publish | ✓ | Can use and view; managing requires being the creator or having a Manage grant | Use and view per grants |
| Shared blueprints: read, edit, trial run, create publish plans | ✓ | Ones they created, and ones that come from apps they manage | ✗ |
| Create personal apps, refresh, submit for review | ✓ | ✓ | Not by default; an admin can enable it in the Apps section of Settings → Preferences |
| Other people's personal apps: name, owner, created and updated times, archiving | ✓ | ✗ | ✗ |
| Other people's personal apps: pages, refresh and trial-run runs, evidence, attachments, blueprints | ✗ | ✗ | ✗ |
| Handle tasks assigned to oneself, verify actions | ✓ | ✓ | ✓ |
| Users, groups, login modes, audit | ✓ | ✗ | ✗ |

## Ask Data for users

Once an admin enables it, Ask Data for users can only start exploratory runs:

- The graph may contain only these nodes: input, LLM call, Agent, report writer, output, knowledge retrieval, data shaping, conditional branch, caliber card, merge query, and Call Tool.
- Agent and Call Tool nodes can only use data source queries and read-only built-in tools.
- Query results are masked at the tool boundary first; data sources are always read-only (Call Tool nodes and Agents go through the same gate); tools changed to write in capability governance aren't executed either.
- The memory scope is fixed to the user's own.
- Conversations are isolated by the person who created them.

Personal apps use the same allowlist for their graphs (see [Personal apps](apps.md#personal-apps)).

## Which roles can see raw values

For the masked columns set on a data source:

- **For users with login on, masking is a boundary**: query snapshots are given only after derived masking is applied; raw tool snapshots and result previews in run events are never given; artifacts are given per an allowlist.
- **For builders, admins, and everyone when login is off, masking only reduces exposure**: full snapshots can still be fetched by artifact id.

With login on, the only raw run records, events, and live streams a user can read are those of their own Ask Data runs. Other runs (including caliber reconciliations started on the user's behalf) are read through app pages and the evidence API, and masked values written in the displayed SQL are replaced with "Masked".

## Audit

Written to the audit log:

- Administrative operations: login modes, users and roles, groups, endpoints, capability governance, app publishing, and version retirement.
- Creating, revoking, rotating, and toggling access keys; every request made with a key is logged and kept for 90 days (see [Access keys](apps.md#access-keys)).
- Creating and revoking share links (opens are only counted).
- Report exports and verifiable report downloads (`report.exported`).

## Code sandbox

Python in code nodes and tools runs in a sandbox, which has two tiers:

- **Lightweight tier**: the access control built into the operating system: Seatbelt (`sandbox-exec`) on macOS, bubblewrap on Linux. Cold start takes about 25ms, and nothing extra needs installing.
- **Heavyweight tier**: microVM (libkrun + Hypervisor.framework), which boots a virtual machine with its own kernel.

Both tiers guarantee three constraints: **network off by default**, **home directory unreadable**, and **only the workspace writable**.

- Seatbelt's policy is enforced by the kernel and applies to child processes too: a `cat` started with `subprocess` inside the sandbox also gets `Operation not permitted` when it reads the home directory.
- The interpreter deliberately uses the clean Python under `sys.base_prefix` rather than the project environment, so sandboxed code can't import FastAPI, SQLAlchemy, or any credential-handling module.
- The bubblewrap sandbox doesn't inherit the backend's environment variables.

**Seatbelt's limitations**: it provides access control, not virtualization.

- The process table is shared, so the host's process list is visible.
- Code runs as the current user rather than dropping privileges to nobody as a container would.
- **macOS doesn't enforce `RLIMIT_AS`, so memory usage can't be limited**: in testing, with 256MB set, 900MB could still be allocated. Limits on CPU time, single-file size, and wall-clock time do work.

**microVM fills exactly this memory gap.** Tested on the development machine: the host is Darwin 27.0.0, the VM runs Linux 6.12.99 (built by libkrunfw), PID 1 is `init.krun`, `/Users` doesn't exist inside the VM, and `free` shows only 512MB. Requesting 900MB gets the process killed by the kernel's OOM killer while the VM survives: the problem of `RLIMIT_AS` being toothless on macOS isn't fixed here; it structurally doesn't exist. Two VMs can't see each other either.

The cost of microVM: the runtime is about 50MB; the OCI image's **first pull measured 54.5 seconds**; once cached, a warm start takes 0.19 seconds and execution inside the VM takes 7–30ms. The cost is concentrated in the first time, so auto mode prefers Seatbelt while the image isn't cached, to avoid a one-minute wait (explicitly specifying strict isn't affected).

When neither tier is available, the sandbox falls back to `local`, which has **no access control at all** and is marked in red in the UI. For Docker deployments on machines without KVM, the only isolation boundary is the container itself (see [Deployment](deployment.md#the-code-sandbox-in-the-bundle)). For choosing the backend and enabling microVM, see [Configuration](configuration.md#sandbox-backend).

### Network isolation is incomplete

> **This has to be stated up front.**
> With `network=false`, HTTP/HTTPS and DNS resolution are both cut off, but **UDP/53 can't be blocked**: in testing, hand-crafted DNS packets still got real responses. `default_egress=DENY`, `Rule.deny_dns()`, explicitly denying UDP, and `max_connections=0` have all been tried, and the UDP channel could never be closed (the rules generated by `deny_dns` apply only to the host, and DNS egress is hard-coded as allowed in microsandbox's network stack).
> That is, **the DNS-tunnel exfiltration channel is always open**. This isolation is enough to guard against "code accidentally going online to install dependencies", but not against "malicious code stealing data". That is why the settings page marks the network item yellow, as "Partially effective", rather than green: showing all green when only half is blocked is more dangerous than showing all red. Real exfiltration protection has to be implemented at the network layer.

## Security boundaries

- **Expressions**: branch conditions and similar expressions use an AST-allowlist evaluator, not `eval`.
- **Tool network access**: HTTP tools check every IP, block internal and metadata addresses, and don't follow redirects; the internal network is open only to registered endpoints with Allow internal network access; file tools stay inside the workspace (see [Tools](tools.md#network-and-credentials)).
- **Databases**: the SQL guard, read-only at the connection layer, and read-only accounts for Oracle (see [Data Sources](data-sources.md#read-only-guarantees)).
- **Human approval**: only an explicit approval counts (`true`, `approve`, or the Chinese word for "agree"), and any reply it can't understand is treated as a rejection. MCP and custom tools wait for approval on every call by default (see [Tools](tools.md#approval-levels)).
- **Secrets**: model API keys are encrypted with Fernet before being stored, and only masked values are returned to the frontend; endpoint auth headers, custom tool request headers, and MCP environment variables and request headers are likewise encrypted and shown only masked.
- **With login on**: for session cookies, the database stores only the token's hash; non-GET requests are checked for the source-marker header and Origin; users can read only runs they are authorized for, and the query results they see are always masked.
- **Writing to external systems**: must go through a Call Tool node; the effect record is written before sending; the content hash on the confirmation card is recomputed and compared before sending; a write whose result is unknown is never resent automatically (see [Apps](apps.md#write-to-external-systems)).
- **Security response headers**: `nosniff`, `Referrer-Policy`, and `frame-ancestors` (default `'self'`; `AGENTLAB_FRAME_ANCESTORS` lists the same-site portals allowed to embed the whole site; when it is only `'self'`, `X-Frame-Options: SAMEORIGIN` is added too). A strict `script-src` is currently sent only as `Content-Security-Policy-Report-Only`, to be enforced after one version with no false positives. HSTS is left to the reverse proxy.
- **Runs the system starts on someone's behalf**: evaluations, caliber reconciliations, and caliber recalls keep their evidence inspectable, but they are left out of usage statistics, and the Records page doesn't list them by default.
