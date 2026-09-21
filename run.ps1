# musicbot 本机启动脚本（PowerShell）
#
# 用法：
#   .\run.ps1              启动服务
#   .\run.ps1 -Check       只做配置自检，不启动
#
# 为什么用 .ps1 而不是 .bat：.bat 里写中文会被控制台代码页搞乱
# （旧项目踩过，最后是 .bat 只当纯 ASCII 壳、真正的逻辑放 .ps1）。

param(
    [switch]$Check,
    [string]$CondaEnv = 'musicdl'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

function Resolve-Python {
    # 1) 已激活的 conda 环境
    if ($env:CONDA_PREFIX) {
        $p = Join-Path $env:CONDA_PREFIX 'python.exe'
        if (Test-Path $p) { return $p }
    }
    # 2) 按 conda 的安装位置去找指定环境
    $candidates = @(
        (Join-Path $env:USERPROFILE "anaconda3\envs\$CondaEnv\python.exe"),
        (Join-Path $env:USERPROFILE "miniconda3\envs\$CondaEnv\python.exe"),
        "E:\Users\27417\anaconda3\envs\$CondaEnv\python.exe",
        "C:\ProgramData\anaconda3\envs\$CondaEnv\python.exe"
    )
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
    # 3) 兜底：PATH 上的 python
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "找不到 Python，请先用 conda 激活环境：conda activate $CondaEnv"
}

if (-not (Test-Path (Join-Path $root '.env'))) {
    Write-Host '[提示] 未找到 .env，将只使用系统环境变量。' -ForegroundColor Yellow
    Write-Host '       可执行：Copy-Item .env.example .env 后填写凭据。' -ForegroundColor Yellow
}

$python = Resolve-Python
Write-Host "[musicbot] Python: $python"

if ($Check) {
    & $python -c "from app.config import settings; m = settings.missing_items(); print('配置检查通过' if not m else '配置不完整:\n  ' + '\n  '.join(m))"
    exit $LASTEXITCODE
}

Write-Host '[musicbot] 启动中…（Ctrl+C 停止）'
& $python main.py
