# build-cross-arm64.ps1
# 在 Windows 上交叉构建 arm64 Docker 镜像并导出 tar.gz，传到 ARM 服务器 docker load 即用。
#
# 用法：
#   .\deploy\build-cross-arm64.ps1                # 构建 + 导出到 dist\data-hub-arm64.tar.gz
#   .\deploy\build-cross-arm64.ps1 -LoadOnly      # 只构建并载入本地 Docker，不导出 tar
#   .\deploy\build-cross-arm64.ps1 -Tag v1.0.0    # 自定义镜像 tag
#   .\deploy\build-cross-arm64.ps1 -Platform arm/v7  # 构建 32 位 arm（树莓派3等）
#
# 前置：Docker Desktop（自带 buildx + QEMU，无需手动装 binfmt）
#       若用老版 Docker Engine，需先：
#         docker buildx create --use --name multiarch --driver docker-container
#         docker run --privileged --rm tonistiigi/binfmt --install all

param(
    [string]$Tag = "arm64",
    [ValidateSet("arm64","arm/v7")][string]$Platform = "arm64",
    [switch]$LoadOnly,
    [string]$OutFile = ""
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$ImageTag = "data-hub:$Tag"
# buildx platform 格式
if ($Platform -eq "arm64") { $Plat = "linux/arm64" } else { $Plat = "linux/arm/v7" }

Write-Host "==> 检查 Docker buildx ..." -ForegroundColor Cyan
$null = docker buildx version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "未检测到 buildx。请安装 Docker Desktop（自带 buildx），或升级 Docker Engine。"
    exit 1
}

# 确认当前 builder 支持 arm64；Docker Desktop 的 desktop-linux 默认支持
# 注意：$builders -notmatch 在数组上返回"不匹配的元素"，不能直接当布尔用
$builders = docker buildx ls
$supportsArm = [bool]($builders | Select-String -SimpleMatch "arm64")
if (-not $supportsArm) {
    Write-Host "==> 当前 builder 不支持 arm64，创建多架构 builder ..." -ForegroundColor Yellow
    docker buildx create --use --name multiarch --driver docker-container
    if ($LASTEXITCODE -ne 0) { Write-Error "创建 builder 失败"; exit 1 }
} else {
    # 优先用 desktop-linux（Docker Desktop，docker 驱动，无需额外拉 buildkit）
    if ($builders | Select-String -SimpleMatch "desktop-linux" | Select-String "\*") {
        Write-Host "==> 使用 desktop-linux builder（已激活）" -ForegroundColor DarkGray
    } elseif ($builders | Select-String -SimpleMatch "desktop-linux") {
        $null = docker buildx use desktop-linux 2>&1
        Write-Host "==> 已切换到 desktop-linux builder" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "==> 交叉构建镜像 $ImageTag ($Plat) ..." -ForegroundColor Cyan
Write-Host "    （首次用 QEMU 模拟会稍慢，本项目仅两个纯 Python 依赖，通常 1-2 分钟）"
# --load 把镜像载入本地 Docker（单架构才能 load；多架构必须 push）
docker buildx build --platform $Plat -t $ImageTag --load .
if ($LASTEXITCODE -ne 0) {
    Write-Error "构建失败。常见原因：1) QEMU 未就绪（Docker Desktop 重启即可）；2) 网络拉镜像失败（Dockerfile 已配国内源）"
    exit 1
}

Write-Host ""
Write-Host "==> 构建成功: $ImageTag" -ForegroundColor Green
docker images $ImageTag

if ($LoadOnly) {
    Write-Host ""
    Write-Host "==> 仅载入本地 Docker，未导出 tar。" -ForegroundColor Green
    Write-Host "    如需传到 ARM："
    Write-Host "      .\deploy\build-cross-arm64.ps1"
    exit 0
}

# 导出 tar.gz
if ([string]::IsNullOrEmpty($OutFile)) {
    $DistDir = Join-Path $Root "dist"
    if (-not (Test-Path $DistDir)) { New-Item -ItemType Directory -Path $DistDir | Out-Null }
    $OutFile = Join-Path $DistDir "data-hub-$Tag.tar.gz"
}

Write-Host ""
Write-Host "==> 导出镜像到 $OutFile ..." -ForegroundColor Cyan
# PowerShell 无 gzip 命令，先用 docker save -o 导出 .tar，再用 .NET GzipStream 压缩
$TarTmp = [System.IO.Path]::ChangeExtension($OutFile, "tar")
docker save -o $TarTmp $ImageTag
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $TarTmp)) {
    Write-Error "docker save 失败"
    exit 1
}
# .NET GzipStream 压缩
$src = [System.IO.File]::OpenRead($TarTmp)
$dst = [System.IO.File]::Create($OutFile)
$gz = New-Object System.IO.Compression.GzipStream($dst, [System.IO.Compression.CompressionLevel]::Optimal)
try { $src.CopyTo($gz) } finally { $gz.Close(); $src.Close(); $dst.Close() }
Remove-Item $TarTmp

$SizeKB = [math]::Round((Get-Item $OutFile).Length / 1KB, 1)
Write-Host ""
Write-Host "==> 完成" -ForegroundColor Green
Write-Host "    镜像: $ImageTag"
Write-Host "    产物: $OutFile ($SizeKB KB)"
Write-Host ""
Write-Host "传到 ARM 服务器并启动：" -ForegroundColor Yellow
Write-Host "  scp $OutFile user@<arm-ip>:/tmp/"
Write-Host "  ssh user@<arm-ip>"
Write-Host "  docker load < /tmp/$(Split-Path $OutFile -Leaf)"
Write-Host "  # 把 docker-compose.deploy.yml 也传过去（已在源码包 deploy\ 内），然后："
Write-Host "  docker compose -f docker-compose.deploy.yml up -d"
Write-Host ""
Write-Host "或直接用 deploy\build-image.sh --start 启动（会自动用 $ImageTag 镜像）。"
