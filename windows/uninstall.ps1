<#
.SYNOPSIS
    Uninstalls smart-powerd for Windows 10/11 and restores baseline power & display settings.

.DESCRIPTION
    - Stops any running smart_power_win.py process.
    - Removes the Windows logon autostart entry (HKCU Run and any Startup shortcut).
    - Restores Balanced power overlay, baseline Wi-Fi Power Saving / PCIe ASPM / USB Selective
      Suspend settings from baseline.json, and native highest display refresh rate.
    - Removes installed files in %LOCALAPPDATA%\smart-powerd (preserving config.toml unless
      -Purge is specified).

.PARAMETER Purge
    Remove the entire %LOCALAPPDATA%\smart-powerd directory including user config.toml.

.PARAMETER InstallDir
    Custom installation directory (default: %LOCALAPPDATA%\smart-powerd).
#>
[CmdletBinding()]
param(
    [switch]$Purge,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "smart-powerd")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$OVERLAY_BALANCED = "00000000-0000-0000-0000-000000000000"
$SCHEME_BALANCED  = "381b4222-f694-41f0-9685-ff5bb260df2e"

$SUBGROUP_WIFI    = "19cbb8fa-5279-450e-9fac-8a3d5fedd0c1"
$SETTING_WIFI_PS  = "12bbebe6-58d6-4636-95bb-3217ef867c1a"

$SUBGROUP_PCIE    = "501a4d13-42af-4429-9fd1-a8218c268e20"
$SETTING_PCIE_ASPM = "ee12f906-d277-404b-b6da-e5fa1a576df5"

$SUBGROUP_USB     = "2a737441-1930-4402-8d77-b2bebba308a3"
$SETTING_USB_SUSP = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"

function Restore-PowerOverlayBalanced {
    try {
        if (-not ("SmartPowerWin32Restore" -as [type])) {
            Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class SmartPowerWin32Restore {
    [DllImport("powrprof.dll", EntryPoint = "PowerSetActiveOverlayScheme")]
    public static extern uint PowerSetActiveOverlayScheme(ref Guid OverlaySchemeGuid);
}
"@ -ErrorAction SilentlyContinue
        }
        $guid = [Guid]::Parse($OVERLAY_BALANCED)
        [void][SmartPowerWin32Restore]::PowerSetActiveOverlayScheme([ref]$guid)
    }
    catch {
        # Fallback to classic Balanced power scheme
        & powercfg /setactive $SCHEME_BALANCED 2>$null | Out-Null
    }
}

Write-Host "==> Stopping running smart-powerd processes..." -ForegroundColor Cyan
try {
    $procs = Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*smart_power_win.py*" }
    foreach ($p in $procs) {
        Write-Host "    Stopping PID $($p.ProcessId)..."
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($procs) {
        Start-Sleep -Milliseconds 500
    }
}
catch {
    # Non-fatal
}

Write-Host "==> Removing Windows logon autostart..." -ForegroundColor Cyan
$RunKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if (Test-Path -LiteralPath $RunKeyPath) {
    $existing = Get-ItemProperty -Path $RunKeyPath -Name "SmartPowerd" -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        Remove-ItemProperty -Path $RunKeyPath -Name "SmartPowerd" -ErrorAction SilentlyContinue
        Write-Host "    Removed HKCU\...\Run\SmartPowerd"
    }
}

$StartupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "SmartPowerd.lnk"
if (Test-Path -LiteralPath $StartupShortcut) {
    Remove-Item -LiteralPath $StartupShortcut -Force -ErrorAction SilentlyContinue
    Write-Host "    Removed Startup shortcut $StartupShortcut"
}

Write-Host "==> Restoring Balanced overlay, baseline power settings, and native refresh rate..." -ForegroundColor Cyan
$TargetScript = Join-Path $InstallDir "smart_power_win.py"
$VenvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
$RestoredByPython = $false

if (Test-Path -LiteralPath $TargetScript) {
    $pyCandidates = @()
    if (Test-Path -LiteralPath $VenvPython) {
        $pyCandidates += $VenvPython
    }
    $sysPy = Get-Command python -ErrorAction SilentlyContinue
    if ($sysPy) {
        $pyCandidates += $sysPy.Source
    }
    foreach ($py in $pyCandidates) {
        try {
            & $py $TargetScript --restore 2>$null
            if ($LASTEXITCODE -eq 0) {
                $RestoredByPython = $true
                break
            }
        }
        catch {
            continue
        }
    }
}

# Guaranteed native PowerShell restoration fallback (even if Python/venv is absent)
$BaselinePath = Join-Path $InstallDir "baseline.json"
$WifiAc = 0; $WifiDc = 0
$PcieAc = 1; $PcieDc = 1
$UsbAc  = 1; $UsbDc  = 1

if (Test-Path -LiteralPath $BaselinePath) {
    try {
        $base = Get-Content -LiteralPath $BaselinePath -Raw | ConvertFrom-Json
        if ($base.wifi_ps) {
            if ($null -ne $base.wifi_ps.ac) { $WifiAc = [int]$base.wifi_ps.ac }
            if ($null -ne $base.wifi_ps.dc) { $WifiDc = [int]$base.wifi_ps.dc }
        }
        if ($base.pcie_aspm) {
            if ($null -ne $base.pcie_aspm.ac) { $PcieAc = [int]$base.pcie_aspm.ac }
            if ($null -ne $base.pcie_aspm.dc) { $PcieDc = [int]$base.pcie_aspm.dc }
        }
        if ($base.usb_suspend) {
            if ($null -ne $base.usb_suspend.ac) { $UsbAc = [int]$base.usb_suspend.ac }
            if ($null -ne $base.usb_suspend.dc) { $UsbDc = [int]$base.usb_suspend.dc }
        }
    }
    catch {
        # Use safe Windows defaults above
    }
}

if (-not $RestoredByPython) {
    Restore-PowerOverlayBalanced
    & powercfg /setacvalueindex SCHEME_CURRENT $SUBGROUP_WIFI $SETTING_WIFI_PS $WifiAc 2>$null | Out-Null
    & powercfg /setdcvalueindex SCHEME_CURRENT $SUBGROUP_WIFI $SETTING_WIFI_PS $WifiDc 2>$null | Out-Null
    & powercfg /setacvalueindex SCHEME_CURRENT $SUBGROUP_PCIE $SETTING_PCIE_ASPM $PcieAc 2>$null | Out-Null
    & powercfg /setdcvalueindex SCHEME_CURRENT $SUBGROUP_PCIE $SETTING_PCIE_ASPM $PcieDc 2>$null | Out-Null
    & powercfg /setacvalueindex SCHEME_CURRENT $SUBGROUP_USB  $SETTING_USB_SUSP $UsbAc  2>$null | Out-Null
    & powercfg /setdcvalueindex SCHEME_CURRENT $SUBGROUP_USB  $SETTING_USB_SUSP $UsbDc  2>$null | Out-Null
    & powercfg /setactive SCHEME_CURRENT 2>$null | Out-Null
}

Write-Host "==> Removing installed files from $InstallDir..." -ForegroundColor Cyan
if (Test-Path -LiteralPath $InstallDir) {
    if ($Purge) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "    Purged entire directory $InstallDir (including config.toml)."
    }
    else {
        $ConfigPath = Join-Path $InstallDir "config.toml"
        $HasUserConfig = Test-Path -LiteralPath $ConfigPath

        $ItemsToRemove = @(
            "smart_power_win.py",
            "smart_power_config.py",
            "adaptive_brightness.py",
            "display_refresh.py",
            "requirements-windows.txt",
            "config.example.toml",
            "smart-powerctl.cmd",
            "state.json",
            "status.json",
            "baseline.json",
            "uninstall.ps1",
            ".venv",
            "__pycache__"
        )
        foreach ($rel in $ItemsToRemove) {
            $full = Join-Path $InstallDir $rel
            if (Test-Path -LiteralPath $full) {
                Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction SilentlyContinue
            }
        }

        if ($HasUserConfig) {
            Write-Host "    Preserved user config at $ConfigPath (pass -Purge to delete it)."
        }
        else {
            $remaining = @(Get-ChildItem -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue)
            if ($remaining.Count -eq 0) {
                Remove-Item -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

Write-Host ""
Write-Host "smart-powerd for Windows uninstalled cleanly." -ForegroundColor Green
