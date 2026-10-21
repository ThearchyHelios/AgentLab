[English](README.md) | 简体中文

# AgentLab

**可视化 Agent 编排实验台**：在画布上定义工作流，运行时逐节点观察执行过程，并对产出施加可验证的约束。模板定骨架，agent 只在被授权的关节处自由；报告里的数字由系统照快照填值，每一个都点得开出处。

![运行中的画布](docs/images/canvas-running.png)

<sub>研究助手模板用 Mock 执行中：跑完的节点打勾，执行中的节点显示耗时和流式输出；底部航迹是实时时间轴，右栏是同一份事件流驱动的运行步骤。</sub>

## 为什么用它

- **数字可核对**：所有算术都在 SQL 和口径卡（受控指标集）里完成，叙述层无权创造数字。报告撰写节点只写引用标记，系统照快照填数，每个数一路点得开到查询结果里的那一格。
- **受限编排**：引擎是编译式的，运行时模型改不了图结构，它的自由限在 agent 节点内部和分支决策里；全动态的多 agent 协作只留在探索层。
- **版本固定、可复现**：正式运行只认已发布的不可变版本（按图哈希固定）。每次执行的完整证据落进内容寻址的工件库并封存，一键核对有没有被改过。
- **人工把关**：审批、会签、时限、驳回出口；永远不会自动批准，进程重启不丢状态。
- **照实出具**：出具契约按 formal / degraded / withheld 三档判定，缺数据就降档，不硬出。

## 主要功能

**编排与运行**

- 画布：18 种节点拖拽连线，运行时逐节点高亮、流式输出、航迹时间轴 → [工作流](docs/zh-CN/workflows.md)
- 助手：一句话生成或改写整张图，按需查看表结构，交付前自查 → [助手](docs/zh-CN/ask-data.md#助手)
- 人工介入：审批、会签、时限、驳回出口 → [人工节点](docs/zh-CN/workflows.md#人工节点)
- Agent 护栏：重复调用、花费、上下文都有挡法，步数只留兜底 → [护栏](docs/zh-CN/workflows.md#护栏)
- 运行与断点续跑：失败从断点继续；逐节点耗时、token 用量与成本 → [运行](docs/zh-CN/workflows.md#运行)
- 发布与证据门禁：已发布、受管两级，门禁 G1–G5，发布前检查与修复 → [发布与版本](docs/zh-CN/workflows.md#发布与版本)
- 工具与能力登记：内置工具、自定义工具、MCP、审批档位、登记端点 → [工具](docs/zh-CN/tools.md)
- 知识库、记忆与 Skill：混合检索、跨运行记忆、可复用的方法论 → [知识库](docs/zh-CN/workflows.md#知识库记忆与-skill)
- 多模型接入：Anthropic、OpenAI、OpenAI 兼容服务和 Mock → [模型接入](docs/zh-CN/configuration.md#模型接入)
- 代码沙箱：系统沙箱（Seatbelt / bubblewrap）或 microVM 两档隔离 → [代码沙箱](docs/zh-CN/identity-and-security.md#代码沙箱)

**数据**

- 问数据：自然语言提问，系统建图、执行、复核并给出结论 → [问数据](docs/zh-CN/ask-data.md)
- 数据源与表格导入：业务库只读接入，Excel / CSV 变成可以用 SQL 查的表，跨库合并 → [数据源](docs/zh-CN/data-sources.md)
- 数据目录：粒度、业务主键、有效记录条件、码值、关联；起草、审阅、剖析、变化记录 → [数据目录](docs/zh-CN/data-catalog.md)
- SQL 检查：按目录查出七类会算错数的写法 → [SQL 检查](docs/zh-CN/data-catalog.md#sql-检查)

**证据与出具**

- 可点击证据：数字、表名字段名、知识库原话都点得开出处 → [证据与出具](docs/zh-CN/evidence.md)
- 三档出具：口径卡加出具契约，按数值回指或按引用核对 → [出具契约](docs/zh-CN/evidence.md#出具契约与三档)
- 封存与导出：一键核对封存；自包含的报告导出和可验证报告 → [导出](docs/zh-CN/evidence.md#导出)
- 复核套件：双盲复算、反方审稿、审批质询、数字推演、运行对比 → [复核套件](docs/zh-CN/evidence.md#复核套件)

**质量**

- 回归评测：题集、后台评测、带区间的准确率、对比与 A/B、定时评测 → [回归评测](docs/zh-CN/evaluation-and-ledger.md#回归评测)
- 口径账本：答案里的数逐个标口径，按口径出具、对账、召回 → [口径账本](docs/zh-CN/evaluation-and-ledger.md#口径账本)

**应用与集成**

- 应用：从已发布版本派生入口表单、视图、待办和动作 → [应用](docs/zh-CN/apps.md)
- 写外部系统：确认卡、幂等键，结果未知不自动重发 → [写外部系统](docs/zh-CN/apps.md#写外部系统)
- 对外集成：入站钩子、访问密钥、分享链接、嵌入门户 → [对外集成](docs/zh-CN/apps.md#对外集成)
- 定时任务、事件触发与通知 → [定时与通知](docs/zh-CN/schedules.md)

**运维与安全**

- 身份与权限：本地账号或 OIDC，三种角色加应用授权，遮罩边界 → [身份、权限与安全](docs/zh-CN/identity-and-security.md)
- PostgreSQL 与多进程：导入、连接池、PgBouncer、租约与接手 → [PostgreSQL 与多进程](docs/zh-CN/postgresql-and-scaling.md)

## 快速开始

需要 conda（[miniforge](https://conda-forge.org/miniforge/) 即可）、Node 20+、pnpm；不需要 Docker。

```bash
conda env create -f environment.yml   # 首次：创建 agentlab 环境并安装依赖
./scripts/dev.sh                      # 之后：一条命令启动前后端
```

打开 http://localhost:5273 。不配 API Key 也能完整体验：内置的 Mock 会把编排链路完整走一遍；接入真实模型见[模型接入](docs/zh-CN/configuration.md#模型接入)。内置 9 个模板可以直接运行。

**默认不开登录**：同一网络里打得开页面的人都能用全部功能。多人使用前先开登录，见[登录方式](docs/zh-CN/identity-and-security.md#登录方式)。更多启动选项见[开发](docs/zh-CN/development.md#启动)。

## 部署

- 不用 Docker：`./scripts/prod.sh` 构建前端，由后端一并托管页面，缺省一个进程，地址 http://127.0.0.1:8000 。
- 离线环境：用 `./scripts/package-docker.sh` 打出 Docker 离线包，拷到只装了 Docker Engine 的机器上运行 `./deploy.sh`。

详见[部署](docs/zh-CN/deployment.md)和 [deploy/README.md](deploy/README.md)。应用库缺省是 SQLite、单进程；要多进程，换成 PostgreSQL（见 [PostgreSQL 与多进程](docs/zh-CN/postgresql-and-scaling.md)）。升级前看[升级说明](docs/zh-CN/upgrade-notes.md)。

## 文档

| 文档 | 讲什么 |
|---|---|
| **使用与搭建** | |
| [问数据与助手](docs/zh-CN/ask-data.md) | 提问之后发生什么、产出复核、记录保留，助手怎么生成图、怎么按需查看 |
| [工作流](docs/zh-CN/workflows.md) | 画布、节点类型、传数据、人工节点与驳回、agent 护栏、子工作流、发布与门禁、运行与断点续跑 |
| [证据与出具](docs/zh-CN/evidence.md) | 口径卡、出具契约与三档、引用标记、结论句、查询区间里没有数据、封存与核对、报告导出与可验证报告、复核套件 |
| [数据源与表格导入](docs/zh-CN/data-sources.md) | 接入数据库、只读保证、上传表格与取数配方、按期累积、跨库合并、遮罩列 |
| [数据目录](docs/zh-CN/data-catalog.md) | 目录记什么、起草与审阅、导出导入、数据剖析、SQL 检查、大库探查范围、结构与取值变化 |
| [工具、MCP 与能力登记](docs/zh-CN/tools.md) | 工具种类、审批档位、能力治理、写入能力的限制、登记端点、网络规则 |
| [应用](docs/zh-CN/apps.md) | 生成应用、视图与印章、流程与待办、写外部系统、填报登记、个人应用、对外集成 |
| [回归评测与口径账本](docs/zh-CN/evaluation-and-ledger.md) | 题集与评测结果、逐个数标口径、按口径出具、对账、沉淀、召回 |
| [定时任务、事件触发与通知](docs/zh-CN/schedules.md) | 定时任务、事件触发、通知渠道与邮件、可订阅的事件 |
| **部署运维** | |
| [部署](docs/zh-CN/deployment.md) | `prod.sh`、Docker 离线包、数据目录、备份、升级 |
| [配置](docs/zh-CN/configuration.md) | 模型接入、对外访问地址、环境变量、界面设置、沙箱后端与 microVM |
| [PostgreSQL 与多进程](docs/zh-CN/postgresql-and-scaling.md) | 换库、导入、连接池、PgBouncer、多进程的租约与接手、多台机器、升级 |
| [身份、权限与安全](docs/zh-CN/identity-and-security.md) | 登录方式、角色与权限表、遮罩的边界、审计、代码沙箱、安全边界 |
| [升级说明](docs/zh-CN/upgrade-notes.md) | 各次更新对使用者和运维的影响，以及升级时要做的事 |
| **开发** | |
| [架构与设计决策](docs/zh-CN/architecture.md) | 总体架构、受限动态编排，以及几个关键设计决策的理由 |
| [开发](docs/zh-CN/development.md) | 开发环境、目录结构、依赖与锁文件、测试、前端检查、文档检查 |
| [常见问题](docs/zh-CN/faq.md) | 常见报错和现象的处理办法 |

## 开发与测试

后端测试前要先去掉环境里的模型密钥，否则测试可能真的去调接口：

```bash
cd backend && env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
  python -m pytest -n auto
cd frontend && npx tsc -b
node scripts/check-all.mjs   # 前端检查，先起 ./scripts/dev.sh
```

PostgreSQL 上的测试、43 项前端检查和文档检查见[开发](docs/zh-CN/development.md)。

## License

This project is **source-available** and is licensed under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0).

### Noncommercial Use

You may use, study, modify, experiment with, and develop upon this software for purposes permitted under the PolyForm Noncommercial License 1.0.0.

Typical permitted noncommercial uses include:

* personal research;
* learning and education;
* experimentation;
* evaluation and testing;
* hobby projects;
* noncommercial software development.

See the [`LICENSE`](./LICENSE) file for the applicable license terms.

### Commercial Use

**Commercial use is not permitted without prior written authorization from Yilun JIANG.**

If you intend to use this software in a commercial product, commercial service, SaaS offering, customer project, commercial deployment, or other commercial context, you must obtain a separate commercial license before doing so.

For commercial licensing, see [`COMMERCIAL-LICENSE.md`](./COMMERCIAL-LICENSE.md) or contact:

**Yilun JIANG**
**[me@thearchyhelios.com](mailto:me@thearchyhelios.com)**

### Copyright

Copyright (c) 2026 Yilun JIANG.

All rights reserved except as expressly granted under the applicable license.
