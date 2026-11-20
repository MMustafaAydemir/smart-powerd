# smart-power

**Adaptive, workload-aware Linux laptop power, brightness, and refresh-rate optimizer.**

`smart-power` extends battery life and keeps laptops cool and quiet during everyday browsing, coding, and reading—without sacrificing burst responsiveness or full performance during builds, video calls, downloads, or gaming. It is hardware-agnostic, distro-agnostic, reversible, and designed from the ground up to never wake a suspended discrete GPU or interfere with battery charge limits.

---

## Highlights

- **4-Mode Workload Engine (`smart-powerd`)**: Smoothly switches between **Light**, **Download**, **Heavy**, and **Off** modes with 12-second downgrade hysteresis so bursty workloads never flap.
- **Zero-Lag Light Mode**: Keeps CPU boost/turbo uncapped (`boost=1` / `no_turbo=0` and full `scaling_max_freq`) while setting `balance_power` EPP and `balanced` PPD—avoiding the sluggishness of `power-saver` profiles on modern AMD and Intel laptops.
- **Deep Battery Optimization Tricks**: Dynamic 60 Hz refresh rate on Battery + Light, AMD Panel Power Savings (ABM), VM dirty writeback tuning, NMI watchdog disable, SATA/AHCI ALPM, PCIe ASPM, HD-Audio power save, HID-safe USB autosuspend, unused Ethernet/SD-card PCI runtime PM, and optional keyboard backlight dimming.
- **3-Tier Adaptive Screen Brightness**: Blends hardware Ambient Light Sensors (IIO / `iio-sensor-proxy`), a privacy-safe single-frame webcam luma fallback (every 30 minutes when no hardware ALS is present, one-click toggleable in the tray), and a smooth circadian time-of-day curve—with automatic manual-override detection.
- **D3cold / RTD3 Safe**: Never runs `nvidia-smi` or reads `/proc/driver/nvidia/*`. GPU activity is inspected exclusively via kernel sysfs (`power/runtime_status`, `power_state`), and backlight control skips disconnected/dGPU backlight entries.
- **Battery Health Safe**: Never modifies OEM battery charge thresholds (`charge_control_*`, `conservation_mode`, etc.)—reports them read-only in status output.
- **One-Click System Tray & CLI**: Passwordless admin toggles via a strictly allowlisted `sudoers` helper (`smart-powerd-helper`), live wattage/mode/refresh/brightness telemetry in both tray tooltip and `smart-powerctl status`.
- **Cleanly Reversible**: Captures a portable hardware baseline before changing anything and restores every setting on uninstall.

---

## How the 4 Modes Work

`smart-powerd` samples CPU utilization (aggregate and single-core), active processes, network RX throughput, discrete GPU runtime state, and media/fullscreen desktop inhibitors every 2.5 seconds:

| Mode | When Activated | CPU / Profile | Wi-Fi & Power Tricks | Display Refresh (Battery) |
| :--- | :--- | :--- | :--- | :--- |
| **Light** | Everyday browsing, editing, reading, idle | EPP `balance_power`, PPD `balanced`, boost uncapped | ASPM `powersave`, Wi-Fi PS `on`, audio PS `5s`, USB/PCI PM, plus Battery+Light tricks (ABM, dirty writeback, SATA ALPM, kbd dim) | Low refresh (default `60 Hz`) |
| **Download** | Sustained RX >= `1.5 MiB/s`, RX spike >= `3.0 MiB/s`, or package manager / download tool active | EPP `balance_performance`, PPD `balanced` | Wi-Fi PS `off` (full throughput & low jitter), baseline PCIe/audio | Native max refresh |
| **Heavy** | Total CPU >= `25%`, single-core >= `85%`, active discrete GPU, compiler/IDE/game/encoder process, or fullscreen inhibitor | EPP `performance`, PPD `performance` | All power-saving throttles disabled (baseline ASPM, Wi-Fi PS `off`, ABM `0`, audio PS baseline) | Native max refresh |
| **Off** | Manually toggled off via tray click or `smart-powerctl off` | Restored to baseline (`balanced` / `balance_performance`) | All baseline hardware values restored | Native max refresh |

> **Hysteresis / Cooldown**: Stepping up from Light -> Download/Heavy is immediate. Stepping back down waits `cooldown_sec` (default `12.0s`) after the workload subsides to prevent rapid mode oscillation during compilation stages or video buffering.
>
> **Smart Browser Filtering**: Chromium/Electron UI helper processes (`--type=renderer`, `--type=gpu-process`, etc.) are filtered carefully so normal web browsing does not falsely lock the laptop in Heavy mode.

---

## Adaptive Screen Brightness

Brightness management runs in the user's graphical session (`smart-power-tray`) using a 3-tier ambient pipeline:

1. **Tier 1 — Hardware Ambient Light Sensor (ALS)**: Automatically reads `iio-sensor-proxy` (`net.hadess.SensorProxy`) or `/sys/bus/iio/devices/iio:device*/in_illuminance_*` and maps lux logarithmically (`0 lux -> min_pct`, `100 lux -> ~52%`, `400 lux -> ~75%`, `1500+ lux -> max_pct`).
2. **Tier 2 — Privacy-Safe Webcam Ambient Sensor** *(enabled by default at a 30-minute interval, used ONLY when no hardware ALS is present)*:
   - Never invoked if a hardware Ambient Light Sensor is available, and toggleable with one click in the tray menu (`Webcam Ambient Sensor`), `smart-powerctl webcam-als off`, or `config.toml` (`webcam_als_enabled = false`).
   - Checks `/proc/*/fd` before touching `/dev/video*` and skips cleanly if any application (Zoom, Meet, browser, OBS) has the camera open.
   - Captures a **single 160x120 grayscale frame purely in memory** via `ffmpeg`, computes mean luminance, and closes the device immediately so USB autosuspend sleeps the camera again. **No image is ever written to disk.**
   - Periodic sampling runs every `webcam_interval_sec` (default `1800s` / 30 minutes), or immediately on demand via **"Measure Room Light Now"** / `smart-powerctl measure-light`.
3. **Tier 3 — Circadian Time-of-Day Curve**: Smooth piecewise-linear interpolation across 24 hours (with midnight wrap-around). Always works as a standalone fallback and blends cleanly (`70% ambient + 30% schedule`) when an ambient sensor is available so late-night use stays comfortable.

### Battery Scaling, Smooth Stepping & Manual Override Protection

- **Battery Multiplier**: Multiplies the target brightness by `battery_multiplier` (default `0.85`) when unplugged, clamped to `[min_pct, max_pct]` (default `12%..95%`).
- **Smooth Transitions**: Moves at most `smooth_step_pct` (default `3%`) per polling tick so the screen never flashes abruptly.
- **Manual Override Detection**: Whenever you press your laptop's brightness Fn keys or drag the desktop brightness slider (`>= 4%` delta from the last applied value), automatic brightness pauses for `manual_override_pause_sec` (default `1800s` / 30 minutes). Triggering **"Measure Room Light Now"** or toggling auto-brightness resumes immediately.
- **dGPU-Safe Backlight Backends**:
  1. KDE Plasma 6 D-Bus (`org.kde.Solid.PowerManagement` `setBrightnessSilent`) — keeps the Plasma slider synced with no OSD popups.
  2. `brightnessctl` targeting only the verified connected internal panel device.
  3. Direct `/sys/class/backlight/*` sysfs — automatically selects a connected `eDP`/`LVDS` panel (`amdgpu_bl*`, `intel_backlight`, `acpi_video*`) and rejects disconnected/dGPU entries (`nvidia_*`).

---

## Battery Optimization Tricks

Every optimization is toggleable in `/etc/smart-powerd/config.toml` and restores its captured baseline automatically:

| Optimization | Applied In | Behavior | Restored On |
| :--- | :--- | :--- | :--- |
| **Dynamic Low Refresh Rate** | Battery + Light | Switches built-in `eDP`/`LVDS` panel to `low_refresh_hz` (default `60 Hz`) via `kscreen-doctor`, `wlr-randr`, `gdctl`/`gnome-randr`, or `xrandr` | AC, Download, Heavy, Off, or tray exit |
| **AMD Panel Power Savings (ABM)** | Battery + Light | Sets `/sys/class/drm/card*-eDP-*/amdgpu/panel_power_savings` to `amd_abm_battery_level` (default `2`) | AC, Heavy, Off, Uninstall (`0` / baseline) |
| **VM Dirty Writeback** | Battery + Light | Raises `/proc/sys/vm/dirty_writeback_centisecs` to `1500` (15 s) to batch disk flushes | AC, Heavy, Off, Uninstall |
| **NMI Watchdog Disable** | Light | Sets `/proc/sys/kernel/nmi_watchdog` to `0` when writable to eliminate periodic timer interrupts | Heavy, Off, Uninstall |
| **SATA / AHCI ALPM** | Battery + Light | Sets discovered `/sys/class/scsi_host/host*/link_power_management_policy` to `med_power_with_dipm` | AC, Heavy, Off, Uninstall |
| **PCIe ASPM** | Light | Sets `/sys/module/pcie_aspm/parameters/policy` to `powersave` | Heavy, Off, Uninstall |
| **HD-Audio Power Save** | Light | Sets `/sys/module/snd_hda_intel/parameters/power_save` to `5` s | Heavy, Off, Uninstall |
| **Wi-Fi Power Save** | Light | Enables `iw dev <iface> set power_save on` (re-checked periodically against NetworkManager resets) | Download, Heavy, Off, Uninstall |
| **HID-Safe USB Autosuspend** | Light | Enables `power/control=auto` only on non-HID (`!= 03`), non-Bluetooth (`!= e0`), non-Audio (`!= 01`), non-hub (`!= 09`) USB devices | Heavy, Off, Uninstall |
| **Unused PCI Runtime PM** | Light | Auto-discovers Ethernet (`0x0200`, only when link `operstate != up`) and SD card readers (`0x0805`). Never touches Wi-Fi, NVMe, or active GPUs | Heavy, Off, Uninstall |
| **Keyboard Backlight Dim** | Battery + Light | Auto-discovers `/sys/class/leds/*kbd_backlight*`, saves current level, and dims to `0` | AC, Heavy, Off, Uninstall |
| **RTD3 / D3cold Protection** | Always | Reads only sysfs `power/runtime_status` and `power_state` for detected NVIDIA/AMD dGPUs — zero GPU wakeups | N/A |
| **Battery Charge Thresholds** | Always | Strictly read-only — preserves BIOS/OEM charge limits (`charge_control_end_threshold`, `conservation_mode`, etc.) | Never modified |

---

## Compatibility Matrix

### CPUs & Power Drivers

| Platform / Driver | Status | Notes |
| :--- | :--- | :--- |
| **AMD Ryzen (`amd-pstate-epp`)** | Full support | Dynamic EPP (`balance_power` / `balance_performance` / `performance`) + uncapped boost in Light mode |
| **Intel Core (`intel_pstate` active)** | Full support | Dynamic EPP + uncapped turbo (`no_turbo=0`) in Light mode |
| **Generic (`acpi-cpufreq` / passive)** | Full support | Automatic fallback to `scaling_governor` (`schedutil`/`powersave` vs `performance`) |
| **PPD / `tuned-ppd` / ACPI `platform_profile`** | Full support | Cooperates with `power-profiles-daemon` or `tuned-ppd` via D-Bus; falls back cleanly to `/sys/firmware/acpi/platform_profile` or standalone mode |

### GPUs

| Configuration | Status | Notes |
| :--- | :--- | :--- |
| **Hybrid AMD/Intel iGPU + NVIDIA dGPU (RTD3/D3cold)** | Full support | Auto-detects PCI vendor `0x10de` + sibling audio `0x0403` via sysfs only; never wakes suspended dGPU |
| **Hybrid iGPU + Discrete AMD GPU** | Full support | Auto-detects secondary `0x1002` (`boot_vga != 1`) via sysfs |
| **Integrated AMD / Intel GPU Only** | Full support | Clean no-op for dGPU checks; AMD ABM auto-enabled on supported AMD eDP panels |

### Desktop Environments & Display Servers

| Environment | Tray UI | Adaptive Brightness | Auto Refresh Rate |
| :--- | :--- | :--- | :--- |
| **KDE Plasma 6 (Wayland)** | Full (PyQt6 SNI) | Native silent D-Bus (`Solid.PowerManagement`) | Native (`kscreen-doctor`) |
| **GNOME (Wayland)** | Full (with AppIndicator extension) | `brightnessctl` / sysfs | `gdctl` / `gnome-randr` |
| **Hyprland / Sway / wlroots** | Full (StatusNotifier tray) | `brightnessctl` / sysfs | `wlr-randr` |
| **X11 (XFCE, i3, Cinnamon, MATE, etc.)** | Full | `brightnessctl` / sysfs | `xrandr` |

### Linux Distributions

| Distribution | Package Manager | Admin Group Auto-Detected |
| :--- | :--- | :--- |
| **Arch Linux / CachyOS / EndeavourOS** | `pacman` | `%wheel` |
| **Fedora / Nobara / RHEL-family** | `dnf` | `%wheel` |
| **Debian / Ubuntu / Pop!_OS / Mint** | `apt` | `%sudo` |
| **openSUSE Tumbleweed / Leap** | `zypper` | `%wheel` |

---

## Quick Install

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

### 2. Clone and Install

```bash
git clone https://github.com/<owner>/smart-power.git
cd smart-power
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

## Configuration

`install.sh` installs `/etc/smart-powerd/config.toml` from [`config.example.toml`](config.example.toml) **only if `/etc/smart-powerd/config.toml` does not already exist**, preserving your edits across upgrades.

Any key omitted from `/etc/smart-powerd/config.toml` safely inherits its default value. Reload changes at runtime without restarting the daemon:

```bash
sudo nano /etc/smart-powerd/config.toml
smart-powerctl reload
```

Key sections in `/etc/smart-powerd/config.toml`:
- `[general]` — `poll_interval_sec`, `cooldown_sec`, `wifi_recheck_ticks`
- `[cpu]` — `light_epp_ac`, `light_epp_battery`, `download_epp`, `heavy_epp`, `uncap_boost_in_light`
- `[battery_tricks]` — `aspm_light`, `audio_power_save_light`, `dirty_writeback_battery`, `disable_nmi_watchdog`, `sata_alpm_light`, `amd_abm_battery_level`, `usb_autosuspend_non_hid`, `pci_runtime_pm_unused`, `kbd_backlight_dim_battery`, `low_refresh_on_battery_light`, `low_refresh_hz`
- `[brightness]` — `enabled`, `ambient_source`, `webcam_als_enabled`, `webcam_device`, `webcam_interval_sec`, `battery_multiplier`, `min_pct`, `max_pct`, `smooth_step_pct`, `manual_override_pause_sec`, `schedule`
- `[workloads]` — `extra_heavy_processes`, `extra_download_processes`, `rx_threshold_mib_s`, `rx_immediate_mib_s`, `cpu_heavy_pct`, `single_core_heavy_pct`, `heavy_nice`

Runtime toggle overrides made from the tray or CLI (`enabled`, `brightness_enabled`, `webcam_als_enabled`) are persisted separately in `/var/lib/smart-powerd/state.json` so your `config.toml` is never rewritten behind your back.

---

## CLI & Tray Usage

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
- **Hover Tooltip**: Live mode, trigger reason, battery percentage & discharge wattage, CPU EPP, Wi-Fi power save, dGPU status, panel refresh rate, and brightness level/source.
- **Right-Click Menu**:
  - `Turn Smart Optimize OFF / ON`
  - `Auto Brightness: ON / OFF`
  - `Webcam Ambient Sensor: ON / OFF`
  - `Measure Room Light Now` (runs a one-shot measurement, clears manual override pause, and adjusts brightness smoothly)
  - `Refresh Status`
  - `Quit Tray` (automatically restores native panel refresh rate before exiting)

---

## Architecture & Installed Files

```text
/usr/local/lib/smart-power/
├── smart_power_config.py          # Shared TOML loader & default schema
├── adaptive_brightness.py         # 3-tier ambient + circadian brightness controller
└── display_refresh.py             # Multi-DE refresh rate controller (KDE/wlroots/GNOME/X11)

/usr/local/sbin/smart-powerd       # Root systemd daemon (reads sysfs, applies power tuning)
/usr/local/sbin/smart-power-uninstall  # Clean reversible uninstaller
/usr/local/libexec/smart-powerd-helper # Strictly allowlisted privilege helper (no shell calls)
/usr/local/bin/smart-powerctl      # User CLI client
/usr/local/bin/smart-power-tray    # PyQt6 desktop tray (hosts brightness + refresh rate)

/etc/smart-powerd/config.toml      # User configuration (preserved across upgrades)
/etc/systemd/system/smart-powerd.service
/etc/sudoers.d/smart-powerd        # Generated for %wheel/%sudo & validated with visudo -cf
/etc/ananicy.d/99-smart-powerd.{types,rules}  # Installed only if /etc/ananicy.d exists
/usr/local/share/applications/smart-power-tray.desktop
~/.config/autostart/smart-power-tray.desktop
```

Runtime & state paths:
- `/run/smart-powerd/status.json` — live daemon telemetry (read-only for desktop users)
- `/run/smart-powerd/control.sock` — root-owned control socket (accessed only via `smart-powerd-helper`)
- `/var/lib/smart-powerd/baseline.json` — captured pre-install hardware baseline
- `/var/lib/smart-powerd/state.json` — persistent runtime toggle overrides
- `$XDG_RUNTIME_DIR/smart-power-display.json` — live user-session brightness & refresh telemetry

### Security Model

`/etc/sudoers.d/smart-powerd` grants detected admin groups (`%wheel` and/or `%sudo`) passwordless execution **only** for `/usr/local/libexec/smart-powerd-helper` with the exact 10 literal subcommands (`on`, `off`, `toggle`, `reload`, `brightness-on`, `brightness-off`, `brightness-toggle`, `webcam-als-on`, `webcam-als-off`, `webcam-als-toggle`). No arbitrary arguments or shell execution are permitted.

---

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

---

## Clean Uninstall

```bash
sudo smart-power-uninstall
```

Or to also delete `/etc/smart-powerd/config.toml`:

```bash
sudo smart-power-uninstall --purge
```

What uninstall does:
1. Stops `smart-power-tray` and removes autostart & desktop files.
2. Disables and stops `smart-powerd.service`.
3. Restores **all** baseline hardware settings from `/var/lib/smart-powerd/baseline.json` (PPD `balanced`, CPU boost=1 / `no_turbo=0`, uncapped `scaling_max_freq`, EPP `balance_performance`, PCIe ASPM, HD-Audio power save, Wi-Fi power save, VM dirty writeback, NMI watchdog, SATA ALPM, AMD ABM, keyboard backlight, PCI runtime PM, and USB autosuspend).
4. Removes all installed binaries, libraries, systemd unit, `sudoers.d` rule, optional `ananicy.d` rules, `/run/smart-powerd`, and `/var/lib/smart-powerd`.
5. **Never touches** battery charge thresholds or desktop environment power settings.

---

## License

MIT License — see [`LICENSE`](LICENSE) for details.
