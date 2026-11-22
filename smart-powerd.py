#!/usr/bin/env python3
"""
smart-powerd: Portable, adaptive, workload-aware Linux laptop power daemon.

Manages three dynamic states when enabled (Light, Download, Heavy) and restores
unrestricted baseline/performance defaults when paused (Off), cooperating cleanly
with power-profiles-daemon / tuned-ppd (or running standalone via ACPI
platform_profile & cpufreq) across AMD and Intel CPUs and hybrid GPUs.
"""

from __future__ import annotations

import errno
import glob
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_LIB_DIR = "/usr/local/lib/smart-power"
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
if _LIB_DIR not in sys.path:
    sys.path.append(_LIB_DIR)

from smart_power_config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    effective_brightness_config,
    load_config,
)

try:
    import dbus  # type: ignore[import-untyped]
except ImportError:
    dbus = None

POLL_INTERVAL = 2.5
COOLDOWN_SECONDS = 12.0
WIFI_RECHECK_TICKS = 6

STATE_DIR = Path("/var/lib/smart-powerd")
STATE_FILE = STATE_DIR / "state.json"
BASELINE_FILE = STATE_DIR / "baseline.json"
RUN_DIR = Path("/run/smart-powerd")
STATUS_FILE = RUN_DIR / "status.json"
CONTROL_SOCK = RUN_DIR / "control.sock"

PPD_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles"),
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles"),
)
PPD_PROP_IFACE = "org.freedesktop.DBus.Properties"

PLATFORM_PROFILE_PATH = Path("/sys/firmware/acpi/platform_profile")
PLATFORM_PROFILE_CHOICES_PATH = Path("/sys/firmware/acpi/platform_profile_choices")
DIRTY_WRITEBACK_PATH = Path("/proc/sys/vm/dirty_writeback_centisecs")
NMI_WATCHDOG_PATH = Path("/proc/sys/kernel/nmi_watchdog")
INTEL_NO_TURBO_PATH = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
INTEL_MAX_PERF_PATH = Path("/sys/devices/system/cpu/intel_pstate/max_perf_pct")
GLOBAL_CPUFREQ_BOOST_PATH = Path("/sys/devices/system/cpu/cpufreq/boost")

MODE_RANK = {
    "off": 0,
    "light": 1,
    "download": 2,
    "heavy": 3,
}

EXCLUDED_COMMS = {
    "smart-powerd",
    "smart-power-tray",
    "smart-power-tra",
    # KDE Plasma
    "plasmashell",
    "kwin_wayland",
    "kwin_x11",
    "kded6",
    "kded5",
    "org_kde_powerdevil",
    "org_kde_powerde",
    # GNOME
    "gnome-shell",
    "mutter",
    "gsd-power",
    "gsd-color",
    "gsd-media-keys",
    "gsd-wacom",
    # wlroots / Wayland / X11 compositors & panels
    "Hyprland",
    "sway",
    "waybar",
    "swayidle",
    "swaylock",
    "niri",
    "river",
    "labwc",
    "Xwayland",
    "Xorg",
    "xfwm4",
    "xfce4-panel",
    "cinnamon",
    "marco",
    "picom",
    # Audio & core system services
    "pipewire",
    "pipewire-pulse",
    "wireplumber",
    "pulseaudio",
    "ananicy-cpp",
    "upowerd",
    "systemd",
    "systemd-journald",
    "systemd-journal",
    "systemd-logind",
    "power-profiles-daemon",
    "power-profiles-",
    "tuned",
    "tuned-ppd",
    "thermald",
    "polkitd",
    "dbus-daemon",
    "dbus-broker",
    "NetworkManager",
}

HEAVY_IMMEDIATE_NAMES = {
    "gcc",
    "g++",
    "cc1",
    "cc1plus",
    "clang",
    "clang++",
    "rustc",
    "cargo",
    "cmake",
    "make",
    "ninja",
    "colcon",
    "msbuild",
    "lld",
    "ld.lld",
    "mold",
    "javac",
    "gradle",
    "mvn",
    "gamemoded",
    "gamescope",
    "wine",
    "wine64",
    "wineserver",
    "proton",
    "reaper",
    "heroic",
    "lutris",
    "mangohud",
    "pressure-vessel",
    "blender",
    "ffmpeg",
    "llama-server",
}

HEAVY_CPU_GATED_NAMES = {
    "python",
    "python3",
    "dotnet",
    "node",
    "java",
    "rider",
    "Rider.Backend",
    "code",
    "clion",
    "pycharm",
    "idea",
    "antigravity",
    "freecad",
    "ollama",
}

DOWNLOAD_IMMEDIATE_NAMES = {
    "pacman",
    "paru",
    "yay",
    "makepkg",
    "dnf",
    "dnf5",
    "yum",
    "zypper",
    "apt",
    "apt-get",
    "dpkg",
    "apk",
    "emerge",
    "xbps-install",
    "nix",
    "nix-build",
    "rpm-ostree",
    "flatpak",
    "snap",
    "pip",
    "pip3",
    "uv",
    "npm",
    "pnpm",
    "yarn",
    "bun",
    "fwupdmgr",
    "aria2c",
    "wget",
    "curl",
    "axel",
    "rsync",
    "jdownloader",
    "fdm",
    "filecxx",
}

RCLONE_DAEMON_SUBCOMMANDS = frozenset(
    {"mount", "cmount", "nfsmount", "serve", "rcd"}
)

# USB interface classes excluded from autosuspend:
# 03 = HID (keyboard/mouse/touchpad), e0 = Wireless/Bluetooth, 01 = Audio
USB_SKIP_INTERFACE_CLASSES = {"03", "e0", "01"}

ELECTRON_UI_HELPER_FLAGS = (
    "--type=gpu-process",
    "--type=renderer",
    "--type=utility",
    "--type=zygote",
    "--type=crashpad-handler",
)

# Read-only battery charge threshold / conservation sysfs attribute names
CHARGE_THRESHOLD_ATTRS = (
    "charge_control_start_threshold",
    "charge_control_end_threshold",
    "charge_start_threshold",
    "charge_stop_threshold",
    "charge_behaviour",
)

RX_THRESHOLD_CONSEC_BPS = 1_572_864  # 1.5 MiB/s
RX_THRESHOLD_IMMEDIATE_BPS = 3_145_728  # 3.0 MiB/s


def read_text(path: Path | str, default: str | None = None) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return default


def write_text(path: Path | str, value: str) -> bool:
    try:
        p = Path(path)
        current = read_text(p)
        if current == value:
            return True
        p.write_text(value, encoding="utf-8")
        return True
    except OSError:
        return False


def write_json_atomic(path: Path, data: dict[str, Any], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_active_token(path: Path | str) -> str | None:
    raw = read_text(path)
    if not raw:
        return None
    for tok in raw.split():
        if tok.startswith("[") and tok.endswith("]"):
            return tok[1:-1]
    return raw.split()[0]


def find_iw_binary() -> str | None:
    found = shutil.which("iw")
    if found:
        return found
    for candidate in ("/usr/bin/iw", "/usr/sbin/iw", "/sbin/iw"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def is_electron_ui_helper(args: list[str]) -> bool:
    """Return True if argv belongs to a Chromium/Electron UI helper subprocess."""
    joined = " ".join(args)
    return any(flag in joined for flag in ELECTRON_UI_HELPER_FLAGS)


def is_rclone_download(args: list[str]) -> bool:
    """
    Return True when rclone is invoked for an active transfer rather than a
    long-running FUSE mount or server daemon (mount/cmount/nfsmount/serve/rcd).
    """
    if len(args) < 2:
        return False
    subcmd: str | None = None
    skip_next = False
    value_flags = {
        "--config",
        "--log-file",
        "--log-level",
        "--cache-dir",
        "--temp-dir",
        "--password-command",
        "--bwlimit",
        "--transfers",
        "--checkers",
    }
    for tok in args[1:10]:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok in value_flags:
                skip_next = True
            continue
        subcmd = tok.lower()
        break
    if not subcmd:
        return False
    return subcmd not in RCLONE_DAEMON_SUBCOMMANDS


def should_skip_usb_device(dev_cls: str, iface_classes: set[str]) -> bool:
    """Return True for USB hubs, HID input, wireless/Bluetooth, and audio devices."""
    norm_dev = (dev_cls or "").strip().lower()
    norm_ifaces = {c.strip().lower() for c in iface_classes if c}
    return (
        norm_dev == "09"
        or norm_dev in USB_SKIP_INTERFACE_CLASSES
        or bool(norm_ifaces & USB_SKIP_INTERFACE_CLASSES)
    )


def compute_battery_watts(
    power_now: str | None,
    current_now: str | None,
    voltage_now: str | None,
) -> float:
    """Compute live battery power draw in Watts from sysfs micro-units."""
    if power_now is not None:
        try:
            return round(abs(int(power_now)) / 1e6, 2)
        except ValueError:
            pass
    if current_now is not None and voltage_now is not None:
        try:
            return round((abs(int(current_now)) * int(voltage_now)) / 1e12, 2)
        except ValueError:
            pass
    return 0.0


LOW_BATTERY_HYSTERESIS_PCT = 2


def compute_desired_refresh_profile(
    enabled: bool,
    ac_online: bool,
    battery_pct: int | float | None = 100,
    refresh_policy_enabled: bool = True,
    low_battery_refresh_pct: int = 20,
    previous_profile: str | None = None,
    battery_present: bool = True,
    hysteresis_pct: int = LOW_BATTERY_HYSTERESIS_PCT,
) -> str:
    """
    Compute the 3-tier refresh profile ('high' | 'mid' | 'low'):
      - Smart Optimize OFF or refresh policy disabled -> 'high'
      - AC online or no battery present -> 'high' (exits 'low' immediately)
      - Unplugged & battery_pct < low_battery_refresh_pct -> 'low'
      - Unplugged & previous_profile == 'low' & battery_pct < threshold + hysteresis -> 'low'
      - Otherwise on battery -> 'mid'
    """
    if not enabled or not refresh_policy_enabled:
        return "high"
    if ac_online or not battery_present:
        return "high"

    try:
        threshold = max(0, min(100, int(low_battery_refresh_pct)))
    except (TypeError, ValueError):
        threshold = 20

    if threshold <= 0:
        return "mid"

    if battery_pct is None:
        return "low" if previous_profile == "low" else "mid"

    try:
        pct = float(battery_pct)
    except (TypeError, ValueError):
        return "low" if previous_profile == "low" else "mid"

    if pct < threshold:
        return "low"

    try:
        hyst = max(0, int(hysteresis_pct))
    except (TypeError, ValueError):
        hyst = LOW_BATTERY_HYSTERESIS_PCT

    if previous_profile == "low" and pct < (threshold + hyst):
        return "low"

    return "mid"


def discover_dgpu_in(
    pci_root: Path = Path("/sys/bus/pci/devices"),
) -> tuple[Path | None, str | None, str | None, Path | None, str | None]:
    """
    Auto-detect a hybrid discrete GPU (NVIDIA 0x10de or secondary AMD 0x1002
    with boot_vga != 1) and its sibling HDA audio function (0x0403) purely via
    cached PCI sysfs metadata without waking any suspended device.
    """
    if not pci_root.is_dir():
        return None, None, None, None, None

    display_devs: list[tuple[Path, str, str | None]] = []
    for dev in sorted(pci_root.iterdir()):
        cls = (read_text(dev / "class", "") or "").lower()
        if not (cls.startswith("0x0300") or cls.startswith("0x0302")):
            continue
        vendor = (read_text(dev / "vendor", "") or "").lower()
        boot_vga = read_text(dev / "boot_vga")
        display_devs.append((dev, vendor, boot_vga))

    dgpu_pci: Path | None = None
    dgpu_vendor: str | None = None

    # Prefer NVIDIA hybrid dGPU first (skip boot_vga == "1" dGPU-only MUX systems)
    for dev, vendor, boot_vga in display_devs:
        if vendor == "0x10de" and boot_vga != "1":
            dgpu_pci = dev
            dgpu_vendor = vendor
            break

    # Otherwise check secondary AMD dGPU (boot_vga != "1" and not sole iGPU)
    if dgpu_pci is None:
        for dev, vendor, boot_vga in display_devs:
            if vendor == "0x1002" and boot_vga != "1" and (
                boot_vga == "0" or len(display_devs) >= 2
            ):
                dgpu_pci = dev
                dgpu_vendor = vendor
                break

    if dgpu_pci is None:
        return None, None, None, None, None

    render_name: str | None = None
    card_name: str | None = None
    drm_dir = dgpu_pci / "drm"
    if drm_dir.is_dir():
        for item in sorted(drm_dir.iterdir()):
            if item.name.startswith("renderD"):
                render_name = item.name
            elif item.name.startswith("card"):
                card_name = item.name

    dgpu_audio: Path | None = None
    slot_prefix = dgpu_pci.name.rsplit(".", 1)[0]
    for sibling in sorted(pci_root.glob(f"{slot_prefix}.*")):
        if sibling == dgpu_pci:
            continue
        s_vendor = (read_text(sibling / "vendor", "") or "").lower()
        s_cls = (read_text(sibling / "class", "") or "").lower()
        if s_vendor == dgpu_vendor and s_cls.startswith("0x0403"):
            dgpu_audio = sibling
            break

    return dgpu_pci, render_name, card_name, dgpu_audio, dgpu_vendor


def discover_pci_by_class(
    class_prefix: str, pci_root: Path = Path("/sys/bus/pci/devices")
) -> list[Path]:
    """Return sorted PCI sysfs paths whose class begins with ``class_prefix``."""
    if not pci_root.is_dir():
        return []
    prefix = class_prefix.lower()
    matches: list[Path] = []
    for dev in sorted(pci_root.iterdir()):
        cls = (read_text(dev / "class", "") or "").lower()
        if cls.startswith(prefix):
            matches.append(dev)
    return matches


def discover_als_lux(iio_root: Path = Path("/sys/bus/iio/devices")) -> float | None:
    """Read hardware ambient light sensor lux from Linux IIO sysfs, if present."""
    if not iio_root.is_dir():
        return None
    for dev in sorted(iio_root.glob("iio:device*")):
        inp = read_text(dev / "in_illuminance_input")
        if inp is not None:
            try:
                val = float(inp)
                if val >= 0.0:
                    return round(val, 2)
            except ValueError:
                pass
        raw = read_text(dev / "in_illuminance_raw")
        if raw is not None:
            try:
                raw_val = float(raw)
                offset_s = read_text(dev / "in_illuminance_offset")
                scale_s = read_text(dev / "in_illuminance_scale")
                offset = float(offset_s) if offset_s is not None else 0.0
                scale = float(scale_s) if scale_s is not None else 1.0
                lux = (raw_val + offset) * scale
                if lux >= 0.0:
                    return round(lux, 2)
            except ValueError:
                pass
    return None


class SmartPowerDaemon:
    """Portable adaptive power daemon polling /proc and /sys with minimal wakeups."""

    def __init__(self, config_path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        self.running = True
        self._reload_requested = False
        self._force_reapply = False
        self.config_path = Path(config_path)
        self.clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])

        # D-Bus is optional: cooperate with PPD/tuned-ppd if present, else standalone
        self.bus = None
        if dbus is not None:
            try:
                self.bus = dbus.SystemBus()
            except Exception:
                self.bus = None

        self.iw_bin = find_iw_binary()

        # Load TOML configuration and derived settings
        self.config: dict[str, Any] = {}
        self.poll_interval = POLL_INTERVAL
        self.cooldown_sec = COOLDOWN_SECONDS
        self.wifi_recheck_ticks = WIFI_RECHECK_TICKS
        self.heavy_immediate_names: set[str] = set(HEAVY_IMMEDIATE_NAMES)
        self.download_immediate_names: set[str] = set(DOWNLOAD_IMMEDIATE_NAMES)
        self.rx_threshold_consec_bps = float(RX_THRESHOLD_CONSEC_BPS)
        self.rx_threshold_immediate_bps = float(RX_THRESHOLD_IMMEDIATE_BPS)
        self.cpu_heavy_pct = 25.0
        self.single_core_heavy_pct = 85.0
        self.heavy_nice = -5
        self._apply_config(load_config(self.config_path))

        # Hardware discovery (100% dynamic, zero hardcoded BDFs)
        self.cpufreq_dirs = sorted(
            Path(p) for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq")
        )
        self.epp_paths = sorted(
            Path(p)
            for p in glob.glob(
                "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/energy_performance_preference"
            )
        )
        self.cpu_driver = self._detect_cpu_driver()
        self.available_epps = self._detect_available_epps()
        self.available_governors = self._detect_available_governors()
        self.platform_profile_choices = self._detect_platform_profile_choices()

        self.aspm_path = Path("/sys/module/pcie_aspm/parameters/policy")
        self.audio_ps_path = Path("/sys/module/snd_hda_intel/parameters/power_save")
        (
            self.dgpu_pci,
            self.dgpu_render,
            self.dgpu_card,
            self.dgpu_audio_pci,
            self.dgpu_vendor,
        ) = discover_dgpu_in()
        self.wifi_iface = self._discover_wifi()
        self.bat_path, self.ac_paths, self.usb_pd_paths = self._discover_power_supplies()
        self.eth_pcis = discover_pci_by_class("0x0200")
        self.sdhci_pcis = discover_pci_by_class("0x0805")
        self.sata_alpm_paths = sorted(
            Path(p)
            for p in glob.glob("/sys/class/scsi_host/host*/link_power_management_policy")
        )
        self.amd_abm_paths = sorted(
            Path(p)
            for p in glob.glob("/sys/class/drm/card*-eDP-*/amdgpu/panel_power_savings")
        )
        self.kbd_backlight_dirs = sorted(
            Path(p)
            for p in glob.glob("/sys/class/leds/*kbd_backlight*")
            if (Path(p) / "brightness").is_file()
        )

        # Persistent toggle state & hardware baseline
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        self.enabled = bool(self.state.get("enabled", True))
        self.baseline = self._load_or_capture_baseline()

        # Runtime state
        self.current_mode: str | None = None
        self.current_reason = "initializing"
        self.last_ac_online: bool | None = None
        self.last_refresh_profile: str = "high"
        self.last_mode_activity_ts: dict[str, float] = {}
        self.inhibit_fd: int | None = None
        self.last_wifi_ps: str | None = None
        self.wifi_tick_counter = 0
        self.kbd_dimmed = False
        self.saved_kbd_brightness: dict[str, str] = {}

        # Telemetry history
        self.prev_cpu_total: int | None = None
        self.prev_cpu_idle: int | None = None
        self.prev_proc_ticks: dict[tuple[int, str], int] = {}
        self.prev_rx_bytes: int | None = None
        self.prev_sample_time: float | None = None

        self.high_cpu_streak = 0
        self.dgpu_active_streak = 0
        self.rx_streak = 0

        # Latest telemetry snapshot
        self.last_cpu_pct = 0.0
        self.last_rx_mbps = 0.0
        self.last_heavy_pids: list[int] = []
        self.last_download_pids: list[int] = []

    def _apply_config(self, cfg: dict[str, Any]) -> None:
        self.config = cfg
        gen = cfg.get("general", {})
        wl = cfg.get("workloads", {})

        try:
            self.poll_interval = max(0.5, float(gen.get("poll_interval_sec", POLL_INTERVAL)))
        except (TypeError, ValueError):
            self.poll_interval = POLL_INTERVAL

        try:
            self.cooldown_sec = max(0.0, float(gen.get("cooldown_sec", COOLDOWN_SECONDS)))
        except (TypeError, ValueError):
            self.cooldown_sec = COOLDOWN_SECONDS

        try:
            self.wifi_recheck_ticks = max(1, int(gen.get("wifi_recheck_ticks", WIFI_RECHECK_TICKS)))
        except (TypeError, ValueError):
            self.wifi_recheck_ticks = WIFI_RECHECK_TICKS

        extra_heavy = {
            str(x).strip()
            for x in wl.get("extra_heavy_processes", [])
            if isinstance(x, str) and x.strip()
        }
        extra_dl = {
            str(x).strip()
            for x in wl.get("extra_download_processes", [])
            if isinstance(x, str) and x.strip()
        }
        self.heavy_immediate_names = HEAVY_IMMEDIATE_NAMES | extra_heavy
        self.download_immediate_names = DOWNLOAD_IMMEDIATE_NAMES | extra_dl

        try:
            self.rx_threshold_consec_bps = max(
                0.1, float(wl.get("rx_threshold_mib_s", 1.5))
            ) * (1024 * 1024)
        except (TypeError, ValueError):
            self.rx_threshold_consec_bps = float(RX_THRESHOLD_CONSEC_BPS)

        try:
            self.rx_threshold_immediate_bps = max(
                0.2, float(wl.get("rx_immediate_mib_s", 3.0))
            ) * (1024 * 1024)
        except (TypeError, ValueError):
            self.rx_threshold_immediate_bps = float(RX_THRESHOLD_IMMEDIATE_BPS)

        try:
            self.cpu_heavy_pct = max(5.0, min(100.0, float(wl.get("cpu_heavy_pct", 25.0))))
        except (TypeError, ValueError):
            self.cpu_heavy_pct = 25.0

        try:
            self.single_core_heavy_pct = max(
                10.0, float(wl.get("single_core_heavy_pct", 85.0))
            )
        except (TypeError, ValueError):
            self.single_core_heavy_pct = 85.0

        try:
            self.heavy_nice = max(-20, min(0, int(wl.get("heavy_nice", -5))))
        except (TypeError, ValueError):
            self.heavy_nice = -5

    def _detect_cpu_driver(self) -> str:
        if not self.cpufreq_dirs:
            return "unknown"
        return read_text(self.cpufreq_dirs[0] / "scaling_driver", "unknown") or "unknown"

    def _detect_available_epps(self) -> set[str]:
        if not self.cpufreq_dirs:
            return set()
        raw = read_text(self.cpufreq_dirs[0] / "energy_performance_available_preferences", "")
        return set(raw.split()) if raw else set()

    def _detect_available_governors(self) -> set[str]:
        if not self.cpufreq_dirs:
            return set()
        raw = read_text(self.cpufreq_dirs[0] / "scaling_available_governors", "")
        return set(raw.split()) if raw else set()

    @staticmethod
    def _detect_platform_profile_choices() -> set[str]:
        raw = read_text(PLATFORM_PROFILE_CHOICES_PATH, "")
        return set(raw.split()) if raw else set()

    @staticmethod
    def _discover_wifi() -> str | None:
        net_root = Path("/sys/class/net")
        if not net_root.is_dir():
            return None
        for net_dev in sorted(net_root.iterdir()):
            if (net_dev / "wireless").is_dir():
                return net_dev.name
        return None

    @staticmethod
    def _discover_power_supplies() -> tuple[Path | None, list[Path], list[Path]]:
        bat: Path | None = None
        mains: list[Path] = []
        usb_pd: list[Path] = []
        ps_root = Path("/sys/class/power_supply")
        if not ps_root.is_dir():
            return None, mains, usb_pd
        for entry in sorted(ps_root.iterdir()):
            ptype = read_text(entry / "type", "") or ""
            if ptype == "Battery" and bat is None:
                bat = entry
            elif ptype == "Mains":
                mains.append(entry)
            elif ptype.startswith("USB") and (entry / "online").is_file():
                usb_pd.append(entry)
        return bat, mains, usb_pd

    def _load_state(self) -> dict[str, Any]:
        default_state: dict[str, Any] = {
            "enabled": True,
            "brightness_enabled": None,
            "webcam_als_enabled": None,
        }
        raw = read_text(STATE_FILE)
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    state = {
                        "enabled": bool(data.get("enabled", True)),
                        "brightness_enabled": (
                            data["brightness_enabled"]
                            if isinstance(data.get("brightness_enabled"), bool)
                            else None
                        ),
                        "webcam_als_enabled": (
                            data["webcam_als_enabled"]
                            if isinstance(data.get("webcam_als_enabled"), bool)
                            else None
                        ),
                    }
                    # Upgrade older state.json missing the new keys
                    if set(state.keys()) != set(data.keys()):
                        write_json_atomic(STATE_FILE, state, mode=0o644)
                    return state
            except (ValueError, TypeError):
                pass
        write_json_atomic(STATE_FILE, default_state, mode=0o644)
        return default_state

    def _save_state(self) -> None:
        self.state["enabled"] = self.enabled
        write_json_atomic(STATE_FILE, self.state, mode=0o644)

    def _get_wifi_power_save(self) -> str | None:
        if not self.wifi_iface or not self.iw_bin:
            return None
        try:
            res = subprocess.run(
                [self.iw_bin, "dev", self.wifi_iface, "get", "power_save"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if res.returncode == 0:
                out = res.stdout.lower()
                if "power save: on" in out:
                    return "on"
                if "power save: off" in out:
                    return "off"
        except (OSError, subprocess.SubprocessError):
            pass
        return self.last_wifi_ps

    def _set_wifi_power_save(self, target: str) -> None:
        if not self.wifi_iface or not self.iw_bin:
            return
        try:
            res = subprocess.run(
                [self.iw_bin, "dev", self.wifi_iface, "set", "power_save", target],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if res.returncode == 0:
                self.last_wifi_ps = target
        except (OSError, subprocess.SubprocessError):
            pass

    @staticmethod
    def _usb_interface_classes(usb_dev: Path) -> set[str]:
        classes: set[str] = set()
        prefix = usb_dev.name + ":"
        try:
            for child in usb_dev.iterdir():
                if child.name.startswith(prefix) and child.is_dir():
                    cls = read_text(child / "bInterfaceClass")
                    if cls:
                        classes.add(cls.lower())
        except OSError:
            pass
        return classes

    def _iter_usb_devices(self) -> list[tuple[Path, str, set[str]]]:
        result: list[tuple[Path, str, set[str]]] = []
        usb_root = Path("/sys/bus/usb/devices")
        if not usb_root.is_dir():
            return result
        for entry in sorted(usb_root.iterdir()):
            if not (entry / "idVendor").is_file():
                continue
            dev_cls = (read_text(entry / "bDeviceClass", "") or "").lower()
            iface_classes = self._usb_interface_classes(entry)
            result.append((entry, dev_cls, iface_classes))
        return result

    def _load_or_capture_baseline(self) -> dict[str, Any]:
        baseline: dict[str, Any] = {}
        dirty = False
        raw = read_text(BASELINE_FILE)
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    baseline = parsed
            except (ValueError, TypeError):
                baseline = {}

        if "pcie_aspm" not in baseline:
            baseline["pcie_aspm"] = read_active_token(self.aspm_path) or "default"
            dirty = True
        if "snd_hda_power_save" not in baseline:
            baseline["snd_hda_power_save"] = read_text(self.audio_ps_path, "0") or "0"
            dirty = True
        if "wifi_power_save" not in baseline:
            baseline["wifi_power_save"] = self._get_wifi_power_save() or "on"
            dirty = True
        if "dirty_writeback_centisecs" not in baseline:
            baseline["dirty_writeback_centisecs"] = (
                read_text(DIRTY_WRITEBACK_PATH, "500") or "500"
            )
            dirty = True
        if "nmi_watchdog" not in baseline:
            baseline["nmi_watchdog"] = read_text(NMI_WATCHDOG_PATH, "0") or "0"
            dirty = True
        if "platform_profile" not in baseline:
            baseline["platform_profile"] = read_text(PLATFORM_PROFILE_PATH)
            dirty = True

        sata_base = baseline.get("sata_alpm")
        if not isinstance(sata_base, dict):
            sata_base = {}
            baseline["sata_alpm"] = sata_base
            dirty = True
        for path in self.sata_alpm_paths:
            host_name = path.parent.name
            if host_name not in sata_base:
                pol = read_text(path)
                if pol:
                    sata_base[host_name] = pol
                    dirty = True

        abm_base = baseline.get("amd_abm")
        if not isinstance(abm_base, dict):
            abm_base = {}
            baseline["amd_abm"] = abm_base
            dirty = True
        for path in self.amd_abm_paths:
            conn_name = path.parent.parent.name
            if conn_name not in abm_base:
                lvl = read_text(path, "0") or "0"
                abm_base[conn_name] = lvl
                dirty = True

        kbd_base = baseline.get("kbd_backlight")
        if not isinstance(kbd_base, dict):
            kbd_base = {}
            baseline["kbd_backlight"] = kbd_base
            dirty = True
        for led_dir in self.kbd_backlight_dirs:
            if led_dir.name not in kbd_base:
                val = read_text(led_dir / "brightness", "0") or "0"
                kbd_base[led_dir.name] = val
                dirty = True

        pci_base = baseline.get("pci_controls")
        if not isinstance(pci_base, dict):
            pci_base = {}
            baseline["pci_controls"] = pci_base
            dirty = True
        for pci in (*self.eth_pcis, *self.sdhci_pcis):
            if pci.name not in pci_base:
                ctrl = read_text(pci / "power/control")
                if ctrl:
                    pci_base[pci.name] = ctrl
                    dirty = True

        usb_base = baseline.get("usb_controls")
        if not isinstance(usb_base, dict):
            usb_base = {}
            baseline["usb_controls"] = usb_base
            dirty = True
        if not usb_base:
            for dev, dev_cls, iface_classes in self._iter_usb_devices():
                if should_skip_usb_device(dev_cls, iface_classes):
                    continue
                ctrl = read_text(dev / "power/control")
                if ctrl:
                    usb_base[dev.name] = ctrl
                    dirty = True

        if dirty or not BASELINE_FILE.is_file():
            write_json_atomic(BASELINE_FILE, baseline, mode=0o644)
        return baseline

    def _get_ppd_interface(self) -> tuple[Any, str] | None:
        if self.bus is None or dbus is None:
            return None
        for bus_name, obj_path in PPD_ENDPOINTS:
            try:
                obj = self.bus.get_object(bus_name, obj_path)
                props = dbus.Interface(obj, PPD_PROP_IFACE)
                props.Get(bus_name, "ActiveProfile")
                return props, bus_name
            except Exception:
                continue
        return None

    def _set_ppd_profile(self, profile: str) -> bool:
        """Set power-profiles-daemon / tuned-ppd ActiveProfile; return True if PPD is active."""
        iface = self._get_ppd_interface()
        if iface is None or dbus is None:
            return False
        props, bus_name = iface
        try:
            current = str(props.Get(bus_name, "ActiveProfile"))
            if current != profile:
                props.Set(bus_name, "ActiveProfile", dbus.String(profile))
            return True
        except Exception:
            return False

    def _get_ppd_profile(self) -> str:
        iface = self._get_ppd_interface()
        if iface is None:
            return "none"
        props, bus_name = iface
        try:
            return str(props.Get(bus_name, "ActiveProfile"))
        except Exception:
            return "none"

    def _set_platform_profile(self, desired: str) -> None:
        """Write ACPI platform_profile only when PPD is inactive and target is supported."""
        if not PLATFORM_PROFILE_PATH.is_file():
            return
        if self.platform_profile_choices and desired not in self.platform_profile_choices:
            if "balanced" in self.platform_profile_choices:
                desired = "balanced"
            else:
                return
        write_text(PLATFORM_PROFILE_PATH, desired)

    def _set_power_profile(self, profile: str, is_off: bool = False) -> None:
        """Cooperate with PPD/tuned-ppd if active, else fall back to ACPI platform_profile."""
        if self._set_ppd_profile(profile):
            return
        if is_off:
            base_prof = self.baseline.get("platform_profile")
            target = str(base_prof) if base_prof else "balanced"
            self._set_platform_profile(target)
        else:
            self._set_platform_profile(profile)

    def _resolve_supported_epp(self, desired: str) -> str:
        # Always re-read live available EPPs: amd-pstate-epp / intel_pstate
        # dynamically change available preferences when scaling_governor changes.
        live_epps = self._detect_available_epps()
        if live_epps:
            self.available_epps = live_epps
        available = live_epps or self.available_epps
        if not available or desired in available:
            return desired
        fallbacks = {
            "balance_power": ("power", "balance_performance", "default"),
            "balance_performance": ("default", "balance_power", "performance"),
            "performance": ("balance_performance", "default"),
        }
        for alt in fallbacks.get(desired, ("default",)):
            if alt in available:
                return alt
        return desired

    def _resolve_fallback_governor(self, mode: str) -> str | None:
        prefs = (
            ("performance", "schedutil", "ondemand")
            if mode == "heavy"
            else ("schedutil", "ondemand", "powersave", "conservative")
        )
        if self.available_governors:
            for gov in prefs:
                if gov in self.available_governors:
                    return gov
            return None
        return prefs[0]

    def _ensure_epp_compatible_governor(self, desired_epp: str) -> None:
        """
        On amd-pstate-epp and active intel_pstate, scaling_governor == 'performance'
        locks energy_performance_available_preferences to {'performance'} only.
        Switch to 'powersave' first before writing any non-performance EPP.
        """
        if desired_epp == "performance" or not self.cpufreq_dirs:
            return
        avail_govs = self._detect_available_governors() or self.available_governors
        if avail_govs:
            self.available_governors = avail_govs
        if "powersave" not in avail_govs:
            return
        for cdir in self.cpufreq_dirs:
            gov_path = cdir / "scaling_governor"
            if read_text(gov_path) == "performance":
                write_text(gov_path, "powersave")

    def _set_cpu_performance(self, mode: str, desired_epp: str) -> None:
        if self.epp_paths:
            self._ensure_epp_compatible_governor(desired_epp)
            target = self._resolve_supported_epp(desired_epp)
            for path in self.epp_paths:
                write_text(path, target)
            return

        gov = self._resolve_fallback_governor(mode)
        if gov:
            for cdir in self.cpufreq_dirs:
                write_text(cdir / "scaling_governor", gov)

    def _check_cpu_performance(self, mode: str, desired_epp: str) -> None:
        if self.epp_paths:
            if (
                desired_epp != "performance"
                and self.cpufreq_dirs
                and read_text(self.cpufreq_dirs[0] / "scaling_governor") == "performance"
            ):
                avail_govs = self._detect_available_governors() or self.available_governors
                if "powersave" in avail_govs:
                    self._set_cpu_performance(mode, desired_epp)
                    return
            target = self._resolve_supported_epp(desired_epp)
            if read_text(self.epp_paths[0]) != target:
                self._set_cpu_performance(mode, desired_epp)
        elif self.cpufreq_dirs:
            gov = self._resolve_fallback_governor(mode)
            if gov and read_text(self.cpufreq_dirs[0] / "scaling_governor") != gov:
                self._set_cpu_performance(mode, desired_epp)

    def _read_current_epp_or_gov(self) -> str:
        if self.epp_paths:
            return read_text(self.epp_paths[0], "unknown") or "unknown"
        if self.cpufreq_dirs:
            return read_text(self.cpufreq_dirs[0] / "scaling_governor", "unknown") or "unknown"
        return "unknown"

    def _ensure_boost_uncapped(self) -> None:
        # Global cpufreq boost (acpi-cpufreq / older kernels)
        if GLOBAL_CPUFREQ_BOOST_PATH.is_file() and read_text(GLOBAL_CPUFREQ_BOOST_PATH) != "1":
            write_text(GLOBAL_CPUFREQ_BOOST_PATH, "1")

        # Intel P-State turbo uncapping
        if INTEL_NO_TURBO_PATH.is_file() and read_text(INTEL_NO_TURBO_PATH) != "0":
            write_text(INTEL_NO_TURBO_PATH, "0")
        if INTEL_MAX_PERF_PATH.is_file():
            raw_pct = read_text(INTEL_MAX_PERF_PATH)
            if raw_pct is not None:
                try:
                    if int(raw_pct) < 100:
                        write_text(INTEL_MAX_PERF_PATH, "100")
                except ValueError:
                    pass

        # Per-CPU boost & scaling_max_freq (amd-pstate-epp & generic cpufreq)
        for cdir in self.cpufreq_dirs:
            boost_path = cdir / "boost"
            if boost_path.is_file() and read_text(boost_path) != "1":
                write_text(boost_path, "1")
            max_info_raw = read_text(cdir / "cpuinfo_max_freq")
            scale_max_path = cdir / "scaling_max_freq"
            scale_max_raw = read_text(scale_max_path)
            if max_info_raw and scale_max_raw:
                try:
                    if int(scale_max_raw) < int(max_info_raw):
                        write_text(scale_max_path, max_info_raw)
                except ValueError:
                    pass

    def _acquire_inhibitor(self) -> None:
        if self.inhibit_fd is not None or self.bus is None or dbus is None:
            return
        try:
            obj = self.bus.get_object(
                "org.freedesktop.login1", "/org/freedesktop/login1"
            )
            mgr = dbus.Interface(obj, "org.freedesktop.login1.Manager")
            unix_fd = mgr.Inhibit(
                "idle:sleep",
                "smart-powerd",
                "Active download or package installation",
                "block",
            )
            self.inhibit_fd = unix_fd.take()
        except Exception:
            self.inhibit_fd = None

    def _release_inhibitor(self) -> None:
        if self.inhibit_fd is not None:
            try:
                os.close(self.inhibit_fd)
            except OSError:
                pass
            self.inhibit_fd = None

    def _apply_usb_autosuspend(self, enable: bool) -> None:
        allow_usb = bool(
            self.config.get("battery_tricks", {}).get("usb_autosuspend_non_hid", True)
        )
        effective_enable = enable and allow_usb
        usb_base: dict[str, str] = self.baseline.setdefault("usb_controls", {})
        changed = False
        for dev, dev_cls, iface_classes in self._iter_usb_devices():
            if dev_cls == "09":
                continue
            if dev_cls == "03" or "03" in iface_classes:
                # Never autosuspend HID input devices (mouse, keyboard, touchpad, RGB)
                write_text(dev / "power/control", "on")
                continue
            if should_skip_usb_device(dev_cls, iface_classes):
                continue
            ctrl_path = dev / "power/control"
            if effective_enable:
                if dev.name not in usb_base:
                    cur = read_text(ctrl_path)
                    if cur:
                        usb_base[dev.name] = cur
                        changed = True
                write_text(ctrl_path, "auto")
            else:
                target = usb_base.get(dev.name, "on")
                write_text(ctrl_path, target)
        if changed:
            write_json_atomic(BASELINE_FILE, self.baseline, mode=0o644)

    def _apply_pci_runtime_pm(self, power_save: bool) -> None:
        # Keep hybrid dGPU & its sibling audio in auto PM so RTD3/D3cold works
        for dgpu_dev in (self.dgpu_pci, self.dgpu_audio_pci):
            if dgpu_dev is not None and read_text(dgpu_dev / "boot_vga") != "1":
                ctrl_path = dgpu_dev / "power/control"
                if read_text(ctrl_path) != "auto":
                    write_text(ctrl_path, "auto")

        allow_pci = bool(
            self.config.get("battery_tricks", {}).get("pci_runtime_pm_unused", True)
        )
        effective_ps = power_save and allow_pci
        pci_base: dict[str, str] = self.baseline.get("pci_controls", {})

        for eth_pci in self.eth_pcis:
            if effective_ps:
                eth_up = False
                net_dir_root = eth_pci / "net"
                if net_dir_root.is_dir():
                    for net_dir in net_dir_root.glob("*"):
                        if read_text(net_dir / "operstate") == "up":
                            eth_up = True
                            break
                write_text(eth_pci / "power/control", "on" if eth_up else "auto")
            else:
                write_text(
                    eth_pci / "power/control",
                    str(pci_base.get(eth_pci.name, "on")),
                )

        for sdhci_pci in self.sdhci_pcis:
            target = "auto" if effective_ps else str(pci_base.get(sdhci_pci.name, "on"))
            write_text(sdhci_pci / "power/control", target)

    def _restore_kbd_backlight(self) -> None:
        if not self.kbd_dimmed:
            return
        kbd_base = self.baseline.get("kbd_backlight", {})
        for led_dir in self.kbd_backlight_dirs:
            bpath = led_dir / "brightness"
            # Respect manual user re-illumination while on battery
            if read_text(bpath) == "0":
                restore_val = self.saved_kbd_brightness.get(
                    led_dir.name, str(kbd_base.get(led_dir.name, "0"))
                )
                if restore_val and restore_val != "0":
                    write_text(bpath, restore_val)
        self.kbd_dimmed = False
        self.saved_kbd_brightness.clear()

    def _apply_extra_battery_tricks(self, battery_light: bool, light_mode: bool) -> None:
        tricks = self.config.get("battery_tricks", {})

        # 1. AMD Panel Power Savings (ABM): level 0..4 on Battery+Light, 0 otherwise
        try:
            abm_lvl = max(0, min(4, int(tricks.get("amd_abm_battery_level", 2))))
        except (TypeError, ValueError):
            abm_lvl = 2
        target_abm = str(abm_lvl) if (battery_light and abm_lvl > 0) else "0"
        for abm_path in self.amd_abm_paths:
            write_text(abm_path, target_abm)

        # 2. VM dirty writeback centisecs: 1500 on Battery+Light, restore baseline otherwise
        if DIRTY_WRITEBACK_PATH.is_file():
            try:
                dw_target = int(tricks.get("dirty_writeback_battery", 1500))
            except (TypeError, ValueError):
                dw_target = 1500
            if battery_light and dw_target > 0:
                write_text(DIRTY_WRITEBACK_PATH, str(dw_target))
            else:
                write_text(
                    DIRTY_WRITEBACK_PATH,
                    str(self.baseline.get("dirty_writeback_centisecs", "500")),
                )

        # 3. NMI watchdog: disable in Light Mode if enabled and writable, restore baseline otherwise
        if NMI_WATCHDOG_PATH.is_file():
            disable_nmi = bool(tricks.get("disable_nmi_watchdog", True))
            if light_mode and disable_nmi:
                if read_text(NMI_WATCHDOG_PATH) == "1":
                    write_text(NMI_WATCHDOG_PATH, "0")
            else:
                base_nmi = str(self.baseline.get("nmi_watchdog", "0"))
                write_text(NMI_WATCHDOG_PATH, base_nmi)

        # 4. SATA/AHCI ALPM: med_power_with_dipm on Battery+Light, restore baseline otherwise
        sata_base = self.baseline.get("sata_alpm", {})
        sata_light = str(tricks.get("sata_alpm_light", "med_power_with_dipm")).strip()
        for sata_path in self.sata_alpm_paths:
            host_name = sata_path.parent.name
            if battery_light and sata_light:
                if not write_text(sata_path, sata_light) and sata_light == "med_power_with_dipm":
                    write_text(sata_path, "min_power")
            else:
                base_pol = str(sata_base.get(host_name, "max_performance"))
                write_text(sata_path, base_pol)

        # 5. Keyboard backlight dimming on Battery+Light (with live save/restore)
        dim_kbd = bool(tricks.get("kbd_backlight_dim_battery", True))
        if battery_light and dim_kbd and self.kbd_backlight_dirs:
            if not self.kbd_dimmed:
                for led_dir in self.kbd_backlight_dirs:
                    bpath = led_dir / "brightness"
                    cur_val = read_text(bpath, "0") or "0"
                    self.saved_kbd_brightness[led_dir.name] = cur_val
                    if cur_val != "0":
                        write_text(bpath, "0")
                self.kbd_dimmed = True
        else:
            self._restore_kbd_backlight()

    def _apply_mode(
        self, mode: str, has_download: bool, heavy_pids: list[int], ac_online: bool
    ) -> None:
        transition = (mode != self.current_mode) or self._force_reapply
        self._force_reapply = False
        ac_changed = (
            self.last_ac_online is not None and ac_online != self.last_ac_online
        )
        cpu_cfg = self.config.get("cpu", {})
        tricks_cfg = self.config.get("battery_tricks", {})
        uncap_light = bool(cpu_cfg.get("uncap_boost_in_light", True))

        if mode == "off":
            self._release_inhibitor()
            if transition:
                self._set_power_profile("balanced", is_off=True)
                time.sleep(0.08)
                self._ensure_boost_uncapped()
                dl_epp = str(cpu_cfg.get("download_epp", "balance_performance"))
                self._set_cpu_performance("off", dl_epp)
                write_text(self.aspm_path, str(self.baseline.get("pcie_aspm", "default")))
                write_text(
                    self.audio_ps_path,
                    str(self.baseline.get("snd_hda_power_save", "0")),
                )
                self._set_wifi_power_save("off")
                self._apply_pci_runtime_pm(power_save=False)
                self._apply_usb_autosuspend(enable=False)
                self._apply_extra_battery_tricks(battery_light=False, light_mode=False)
            self.current_mode = mode
            self.last_ac_online = ac_online
            return

        if mode == "light":
            self._release_inhibitor()
            light_epp = str(
                cpu_cfg.get("light_epp_ac", "balance_power")
                if ac_online
                else cpu_cfg.get("light_epp_battery", "balance_power")
            )
            aspm_light = str(tricks_cfg.get("aspm_light", "powersave"))
            audio_light = str(tricks_cfg.get("audio_power_save_light", 5))
            battery_light = not ac_online

            if transition:
                # Keep PPD at balanced (never power-saver, which caps AMD boost)
                self._set_power_profile("balanced")
                time.sleep(0.08)
                if uncap_light:
                    self._ensure_boost_uncapped()
                self._set_cpu_performance("light", light_epp)
                write_text(self.aspm_path, aspm_light)
                write_text(self.audio_ps_path, audio_light)
                self._set_wifi_power_save("on")
                self._apply_pci_runtime_pm(power_save=True)
                self._apply_usb_autosuspend(enable=True)
                self._apply_extra_battery_tricks(
                    battery_light=battery_light, light_mode=True
                )
                self.wifi_tick_counter = 0
            else:
                if ac_changed:
                    self._apply_extra_battery_tricks(
                        battery_light=battery_light, light_mode=True
                    )
                    if uncap_light:
                        self._ensure_boost_uncapped()
                    self._set_cpu_performance("light", light_epp)

                # Counter AC plug/unplug EPP or boost reset, or NM Wi-Fi power_save reset
                if uncap_light:
                    if self.cpufreq_dirs:
                        b0 = self.cpufreq_dirs[0] / "boost"
                        if b0.is_file() and read_text(b0) != "1":
                            self._ensure_boost_uncapped()
                    if INTEL_NO_TURBO_PATH.is_file() and read_text(INTEL_NO_TURBO_PATH) != "0":
                        self._ensure_boost_uncapped()
                self._check_cpu_performance("light", light_epp)

                self.wifi_tick_counter += 1
                if self.wifi_tick_counter >= self.wifi_recheck_ticks:
                    self.wifi_tick_counter = 0
                    if self._get_wifi_power_save() != "on":
                        self._set_wifi_power_save("on")
                    if uncap_light:
                        self._ensure_boost_uncapped()
                    self._apply_pci_runtime_pm(power_save=True)
                    self._apply_usb_autosuspend(enable=True)
                    self._apply_extra_battery_tricks(
                        battery_light=battery_light, light_mode=True
                    )

        elif mode == "download":
            self._acquire_inhibitor()
            dl_epp = str(cpu_cfg.get("download_epp", "balance_performance"))
            if transition:
                self._set_wifi_power_save("off")
                self._set_power_profile("balanced")
                time.sleep(0.08)
                self._ensure_boost_uncapped()
                self._set_cpu_performance("download", dl_epp)
                write_text(self.aspm_path, "default")
                write_text(self.audio_ps_path, "0")
                self._apply_pci_runtime_pm(power_save=False)
                self._apply_usb_autosuspend(enable=False)
                self._apply_extra_battery_tricks(battery_light=False, light_mode=False)
            else:
                self._check_cpu_performance("download", dl_epp)

        elif mode == "heavy":
            if has_download:
                self._acquire_inhibitor()
            else:
                self._release_inhibitor()

            heavy_epp = str(cpu_cfg.get("heavy_epp", "performance"))
            if transition:
                self._set_wifi_power_save("off")
                self._set_power_profile("performance")
                time.sleep(0.08)
                self._ensure_boost_uncapped()
                self._set_cpu_performance("heavy", heavy_epp)
                write_text(self.aspm_path, "default")
                write_text(self.audio_ps_path, "0")
                self._apply_pci_runtime_pm(power_save=False)
                self._apply_usb_autosuspend(enable=False)
                self._apply_extra_battery_tricks(battery_light=False, light_mode=False)
            else:
                self._check_cpu_performance("heavy", heavy_epp)

            # Boost active heavy/compile/game processes to configured nice priority
            for pid in heavy_pids:
                try:
                    if os.getpriority(os.PRIO_PROCESS, pid) >= 0:
                        os.setpriority(os.PRIO_PROCESS, pid, self.heavy_nice)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

        self.current_mode = mode
        self.last_ac_online = ac_online

    def _read_total_cpu_ticks(self) -> tuple[int, int] | None:
        try:
            with open("/proc/stat", "r", encoding="utf-8") as fh:
                line = fh.readline()
            if not line.startswith("cpu "):
                return None
            parts = [int(x) for x in line.split()[1:]]
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
            total = sum(parts)
            return total, idle
        except (OSError, ValueError, IndexError):
            return None

    def _read_rx_bytes(self) -> int:
        total_rx = 0
        try:
            with open("/proc/net/dev", "r", encoding="utf-8") as fh:
                lines = fh.readlines()[2:]
            for line in lines:
                if ":" not in line:
                    continue
                iface, rest = line.split(":", 1)
                iface = iface.strip()
                # Physical NICs have a /sys/class/net/<iface>/device symlink
                if not os.path.exists(f"/sys/class/net/{iface}/device"):
                    continue
                fields = rest.split()
                if fields:
                    total_rx += int(fields[0])
        except (OSError, ValueError):
            pass
        return total_rx

    def _dgpu_has_active_clients(self) -> bool:
        if not self.dgpu_pci:
            return False
        targets: set[str] = set()
        if self.dgpu_vendor == "0x10de":
            targets.add("/dev/nvidia0")
        if self.dgpu_render:
            targets.add(f"/dev/dri/{self.dgpu_render}")
        if self.dgpu_card:
            targets.add(f"/dev/dri/{self.dgpu_card}")
        if not targets:
            return False

        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid_str = entry.name
            comm = read_text(f"/proc/{pid_str}/comm", "") or ""
            if comm in {"Xwayland", "RDD Process"}:
                continue
            fd_dir = f"/proc/{pid_str}/fd"
            try:
                for fd_entry in os.scandir(fd_dir):
                    try:
                        target = os.readlink(fd_entry.path)
                        if target in targets:
                            return True
                    except OSError:
                        continue
            except OSError:
                continue
        return False

    def _sample_workloads(
        self, now: float
    ) -> tuple[float, float, list[int], list[int], str | None, str | None]:
        elapsed = (
            max(0.1, now - self.prev_sample_time)
            if self.prev_sample_time is not None
            else self.poll_interval
        )

        # Total CPU %
        cpu_pct = 0.0
        cpu_sample = self._read_total_cpu_ticks()
        if cpu_sample is not None:
            total, idle = cpu_sample
            if self.prev_cpu_total is not None and self.prev_cpu_idle is not None:
                dt = total - self.prev_cpu_total
                di = idle - self.prev_cpu_idle
                if dt > 0:
                    cpu_pct = max(0.0, min(100.0, 100.0 * (dt - di) / dt))
            self.prev_cpu_total, self.prev_cpu_idle = total, idle

        # Network RX throughput
        rx_bps = 0.0
        cur_rx = self._read_rx_bytes()
        if self.prev_rx_bytes is not None and cur_rx >= self.prev_rx_bytes:
            rx_bps = (cur_rx - self.prev_rx_bytes) / elapsed
        self.prev_rx_bytes = cur_rx
        rx_mbps = rx_bps / (1024 * 1024)

        # Process scan
        my_pid = os.getpid()
        new_proc_ticks: dict[tuple[int, str], int] = {}
        heavy_pids: list[int] = []
        download_pids: list[int] = []
        heavy_reason: str | None = None
        download_reason: str | None = None
        max_non_excluded_cpu = 0.0

        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == my_pid:
                continue
            stat_raw = read_text(f"/proc/{pid}/stat")
            if not stat_raw:
                continue
            rparen = stat_raw.rfind(")")
            lparen = stat_raw.find("(")
            if rparen == -1 or lparen == -1:
                continue
            comm = stat_raw[lparen + 1 : rparen]
            rest = stat_raw[rparen + 2 :].split()
            if len(rest) < 20:
                continue
            try:
                proc_ticks = int(rest[11]) + int(rest[12])  # utime + stime
                start_time = rest[19]
            except (ValueError, IndexError):
                continue

            key = (pid, start_time)
            new_proc_ticks[key] = proc_ticks
            prev_ticks = self.prev_proc_ticks.get(key)
            pid_cpu = 0.0
            if prev_ticks is not None and proc_ticks >= prev_ticks:
                pid_cpu = ((proc_ticks - prev_ticks) / self.clk_tck) / elapsed * 100.0

            cmdline_raw = read_text(f"/proc/{pid}/cmdline", "") or ""
            args = [a for a in cmdline_raw.split("\x00") if a]
            first_arg = args[0].split(" --", 1)[0].strip() if args else ""
            exe_name = os.path.basename(first_arg) if first_arg else comm
            ui_helper = is_electron_ui_helper(args)

            # Ignore self, tray, desktop shells, and core audio/power daemons
            if (
                comm in EXCLUDED_COMMS
                or exe_name in EXCLUDED_COMMS
                or any(
                    "smart-powerd" in a or "smart-power-tray" in a for a in args[:3]
                )
            ):
                continue

            if pid_cpu > max_non_excluded_cpu:
                max_non_excluded_cpu = pid_cpu

            # Check Heavy triggers
            is_heavy_proc = False
            if comm in self.heavy_immediate_names or exe_name in self.heavy_immediate_names:
                is_heavy_proc = True
                heavy_reason = heavy_reason or f"process:{exe_name}"
            elif any(a.lower().endswith(".exe") for a in args[:4]):
                is_heavy_proc = True
                heavy_reason = heavy_reason or f"wine/proton:{exe_name}"
            elif (
                not ui_helper
                and pid_cpu >= self.cpu_heavy_pct
                and (comm in HEAVY_CPU_GATED_NAMES or exe_name in HEAVY_CPU_GATED_NAMES)
            ):
                is_heavy_proc = True
                heavy_reason = heavy_reason or f"dev:{exe_name} ({pid_cpu:.0f}%)"
            elif (
                exe_name in {"docker", "podman"}
                and any(a in {"build", "buildx"} for a in args[1:4])
                and pid_cpu >= self.cpu_heavy_pct
            ):
                is_heavy_proc = True
                heavy_reason = heavy_reason or f"container:{exe_name} build"

            if is_heavy_proc:
                heavy_pids.append(pid)

            # Check Download triggers
            is_dl_proc = False
            if (
                comm in self.download_immediate_names
                or exe_name in self.download_immediate_names
            ):
                is_dl_proc = True
                download_reason = download_reason or f"pkg/dl:{exe_name}"
            elif (comm == "rclone" or exe_name == "rclone") and is_rclone_download(args):
                is_dl_proc = True
                download_reason = download_reason or "pkg/dl:rclone"
            elif exe_name in {"docker", "podman"} and any(
                a in {"pull", "push", "load", "build"} for a in args[1:4]
            ):
                is_dl_proc = True
                download_reason = download_reason or f"{exe_name} transfer"

            if is_dl_proc:
                download_pids.append(pid)

        self.prev_proc_ticks = new_proc_ticks
        self.prev_sample_time = now

        # Fallback sustained CPU trigger
        if (
            cpu_pct >= self.cpu_heavy_pct
            or max_non_excluded_cpu >= self.single_core_heavy_pct
        ):
            self.high_cpu_streak += 1
        else:
            self.high_cpu_streak = 0

        if not heavy_reason and self.high_cpu_streak >= 2:
            heavy_reason = f"sustained CPU ({cpu_pct:.0f}%)"

        # dGPU sustained active check (read ONLY sysfs runtime_status; never wakes D3cold)
        dgpu_rt = (
            read_text(self.dgpu_pci / "power/runtime_status", "unknown")
            if self.dgpu_pci
            else "none"
        )
        if dgpu_rt == "active":
            self.dgpu_active_streak += 1
        else:
            self.dgpu_active_streak = 0

        if not heavy_reason and self.dgpu_active_streak >= 3:
            if cpu_pct >= 15.0 or self._dgpu_has_active_clients():
                heavy_reason = "dGPU active workload"

        # Network RX throughput trigger
        if rx_bps > self.rx_threshold_consec_bps:
            self.rx_streak += 1
        else:
            self.rx_streak = 0

        if not download_reason:
            if rx_bps > self.rx_threshold_immediate_bps or self.rx_streak >= 2:
                download_reason = f"network RX ({rx_mbps:.2f} MiB/s)"

        return cpu_pct, rx_mbps, heavy_pids, download_pids, heavy_reason, download_reason

    def _read_charge_thresholds(self) -> dict[str, Any]:
        """Read-only inspection of battery charge thresholds / conservation mode."""
        thresholds: dict[str, Any] = {}
        if self.bat_path and self.bat_path.is_dir():
            for attr in CHARGE_THRESHOLD_ATTRS:
                val = read_text(self.bat_path / attr)
                if val is not None and val != "":
                    if val.isdigit():
                        thresholds[attr] = int(val)
                    else:
                        thresholds[attr] = val

        for cm_path in sorted(
            Path(p)
            for p in glob.glob(
                "/sys/bus/platform/drivers/ideapad_acpi/VPC*/conservation_mode"
            )
        ):
            val = read_text(cm_path)
            if val is not None and val != "":
                thresholds["conservation_mode"] = int(val) if val.isdigit() else val
                break

        return thresholds

    def _read_battery_telemetry(self) -> dict[str, Any]:
        bat_present = bool(self.bat_path and self.bat_path.exists())
        status = (
            (read_text(self.bat_path / "status", "Unknown") or "Unknown")
            if bat_present and self.bat_path
            else "No Battery"
        )

        # Determine AC online state cleanly across Mains, USB-C PD, and desktops
        if self.ac_paths:
            ac_online = any(read_text(p / "online") == "1" for p in self.ac_paths)
            if not ac_online and self.usb_pd_paths and status != "Discharging":
                ac_online = any(
                    read_text(p / "online") == "1" for p in self.usb_pd_paths
                )
        elif self.usb_pd_paths:
            ac_online = any(read_text(p / "online") == "1" for p in self.usb_pd_paths)
        elif bat_present:
            ac_online = status != "Discharging"
        else:
            ac_online = True

        if not bat_present or not self.bat_path:
            return {
                "battery_present": False,
                "ac_online": ac_online,
                "battery_status": "No Battery",
                "battery_pct": 0,
                "battery_watts": 0.0,
            }

        cap_raw = read_text(self.bat_path / "capacity", "0") or "0"
        try:
            pct = int(cap_raw)
        except ValueError:
            pct = 0

        watts = compute_battery_watts(
            read_text(self.bat_path / "power_now"),
            read_text(self.bat_path / "current_now"),
            read_text(self.bat_path / "voltage_now"),
        )

        return {
            "battery_present": True,
            "ac_online": ac_online,
            "battery_status": status,
            "battery_pct": pct,
            "battery_watts": watts,
        }

    def _read_amd_abm_level(self) -> int | None:
        if not self.amd_abm_paths:
            return None
        raw = read_text(self.amd_abm_paths[0])
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _read_dirty_writeback_cs(self) -> int | None:
        raw = read_text(DIRTY_WRITEBACK_PATH)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _read_sata_alpm(self) -> str:
        if not self.sata_alpm_paths:
            return "none"
        return read_text(self.sata_alpm_paths[0], "unknown") or "unknown"

    def _publish_status(self, bat_info: dict[str, Any] | None = None) -> None:
        epp = self._read_current_epp_or_gov()
        aspm = read_active_token(self.aspm_path) or "unknown"
        audio_raw = read_text(self.audio_ps_path, "0") or "0"
        try:
            audio_ps = int(audio_raw)
        except ValueError:
            audio_ps = 0

        dgpu_status = (
            read_text(self.dgpu_pci / "power/runtime_status", "unknown")
            if self.dgpu_pci
            else "none"
        )
        dgpu_state = (
            read_text(self.dgpu_pci / "power_state", "unknown")
            if self.dgpu_pci
            else "none"
        )

        if bat_info is None:
            bat_info = self._read_battery_telemetry()

        effective_mode = self.current_mode or ("light" if self.enabled else "off")
        tricks_cfg = self.config.get("battery_tricks", {})
        if "refresh_policy_enabled" in tricks_cfg:
            refresh_policy_enabled = bool(tricks_cfg["refresh_policy_enabled"])
        else:
            refresh_policy_enabled = bool(
                tricks_cfg.get("low_refresh_on_battery_light", True)
            )
        try:
            ac_refresh_hz = max(0, int(tricks_cfg.get("ac_refresh_hz", 0)))
        except (TypeError, ValueError):
            ac_refresh_hz = 0
        try:
            battery_refresh_hz = max(0, int(tricks_cfg.get("battery_refresh_hz", 0)))
        except (TypeError, ValueError):
            battery_refresh_hz = 0
        try:
            low_battery_refresh_pct = max(
                0, min(100, int(tricks_cfg.get("low_battery_refresh_pct", 20)))
            )
        except (TypeError, ValueError):
            low_battery_refresh_pct = 20
        try:
            low_refresh_hz = max(30, int(tricks_cfg.get("low_refresh_hz", 60)))
        except (TypeError, ValueError):
            low_refresh_hz = 60

        desired_refresh = compute_desired_refresh_profile(
            enabled=self.enabled,
            ac_online=bool(bat_info["ac_online"]),
            battery_pct=bat_info.get("battery_pct", 100),
            refresh_policy_enabled=refresh_policy_enabled,
            low_battery_refresh_pct=low_battery_refresh_pct,
            previous_profile=self.last_refresh_profile,
            battery_present=bool(bat_info.get("battery_present", True)),
        )
        self.last_refresh_profile = desired_refresh

        platform_profile = read_text(PLATFORM_PROFILE_PATH) or "none"

        payload = {
            "timestamp": time.time(),
            "enabled": self.enabled,
            "mode": effective_mode,
            "reason": self.current_reason,
            "cpu_driver": self.cpu_driver,
            "epp": epp,
            "ppd_profile": self._get_ppd_profile(),
            "platform_profile": platform_profile,
            "aspm": aspm,
            "audio_power_save": audio_ps,
            "amd_abm_level": self._read_amd_abm_level(),
            "dirty_writeback_cs": self._read_dirty_writeback_cs(),
            "sata_alpm": self._read_sata_alpm(),
            "charge_thresholds": self._read_charge_thresholds(),
            "als_lux": discover_als_lux(),
            "desired_refresh_profile": desired_refresh,
            "ac_refresh_hz": ac_refresh_hz,
            "battery_refresh_hz": battery_refresh_hz,
            "low_battery_refresh_pct": low_battery_refresh_pct,
            "low_refresh_hz": low_refresh_hz,
            "brightness_config": effective_brightness_config(self.config, self.state),
            "wifi_iface": self.wifi_iface or "none",
            "wifi_power_save": self.last_wifi_ps
            or self._get_wifi_power_save()
            or "unknown",
            "rx_mbps": round(self.last_rx_mbps, 2),
            "cpu_pct": round(self.last_cpu_pct, 1),
            "dgpu_pci": self.dgpu_pci.name if self.dgpu_pci else "none",
            "dgpu_status": dgpu_status,
            "dgpu_power_state": dgpu_state,
            "inhibit_active": self.inhibit_fd is not None,
            "heavy_pids": self.last_heavy_pids[:16],
            "download_pids": self.last_download_pids[:16],
            **bat_info,
        }
        write_json_atomic(STATUS_FILE, payload, mode=0o644)

    def _select_mode_with_hysteresis(
        self, now: float, heavy_reason: str | None, download_reason: str | None
    ) -> tuple[str, str]:
        if not self.enabled:
            return "off", "smart optimization paused by user"

        if heavy_reason:
            raw_mode, raw_reason = "heavy", heavy_reason
            self.last_mode_activity_ts["heavy"] = now
            self.last_mode_activity_ts["download"] = now
        elif download_reason:
            raw_mode, raw_reason = "download", download_reason
            self.last_mode_activity_ts["download"] = now
        else:
            raw_mode, raw_reason = "light", "idle / light workload"

        cur = self.current_mode
        if cur is None or cur == "off":
            return raw_mode, raw_reason

        if MODE_RANK[raw_mode] >= MODE_RANK[cur]:
            return raw_mode, raw_reason

        # Downgrade requires cooldown_sec since last activity at current level
        last_active = self.last_mode_activity_ts.get(cur, 0.0)
        if (now - last_active) < self.cooldown_sec:
            base_reason = self.current_reason
            while base_reason.endswith(" (cooldown)"):
                base_reason = base_reason.removesuffix(" (cooldown)")
            return cur, f"{base_reason} (cooldown)"

        return raw_mode, raw_reason

    def tick(self) -> None:
        if self._reload_requested:
            self._reload_requested = False
            self._apply_config(load_config(self.config_path))
            self._force_reapply = True

        now = time.monotonic()
        (
            cpu_pct,
            rx_mbps,
            heavy_pids,
            download_pids,
            heavy_reason,
            download_reason,
        ) = self._sample_workloads(now)

        self.last_cpu_pct = cpu_pct
        self.last_rx_mbps = rx_mbps
        self.last_heavy_pids = heavy_pids
        self.last_download_pids = download_pids

        target_mode, target_reason = self._select_mode_with_hysteresis(
            now, heavy_reason, download_reason
        )
        self.current_reason = target_reason
        bat_info = self._read_battery_telemetry()
        self._apply_mode(
            target_mode,
            bool(download_reason),
            heavy_pids,
            ac_online=bool(bat_info["ac_online"]),
        )
        self._publish_status(bat_info)

    def handle_command(self, cmd: str) -> dict[str, Any]:
        if cmd in {"on", "off", "toggle"}:
            if cmd == "toggle":
                new_enabled = not self.enabled
            else:
                new_enabled = cmd == "on"

            if new_enabled != self.enabled:
                self.enabled = new_enabled
                self._save_state()
                bat_info = self._read_battery_telemetry()
                if not self.enabled:
                    self.current_reason = "smart optimization paused by user"
                    self._apply_mode(
                        "off", False, [], ac_online=bool(bat_info["ac_online"])
                    )
                else:
                    self.last_mode_activity_ts.clear()
                    self.current_reason = "smart optimization enabled"
                    self._apply_mode(
                        "light", False, [], ac_online=bool(bat_info["ac_online"])
                    )
                self._publish_status(bat_info)

            return {
                "ok": True,
                "enabled": self.enabled,
                "mode": self.current_mode or ("light" if self.enabled else "off"),
            }

        if cmd == "reload":
            self._apply_config(load_config(self.config_path))
            self._force_reapply = True
            bat_info = self._read_battery_telemetry()
            cur_mode = self.current_mode or ("light" if self.enabled else "off")
            self._apply_mode(
                cur_mode,
                bool(self.last_download_pids),
                self.last_heavy_pids,
                ac_online=bool(bat_info["ac_online"]),
            )
            self._publish_status(bat_info)
            return {
                "ok": True,
                "reloaded": True,
                "enabled": self.enabled,
                "mode": cur_mode,
            }

        if cmd in {"brightness-on", "brightness-off", "brightness-toggle"}:
            bcfg = effective_brightness_config(self.config, self.state)
            if cmd == "brightness-toggle":
                new_val = not bool(bcfg["enabled"])
            else:
                new_val = cmd == "brightness-on"
            self.state["brightness_enabled"] = new_val
            self._save_state()
            self._publish_status()
            return {
                "ok": True,
                "brightness_enabled": new_val,
                "enabled": self.enabled,
                "mode": self.current_mode or ("light" if self.enabled else "off"),
            }

        if cmd in {"webcam-als-on", "webcam-als-off", "webcam-als-toggle"}:
            bcfg = effective_brightness_config(self.config, self.state)
            if cmd == "webcam-als-toggle":
                new_val = not bool(bcfg["webcam_als_enabled"])
            else:
                new_val = cmd == "webcam-als-on"
            self.state["webcam_als_enabled"] = new_val
            self._save_state()
            self._publish_status()
            return {
                "ok": True,
                "webcam_als_enabled": new_val,
                "enabled": self.enabled,
                "mode": self.current_mode or ("light" if self.enabled else "off"),
            }

        return {"ok": False, "error": f"invalid command: {cmd}"}

    def run(self) -> None:
        def _stop(_signum: int, _frame: Any) -> None:
            self.running = False

        def _reload(_signum: int, _frame: Any) -> None:
            self._reload_requested = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGHUP, _reload)

        if CONTROL_SOCK.exists():
            CONTROL_SOCK.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(CONTROL_SOCK))
        os.chmod(CONTROL_SOCK, 0o600)
        srv.listen(8)
        srv.setblocking(False)

        next_tick = 0.0
        try:
            while self.running:
                now = time.monotonic()
                if now >= next_tick or self._reload_requested:
                    self.tick()
                    next_tick = time.monotonic() + self.poll_interval

                timeout = max(0.05, next_tick - time.monotonic())
                try:
                    readable, _, _ = select.select([srv], [], [], timeout)
                except select.error as exc:
                    if exc.args[0] == errno.EINTR:
                        continue
                    raise

                if srv in readable:
                    try:
                        conn, _ = srv.accept()
                    except OSError:
                        continue
                    with conn:
                        conn.settimeout(0.5)
                        try:
                            data = (
                                conn.recv(1024).decode("utf-8", errors="ignore").strip()
                            )
                            req = json.loads(data) if data else {}
                            resp = self.handle_command(str(req.get("cmd", "")))
                        except Exception as exc:
                            resp = {"ok": False, "error": str(exc)}
                        try:
                            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                        except OSError:
                            pass
        finally:
            self._restore_kbd_backlight()
            self._release_inhibitor()
            srv.close()
            if CONTROL_SOCK.exists():
                try:
                    CONTROL_SOCK.unlink()
                except OSError:
                    pass


def main() -> int:
    if os.geteuid() != 0:
        print("smart-powerd must run as root.", file=sys.stderr)
        return 1
    daemon = SmartPowerDaemon()
    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
