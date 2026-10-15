"""场景模板列表（docs/design/normal-user-apps.md §3.3）：GET /api/templates。登录即可看（新建应用的「从场景模板开始」）。

模板登记在导入这个模块时核对一遍（blueprints/instantiate.registry_problems）：首批都登记了、关节类型认得、每个模板的
示例蓝图实例化出来过得了画布校验、骨架比对没有差异。缺一样启动即报错（N3：模板定义、实例化、测试三样都要给）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.blueprints.instantiate import registry_problems
from app.blueprints.templates import list_templates

router = APIRouter(prefix="/api/templates", tags=["templates"])

_PROBLEMS = registry_problems()
if _PROBLEMS:
    raise RuntimeError("场景模板登记不完整：" + "；".join(_PROBLEMS))


def template_out(template: Any) -> dict[str, Any]:
    """一个模板交给前端的样子（frontend/src/typesBlueprint.ts 的 TemplateInfo）。"""
    return {"id": template.id, "version": template.version, "label": template.label,
            "description": template.description, "applies_when": template.applies_when,
            "cards": [{"card": card, "note": note} for card, note in template.cards.items()],
            "personal": template.personal, "tags": list(template.tags)}


@router.get("")
async def list_templates_api() -> dict[str, Any]:
    return {"items": [template_out(t) for t in list_templates()]}
