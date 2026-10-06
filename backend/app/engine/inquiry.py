"""审批质询：审批人针对待审报告里的一句话提问，一个只读 agent 当场查库作答，问答写进这次运行。

审批卡上只有报告正文和核对概况。审批人对某句话有疑问（「排除测试账号会怎样」「这个数含不含退款」），以前只能驳回重跑，
或者自己去查库。质询让一个只读 agent（engine/verify_agent.py）当场作答，报告本身一个字不改：

1. 开放条件（_open）：运行停在审批上、问的是那张待处理的审批卡——问答落在以后的封存范围里，随运行结束一起封存；或者
   运行已经结束封存、核对一致、是探索运行——问答追加在封存之后（post_seal），是批注不是证据。正式运行封存之后不再追加
   （同按需裁判）；设置里关了「审批质询 · 启用」一律不答。同一次运行同时只答一条：进程内登记（_ANSWERING），登记期间
   这次运行的审批回复、继续运行也不受理（api/runs.py），免得运行往下走了、问答还在路上
2. 回答（_answer）：交给 agent 被问的那句原文、它挂的证据摘录（照 judge.prepare 的办法取，数据源遮罩的列照旧不给，原报告
   的编号标明「原报告」）、审批人选中的原文和问题；工具是这次运行用过的查询工具对应的数据源（同名重建的、没固定过版本的
   上传表格不算，见 _usable_sources）；上限按设置（每次的查询次数、时长，全局每日金额）。回答先给结论，数字只能引用 agent
   自己查询的格子，或者报告里已有的指标、运行输入；回答文档由 compose_doc 组装，裸数字被标出来（提问人自己写的数除外）。
   封存之后的追问查的是数据源现在的数据，任务里给报告生成的时间，回答末尾注明；触顶、查询失败、引用解析不了也在末尾注明
3. 遮罩（_plan）：agent 的查询结果在核查 agent 的工具边界就遮好了（engine/masking.py 的 mask_query_result）：取自
   遮罩列的格子交给模型、落进快照之前就是「已遮罩」，模型看不到遮罩值。报告自己快照里的遮罩值在派 agent 之前就交给
   查询工具（snapshot_secrets → run_verify_agent 的 masked_values），agent 前面的查询遮掉的值也一样：后面查到的备注、
   别的表里写着同一个号码的，交给模型、落库之前就按值换掉。这里是第二道防线：查询时遮掉的值由 verify_agent 交回
   （masked_values，只在内存里），连同报告自己快照里的遮罩值，照下面的办法按值遮。事后取质询工件时内存里的值已经
   不在：回答时只靠它们才换掉的地方（先查备注、后查出号码的那份备注快照）记成位置写进事件（REPLAY_KEY，不含值，
   masking.view_edits），取工件时照着再换一遍；SQL 检查结果照事件里记下的 SQL 换（_recorded_swaps）。
   哪些列要遮照证据面板的规则（查询当时记下的，加上数据源现在设的），另按 SQL 认出改了名字的遮罩列
   （起别名、套函数、经子查询转一道、和 * 写在同一支里，_derived_hidden；认不出落在哪一列的，整份快照遮掉；按遮罩列
   分档、当分组键或套在汇总里数个数的不算，_band_only）；快照按列遮，再按值遮（别的列里写着遮罩值的格子，_masked_view；质询
   工件里，文字格里夹着的遮罩值另按子串换）。回答正文、查询清单里的遮罩值换成「已遮罩」（_Secrets、_scrub_sql）：
   按值认有门槛，只装着分档标签的列（agent 写在 SQL 里的标签）不收进遮罩值（_label_columns）；SQL 里的字面量按语法树
   认用在哪（_literal_uses），数字照原样留着的只有范围比较的边界（没把某一个值夹住的，_pinned）和 LIMIT 的行数，而且
   同一个数在这条 SQL 别处没被换掉，封存的是实际执行的语句；在找某一个值的（等值、IN、简单 CASE、夹住某一个值的
   范围……，值换个写法、写成几个数的算式也认）和别处够长的遮罩值都换。已经摆在读者眼前的值不换：问题和选中原文里的、
   报告列出的指标和运行输入的值（agent 后面的查询遮掉的值照样免遮，_Secrets.protected）；SQL 里照原样留着的阈值只在
   回答正文里不换，格子照旧换。回答正文、质询工件的文字格、SQL 的字符串、带引号的名字和注释里，换了写法的长号码
   （199-1474-2785、(199)1474-2785、+86 199 1474 2785、全角数字、不换行空格或减号隔开的）按纯数字写法认（masking 的
   _Secrets.loose_spans）。组装之后还认得出遮罩值的（换了写法的也算），这份回答不交出去
4. 写入（_record）：一条 approval.inquiry 事件，node_id 为空，用量写在数据的 usage 里（计进运行用量，见 runner 的
   _usage_of_events）。质询的查询快照只放在 inquiry_artifacts / queries 里，绝不放在 query_artifact 下：证据接口按那个键
   收集封存的查询，会把质询的查询当成报告的证据。回答期间运行状态变了（被批准、被放弃、继续运行之后重新封存），
   append_events 不写，回答照样交回给提问的人，说明没有写进运行记录（这样的回答在进程里留一阵，_HELD）。触顶、失败
   照样记一条
5. 读取：列表读 approval.inquiry 事件（封存前后的都算，按先后）；质询工件只认质询引用过的（查询快照、回答文档、回答里
   引用的报告口径卡），先照事件里记下的位置重放回答时换掉的地方，再照回答时的办法遮，快照里 SQL 检查结果的片段、说明
   照整条 SQL 的换法换（_masked_checks）

不发 tool.start、llm.start 这类事件（run_verify_agent 不传 emit）：界面从接口的返回取回答；runner 的 _stale_replay
只看 tool.start，质询不影响恢复后的重放。

已知局限（遮罩只减少暴露，不是安全边界，同证据面板）：
- 按值认有门槛，两头都有误差：短的数（年龄、等级这类不到 SECRET_DIGITS 位的）在回答正文、格子里不按值遮，agent 在
  正文里写出某人的年龄遮不住，只靠 SQL 里的等值比较和按列遮；同一个短的数在 SQL 里被换掉了，别的列上撞上它的阈值、
  LIMIT 的行数照原样（age = 30 AND visits >= 30）。够长的数在格子和正文里按值遮，碰巧和某个遮罩值相同的数（访问次数
  正好等于某一笔金额）也遮掉。这是产品取舍，不按上下文去猜
- 遮罩数值列的 SUM / AVG / MIN / MAX 算派生自遮罩列，整列遮（COUNT 不算，见 _value_columns），CASE 里有一支取的是
  遮罩列的也算：MIN / MAX 就是某一行的原值，只有一行的分组 SUM / AVG 也是，按列认不出哪些汇总值安全，宁可多遮。
  核查 agent 的查询在工具边界照这个办法遮，所以这样的指标双盲复算不了（原 SQL 里认得出的，派盲算之前就记为无法
  复算），反方审稿核对这类汇总数的结论也拿不到数。汇总里套着结果全是写死常量的 CASE（SUM(CASE WHEN amount <= 20000
  THEN 1 ELSE 0 END)）只是按条件数个数，同 COUNT；汇总里的 CASE 只在条件里用到遮罩列的（SUM(CASE WHEN buyer_phone
  IS NOT NULL THEN amount ELSE 0 END)）同写在 WHERE 里，不算
- FROM、JOIN、LATERAL 里的表值函数（json_each、json_tree、unnest、jsonb_each_text、JSON_TABLE、LATERAL VALUES）拆开
  遮罩列的，按出列名认它出的列（常见函数的出列名、写的列名表、别名、函数名，_function_columns），同名的别的列一并遮
  （多遮）；不认得的函数又没写列名表的，整份快照都遮。函数参数里没写表名的 col->'path'（JSON 箭头，解析器当成以 col
  为参数的 lambda）按 col 认，真的 lambda（transform(arr, x -> …)）的参数和遮罩列同名时也算（多遮）
- * 把遮罩列带进别的列名（WITH t(a, b, …) AS (SELECT * …) 这类列名表、表的列名表 FROM sales AS s(i, r, p)、UNION 后一支
  的 *、TABLE x、标量子查询里的 *）：只展开一个来源、那里每一列都数得清的，按位置只遮取自遮罩列的那几列——子查询按它的
  选出项数，数据源里的表按查询当时冻结的表结构数（几张列一样的月度表 SELECT * 连起来汇总，金额照常）。表结构里查不到
  这张表（写了库名的按「库名.表名」查：另一个库里的同名表 archive.sales 不按探查的 sales 数，探查时没设 schema、查询
  又写了库名的也一样）、数出来的列数和实际展开的对不上（UNION 第一支的 * 按表结构数出的列名，后面每一支都要一样宽才用；
  Postgres 的列名表只写前几列、后面的照原名时也对不上），或者 * 连着几个来源的，* 展开落到的那一段整段遮，同一段里的
  金额这类别的列一并遮（多遮）。表结构过期、列数没变却换了次序或改了名的，可能数错（同按表结构认标签列）；没写库名的表
  按探查的那张数，探查的不是默认的库、默认的库里又有一张列序不同的同名表时也会数错。标量子查询里的 * 展开数据源里的表
  一律算取自遮罩列。EXCEPT 后一支只用来去掉行，也照 UNION 的后一支算（多遮）
- 整行和函数参数里的 *（Postgres 的 row_to_json(s)、json_agg(t)、SELECT s、s::text、to_json(s.*)、concat_ws(',', s.*)，
  Oracle 的 JSON_OBJECT(*)）按 * 展开的来源算：取自子查询、CTE 的看它选出的列，取自数据源里的表的一律算取自遮罩列（整列
  遮）。Postgres 先按列认：表结构里查得到和表同名的列（visits 表的 visits 列）才当列，查不到时按整行算（多遮）。PIVOT、
  UNPIVOT、MATCH_RECOGNIZE 读到遮罩列的，整份快照的列都遮（多遮）。SQL 解析不了的只看文字：写着遮罩列、*（COUNT(*) 除外）、
  TABLE x 的整份遮，整行引用这类不写列名的写法认不出
- 按遮罩列分档的标签（CASE、IIF 的结果全是写死的常量）：标签文字是 agent 写在 SQL 里的，不当遮罩值，SQL、回答正文里
  照原样；标签列当分组键（每一档一行）照常显示，不分组的逐行标签列整列遮（格子里看不出某一笔落在哪一档），可 agent
  在正文里写出「这一笔是大单」遮不住，按 id 分组再按档数个数也看得出（同按阈值查出某几行）。只有只装着标签的列不收：
  结果的这一列在 UNION 的每一支里都是这样的 CASE / IIF，或者按作用域认得出取自子查询、CTE 里只装着标签的一列；有一支
  取自数据源里的表（遮罩列原样选出、经子查询按原名传出，哪怕起了标签的名字）就照收。认不准的也照收（多遮），比如
  按这一列 USING、NATURAL 合成的同名列，递归的 CTE，UNION 里落在某一支 * 里的列，写死的字符串、和列一起算的值；
  派生来源里同名的几列（SQLite 都留着，外层按名字取排在最前面的那一列）：标签前面有 * 带得出这一列（* 展开了数据源里
  的表、冻结的表结构里查不到这张表或者它有同名的列，就算带得出），或者有没起别名、套了括号或 COLLATE 的同名列
  （(vip_level)、vip_level COLLATE NOCASE），都不算只装着标签（_block_label_of、_star_label）。* 展开的表在表结构里
  查得到、又没有同名的列时，照常按派生来源认标签（_table_lacks）。标签恰好和别的列里的遮罩值相同时，正文里的档名、
  SQL 里写成字符串的标签也换（多遮）
- 「夹住某一个值」（_pinned）：范围比较在整条 SQL 里配对，只有 CASE 的不同分支、SELECT 里不筛行的不同项（选出列、
  GROUP BY、ORDER BY）、UNION 的不同支各算各的，两头误差都有：把找值拆进这几处（CASE WHEN age > 25 THEN … ELSE CASE
  WHEN age >= 25 …，两个选出列 SUM(age >= 25)、SUM(age > 25) 再自己相减）认不出，边界照原样留着，正文里复述也不遮；
  反过来，不在同一个条件里的两边只要不是这几处就按夹住算，分档写成两个 CTE、FROM 里的两个子查询再连起来，或者同一个
  选出列里把两档的汇总值算在一起（小单数 / 大单数）时，共用的边界会多遮。同一个条件里细看合起来的真假，整数上的半开、
  开区间（col >= v AND col < v + 1）、两头不是整数的（col > v - 0.5 AND col < v + 0.5）只在整个条件只认 v 时算，被
  更宽的范围吸收、和相邻的分档连成一段的不算。不等、NOT IN 只在同一个条件里和范围合起来看（age >= 25 AND age <= 26
  AND age <> 26 只认 25；age <= 30 时 27 也成立，不算），拆进别的条件（子查询、HAVING）认不出；整数上紧挨着 v 的一串
  被排除的数最多越过 PAIR_MAX 个，再多就按夹住算（多遮）。细看的步数有上限（PIN_WORK），超出之后两边都有范围比较
  隔开的一律按夹住算，分档很多时会成批多遮
- 找某一个值的写法按语法树认常见的几种：等值、IN、简单 CASE、把它夹住的范围，被比较的一边套了 CAST、函数、汇总
  （CAST(age AS INTEGER) = 25、HAVING MAX(age) BETWEEN 25 AND 25），值换个写法（25.0、025、2.5e1）、写成几个数的
  算式（加减乘除、取模、整除：13900000000 + 1、BETWEEN 20 + 5 AND 30 - 5、125 % 100、50 DIV 2，算式里的数都换；
  两个整数相除照数据源的算法，SQLite、Postgres 里 53 / 2 是 26，MySQL 里是 26.5）。更绕的写法认不出：值藏进和列
  一起算的算式（age - 25 = 0、buyer_phone - 1 = 13900000000）、位运算（24 | 1）、拼接（'1390' || '0000001'）、
  函数拼出来的值（REPLACE、SUBSTR、POWER）、CAST 改了值的（CAST(25.6 AS INTEGER) 按 25.6 认，CAST(53 AS REAL) / 2
  按整数相除算），SQLite 里带小数的取模（它先把两边取整再算：25.9 % 26 是 25，这里照小数算成 25.9，认不出在找
  25），短的值照原样封存，长号码只在整个写出来时按值换
- 质询工件里 SQL 检查结果的片段是检查器按语法树重新写出来的，按整条 SQL 里整个换掉了的字面量照值换：同一个值在
  片段里别处出现也换（多遮，算式里换掉的 0、1 这类也是）；截断处半截的值去掉（停在字符串、带引号的名字、注释里的，
  从开头的引号、注释起整个去掉）。说明里码值表列出的码值和别的字一样按值换，恰好是遮罩值的码值也换（多遮）
- 运行详情的「工件」页签、通用的工件接口按工件原样给：质询的查询快照落库时就遮好了（工具边界先遮），取自遮罩列的值、
  报告快照里的遮罩值、agent 前面的查询里遮掉的值都看不到。只有一种照原样：先查了写着号码的备注（那时还不知道这个号码
  是遮罩值），后来才查出号码——备注快照落库时原样，从通用接口打开照原样，质询工件接口照事件里记下的位置换。报告自己
  的查询快照（写作时的查询）仍原样存着，从那里打开不经这里的遮罩
- 遮罩值最多收 SECRETS_MAX 种写法，再多的按值认不出（按列遮照旧）。agent 自己查到的值先收，报告快照里的后收：核查
  agent 把两份分开记（masking.SeenValues），查询时按值遮、交回调用方、回答时合起来，都是 agent 查到的在前
- 换了写法的号码只认数字之间夹着一到四个分隔符（空格、制表符、全角空格、不换行空格、窄空格这类空白，连字符、减号、
  点、括号，含全角），带 +86 / 0086 / 86 前缀、全角数字：写成汉字（一三九……）、中间夹着别的字、拆成几处写的、跨行
  写的认不出；和别的数字只隔着一个连字符或点连着的（13900000001-2、19914.74）不算号码；SQL 里不带引号的数不这样认
  （199-1474-2785 在 SQL 里是三个数相减），字符串、带引号的名字、注释里照样认
- 质询 agent 的任务里的证据摘录照 judge.prepare 按列名遮：报告取数把遮罩列改了名字选出来（buyer_phone AS 手机号）时，
  摘录里有原值，agent 看得到；它写进回答、SQL 的，按值认得出的照样换掉（报告快照里的值都收了），封存的是换过的。摘录
  按 SQL 认改了名字的遮罩列要改 judge.py，另行处理
"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import itertools
import logging
import operator
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import func, select

from app.core import artifact_store
from app.core.errors import describe_exception
from app.core.events import EventType
from app.data.catalog import QUERY_SNAPSHOT_KIND
from app.data.provenance_types import DOC_PROVENANCE
from app.db.base import SessionLocal
from app.db.models import Approval, Artifact, DataSource, Run, RunEvent
from app.engine import judge as judging
from app.engine.evidence import compose_doc, iter_segments, iter_units
from app.engine.issuance import extract_numbers
# 认遮罩列、遮罩值的共用部分在 engine/masking.py。名字照旧从这里导出：nodes/dual_check、测试按 inquiry 的名字引用
from app.engine.masking import (  # noqa: F401
    SECRETS_MAX, SECRET_DIGITS, SECRET_MIN, _ABSENT, _DIGITS, _EXPONENT_MAX, _LabelMemo, _NUMERIC, _PLAIN_NUMBER,
    _PREFIX, _Secrets, _WORD, _WORDS, _all_labels, _arithmetic, _band_only, _block_label, _block_label_of, _blocks,
    _branch_results, _branches, _canonical, _constant, _constants, _decimal, _derived_block, _derived_hidden,
    _digits_of, _fixed, _forms, _from_sources, _is_star, _item_label, _label_columns, _leaves, _literal_types,
    _masked_view, _masked_word, _mentions, _merging, _numeric, _output_name, _pattern, _places, _plain_text,
    _point_text, _query_shaped, _result_label, _scrubbed_source, _snapshot_labels, _source_label, _source_names,
    _star_label, _strong, _tainted, _value_columns, _worked_out, replay_edits, view_edits,
)
from app.engine.merge_query import MERGE_SOURCE
from app.engine.nodes.challenge import mark_original, with_note
from app.engine.run_append import append_events
from app.engine.toolcalls import QUERY_PREFIX
from app.engine.verify_agent import RATIONALE, VerifyOutcome, run_verify_agent

logger = logging.getLogger(__name__)

#: 不受理时的机读码（前后端约定）。审批回复、继续运行在质询期间回的也是 INQUIRY_BUSY（api/runs.py）
INQUIRY_NOT_OPEN = "inquiry_not_open"
INQUIRY_BUSY = "inquiry_busy"
INQUIRY_DISABLED = "inquiry_disabled"
RUN_NOT_FOUND = "run_not_found"
ARTIFACT_NOT_FOUND = "inquiry_artifact_not_found"
ARTIFACT_TAMPERED = "snapshot_tampered"
#: 问题、选中的原文最长多少字
QUESTION_MAX = 1000
QUOTE_MAX = 500
#: 回答期间运行状态变了、问答没写进运行记录时给提问的人的说明
NOT_RECORDED = "运行状态已经变化，这次质询的问答没有写入运行记录"
BUSY = "这次运行另有一条质询正在回答，回答完再提问"
#: 质询正在回答时，这次运行的审批回复（批准 / 驳回）、继续运行回的话（api/runs.py）
BUSY_DECIDE = "正在回答质询，回答完再批准或驳回"
BUSY_CONTINUE = "正在回答质询，回答完再继续运行"
#: 触顶原因：前后端约定的四种。verify_agent 另有 max_steps（模型想了太多轮）：走到那一步时查询次数一定还没用完，
#: 不能说成查询次数到顶，记为 null，回答末尾注明（NOTE_ROUNDS）
LIMITS = ("max_queries", "timeout_s", "max_cost_usd", "daily_max_usd")
#: 事件、接口里的用量只给这几项（runner 计进运行用量的也是这几项，_INQUIRY_KEYS）
USAGE_KEYS = ("input_tokens", "output_tokens", "cost_usd", "calls", "unpriced_calls")
#: 查询清单每条给的几项：没有行，查询结果只在快照里（取的时候按遮罩遮好）
QUERY_KEYS = ("alias", "artifact", "sql", "row_count", "truncated", "source")
#: 质询事件数据里记「回答时只靠 agent 查询时遮掉的值才换掉的位置」的键：{快照工件: masking.view_edits 的形状}。
#: 不含值；事后取质询工件时内存里的值已经不在，照着位置再换一遍（inquiry_artifact）
REPLAY_KEY = "masked_cells"
#: 回答文档存成工件时的种类；工件引用的 meta 里记下问题的前这么多字，运行的工件列表据此认得出是哪个问题的回答
ANSWER_KIND = "inquiry_answer"
QUESTION_BRIEF = 40
#: 没指明句子时交给 agent 的报告正文最多多少字
REPORT_CHARS = 4000
#: 列给 agent 的报告指标、运行输入最多几条
REFS_MAX = 40
#: 被问的是表格里的一格时，交给 agent 的表格最多几行（不含表头）
TABLE_ROWS = 20
#: 没写进运行记录的回答在进程里留多久、最多留几条：这段时间里质询工件接口照样认它引用的工件
HELD_TTL_S = 3600
HELD_MAX = 200
#: 运行的终态。封存之后的追问只在这几种状态上
_TERMINAL = ("succeeded", "failed", "cancelled")
#: 一条 SQL 里认夹没夹住某一个值（_pinned）最多走这么多步（细看一个条件，一步是其中的一串且 / 或、一个非、括号或
#: 比较；找两个条件的公共祖先，一步是往上走一层）：几百个分档边界连成一长串时，挨个细看要算上好几秒。超出之后两边
#: 都隔开的一律按夹住算（宁可多换），分档很多时会成批多遮
PIN_WORK = 100_000

INQUIRY_SYSTEM = (
    "你在帮人核对一份报告：提问人针对报告里的一句话提了问题，你查库作答。你不写报告，也不改报告。\n"
    "- 问题是「如果……会怎样」（排除测试账号、换个时间范围、只看某个区域）时，写只读查询把它算出来，不要凭印象推测\n"
    "- 回答直截了当：第一句直接给结论（是还是不是、是多少、差多少），再用一两句说依据；不要复述问题，不要客套\n"
    "- 回答里的每个数字都写成引用：你自己查询结果里的一格写 [[v:Qn.rX.列名]]，报告里已有的指标、运行输入照任务里列出的"
    "写法引用；不要直接写数字，没有出处的数字会被标出来\n"
    "- 数据源设了遮罩的列（手机号、姓名这类）：查询里不要选出原值，也不要给它们起别名；要数人数、笔数用 COUNT，回答里需要"
    "指明时说「该客户」「这一笔」，不要写出原值\n"
    "- 查不到、数据里没有判断所需的信息，就照实说查不到，不要编造"
)
INQUIRY_TASK_PROMPT = (
    "报告「{report}」{where}\n\n"
    "{evidence}"
    "{refs}"
    "可以查询的数据源：{sources}\n\n"
    "{since}"
    "{masked}"
    "提问人的问题：\n{question}\n\n"
    "按上面的做法作答，回答写在 rationale 里。"
)
#: 交给核查 agent 的结果结构：只有回答一项（verify_agent 的理由字段，沿用这里写的说明）
INQUIRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {RATIONALE: {
        "type": "string",
        "description": "回答：第一句直接给结论，再用一两句说依据；数字一律写成引用（[[v:Qn.rX.列名]]，"
                       "或任务里列出的报告指标、运行输入）",
    }},
    "required": [RATIONALE],
    "additionalProperties": False,
}

# 不受理的说法
DISABLED_WORDS = "审批质询已关闭（设置中的「{label}」），如需质询，请在设置的「核查」一节开启"
STILL_RUNNING = "运行仍在执行。停在人工审批时，或运行结束并封存后，才能质询"
STILL_RUNNING_FORMAL = "运行仍在执行，停在人工审批时才能质询"
CARD_MOVED = "这张审批卡已处理，运行正在继续执行，请刷新页面查看最新状态"
NO_CARD = "运行没有停在人工审批上（可能是服务重启时中断的），没有可以质询的审批"
CARD_GONE = "这张审批卡已处理，或不是这次运行正在等待的审批，请刷新页面查看最新状态"
CARD_DONE = "这张审批卡已处理，运行已经结束，请刷新页面查看最新状态"
FORMAL_SEALED = "正式运行已结束并封存，问答不能再追加进运行记录；封存之后只有探索运行可以继续质询"
UNSEALED = "运行已结束，但还没有封存，暂时不能质询"
BROKEN_SEAL = "封存核对未通过：封存后有事件被修改、删除或插入，报告和证据已不可信，不能再质询"
NEED_CARD = "运行停在人工审批上，需要指明质询的是哪张审批卡"
BAD_BODY = ("请求格式有误：需要一个对象，写明报告（report，报告撰写节点的 ID）和问题（question），"
            "可选句子编号（unit）、选中的原文（quote）、审批卡（approval_id）")
DOC_TAMPERED = "报告文档与哈希不一致，疑似被修改，不能据此质询"
DOC_MISSING = "无法读取报告文档"
# 没能给出回答的说法：说发生了什么、下一步。核查 agent 的原话（「交结果」「按结构」、裸写的字段名）只进日志
NO_ANSWER = "模型没有给出回答内容，请重新提问"
LEAKED = "回答中含有设了遮罩的值，无法遮住，已不予展示，请换个问法重新提问"
ERR_DAILY = "今日核查花费已达上限（{cap}），未能给出回答，可在设置的「核查」一节调高上限"
ERR_COST = "这次回答的花费已达上限，未能给出回答，可缩小问题范围后重新提问"
ERR_TIMEOUT = "回答超过时长上限{seconds}，未能给出回答，可缩小问题范围后重新提问"
ERR_SHAPE = "模型未能按要求给出回答，请重新提问；如反复出现，请在设置的「核查」一节更换核查模型"
ERR_SILENT = "模型没有给出回复，未能给出回答，请重新提问"
# 回答末尾的系统说明（with_note：另起一段，不挂出处、不查裸数字）
NOTE_LOST = "注：这次运行还用到了数据源{names}，已不存在、已停用或已被重建，未能查询，以上回答不含其中的数据。"
NOTE_ROUNDS = "注：调用模型的轮数已用完，以上回答只依据已经查到的结果，可能不完整。"
NOTE_FAILED = "注：有 {n} 次查询执行失败（查询清单只列成功的查询），以上回答可能缺少依据。"
NOTE_UNRESOLVED = "注：回答中有 {n} 处引用无法解析，相关的数没有出处。"
NOTE_LIVE = "注：以上回答依据的是数据源当前的数据，晚于报告生成的时间；与报告不一致时，可能是报告生成之后数据有更新。"


class InquiryRefused(Exception):
    """质询不受理：status 是 HTTP 状态码，message 是给人看的话；409 带机读码 code，422 带出错的那一项 field。"""

    def __init__(self, status: int, message: str, *, code: str | None = None, field: str | None = None) -> None:
        super().__init__(message)
        self.status, self.message, self.code, self.field = status, message, code, field


def _invalid(message: str, name: str | None) -> InquiryRefused:
    return InquiryRefused(422, message, field=name)


def _not_open(message: str) -> InquiryRefused:
    return InquiryRefused(409, message, code=INQUIRY_NOT_OPEN)


# --------------------------------------------------------------------------
# 同一次运行同时只答一条
# --------------------------------------------------------------------------


class _Answering:
    """正在回答质询的运行（进程内登记）。

    只在本进程里有效：另一个进程里的批准拦不住，那时追加按条件更新核对（append_events），写不进就照实说没写进。"""

    def __init__(self) -> None:
        self._runs: set[str] = set()

    def busy(self, run_id: str) -> bool:
        return run_id in self._runs

    @contextlib.contextmanager
    def hold(self, run_id: str) -> Iterator[None]:
        # 查和登记之间没有 await：同一个事件循环里的两条质询不会都看到「没人在答」
        if run_id in self._runs:
            raise InquiryRefused(409, BUSY, code=INQUIRY_BUSY)
        self._runs.add(run_id)
        try:
            yield
        finally:
            self._runs.discard(run_id)


_ANSWERING = _Answering()


def inquiry_busy(run_id: str) -> bool:
    """这次运行是不是正在回答质询（api/runs.py 据此让审批回复、继续运行回 409）。"""
    return _ANSWERING.busy(run_id)


class _Held:
    """没写进运行记录的回答（回答期间运行状态变了）：进程内留 HELD_TTL_S 秒、最多 HELD_MAX 条，先进先出。

    提问人手里那份回答里的数要点得开，质询工件接口在这段时间里照样认它引用的工件（遮罩照旧）。过了时间、或者服务
    重启过，接口照常回 404：回答本身已经交回去了，只是数字不能再下钻。"""

    def __init__(self) -> None:
        self._items: OrderedDict[int, tuple[str, float, dict[str, Any]]] = OrderedDict()
        self._next = 0

    def keep(self, run_id: str, data: dict[str, Any]) -> None:
        self._next += 1
        self._items[self._next] = (run_id, time.monotonic() + HELD_TTL_S, data)
        while len(self._items) > HELD_MAX:
            self._items.popitem(last=False)

    def of(self, run_id: str) -> list[dict[str, Any]]:
        now = time.monotonic()
        for key in [k for k, (_, until, _) in self._items.items() if until <= now]:
            del self._items[key]
        return [data for owner, _, data in self._items.values() if owner == run_id]


_HELD = _Held()


# --------------------------------------------------------------------------
# 请求
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Ask:
    """一条质询：哪份报告（报告撰写节点的 id）的哪一句（句子单元 id，可以不指明）、选中的原文、问题、问的是哪张审批卡。"""

    report: str
    unit: str | None
    quote: str | None
    question: str
    approval_id: str | None


def parse_ask(body: Any) -> Ask:
    """请求体 → Ask。不合法时抛 422，field 指明是哪一项。"""
    if not isinstance(body, dict):
        raise _invalid(BAD_BODY, None)
    report = body.get("report")
    if not isinstance(report, str) or not report.strip():
        raise _invalid("请指明质询的是哪份报告（报告撰写节点的 ID）", "report")
    unit = _blank_as_none(body.get("unit"))
    if unit is not None and not isinstance(unit, str):
        raise _invalid("句子编号需要是文字，例如「u12」；不指明句子时留空", "unit")
    quote = body.get("quote")
    if quote is not None and not isinstance(quote, str):
        raise _invalid("选中的原文需要是文字", "quote")
    quote = (quote or "").strip() or None
    if quote is not None and len(quote) > QUOTE_MAX:
        raise _invalid(f"选中的原文最长 {QUOTE_MAX} 字，当前为 {len(quote)} 字，请缩短选区", "quote")
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        raise _invalid("请填写要质询的问题", "question")
    question = question.strip()
    if len(question) > QUESTION_MAX:
        raise _invalid(f"问题最长 {QUESTION_MAX} 字，当前为 {len(question)} 字", "question")
    approval_id = _blank_as_none(body.get("approval_id"))
    if approval_id is not None and not isinstance(approval_id, str):
        raise _invalid("审批卡的 ID 需要是文字；不是在审批卡上提问时留空", "approval_id")
    return Ask(report=report.strip(), unit=unit.strip() if unit else None, quote=quote, question=question,
               approval_id=approval_id.strip() if approval_id else None)


def _blank_as_none(value: Any) -> Any:
    """不指明句子、不是在审批卡上问的：写 null 和写空串是一个意思。别的类型原样交回，由调用方报格式不对。"""
    return None if isinstance(value, str) and not value.strip() else value


# --------------------------------------------------------------------------
# 开放条件
# --------------------------------------------------------------------------

Events = list[tuple[int, str, str | None, Any]]


@dataclass
class _Opening:
    """开放了的这一次质询：往哪种运行里写（mode）、写的时候要核对什么（approval_id / seal），以及被问的报告文档。

    loader 是取证据的函数：封存之后只认封存范围内的工件（同按需裁判，api/evidence._sealed_loader），停在审批时和
    报告节点里的裁判一样直接读工件库。events 是这次运行的事件（封存之后只取封存范围内的）。finished 是运行结束的时间
    （UTC，封存之后的追问才有）：报告是那时生成的，之后数据可能有更新。"""

    run_id: str
    mode: str
    title: str
    versions: Any
    events: Events
    doc: dict[str, Any]
    loader: Callable[[str], Any]
    approval_id: str | None = None
    seal: tuple[int | None, str | None] | None = None
    finished: str | None = None


async def _open(run_id: str, ask: Ask) -> _Opening:
    """这条质询开不开放：开放就交回往哪写、问的是哪份文档；不开放抛 409（输入不对的抛 422、运行不在的抛 404）。"""
    cards: list[str] = []
    rows: Events = []
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise InquiryRefused(404, "运行记录不存在", code=RUN_NOT_FOUND)
        status, run_class, graph, versions = run.status, run.run_class, run.graph or {}, run.data_versions
        finished = _utc(run.finished_at)
        if status == "interrupted":
            cards = [str(a) for a in (await session.execute(
                select(Approval.id).where(Approval.run_id == run_id, Approval.status == "pending"))).scalars()]
            rows = [tuple(r) for r in await session.execute(
                select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
                .where(RunEvent.run_id == run_id).order_by(RunEvent.seq))]
    title = _node_title(graph, ask.report)
    if status == "interrupted":
        if not cards:
            raise _not_open(NO_CARD)
        if ask.approval_id is None:
            raise _invalid(NEED_CARD, "approval_id")
        if ask.approval_id not in cards:
            raise _not_open(CARD_GONE)
        doc = _waiting_doc(rows, run_id, ask.report, title)
        return _Opening(run_id=run_id, mode="interrupted", title=title, versions=versions, events=rows, doc=doc,
                        loader=artifact_store.load, approval_id=ask.approval_id)
    if status not in _TERMINAL:
        # 带着审批卡来的，多半是卡刚被别人批了、运行往下走了：叫人刷新。没带的按运行类别说什么时候能问
        raise _not_open(CARD_MOVED if ask.approval_id is not None
                        else STILL_RUNNING_FORMAL if run_class == "formal" else STILL_RUNNING)
    if ask.approval_id is not None:
        raise _not_open(CARD_DONE)
    if run_class == "formal":
        raise _not_open(FORMAL_SEALED)
    from app.api.evidence import _reports, _sealed, _sealed_loader   # api.evidence 导入了 engine 里的模块

    sealed = await _sealed(run_id)
    if sealed.status not in _TERMINAL:
        # 上面读完状态之后运行被继续了：记录上的封存是上一次结束时的，追加会被下一次封存收进去
        raise _not_open(STILL_RUNNING)
    if not sealed.sealed:
        raise _not_open(UNSEALED)
    if not sealed.trusted:
        raise _not_open(BROKEN_SEAL)
    report = next((r for r in _reports(sealed) if r.node_id == ask.report), None)
    if report is None:
        raise _invalid(_no_report(title), "report")
    if report.hash_ok is False:
        raise _invalid(DOC_TAMPERED, "report")
    if report.doc is None:
        raise _invalid(DOC_MISSING, "report")
    # 带期 4 标记的文档才让摘录取得到哈希链上的清单，和按需裁判同一个口径
    loader = _sealed_loader(sealed, chain=report.doc.get("provenance") == DOC_PROVENANCE)
    return _Opening(run_id=run_id, mode="sealed", title=title, versions=versions, events=list(sealed.events),
                    doc=report.doc, loader=loader, seal=(sealed.run_manifest_seq, sealed.manifest_hash),
                    finished=finished)


def _utc(ts: datetime | None) -> str | None:
    """运行记录上的时间 → UTC 的「2026-10-06T08:12:33」。带时区的换成 UTC，不带的本来就是 UTC。"""
    if ts is None:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.isoformat(timespec="seconds")


def _node_title(graph: dict[str, Any], node_id: str) -> str:
    for n in graph.get("nodes") or []:
        if isinstance(n, dict) and n.get("id") == node_id:
            return str((n.get("data") or {}).get("label") or node_id)
    return node_id


def _no_report(title: str) -> str:
    return f"这次运行里没有报告「{title}」的报告文档，请刷新页面后重新选择"


def _waiting_doc(events: Events, run_id: str, report: str, title: str) -> dict[str, Any]:
    """停在审批时被问的报告文档：这个报告节点最后一次 report.checked 里的那份（同证据接口的 _reports）。文档要真是
    这次运行里这个节点写的——内容寻址只保证没被改，不保证没被张冠李戴。"""
    artifact = None
    for _, etype, node_id, data in events:
        if etype == EventType.REPORT_CHECKED and node_id == report and isinstance(data, dict) and data.get("doc_artifact"):
            artifact = str(data["doc_artifact"])
    if artifact is None:
        raise _invalid(_no_report(title), "report")
    try:
        doc = artifact_store.load(artifact)
    except ValueError as e:
        raise _invalid(DOC_TAMPERED, "report") from e
    if not isinstance(doc, dict):
        raise _invalid(DOC_MISSING, "report")
    if doc.get("node_id") != report or doc.get("run_id") != run_id:
        raise _invalid(DOC_TAMPERED, "report")
    return doc


# --------------------------------------------------------------------------
# 被问的那句
# --------------------------------------------------------------------------


@dataclass
class _Target:
    """被问的那句：按裁判的候选句建（judge._candidate，带它挂的依据），表格里的一格另带整张表。"""

    cand: judging.Candidate
    table: str | None = None


def _target(doc: dict[str, Any], unit: str) -> _Target:
    """文档里的这一句。按 judge.candidates 的走法记小节和先后；不是结论句的（表格里的一格、标题）也照样建，
    审批人问的不一定是结论句。没有这一句抛 422。"""
    markdown = str(doc.get("markdown") or "")
    section = ""
    for order, (block, item) in enumerate(iter_units(doc)):
        text = judging._unit_text(item, markdown)
        if block.get("type") == "heading":
            section = text
        if item.get("id") != unit:
            continue
        priority = judging.claim_priority(item, block, markdown) or 3
        cand = judging._candidate(item, priority, order, text, section)
        return _Target(cand, _table_text(block, markdown) if block.get("type") == "table" else None)
    raise _invalid(f"报告里没有这句（{unit}），请刷新页面后重新选择", "unit")


def _table_text(block: dict[str, Any], markdown: str) -> str:
    """表格整张压成几行（表头在前），一格的值离了行列说不清是什么。"""
    rows: dict[int, dict[int, str]] = {}
    for unit in block.get("units") or []:
        loc = unit.get("loc") if isinstance(unit, dict) else None
        if isinstance(loc, dict) and isinstance(loc.get("row"), int) and isinstance(loc.get("col"), int):
            rows.setdefault(loc["row"], {})[loc["col"]] = judging._unit_text(unit, markdown)
    lines = [" | ".join(cells[c] for c in sorted(cells)) for _, cells in sorted(rows.items())]
    kept = lines[:TABLE_ROWS + 1]
    return "\n".join(kept) + ("\n…（后面的行省略了）" if len(lines) > len(kept) else "")


def _excerpts(doc: dict[str, Any], catalog: dict[str, Any], cand: judging.Candidate, loader: Callable[[str], Any],
              masked: dict[str, Any]) -> list[str]:
    """这句挂的每件证据的摘录：和 judge.prepare 一样逐件摘（数据源遮罩的列不给，带期 4 标记的文档多来历行），
    指向原报告编号的写法标上「原报告」——agent 自己的查询也编 Q1、Q2…，不分开的话会把它们混成一回事。"""
    fetch = judging._Fetch(loader)
    provenance_on = doc.get("provenance") == DOC_PROVENANCE
    queries = {a for a, e in catalog.items() if isinstance(e, dict) and e.get("kind") == "query"}
    out: list[str] = []
    for alias in cand.aliases:
        entry = catalog.get(alias)
        if isinstance(entry, dict) and (text := judging._excerpt(alias, entry, [cand], fetch, masked, catalog,
                                                                 provenance_on=provenance_on)):
            out.append(mark_original(text, alias, queries))
    return out


def _report_refs(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """报告目录里回答可以直接引用的出处：有值的口径卡指标（m:）和运行输入（i:）。它们的编号和 agent 自己的 Q1… 不会
    撞；报告的查询格子不行——agent 的查询也编 Q1，引用 Q1 说不清是哪一份。没有值的引用了也解析不了，不列。"""
    return {a: e for a, e in catalog.items()
            if isinstance(e, dict) and e.get("kind") in ("metric", "input") and e.get("value") not in (None, "")}


def _ref_values(refs: dict[str, dict[str, Any]]) -> list[str]:
    """报告里列出的指标、运行输入的值（原样和渲染出来的写法）：报告里就看得到，碰巧和遮罩列里的某个值一样，回答引用
    它也不算露出遮罩值（_Secrets.protect）。不免的话，引用「单笔目标」这样的指标整份回答都交不出去，换个问法也一样。"""
    out: list[str] = []
    for entry in refs.values():
        out += _forms(entry.get("value"))
        if isinstance(entry.get("rendered"), str):
            out.append(entry["rendered"])
    return out


def _run_sources(events: Events) -> list[str]:
    """这次运行用过的查询工具对应的数据源，按第一次用到的先后。"""
    out: list[str] = []
    for _, etype, _, data in events:
        if etype not in (EventType.TOOL_START, EventType.TOOL_END) or not isinstance(data, dict):
            continue
        tool = str(data.get("tool") or "")
        name = tool[len(QUERY_PREFIX):] if tool.startswith(QUERY_PREFIX) else ""
        if name and name not in out:
            out.append(name)
    return out


async def _usable_sources(run_id: str, names: Iterable[str], versions: Any) -> dict[str, list[str]]:
    """这次运行用过、现在还能查的数据源 → 它现在设了遮罩的列。

    工具按名字建（核查建工具时只认存在、没有停用的源），名字背后的源却可能换过：运行用过的源被删了、又用同一个名字
    新建一个，查到的已经不是这次运行的数据。所以按名字找到现在的源之后，再核对是不是运行用过的那一个——运行里的查询
    快照在工件引用的 meta 里记着源 id（质询自己的查询不算，它们的 node_id 为空），上传表格还记在运行固定的版本里
    （data_versions）。没固定过版本的上传表格也不交给 agent：建工具时会把当前版本补固定进运行记录（tools/datasource
    的 _pin_late），质询不发事件，已经封存的运行就被悄悄改了。升级前发起的运行（data_versions 为 None）照旧按当前版本。"""
    from app.data.engine import masked_columns

    wanted = list(dict.fromkeys(names))
    if not wanted:
        return {}
    pins = versions if isinstance(versions, dict) else None
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(DataSource.id, DataSource.name, DataSource.origin, DataSource.options)
            .where(DataSource.name.in_(wanted), DataSource.enabled.is_(True)))).all()
        metas = (await session.execute(
            select(Artifact.meta).where(Artifact.run_id == run_id, Artifact.kind == QUERY_SNAPSHOT_KIND,
                                        func.coalesce(Artifact.node_id, "") != ""))).scalars().all()
    used: dict[str, set[str]] = {}
    for meta in metas:
        if isinstance(meta, dict) and meta.get("source") in wanted and meta.get("source_id"):
            used.setdefault(str(meta["source"]), set()).add(str(meta["source_id"]))
    for source_id, entry in (pins or {}).items():
        if isinstance(entry, dict) and entry.get("name") in wanted:
            used.setdefault(str(entry["name"]), set()).add(str(source_id))
    out: dict[str, list[str]] = {}
    for source_id, name, origin, options in rows:
        known = used.get(str(name))
        if known and str(source_id) not in known:
            continue
        if (origin or "manual") == "upload" and pins is not None and source_id not in pins:
            continue
        out[str(name)] = masked_columns(options)
    return out


def _quoted(names: Iterable[str]) -> str:
    return "、".join(f"「{n}」" for n in names)


def _task(op: _Opening, ask: Ask, target: _Target | None, excerpts: list[str], refs: dict[str, dict[str, Any]],
          live: dict[str, list[str]], lost: list[str]) -> str:
    if target is not None:
        cand = target.cand
        where = f"中的这句话{f'（所在小节「{cand.section}」）' if cand.section else ''}：\n「{cand.text}」"
        if target.table:
            where += f"\n\n这句话在一张表格里，表格是（每行一条，单元格用 | 隔开）：\n{target.table}"
    else:
        where = "，提问人没有指明是哪一句"
    if ask.quote:
        where += f"\n\n提问人选中的原文：\n「{ask.quote}」"
    if target is None:
        markdown = str(op.doc.get("markdown") or "")
        cut = markdown[:REPORT_CHARS] + ("\n…（后面的省略了）" if len(markdown) > REPORT_CHARS else "")
        where += f"\n\n报告正文：\n{cut}"
    evidence = ""
    if target is not None:
        evidence = ("这句话挂的证据摘录（编号属于原报告；你的查询另起编号，回答里只能引用你自己的查询）：\n"
                    + "\n\n".join(excerpts) + "\n\n") if excerpts else "这句话没有挂依据。\n\n"
    lines = [f"[[{alias}]] {entry.get('label') or alias}" for alias, entry in list(refs.items())[:REFS_MAX]]
    more = f"\n…另有 {len(refs) - REFS_MAX} 项" if len(refs) > REFS_MAX else ""
    refs_text = ("报告里已有的指标和运行输入（回答里可以照这个写法直接引用）：\n" + "\n".join(lines) + more + "\n\n"
                 if lines else "")
    sources = "、".join(live) if live else "（没有可以查询的数据源，只能依据上面的报告内容作答）"
    if lost:
        sources += f"（这次运行还用到了{_quoted(lost)}，已不存在、已停用或已被重建，无法查询）"
    # 封存之后的追问：报告是运行结束时生成的，现在查到的数据可能已经变了
    since = (f"报告生成于 {op.finished}（UTC），之后数据可能有更新：查询结果和报告对不上时，先说明这一点。\n\n"
             if op.mode == "sealed" and op.finished else "")
    hidden = [f"「{name}」的 {'、'.join(cols)}" for name, cols in live.items() if cols]
    masked = (f"设了遮罩的列（查询里不要选出原值、不要起别名，回答里不要写出这些列的值）：{'；'.join(hidden)}\n\n"
              if hidden else "")
    return INQUIRY_TASK_PROMPT.format(report=op.title, where=where, evidence=evidence, refs=refs_text,
                                      sources=sources, since=since, masked=masked, question=ask.question)


# --------------------------------------------------------------------------
# 遮罩（哪些列、哪些值的认法在 engine/masking.py）
# --------------------------------------------------------------------------


async def _hidden_columns(snapshot: dict[str, Any], masks: Any) -> tuple[set[str], list[str]]:
    """一份查询快照要遮的列（小写）和已经不在的数据源，照证据面板的规则（api/evidence.py 的 _query_step）：查询当时
    记下的，加上数据源现在设的；合并查询按它合并的那几个数据源算。"""
    from app.api.evidence import _merge_masks, _recorded_masks

    if snapshot.get("source") == MERGE_SOURCE:
        return await _merge_masks(snapshot, masks)
    cols, found = await masks.of(snapshot.get("source"))
    return cols | _recorded_masks(snapshot), ([] if found or not snapshot.get("source") else [str(snapshot["source"])])


#: 按词认在找某一个值的比较（SQL 解析不了时的退路，见 _context）：col = 'x'、col <> 'x'、col LIKE 'x'、SQLite 的
#: col IS 'x'（IN 列表另外认）
_EQUAL_TOKENS = frozenset({"EQ", "NEQ", "NULLSAFE_EQ", "LIKE", "ILIKE", "GLOB", "RLIKE", "IS"})
#: 范围比较（BETWEEN 另外认）
_RANGE_TOKENS = frozenset({"GT", "GTE", "LT", "LTE"})
_NAME_TOKENS = frozenset({"VAR", "IDENTIFIER"})
#: 字面量前面可能连着的：正负号、MySQL 的字符集前缀（_utf8mb4'x'）
_LEADS = frozenset({"DASH", "PLUS", "INTRODUCER"})
#: 能结束一个值的词：减号跟在它们后面是两数相减，不是负号
_VALUE_ENDS = frozenset({"NUMBER", "VAR", "IDENTIFIER", "R_PAREN", "R_BRACKET", "NULL", "TRUE", "FALSE"})
#: 数字照原样留在 SQL 里的两种用法（_literal_uses）：范围比较的边界、LIMIT 这类的行数
_KEPT = frozenset({"range", "limit"})
#: 字面量写在左边时运算符翻过来：25 < age 就是 age > 25（等值一类翻过来还是它自己）
_FLIP = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "=": "=", "<>": "<>"}


def _is_literal(kind: str) -> bool:
    """字符串（含 N'…'、E'…'、$$…$$、U&'…' 这些带前缀的变体）和数字。"""
    return kind == "NUMBER" or kind.endswith("STRING")


def _operand_start(kinds: list[str], k: int) -> int:
    """第 k 个字面量连同它前面的负号、字符集前缀从哪个词开始（-25、_utf8mb4'x'）。"""
    while k >= 1 and kinds[k - 1] in _LEADS and (
            kinds[k - 1] == "INTRODUCER" or k < 2 or not (kinds[k - 2] in _VALUE_ENDS or _is_literal(kinds[k - 2]))):
        k -= 1
    return k


def _name_at(tokens: list[Any], kinds: list[str], k: int) -> str | None:
    return tokens[k].text.lower() if 0 <= k < len(kinds) and kinds[k] in _NAME_TOKENS else None


def _context(tokens: list[Any], kinds: list[str], i: int) -> tuple[str, str | None]:
    """按词认第 i 个字面量用在哪，只在 SQL 解析不了时用（_literals），只认得出简单的写法：("equal", 列名) 在找某一个
    值——col = 'x'、col NOT LIKE 'x'、col IS NOT 25、col IN ('x', …)、'x' = col；("range", None) 范围比较的边界——
    col > 25、col BETWEEN a AND b；("other", None) 别的。列名是比较另一边的那个名字（s.buyer_phone 取 buyer_phone，
    小写），另一边不是单独一个名字（函数、算式）时为 None。"""
    j = _operand_start(kinds, i) - 1
    if j >= 1 and kinds[j] == "NOT" and kinds[j - 1] == "IS":
        return "equal", _name_at(tokens, kinds, j - 2)
    if j >= 0 and kinds[j] in _EQUAL_TOKENS:
        return "equal", _name_at(tokens, kinds, j - 2 if j >= 1 and kinds[j - 1] == "NOT" else j - 1)
    if j >= 0 and (kinds[j] in _RANGE_TOKENS or kinds[j] == "BETWEEN"):
        return "range", None
    if j >= 1 and kinds[j] == "AND" and _is_literal(kinds[j - 1]):
        # BETWEEN a AND b 的 b
        lower = _operand_start(kinds, j - 1) - 1
        if lower >= 0 and kinds[lower] == "BETWEEN":
            return "range", None
    # IN 列表：往回越过逗号和别的字面量，找到「IN (」
    k = j
    while k >= 0 and (kinds[k] == "COMMA" or kinds[k] in _LEADS or _is_literal(kinds[k])):
        k -= 1
    if k >= 1 and kinds[k] == "L_PAREN" and kinds[k - 1] == "IN":
        return "equal", _name_at(tokens, kinds, k - 3 if k >= 2 and kinds[k - 2] == "NOT" else k - 2)
    # 字面量在左边：'x' = col、25 < t.col
    after = i + 1
    if after < len(kinds) and kinds[after] in _RANGE_TOKENS:
        return "range", None
    if after < len(kinds) and kinds[after] in _EQUAL_TOKENS:
        last = after + 1
        while last + 2 < len(kinds) and kinds[last + 1] == "DOT" and kinds[last + 2] in _NAME_TOKENS:
            last += 2
        return "equal", _name_at(tokens, kinds, last)
    return "other", None


def _column_of(node: Any, index: int | None) -> str | None:
    """比较另一边的列名（小写、不带表名）；另一边是行值时取第 index 项。不是单独一列（函数、算式、子查询）时为 None。"""
    from sqlglot import exp

    node = _item_of(node, index)
    if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star):
        return node.name.lower() or None
    return None


def _item_of(node: Any, index: int | None) -> Any:
    """比较的另一边去掉括号；是行值时取第 index 项（没有这一项为 None）。"""
    from sqlglot import exp

    if index is not None and isinstance(node, exp.Tuple):
        items = node.expressions
        node = items[index] if index < len(items) else None
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _columns_of(node: Any, index: int | None) -> frozenset[str]:
    """比较另一边的值取自哪些列（小写、不带表名）：单独一列就是它；套了 CAST、函数、汇总、算式的，按值取自哪些列算
    （_value_columns：CAST(age AS INTEGER)、MAX(age) 取的还是年龄；COUNT 只数个数，不算）。和遮罩列比较的字面量，
    短的数也按遮罩列里的值认（_swap_literal）。"""
    from sqlglot import exp

    node = _item_of(node, index)
    if node is None:
        return frozenset()
    if isinstance(node, exp.Column):
        name = None if isinstance(node.this, exp.Star) else node.name.lower()
        return frozenset({name} if name else ())
    return frozenset(_value_columns(node))


def _use_of(node: Any, values: dict[int, Any], lifts: dict[int, Any]) -> tuple[str, frozenset[str], Any]:
    """语法树里一个字面量用在哪：(用法, 被比较的一边取自哪些列, 算出来的值)，用法见 _literal_uses。

    先往上越过不改变「找的是哪个值」的外壳：括号、负号、CAST、字符集前缀、COLLATE，以及几个写死的数的加减乘除——
    整个算式按算出来的那一个值认（_lifts，第三项交回走到的那一层的值，什么都没越过为 None）；行值、VALUES、ARRAY、ANY / ALL 也
    越过，记下「在一组值里」（行值另记下是第几项）。再看外壳的上一层：等值一类、IN、简单 CASE 的 WHEN 是在找这个值；
    范围比较、LIMIT 一类只认单独一个值（一组值里的、和列一起算的算式里的都不算）。"""
    from sqlglot import exp

    top = lifts.get(id(node), node)
    computed = values.get(id(top)) if top is not node else None
    child, parent = top, top.parent
    index, grouped = None, False
    while parent is not None:
        if isinstance(parent, exp.Tuple):
            if index is None:
                index = next((k for k, item in enumerate(parent.expressions) if item is child), None)
            grouped = True
        elif isinstance(parent, (exp.Array, exp.Values, exp.Any, exp.All)) or (
                isinstance(parent, exp.Subquery) and isinstance(child, exp.Values)) or (
                isinstance(parent, exp.Anonymous) and str(parent.this).upper() in ("ANY", "ALL", "SOME")):
            # = ANY(ARRAY[…])、= ANY(VALUES …)；Postgres 的 <> ALL(…) 解析成名叫 ALL 的函数
            grouped = True
        elif not _wraps(parent, child):
            break
        child, parent = parent, parent.parent
    if isinstance(parent, (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.NullSafeNEQ, exp.Is, exp.Like, exp.ILike, exp.Glob,
                           exp.RegexpLike, exp.SimilarTo)):
        return "equal", _columns_of(parent.expression if child is parent.this else parent.this, index), computed
    if isinstance(parent, exp.In):
        return "equal", frozenset() if child is parent.this else _columns_of(parent.this, index), computed
    if isinstance(parent, exp.If) and child is parent.this and isinstance(parent.parent, exp.Case) \
            and parent.parent.this is not None:
        # 简单 CASE：CASE buyer_phone WHEN 13900000001 THEN …（CASE WHEN col = v 的写法走上面的等值）
        return "equal", _columns_of(parent.parent.this, index), computed
    if grouped:
        return "other", frozenset(), computed
    if type(parent) in _range_ops() and (child is parent.this or child is parent.expression):
        return "range", _columns_of(parent.expression if child is parent.this else parent.this, None), computed
    if isinstance(parent, exp.Between) and child is not parent.this:
        return "range", _columns_of(parent.this, None), computed
    if (isinstance(parent, (exp.Limit, exp.Offset)) and child is parent.expression) or (
            isinstance(parent, exp.Fetch) and child is parent.args.get("count")):
        return "limit", frozenset(), computed
    return "other", frozenset(), computed


def _wraps(parent: Any, child: Any) -> bool:
    """parent 只是 child 的外壳，不改变它是哪个值：括号、负号、CAST（只算被转换的那个值，类型参数、DEFAULT 后面的值
    不算）、字符集前缀（_utf8mb4'x'）、COLLATE。"""
    from sqlglot import exp

    return (isinstance(parent, (exp.Paren, exp.Neg, exp.Cast)) and child is parent.this) or (
        isinstance(parent, exp.Introducer) and child is parent.expression) or (
        isinstance(parent, exp.Collate) and child is parent.this)


def _range_ops() -> dict[Any, str]:
    """范围比较的语法树节点 → 「列 运算符 值」方向的运算符。"""
    from sqlglot import exp

    return {exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}


def _exact_ops() -> dict[Any, str]:
    """等值一类的语法树节点 → 运算符：=、IS NOT DISTINCT FROM、SQLite 的 IS 是「=」，<>、IS DISTINCT FROM 是「<>」。
    IN 另外认（一串「=」的「或」）。它们和范围比较合起来也能夹住一个值（age >= 25 AND age <= 26 AND age <> 26），
    只在同一个条件里算（_pinned）。"""
    from sqlglot import exp

    return {exp.EQ: "=", exp.NullSafeEQ: "=", exp.Is: "=", exp.NEQ: "<>", exp.NullSafeNEQ: "<>"}


def _lifts(root: Any, values: dict[int, Any]) -> dict[int, Any]:
    """写死的值往上能越过的外壳和算式：id(节点) → 越过之后走到的那一层（13900000000 + 1 里的两个数都走到整个算式，
    (25) 里的 25 走到括号）。先父亲后孩子（广度优先）地记，每个字面量直接跳到那一层，上千项的连加不必每个数都一层层
    往上走。"""
    out: dict[int, Any] = {}
    for node in root.walk():
        if id(node) not in values:
            continue
        parent = node.parent
        lifted = parent is not None and id(parent) in values and (
            isinstance(parent, _arithmetic()) or _wraps(parent, node))
        out[id(node)] = out[id(parent)] if lifted else node
    return out


#: 一个比较的一半：(算出边界的那几个字面量, 另一边的键, 写死的值, 「键 运算符 值」的运算符)。边界是几个数算出来的
#: （13900000002 - 1），算式里的字面量都记下：夹住了某一个值，留着哪一个都可能算得出它，都换。不参与算值的字面量
#: （CAST(30000 AS DECIMAL(10, 2)) 的类型参数 10、2）不记，见 _halves
_Half = tuple[tuple[Any, ...], str, Any, str]
#: 一个比较拆成的几半，加上几半怎么连（False：且，BETWEEN；True：或，IN）、是不是等值一类（不等、IN）
_Leaf = tuple[list[_Half | None], bool, bool]


def _halves(node: Any, values: dict[int, Any], lifts: dict[int, Any]) -> list[_Half | None]:
    """一个比较拆成的几半（values 是 _constants 算好的写死的值，lifts 见 _lifts）：BETWEEN 是两半的「且」（不低于下界、
    不高于上界），> >= < <= 是一半，= <> 这类是一半，IN 是一串「=」的「或」。边界不是写死的值（另一列、子查询）或者
    两边都写死了的，这一半记 None：它的真假跟哪一列的取值都对不上，当成不知道真假。算出边界的字面量是往上走得到这一边
    的那几个（_lifts）：CAST(30000 AS DECIMAL(10, 2)) 里只有 30000，类型参数 10、2 不算出值，换了只会让封存的 SQL 难读，
    SQL 检查结果的片段里别处的 10、2 也会跟着换。"""
    from sqlglot import exp

    if isinstance(node, exp.Between):
        sides = [(node.args.get("low"), node.this, ">="), (node.args.get("high"), node.this, "<=")]
    elif isinstance(node, exp.In):
        if node.args.get("query") or node.args.get("unnest") or node.args.get("field") or id(node.this) in values:
            return [None]
        sides = [(item, node.this, "=") for item in node.expressions]
    else:
        op = _op_of(node)
        right, left = id(node.expression) in values, id(node.this) in values
        if op is None or right == left:
            return [None]
        sides = [(node.expression, node.this, op)] if right else [(node.this, node.expression, _FLIP[op])]
    out: list[_Half | None] = []
    for operand, other, op in sides:
        value = values.get(id(operand)) if operand is not None else None
        literals = () if value is None else tuple(
            literal for literal in operand.find_all(*_literal_types()) if lifts.get(id(literal)) is operand)
        out.append((literals, _key_of(other), value, op) if value is not None else None)
    return out


def _op_of(node: Any) -> str | None:
    """范围比较、等值一类的运算符（「列 运算符 值」方向）；别的为 None。"""
    for kind, op in (*_range_ops().items(), *_exact_ops().items()):
        if isinstance(node, kind):
            return op
    return None


def _key_of(node: Any) -> str:
    """比较另一边的键：单独一列取列名（小写、不带表名，同 _column_of）；别的表达式（SUM(amount)、函数、算式）取
    整个写法，只和写得一样的表达式算同一个——SUM(amount) <= v 和 amount >= v 说的不是一回事。前面垫一个列名里不会
    有的字，免得和列名撞上。"""
    from sqlglot import exp

    column = _column_of(node, None)
    if column is not None:
        return column
    while isinstance(node, exp.Paren):
        node = node.this
    return "\0" + (node.sql() if node is not None else "")


def _logic() -> tuple[Any, ...]:
    """把几个条件连成一个条件的：且、或、非、异或（MySQL 的 XOR）、括号。"""
    from sqlglot import exp

    return exp.And, exp.Or, exp.Not, exp.Xor, exp.Paren


def _condition_root(node: Any) -> Any:
    """一个比较所在的条件：往上只越过且、或、非、异或、括号，走到的那一层。同一个条件里的几个比较按合起来的真假细看
    （_isolated）；不在同一个条件里的，看是不是分档的几种写法各算各的（_apart）。"""
    while isinstance(node.parent, _logic()):
        node = node.parent
    return node


#: SELECT 里不筛行的几处（选出列、GROUP BY、ORDER BY 这些）：对同一批行各算各的。WHERE、ON、HAVING、QUALIFY、FROM 里
#: 的子查询、WITH 里的一段都在筛这一批行
_VALUE_ARGS = frozenset({"expressions", "group", "order", "distinct", "windows", "sort", "cluster", "distribute"})


def _apart(a: Any, b: Any, paths: dict[int, dict[int, Any]]) -> tuple[bool, int]:
    """两个条件（不是同一个）是不是分档的几种写法各算各的，以及为此走了几步（记进 PIN_WORK）。各算各的只有：
    - CASE 的不同分支（不同的 WHEN，或 WHEN 和 ELSE），IIF 的真假两支：一行只走其中一支；
    - SELECT 里不筛行的不同项：两个选出列，选出列和 GROUP BY 里照原样再写一遍的 CASE；
    - UNION、UNION ALL 的不同支：各查各的行，结果摞在一起。
    别的都按同一批行上的条件算，宁可多换：ON 和 WHERE、WHERE 和 HAVING、INTERSECT 两支、EXCEPT 两支（前一支成立、
    后一支不成立）、包在 CASE 条件 / 函数 / 算式里的一半、选出列里的 FILTER 或 CASE 和 WHERE、子查询和 CTE 里的筛选
    和外层。看的是两处的最近公共祖先，和它往下分别经哪一项走到两处；paths 记下走过的祖先链，同一个条件只走一遍。"""
    from sqlglot import exp

    steps = 0
    path = paths.get(id(a))
    if path is None:
        path, child, node = {}, None, a
        while node is not None:
            path[id(node)] = child
            child, node = node, node.parent
        paths[id(a)], steps = path, len(path)
    child, node = None, b
    while node is not None and id(node) not in path:
        child, node = node, node.parent
        steps += 1
    if node is None or child is None or path[id(node)] is None:
        # 一处套在另一处里面（WHERE 的且里有个 CASE，CASE 的条件是另一个比较）：经且、或连着，筛的是同一批行
        return False, steps
    keys = {path[id(node)].arg_key, child.arg_key}
    if isinstance(node, exp.Case):
        return keys <= {"ifs", "default"}, steps
    if isinstance(node, exp.If):
        return keys == {"true", "false"}, steps
    if isinstance(node, exp.Select):
        return keys <= _VALUE_ARGS, steps
    return isinstance(node, exp.Union) and keys == {"this", "expression"}, steps


def _marks(value: Any, op: str) -> list[tuple[Any, int]]:
    """一个边界从哪边隔开了哪个值：(值, 0) 是从下面隔开（它和比它小一点的值结果不同：>= v、< v），(值, 1) 是从上面
    隔开（和比它大一点的值结果不同：<= v、> v）；= v、<> v 从两边隔开 v。整数上还隔开了相邻的那一个：> v、<= v 从下面
    隔开 v + 1（v 不成立、v + 1 成立，或者反过来），< v、>= v 从上面隔开 v - 1——age <= 24 OR age >= 26 就是
    age <> 25；= v、<> v 从上面隔开 v - 1、从下面隔开 v + 1。边界不是整数的，整数上隔开的是它两边紧挨着的两个整数
    （age > 24.5 就是 age >= 25）。"""
    if not isinstance(value, Decimal):
        return {">=": [(value, 0)], "<=": [(value, 1)], ">": [(value, 1)], "<": [(value, 0)],
                "=": [(value, 0), (value, 1)], "<>": [(value, 0), (value, 1)]}[op]
    if value == value.to_integral_value():
        above, below = [(value + 1, 0)], [(value - 1, 1)]
        return {">=": [(value, 0), *below], "<=": [(value, 1), *above], ">": [(value, 1), *above],
                "<": [(value, 0), *below], "=": [(value, 0), (value, 1), *below, *above],
                "<>": [(value, 0), (value, 1), *below, *above]}[op]
    # 不是整数：整数上 = v 永远不成立、<> v 永远成立，隔不开谁
    if op in ("=", "<>"):
        return [(value, 0), (value, 1)]
    steps = [(value.to_integral_value(rounding=ROUND_CEILING), 0), (value.to_integral_value(rounding=ROUND_FLOOR), 1)]
    return [(value, 0 if op in (">=", "<") else 1), *steps]


def _pinned(root: Any, values: dict[int, Any], lifts: dict[int, Any]) -> dict[int, tuple[str, ...]]:
    """把某一个值夹住了的边界字面量：id(字面量节点) → 夹住的值（_point_text 的写法）。values 是 _constants 算好的
    写死的值，lifts 见 _lifts。

    一个值 v 两边都被隔开（_marks），条件才可能只认 v 这一个值。范围比较在整条 SQL 里配对，只有分档的几种写法各算
    各的（_apart：CASE 的不同分支、SELECT 里不筛行的不同项、UNION 的不同支），它们共用的边界是分档的界，不是在找哪
    一个值；别处筛的是同一批行（ON 和 WHERE、WHERE 和 HAVING、INTERSECT 两支、包在 CASE / 函数 / 算式里的一半、
    子查询、CTE），两边一隔开就按夹住算——只在同一个条件里配对的话，把找值拆到这些地方，号码就照原样留在 SQL 里，
    回答正文也跟着不遮。同一个条件里的再细看合起来的真假（_isolated）：col >= v AND col <= v、BETWEEN v AND v、
    col > v OR col < v（就是 col <> v）、整数的 col >= v AND col < v + 1、col > v - 1 AND col < v + 1、两头不是整数的
    col > v - 0.5 AND col < v + 0.5 是在找 v，和等值比较一样换；范围再用不等、NOT IN 去掉一头的（age >= 25 AND
    age <= 26 AND age <> 26）也是，不等、IN 只在同一个条件里和范围合起来看（跨条件配对只看范围比较：选出列里
    CASE WHEN age = 26 和 WHERE age >= 25 说不上在找谁）；col > 31 AND col <= 25 AND col NOT BETWEEN 25 AND 31 这样
    谁也找不着的、col <= v OR col >= v 这样全都要的不是。另一边不是单独一列的（SUM(amount) >= 900）只和写得一样的
    表达式配（_key_of）。夹住了就把隔开 v 的边界都记下：两头都不是 v 的（col > 24 AND col < 26）、边界是算式的
    （col <= 25 + 0），算式里的数都换，留着就等于写出了 v。
    细看、找公共祖先走的步数有上限（PIN_WORK），超出之后两边都有范围比较隔开的一律按夹住算。"""
    from sqlglot import exp

    exact_types = (*_exact_ops(), exp.In)
    tops: dict[int, Any] = {}
    within: dict[int, dict[int, _Leaf]] = {}
    # 范围比较隔开的、等值一类（不等、IN）隔开的分开记：(键, 值) → (从下面隔开的, 从上面隔开的)，各按条件分组
    ranged: dict[tuple[str, Any], tuple[dict[int, list[Any]], dict[int, list[Any]]]] = {}
    exact: dict[tuple[str, Any], tuple[dict[int, list[Any]], dict[int, list[Any]]]] = {}
    # 同一个条件里等值一类写着的数（按条件、键）：整数上细看时，紧挨着的这些数要越过去再看（_window）
    nearby: dict[tuple[int, str], set[Decimal]] = {}
    for node in root.find_all(exp.Between, *_range_ops(), *exact_types):
        is_exact = isinstance(node, exact_types)
        parts = _halves(node, values, lifts)
        if is_exact and all(h is None for h in parts):
            continue  # 连接条件（两边都是列）这类：和夹住哪个值无关
        top = _condition_root(node)
        tops[id(top)] = top
        within.setdefault(id(top), {})[id(node)] = (parts, isinstance(node, exp.In), is_exact)
        marks = exact if is_exact else ranged
        for literals, key, value, op in (h for h in parts if h is not None):
            if is_exact and isinstance(value, Decimal):
                nearby.setdefault((id(top), key), set()).add(value)
            for point, side in _marks(value, op):
                marks.setdefault((key, point), ({}, {}))[side].setdefault(id(top), []).extend(literals)
    budget = PIN_WORK
    orders: dict[int, list[tuple[Any, list[Any]]]] = {}
    apart: dict[tuple[int, int], bool] = {}
    paths: dict[int, dict[int, Any]] = {}
    out: dict[int, set[str]] = {}
    for (key, point), (below, above) in ranged.items():
        low_exact, high_exact = exact.get((key, point), ({}, {}))
        marked: list[Any] = []
        # 不在同一个条件里的两边（只看范围比较）：不是各算各的，就按夹住算
        for low, lows in below.items():
            for high, highs in above.items():
                if low == high:
                    continue
                pair = (low, high) if low < high else (high, low)
                if pair not in apart:
                    apart[pair], steps = _apart(tops[pair[0]], tops[pair[1]], paths) if budget > 0 else (False, 0)
                    budget -= steps
                if not apart[pair]:
                    marked += lows + highs
        # 同一个条件里的两边（至少一边是范围比较）：细看合起来的真假
        for top in below.keys() | above.keys():
            lows = below.get(top, []) + low_exact.get(top, [])
            highs = above.get(top, []) + high_exact.get(top, [])
            if not (lows and highs):
                continue
            if top not in orders:
                orders[top] = _logic_order(tops[top])
            if len(orders[top]) <= budget:
                budget -= len(orders[top])
                if _isolated(orders[top], within[top], key, point, nearby.get((top, key), set())):
                    marked += lows + highs
            elif top in below and top in above:
                marked += below[top] + above[top]
        for literal in marked:
            out.setdefault(id(literal), set()).add(_point_text(point))
    return {literal: tuple(sorted(points)) for literal, points in out.items()}


#: 条件里和这一列无关的部分（别的列、函数、子查询）：这一列怎么取它都不变，可能真也可能假
_EITHER = frozenset({(False, False, False), (True, True, True)})
#: 只认中间那一个值（或者只排除它）：(比它小一点, 它, 比它大一点) 的真假
_ISOLATED = frozenset({(False, True, False), (True, False, True)})
_HOLDS: dict[str, Callable[[int], bool]] = {
    ">=": lambda s: s >= 0, ">": lambda s: s > 0, "<=": lambda s: s <= 0, "<": lambda s: s < 0,
    "=": lambda s: s == 0, "<>": lambda s: s != 0}
#: 且、或连起来的一串里，两两细看的项最多这么多（随这一列取值变真假的项）：再多就按夹住算。整数上细看时，紧挨着
#: 的一串不等、IN 写着的数最多越过这么多个（_window）
PAIR_MAX = 64


def _isolated(order: list[tuple[Any, list[Any]]], halves: dict[int, _Leaf], key: str, point: Any,
              nearby: set[Decimal] | frozenset[Decimal] = frozenset()) -> bool:
    """这个条件（order 是它的各层，_logic_order）里 key 取 point 时夹没夹住，两种都算：
    - 整个条件只认 point 这一个值，或者只排除它（真假和它两边都不一样）。数按两种看：连续的（紧挨着 point 两边的值）
      和整数的（point - 1、point + 1，col >= v AND col < v + 1 只在整数上夹住 v）。整数上紧挨着的是同一个条件里
      不等、IN 写着的数（nearby）时越过去接着看（_window）：age >= 25 AND age <= 30 AND age <> 26 里 26 被去掉了，
      27 照样成立，不只认 25；age <= 26 时 27 不成立，就只认 25；
    - 条件里哪一层（一个比较、括号、非、异或，且 / 或连起来的一串里的任意一项、任意两项）不论整数都只认 point：
      BETWEEN v AND v、>= v AND <= v、> v OR < v 本身就写出了 v，旁边的条件把它吸收了（OR 一个更宽的范围）、和它
      矛盾了也算，隔着别的条件写也一样。不等、IN 不这样一层层看（单拿出来本来就只认、只排除写着的值，它们自己的
      字面量照等值比较换），整数上的半开、开区间也不：一长串相邻的分档（age >= 0 AND age < 1 OR age >= 1 AND
      age < 2 …）每一档单拿出来都只认一个整数，连起来谁也没找。
    条件里和这一列无关的部分当成不知道真假，只要有一种取法夹住了就算（宁可多换）。"""
    if _outcomes(order, halves, key, point, None, signals=True)[1]:
        return True
    if not (isinstance(point, Decimal) and point == point.to_integral_value()):
        return False
    offsets = _window(point, nearby)
    if offsets is None:
        return True
    lone = tuple(o == 0 for o in offsets)
    return bool({lone, tuple(not x for x in lone)} & _outcomes(order, halves, key, point, offsets)[0])


def _window(point: Decimal, nearby: set[Decimal] | frozenset[Decimal]) -> tuple[int, ...] | None:
    """整数上细看 point 时看哪几个整数（相对 point 的偏移）：两边各看一个；紧挨着的是不等、IN 写着的数，越过它们再
    多看一个。一边要越过的超过 PAIR_MAX 个时为 None（看不过来，按夹住算）。"""
    below = above = 0
    while point - below - 1 in nearby:
        below += 1
        if below > PAIR_MAX:
            return None
    while point + above + 1 in nearby:
        above += 1
        if above > PAIR_MAX:
            return None
    return tuple(range(-below - 1, above + 2))


def _logic_children(node: Any) -> list[Any]:
    """条件往下一层：括号、非里面的那一个，异或的两边；且（或）连起来的一串整串摊开，越过同一种连接和套在外面的括号，
    一项一项给——这样隔着别的条件写的两项也挨得着（_outcomes 两两细看）。比较和别的写法没有下一层。"""
    from sqlglot import exp

    if isinstance(node, (exp.Paren, exp.Not)):
        return [node.this]
    if isinstance(node, exp.Xor) and node.this is not None and node.expression is not None:
        return [node.this, node.expression]
    if not (isinstance(node, (exp.And, exp.Or)) and node.this is not None and node.expression is not None):
        return []
    kind, items, stack = type(node), [], [node]
    while stack:
        item = inner = stack.pop()
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if type(inner) is kind and inner.this is not None and inner.expression is not None:
            stack.extend((inner.expression, inner.this))
        else:
            items.append(item)
    return items


def _logic_order(top: Any) -> list[tuple[Any, list[Any]]]:
    """一个条件里的各层（且 / 或连起来的串、非、括号、比较），先下层后上层排好，每层带着它的下一层：一个条件要细看
    好几个值，摊开一次、反复用。有几层就是看一次要走的步数（PIN_WORK）。不用递归：几百个条件连成的一长串会超过
    递归深度。"""
    order: list[tuple[Any, list[Any]]] = []
    stack: list[tuple[Any, list[Any] | None]] = [(top, None)]
    while stack:
        node, kids = stack.pop()
        if kids is None and (kids := _logic_children(node)):
            stack.append((node, kids))
            stack.extend((kid, None) for kid in kids)
            continue
        order.append((node, kids))
    return order


def _outcomes(order: list[tuple[Any, list[Any]]], halves: dict[int, _Leaf], key: str, point: Any,
              offsets: tuple[int, ...] | None, *, signals: bool = False) -> tuple[set[tuple[bool, ...]], bool]:
    """条件（各层见 _logic_order）在 key 取 point 和它两边时可能的真假组合。offsets 是整数上看的那几个偏移
    （_window），为 None 时按紧挨着的值看 (比它小一点, point, 比它大一点)。signals 为真时另交回条件里有没有哪一层、
    且 / 或连起来的哪一两项单拿出来就只认 point（_isolated，只按紧挨着的值看）。"""
    from sqlglot import exp

    either = _either(3 if offsets is None else len(offsets))
    done: dict[int, set[tuple[bool, ...]]] = {}
    found = False
    for node, kids in order:
        if isinstance(node, exp.Paren):
            out = done[id(kids[0])]
        elif isinstance(node, exp.Not):
            out = {tuple(not x for x in t) for t in done[id(kids[0])]}
        elif kids:
            join = operator.and_ if isinstance(node, exp.And) else operator.or_ if isinstance(node, exp.Or) \
                else operator.xor
            parts = [done[id(kid)] for kid in kids]
            out = parts[0]
            for part in parts[1:]:
                out = _joined(join, out, part)
            if signals and not found and len(parts) > 2:
                # 一串里随这一列取值变真假的项两两细看（两项的串就是它自己，下面照常看）
                varying = [part for part in parts if not part <= either]
                found = len(varying) > PAIR_MAX or any(
                    _ISOLATED & _joined(join, s, t) for s, t in itertools.combinations(varying, 2))
        else:
            out = _leaf_outcomes(halves.get(id(node)), key, point, offsets)
        found = found or (signals and bool(_ISOLATED & out))
        done[id(node)] = out
    return out, found


def _either(width: int) -> frozenset[tuple[bool, ...]]:
    """和这一列无关的部分在 width 个取值上的真假：全真或全假（_EITHER 是三个取值的）。"""
    return _EITHER if width == 3 else frozenset({(False,) * width, (True,) * width})


def _joined(join: Callable[[bool, bool], bool], left: set[tuple[bool, ...]], right: set[tuple[bool, ...]]
            ) -> set[tuple[bool, ...]]:
    return {tuple(join(x, y) for x, y in zip(s, t)) for s in left for t in right}


def _leaf_outcomes(leaf: _Leaf | None, key: str, point: Any, offsets: tuple[int, ...] | None
                   ) -> set[tuple[bool, ...]]:
    """一个比较（几半的「且」，IN 是几半的「或」）可能的真假组合；别的条件当成不知道真假。不等、IN 只在整数上细看
    时算（offsets 不为 None），按紧挨着的值看时也当成不知道真假，见 _isolated。"""
    either = _either(3 if offsets is None else len(offsets))
    if leaf is None or (leaf[2] and offsets is None):
        return set(either)
    parts, any_, _ = leaf
    out: set[tuple[bool, ...]] = {(not any_,) * len(next(iter(either)))}
    join = operator.or_ if any_ else operator.and_
    for half in parts:
        cell = {_truth(half, point, offsets)} if half is not None and half[1] == key and (
            isinstance(half[2], Decimal) == isinstance(point, Decimal)) else either
        out = _joined(join, out, cell)
    return out


def _truth(half: _Half, point: Any, offsets: tuple[int, ...] | None) -> tuple[bool, ...]:
    """一半比较在 point 和它两边的真假（offsets 同 _outcomes）。"""
    _, _, bound, op = half
    if offsets is not None:
        signs = [_sign(point + step, bound) for step in offsets]
    else:
        at = _sign(point, bound)
        signs = [at or -1, at, at or 1]
    return tuple(_HOLDS[op](s) for s in signs)


def _sign(a: Any, b: Any) -> int:
    return (a > b) - (a < b)


#: SQL 里一个字面量用在哪：(用法, 被比较的一边取自哪些列, 认它时一并看的值)，见 _literal_uses
_Use = tuple[str, frozenset[str], tuple[str, ...]]


def _literal_uses(sql: str, kind: str | None) -> dict[int, _Use] | None:
    """SQL 里每个字面量（按它在原文里的起始位置）用在哪，按语法树认（写法怎么变都认得出）：
    - ("equal", 列, 值)：在找某一个值——等值、不等、IN（含 VALUES、行值）、LIKE 一类、IS [NOT] DISTINCT FROM、
      简单 CASE 的 WHEN、= ANY(ARRAY[…])，套了括号、负号、CAST 的照认；把某一个值夹住的范围（_pinned）也算，夹住的
      值记在第三项（换不换按它认：整数的 age > 24 AND age < 26 找的是 25）；
    - ("range", 列, 值)：范围比较（> >= < <=、BETWEEN）的边界，是 agent 定的阈值；
    - ("limit", 空, 值)：LIMIT / OFFSET / FETCH / TOP 的行数；
    - ("other", 空, 值)：别的——选出列、函数参数、和列一起算的算式、THEN 后面的值、子查询里选出的值。
    列是被比较的一边取自哪些列（小写、不带表名，_columns_of：age、CAST(age AS INTEGER)、MAX(age) 都是 age）。
    字面量是几个写死的数算出来的算式的一截（13900000000 + 1）时，算出来的值也记在第三项：整个算式按它认，用法看
    算式摆在哪。解析不了返回 None。"""
    from app.data.sql_semantics import _parse, _read

    try:
        root, _ = _parse(sql, _read(kind))
    except Exception:  # noqa: BLE001 - 解析器的意外按解析不了处理
        root = None
    if root is None:
        return None
    # 解析的是去掉头尾空白的 SQL：位置补上开头的空白，才对得上原文里的词
    offset = len(sql) - len(sql.lstrip())
    uses: dict[int, _Use] = {}
    try:
        values = _constants(root)
        lifts = _lifts(root, values)
        pinned = _pinned(root, values, lifts)
        for node in root.walk():
            start = (node.meta or {}).get("start") if isinstance(node, _literal_types()) else None
            if not isinstance(start, int):
                continue
            use, columns, computed = _use_of(node, values, lifts)
            extra = (_point_text(computed),) if computed is not None else ()
            points = pinned.get(id(node))
            uses[start + offset] = ("equal", columns, tuple(dict.fromkeys((*points, *extra)))) if points \
                else (use, columns, extra)
    except Exception:  # noqa: BLE001 - 分析本身的缺陷不能让回答出错：按解析不了处理（认不准的地方不免）
        logger.warning("质询的 SQL 字面量分析失败", exc_info=True)
        return None
    return uses


def _requote(raw: str, change: Callable[[str], str]) -> str:
    """一个字面量或名字的原文 → 只换里面的字：引号、N'' E'' U&'' 这类前缀、$tag$ 照留；不带引号的整个交给 change。"""
    if raw.startswith("$") and (end := raw.find("$", 1)) > 0:
        tag = raw[:end + 1]
        if len(raw) >= 2 * len(tag) and raw.endswith(tag):
            return tag + change(raw[len(tag):-len(tag)]) + tag
    for opening, closing in (("'", "'"), ('"', '"'), ("`", "`"), ("[", "]")):
        start = raw.find(opening)
        if start >= 0 and len(raw) - start >= 2 and raw.endswith(closing):
            return raw[:start + 1] + change(raw[start + 1:-1]) + closing
    return change(raw)


def _sql_tokens(sql: str, kind: str | None) -> tuple[list[Any], list[str]] | None:
    from app.data.sql_semantics import _read, _tokens

    try:
        tokens = _tokens(sql, _read(kind))
    except Exception:  # noqa: BLE001 - 分词器认不了的写法（引号没闭合这类）
        return None
    return tokens, [t.token_type.name for t in tokens]


def _literals(sql: str, kind: str | None, secrets: _Secrets, compared: set[str]
              ) -> tuple[list[Any], list[str], dict[int, _Use]] | None:
    """分词，加上每个字面量（按词的下标）用在哪（_literal_uses）。语法树里对不上位置的字面量记 "other"；SQL 解析
    不了时只按词认简单的等值比较（_context），别的一律 "other"——认不准的地方不免。分词出错返回 None。

    照原样留着的数还得在这条 SQL 里没有别处被换掉：同一个数在别的字面量里（等值比较、算式、字符串、夹住的范围）
    确实被换掉了，这里留着就把它露了（buyer_phone >= 13900000001 AND buyer_phone - 13900000001 < 5、age >= 30 AND
    age = 30；边界是算式的，算出来的值被换掉了也算：amount >= 15000 + 15000 OR amount = 30000），所以这里改记成在找
    这个值，再照等值的办法换：阈值也和遮罩列比较的（短的数也换）、数够长的换；别的列上的短阈值、LIMIT 的行数
    （age = 30 AND visits >= 30、LIMIT 30）换不掉，照原样——短的数本来就不按值遮（SECRET_DIGITS），撞上同一个数只是
    巧合，一律换会把阈值、行数改得面目全非（见模块说明的已知局限）。
    换没换照 _swap_literal 认，和真去换的是同一套判断：只是写在日期、天数修饰、标签里（'2026-01-31'、'-30 days'、
    '30岁以下'），或者没设遮罩的列上的短的数（amount = 30），那里一个字没换，阈值也照原样。名字、注释里的不算：那是
    agent 给阈值起的说法（AS 超过30000的笔数），照常按值换，留着的阈值不必跟着换。"""
    split = _sql_tokens(sql, kind)
    if split is None:
        return None
    tokens, kinds = split
    uses = _literal_uses(sql, kind)
    found: dict[int, _Use] = {}
    for i, token in enumerate(tokens):
        if not _is_literal(kinds[i]):
            continue
        if uses is not None:
            found[i] = uses.get(token.start, ("other", frozenset(), ()))
        else:
            use, column = _context(tokens, kinds, i)
            found[i] = (use, frozenset({column} if column else ()), ()) if use == "equal" \
                else ("other", frozenset(), ())
    kept = {i for i, (use, _, _) in found.items() if kinds[i] == "NUMBER" and use in _KEPT}
    swapped = _swapped(sql, tokens, kinds, found, kept, secrets, compared)
    for i in kept:
        _, columns, points = found[i]
        if any(_digits_of(v) in swapped for v in (tokens[i].text, *points)):
            found[i] = ("equal", columns, points)
    return tokens, kinds, found


def _swapped(sql: str, tokens: list[Any], kinds: list[str], uses: dict[int, _Use], skip: set[int],
             secrets: _Secrets, compared: set[str]) -> set[str]:
    """这几个字面量（skip 以外的）里确实换掉的数（_swap_literal），去掉千分位、正负号。"""
    out: set[str] = set()
    for i, use in uses.items():
        if i not in skip:
            raw = sql[tokens[i].start:tokens[i].end + 1]
            out |= _swap_literal(raw, tokens[i].text, kinds[i], use, secrets, compared)[1]
    return out


def _swap_literal(raw: str, text: str, kind: str, use: _Use, secrets: _Secrets, compared: set[str]
                  ) -> tuple[str, set[str]]:
    """SQL 里一个字面量（原文 raw、分出来的字 text、词的种类 kind）换成什么样，和换掉了哪些遮罩数（去掉千分位、正负号）。
    _scrub_sql 照它换，_literals 照它认别处确实换掉的数，两处是同一套判断：

    - 在找某一个值的（equal）：和遮罩列（或它的别名，compared；套了 CAST、汇总的也算，见 _columns_of）比较的，是遮罩
      列里出现过的值就整个换，短的数也换（age = 25）；和别的列比较的，够长的遮罩值也换（另一张表里没设遮罩的同一个
      手机号）。数换个写法照样认（25.0、025、2.5e1 按 25 认，_canonical）；夹住某一个值的范围按夹住的值认（age > 24
      AND age < 26 的两头都按 25 认），算式里的数按算出来的值认，同一个数在别处换掉了的阈值、行数也按等值认（见
      _literals）；
    - 字符串在哪都按值换里面的遮罩值（范围边界也一样：写成字符串的长号码做边界，多半是在找那一个人），换了写法的号码
      也认（LIKE '%199-1474-2785%'：字符串里的 199-1474-2785 不是算式）；换掉的号码按纯数字写法记，同一个数写成阈值的
      据此不留；
    - 别的数：范围比较的边界、LIMIT / OFFSET 的行数照原样（agent 自己定的阈值，换了封存的就不是实际执行的语句）；
      别处（选出列、函数参数、算式）认不准是不是在找某一个值，够长的遮罩值整个换（算出来的值是也换）。不带引号的数
      不按换了写法认：199-1474-2785 在 SQL 里是三个数相减。"""
    how, columns, points = use
    word = _masked_word()
    canonical = _canonical(text)
    looked = [text, *([canonical] if canonical not in (None, text) else []), *points]
    if how == "equal":
        if any((columns & compared and secrets.weak_hit(v)) or secrets.hit(v) for v in looked):
            return _requote(raw, lambda _: word), _number_forms(looked)
    if kind != "NUMBER":
        found: list[str] = []

        def scrub(inner: str) -> str:
            out, swapped = secrets.swap(inner, loose=True)
            found.extend(swapped)
            return out

        return _requote(raw, scrub), _number_forms(found)
    if how not in _KEPT and any(secrets.hit(v) for v in looked):
        return word, _number_forms(looked)
    return raw, set()


def _number_forms(values: Iterable[str]) -> set[str]:
    """几个换掉的值里是数的那几个（去掉千分位、正负号）：阈值只会和数撞上。"""
    return {_digits_of(text) for value in values if _NUMERIC.fullmatch(text := _plain_text(value))}


def _scrub_sql(sql: str, secrets: _Secrets, compared: set[str], kind: str | None) -> str:
    """SQL 里遮罩列的值换成「已遮罩」，别的一个字不动：查询清单随运行封存，它得是实际执行的那条，审批人看 what-if 的
    条件（阈值、日期、LIMIT、别的列的条件）看的就是它。字面量按用在哪换（_literals、_swap_literal）：在找某一个值的
    换，范围比较的边界、LIMIT / OFFSET 的行数照原样，别处的数够长的遮罩值换，字符串按值换；注释、名字（AS
    "13900000001 的单"、张伟的单数）也按值换（同正文：够长的数、两个字以上的文字）。
    分词出错就整条按正文的办法换；解析不了的认不准哪个数是阈值，范围边界、行数也不免（_literals）。"""
    if not sql or not (secrets.strong or secrets.weak):
        return sql
    split = _literals(sql, kind, secrets, compared)
    if split is None:
        return secrets.scrub(sql)
    tokens, kinds, uses = split
    return _rebuild(sql, tokens, kinds, secrets, lambda i, raw: _swap_literal(
        raw, tokens[i].text, kinds[i], uses[i], secrets, compared)[0] if i in uses else raw)


def _rebuild(sql: str, tokens: list[Any], kinds: list[str], secrets: _Secrets, literal: Callable[[int, str], str]
             ) -> str:
    """按词重写一段 SQL：字面量交给 literal（第几个词、原文）定换成什么，名字（带不带引号）、注释和别的字按值换。
    词与词之间只有空白和注释，名字里的数也不是算式：都按文字的办法换（换了写法的号码也认，/* 客户 199-1474-2785 */、
    AS "199-1474-2785 的备注"）。"""
    out: list[str] = []
    last = 0
    for i, token in enumerate(tokens):
        start, end = token.start, token.end + 1
        if start < last or end > len(sql):
            continue
        gap = sql[last:start]
        out.append(secrets.scrub(gap, loose=True) if gap.strip() else gap)
        raw = sql[start:end]
        if _is_literal(kinds[i]):
            raw = literal(i, raw)
        elif kinds[i] in _NAME_TOKENS:
            raw = _requote(raw, lambda inner: secrets.scrub(inner, loose=True))
        out.append(raw)
        last = end
    rest = sql[last:]
    out.append(secrets.scrub(rest, loose=True) if rest.strip() else rest)
    return "".join(out)


def _literal_swaps(sql: Any, secrets: _Secrets, compared: set[str], kind: str | None) -> set[str] | None:
    """整条 SQL 里整个换掉了的字面量（分出来的字，字符串不带引号）：SQL 检查结果的片段、说明照它换（_masked_checks）。
    没有 SQL、分词都分不了时为 None（那时整条 SQL 也只是按值换）。"""
    if not isinstance(sql, str) or not sql.strip():
        return None
    if not (secrets.strong or secrets.weak):
        return set()
    split = _literals(sql, kind, secrets, compared)
    if split is None:
        return None
    tokens, kinds, uses = split
    word = _masked_word()
    out: set[str] = set()
    for i, use in uses.items():
        raw = sql[tokens[i].start:tokens[i].end + 1]
        if tokens[i].text and _swap_literal(raw, tokens[i].text, kinds[i], use, secrets, compared)[0] in (
                word, _requote(raw, lambda _: word)):
            out.add(tokens[i].text)
    return out


def _scrub_excerpt(text: str, whole: set[str] | None, secrets: _Secrets, kind: str | None) -> str:
    """SQL 检查结果的片段：检查器按语法树重新写出的一段表达式（最长 160 字，再长截断加「…」），单独拿出来认不出在找
    哪一个值、哪个数是阈值，照整条 SQL 的认法换：整条 SQL 里整个换掉了的字面量（whole），片段里写着同样的字面量也换
    （同一个值在片段里别处出现也换，宁可多换）；整条 SQL 里照原样留着的阈值，片段里也照原样；字符串、名字、注释按值
    换（同 _scrub_sql）。截断处可能正好截在一个值中间（'1390000…），那半截按值认不出，去掉（_cut_tail）。"""
    cut = text.endswith("…")
    body = _cut_tail(text[:-1]) if cut else text
    split = _sql_tokens(body, kind) if whole is not None and body.strip() else None
    word = _masked_word()
    if split is None:
        # 分不了词（截断后引号没闭合这类）：按值换，整条 SQL 里整个换掉的字面量也按值找
        out = secrets.scrub(body)
        values = sorted((v for v in whole or () if v.strip()), key=lambda v: (-len(v), v))
        out = re.sub("|".join(_pattern(v) for v in values), word, out) if values else out
    else:
        tokens, kinds = split

        def literal(i: int, raw: str) -> str:
            if tokens[i].text in whole:
                return word if kinds[i] == "NUMBER" else _requote(raw, lambda _: word)
            return raw if kinds[i] == "NUMBER" else _requote(raw, lambda inner: secrets.scrub(inner, loose=True))

        out = _rebuild(body, tokens, kinds, secrets, literal)
    return out + "…" if cut else out


def _cut_tail(body: str) -> str:
    """截断的片段去掉截断处半截的值：停在没闭合的字符串、带引号的名字、注释里的，从开头的引号、注释起整个去掉（地址
    这类遮罩值中间有空格，只去掉最后一段还会留下前半截），紧贴在引号前的前缀（N'、E'、U&'）一起去掉；停在不带引号
    的词中间的，去掉那半个词。"""
    opened = _unclosed(body)
    if opened is not None:
        return re.sub(r"[0-9A-Za-z_&]*$", "", body[:opened])
    return re.sub(r"\S*$", "", body)


def _unclosed(text: str) -> int | None:
    """text 停在没闭合的字符串、带引号的名字（'…、"…、`…）或注释（/* …、-- …）里时，交回它开头的位置；不然为 None。
    引号里两个引号连写是引号本身。"""
    i = 0
    while i < len(text):
        if text.startswith(("/*", "--"), i):
            end = text.find("*/" if text[i] == "/" else "\n", i + 2)
            if end < 0:
                return i
            i = end + 2 if text[i] == "/" else end + 1
        elif (quote := text[i]) in "'\"`":
            end = i + 1
            while (end := text.find(quote, end)) >= 0 and text.startswith(quote * 2, end):
                end += 2
            if end < 0:
                return i
            i = end + 1
        else:
            i += 1
    return None


#: SQL 检查结果的说明里照引的取值：「…」括起来
_QUOTED = re.compile(r"「([^「」]*)」")


def _scrub_message(text: str, whole: set[str] | None, secrets: _Secrets) -> str:
    """SQL 检查结果的说明：照引 SQL 里写的取值（码值表已列全时「没有码值「3」」），整条 SQL 里整个换掉了的（whole），
    这里一样换成「已遮罩」，负数照留负号（同 SQL 里）；别的字按值换。码值表里列出的码值（「已列出的全部码值：
    线上直销=…」）是数据目录写明的，不从 SQL 摘，也不另外免遮：和别的字一样按值换，恰好是按值认得出的遮罩值的码值
    （两个字以上的文字、够长的数）换成「已遮罩」，多遮，不漏；短的码值照原样。"""
    word = _masked_word()

    def quoted(m: re.Match[str]) -> str:
        inner = m.group(1)
        if inner in whole:
            return f"「{word}」"
        if inner[:1] in ("-", "+") and inner[1:] in whole:
            return f"「{inner[0]}{word}」"
        return m.group()

    return secrets.scrub(_QUOTED.sub(quoted, text) if whole else text)


def _shown_literals(sqls: Iterable[tuple[str, str | None, set[str]]], secrets: _Secrets) -> set[str]:
    """几条 SQL（各自数据源的类型、SQL 里认哪些名字是遮罩列，见 _compared）里照原样留着的数，去掉千分位、正负号：
    范围比较的边界、LIMIT / OFFSET 的行数（_scrub_sql 不换的那两种；边界是算式的，算出来的值也算：20000 * 1.5 就是
    30000）。它们随 SQL 摆在读者眼前，回答正文里复述（「金额不低于 30000 元」）再遮也遮不住什么，只会让回答和 SQL
    对不上（_Secrets.show）。同一个数在别的查询里确实被换掉了
    也不算（同一条里的 _literals 已经不留）：正文写出来就把那里换掉的值露了；只是写在日期、批次号、标签里没换的不牵连。
    分词都分不了的整条按值换（同 _scrub_sql），换掉的数也算。"""
    kept: set[str] = set()
    swapped: set[str] = set()
    for sql, kind, compared in sqls:
        if not sql:
            continue
        split = _literals(sql, kind, secrets, compared)
        if split is None:
            pattern = secrets.pattern(sql)
            swapped |= _number_forms(m.group() for m in pattern.finditer(sql)) if pattern is not None else set()
            continue
        tokens, kinds, uses = split
        keep = {i for i, (use, _, _) in uses.items() if kinds[i] == "NUMBER" and use in _KEPT}
        kept |= {_digits_of(v) for i in keep for v in (tokens[i].text, *uses[i][2])}
        swapped |= _swapped(sql, tokens, kinds, uses, keep, secrets, compared)
    return kept - swapped


@dataclass
class _Plan:
    """一次回答（或一次取质询工件）的遮罩：agent 每份查询快照遮好的样子（views）和遮掉的列（masked）、SQL 里指代
    遮罩列的名字（tainted）、数据源的类型（kinds，SQL 按它的方言分词）、遮罩列里出现过的值（secrets），以及已经不在的
    数据源（gone，按查询当时记下的遮罩处理）。replay：回答时只靠 agent 查询时遮掉的值（seen，只在内存里）才换掉的
    地方，按快照记成位置（masking.view_edits），写进质询事件，事后取质询工件照着再换一遍；取工件时为空。"""

    views: dict[str, dict[str, Any]]
    masked: dict[str, list[str]]
    tainted: dict[str, set[str]]
    kinds: dict[str, str | None]
    secrets: _Secrets
    gone: dict[str, list[str]]
    replay: dict[str, dict[str, Any]] = field(default_factory=dict)


async def _source_kinds(names: Iterable[str]) -> dict[str, str | None]:
    wanted = sorted({n for n in names if n})
    if not wanted:
        return {}
    async with SessionLocal() as session:
        rows = (await session.execute(select(DataSource.name, DataSource.kind)
                                      .where(DataSource.name.in_(wanted)))).all()
    return {str(name): kind for name, kind in rows}


async def _plan(own: dict[str, dict[str, Any]], others: list[dict[str, Any]], given: list[str | None],
                shown: list[tuple[str, str, str]] | None = None, live: dict[str, list[str]] | None = None, *,
                embedded: frozenset[str] = frozenset(), seen: _Secrets | None = None) -> _Plan:
    """own：agent 自己的查询快照（工件 id → 内容）；others：报告自己的查询快照（被问的那句可能直接写着遮罩列的值，
    agent 会照着复述、写进查询条件）；given：问题、选中的原文、报告里列出的指标和运行输入的值（都已经摆在读者眼前，
    里面的值哪里都不换）；shown：agent 执行的查询，(SQL, 数据源名, 快照工件 id)（照原样留在 SQL 里的阈值、LIMIT，
    回答正文里不换，见 _Secrets.show；取质询工件不用：快照里的格子照旧按值遮）；live：用得上的数据源现在设的遮罩列
    （快照没取到的查询按它认 SQL 里的比较，见 _compared）；embedded：这几份快照的文字格里夹着的遮罩值也按子串换
    （只给要取的那份质询工件，见 _masked_view）；seen：agent 查询时在工具边界遮掉的值（verify_agent 交回的
    masked_values，只在内存里）。agent 自己的快照查询时就遮好了，遮罩列里收不到值；模型照着别处看到的号码写进回答、
    查询条件时，按值遮要靠它认出来。

    查数据源的遮罩要读库，在这里读；解析 SQL、收上万个值、逐格比对是纯计算，放进线程：放在事件循环里会挡住同一进程
    里别的运行（同 tools/datasource 的 _sql_checks）。"""
    from app.api.evidence import _Masks

    masks = _Masks()
    shown = shown or []
    named = {artifact: await _hidden_columns(snapshot, masks) for artifact, snapshot in own.items()}
    based = [(snapshot, (await _hidden_columns(snapshot, masks))[0]) for snapshot in others]
    kinds = await _source_kinds([*(str(s.get("source") or "") for s in [*own.values(), *others]),
                                 *(name for _, name, _ in shown)])
    return await asyncio.to_thread(_settle_plan, own, named, based, kinds, given, shown, live or {}, embedded, seen)


def _settle_plan(own: dict[str, dict[str, Any]], named: dict[str, tuple[set[str], list[str]]],
                 based: list[tuple[dict[str, Any], set[str]]], kinds: dict[str, str | None],
                 given: list[str | None], shown: list[tuple[str, str, str]], live: dict[str, list[str]],
                 embedded: frozenset[str], seen: _Secrets | None = None) -> _Plan:
    secrets = _Secrets()
    hidden: dict[str, set[str]] = {}
    tainted: dict[str, set[str]] = {}
    for artifact, snapshot in own.items():
        kind, base = kinds.get(str(snapshot.get("source") or "")), named[artifact][0]
        hidden[artifact], tainted[artifact] = _derived_hidden(snapshot, base, kind)
        secrets.add(snapshot, hidden[artifact], labels=_snapshot_labels(snapshot, kind), base=base)
    # agent 查询时遮掉的值先收（报告快照里的值收满了上限 SECRETS_MAX，也不丢 agent 自己查到的）；不带它们的一份另算，
    # 比出事后取质询工件时认不出、要照位置重放的地方
    baseline = secrets.copy() if seen is not None and seen.strong else None
    secrets.merge(seen)
    for target in (secrets, baseline):
        if target is not None:
            _add_based(target, based, kinds)
            target.protect(given)
    secrets.show(_shown_literals([(sql, kinds.get(name), _compared(tainted, artifact, live, name))
                                  for sql, name, artifact in shown], secrets))
    secrets.prepare()
    views: dict[str, dict[str, Any]] = {}
    masked: dict[str, list[str]] = {}
    replay: dict[str, dict[str, Any]] = {}
    for artifact, snapshot in own.items():
        views[artifact], masked[artifact] = _masked_view(snapshot, hidden[artifact], secrets,
                                                         embedded=artifact in embedded)
        if baseline is not None and (edits := view_edits(snapshot, hidden[artifact], secrets, baseline)):
            replay[artifact] = edits
    return _Plan(views=views, masked=masked, tainted=tainted, kinds=kinds, secrets=secrets,
                 gone={artifact: named[artifact][1] for artifact in own}, replay=replay)


def _add_based(secrets: _Secrets, based: list[tuple[dict[str, Any], set[str]]], kinds: dict[str, str | None]) -> None:
    """收报告自己的查询快照里遮罩列的值（based：快照和它要遮的列），按 SQL 认出改了名字的遮罩列（_derived_hidden）。"""
    for snapshot, base in based:
        kind = kinds.get(str(snapshot.get("source") or ""))
        secrets.add(snapshot, _derived_hidden(snapshot, base, kind)[0], labels=_snapshot_labels(snapshot, kind),
                    base=base)


async def snapshot_secrets(snapshots: Iterable[Any], given: Iterable[str | None] = ()) -> _Secrets:
    """几份查询快照（报告自己的）里遮罩列的值，收法同回答时收报告快照（_plan），去掉已经摆在读者眼前的（given）。
    审批质询、反方审稿在派核查 agent 之前交给它的查询工具（run_verify_agent 的 masked_values）：agent 查到的别的表、
    备注里写着同一个号码时，交给模型之前就换掉，模型看不到。"""
    from app.api.evidence import _Masks

    masks = _Masks()
    based = [(snapshot, (await _hidden_columns(snapshot, masks))[0]) for snapshot in snapshots
             if _query_shaped(snapshot)]
    kinds = await _source_kinds(str(snapshot.get("source") or "") for snapshot, _ in based)
    asked = list(given)

    def collect() -> _Secrets:
        secrets = _Secrets()
        _add_based(secrets, based, kinds)
        secrets.protect(asked)
        secrets.prepare()
        return secrets

    return await asyncio.to_thread(collect)


def _compared(tainted: dict[str, set[str]], artifact: str, live: dict[str, list[str]], name: str) -> set[str]:
    """一条查询的 SQL 里认哪些名字是遮罩列（和它们做等值比较的，短的数也换）：快照认出的（_derived_hidden 的名字，
    含改了名字的）；快照没取到的（存不下、被删了），按这个源现在设的遮罩列认。"""
    return tainted.get(artifact) or {c.lower() for c in live.get(name, [])}


def _masking_loader(views: dict[str, dict[str, Any]]) -> Callable[[str], Any]:
    """组装回答文档用的 loader：agent 自己的查询快照交遮好的那份（引用到遮罩的格子渲染成「已遮罩」）；别的查询快照
    不交（回答的目录里只有 agent 自己的查询）；口径卡这类没有行的原样，哈希照旧复验。"""

    def load(artifact: str) -> Any:
        if artifact in views:
            return views[artifact]
        content = artifact_store.load(artifact)
        return None if _query_shaped(content) else content

    return load


# --------------------------------------------------------------------------
# 回答
# --------------------------------------------------------------------------


class _Leak(Exception):
    """组装出来的正文里还认得出遮罩值（渲染出来才露的，比如别的列里一格文字夹着手机号）：这份回答不交出去。"""


def _limit(outcome: VerifyOutcome) -> str | None:
    return outcome.limit if outcome.limit in LIMITS else None


def _error_words(outcome: VerifyOutcome, *, timeout_s: Any, daily_max_usd: Any) -> str | None:
    """核查 agent 没交成结果的原话 → 提问人看得懂的话（发生了什么、下一步）。原话里有内部的说法（「交结果」「按结构」、
    裸写的字段名），只进日志；模型不可用、调用失败的说明本来就是给人看的，照用。"""
    error = outcome.error
    if not error:
        return None
    if error.startswith("已达") and error.endswith("没能交结果"):
        if "每日" in error:
            cap = f"核查每日金额上限 {judging._usd(daily_max_usd)}" if daily_max_usd is not None else "核查每日金额上限"
            return ERR_DAILY.format(cap=cap)
        if "时长" in error:
            return _timeout_words(timeout_s)
        return ERR_COST
    if error.startswith("收尾时超过时长上限"):
        return _timeout_words(timeout_s)
    if error.startswith("核查模型没有按结构交结果"):
        return ERR_SHAPE
    if error == "核查模型没有给出回复":
        return ERR_SILENT
    if error.startswith("核查出错（"):
        return f"回答时出错{error[len('核查出错'):]}，请重新提问"
    return error


def _timeout_words(timeout_s: Any) -> str:
    seconds = timeout_s if isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool) else None
    return ERR_TIMEOUT.format(seconds=f"（{seconds:g} 秒）" if seconds is not None else "")


def _given_numbers(ask: Ask) -> list[str]:
    """问题、选中的原文里的数：提问人给的阈值、条件（「排除金额低于 500 的」），回答里复述它们不算没有出处的结论。
    查询结果里来的数照旧必须引用。"""
    return [token.raw for text in (ask.question, ask.quote) if text for token in extract_numbers(text)]


async def _answer(op: _Opening, ask: Ask, target: _Target | None, settings: dict[str, Any]) -> dict[str, Any]:
    """问 agent，组装回答文档，交回事件数据里回答的那几项。模型用不了、触顶、组装出错都落在 ok / limit / error 里：
    钱已经花了，回答没成也照样记一条。"""
    from app.api.settings import verify_model_spec

    doc = op.doc
    catalog = doc.get("catalog") if isinstance(doc.get("catalog"), dict) else {}
    masked = await judging.source_masks(catalog, loader=op.loader)
    excerpts = _excerpts(doc, catalog, target.cand, op.loader, masked) if target is not None else []
    refs = _report_refs(catalog)
    wanted = _run_sources(op.events)
    usable = await _usable_sources(op.run_id, wanted, op.versions)
    live, lost = {s: usable[s] for s in wanted if s in usable}, [s for s in wanted if s not in usable]
    part = settings.get("inquiry") if isinstance(settings.get("inquiry"), dict) else {}
    async with SessionLocal() as session:
        spec = await verify_model_spec(session, None, settings=settings)
    # 报告自己的查询快照：被问的那句可能直接写着遮罩列的值，agent 会照着复述；里面的遮罩值在派 agent 之前就交给它的
    # 查询工具（masked_values），agent 查到写着同一个号码的备注、别的表时，交给模型之前就换掉
    given = [ask.question, ask.quote, *_ref_values(refs)]
    others = [snapshot for entry in catalog.values() if isinstance(entry, dict) and entry.get("kind") == "query"
              and _query_shaped(snapshot := _quiet_load(op.loader, entry.get("artifact")))]
    outcome = await run_verify_agent(
        spec=spec, system=INQUIRY_SYSTEM, task=_task(op, ask, target, excerpts, refs, live, lost), sources=list(live),
        output_schema=INQUIRY_SCHEMA, max_queries=part.get("max_queries"), timeout_s=part.get("timeout_s"),
        daily_max_usd=settings.get("daily_max_usd"), run_id=op.run_id, data_versions=op.versions,
        masked_values=await snapshot_secrets(others, given))

    # 遮罩：agent 自己的查询快照（查询时就遮好了，遮掉的值在 outcome.masked_values 里）、报告自己的查询快照
    own: dict[str, dict[str, Any]] = {}
    for query in outcome.queries:
        snapshot = _quiet_load(artifact_store.load, query.get("artifact"))
        if _query_shaped(snapshot):
            own[str(query["artifact"])] = snapshot
    plan = await _plan(own, others, given,
                       [(str(q.get("sql") or ""), str(q.get("source") or ""), str(q.get("artifact") or ""))
                        for q in outcome.queries], live, seen=outcome.masked_values)

    text, doc_id, failed = "", None, None
    source = (outcome.rationale or {}).get("source") if outcome.result is not None else None
    if isinstance(source, str) and source.strip():
        try:
            answer = _compose(source, {**refs, **((outcome.rationale or {}).get("catalog") or {})}, run_id=op.run_id,
                              views=plan.views, secrets=plan.secrets, allow=_given_numbers(ask))
        except _Leak:
            logger.warning("质询的回答里有遮罩值遮不住，不交出去（运行 %s）", op.run_id)
            failed = LEAKED
        except Exception as e:  # noqa: BLE001 - 原文没组装成文档就不交出去：里面的引用没换成值，遮罩也没做
            logger.warning("质询的回答组装失败（运行 %s）", op.run_id, exc_info=True)
            failed = f"整理回答时出错（{describe_exception(e)}），请重新提问"
        else:
            answer = _with_notes(answer, op, outcome, part, lost, own)
            text = str(answer.get("markdown") or "")
            doc_id = await _store(answer, op, ask)
    ok = bool(text.strip())
    error = None
    if not ok:
        error = _error_words(outcome, timeout_s=part.get("timeout_s"), daily_max_usd=settings.get("daily_max_usd"))
        if outcome.error:
            logger.info("质询没能给出回答（运行 %s）：%s", op.run_id, outcome.error)
    queries = await asyncio.to_thread(_sealed_queries, outcome.queries, plan, live)
    return {
        "answer_text": text, "answer_doc": doc_id, "ok": ok, "limit": _limit(outcome),
        "error": None if ok else (error or failed or NO_ANSWER),
        # 结果一模一样的两次查询是同一份快照（工件按内容寻址）：引用列表里只记一次
        "inquiry_artifacts": list(dict.fromkeys(q["artifact"] for q in queries if q.get("artifact"))),
        "queries": queries,
        "model": outcome.model, "usage": {k: outcome.usage.get(k, 0) for k in USAGE_KEYS},
        # 只靠内存里的遮罩值才换掉的位置（不含值），质询工件照着再换一遍；没有就不加这个键，事件和以前一字不差
        **({REPLAY_KEY: plan.replay} if plan.replay else {}),
    }


def _sealed_queries(queries: list[dict[str, Any]], plan: _Plan, live: dict[str, list[str]]) -> list[dict[str, Any]]:
    """随运行封存的查询清单：每条只留约定的几项，SQL 里的遮罩值换掉。_scrub_sql 要解析 SQL，放在线程里（同 _plan）。"""
    out: list[dict[str, Any]] = []
    for q in queries:
        artifact, name = str(q.get("artifact") or ""), str(q.get("source") or "")
        compared = _compared(plan.tainted, artifact, live, name)
        out.append({**{k: q.get(k) for k in QUERY_KEYS},
                    "sql": _scrub_sql(str(q.get("sql") or ""), plan.secrets, compared, plan.kinds.get(name))})
    return out


def _quiet_load(loader: Callable[[str], Any], artifact: Any) -> Any:
    """取一份快照；取不到、哈希不符都当没有（这里只是为了找遮罩列的值，取不到的那份本来也渲染不出来）。"""
    if not artifact:
        return None
    try:
        return loader(str(artifact))
    except Exception:  # noqa: BLE001
        return None


def _compose(source: str, catalog: dict[str, Any], *, run_id: str, views: dict[str, dict[str, Any]],
             secrets: _Secrets, allow: list[str]) -> dict[str, Any]:
    """agent 写的回答原文（带引用标记）→ 回答文档：目录是 agent 自己的查询，加上报告里的指标和运行输入；快照按遮罩
    遮好再渲染。遮罩值在组装之前换掉（_scrubbed_source）；组装出来还认得出遮罩值（渲染出来才露的，比如别的列里一格
    文字夹着手机号），抛 _Leak，不交出去。allow 是提问人自己写的数（_given_numbers），回答里复述它们不标「无出处」。"""
    loader = _masking_loader(views)
    allowed = list(allow) or None
    # 列名里写着遮罩值的查询：目录里的列名照快照换（_masked_view），整表展开、表头核对、存下的目录都按换过的名字
    catalog = {alias: _renamed_entry(entry, secrets) for alias, entry in catalog.items()}
    doc = compose_doc(_scrubbed_source(source, secrets), catalog, run_id=run_id, loader=loader,
                      allow_numbers=allowed)
    if _leaked(doc, secrets):
        raise _Leak()
    return doc


def _leaked(doc: dict[str, Any], secrets: _Secrets) -> bool:
    """组装出来的正文里还认得出遮罩值，换了写法的长号码也算（199-1474-2785、+86 …、全角数字，_Secrets.loose_spans）。
    SQL 里照原样留着的阈值写在正文里不算（_Secrets.show），渲染进单元格引用里的算：格子已经按值遮过，遮完还认得出，
    就是一格文字里夹着这个数，渲染出来才露。"""
    markdown = str(doc.get("markdown") or "")
    pattern = secrets.pattern(markdown)
    hits = [(m.start(), m.end(), secrets.is_shown(m.group())) for m in pattern.finditer(markdown)] if pattern else []
    hits += [(start, end, key in secrets.shown) for start, end, key in secrets.loose_spans(markdown)]
    if not hits:
        return False
    cells = [seg["span"] for seg in iter_segments(doc) if str(seg.get("ref") or "").startswith("v:")]
    return any(not shown or any(a < end and start < b for a, b in cells) for start, end, shown in hits)


def _renamed_entry(entry: Any, secrets: _Secrets) -> Any:
    if not (isinstance(entry, dict) and entry.get("kind") == "query" and isinstance(entry.get("columns"), list)):
        return entry
    # 同快照的列名一样换（_masked_view）：换了写法的号码也认
    names = [secrets.scrub(str(c), loose=True) for c in entry["columns"]]
    return {**entry, "columns": names} if names != [str(c) for c in entry["columns"]] else entry


def _with_notes(answer: dict[str, Any], op: _Opening, outcome: VerifyOutcome, part: dict[str, Any], lost: list[str],
                own: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """回答末尾的系统说明：用不了的数据源、模型的轮数用完了、有查询执行失败、有引用解析不了、封存之后查的是现在的数据。
    审批人据此知道这份回答哪里不完整，不会看到「回答完成」却满屏 ⟦?…⟧。"""
    notes: list[str] = []
    if lost:
        notes.append(NOTE_LOST.format(names=_quoted(lost)))
    if outcome.limit == "max_steps":
        notes.append(NOTE_ROUNDS)
    cap = part.get("max_queries")
    # 查询次数到顶时次数一定用满了（被拒的、执行失败的也算一次），清单里少的就是失败的那几次
    if outcome.limit == "max_queries" and isinstance(cap, int) and not isinstance(cap, bool) \
            and len(outcome.queries) < cap:
        notes.append(NOTE_FAILED.format(n=cap - len(outcome.queries)))
    unresolved = sum(1 for seg in iter_segments(answer) if seg.get("issue") == "unresolved_ref")
    if unresolved:
        notes.append(NOTE_UNRESOLVED.format(n=unresolved))
    # 上传表格按运行固定的版本查（快照记着 data_version），手工登记的源查的是现在的库
    if op.mode == "sealed" and any(not snapshot.get("data_version") for snapshot in own.values()):
        notes.append(NOTE_LIVE)
    for note in notes:
        answer = with_note(answer, note)
    return answer


async def _store(doc: dict[str, Any], op: _Opening, ask: Ask) -> str | None:
    try:
        return await artifact_store.put_json(doc, kind=ANSWER_KIND, run_id=op.run_id,
                                             meta={"report": ask.report, "unit": ask.unit,
                                                   "question": ask.question[:QUESTION_BRIEF]})
    except Exception:  # noqa: BLE001 - 存不下不影响回答本身：正文照常交回，只是数字点不开
        logger.warning("质询的回答文档保存失败（运行 %s）", op.run_id, exc_info=True)
        return None


# --------------------------------------------------------------------------
# 写入与读取
# --------------------------------------------------------------------------


async def _record(op: _Opening, data: dict[str, Any]) -> tuple[Any, str | None]:
    """记一条 approval.inquiry。返回 (写入的事件, 没写进时的说明)。

    停在审批时：那张卡仍待审批、运行仍停着才写（批过了，运行可能已经停到下一张卡上）。封存之后：仍是开始回答时
    的那份封存才写。append_events 返回 None 就是运行在回答期间变了。"""
    items = [(EventType.APPROVAL_INQUIRY, None, data)]
    try:
        if op.mode == "interrupted":
            written = await append_events(op.run_id, items, require="interrupted", approval_id=op.approval_id)
        else:
            written = await append_events(op.run_id, items, require="sealed", seal=op.seal)
    except Exception as e:  # noqa: BLE001 - 写不进运行记录也要把回答交回给提问的人
        logger.warning("质询的问答写入运行记录失败（运行 %s）", op.run_id, exc_info=True)
        return None, f"这次质询的问答没能写入运行记录（{describe_exception(e)}）"
    return (written[0], None) if written else (None, NOT_RECORDED)


def _iso(ts: Any) -> str | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def inquiry_of(data: Any, *, seq: int | None, ts: Any) -> dict[str, Any]:
    """事件数据 → 接口上的 Inquiry 对象（前后端约定的形状）。POST 的返回和 GET 的列表都经这里，两边一样。"""
    data = data if isinstance(data, dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "seq": seq, "at": _iso(ts), "actor": data.get("actor"), "report": data.get("report"), "unit": data.get("unit"),
        "quote": data.get("quote"), "question": str(data.get("question") or ""),
        "answer": {"text": str(data.get("answer_text") or ""), "doc_artifact": data.get("answer_doc"),
                   "ok": data.get("ok") is True, "limit": data.get("limit"), "error": data.get("error")},
        "queries": [{k: q.get(k) for k in QUERY_KEYS} for q in data.get("queries") or [] if isinstance(q, dict)],
        "model": data.get("model"),
        "usage": {k: usage.get(k, 0) for k in USAGE_KEYS},
        "post_seal": bool(data.get("post_seal")), "approval_id": data.get("approval_id"),
    }


async def ask(run_id: str, request: Ask, *, actor: str | None = None) -> dict[str, Any]:
    """答一条质询：{inquiry, recorded, note}。不受理时抛 InquiryRefused。

    设置按现在的（不按发起时快照进运行选项的那份）：质询不在图里执行、不参与重放，关掉「审批质询」应当马上生效。"""
    from app.api.settings import VERIFY_SETTING_LABEL, verify_settings

    async with SessionLocal() as session:
        settings = await verify_settings(session)
    part = settings.get("inquiry") if isinstance(settings.get("inquiry"), dict) else {}
    if not part.get("enabled", True):
        raise InquiryRefused(409, DISABLED_WORDS.format(label=VERIFY_SETTING_LABEL["inquiry.enabled"]),
                             code=INQUIRY_DISABLED)
    with _ANSWERING.hold(run_id):
        opening = await _open(run_id, request)
        target = _target(opening.doc, request.unit) if request.unit else None
        data = {"question": request.question, "quote": request.quote, "report": request.report, "unit": request.unit,
                "approval_id": opening.approval_id, "actor": actor,
                **await _answer(opening, request, target, settings)}
        event, note = await _record(opening, data)
    if event is not None:
        return {"inquiry": inquiry_of(event.data, seq=event.seq, ts=event.ts), "recorded": True, "note": None}
    shown = {**data, "post_seal": True} if opening.mode == "sealed" else data
    _HELD.keep(run_id, shown)
    return {"inquiry": inquiry_of(shown, seq=None, ts=time.time()), "recorded": False, "note": note}


async def _inquiry_events(run_id: str) -> list[tuple[int, float, dict[str, Any]]]:
    async with SessionLocal() as session:
        if await session.get(Run, run_id) is None:
            raise InquiryRefused(404, "运行记录不存在", code=RUN_NOT_FOUND)
        rows = await session.execute(
            select(RunEvent.seq, RunEvent.ts, RunEvent.data)
            .where(RunEvent.run_id == run_id, RunEvent.type == EventType.APPROVAL_INQUIRY).order_by(RunEvent.seq))
    return [(seq, ts, data if isinstance(data, dict) else {}) for seq, ts, data in rows]


async def list_inquiries(run_id: str, *, report: str | None = None, unit: str | None = None) -> list[dict[str, Any]]:
    """这次运行的质询（封存前后的都算），按先后；给了 report / unit 只要问那份报告、那一句的。"""
    return [inquiry_of(data, seq=seq, ts=ts) for seq, ts, data in await _inquiry_events(run_id)
            if (not report or data.get("report") == report) and (not unit or data.get("unit") == unit)]


def _cited_cards(doc_id: Any) -> set[str]:
    """回答文档里引用的报告口径卡（m: 条目指向的工件）：界面按 doc.catalog[alias].artifact 取，和查询快照走同一个接口。"""
    doc = _quiet_load(artifact_store.load, doc_id)
    catalog = doc.get("catalog") if isinstance(doc, dict) and isinstance(doc.get("catalog"), dict) else {}
    return {str(e["artifact"]) for e in catalog.values()
            if isinstance(e, dict) and e.get("kind") in ("metric", "input") and e.get("artifact")}


async def _report_material(run_id: str, reports: Iterable[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """这几份报告（报告节点 id）的文档目录里的查询快照，和列出的指标、运行输入的值：和回答时收遮罩值、免遮的是同一批
    （报告最后一次 report.checked 的文档）。"""
    wanted = sorted({str(r) for r in reports if isinstance(r, str) and r})
    if not wanted:
        return [], []
    async with SessionLocal() as session:
        rows = await session.execute(
            select(RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id, RunEvent.type == EventType.REPORT_CHECKED, RunEvent.node_id.in_(wanted))
            .order_by(RunEvent.seq))
    docs = {node_id: str(data["doc_artifact"]) for node_id, data in rows
            if isinstance(data, dict) and data.get("doc_artifact")}
    snapshots: list[dict[str, Any]] = []
    values: list[str] = []
    for artifact in docs.values():
        doc = _quiet_load(artifact_store.load, artifact)
        catalog = doc.get("catalog") if isinstance(doc, dict) and isinstance(doc.get("catalog"), dict) else {}
        values += _ref_values(_report_refs(catalog))
        for entry in catalog.values():
            if isinstance(entry, dict) and entry.get("kind") == "query":
                snapshot = _quiet_load(artifact_store.load, entry.get("artifact"))
                if _query_shaped(snapshot):
                    snapshots.append(snapshot)
    return snapshots, values


async def inquiry_artifact(run_id: str, artifact_id: str) -> dict[str, Any]:
    """质询引用过的工件：查询快照（遮罩照回答时的办法，另把文字格里夹着的遮罩值按子串换掉；SQL 用事件里记下的那条，
    两处一字不差；SQL 检查结果见 _masked_checks）、回答文档（组装时就遮好了）、回答里引用的报告口径卡。
    {id, content, masked[, mask_note]}。

    认的是这次运行的质询事件引用过的，加上没写进运行记录、还留在进程里的回答（_HELD）引用过的；别的一律 404。"""
    from app.api.evidence import SOURCE_GONE

    datas = [data for _, _, data in await _inquiry_events(run_id)] + _HELD.of(run_id)
    referenced = [d for d in datas
                  if artifact_id in (d.get("inquiry_artifacts") or []) or d.get("answer_doc") == artifact_id]
    if not referenced:
        referenced = [d for d in datas if d.get("answer_doc") and artifact_id in _cited_cards(d.get("answer_doc"))]
    if not referenced:
        raise InquiryRefused(404, "这次运行的质询没有引用这件工件", code=ARTIFACT_NOT_FOUND)
    try:
        content = artifact_store.load(artifact_id)
    except ValueError as e:
        raise InquiryRefused(409, "工件与哈希不一致，疑似被修改，不予展示", code=ARTIFACT_TAMPERED) from e
    if content is None:
        raise InquiryRefused(404, "无法读取这件工件，可能已被删除", code=ARTIFACT_NOT_FOUND)
    if not _query_shaped(content):
        return {"id": artifact_id, "content": content, "masked": []}
    # agent 自己查询里遮掉的值事后已经不在（查询时就遮好了，快照里收不到）：回答时只靠它们才换掉的地方记在事件里
    # （REPLAY_KEY，只记位置），先照着再换一遍。先查号码、后查备注的，备注查询时就遮好了，用不着这一步
    edits = [e for d in referenced if isinstance(d.get(REPLAY_KEY), dict) and (e := d[REPLAY_KEY].get(artifact_id))]
    replayed = replay_edits(content, edits) if edits else content
    # 再照回答时的办法遮。遮罩值照回答时收：引用它的那几条质询各自查到的全部快照（查询条件里写着的值，不一定在这一份
    # 的结果里），加上报告自己的查询快照；免遮的也照回答时：提问人自己写的、报告里列出的指标和运行输入的值。SQL 里照原样
    # 留着的阈值只在回答正文里不遮，格子照旧按值遮，这里不用。文字格里夹着的遮罩值按子串换（embedded）：回答那边渲染
    # 出来露了就整份不交，这份快照却照样记在事件里、点得开，不换就等于在「不予展示」旁边把原值摆出来
    own: dict[str, dict[str, Any]] = {artifact_id: replayed}
    for data in referenced:
        for other in dict.fromkeys(str(a) for a in data.get("inquiry_artifacts") or []):
            if other not in own and _query_shaped(snapshot := _quiet_load(artifact_store.load, other)):
                own[other] = snapshot
    others, values = await _report_material(run_id, (d.get("report") for d in referenced))
    asked = [t for d in referenced for t in (d.get("question"), d.get("quote")) if isinstance(t, str)]
    plan = await _plan(own, others, [*asked, *values], embedded=frozenset({artifact_id}))
    shown = plan.views[artifact_id]
    masked = list(dict.fromkeys([*plan.masked[artifact_id],
                                 *(m for e in edits if isinstance(e, dict) for m in e.get("masked") or []
                                   if isinstance(m, str))]))
    recorded = next((q["sql"] for d in referenced for q in d.get("queries") or []
                     if isinstance(q, dict) and q.get("artifact") == artifact_id and isinstance(q.get("sql"), str)),
                    None)
    compared, kind = plan.tainted.get(artifact_id, set()), plan.kinds.get(str(content.get("source") or ""))
    if recorded is not None:
        shown = {**shown, "sql": recorded}
    elif isinstance(shown.get("sql"), str):
        shown = {**shown, "sql": _scrub_sql(shown["sql"], plan.secrets, compared, kind)}
    if "checks" in shown:
        whole, pieces = _recorded_swaps(content.get("sql"), recorded, kind) if recorded is not None else (set(), set())
        shown = {**shown, "checks": _masked_checks(shown["checks"], content.get("sql"), plan.secrets, compared, kind,
                                                   swapped=whole, pieces=pieces)}
    out: dict[str, Any] = {"id": artifact_id, "content": shown, "masked": masked}
    if gone := plan.gone.get(artifact_id):
        out["mask_note"] = SOURCE_GONE.format(source="、".join(gone))
    return out


def _recorded_swaps(sql: Any, recorded: str, kind: str | None) -> tuple[set[str], set[str]]:
    """回答时这条 SQL 里换掉的值：快照里原样的那条和事件里记下的那条（_scrub_sql 换过的）逐词对，原样那条里的字面量
    到了记下的那条里整个换成了「已遮罩」的（第一项，字面量的字），或者其中一截换成了「已遮罩」的（第二项，那一截，
    LIKE '%13900000001%' 这类）。_scrub_sql 只换字面量、名字和注释里的值，一个词换成一个词，两条分出来的词一一对得上；
    对不上（分词出错这类）、没有记下的那条时，什么都不认。"""
    if not isinstance(sql, str) or not sql.strip() or not recorded or sql == recorded:
        return set(), set()
    before, after = _sql_tokens(sql, kind), _sql_tokens(recorded, kind)
    if before is None or after is None or len(before[0]) != len(after[0]):
        return set(), set()
    word = _masked_word()
    whole: set[str] = set()
    pieces: set[str] = set()
    for old, kind_of, new in zip(before[0], before[1], after[0]):
        if not _is_literal(kind_of) or old.text == new.text or word not in new.text:
            continue
        if new.text == word:
            whole.add(old.text)
            continue
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old.text, new.text, autojunk=False).get_opcodes():
            if op == "replace" and new.text[j1:j2] == word and old.text[i1:i2].strip():
                pieces.add(old.text[i1:i2])
    return whole, pieces


def _masked_checks(checks: Any, sql: Any, secrets: _Secrets, compared: set[str], kind: str | None, *,
                   swapped: set[str] | frozenset[str] = frozenset(), pieces: set[str] | frozenset[str] = frozenset()
                   ) -> list[dict[str, Any]]:
    """快照里记下的 SQL 检查结果（数据源查询工具对照数据目录查出的问题）交给质询工件的样子：字段照证据面板
    （_query_checks），给模型的那句改法不交出去——它照抄了 SQL 片段。片段（sql_excerpt）、说明（message）摘自、照引
    同一条 SQL（快照里原样的那条），照它在查询清单里的换法换掉遮罩值（_literal_swaps）：SQL 换了、片段和说明没换，
    等于把换掉的值从旁边露出来；片段单拿出来认不出在找哪一个值，不能自己另认一遍。
    swapped / pieces：回答时这条 SQL 里整个换掉的字面量、换掉的一截（_recorded_swaps，照事件里记下的那条认）。回答时
    agent 自己查询里遮掉的值还在内存里，事后取工件时已经不在了（快照查询时就遮好了，收不到值），照记下的 SQL 补上。"""
    from app.api.evidence import _query_checks

    whole = _literal_swaps(sql, secrets, compared, kind)
    if swapped:
        whole = (whole or set()) | set(swapped)
    if pieces:
        secrets = replace(secrets, strong=secrets.strong | set(pieces), weak=secrets.weak | set(pieces), _index=None,
                          _compiled={})
    out: list[dict[str, Any]] = []
    for check in _query_checks({"checks": checks}):
        if isinstance(check.get("sql_excerpt"), str):
            check = {**check, "sql_excerpt": _scrub_excerpt(check["sql_excerpt"], whole, secrets, kind)}
        if isinstance(check.get("message"), str):
            check = {**check, "message": _scrub_message(check["message"], whole, secrets)}
        out.append(check)
    return out


__all__ = [
    "BUSY_CONTINUE", "BUSY_DECIDE", "INQUIRY_BUSY", "INQUIRY_DISABLED", "INQUIRY_NOT_OPEN", "INQUIRY_SCHEMA",
    "INQUIRY_SYSTEM", "NOT_RECORDED", "QUESTION_MAX", "QUOTE_MAX", "Ask", "InquiryRefused", "ask", "inquiry_artifact",
    "inquiry_busy", "inquiry_of", "list_inquiries", "parse_ask",
]
