<#
打包成单文件 exe。

用法：
    powershell -ExecutionPolicy Bypass -File scripts/build.ps1
    powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -Console   # 带控制台，方便看报错

产物：dist\FH6LotteryHelper.exe
#>
param(
    [switch]$Console,
    [switch]$SkipIcon
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "[1/4] 检查 Python 与依赖 ..." -ForegroundColor Cyan
$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) { throw "找不到 python，请先安装 Python 3.10+ 并加入 PATH" }
python -c "import PySide6, cv2, numpy, mss" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "缺少依赖，先执行：pip install -r requirements.txt"
}

if (-not $SkipIcon) {
    Write-Host "[2/4] 生成图标 ..." -ForegroundColor Cyan
    python scripts\make_icon.py
} else {
    Write-Host "[2/4] 跳过图标生成" -ForegroundColor DarkGray
}

Write-Host "[3/4] 清理旧产物 ..." -ForegroundColor Cyan
foreach ($d in @("build", "dist")) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}

Write-Host "[4/4] PyInstaller 打包中（第一次会比较慢）..." -ForegroundColor Cyan
$mode = if ($Console) { "--console" } else { "--windowed" }
# 图标要传绝对路径：spec 文件生成在 build\ 下，相对路径会相对 build\ 解析而找不到
$iconArg = if (Test-Path "assets\app.ico") {
    @("--icon", (Resolve-Path "assets\app.ico").Path)
} else {
    @()
}

$args = @(
    "--noconfirm", "--clean", "--onefile", $mode,
    "--name", "FH6LotteryHelper",
    "--distpath", "dist", "--workpath", "build", "--specpath", "build",
    "--exclude-module", "PySide6.QtWebEngineCore",
    "--exclude-module", "PySide6.QtWebEngineWidgets",
    "--exclude-module", "PySide6.QtQuick",
    "--exclude-module", "PySide6.QtQml",
    "--exclude-module", "PySide6.Qt3DCore",
    "--exclude-module", "PySide6.QtMultimedia",
    "--exclude-module", "PySide6.QtNetwork",
    "--exclude-module", "tkinter",
    "--exclude-module", "matplotlib",
    "--hidden-import", "PySide6.QtSvg",
    "main.py"
) + $iconArg
python -m PyInstaller @args
if ($LASTEXITCODE -ne 0) { throw "打包失败" }

$exe = Join-Path $root "dist\FH6LotteryHelper.exe"
if (-not (Test-Path $exe)) { throw "没有找到产物 $exe" }
$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "完成：$exe（$size MB）" -ForegroundColor Green
Write-Host "直接双击运行即可，不需要装 Python。" -ForegroundColor Green
