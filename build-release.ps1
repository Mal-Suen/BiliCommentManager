# build-release.ps1
# BiliCommentManager 发布包自动组装脚本
#
# 根治「发布包/ 与 zip 落后于根目录源码」的版本漂移问题：
# 发布包/ 与 *.zip 按 .gitignore 设计为本地构建产物（不进版本控制），
# 本脚本从根目录源码自动组装并校验一致性，保证发布物永远等于当前源码。
#
# 用法：在仓库根目录运行  powershell -ExecutionPolicy Bypass -File build-release.ps1
# 产物：发布包/（5 个文件）与 BiliCommentManager_源码发布包.zip

$ErrorActionPreference = 'Stop'

# 仓库根目录（脚本所在目录）
$root = $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }
Set-Location $root

# 发布包需要的文件（相对根目录）
$files = @(
    'bili_comment_manager.py',
    'bili_comment_gui.py',
    'README.md',
    'requirements.txt',
    '双击启动.bat'
)

$pkgDir = Join-Path $root '发布包'
$zipPath = Join-Path $root 'BiliCommentManager_源码发布包.zip'

Write-Host "== 组装发布包：$pkgDir" -ForegroundColor Cyan

# 1. 校验源文件存在
$missing = $files | Where-Object { -not (Test-Path (Join-Path $root $_)) }
if ($missing) {
    Write-Host "错误：源文件缺失：$($missing -join ', ')" -ForegroundColor Red
    exit 1
}

# 2. 复制到发布包/（先建目录）
New-Item -ItemType Directory -Path $pkgDir -Force | Out-Null
foreach ($f in $files) {
    Copy-Item -Path (Join-Path $root $f) -Destination (Join-Path $pkgDir $f) -Force
}

# 3. 校验发布包与根目录一致（防复制失败 / 编码损坏）
$mismatch = @()
foreach ($f in $files) {
    $srcHash = (Get-FileHash (Join-Path $root $f)).Hash
    $dstHash = (Get-FileHash (Join-Path $pkgDir $f)).Hash
    if ($srcHash -ne $dstHash) { $mismatch += $f }
}
if ($mismatch) {
    Write-Host "错误：发布包与根目录不一致：$($mismatch -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "发布包已组装，5 个文件与根目录一致。" -ForegroundColor Green

# 4. 打 zip（临时目录组装，避免把多余文件打进去）
$tmp = Join-Path $env:TEMP ("bcm_zip_" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    foreach ($f in $files) {
        Copy-Item -Path (Join-Path $root $f) -Destination (Join-Path $tmp $f) -Force
    }
    # 删除旧 zip 再打新包，避免残留条目
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path (Join-Path $tmp '*') -DestinationPath $zipPath -Force

    # 5. 校验 zip 内容与发布包一致
    $checkDir = Join-Path $env:TEMP ("bcm_zipcheck_" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $checkDir | Out-Null
    try {
        Expand-Archive -Path $zipPath -DestinationPath $checkDir -Force
        $zipMismatch = @()
        foreach ($f in $files) {
            $zipHash = (Get-FileHash (Join-Path $checkDir $f)).Hash
            $pkgHash = (Get-FileHash (Join-Path $pkgDir $f)).Hash
            if ($zipHash -ne $pkgHash) { $zipMismatch += $f }
        }
        if ($zipMismatch) {
            Write-Host "错误：zip 与发布包不一致：$($zipMismatch -join ', ')" -ForegroundColor Red
            exit 1
        }
    } finally {
        Remove-Item $checkDir -Recurse -Force -ErrorAction SilentlyContinue
    }
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

$sizeKB = [math]::Round((Get-Item $zipPath).Length / 1KB, 1)
Write-Host "== 完成：$zipPath（${sizeKB}KB），发布包与 zip 均已与根目录源码一致。" -ForegroundColor Green
Write-Host "提示：发布包/ 与 *.zip 按 .gitignore 不进版本控制，属构建产物；" -ForegroundColor Yellow
Write-Host "     每次发布前运行本脚本即可，无需手动同步。" -ForegroundColor Yellow