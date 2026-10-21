[English](../en/deployment.md) | 简体中文

# 部署

这篇讲怎么把 AgentLab 部署给多人使用：不用 Docker 的 `prod.sh`、Docker 离线包、数据目录、备份和升级。给运维看。开发时怎么启动见[开发](development.md)。

## 选哪种方式

- **不用 Docker**：`./scripts/prod.sh` 在本机构建前端，由后端一并托管页面。
- **Docker 离线包**：在能联网的机器上打包，拷到只装了 Docker Engine 的离线机器上部署。

两种方式默认都**不开登录**：同一网络里打得开页面的人都能用全部功能。多人使用前先开登录，见[身份、权限与安全](identity-and-security.md#登录方式)。放在反向代理后面时，设对外访问地址（见[配置](configuration.md#对外访问地址)）。

## 不用 Docker：prod.sh

```bash
./scripts/prod.sh               # 构建前端（需要时）并启动，地址 http://127.0.0.1:8000
./scripts/prod.sh --workers 4   # 多进程，要先把应用库换成 PostgreSQL
```

- 缺省一个进程，不热重载。
- 前端产物不存在或比源码旧时才重新构建；`--build` / `--no-build` 强制构建或跳过。
- `--port`（或 `AGENTLAB_PORT`）改端口；`--host` 监听所有网卡。
- 数据目录默认是仓库下的 `data/`，用 `AGENTLAB_DATA_DIR` 改。
- 应用库默认是数据目录里的 SQLite，**只支持单进程**：同一个数据目录已经有一个服务在用时，第二个进程拒绝启动，照实报错。不要给 uvicorn 加 `--workers`；`--workers` 大于 1 时，`prod.sh` 先拦下。
- 换成 PostgreSQL 之后才能多进程，见 [PostgreSQL 与多进程](postgresql-and-scaling.md#多进程)。

## Docker 离线包

1. 在打包的机器上运行 `./scripts/package-docker.sh`。它构建 Linux 镜像（默认取本机架构；目标机不同时加 `--platform linux/arm64` 或 `linux/amd64`），连同一键部署脚本、配置模板和部署说明，打成 `release/agentlab-<版本>-linux-<arch>.tar`。
2. 拷到只装了 Docker Engine 的离线机器上，解包，按需修改 `agentlab.env`，运行 `./deploy.sh`。不需要 docker compose。

安装、升级、备份、沙箱权限和常见问题，见包里的部署说明（仓库里是 [deploy/README.md](../../deploy/README.md)）。

- `deploy.sh` 靠数据目录里的 `.agentlab-data` 标记文件（或 `agentlab.db`、`.secret_key`）认数据目录，别删。
- 表结构和默认数据在启动时自动补齐。

### 应用库放哪里

应用库有三种放法，选一种：

| 放法 | 怎么做 |
|---|---|
| SQLite（缺省） | 什么都不用做 |
| 内置 PostgreSQL | 新部署用 `./deploy.sh install --postgres`：旁边再起一个 PostgreSQL 容器，端口不对外，密码自动生成在数据目录里。已经在用 SQLite 的，用 `./deploy.sh migrate-to-postgres`：停服务、备份、导入、核对，哪一步出错都退回 SQLite |
| 外接 PostgreSQL | 在 `agentlab.env` 里设 `AGENTLAB_DATABASE_URL` |

- 内置和外接只能选一种。
- `./deploy.sh upgrade` 升级前自动备份：SQLite 拷到数据目录的 `backups/sqlite/`，内置 PostgreSQL 用 `pg_dump` 存到 `backups/postgres/`；各留最近 5 份（`BACKUP_KEEP`），只有属主能读。
- `restart` 缺省只重启服务；`restart --postgres` 连库一起重启。
- 多进程：在 `agentlab.env` 里加一行 `AGENTLAB_WORKERS=4`。

三种放法的细节见 deploy/README.md 的「用 PostgreSQL」。

### 离线包里的代码沙箱

- 镜像里默认带 microVM 的运行时和沙箱镜像（约 150 MB）；打包时加 `--no-microvm` 可以不带。
- 目标机有 KVM 时，`deploy.sh` 先用临时容器试启动一台 microVM。起得来，就把 `/dev/kvm` 交给容器，代码在 microVM 里执行。部署时不用联网。
- 没有 KVM 时（Mac、Windows 上的 Docker Desktop，多数云主机），默认权限下代码沙箱退回本地子进程，隔离边界只有容器本身。

## 数据目录

全部数据都在数据目录（默认 `data/`）里：

| 路径 | 内容 |
|---|---|
| `agentlab.db` | 业务数据（应用库） |
| `checkpoints.db` | 执行断点 |
| `artifacts/` | 工件库 |
| `workspace/` | 沙箱文件 |
| `uploads/` | 上传文件 |
| `records/` | 填报登记的记录表 |
| `files/` | 表单附件 |
| `.secret_key` | 加密密钥 |

- 删除 `data/` 就恢复出厂设置。
- 应用库用 PostgreSQL 时，不再用 `agentlab.db`、`checkpoints.db`，业务数据和断点都在 PostgreSQL 里；其余照旧在 `data/`，数据目录照样要有、要备份。
- 几台机器共享一份数据目录时的要求，见[多台机器](postgresql-and-scaling.md#多台机器)。

## 备份

- 用 SQLite 时，数据目录就是全部数据，整份备份它。Docker 部署的做法见 deploy/README.md 的「数据和备份」。
- 用 PostgreSQL 时，备份是 `pg_dump` 加数据目录，见[备份](postgresql-and-scaling.md#备份)。
- 填报的记录表 `records/*.db` 是原地追加写的 SQLite 文件，开着服务直接拷，可能拷到写了一半的。要么停服务拷，要么用 SQLite 的备份接口逐个拷。

## 升级

- 每次升级前，先看[升级说明](upgrade-notes.md)：里面列出对使用者和运维有影响的变化，以及要做的事。
- Docker 部署用 `./deploy.sh upgrade`：先停掉旧容器（里面的全部工作进程一起停）、备份，再起新版本。
- 新旧版本的进程不能同时连着一个库，代码里拦着，见[升级多进程部署](postgresql-and-scaling.md#升级)。
- 流程实例升级后能不能继续，见[应用](apps.md#升级时流程实例怎么办)。
