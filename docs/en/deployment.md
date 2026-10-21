English | [Simplified Chinese](../zh-CN/deployment.md)

# Deployment

This page covers deploying AgentLab for multiple users: `prod.sh` without Docker, the Docker offline bundle, the data directory, backups, and upgrades. It is for operators. For starting AgentLab during development, see [Development](development.md). The user interface is currently available in Simplified Chinese only.

## Choose a method

- **Without Docker**: `./scripts/prod.sh` builds the frontend on the machine and has the backend serve the pages too.
- **Docker offline bundle**: build a bundle on a machine with internet access, then copy it to an offline machine that has only Docker Engine.

Both methods have **login off by default**: anyone on the same network who can open the page can use every feature. Turn on login before multiple people use it; see [Identity, Permissions, and Security](identity-and-security.md#login-modes). Behind a reverse proxy, set the public URL (see [Configuration](configuration.md#public-url)).

## Without Docker: prod.sh

```bash
./scripts/prod.sh               # build the frontend (if needed) and start at http://127.0.0.1:8000
./scripts/prod.sh --workers 4   # multiple processes; switch the app database to PostgreSQL first
```

- One process by default, without hot reload.
- The frontend is rebuilt only when the build output is missing or older than the source; `--build` / `--no-build` forces or skips the build.
- `--port` (or `AGENTLAB_PORT`) changes the port; `--host` listens on all network interfaces.
- The data directory defaults to `data/` under the repository; change it with `AGENTLAB_DATA_DIR`.
- The app database defaults to SQLite in the data directory, which **supports a single process only**: when a service is already using the same data directory, a second process refuses to start and says so plainly. Don't add `--workers` to uvicorn; `prod.sh` stops you first when `--workers` is greater than 1.
- Multiple processes are possible only after switching to PostgreSQL; see [PostgreSQL and Scaling](postgresql-and-scaling.md#multiple-processes).

## Docker offline bundle

1. On the packaging machine, run `./scripts/package-docker.sh`. It builds a Linux image (the local architecture by default; add `--platform linux/arm64` or `linux/amd64` when the target machine differs) and packs it, together with a one-step deploy script, a configuration template, and deployment notes, into `release/agentlab-<version>-linux-<arch>.tar`.
2. Copy it to an offline machine that has only Docker Engine, unpack it, edit `agentlab.env` as needed, and run `./deploy.sh`. Docker Compose isn't needed.

For installation, upgrades, backups, sandbox permissions, and common problems, see the deployment notes in the bundle ([deploy/README.md](../../deploy/README.md) in the repository; written in Chinese).

- `deploy.sh` recognizes the data directory by its `.agentlab-data` marker file (or by `agentlab.db` or `.secret_key`); don't delete it.
- The schema and default data are filled in automatically at startup.

### Where the app database goes

There are three ways to place the app database; choose one:

| Option | How |
|---|---|
| SQLite (default) | Nothing to do |
| Built-in PostgreSQL | For a new deployment, `./deploy.sh install --postgres` starts a PostgreSQL container alongside, with no externally exposed port and a password generated automatically in the data directory. For a deployment already on SQLite, `./deploy.sh migrate-to-postgres` stops the service, backs up, imports, and verifies; if any step fails, it falls back to SQLite |
| External PostgreSQL | Set `AGENTLAB_DATABASE_URL` in `agentlab.env` |

- Built-in and external PostgreSQL are mutually exclusive.
- `./deploy.sh upgrade` backs up automatically before upgrading: SQLite is copied to `backups/sqlite/` in the data directory, and the built-in PostgreSQL is saved with `pg_dump` to `backups/postgres/`; each keeps the latest 5 copies (`BACKUP_KEEP`), readable only by the owner.
- `restart` restarts only the service by default; `restart --postgres` restarts the database too.
- Multiple processes: add the line `AGENTLAB_WORKERS=4` to `agentlab.env`.

For details on the three options, see the PostgreSQL section of deploy/README.md.

### The code sandbox in the bundle

- By default the image includes the microVM runtime and sandbox image (about 150 MB); add `--no-microvm` when packaging to leave them out.
- When the target machine has KVM, `deploy.sh` first tries to boot a microVM in a temporary container. If it boots, `/dev/kvm` is handed to the container and code executes inside a microVM. No network access is needed during deployment.
- Without KVM (Docker Desktop on Mac or Windows, most cloud hosts), under default permissions the code sandbox falls back to a local subprocess, and the only isolation boundary is the container itself.

## The data directory

All data lives in the data directory (`data/` by default):

| Path | Contents |
|---|---|
| `agentlab.db` | Application data (the app database) |
| `checkpoints.db` | Execution checkpoints |
| `artifacts/` | Artifact store |
| `workspace/` | Sandbox files |
| `uploads/` | Uploaded files |
| `records/` | Form-submission record tables |
| `files/` | Form attachments |
| `.secret_key` | Encryption key |

- Deleting `data/` restores factory settings.
- When the app database uses PostgreSQL, `agentlab.db` and `checkpoints.db` are no longer used, and the application data and checkpoints live in PostgreSQL; everything else stays in `data/`, so you still need the data directory and still need to back it up.
- For sharing one data directory across several machines, see [Multiple machines](postgresql-and-scaling.md#multiple-machines).

## Backups

- With SQLite, the data directory is all the data; back up the whole directory. For Docker deployments, see the data and backup section of deploy/README.md.
- With PostgreSQL, a backup is `pg_dump` plus the data directory; see [Backups](postgresql-and-scaling.md#backups).
- The form-submission record tables `records/*.db` are SQLite files appended in place, so copying them while the service is running may capture a half-written file. Either copy them with the service stopped, or copy them one by one with SQLite's backup API.

## Upgrades

- Before every upgrade, read the [Upgrade Notes](upgrade-notes.md): they list the changes that affect users and operators, and what you need to do.
- For Docker deployments, use `./deploy.sh upgrade`: it stops the old container first (all worker processes inside stop with it), backs up, and then starts the new version.
- Processes of an old and a new version can't be connected to the same database at the same time, and the code enforces this; see [Upgrading a multi-process deployment](postgresql-and-scaling.md#upgrades).
- Whether process instances can continue after an upgrade is covered in [Apps](apps.md#process-instances-across-upgrades).
