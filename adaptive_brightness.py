#!/usr/bin/env python3
"""
Standalone circadian day/night adaptive display brightness module.

Runs in the user's graphical session so it works natively with KDE Plasma 6,
GNOME, wlroots, X11, and Windows (via injectable backend) without elevated
privileges, without waking a suspended discrete GPU, and without touching any
camera or external sensor hardware.

Features:
  - Smooth 24-hour wrap-around circadian time-of-day brightness curve
  - Battery brightness multiplier and min/max clamping
  - Automatic user manual-override detection & temporary pause
  - Smooth brightness stepping
  - Portable, dGPU-safe backlight selection (rejects unconnected NVIDIA nodes)
"""

from __future__ import annotations

import copy
import datetime
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_LIB_DIR = Path("/usr/local/lib/smart-power")
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
if (sys.platform != "win32" or _LIB_DIR.is_dir()) and str(_LIB_DIR) not in sys.path:
    sys.path.append(str(_LIB_DIR))

try:
    from smart_power_config import DEFAULT_CONFIG as _SHARED_DEFAULT_CONFIG
    from smart_power_config import load_config as _shared_load_config
except ImportError:
    _SHARED_DEFAULT_CONFIG = None
    _shared_load_config = None

try:
    import dbus  # type: ignore[import-untyped]
except ImportError:
    dbus = None

DEFAULT_SCHEDULE: list[list[float]] = [
    [0, 22],
    [6, 25],
    [8, 55],
    [12, 78],
    [17, 65],
    [20, 42],
    [22, 28],
]

_FALLBACK_BRIGHTNESS_CONFIG: dict[str, Any] = {
    "enabled": True,
    "battery_multiplier": 0.85,
    "min_pct": 12,
    "max_pct": 95,
    "smooth_step_pct": 3,
    "manual_override_pause_sec": 1800,
    "schedule": copy.deepcopy(DEFAULT_SCHEDULE),
}

DEFAULT_BRIGHTNESS_CONFIG: dict[str, Any] = (
    {
        k: copy.deepcopy(_SHARED_DEFAULT_CONFIG["brightness"].get(k, v))
        for k, v in _FALLBACK_BRIGHTNESS_CONFIG.items()
    }
    if isinstance(_SHARED_DEFAULT_CONFIG, dict)
    and isinstance(_SHARED_DEFAULT_CONFIG.get("brightness"), dict)
    else copy.deepcopy(_FALLBACK_BRIGHTNESS_CONFIG)
)

INTERNAL_CONNECTOR_RE = re.compile(
    r"(?:^|[-/_])(?:edp|lvds|dsi)(?:[-/_]?\d+)?(?:$|[-/_])",
    re.IGNORECASE,
)

KDE_PM_SERVICE = "org.kde.Solid.PowerManagement"
KDE_BRIGHTNESS_PATH = "/org/kde/Solid/PowerManagement/Actions/BrightnessControl"
KDE_BRIGHTNESS_IFACE = "org.kde.Solid.PowerManagement.Actions.BrightnessControl"

GNOME_PM_SERVICE = "org.gnome.SettingsDaemon.Power"
GNOME_PM_PATH = "/org/gnome/SettingsDaemon/Power"
GNOME_SCREEN_IFACE = "org.gnome.SettingsDaemon.Power.Screen"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

MANUAL_OVERRIDE_THRESHOLD_PCT = 4


def load_brightness_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load brightness config section via smart_power_config with safe fallbacks."""
    if _shared_load_config is not None:
        cfg = _shared_load_config(path) if path is not None else _shared_load_config()
        if isinstance(cfg, dict) and isinstance(cfg.get("brightness"), dict):
            return normalize_brightness_config(cfg["brightness"])
    return normalize_brightness_config(None)


def normalize_brightness_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """
    Merge partial brightness config over safe defaults, sanitize numeric ranges,
    and silently ignore any unknown or legacy keys.
    """
    merged = copy.deepcopy(DEFAULT_BRIGHTNESS_CONFIG)
    if isinstance(cfg, dict):
        for key in merged:
            if key in cfg and cfg[key] is not None:
                merged[key] = copy.deepcopy(cfg[key])

    merged["enabled"] = bool(merged.get("enabled", True))

    try:
        mult = float(merged.get("battery_multiplier", 0.85))
        merged["battery_multiplier"] = max(0.1, min(1.5, mult)) if math.isfinite(mult) else 0.85
    except (TypeError, ValueError):
        merged["battery_multiplier"] = 0.85

    try:
        min_pct = int(round(float(merged.get("min_pct", 12))))
    except (TypeError, ValueError):
        min_pct = 12
    try:
        max_pct = int(round(float(merged.get("max_pct", 95))))
    except (TypeError, ValueError):
        max_pct = 95
    min_pct = max(1, min(100, min_pct))
    max_pct = max(min_pct, min(100, max_pct))
    merged["min_pct"] = min_pct
    merged["max_pct"] = max_pct

    try:
        merged["smooth_step_pct"] = max(1, min(50, int(merged.get("smooth_step_pct", 3))))
    except (TypeError, ValueError):
        merged["smooth_step_pct"] = 3

    try:
        merged["manual_override_pause_sec"] = max(
            0, int(merged.get("manual_override_pause_sec", 1800))
        )
    except (TypeError, ValueError):
        merged["manual_override_pause_sec"] = 1800

    sched = merged.get("schedule")
    if not isinstance(sched, list) or not sched:
        merged["schedule"] = copy.deepcopy(DEFAULT_SCHEDULE)

    return merged


def interpolate_schedule(
    hour_float: float,
    schedule: Sequence[Sequence[float]] | None,
) -> float:
    """
    Smooth wrap-around piecewise-linear circadian brightness interpolation across 24 hours.

    ``schedule`` is a sequence of ``[hour, brightness_pct]`` pairs (e.g.
    ``[[0, 22], [6, 25], [8, 55], [12, 78], [17, 65], [20, 42], [22, 28]]``).
    Hours wrap smoothly across midnight (23:59 -> 00:00).
    """
    hour = float(hour_float) % 24.0
    raw_points = schedule if schedule else DEFAULT_SCHEDULE

    by_hour: dict[float, float] = {}
    for item in raw_points:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            h = float(item[0]) % 24.0
            v = float(item[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(h) and math.isfinite(v):
            by_hour[h] = v

    if not by_hour:
        by_hour = {float(h) % 24.0: float(v) for h, v in DEFAULT_SCHEDULE}

    points = sorted(by_hour.items(), key=lambda pair: pair[0])
    if len(points) == 1:
        return points[0][1]

    for idx in range(len(points) - 1):
        h1, v1 = points[idx]
        h2, v2 = points[idx + 1]
        if h1 <= hour <= h2:
            span = h2 - h1
            if span <= 0.0:
                return v1
            frac = (hour - h1) / span
            return v1 + frac * (v2 - v1)

    # Wrap-around segment from the last point of the day to the first point of the next day.
    h1, v1 = points[-1]
    h2, v2 = points[0][0] + 24.0, points[0][1]
    adj_hour = hour if hour >= h1 else hour + 24.0
    span = h2 - h1
    if span <= 0.0:
        return v1
    frac = (adj_hour - h1) / span
    return v1 + frac * (v2 - v1)


def compute_target_brightness(
    schedule_pct: float,
    ac_online: bool = True,
    battery_multiplier: float = 0.85,
    min_pct: float = 12.0,
    max_pct: float = 95.0,
) -> int:
    """
    Apply battery brightness multiplier (when unplugged) to the circadian schedule
    target and clamp cleanly into ``[min_pct, max_pct]``.
    """
    low = float(min_pct)
    high = max(low, float(max_pct))
    target = float(schedule_pct)

    if not ac_online:
        target *= float(battery_multiplier)

    clamped = max(low, min(high, target))
    return int(round(clamped))


def step_toward(current_pct: int, target_pct: int, max_step: int = 3) -> int:
    """Move ``current_pct`` toward ``target_pct`` by at most ``max_step`` percent."""
    step = max(1, int(max_step))
    cur = int(current_pct)
    tgt = int(target_pct)
    diff = tgt - cur
    if abs(diff) <= step:
        return tgt
    return cur + (step if diff > 0 else -step)


def detect_manual_override(
    current_pct: int,
    last_applied_pct: int | None,
    threshold: int = MANUAL_OVERRIDE_THRESHOLD_PCT,
) -> bool:
    """Return True if actual display brightness diverged from our last applied value."""
    if last_applied_pct is None:
        return False
    return abs(int(current_pct) - int(last_applied_pct)) >= max(1, int(threshold))


@dataclass(frozen=True)
class BacklightEntry:
    """Metadata describing a /sys/class/backlight/* interface."""

    name: str
    path: str = ""
    device_path: str = ""
    backlight_type: str = ""
    connector_status: str = ""
    max_brightness: int = 0


def discover_sysfs_backlights(
    sysfs_root: Path = Path("/sys/class/backlight"),
) -> list[BacklightEntry]:
    """
    Discover backlight devices under /sys/class/backlight without touching dGPU
    brightness registers (reads only symlinks, connector status, and type).
    """
    entries: list[BacklightEntry] = []
    if not sysfs_root.is_dir():
        return entries

    try:
        children = sorted(sysfs_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return entries

    for bl_dir in children:
        name = bl_dir.name
        dev_link = bl_dir / "device"
        try:
            device_path = os.path.realpath(dev_link) if dev_link.exists() else ""
        except OSError:
            device_path = ""

        try:
            bl_type = (bl_dir / "type").read_text(encoding="utf-8", errors="ignore").strip().lower()
        except OSError:
            bl_type = ""

        status_path = dev_link / "status"
        try:
            conn_status = (
                status_path.read_text(encoding="utf-8", errors="ignore").strip().lower()
                if status_path.is_file()
                else ""
            )
        except OSError:
            conn_status = ""

        # Avoid reading brightness registers on discrete NVIDIA backlight nodes during discovery
        max_bl = 0
        if "nvidia" not in name.lower():
            try:
                max_bl = int(
                    (bl_dir / "max_brightness").read_text(encoding="utf-8", errors="ignore").strip()
                )
            except (OSError, ValueError):
                max_bl = 0

        entries.append(
            BacklightEntry(
                name=name,
                path=str(bl_dir),
                device_path=device_path,
                backlight_type=bl_type,
                connector_status=conn_status,
                max_brightness=max_bl,
            )
        )
    return entries


def _extract_backlight_fields(entry: Any) -> tuple[str, str, str, str]:
    """Normalize BacklightEntry, dict, or str into (name, device_path, bl_type, conn_status)."""
    if isinstance(entry, BacklightEntry):
        return (
            entry.name,
            entry.device_path,
            entry.backlight_type.lower(),
            entry.connector_status.lower(),
        )
    if isinstance(entry, dict):
        name = str(entry.get("name", ""))
        device_path = str(
            entry.get("device_path")
            or entry.get("device")
            or entry.get("realpath")
            or entry.get("parent")
            or ""
        )
        bl_type = str(entry.get("backlight_type") or entry.get("type") or "").lower()
        conn_status = str(
            entry.get("connector_status") or entry.get("status") or ""
        ).lower()
        return name, device_path, bl_type, conn_status
    if isinstance(entry, str):
        return entry, "", "", ""
    return (
        str(getattr(entry, "name", "")),
        str(getattr(entry, "device_path", getattr(entry, "device", ""))),
        str(getattr(entry, "backlight_type", getattr(entry, "type", ""))).lower(),
        str(getattr(entry, "connector_status", getattr(entry, "status", ""))).lower(),
    )


def select_safe_backlight(entries: Sequence[Any]) -> Any | None:
    """
    Choose the safest internal-panel backlight device from ``entries`` while
    never selecting a suspended/unconnected discrete NVIDIA backlight.

    Preference order:
      1. Connected internal eDP/LVDS/DSI panel backlight (amdgpu_bl*, intel_backlight, etc.)
      2. Non-NVIDIA raw/firmware/platform backlight (e.g. acpi_video0)
      3. Rejects nvidia_* backlights when any safe candidate exists OR when the
         nvidia_* node is not explicitly attached to a connected internal panel.
    """
    if not entries:
        return None

    safe_non_nvidia: list[tuple[int, str, Any]] = []
    connected_nvidia: list[tuple[int, str, Any]] = []

    for item in entries:
        name, device_path, bl_type, conn_status = _extract_backlight_fields(item)
        if not name:
            continue
        if conn_status == "disconnected":
            continue

        lower_name = name.lower()
        is_nvidia = "nvidia" in lower_name
        has_internal_panel = bool(INTERNAL_CONNECTOR_RE.search(device_path))
        is_connected = conn_status == "connected"

        score = 0
        if is_connected:
            score += 100
        if has_internal_panel:
            score += 60
        if lower_name.startswith(
            ("amdgpu_bl", "intel_backlight", "i915", "apple-panel-bl", "gmux_backlight")
        ):
            score += 40
        elif lower_name.startswith("acpi_video"):
            score += 15

        if bl_type == "raw":
            score += 25
        elif bl_type == "firmware":
            score += 15
        elif bl_type == "platform":
            score += 10

        if is_nvidia:
            # Never touch an NVIDIA backlight unless it is explicitly on a connected internal panel
            if is_connected and has_internal_panel:
                connected_nvidia.append((score - 200, name, item))
        else:
            safe_non_nvidia.append((score, name, item))

    pool = safe_non_nvidia if safe_non_nvidia else connected_nvidia
    if not pool:
        return None

    # Highest score wins; deterministic lexicographic tie-breaker on name
    pool.sort(key=lambda tup: (-tup[0], tup[1]))
    return pool[0][2]


class BacklightBackend:
    """
    Portable, dGPU-safe backlight controller with multi-backend fallback:
      1. KDE Plasma 6 PowerDevil D-Bus (setBrightnessSilent — zero OSD popup, syncs slider)
      2. GNOME SettingsDaemon Power D-Bus (syncs GNOME slider)
      3. brightnessctl CLI pinned explicitly to the safe internal backlight device
      4. Direct /sys/class/backlight/<safe_device> sysfs read/write
    """

    def __init__(self, sysfs_root: Path = Path("/sys/class/backlight")) -> None:
        self._sysfs_root = sysfs_root
        self._backend_name = "none"
        self._safe_sysfs_entry: BacklightEntry | None = None
        self._kde_iface: Any = None
        self._gnome_props: Any = None
        self._brightnessctl_bin = shutil.which("brightnessctl")
        self._detect_backend()

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def safe_sysfs_device(self) -> str | None:
        return self._safe_sysfs_entry.name if self._safe_sysfs_entry else None

    def _detect_backend(self) -> None:
        # Discover safe sysfs device first (read-only, dGPU-safe) for fallback / inspection
        entries = discover_sysfs_backlights(self._sysfs_root)
        selected = select_safe_backlight(entries)
        self._safe_sysfs_entry = selected if isinstance(selected, BacklightEntry) else None

        # Backend 1: KDE Plasma 6 D-Bus
        if self._try_connect_kde():
            self._backend_name = "kde_dbus"
            return

        # Backend 1b: GNOME SettingsDaemon D-Bus
        if self._try_connect_gnome():
            self._backend_name = "gnome_dbus"
            return

        # Backend 2: brightnessctl CLI pinned to safe internal panel device
        if self._brightnessctl_bin and self._safe_sysfs_entry is not None:
            self._backend_name = f"brightnessctl:{self._safe_sysfs_entry.name}"
            return

        # Backend 3: Direct sysfs on safe internal panel device
        if self._safe_sysfs_entry is not None:
            self._backend_name = f"sysfs:{self._safe_sysfs_entry.name}"
            return

        self._backend_name = "none"

    def _try_connect_kde(self) -> bool:
        if dbus is None:
            return False
        try:
            bus = dbus.SessionBus()
            obj = bus.get_object(KDE_PM_SERVICE, KDE_BRIGHTNESS_PATH)
            iface = dbus.Interface(obj, KDE_BRIGHTNESS_IFACE)
            mx = int(iface.brightnessMax())
            if mx > 0:
                self._kde_iface = iface
                return True
        except Exception:
            self._kde_iface = None
        return False

    def _try_connect_gnome(self) -> bool:
        if dbus is None:
            return False
        try:
            bus = dbus.SessionBus()
            obj = bus.get_object(GNOME_PM_SERVICE, GNOME_PM_PATH)
            props = dbus.Interface(obj, DBUS_PROPS_IFACE)
            cur = int(props.Get(GNOME_SCREEN_IFACE, "Brightness"))
            if 0 <= cur <= 100:
                self._gnome_props = props
                return True
        except Exception:
            self._gnome_props = None
        return False

    def _read_sysfs_pct(self) -> int | None:
        if self._safe_sysfs_entry is None:
            return None
        bl_dir = Path(self._safe_sysfs_entry.path)
        try:
            cur = int((bl_dir / "brightness").read_text(encoding="utf-8", errors="ignore").strip())
            mx = int((bl_dir / "max_brightness").read_text(encoding="utf-8", errors="ignore").strip())
            if mx <= 0:
                return None
            return max(0, min(100, int(round(cur * 100.0 / mx))))
        except (OSError, ValueError):
            return None

    def get_pct(self) -> int | None:
        """Read current display brightness percentage (0..100), or None if unavailable."""
        if self._backend_name == "kde_dbus":
            if self._kde_iface is not None:
                try:
                    mx = int(self._kde_iface.brightnessMax())
                    cur = int(self._kde_iface.brightness())
                    if mx > 0:
                        return max(0, min(100, int(round(cur * 100.0 / mx))))
                except Exception:
                    if self._try_connect_kde() and self._kde_iface is not None:
                        try:
                            mx = int(self._kde_iface.brightnessMax())
                            cur = int(self._kde_iface.brightness())
                            if mx > 0:
                                return max(0, min(100, int(round(cur * 100.0 / mx))))
                        except Exception:
                            pass
            return self._read_sysfs_pct()

        if self._backend_name == "gnome_dbus":
            if self._gnome_props is not None:
                try:
                    cur = int(self._gnome_props.Get(GNOME_SCREEN_IFACE, "Brightness"))
                    if 0 <= cur <= 100:
                        return cur
                except Exception:
                    pass
            return self._read_sysfs_pct()

        if self._backend_name.startswith(("brightnessctl:", "sysfs:")):
            pct = self._read_sysfs_pct()
            if pct is not None:
                return pct
            if self._brightnessctl_bin and self._safe_sysfs_entry is not None:
                try:
                    proc = subprocess.run(
                        [self._brightnessctl_bin, "-d", self._safe_sysfs_entry.name, "-m", "info"],
                        capture_output=True,
                        text=True,
                        timeout=2.0,
                        check=False,
                    )
                    if proc.returncode == 0 and proc.stdout:
                        parts = proc.stdout.strip().split(",")
                        if len(parts) >= 4 and parts[3].endswith("%"):
                            return max(0, min(100, int(parts[3].rstrip("%"))))
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
        return None

    def set_pct(self, pct: int) -> bool:
        """Set display brightness percentage (1..100) using the active safe backend."""
        target = max(1, min(100, int(round(pct))))

        if self._backend_name == "kde_dbus":
            if self._kde_iface is None:
                self._try_connect_kde()
            if self._kde_iface is not None:
                try:
                    mx = int(self._kde_iface.brightnessMax())
                    if mx > 0:
                        raw_val = max(1, min(mx, int(round(mx * target / 100.0))))
                        self._kde_iface.setBrightnessSilent(raw_val)
                        return True
                except Exception:
                    self._kde_iface = None

        if self._backend_name == "gnome_dbus" and self._gnome_props is not None and dbus is not None:
            try:
                self._gnome_props.Set(GNOME_SCREEN_IFACE, "Brightness", dbus.Int32(target))
                return True
            except Exception:
                self._gnome_props = None

        if self._brightnessctl_bin and self._safe_sysfs_entry is not None:
            try:
                proc = subprocess.run(
                    [
                        self._brightnessctl_bin,
                        "-d",
                        self._safe_sysfs_entry.name,
                        "-q",
                        "set",
                        f"{target}%",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                    check=False,
                )
                if proc.returncode == 0:
                    return True
            except (OSError, subprocess.SubprocessError):
                pass

        if self._safe_sysfs_entry is not None:
            bl_dir = Path(self._safe_sysfs_entry.path)
            try:
                mx = int((bl_dir / "max_brightness").read_text(encoding="utf-8", errors="ignore").strip())
                if mx > 0:
                    raw_val = max(1, min(mx, int(round(mx * target / 100.0))))
                    (bl_dir / "brightness").write_text(f"{raw_val}\n", encoding="utf-8")
                    return True
            except (OSError, ValueError):
                pass

        return False


class AdaptiveBrightnessController:
    """
    Stateful day/night circadian adaptive brightness controller coordinating:
      - 24-hour wrap-around time-of-day brightness schedule
      - Battery brightness multiplier & min/max clamping
      - User manual override detection and temporary pause
      - Smooth brightness stepping
    """

    def __init__(
        self,
        backend: BacklightBackend | Any | None = None,
    ) -> None:
        self.backend = backend if backend is not None else BacklightBackend()

        self.last_applied_pct: int | None = None
        self.last_target_pct: int | None = None
        self.last_current_pct: int | None = None
        self.manual_override_until: float = 0.0
        self.last_source: str = "Schedule"
        self._prev_enabled: bool | None = None

    def clear_manual_override(self) -> None:
        """Resume automatic brightness control immediately."""
        self.manual_override_until = 0.0
        self.last_applied_pct = None

    @staticmethod
    def _current_local_hour() -> float:
        dt = datetime.datetime.now()
        return dt.hour + (dt.minute / 60.0) + (dt.second / 3600.0)

    def update(
        self,
        config: dict[str, Any] | None,
        ac_online: bool,
        now: float | None = None,
        local_hour: float | None = None,
        force_apply: bool = False,
    ) -> dict[str, Any]:
        """
        Run one circadian schedule evaluation and smoothly step brightness if active.

        Returns the current brightness telemetry dictionary.
        """
        cfg = normalize_brightness_config(config)
        ts = time.time() if now is None else float(now)
        hour = self._current_local_hour() if local_hour is None else float(local_hour)

        # If user toggled Auto Brightness from OFF -> ON, clear any manual pause
        if self._prev_enabled is False and cfg["enabled"]:
            self.clear_manual_override()
        self._prev_enabled = cfg["enabled"]

        if force_apply:
            self.clear_manual_override()

        current_pct = self.backend.get_pct()
        self.last_current_pct = current_pct

        if not cfg["enabled"] and not force_apply:
            self.last_source = "Disabled"
            self.last_target_pct = None
            self.last_applied_pct = None
            return self.get_telemetry()

        if current_pct is None:
            self.last_source = "Unavailable"
            self.last_target_pct = None
            return self.get_telemetry()

        # Check manual override (Fn keys or desktop brightness slider)
        if not force_apply:
            if ts < self.manual_override_until:
                self.last_source = "Manual Pause"
                return self.get_telemetry()
            if self.manual_override_until > 0.0 and ts >= self.manual_override_until:
                self.manual_override_until = 0.0
                self.last_applied_pct = None

            if detect_manual_override(current_pct, self.last_applied_pct):
                pause_sec = float(cfg["manual_override_pause_sec"])
                if pause_sec > 0.0:
                    self.manual_override_until = ts + pause_sec
                    self.last_applied_pct = None
                    self.last_source = "Manual Pause"
                    return self.get_telemetry()

        schedule_pct = interpolate_schedule(hour, cfg["schedule"])
        target_pct = compute_target_brightness(
            schedule_pct=schedule_pct,
            ac_online=ac_online,
            battery_multiplier=float(cfg["battery_multiplier"]),
            min_pct=float(cfg["min_pct"]),
            max_pct=float(cfg["max_pct"]),
        )

        self.last_target_pct = target_pct
        self.last_source = "Schedule"

        if current_pct == target_pct:
            self.last_applied_pct = current_pct
            return self.get_telemetry()

        next_pct = step_toward(current_pct, target_pct, int(cfg["smooth_step_pct"]))
        if self.backend.set_pct(next_pct):
            actual = self.backend.get_pct()
            if actual is not None and abs(actual - next_pct) <= 2:
                self.last_applied_pct = actual
                self.last_current_pct = actual
            else:
                self.last_applied_pct = next_pct
                self.last_current_pct = next_pct
        return self.get_telemetry()

    def get_telemetry(self) -> dict[str, Any]:
        """Return brightness telemetry fields for smart-power-display.json."""
        return {
            "brightness_pct": self.last_current_pct,
            "brightness_target_pct": self.last_target_pct,
            "brightness_source": self.last_source,
            "manual_override_until": float(self.manual_override_until),
        }

    def cleanup(self) -> None:
        """Clean no-op hook for lifecycle symmetry."""
        return None
