[English](../en/configuration.md) | 简体中文

# 配置

这篇讲配置写在哪里、怎么接入模型、常用的环境变量和界面设置，以及怎么选沙箱后端、启用 microVM。给管理员和运维看。数据库和多进程的配置见 [PostgreSQL 与多进程](postgresql-and-scaling.md)。

## 配置写在哪里

- **环境变量**。开发时也可以写在仓库根目录的 `.env` 里，环境变量优先。`.env.example` 列着应用库、连接池、PgBouncer、多进程和开发用 PostgreSQL 容器这几组。
- **Docker 部署**写在 `agentlab.env` 里（见 [deploy/README.md](../../deploy/README.md)）。
- **界面里的「设置」**。有两项环境变量优先：`AGENTLAB_AUTH_MODE` 优先于「设置 → 身份」；`AGENTLAB_PUBLIC_URL` 优先于「设置 → 通用」的对外访问地址。

## 模型接入

在「设置 → 模型接入」里配置。支持 Anthropic、OpenAI、任意 OpenAI 兼容服务（DeepSeek、Kimi、通义、智谱、硅基流动、Ollama 等），以及内置的 Mock。

- 启动时，环境变量里有 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `OPENAI_API_KEY` 的，自动导入为 provider。没配 Key 的 provider 也会退回环境变量里的密钥。
- API Key 经 Fernet 加密落库，只向前端返回掩码。
- Mock 不用 API Key：产出流式回复、工具调用和符合 schema 的结构化数据，编排链路（画布高亮、审批中断、沙箱执行）完整走一遍。但 Mock 不写引用标记，报告里的数字全标「无证据」。
- 成本按目录价折算。
- 部分中转网关只认 `Authorization: Bearer`，额外带 `x-api-key` 就拒绝：在 provider 设置里勾选「用 Authorization: Bearer 认证」。
- Claude 4.6 之后的模型默认启用思考，`max_tokens` 太小会「思考耗尽额度，正文为空」，见[常见问题](faq.md)。

其他用到模型的地方：起草模型在「设置 → 偏好设置」里选（见[数据目录](data-catalog.md#起草)）；知识库的向量模型可以接本地端点（见[工作流](workflows.md#知识库记忆与-skill)）。

## 对外访问地址

AgentLab 放在反向代理后面、或者要给外部系统回调时，设对外访问地址。

- 「设置 → 通用」里只有一个「对外访问地址」。`AGENTLAB_PUBLIC_URL` 优先，写错了启动就报错。
- OIDC 回调按这个地址拼：`<对外地址>/api/auth/oidc/callback`，要和身份提供方那边登记的一致。
- HSTS 交给反向代理。

## 常用环境变量

| 变量 | 缺省 | 作用 |
|---|---|---|
| `AGENTLAB_DATA_DIR` | 仓库下的 `data/` | 数据目录 |
| `AGENTLAB_PORT` | 8000 | 后端端口 |
| `AGENTLAB_WEB_PORT` | 5273 | 开发时的前端端口 |
| `AGENTLAB_CONDA_ENV` | `agentlab` | `dev.sh` 用的 conda 环境名 |
| `AGENTLAB_AUTH_MODE` | `off` | 登录方式：`off` / `local` / `oidc` |
| `AGENTLAB_PUBLIC_URL` | — | 对外访问地址 |
| `AGENTLAB_CORS_ORIGINS` | — | 允许带 `Origin` 发请求的其他地址 |
| `AGENTLAB_AUTH_SESSION_IDLE_HOURS` | 12 | 会话闲置多久过期（小时） |
| `AGENTLAB_AUTH_SESSION_MAX_DAYS` | 7 | 会话最长多久过期（天） |
| `AGENTLAB_AUTH_COOKIE_SECURE` | 按请求是不是 https | 强制 cookie 带 Secure |
| `AGENTLAB_FRAME_ANCESTORS` | —（只允许本站嵌入） | 允许嵌整站的同站门户 |
| `AGENTLAB_EMBED_ORIGINS` | — | 允许嵌分享页的站点 |
| `AGENTLAB_SCHEDULER` | 开 | 调度开关，设成 0 关闭 |
| `AGENTLAB_SCHEDULER_TICK_S` | 15 | 调度循环一拍的间隔（秒） |
| `AGENTLAB_MAX_CONCURRENT_RUNS` | 20 | 全站同时执行的运行上限 |
| `AGENTLAB_MAX_RUN_SECONDS` | 600 | 单次执行的时限（秒） |
| `AGENTLAB_MODEL_TIMEOUT_SECONDS` | 300 | 单次模型调用多久没有输出算超时（秒） |
| `AGENTLAB_MAX_GRAPH_STEPS` | 200 | 图的步数上限 |
| `AGENTLAB_MAX_AGENT_STEPS` | — | agent 步数的封顶 |
| `AGENTLAB_SANDBOX_BACKEND` | 自动选择 | 强制指定沙箱后端 |
| `AGENTLAB_MICROVM_IMAGE` | `python:3.12-slim` | microVM 用的镜像 |
| `AGENTLAB_MICROVM_IDLE_SECONDS` | — | 闲置 VM 多久回收 |
| `AGENTLAB_HTTP_TOOL_FAKE_IP_RANGES` | — | 本机代理 fake-ip 的网段，如 `198.18.0.0/15` |
| `AGENTLAB_HTTP_TOOL_DOH_URLS` | — | 核实 fake-ip 用的 DoH 地址，逗号分隔 |

应用库、断点库、连接池、PgBouncer 和多进程的变量见 [PostgreSQL 与多进程](postgresql-and-scaling.md)。

## 界面里的设置

| 位置 | 内容 |
|---|---|
| 设置 → 模型接入 | provider 和密钥 |
| 设置 → 偏好设置 | 运行默认值（默认记忆作用域、默认知识库、危险工具默认需要人工审批）；证据裁判（裁判模型和各项上限）；核查（核查模型，双盲复算、反方审稿、审批质询的开关和上限，每日金额上限，见[复核套件](evidence.md#复核套件)）；评测的并发、每题时限、单次和每日金额；运行并发（管理员）；口径账本（自动按口径出具的开关和每日上限、口径比对时限、召回上限 `ledger.recall_limit`，见[口径账本](evaluation-and-ledger.md#口径账本)）；起草模型；应用（每个应用每天的按需运行上限、让使用者建个人应用、允许建分享链接）；界面主题 |
| 设置 → 通用 | 对外访问地址 |
| 设置 → 运行环境 | 沙箱候选和可用性、当前选中项和选择原因；时限等当前值 |
| 设置 → 身份 | 登录方式；给使用者开启问数据 |
| 设置 → 通知 | 渠道订阅；邮件服务（SMTP） |
| 设置 → 访问密钥 | 服务账号、个人访问密钥的开关、接口说明 |
| 设置 → 审计 | 审计记录 |

其他设置项：`notify.digest_at`（每天待办汇总的时间），以及 agent 的令牌预算、金额预算和步数。

## 沙箱后端

「设置 → 运行环境」列出全部候选和它们的可用性，并标出当前选中项和「选择原因」。自动选择的顺序：

1. 装好 microVM 依赖、镜像已缓存（Linux 上还要拿得到 `/dev/kvm`）时，选 `microvm`。
2. 否则 macOS 用 `seatbelt`；Linux 装了 `bwrap`（`apt install bubblewrap`）后用 `bubblewrap`。
3. 都不可用时降级到 `local`。**这条路径不含任何访问控制**，界面明确标红。

- 装了 `bwrap` 还要能真正创建命名空间才算可用：启动后第一次选择时，会实际起一次 `bwrap` 探测。Docker 默认权限、Ubuntu 24.04 的 AppArmor 限制下探测会失败，`bubblewrap` 被跳过，「选择原因」里写明。
- 用 `AGENTLAB_SANDBOX_BACKEND` 强制指定；也可以在代码节点上单独选隔离档位（strict = microVM，fast = 系统沙箱）。
- 后端选定沙箱后，整个进程都沿用。装好 microVM 之后，要重启服务才会重新选。

两档沙箱各自保证什么、有什么局限，见[身份、权限与安全](identity-and-security.md#代码沙箱)。

### 启用 microVM

要做三项准备，**都只做一次**；之后日常启动不变：

1. Python 包：用 `environment.yml` 建环境时已经装上（含 `[microvm]` extra）。
2. 运行时（约 50MB，放在 `~/.microsandbox`）：每台机器一次。
3. OCI 镜像（实测约 55 秒）：每个镜像一次。

第 2、3 项手动执行。装完用 `doctor` 检查本机的虚拟化条件，再重启 `dev.sh`：

```bash
conda run -n agentlab python -c "exec('import asyncio, microsandbox as m\nasync def main():\n    await m.install()\nasyncio.run(main())')"
conda run -n agentlab microsandbox pull python:3.12-slim   # 默认镜像；换了 AGENTLAB_MICROVM_IMAGE 就拉那个
conda run -n agentlab microsandbox doctor
```

- **镜像要先拉**。auto 只选镜像已经缓存的 microVM（免得第一次跑代码卡一分钟）；而镜像只在 microVM 里跑代码时才会拉。不手动拉，auto 永远选不到它。不想等，也可以设 `AGENTLAB_SANDBOX_BACKEND=microvm`，第一次跑代码时再拉。
- 拉镜像报 `registry error: error sending request for url (https://index.docker.io/...)`，是连不上 Docker Hub。msb 认 `HTTPS_PROXY` / `ALL_PROXY`，可以给它指一个代理：`HTTPS_PROXY=http://<代理地址>:<端口> microsandbox pull python:3.12-slim`。或者从能连上的镜像源拉（写完整名字，如 `<镜像源>/library/python:3.12-slim`），再把 `AGENTLAB_MICROVM_IMAGE` 设成同一个名字。
- `install` 要在正在跑的事件循环里调用。写成 `asyncio.run(m.install())` 会先在循环外调用它，报 `RuntimeError: no running event loop`，所以上面包了一层 `async def`。
- Linux 要有 `/dev/kvm`，当前用户还得有权限。doctor 报「/dev/kvm is not accessible by your user」时：

```bash
sudo usermod -aG kvm $USER               # 永久生效，重新登录后才算数
sudo setfacl -m u:$USER:rw /dev/kvm      # 当前会话立即可用
```

- 也可以直接 `microsandbox doctor --fix`。改完再跑一次 doctor，应当全是 ✓。
- doctor 全绿不等于第 2 项装好了：它只查 wheel 里自带的 msb 和 libkrunfw；AgentLab 用 `microsandbox.is_installed()` 判断，看的是 `~/.microsandbox`。所以上面那条安装命令照样要跑。
- 三项都落在磁盘上，重启机器或重开终端不用重来。「设置 → 运行环境」里 `microvm` 条目的 `warm` 为 true，就表示三项齐备，auto 会自动升到 `microvm`。
- 镜像用 `AGENTLAB_MICROVM_IMAGE` 更换；闲置 VM 的回收时间由 `AGENTLAB_MICROVM_IDLE_SECONDS` 控制。
