#!/usr/bin/env bash
# build-cross-arm64.sh
# 方式B：在 x86 开发机上交叉构建 arm64 镜像并导出为 tar.gz，便于传到 ARM 服务器。
#
# 用法：
#   ./deploy/build-cross-arm64.sh                  # 默认输出 dist/data-hub-arm64.tar.gz
#   ./deploy/build-cross-arm64.sh -o /tmp/img.tgz  # 自定义输出
#   ./deploy/build-cross-arm64.sh -t v1.0.0        # 指定 tag
#   ./deploy/build-cross-arm64.sh --load           # 只构建并载入本地 Docker，不导出
#
# 前置（一次性）：
#   docker buildx create --use --name multiarch --driver docker-container
#   docker run --privileged --rm tonistiigi/binfmt --install all
#
# 传到 ARM 服务器后：
#   docker load < data-hub-arm64.tar.gz
#   docker compose -f docker-compose.deploy.yml up -d   # deploy 文件需一并传过去

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# 默认参数
OUT_FILE=""
TAG="arm64"
IMAGE_NAME="data-hub"
DO_LOAD_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output) OUT_FILE="$2"; shift 2 ;;
        -t|--tag)    TAG="$2"; shift 2 ;;
        --load)      DO_LOAD_ONLY=true; shift ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

IMAGE_TAG="${IMAGE_NAME}:${TAG}"

echo "==> 检查 buildx ..."
if ! docker buildx version >/dev/null 2>&1; then
    echo "错误：未安装 buildx，请升级 Docker 或安装 buildx 插件。" >&2
    exit 1
fi

# 确保有可用的多架构 builder
BUILDER_NAME="multiarch"
if ! docker buildx ls | grep -q "^${BUILDER_NAME} "; then
    echo "==> 创建多架构 builder '$BUILDER_NAME' ..."
    docker buildx create --use --name "$BUILDER_NAME" --driver docker-container
else
    docker buildx use "$BUILDER_NAME" >/dev/null 2>&1 || true
fi

echo "==> 构建 arm64 镜像 ($IMAGE_TAG) ..."
if $DO_LOAD_ONLY; then
    docker buildx build --platform linux/arm64 -t "$IMAGE_TAG" --load .
    echo ""
    echo "==> 完成：镜像已载入本地 Docker" 
    echo "    $IMAGE_TAG"
    echo "    用 docker images 确认；如需传到 ARM 服务器，去掉 --load 重跑或手动："
    echo "    docker save $IMAGE_TAG | gzip > data-hub-arm64.tar.gz"
    exit 0
fi

# 构建 + 导出
docker buildx build --platform linux/arm64 -t "$IMAGE_TAG" --load .

# 默认输出路径
if [[ -z "$OUT_FILE" ]]; then
    mkdir -p "$ROOT_DIR/dist"
    OUT_FILE="$ROOT_DIR/dist/data-hub-${TAG}.tar.gz"
fi

echo "==> 导出镜像到 $OUT_FILE ..."
docker save "$IMAGE_TAG" | gzip > "$OUT_FILE"

SIZE=$(du -h "$OUT_FILE" | cut -f1)
echo ""
echo "==> 完成"
echo "    镜像: $IMAGE_TAG"
echo "    产物: $OUT_FILE ($SIZE)"
echo ""
echo "传输到 ARM 服务器并加载："
echo "    scp $OUT_FILE user@<arm-server>:/tmp/"
echo "    ssh user@<arm-server> 'docker load < /tmp/$(basename "$OUT_FILE")'"
echo "    # 然后把 deploy/docker-compose.deploy.yml 传过去，执行："
echo "    docker compose -f docker-compose.deploy.yml up -d"
