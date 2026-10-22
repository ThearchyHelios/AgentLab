"""给助手查看（look 的 workflow_version 类型）用的工作流版本详情：入口字段、人工节点、口径卡指标、报告、
取数节点、写入动作，以及版本、级别、哈希。不含任何密钥（L3）。

只写名字和结构：不写 SQL、工具参数、请求头、节点提示词——SQL 里可能写着遮罩值，参数里可能有凭据，模型润色应用用不着。
"""
from __future__ import annotations

from typing import Any

from app.apps.tree import node_map

_LEVEL = {"governed": "受管级", "published": "已发布级"}


def describe_version(version: Any, *, workflow_name: str) -> str:
    graph = getattr(version, "graph", None) or {}
    nodes = node_map(graph)
    hash_ = (getattr(version, "graph_hash", None) or "")[:8]
    level = _LEVEL.get(getattr(version, "level", None) or "", "未发布")
    retired = "，已退役" if getattr(version, "retired_at", None) is not None else ""
    lines = [f"工作流「{workflow_name}」v{getattr(version, 'version', '?')}（{level}{retired}，指纹 {hash_}）"]
    fields = []
    for node in nodes.values():
        if node["type"] != "input":
            continue
        for f in node["config"].get("fields") or []:
            if not isinstance(f, dict) or not f.get("name"):
                continue
            bits = [str(f.get("type") or "string")]
            if f.get("required") and f.get("default") in (None, ""):
                bits.append("必填")
            if isinstance(f.get("enum"), list):
                bits.append("可选值 " + " / ".join(str(v) for v in f["enum"][:10]))
            if f.get("format"):
                bits.append(f"格式 {f['format']}")
            desc = f"：{f['description']}" if f.get("description") else ""
            fields.append(f"- {f['name']}（{'，'.join(bits)}）{desc}")
    lines.append("入口字段：" if fields else "入口字段：无")
    lines += fields
    for node_id, node in nodes.items():
        cfg = node["config"]
        title = f"「{node['title']}」（{node_id}）"
        if node["type"] == "metrics":
            caliber = f"口径 {cfg.get('caliber') or '未命名'} {cfg.get('caliber_version') or ''}".strip()
            metrics = [m for m in cfg.get("metrics") or [] if isinstance(m, dict) and m.get("id")]
            lines.append(f"口径卡{title}，{caliber}，指标 {len(metrics)} 个：" if metrics
                         else f"口径卡{title}，{caliber}，指标转抄自别处的口径卡")
            lines += [f"- {node_id}.{m['id']} {m.get('name') or ''}{('（' + m['unit'] + '）') if m.get('unit') else ''}"
                      for m in metrics]
        elif node["type"] == "analysis":
            from app.engine.labels import ANALYSIS_OP_LABEL

            items = [i for i in cfg.get("items") or [] if isinstance(i, dict) and i.get("id")]
            lines.append(f"分析卡{title}，分析项 {len(items)} 个：" if items else f"分析卡{title}，还没有分析项")
            lines += [f"- {node_id}.results.{i['id']} {ANALYSIS_OP_LABEL.get(str(i.get('op')), str(i.get('op') or ''))}"
                      for i in items]
        elif node["type"] == "report":
            lines.append(f"报告撰写{title}")
        elif node["type"] in ("tool", "merge"):
            tool = cfg.get("tool") if node["type"] == "tool" else "合并查询"
            lines.append(f"调用工具{title}：{tool}")
        elif node["type"] == "agent":
            tools = [t for t in cfg.get("tools") or [] if isinstance(t, str)]
            lines.append(f"Agent{title}：可用工具 {'、'.join(tools[:10]) or '无'}")
        elif node["type"] == "human":
            assignee = cfg.get("assignee") if isinstance(cfg.get("assignee"), dict) else {}
            who = f"岗位 {assignee.get('slot')}" if assignee.get("kind") == "slot" else (
                "发起人" if assignee.get("kind") == "initiator" else "任何人")
            policy = cfg.get("policy") or "any"
            lines.append(f"人工节点{title}：模式 {cfg.get('mode') or 'approve'}，派给{who}，会签 {policy}"
                         + (f"，时限 {cfg['due']}" if cfg.get("due") else ""))
        elif node["type"] == "output":
            names = [f.get("name") for f in cfg.get("fields") or [] if isinstance(f, dict) and f.get("name")]
            if names:
                lines.append(f"成果{title}：字段 {'、'.join(names)}")
    return "\n".join(lines)
