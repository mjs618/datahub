# ARM 服务器部署指南

Data Hub 的依赖（`asyncua`、`requests`）均为纯 Python，无 C 扩展、无需编译工具链，
基础镜像 `python:3.9-slim` 官方提供 `linux/amd64`、`linux/arm64`、`linux/arm/v7` 三种架构。
因此部署到 ARM 基本零障碍，本文档覆盖常见的三种部署路径与排错。

---

## 0. 先确认服务器架构

ARM 有多个变体，镜像不互通，先在目标服务器上确认：

```bash
uname -m
```

| 输出              | buildx platform    | 常见设备                                              |
|------------------|--------------------|-------------------------------------------------------|
| `aarch64` / `arm64` | `linux/arm64`      | 树莓派 4(64位)、AWS Graviton、阿里云倚天、华为鲲鹏    |
| `armv7l`         | `linux/arm/v7`     | 树莓派 3/4(32位系统)、部分嵌入式网关                  |

> 90% 以上的现代 ARM 服务器是 `aarch64`。下文以 `arm64` 为主，`arm/v7` 同理替换 platform 即可。

---

## 1. 方式 A：在 ARM 服务器上直接构建（最省事）

服务器装好 Docker 后直接本机构建本机架构，无需 buildx / QEMU 交叉编译。

```bash
# 1) 安装 Docker（官方脚本支持 arm64）
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker

# 2) 把项目代码放到服务器（git clone 或 scp）
git clone <repo-url> data-hub && cd data-hub

# 3) 构建并启动（compose 会自动选择当前架构）
sudo docker compose up -d --build
```

查看状态与日志：

```bash
sudo docker compose ps
sudo docker compose logs -f data-hub
curl http://localhost:8088/health     # 健康端点
```

打开控制台：浏览器访问 `http://<服务器IP>:8089/`。

---

## 2. 方式 B：在 x86 开发机交叉构建 ARM 镜像

适合 CI/CD 或开发机统一构建后分发。本项目的 bake 配置已固化（见 `docker-bake.hcl`）。

### 2.1 前置准备（一次性）

```bash
# 创建多架构 builder
docker buildx create --use --name multiarch --driver docker-container

# 安装 QEMU（交叉构建非本机架构时必需，一次性配置）
docker run --privileged --rm tonistiigi/binfmt --install all
```

### 2.2 只构建 arm64 并加载到本地 Docker（不推送）

```bash
docker buildx bake --load local-arm64
# 产物镜像名：data-hub:arm64
```

### 2.3 导出镜像并传输到 ARM 服务器

```bash
# 导出
docker save data-hub:arm64 | gzip > data-hub-arm64.tar.gz

# 传到服务器（scp / rsync / 文件中转均可）
scp data-hub-arm64.tar.gz user@<arm-server>:/tmp/

# 在服务器上加载
ssh user@<arm-server>
docker load < /tmp/data-hub-arm64.tar.gz
```

### 2.4 在服务器上用已加载的镜像启动

把 `docker-compose.yml` 拷到服务器，把 `build:` 段改成引用已有镜像：

```yaml
services:
  data-hub:
    image: data-hub:arm64        # 用已加载的镜像，不再 build
    # build: ...                 # 注释或删除
    container_name: data-hub-service
    # ...其余保持不变
```

然后：

```bash
docker compose up -d
```

---

## 3. 方式 C：多架构发布到 Registry（团队/CI 推荐）

一次构建产出 `amd64 + arm64 + arm/v7` 的统一 manifest，部署端 `docker pull` 会自动拉取匹配架构。

```bash
# 推送到默认 registry（修改 docker-bake.hcl 里的 REGISTRY 默认值，或用环境变量覆盖）
REGISTRY=ghcr.io/yourorg/data-hub TAG=v1.0.0 docker buildx bake --push release
```

ARM 服务器上拉取即用：

```bash
docker pull ghcr.io/yourorg/data-hub:v1.0.0
# compose 里 image: 改成上面的引用，docker compose up -d
```

---

## 4. 部署前必改的配置

`docker-compose.yml` 里的环境变量是示例值，**务必按实际环境修改**：

| 变量                | 说明                                       | 默认示例值                              |
|--------------------|--------------------------------------------|----------------------------------------|
| `BASE_IP`          | 历史库 / 实时库服务地址                     | `http://192.168.1.35:6543`             |
| `OPCUA_URL`        | OPC UA 服务地址                             | `opc.tcp://192.168.1.35:6810`          |
| `APP_CODE` / `APP_SECRET` | 鉴权凭据                              | `data` / `123456`                      |
| `TRIG_NODE_ID`     | OPC UA 触发节点 ID                          | `ns=2;s=Trigger`                       |
| `WATCH_LIST`       | 监听的历史点 ID 列表（JSON 数组）           | 示例两个点                             |
| `NODE_MAPPING`     | 历史点 → RTDB 点映射（JSON dict）           | 示例映射                               |

> 触发配置、监听列表、映射也可启动后在 Web UI 控制台（`http://<IP>:8089`）的「配置」页修改，会写入 `config_runtime.json` 持久化，优先级高于环境变量。

### 端口说明

| 端口  | 用途                 | 访问方式                  |
|------|----------------------|--------------------------|
| 8088 | 健康检查端点          | `curl http://<IP>:8088/health` |
| 8089 | Web UI 控制台         | 浏览器 `http://<IP>:8089/`     |

两个端口都已在 `docker-compose.yml` 的 `ports:` 映射。Web UI 端口可用环境变量 `WEB_UI_PORT` 修改（改后同步改端口映射）。

### 网络连通性注意

- Docker 默认 bridge 网络可访问宿主机所在局域网。
- 若 `BASE_IP` / `OPCUA_URL` 与宿主机不在同网段，或需直接使用宿主机网络栈，可在 compose 加：
  ```yaml
  network_mode: host
  ```
  （仅 Linux 有效；此时 `ports:` 段会被忽略，容器直接占用宿主机端口）

### 时区

compose 已设置 `TZ=Asia/Shanghai`，日志与时间戳按本地时区。如需改其他时区，改这一行即可。

> 说明：项目 Web UI 的时间显示是浏览器端用 `toLocaleString('zh-CN')` 渲染的，服务端只传 epoch 秒，
> 所以即使不设 TZ，控制台显示也基本正确；TZ 主要影响容器内日志时间戳。

---

## 5. 常见问题

### Q1：`docker buildx bake` 提示 buildx 不可用
老版 Docker（< 20.10）不带 buildx。升级 Docker，或 `docker buildx install`。
实在没有 buildx，改用方式 A 在 ARM 机上直接构建。

### Q2：交叉构建很慢 / 报 QEMU 错误
交叉构建依赖 QEMU 模拟，本项目仅两个纯 Python 包，正常几十秒完成。
若失败，重新配置 binfmt：`docker run --privileged --rm tonistiigi/binfmt --install all`。

### Q3：`exec format error` 启动即崩
镜像架构与服务器架构不匹配（常见 `arm/v7` 镜像跑在 `arm64` 上，或反之）。
用 `uname -m` 确认后重新选择对应 platform 构建。

### Q4：服务器访问不到历史库 / OPC UA
- 容器内确认：`docker compose exec data-hub python -c "import socket;print(socket.create_connection(('192.168.1.35',6543),3))"`
- 不通则检查宿主机路由/防火墙，或改用 `network_mode: host`。

### Q5：从外部访问不到 8089 控制台
- 确认 `docker-compose.yml` 的 `ports:` 已映射 `8089:8089`（本仓库已配置）。
- 检查服务器安全组 / 防火墙放行 8089。

### Q6：pip 在 ARM 上装依赖失败
本项目依赖（`asyncua`、`requests`）均为纯 Python wheel，arm64 上直接安装不会触发编译。
若未来新增带 C 扩展的依赖，ARM 上需先装编译工具链：`apt-get install -y gcc python3-dev`。

---

## 6. 裸机（非 Docker）部署

如果不用 Docker，ARM 服务器上直接跑 Python：

```bash
# Python ≥ 3.7（推荐 3.9+）
sudo apt-get install -y python3 python3-pip

cd data-hub
pip3 install -r requirements.txt

# 通过环境变量配置（参考 docker-compose.yml 的 environment 段）
export BASE_IP=http://192.168.1.35:6543
export OPCUA_URL=opc.tcp://192.168.1.35:6810
export APP_CODE=data APP_SECRET=123456
# ...其余变量按需

python3 main.py
```

建议用 systemd 或 supervisor 做进程守护与开机自启，并保证 `STATE_FILE`、
`WRITE_CACHE_FILE` 指向持久化目录（默认 `/tmp/...`，重启可能被清理）。
