#!/usr/bin/env bash
# build-image.sh
# 在 ARM 服务器上本机构建 Docker 镜像并启动容器（原生构建，无需 buildx/QEMU）。
#
# 用法：
#   ./deploy/build-image.sh              # 构建 + 启动
#   ./deploy/build-image.sh --build-only # 只构建镜像，不启动
#   ./deploy/build-image.sh --start      # 只启动已构建的镜像（不重新构建）
#   ./deploy/build-image.sh --logs       # 构建启动后跟随日志
#
# 前置：ARM 服务器已装 Docker
#   curl -fsSL https://get.docker.com | sh && sudo systemctl enable --now docker
#
# 镜像名默认 data-hub:arm64，可用环境变量覆盖：
#   IMAGE=data-hub:v1.0 ./deploy/build-image.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

IMAGE="${IMAGE:-data-hub:arm64}"
DO_BUILD=true
DO_START=true
DO_LOGS=false

# 解析参数
for arg in "$@"; do
    case "$arg" in
        --build-only) DO_START=false ;;
        --start)      DO_BUILD=false ;;
        --logs)       DO_LOGS=true ;;
        -h|--help)
            sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "未知参数: $arg (可用: --build-only --start --logs)" >&2; exit 1 ;;
    esac
done

# 检查 Docker
if ! command -v docker >/dev/null 2>&1; then
    echo "错误：未安装 Docker。请先安装：" >&2
    echo "  curl -fsSL https://get.docker.com | sh && sudo systemctl enable --now docker" >&2
    exit 1
fi

# 确认当前架构
ARCH="$(uname -m)"
echo "==> 当前架构: $ARCH"
case "$ARCH" in
    aarch64|arm64)    echo "    (arm64，原生构建)" ;;
    armv7l)           echo "    (arm/v7，原生构建)" ;;
    x86_64)           echo "    警告: 当前是 x86_64，本脚本用于 ARM 本机构建。如需交叉构建请用 build-cross-arm64.sh。" >&2 ;;
    *)                echo "    警告: 未知架构 $ARCH" >&2 ;;
esac

if $DO_BUILD; then
    echo ""
    echo "==> 构建镜像 $IMAGE ..."
    # 原生构建：不带 --platform，Docker 自动用宿主架构
    # .dockerignore 会排除所有 test_*/probe_*/deploy 等无关文件，保持镜像精简
    docker build -t "$IMAGE" .
    echo "==> 构建完成: $IMAGE"
fi

if ! $DO_START; then
    echo ""
    echo "==> 仅构建，未启动。后续启动："
    echo "    docker compose -f docker-compose.deploy.yml up -d"
    echo "    （或 IMAGE=$IMAGE ./deploy/build-image.sh --start）"
    exit 0
fi

echo ""
echo "==> 启动容器 ..."
# 用 deploy compose（引用已有镜像，不再 build）
# 通过 IMAGE 环境变量把构建出的镜像名传给 compose
export IMAGE
docker compose -f docker-compose.deploy.yml up -d

echo ""
echo "==> 启动完成。状态："
docker compose -f docker-compose.deploy.yml ps

echo ""
echo "访问："
echo "  健康检查: curl http://localhost:8088/health"
echo "  Web 控制台: http://<服务器IP>:8089/"

if $DO_LOGS; then
    echo ""
    echo "==> 跟随日志 (Ctrl+C 退出，不影响容器) ..."
    docker compose -f docker-compose.deploy.yml logs -f data-hub
fi
