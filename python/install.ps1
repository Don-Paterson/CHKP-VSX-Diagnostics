# install.ps1
# Deploy vsx_diagnostics Python tool to C:\vsx_diagnostics on A-GUI
# Usage: irm https://raw.githubusercontent.com/Don-Paterson/CHKP-VSX-Diagnostics/main/python/install.ps1 | iex
#
# Self-bootstrapping: if no Python interpreter is found, installs Python 3.12
# via winget (with a direct-download fallback), then re-resolves and proceeds.

$dest        = "C:\vsx_diagnostics"
$zip         = "$env:TEMP\vsx_diag.zip"
$extract     = "$env:TEMP\vsx_diag_extract"
$src         = "$extract\CHKP-VSX-Diagnostics-main\python"
$pyVersion   = "3.12.7"   # version used for the direct-download fallback

# -----------------------------------------------------------------------
# Resolve a working Python launcher.
# Tries, in order: python, py -3, python3.
# Returns a hashtable with .Exe and .Args (the launcher prefix), or $null
# if no real interpreter is found. The WindowsApps Store-alias stub
# returns a non-zero exit code / no version, so we test by actually
# importing sys and reading the version string.
# -----------------------------------------------------------------------
function Resolve-Python {
    $candidates = @(
        @{ Exe = "python";  Args = @() },
        @{ Exe = "py";      Args = @("-3") },
        @{ Exe = "python3"; Args = @() }
    )
    foreach ($c in $candidates) {
        try {
            $ver = & $c.Exe @($c.Args + @("-c", "import sys; print(sys.version.split()[0])")) 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -match '^\d+\.\d+') {
                $c.Version = "$ver".Trim()
                return $c
            }
        } catch {
            # candidate not present / not runnable - try next
        }
    }
    return $null
}

# -----------------------------------------------------------------------
# Refresh PATH in the current session from the machine + user registry.
# A freshly installed Python prepends itself to PATH, but that change is
# not visible to this already-running process until we re-read it.
# -----------------------------------------------------------------------
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ";"
}

# -----------------------------------------------------------------------
# Install Python when none is present.
# Strategy: winget first (App Installer), direct python.org installer as
# fallback. Both put python on PATH (winget package does so by default;
# the .exe is run with PrependPath=1). Returns $true on a usable install.
# -----------------------------------------------------------------------
function Install-Python {
    # --- Attempt 1: winget ------------------------------------------------
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "        Installing Python $pyVersion via winget..." -ForegroundColor Yellow
        # --source winget bypasses the Skillable HTTPS-Inspection MITM that
        # breaks the default 'msstore' source on A-GUI lab images.
        & winget install --id Python.Python.3.12 -e --source winget `
            --accept-package-agreements --accept-source-agreements --silent 2>&1 | Out-Null

        Update-SessionPath
        if (Resolve-Python) { return $true }
        Write-Host "        winget install did not yield a usable interpreter - trying direct download." -ForegroundColor Yellow
    } else {
        Write-Host "        winget not available - using direct download." -ForegroundColor Yellow
    }

    # --- Attempt 2: direct python.org installer ---------------------------
    $url = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-amd64.exe"
    $out = "$env:TEMP\python-$pyVersion-amd64.exe"
    Write-Host "        Downloading $url ..." -ForegroundColor Yellow
    try {
        Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
    } catch {
        Write-Host "        Download failed: $_" -ForegroundColor Red
        return $false
    }

    Write-Host "        Running silent installer (PrependPath=1, Include_pip=1)..." -ForegroundColor Yellow
    # InstallAllUsers=1 needs elevation; default per-user install avoids that
    # and still prepends PATH, which is all Resolve-Python needs.
    $p = Start-Process -FilePath $out `
        -ArgumentList "/quiet PrependPath=1 Include_pip=1 Include_launcher=1" `
        -Wait -PassThru
    Remove-Item $out -Force -ErrorAction SilentlyContinue

    if ($p.ExitCode -ne 0) {
        Write-Host "        Installer exited with code $($p.ExitCode)." -ForegroundColor Red
        return $false
    }

    Update-SessionPath
    return [bool](Resolve-Python)
}

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
# Preserve hcp_archive (contains downloaded HCP reports - never delete)
# Update everything else in place using robocopy
# -----------------------------------------------------------------------
Write-Host "Step 2: Deploying to $dest..." -ForegroundColor Yellow

New-Item -ItemType Directory -Path $dest -Force | Out-Null

# robocopy: /E=include subdirs /IS=overwrite same /IT=overwrite tweaked
# /XD hcp_archive = skip that folder so archived reports survive updates
# /NFL /NDL /NJH /NJS = suppress file/dir/header/summary output
robocopy $src $dest /E /IS /IT /XD "$dest\hcp_archive" /NFL /NDL /NJH /NJS | Out-Null

# Ensure output dirs exist
New-Item -ItemType Directory -Path "$dest\reports"     -Force | Out-Null
New-Item -ItemType Directory -Path "$dest\hcp_archive" -Force | Out-Null

# Cleanup temp files
Remove-Item $zip     -Force -ErrorAction SilentlyContinue
Remove-Item $extract -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "        Done." -ForegroundColor Green

# -----------------------------------------------------------------------
# Resolve interpreter - bootstrap Python if missing
# -----------------------------------------------------------------------
$py = Resolve-Python
if (-not $py) {
    Write-Host "Step 2b: No Python found - bootstrapping..." -ForegroundColor Yellow
    if (Install-Python) {
        $py = Resolve-Python
        Write-Host "        Python installed." -ForegroundColor Green
    } else {
        Write-Host "        Automatic install failed - install Python manually and re-run." -ForegroundColor Red
    }
}
if ($py) {
    $pyDisplay = (@($py.Exe) + $py.Args) -join " "
    Write-Host "        Using Python: $pyDisplay (v$($py.Version))" -ForegroundColor Green
}

# -----------------------------------------------------------------------
# Step 3 - Install Python dependency
# -----------------------------------------------------------------------
Write-Host "Step 3: Installing paramiko..." -ForegroundColor Yellow

if (-not $py) {
    Write-Host "        Skipped - no Python interpreter available." -ForegroundColor Red
    Write-Host "        Install Python (tick 'Add to PATH'), then run manually:" -ForegroundColor Yellow
    Write-Host "          winget install --id Python.Python.3.12 -e --source winget" -ForegroundColor Yellow
    Write-Host "          py -m pip install paramiko==3.5.1" -ForegroundColor Yellow
} else {
    try {
        $pipOutput = & $py.Exe @($py.Args + @("-m", "pip", "install", "paramiko==3.5.1")) 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "        paramiko installed OK." -ForegroundColor Green
        } else {
            Write-Host "        pip returned an error - see below:" -ForegroundColor Red
            Write-Host $pipOutput
        }
    } catch {
        Write-Host "        Could not run pip: $_" -ForegroundColor Red
        Write-Host "        Run manually: $pyDisplay -m pip install paramiko==3.5.1" -ForegroundColor Yellow
    }
}

# -----------------------------------------------------------------------
# Step 4 - Verify
# -----------------------------------------------------------------------
Write-Host "Step 4: Verifying installation..." -ForegroundColor Yellow

if (-not $py) {
    Write-Host "        Skipped - no Python interpreter available." -ForegroundColor Red
} else {
    $testResult = & $py.Exe @($py.Args + @("-c", "import paramiko; print('paramiko', paramiko.__version__)")) 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "        $testResult" -ForegroundColor Green
    } else {
        Write-Host "        Verification failed: $testResult" -ForegroundColor Red
    }
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
# Use the resolved launcher in the usage hints so copy/paste works on
# machines where 'python' is not on PATH. Falls back to 'python' if none.
$run = if ($py) { (@($py.Exe) + $py.Args) -join " " } else { "python" }

Write-Host ""
Write-Host "Installation complete." -ForegroundColor Green
Write-Host ""
Write-Host "Deployed to  : $dest"
Write-Host "Reports      : $dest\reports"
Write-Host "HCP archives : $dest\hcp_archive"

if (-not $py) {
    Write-Host ""
    Write-Host "NOTE: Python was not available and could not be auto-installed." -ForegroundColor Yellow
    Write-Host "      Open a NEW PowerShell window after installing Python, then re-run." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Usage:" -ForegroundColor Cyan
Write-Host "  # First run (recommended - fetches NCS topology data):"
Write-Host "  $run $dest\vsx_diagnostics.py --fetch"
Write-Host ""
Write-Host "  # Subsequent runs:"
Write-Host "  $run $dest\vsx_diagnostics.py"
Write-Host ""
Write-Host "  # Custom cluster IPs:"
Write-Host "  $run $dest\vsx_diagnostics.py --hosts 10.1.1.2 10.1.1.3 10.1.1.4"
Write-Host ""
