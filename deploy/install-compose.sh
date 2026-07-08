#!/usr/bin/env bash
# install-compose.sh
# 离线安装 docker compose v2 插件到 ARM 服务器。
#
# 用法（在 ARM 服务器上，二进制文件已传过去后）：
#   ./install-compose.sh                         # 安装到系统目录 /usr/local/lib/docker/cli-plugins（需 sudo）
#   ./install-compose.sh --user                  # 安装到当前用户 ~/.docker/cli-plugins（无需 sudo）
#   ./install-compose.sh /path/to/docker-compose # 指定已下载的二进制路径
#
# 默认在脚本同目录找 docker-compose 二进制；找不到则报错。
# 安装后验证：docker compose version

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 默认二进制路径：脚本同目录
BIN="${1:-$SCRIPT_DIR/docker-compose}"
USER_INSTALL=false
if [ "$1" = "--user" ]; then
    USER_INSTALL=true
    BIN="$SCRIPT_DIR/docker-compose"
fi

# 检查二进制是否存在
if [ ! -f "$BIN" ]; then
    echo "错误：找不到 compose 二进制 $BIN" >&2
    echo "请先把 docker-compose-linux-aarch64 传到服务器，重命名为 docker-compose" >&2
    echo "或作为参数传入：$0 /path/to/docker-compose" >&2
    exit 1
fi

# 检查架构
ARCH="$(uname -m)"
echo "==> 当前架构: $ARCH"
if [ "$ARCH" != "aarch64" ] && [ "$ARCH" != "arm64" ]; then
    echo "警告：当前是 $ARCH，本二进制是 aarch64。若架构不符请重新下载对应版本。" >&2
fi

# 检查 docker 是否安装
if ! command -v docker >/dev/null 2>&1; then
    echo "错误：未检测到 docker 命令。请先安装 Docker Engine。" >&2
    exit 1
fi

if $USER_INSTALL; then
    # 用户级安装（无需 sudo）
    DEST_DIR="$HOME/.docker/cli-plugins"
    mkdir -p "$DEST_DIR"
    cp "$BIN" "$DEST_DIR/docker-compose"
    chmod +x "$DEST_DIR/docker-compose"
    echo "==> 已安装到 $DEST_DIR/docker-compose（当前用户）"
else
    # 系统级安装（需要 sudo）
    DEST_DIR="/usr/local/lib/docker/cli-plugins"
    echo "==> 安装到 $DEST_DIR（需要 sudo）..."
    sudo mkdir -p "$DEST_DIR"
    sudo cp "$BIN" "$DEST_DIR/docker-compose"
    sudo chmod +x "$DEST_DIR/docker-compose"
    echo "==> 已安装到 $DEST_DIR/docker-compose（系统级）"
fi

echo ""
echo "==> 验证："
docker compose version || {
    echo "验证失败。检查：" >&2
    echo "  1) docker --version 是否正常" >&2
    echo "  2) ls -l $DEST_DIR/docker-compose 是否有执行权限" >&2
    echo "  3) docker cli-plugins 目录是否在搜索路径" >&2
    exit 1
}

echo ""
echo "==> 安装成功！现在可以用 docker compose 了。"
echo "    启动：docker compose -f docker-compose.deploy.yml up -d"
