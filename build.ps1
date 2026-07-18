# Build Subtap.exe -- a self-contained Windows app (no Python needed on the target).
# Regenerates the icon, then freezes subtap.py with PyInstaller into dist\Subtap.exe.
# Run this whenever you cut a release so the exe isn't a stale snapshot.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> Regenerating subtap.ico from the favicon design"
python make_icon.py

Write-Host "==> Freezing subtap.py -> dist\Subtap.exe (onefile, windowed)"
# --onefile   : single portable .exe
# --windowed  : no console window (like pythonw)
# --icon      : Explorer file icon on the exe itself
# --add-data  : bundle the .ico so webview.start() can set the taskbar/window icon at runtime
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name Subtap --icon subtap.ico --add-data "subtap.ico;." subtap.py

Write-Host ""
$exe = Join-Path $PSScriptRoot "dist\Subtap.exe"
if (Test-Path $exe) {
  $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
  Write-Host "Done: $exe ($mb MB)" -ForegroundColor Green
} else {
  Write-Error "Build finished but dist\Subtap.exe is missing"
}
