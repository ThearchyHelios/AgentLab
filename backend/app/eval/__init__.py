"""评测（docs/design/next-round/01-效果度量与回归评测.md）：一把尺——题集 + 由 SQL / 口径算出的期望答案。

- suites.py、cases.py：题集和题目的形状与校验（数值期望不许人填，评测-D4；只做单轮，评测-D12）、题目指纹；
- gold.py：期望答案的三种来源（SQL 题、口径题、从运行认定）、变量按截至时间渲染、试算、作答前后各算一次、口径升版处置；
- snapshots.py：目录快照（当前目录 / 影子起草）；
- transfer.py：题集导出导入（按数据源名对上，不含期望值、答案、运行）；
- variants.py：变体（助手、写作模型，目录视图，结论裁判，重复次数）和「同一变体」的认法、目录贡献的预设对照；
- answer.py：无头地走一遍问数据页那一轮（answer_ops → chat/turn 的决定 → 运行 → 复核 → 要的话重来一次）；
- runner.py：后台评测（并发、预算、止损、停止、只重试没做完的、重启标中断）；
- score.py、judging.py、rescore.py：评分（五种结论、维度配对、逐题 E1）、结论裁判和文字要点（模型判断，单列）、按新规则重评；
- judgements.py：分析期望（贡献、判定）：在期望 SQL 的结果上用平台的分析方法算期望的判定，答案里的分析结论和正文的判断词按
  同一套判定类、同一张说法表比（docs/design/analysis-round/00-总方案.md §3.8）；
- stats.py、compare.py：总表与区间、和上一次比、A/B 配对表与精确 McNemar；
- cleanup.py：按保留规则清理明细、回收工件；scheduled.py：题集的定时。

界面在第 4 波；接口在 app/api/evals.py（只给搭建者）。
"""
