# AgentLab 离线部署说明

版本 **__AGENTLAB_VERSION__** · 架构 **linux/__AGENTLAB_ARCH__**

这个包在一台只装了 Docker Engine、不能联网的 Linux 机器上部署 AgentLab。
不需要 docker compose，不需要 Python、Node，也不需要联网。

## 包里有什么

| 文件 | 用途 |
| --- | --- |
| `image.tar` | Docker 镜像（`docker save` 导出），镜像名 `agentlab:__AGENTLAB_VERSION__` |
| `postgres-image.tar` | 内置 PostgreSQL 的镜像（`install --postgres` 时用，见「用 PostgreSQL」）。包因此多约 160 MB，导入后在 docker 里占约 0.7 GB；只用 SQLite 或外接 PostgreSQL 时用不上，打包时用 `--no-postgres` 就没有这个文件 |
| `deploy.sh` | 一键部署脚本：安装、启动、停止、升级、卸载 |
| `agentlab.env` | 配置：端口、监听地址、数据目录、模型 Key、代理、沙箱 |
| `README.md` | 本说明 |
| `SHA256SUMS` | 校验和。拷完先校验：`sha256sum -c SHA256SUMS`（改过 `agentlab.env` 之后它那一行会对不上，正常） |

## 前提条件

- Ubuntu 24.04（别的 64 位 Linux 发行版一般也行），已装好 Docker Engine 并在运行；
- **架构要一致**：这个包是 `linux/__AGENTLAB_ARCH__`。在目标机上运行 `uname -m`：
  `aarch64` 对应 arm64（ARM 单板机要装 64 位系统），`x86_64` 对应 amd64。不一致的镜像跑不起来，
  deploy.sh 会在导入前就报出来；
- 磁盘：镜像导入后约 0.8～1 GB（看 Docker 用的存储方式），另留数据目录的空间；用内置 PostgreSQL 再加约 0.7 GB 的镜像
  和库本身的空间（在 docker 的数据卷里）；
- 可选：宿主机支持 KVM（有 `/dev/kvm`）时，代码会在 microVM 里执行，隔离最强，见下面的「代码沙箱」；
- 当前用户能用 docker（在 `docker` 组里），或者用 `sudo` 运行 deploy.sh。

## 三步部署

```bash
# 1. 解包
tar -xf agentlab-__AGENTLAB_VERSION__-linux-__AGENTLAB_ARCH__.tar
cd agentlab-__AGENTLAB_VERSION__-linux-__AGENTLAB_ARCH__

# 2. 按需修改配置（端口、监听地址、数据目录、模型 Key……每一项都有注释）
nano agentlab.env

# 3. 部署
./deploy.sh
```

deploy.sh 依次：检查 docker 和架构 → 导入镜像（`docker load -i image.tar`，本机已有就跳过）→
创建数据目录 → 启动容器（`--restart unless-stopped`，开机自动起来）→ 等健康检查通过 → 打印访问地址。
库缺省是数据目录里的 SQLite；要用 PostgreSQL 的新部署把第 3 步换成 `./deploy.sh install --postgres`（见「用 PostgreSQL」）。

## 访问

浏览器打开 deploy.sh 最后打印的地址，默认 `http://<这台机器的 IP>:8000`。
端口改 `HOST_PORT`；只给本机用的话把 `BIND_ADDR` 改成 `127.0.0.1`。

第一次打开就能用：内置的演示模型不需要 API Key，编排链路能完整走一遍。接入真实模型有两种方式：

- 在 `agentlab.env` 里填 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`，第一次启动时自动导入成模型接入；
- 或者启动后在界面「设置 → 模型接入」里配。

注意：环境变量只在第一次启动（库里还没有任何模型接入）时导入，之后要改 Key 请到界面里改。

## 数据和备份

所有数据都在 `DATA_DIR`（默认是包目录下的 `data/`）：数据库 `agentlab.db`、执行断点 `checkpoints.db`、
工件、上传文件、沙箱工作区，以及加密密钥 `.secret_key`。**要备份的就是这个目录**，
丢了 `.secret_key`，存进去的 API Key 就解不开了。目录里的 `.agentlab-data` 是服务启动时建的标记，deploy.sh 改属主前按它
（或 `agentlab.db`、`.secret_key`、`.postgres/state`）认出这是 AgentLab 的数据目录，别删。数据库放在 PostgreSQL 时（下面「用 PostgreSQL」）没有这两个库文件，
另用 `pg_dump` 备份数据库。

`./deploy.sh upgrade` 换版本之前会自动备份一次库：SQLite 拷到数据目录的 `backups/sqlite/<时间>/`（SQLite 的备份接口，
一致的一份），内置 PostgreSQL 用 `pg_dump` 存到 `backups/postgres/`，各只留最近 5 份（`agentlab.env` 里 `BACKUP_KEEP=` 改）。
备份里是整个库（含加密存着的密钥），所以 `backups/` 和下面每一层目录都是 0700、备份文件 0600，属主同数据目录（uid 10001）：
宿主机上要看、要拷走都得用 sudo（`sudo ls -l data/backups/sqlite/`、`sudo cp -a data/backups /别处/`）。
这只是升级的保险，不代替下面的整目录备份：工件、上传文件和 `.secret_key` 不在里面。

用 SQLite 的部署要从 `backups/sqlite/` 恢复（以缺省的 `./data` 为例，`<时间>` 换成目录名）：

```bash
./deploy.sh stop
sudo rm -f data/agentlab.db-wal data/agentlab.db-shm data/checkpoints.db-wal data/checkpoints.db-shm
sudo cp -p data/backups/sqlite/<时间>/agentlab.db data/backups/sqlite/<时间>/checkpoints.db data/   # -p：属主仍是 10001
./deploy.sh start
```

`-wal`、`-shm` 要先删：留着的话 SQLite 会把它们当成换回来的库的一部分。

deploy.sh 会把数据目录的属主改成容器里的用户（uid 10001），所以备份、删除要用 sudo。
停下来备份最稳妥（以默认的 `./data` 为例，在包目录里执行）：

```bash
./deploy.sh stop
sudo tar -czf ../agentlab-data-$(date +%Y%m%d).tar.gz data
./deploy.sh start
```

恢复：停掉容器，把备份解回 `DATA_DIR`，再 `./deploy.sh start`。

## 用 PostgreSQL

库有三种放法，选一种：

| | 怎么用 | 库在哪里 | 数据目录里 |
| --- | --- | --- | --- |
| SQLite（缺省） | `./deploy.sh` | 数据目录的 `agentlab.db`、`checkpoints.db` | 全部数据 |
| 内置 PostgreSQL | 新部署 `./deploy.sh install --postgres`；已经在用 SQLite 的 `./deploy.sh migrate-to-postgres` | docker 数据卷 `<CONTAINER_NAME>-pgdata` | 工件、上传文件、`.secret_key`、库的密码 `.postgres/` 和备份 `backups/` |
| 外接 PostgreSQL | `agentlab.env` 里设 `AGENTLAB_DATABASE_URL` | 你自己的 PostgreSQL | 工件、上传文件、`.secret_key` |

内置和外接只能选一种：`agentlab.env` 里设了 `AGENTLAB_DATABASE_URL`（或 `AGENTLAB_CHECKPOINT_URL`）又加 `--postgres`，
或者数据目录已经是内置的，deploy.sh 照实报错、不起任何容器。不管哪种，数据目录都照样要挂、要备份。

### 内置 PostgreSQL

`./deploy.sh install --postgres` 在 AgentLab 旁边再起一个 PostgreSQL 容器：

- 镜像缺省 `postgres:16.15`（小版本固定在 deploy.sh 里，每台机器起的都一样；离线包里带着 `postgres-image.tar`，本机已有同名镜像
  就不导入）。要换在 `agentlab.env` 里写 `POSTGRES_IMAGE=postgres:16.x`，再 `./deploy.sh install`：服务先停、库的容器按新镜像重建，
  数据卷不动；新镜像起不来（跨大版本时数据格式不通用）就换回原来的镜像、照实报错。
- 两个容器在同一个 docker 网络 `<CONTAINER_NAME>-net` 里，PostgreSQL 的端口**不对外开**，只有 AgentLab 的容器连得到。
- 库的数据在 docker 数据卷 `<CONTAINER_NAME>-pgdata` 里（不在数据目录：数据目录会被改属主，PostgreSQL 的文件属主不能动）。
- 密码第一次部署时自动生成，存在数据目录的 `.postgres/password` 和 `.postgres/pgpass`（都是 0600），不进镜像、不进日志、
  不进 `docker inspect`，`agentlab.env` 里什么都不用填。AgentLab 的容器按 `PGPASSFILE` 读它。**丢了数据目录就连不上这个库**，
  所以数据目录照样要整份备份。
- 用过一次之后这个数据目录就一直是内置 PostgreSQL（记在 `.postgres/state`），之后的 `install`、`upgrade`、`start` 不用再加
  `--postgres`。
- `start` 先起库、等 `pg_isready` 通过再起服务；`stop` 先停服务再停库。`restart` 缺省只重启服务（库停着的话先把库起来），
  `restart --postgres` 连库一起重启（先停服务再停库，再先起库后起服务）。开机时两个容器一起被 docker 拉起来，库还没就绪时
  服务按退避重试（日志里写着「应用库还没就绪……秒后重试」），最多等 90 秒（`AGENTLAB_DB_STARTUP_TIMEOUT`），不会一上来就崩。
- 表结构和默认数据（内置模板、技能）每次启动自动补齐；几个工作进程（`AGENTLAB_WORKERS`）同时起也只做一次（库里的锁）。
- `./deploy.sh status` 显示两个容器的状态，`./deploy.sh logs --postgres` 看库的日志。

可以在 `agentlab.env` 里改的（都有缺省值，一般不用写）：

| 项 | 缺省 | 说明 |
| --- | --- | --- |
| `POSTGRES_IMAGE` | `postgres:16.15` | 库的镜像 |
| `POSTGRES_CONTAINER` | `<CONTAINER_NAME>-postgres` | 库的容器名 |
| `DOCKER_NETWORK` | `<CONTAINER_NAME>-net` | 两个容器共用的网络 |
| `POSTGRES_VOLUME` | `<CONTAINER_NAME>-pgdata` | 库的数据卷 |
| `BACKUP_KEEP` | `5` | 升级前自动备份留几份（SQLite 也按它） |

环境变量 `DEPLOY_<名字>` 盖过 `agentlab.env` 里的同名项（这几项加上 `HOST_PORT`、`BIND_ADDR`、`DATA_DIR`、`CONTAINER_NAME`），
一台机器上并排部署两套、或者测试时用。

**备份。** `upgrade` 前自动 `pg_dump` 一份到 `backups/postgres/`（0600，见「数据和备份」）。平时要手动备份（服务开着也行，
`pg_dump` 读的是一致的快照；`umask 077` 让导出的文件只有自己读得到）：

```bash
(umask 077; docker exec agentlab-postgres pg_dump -U agentlab -d agentlab -Fc > agentlab-$(date +%Y%m%d).dump)
```

再加上数据目录（见「数据和备份」），两样合起来才是完整的一份。

**恢复。** 用 `backups/postgres/` 里的备份把库换回去（以缺省的名字、在包目录里为例，`<备份>` 换成文件名，`sudo ls data/backups/postgres/`
看有哪些）。备份文件是 0600、属主 10001，宿主机上的人读不到，所以最后一步在一个以 10001 运行的临时容器里读它，不用 sudo：

```bash
./deploy.sh stop                       # 两个都停
docker start agentlab-postgres         # 只起库
until docker exec agentlab-postgres pg_isready -q -h 127.0.0.1 -U agentlab; do sleep 1; done
docker exec agentlab-postgres psql -U agentlab -d postgres \
  -c 'DROP DATABASE agentlab WITH (FORCE)' -c 'CREATE DATABASE agentlab OWNER agentlab'
docker run --rm --network agentlab-net -v "$(pwd)/data:/data" --user 10001:10001 -e PGPASSFILE=/data/.postgres/pgpass \
  --entrypoint pg_restore postgres:16.15 -h agentlab-postgres -U agentlab -d agentlab --no-owner /data/backups/postgres/<备份>
./deploy.sh start
```

自己手动导出、放在别处的备份（上面「备份」那条命令导出的，属主是你自己）直接从宿主机喂进去：
`docker exec -i agentlab-postgres pg_restore -U agentlab -d agentlab --no-owner < agentlab-<日期>.dump`。

数据目录要换回同一时间的备份，工件、上传文件和库里的记录才对得上。

**从 SQLite 切过来。** 正在用 SQLite 的部署运行 `./deploy.sh migrate-to-postgres`，它依次：

1. 停服务；
2. 备份 SQLite 库到 `backups/sqlite/<时间>/`；
3. 起内置 PostgreSQL（第一次生成密码）；
4. 用镜像里的 `python -m app.db copy-to-postgres` 先核对（`--dry-run`，只数行数）；
5. 正式导入，逐表按主键核对、断点按条数核对（停在审批上的、中断了的运行导过去之后照样能接着跑）；
6. 在数据目录里记下「已切到内置 PostgreSQL」，按内置库起服务、等健康检查通过。

记下之前任何一步出错，都停在原地：服务按 SQLite 原样恢复（原来在跑就起回来），库的容器停掉，屏幕上是那一步的原话。
处理好之后重新运行就行，重复执行是安全的：已经切过的什么都不做；上次没切成、留在内置库里的半份数据先清空再导，
不会和 SQLite 上后来改过的混在一起（`status` 里会写「上次 migrate-to-postgres 没切成」）。数据卷已经存在、又不是这个
数据目录上次没切成留下的（比如以前卸载时留下的另一份库），它不会去清空，照实报错让你决定。

切完之后 `agentlab.db`、`checkpoints.db` 原样留在数据目录里、不再使用，`backups/sqlite/` 里还有一份。**什么时候能删**：
在 PostgreSQL 上用一段时间、做过一次完整备份（`pg_dump` 加数据目录）之后。删之前它们是回退的路：`./deploy.sh stop`、
`sudo rm <数据目录>/.postgres/state`、`./deploy.sh install`，就回到切换那一刻的 SQLite（切换之后在 PostgreSQL 上新产生的东西
不会带回来；库的容器停着不用，`docker rm -f agentlab-postgres` 可删）。

**首次在 Linux 上切换前的检查。** 第一次在一台机器上 `install --postgres` 或 `migrate-to-postgres` 之前，花几分钟看一遍
（以缺省的 `./data` 为例，在包目录里执行）：

- **docker 版本**：`docker version --format '{{.Server.Version}}'`，要 20.10 以上（健康检查、自建网络、`--stop-timeout` 都要用）；
  运行 deploy.sh 的用户在 `docker` 组里（`id -nG` 里有 docker），否则整条命令前加 sudo。
- **属主**：`ls -lnd data` 第三列应是 `10001`（deploy.sh 改的）。数据目录不要放在 NFS、SMB 这类不认 uid 的挂载上：属主改不过去，
  服务写不进去。
- **权限**：切换（或第一次起库）之后看一眼——`ls -ld data/.postgres` 应是 `drwx--x--x`（0711），`cat data/.postgres/state`
  不用 sudo 就能读到（deploy.sh 在宿主机上直接读它，读不到时会退回去用容器读，只是慢一点）；`sudo ls -ln data/.postgres`
  里 `password`、`pgpass` 是 `-rw-------`、属主 10001；`sudo ls -ld data/backups` 是 `drwx------`。
- **磁盘空间**：切换时数据目录里要放得下两份库（`backups/sqlite/` 里的一份备份，加上导入时的临时快照，用完就删），
  另外 docker 的存储目录要放得下 PostgreSQL 的镜像（约 0.7 GB）和库本身（大致和 SQLite 的库一样大，索引多的表会大一些）。
  看大小：`sudo du -sh data/agentlab.db data/checkpoints.db`；看剩余：`df -h data` 和
  `df -h "$(docker info --format '{{.DockerRootDir}}')"`。
- **整目录备份**：切换会先自动备份一份库，但还是建议先按「数据和备份」停服务整份打包一次数据目录。切换期间服务是停着的，
  时长大致是导入的时间（每秒几千到几万行，看机器）。

**卸载。** `./deploy.sh uninstall` 连库的容器和网络一起删，数据卷和数据目录都留着；同一个数据目录再 `./deploy.sh install`
就接着用原来的库。确定不要了：`docker volume rm agentlab-pgdata`（不可恢复）。

### 外接 PostgreSQL

库在你自己的 PostgreSQL 上（要先建好），在 `agentlab.env` 里加一行再 `./deploy.sh install`：

```bash
AGENTLAB_DATABASE_URL=postgresql+asyncpg://agentlab:<密码>@<数据库地址>:5432/agentlab
```

容器里的 `127.0.0.1` 是容器自己：数据库在别的机器上就写那台的地址；就在这台机器上，写宿主机在 docker 网桥上的地址
（一般是 `172.17.0.1`），并让 PostgreSQL 监听这个地址、`pg_hba.conf` 放行容器的网段。执行断点也进同一个库。
表结构和默认数据同样在每次启动时自动补齐，库暂时连不上时同样按退避重试。`agentlab.env` 末尾「外接 PostgreSQL 与多进程（可选）」
一段列着这几项（地址、断点库、PgBouncer、工作进程、连接池、启动时等库），都注释着，要用的去掉行首的 `#`；它们和别的 `AGENTLAB_*`
一样经 `--env-file` 原样进容器。

已经在用 SQLite 的，先把数据导过去（同一个镜像里带着导入命令；`--dry-run` 只核对、不写）：

```bash
./deploy.sh stop
sudo tar -czf ../agentlab-data-$(date +%Y%m%d).tar.gz data        # 先备份
docker run --rm --user 10001:10001 -v "$(pwd)/data:/data" --env-file agentlab.env --entrypoint python \
  agentlab:<版本> -m app.db copy-to-postgres /data --dry-run
docker run --rm --user 10001:10001 -v "$(pwd)/data:/data" --env-file agentlab.env --entrypoint python \
  agentlab:<版本> -m app.db copy-to-postgres /data
./deploy.sh install                                                # agentlab.env 里已经有 AGENTLAB_DATABASE_URL
```

导入结束时逐表打印来源和目标的行数、断点的条数，对不上的列出来、退出码非零；重跑只补没有的行，不重复、不覆盖。
导完 `data/agentlab.db` 可以留着当回退：把那一行删掉、重新部署就回到 SQLite（导入之后在 PostgreSQL 上新产生的东西不会回来）。
外接的库 deploy.sh 不备份，自己用 `pg_dump` 备份。

### 多进程

**多进程。** 换成 PostgreSQL 之后可以在容器里起几个工作进程：`agentlab.env` 里加 `AGENTLAB_WORKERS=4`（缺省 1）再
`./deploy.sh upgrade`。几个进程共用一个端口，各自在库里登记、续租约；一个进程没了，别的进程在租约到期后接手它留下的运行、
问数据和后台任务（规矩见仓库 README「多进程」）。SQLite 时设大于 1 会启动失败（照实报错：SQLite 只支持单进程）。
几台机器、几个容器指着同一个库也行，但数据目录要放在共享存储上、每台挂在同一个路径，限制见仓库 README「多进程」。

内置 PostgreSQL 是直连（中间没有 PgBouncer），多进程时什么都不用另设（不用 `AGENTLAB_DATABASE_DIRECT_URL`；设了它、或者
`AGENTLAB_DB_PGBOUNCER=1`，deploy.sh 照实拦下）。库还是 SQLite 时 `AGENTLAB_WORKERS` 大于 1，deploy.sh 在起容器之前就拦下。新旧版本的进程不能同时
连着一个库（代码里拦着：新版本发现库里还有活着的旧版本进程就拒绝启动）。`./deploy.sh upgrade` 就是按这个顺序做的：先停掉旧容器
（里面的全部工作进程一起停）、备份，再起新版本；旧进程要是被强杀、登记还没过期，新版本启动时会等它过期（最多半分钟左右）。
同一个库还接着别处的 AgentLab 进程（另一台机器、另一个容器）的，升级前先把它们也停掉。

## 升级

把新版本的离线包拷到机器上，解到一个新目录，然后：

```bash
cd agentlab-<新版本>-linux-__AGENTLAB_ARCH__
cp ../agentlab-<旧版本>-linux-__AGENTLAB_ARCH__/agentlab.env .   # 沿用原来的配置
# 数据目录是相对路径（./data）的话，把 agentlab.env 里的 DATA_DIR 改成原来那个目录的绝对路径
./deploy.sh upgrade
```

`upgrade` 导入新镜像、用新镜像替换容器，数据目录不动。deploy.sh 会检查新配置指向的数据目录和
正在用的是不是同一个，不是就停下来提醒，不会让新版本悄悄从一个空库开始。换之前先停旧服务、自动备份一次库
（见「数据和备份」），备份没做成就把旧服务起回来、不升级。新版本启动时自动补表结构和默认数据，不用手动迁移。
用内置 PostgreSQL 的，新包里的 `POSTGRES_IMAGE` 换了小版本时库的容器跟着换（数据卷不动）。

新容器没通过健康检查时，原来的容器会保留为 `agentlab-previous`，屏幕上会打印回退命令。
旧镜像也留着，确认新版本没问题后可以 `docker rmi agentlab:<旧版本>` 删掉。

从没有「应用」的版本升上来时，新版本第一次启动会自动整理一遍（都只做一次，重复启动不改东西）：存量的待审批补建成
待办；自定义工具的请求头、MCP 的环境变量里的明文加密；记级别之前发布过、有正式运行凭据的老版本补记为已发布；
指向本机或内网的 http MCP 服务自动登记成开了内网访问的端点并绑定。升级前照常备份数据目录。

## 查看状态和日志

```bash
./deploy.sh status        # 容器状态、健康检查、数据目录、代码沙箱、访问地址
./deploy.sh logs          # 最后 200 行
./deploy.sh logs -f       # 持续跟随，Ctrl-C 退出
./deploy.sh logs --tail 1000
./deploy.sh logs --postgres  # 内置 PostgreSQL 的日志
```

日志由 docker 自动轮转（单个 10 MB、保留 3 个），不会塞满存储卡。

## 停止和卸载

```bash
./deploy.sh stop                        # 停止（开机也不会自动起来）
./deploy.sh start                       # 再启动
./deploy.sh restart                     # 重启服务（内置 PostgreSQL 不跟着重启）
./deploy.sh restart --postgres          # 连内置 PostgreSQL 一起重启
./deploy.sh uninstall                   # 删除容器，保留数据和镜像
./deploy.sh uninstall --purge-image     # 连镜像一起删（PostgreSQL 的镜像不删，别的东西可能也在用）
```

内置 PostgreSQL 时这几个命令按顺序管两个容器（起的时候先起库，停的时候先停服务；`restart` 不加 `--postgres` 只动服务），
卸载连库的容器和网络一起删。
卸载**永远不删数据**。确定不要了再手动删除数据目录（不可恢复）：`sudo rm -rf <数据目录>`；内置 PostgreSQL 的数据卷另删：
`docker volume rm <CONTAINER_NAME>-pgdata`。

改了 `agentlab.env` 之后运行 `./deploy.sh install` 让新配置生效（`restart` 不会重读配置）。

## 安全提示

- **默认不开登录。** 不开登录时，同一网络里打得开页面的人都能用它：跑工作流、花你配的模型额度、
  读你接入的数据源、在沙箱里执行代码，审批记的是各自填的署名、未经核实。只在信得过的网络里这样用。
  多人使用请开登录：`agentlab.env` 里设 `AGENTLAB_AUTH_MODE=local`（账号密码）或 `oidc`（单点登录），
  `./deploy.sh install` 生效，第一次打开登录页时建第一个管理员。角色、权限和遮罩的边界见仓库 README 的「身份与权限」。
- 忘了管理员密码：`docker exec -it agentlab python -m app.auth reset-admin`（不给 `--password` 时随机生成并打印一次）。
- 用防火墙限制能访问端口的来源。注意 Docker 发布的端口会绕过 ufw 的默认规则，要么把 `BIND_ADDR`
  设成具体网卡的地址或 `127.0.0.1`，要么在 `DOCKER-USER` 链里加规则。
- 放在反向代理后面时，设 `AGENTLAB_PUBLIC_URL` 为对外地址（OIDC 回调和来源核对按它认）；代理终结了 https 的，
  设 `AGENTLAB_AUTH_COOKIE_SECURE=true`。
- API Key 加密存在数据目录里，密钥也在数据目录里：数据目录的备份要和 Key 一样保管好。

## 代码沙箱

工作流里的代码节点、界面上的沙箱都在服务端执行代码。AgentLab 按下表从上往下，挑第一种能用的：

| 沙箱 | 条件 | 隔离效果 |
| --- | --- | --- |
| microVM | 宿主机有 KVM（默认 `ENABLE_MICROVM=auto`，自动检测） | 每段代码在独立内核的虚拟机里执行：看不到数据目录、密钥和别的进程，内存上限真正生效 |
| bubblewrap | `ENABLE_BWRAP=1` | 共享内核的隔离环境：看不到数据目录和别的进程，默认断网 |
| local | 前两种都用不了 | 容器里的普通子进程。隔离边界只有容器本身：代码伤不到宿主机，但和 AgentLab 同一个容器、同一个用户，读得到数据目录里的数据库和加密密钥、读得到传进容器的 API Key、能联网 |

实际用的是哪一种，deploy.sh 部署完会打印「代码沙箱：……」，`./deploy.sh status` 和界面「设置 → 运行环境」里
也看得到。只运行你信得过的工作流；完全不需要执行代码的话，在 `agentlab.env` 里设 `AGENTLAB_SANDBOX_BACKEND=off`。

### microVM

要宿主机是支持 KVM 的 Linux：物理机、树莓派 5 这类 64 位 ARM 单板机、开了嵌套虚拟化的云主机。
Mac、Windows 上的 Docker Desktop 和多数云主机没有 KVM，用不了。检查：`ls -l /dev/kvm` 能看到这个设备就行。

运行时和沙箱镜像（`python:3.12-slim`）已经打在镜像里，部署时不用联网。`./deploy.sh install` 会这样处理：

1. 本机没有 `/dev/kvm`：不启用，照常部署；
2. 有 `/dev/kvm`：先用一个临时容器真起一台 microVM、执行一行代码。起得来，就给正式容器加上
   `--device /dev/kvm` 和 `/dev/kvm` 所属的组（容器里的服务以 uid 10001 运行，要靠这个组读写它）；
   起不来，打印原因，照常部署但不启用。

`agentlab.env` 里的 `ENABLE_MICROVM`：`auto` 按上面处理（默认），`1` 起不来就报错停下，`0` 不启用。

已知缺口：代码节点关掉网络时，HTTP/HTTPS 和域名解析都会断，但 UDP/53 仍然放行，存在经 DNS 外泄数据的可能。

### bubblewrap

把 `agentlab.env` 里的 `ENABLE_BWRAP` 改成 `1`，运行 `./deploy.sh install`。microVM 已经启用时用不上它。
Docker 默认权限下 bubblewrap 无法创建命名空间（默认的系统调用过滤不允许），所以
deploy.sh 会给容器加 `--security-opt seccomp=unconfined`、`--security-opt systempaths=unconfined`、
`--security-opt apparmor=unconfined`。代价是整个容器少了系统调用过滤和对 `/proc`、`/sys` 部分路径的遮蔽，
内核暴露给容器的面变大（AgentLab 自己仍以非 root 运行）。这组权限在 Docker Desktop（arm64）上实测生效。
Ubuntu 24.04 默认还限制非特权 user namespace：开了之后「设置 → 运行环境」里仍显示 local 的话，
要在宿主机上执行 `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`，这会放宽整台机器的限制，自行权衡。

## 常见问题

**端口被占用。** deploy.sh 报「端口 8000 已被别的程序占用」，或 docker 报 `port is already allocated` /
`address already in use`：改 `agentlab.env` 里的 `HOST_PORT`，再 `./deploy.sh install`。
看是谁占着：`sudo ss -ltnp 'sport = :8000'`。

**没有权限。** 报 `permission denied while trying to connect to the Docker daemon socket`：
用 `sudo ./deploy.sh`，或者 `sudo usermod -aG docker $USER` 后重新登录。
数据目录建不了：用 sudo，或者把 `DATA_DIR` 改到有写权限的位置。

**架构不对。** 报「架构不一致」或容器日志里有 `exec format error`：这个包是 `linux/__AGENTLAB_ARCH__`，
和 `uname -m` 对不上。在打包的机器上按目标机的架构重新打一份
（`./scripts/package-docker.sh --platform linux/arm64` 或 `linux/amd64`）。ARM 单板机要装 64 位系统。

**健康检查不过。** deploy.sh 会打出容器最后 60 行日志，先看那里。常见原因：

- 数据目录写不进去（日志里有 `Permission denied` / `unable to open database file`）：确认 `DATA_DIR`
  没指到只读的位置，重新运行 `./deploy.sh install`，它会修正属主；
- 机器太慢，启动超过 3 分钟：`./deploy.sh status` 看看，变成 `healthy` 就是好了；
- `agentlab.env` 里的值带了引号、或者注释写在了值后面：去掉。

**`sha256sum -c` 报 image.tar FAILED。** 包在拷贝中损坏了，重新拷一份。

**有 `/dev/kvm`，microVM 却没启用。** deploy.sh 会打印试启动失败的原因。常见的几种：

- 权限类报错（`Permission denied`、`Operation not permitted`）：先确认 `ls -l /dev/kvm` 的属组是 `kvm` 之类的普通组。
  仍然不行的话，可能是 Docker 默认的系统调用过滤拦住了，可以试 `ENABLE_BWRAP=1`（它会放开这项过滤）再 `./deploy.sh install`；
- 云主机上报虚拟化相关的错：机型不支持或没打开嵌套虚拟化，这种情况用不了 microVM；
- 想让它起不来就停下、而不是悄悄不用：把 `ENABLE_MICROVM` 设成 `1`。

**启用 microVM 后，机器重启时容器没起来。** 容器启动时要用到 `/dev/kvm`，开机时 kvm 模块没加载，
Docker 就启动不了这个容器。`ls -l /dev/kvm` 确认设备在，再运行 `./deploy.sh install`，它会重新检测。
