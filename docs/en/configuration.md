English | [Simplified Chinese](../zh-CN/configuration.md)

# Configuration

This page covers where configuration lives, how to connect models, common environment variables and UI settings, and how to choose the sandbox backend and enable microVM. It is for admins and operators. For database and multi-process settings, see [PostgreSQL and Scaling](postgresql-and-scaling.md). The user interface is currently available in Simplified Chinese only.

## Where configuration lives

- **Environment variables**. During development they can also go in a `.env` file at the repository root; environment variables take precedence. `.env.example` lists the groups for the app database, connection pool, PgBouncer, multiple processes, and the development PostgreSQL container.
- **Docker deployments** keep them in `agentlab.env` (see [deploy/README.md](../../deploy/README.md), written in Chinese).
- **Settings in the UI**. For two items the environment variable takes precedence: `AGENTLAB_AUTH_MODE` over Settings → Identity, and `AGENTLAB_PUBLIC_URL` over the public URL under Settings → General.

## Model access

Configure models under Settings → Model Access. Supported: Anthropic, OpenAI, any OpenAI-compatible service (DeepSeek, Kimi, Tongyi, Zhipu, SiliconFlow, Ollama, and others), and the built-in Mock.

- At startup, `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `OPENAI_API_KEY` found in the environment are imported as providers automatically. Providers without a configured key also fall back to keys in the environment.
- API keys are encrypted with Fernet before being stored, and only masked values are returned to the frontend.
- Mock needs no API key: it produces streaming replies, tool calls, and schema-conforming structured data, so the whole orchestration path (canvas highlighting, approval interrupts, sandbox execution) runs end to end. But Mock writes no citation markers, so every number in its reports is marked "No evidence".
- Cost is computed at list prices.
- Some relay gateways accept only `Authorization: Bearer` and reject requests that also carry `x-api-key`: tick Authenticate with Authorization: Bearer in the provider settings.
- Models after Claude 4.6 enable thinking by default, and with too small a `max_tokens` you get "Thinking used up the budget; the body is empty"; see the [FAQ](faq.md).

Other places that use models: the drafting model is chosen under Settings → Preferences (see [Data Catalog](data-catalog.md#drafting)); the knowledge base's embedding model can use a local endpoint (see [Workflows](workflows.md#knowledge-base-memory-and-skills)).

## Public URL

Set the public URL when AgentLab sits behind a reverse proxy, or when external systems need to call back into it.

- Settings → General has a single public URL setting. `AGENTLAB_PUBLIC_URL` takes precedence, and a malformed value fails at startup.
- The OIDC callback is built from this address, `<public-url>/api/auth/oidc/callback`, and must match what is registered with the identity provider.
- HSTS is left to the reverse proxy.

## Common environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AGENTLAB_DATA_DIR` | `data/` under the repository | Data directory |
| `AGENTLAB_PORT` | 8000 | Backend port |
| `AGENTLAB_WEB_PORT` | 5273 | Frontend port during development |
| `AGENTLAB_CONDA_ENV` | `agentlab` | Conda environment name used by `dev.sh` |
| `AGENTLAB_AUTH_MODE` | `off` | Login mode: `off` / `local` / `oidc` |
| `AGENTLAB_PUBLIC_URL` | — | Public URL |
| `AGENTLAB_CORS_ORIGINS` | — | Other addresses allowed to send requests with an `Origin` |
| `AGENTLAB_AUTH_SESSION_IDLE_HOURS` | 12 | Session idle timeout (hours) |
| `AGENTLAB_AUTH_SESSION_MAX_DAYS` | 7 | Maximum session lifetime (days) |
| `AGENTLAB_AUTH_COOKIE_SECURE` | Depends on whether the request is https | Force the cookie's Secure flag |
| `AGENTLAB_FRAME_ANCESTORS` | — (only the same site may embed) | Same-site portals allowed to embed the whole site |
| `AGENTLAB_EMBED_ORIGINS` | — | Sites allowed to embed share pages |
| `AGENTLAB_SCHEDULER` | On | Scheduler switch; set to 0 to turn it off |
| `AGENTLAB_SCHEDULER_TICK_S` | 15 | Interval between scheduler loop ticks (seconds) |
| `AGENTLAB_MAX_CONCURRENT_RUNS` | 20 | Site-wide limit on concurrently executing runs |
| `AGENTLAB_MAX_RUN_SECONDS` | 600 | Time limit for a single execution (seconds) |
| `AGENTLAB_MODEL_TIMEOUT_SECONDS` | 300 | How long a model call may go without output before it times out (seconds) |
| `AGENTLAB_MAX_GRAPH_STEPS` | 200 | Step limit for a graph |
| `AGENTLAB_MAX_AGENT_STEPS` | — | Cap on agent steps |
| `AGENTLAB_SANDBOX_BACKEND` | Chosen automatically | Force a sandbox backend |
| `AGENTLAB_MICROVM_IMAGE` | `python:3.12-slim` | Image used by microVM |
| `AGENTLAB_MICROVM_IDLE_SECONDS` | — | How long an idle VM is kept before it is reclaimed |
| `AGENTLAB_HTTP_TOOL_FAKE_IP_RANGES` | — | The local proxy's fake-ip range, such as `198.18.0.0/15` |
| `AGENTLAB_HTTP_TOOL_DOH_URLS` | — | DoH URLs used to verify fake-ip results, comma-separated |

For the app database, checkpoint database, connection pool, PgBouncer, and multi-process variables, see [PostgreSQL and Scaling](postgresql-and-scaling.md).

## Settings in the UI

| Location | Contents |
|---|---|
| Settings → Model Access | Providers and keys |
| Settings → Preferences | Drafting model; Allow creating share links in the Apps section; evaluation concurrency, per-question time limit, and per-evaluation and daily spending; run concurrency (admins) |
| Settings → Runtime Environment | Sandbox candidates and their availability, the current choice and its reason; current values such as time limits |
| Settings → General | Public URL |
| Settings → Identity | Login mode; enabling Ask Data for users |
| Settings → Notifications | Channel subscriptions; Mail Service (SMTP) |
| Settings → Apps | Letting users create personal apps |
| Settings → Access Keys | Service accounts, the personal access key switch, the API reference |
| The `ledger` group in Settings | The automatic issuance switch `auto_issue`; the recall limit `ledger.recall_limit` |

Other settings: `notify.digest_at` (the time of the daily task digest), `apps.on_demand_daily_limit` (the daily cap on on-demand runs per app), and the agent token budget, spending budget, and step count.

## Sandbox backend

Settings → Runtime Environment lists every candidate and its availability, and marks the current choice and its selection reason. Automatic selection works in this order:

1. When the microVM dependencies are installed and the image is cached (and, on Linux, `/dev/kvm` is accessible), choose `microvm`.
2. Otherwise macOS uses `seatbelt`, and Linux uses `bubblewrap` once `bwrap` is installed (`apt install bubblewrap`).
3. When none is available, fall back to `local`. **This path has no access control at all**, and the UI clearly marks it in red.

- An installed `bwrap` counts as available only if it can actually create namespaces: the first selection after startup launches `bwrap` once as a probe. Under Docker's default permissions or Ubuntu 24.04's AppArmor restrictions the probe fails, `bubblewrap` is skipped, and the selection reason says so.
- Force a backend with `AGENTLAB_SANDBOX_BACKEND`, or choose the isolation tier on an individual code node (strict = microVM, fast = OS sandbox).
- Once the backend has chosen a sandbox, the whole process keeps using it. After installing microVM, restart the service for it to choose again.

For what each sandbox tier guarantees and its limits, see [Identity, Permissions, and Security](identity-and-security.md#code-sandbox).

### Enable microVM

There are three preparations, **each done only once**; day-to-day startup doesn't change:

1. The Python package: already installed when the environment is created from `environment.yml` (including the `[microvm]` extra).
2. The runtime (about 50MB, placed in `~/.microsandbox`): once per machine.
3. The OCI image (measured at about 55 seconds): once per image.

Items 2 and 3 are done by hand. After installing, check the machine's virtualization support with `doctor`, then restart `dev.sh`:

```bash
conda run -n agentlab python -c "exec('import asyncio, microsandbox as m\nasync def main():\n    await m.install()\nasyncio.run(main())')"
conda run -n agentlab microsandbox pull python:3.12-slim   # the default image; if you changed AGENTLAB_MICROVM_IMAGE, pull that one
conda run -n agentlab microsandbox doctor
```

- **Pull the image first.** Auto mode only chooses microVM when its image is already cached (so the first code run doesn't hang for a minute), but the image is only pulled when code runs inside a microVM. If you don't pull it by hand, auto mode never chooses it. If you don't want to wait, set `AGENTLAB_SANDBOX_BACKEND=microvm`, and the image is pulled the first time code runs.
- If pulling fails with `registry error: error sending request for url (https://index.docker.io/...)`, Docker Hub can't be reached. msb honors `HTTPS_PROXY` / `ALL_PROXY`, so you can point it at a proxy: `HTTPS_PROXY=http://<proxy-host>:<port> microsandbox pull python:3.12-slim`. Or pull from a mirror you can reach (write the full name, such as `<mirror>/library/python:3.12-slim`), then set `AGENTLAB_MICROVM_IMAGE` to the same name.
- `install` must be called inside a running event loop. Writing `asyncio.run(m.install())` would call it outside the loop first and fail with `RuntimeError: no running event loop`, which is why the command above wraps it in an `async def`.
- Linux needs `/dev/kvm`, and the current user needs permission for it. When doctor reports "/dev/kvm is not accessible by your user":

```bash
sudo usermod -aG kvm $USER               # permanent; takes effect after you log in again
sudo setfacl -m u:$USER:rw /dev/kvm      # usable immediately in the current session
```

- You can also just run `microsandbox doctor --fix`. Run doctor again afterwards; everything should be ✓.
- All green in doctor doesn't mean item 2 is installed: doctor only checks the msb and libkrunfw bundled in the wheel, while AgentLab decides with `microsandbox.is_installed()`, which looks at `~/.microsandbox`. So you still need to run the install command above.
- All three live on disk, so restarting the machine or reopening the terminal doesn't mean redoing them. When the `microvm` entry under Settings → Runtime Environment shows `warm` as true, all three are in place, and auto mode switches to `microvm` by itself.
- Change the image with `AGENTLAB_MICROVM_IMAGE`; how long idle VMs are kept before being reclaimed is set by `AGENTLAB_MICROVM_IDLE_SECONDS`.
