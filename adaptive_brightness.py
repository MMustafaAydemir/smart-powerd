#!/usr/bin/env python3
"""
Standalone adaptive display brightness and 3-tier ambient light sensing module.

Runs in the user's graphical session so it works natively with KDE Plasma 6,
GNOME, wlroots, and X11 without root privileges and without waking a suspended
discrete GPU.

3-tier hybrid ambient model:
  1. Hardware ALS (daemon status.json als_lux -> iio-sensor-proxy D-Bus -> sysfs IIO)
  2. Optional privacy-safe webcam ambient luma (RAM-only single frame, skips if busy)
  3. Circadian time-of-day piecewise-linear schedule (always-available fallback + blend)
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
for _path_entry in (str(_SCRIPT_DIR), str(_LIB_DIR)):
    if _path_entry not in sys.path:
        sys.path.insert(0, _path_entry)

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

DEFAULT_BRIGHTNESS_CONFIG: dict[str, Any] = (
    copy.deepcopy(_SHARED_DEFAULT_CONFIG["brightness"])
    if isinstance(_SHARED_DEFAULT_CONFIG, dict)
    and isinstance(_SHARED_DEFAULT_CONFIG.get("brightness"), dict)
    else {
        "enabled": True,
        "ambient_source": "auto",
        "webcam_als_enabled": True,
        "webcam_device": "auto",
        "webcam_interval_sec": 1800,
        "battery_multiplier": 0.85,
        "min_pct": 12,
        "max_pct": 95,
        "smooth_step_pct": 3,
        "manual_override_pause_sec": 1800,
        "schedule": copy.deepcopy(DEFAULT_SCHEDULE),
    }
)

INTERNAL_CONNECTOR_RE = re.compile(
    r"(?:^|[-/_])(?:edp|lvds|dsi)(?:[-/_]?\d+)?(?:$|[-/_])",
    re.IGNORECASE,
)
VIDEO_INDEX_RE = re.compile(r"video(\d+)$")
IR_CAMERA_RE = re.compile(r"(?:^|[\s:_-])(?:ir|infrared)(?:$|[\s:_-])", re.IGNORECASE)

KDE_PM_SERVICE = "org.kde.Solid.PowerManagement"
KDE_BRIGHTNESS_PATH = "/org/kde/Solid/PowerManagement/Actions/BrightnessControl"
KDE_BRIGHTNESS_IFACE = "org.kde.Solid.PowerManagement.Actions.BrightnessControl"

GNOME_PM_SERVICE = "org.gnome.SettingsDaemon.Power"
GNOME_PM_PATH = "/org/gnome/SettingsDaemon/Power"
GNOME_SCREEN_IFACE = "org.gnome.SettingsDaemon.Power.Screen"

SENSOR_PROXY_SERVICE = "net.hadess.SensorProxy"
SENSOR_PROXY_PATH = "/net/hadess/SensorProxy"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

AMBIENT_BLEND_WEIGHT = 0.70
MANUAL_OVERRIDE_THRESHOLD_PCT = 4


def load_brightness_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load brightness config section via smart_power_config with safe fallbacks."""
    if _shared_load_config is not None:
        cfg = _shared_load_config(path) if path is not None else _shared_load_config()
        if isinstance(cfg, dict) and isinstance(cfg.get("brightness"), dict):
            return normalize_brightness_config(cfg["brightness"])
    return normalize_brightness_config(None)


def normalize_brightness_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Merge partial brightness config over safe defaults and sanitize numeric ranges."""
    merged = copy.deepcopy(DEFAULT_BRIGHTNESS_CONFIG)
    if isinstance(cfg, dict):
        for key in merged:
            if key in cfg and cfg[key] is not None:
                merged[key] = copy.deepcopy(cfg[key])

    merged["enabled"] = bool(merged.get("enabled", True))
    source = str(merged.get("ambient_source", "auto")).strip().lower()
    if source not in ("auto", "als", "webcam", "schedule"):
        source = "auto"
    merged["ambient_source"] = source
    merged["webcam_als_enabled"] = bool(merged.get("webcam_als_enabled", True))
    merged["webcam_device"] = str(merged.get("webcam_device", "auto")).strip() or "auto"

    try:
        merged["webcam_interval_sec"] = max(15, int(merged.get("webcam_interval_sec", 1800)))
    except (TypeError, ValueError):
        merged["webcam_interval_sec"] = 1800

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


def lux_to_brightness_pct(
    lux: float,
    min_pct: float = 12.0,
    max_pct: float = 95.0,
) -> float:
    """
    Logarithmically map ambient illuminance (lux) to target display brightness %.

    Calibration targets (with default 12..95% range):
      0 lux     -> 12% (min_pct)
      10 lux    -> ~25%
      100 lux   -> ~53%
      400 lux   -> ~74%
      1500+ lux -> 95% (max_pct)
    """
    low = float(min_pct)
    high = max(low, float(max_pct))
    try:
        val = float(lux)
    except (TypeError, ValueError):
        return low
    if not math.isfinite(val) or val <= 0.0:
        return low

    knee_lux = 8.0
    max_ref_lux = 1500.0
    if val >= max_ref_lux:
        return high

    norm = math.log10(1.0 + (val / knee_lux)) / math.log10(1.0 + (max_ref_lux / knee_lux))
    norm = max(0.0, min(1.0, norm))
    return low + norm * (high - low)


def luma_to_brightness_pct(
    luma: float,
    min_pct: float = 12.0,
    max_pct: float = 95.0,
) -> float:
    """
    Map mean grayscale webcam frame luminance (0..255) to target display brightness %.
    """
    low = float(min_pct)
    high = max(low, float(max_pct))
    try:
        val = float(luma)
    except (TypeError, ValueError):
        return low
    if not math.isfinite(val) or val <= 0.0:
        return low

    clamped = max(0.0, min(255.0, val))
    norm = (clamped / 255.0) ** 0.85
    return low + norm * (high - low)


def compute_target_brightness(
    schedule_pct: float,
    ambient_pct: float | None,
    ac_online: bool,
    battery_multiplier: float = 0.85,
    min_pct: float = 12.0,
    max_pct: float = 95.0,
    ambient_weight: float = AMBIENT_BLEND_WEIGHT,
) -> int:
    """
    Blend circadian schedule with optional ambient reading, apply battery multiplier,
    and clamp into [min_pct, max_pct].
    """
    low = float(min_pct)
    high = max(low, float(max_pct))
    weight = max(0.0, min(1.0, float(ambient_weight)))

    if ambient_pct is not None and math.isfinite(float(ambient_pct)):
        target = weight * float(ambient_pct) + (1.0 - weight) * float(schedule_pct)
    else:
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


def read_iio_als_lux(iio_root: Path = Path("/sys/bus/iio/devices")) -> float | None:
    """Read ambient illuminance in lux from Linux sysfs IIO light sensors, if present."""
    if not iio_root.is_dir():
        return None
    try:
        devices = sorted(iio_root.glob("iio:device*"))
    except OSError:
        return None

    for dev in devices:
        for input_name in ("in_illuminance_input", "in_illuminance0_input"):
            input_file = dev / input_name
            if input_file.is_file():
                try:
                    val = float(input_file.read_text(encoding="utf-8", errors="ignore").strip())
                    if math.isfinite(val) and val >= 0.0:
                        return val
                except (OSError, ValueError):
                    continue

        for raw_name, scale_name in (
            ("in_illuminance_raw", "in_illuminance_scale"),
            ("in_illuminance0_raw", "in_illuminance0_scale"),
        ):
            raw_file = dev / raw_name
            if raw_file.is_file():
                try:
                    raw_val = float(raw_file.read_text(encoding="utf-8", errors="ignore").strip())
                    scale = 1.0
                    scale_file = dev / scale_name
                    if scale_file.is_file():
                        scale = float(scale_file.read_text(encoding="utf-8", errors="ignore").strip())
                    lux = raw_val * scale
                    if math.isfinite(lux) and lux >= 0.0:
                        return lux
                except (OSError, ValueError):
                    continue
    return None


class HardwareALSReader:
    """
    Reads hardware ambient light sensors using:
      1. Daemon-provided als_lux (from /run/smart-powerd/status.json)
      2. iio-sensor-proxy over system D-Bus (net.hadess.SensorProxy)
      3. Direct /sys/bus/iio/devices/iio:device* sysfs fallback
    """

    def __init__(self) -> None:
        self._sensor_proxy_checked = False
        self._sensor_proxy_available = False
        self._sensor_proxy_claimed = False
        self._sensor_proxy_obj: Any = None

    def _init_sensor_proxy(self) -> None:
        if self._sensor_proxy_checked:
            return
        self._sensor_proxy_checked = True
        if dbus is None:
            return
        try:
            bus = dbus.SystemBus()
            obj = bus.get_object(SENSOR_PROXY_SERVICE, SENSOR_PROXY_PATH)
            props = dbus.Interface(obj, DBUS_PROPS_IFACE)
            has_light = bool(props.Get(SENSOR_PROXY_SERVICE, "HasAmbientLight"))
            if not has_light:
                return
            proxy_iface = dbus.Interface(obj, SENSOR_PROXY_SERVICE)
            proxy_iface.ClaimLight()
            self._sensor_proxy_obj = obj
            self._sensor_proxy_claimed = True
            self._sensor_proxy_available = True
        except Exception:
            self._sensor_proxy_available = False
            self._sensor_proxy_obj = None

    def _read_sensor_proxy_lux(self) -> float | None:
        self._init_sensor_proxy()
        if not self._sensor_proxy_available or self._sensor_proxy_obj is None or dbus is None:
            return None
        try:
            props = dbus.Interface(self._sensor_proxy_obj, DBUS_PROPS_IFACE)
            val = float(props.Get(SENSOR_PROXY_SERVICE, "LightLevel"))
            if math.isfinite(val) and val >= 0.0:
                return val
        except Exception:
            self._sensor_proxy_available = False
        return None

    def read_lux(self, daemon_als_lux: float | None = None) -> float | None:
        """Return ambient lux from the highest-priority hardware ALS source, or None."""
        if daemon_als_lux is not None:
            try:
                val = float(daemon_als_lux)
                if math.isfinite(val) and val >= 0.0:
                    return val
            except (TypeError, ValueError):
                pass

        proxy_lux = self._read_sensor_proxy_lux()
        if proxy_lux is not None:
            return proxy_lux

        return read_iio_als_lux()

    def cleanup(self) -> None:
        if self._sensor_proxy_claimed and self._sensor_proxy_obj is not None and dbus is not None:
            try:
                proxy_iface = dbus.Interface(self._sensor_proxy_obj, SENSOR_PROXY_SERVICE)
                proxy_iface.ReleaseLight()
            except Exception:
                pass
            self._sensor_proxy_claimed = False


def _video_sort_key(name: str) -> tuple[int, str]:
    match = VIDEO_INDEX_RE.search(name)
    if match:
        return (int(match.group(1)), name)
    return (9999, name)


def detect_webcam_device(
    preferred: str = "auto",
    v4l_sysfs: Path = Path("/sys/class/video4linux"),
) -> str | None:
    """
    Auto-detect the primary visible-light capture V4L2 device (/dev/video*) using
    sysfs metadata only (without opening USB devices unnecessarily).
    """
    pref = (preferred or "auto").strip()
    if pref and pref.lower() != "auto":
        dev_path = Path(pref)
        if dev_path.exists() and os.access(dev_path, os.R_OK):
            return str(dev_path)
        return None

    candidates: list[tuple[int, int, int, str]] = []
    if v4l_sysfs.is_dir():
        try:
            entries = sorted(v4l_sysfs.iterdir(), key=lambda p: _video_sort_key(p.name))
        except OSError:
            entries = []
        for entry in entries:
            if not entry.name.startswith("video"):
                continue
            dev_node = Path("/dev") / entry.name
            if not dev_node.exists() or not os.access(dev_node, os.R_OK):
                continue
            try:
                idx_str = (entry / "index").read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                idx_str = "0"
            try:
                name_str = (entry / "name").read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                name_str = ""
            # On UVC webcams, index 0 is the capture stream and index 1 is metadata.
            is_capture_index = 0 if idx_str == "0" else 1
            is_ir = 1 if IR_CAMERA_RE.search(name_str) else 0
            dev_num = _video_sort_key(entry.name)[0]
            candidates.append((is_capture_index, is_ir, dev_num, str(dev_node)))

        if candidates:
            candidates.sort()
            return candidates[0][3]

    # Fallback when /sys/class/video4linux is unavailable
    try:
        dev_nodes = sorted(Path("/dev").glob("video*"), key=lambda p: _video_sort_key(p.name))
    except OSError:
        return None
    for node in dev_nodes:
        if os.access(node, os.R_OK):
            return str(node)
    return None


def is_video_device_busy(
    device_path: str,
    proc_root: Path = Path("/proc"),
) -> bool:
    """
    Check /proc/*/fd to verify whether another process currently holds ``device_path``
    open (e.g., Zoom, browser WebRTC, OBS). Returns True if busy.
    """
    if not device_path or not proc_root.is_dir():
        return False

    own_pid = str(os.getpid())
    try:
        resolved_target = os.path.realpath(device_path)
    except OSError:
        resolved_target = device_path
    targets = {device_path, resolved_target}

    try:
        proc_entries = os.scandir(proc_root)
    except OSError:
        return False

    with proc_entries as entries:
        for entry in entries:
            if not entry.name.isdigit() or entry.name == own_pid:
                continue
            fd_dir = Path(entry.path) / "fd"
            try:
                with os.scandir(fd_dir) as fds:
                    for fd_entry in fds:
                        try:
                            link_target = os.readlink(fd_entry.path)
                        except OSError:
                            continue
                        if link_target in targets:
                            return True
            except OSError:
                continue
    return False


WEBCAM_WARMUP_FRAMES = 4
WEBCAM_SHUTTER_MAX_THRESHOLD = 1


def extract_last_raw_frame(
    raw_bytes: bytes,
    frame_count: int = WEBCAM_WARMUP_FRAMES,
) -> bytes:
    """Extract the final raw grayscale frame from ``frame_count`` concatenated frames."""
    if not raw_bytes or frame_count <= 1:
        return raw_bytes
    frame_len = len(raw_bytes) // int(frame_count)
    if frame_len <= 0:
        return raw_bytes
    return raw_bytes[-frame_len:]


def analyze_webcam_frame(
    frame: bytes,
    shutter_max_threshold: int = WEBCAM_SHUTTER_MAX_THRESHOLD,
) -> tuple[float | None, str]:
    """
    Compute mean grayscale luminance (0..255) of a single raw frame.

    Returns ``(None, "webcam-shutter-closed")`` when ``max(frame) <= 1``, which
    cannot happen on an uncovered CMOS sensor due to read noise and indicates a
    closed physical privacy shutter (or disabled camera stream).
    """
    if not frame:
        return None, "webcam-capture-failed"
    if max(frame) <= int(shutter_max_threshold):
        return None, "webcam-shutter-closed"
    return sum(frame) / float(len(frame)), "ok"


def measure_webcam_luma(
    device: str = "auto",
    timeout_sec: float = 5.0,
    warmup_frames: int = WEBCAM_WARMUP_FRAMES,
) -> tuple[float | None, str]:
    """
    Privacy-safe ambient luminance probe via ffmpeg in RAM only.

    - Never writes any image or temporary file to disk.
    - Checks /proc/*/fd first and cleanly skips if another app owns the camera.
    - Captures ``warmup_frames`` (default 4) tiny grayscale frames to stdout pipe
      so UVC auto-exposure settles, inspects only the last frame, rejects closed
      privacy shutters (``max(frame) <= 1``), and exits immediately so USB
      autosuspend puts the webcam back to sleep.
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return None, "ffmpeg-missing"

    dev_path = detect_webcam_device(device)
    if not dev_path:
        return None, "no-webcam"

    if is_video_device_busy(dev_path):
        return None, "webcam-busy"

    frame_count = max(1, int(warmup_frames))
    attempts = [
        [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "v4l2",
            "-video_size",
            "160x120",
            "-i",
            dev_path,
            "-frames:v",
            str(frame_count),
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "v4l2",
            "-i",
            dev_path,
            "-frames:v",
            str(frame_count),
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
    ]

    for cmd in attempts:
        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, "webcam-timeout"
        except OSError:
            return None, "webcam-error"

        if proc.returncode == 0 and proc.stdout:
            last_frame = extract_last_raw_frame(proc.stdout, frame_count=frame_count)
            return analyze_webcam_frame(last_frame)

    return None, "webcam-capture-failed"


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
                    # Attempt one reconnect in case PowerDevil restarted
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
                        # Format: device,class,current,percent%,max
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
            # Fall through to brightnessctl / sysfs if KDE D-Bus temporarily failed

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
    Stateful adaptive brightness controller coordinating:
      - 3-tier ambient light sensing (Hardware ALS -> Optional Webcam -> Circadian Schedule)
      - Battery brightness multiplier & clamping
      - User manual override detection and temporary pause
      - Smooth brightness stepping
    """

    def __init__(
        self,
        backend: BacklightBackend | Any | None = None,
        als_reader: HardwareALSReader | Any | None = None,
    ) -> None:
        self.backend = backend if backend is not None else BacklightBackend()
        self.als_reader = als_reader if als_reader is not None else HardwareALSReader()

        self.last_applied_pct: int | None = None
        self.last_target_pct: int | None = None
        self.last_current_pct: int | None = None
        self.manual_override_until: float = 0.0
        self.last_source: str = "Schedule"
        self.last_ambient_lux: float | None = None
        self.last_ambient_luma: float | None = None
        self.cached_webcam_luma: float | None = None
        self.last_webcam_sample_ts: float = 0.0
        self.last_webcam_status: str = ""
        self._manual_webcam_valid_until: float = 0.0
        self._prev_enabled: bool | None = None

    def clear_manual_override(self) -> None:
        """Resume automatic brightness control immediately."""
        self.manual_override_until = 0.0
        self.last_applied_pct = None

    def ingest_webcam_luma(
        self,
        luma: float | None,
        status: str = "ok",
        now: float | None = None,
        manual: bool = False,
        manual_ttl_sec: float = 600.0,
    ) -> None:
        """Record an asynchronously measured webcam luminance reading."""
        ts = time.time() if now is None else float(now)
        self.last_webcam_sample_ts = ts
        self.last_webcam_status = status
        if luma is not None and math.isfinite(float(luma)):
            self.cached_webcam_luma = max(0.0, min(255.0, float(luma)))
            self.last_ambient_luma = round(self.cached_webcam_luma, 1)
            if manual:
                self._manual_webcam_valid_until = ts + max(60.0, float(manual_ttl_sec))
        elif status in ("webcam-shutter-closed", "no-webcam"):
            # Clear stale webcam reading so brightness falls back cleanly to Schedule
            self.cached_webcam_luma = None
            self.last_ambient_luma = None
            self._manual_webcam_valid_until = 0.0

    def should_sample_webcam(
        self,
        config: dict[str, Any] | None,
        daemon_als_lux: float | None = None,
        now: float | None = None,
        force: bool = False,
    ) -> bool:
        """
        Return True when the caller should launch an asynchronous webcam luminance
        capture (periodic interval elapsed or manual trigger, and no hardware ALS).
        """
        cfg = normalize_brightness_config(config)
        ts = time.time() if now is None else float(now)

        if cfg["ambient_source"] not in ("auto", "webcam"):
            return False

        if not force:
            if not cfg["enabled"]:
                return False
            if ts < self.manual_override_until:
                return False
            if not cfg["webcam_als_enabled"]:
                return False

        # Skip webcam when hardware ALS is available unless user explicitly forced webcam-only mode
        if cfg["ambient_source"] != "webcam":
            hw_lux = self.als_reader.read_lux(daemon_als_lux)
            if hw_lux is not None:
                return False

        if force:
            return True

        interval = float(cfg["webcam_interval_sec"])
        return (self.last_webcam_sample_ts <= 0.0) or ((ts - self.last_webcam_sample_ts) >= interval)

    @staticmethod
    def _current_local_hour() -> float:
        dt = datetime.datetime.now()
        return dt.hour + (dt.minute / 60.0) + (dt.second / 3600.0)

    def update(
        self,
        config: dict[str, Any] | None,
        ac_online: bool,
        als_lux: float | None = None,
        now: float | None = None,
        local_hour: float | None = None,
        force_measure: bool = False,
        webcam_luma_override: float | None = None,
    ) -> dict[str, Any]:
        """
        Run one adaptive brightness evaluation and smoothly step brightness if active.

        Returns the current brightness telemetry dictionary.
        """
        cfg = normalize_brightness_config(config)
        ts = time.time() if now is None else float(now)
        hour = self._current_local_hour() if local_hour is None else float(local_hour)

        if webcam_luma_override is not None:
            self.ingest_webcam_luma(
                webcam_luma_override,
                status="ok",
                now=ts,
                manual=force_measure,
                manual_ttl_sec=max(float(cfg["webcam_interval_sec"]), 300.0),
            )

        # If user toggled Auto Brightness from OFF -> ON, clear any manual pause
        if self._prev_enabled is False and cfg["enabled"]:
            self.clear_manual_override()
        self._prev_enabled = cfg["enabled"]

        if force_measure:
            self.clear_manual_override()

        current_pct = self.backend.get_pct()
        self.last_current_pct = current_pct

        if not cfg["enabled"] and not force_measure:
            self.last_source = "Disabled"
            self.last_target_pct = None
            self.last_applied_pct = None
            return self.get_telemetry()

        if current_pct is None:
            self.last_source = "Unavailable"
            self.last_target_pct = None
            return self.get_telemetry()

        # Check manual override (Fn keys or desktop brightness slider)
        if not force_measure:
            if ts < self.manual_override_until:
                self.last_source = "Manual Pause"
                return self.get_telemetry()
            if self.manual_override_until > 0.0 and ts >= self.manual_override_until:
                # Pause period elapsed cleanly; resume without re-triggering override
                self.manual_override_until = 0.0
                self.last_applied_pct = None

            if detect_manual_override(current_pct, self.last_applied_pct):
                pause_sec = float(cfg["manual_override_pause_sec"])
                if pause_sec > 0.0:
                    self.manual_override_until = ts + pause_sec
                    self.last_applied_pct = None
                    self.last_source = "Manual Pause"
                    return self.get_telemetry()

        min_pct = float(cfg["min_pct"])
        max_pct = float(cfg["max_pct"])
        schedule_pct = interpolate_schedule(hour, cfg["schedule"])

        ambient_source_pref = cfg["ambient_source"]
        ambient_pct: float | None = None
        source_label = "Schedule"

        # Tier 1: Hardware ALS
        hw_lux: float | None = None
        if ambient_source_pref in ("auto", "als"):
            hw_lux = self.als_reader.read_lux(als_lux)
            if hw_lux is not None:
                self.last_ambient_lux = round(hw_lux, 1)
                ambient_pct = lux_to_brightness_pct(hw_lux, min_pct, max_pct)
                source_label = "ALS"
            else:
                self.last_ambient_lux = None

        # Tier 2: Optional Webcam Ambient Sensor (only when no hardware ALS reading)
        if ambient_pct is None and ambient_source_pref in ("auto", "webcam"):
            max_age = max(float(cfg["webcam_interval_sec"]) * 2.5, 600.0)
            has_fresh_webcam = (
                self.cached_webcam_luma is not None
                and self.last_webcam_sample_ts > 0.0
                and (ts - self.last_webcam_sample_ts) <= max_age
            )
            webcam_allowed = (
                cfg["webcam_als_enabled"]
                or force_measure
                or (webcam_luma_override is not None)
                or (ts <= self._manual_webcam_valid_until)
            )
            if has_fresh_webcam and (webcam_allowed or ambient_source_pref == "webcam"):
                assert self.cached_webcam_luma is not None
                ambient_pct = luma_to_brightness_pct(self.cached_webcam_luma, min_pct, max_pct)
                source_label = "Webcam"

        # Tier 3: Circadian Time-of-Day Schedule (fallback + blend)
        target_pct = compute_target_brightness(
            schedule_pct=schedule_pct,
            ambient_pct=ambient_pct,
            ac_online=ac_online,
            battery_multiplier=float(cfg["battery_multiplier"]),
            min_pct=min_pct,
            max_pct=max_pct,
        )

        self.last_target_pct = target_pct
        self.last_source = source_label

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

    def step_ramp(self, step_size: int = 3) -> bool:
        """
        Advance one smooth mini-step toward ``last_target_pct`` (used after manual
        'Measure Room Light Now' so transitions feel smooth without blocking the UI).
        Returns True if additional steps remain.
        """
        if self.last_target_pct is None or self.manual_override_until > 0.0:
            return False
        current_pct = self.backend.get_pct()
        if current_pct is None:
            return False
        self.last_current_pct = current_pct

        if detect_manual_override(current_pct, self.last_applied_pct):
            return False
        if current_pct == self.last_target_pct:
            self.last_applied_pct = current_pct
            return False

        next_pct = step_toward(current_pct, self.last_target_pct, step_size)
        if not self.backend.set_pct(next_pct):
            return False

        actual = self.backend.get_pct()
        if actual is not None and abs(actual - next_pct) <= 2:
            self.last_applied_pct = actual
            self.last_current_pct = actual
        else:
            self.last_applied_pct = next_pct
            self.last_current_pct = next_pct
        return self.last_current_pct != self.last_target_pct

    def get_telemetry(self) -> dict[str, Any]:
        """Return display telemetry fields for smart-power-display.json."""
        return {
            "brightness_pct": self.last_current_pct,
            "brightness_target_pct": self.last_target_pct,
            "brightness_source": self.last_source,
            "ambient_lux": self.last_ambient_lux,
            "ambient_luma": self.last_ambient_luma,
            "manual_override_until": float(self.manual_override_until),
        }

    def cleanup(self) -> None:
        self.als_reader.cleanup()
