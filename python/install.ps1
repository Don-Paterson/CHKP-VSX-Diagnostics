# install.ps1 - Deploy vsx_diagnostics Python tool to C:\vsx_diagnostics on A-GUI
# Usage: irm https://raw.githubusercontent.com/Don-Paterson/CHKP-VSX-Diagnostics/main/python/install.ps1 | iex

$dest    = "C:\vsx_diagnostics"
$zip     = "$env:TEMP\vsx_diag.zip"
$extract = "$env:TEMP\vsx_diag_extract"

Write-Host "Downloading CHKP-VSX-Diagnostics..." -ForegroundColor Cyan
Invoke-WebRequest -Uri "https://github.com/Don-Paterson/CHKP-VSX-Diagnostics/archive/refs/heads/main.zip" `
    -OutFile $zip -UseBasicParsing

Expand-Archive -Path $zip -DestinationPath $extract -Force

if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Copy-Item "$extract\CHKP-VSX-Diagnostics-main\python" $dest -Recurse

Remove-Item $zip, $extract -Recurse -Force

Write-Host "Deployed to $dest" -ForegroundColor Green
Write-Host ""
Write-Host "Install dependencies:" -ForegroundColor Yellow
Write-Host "  pip install paramiko==3.5.1"
Write-Host ""
Write-Host "Run:" -ForegroundColor Yellow
Write-Host "  python C:\vsx_diagnostics\vsx_diagnostics.py --hosts 10.1.1.2 10.1.1.3 10.1.1.4"
