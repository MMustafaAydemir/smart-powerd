<#
.SYNOPSIS
    Builds standalone dist\smart-powerd.exe for Windows 10/11 via PyInstaller.

.DESCRIPTION
    Packages windows\smart_power_win.py together with shared pure-Python modules
    (smart_power_config.py, adaptive_brightness.py, display_refresh.py), PyQt6,
    psutil, and config.example.toml into a single-file, zero-console Windows
    executable at dist\smart-powerd.exe.

.PARAMETER SkipSmokeTest
    Skip running `dist\smart-powerd.exe --version` after building.
#>
[CmdletBinding()]
param(
    [switch]$SkipSmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

$EntryScript = Join-Path $RepoRoot "windows\smart_power_win.py"
$ExampleConfig = Join-Path $RepoRoot "config.example.toml"

if (-not (Test-Path -LiteralPath $EntryScript)) {
    throw "Entry script not found: $EntryScript"
}
if (-not (Test-Path -LiteralPath $ExampleConfig)) {
    throw "Bundled config template not found: $ExampleConfig"
}

$PyInstallerCmd = Get-Command pyinstaller -ErrorAction SilentlyContinue
$PythonCmd = Get-Command python -ErrorAction SilentlyContinue

$PyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "smart-powerd",
    "--paths", ".",
    "--add-data", "config.example.toml;.",
    "--hidden-import", "smart_power_config",
    "--hidden-import", "adaptive_brightness",
    "--hidden-import", "display_refresh",
    "--hidden-import", "psutil",
    "--hidden-import", "PyQt6.QtCore",
    "--hidden-import", "PyQt6.QtGui",
    "--hidden-import", "PyQt6.QtWidgets",
    "--hidden-import", "PyQt6.QtNetwork",
    "windows/smart_power_win.py"
)

Push-Location -LiteralPath $RepoRoot
try {
    Write-Host "==> Building standalone Windows executable (dist\smart-powerd.exe)..." -ForegroundColor Cyan
    # PyInstaller logs INFO messages to stderr; keep Continue during invocation and gate on $LASTEXITCODE.
    $PrevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($PyInstallerCmd) {
            & $PyInstallerCmd.Source @PyInstallerArgs
        }
        elseif ($PythonCmd) {
            & $PythonCmd.Source -m PyInstaller @PyInstallerArgs
        }
        else {
            throw "Neither 'pyinstaller' nor 'python' was found on PATH. Install PyInstaller via 'python -m pip install pyinstaller'."
        }
        $BuildExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PrevEap
    }

    if ($BuildExitCode -ne 0) {
        throw "PyInstaller failed with exit code $BuildExitCode."
    }

    $OutputExe = Join-Path $RepoRoot "dist\smart-powerd.exe"
    if (-not (Test-Path -LiteralPath $OutputExe)) {
        throw "Expected executable was not produced: $OutputExe"
    }

    $Item = Get-Item -LiteralPath $OutputExe
    if ($Item.Length -le 0) {
        throw "Built executable is empty: $OutputExe"
    }

    if (-not $SkipSmokeTest) {
        Write-Host "==> Running smoke test: dist\smart-powerd.exe --version..." -ForegroundColor Cyan
        $TmpOut = [System.IO.Path]::GetTempFileName()
        try {
            $Proc = Start-Process -FilePath $OutputExe -ArgumentList @("--version") -Wait -PassThru -RedirectStandardOutput $TmpOut
            if ($Proc.ExitCode -ne 0) {
                throw "Smoke test failed: smart-powerd.exe --version exited with code $($Proc.ExitCode)."
            }
            $VerOut = (Get-Content -LiteralPath $TmpOut -Raw -ErrorAction SilentlyContinue)
            if ($VerOut) {
                $VerOut = $VerOut.Trim()
            }
            if ($VerOut -notlike "smart-powerd*") {
                throw "Smoke test unexpected output: '$VerOut'"
            }
            Write-Host "    Output: $VerOut"
        }
        finally {
            if (Test-Path -LiteralPath $TmpOut) {
                Remove-Item -LiteralPath $TmpOut -Force -ErrorAction SilentlyContinue
            }
        }
    }

    $Hash = (Get-FileHash -LiteralPath $OutputExe -Algorithm SHA256).Hash.ToLowerInvariant()
    $SizeMB = [Math]::Round($Item.Length / 1MB, 2)
    Write-Host ""
    Write-Host "Built standalone executable successfully!" -ForegroundColor Green
    Write-Host "  Path   : $OutputExe"
    Write-Host "  Size   : $SizeMB MiB"
    Write-Host "  SHA256 : $Hash"
}
finally {
    Pop-Location
}
