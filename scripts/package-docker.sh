#!/usr/bin/env bash
# 打 Docker 离线包：构建 Linux 镜像，连同一键部署脚本、配置模板和部署说明打成一个 tar，
# 拷到只装了 Docker Engine、不能联网的机器上就能部署。部署说明见 deploy/README.md。
set -euo pipefail

usage() {
  cat <<'USAGE'
用法：./scripts/package-docker.sh [--platform linux/arm64|linux/amd64] [--version <版本>] [--out <目录>] [--gzip] [--no-microvm]

  --platform <平台>  目标机的平台。默认按本机架构取 linux/<arch>。64 位系统的 ARM 单板机是 linux/arm64，
                     普通 PC 和服务器是 linux/amd64。和本机架构不同时 docker 用模拟构建，会慢很多。
  --version <版本>   镜像标签和包名里的版本。默认 git describe --tags --always --dirty，拿不到就用短哈希加日期。
  --out <目录>       产物放哪里，默认仓库下的 release/。
  --gzip             另外再产出一份 .tar.gz。docker 用 containerd 镜像存储时镜像层本身已经压缩过，.tar.gz 小不了多少。
  --no-microvm       不在镜像里带 microVM 运行时和沙箱镜像，镜像小约 150 MB。目标机没有 KVM 时用不上它们。
                     默认带上：目标机有 KVM 时，deploy.sh 会自动启用 microVM，而且不用联网。
  -h, --help         显示这段说明。

产物：<out>/agentlab-<版本>-linux-<arch>.tar，里面有 image.tar、deploy.sh、agentlab.env、README.md、SHA256SUMS。
构建需要联网（拉基础镜像、装依赖、拉 microVM 的沙箱镜像）；部署不需要。镜像只留在本机，不会推到任何仓库。
USAGE
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PLATFORM=""
VERSION=""
OUT="$ROOT/release"
GZIP=""
WITH_MICROVM=1
need_value() { [ $# -ge 2 ] && [ -n "$2" ] || { echo "$1 后面要跟一个值"; echo; usage; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) need_value "$@"; PLATFORM="$2"; shift ;;
    --platform=*) PLATFORM="${1#--platform=}" ;;
    --version) need_value "$@"; VERSION="$2"; shift ;;
    --version=*) VERSION="${1#--version=}" ;;
    --out) need_value "$@"; OUT="$2"; shift ;;
    --out=*) OUT="${1#--out=}" ;;
    --gzip) GZIP=1 ;;
    --no-microvm) WITH_MICROVM=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "认不出的参数：$1"; echo; usage; exit 2 ;;
  esac
  shift
done

# ---- 平台 ----
if [ -z "$PLATFORM" ]; then
  case "$(uname -m)" in
    arm64|aarch64) PLATFORM="linux/arm64" ;;
    x86_64|amd64) PLATFORM="linux/amd64" ;;
    *) echo "认不出本机架构 $(uname -m)，用 --platform 指定 linux/arm64 或 linux/amd64"; exit 2 ;;
  esac
fi
case "$PLATFORM" in
  linux/arm64|linux/amd64) ARCH="${PLATFORM#linux/}" ;;
  *) echo "只支持 linux/arm64 和 linux/amd64：$PLATFORM"; exit 2 ;;
esac

# ---- 版本 ----
# GIT_OPTIONAL_LOCKS=0：只读版本库，不顺手刷新索引文件
if [ -z "$VERSION" ]; then
  VERSION="$(GIT_OPTIONAL_LOCKS=0 git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || true)"
fi
if [ -z "$VERSION" ]; then
  HASH="$(GIT_OPTIONAL_LOCKS=0 git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
  VERSION="$HASH-$(date +%Y%m%d)"
fi
# docker 标签只认字母、数字和 _ . -，最长 128，不能以 . 或 - 开头
VERSION="$(printf '%s' "$VERSION" | tr -c 'A-Za-z0-9_.-' '-')"
VERSION="${VERSION#[.-]}"
VERSION="${VERSION:0:120}"
[ -n "$VERSION" ] || { echo "版本号是空的，用 --version 指定"; exit 2; }

IMAGE="agentlab:$VERSION"
NAME="agentlab-$VERSION-linux-$ARCH"

# ---- 工具 ----
command -v docker >/dev/null || { echo "缺少 docker"; exit 1; }
docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || { echo "连不上 docker 服务，先把 Docker 启动起来"; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "缺少 docker buildx（Docker Desktop 自带；Linux 上装 docker-buildx-plugin）"; exit 1; }
for f in deploy/deploy.sh deploy/agentlab.env deploy/README.md Dockerfile .dockerignore; do
  [ -f "$ROOT/$f" ] || { echo "缺少 $f"; exit 1; }
done
if command -v sha256sum >/dev/null 2>&1; then SHA=(sha256sum); else SHA=(shasum -a 256); fi

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
STAGE="$OUT/$NAME"
TAR="$OUT/$NAME.tar"

if [ "$WITH_MICROVM" = 1 ]; then MICROVM_NOTE="带 microVM"; else MICROVM_NOTE="不带 microVM"; fi
echo "==> 版本 $VERSION · 平台 $PLATFORM · $MICROVM_NOTE · 产物 $TAR"

# ---- 1. 构建 ----
# 不带 provenance / SBOM：它们会让导出的镜像多出几份附加清单，老版本的 docker load 认不全
echo "==> 构建镜像 $IMAGE"
docker buildx build \
  --platform "$PLATFORM" \
  --provenance=false --sbom=false \
  --build-arg VERSION="$VERSION" \
  --build-arg WITH_MICROVM="$WITH_MICROVM" \
  --load \
  -t "$IMAGE" \
  "$ROOT"

GOT="$(docker image inspect -f '{{.Os}}/{{.Architecture}}' "$IMAGE")"
[ "$GOT" = "$PLATFORM" ] || { echo "构建出来的镜像是 ${GOT}，不是 $PLATFORM"; exit 1; }

# ---- 2. 暂存目录 ----
rm -rf "$STAGE"
mkdir -p "$STAGE"
trap 'rm -rf "$STAGE"' EXIT

echo "==> 导出镜像（docker save）"
if docker save --help 2>/dev/null | grep -q -- '--platform'; then
  docker save --platform "$PLATFORM" -o "$STAGE/image.tar" "$IMAGE"
else
  docker save -o "$STAGE/image.tar" "$IMAGE"
fi

fill() {
  # 模板里的占位符换成这一次的版本和架构
  sed -e "s|__AGENTLAB_VERSION__|$VERSION|g" -e "s|__AGENTLAB_ARCH__|$ARCH|g" "$1" > "$2"
}
fill "$ROOT/deploy/deploy.sh" "$STAGE/deploy.sh"
fill "$ROOT/deploy/README.md" "$STAGE/README.md"
cp "$ROOT/deploy/agentlab.env" "$STAGE/agentlab.env"
chmod 755 "$STAGE/deploy.sh"
chmod 644 "$STAGE/README.md" "$STAGE/agentlab.env" "$STAGE/image.tar"
if grep -q '__AGENTLAB_' "$STAGE/deploy.sh" "$STAGE/README.md"; then
  echo "模板里还有没填上的占位符"; exit 1
fi

(cd "$STAGE" && "${SHA[@]}" image.tar deploy.sh agentlab.env README.md > SHA256SUMS)
chmod 644 "$STAGE/SHA256SUMS"

# ---- 3. 打包 ----
# macOS 的 tar（bsdtar）默认会塞进 ._ 开头的资源文件和扩展属性，Linux 上解包会报一堆警告，关掉；
# 属主统一写成 0，免得目标机上用 sudo 解包时冒出一个不存在的 uid
echo "==> 打包 $TAR"
rm -f "$TAR" "$TAR.gz"
if tar --version 2>/dev/null | grep -q 'GNU tar'; then
  tar --owner=0 --group=0 --numeric-owner -cf "$TAR" -C "$OUT" "$NAME"
else
  COPYFILE_DISABLE=1 tar --uid 0 --gid 0 --uname root --gname root --no-mac-metadata --no-xattrs -cf "$TAR" -C "$OUT" "$NAME"
fi
if [ -n "$GZIP" ]; then
  echo "==> 压缩 $TAR.gz"
  if command -v pigz >/dev/null 2>&1; then pigz -c "$TAR" > "$TAR.gz"; else gzip -c "$TAR" > "$TAR.gz"; fi
fi

size() { du -h "$1" | awk '{print $1}'; }
echo
echo "完成："
echo "  $TAR  ($(size "$TAR"))"
[ -z "$GZIP" ] || echo "  $TAR.gz  ($(size "$TAR.gz"))"
echo "  镜像 $IMAGE 留在本机（没有推送到任何仓库），$MICROVM_NOTE"
echo
echo "下一步："
echo "  1. 把 $NAME.tar 拷到目标机（scp、U 盘都行）"
echo "  2. 在目标机上："
echo "       tar -xf $NAME.tar && cd $NAME"
echo "       nano agentlab.env      # 端口、监听地址、数据目录、模型 Key"
echo "       ./deploy.sh"
echo "  详细说明见包里的 README.md（仓库里是 deploy/README.md）"
