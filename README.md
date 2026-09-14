# AgentLab · 可视化 Agent 编排实验台

在画布上拖节点、连线，编出一条 agent 工作流，然后**看着它跑**——哪个节点在执行、
模型正在吐什么字、调了什么工具、沙箱里的代码返回了什么、在哪一步停下来等你拍板。

不是 demo，是一套能真用的本地实验环境：多模型接入、隔离沙箱、工具链与 MCP、
长期记忆与知识库、人工介入、断点续跑、结构化校验、运行追踪与成本核算。

```
┌──────────┬──────────────────────────────────┬─────────────────┐
│ 节点库    │            画布                   │  运行 / 属性     │
│          │   ┌────┐   ┌────┐   ┌────┐        │  ▸ 输入表单      │
│ 模型      │   │输入│──▶│Agent│──▶│成果│       │  ▸ 实时时间线    │
│ 执行      │   └────┘   └─┬──┘   └────┘        │  ▸ 人工审批      │
│ 控制      │             ▼ 工具调用             │  ▸ 成果 / 用量   │
│ 上下文    │                                   │                 │
│ 把关      │                                   │                 │
└──────────┴──────────────────────────────────┴─────────────────┘
```

## 快速开始

需要 conda（[miniforge](https://conda-forge.org/miniforge/) 即可）、Node 20+、pnpm。
**不需要 Docker** —— 代码沙箱用操作系统自带的隔离原语，可选升级到 microVM。

```bash
conda env create -f environment.yml   # 首次：建 agentlab 环境并装依赖
./scripts/dev.sh                      # 之后：一条命令拉起前后端
```

`dev.sh` 会自己找到 `agentlab` 这个 conda 环境；环境不存在时它会照
`environment.yml` 建一个。想用别的环境名就设 `AGENTLAB_CONDA_ENV`。

打开 http://localhost:5273 。

**不配任何 API Key 也能直接玩**——内置了一个 Mock provider，会产出假的流式回复、
假的工具调用、符合 schema 的假结构化数据，整条编排链路（包括画布高亮、审批中断、
沙箱执行）都会真实走一遍。想要真结果，去「设置 → 模型接入」填 Key 即可。

启动时如果环境里已有 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `OPENAI_API_KEY`，
会自动导入成 provider，不用手填。

内置 7 个模板，从「最小问答」到「多 Agent 协作」，既是能直接跑的例子，也是各类节点用法的活文档。

## 能做什么

| 能力 | 怎么体现 |
|---|---|
| **可视化编排** | 15 种节点拖拽连线；运行时节点实时高亮、边上数据流动画、卡片里直接看到流式 token |
| **多模型接入** | Anthropic / OpenAI / 任意 OpenAI 兼容服务（DeepSeek、Kimi、通义、智谱、硅基流动、Ollama…）+ Mock |
| **工具链** | 15 个内置工具 + 自定义工具（HTTP 模板 / 沙箱 Python）+ MCP server 接入 |
| **代码沙箱** | 两档：系统沙箱（Seatbelt / bubblewrap，冷启动 ~25ms）或 microVM（独立 Linux 内核，内存限额真生效）。断网、家目录不可读、只有工作区可写、超时强杀 |
| **人工介入** | 图暂停并落盘，人在界面上批准/驳回/改稿，从断点继续——进程重启也不丢 |
| **记忆与知识库** | 跨运行的长期记忆（自动去重）+ 文档知识库（向量 + BM25 混合检索，可调配比） |
| **Skill 管理** | 把「怎么做事」抽成可复用的方法论，挂到节点上注入 system prompt |
| **自然语言编排** | Copilot：一句话生成或改写整张图，自动排版 + 校验 |
| **结构化成果** | JSON Schema 校验，不合格把错误喂回模型自动返工 |
| **追踪与成本** | 每步事件全量落库，可回放；逐节点耗时、token、按目录价折算的美元成本 |
| **三档出具** | 口径卡（受控指标集）+ 出具契约：叙述里每个数字回指指标集，formal / degraded / withheld 三档判定，缺数据声明与口径版本随成果印出 |
| **正式 / 探索分级** | 正式运行只从已发布的不可变版本发起（按图哈希钉死），画布试跑自动标探索性；受管（governed）发布过治理 lint |
| **模板生长** | 探索性运行可一键提取为草稿模板（只保留实际走过的路径）；探索问题聚类提示"该晋升为固定节了"；工具白名单衰减报表 |

## 受限动态编排：模板定骨架、agent 填关节

工业场景的大多数任务步骤是已知的（接单报价、周报、体检），让模型每次现场重新规划
是在重复发明已知答案，而且不可复现就不可验收。AgentLab 的引擎是编译式的——运行时
模型改不了图，它的自由被结构性地关在 agent 节点内部和 branch 决策里。在此之上：

- **模板层**：正式运行只认已发布的不可变版本（`WorkflowVersion` + 图哈希）。
  发布后改画布不影响它。governed 级发布要过治理 lint：禁 supervisor（全动态属于
  探索层）、方法卡必须钉版本、agent 不得关闭全部审批、output 必须有出具契约。
- **关节层**：agent 的工具白名单、分支的语义分类、缺数据时降档——有边界的自由。
  白名单衰减报表（`/api/governance/tool-usage`）把"授了权没用过"的工具摆上台面。
- **探索层**：画布试跑、临时图、追问，结果自动标探索性、不进正式归档。跑通的路径
  用「提取模板」沉淀为草稿（只保留实际执行的节点，剪掉没走的分支），人审后发布。
  探索问题聚类提示哪些追问该长成模板里的固定一节。

**出具链路**（模板⑧就是完整示范）：取数（固定，快照落工件库）→ 口径卡（metrics
节点，所有算术在这里确定性发生，带口径名和版本）→ 叙述（LLM，只许引用指标清单）
→ 出具契约（从叙述抽出所有数字逐个回指指标集，支持千分位/百分数↔比率/万亿后缀/
按书写精度的舍入容差；日期、枚举序号、引用标记不参与）。三档判定印在成果上。
实测：Opus 5 写的正文数字全部回指，但结尾"（约 100 字）"里的 100 被抓住降档——
修的是 prompt 而不是白名单，这正是校验器该有的行为。

钉住的方法卡出现新版本时，正式运行会被拒绝启动，直到模板声明升版处置：
`recompute`（回算历史）/ `dual`（双印）/ `incomparable`（标注不可比），没有默认值
——口径变更最容易静默翻车的地方是"数看起来都对，只是和上周不可比"。

每次运行的完整证据在内容寻址的工件库里（`data/artifacts/`，sha256 即地址，取回
复验哈希）；运行终态的事件清单哈希存在 Run 上，事后改流水会对不上。

## 节点类型

| 分类 | 节点 | 说明 |
|---|---|---|
| 起止 | `input` `output` | 声明输入字段 / 收集结构化成果 |
| 模型 | `llm` `agent` `supervisor` | 单次调用 / 带工具循环 / 多 agent 协作 |
| 执行 | `tool` `code` | 直接调工具 / 沙箱里跑代码 |
| 控制 | `branch` `loop` `subgraph` | 条件或语义分支 / 遍历与条件循环 / 嵌套工作流 |
| 上下文 | `memory` `retrieve` `transform` | 长期记忆读写 / 知识检索 / 数据整形 |
| 把关 | `human` `validate` `metrics` | 人工介入 / Schema 校验与自动返工 / 口径卡（受控指标集） |

节点之间用模板语法传数据：

```
{{ input.question }}        入口输入
{{ vars.answer }}           某节点 assign_to 写入的变量
{{ nodes.n1.text }}         指定节点的输出
{{ vars.items | json }}     过滤器：json / compact / upper / length / first …
```

连线即并行：一个节点连出多条边就是 fan-out，多条边汇入同一节点会自动等待汇聚。

## 架构

```
backend/                  FastAPI + LangGraph
  app/engine/             画布 JSON → StateGraph 的编译与执行
    schema.py             图定义与静态校验
    compiler.py           编译：节点包装、事件、重试、条件路由
    runner.py             执行调度、事件总线、中断与恢复
    expressions.py        模板插值 + AST 白名单表达式求值
    nodes/                各类节点的执行器
  app/providers/          多模型接入与成本估算
  app/sandbox/            microVM / Seatbelt / bubblewrap / 裸子进程四后端
  app/tools/              工具注册表、内置工具、MCP、自定义工具
  app/memory/             记忆、知识库、混合检索
  app/api/                REST + WebSocket
frontend/                 React 19 + React Flow + Zustand
  src/canvas/nodeDefs.ts  节点元数据——属性面板、节点库、连接桩全由它驱动
  src/store/studio.ts     画布状态 + 事件流到高亮的映射
environment.yml           conda 环境定义
scripts/dev.sh            一键启动
scripts/e2e-check.mjs     浏览器冒烟测试
```

### 几个值得说明的决定

**为什么用 LangGraph。** 可视化编排真正难的不是画框连线，是暂停、恢复、回放。
LangGraph 的 checkpointer 和 `interrupt()` 正好把这几件事做在了底层：人工介入是
一次中断 + 一次 `Command(resume=...)`，进程重启后仍能从断点继续；`aget_state_history()`
直接给出每一步的状态快照。自己实现这套语义的成本远高于接一个框架。

**Agent 内部的工具审批不会重复计费。** `interrupt()` 会让整个节点重放，
朴素实现会把之前的模型调用再跑一遍。Agent 节点里的模型调用和工具执行都包在
LangGraph 的 `@task` 里，结果进 checkpoint，重放时直接取缓存。

**事件流是唯一的可视化数据源。** 节点通过 `get_stream_writer()` 发事件，runner 转成
`RunEvent` 落库并广播。画布高亮、token 流、工具卡片、时间线全部由同一份事件驱动，
所以刷新页面或中途接入都能拿到完整过程（WebSocket 先补历史再接实时）。

**沙箱不用容器，但分两档。** 轻的一档用操作系统自带的访问控制：macOS 的 Seatbelt
（`sandbox-exec`）、Linux 的 bubblewrap，冷启动 ~25ms，不用装任何东西。重的一档是
microVM（libkrun + Hypervisor.framework），起一台带独立内核的虚拟机。

两档守的都是那三件事：**默认断网**、**家目录不可读**、**只有工作区可写**。Seatbelt 侧策略
由内核强制且对子进程继承——沙箱里 `subprocess` 出来的 `cat` 读家目录一样是
`Operation not permitted`；解释器刻意用 `sys.base_prefix` 下那个干净的 Python 而不是
项目环境，所以沙箱代码 import 不到 FastAPI、SQLAlchemy 和凭据处理相关的任何东西。

**Seatbelt 少了什么，要说清楚**：它做的是访问控制而非虚拟化——进程表共享（看得见宿主机
进程列表），代码以当前用户身份运行（不像容器会降到 nobody），而且 **macOS 不强制
`RLIMIT_AS`，内存用量根本限不住**（实测设 256MB 仍能分配 900MB）。CPU 时间、单文件
大小、墙钟超时这三项有效。写 SBPL 还有个坑：`(deny default)` 会让 Python 直接 SIGABRT
起不来，要枚举的路径和 syscall 太多、漏一个就崩，所以用的是「默认允许 + 精确拒绝」。

**microVM 补上的正是内存那一刀。** 本机实测：宿主是 Darwin 27.0.0，VM 里是 Linux 6.12.99
（libkrunfw 编译），PID 1 是 `init.krun`，`/Users` 在 VM 里根本不存在，`free` 只看得见
512MB。申请 900MB 会被内核 OOM killer 杀掉而 VM 照常存活——`RLIMIT_AS` 在 macOS 上
形同虚设的问题在这里不是被修好，是结构上不存在。两台 VM 之间也互不可见。

代价照实说：运行时约 50MB，OCI 镜像**首次拉取实测 54.5s**；拉过之后热启动 0.19s、
VM 内执行 7~30ms。所以贵的是第一次而不是每一次——也正因为只贵第一次，
auto 模式在镜像没缓存时会先用 Seatbelt，不让人干等一分钟（显式选 strict 不受此限）。

**但网络关不干净，这条必须写在前面。** `network=false` 时 HTTP/HTTPS 和域名解析都会断，
可 **UDP/53 拦不住**——实测手写 DNS 包仍能拿到真实响应。`default_egress=DENY`、
`Rule.deny_dns()`、显式 deny UDP、`max_connections=0` 全试过，UDP 那条都堵不上
（deny_dns 生成的规则只针对宿主，DNS 出口在 microsandbox 的网络栈里是硬编码放行的）。
也就是说 **DNS 隧道外泄通道始终开着**。防「代码意外联网装包」够用，防「恶意代码偷数据」
不够。所以设置页把网络这项标成「部分生效」的黄色而不是绿色——拦了一半要是显示成全绿，
比显示成全红更危险。真要防外泄，得在网络层做，不能指望沙箱。

**检索为什么是混合的。** 默认 embedding 是本地特征哈希，不联网不下模型，零配置可用，
但语义泛化弱；BM25 补上精确关键词匹配这一半。两者各自归一化后加权，权重在界面上可调，
命中结果会分别显示语义分和关键词分，方便调参。配了 OpenAI Key 可切换成真 embedding。

**安全边界。** 分支条件用 AST 白名单求值器而不是 `eval`；HTTP 工具解析域名后逐个 IP
检查，拦截内网、回环和云元数据地址，且不跟随重定向（逐跳重新校验）；文件工具的路径
`resolve()` 后必须仍在工作区内；API Key 用 Fernet 加密落库，只回掩码给前端。

## 常见问题

**沙箱用的是哪个后端？** 设置页「运行环境」会列出所有候选及其可用性，并标出当前选中的那个。
装了 microVM 依赖且镜像已缓存时 auto 会选 `microvm`；否则 macOS 走 `seatbelt`、Linux 上装了
`bwrap`（`apt install bubblewrap`）走 `bubblewrap`，都没有则降级到 `local`——**那条路径没有
任何访问控制**，界面上会明确标红。可以用 `AGENTLAB_SANDBOX_BACKEND` 强制指定，
也可以在代码节点上单独选「隔离档位」（strict = microVM，fast = 系统沙箱）。

**怎么启用 microVM？** 装可选依赖后跑一次安装（约 50MB 运行时）：

```bash
conda run -n agentlab pip install 'microsandbox>=0.6'
conda run -n agentlab python -c "import asyncio,microsandbox as m; asyncio.run(m.install())"
```

之后第一次执行代码会拉 OCI 镜像（实测 ~55s，只此一次），auto 就会自动升到 `microvm`。
镜像可以用 `AGENTLAB_MICROVM_IMAGE` 换，闲置 VM 回收时间用 `AGENTLAB_MICROVM_IDLE_SECONDS`。

**模型返回空内容？** Claude 4.6 之后的模型默认开着 thinking，`max_tokens` 给小了会出现
「思考完就没额度写正文」。节点里把 max_tokens 调大，或把思考模式设为关闭。时间线里会有提示。

**中转网关报 401？** 有的网关只认 `Authorization: Bearer`，且多带一个 `x-api-key` 就会拒。
在 provider 设置里勾上「用 Authorization: Bearer 认证」。

**端口冲突？** 前端默认 5273、后端 8000，用 `AGENTLAB_WEB_PORT` / `AGENTLAB_PORT` 改。

**数据在哪？** 全在 `data/`：`agentlab.db` 业务数据、`checkpoints.db` 执行断点、
`workspace/` 沙箱文件、`.secret_key` 加密密钥。删掉 `data/` 就是恢复出厂设置。

## 冒烟测试

```bash
node scripts/e2e-check.mjs      # 开浏览器跑一遍：打开画布 → 选模板 → 运行 → 看高亮
```
