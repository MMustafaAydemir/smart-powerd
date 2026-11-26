<#
.SYNOPSIS
    Installs smart-powerd for Windows 10/11 into %LOCALAPPDATA%\smart-powerd.

.DESCRIPTION
    Supports two install modes automatically:
      1. Standalone Executable (zero Python/pip required): if smart-powerd.exe is
         present alongside install.ps1, at the release archive root, or in dist\,
         installs %LOCALAPPDATA%\smart-powerd\smart-powerd.exe directly and
         registers it for logon autostart.
      2. Python Source Fallback: verifies Python 3.11+, installs shared pure-Python
         modules and smart_power_win.py into %LOCALAPPDATA%\smart-powerd, creates
         an isolated .venv with PyQt6 + psutil (skip with -SkipPip), and registers
         silent pythonw.exe logon autostart.

    Copies config.example.toml to config.toml only if config.toml does not yet exist,
    and launches smart-powerd immediately unless -NoLaunch is specified.

.PARAMETER FromSource
    Force Python source installation even if smart-powerd.exe is present.

.PARAMETER ExePath
    Explicit path to a prebuilt smart-powerd.exe binary.

.PARAMETER SkipPip
    (Source mode only) Skip virtual environment creation and pip package installation.

.PARAMETER NoVenv
    (Source mode only) Install requirements into user site-packages instead of .venv.

.PARAMETER NoAutostart
    Do not register HKCU:\Software\Microsoft\Windows\CurrentVersion\Run logon autostart.

.PARAMETER NoLaunch
    Install files and autostart without launching smart-powerd immediately.

.PARAMETER InstallDir
    Custom installation directory (default: %LOCALAPPDATA%\smart-powerd).

.PARAMETER PythonExe
    (Source mode only) Explicit path to a Python 3.11+ executable.
#>
[CmdletBinding()]
param(
    [switch]$FromSource,
    [string]$ExePath = "",
    [switch]$SkipPip,
    [switch]$NoVenv,
    [switch]$NoAutostart,
    [switch]$NoLaunch,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "smart-powerd"),
    [string]$PythonExe = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-Python311 {
    param([string]$ExplicitExe)

    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($ExplicitExe)) {
        $candidates += ,@($ExplicitExe, @())
    }
    else {
        $candidates += ,@("py", @("-3"))
        $candidates += ,@("python", @())
        $candidates += ,@("python3", @())
    }

    $probeCode = "import sys; print(sys.executable); print(f'{sys.version_info.major}.{sys.version_info.minor}'); sys.exit(0 if sys.version_info >= (3, 11) else 1)"

    foreach ($cand in $candidates) {
        $cmd = $cand[0]
        $prefixArgs = $cand[1]
        try {
            $cmdInfo = Get-Command $cmd -ErrorAction SilentlyContinue
            if (-not $cmdInfo -and -not (Test-Path $cmd)) {
                continue
            }
            $allArgs = @()
            $allArgs += $prefixArgs
            $allArgs += @("-c", $probeCode)
            $out = & $cmd @allArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $lines = @($out | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
                if ($lines.Count -ge 2) {
                    return @{
                        Executable = $lines[0].Trim()
                        Version    = $lines[1].Trim()
                    }
                }
            }
        }
        catch {
            continue
        }
    }

    throw "Python 3.11 or newer is required for source installs. Install it via 'winget install Python.Python.3.12' or download the standalone smart-powerd.exe release."
}

function Stop-RunningSmartPower {
    $stoppedAny = $false
    try {
        $exeProcs = Get-Process -Name "smart-powerd" -ErrorAction SilentlyContinue
        foreach ($ep in $exeProcs) {
            Write-Host "Stopping running smart-powerd.exe (PID $($ep.Id))..."
            Stop-Process -Id $ep.Id -Force -ErrorAction SilentlyContinue
            $stoppedAny = $true
        }
    }
    catch {
        # Non-fatal
    }

    try {
        $procs = Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                ($_.Name -eq "smart-powerd.exe") -or
                ($_.CommandLine -and $_.CommandLine -like "*smart_power_win.py*")
            }
        foreach ($p in $procs) {
            Write-Host "Stopping running smart-powerd process (PID $($p.ProcessId))..."
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            $stoppedAny = $true
        }
    }
    catch {
        # Non-fatal if process enumeration is restricted
    }

    if ($stoppedAny) {
        Start-Sleep -Milliseconds 600
    }
}

function Find-BundledExe {
    param(
        [string]$ExplicitPath,
        [string]$ScriptDir,
        [string]$RepoRoot
    )

    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        if (-not (Test-Path -LiteralPath $ExplicitPath)) {
            throw "Specified -ExePath not found: $ExplicitPath"
        }
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }

    $candidates = @(
        (Join-Path $ScriptDir "smart-powerd.exe"),
        (Join-Path $RepoRoot "smart-powerd.exe"),
        (Join-Path $RepoRoot "dist\smart-powerd.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            return (Resolve-Path -LiteralPath $c).Path
        }
    }
    return $null
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

$DetectedExe = $null
if (-not $FromSource) {
    $DetectedExe = Find-BundledExe -ExplicitPath $ExePath -ScriptDir $ScriptDir -RepoRoot $RepoRoot
}

$ExampleCandidates = @(
    (Join-Path $RepoRoot "config.example.toml"),
    (Join-Path $ScriptDir "config.example.toml")
)
$ExampleSrc = $null
foreach ($ec in $ExampleCandidates) {
    if (Test-Path -LiteralPath $ec) {
        $ExampleSrc = $ec
        break
    }
}

$UninstallSrc = Join-Path $ScriptDir "uninstall.ps1"
$ExampleDst = Join-Path $InstallDir "config.example.toml"
$UserConfigDst = Join-Path $InstallDir "config.toml"
$CtlCmdPath = Join-Path $InstallDir "smart-powerctl.cmd"

Stop-RunningSmartPower
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

if ($DetectedExe) {
    Write-Host "==> Installing standalone smart-powerd.exe into $InstallDir (zero Python required)..." -ForegroundColor Cyan
    $TargetExe = Join-Path $InstallDir "smart-powerd.exe"
    Copy-Item -LiteralPath $DetectedExe -Destination $TargetExe -Force

    if ($ExampleSrc) {
        Copy-Item -LiteralPath $ExampleSrc -Destination $ExampleDst -Force
        if (-not (Test-Path -LiteralPath $UserConfigDst)) {
            Copy-Item -LiteralPath $ExampleSrc -Destination $UserConfigDst
            Write-Host "    Created default config at $UserConfigDst"
        }
        else {
            Write-Host "    Preserved existing config at $UserConfigDst"
        }
    }

    if (Test-Path -LiteralPath $UninstallSrc) {
        Copy-Item -LiteralPath $UninstallSrc -Destination (Join-Path $InstallDir "uninstall.ps1") -Force
    }

    $CtlCmdContent = "@echo off`r`n`"$TargetExe`" %*`r`n"
    Set-Content -LiteralPath $CtlCmdPath -Value $CtlCmdContent -Encoding Ascii

    if (-not $NoAutostart) {
        Write-Host "==> Registering Windows logon autostart (HKCU Run)..." -ForegroundColor Cyan
        $RunKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        if (-not (Test-Path -LiteralPath $RunKeyPath)) {
            New-Item -Path $RunKeyPath -Force | Out-Null
        }
        $AutostartCmd = "`"$TargetExe`" --tray"
        Set-ItemProperty -Path $RunKeyPath -Name "SmartPowerd" -Value $AutostartCmd
        Write-Host "    Registered HKCU\...\Run\SmartPowerd -> $AutostartCmd"
    }

    if (-not $NoLaunch) {
        Write-Host "==> Launching smart-powerd.exe tray..." -ForegroundColor Cyan
        Start-Process -FilePath $TargetExe -ArgumentList @("--tray") -WorkingDirectory $InstallDir
    }

    Write-Host ""
    Write-Host "smart-powerd for Windows (standalone) installed successfully!" -ForegroundColor Green
    Write-Host "  Executable  : $TargetExe"
    Write-Host "  Config File : $UserConfigDst"
    Write-Host "  Status CLI  : `"$CtlCmdPath`" --status"
    Write-Host "  Uninstall   : powershell -ExecutionPolicy Bypass -File `"$(Join-Path $InstallDir 'uninstall.ps1')`""
    exit 0
}

# ---------------------------------------------------------------------------
# Python Source Fallback Mode
# ---------------------------------------------------------------------------

$RequiredFiles = @(
    @{ Src = (Join-Path $RepoRoot "smart_power_config.py"); Dst = "smart_power_config.py" },
    @{ Src = (Join-Path $RepoRoot "adaptive_brightness.py"); Dst = "adaptive_brightness.py" },
    @{ Src = (Join-Path $RepoRoot "display_refresh.py"); Dst = "display_refresh.py" },
    @{ Src = (Join-Path $ScriptDir "smart_power_win.py"); Dst = "smart_power_win.py" },
    @{ Src = (Join-Path $RepoRoot "requirements-windows.txt"); Dst = "requirements-windows.txt" }
)

foreach ($item in $RequiredFiles) {
    if (-not (Test-Path -LiteralPath $item.Src)) {
        throw "Required source file missing: $($item.Src). Run install.ps1 from the smart-powerd repository or extracted Windows release archive."
    }
}

Write-Host "==> Checking Python 3.11+..." -ForegroundColor Cyan
$pyInfo = Resolve-Python311 -ExplicitExe $PythonExe
$BasePython = $pyInfo.Executable
Write-Host "    Found Python $($pyInfo.Version) at $BasePython"

Write-Host "==> Installing Python source files into $InstallDir..." -ForegroundColor Cyan
foreach ($item in $RequiredFiles) {
    Copy-Item -LiteralPath $item.Src -Destination (Join-Path $InstallDir $item.Dst) -Force
}

if ($ExampleSrc) {
    Copy-Item -LiteralPath $ExampleSrc -Destination $ExampleDst -Force
    if (-not (Test-Path -LiteralPath $UserConfigDst)) {
        Copy-Item -LiteralPath $ExampleSrc -Destination $UserConfigDst
        Write-Host "    Created default config at $UserConfigDst"
    }
    else {
        Write-Host "    Preserved existing config at $UserConfigDst"
    }
}

if (Test-Path -LiteralPath $UninstallSrc) {
    Copy-Item -LiteralPath $UninstallSrc -Destination (Join-Path $InstallDir "uninstall.ps1") -Force
}

$VenvDir = Join-Path $InstallDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
$ReqFile = Join-Path $InstallDir "requirements-windows.txt"

$RuntimePython = $BasePython
if (-not $SkipPip) {
    if (-not $NoVenv) {
        if (-not (Test-Path -LiteralPath $VenvPython)) {
            Write-Host "==> Creating virtual environment at $VenvDir..." -ForegroundColor Cyan
            & $BasePython -m venv $VenvDir
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to create Python virtual environment."
            }
        }
        Write-Host "==> Installing Windows dependencies (PyQt6, psutil)..." -ForegroundColor Cyan
        & $VenvPython -m pip install --upgrade --quiet -r $ReqFile
        if ($LASTEXITCODE -ne 0) {
            throw "pip install failed. Re-run with internet access or use -SkipPip if dependencies are already installed."
        }
        $RuntimePython = $VenvPython
    }
    else {
        Write-Host "==> Installing Windows dependencies into user site-packages..." -ForegroundColor Cyan
        & $BasePython -m pip install --user --upgrade --quiet -r $ReqFile
        if ($LASTEXITCODE -ne 0) {
            throw "pip install --user failed."
        }
        $RuntimePython = $BasePython
    }
}
elseif (Test-Path -LiteralPath $VenvPython) {
    $RuntimePython = $VenvPython
}

# Resolve pythonw.exe so tray starts silently with zero console window flash
$RuntimePythonDir = Split-Path -Parent $RuntimePython
$CandidatePythonw = Join-Path $RuntimePythonDir "pythonw.exe"
if ((Test-Path -LiteralPath $VenvPythonw) -and ($RuntimePython -eq $VenvPython)) {
    $LaunchPythonw = $VenvPythonw
}
elseif (Test-Path -LiteralPath $CandidatePythonw) {
    $LaunchPythonw = $CandidatePythonw
}
else {
    $LaunchPythonw = $RuntimePython
}

$TargetScript = Join-Path $InstallDir "smart_power_win.py"

$CtlCmdContent = "@echo off`r`n`"$RuntimePython`" `"$TargetScript`" %*`r`n"
Set-Content -LiteralPath $CtlCmdPath -Value $CtlCmdContent -Encoding Ascii

if (-not $NoAutostart) {
    Write-Host "==> Registering Windows logon autostart (HKCU Run)..." -ForegroundColor Cyan
    $RunKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    if (-not (Test-Path -LiteralPath $RunKeyPath)) {
        New-Item -Path $RunKeyPath -Force | Out-Null
    }
    $AutostartCmd = "`"$LaunchPythonw`" `"$TargetScript`" --tray"
    Set-ItemProperty -Path $RunKeyPath -Name "SmartPowerd" -Value $AutostartCmd
    Write-Host "    Registered HKCU\...\Run\SmartPowerd -> $AutostartCmd"
}

if (-not $NoLaunch) {
    Write-Host "==> Launching smart-powerd tray..." -ForegroundColor Cyan
    Start-Process -FilePath $LaunchPythonw -ArgumentList @("`"$TargetScript`"", "--tray") -WorkingDirectory $InstallDir -WindowStyle Hidden
}

Write-Host ""
Write-Host "smart-powerd for Windows installed successfully!" -ForegroundColor Green
Write-Host "  Install Dir : $InstallDir"
Write-Host "  Config File : $UserConfigDst"
Write-Host "  Status CLI  : `"$CtlCmdPath`" --status"
Write-Host "  Uninstall   : powershell -ExecutionPolicy Bypass -File `"$(Join-Path $InstallDir 'uninstall.ps1')`""
