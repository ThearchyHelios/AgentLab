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

# 给沙箱用 bubblewrap 时容器要加的权限，说明见 agentlab.env 的沙箱一节
BWRAP_OPTS=(--security-opt seccomp=unconfined --security-opt systempaths=unconfined --security-opt apparmor=unconfined)

usage() {
  cat <<USAGE
用法：./deploy.sh [子命令] [选项]

  install            部署（默认）：导入镜像、创建数据目录、启动容器，等健康检查通过后打印访问地址。
                     已经有同名容器的，先停掉再换成新的，数据保留。改了 agentlab.env 也用它让配置生效。
  upgrade            用这个包里的镜像替换正在用的容器，数据保留。旧镜像留着，方便回退。
  start | stop | restart
                     启动 / 停止 / 重启容器。
  status             查看容器状态、健康检查、数据目录和访问地址。
  logs [-f] [--tail N]
                     查看日志（默认最后 200 行），-f 持续跟随。
  uninstall [--purge-image]
                     删除容器。默认保留数据和镜像，--purge-image 连镜像一起删。数据永远不删。
  -h, --help         显示这段说明。

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
if [ $# -gt 0 ]; then
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -*) ;;
    *) CMD="$1"; shift ;;
  esac
fi
case "$CMD" in
  install|upgrade|start|stop|restart|status|logs|uninstall) ;;
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
      HOST_PORT|BIND_ADDR|DATA_DIR|CONTAINER_NAME|ENABLE_BWRAP)
        printf -v "$key" '%s' "$(unquote "$(trim "$val")")" ;;
      AGENTLAB_DATA_DIR|AGENTLAB_WEB_DIST|AGENTLAB_PORT|AGENTLAB_HOST)
        die "agentlab.env 第 $n 行：不要设 ${key}。容器里的路径和端口是固定的，改宿主机这边用 DATA_DIR、HOST_PORT、BIND_ADDR" ;;
      *)
        case "$val" in
          \"*\"|\'*\') warn "agentlab.env 第 $n 行：$key 的值带了引号。docker 不去引号，引号会原样传进容器，一般要去掉" ;;
        esac ;;
    esac
  done < "$ENV_FILE"

  case "$HOST_PORT" in ''|*[!0-9]*) die "HOST_PORT 必须是数字：$HOST_PORT" ;; esac
  if [ "$HOST_PORT" -lt 1 ] || [ "$HOST_PORT" -gt 65535 ]; then die "HOST_PORT 超出范围：$HOST_PORT"; fi
  [ -n "$BIND_ADDR" ] || die "BIND_ADDR 不能为空（所有网卡写 0.0.0.0，只给本机用写 127.0.0.1）"
  [ -n "$DATA_DIR" ] || die "DATA_DIR 不能为空"
  case "$CONTAINER_NAME" in
    ''|[!a-zA-Z0-9]*|*[!a-zA-Z0-9_.-]*) die "CONTAINER_NAME 只能用字母、数字和 _ . -，并以字母或数字开头：$CONTAINER_NAME" ;;
  esac
  case "$ENABLE_BWRAP" in 0|1) ;; *) die "ENABLE_BWRAP 只能是 0 或 1：$ENABLE_BWRAP" ;; esac

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
  [ -f "$HERE/SHA256SUMS" ] || return 0
  command -v sha256sum >/dev/null 2>&1 || return 0
  local line
  line="$(grep ' image.tar$' "$HERE/SHA256SUMS" || true)"
  [ -n "$line" ] || return 0
  say "校验 image.tar"
  (cd "$HERE" && printf '%s\n' "$line" | sha256sum -c --quiet -) \
    || die "image.tar 校验和对不上，文件不完整或损坏。重新拷一份离线包再试"
}

load_image() {
  [ -f "$IMAGE_TAR" ] || die "本地没有镜像 ${IMAGE}，包里也找不到 image.tar"
  verify_tar
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
  if [ -n "$(ls -A "$DATA_ABS" 2>/dev/null)" ] && [ ! -e "$DATA_ABS/agentlab.db" ]; then
    die "DATA_DIR 指向的 $DATA_ABS 不是空目录，也不像 AgentLab 的数据目录（里面没有 agentlab.db）。
为免改错别的文件的属主，请指向一个新目录，或者原来的 AgentLab 数据目录"
  fi
  say "把数据目录的属主改成容器里的用户（uid ${uid}），容器里的服务才写得进去"
  docker run --rm --user 0:0 --network none --entrypoint chown \
    -v "$DATA_ABS:/data" "$IMAGE" -R "$uid:$gid" /data \
    || die "无法修改 $DATA_ABS 的属主"
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
    "$IMAGE" >/dev/null
}

wait_healthy() {
  local name="$1" state health i
  say "等健康检查通过"
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
  deploy_container
  if [ "$old_image" != "$IMAGE" ] && docker image inspect "$old_image" >/dev/null 2>&1; then
    echo "旧镜像 $old_image 还留着，方便回退。确认新版本没问题后可以删：docker rmi $old_image"
  fi
}

cmd_start() {
  check_docker; read_env; require_container
  docker start "$CONTAINER_NAME" >/dev/null
  if wait_healthy "$CONTAINER_NAME"; then
    say "已启动"; print_access
  else
    show_failure; die "容器没通过健康检查"
  fi
}

cmd_stop() {
  check_docker; read_env; require_container
  say "停止 ${CONTAINER_NAME}（数据保留）"
  docker stop "$CONTAINER_NAME" >/dev/null
  say "已停止。开机也不会自动起来，要用时运行 ./deploy.sh start"
}

cmd_restart() {
  check_docker; read_env; require_container
  say "重启 $CONTAINER_NAME"
  docker restart "$CONTAINER_NAME" >/dev/null
  if wait_healthy "$CONTAINER_NAME"; then
    say "已重启"; print_access
  else
    show_failure; die "容器没通过健康检查"
  fi
}

cmd_status() {
  check_docker; read_env
  if ! container_exists "$CONTAINER_NAME"; then
    echo "没有名为 $CONTAINER_NAME 的容器（还没部署，或已卸载）。部署：./deploy.sh install"
    exit 1
  fi
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
  echo "这个包    $IMAGE"
  if [ "$state" = running ]; then print_access; fi
}

cmd_logs() {
  check_docker; read_env; require_container
  local args=(--tail "$TAIL")
  [ -z "$FOLLOW" ] || args+=(--follow)
  # exec：Ctrl-C 直接交给 docker logs，不留一个还在跟随的子进程
  exec docker logs "${args[@]}" "$CONTAINER_NAME"
}

cmd_uninstall() {
  check_docker; read_env
  local src="" name
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
  if [ -n "$PURGE_IMAGE" ]; then
    if image_present; then
      say "删除镜像 $IMAGE"
      docker rmi "$IMAGE" >/dev/null || warn "镜像 $IMAGE 没删掉（可能还有别的容器在用）"
    fi
  else
    echo "镜像 $IMAGE 保留着，重新部署不用再导入。要删：./deploy.sh uninstall --purge-image"
  fi
  [ -n "$src" ] || src="$DATA_PATH"
  echo
  echo "数据没有删，还在：$src"
  echo "确定不要了再手动删除（不可恢复）：sudo rm -rf '$src'"
}

case "$CMD" in
  install) cmd_install ;;
  upgrade) cmd_upgrade ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  uninstall) cmd_uninstall ;;
esac
