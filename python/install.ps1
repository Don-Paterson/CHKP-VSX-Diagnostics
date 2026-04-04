# install.ps1
# Deploy vsx_diagnostics Python tool to C:\vsx_diagnostics on A-GUI
# Usage: irm https://raw.githubusercontent.com/Don-Paterson/CHKP-VSX-Diagnostics/main/python/install.ps1 | iex

$dest     = "C:\vsx_diagnostics"
$zip      = "$env:TEMP\vsx_diag.zip"
$extract  = "$env:TEMP\vsx_diag_extract"

# -----------------------------------------------------------------------
# Step 1 - Download and extract
# -----------------------------------------------------------------------
Write-Host ""
Write-Host "VSX Diagnostics - Installer" -ForegroundColor Cyan
Write-Host "===========================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Step 1: Downloading from GitHub..." -ForegroundColor Yellow

Invoke-WebRequest `
    -Uri "https://github.com/Don-Paterson/CHKP-VSX-Diagnostics/archive/refs/heads/main.zip" `
    -OutFile $zip -UseBasicParsing

Write-Host "        Extracting..." -ForegroundColor Yellow
Expand-Archive -Path $zip -DestinationPath $extract -Force

# -----------------------------------------------------------------------
# Step 2 - Deploy files
# -----------------------------------------------------------------------
Write-Host "Step 2: Deploying to $dest..." -ForegroundColor Yellow

if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Copy-Item "$extract\CHKP-VSX-Diagnostics-main\python" $dest -Recurse

# Create output directories
New-Item -ItemType Directory -Path "$dest\reports"     -Force | Out-Null
New-Item -ItemType Directory -Path "$dest\hcp_archive" -Force | Out-Null

# Cleanup temp files
Remove-Item $zip, $extract -Recurse -Force

Write-Host "        Done." -ForegroundColor Green

# -----------------------------------------------------------------------
# Step 3 - Install Python dependency
# -----------------------------------------------------------------------
Write-Host "Step 3: Installing paramiko..." -ForegroundColor Yellow

try {
    $pipOutput = & python -m pip install "paramiko==3.5.1" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "        paramiko installed OK." -ForegroundColor Green
    } else {
        Write-Host "        pip returned an error - see below:" -ForegroundColor Red
        Write-Host $pipOutput
    }
} catch {
    Write-Host "        Could not run pip - is Python in PATH?" -ForegroundColor Red
    Write-Host "        Run manually: pip install paramiko==3.5.1" -ForegroundColor Yellow
}

# -----------------------------------------------------------------------
# Step 4 - Verify
# -----------------------------------------------------------------------
Write-Host "Step 4: Verifying installation..." -ForegroundColor Yellow

$testResult = & python -c "import paramiko; print('paramiko', paramiko.__version__)" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "        $testResult" -ForegroundColor Green
} else {
    Write-Host "        Verification failed: $testResult" -ForegroundColor Red
}

$entryPoint = "$dest\vsx_diagnostics.py"
if (Test-Path $entryPoint) {
    Write-Host "        Entry point found: $entryPoint" -ForegroundColor Green
} else {
    Write-Host "        WARNING: $entryPoint not found - check deployment" -ForegroundColor Red
}

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
Write-Host ""
Write-Host "Installation complete." -ForegroundColor Green
Write-Host ""
Write-Host "Deployed to  : $dest"
Write-Host "Reports      : $dest\reports"
Write-Host "HCP archives : $dest\hcp_archive"
Write-Host ""
Write-Host "Usage:" -ForegroundColor Cyan
Write-Host "  # First run (recommended - fetches NCS topology data):"
Write-Host "  python $dest\vsx_diagnostics.py --fetch"
Write-Host ""
Write-Host "  # Subsequent runs:"
Write-Host "  python $dest\vsx_diagnostics.py"
Write-Host ""
Write-Host "  # Custom cluster IPs:"
Write-Host "  python $dest\vsx_diagnostics.py --hosts 10.1.1.2 10.1.1.3 10.1.1.4"
Write-Host ""
