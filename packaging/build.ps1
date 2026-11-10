$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path (Split-Path -Parent $Root) ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "找不到虚拟环境 Python: $Python"
}

Push-Location $Root
try {
    Get-Process -Name "Eolian" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
    # 默认不启用 UPX（体积大一点，但不会被 UPX 壳连累误报）。
    # 需要压体积时：把 upx.exe 放进 packaging\upx\，并设 EOLIAN_BUILD_UPX=1
    $UpXDir = Join-Path $PSScriptRoot "upx"
    $UpXArgs = @()
    $wantUpX = ($env:EOLIAN_BUILD_UPX -eq "1")
    if ($wantUpX -and (Test-Path -LiteralPath (Join-Path $UpXDir "upx.exe"))) {
        $UpXArgs = @("--upx-dir", $UpXDir)
        Write-Host ("UPX: " + (Join-Path $UpXDir "upx.exe"))
    } elseif ($wantUpX) {
        Write-Host "EOLIAN_BUILD_UPX=1 but packaging\upx\upx.exe missing; building uncompressed"
    } else {
        Write-Host "Building uncompressed (UPX disabled by default)"
    }
    & $Python -m PyInstaller --noconfirm --clean @UpXArgs "packaging\Eolian.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 构建失败，退出码 $LASTEXITCODE"
    }

    # onedir 产物：dist\Eolian\Eolian.exe + dist\Eolian\_internal\
    $DistDir = Join-Path $Root "dist\Eolian"
    $DistExe = Join-Path $DistDir "Eolian.exe"
    if (-not (Test-Path -LiteralPath $DistExe)) {
        throw "构建产物缺失: $DistExe"
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "USAGE.txt") `
        -Destination (Join-Path $DistDir "USAGE.txt") -Force

    # 清掉旧的单文件产物，避免误运行老版本
    $LegacyExe = Join-Path $Root "dist\Eolian.exe"
    if (Test-Path -LiteralPath $LegacyExe) {
        Remove-Item -LiteralPath $LegacyExe -Force
    }

    Compress-Archive -Path $DistDir `
        -DestinationPath (Join-Path $Root "dist\Eolian-portable.zip") `
        -CompressionLevel Optimal -Force
    Write-Host "构建完成: $DistExe"
}
finally {
    Pop-Location
}
