# smart-powerd

**Cross-platform (Linux + Windows 10/11) adaptive, workload-aware laptop power, brightness, and 3-tier refresh-rate optimizer.**

`smart-powerd` extends battery life and keeps laptops cool and quiet during everyday browsing, coding, and reading—without sacrificing burst responsiveness or full performance during builds, video calls, downloads, or gaming. It is hardware-agnostic, distro-agnostic, reversible, and designed from the ground up to never wake a suspended discrete GPU or interfere with battery charge limits.

---

## Highlights

- **Cross-Platform (Linux + Windows 10/11)**:
  - **Linux**: Root systemd daemon (`smart-powerd`) + allowlisted `sudoers` helper + PyQt6 tray (`smart-power-tray`) + CLI (`smart-powerctl`).
  - **Windows 10/11**: Self-contained Win32 engine + PyQt6 tray & CLI (`windows/smart_power_win.py`) using native `powrprof.dll`, `powercfg`, `ChangeDisplaySettingsExW`, `SetThreadExecutionState`, WMI, and `psutil`—zero external display tools required.
- **4-Mode Workload Engine**: Smoothly switches between **Light**, **Download**, **Heavy**, and **Off** modes with 12-second downgrade hysteresis so bursty workloads never flap.
- **Zero-Lag Light Mode**: Keeps CPU boost/turbo uncapped on Linux (`boost=1` / `no_turbo=0` and full `scaling_max_freq`) with `balance_power` EPP and `balanced` PPD—avoiding the sluggishness of `power-saver` profiles.
- **Portable 3-Tier Adaptive Refresh Rate**: Automatically matches **AC / Charging (Max Hz)**, **Battery >= 20% (Smart Step-Down Mid Hz)**, and **Battery < 20% (60 Hz Battery Saver)** across any laptop panel on both Linux (KDE/wlroots/GNOME/X11) and Windows (`ChangeDisplaySettingsExW`).
- **3-Tier Adaptive Screen Brightness**: Blends hardware Ambient Light Sensors (Linux IIO / `iio-sensor-proxy`, Windows WinRT ALS), a privacy-safe single-frame webcam luma fallback (every 30 minutes when no hardware ALS is present, closed-shutter aware, RAM-only, one-click toggleable), and a smooth 24h circadian curve—with automatic manual-override detection.
- **Deep Battery Optimization Tricks**: AMD Panel Power Savings (ABM), VM dirty writeback tuning, NMI watchdog disable, SATA/AHCI ALPM, PCIe ASPM, HD-Audio power save, HID-safe USB autosuspend, unused Ethernet/SD-card PCI runtime PM, optional keyboard backlight dimming, and Windows Wi-Fi/PCIe power saving.
- **D3cold / RTD3 Safe**: Never runs `nvidia-smi` or reads `/proc/driver/nvidia/*`. GPU activity is inspected exclusively via kernel sysfs (`power/runtime_status`, `power_state`), and backlight control skips disconnected/dGPU entries.
- **Battery Health Safe**: Never modifies OEM battery charge thresholds (`charge_control_*`, `conservation_mode`, etc.)—reports them read-only in status output.
- **Cleanly Reversible**: Captures a portable hardware baseline before changing anything and restores every setting on uninstall.

---

## How the 4 Modes Work

`smart-powerd` samples CPU utilization (aggregate and single-core), active processes, network RX throughput, discrete GPU runtime state, and media/fullscreen inhibitors every 2.5 seconds:

| Mode | When Activated | Linux CPU / Profile | Windows Power Overlay | Wi-Fi & Power Tricks |
| :--- | :--- | :--- | :--- | :--- |
| **Light** | Everyday browsing, editing, reading, idle | EPP `balance_power`, PPD `balanced`, boost uncapped | `Better Battery` overlay | Wi-Fi PS `on` / Medium PS, ASPM `powersave`, audio PS `5s`, USB/PCI PM, plus Linux Battery+Light tricks (ABM, dirty writeback, SATA ALPM, kbd dim) |
| **Download** | Sustained RX >= `1.5 MiB/s`, RX spike >= `3.0 MiB/s`, or package manager / download tool active (`rclone mount` excluded) | EPP `balance_performance`, PPD `balanced` | `Balanced` overlay + `ES_SYSTEM_REQUIRED` sleep inhibit | Wi-Fi PS `off` / Max Perf (full throughput & low jitter), baseline PCIe/audio |
| **Heavy** | Total CPU >= `25%`, single-core >= `85%`, active dGPU, compiler/IDE/game/encoder process, or fullscreen inhibitor | EPP `performance`, PPD `performance` | `Best Performance` overlay + `ABOVE_NORMAL` priority boost | All power-saving throttles disabled (baseline ASPM, Wi-Fi PS `off`, ABM `0`, audio PS baseline) |
| **Off** | Manually toggled off via tray click or CLI | Restored to baseline (`balanced` / `balance_performance`) | Restored to `Balanced` + baseline Wi-Fi/PCIe | All baseline hardware values & max native refresh restored |

> **Hysteresis / Cooldown**: Stepping up from Light -> Download/Heavy is immediate. Stepping back down waits `cooldown_sec` (default `12.0s`) after the workload subsides to prevent rapid mode oscillation during compilation stages or video buffering.
>
> **Smart Filtering**: Chromium/Electron UI helper processes (`--type=renderer`, `--type=gpu-process`, etc.) and idle FUSE mounts (`rclone mount`/`serve`/`rcd`) are filtered cleanly so everyday browsing or cloud mounts do not lock the laptop out of Light mode.

---

## 3-Tier Portable Adaptive Refresh Rate

Instead of dropping straight to 60 Hz the moment you unplug, `smart-powerd` automatically adapts to the exact refresh rates supported by your laptop panel on both Linux and Windows:

- **AC / Charging (`high`)**: Highest native refresh rate (`ac_refresh_hz = 0` auto-selects max Hz).
- **On Battery >= 20% (`mid`)**: Automatic smooth step-down (`battery_refresh_hz = 0` selects the second-highest rate strictly above `low_refresh_hz` whenever the panel exposes 3+ refresh rates; 2-rate panels stay at max Hz until low battery).
- **On Battery < 20% (`low`)**: Drops to `low_refresh_hz` (default `60 Hz`) for maximum remaining runtime, with **`+2%` exit hysteresis on battery** (exits low mode once >= 22%) and **immediate restore to `high` on AC**.
- **Smart Optimize OFF**: Always restores `high` (highest native Hz) cleanly, as does quitting the tray.

### Automatic Panel Mapping Examples

| Laptop Panel Modes | AC / Charging (`high`) | Battery >= 20% (`mid`) | Battery < 20% (`low`) | Smart Optimize OFF |
| :--- | :---: | :---: | :---: | :---: |
| **300 / 240 / 60 Hz** | `300 Hz` | `240 Hz` | `60 Hz` | `300 Hz` |
| **240 / 165 / 60 Hz** | `240 Hz` | `165 Hz` | `60 Hz` | `240 Hz` |
| **165 / 120 / 60 Hz** | `165 Hz` | `120 Hz` | `60 Hz` | `165 Hz` |
| **144 / 90 / 60 Hz** | `144 Hz` | `90 Hz` | `60 Hz` | `144 Hz` |
| **144 / 60 Hz** *(2-rate)* | `144 Hz` | `144 Hz` | `60 Hz` | `144 Hz` |
| **120 / 60 Hz** *(2-rate)* | `120 Hz` | `120 Hz` | `60 Hz` | `120 Hz` |
| **60 Hz only** | `60 Hz` *(no-op)* | `60 Hz` *(no-op)* | `60 Hz` *(no-op)* | `60 Hz` *(no-op)* |

Need custom targets? Set explicit values in `[battery_tricks]` (e.g. `ac_refresh_hz = 240`, `battery_refresh_hz = 120`, `low_battery_refresh_pct = 25`, `low_refresh_hz = 60`), or disable refresh switching entirely with `refresh_policy_enabled = false`.

---

## Adaptive Screen Brightness

Brightness management runs in the user's graphical session using a 3-tier ambient pipeline shared across Linux and Windows:

1. **Tier 1 — Hardware Ambient Light Sensor (ALS)**: Reads Linux `iio-sensor-proxy` (`net.hadess.SensorProxy`) / `/sys/bus/iio/devices/iio:device*/in_illuminance_*` (or Windows WinRT ALS) and maps lux logarithmically (`0 lux -> min_pct`, `100 lux -> ~52%`, `400 lux -> ~75%`, `1500+ lux -> max_pct`).
2. **Tier 2 — Privacy-Safe Webcam Ambient Sensor** *(enabled by default at a 30-minute interval, used ONLY when no hardware ALS is present)*:
   - Never invoked if a hardware Ambient Light Sensor is available, and toggleable with one click in the tray menu (`Webcam Ambient Sensor`), `smart-powerctl webcam-als off`, or `config.toml` (`webcam_als_enabled = false`).
   - Checks active camera users before touching the webcam (e.g. `/proc/*/fd` on Linux) and skips cleanly if Zoom, Meet, OBS, or a browser has the camera open.
   - Captures a tiny **160x120 grayscale probe purely in memory** via `ffmpeg` (4-frame UVC auto-exposure warmup, inspecting only the last frame), detects closed privacy shutters (`max(frame) <= 1` -> falls back cleanly to Circadian Schedule), and closes the device immediately so USB autosuspend sleeps the camera again. **No image is ever written to disk.**
   - Periodic sampling runs every `webcam_interval_sec` (default `1800s` / 30 minutes), or immediately on demand via **"Measure Room Light Now"** / `smart-powerctl measure-light`.
3. **Tier 3 — Circadian Time-of-Day Curve**: Smooth piecewise-linear interpolation across 24 hours (with midnight wrap-around). Always works as a standalone fallback and blends cleanly (`70% ambient + 30% schedule`) when an ambient sensor is available so late-night use stays comfortable.

### Battery Scaling, Smooth Stepping & Manual Override Protection

- **Battery Multiplier**: Multiplies the target brightness by `battery_multiplier` (default `0.85`) when unplugged, clamped to `[min_pct, max_pct]` (default `12%..95%`).
- **Smooth Transitions**: Moves at most `smooth_step_pct` (default `3%`) per polling tick so the screen never flashes abruptly.
- **Manual Override Detection**: Whenever you press your laptop's brightness Fn keys or drag the OS brightness slider (`>= 4%` delta from the last applied value), automatic brightness pauses for `manual_override_pause_sec` (default `1800s` / 30 minutes). Triggering **"Measure Room Light Now"** or toggling auto-brightness resumes immediately.
- **Safe Backlight Backends**:
  - **Linux**: KDE Plasma 6 D-Bus (`org.kde.Solid.PowerManagement` `setBrightnessSilent`) -> `brightnessctl` targeting the verified internal panel -> direct connected `eDP`/`LVDS` sysfs (`amdgpu_bl*`, `intel_backlight`, `acpi_video*`), strictly rejecting disconnected/dGPU entries (`nvidia_*`).
  - **Windows**: WMI `WmiMonitorBrightness` / `WmiMonitorBrightnessMethods` (silent non-windowed PowerShell).

---

## Battery Optimization Tricks

Every optimization is toggleable in `config.toml` and restores its captured baseline automatically:

| Optimization | Platform | Applied In | Behavior | Restored On |
| :--- | :--- | :--- | :--- | :--- |
| **3-Tier Adaptive Refresh Rate** | Linux + Windows | Battery >= 20% (`mid`), < 20% (`low`) | Steps down built-in `eDP`/`LVDS` panel (`kscreen-doctor`, `wlr-randr`, `gdctl`/`gnome-randr`, `xrandr`, Win32 `ChangeDisplaySettingsExW`) | AC, Off, or tray exit |
| **AMD Panel Power Savings (ABM)** | Linux | Battery + Light | Sets `/sys/class/drm/card*-eDP-*/amdgpu/panel_power_savings` to `amd_abm_battery_level` (default `2`) | AC, Heavy, Off, Uninstall (`0` / baseline) |
| **VM Dirty Writeback** | Linux | Battery + Light | Raises `/proc/sys/vm/dirty_writeback_centisecs` to `1500` (15 s) to batch disk flushes | AC, Heavy, Off, Uninstall |
| **NMI Watchdog Disable** | Linux | Light | Sets `/proc/sys/kernel/nmi_watchdog` to `0` when writable to eliminate periodic timer interrupts | Heavy, Off, Uninstall |
| **SATA / AHCI ALPM** | Linux | Battery + Light | Sets discovered `/sys/class/scsi_host/host*/link_power_management_policy` to `med_power_with_dipm` | AC, Heavy, Off, Uninstall |
| **PCIe ASPM** | Linux + Windows | Light | Sets Linux `pcie_aspm` to `powersave` / Windows `powercfg` ASPM on active scheme | Heavy, Off, Uninstall |
| **HD-Audio Power Save** | Linux | Light | Sets `/sys/module/snd_hda_intel/parameters/power_save` to `5` s | Heavy, Off, Uninstall |
| **Wi-Fi Power Save** | Linux + Windows | Light | Linux `iw dev <iface> set power_save on` (periodically re-checked) / Windows `powercfg` Medium Power Saving | Download, Heavy, Off, Uninstall |
| **HID-Safe USB Autosuspend** | Linux | Light | Enables `power/control=auto` only on non-HID (`!= 03`), non-Bluetooth (`!= e0`), non-Audio (`!= 01`), non-hub (`!= 09`) USB devices | Download, Heavy, Off, Uninstall |
| **Unused PCI Runtime PM** | Linux | Light | Auto-discovers Ethernet (`0x0200`, link down only) and SD card readers (`0x0805`). Never touches Wi-Fi, NVMe, or active GPUs | Download, Heavy, Off, Uninstall |
| **Keyboard Backlight Dim** | Linux | Battery + Light | Auto-discovers `/sys/class/leds/*kbd_backlight*`, saves current level, and dims to `0` | AC, Heavy, Off, Uninstall |
| **RTD3 / D3cold Protection** | Linux | Always | Reads only sysfs `power/runtime_status` and `power_state` for detected NVIDIA/AMD dGPUs — zero GPU wakeups | N/A |
| **Battery Charge Thresholds** | Linux | Always | Strictly read-only — preserves BIOS/OEM charge limits (`charge_control_end_threshold`, `conservation_mode`, etc.) | Never modified |

---

## Compatibility Matrix

### Operating Systems & Desktop Environments

| Platform / Environment | Power Engine | Tray UI | Adaptive Brightness | 3-Tier Refresh Rate |
| :--- | :--- | :--- | :--- | :--- |
| **Linux — KDE Plasma 6 (Wayland)** | `smart-powerd` (systemd) | PyQt6 SNI | Native silent D-Bus (`Solid.PowerManagement`) | Native (`kscreen-doctor`) |
| **Linux — GNOME (Wayland)** | `smart-powerd` (systemd) | PyQt6 (AppIndicator) | `brightnessctl` / sysfs | `gdctl` / `gnome-randr` |
| **Linux — Hyprland / Sway / wlroots** | `smart-powerd` (systemd) | PyQt6 SNI | `brightnessctl` / sysfs | `wlr-randr` |
| **Linux — X11 (XFCE, i3, Cinnamon, MATE)** | `smart-powerd` (systemd) | PyQt6 | `brightnessctl` / sysfs | `xrandr` |
| **Windows 10 / 11 (x64 / ARM64)** | `windows/smart_power_win.py` | PyQt6 (`pythonw.exe`) | WMI + WinRT ALS / `ffmpeg` dshow | Native Win32 `ChangeDisplaySettingsExW` |

### CPUs, GPUs & Linux Distributions

| Category | Supported Targets |
| :--- | :--- |
| **CPUs** | AMD Ryzen (`amd-pstate-epp` with uncapped boost & dynamic governor unlock), Intel Core (`intel_pstate` active/passive), generic `acpi-cpufreq`, plus Windows `powrprof.dll` power overlays |
| **GPUs** | Hybrid AMD/Intel iGPU + NVIDIA dGPU (RTD3/D3cold safe), Hybrid iGPU + Discrete AMD GPU, iGPU-only systems |
| **Linux Distros** | Arch / CachyOS / EndeavourOS (`%wheel`), Fedora / Nobara / RHEL (`%wheel`), Debian / Ubuntu / Pop!_OS / Mint (`%sudo`), openSUSE Tumbleweed / Leap (`%wheel`) |

---

## GitHub Releases (Pre-Packaged Downloads)

Tagged releases on [GitHub Releases](https://github.com/HuzursuzAdam/smart-powerd/releases) include ready-to-install archives and SHA-256 checksums:

- `smart-powerd-vX.Y.Z-linux.tar.gz` — complete Linux daemon, tray, CLI, systemd unit, sudoers template, optional `ananicy-cpp` rules, and `install.sh` / `uninstall.sh`
- `smart-powerd-vX.Y.Z-windows.zip` — complete Windows engine/tray (`windows/smart_power_win.py`), shared Python modules, `requirements-windows.txt`, and PowerShell `install.ps1` / `uninstall.ps1`
- `SHA256SUMS.txt` — verify downloaded archives before extracting:
  ```bash
  sha256sum -c SHA256SUMS.txt
  ```

---

## Quick Install — Linux

### 1. Install Dependencies

`install.sh` checks required and optional dependencies automatically and prints tailored commands if anything is missing.

- **Arch / CachyOS / EndeavourOS**:
  ```bash
  sudo pacman -S python python-pyqt6 python-dbus iw sudo
  # Optional (recommended for webcam ambient sensor & non-KDE brightness fallback):
  sudo pacman -S ffmpeg brightnessctl
  ```
- **Fedora**:
  ```bash
  sudo dnf install python3 python3-pyqt6 python3-dbus iw sudo
  # Optional:
  sudo dnf install ffmpeg brightnessctl
  ```
- **Debian / Ubuntu**:
  ```bash
  sudo apt install python3 python3-pyqt6 python3-dbus iw sudo
  # Optional:
  sudo apt install ffmpeg brightnessctl
  ```
- **openSUSE**:
  ```bash
  sudo zypper install python311 python311-qt6 python311-dbus-python iw sudo
  # Optional:
  sudo zypper install ffmpeg brightnessctl
  ```

### 2. Download or Clone and Install

From a release tarball:
```bash
tar -xzf smart-powerd-v2.1.0-linux.tar.gz
cd smart-powerd-v2.1.0-linux
sudo ./install.sh
```

Or from Git:
```bash
git clone https://github.com/HuzursuzAdam/smart-powerd.git
cd smart-powerd
sudo ./install.sh
```

Installer flags:
- `--user <username>` — explicitly select which desktop user receives tray autostart (otherwise auto-detected from `PKEXEC_UID`, `SUDO_USER`, `loginctl`, or first non-system user).
- `--no-autostart` — skip installing the user autostart `.desktop` file.
- `--no-start` — install files and enable the unit without starting `smart-powerd` immediately.
- `--skip-deps` — bypass missing dependency check (advanced).

### 3. Verify & Launch the Tray

```bash
smart-powerctl status
nohup /usr/local/bin/smart-power-tray >/dev/null 2>&1 &
```

On subsequent logins, `smart-power-tray` starts automatically via `~/.config/autostart/smart-power-tray.desktop`.

---

## Quick Install — Windows 10 / 11

### Prerequisites
- **Python 3.11+** installed from [python.org](https://www.python.org/downloads/windows/) or `winget install Python.Python.3.12` (ensure `python` & `pythonw` are on `PATH`).
- Optional: `ffmpeg` (`winget install Gyan.FFmpeg`) if your laptop has no hardware Ambient Light Sensor and you want the 30-minute privacy-safe webcam fallback.

### Install via PowerShell

Extract `smart-powerd-v2.1.0-windows.zip` (or clone the repo) and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
```

What `windows\install.ps1` does:
1. Checks Python 3.11+ and installs `smart_power_config.py`, `adaptive_brightness.py`, `display_refresh.py`, `windows\smart_power_win.py`, and `requirements-windows.txt` into `%LOCALAPPDATA%\smart-powerd`.
2. Copies `config.example.toml` -> `%LOCALAPPDATA%\smart-powerd\config.toml` **only if it does not already exist** (preserving user edits across upgrades).
3. Installs Python dependencies (`PyQt6>=6.5`, `psutil>=5.9`) via `pip` (pass `-SkipPip` to skip).
4. Registers silent logon autostart under `HKCU:\Software\Microsoft\Windows\CurrentVersion\Run` using `pythonw.exe` (no console window flash) and launches the system tray immediately.

### Windows CLI & Tray Usage

- **System Tray**:
  - **Left-Click**: Toggle Smart Optimize ON / OFF (green Light, blue Download, amber Heavy, gray Off).
  - **Hover Tooltip**: Mode, Battery % / Wattage, Power Overlay, Wi-Fi PS, live Refresh Rate Hz + tier, and Brightness % + source.
  - **Right-Click Menu**: Toggle Smart Optimize, Auto Brightness, Webcam Ambient Sensor, Measure Light Now, Open Config, Quit (restores native max Hz).
- **CLI**:
  ```powershell
  python "$env:LOCALAPPDATA\smart-powerd\smart_power_win.py" --status
  python "$env:LOCALAPPDATA\smart-powerd\smart_power_win.py" --status --json
  python "$env:LOCALAPPDATA\smart-powerd\smart_power_win.py" --headless
  python "$env:LOCALAPPDATA\smart-powerd\smart_power_win.py" --once
  ```

### Clean Uninstall (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\uninstall.ps1
# Or also delete %LOCALAPPDATA%\smart-powerd\config.toml:
powershell -ExecutionPolicy Bypass -File .\windows\uninstall.ps1 -Purge
```

Restores `Balanced` power overlay, native max display refresh rate, and captured Wi-Fi/PCIe baseline settings, removes the logon autostart entry, and cleans up `%LOCALAPPDATA%\smart-powerd`.

---

## Configuration

- **Linux**: `/etc/smart-powerd/config.toml` (hot-reload via `smart-powerctl reload`)
- **Windows**: `%LOCALAPPDATA%\smart-powerd\config.toml` (also falls back to `%ProgramData%\smart-powerd\config.toml`)

Both installers copy [`config.example.toml`](config.example.toml) **only if `config.toml` does not already exist**, preserving customizations across upgrades. Any omitted key safely inherits its built-in default.

Key sections in `config.toml`:
- `[general]` — `poll_interval_sec`, `cooldown_sec`, `wifi_recheck_ticks`
- `[cpu]` — `light_epp_ac`, `light_epp_battery`, `download_epp`, `heavy_epp`, `uncap_boost_in_light`
- `[battery_tricks]` — `aspm_light`, `audio_power_save_light`, `dirty_writeback_battery`, `disable_nmi_watchdog`, `sata_alpm_light`, `amd_abm_battery_level`, `usb_autosuspend_non_hid`, `pci_runtime_pm_unused`, `kbd_backlight_dim_battery`, `refresh_policy_enabled`, `ac_refresh_hz`, `battery_refresh_hz`, `low_battery_refresh_pct`, `low_refresh_hz`
- `[brightness]` — `enabled`, `ambient_source`, `webcam_als_enabled`, `webcam_device`, `webcam_interval_sec`, `battery_multiplier`, `min_pct`, `max_pct`, `smooth_step_pct`, `manual_override_pause_sec`, `schedule`
- `[workloads]` — `extra_heavy_processes`, `extra_download_processes`, `rx_threshold_mib_s`, `rx_immediate_mib_s`, `cpu_heavy_pct`, `single_core_heavy_pct`, `heavy_nice`

Runtime toggle overrides made from the tray or CLI (`enabled`, `brightness_enabled`, `webcam_als_enabled`) are persisted separately in `state.json` so your `config.toml` is never rewritten behind your back.

---

## Linux CLI & Tray Usage

### CLI (`smart-powerctl`)

```bash
smart-powerctl status              # Human-readable telemetry (mode, wattage, CPU, GPU, Wi-Fi, brightness, refresh)
smart-powerctl status --json       # Machine-readable JSON telemetry
smart-powerctl on                  # Enable adaptive optimization
smart-powerctl off                 # Pause optimization and restore baseline hardware settings
smart-powerctl toggle              # Toggle optimization ON / OFF
smart-powerctl reload              # Hot-reload /etc/smart-powerd/config.toml
smart-powerctl brightness on       # Enable adaptive brightness
smart-powerctl brightness off      # Disable adaptive brightness
smart-powerctl brightness toggle   # Toggle adaptive brightness
smart-powerctl webcam-als on       # Enable periodic webcam ambient sensing (default: on, 30-min interval)
smart-powerctl webcam-als off      # Disable periodic webcam ambient sensing
smart-powerctl webcam-als toggle   # Toggle webcam ambient sensing
smart-powerctl measure-light       # Trigger one immediate ambient brightness measurement
```

### System Tray (`smart-power-tray`)

- **Left-Click**: Instant one-click toggle of Smart Optimize ON / OFF (passwordless via allowlisted `sudoers`).
- **Hover Tooltip**: Live mode, trigger reason, battery percentage & discharge wattage, CPU EPP, Wi-Fi power save, dGPU status, panel refresh rate & tier (`AC` / `Battery` / `Low Battery`), and brightness level/source.
- **Right-Click Menu**:
  - `Turn Smart Optimize OFF / ON`
  - `Auto Brightness: ON / OFF`
  - `Webcam Ambient Sensor: ON / OFF`
  - `Measure Room Light Now` (runs a one-shot measurement, clears manual override pause, and adjusts brightness smoothly)
  - `Refresh Status`
  - `Quit Tray` (automatically restores native max panel refresh rate before exiting)

---

## Architecture & Installed Files (Linux)

```text
/usr/local/lib/smart-power/
├── smart_power_config.py          # Shared TOML loader & default schema
├── adaptive_brightness.py         # 3-tier ambient + circadian brightness controller
└── display_refresh.py             # Cross-platform 3-tier refresh controller (KDE/wlroots/GNOME/X11/Win32)

/usr/local/sbin/smart-powerd           # Root systemd daemon (reads sysfs, applies power tuning)
/usr/local/sbin/smart-power-uninstall  # Clean reversible uninstaller
/usr/local/libexec/smart-powerd-helper # Strictly allowlisted privilege helper (no shell calls)
/usr/local/bin/smart-powerctl          # User CLI client
/usr/local/bin/smart-power-tray        # PyQt6 desktop tray (hosts brightness + refresh rate)

/etc/smart-powerd/config.toml          # User configuration (preserved across upgrades)
/etc/systemd/system/smart-powerd.service
/etc/sudoers.d/smart-powerd            # Generated for %wheel/%sudo & validated with visudo -cf
/etc/ananicy.d/99-smart-powerd.{types,rules}  # Installed only if /etc/ananicy.d exists
/usr/local/share/applications/smart-power-tray.desktop
~/.config/autostart/smart-power-tray.desktop
```

### Security Model (Linux)

`/etc/sudoers.d/smart-powerd` grants detected admin groups (`%wheel` and/or `%sudo`) passwordless execution **only** for `/usr/local/libexec/smart-powerd-helper` with the exact 10 literal subcommands (`on`, `off`, `toggle`, `reload`, `brightness-on`, `brightness-off`, `brightness-toggle`, `webcam-als-on`, `webcam-als-off`, `webcam-als-toggle`). No arbitrary arguments or shell execution are permitted.

---

## Running Tests & Building Releases

Run the full cross-platform unit and contract test suite (runs cleanly on both Linux and Windows):

```bash
python3 -m unittest discover -s tests -v
```

Build Linux + Windows release archives locally:

```bash
./scripts/build-release.sh v2.1.0
(cd dist && sha256sum -c SHA256SUMS.txt)
```

---

## Clean Uninstall (Linux)

```bash
sudo smart-power-uninstall
```

Or to also delete `/etc/smart-powerd/config.toml`:

```bash
sudo smart-power-uninstall --purge
```

What Linux uninstall does:
1. Stops `smart-power-tray` gracefully (restoring native max refresh rate) and removes autostart & desktop files.
2. Disables and stops `smart-powerd.service`.
3. Restores **all** baseline hardware settings from `/var/lib/smart-powerd/baseline.json` (PPD `balanced`, CPU boost=1 / `no_turbo=0`, uncapped `scaling_max_freq`, EPP `balance_performance` or `schedutil`/`ondemand`/`powersave` governor, PCIe ASPM, HD-Audio power save, Wi-Fi power save, VM dirty writeback, NMI watchdog, SATA ALPM, AMD ABM, keyboard backlight, PCI runtime PM, and USB autosuspend).
4. Removes all installed binaries, libraries, systemd unit, `sudoers.d` rule, optional `ananicy.d` rules, `/run/smart-powerd`, and `/var/lib/smart-powerd`.
5. **Never touches** battery charge thresholds or desktop environment power settings.

---

## Changelog & License

- See [`CHANGELOG.md`](CHANGELOG.md) for version history.
- Released under the MIT License — see [`LICENSE`](LICENSE) for details.
