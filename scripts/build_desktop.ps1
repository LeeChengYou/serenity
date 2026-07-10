# scripts/build_desktop.ps1
# Serenity 桌面版打包腳本（PowerShell 5.1 語法，無 &&）
# 用法：PowerShell -File scripts\build_desktop.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path $PSScriptRoot -Parent

Write-Host "=== Serenity Desktop Build ===" -ForegroundColor Cyan
Write-Host "Repo: $RepoRoot"

# 1. 檢查 pyinstaller 是否已安裝
$piCheck = python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null
if (-not $?) {
    Write-Host ""
    Write-Host "錯誤：PyInstaller 未安裝。" -ForegroundColor Red
    Write-Host "請執行：pip install pyinstaller>=6" -ForegroundColor Yellow
    exit 1
}
Write-Host "PyInstaller $piCheck OK"

# 2. 檢查 pywebview 是否已安裝
$wvCheck = python -c "import webview; print(webview.__version__)" 2>$null
if (-not $?) {
    Write-Host ""
    Write-Host "錯誤：pywebview 未安裝。" -ForegroundColor Red
    Write-Host "請執行：pip install pywebview>=5" -ForegroundColor Yellow
    exit 1
}
Write-Host "pywebview $wvCheck OK"

# 3. 執行 PyInstaller
Write-Host ""
Write-Host "執行 PyInstaller..." -ForegroundColor Cyan
$specPath = Join-Path $RepoRoot "packaging\desktop.spec"
pyinstaller $specPath --noconfirm

if (-not $?) {
    Write-Host "打包失敗！" -ForegroundColor Red
    exit 1
}

# 4. 輸出結果
$distDir = Join-Path $RepoRoot "dist\Serenity"
if (Test-Path $distDir) {
    $fileCount = (Get-ChildItem $distDir -Recurse -File).Count
    Write-Host ""
    Write-Host "=== 打包完成 ===" -ForegroundColor Green
    Write-Host "輸出目錄：$distDir"
    Write-Host "檔案數量：$fileCount"
    Write-Host "執行檔：$distDir\Serenity.exe"
} else {
    Write-Host "警告：找不到 dist\Serenity 目錄" -ForegroundColor Yellow
}
