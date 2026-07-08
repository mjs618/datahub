# pack-deploy.ps1
# 在 Windows 上打出仅含运行时必需文件的干净源码包，用于传输到 ARM 服务器构建/运行。
#
# 用法：
#   .\deploy\pack-deploy.ps1                    # 默认输出 dist\data-hub-deploy-<时间戳>.zip
#   .\deploy\pack-deploy.ps1 -OutFile my.zip    # 自定义输出路径
#   .\deploy\pack-deploy.ps1 -Format tar        # 输出 .tar.gz（ARM Linux 上直接 docker build 更顺手）
#
# 打包内容：运行时 Python 模块 + Docker 构建文件 + 部署辅助文件
# 排除：所有 test_*/probe_*/scan_*/explore_* 脚本、缓存、状态文件、虚拟环境、.git

param(
    [string]$OutFile = "",
    [ValidateSet("zip", "tar")][string]$Format = "zip"
)

$ErrorActionPreference = "Stop"

# 项目根目录（脚本位于 deploy/ 下，根目录是上一级）
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

# 时间戳
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if ([string]::IsNullOrEmpty($OutFile)) {
    $DistDir = Join-Path $Root "dist"
    if (-not (Test-Path $DistDir)) { New-Item -ItemType Directory -Path $DistDir | Out-Null }
    if ($Format -eq "zip") { $Ext = "zip" } else { $Ext = "tar.gz" }
    $OutFile = Join-Path $DistDir "data-hub-deploy-$Stamp.$Ext"
}

# 运行时必需文件清单（Python 模块）
$RuntimeFiles = @(
    "main.py",
    "opcua_client.py",
    "history_api.py",
    "rt_db_client.py",
    "auth_client.py",
    "state_manager.py",
    "health_server.py",
    "web_ui.py",
    "config.py",
    "tasks_config.py",
    "requirements.txt"
)

# Docker / 部署文件
$BuildFiles = @(
    "Dockerfile",
    "docker-compose.yml",
    "docker-bake.hcl",
    ".dockerignore"
)

# 校验必需文件存在
Write-Host "==> 校验运行时文件..." -ForegroundColor Cyan
$Missing = @()
foreach ($f in ($RuntimeFiles + $BuildFiles)) {
    if (-not (Test-Path (Join-Path $Root $f))) { $Missing += $f }
}
if ($Missing.Count -gt 0) {
    Write-Error "缺少必需文件: $($Missing -join ', ')"
    exit 1
}

# 准备临时打包目录
$Stage = Join-Path $env:TEMP "data-hub-stage-$Stamp"
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Path $Stage | Out-Null

Write-Host "==> 复制运行时文件到暂存目录 $Stage ..." -ForegroundColor Cyan
foreach ($f in $RuntimeFiles) {
    Copy-Item (Join-Path $Root $f) -Destination $Stage -Force
}
foreach ($f in $BuildFiles) {
    Copy-Item (Join-Path $Root $f) -Destination $Stage -Force
}

# 连同 deploy/ 目录一并打入（含 systemd unit、deploy compose、本脚本），便于服务器上参考
$DeployDir = Join-Path $Stage "deploy"
New-Item -ItemType Directory -Path $DeployDir | Out-Null
Get-ChildItem (Join-Path $Root "deploy") -File | ForEach-Object {
    Copy-Item $_.FullName -Destination $DeployDir -Force
}

Write-Host "==> 打包为 $Format ..." -ForegroundColor Cyan
if ($Format -eq "zip") {
    Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $OutFile -Force
} else {
    # tar.gz：Windows 10+ 自带 tar
    $tar = Get-Command tar -ErrorAction SilentlyContinue
    if (-not $tar) {
        Write-Error "未找到 tar 命令，请改用 -Format zip，或安装 tar（Windows 10 1803+ 自带）。"
        exit 1
    }
    # 在 TEMP 上一层打包，保留 data-hub-stage-<stamp>/ 顶层目录
    $Parent = Split-Path $Stage -Parent
    $Leaf = Split-Path $Stage -Leaf
    & tar -czf $OutFile -C $Parent $Leaf
}

# 清理暂存
Remove-Item $Stage -Recurse -Force

$Size = (Get-Item $OutFile).Length / 1KB
Write-Host ""
Write-Host "==> 打包完成" -ForegroundColor Green
Write-Host "    输出: $OutFile"
Write-Host "    大小: $([math]::Round($Size, 1)) KB"
Write-Host ""
Write-Host "传输到 ARM 服务器后：" -ForegroundColor Yellow
Write-Host "  方式A (Docker 本机构建):"
Write-Host "    unzip data-hub-deploy-$Stamp.zip -d data-hub && cd data-hub"
Write-Host "    docker compose up -d --build"
Write-Host "  方式C (裸机 Python):"
Write-Host "    unzip data-hub-deploy-$Stamp.zip -d data-hub && cd data-hub"
Write-Host "    pip3 install -r requirements.txt"
Write-Host "    python3 main.py   # 或用 deploy/data-hub.service 做 systemd 守护"
