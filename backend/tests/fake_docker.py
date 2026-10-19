"""给 deploy/deploy.sh 的测试用的假 docker（tests/test_deploy_script.py）：不碰本机的 docker，状态放一个 JSON 文件里。

只认 deploy.sh 用到的那些子命令和 inspect 格式，认不出的直接报错退出（测试就会失败），免得假的悄悄放过脚本的新用法。
每次调用记一行 JSON（argv）到 FAKE_DOCKER_LOG，测试按它核对先后顺序和参数。用完就删的容器（run --rm）里要做的事照做：
python -m app.ops 真跑（/data 换成挂进来的目录），chown 不做，mkdir、pg_dump 在目录里建文件，copy-to-postgres 按
FAKE_COPY_FAIL 成功或失败。

几个开关（环境变量）：
    FAKE_COPY_FAIL=dry|real          copy-to-postgres 的核对（--dry-run）或正式导入失败
    FAKE_PGDUMP_FAIL=1               pg_dump 失败
    FAKE_PSQL_FAIL=1                 docker exec … psql 失败
    FAKE_UNHEALTHY_ENV=<变量名>      带这个环境变量（-e）起的容器健康检查不过
    FAKE_UNHEALTHY_IMAGE=<镜像>      用这个镜像起的容器健康检查不过
    FAKE_RESTARTING_IMAGE=<镜像>     用这个镜像起的容器一直在重启（restarting）
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
# 测试里 import 这个模块只为 host_arch，不设这两个变量
STATE = Path(os.environ.get("FAKE_DOCKER_STATE", "fake-docker-state.json"))
LOG = Path(os.environ.get("FAKE_DOCKER_LOG", "fake-docker-calls.jsonl"))

VALUE_OPTS = {
    "--name", "--label", "--restart", "--network", "-e", "--env", "-v", "--volume", "-p", "--publish", "--env-file",
    "--entrypoint", "--user", "-u", "--health-cmd", "--health-interval", "--health-timeout", "--health-retries",
    "--health-start-period", "--stop-timeout", "--log-opt", "--security-opt", "--device", "--group-add", "--tmpfs",
}
FLAG_OPTS = {"-d", "--detach", "--rm", "-i", "-t", "-it"}


def host_arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


def load() -> dict:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    state.setdefault("images", {})
    state.setdefault("containers", {})
    # 网络、数据卷：名字 → 标签。测试里直接写列表（只有名字、没标签）的也认
    for kind in ("networks", "volumes"):
        value = state.get(kind) or {}
        state[kind] = {name: {} for name in value} if isinstance(value, list) else value
    return state


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    sys.exit(code)


def unknown(argv: list[str]) -> None:
    fail(f"fake docker: 认不出 {argv!r}", 99)


def image_info(state: dict, ref: str) -> dict | None:
    return state["images"].get(ref)


def container_health(c: dict) -> str:
    if c["state"] != "running":
        return c.get("health", "")
    bad_env = os.environ.get("FAKE_UNHEALTHY_ENV")
    if bad_env and bad_env in c["env"]:
        return "unhealthy"
    if os.environ.get("FAKE_UNHEALTHY_IMAGE") and c["image"] == os.environ["FAKE_UNHEALTHY_IMAGE"]:
        return "unhealthy"
    return "healthy"


def fmt_container(c: dict, fmt: str) -> str:
    if fmt == "{{.State.Status}}":
        return c["state"]
    if fmt == "{{.State.Running}}":
        return "true" if c["state"] == "running" else "false"
    if fmt == "{{.Config.Image}}":
        return c["image"]
    if ".Mounts" in fmt and (m := re.search(r'eq \.Destination "([^"]+)"', fmt)):
        # .Source（绑定挂载的宿主机路径）和 .Name（数据卷名）这里存的是同一个值
        return c["mounts"].get(m.group(1), "")
    if ".State.Health" in fmt:
        return container_health(c)
    if ".Config.Labels" in fmt and (m := re.search(r'index \.Config\.Labels "([^"]+)"', fmt)):
        return c["labels"].get(m.group(1), "")
    if ".HostConfig.PortBindings" in fmt:
        return "".join(p.rsplit(":", 2)[-2] for p in c["ports"])
    raise KeyError(fmt)


def fmt_image(img: dict, fmt: str) -> str:
    if fmt == "{{.Architecture}}":
        return img["arch"]
    if fmt == "{{.Os}}/{{.Architecture}}":
        return f"linux/{img['arch']}"
    if fmt == "{{.Config.User}}":
        return img.get("user", "")
    if '"agentlab.microvm"' in fmt:
        return img.get("microvm", "0")
    raise KeyError(fmt)


def parse_run(args: list[str]) -> tuple[dict, list[str]]:
    opts: dict = {"env": {}, "labels": {}, "mounts": {}, "ports": [], "flags": set(), "volumes": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a in FLAG_OPTS:
            opts["flags"].add(a)
            i += 1
            continue
        if a.startswith("-") and "=" in a and a.split("=", 1)[0] in VALUE_OPTS:
            a, value = a.split("=", 1)
            i += 1
        elif a in VALUE_OPTS:
            value = args[i + 1]
            i += 2
        elif a.startswith("-"):
            unknown(["run", *args])
        else:
            break
        if a in ("-e", "--env"):
            key, _, val = value.partition("=")
            opts["env"][key] = val
        elif a == "--label":
            key, _, val = value.partition("=")
            opts["labels"][key] = val
        elif a in ("-v", "--volume"):
            src, dst = value.split(":")[:2]
            opts["mounts"][dst] = src
            if not src.startswith("/"):
                opts["volumes"].append(src)
        elif a in ("-p", "--publish"):
            opts["ports"].append(value)
        else:
            opts[a] = value
    return opts, args[i:]


def to_host(arg: str, data: str | None) -> str:
    if data and (arg == "/data" or arg.startswith("/data/")):
        return data + arg[len("/data"):]
    return arg


def run_oneoff(state: dict, opts: dict, image: str, command: list[str]) -> int:
    data = opts["mounts"].get("/data")
    entry = opts.get("--entrypoint")
    if entry == "python" and command[:2] == ["-m", "app.ops"]:
        sys.path.insert(0, str(BACKEND))
        from app import ops

        rest = [to_host(a, data) for a in command[2:]]
        if rest and rest[0] in ("postgres-secrets", "state", "backup-sqlite", "backup-dir") and "--data" not in rest:
            rest[1:1] = ["--data", data or "/data"]
        return ops.main(rest)
    if entry == "python" and command[:3] == ["-m", "app.db", "copy-to-postgres"]:
        which = "dry" if "--dry-run" in command else "real"
        if os.environ.get("FAKE_COPY_FAIL") == which:
            print("没有导入：fake 的失败", file=sys.stderr)
            return 1
        print("核对通过")
        return 0
    if entry == "chown":
        return 0
    if entry == "mkdir":
        for a in command:
            if not a.startswith("-"):
                Path(to_host(a, data)).mkdir(parents=True, exist_ok=True)
        return 0
    if entry == "sh" and command[:1] == ["-c"] and 'pg_dump "$@"' in command[1]:
        # sh -c '<脚本>' pg_dump <参数…>：照脚本里的 umask 建文件
        umask = 0o077 if "umask 077" in command[1] else 0o022
        if os.environ.get("FAKE_PGDUMP_FAIL"):
            print("pg_dump: fake 的失败", file=sys.stderr)
            return 1
        args = command[3:]
        out = Path(to_host(args[args.index("-f") + 1], data))
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666 & ~umask)
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"PGDMP fake")
        return 0
    unknown(["run", "--rm", image, *command])
    return 99


def main(argv: list[str]) -> int:
    with LOG.open("a") as fh:
        fh.write(json.dumps(argv, ensure_ascii=False) + "\n")
    state = load()
    if not argv:
        unknown(argv)
    cmd, args = argv[0], argv[1:]

    if cmd == "version":
        print("27.0.0-fake")
        return 0

    if cmd == "image" and args[:1] == ["inspect"]:
        fmt = None
        rest = args[1:]
        if rest[:1] == ["-f"]:
            fmt, rest = rest[1], rest[2:]
        img = image_info(state, rest[0])
        if img is None:
            fail(f"Error: No such image: {rest[0]}")
        if fmt:
            try:
                print(fmt_image(img, fmt))
            except KeyError:
                unknown(argv)
        else:
            print("[{}]")
        return 0

    if cmd == "load":
        tar = Path(args[args.index("-i") + 1])
        for line in tar.read_text().splitlines():
            ref, _, user = line.partition(" ")
            if ref:
                state["images"][ref] = {"arch": host_arch(), "user": user, "microvm": "0"}
                print(f"Loaded image: {ref}")
        save(state)
        return 0

    if cmd == "rmi":
        for ref in args:
            state["images"].pop(ref, None)
        save(state)
        return 0

    if cmd == "container" and args[:1] == ["inspect"]:
        return 0 if args[1] in state["containers"] else 1

    if cmd == "inspect":
        if args[:1] != ["-f"]:
            unknown(argv)
        fmt, name = args[1], args[2]
        c = state["containers"].get(name)
        if c is None:
            fail(f"Error: No such object: {name}")
        try:
            print(fmt_container(c, fmt))
        except KeyError:
            unknown(argv)
        return 0

    if cmd == "run":
        opts, rest = parse_run(args)
        image, command = rest[0], rest[1:]
        if image_info(state, image) is None:
            fail(f"Unable to find image '{image}' locally", 125)
        network = opts.get("--network")
        if network and network not in ("none", "host", "bridge") and network not in state["networks"]:
            fail(f"docker: Error response from daemon: network {network} not found.", 125)
        if "--rm" in opts["flags"]:
            return run_oneoff(state, opts, image, command)
        name = opts.get("--name")
        if not name or "-d" not in opts["flags"]:
            unknown(argv)
        if name in state["containers"]:
            fail(f'docker: Error response from daemon: Conflict. The container name "/{name}" is already in use.', 125)
        for vol in opts["volumes"]:
            state["volumes"].setdefault(vol, {})
        flapping = os.environ.get("FAKE_RESTARTING_IMAGE") == image
        state["containers"][name] = {
            "image": image, "state": "restarting" if flapping else "running", "env": opts["env"],
            "labels": opts["labels"], "mounts": opts["mounts"], "ports": opts["ports"], "network": network or "bridge",
            "restart": opts.get("--restart", "no"),
        }
        save(state)
        print("f" * 64)
        return 0

    if cmd == "update":
        name = args[-1]
        c = state["containers"].get(name)
        if c is None:
            fail(f"Error response from daemon: No such container: {name}")
        for i, a in enumerate(args[:-1]):
            if a.startswith("--restart="):
                c["restart"] = a.split("=", 1)[1]
            elif a == "--restart":
                c["restart"] = args[i + 1]
        save(state)
        return 0

    if cmd in ("start", "stop", "restart"):
        names = [a for a in args if not a.startswith("-") and not a.isdigit()]
        for name in names:
            c = state["containers"].get(name)
            if c is None:
                fail(f"Error response from daemon: No such container: {name}")
            c["state"] = "exited" if cmd == "stop" else "running"
        save(state)
        return 0

    if cmd == "rm":
        force = "-f" in args
        for name in (a for a in args if not a.startswith("-")):
            c = state["containers"].get(name)
            if c is None:
                fail(f"Error response from daemon: No such container: {name}")
            if c["state"] == "running" and not force:
                fail(f"Error response from daemon: cannot remove container {name}: container is running")
            del state["containers"][name]
        save(state)
        return 0

    if cmd == "rename":
        old, new = args
        if old not in state["containers"] or new in state["containers"]:
            fail(f"Error response from daemon: cannot rename {old} to {new}")
        state["containers"][new] = state["containers"].pop(old)
        save(state)
        return 0

    if cmd == "exec":
        name, command = args[0], args[1:]
        c = state["containers"].get(name)
        if c is None or c["state"] != "running":
            fail(f"Error response from daemon: container {name} is not running")
        if command[:1] == ["psql"]:
            return 1 if os.environ.get("FAKE_PSQL_FAIL") else 0
        if command[:1] == ["python"]:
            print("子进程（fake）")
            return 0
        if command[:1] == ["pg_isready"]:
            return 0
        unknown(argv)

    if cmd == "logs":
        print("fake 的日志")
        return 0

    if cmd == "port":
        c = state["containers"].get(args[0])
        if c is None:
            return 1
        for p in c["ports"]:
            host_part = p.rsplit(":", 1)[0]
            print(host_part)
        return 0

    if cmd in ("network", "volume"):
        kind = cmd + "s"
        sub, name = args[0], args[-1]
        if sub == "inspect":
            if name not in state[kind]:
                return 1
            if len(args) >= 3 and args[1] == "-f":
                m = re.search(r'index \.Labels "([^"]+)"', args[2])
                if not m:
                    unknown(argv)
                print(state[kind][name].get(m.group(1), ""))
            return 0
        if sub == "create":
            labels = dict(a.split("=", 1) for a in (args[i + 1] for i, x in enumerate(args) if x == "--label"))
            if name in state[kind] and cmd == "network":
                fail(f"network with name {name} already exists")
            state[kind].setdefault(name, labels)
            save(state)
            return 0
        if sub == "rm":
            if cmd == "network" and any(c["network"] == name for c in state["containers"].values()):
                fail(f"error while removing network: network {name} has active endpoints")
            state[kind].pop(name, None)
            save(state)
            return 0
        unknown(argv)

    unknown(argv)
    return 99


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
