# Apply staged build: dist\_next\Eolian -> dist\Eolian
# Usage: close Eolian completely, then run this script.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Root "dist"
$Live = Join-Path $Dist "Eolian"
$Next = Join-Path $Dist "_next\Eolian"

$running = @(Get-Process -Name "Eolian" -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    $pids = ($running | ForEach-Object { $_.Id }) -join ", "
    throw "Eolian is still running (pid: $pids). Close it first."
}
if (-not (Test-Path -LiteralPath (Join-Path $Next "Eolian.exe"))) {
    throw "No staged build found: $Next"
}

if (Test-Path -LiteralPath $Live) {
    Remove-Item -LiteralPath $Live -Recurse -Force
}
Move-Item -LiteralPath $Next -Destination $Live
Remove-Item -LiteralPath (Join-Path $Dist "_next") -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item -LiteralPath (Join-Path $Root "packaging\USAGE.txt") `
    -Destination (Join-Path $Live "USAGE.txt") -Force
Compress-Archive -Path $Live -DestinationPath (Join-Path $Dist "Eolian-portable.zip") `
    -CompressionLevel Optimal -Force
Write-Host ("Updated: " + (Join-Path $Live "Eolian.exe"))
