#!/usr/bin/env bash
# AgentLab 一键部署（离线包自带）。只依赖 docker，不需要 docker compose、不需要联网。
# 配置在同目录的 agentlab.env 里，用法见 ./deploy.sh --help。
set -euo pipefail

# 下面两个值由 scripts/package-docker.sh 打包时填进来
VERSION="__AGENTLAB_VERSION__"
IMAGE_ARCH="__AGENTLAB_ARCH__"
IMAGE="agentlab:$VERSION"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/agentlab.env"
IMAGE_TAR="$HERE/image.tar"
# 内置 PostgreSQL 的镜像：离线包里带一份（scripts/package-docker.sh 打进去），本机已经有同名镜像就不导入
POSTGRES_IMAGE_TAR="$HERE/postgres-image.tar"

# 给沙箱用 bubblewrap 时容器要加的权限，说明见 agentlab.env 的沙箱一节
BWRAP_OPTS=(--security-opt seccomp=unconfined --security-opt systempaths=unconfined --security-opt apparmor=unconfined)

usage() {
  cat <<USAGE
用法：./deploy.sh [子命令] [选项]

  install [--postgres]
                     部署（默认）：导入镜像、创建数据目录、启动容器，等健康检查通过后打印访问地址。
                     已经有同名容器的，先停掉再换成新的，数据保留。改了 agentlab.env 也用它让配置生效。
                     本机有 KVM 时会先试着启动一台 microVM，起得来才让代码在 microVM 里执行（见 agentlab.env）。
                     --postgres：新部署用内置的 PostgreSQL（再起一个 PostgreSQL 容器，同一个 docker 网络、端口不对外；
                     密码第一次自动生成，存在数据目录的 .postgres/ 里）。用过一次之后这个数据目录就一直是内置
                     PostgreSQL，之后的 install / upgrade / start 不用再加。已经在用 SQLite 的，用 migrate-to-postgres。
  upgrade            用这个包里的镜像替换正在用的容器，数据保留。旧镜像留着，方便回退。换之前先自动备份库
                     （SQLite：数据目录的 backups/sqlite/；内置 PostgreSQL：pg_dump 到 backups/postgres/；各留最近
                     BACKUP_KEEP 份，缺省 5）。新版本启动时自动补表结构（迁移）和默认数据。
  migrate-to-postgres
                     把正在用 SQLite 的部署切到内置 PostgreSQL：停服务 → 备份 → 起 PostgreSQL → 先核对再导入 → 记下
                     「已切换」→ 起服务。哪一步出错都停在原地、仍用 SQLite。重复执行是安全的；SQLite 的库文件原样留着。
  start | stop       启动 / 停止。内置 PostgreSQL 时先起库、等它就绪再起服务；停的时候先停服务再停库。
  restart [--postgres]
                     重启服务（库停着的话先把库起来）。--postgres：内置 PostgreSQL 也一起重启（先停服务、再停库，
                     起的时候先起库）。
  status             查看容器状态、健康检查、数据目录、用的哪种库、代码沙箱和访问地址。
  logs [-f] [--tail N] [--postgres]
                     查看日志（默认最后 200 行），-f 持续跟随，--postgres 看内置 PostgreSQL 的。
  uninstall [--purge-image]
                     删除容器（内置 PostgreSQL 的容器和网络一起删）。默认保留数据和镜像，--purge-image 连 AgentLab
                     的镜像一起删。数据永远不删：数据目录和 PostgreSQL 的数据卷都留着。
  -h, --help         显示这段说明。

环境变量 DEPLOY_<名字> 盖过 agentlab.env 里的同名项（测试、一台机器上并排部署两套时用）：HOST_PORT、BIND_ADDR、
DATA_DIR、CONTAINER_NAME，以及内置 PostgreSQL 的 POSTGRES_IMAGE、POSTGRES_CONTAINER、DOCKER_NETWORK、POSTGRES_VOLUME，
备份份数 BACKUP_KEEP。

这个包：AgentLab ${VERSION}（linux/${IMAGE_ARCH}）
USAGE
}

say() { printf '==> %s\n' "$*"; }
warn() { printf '警告：%s\n' "$*" >&2; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }
bad_arg() { printf '认不出的参数：%s\n\n' "$1" >&2; usage >&2; exit 2; }

# ---------------------------------------------------------------------------
# 参数：先认完再干活，认不出的直接报错
# ---------------------------------------------------------------------------
CMD="install"
FOLLOW=""
TAIL="200"
PURGE_IMAGE=""
WITH_POSTGRES=""
LOGS_POSTGRES=""
RESTART_POSTGRES=""
if [ $# -gt 0 ]; then
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -*) ;;
    *) CMD="$1"; shift ;;
  esac
fi
case "$CMD" in
  install|upgrade|migrate-to-postgres|start|stop|restart|status|logs|uninstall) ;;
  *) printf '认不出的子命令：%s\n\n' "$CMD" >&2; usage >&2; exit 2 ;;
esac
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -f|--follow) [ "$CMD" = logs ] || bad_arg "$1"; FOLLOW=1 ;;
    --tail)
      [ "$CMD" = logs ] && [ $# -ge 2 ] || bad_arg "$1"
      TAIL="$2"; shift ;;
    --tail=*) [ "$CMD" = logs ] || bad_arg "$1"; TAIL="${1#--tail=}" ;;
    --purge-image) [ "$CMD" = uninstall ] || bad_arg "$1"; PURGE_IMAGE=1 ;;
    --postgres)
      case "$CMD" in
        install) WITH_POSTGRES=1 ;;
        logs) LOGS_POSTGRES=1 ;;
        restart) RESTART_POSTGRES=1 ;;
        *) bad_arg "$1" ;;
      esac ;;
    *) bad_arg "$1" ;;
  esac
  shift
done
case "$TAIL" in
  all) ;;
  ''|*[!0-9]*) die "--tail 要跟行数或 all：$TAIL" ;;
esac

case "$VERSION$IMAGE_ARCH" in
  *"__AGENTLAB"_*) die "这是仓库里的模板，要用 scripts/package-docker.sh 打出来的离线包里的 deploy.sh" ;;
esac

# ---------------------------------------------------------------------------
# 读 agentlab.env。不 source 它：docker --env-file 不处理引号和变量，这里按同样的规矩逐行读，
# 两边看到的值才一致；source 还会把值当命令执行
# ---------------------------------------------------------------------------
trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

unquote() {
  local s="$1"
  case "$s" in
    \"*\"|\'*\') s="${s:1:${#s}-2}" ;;
  esac
  printf '%s' "$s"
}

HOST_PORT="8000"
BIND_ADDR="0.0.0.0"
DATA_DIR="./data"
CONTAINER_NAME="agentlab"
ENABLE_BWRAP="0"
ENABLE_MICROVM="auto"
# 内置 PostgreSQL。小版本固定：同一个包到哪台机器都起同一个版本（pg_dump 和库同版本）；要换在 agentlab.env 里写
# POSTGRES_IMAGE。容器、网络、数据卷的名字缺省跟着 CONTAINER_NAME 走
POSTGRES_IMAGE="postgres:16.15"
POSTGRES_CONTAINER=""
DOCKER_NETWORK=""
POSTGRES_VOLUME=""
BACKUP_KEEP="5"
# agentlab.env 里设了外接的库（AGENTLAB_DATABASE_URL 等）没有：只记设了哪一项，值（带密码）不读进来
EXTERNAL_DB=""
EXTERNAL_URL_SET=""
DIRECT_URL_SET=""
# 这两项要看值：PgBouncer 事务模式的开关（内置库是直连，开了就拦下）、工作进程数（SQLite 只能是 1）。都原样经 --env-file 进容器
PGBOUNCER_VALUE=""
WORKERS_VALUE=""
# 内置 PostgreSQL 里的库名、用户名（PostgreSQL 容器第一次初始化时按它建）
PG_DB="agentlab"
PG_USER="agentlab"

read_env() {
  [ -f "$ENV_FILE" ] || die "找不到 ${ENV_FILE}，它应该和 deploy.sh 在同一个目录"
  if grep -q "$(printf '\r')" "$ENV_FILE"; then
    die "agentlab.env 是 Windows 换行（CRLF），docker 会把行尾的 \\r 当成值的一部分。先转换：sed -i 's/\\r\$//' agentlab.env"
  fi
  local line key val n=0
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    case "$(trim "$line")" in ''|'#'*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="$(trim "${line%%=*}")"
    val="${line#*=}"
    case "$key" in
      HOST_PORT|BIND_ADDR|DATA_DIR|CONTAINER_NAME|ENABLE_BWRAP|ENABLE_MICROVM|POSTGRES_IMAGE|POSTGRES_CONTAINER|\
      DOCKER_NETWORK|POSTGRES_VOLUME|BACKUP_KEEP)
        printf -v "$key" '%s' "$(unquote "$(trim "$val")")" ;;
      AGENTLAB_DATABASE_URL|AGENTLAB_CHECKPOINT_URL|AGENTLAB_DATABASE_DIRECT_URL)
        if [ -n "$(trim "$val")" ]; then
          [ -n "$EXTERNAL_DB" ] || EXTERNAL_DB="$key"
          case "$key" in
            AGENTLAB_DATABASE_URL) EXTERNAL_URL_SET=1 ;;
            AGENTLAB_DATABASE_DIRECT_URL) DIRECT_URL_SET=1 ;;
          esac
        fi ;;
      AGENTLAB_DB_PGBOUNCER) PGBOUNCER_VALUE="$(unquote "$(trim "$val")")" ;;
      AGENTLAB_WORKERS) WORKERS_VALUE="$(unquote "$(trim "$val")")" ;;
      AGENTLAB_DATA_DIR|AGENTLAB_WEB_DIST|AGENTLAB_PORT|AGENTLAB_HOST)
        die "agentlab.env 第 $n 行：不要设 ${key}。容器里的路径和端口是固定的，改宿主机这边用 DATA_DIR、HOST_PORT、BIND_ADDR" ;;
      *)
        case "$val" in
          \"*\"|\'*\') warn "agentlab.env 第 $n 行：$key 的值带了引号。docker 不去引号，引号会原样传进容器，一般要去掉" ;;
        esac ;;
    esac
  done < "$ENV_FILE"

  # 环境变量 DEPLOY_<名字> 盖过 agentlab.env（测试、并排部署两套时用）
  local k v
  for k in HOST_PORT BIND_ADDR DATA_DIR CONTAINER_NAME POSTGRES_IMAGE POSTGRES_CONTAINER DOCKER_NETWORK \
           POSTGRES_VOLUME BACKUP_KEEP; do
    v="$(printenv "DEPLOY_$k" 2>/dev/null || true)"
    [ -z "$v" ] || printf -v "$k" '%s' "$v"
  done
  [ -n "$POSTGRES_CONTAINER" ] || POSTGRES_CONTAINER="$CONTAINER_NAME-postgres"
  [ -n "$DOCKER_NETWORK" ] || DOCKER_NETWORK="$CONTAINER_NAME-net"
  [ -n "$POSTGRES_VOLUME" ] || POSTGRES_VOLUME="$CONTAINER_NAME-pgdata"

  case "$HOST_PORT" in ''|*[!0-9]*) die "HOST_PORT 必须是数字：$HOST_PORT" ;; esac
  if [ "$HOST_PORT" -lt 1 ] || [ "$HOST_PORT" -gt 65535 ]; then die "HOST_PORT 超出范围：$HOST_PORT"; fi
  [ -n "$BIND_ADDR" ] || die "BIND_ADDR 不能为空（所有网卡写 0.0.0.0，只给本机用写 127.0.0.1）"
  [ -n "$DATA_DIR" ] || die "DATA_DIR 不能为空"
  for k in CONTAINER_NAME POSTGRES_CONTAINER DOCKER_NETWORK POSTGRES_VOLUME; do
    v="${!k}"
    case "$v" in
      ''|[!a-zA-Z0-9]*|*[!a-zA-Z0-9_.-]*) die "$k 只能用字母、数字和 _ . -，并以字母或数字开头：$v" ;;
    esac
  done
  [ "$POSTGRES_CONTAINER" != "$CONTAINER_NAME" ] || die "POSTGRES_CONTAINER 不能和 CONTAINER_NAME 同名：$CONTAINER_NAME"
  case "$BACKUP_KEEP" in ''|*[!0-9]*|0) die "BACKUP_KEEP 要是正整数：$BACKUP_KEEP" ;; esac
  case "$POSTGRES_IMAGE" in ''|*[[:space:]]*) die "POSTGRES_IMAGE 写错了：$POSTGRES_IMAGE" ;; esac
  case "$ENABLE_BWRAP" in 0|1) ;; *) die "ENABLE_BWRAP 只能是 0 或 1：$ENABLE_BWRAP" ;; esac
  case "$ENABLE_MICROVM" in auto|0|1) ;; *) die "ENABLE_MICROVM 只能是 auto、0 或 1：$ENABLE_MICROVM" ;; esac

  # 相对路径以 deploy.sh 所在的目录为准，和从哪里运行无关
  # 下面比较的就是字面上的 ~，不需要展开
  # shellcheck disable=SC2088
  case "$DATA_DIR" in
    /*) DATA_PATH="$DATA_DIR" ;;
    "~"|"~/"*) DATA_PATH="$HOME${DATA_DIR#\~}" ;;
    *) DATA_PATH="$HERE/${DATA_DIR#./}" ;;
  esac
}

# ---------------------------------------------------------------------------
# docker 与镜像
# ---------------------------------------------------------------------------
check_docker() {
  command -v docker >/dev/null 2>&1 || die "没找到 docker。先装好 Docker Engine（离线机器要提前下载好安装包），再运行这个脚本"
  local out
  if ! out="$(docker version --format '{{.Server.Version}}' 2>&1)"; then
    case "$out" in
      *"ermission denied"*)
        die "当前用户没有权限访问 docker。二选一：
  - 用 sudo 运行：sudo ./deploy.sh $CMD
  - 把当前用户加进 docker 组（之后重新登录才生效）：sudo usermod -aG docker \$USER" ;;
      *) die "连不上 docker 服务，确认它在运行：sudo systemctl start docker
docker 的原话：$out" ;;
    esac
  fi
}

host_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    armv7l|armv6l|armhf) echo arm ;;
    *) uname -m ;;
  esac
}

check_arch() {
  local host
  host="$(host_arch)"
  [ "$host" = "$IMAGE_ARCH" ] && return 0
  local hint="在打包的机器上用 ./scripts/package-docker.sh --platform linux/$host 重新打一份。"
  [ "$host" = arm ] && hint="这台机器跑的是 32 位系统，AgentLab 只提供 64 位镜像。ARM 单板机请装 64 位系统（arm64）。"
  die "架构不一致：这个包里的镜像是 linux/${IMAGE_ARCH}，本机是 $(uname -m)。
不同架构的镜像跑不起来（没有模拟层时直接报 exec format error）。$hint"
}

image_present() { docker image inspect "$IMAGE" >/dev/null 2>&1; }

verify_tar() {
  # 拷贝中途断了、U 盘出错，docker load 的报错不好懂：先对一下校验和
  local name="${1:-image.tar}" line
  [ -f "$HERE/SHA256SUMS" ] || return 0
  command -v sha256sum >/dev/null 2>&1 || return 0
  line="$(grep " $name\$" "$HERE/SHA256SUMS" || true)"
  [ -n "$line" ] || return 0
  say "校验 $name"
  (cd "$HERE" && printf '%s\n' "$line" | sha256sum -c --quiet -) \
    || die "$name 校验和对不上，文件不完整或损坏。重新拷一份离线包再试"
}

load_image() {
  [ -f "$IMAGE_TAR" ] || die "本地没有镜像 ${IMAGE}，包里也找不到 image.tar"
  verify_tar image.tar
  say "导入镜像：docker load -i image.tar（性能较弱的 ARM 单板机上可能要一两分钟）"
  docker load -i "$IMAGE_TAR"
  image_present || die "导入后仍找不到 ${IMAGE}，image.tar 可能不是这个版本的"
}

ensure_image() {
  if image_present; then
    say "镜像 $IMAGE 已在本地，跳过导入"
  else
    load_image
  fi
  local arch
  arch="$(docker image inspect -f '{{.Architecture}}' "$IMAGE")"
  [ "$arch" = "$(host_arch)" ] || die "镜像 $IMAGE 的架构是 ${arch}，本机是 $(uname -m)，跑不起来"
}

# ---------------------------------------------------------------------------
# 数据目录
# ---------------------------------------------------------------------------
prepare_data() {
  DATA_CREATED=""
  [ -d "$DATA_PATH" ] || DATA_CREATED=1
  mkdir -p "$DATA_PATH" 2>/dev/null \
    || die "无法创建数据目录 ${DATA_PATH}。权限不够的话用 sudo 运行，或者把 DATA_DIR 改到有写权限的位置"
  DATA_ABS="$(cd "$DATA_PATH" && pwd -P)"
  case "$DATA_ABS" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/opt|/proc|/root|/sbin|/srv|/sys|/usr|/var)
      die "DATA_DIR 不能是系统目录：$DATA_ABS" ;;
  esac
  [ "$DATA_ABS" != "$HOME" ] || die "DATA_DIR 不能是家目录本身：$DATA_ABS"
}

# 已经有同名容器时：它得是 AgentLab 的，数据目录也得和配置的是同一个。
# 升级解到新目录、DATA_DIR 还是相对路径，是最容易踩的坑：新容器会从一个空库开始，看着像数据丢了
check_existing() {
  container_exists "$CONTAINER_NAME" || return 0
  check_is_agentlab
  local old_src
  old_src="$(data_source_of "$CONTAINER_NAME")"
  [ -n "$old_src" ] && [ "$old_src" != "$DATA_ABS" ] || return 0
  # 刚才为这次部署新建的空目录收回去，别留一个误导人的空 data
  [ -z "$DATA_CREATED" ] || rmdir "$DATA_ABS" 2>/dev/null || true
  die "现有容器 ${CONTAINER_NAME} 用的数据目录是 ${old_src}，而 agentlab.env 配置的是 ${DATA_ABS}。
直接换过去，新容器会从一个空库开始（旧数据还在原处，不会丢）。
  - 要沿用原来的数据：把 agentlab.env 里的 DATA_DIR 改成 ${old_src} 再运行；
  - 确实要换新目录：先 ./deploy.sh uninstall，再 ./deploy.sh install。"
}

fix_data_owner() {
  # 容器里的服务以固定的非 root 用户运行，数据目录得归它，否则启动时写不进数据库
  local user uid gid owner
  user="$(docker image inspect -f '{{.Config.User}}' "$IMAGE")"
  uid="${user%%:*}"
  gid="${user#*:}"
  [ "$gid" != "$user" ] || gid="$uid"
  case "$uid$gid" in ''|*[!0-9]*) return 0 ;; esac
  # 只看一个目录自己的属主；stat 的参数 GNU 和 BSD 不一样，ls -nd 两边通用
  # shellcheck disable=SC2012
  owner="$(ls -nd "$DATA_ABS" | awk '{print $3}')"
  [ "$owner" = "$uid" ] && return 0
  # 改属主前确认这确实是一个新目录或者 AgentLab 的数据目录，免得 DATA_DIR 写错改了别的东西
  # 认这几样里的一样：agentlab.db（库在 SQLite）、.secret_key（自动生成的加密密钥）、.agentlab-data（服务启动时建的标记，
  # 库放在 PostgreSQL、密钥又由 AGENTLAB_SECRET_KEY 给时只有它）、.postgres/state（deploy.sh 记的用哪种库：内置库起来了、
  # 服务还没起过时只有它）。只有 uploads/ 这类普通目录名的不认，免得 chown 错了别人的目录
  if [ -n "$(ls -A "$DATA_ABS" 2>/dev/null)" ] && [ ! -e "$DATA_ABS/agentlab.db" ] \
     && [ ! -e "$DATA_ABS/.secret_key" ] && [ ! -e "$DATA_ABS/.agentlab-data" ] && [ ! -e "$DATA_ABS/.postgres/state" ]; then
    die "DATA_DIR 指向的 $DATA_ABS 不是空目录，也不像 AgentLab 的数据目录（里面没有 agentlab.db、.secret_key、.agentlab-data）。
为免改错别的文件的属主，请指向一个新目录，或者原来的 AgentLab 数据目录"
  fi
  say "把数据目录的属主改成容器里的用户（uid ${uid}），容器里的服务才写得进去"
  docker run --rm --user 0:0 --network none --entrypoint chown \
    -v "$DATA_ABS:/data" "$IMAGE" -R "$uid:$gid" /data \
    || die "无法修改 $DATA_ABS 的属主"
}

# ---------------------------------------------------------------------------
# microVM：本机有 KVM、镜像里带了运行时，就先用一个临时容器真起一台 VM，起得来才把 /dev/kvm
# 交给正式容器。只看设备在不在不够：系统调用过滤、嵌套虚拟化没开，都可能让 VM 起不来。
# 起不来还把设备交出去，服务端会选中 microVM，之后每次执行代码都失败
# ---------------------------------------------------------------------------
KVM_OPTS=()
MICROVM_OPTS=()

kvm_opts() {
  # 容器里的服务以 uid 10001 运行，要靠 /dev/kvm 所属的组才能读写它；其他用户本来就能读写的不用加组
  local perm gid
  perm="$(ls -lL /dev/kvm | cut -c1-10)"
  gid="$(ls -nL /dev/kvm | awk '{print $4}')"
  KVM_OPTS=(--device /dev/kvm)
  case "$perm" in
    ???????rw*) ;;
    *) KVM_OPTS+=(--group-add "$gid") ;;
  esac
}

microvm_selftest() {
  # 和正式容器一样的配置和权限，只是不挂数据目录、不开端口。只取最后一行：结论，或者报错的最后一句
  local extra=()
  [ "$ENABLE_BWRAP" = 1 ] && extra=("${BWRAP_OPTS[@]}")
  docker run --rm \
    --env-file "$ENV_FILE" \
    -e AGENTLAB_MICROVM_BOOT_TIMEOUT=120 \
    "${KVM_OPTS[@]}" ${extra[@]+"${extra[@]}"} \
    --entrypoint python "$IMAGE" -m app.sandbox.selftest 2>&1 | tail -n 1
}

decide_microvm() {
  MICROVM_OPTS=()
  [ "$ENABLE_MICROVM" != 0 ] || return 0
  local baked out
  baked="$(docker image inspect -f '{{index .Config.Labels "agentlab.microvm"}}' "$IMAGE" 2>/dev/null || true)"
  if [ "$baked" != 1 ]; then
    [ "$ENABLE_MICROVM" = auto ] || die "ENABLE_MICROVM=1，但这个包打的时候没有带 microVM 运行时（用了 --no-microvm）。
换一个默认方式打出来的包，或者把 ENABLE_MICROVM 改成 auto"
    return 0
  fi
  if [ ! -e /dev/kvm ]; then
    if [ "$ENABLE_MICROVM" = 1 ]; then
      die "ENABLE_MICROVM=1，但这台机器没有 /dev/kvm，启动不了 microVM。
  - 物理机：确认 CPU 支持虚拟化、BIOS 里已打开，并已加载 kvm 模块（ls -l /dev/kvm 能看到它）；
  - 云主机：要选支持嵌套虚拟化的机型并打开这项功能；
  - 不需要 microVM 的话，把 ENABLE_MICROVM 改成 auto 或 0。"
    fi
    say "这台机器没有 KVM（/dev/kvm），不启用 microVM"
    return 0
  fi
  kvm_opts
  say "检测到 /dev/kvm，试着启动一台 microVM（第一次要十几秒到一分钟）"
  if out="$(microvm_selftest)"; then
    MICROVM_OPTS=("${KVM_OPTS[@]}")
    say "$out"
    return 0
  fi
  [ "$ENABLE_MICROVM" = auto ] || die "ENABLE_MICROVM=1，但 microVM 启动不了：${out:-没有输出}
常见原因见 README 的常见问题。不需要 microVM 的话，把 ENABLE_MICROVM 改成 auto 或 0"
  warn "microVM 启动不了，这次不启用，代码仍按原来的方式执行。原因：${out:-没有输出}"
}

# 服务端实际选中的沙箱。镜像里没有 curl，用 python 从容器里问
sandbox_summary() {
  docker exec "$CONTAINER_NAME" python -c '
import json, urllib.request as u
r = u.build_opener(u.ProxyHandler({})).open("http://127.0.0.1:8000/api/sandbox/health", timeout=5)
d = json.load(r)
print(d.get("selected_because") or d.get("backend") or "未知")
' 2>/dev/null || echo "查不到"
}

# ---------------------------------------------------------------------------
# 容器
# ---------------------------------------------------------------------------
container_exists() { docker container inspect "$1" >/dev/null 2>&1; }

container_state() { docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo missing; }

data_source_of() {
  docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$1" 2>/dev/null || true
}

require_container() {
  container_exists "$CONTAINER_NAME" || die "没有名为 $CONTAINER_NAME 的容器。还没部署的话运行 ./deploy.sh install"
}

check_is_agentlab() {
  local image
  image="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER_NAME")"
  case "$image" in
    agentlab:*) ;;
    *) die "已经有一个叫 $CONTAINER_NAME 的容器，但它用的镜像是 ${image}，不是 AgentLab。改一下 agentlab.env 里的 CONTAINER_NAME" ;;
  esac
}

port_in_use() {
  # 查不了（没有 ss）就当没占，交给 docker run 报错
  command -v ss >/dev/null 2>&1 || return 1
  ss -ltnH "sport = :$1" 2>/dev/null | grep -q .
}

container_publishes() {
  docker port "$1" 8000/tcp 2>/dev/null | grep -q ":$2\$"
}

run_container() {
  local extra=()
  [ "$ENABLE_BWRAP" = 1 ] && extra=("${BWRAP_OPTS[@]}")
  docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --env-file "$ENV_FILE" \
    -p "$BIND_ADDR:$HOST_PORT:8000" \
    -v "$DATA_ABS:/data" \
    --log-opt max-size=10m --log-opt max-file=3 \
    ${extra[@]+"${extra[@]}"} \
    ${MICROVM_OPTS[@]+"${MICROVM_OPTS[@]}"} \
    ${DB_OPTS[@]+"${DB_OPTS[@]}"} \
    "$IMAGE" >/dev/null
}

wait_healthy() {
  local name="$1" state health i
  say "等 $name 通过健康检查"
  for i in $(seq 1 90); do
    state="$(container_state "$name")"
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$name" 2>/dev/null || true)"
    case "$state" in
      exited|dead|missing) return 1 ;;
      restarting) [ "$i" -gt 3 ] && return 1 ;;
    esac
    case "$health" in
      healthy) return 0 ;;
      unhealthy) return 1 ;;
    esac
    sleep 2
  done
  return 1
}

print_access() {
  echo
  case "$BIND_ADDR" in
    0.0.0.0)
      local ip
      ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
      echo "  本机    http://127.0.0.1:$HOST_PORT"
      echo "  局域网  http://${ip:-<本机 IP>}:$HOST_PORT"
      echo
      echo "  注意：AgentLab 没有登录，同一网络里打得开页面的人都能用它（跑工作流、花模型额度、"
      echo "  读数据源、执行代码）。只在信得过的网络里开，并用防火墙限制来源。"
      ;;
    127.0.0.1|localhost) echo "  http://127.0.0.1:$HOST_PORT   （只有本机能打开）" ;;
    *) echo "  http://$BIND_ADDR:$HOST_PORT" ;;
  esac
  echo
}

show_failure() {
  echo >&2
  echo "---- 容器最后 60 行日志 ----" >&2
  docker logs --tail 60 "$CONTAINER_NAME" >&2 2>&1 || true
  echo "----" >&2
}

# 起一个新容器替换掉同名的旧容器（没有旧的就直接起）。数据目录不动。
# 旧容器先改名留着：新容器起不来就换回去；起来了但健康检查没过，告诉用户怎么回退
deploy_container() {
  local prev="$CONTAINER_NAME-previous" had_old=""
  container_exists "$CONTAINER_NAME" && had_old=1

  if port_in_use "$HOST_PORT"; then
    if [ -z "$had_old" ] || ! container_publishes "$CONTAINER_NAME" "$HOST_PORT"; then
      die "端口 $HOST_PORT 已被别的程序占用。改 agentlab.env 里的 HOST_PORT，或停掉占用方（sudo ss -ltnp 'sport = :$HOST_PORT' 可以看是谁）"
    fi
  fi

  if [ -n "$had_old" ]; then
    docker rm -f "$prev" >/dev/null 2>&1 || true
    say "停止现有容器 ${CONTAINER_NAME}（数据保留）"
    docker stop "$CONTAINER_NAME" >/dev/null
    docker rename "$CONTAINER_NAME" "$prev"
  fi

  say "启动容器 ${CONTAINER_NAME}（${IMAGE}）"
  if ! run_container; then
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    if [ -n "$had_old" ]; then
      docker rename "$prev" "$CONTAINER_NAME" && docker start "$CONTAINER_NAME" >/dev/null \
        && warn "新容器没能启动，已换回原来的容器"
    fi
    die "容器启动失败，见上面 docker 的报错（端口被占、数据目录权限不对最常见）"
  fi

  if wait_healthy "$CONTAINER_NAME"; then
    [ -z "$had_old" ] || docker rm "$prev" >/dev/null 2>&1 || true
    say "AgentLab $VERSION 已就绪，数据目录 $DATA_ABS"
    say "代码沙箱：$(sandbox_summary)"
    print_access
    return 0
  fi

  show_failure
  if [ -n "$had_old" ]; then
    echo "原来的容器还留着，名字改成了 ${prev}。要回退：" >&2
    echo "  docker rm -f $CONTAINER_NAME && docker rename $prev $CONTAINER_NAME && docker start $CONTAINER_NAME" >&2
  fi
  die "容器没通过健康检查。看上面的日志；常见原因见 README 的常见问题"
}


# ---------------------------------------------------------------------------
# 用的哪种库：sqlite（缺省）、builtin（内置 PostgreSQL，数据目录里记着）、external（agentlab.env 里设了
# AGENTLAB_DATABASE_URL）。内置和外接互斥
# ---------------------------------------------------------------------------
PG_STATE_REL=".postgres/state"
# 应用容器里连内置库：不带密码，密码在数据目录的 pgpass 里（PGPASSFILE），不进容器的环境变量、不进日志
PG_PASSFILE="/data/.postgres/pgpass"
MODE="sqlite"
DB_OPTS=()

# 数据目录里记的状态：sqlite、builtin，或者 migrating（migrate-to-postgres 开始往内置库里导了、还没切成，用的仍是
# SQLite）。.postgres/ 是 0711、state 是 0644，一般宿主机上直接读得到；读不到（权限）就交给容器里的 app.ops 读
stored_state() {
  local file="$DATA_ABS/$PG_STATE_REL" dir="$DATA_ABS/.postgres" line
  if [ -r "$file" ]; then
    line="$(grep '^mode=' "$file" 2>/dev/null | head -n 1 || true)"
    case "${line#mode=}" in builtin|migrating) echo "${line#mode=}" ;; *) echo sqlite ;; esac
  elif [ -e "$file" ] || { [ -e "$dir" ] && [ ! -x "$dir" ]; } || [ ! -x "$DATA_ABS" ]; then
    ops state | tail -n 1
  else
    echo sqlite
  fi
}

# 实际用的：只有 builtin 算内置，migrating 还是 SQLite
stored_mode() {
  case "$(stored_state)" in builtin) echo builtin ;; *) echo sqlite ;; esac
}

volume_exists() { docker volume inspect "$1" >/dev/null 2>&1; }

resolve_mode() {
  local stored
  stored="$(stored_mode)"
  if [ -n "$EXTERNAL_DB" ]; then
    if [ "$stored" = builtin ] || [ -n "$WITH_POSTGRES" ]; then
      die "两种库只能选一种：agentlab.env 里设了 ${EXTERNAL_DB}（外接 PostgreSQL），这个数据目录又是内置 PostgreSQL$( \
        [ -n "$WITH_POSTGRES" ] && [ "$stored" != builtin ] && printf '（--postgres）')。
  - 用内置的：把 agentlab.env 里的 $EXTERNAL_DB 那一行删掉；
  - 用外接的：去掉 --postgres；数据目录已经是内置 PostgreSQL 的，先用 pg_dump 把内置库的数据导出来，换一个新的数据目录再部署"
    fi
    MODE="external"
  elif [ "$stored" = builtin ]; then
    MODE="builtin"
  elif [ -n "$WITH_POSTGRES" ]; then
    if [ -e "$DATA_ABS/agentlab.db" ]; then
      die "数据目录 $DATA_ABS 里已经有 SQLite 的库（agentlab.db）。install --postgres 只给新部署用；要把现有数据搬进内置
PostgreSQL，运行 ./deploy.sh migrate-to-postgres"
    fi
    MODE="builtin"
  else
    MODE="sqlite"
  fi
  check_db_settings
  DB_OPTS=()
  if [ "$MODE" = builtin ]; then
    DB_OPTS=(--network "$DOCKER_NETWORK"
      -e "AGENTLAB_DATABASE_URL=postgresql+asyncpg://$PG_USER@$POSTGRES_CONTAINER:5432/$PG_DB"
      -e "PGPASSFILE=$PG_PASSFILE")
  fi
}

# agentlab.env 里和库有关的几项是否和用的哪种库对得上（resolve_mode 定了 MODE 之后查；migrate-to-postgres 按切过去的内置库查）
pgbouncer_on() {
  case "$(printf '%s' "$PGBOUNCER_VALUE" | tr '[:upper:]' '[:lower:]')" in 1|true|yes|on) return 0 ;; *) return 1 ;; esac
}

check_db_settings() {
  local mode="${1:-$MODE}"
  if [ -n "$DIRECT_URL_SET" ] && [ -z "$EXTERNAL_URL_SET" ]; then
    die "agentlab.env 里设了 AGENTLAB_DATABASE_DIRECT_URL，却没设 AGENTLAB_DATABASE_URL：直连地址是给外接 PostgreSQL 配 PgBouncer 用的，
要和 AGENTLAB_DATABASE_URL 一起设。不用外接的库就把它删掉"
  fi
  if [ "$mode" = builtin ] && pgbouncer_on; then
    die "agentlab.env 里 AGENTLAB_DB_PGBOUNCER=${PGBOUNCER_VALUE}，但内置 PostgreSQL 是直连、中间没有 PgBouncer。把这一行删掉或改成 0"
  fi
  case "$WORKERS_VALUE" in
    ''|*[!0-9]*) ;;
    *)
      if [ "$mode" = sqlite ] && [ "$WORKERS_VALUE" -gt 1 ]; then
        die "agentlab.env 里 AGENTLAB_WORKERS=${WORKERS_VALUE}，但库是 SQLite：SQLite 只支持单进程，服务会拒绝启动。
  - 改回 1（或删掉这一行）；
  - 要多进程，先换成 PostgreSQL：./deploy.sh migrate-to-postgres（内置）或设 AGENTLAB_DATABASE_URL（外接）"
      fi ;;
  esac
}

mode_label() {
  case "$MODE" in
    builtin) echo "内置 PostgreSQL（容器 ${POSTGRES_CONTAINER}，数据卷 ${POSTGRES_VOLUME}）" ;;
    external) echo "外接 PostgreSQL（agentlab.env 里的 ${EXTERNAL_DB}）" ;;
    *) echo "SQLite（数据目录里的 agentlab.db）" ;;
  esac
}

# 在一个用完就删的容器里、以容器里的用户跑 python -m app.ops（数据目录归它，宿主机上的人不一定写得进去）。
# start / stop 这类用容器正在用的镜像（这个包的镜像可能已经删了）
OPS_IMAGE=""
ops() {
  docker run --rm --network none -v "$DATA_ABS:/data" --entrypoint python "${OPS_IMAGE:-$IMAGE}" -m app.ops "$@"
}

# ---------------------------------------------------------------------------
# 内置 PostgreSQL：镜像、网络、密码、容器、备份
# ---------------------------------------------------------------------------
pg_image_present() { docker image inspect "$POSTGRES_IMAGE" >/dev/null 2>&1; }

ensure_pg_image() {
  if pg_image_present; then
    return 0
  fi
  [ -f "$POSTGRES_IMAGE_TAR" ] || die "本地没有 PostgreSQL 的镜像 ${POSTGRES_IMAGE}，包里也没有 postgres-image.tar。
  - 能联网：docker pull ${POSTGRES_IMAGE}
  - 离线：用 scripts/package-docker.sh 重新打包（缺省会带上它），或者在能联网的机器上 docker save 一份拷过来 docker load"
  verify_tar postgres-image.tar
  say "导入 PostgreSQL 镜像：docker load -i postgres-image.tar"
  docker load -i "$POSTGRES_IMAGE_TAR"
  pg_image_present || die "导入后仍找不到 ${POSTGRES_IMAGE}：postgres-image.tar 里的不是这个标签。agentlab.env 里的 POSTGRES_IMAGE 要和包里的一致"
}

ensure_network() {
  docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1 && return 0
  say "创建 docker 网络 ${DOCKER_NETWORK}（PostgreSQL 只在这个网络里可见，端口不对外）"
  docker network create "$DOCKER_NETWORK" >/dev/null
}

pg_secrets() {
  # 第一次生成密码（0600），之后沿用；pgpass 按容器名重写。密码只在数据目录里，不打印
  ops postgres-secrets --host "$POSTGRES_CONTAINER" --port 5432 --db "$PG_DB" --user "$PG_USER" >/dev/null \
    || die "没能在数据目录里生成 PostgreSQL 的密码文件（$DATA_ABS/.postgres/）"
}

check_pg_is_ours() {
  local role
  role="$(docker inspect -f '{{index .Config.Labels "agentlab.role"}}' "$POSTGRES_CONTAINER" 2>/dev/null || true)"
  [ "$role" = postgres ] || die "已经有一个叫 $POSTGRES_CONTAINER 的容器，但它不是 deploy.sh 起的内置 PostgreSQL。
改一下 agentlab.env 里的 POSTGRES_CONTAINER（缺省是 <CONTAINER_NAME>-postgres）"
}

run_pg_container() {
  docker run -d \
    --name "$POSTGRES_CONTAINER" \
    --label agentlab.role=postgres \
    --restart unless-stopped \
    --network "$DOCKER_NETWORK" \
    -e "POSTGRES_USER=$PG_USER" \
    -e "POSTGRES_DB=$PG_DB" \
    -e POSTGRES_PASSWORD_FILE=/run/agentlab/password \
    -v "$POSTGRES_VOLUME:/var/lib/postgresql/data" \
    -v "$DATA_ABS/.postgres/password:/run/agentlab/password:ro" \
    --health-cmd "pg_isready -h 127.0.0.1 -U $PG_USER -d $PG_DB" \
    --health-interval 5s --health-timeout 5s --health-retries 5 --health-start-period 60s \
    --stop-timeout 60 \
    --log-opt max-size=10m --log-opt max-file=3 \
    "$POSTGRES_IMAGE" >/dev/null
}

pg_logs_to_stderr() {
  echo >&2; echo "---- PostgreSQL 最后 40 行日志 ----" >&2
  docker logs --tail 40 "$POSTGRES_CONTAINER" >&2 2>&1 || true
  echo "----" >&2
}

# install / upgrade 时 POSTGRES_IMAGE 和现有容器的不一样（新包换了小版本，或者 agentlab.env 里改了）：服务先停，库的容器
# 按新镜像重建，数据卷不动。新镜像起不来（多半是跨了大版本，数据格式不认）就换回原来的镜像，什么都不改
PG_APPLY_IMAGE=""

replace_pg_image() {
  local cur="$1" was_running=""
  [ "$(container_state "$CONTAINER_NAME")" = running ] && was_running=1
  say "内置 PostgreSQL 换镜像：$cur → ${POSTGRES_IMAGE}（数据卷 $POSTGRES_VOLUME 不动）"
  stop_app
  docker stop "$POSTGRES_CONTAINER" >/dev/null 2>&1 || true
  docker rm -f "$POSTGRES_CONTAINER" >/dev/null
  if run_pg_container && wait_healthy "$POSTGRES_CONTAINER"; then
    return 0
  fi
  pg_logs_to_stderr
  docker rm -f "$POSTGRES_CONTAINER" >/dev/null 2>&1 || true
  local wanted="$POSTGRES_IMAGE"
  POSTGRES_IMAGE="$cur"
  if run_pg_container && wait_healthy "$POSTGRES_CONTAINER"; then
    [ -z "$was_running" ] || docker start "$CONTAINER_NAME" >/dev/null 2>&1 || true
    die "内置 PostgreSQL 用 $wanted 起不来（见上面的日志；跨大版本时数据格式不通用，要 pg_dump 导出、新版本里导回），
已换回原来的 ${cur}，什么都没改。agentlab.env 里的 POSTGRES_IMAGE 改回 $cur 再运行"
  fi
  die "内置 PostgreSQL 用 $wanted 起不来，换回 $cur 也没起来。看 ./deploy.sh logs --postgres"
}

# 起内置库（没有就建，停着就起），等 pg_isready 通过
start_pg() {
  ensure_network
  if container_exists "$POSTGRES_CONTAINER"; then
    check_pg_is_ours
    local cur
    cur="$(docker inspect -f '{{.Config.Image}}' "$POSTGRES_CONTAINER")"
    if [ -n "$PG_APPLY_IMAGE" ] && [ "$cur" != "$POSTGRES_IMAGE" ]; then
      replace_pg_image "$cur"
      return 0
    fi
    if [ "$(container_state "$POSTGRES_CONTAINER")" != running ]; then
      say "启动内置 PostgreSQL（${POSTGRES_CONTAINER}）"
      docker start "$POSTGRES_CONTAINER" >/dev/null || die "内置 PostgreSQL 的容器起不来，见上面 docker 的报错"
    fi
  else
    say "启动内置 PostgreSQL（${POSTGRES_CONTAINER}，${POSTGRES_IMAGE}，数据卷 ${POSTGRES_VOLUME}）"
    run_pg_container || die "PostgreSQL 容器启动失败，见上面 docker 的报错"
  fi
  if ! wait_healthy "$POSTGRES_CONTAINER"; then
    pg_logs_to_stderr
    die "内置 PostgreSQL 没就绪（pg_isready 不通过）。看上面的日志"
  fi
}

stop_pg() {
  container_exists "$POSTGRES_CONTAINER" || return 0
  [ "$(container_state "$POSTGRES_CONTAINER")" = running ] || return 0
  say "停止内置 PostgreSQL（${POSTGRES_CONTAINER}，数据保留）"
  docker stop "$POSTGRES_CONTAINER" >/dev/null
}

stop_app() {
  container_exists "$CONTAINER_NAME" || return 0
  [ "$(container_state "$CONTAINER_NAME")" = running ] || return 0
  say "停止 ${CONTAINER_NAME}（数据保留）"
  docker stop "$CONTAINER_NAME" >/dev/null
}

image_user() { docker image inspect -f '{{.Config.User}}' "$IMAGE"; }

# pg_dump 一份到数据目录的 backups/postgres/，只留最近 BACKUP_KEEP 份。用 PostgreSQL 镜像里的 pg_dump（和库同版本），
# 以数据目录的属主写文件。备份里是整个库：目录 0700（app.ops backup-dir）、文件 0600（umask 077 下建，没有别人读得到的那一刻）
backup_pg() {
  local stamp file
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  file="backups/postgres/$PG_DB-$stamp.dump"
  say "备份内置 PostgreSQL：pg_dump 到 $DATA_ABS/$file"
  ops backup-dir postgres >/dev/null || { warn "没能在数据目录里建备份目录 backups/postgres/"; return 1; }
  docker run --rm --network "$DOCKER_NETWORK" -v "$DATA_ABS:/data" --user "$(image_user)" -e "PGPASSFILE=$PG_PASSFILE" \
    --entrypoint sh "$POSTGRES_IMAGE" -c 'umask 077 && exec pg_dump "$@"' pg_dump \
    -h "$POSTGRES_CONTAINER" -U "$PG_USER" -d "$PG_DB" -Fc -f "/data/$file" \
    || { warn "pg_dump 失败"; return 1; }
  ops prune --dir /data/backups/postgres --keep "$BACKUP_KEEP" >/dev/null || warn "旧备份没删掉，不影响这次升级"
}

backup_sqlite() {
  local out
  say "备份 SQLite 库到 $DATA_ABS/backups/sqlite/"
  out="$(ops backup-sqlite --keep "$BACKUP_KEEP")" || { warn "SQLite 库备份失败"; return 1; }
  # 容器里看到的是 /data/…，换成宿主机上的路径
  case "$out" in
    /data/*) say "备份好了：${out/#\/data/$DATA_ABS}" ;;
    *) say "$out" ;;
  esac
}

backup_before_upgrade() {
  case "$MODE" in
    builtin) backup_pg ;;
    sqlite) backup_sqlite ;;
    external) say "外接 PostgreSQL：升级前请自己备份那个库（pg_dump），这里不替你做" ;;
  esac
}

# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
cmd_install() {
  check_docker
  check_arch
  read_env
  ensure_image
  prepare_data
  check_existing
  fix_data_owner
  resolve_mode
  say "应用库：$(mode_label)"
  PG_APPLY_IMAGE=1
  if [ "$MODE" = builtin ]; then
    ensure_pg_image
    if [ "$(stored_mode)" != builtin ]; then
      # 新部署：数据卷要是已经有了，里面的库是按别的密码初始化的（多半是以前 uninstall 留下的），连不上，也不该拿来用
      volume_exists "$POSTGRES_VOLUME" && die "docker 数据卷 $POSTGRES_VOLUME 已经存在，不是这个数据目录建的（多半是以前卸载时留下的
内置 PostgreSQL 的数据）。
  - 要接着用那份数据：把 DATA_DIR 指回原来的数据目录再 ./deploy.sh install；
  - 确定不要了：docker volume rm ${POSTGRES_VOLUME}（不可恢复），再运行；
  - 两份都留着：agentlab.env 里另起一个 POSTGRES_VOLUME 的名字"
      pg_secrets
      # 先记下再起库：新数据目录里没有 SQLite 的库，没什么可「切」的；起库失败了重来也认得出数据卷是自己的
      ops state --set builtin >/dev/null || die "没能在数据目录里记下「用内置 PostgreSQL」"
    else
      pg_secrets
    fi
    start_pg
  fi
  decide_microvm
  deploy_container
}

cmd_upgrade() {
  check_docker
  check_arch
  read_env
  container_exists "$CONTAINER_NAME" || die "没有名为 $CONTAINER_NAME 的容器，这台机器还没部署过。运行 ./deploy.sh install"
  check_is_agentlab
  local old_image
  old_image="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER_NAME")"
  say "当前版本 ${old_image#agentlab:}，升级到 $VERSION"
  # 总是从包里导入：同名标签在本机可能指向别的构建
  load_image
  local arch
  arch="$(docker image inspect -f '{{.Architecture}}' "$IMAGE")"
  [ "$arch" = "$(host_arch)" ] || die "镜像 $IMAGE 的架构是 ${arch}，本机是 $(uname -m)，跑不起来"
  prepare_data
  check_existing
  fix_data_owner
  resolve_mode
  say "应用库：$(mode_label)"
  PG_APPLY_IMAGE=1
  if [ "$MODE" = builtin ]; then
    ensure_pg_image
    pg_secrets
    start_pg
  fi
  decide_microvm
  # 先停旧服务再备份：备份的是升级前最后的样子。备份失败就把旧服务起回来、不升级
  stop_app
  if ! backup_before_upgrade; then
    docker start "$CONTAINER_NAME" >/dev/null 2>&1 || true
    die "升级前的备份没做成，没有升级，原来的服务已经起回来了"
  fi
  # 新版本启动时自动补表结构（迁移）和默认数据，几个工作进程同时起也只做一次
  deploy_container
  if [ "$old_image" != "$IMAGE" ] && docker image inspect "$old_image" >/dev/null 2>&1; then
    echo "旧镜像 $old_image 还留着，方便回退。确认新版本没问题后可以删：docker rmi $old_image"
  fi
}

# 把正在用 SQLite 的部署切到内置 PostgreSQL。记下「已切换」之前任何一步出错：服务按 SQLite 原样恢复（原来在跑就起回来）、
# 内置库的容器停掉，数据目录里只多了 backups/ 里的一份备份和 .postgres/ 里的密码、状态（migrating）。重复执行：已经切过的
# 什么都不做；上次没切成留在内置库里的半份数据先清空再导，不会和 SQLite 里后来改过的混在一起
MIGRATE_WAS_RUNNING=""
MIGRATE_HAD_APP=""
MIGRATE_DEPLOYING=""

migrate_rollback() {
  local prev="$CONTAINER_NAME-previous"
  echo >&2
  warn "$1"
  if [ -n "$MIGRATE_DEPLOYING" ]; then
    # 按内置库起的新容器不能留着（它带着 --restart，会一直去连内置库）：换回原来的，原来就没有就删掉
    if container_exists "$prev"; then
      docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
      docker rename "$prev" "$CONTAINER_NAME" >/dev/null 2>&1 || warn "没能把原来的容器改回 ${CONTAINER_NAME}：docker rename $prev $CONTAINER_NAME"
    elif [ -z "$MIGRATE_HAD_APP" ]; then
      docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi
    # 已经记成 builtin 的改回 migrating：仍用 SQLite，数据卷还认得出是这个数据目录的
    ops state --set migrating >/dev/null 2>&1 || warn "没能把数据目录的状态改回去：手动把 $DATA_ABS/$PG_STATE_REL 里的 mode=builtin 改成 mode=migrating"
  fi
  if container_exists "$CONTAINER_NAME"; then
    if [ -n "$MIGRATE_WAS_RUNNING" ]; then
      docker start "$CONTAINER_NAME" >/dev/null 2>&1 && say "服务已按 SQLite 起回来" || warn "服务没能起回来：./deploy.sh start"
    else
      docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi
  fi
  stop_pg
  die "没有切换，仍用 SQLite（数据目录里的 agentlab.db 没动过）。照上面的报错处理好之后重新运行 ./deploy.sh migrate-to-postgres，
重复执行是安全的：内置库里这次导了一半的会先清空再导"
}

copy_to_pg() {
  # --assume-stopped：服务是这里刚停的（强杀留下的 -wal 也不挡）；快照放数据目录里（和库同一块盘，空间够）。
  # 内置库是直连（没有 PgBouncer），断点跟着目标地址走，不用 --direct-url。
  # 逐表的结果里来源是空表的几十行不列（没什么可核对的），别的原样打出来；退出码照 copy-to-postgres 的（pipefail）
  docker run --rm --network "$DOCKER_NETWORK" -v "$DATA_ABS:/data" -e "PGPASSFILE=$PG_PASSFILE" \
    --entrypoint python "$IMAGE" -m app.db copy-to-postgres /data \
    "postgresql+asyncpg://$PG_USER@$POSTGRES_CONTAINER:5432/$PG_DB" --assume-stopped --work-dir /data "$@" \
    | grep -v '  空表$'
}

cmd_migrate_to_postgres() {
  check_docker
  check_arch
  read_env
  ensure_image
  prepare_data
  check_existing
  [ -z "$EXTERNAL_DB" ] || die "agentlab.env 里设了 ${EXTERNAL_DB}（外接 PostgreSQL）。内置和外接只能选一种：要切到内置的，先删掉那一行；
要搬进外接的库，用镜像里的 python -m app.db copy-to-postgres（见 README「用 PostgreSQL」）"
  # 切过去就是内置库：和它对不上的设置（PgBouncer 开关）先拦下，别停了服务才发现
  check_db_settings builtin
  local state
  state="$(stored_state)"
  if [ "$state" = builtin ]; then
    say "这个数据目录已经切到内置 PostgreSQL 了，什么都不用做（./deploy.sh status 看状态）"
    return 0
  fi
  [ -e "$DATA_ABS/agentlab.db" ] || die "数据目录 $DATA_ABS 里没有 SQLite 的库（agentlab.db），没有可搬的。新部署直接用 ./deploy.sh install --postgres"
  # 数据卷已经有了、又不是这个数据目录上次没切成留下的：里面可能是别人的数据，下面要清空它，不能碰
  if [ "$state" != migrating ] && volume_exists "$POSTGRES_VOLUME"; then
    die "docker 数据卷 $POSTGRES_VOLUME 已经存在，不是这个数据目录上次没切成留下的（多半是以前卸载时留下的内置 PostgreSQL
的数据）。切换要清空内置库，为免删错数据，没有动它。
  - 确定不要了：docker volume rm ${POSTGRES_VOLUME}（不可恢复），再运行；
  - 要留着：agentlab.env 里另起一个 POSTGRES_VOLUME 的名字"
  fi
  if container_exists "$POSTGRES_CONTAINER"; then check_pg_is_ours; fi
  fix_data_owner
  ensure_pg_image
  PG_APPLY_IMAGE=1
  container_exists "$CONTAINER_NAME" && MIGRATE_HAD_APP=1
  [ "$(container_state "$CONTAINER_NAME")" = running ] && MIGRATE_WAS_RUNNING=1

  say "1/6 停服务（导入要在服务停着时做）"
  stop_app
  say "2/6 备份 SQLite 库"
  backup_sqlite || migrate_rollback "SQLite 库备份失败"
  say "3/6 起内置 PostgreSQL"
  ( pg_secrets ) || migrate_rollback "没能生成内置 PostgreSQL 的密码"
  ops state --set migrating >/dev/null || migrate_rollback "没能在数据目录里记下「开始切换」"
  ( start_pg ) || migrate_rollback "内置 PostgreSQL 没起来"
  # 内置库里要是有东西，只能是上次没切成留下的半份（上面查过数据卷），清空重来
  docker exec "$POSTGRES_CONTAINER" psql -q -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS \"$PG_DB\" WITH (FORCE)" -c "CREATE DATABASE \"$PG_DB\" OWNER \"$PG_USER\"" >/dev/null \
    || migrate_rollback "没能清空内置库里上次留下的数据"
  say "4/6 先核对（只数行数，不写）"
  copy_to_pg --dry-run || migrate_rollback "核对没过"
  say "5/6 导入并逐表核对"
  copy_to_pg || migrate_rollback "导入或核对没过"
  MIGRATE_DEPLOYING=1
  ops state --set builtin >/dev/null || migrate_rollback "没能在数据目录里记下「已切到内置 PostgreSQL」"
  say "6/6 按内置 PostgreSQL 起服务"
  resolve_mode
  decide_microvm
  ( deploy_container ) || migrate_rollback "按内置 PostgreSQL 起的服务没通过健康检查"
  echo
  say "已切到内置 PostgreSQL。SQLite 的库文件（agentlab.db、checkpoints.db）原样留在 $DATA_ABS 里、不再使用，"
  echo "    另有一份备份在 backups/sqlite/。用一段时间确认没问题后再删（见 README「用 PostgreSQL」）"
}

# start / stop / restart / status / logs / uninstall：数据目录看容器实际挂的，没有容器再看配置
# 这几个只管容器，不改配置：模式照数据目录里记的认，agentlab.env 和它冲突（内置的又设了外接地址）也不在这里拦，留给 install
locate_data() {
  DATA_ABS=""
  if container_exists "$CONTAINER_NAME"; then
    DATA_ABS="$(data_source_of "$CONTAINER_NAME")"
    OPS_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER_NAME")"
  fi
  if [ -z "$DATA_ABS" ] && [ -d "$DATA_PATH" ]; then DATA_ABS="$(cd "$DATA_PATH" && pwd -P)"; fi
  MODE="sqlite"
  if [ -n "$DATA_ABS" ] && [ "$(stored_mode)" = builtin ]; then
    MODE="builtin"
  elif [ -n "$EXTERNAL_DB" ]; then
    MODE="external"
  fi
}

wait_app() {
  if wait_healthy "$CONTAINER_NAME"; then
    say "$1"; print_access
  else
    show_failure; die "容器没通过健康检查"
  fi
}

cmd_start() {
  check_docker; read_env; require_container; locate_data
  [ "$MODE" != builtin ] || start_pg
  docker start "$CONTAINER_NAME" >/dev/null
  wait_app "已启动"
}

cmd_stop() {
  check_docker; read_env; require_container; locate_data
  # 先停服务再停库：服务收尾时还要写库
  say "停止 ${CONTAINER_NAME}（数据保留）"
  docker stop "$CONTAINER_NAME" >/dev/null
  [ "$MODE" != builtin ] || stop_pg
  say "已停止。开机也不会自动起来，要用时运行 ./deploy.sh start"
}

# 缺省只重启服务：库不用跟着断一次（库停着的话先把它起来，服务才连得上）。--postgres 连库一起重启
cmd_restart() {
  check_docker; read_env; require_container; locate_data
  if [ -n "$RESTART_POSTGRES" ]; then
    [ "$MODE" = builtin ] || die "这个部署没用内置 PostgreSQL，--postgres 没有可重启的库。只重启服务：./deploy.sh restart"
    say "重启 $CONTAINER_NAME 和内置 PostgreSQL（${POSTGRES_CONTAINER}）"
    stop_app
    stop_pg
    start_pg
    docker start "$CONTAINER_NAME" >/dev/null
  else
    say "重启 $CONTAINER_NAME"
    if [ "$MODE" = builtin ] && [ "$(container_state "$POSTGRES_CONTAINER")" != running ]; then
      start_pg
    fi
    docker restart "$CONTAINER_NAME" >/dev/null
  fi
  wait_app "已重启"
}

cmd_status() {
  check_docker; read_env
  if ! container_exists "$CONTAINER_NAME"; then
    echo "没有名为 $CONTAINER_NAME 的容器（还没部署，或已卸载）。部署：./deploy.sh install"
    exit 1
  fi
  locate_data
  local state health image src
  state="$(container_state "$CONTAINER_NAME")"
  health=""
  # 停着的容器，健康检查结果是停之前的，不显示免得误导
  [ "$state" != running ] || health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$CONTAINER_NAME")"
  image="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER_NAME")"
  src="$(data_source_of "$CONTAINER_NAME")"
  echo "容器      $CONTAINER_NAME"
  echo "状态      $state${health:+（健康检查：${health}）}"
  echo "镜像      $image"
  echo "端口      $(docker port "$CONTAINER_NAME" 8000/tcp 2>/dev/null | tr '\n' ' ')"
  echo "数据目录  ${src:-（未挂载）}"
  echo "应用库    $(mode_label)"
  if [ -n "$DATA_ABS" ] && [ "$MODE" = sqlite ] && [ "$(stored_state)" = migrating ]; then
    echo "          上次 migrate-to-postgres 没切成（仍用 SQLite），数据卷 $POSTGRES_VOLUME 里留着导了一半的；重新运行会清空重来"
  fi
  if [ "$MODE" = builtin ]; then
    local pstate phealth pimage
    pstate="$(container_state "$POSTGRES_CONTAINER")"
    phealth=""
    [ "$pstate" != running ] || phealth="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$POSTGRES_CONTAINER")"
    pimage="$(docker inspect -f '{{.Config.Image}}' "$POSTGRES_CONTAINER" 2>/dev/null || echo "$POSTGRES_IMAGE")"
    echo "内置库    $POSTGRES_CONTAINER ${pstate}${phealth:+（pg_isready：${phealth}）}，镜像 ${pimage}，网络 ${DOCKER_NETWORK}（端口不对外）"
    echo "库的备份  ${src:-$DATA_ABS}/backups/postgres/（upgrade 前自动 pg_dump，留最近 $BACKUP_KEEP 份）"
  fi
  [ "$state" != running ] || echo "代码沙箱  $(sandbox_summary)"
  echo "这个包    $IMAGE"
  if [ "$state" = running ]; then print_access; fi
}

cmd_logs() {
  check_docker; read_env
  local target="$CONTAINER_NAME"
  if [ -n "$LOGS_POSTGRES" ]; then
    target="$POSTGRES_CONTAINER"
    container_exists "$target" || die "没有名为 $target 的容器：这个部署没用内置 PostgreSQL"
  else
    require_container
  fi
  local args=(--tail "$TAIL")
  [ -z "$FOLLOW" ] || args+=(--follow)
  # exec：Ctrl-C 直接交给 docker logs，不留一个还在跟随的子进程
  exec docker logs "${args[@]}" "$target"
}

cmd_uninstall() {
  check_docker; read_env
  local src="" name pg=""
  if container_exists "$CONTAINER_NAME"; then
    check_is_agentlab
    src="$(data_source_of "$CONTAINER_NAME")"
  fi
  for name in "$CONTAINER_NAME" "$CONTAINER_NAME-previous"; do
    if container_exists "$name"; then
      say "删除容器 $name"
      docker rm -f "$name" >/dev/null
    fi
  done
  if container_exists "$POSTGRES_CONTAINER"; then
    check_pg_is_ours
    say "删除内置 PostgreSQL 的容器 ${POSTGRES_CONTAINER}（数据卷 $POSTGRES_VOLUME 留着）"
    docker rm -f "$POSTGRES_CONTAINER" >/dev/null
    pg=1
  fi
  if docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1; then
    docker network rm "$DOCKER_NETWORK" >/dev/null 2>&1 && say "删除 docker 网络 $DOCKER_NETWORK" \
      || warn "docker 网络 $DOCKER_NETWORK 没删掉（可能还有别的容器连着）"
  fi
  if [ -n "$PURGE_IMAGE" ]; then
    if image_present; then
      say "删除镜像 $IMAGE"
      docker rmi "$IMAGE" >/dev/null || warn "镜像 $IMAGE 没删掉（可能还有别的容器在用）"
    fi
    # PostgreSQL 的镜像不删：本机别的东西可能也在用同一个镜像
  else
    echo "镜像 $IMAGE 保留着，重新部署不用再导入。要删：./deploy.sh uninstall --purge-image"
  fi
  [ -n "$src" ] || src="$DATA_PATH"
  echo
  echo "数据没有删，还在：$src"
  if [ -n "$pg" ] || docker volume inspect "$POSTGRES_VOLUME" >/dev/null 2>&1; then
    echo "内置 PostgreSQL 的数据在 docker 数据卷 $POSTGRES_VOLUME 里，也没有删"
    echo "确定不要了再手动删除（不可恢复）：sudo rm -rf '$src' && docker volume rm $POSTGRES_VOLUME"
  else
    echo "确定不要了再手动删除（不可恢复）：sudo rm -rf '$src'"
  fi
}

case "$CMD" in
  install) cmd_install ;;
  upgrade) cmd_upgrade ;;
  migrate-to-postgres) cmd_migrate_to_postgres ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  uninstall) cmd_uninstall ;;
esac
