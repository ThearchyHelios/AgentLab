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

需要 Python 3.12+（`uv` 会自动装）、Node 20+、pnpm；Docker 可选但强烈建议（代码沙箱靠它做隔离）。

```bash
./scripts/dev.sh
```

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
| **代码沙箱** | Docker 隔离：断网、只读根文件系统、非 root、内存/CPU/进程数限额、超时强杀 |
| **人工介入** | 图暂停并落盘，人在界面上批准/驳回/改稿，从断点继续——进程重启也不丢 |
| **记忆与知识库** | 跨运行的长期记忆（自动去重）+ 文档知识库（向量 + BM25 混合检索，可调配比） |
| **Skill 管理** | 把「怎么做事」抽成可复用的方法论，挂到节点上注入 system prompt |
| **自然语言编排** | Copilot：一句话生成或改写整张图，自动排版 + 校验 |
| **结构化成果** | JSON Schema 校验，不合格把错误喂回模型自动返工 |
| **追踪与成本** | 每步事件全量落库，可回放；逐节点耗时、token、按目录价折算的美元成本 |

## 节点类型

| 分类 | 节点 | 说明 |
|---|---|---|
| 起止 | `input` `output` | 声明输入字段 / 收集结构化成果 |
| 模型 | `llm` `agent` `supervisor` | 单次调用 / 带工具循环 / 多 agent 协作 |
| 执行 | `tool` `code` | 直接调工具 / 沙箱里跑代码 |
| 控制 | `branch` `loop` `subgraph` | 条件或语义分支 / 遍历与条件循环 / 嵌套工作流 |
| 上下文 | `memory` `retrieve` `transform` | 长期记忆读写 / 知识检索 / 数据整形 |
| 把关 | `human` `validate` | 人工介入 / Schema 校验与自动返工 |

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
  app/sandbox/            Docker / 本地子进程双后端
  app/tools/              工具注册表、内置工具、MCP、自定义工具
  app/memory/             记忆、知识库、混合检索
  app/api/                REST + WebSocket
frontend/                 React 19 + React Flow + Zustand
  src/canvas/nodeDefs.ts  节点元数据——属性面板、节点库、连接桩全由它驱动
  src/store/studio.ts     画布状态 + 事件流到高亮的映射
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

**沙箱的隔离姿态。** 容器 `read_only` + tmpfs 工作区 + `cap_drop ALL` +
`no-new-privileges` + 非 root + 默认断网 + 内存/CPU/pids 限额 + 超时整容器销毁。
注意 Docker 的 `put_archive` 在只读容器上会被拒，所以代码是用 exec + base64 管道写进去的
——为的是不牺牲只读根文件系统。Docker 不可用时降级到子进程 + rlimit，
**那条路径没有内核级隔离**，界面上会明确标注。

**检索为什么是混合的。** 默认 embedding 是本地特征哈希，不联网不下模型，零配置可用，
但语义泛化弱；BM25 补上精确关键词匹配这一半。两者各自归一化后加权，权重在界面上可调，
命中结果会分别显示语义分和关键词分，方便调参。配了 OpenAI Key 可切换成真 embedding。

**安全边界。** 分支条件用 AST 白名单求值器而不是 `eval`；HTTP 工具解析域名后逐个 IP
检查，拦截内网、回环和云元数据地址，且不跟随重定向（逐跳重新校验）；文件工具的路径
`resolve()` 后必须仍在工作区内；API Key 用 Fernet 加密落库，只回掩码给前端。

## 常见问题

**沙箱不可用？** 设置页「运行环境」会显示实际后端和原因。Docker 没启动时会自动降级到
本地子进程，功能可用但没有隔离。首次执行会拉 `python:3.12-slim` 镜像，需要等一会儿。

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
