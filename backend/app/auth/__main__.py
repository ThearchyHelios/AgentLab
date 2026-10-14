"""找回管理员：python -m app.auth reset-admin [--username 名字] [--password 密码]

开了登录、管理员忘了密码或者被停用、单点登录配错了进不去时，在服务器上执行。用户名已存在的：改成管理员、启用、
重设密码、吊销已有的会话；不存在的建一个本地管理员。不给密码时随机生成一个，打印一次。单点登录模式下也能用这个
账号密码登录（登录页的账号密码入口）。写一条审计，操作人记为 cli。
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import sys


async def reset_admin(username: str, password: str) -> str:
    from sqlalchemy import select

    from app.auth.audit import record
    from app.auth.passwords import hash_password
    from app.auth.sessions import revoke_user
    from app.db.base import SessionLocal, init_db
    from app.db.models import User

    await init_db()
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        created = user is None
        if user is None:
            user = User(username=username, display_name=username, created_by="cli")
            session.add(user)
            await session.flush()
        else:
            await revoke_user(session, user.id)
        user.role, user.disabled, user.password_hash = "admin", False, hash_password(password)
        await record(session, "user.reset_admin", "user", user.id, actor="cli", verified=False,
                     data={"username": username, "created": created})
        await session.commit()
    return "已新建" if created else "已重置"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth")
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser("reset-admin", help="把一个账号设为管理员并重设密码（不存在就新建）")
    reset.add_argument("--username", default="admin")
    reset.add_argument("--password", default=None, help="不给就随机生成一个并打印")
    args = parser.parse_args(argv)

    from app.auth.passwords import password_problem

    password = args.password or secrets.token_urlsafe(12)
    if problem := password_problem(password):
        print(problem, file=sys.stderr)
        return 2
    done = asyncio.run(reset_admin(args.username, password))
    print(f"{done}管理员「{args.username}」。")
    if not args.password:
        print(f"临时密码：{password}（只显示这一次，登录后请到「设置 → 身份」修改）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
