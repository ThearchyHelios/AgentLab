"""人和组的名录：选人控件的候选（GET /api/directory）、应用检查核对岗位绑定和授权、改授权时核对填的名字，同一处取、
同一个口径（停用的人也在名录里，标 disabled：已经绑着他的岗位、授权要能认出「这个人停用了」，不是「没有这个人」）。

使用者选人（表单里的选人控件、个人应用提交审核）不拿整份名录，拿按用途算的候选池（candidates，GET
/api/directory/candidates）：只有这件事能选的人，只给用户名和显示名，停用的不列；开了登录时提交的值按同一个池核对
（docs/design/next-round/05-已知遗留.md (b)）。名录整份仍只给搭建者。

名录里对不上怎么算看登录方式（同前端 PrincipalPicker）：
- 账号密码登录：人和组都在本系统里建，名录里没有就是填错了（或人被删了），停用的人处理不了待办——都算错；
- 单点登录：人和组第一次登录后才进名录，名录里没有的照实提醒、不拒收；停用的人名录里认得出，照样算错；
- 关登录：没有用户表，签批记的是各自填的署名，不核对。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: 名录里对不上的两种：没有这个人或组、人停用了
Problem = Literal["missing", "disabled"]
_KIND = {"user": "用户", "group": "组"}


@dataclass(frozen=True)
class DirectoryUser:
    username: str
    display_name: str
    disabled: bool


@dataclass(frozen=True)
class DirectoryGroup:
    name: str
    label: str


@dataclass
class Directory:
    users: dict[str, DirectoryUser] = field(default_factory=dict)
    groups: dict[str, DirectoryGroup] = field(default_factory=dict)

    def problem(self, kind: str, name: str) -> Problem | None:
        """这一项在名录里对不上的地方；所有人（everyone）不判。"""
        if kind == "group":
            return None if name in self.groups else "missing"
        if kind == "user":
            user = self.users.get(name)
            return "missing" if user is None else "disabled" if user.disabled else None
        return None


async def display_names(session: AsyncSession, usernames: Iterable[str | None]) -> dict[str, str]:
    """用户名 → 显示名（名录里有的；没写显示名的就是用户名）。待办上派给谁、谁回复的、谁处理的给人看时写显示名，悬停给
    用户名。关登录时没有用户表，查不到的照写署名（调用方 people.get(名字, 名字)）。"""
    from app.db.models import User

    wanted = sorted({u for u in usernames if u})
    if not wanted:
        return {}
    rows = (await session.execute(select(User.username, User.display_name).where(User.username.in_(wanted)))).all()
    return {username: display or username for username, display in rows}


async def load_directory(session: AsyncSession) -> Directory:
    from app.db.models import Group, User

    users = (await session.execute(select(User).order_by(User.username))).scalars()
    groups = (await session.execute(select(Group).order_by(Group.name))).scalars()
    return Directory(
        users={u.username: DirectoryUser(u.username, u.display_name or u.username, u.disabled) for u in users},
        groups={g.name: DirectoryGroup(g.name, g.label or g.name) for g in groups})


@dataclass(frozen=True)
class Mismatch:
    """一项对不上：blocking 为真是错（账号密码登录名录里没有、任何登录方式下停用了），为假只提醒（单点登录名录里还没有）。
    text 是「用户「x」已停用」这样的半句，调用方接上是哪个岗位、哪一档授权。"""
    kind: str
    name: str
    problem: Problem
    blocking: bool
    text: str


def mismatch(directory: Directory, mode: str, kind: str, name: str) -> Mismatch | None:
    """按登录方式判这一项：关登录、所有人、对得上的都是 None。"""
    if mode == "off":
        return None
    problem = directory.problem(kind, name)
    if problem is None:
        return None
    what = f"{_KIND.get(kind, kind)}「{name}」"
    if problem == "disabled":
        return Mismatch(kind, name, problem, True, f"{what}已停用")
    if mode == "oidc":
        return Mismatch(kind, name, problem, False,
                        f"{what}名录里还没有：单点登录的人和组第一次登录后才进名录，名字要和身份提供方里的一致才生效")
    return Mismatch(kind, name, problem, True, f"{what}名录里没有")


# --------------------------------------------------------------------------
# 候选池：使用者选人时能选谁（05-已知遗留 (b)）
# --------------------------------------------------------------------------

#: 候选池的用途：form 表单里的选人控件（入口表单、待办表单）；review 个人应用提交审核选审核的人
PURPOSES = ("form", "review")
#: 一次最多给几位候选
CANDIDATES_MAX = 50
#: 表单字段声明候选池（x-principal.pool）：app 能用这个应用的人（缺省）；group:<组> 这个组的成员；slot:<岗位> 这个岗位
#: 绑定的人和组（按应用版本的 bindings）
POOL_APP = "app"
POOL_GROUP = "group:"
POOL_SLOT = "slot:"


def pool_problem(value: Any) -> str | None:
    """x-principal 的写法对不对：只能是 {pool: app | group:<组> | slot:<岗位>}。没问题为 None。"""
    if not isinstance(value, dict) or set(value) != {"pool"} or not isinstance(value.get("pool"), str):
        return "的候选范围只能写成 {\"pool\": \"app\"}、{\"pool\": \"group:<组名>\"} 或 {\"pool\": \"slot:<岗位>\"}"
    pool = value["pool"].strip()
    if pool == POOL_APP:
        return None
    for prefix in (POOL_GROUP, POOL_SLOT):
        if pool.startswith(prefix) and pool[len(prefix):].strip():
            return None
    return "的候选范围只能是 app（能用这个应用的人）、group:<组名> 或 slot:<岗位>"


def pool_of(field: Any) -> str:
    """表单字段声明的候选池；没声明、写法不对的按缺省（能用这个应用的人）。"""
    setting = field.get("x-principal") if isinstance(field, dict) else None
    if isinstance(setting, dict) and pool_problem(setting) is None:
        return setting["pool"].strip()
    return POOL_APP


@dataclass
class Pool:
    """一个候选池。open：池就是全部启用的人（应用授权给了所有人）——不带前缀不列，免得等于发通讯录。"""
    users: dict[str, DirectoryUser] = field(default_factory=dict)
    groups: dict[str, DirectoryGroup] = field(default_factory=dict)
    open: bool = False

    def has(self, name: str) -> bool:
        """提交的这个名字在不在池里（用户名或组名）。"""
        return name in self.users or name in self.groups

    def pick(self, q: str | None, limit: int = CANDIDATES_MAX) -> dict[str, Any]:
        """接口给的那一份：{users: [{username, display_name}], groups: [{name, label}], truncated}；按前缀筛
        （用户名或显示名，不分大小写）。池是 open 的、又没带前缀：什么都不给，needs_query 为真。"""
        prefix = (q or "").strip().lower()
        if self.open and not prefix:
            return {"users": [], "groups": [], "truncated": False, "needs_query": True}
        limit = max(1, min(int(limit or CANDIDATES_MAX), CANDIDATES_MAX))

        def hit(*names: str) -> bool:
            return not prefix or any(n.lower().startswith(prefix) for n in names if n)

        users = [u for u in sorted(self.users.values(), key=lambda u: u.username) if hit(u.username, u.display_name)]
        groups = [g for g in sorted(self.groups.values(), key=lambda g: g.name) if hit(g.name, g.label)]
        return {"users": [{"username": u.username, "display_name": u.display_name} for u in users[:limit]],
                "groups": [{"name": g.name, "label": g.label} for g in groups[:limit]],
                "truncated": len(users) > limit or len(groups) > limit}


async def _members(session: AsyncSession, group: str) -> dict[str, DirectoryUser]:
    from app.db.models import Group, GroupMember, User

    rows = (await session.execute(
        select(User).join(GroupMember, GroupMember.user_id == User.id).join(Group, Group.id == GroupMember.group_id)
        .where(Group.name == group, User.disabled.is_(False)))).scalars()
    return {u.username: DirectoryUser(u.username, u.display_name or u.username, False) for u in rows}


async def _people(session: AsyncSession, items: Iterable[tuple[str, str]]) -> Pool:
    """一组（种类, 名字）展开成池：人按名录认（停用的、名录里没有的不列），组列出组本身、再展开成员。"""
    from app.db.models import Group, User

    pool = Pool()
    items = list(items)
    names = sorted({n for k, n in items if k == "user" and n})
    if names:
        for u in (await session.execute(select(User).where(User.username.in_(names), User.disabled.is_(False))))\
                .scalars():
            pool.users[u.username] = DirectoryUser(u.username, u.display_name or u.username, False)
    groups = sorted({n for k, n in items if k == "group" and n})
    if groups:
        for g in (await session.execute(select(Group).where(Group.name.in_(groups)))).scalars():
            pool.groups[g.name] = DirectoryGroup(g.name, g.label or g.name)
            pool.users.update(await _members(session, g.name))
    return pool


async def _everyone(session: AsyncSession) -> Pool:
    from app.db.models import Group, User

    pool = Pool(open=True)
    for u in (await session.execute(select(User).where(User.disabled.is_(False)))).scalars():
        pool.users[u.username] = DirectoryUser(u.username, u.display_name or u.username, False)
    for g in (await session.execute(select(Group))).scalars():
        pool.groups[g.name] = DirectoryGroup(g.name, g.label or g.name)
    return pool


async def app_pool(session: AsyncSession, app: Any) -> Pool:
    """能用这个应用的人：使用及以上的授权（人、组展开成员），加上管理它的人（管理员、建它的人）。授权给了所有人的，池就是
    全部启用的人（open）。按 apps/access 的口径：搭建者没有授权时只能查看全部实例，不算「能用」的候选。"""
    from app.apps.access import RANK, grants_of
    from app.db.models import User

    grants = [g for g in await grants_of(session, app.id) if RANK.get(g.role, 0) >= RANK["use"]]
    if any(g.principal_kind == "everyone" for g in grants):
        return await _everyone(session)
    admins = (await session.execute(select(User.username).where(User.role == "admin"))).scalars()
    items = [(g.principal_kind, g.principal) for g in grants] + [("user", a) for a in admins]
    if getattr(app, "created_by", None):
        items.append(("user", app.created_by))
    return await _people(session, items)


async def field_pool(session: AsyncSession, app: Any, bindings: dict[str, Any] | None, field: Any) -> Pool:
    """表单里一个选人控件的候选池（按字段的 x-principal，缺省能用这个应用的人）。bindings 是这次用到的应用版本的绑定。"""
    pool = pool_of(field)
    if pool.startswith(POOL_GROUP):
        name = pool[len(POOL_GROUP):].strip()
        return Pool(users=await _members(session, name))
    if pool.startswith(POOL_SLOT):
        bound = ((bindings or {}).get("slots") or {}).get(pool[len(POOL_SLOT):].strip())
        items = [(str(b.get("kind") or "user"), str(b.get("name") or "")) for b in bound or [] if isinstance(b, dict)]
        if any(k == "everyone" for k, _ in items):
            return await _everyone(session)
        return await _people(session, items)
    return await app_pool(session, app)


async def review_pool(session: AsyncSession) -> Pool:
    """个人应用提交审核能交给谁：启用的搭建者和管理员（同 blueprints/personal._review_targets 的规则）。"""
    from app.db.models import User

    rows = (await session.execute(select(User).where(User.role.in_(("builder", "admin")),
                                                     User.disabled.is_(False)))).scalars()
    return Pool(users={u.username: DirectoryUser(u.username, u.display_name or u.username, False) for u in rows})


def principal_fields(form: Any) -> dict[str, dict[str, Any]]:
    """表单里的选人控件：{字段名: 字段}。"""
    props = form.get("properties") if isinstance(form, dict) else None
    return {name: f for name, f in (props or {}).items() if isinstance(f, dict) and f.get("x-widget") == "principal"}


#: 选人控件的值不在候选池里时表单错误的 code（apps/spec.FORM_ERROR_CODES 的 principal）
PRINCIPAL_ERROR_CODE = "principal"


async def principal_errors(session: AsyncSession, app: Any, bindings: dict[str, Any] | None, form: Any,
                           values: Any, mode: str) -> list[dict[str, str]]:
    """开了登录时按候选池核对表单里选人控件的值：不在池里的按表单错误交回（code=principal），话写成「「x」不在可选的人里」。
    关登录不核对：没有用户表，签批记的是各自填的署名。没填的不在这里报（必填由表单校验报）。"""
    if mode == "off" or not isinstance(values, dict):
        return []
    out: list[dict[str, str]] = []
    for name, f in principal_fields(form).items():
        value = values.get(name)
        if value in (None, "") or not isinstance(value, str):
            continue
        pool = await field_pool(session, app, bindings, f)
        if not pool.has(value.strip()):
            hint = "：单点登录的人要先登录一次才进名录" if mode == "oidc" else "，请从候选中选择"
            out.append({"path": name, "code": PRINCIPAL_ERROR_CODE, "message": f"「{value.strip()}」不在可选的人里{hint}"})
    return out
