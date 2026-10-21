[English](../en/development.md) | 简体中文

# 开发

这篇讲本地开发：环境、启动、目录结构、依赖与锁文件、测试和检查脚本。给改代码的人看。系统为什么这样设计见[架构与设计决策](architecture.md)。

## 开发环境

- 需要 conda（[miniforge](https://conda-forge.org/miniforge/) 即可）、Node 20+、pnpm。
- **不需要 Docker**：代码沙箱基于操作系统自带的隔离原语，可选升级到 microVM（见[配置](configuration.md#启用-microvm)）。
- 不配 API Key 也能完整体验：内置的 Mock provider 会把编排链路完整走一遍（见[配置](configuration.md#模型接入)）。

## 启动

```bash
conda env create -f environment.yml   # 首次：创建 agentlab 环境并安装依赖
./scripts/dev.sh                      # 之后：一条命令启动前后端
```

- 打开 http://localhost:5273 。
- `dev.sh` 自动找 `agentlab` conda 环境，不存在时按 `environment.yml` 创建。要用别的环境名，设 `AGENTLAB_CONDA_ENV`。
- 端口：前端默认 5273、后端 8000，用 `AGENTLAB_WEB_PORT` / `AGENTLAB_PORT` 改。
- `--host`：前端监听所有网卡，启动时打印局域网地址，同一网络里的手机、别的电脑也能打开。后端仍只听 127.0.0.1，页面里的 `/api` 由前端开发服务器在本机转发。默认不开登录，只在信得过的网络里这样用（见[登录方式](identity-and-security.md#登录方式)）。
- `--postgres`：应用库用 PostgreSQL，本机 docker 起一个开发用的库（见[和库一起起](postgresql-and-scaling.md#和库一起起)）。
- 认不出的参数直接报错，`./scripts/dev.sh --help` 看用法。
- `dev.sh` 开着热重载：改一个后端文件就是一次服务重启，正在跑的运行显示中断，断点完好，可以接着跑（见[断点续跑](workflows.md#断点续跑)）。

## 目录结构

```
backend/                  FastAPI + LangGraph
  app/engine/             画布 JSON → StateGraph 的编译与执行
    schema.py             图定义与静态校验
    compiler.py           编译：节点包装、事件、重试、条件路由
    runner.py             执行调度、事件总线、中断与恢复
    issuance.py           出具契约：数字回指与三档判定
    evidence.py           可点击证据：证据台账、引用标记、报告文档的切块与核对（纯函数）
    judge.py              结论句裁判：预筛、成本上限、判定写回
    governance.py         发布门禁：受管 lint 与证据门禁 G1–G5
    autofix.py            发布前自动修复；upgrade.py 一键升级为可追溯结构
    guards.py             agent 护栏：重复调用、预算、上下文压缩
    review.py             产出复核：事件流规则扫描 + 模型复核
    expressions.py        模板插值 + AST 白名单表达式求值
    nodes/                各类节点的执行器（report.py 是报告撰写）
  app/data/               数据源接入：连接、SQL 守卫、结构探查、表格导入
  app/providers/          多模型接入与成本估算
  app/sandbox/            microVM / Seatbelt / bubblewrap / 裸子进程四后端
  app/tools/              工具注册表、内置工具、MCP、自定义工具
  app/memory/             记忆、知识库、倒排索引、混合检索、文档解析
  app/api/                REST + WebSocket（证据接口在 evidence.py）
frontend/                 React 19 + React Flow + Zustand
  src/canvas/nodeDefs.ts  节点元数据——属性面板、节点库、连接桩均由其驱动
  src/canvas/routing.ts   连线走线：端口错开、走廊车道、长边绕行
  src/run/decode.ts       事件流 → 人类可读步骤的唯一翻译层
  src/run/EvidenceDoc.tsx 报告逐段渲染：数字、名字、原话都是可点的片段；EvidencePanel.tsx 是出处链
  src/store/studio.ts     画布状态 + 事件流到高亮的映射
environment.yml           conda 环境定义
scripts/dev.sh            一键启动
scripts/check-all.mjs     前端检查的总入口：分道并行跑全部 check-*.mjs，汇总成一张表
```

## 依赖与锁文件

- `backend/pyproject.toml` 写版本范围，`backend/requirements.lock` 写精确版本：后端全部依赖（含间接依赖、全部 extras）锁在测试跑过的那一套上，跨平台通用。
- `environment.yml` 装包时以锁文件为约束，所以换一台机器装到的也是同一套，不是当天的最新版。
- LangGraph / LangChain 一族在 `pyproject.toml` 里只放行到下一个小版本：引擎直接依赖它们的断点格式、interrupt / resume 和流式事件，这些在小版本里也改过行为。
- 断点格式本身在 `langgraph-checkpoint` 里（langgraph 只要求它 <5，所以单独卡住）。SQLite、PostgreSQL 两个断点库（`langgraph-checkpoint-sqlite`、`langgraph-checkpoint-postgres`）都按它读写；`copy-to-postgres` 能搬断点，也靠两边是同一版格式。
- `langgraph-checkpoint-postgres` 从 3.1.3 起要 `langgraph-checkpoint` 4.3，所以锁在 3.1.2；升级时三个一起动。
- aiosqlite 与 SQLAlchemy 同理：`db/base.py` 给 aiosqlite 打了补丁，连接池断开连接时会走到它。应用库换成 PostgreSQL 之后补丁照样打着，因为上传表格、手工登记的 SQLite 数据源还走 aiosqlite。
- `[postgres]` 里是应用库用 PostgreSQL 时要的：SQLAlchemy 这边走 asyncpg（和 `[db]` 里连业务库的是同一个包），断点库走官方的 `langgraph-checkpoint-postgres`，它只认 psycopg 3 和 `psycopg-pool`。psycopg 带 `[binary]`：wheel 里自带 libpq，镜像和本机都不用另装 PostgreSQL 客户端。

### 可选依赖

`environment.yml` 默认已经全部装上。单独补装时带上锁文件，版本才和测过的一致：

```bash
pip install -e 'backend[docs]' -c backend/requirements.lock     # PDF / Word / PowerPoint / Excel 解析
pip install -e 'backend[db]' -c backend/requirements.lock       # MySQL / PostgreSQL / Oracle 驱动
pip install -e 'backend[microvm]' -c backend/requirements.lock  # microVM 沙箱
pip install -e 'backend[postgres]' -c backend/requirements.lock # 应用库用 PostgreSQL（asyncpg、psycopg 3、官方断点库）
pip install -e 'backend[dev]' -c backend/requirements.lock      # pytest
```

### 升级依赖

升级依赖要主动做，改完跑一遍测试再提交：

```bash
./scripts/lock-deps.sh --upgrade-package langgraph   # 升一个包（连带它必须跟着动的）
./scripts/lock-deps.sh --upgrade                     # 全部升到 pyproject 允许的最新
./scripts/lock-deps.sh                               # 只加了新依赖：补上它，已锁的不动
pip install -e 'backend[microvm,dev,db,docs,postgres]' -c backend/requirements.lock   # 装到当前环境
```

重新生成锁文件需要 [uv](https://docs.astral.sh/uv/)；装依赖本身只用 pip。

## 测试

```bash
# 后端：约 16000 个测试，-n auto 并行约 2–3 分钟
cd backend && env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
  python -m pytest -n auto

# 前端类型检查
cd frontend && npx tsc -b

# 应用库换成 PostgreSQL 跑同一套后端测试（起临时的 postgres:16 容器，跑完删掉）
PYTHON=$(conda run -n agentlab which python) ./scripts/test-postgres.sh
```

- pytest 前面的 `env -u` 不能省：没配 Key 的 provider 会退回环境变量里的密钥，启动时的 seed 也会拿它种一个 provider。环境里留着真密钥，测试就可能真的去调接口。
- PostgreSQL 和多进程的测试细节见 [PostgreSQL 与多进程](postgresql-and-scaling.md#测试)。

### 验收用的假外部系统

```bash
python scripts/fake-erp.py --port 8765
```

它可以切换「正常 / 写成功后超时 / 500 / 4xx」，用来验收写外部系统的各种结果；接口见脚本开头。

## 前端检查

```bash
# 43 项，默认 8 道并行。连 5273 / 8000，先起 ./scripts/dev.sh
node scripts/check-all.mjs
# 另起 2 套只给检查用的「后端 + vite」，各用一份数据拷贝，约 6.5 分钟；边改代码边跑也不被打断
CHECK_DATA_SRC=<数据目录> node scripts/check-all.mjs --stacks 2 --lanes 12
# 只跑某几项（名字去掉 check- 前缀）
node scripts/check-all.mjs evidence publish
```

- 完整输出写进 `/tmp/agentlab-checks/<时间>/`，终端只给汇总。并行时没过的，最后单独重跑一遍；重跑才过的单列成「偶发」。
- `--stacks` 每套拷一份数据、端口自动挑空闲的；后端不热重载、vite 不监听文件；跑完、出错或被中断都收干净。`--stacks` 只支持 SQLite。
- `scripts/e2e-check.mjs` 真发起一次运行（Mock，不花钱），会在库里留下记录，所以不在 check-all 里。
- `scripts/check-auth.mjs`（登录、按角色收起的导航、401 跳登录）要另起一套开着本地账号登录的服务单独跑，也不在 check-all 里。

`check-all` 的 43 项按 `scripts/check-all.mjs` 里 `ORDER` 的顺序排：

| 类别 | 检查 |
|---|---|
| 不开浏览器 | `tokens`（设计令牌的静态扫描）`copy`（界面文案）`trace`（运行态内核的纯函数）`decode`（事件翻译层） |
| 走线、组件与护栏 | `canvas-layout` `ui-kit` `guards` |
| 外壳、回答流与运行态 | `shell` `stream` `evidence`（可点击证据）`run-states`（节点卡）`canvas-fx`（画布表层） |
| 各页面 | `studio` `publish`（发布前检查与自动修复）`chat` `runs` `manage` `versions`（上传表格的版本）`catalog`（数据目录）`catalog-loop`（表助手、审阅队列、导出导入）`catalog-profile`（数据剖析的逐表任务）`catalog-changes`（变化、表详情的变化一节、定时维护、后台任务条）`ui`（端到端，连真数据） |
| 复核套件与导出 | `schedules`（定时任务）`compare`（运行对比）`verify`（双盲复算、反方审稿）`inquiry`（审批质询）`challenge`（反方审稿）`bundle`（可验证报告）`whatif`（数字推演）`report-export`（报告导出） |
| 评测与口径账本 | `evals`（题集、加题、发起与进度、结果、对比与 A/B、口径命中与重复度）`ledger`（证据里的「口径」一步、轮次摘要、出具与对账、可沉淀的问题） |
| 应用平台 | `apps`（应用页、印章、点数字看出处、入口校验、实例页）`inbox`（会签进度、超时、动作确认的哈希卡、核实）`app-editor`（应用编辑、派生、检查、发布时选处置、助手）`capabilities`（能力、端点、身份、通知、审计）`access-keys`（访问密钥）`share`（分享链接与 /s 页）`blueprint`（新建应用、蓝图卡片、试运行、发布摘要）`personal`（个人应用）`records`（记录列表）`files`（文件控件） |

### 三层验证的分工

- `check-decode` 守「翻译对不对」，`check-stream` 守「画出来对不对」，`check-ui` 守「整条路走得通不通」。三者可以各自通过、合起来却是坏的：例如 Step 字段全部正确，组件却把它渲染成一屏转义的 JSON。
- `check-canvas-fx` 守「事件对了但界面没跟上」：把脚本化的事件灌进 store（走的是页面上那一份 applyEvent / NodeCard / FlowEdge，只有事件来源是脚本），断言协作矩阵真的长出来了、并行度算对了、条形长度和耗时成正比、分支命中的出口被点亮、系统关了动效时光效确实不绘制。真跑一张多 agent 的图要连模型、要等十几秒，而且撞不上「三个人同时还在跑」那一瞬间，那样的检查会变成偶发失败。
- `check-canvas-layout` 单独守「图好不好看」里唯一能算的那部分：它把夹具图交给后端 `/api/copilot/layout` 排版，再直接 `import('/src/canvas/routing.ts')`，用页面上正在跑的那份走线代码算路径，然后按几何断言：没有两条线压在同一段上、没有线从别人的卡片中间穿过去、每条线的两端都接在节点边缘。连线叠成一团时，`check-ui` 和 `e2e-check` 一个字都不会说。

## 文档检查

```bash
python3 scripts/check-docs.py
```

它检查三件事：`README.md` 和 `docs/en/` 下不许出现中文字符和全角符号（代码块也算）；`README.md`、`README.zh-CN.md`、`docs/` 下全部文档和 `deploy/README.md` 里的相对链接和锚点都要通；`docs/zh-CN/` 和 `docs/en/` 的文件一一对应、每一对的标题层级相同。改文档后跑一遍。
