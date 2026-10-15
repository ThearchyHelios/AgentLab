"""人和组的名录：选人控件的候选（GET /api/directory）、应用检查核对岗位绑定和授权、改授权时核对填的名字，同一处取、
同一个口径（停用的人也在名录里，标 disabled：已经绑着他的岗位、授权要能认出「这个人停用了」，不是「没有这个人」）。

名录里对不上怎么算看登录方式（同前端 PrincipalPicker）：
- 账号密码登录：人和组都在本系统里建，名录里没有就是填错了（或人被删了），停用的人处理不了待办——都算错；
- 单点登录：人和组第一次登录后才进名录，名录里没有的照实提醒、不拒收；停用的人名录里认得出，照样算错；
- 关登录：没有用户表，签批记的是各自填的署名，不核对。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

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
