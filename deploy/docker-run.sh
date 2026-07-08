#!/usr/bin/env bash
# docker-run.sh
# 不依赖 docker compose，用原生 docker run 启动 data-hub 容器。
# 适合 ARM 服务器上只装了 Docker Engine、没装 compose 插件的场景。
#
# 用法：
#   ./deploy/docker-run.sh            # 启动（若已存在同名容器先删除再创建）
#   ./deploy/docker-run.sh -d         # 后台启动
#   ./deploy/docker-run.sh --logs     # 启动后跟随日志
#   ./deploy/docker-run.sh stop       # 停止并删除容器
#   ./deploy/docker-run.sh status     # 查看容器状态
#
# 镜像名可用环境变量覆盖：IMAGE=data-hub:v1.0 ./deploy/docker-run.sh
# 配置按实际环境修改下方 ENV_* 变量（对应 docker-compose.deploy.yml）

set -euo pipefail

# ===== 配置区（按实际环境修改）=====
IMAGE="${IMAGE:-data-hub:arm64}"
CONTAINER="data-hub-service"

ENV_BASE_IP="http://192.168.1.35:6543"
ENV_OPCUA_URL="opc.tcp://192.168.1.35:6810"
ENV_APP_CODE="data"
ENV_APP_SECRET="123456"
ENV_TRIG_NODE_ID="ns=2;s=Trigger"
ENV_TRIG_HISTORY_ID="10001:ICSSYS.Trigger"
# ===== 配置区结束 =====

# 命名卷（与 compose 行为一致，持久化状态）
VOLUME="data-hub-state"

# 解析子命令
ACTION="${1:-start}"
DETACH="-d"
case "$ACTION" in
    start)    shift 2>/dev/null || true ;;
    -d)       ACTION="start"; shift 2>/dev/null || true ;;
    --logs)   ACTION="start"; DETACH=""; shift ;;
    stop)     ACTION="stop" ;;
    status)   ACTION="status" ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
esac

if [ "$ACTION" = "stop" ]; then
    echo "==> 停止并删除容器 $CONTAINER ..."
    docker rm -f "$CONTAINER" 2>/dev/null || echo "    容器不存在，跳过"
    exit 0
fi

if [ "$ACTION" = "status" ]; then
    docker ps -a --filter "name=$CONTAINER" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    exit 0
fi

# start
# 确保镜像已加载
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "错误：镜像 $IMAGE 不存在。请先执行：docker load < data-hub-arm64.tar.gz" >&2
    exit 1
fi

# 确保命名卷存在
docker volume create "$VOLUME" >/dev/null 2>&1 || true

# 删除可能存在的旧容器
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo "==> 启动容器 $CONTAINER （镜像 $IMAGE）..."
docker run $DETACH \
    --name "$CONTAINER" \
    --restart always \
    -e BASE_IP="$ENV_BASE_IP" \
    -e OPCUA_URL="$ENV_OPCUA_URL" \
    -e APP_CODE="$ENV_APP_CODE" \
    -e APP_SECRET="$ENV_APP_SECRET" \
    -e TRIG_NODE_ID="$ENV_TRIG_NODE_ID" \
    -e TRIG_HISTORY_ID="$ENV_TRIG_HISTORY_ID" \
    -e POLL_INTERVAL=1 \
    -e LOOKBACK_MINUTES=10 \
    -e SETTLE_TIME=2 \
    -e HTTP_TIMEOUT=10 \
    -e HTTP_MAX_RETRIES=3 \
    -e HTTP_RETRY_BACKOFF=1.5 \
    -e OPCUA_RECONNECT_INTERVAL=5 \
    -e HISTORY_PAGE_SIZE=0 \
    -e ENABLE_WRITE_CACHE=true \
    -e WRITE_CACHE_FILE=/data/data_hub_write_cache.json \
    -e STATE_FILE=/data/data_hub_state.json \
    -e ENABLE_DEDUP=true \
    -e HEALTH_ENDPOINT_ENABLED=true \
    -e HEALTH_ENDPOINT_PORT=8088 \
    -e HEALTH_STALE_THRESHOLD=60 \
    -e WEB_UI_PORT=8089 \
    -e TZ=Asia/Shanghai \
    -p 8088:8088 \
    -p 8089:8089 \
    -v "$VOLUME:/data" \
    --health-cmd="python -c \"import urllib.request,sys; r=urllib.request.urlopen('http://localhost:8088/health', timeout=5); sys.exit(0 if r.status==200 else 1)\"" \
    --health-interval=30s \
    --health-timeout=10s \
    --health-retries=3 \
    --health-start-period=30s \
    --log-driver=json-file \
    --log-opt max-size=10m \
    --log-opt max-file=3 \
    "$IMAGE"

if [ -z "$DETACH" ]; then
    # --logs 模式：前台已跟随，退出即结束
    exit 0
fi

echo ""
echo "==> 启动完成"
docker ps --filter "name=$CONTAINER" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""
echo "访问："
echo "  健康检查: curl http://localhost:8088/health"
echo "  Web 控制台: http://<服务器IP>:8089/"
echo ""
echo "常用操作："
echo "  查看日志:   docker logs -f $CONTAINER"
echo "  停止删除:   ./deploy/docker-run.sh stop"
echo "  查看状态:   ./deploy/docker-run.sh status"
