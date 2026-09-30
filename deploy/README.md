# AgentLab 离线部署说明

版本 **__AGENTLAB_VERSION__** · 架构 **linux/__AGENTLAB_ARCH__**

这个包在一台只装了 Docker Engine、不能联网的 Linux 机器上部署 AgentLab。
不需要 docker compose，不需要 Python、Node，也不需要联网。

## 包里有什么

| 文件 | 用途 |
| --- | --- |
| `image.tar` | Docker 镜像（`docker save` 导出），镜像名 `agentlab:__AGENTLAB_VERSION__` |
| `deploy.sh` | 一键部署脚本：安装、启动、停止、升级、卸载 |
| `agentlab.env` | 配置：端口、监听地址、数据目录、模型 Key、代理、沙箱 |
| `README.md` | 本说明 |
| `SHA256SUMS` | 校验和。拷完先校验：`sha256sum -c SHA256SUMS`（改过 `agentlab.env` 之后它那一行会对不上，正常） |

## 前提条件

- Ubuntu 24.04（别的 64 位 Linux 发行版一般也行），已装好 Docker Engine 并在运行；
- **架构要一致**：这个包是 `linux/__AGENTLAB_ARCH__`。在目标机上运行 `uname -m`：
  `aarch64` 对应 arm64（ARM 单板机要装 64 位系统），`x86_64` 对应 amd64。不一致的镜像跑不起来，
  deploy.sh 会在导入前就报出来；
- 磁盘：镜像导入后约 0.6～0.8 GB（看 Docker 用的存储方式），另留数据目录的空间；
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
丢了 `.secret_key`，存进去的 API Key 就解不开了。

deploy.sh 会把数据目录的属主改成容器里的用户（uid 10001），所以备份、删除要用 sudo。
停下来备份最稳妥（以默认的 `./data` 为例，在包目录里执行）：

```bash
./deploy.sh stop
sudo tar -czf ../agentlab-data-$(date +%Y%m%d).tar.gz data
./deploy.sh start
```

恢复：停掉容器，把备份解回 `DATA_DIR`，再 `./deploy.sh start`。

## 升级

把新版本的离线包拷到机器上，解到一个新目录，然后：

```bash
cd agentlab-<新版本>-linux-__AGENTLAB_ARCH__
cp ../agentlab-<旧版本>-linux-__AGENTLAB_ARCH__/agentlab.env .   # 沿用原来的配置
# 数据目录是相对路径（./data）的话，把 agentlab.env 里的 DATA_DIR 改成原来那个目录的绝对路径
./deploy.sh upgrade
```

`upgrade` 导入新镜像、用新镜像替换容器，数据目录不动。deploy.sh 会检查新配置指向的数据目录和
正在用的是不是同一个，不是就停下来提醒，不会让新版本悄悄从一个空库开始。

新容器没通过健康检查时，原来的容器会保留为 `agentlab-previous`，屏幕上会打印回退命令。
旧镜像也留着，确认新版本没问题后可以 `docker rmi agentlab:<旧版本>` 删掉。

## 查看状态和日志

```bash
./deploy.sh status        # 容器状态、健康检查、数据目录、访问地址
./deploy.sh logs          # 最后 200 行
./deploy.sh logs -f       # 持续跟随，Ctrl-C 退出
./deploy.sh logs --tail 1000
```

日志由 docker 自动轮转（单个 10 MB、保留 3 个），不会塞满存储卡。

## 停止和卸载

```bash
./deploy.sh stop                        # 停止（开机也不会自动起来）
./deploy.sh start                       # 再启动
./deploy.sh restart                     # 重启
./deploy.sh uninstall                   # 删除容器，保留数据和镜像
./deploy.sh uninstall --purge-image     # 连镜像一起删
```

卸载**永远不删数据**。确定不要了再手动删除数据目录（不可恢复）：`sudo rm -rf <数据目录>`。

改了 `agentlab.env` 之后运行 `./deploy.sh install` 让新配置生效（`restart` 不会重读配置）。

## 安全提示

- **AgentLab 没有登录。** 同一网络里打得开页面的人都能用它：跑工作流、花你配的模型额度、
  读你接入的数据源、在沙箱里执行代码。只在信得过的网络里开。
- 用防火墙限制能访问端口的来源。注意 Docker 发布的端口会绕过 ufw 的默认规则，要么把 `BIND_ADDR`
  设成具体网卡的地址或 `127.0.0.1`，要么在 `DOCKER-USER` 链里加规则。
- 需要多人使用时，把 `BIND_ADDR` 改成 `127.0.0.1`，前面另配一个带登录的反向代理。
- API Key 加密存在数据目录里，密钥也在数据目录里：数据目录的备份要和 Key 一样保管好。

## 代码沙箱

工作流里的代码节点、界面上的沙箱都在服务端执行代码。容器里能用哪种隔离，实测结果如下：

| 容器权限 | 实际使用的沙箱 | 隔离效果 |
| --- | --- | --- |
| 默认（`ENABLE_BWRAP=0`） | local：容器里的普通子进程 | 隔离边界只有容器本身。代码伤不到宿主机，但和 AgentLab 同一个容器、同一个用户，读得到数据目录里的数据库和加密密钥、读得到传进容器的 API Key、能联网 |
| `ENABLE_BWRAP=1` | bubblewrap | 代码看不到数据目录、看不到别的进程，默认断网 |

默认权限下 bubblewrap 无法创建命名空间（Docker 默认的系统调用过滤不允许），AgentLab 会自动退回 local，
「设置 → 运行环境」里的「选择原因」会写明。只运行你信得过的工作流；完全不需要执行代码的话，
在 `agentlab.env` 里设 `AGENTLAB_SANDBOX_BACKEND=off`。

要用 bubblewrap：把 `agentlab.env` 里的 `ENABLE_BWRAP` 改成 `1`，运行 `./deploy.sh install`。
deploy.sh 会给容器加 `--security-opt seccomp=unconfined`、`--security-opt systempaths=unconfined`、
`--security-opt apparmor=unconfined`。代价是整个容器少了系统调用过滤和对 `/proc`、`/sys` 部分路径的遮蔽，
内核暴露给容器的面变大（AgentLab 自己仍以非 root 运行）。这组权限在 Docker Desktop（arm64）上实测生效。
Ubuntu 24.04 默认还限制非特权 user namespace：开了之后「设置 → 运行环境」里仍显示 local 的话，
要在宿主机上执行 `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`，这会放宽整台机器的限制，自行权衡。

microVM（独立内核）需要 `/dev/kvm` 和额外下载的运行时与镜像，离线包默认不支持。

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
