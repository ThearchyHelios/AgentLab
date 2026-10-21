"""白话层（docs/design/normal-user-apps.md N10）：问题编号 → 一句话、为什么要紧、谁来改、怎么办。

覆盖工作流发布门禁（contract.* / governed.* / action.* / process.*）、应用检查（app.*）、蓝图（bp.*）、模板（tpl.*）、
个人应用（personal.*）、发布单步骤失败（plan.*）。没登记白话的照旧显示技术说法；检查脚本报「有问题编号没有白话」。
who：you（你来定）/ assistant（助手可以修）/ admin（要找管理员），按自动修复的 kind、APP_HUMAN_ONLY、要管理员的编号判。

框架在这里；内容由包 B2 补齐（B1、B3、B4 新增的编号各自补进来）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

WHO = ("you", "assistant", "admin")


@dataclass(frozen=True)
class Plain:
    title: str
    why: str = ""
    who: str = "you"
    action: str = ""


_Y, _A, _M = "you", "assistant", "admin"

#: 问题编号 → 白话。前缀匹配用 PLAIN_PREFIX（如 "subgraph_version_" 一类）
PLAIN: dict[str, Plain] = {
    # ---------------------------------------------------------------- 出具契约（engine/governance.py）
    "contract.not_object": Plain(
        "成果的出具规则格式不对", "出具规则读不出来，系统就判不了这份成果能不能正式交出去。", _A,
        "交给助手按出具规则的格式重写，或在成果节点上重新设置。"),
    "contract.metrics_from_missing": Plain(
        "没说指标从哪张口径卡来", "报告里的数对不上任何指标，核对不了出处。", _A,
        "在成果节点的出具规则里选上提供指标的口径卡。"),
    "contract.metrics_from_invalid": Plain(
        "出具规则选的口径卡不存在", "选错了口径卡，核对时找不到指标。", _A, "改选工作流里实际存在的口径卡。"),
    "contract.report_from_missing": Plain(
        "没说核对哪一份报告", "不指明报告，报告里的数就没人核对。", _A, "在出具规则里选上写报告的那个节点。"),
    "contract.report_from_invalid": Plain(
        "出具规则选的报告节点不对", "选的不是写报告的节点，或者不在成果的上游，核对会落空。", _A,
        "改选成果上游的报告撰写节点。"),
    "contract.required_missing": Plain(
        "没列出必须有的指标", "不列出必需指标，缺了关键的数也会交出去。", _Y,
        "选出这份成果里必须有的指标（哪些缺了就不交出去由你定）。"),
    "contract.cells_undeclared": Plain(
        "报告直接引用了查询结果的格子，却没声明", "受管级别不认没声明的引用，正式运行时报告会写不出来。", _Y,
        "选择允许直接引用查询结果，或者加一张口径卡把数登记成指标。"),
    "contract.strict_off": Plain(
        "出具规则没开严格模式", "不开的话，说不清出处的数只会让成果降档，不会被拦下。", _A, "打开严格模式。"),
    "contract.claims_ignored": Plain(
        "「没附依据的结论句」那一项设了也不生效", "受管级别仍会把没附依据的结论句算进缺口，这项设置只会误导人。", _A,
        "改成降档或不予出具。"),
    # ---------------------------------------------------------------- 受管级门禁（engine/governance.py）
    "governed.supervisor": Plain(
        "用了由模型临场分工的节点", "分工和轮数在运行时才定，事先没法审核，受管级别不收。", _Y,
        "改用配置固定的 Agent 或「模型调用」节点（结构要拆成哪几步由你定）。"),
    "governed.subgraph_unpinned": Plain(
        "引用的工作流没固定版本", "上游一改，这边的口径跟着变，没人知道。", _Y, "选定一个已发布的版本。"),
    "governed.agent_approval_never": Plain(
        "Agent 调危险工具不用审批", "受管级别要求危险工具至少经人审批。", _A, "把审批策略改为「仅危险工具需要审批」。"),
    "governed.default_approval_never": Plain(
        "全图默认不用审批", "跟着全图默认的 Agent 调危险工具时没人把关。", _A, "把全图默认的审批策略改为需要审批。"),
    "governed.agent_no_tools": Plain(
        "Agent 没配工具", "不调工具的 Agent 只是在写文字，用「模型调用」节点更合适。", _Y, "改用「模型调用」节点，或给它配上工具。"),
    "governed.agent_tools_wide": Plain(
        "Agent 的工具太多", "授权范围越宽，越难说清它会做什么。", _Y, "只留下完成任务要用的工具。"),
    "governed.code_network": Plain(
        "代码节点开了联网", "在代码里取的数不留快照，事后查不了出处。", _Y, "把取数改到「调用工具」节点。"),
    "governed.no_contract": Plain(
        "成果没有出具规则", "没有出具规则，系统判不了这份成果完不完整。", _A, "给成果节点加上出具规则。"),
    "governed.metric_untraced": Plain(
        "有指标追不到原始数据", "按指标出具时，指标的出处就是全部依据；追不到的那一个，「完整出具」就没有根据。", _A,
        "在口径卡里按格取数，或让整形节点原样解析查询结果。"),
    "governed.report_from_required": Plain(
        "出具规则核对的是叙述，不是报告", "按数值找出处可能对上好几个地方，受管级别要求每个数只有一个出处。", _A,
        "把核对对象改为上游的报告撰写节点。"),
    "governed.exit_text_source": Plain(
        "成果里的文字不是报告写的", "别的节点写的文字说不清出处。", _A, "成果字段改取报告撰写节点的正文。"),
    "governed.text_bypass": Plain(
        "模型写的文字绕过了报告直接进了成果", "这些文字里的数和表名都没法追溯。", _A, "改由报告撰写节点来写，成果取报告正文。"),
    "governed.report_policy": Plain(
        "报告的核对规则不够严",
        "受管级别要求报告用最严的核对规则：数字要有出处、结论句要挂依据，「主要来自」「异常」这类判断要挂对分析结论。", _A,
        "把报告撰写节点的核对规则调到最严，分析说法核对也设为严格。"),
    "governed.judge_budget": Plain(
        "模型裁判没设金额上限", "裁判的花费不能沿用默认值，要事先说清。", _Y, "写一个每份报告的裁判金额上限，或明确选择不限。"),
    "governed.caliber_compute_input": Plain(
        "口径卡的数来自代码计算", "沙箱里算出来的数追溯不了出处。", _Y,
        "若这一步是在取数，把它标成取数；若是在计算，把计算挪到口径卡的表达式里（是哪一种由你定）。"),
    "governed.caliber_agent_schema": Plain(
        "给口径卡供数的 Agent 交的是自由文本", "口径卡读到的数没法核对到查询结果。", _A,
        "给 Agent 配上结构化输出，并打开按出处核对字段。"),
    "governed.caliber_agent_cite_fields": Plain(
        "给口径卡供数的 Agent 没核对字段出处", "口径卡读到的仍是模型给的数。", _A, "打开按出处核对字段。"),
    "governed.caliber_model_input": Plain(
        "口径卡的数来自「模型调用」节点", "这种节点不调工具，写出的数没有出处。", _A, "改由「调用工具」节点或配好结构化输出的 Agent 取数。"),
    # ---------------------------------------------------------------- 写外部系统（engine/lint_capabilities.py）
    "action.write_in_agent": Plain(
        "Agent 的工具里有会写外部系统的能力", "写入只能放在「调用工具」节点里，先确认再发送、留生效记录。", _Y,
        "从 Agent 的工具清单里移除它，改用「调用工具」节点。"),
    "action.unattended_write": Plain(
        "写入外部系统不经确认，而这项能力不允许无人值守", "没人确认就写进外部系统，出了错没人拦。", _Y,
        "把这一步改为需要确认；确实要无人值守的，请管理员在能力登记里放开。"),
    "action.prod_missing": Plain(
        "写入能力没有生产环境", "正式运行要写的时候无处可发。", _M, "请管理员在「工具 → 端点」里补上生产环境。"),
    "action.reconcile_unplanned": Plain(
        "写入能力没有登记查询", "写入结果不明时没法自动核实，只能转人工核实。", _M, "请管理员在能力登记里给它指定查询能力。"),
    # ---------------------------------------------------------------- 按流程使用（engine/lint_process.py）
    "process.wait_mid_agent": Plain(
        "Agent 中途可能停下来等审批", "按流程使用时，停在 Agent 中途的断点在系统升级后恢复不了。", _Y,
        "把需要审批的调用挪到「调用工具」节点。"),
    "process.human_in_subgraph": Plain(
        "人工步骤放在了引用的工作流里", "引用的工作流不保存断点，恢复时整段从头再来，可能重复办。", _Y,
        "把人工步骤挪到外层工作流。"),
    "process.code_network": Plain(
        "代码节点联网，可能在写外部系统", "按流程使用时，写外部系统要先留生效记录，结果不明时才能核实。", _Y,
        "把对外部系统的写入挪到「调用工具」节点。"),
    # ---------------------------------------------------------------- 应用检查（apps/lint.py、apps/resolve.py）
    "app.format": Plain(
        "应用描述的格式不对", "格式不对的应用发布不了。", _A, "交给助手按应用格式修好。"),
    "app.pin_unpublished": Plain(
        "应用用的工作流版本还没发布", "应用只能用已发布的版本，正式运行只从已发布的版本发起。", _Y,
        "先发布这一版，或换成一个已发布的版本。"),
    "app.pin_retired": Plain(
        "应用用的工作流版本已退役", "退役的版本不再接受新的正式运行。", _Y, "升级到一个在用的发布版本。"),
    "app.pin_hash": Plain(
        "应用记下的版本和库里的对不上", "按内容指纹认版本，对不上就说不清运行的是哪张图。", _Y, "重新选择这一版。"),
    "app.upgrade_policy": Plain(
        "口径变了，还没选以前的数怎么办", "换版本可能改了口径，新旧数放在一起比之前要先说清。", _Y,
        "选一种处置：按新口径重算、新旧并列，或注明前后不可比。"),
    "app.binding_target_missing": Plain(
        "页面上有一块绑的东西不存在", "这一块会显示不出来。", _A, "改绑到存在的视图、指标或节点，或删掉这一块。"),
    "app.view_source_empty": Plain(
        "这个视图暂时还没有数", "页面会写明还没有数据，等第一次正式运行完成就有了。", _Y, "不用改；想马上有数，发布后运行一次。"),
    "app.on_demand_ttl": Plain(
        "按需查看的缓存时效没设好", "查活库的视图不设时效，同样的条件会一直给旧结果。", _A, "设一个缓存时效。"),
    "app.component_unknown": Plain(
        "用了组件目录里没有的组件", "界面画不出来不认识的组件。", _A, "换成组件目录里有的组件。"),
    "app.props_invalid": Plain(
        "组件的设置不对", "设置不对，这一块可能显示错或显示不出来。", _A, "按组件的说明改设置。"),
    "app.text_number": Plain(
        "页面的固定文字里写了数字", "写死的数不会随数据更新，也说不清出处。", _A, "改成绑定到指标或查询结果。"),
    "app.binding_not_evidenced": Plain(
        "成果字段里的数不是报告里的原文", "说不清出处的数不展示。", _A, "改绑报告，或让成果字段逐字取自报告。"),
    "app.metric_untraced": Plain(
        "页面上的指标追不到原始数据", "这个数点开看不到出处。", _A, "换一个能追到原始数据的指标，或先改口径卡。"),
    "app.expr_arith": Plain(
        "显示条件里做了计算", "页面层不做算术，数要在工作流里算好。", _A, "把计算挪到口径卡，条件里只做比较。"),
    "app.expr_invalid": Plain(
        "显示条件写法不对", "条件算不出来时这一块按不显示处理。", _A, "改正条件的写法。"),
    "app.expr_root": Plain(
        "显示条件用了不认识的名字", "条件算不出来时这一块按不显示处理。", _A, "改用条件里能用的名字。"),
    "app.entry_form_mismatch": Plain(
        "发起表单和工作流的输入对不上", "填的东西传不到工作流里，或者传错位置。", _A, "让表单字段和工作流的输入一一对应。"),
    "app.entry_unmapped_required": Plain(
        "工作流必填的输入，发起表单没给", "发起时会因为缺输入而失败。", _A, "在发起表单里加上这一项，或给它一个固定值。"),
    "app.slot_unbound": Plain(
        "有一步还没指定谁来做", "没人的那一步会派给任何人，审批、升级都会悄悄失效。", _Y, "在「谁来做」里给这一步选人或组。"),
    "app.slot_principal_unknown": Plain(
        "「谁来做」里选的人或组在名录里对不上", "派给这个名字的待办没有人能处理。", _Y, "从候选中重新选择，或移除。"),
    "app.reconciler_unbound": Plain(
        "写入结果不明时没人核实", "写外部系统的结果不明时，要有人去外部系统里查。", _Y, "指定负责核实写入结果的人或组。"),
    "app.process_unsafe": Plain(
        "这个工作流按流程使用不安全", "流程要能停下等人、断了能接着走，这张图有一处做不到。", _Y,
        "按问题里说的改工作流，再发布新版本。"),
    "app.hook_entry_missing": Plain(
        "外部系统发起用的入口不存在", "外部系统发来的请求会被拒收。", _A, "改选存在的入口。"),
    "app.notify_channel_missing": Plain(
        "订阅的通知渠道不存在", "这条通知发不出去。", _Y, "换一个存在的渠道，或请管理员建好它。"),
    "app.acl_empty": Plain(
        "还没给任何人用", "开了登录时，除了管理员和搭建者谁都打不开。", _Y, "在「谁能看」里给要用的人或组授权。"),
    "app.acl_principal_unknown": Plain(
        "授权给的人或组在名录里对不上", "以后建了同名的人会直接拿到这一档权限，或者停用的人重新启用后又能用。", _Y,
        "从候选中重新选择，或移除。"),
    "app.polish_refused": Plain(
        "助手想改的地方超出了它能改的范围", "助手只能改页面的呈现，改数据、改流程要人来。", _Y, "确实要改的，由你在对应的地方改。"),
    # ---------------------------------------------------------------- 发布单（blueprints/publish_plan.py）
    "plan.not_instantiated": Plain(
        "蓝图还没生成工作流", "没有图就没有东西可以发布。", _Y, "先把卡片补齐，用真数试运行一次。"),
    "plan.graph_changed": Plain(
        "出单之后蓝图又改过", "要发布的已经不是这份发布单看过的那张图。", _Y, "重新出一份发布单。"),
    "plan.trial_failed": Plain(
        "最近一次试运行失败了", "用真数运行出了错，这样发布出去，每次出数都会是同一个错。", _Y,
        "回到试运行看出错的原因（可以交给助手修），修好后再用真数试一次。"),
    "plan.trial_rejected": Plain(
        "最近一次试运行被驳回、没有走完", "那是你（或审批人）的决定，不是出错；驳回之后的步骤没用真数看过。", _Y,
        "要看完整结果，再试运行一次并批准；确认不需要的话可以直接发布。"),
    "plan.untrialed": Plain(
        "还没用真数试运行过", "没看过真数就发布，可能不是你要的结果。", _Y, "先试运行一次，看过结果再发布。"),
    "plan.trial_differs": Plain(
        "发布的内容和最近一次试运行不同", "试运行之后又改过，发布的这一版没用真数看过。", _Y, "建议再试运行一次。"),
    "plan.admin_required": Plain(
        "受管级别只有管理员能发布", "受管级别的工作流按最严的规则出具，发布由管理员签字。", _M, "请管理员来发布，或改按已发布级别发布。"),
    "plan.downgrade_admin": Plain(
        "这会把受管工作流降为已发布", "降级之后正式运行不再过受管级别的检查，要管理员决定。", _M, "请管理员来发布，或改按受管级别发布。"),
    "plan.preflight": Plain(
        "发布前检查没通过", "有问题没解决，这时发布会留下半截状态，所以什么都没写。", _Y, "按下面列出的问题逐条处理，再接着做。"),
    "plan.workflow_publish": Plain(
        "工作流没能发布", "工作流不发布，就不会有正式运行。", _Y, "按问题处理后接着做。"),
    "plan.schedule_invalid": Plain(
        "定时设置不对", "按这个设置建不了定时任务。", _Y, "回到「多久更新」卡片改好，再接着做。"),
    "plan.app_publish": Plain(
        "应用没能发布", "工作流已经发布了，应用还没有；使用者暂时看不到它。", _Y, "按问题处理后接着做。"),
    "plan.app_archived": Plain(
        "这个蓝图对应的应用已归档", "归档的应用不再改草稿、不再发布。", _Y, "如需继续，请新建一个应用。"),
    "plan.app_forbidden": Plain(
        "你对这个蓝图发布成的应用没有管理权限", "发布单会改这个应用的页面、发布新的一版、替换它的授权，这些要管理权限。", _M,
        "请应用的管理者或管理员来发布，或请他们在应用的授权里给你管理权限。"),
    "plan.grant_refused": Plain(
        "「谁能看」里有人或组在名录里对不上", "授权只能给名录里在用的人和组。", _Y, "回到「谁能看」卡片重新选择。"),
    "plan.first_run": Plain(
        "第一次运行没能发起", "应用已经发布好了，只是打开时还没有数。", _Y, "稍后在应用里运行一次，或等定时任务出数。"),
    "plan.object_missing": Plain(
        "前面某一步建好的东西不见了", "接着做之前要确认前面几步的结果还在。", _Y, "重新出一份发布单。"),
    "plan.interrupted": Plain(
        "执行到一半服务重启了", "这一步可能只做了一半，后面的步骤都没有做。", _Y,
        "接着做：已经存好的版本、建好的应用会按原样复用，不会另建一份。"),
    "plan.step_failed": Plain(
        "这一步出了意外的错", "这一步没做完，后面的步骤都没有做。", _Y, "稍后接着做；一直不行请把错误信息交给管理员。"),
}
# ---------------------------------------------------------------- 个人应用与页面编辑（包 B4：blueprints/personal.py、
# apps/lint.py 的 _lint_personal / _lint_views / _lint_hidden、api/personal.py）
PLAIN.update({
    "app.view_source_personal": Plain(
        "视图的数据来源和应用的类型对不上", "探索运行的结果没经过发布审核，只能给个人应用的拥有者自己看；个人应用又只有"
        "探索运行，取别的来源永远是空的。", _Y,
        "共享应用的视图改成取正式运行（最近一次出数、按条件查看或本实例）；个人应用的视图改回取它自己的刷新结果。"),
    "app.hidden_bound": Plain(
        "隐藏的这块是唯一显示这组数的地方", "隐藏之后，页面上一处都看不到这组数了。", _Y,
        "确实不要了可以删掉它；还要看的话取消隐藏。"),
    "personal.policy": Plain(
        "个人应用里有使用者不能用的东西", "个人应用只能读数据、整理、撰写，不能审批、不能写入别的系统，也不能执行代码，"
        "这样它的结果才能只给你自己看。", _Y, "回到蓝图换一种做法；需要审批或写回的，请提交给搭建者做成正式应用。"),
    "personal.off": Plain(
        "管理员还没有开放个人应用", "个人应用由管理员决定开不开放。", _M, "请管理员在设置里打开，或请搭建者做成应用。"),
    "personal.not_trialed": Plain(
        "还没有用真数试过", "没看过真数就存，可能不是你要的结果。", _Y, "先试运行一次，看过结果再存成你的应用。"),
    "personal.graph_changed": Plain(
        "蓝图改过，还没重新存成你的应用", "刷新按存下的那张图运行，改过的蓝图还没生效。", _Y,
        "回到蓝图「存成我的应用」，再刷新。"),
    "personal.daily_limit": Plain(
        "今天的刷新次数用完了", "每人每天能刷新的次数有上限，控制运行的花费。", _M, "明天再试，或请管理员调高上限。"),
    "personal.no_run": Plain(
        "还没有成功刷新过", "审核的人要看到这个应用用真数出的结果。", _Y, "先刷新一次，成功之后再提交审核。"),
    "personal.in_review": Plain(
        "已经提交过审核", "同一个应用同时只能有一份在审核。", _Y, "等搭建者发布或退回之后再提交。"),
    "personal.review_target": Plain(
        "审核要交给搭建者", "只有搭建者能把它做成正式应用。", _Y, "从候选里选一位搭建者，或者交给一个组。"),
    "personal.review_forbidden": Plain(
        "审核个人应用要搭建者来做", "审核的人要在副本上试运行、发布成正式应用，这些只有搭建者能做。", _M,
        "请一位搭建者处理这条审核，或请管理员调整你的角色。"),
    "personal.not_personal": Plain(
        "这不是个人应用，或者它出自的蓝图已经不在了", "刷新、提交审核只对还能找到蓝图的个人应用。", _Y,
        "共享应用请用它自己的刷新；蓝图删了的个人应用可以归档。"),
    "personal.not_shareable": Plain(
        "个人应用不能发布或授权给别人", "个人应用的结果没经过发布审核，只给拥有者自己看。", _Y,
        "想给别人用，请提交审核，由搭建者发布成正式应用。"),
})
# ---------------------------------------------------------------- 记录表与文件（包 B3：records/、files/、api/files.py、
# apps/lint.py 的 _lint_records_rows）。records.* 是写记录表被拒的原因，files.* 是上传、提交、下载文件被拒的原因
PLAIN.update({
    "app.records_rows_exposed": Plain(
        "看板上的表格把记录一条条都列出来了", "看板的数是用全部记录算的，这样每个能用应用的人都能看到别人提交的记录。", _Y,
        "要看自己提交了什么，请用「记录列表」组件（按看的人过滤）；看板上的表格改成汇总。确实要公开给所有人看的可以不改。"),
    "records.column_invalid": Plain(
        "表单里有字段存不进记录表", "字段名重复、以下划线开头或者类型不认识，记录表建不起来。", _Y,
        "回到「填什么」卡片改字段名或类型。"),
    "records.column_unknown": Plain(
        "要写入的字段记录表里没有", "表单改过，应用还在用改之前的那一版。", _Y, "重新发布应用，让表单和记录表对上。"),
    "records.required_missing": Plain(
        "必填的字段没有值", "必填的字段空着，这条记录就是不完整的。", _Y, "把必填的字段填上再提交。"),
    "records.value_invalid": Plain(
        "有字段的值和类型对不上", "比如数字字段填了文字，存进去以后汇总会算错。", _Y, "按字段要求的类型重新填写。"),
    "records.supersede_missing": Plain(
        "要更正的那条记录找不到", "更正要指向这张表里已有的一条记录。", _Y, "从记录列表里选要更正的那一条。"),
    "records.supersede_twice": Plain(
        "这条记录已经被更正过了", "同一条记录只能更正一次，后面的修改要改在最新的那一条上。", _Y, "在最新的那一条上更正。"),
    "records.table_retired": Plain(
        "这一版记录表已经停用", "表单改过之后出了新版本，旧记录照原样保留、只能查看。", _Y, "重新发布应用，写到新版本里。"),
    "records.table_foreign": Plain(
        "写的记录表不属于这个应用", "正式运行只能写自己应用的记录表，不然别的应用的记录会被混进来、被不该看的人看到。", _Y,
        "回到写记录表那一步，改成这个应用自己的记录表，再重新发布。"),
    "records.table_missing": Plain(
        "记录表的数据文件不见了", "数据文件被移走或删掉，记录写不进去、也查不了。", _M, "请管理员检查数据目录。"),
    "records.file_exists": Plain(
        "记录表的数据文件已经有数据，不能重建", "为了不覆盖已有的记录，系统拒绝了重建。", _M, "请管理员检查数据目录。"),
    "records.action_key_missing": Plain(
        "写记录表没有经过动作流程", "只有经过动作流程，同一次提交才保证只写一条。", _A,
        "把写入放在「调用工具」节点里，不要放进 Agent。"),
    "files.missing": Plain("没有选文件", "", _Y, "选择要上传的文件。"),
    "files.empty": Plain("文件是空的", "空文件里没有可用的内容。", _Y, "检查后重新选择文件。"),
    "files.type_not_allowed": Plain(
        "这种类型的文件不能上传", "只收常见的文档、表格、图片和文字文件，别的类型可能带来风险。", _Y,
        "另存为 PDF、Word、Excel、CSV、图片或文字文件再上传。"),
    "files.type_not_accepted": Plain(
        "这里不收这种文件", "表单的这一项限定了文件类型或大小。", _Y, "按提示换一个文件。"),
    "files.content_mismatch": Plain(
        "文件内容和扩展名对不上", "改过扩展名的文件，系统没法按它说的类型读。", _Y, "用原始文件重新上传。"),
    "files.context_invalid": Plain(
        "没说清文件是给哪里用的", "上传要对应一个入口、一条待办或一个蓝图，系统才知道谁能用它。", _A,
        "从表单里的文件控件上传。"),
    "files.forbidden": Plain(
        "没有权限", "只有能用这个入口、处理这条待办、看得到这次运行或这条记录的人，才能上传或下载。", _M,
        "请应用的管理者给你授权。"),
    "files.ref_invalid": Plain(
        "文件没有上传好", "提交的是文件的引用，要先上传、拿到引用。", _Y, "重新上传这个文件再提交。"),
    "files.not_found": Plain(
        "文件不见了", "上传后 24 小时还没提交的文件会被清理。", _Y, "重新上传再提交。"),
    "files.not_yours": Plain(
        "这个文件不是你上传的", "只能提交自己上传的文件。", _Y, "自己上传一份再提交。"),
    "files.already_bound": Plain(
        "这个文件已经提交过一次", "一次上传只能随一次提交进运行，这样每次运行用的是哪份文件说得清。", _Y,
        "再提交的话请重新上传。"),
    "files.context_mismatch": Plain(
        "文件是在别的地方上传的", "文件只能用在上传时说明的那个入口或那条待办上。", _Y, "在这里重新上传。"),
    "files.bound": Plain(
        "已经提交的文件不能删除", "它是那次运行的证据，删掉就说不清当时用的是什么。", _Y, "不需要处理。"),
    "files.content_lost": Plain(
        "文件内容不在了或被改动过", "下载到的内容如果和当时登记的不一样，就不能当证据用。", _M, "请管理员检查数据目录。"),
})
# ---------------------------------------------------------------- 蓝图与模板（包 B1：blueprints/ops.py 的 bp.*、
# blueprints/instantiate.py、blueprints/generation.py 的 tpl.*）
PLAIN.update({
    "bp.missing": Plain(
        "蓝图上还有没填的卡片", "缺了这一项，模板没法生成能运行的工作流。", _Y, "在卡片上补上，或者告诉助手这一项是什么。"),
    "bp.unconfirmed": Plain(
        "有助手或模板提的项还没确认", "没确认的项是猜的，发布出去就成了正式的口径和规则。", _Y, "逐项看过，对的点确认，不对的直接改。"),
    "bp.data_unmapped": Plain(
        "有要看的数还没对到表或口径", "对不到数据，就算不出这个数。", _A,
        "请助手按数据目录对到表（或复用已发布的口径），对到之后由你确认。"),
    "bp.data_source_unknown": Plain(
        "有要看的数对到的数据源不存在", "试运行和发布时找不到这个数据源，这个数算不出来。", _Y,
        "在「看哪些数」里点「改」重新选数据源；填报登记汇总本应用的记录，选「本应用的记录表」。"),
    "bp.data_table_unknown": Plain(
        "有要看的数对到的表不在那个数据源里", "查不到这张表，这个数算不出来。", _Y,
        "在「看哪些数」里点「改」重新选表；表刚建的话，先到「数据」页读取一次表结构。"),
    "bp.people_unbound": Plain(
        "有一步还没定谁来处理", "流程走到这一步就没人接手。", _Y, "在「谁来做」里选人、组或岗位。"),
    "bp.reconciler_unbound": Plain(
        "写回的结果不明时没人核实", "这项写入能力没登记查询：结果不明时平台查不了，也不会自动重发，要有人去目标系统里看写进去没有。",
        _Y, "在「写回哪里」卡片的「结果不明时谁去核实」里选人或组。"),
    "bp.template_mismatch": Plain(
        "卡片上写的超出了这个场景能做的", "超出的那部分不会生效，做出来的应用和你说的对不上。", _Y,
        "按提示删掉或改小，或者换一个场景。"),
    "bp.refused": Plain(
        "这处修改没有采纳", "改动不符合这张卡片的写法。", _Y, "按提示的写法重新改。"),
    "tpl.skeleton_changed": Plain(
        "改动超出了模板留出的位置", "模板的结构是锁住的，审批、写入、出具这些地方不能被顺手改掉。", _A,
        "只改模板留给助手的取数和指标；结构要变，请换一个场景或回画布。"),
    "tpl.joint_unfilled": Plain(
        "还有取数或指标没写好", "缺了它，试运行算不出数。", _A, "请助手填上；填不上时补充说明数从哪来、怎么算。"),
    "tpl.joint_refused": Plain(
        "助手想改模板锁住的地方，没有采纳", "模板锁住的结构不归助手改。", _A, "不需要处理；要改结构请回到卡片或换一个场景。"),
    "tpl.metric_expr": Plain(
        "指标的算法写得不合规矩", "指标只能从取数结果里按格取数再算，这样每个数都能追到出处。", _A, "请助手按规矩重写这个指标。"),
    "tpl.metric_column": Plain(
        "指标要用的列，取数的 SQL 里没有", "运行时这个指标会算不出来。", _A, "请助手让取数的 SQL 交出这一列，或改指标。"),
    "tpl.sql_rejected": Plain(
        "取数的 SQL 不是只读的一条查询", "取数只能读，不能改数据，也只能是一条语句。", _A, "请助手重写成一条只读的查询。"),
})
PLAIN_PREFIX: dict[str, Plain] = {
    # 子工作流、口径卡固定的版本不是「已发布且未退役」的（apps/versions.py）
    "subgraph_version_": Plain(
        "引用的工作流版本没发布或已退役", "正式运行发起时会被拒绝。", _Y, "在那个工作流里发布新版本，再把这里的引用换过去。"),
    "caliber_version_": Plain(
        "引用的口径卡版本没发布或已退役", "正式运行发起时会被拒绝。", _Y, "在那个工作流里发布新版本，再把这里的引用换过去。"),
}

#: 要管理员处理的编号：端点、能力登记归管理员管（受管级发布、降级的权限在发布单里另报 plan.admin_required、
#: plan.downgrade_admin）
ADMIN_CODES = frozenset(code for code, plain in PLAIN.items() if plain.who == _M)


def plain_of(code: str | None) -> Plain | None:
    if not code:
        return None
    if code in PLAIN:
        return PLAIN[code]
    for prefix, plain in PLAIN_PREFIX.items():
        if code.startswith(prefix):
            return plain
    return None


def who_of(issue: dict[str, Any]) -> str:
    """这条问题归谁处理（you / assistant / admin）。依次看：要管理员的编号；自动修复给的修复种类（fix_kind：choice 要人
    选，auto、assist 助手能做）；应用检查只有人能处理的（api/copilot_gen.APP_HUMAN_ONLY）；白话里登记的那一档（应用检查
    其余的登记成「助手可以修」：助手的自查会修它们）；没登记的应用检查算「助手可以修」，别的算「你来定」。"""
    from app.api.copilot_gen import APP_HUMAN_ONLY

    code = str(issue.get("code") or "")
    if code in ADMIN_CODES:
        return _M
    kind = issue.get("fix_kind")
    if kind == "choice":
        return _Y
    if kind in ("auto", "assist"):
        return _A
    if code in APP_HUMAN_ONLY:
        return _Y
    plain = plain_of(code)
    if plain is not None:
        return plain.who
    return _A if code.startswith("app.") else _Y


def with_plain(issue: dict[str, Any]) -> dict[str, Any]:
    """给一条问题补上 plain = {title, why, who, action}；没登记的不补（照旧显示技术说法）。who 按 who_of 判。"""
    plain = plain_of(issue.get("code"))
    if plain is None:
        return issue
    return {**issue, "plain": {"title": plain.title, "why": plain.why, "who": who_of(issue), "action": plain.action}}
