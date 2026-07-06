# docker buildx bake 配置：固化 data-hub 的多架构构建。
#
# 常用命令：
#   1) 本地构建单架构（用于在 ARM 机上直接构建本机架构，或 x86 上验证）：
#        docker buildx bake --load local-arm64
#        docker buildx bake --load local-amd64
#
#   2) 多架构构建并推送到 registry（多架构 manifest 必须推送，不能 --load）：
#        docker buildx bake --push release
#      通过变量覆盖 registry / tag：
#        REGISTRY=ghcr.io/myorg TAG=v1.2.0 docker buildx bake --push release
#
#   3) 在 ARM 服务器上交叉构建 amd64 镜像（反之亦然），同样用 local-* target。
#
# 前置：需要 buildx 支持。首次使用创建一个多架构 builder：
#   docker buildx create --use --name multiarch --driver docker-container
# 交叉构建（非本机架构）依赖 QEMU，binfmt 一次性配置：
#   docker run --privileged --rm tonistiigi/binfmt --install all

# 可被环境变量覆盖的变量，便于 CI 注入
variable "REGISTRY" {
  default = "data-hub"   # 留空则只用本地名；推送时改成 "<registry>/<namespace>"
}

variable "TAG" {
  default = "latest"
}

# 共享的目标定义，所有 target 继承它
target "defaults" {
  dockerfile = "Dockerfile"
  context    = "."
}

# 单架构本地构建：可在任意机器上构建「本机之外的单一架构」并载入
# 适合 ARM 服务器自构建，或 x86 上产出 arm64 镜像供测试
target "local-arm64" {
  inherits   = ["defaults"]
  platforms  = ["linux/arm64"]
  tags       = ["${REGISTRY}:arm64"]
  output     = ["type=docker"]   # --load
}

target "local-amd64" {
  inherits   = ["defaults"]
  platforms  = ["linux/amd64"]
  tags       = ["${REGISTRY}:amd64"]
  output     = ["type=docker"]
}

target "local-armv7" {
  inherits   = ["defaults"]
  platforms  = ["linux/arm/v7"]
  tags       = ["${REGISTRY}:armv7"]
  output     = ["type=docker"]
}

# 多架构发布：产出统一 tag 的多架构 manifest 并推送
# 必须推送到 registry，本地无法 load 多架构镜像
target "release" {
  inherits   = ["defaults"]
  platforms  = ["linux/amd64", "linux/arm64", "linux/arm/v7"]
  tags       = ["${REGISTRY}:${TAG}", "${REGISTRY}:latest"]
  output     = ["type=registry"]   # --push
}

# 默认 group：执行 `docker buildx bake` 不带参数时运行
group "default" {
  targets = ["local-arm64"]
}
