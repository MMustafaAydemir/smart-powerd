# Changelog

All notable changes to `smart-powerd` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.0] - 2026-11-22

### Added
- **Windows 10/11 Support** (`windows/smart_power_win.py`, `windows/install.ps1`, `windows/uninstall.ps1`, `requirements-windows.txt`):
  - Native Win32 power overlay switching via `powrprof.dll` (`Better Battery` / `Balanced` / `Best Performance`) with `powercfg` fallback and automatic baseline capture & restore.
  - Wi-Fi adapter power saving and PCIe ASPM tuning via `powercfg`, plus download sleep inhibition via `SetThreadExecutionState`.
  - Native Windows workload detection (`psutil`) with 12-second downgrade cooldown and `ABOVE_NORMAL_PRIORITY_CLASS` boost for active compilers, IDEs, and games.
  - Native Win32 `ChangeDisplaySettingsExW` (`ctypes`) 3-tier refresh rate switching requiring zero external display utilities.
  - Adaptive brightness reusing the shared `adaptive_brightness` controller with WMI `WmiMonitorBrightnessMethods`, optional WinRT ALS, and privacy-safe `ffmpeg` DirectShow webcam fallback.
  - PyQt6 system tray applet (matching Linux visual language) with one-click toggle, live wattage/Hz/brightness telemetry, CLI `--status`/`--headless`/`--once` modes, and clean PowerShell installer/uninstaller (`pythonw.exe` logon autostart).
- **3-Tier Portable Adaptive Refresh Rate Policy** (`refresh_policy_enabled`, `ac_refresh_hz`, `battery_refresh_hz`, `low_battery_refresh_pct`, `low_refresh_hz`):
  - **AC / Charging (`high`)**: Runs at highest native refresh rate (e.g. `300 Hz`, `240 Hz`, `165 Hz`, `144 Hz`).
  - **Battery >= 20% (`mid`)**: Automatically steps down one tier on panels with 3+ refresh rates (e.g. `300/240/60 -> 240 Hz`, `240/165/60 -> 165 Hz`, `165/120/60 -> 120 Hz`, `144/90/60 -> 90 Hz`), while 2-rate panels (`144/60`, `120/60`) stay smooth at max Hz above 20%.
  - **Battery < 20% (`low`)**: Drops to `low_refresh_hz` (`60 Hz`) with `+2%` exit hysteresis on battery (`>= 22%`) and immediate restore to max native Hz on AC or when Smart Optimize is OFF.
  - Full backward compatibility with existing `low_refresh_on_battery_light` configurations.
- **Automated Release Packaging & CI/CD**:
  - `scripts/build-release.sh <tag>` generating `smart-powerd-<tag>-linux.tar.gz`, `smart-powerd-<tag>-windows.zip`, and `SHA256SUMS.txt` under `dist/`.
  - `.github/workflows/ci-release.yml` running cross-platform `ubuntu-latest` + `windows-latest` Python 3.11/3.12 test matrices on every push/PR and publishing GitHub Releases automatically on `v*` tags.

### Changed
- `display_refresh.py`: upgraded `choose_refresh_modes()` and `DisplayRefreshController.apply_profile()` to support 3-tier (`high` / `mid` / `low`) profiles across KDE `kscreen-doctor`, `wlr-randr`, `gdctl`/`gnome-randr`, `xrandr`, and Windows `ctypes` Win32 display modes.
- `smart-power-tray.py` & `smart-powerctl.py`: display active refresh tier context (`AC` / `Battery` / `Low Battery` or `high` / `mid` / `low`) alongside live Hz.
- `adaptive_brightness.py`: ensured full cross-platform import safety on Windows (`dbus` optional).

## [2.0.0] - 2026-11-20

### Added
- **Hardware-Agnostic & Distro-Agnostic Linux Engine**:
  - Portable CPU scaling supporting `amd-pstate-epp`, `intel_pstate`, and `acpi-cpufreq` with uncapped boost/turbo in Light mode and dynamic `energy_performance_available_preferences` / `powersave` governor unlocking.
  - Cooperation with `power-profiles-daemon`, `tuned-ppd`, or `/sys/firmware/acpi/platform_profile`.
  - D3cold/RTD3-safe hybrid discrete GPU auto-discovery (NVIDIA `0x10de`, secondary AMD `0x1002` + sibling audio `0x0403`) reading kernel sysfs only.
  - Dynamic PCI runtime PM for unused Ethernet (`0x0200`, link down only) and SD card readers (`0x0805`), plus HID/Bluetooth/Audio-safe USB autosuspend.
- **Deep Battery Optimization Tricks**:
  - AMD Panel Power Savings (ABM), VM `dirty_writeback_centisecs`, NMI watchdog disable, SATA/AHCI ALPM, PCIe ASPM, HD-Audio power save, Wi-Fi power save re-checking, keyboard backlight dimming, and read-only battery charge threshold preservation.
- **3-Tier Adaptive Brightness**:
  - Hardware IIO / `iio-sensor-proxy` ALS, privacy-safe 30-minute RAM-only webcam luma fallback (with 4-frame warmup and closed privacy shutter detection), smooth 24h circadian schedule, manual override pause, and dGPU-safe backlight backend selection (KDE Plasma 6 silent D-Bus, `brightnessctl`, connected eDP/LVDS sysfs).
- **Portable Packaging & Configuration**:
  - Shared `/etc/smart-powerd/config.toml` loader (`smart_power_config.py`) with hot-reload (`smart-powerctl reload`), separate `/var/lib/smart-powerd/state.json` runtime toggles, and dynamic `install.sh` / `uninstall.sh` supporting `%wheel` / `%sudo` detection, `visudo -cf` pre-validation, and full baseline restoration.
- **Comprehensive Test Suite**: 32 unit and cross-worker contract tests including regression coverage for `rclone mount` FUSE exclusion, dynamic EPP governor unlocking, and closed webcam shutter fallback.
