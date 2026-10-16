"""评测（docs/design/next-round/01-效果度量与回归评测.md）：一把尺——题集 + 由 SQL / 口径算出的期望答案。

第 2 波（这一包）：
- suites.py、cases.py：题集和题目的形状与校验（数值期望不许人填，评测-D4；只做单轮，评测-D12）、题目指纹；
- gold.py：期望答案的三种来源（SQL 题、口径题、从运行认定）、变量按截至时间渲染、试算、作答前后各算一次、口径升版处置；
- snapshots.py：目录快照（当前目录 / 影子起草）；
- transfer.py：题集导出导入（按数据源名对上，不含期望值、答案、运行）。

第 3 波接着写 runner（后台评测）、score、stats、compare、cleanup；第 4 波是界面。接口在 app/api/evals.py（只给搭建者）。
"""
