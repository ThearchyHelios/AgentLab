"""配置项和取值在界面上的叫法。

校验、发布门禁、自动修复、升级预览、运行报错里提到节点配置时，写检查器上的中文标签，
不写键名和枚举值：用户在界面上看到的是「指标来自」「计入缺口」，从没见过 metrics_from、
require_citation。键名只在配置界面里字段自己旁边出现。

叫法以前端为准：字段标签对照 frontend/src/canvas/nodeDefs.ts 的 label，出具契约里的键对照
canvas/issues.ts 的 CONTRACT_KEYS / SUB_KEYS，结论句裁判对照 lib/terms.ts 的 JUDGE_FIELD_LABEL。
前端改了叫法，这里跟着改；新写报错时从这里取，别在各处手写键名。

取值的叫法（option_label）是放进句子里的短说法：前端下拉里的选项文字后面常带一句用途说明
（「算违规，要求重写（默认）」），句子里只取前半句。
"""
from __future__ import annotations

from typing import Any

#: 节点类型的叫法，和前端 lib/terms.ts 的 NODE_TYPE_LABEL 一致
TYPE_LABEL: dict[str, str] = {
    "input": "输入", "output": "成果", "llm": "模型调用", "agent": "Agent",
    "supervisor": "多 Agent 协作", "tool": "调用工具", "code": "沙箱代码",
    "branch": "条件分支", "loop": "循环", "subgraph": "子工作流",
    "memory": "长期记忆", "retrieve": "知识检索", "transform": "数据整形",
    "human": "人工审批", "validate": "结构校验", "metrics": "口径卡", "report": "报告撰写",
}

#: 各类节点共用的字段名（nodeDefs.ts）。同一个键在不同节点上叫法不同的，见 _FIELD_BY_TYPE
FIELD_LABEL: dict[str, str] = {
    "label": "节点名称",
    # 模型相关
    "model": "模型", "thinking": "思考模式", "effort": "投入程度", "temperature": "温度",
    "max_tokens": "最大输出 token",
    # 通用
    "approval": "审批策略", "assign_to": "结果存为变量", "skip_if": "跳过条件",
    "retries": "失败重试次数", "on_error": "出错时",
    # 模型调用、Agent
    "system": "系统提示", "prompt": "用户提示", "skills": "挂载 Skill",
    "output_schema": "结构化输出 Schema", "use_history": "带上对话历史",
    "tools": "可用工具", "max_steps": "最大步数", "budget_tokens": "token 预算",
    "budget_usd": "金额预算（美元）", "parallel_tools": "并行执行工具", "cite_fields": "按出处核对字段",
    # 多 Agent 协作
    "goal": "团队目标", "agents": "团队成员", "max_rounds": "最多轮数", "on_exhausted": "用完轮数时",
    "max_parallel": "每轮最大并行成员数",
    # 调用工具、沙箱代码
    "tool": "工具", "args": "参数", "language": "语言", "code": "代码", "timeout": "超时（秒）",
    "memory_mb": "内存上限（MB）", "network": "允许联网", "isolation": "隔离档位",
    "fail_fast": "执行失败即中断", "evidence_role": "证据角色",
    # 条件分支、循环、子工作流
    "cases": "分支", "instruction": "分类说明", "items": "列表来源", "item_var": "当前项变量名",
    "condition": "继续条件", "max_iterations": "最大迭代次数",
    "workflow_id": "工作流", "workflow_version": "固定版本", "upgrade_policy": "上游发布新版本时",
    # 数据整形、结构校验
    "template": "模板", "expression": "表达式", "schema": "JSON Schema", "max_retries": "最多返工次数",
    "repair_with_llm": "让模型修复",
    # 成果、输入
    "fields": "成果字段", "contract": "出具契约",
    # 报告撰写
    "instructions": "写作要求", "metrics_from": "指标来自", "numbers": "未引用的数字",
    "on_violation": "重写后仍有违规时", "max_repairs": "最多重写次数", "claims": "未附依据的结论句",
    "judge": "结论句裁判", "entities": "表名、字段名",
    # 口径卡
    "caliber_from": "口径卡来源", "caliber": "口径名称", "caliber_version": "口径版本",
    "metrics": "指标定义", "on_missing": "缺输入时",
    # 全图
    "defaults": "工作流默认设置",
}

_FIELD_BY_TYPE: dict[str, dict[str, str]] = {
    "input": {"fields": "输入字段"},
    "agent": {"prompt": "任务"},
    "branch": {"mode": "判断方式", "input": "待分类内容"},
    "loop": {"mode": "循环方式"},
    "subgraph": {"input": "传入参数"},
    "memory": {"action": "操作", "scope": "作用域", "query": "检索内容", "limit": "返回条数",
               "content": "记住的内容", "kind": "记忆类型", "importance": "重要程度"},
    "retrieve": {"query": "检索问题", "collection": "知识库", "limit": "返回片段数", "rerank": "重排",
                 "min_score": "最低分数"},
    "transform": {"mode": "方式"},
    "human": {"mode": "审批方式", "title": "标题", "message": "展示给审批人的内容", "draft": "草稿内容",
              "stop_on_reject": "驳回即终止运行"},
    "validate": {"source": "待校验内容", "fail_fast": "校验失败即中断"},
}

#: 出具契约里的键（canvas/issues.ts 的 CONTRACT_KEYS、SUB_KEYS）
CONTRACT_KEY_LABEL: dict[str, str] = {
    "metrics_from": "指标来自", "report_from": "报告来自", "required": "必需指标", "expected": "期望指标",
    "strict": "严格模式", "narrative": "叙述", "cells": "单元格引用", "allow_numbers": "允许无出处的数字",
    "claims": "未附依据的结论句", "on_uncited": "未附依据时",
}

#: 报告撰写节点 judge 子配置（lib/terms.ts 的 JUDGE_FIELD_LABEL）
JUDGE_FIELD_LABEL: dict[str, str] = {
    "provider": "裁判模型 · 接入", "model": "裁判模型", "max_claims": "最多裁判句数",
    "max_cost_usd": "金额上限（美元）", "timeout_s": "时长上限（秒）",
    "rewrite_once": "证据相矛盾或不足的句子退回改写一次", "on_unsupported": "证据相矛盾时",
}

#: 取值的短说法：键 → {值: 叫法}。前端下拉的选项文字去掉后半句的用途说明
_OPTION_LABEL: dict[str, dict[str, str]] = {
    "numbers": {"strict": "计为违规，要求重写", "off": "仅标注，不要求重写"},
    "on_violation": {"flag": "正常产出，并标注违规之处", "fail": "判为失败"},
    "claims": {"off": "不参与出具判档", "require_citation": "计入缺口", "judge": "计入缺口并由模型逐句判断"},
    "entities": {"link": "核对", "off": "不核对"},
    "evidence_role": {"source": "取数", "compute": "计算"},
    "on_uncited": {"ignore": "仅标注，不计入缺口", "degrade": "计入缺口、出具降档", "withhold": "不予出具"},
    "on_unsupported": {"degrade": "出具降档", "withhold": "不予出具"},
    "on_exhausted": {"fail": "判为失败", "degrade": "降档交付"},
    "on_missing": {"null": "记为空值，交给出具契约判档"},
    "on_error": {"continue": "记录错误并继续"},
    "approval": {"dangerous": "仅危险工具需要审批", "always": "每次调用都审批", "never": "全部无需审批"},
    "upgrade_policy": {"recompute": "用新口径回算历史", "dual": "新旧口径并列展示",
                       "incomparable": "标注与历史不可比"},
    "isolation": {"strict": "严格：microVM", "fast": "快速：系统沙箱"},
    "thinking": {"summarized": "开启并显示摘要", "adaptive": "开启但不显示", "off": "关闭"},
}
#: 让人在几个取值之间选时的完整选项文字（发布前修复的候选），和检查器下拉一致，只去掉「（默认）」
CHOICE_LABEL: dict[str, dict[str, str]] = {
    "claims": {"off": "不处理：不参与出具判档", "require_citation": "计入缺口：出具按档位降档",
               "judge": "计入缺口，并由模型逐句判断证据是否支持"},
}
_MODE_LABEL: dict[str, dict[str, str]] = {
    "branch": {"expression": "表达式判断", "llm": "让模型分类"},
    "loop": {"foreach": "遍历列表", "while": "条件成立时重复"},
    "transform": {"template": "文本模板", "expression": "表达式", "json": "JSON 模板"},
    "human": {"approve": "批准 / 驳回", "input": "补充输入", "edit": "编辑草稿"},
}

#: 分支、循环、人工审批的出口在画布上的叫法（nodeDefs.ts 的 handlesOf）
HANDLE_LABEL: dict[str, str] = {
    "default": "其他", "body": "循环体", "done": "结束", "approved": "批准", "rejected": "驳回",
}

#: 出具档位（lib/terms.ts 的 ISSUANCE_LABEL）
ISSUANCE_LABEL: dict[str, str] = {"formal": "完整出具", "degraded": "降档出具", "withheld": "不予出具"}


def type_label(node_type: Any) -> str:
    """节点类型的叫法；不认识的类型原样返回。"""
    return TYPE_LABEL.get(str(node_type), str(node_type))


def field_label(key: str, node_type: Any = None) -> str:
    """节点配置键的叫法。带点号的路径逐段翻译：「出具契约 · 指标来自」「结论句裁判 · 金额上限（美元）」。
    认不出的段原样留着——宁可露一个键名，也不编一个界面上没有的名字。"""
    head, _, rest = str(key).partition(".")
    kind = str(node_type) if node_type is not None else ""
    base = _FIELD_BY_TYPE.get(kind, {}).get(head) or FIELD_LABEL.get(head) or head
    if not rest:
        return base
    sub = JUDGE_FIELD_LABEL if head == "judge" else CONTRACT_KEY_LABEL
    return " · ".join([base, *(sub.get(part, part) for part in rest.split("."))])


def contract_label(key: str) -> str:
    """出具契约里一个键的叫法（不带「出具契约」前缀）。"""
    return CONTRACT_KEY_LABEL.get(key, key)


def judge_label(key: str) -> str:
    """judge 子配置一项的叫法（不带「结论句裁判」前缀）。"""
    return JUDGE_FIELD_LABEL.get(key, key)


def option_label(key: str, value: Any, node_type: Any = None) -> str:
    """某个配置键取某个值时的叫法。认不出的值原样返回，交给调用方加引号。"""
    last = str(key).rsplit(".", 1)[-1]
    if last == "mode" and node_type is not None:
        return _MODE_LABEL.get(str(node_type), {}).get(str(value), str(value))
    if isinstance(value, bool):
        return "开启" if value else "关闭"
    return _OPTION_LABEL.get(last, {}).get(str(value), str(value))


def q(text: Any) -> str:
    """加「」：给用户看的值一律这样括起来，不用 repr 的英文单引号。"""
    return f"「{text}」"
