#!/usr/bin/env python3
"""
smart_power_win.py: Adaptive Windows 10/11 laptop power engine, PyQt6 tray UI, and CLI.

Reuses the shared cross-platform modules from the smart-powerd repository:
  - smart_power_config.py  (TOML config loading & schema defaults)
  - display_refresh.py     (3-tier adaptive refresh rate selection)
  - adaptive_brightness.py (pure day/night circadian brightness schedule)

Features on Windows 10/11:
  - Win32 GetSystemPowerStatus + optional WMI root\\WMI BatteryStatus live wattage
  - Win32 powrprof.dll PowerSetActiveOverlayScheme (Better Battery / Balanced / Best Performance)
    with safe powercfg /setactive fallback
  - powercfg Wireless Adapter Power Saving, PCIe ASPM, and USB Selective Suspend
    with original baseline saved in %LOCALAPPDATA%\\smart-powerd\\baseline.json
  - Win32 SetThreadExecutionState sleep inhibition during active downloads
  - Windows-native workload classification via psutil with 12s downgrade cooldown
    and ABOVE_NORMAL_PRIORITY_CLASS boost for active builds/games
  - 3-tier adaptive display refresh rate switching via Win32 user32.dll
    (EnumDisplayDevicesW / EnumDisplaySettingsW / ChangeDisplaySettingsExW)
  - Pure day/night circadian adaptive brightness via WMI WmiMonitorBrightness /
    WmiMonitorBrightnessMethods (zero camera or ambient sensor access)
  - PyQt6 system tray UI + CLI (--tray, --headless, --once, --status, --restore, --version)
  - PyInstaller frozen executable support (smart-powerd.exe)

All Windows-only Win32 calls are guarded behind ``sys.platform == "win32"`` so
pure-logic helpers can be imported and unit-tested cleanly on Linux and Windows CI.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

IS_WINDOWS = sys.platform == "win32"
IS_FROZEN = bool(getattr(sys, "frozen", False))
CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
VERSION = "2.3.0"

# Ensure shared repository modules are importable and take precedence over any
# system-installed copy whether launched from repo/windows/smart_power_win.py
# OR installed side-by-side in %LOCALAPPDATA%\smart-powerd.
_THIS_DIR = Path(__file__).resolve().parent
_PARENT_DIR = _THIS_DIR.parent


def _prioritize_local_paths() -> None:
    if IS_FROZEN:
        return
    for _path_entry in (str(_PARENT_DIR), str(_THIS_DIR)):
        if _path_entry in sys.path:
            sys.path.remove(_path_entry)
        sys.path.insert(0, _path_entry)


_prioritize_local_paths()

import smart_power_config  # noqa: E402
from smart_power_config import (  # noqa: E402
    DEFAULT_CONFIG,
    effective_brightness_config,
    load_config,
)

_prioritize_local_paths()

from display_refresh import (  # noqa: E402
    DisplayMode,
    PanelDisplayInfo,
    choose_refresh_modes,
    select_target_mode,
)

_prioritize_local_paths()

from adaptive_brightness import (  # noqa: E402
    AdaptiveBrightnessController,
    normalize_brightness_config,
)

try:
    import psutil  # type: ignore[import-untyped]
except ImportError:
    psutil = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Windows Power Overlay / Scheme / Setting GUIDs
# ---------------------------------------------------------------------------

OVERLAY_BETTER_BATTERY = "961cc777-2547-4f9d-8174-7d86181b8a7a"
OVERLAY_BALANCED = "00000000-0000-0000-0000-000000000000"
OVERLAY_BEST_PERFORMANCE = "ded574b5-45a0-4f42-8737-46345c09c238"

OVERLAY_NAMES: dict[str, str] = {
    OVERLAY_BETTER_BATTERY: "Better Battery",
    OVERLAY_BALANCED: "Balanced",
    OVERLAY_BEST_PERFORMANCE: "Best Performance",
}

SCHEME_BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
SCHEME_HIGH_PERFORMANCE = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

SUBGROUP_WIFI = "19cbb8fa-5279-450e-9fac-8a3d5fedd0c1"
SETTING_WIFI_PS = "12bbebe6-58d6-4636-95bb-3217ef867c1a"

SUBGROUP_PCIE = "501a4d13-42af-4429-9fd1-a8218c268e20"
SETTING_PCIE_ASPM = "ee12f906-d277-404b-b6da-e5fa1a576df5"

SUBGROUP_USB = "2a737441-1930-4402-8d77-b2bebba308a3"
SETTING_USB_SUSPEND = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"

WIFI_PS_LABELS: dict[int, str] = {
    0: "Max Performance",
    1: "Low Power Saving",
    2: "Medium Power Saving",
    3: "Maximum Power Saving",
}

# Win32 SetThreadExecutionState flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

# Win32 Display constants
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004
ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
DM_BITSPERPEL = 0x00040000
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000
CDS_TEST = 0x00000002
DISP_CHANGE_SUCCESSFUL = 0

MODE_RANK: dict[str, int] = {
    "off": 0,
    "light": 1,
    "download": 2,
    "heavy": 3,
}

MODE_COLORS: dict[str, str] = {
    "light": "#2ecc71",     # Emerald green
    "download": "#3498db",  # Sky blue
    "heavy": "#f39c12",     # Amber/orange
    "off": "#7f8c8d",       # Muted gray
}

MODE_LABELS: dict[str, str] = {
    "light": "Light",
    "download": "Download",
    "heavy": "Heavy",
    "off": "Unrestricted (OFF)",
}

# ---------------------------------------------------------------------------
# Windows Workload Classification Tables
# ---------------------------------------------------------------------------

WINDOWS_SYSTEM_EXCLUDED: frozenset[str] = frozenset(
    {
        "system",
        "system idle process",
        "registry",
        "memory compression",
        "idle",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
        "lsaiso.exe",
        "svchost.exe",
        "fontdrvhost.exe",
        "dwm.exe",
        "explorer.exe",
        "shellexperiencehost.exe",
        "startmenuexperiencehost.exe",
        "searchhost.exe",
        "searchindexer.exe",
        "searchapp.exe",
        "taskmgr.exe",
        "runtimebroker.exe",
        "audiodg.exe",
        "spoolsv.exe",
        "msmpeng.exe",
        "nissrv.exe",
        "securityhealthservice.exe",
        "securityhealthsystray.exe",
        "smartscreen.exe",
        "wuauclt.exe",
        "tiworker.exe",
        "trustedinstaller.exe",
        "conhost.exe",
        "sihost.exe",
        "ctfmon.exe",
        "taskhostw.exe",
        "wmiprvse.exe",
        "dllhost.exe",
        "applicationframehost.exe",
        "textinputhost.exe",
        "widgets.exe",
        "smart-powerd.exe",
        "smart_power_win.exe",
        "smart-power-win.exe",
    }
)

WINDOWS_HEAVY_IMMEDIATE: frozenset[str] = frozenset(
    {
        "cl.exe",
        "clang.exe",
        "clang++.exe",
        "clang-cl.exe",
        "gcc.exe",
        "g++.exe",
        "cc1.exe",
        "cc1plus.exe",
        "rustc.exe",
        "msbuild.exe",
        "cmake.exe",
        "ninja.exe",
        "nmake.exe",
        "make.exe",
        "mingw32-make.exe",
        "lld.exe",
        "lld-link.exe",
        "link.exe",
        "javac.exe",
        "gradle.exe",
        "blender.exe",
        "handbrakecli.exe",
        "llama-server.exe",
    }
)

WINDOWS_HEAVY_CPU_GATED: frozenset[str] = frozenset(
    {
        "devenv.exe",
        "rider64.exe",
        "code.exe",
        "pycharm64.exe",
        "idea64.exe",
        "clion64.exe",
        "webstorm64.exe",
        "goland64.exe",
        "studio64.exe",
        "dotnet.exe",
        "python.exe",
        "python3.exe",
        "pythonw.exe",
        "node.exe",
        "java.exe",
        "javaw.exe",
        "ollama.exe",
    }
)

WINDOWS_DOWNLOAD_IMMEDIATE: frozenset[str] = frozenset(
    {
        "winget.exe",
        "choco.exe",
        "scoop.exe",
        "pip.exe",
        "pip3.exe",
        "uv.exe",
        "npm.cmd",
        "npm.exe",
        "pnpm.cmd",
        "pnpm.exe",
        "yarn.cmd",
        "yarn.exe",
        "bun.exe",
        "aria2c.exe",
        "curl.exe",
        "wget.exe",
        "rsync.exe",
        "fdm.exe",
        "idman.exe",
        "jdownloader2.exe",
    }
)

GAME_LAUNCHER_PARENTS: frozenset[str] = frozenset(
    {
        "steam.exe",
        "epicgameslauncher.exe",
        "eadesktop.exe",
        "upc.exe",
        "battle.net.exe",
        "galaxyclient.exe",
        "playnite.fullscreenapp.exe",
        "xboxpcapp.exe",
    }
)

GAME_LAUNCHER_HELPERS: frozenset[str] = frozenset(
    {
        "steam.exe",
        "steamwebhelper.exe",
        "steamservice.exe",
        "gameoverlayui.exe",
        "gameoverlayui64.exe",
        "crashhandler.exe",
        "crashhandler64.exe",
        "steamtours.exe",
        "epicgameslauncher.exe",
        "epicwebhelper.exe",
        "eosoverlayrenderer-win64-shipping.exe",
        "crashreportclient.exe",
        "eadesktop.exe",
        "eabackgroundservice.exe",
        "ealaunchhelper.exe",
        "eaconnect_microsoft.exe",
        "upc.exe",
        "ubisoftgamelauncher.exe",
        "ubisoftgamelauncher64.exe",
        "battle.net.exe",
        "battle.net helper.exe",
        "agent.exe",
        "galaxyclient.exe",
        "galaxyclient helper.exe",
        "xboxpcapp.exe",
        "gamingservices.exe",
        "gamingservicesnet.exe",
        "cmd.exe",
        "conhost.exe",
        "powershell.exe",
        "pwsh.exe",
    }
)

GAME_PATH_MARKERS: tuple[str, ...] = (
    "\\steamapps\\common\\",
    "/steamapps/common/",
    "\\epic games\\",
    "/epic games/",
    "\\xboxgames\\",
    "/xboxgames/",
    "\\riot games\\",
    "/riot games/",
    "\\gog galaxy\\games\\",
    "/gog galaxy/games/",
    "\\ea games\\",
    "/ea games/",
    "\\ubisoft game launcher\\games\\",
    "/ubisoft game launcher/games/",
)

ELECTRON_UI_HELPER_FLAGS: tuple[str, ...] = (
    "--type=gpu-process",
    "--type=renderer",
    "--type=utility",
    "--type=zygote",
    "--type=crashpad-handler",
)

RCLONE_DAEMON_SUBCOMMANDS: frozenset[str] = frozenset(
    {"mount", "cmount", "nfsmount", "serve", "rcd"}
)

DOTNET_BUILD_SUBCOMMANDS: frozenset[str] = frozenset(
    {"build", "publish", "test", "run", "pack", "msbuild", "watch"}
)

DOCKER_DOWNLOAD_SUBCOMMANDS: frozenset[str] = frozenset(
    {"pull", "push", "build", "buildx", "load"}
)

NODE_PM_INSTALL_WORDS: frozenset[str] = frozenset(
    {"install", "i", "ci", "add", "update", "upgrade", "dlx"}
)

HEX_INDEX_RE = re.compile(r"0x([0-9a-fA-F]{8})")


# ---------------------------------------------------------------------------
# Portable Win32 ctypes Structures (safe to define on Linux & Windows)
# ---------------------------------------------------------------------------


class GUID(ctypes.Structure):
    """Win32 GUID structure matching Windows powrprof.dll / rpcrt4.dll layout."""

    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    """Win32 SYSTEM_POWER_STATUS returned by kernel32.GetSystemPowerStatus."""

    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_uint32),
        ("BatteryFullLifeTime", ctypes.c_uint32),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    """Win32 DISPLAY_DEVICEW for user32.EnumDisplayDevicesW."""

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_uint32),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


class POINTL(ctypes.Structure):
    """Win32 POINTL structure for DEVMODEW display position."""

    _fields_ = [
        ("x", ctypes.c_int32),
        ("y", ctypes.c_int32),
    ]


class DEVMODEW(ctypes.Structure):
    """Win32 DEVMODEW structure matching wingdi.h (220 bytes on Windows)."""

    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_uint16),
        ("dmDriverVersion", ctypes.c_uint16),
        ("dmSize", ctypes.c_uint16),
        ("dmDriverExtra", ctypes.c_uint16),
        ("dmFields", ctypes.c_uint32),
        ("dmPosition", POINTL),
        ("dmDisplayOrientation", ctypes.c_uint32),
        ("dmDisplayFixedOutput", ctypes.c_uint32),
        ("dmColor", ctypes.c_int16),
        ("dmDuplex", ctypes.c_int16),
        ("dmYResolution", ctypes.c_int16),
        ("dmTTOption", ctypes.c_int16),
        ("dmCollate", ctypes.c_int16),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_uint16),
        ("dmBitsPerPel", ctypes.c_uint32),
        ("dmPelsWidth", ctypes.c_uint32),
        ("dmPelsHeight", ctypes.c_uint32),
        ("dmDisplayFlags", ctypes.c_uint32),
        ("dmDisplayFrequency", ctypes.c_uint32),
        ("dmICMMethod", ctypes.c_uint32),
        ("dmICMIntent", ctypes.c_uint32),
        ("dmMediaType", ctypes.c_uint32),
        ("dmDitherType", ctypes.c_uint32),
        ("dmReserved1", ctypes.c_uint32),
        ("dmReserved2", ctypes.c_uint32),
        ("dmPanningWidth", ctypes.c_uint32),
        ("dmPanningHeight", ctypes.c_uint32),
    ]


# ---------------------------------------------------------------------------
# Pure Utility, PyInstaller Bundle & Config Helpers
# ---------------------------------------------------------------------------


def guid_from_string(guid_str: str) -> GUID:
    """Convert a canonical UUID string into a Win32 GUID ctypes structure."""
    u = uuid.UUID(guid_str.strip("{} "))
    data4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
    return GUID(u.time_low, u.time_mid, u.time_hi_version, data4)


def guid_to_string(guid: GUID) -> str:
    """Convert a Win32 GUID ctypes structure into a lowercase canonical UUID string."""
    raw = (
        int(guid.Data1).to_bytes(4, "big")
        + int(guid.Data2).to_bytes(2, "big")
        + int(guid.Data3).to_bytes(2, "big")
        + bytes(guid.Data4)
    )
    return str(uuid.UUID(bytes=raw))


def get_bundle_dirs() -> list[Path]:
    """
    Return candidate directories containing bundled resources such as
    ``config.example.toml`` across PyInstaller onefile (``sys._MEIPASS``),
    frozen executable directory, installed directory, and repo root.
    """
    raw_dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        raw_dirs.append(Path(meipass))
    if getattr(sys, "frozen", False):
        try:
            raw_dirs.append(Path(sys.executable).resolve().parent)
        except OSError:
            pass
    raw_dirs.append(_THIS_DIR)
    raw_dirs.append(_PARENT_DIR)

    unique: list[Path] = []
    seen: set[str] = set()
    for d in raw_dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def find_bundled_config_example() -> Path | None:
    """Locate bundled ``config.example.toml`` (frozen _MEIPASS, install dir, or repo root)."""
    for d in get_bundle_dirs():
        candidate = d / "config.example.toml"
        if candidate.is_file():
            return candidate
    return None


def get_windows_data_dir() -> Path:
    """Return %LOCALAPPDATA%\\smart-powerd (with cross-platform fallback for tests)."""
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "smart-powerd"
    if IS_WINDOWS:
        return Path.home() / "AppData" / "Local" / "smart-powerd"
    return Path.home() / ".local" / "share" / "smart-powerd"


def resolve_windows_config_path(explicit_path: str | Path | None = None) -> Path:
    """
    Resolve Windows config.toml path:
      1. Explicit CLI path if given
      2. %LOCALAPPDATA%\\smart-powerd\\config.toml (if present)
      3. %ProgramData%\\smart-powerd\\config.toml (if present)
      4. Bundled config.example.toml (_MEIPASS / executable dir / repo root)
      5. Default target %LOCALAPPDATA%\\smart-powerd\\config.toml
    """
    if explicit_path:
        return Path(explicit_path)

    user_cfg = get_windows_data_dir() / "config.toml"
    if user_cfg.is_file():
        return user_cfg

    program_data = os.environ.get("ProgramData", "").strip()
    if program_data:
        sys_cfg = Path(program_data) / "smart-powerd" / "config.toml"
        if sys_cfg.is_file():
            return sys_cfg

    bundled = find_bundled_config_example()
    if bundled is not None:
        return bundled

    return user_cfg


DEFAULT_BATTERY_CHARGE_CONFIG: dict[str, Any] = {
    "limit_enabled": True,
    "stop_threshold": 80,
    "start_threshold": 75,
    "notify_when_no_hw_limit": True,
}


def resolve_charge_limit_config(
    config: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Resolve effective ``[battery_charge]`` configuration with runtime
    ``state.json`` ``charge_limit_enabled`` override and safe threshold clamps.
    Delegates to ``smart_power_config.effective_charge_limit_config`` when present.
    """
    shared_fn = getattr(smart_power_config, "effective_charge_limit_config", None)
    if callable(shared_fn):
        raw = shared_fn(config, state)
    else:
        raw = copy.deepcopy(DEFAULT_BATTERY_CHARGE_CONFIG)
        cfg_sec = config.get("battery_charge") if isinstance(config, dict) else None
        if isinstance(cfg_sec, dict):
            for k in raw:
                if k in cfg_sec and cfg_sec[k] is not None:
                    raw[k] = cfg_sec[k]
        if isinstance(state, dict) and isinstance(state.get("charge_limit_enabled"), bool):
            raw["limit_enabled"] = state["charge_limit_enabled"]

    try:
        stop_pct = max(50, min(100, int(round(float(raw.get("stop_threshold", 80))))))
    except (TypeError, ValueError):
        stop_pct = 80
    try:
        start_pct = max(40, min(95, int(round(float(raw.get("start_threshold", 75))))))
    except (TypeError, ValueError):
        start_pct = 75
    start_pct = min(start_pct, max(40, stop_pct - 5))

    return {
        "limit_enabled": bool(raw.get("limit_enabled", True)),
        "stop_threshold": stop_pct,
        "start_threshold": start_pct,
        "notify_when_no_hw_limit": bool(raw.get("notify_when_no_hw_limit", True)),
    }


def load_windows_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load TOML config via smart_power_config and ensure 3-tier refresh & charge defaults."""
    resolved = resolve_windows_config_path(path)
    cfg = load_config(resolved)
    bt = cfg.setdefault("battery_tricks", {})
    if "refresh_policy_enabled" not in bt:
        bt["refresh_policy_enabled"] = bool(bt.get("low_refresh_on_battery_light", True))
    bt.setdefault("ac_refresh_hz", 0)
    bt.setdefault("battery_refresh_hz", 0)
    bt.setdefault("low_battery_refresh_pct", 20)
    bt.setdefault("low_refresh_hz", 60)
    cfg["battery_charge"] = resolve_charge_limit_config(cfg, None)
    cfg["brightness"] = normalize_brightness_config(cfg.get("brightness"))
    return cfg


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON to ``path`` using temporary file + os.replace."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=False)
                fh.write("\n")
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        pass


def read_json_file(path: Path) -> dict[str, Any] | None:
    """Read a JSON object from ``path``, returning None on error."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError):
        return None


def parse_powercfg_ac_dc_indices(output: str) -> tuple[int | None, int | None]:
    """
    Locale-independently extract (ac_index, dc_index) from ``powercfg /query`` output.
    The last two ``0xXXXXXXXX`` values in a single setting query are always the
    current AC setting index and current DC setting index.
    """
    matches = HEX_INDEX_RE.findall(output or "")
    if len(matches) >= 2:
        return int(matches[-2], 16), int(matches[-1], 16)
    if len(matches) == 1:
        val = int(matches[0], 16)
        return val, val
    return None, None


# ---------------------------------------------------------------------------
# Pure Testable Core Functions (Linux & Windows CI Contract)
# ---------------------------------------------------------------------------


def _extract_devmode_tuple(item: Any) -> tuple[int, int, float, int, bool] | None:
    """Normalize a DEVMODEW, dict, tuple, or DisplayMode into (w, h, hz, bpp, is_cur)."""
    if item is None:
        return None
    if isinstance(item, DisplayMode):
        return (item.width, item.height, float(item.refresh_hz), 32, item.is_current)
    if isinstance(item, dict):
        w = item.get("width", item.get("dmPelsWidth", 0))
        h = item.get("height", item.get("dmPelsHeight", 0))
        hz = item.get("refresh_hz", item.get("hz", item.get("dmDisplayFrequency", 0)))
        bpp = item.get("bpp", item.get("bits_per_pel", item.get("dmBitsPerPel", 32)))
        is_cur = bool(item.get("is_current", False))
        try:
            return (int(w), int(h), float(hz), int(bpp), is_cur)
        except (TypeError, ValueError):
            return None
    if isinstance(item, (tuple, list)) and len(item) >= 3:
        try:
            w = int(item[0])
            h = int(item[1])
            hz = float(item[2])
            bpp = int(item[3]) if len(item) >= 4 else 32
            is_cur = bool(item[4]) if len(item) >= 5 else False
            return (w, h, hz, bpp, is_cur)
        except (TypeError, ValueError):
            return None
    # ctypes DEVMODEW or generic object with attributes
    w_attr = getattr(item, "dmPelsWidth", getattr(item, "width", 0))
    h_attr = getattr(item, "dmPelsHeight", getattr(item, "height", 0))
    hz_attr = getattr(item, "dmDisplayFrequency", getattr(item, "refresh_hz", 0))
    bpp_attr = getattr(
        item, "dmBitsPerPel", getattr(item, "bits_per_pel", getattr(item, "bpp", 32))
    )
    cur_attr = bool(getattr(item, "is_current", False))
    try:
        return (int(w_attr), int(h_attr), float(hz_attr), int(bpp_attr), cur_attr)
    except (TypeError, ValueError):
        return None


def win_devmodes_to_modes(
    raw_modes: Sequence[Any],
    current_mode: Any | None = None,
    output_name: str = "DISPLAY1",
) -> PanelDisplayInfo:
    """
    Convert raw Windows ``EnumDisplaySettingsW`` DEVMODEW records (or test dicts /
    tuples ``(width, height, hz[, bpp])``) into a deduplicated ``PanelDisplayInfo``
    compatible with ``display_refresh.choose_refresh_modes`` and ``select_target_mode``.
    """
    clean_out = (output_name or "DISPLAY1").replace("\\\\.\\", "").strip() or "DISPLAY1"

    parsed: list[tuple[int, int, float, int, bool]] = []
    for item in raw_modes or ():
        tup = _extract_devmode_tuple(item)
        if tup is None:
            continue
        w, h, hz, bpp, is_cur = tup
        if w <= 0 or h <= 0 or hz < 20.0:
            continue
        parsed.append((w, h, hz, bpp, is_cur))

    cur_tup = _extract_devmode_tuple(current_mode) if current_mode is not None else None
    if cur_tup is not None and cur_tup[0] > 0 and cur_tup[1] > 0 and cur_tup[2] >= 20.0:
        parsed.append((cur_tup[0], cur_tup[1], cur_tup[2], cur_tup[3], True))

    if not parsed:
        return PanelDisplayInfo(
            output_name=clean_out,
            current_mode=None,
            modes=(),
            backend="win32",
        )

    # Prefer 32-bit modes when available (Windows enumerates legacy 8/16-bit modes too)
    target_bpp = cur_tup[3] if (cur_tup is not None and cur_tup[3] >= 24) else 32
    has_target_bpp = any(bpp >= target_bpp for _, _, _, bpp, _ in parsed)
    if has_target_bpp:
        parsed = [p for p in parsed if p[3] >= target_bpp]

    # Deduplicate by (width, height, rounded Hz)
    by_key: dict[tuple[int, int, int], tuple[int, int, float, bool]] = {}
    for w, h, hz, _bpp, is_cur in parsed:
        key = (w, h, int(round(hz)))
        matches_cur = is_cur or (
            cur_tup is not None
            and w == cur_tup[0]
            and h == cur_tup[1]
            and abs(hz - cur_tup[2]) < 1.0
        )
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = (w, h, hz, matches_cur)
        elif matches_cur and not prev[3]:
            by_key[key] = (w, h, hz, True)

    if not by_key:
        return PanelDisplayInfo(
            output_name=clean_out,
            current_mode=None,
            modes=(),
            backend="win32",
        )

    # Determine highest Hz per resolution so we mark preferred modes deterministically
    max_hz_by_res: dict[tuple[int, int], float] = {}
    for w, h, hz, _ in by_key.values():
        res = (w, h)
        if res not in max_hz_by_res or hz > max_hz_by_res[res]:
            max_hz_by_res[res] = hz

    sorted_entries = sorted(
        by_key.values(),
        key=lambda t: (-t[0] * t[1], -t[0], -t[2]),
    )

    modes: list[DisplayMode] = []
    active_mode: DisplayMode | None = None
    for w, h, hz, is_cur in sorted_entries:
        rate_str = f"{int(round(hz))}" if abs(hz - round(hz)) < 0.05 else f"{hz:.2f}"
        is_pref = abs(hz - max_hz_by_res[(w, h)]) < 0.5
        dm = DisplayMode(
            mode_id=f"{w}x{h}@{rate_str}",
            width=w,
            height=h,
            refresh_hz=float(hz),
            is_current=is_cur,
            is_preferred=is_pref,
            rate_str=rate_str,
        )
        modes.append(dm)
        if is_cur and active_mode is None:
            active_mode = dm

    return PanelDisplayInfo(
        output_name=clean_out,
        current_mode=active_mode,
        modes=tuple(modes),
        backend="win32",
    )


def compute_windows_refresh_profile(
    enabled: bool,
    ac_online: bool,
    battery_pct: float | int | None,
    refresh_policy_enabled: bool = True,
    low_battery_refresh_pct: int = 20,
    prev_profile: str = "high",
    hysteresis_pct: int = 2,
    battery_present: bool = True,
) -> str:
    """
    Compute the 3-tier refresh rate profile (``"high"`` | ``"mid"`` | ``"low"``):
      - Smart Optimize OFF or policy disabled -> ``"high"``
      - On AC (or no battery)                 -> ``"high"`` (exits low immediately)
      - Unplugged & battery < threshold       -> ``"low"``
      - Unplugged & prev == "low"             -> stays ``"low"`` until battery >= threshold + hysteresis (+2%)
      - Unplugged & battery >= threshold      -> ``"mid"``
    """
    if not enabled or not refresh_policy_enabled:
        return "high"
    if ac_online or not battery_present:
        return "high"

    prev = (prev_profile or "high").strip().lower()
    if battery_pct is None:
        return "low" if prev == "low" else "mid"

    try:
        pct = float(battery_pct)
    except (TypeError, ValueError):
        return "low" if prev == "low" else "mid"
    if not math.isfinite(pct):
        return "low" if prev == "low" else "mid"

    threshold = max(1.0, min(99.0, float(low_battery_refresh_pct)))
    hyst = max(0.0, float(hysteresis_pct))

    if prev == "low":
        if pct >= (threshold + hyst):
            return "mid"
        return "low"

    if pct < threshold:
        return "low"
    return "mid"


def _normalize_proc_names(items: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(items, (list, tuple, set, frozenset)):
        return names
    for item in items:
        s = str(item).strip().lower()
        if not s:
            continue
        names.add(s)
        if not s.endswith(".exe") and "." not in s:
            names.add(f"{s}.exe")
    return names


def _is_electron_ui_helper(args: Sequence[str]) -> bool:
    joined = " ".join(args).lower()
    return any(flag in joined for flag in ELECTRON_UI_HELPER_FLAGS)


def _is_rclone_transfer(args: Sequence[str]) -> bool:
    if len(args) < 2:
        return False
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
    skip_next = False
    for tok in args[1:10]:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok.lower() in value_flags:
                skip_next = True
            continue
        return tok.lower() not in RCLONE_DAEMON_SUBCOMMANDS
    return False


def _is_active_msiexec(args: Sequence[str]) -> bool:
    """Ignore idle Windows Installer service (``msiexec.exe /V``) while matching installs."""
    if len(args) <= 1:
        return True
    lowered = [a.lower() for a in args[1:]]
    has_install_flag = any(
        a in ("/i", "-i", "/x", "-x", "/p", "-p", "/a", "/update", "/package")
        or a.startswith(("/i", "/p", "/x"))
        or a.endswith((".msi", ".msp"))
        for a in lowered
    )
    if has_install_flag:
        return True
    # Idle Windows Installer service runs exclusively with /V
    if lowered == ["/v"] or lowered == ["-v"]:
        return False
    return True


def _is_game_process(
    name_low: str,
    parent_low: str,
    exe_low: str,
) -> bool:
    """Detect active Windows games via launcher parent, library path, or Unreal shipping exe."""
    if name_low in GAME_LAUNCHER_HELPERS or name_low in WINDOWS_SYSTEM_EXCLUDED:
        return False
    if parent_low in GAME_LAUNCHER_PARENTS:
        return True
    if exe_low and any(marker in exe_low for marker in GAME_PATH_MARKERS):
        if not any(
            skip in name_low
            for skip in ("crash", "launcher", "updater", "helper", "setup", "redist", "unins")
        ):
            return True
    if name_low.endswith(("-win64-shipping.exe", "-win32-shipping.exe")):
        return True
    return False


def classify_windows_workload(
    processes: Sequence[dict[str, Any]],
    total_cpu_pct: float = 0.0,
    max_core_cpu_pct: float = 0.0,
    rx_mib_s: float = 0.0,
    *,
    rx_streak: int = 0,
    cpu_streak: int = 0,
    config: dict[str, Any] | None = None,
    own_pid: int | None = None,
) -> dict[str, Any]:
    """
    Pure Windows workload classifier (Heavy > Download > Light).

    Args:
        processes: Sequence of process snapshot dicts containing keys:
            ``pid``, ``ppid``, ``name``, ``cmdline``, ``cpu_pct``, ``exe``, ``parent_name``.
        total_cpu_pct: System-wide CPU utilization percent (0..100).
        max_core_cpu_pct: Highest individual logical core utilization percent (0..100).
        rx_mib_s: Current physical network RX throughput in MiB/s.
        rx_streak: Consecutive ticks with ``rx_mib_s >= rx_threshold_mib_s`` (including current tick).
        cpu_streak: Consecutive ticks with sustained CPU above threshold (including current tick).
        config: Optional full config dict (or ``[workloads]`` section dict).
        own_pid: PID of this process to exclude.

    Returns:
        dict with keys ``mode`` ("heavy"|"download"|"light"), ``reason``,
        ``has_download``, ``heavy_pids``, ``download_pids``, ``boost_pids``.
    """
    wcfg: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG["workloads"])
    if isinstance(config, dict):
        section = config.get("workloads") if isinstance(config.get("workloads"), dict) else config
        for k in wcfg:
            if k in section and section[k] is not None:
                wcfg[k] = section[k]

    rx_threshold = float(wcfg.get("rx_threshold_mib_s", 1.5))
    rx_immediate = float(wcfg.get("rx_immediate_mib_s", 3.0))
    cpu_heavy_pct = float(wcfg.get("cpu_heavy_pct", 25.0))
    single_core_heavy_pct = float(wcfg.get("single_core_heavy_pct", 85.0))

    heavy_immediate = set(WINDOWS_HEAVY_IMMEDIATE) | _normalize_proc_names(
        wcfg.get("extra_heavy_processes", [])
    )
    download_immediate = set(WINDOWS_DOWNLOAD_IMMEDIATE) | _normalize_proc_names(
        wcfg.get("extra_download_processes", [])
    )

    # Build PID -> lowercase process name table so parent_name can be resolved automatically
    pid_to_name: dict[int, str] = {}
    for proc in processes or ():
        if not isinstance(proc, dict):
            continue
        pid_val = proc.get("pid")
        name_val = str(proc.get("name") or "").strip().lower()
        if isinstance(pid_val, int) and name_val:
            pid_to_name[pid_val] = name_val

    heavy_pids: list[int] = []
    boost_pids: list[int] = []
    download_pids: list[int] = []
    heavy_reason: str | None = None
    download_reason: str | None = None
    max_proc_cpu = 0.0

    for proc in processes or ():
        if not isinstance(proc, dict):
            continue
        pid = int(proc.get("pid") or 0)
        if own_pid is not None and pid == own_pid:
            continue

        raw_name = str(proc.get("name") or "").strip()
        name_low = raw_name.lower()
        if not name_low:
            continue

        raw_cmdline = proc.get("cmdline")
        if isinstance(raw_cmdline, str):
            args = [tok for tok in raw_cmdline.split() if tok]
        elif isinstance(raw_cmdline, (list, tuple)):
            args = [str(tok) for tok in raw_cmdline if tok]
        else:
            args = []
        args_low = [a.lower() for a in args]
        joined_low = " ".join(args_low)

        # Ignore our own script/executable and core Windows OS processes
        if "smart_power_win.py" in joined_low or name_low in WINDOWS_SYSTEM_EXCLUDED:
            continue

        ppid = int(proc.get("ppid") or 0)
        parent_low = str(proc.get("parent_name") or "").strip().lower() or pid_to_name.get(ppid, "")
        exe_low = str(proc.get("exe") or "").strip().lower()
        try:
            proc_cpu = float(proc.get("cpu_pct") or 0.0)
        except (TypeError, ValueError):
            proc_cpu = 0.0

        if proc_cpu > max_proc_cpu:
            max_proc_cpu = proc_cpu

        ui_helper = _is_electron_ui_helper(args)
        is_heavy = False
        is_boostable = False
        is_download = False

        # --- Heavy checks (#1 priority) ---
        if name_low in heavy_immediate:
            is_heavy = True
            is_boostable = True
            heavy_reason = heavy_reason or f"process:{raw_name}"
        elif name_low in ("cargo.exe", "cargo"):
            subcmd = next((a for a in args_low[1:4] if not a.startswith("-")), "")
            if subcmd in ("fetch", "search"):
                is_download = True
                download_reason = download_reason or f"pkg/dl:{raw_name} {subcmd}"
            else:
                is_heavy = True
                is_boostable = True
                heavy_reason = heavy_reason or f"process:{raw_name}"
        elif name_low in ("dotnet.exe", "dotnet") and any(
            a in DOTNET_BUILD_SUBCOMMANDS for a in args_low[1:4]
        ):
            is_heavy = True
            is_boostable = True
            heavy_reason = heavy_reason or f"build:{raw_name}"
        elif _is_game_process(name_low, parent_low, exe_low):
            is_heavy = True
            is_boostable = True
            heavy_reason = heavy_reason or f"game:{raw_name}"
        elif (
            not ui_helper
            and name_low in WINDOWS_HEAVY_CPU_GATED
            and proc_cpu >= cpu_heavy_pct
        ):
            is_heavy = True
            is_boostable = True
            heavy_reason = heavy_reason or f"dev:{raw_name} ({proc_cpu:.0f}%)"
        elif not ui_helper and proc_cpu >= single_core_heavy_pct:
            is_heavy = True
            is_boostable = True
            heavy_reason = heavy_reason or f"cpu:{raw_name} ({proc_cpu:.0f}%)"

        # --- Download checks (#2 priority) ---
        if not is_download:
            if name_low in download_immediate:
                is_download = True
                download_reason = download_reason or f"pkg/dl:{raw_name}"
            elif name_low == "msiexec.exe" and _is_active_msiexec(args):
                is_download = True
                download_reason = download_reason or "installer:msiexec.exe"
            elif name_low in ("docker.exe", "docker") and any(
                a in DOCKER_DOWNLOAD_SUBCOMMANDS for a in args_low[1:4]
            ):
                is_download = True
                download_reason = download_reason or "docker transfer"
            elif name_low in ("rclone.exe", "rclone") and _is_rclone_transfer(args):
                is_download = True
                download_reason = download_reason or "pkg/dl:rclone"
            elif name_low in ("node.exe", "cmd.exe") and (
                any(pm in joined_low for pm in ("npm", "pnpm", "yarn", "bun"))
                and any(tok in NODE_PM_INSTALL_WORDS for tok in args_low[1:])
            ):
                is_download = True
                download_reason = download_reason or f"pkg/dl:{raw_name} (npm)"
            elif name_low in ("powershell.exe", "pwsh.exe", "cmd.exe") and (
                "scoop" in joined_low
                and any(w in joined_low for w in ("install", "update", "download"))
            ):
                is_download = True
                download_reason = download_reason or "pkg/dl:scoop"
            elif name_low in ("python.exe", "python3.exe") and (
                "pip" in args_low[1:5]
                and any(w in args_low[1:7] for w in ("install", "download", "wheel"))
            ):
                is_download = True
                download_reason = download_reason or "pkg/dl:pip"

        if is_heavy and pid > 0:
            heavy_pids.append(pid)
            if is_boostable:
                boost_pids.append(pid)
        if is_download and pid > 0:
            download_pids.append(pid)

    # Sustained CPU fallback trigger
    cpu_above = (
        float(total_cpu_pct) >= cpu_heavy_pct
        or float(max_core_cpu_pct) >= single_core_heavy_pct
        or max_proc_cpu >= single_core_heavy_pct
    )
    if not heavy_reason and cpu_above and cpu_streak >= 2:
        if float(max_core_cpu_pct) >= single_core_heavy_pct:
            heavy_reason = f"sustained core CPU ({max_core_cpu_pct:.0f}%)"
        else:
            heavy_reason = f"sustained CPU ({total_cpu_pct:.0f}%)"

    # Network RX trigger (immediate >= 3.0 MiB/s or >= 1.5 MiB/s for 2 ticks)
    if not download_reason:
        if float(rx_mib_s) >= rx_immediate:
            download_reason = f"network RX ({rx_mib_s:.2f} MiB/s)"
        elif float(rx_mib_s) >= rx_threshold and rx_streak >= 2:
            download_reason = f"network RX ({rx_mib_s:.2f} MiB/s)"

    has_download = download_reason is not None
    if heavy_reason is not None:
        mode = "heavy"
        reason = heavy_reason
    elif download_reason is not None:
        mode = "download"
        reason = download_reason
    else:
        mode = "light"
        reason = "idle / light workload"

    return {
        "mode": mode,
        "reason": reason,
        "has_download": has_download,
        "heavy_pids": heavy_pids,
        "download_pids": download_pids,
        "boost_pids": boost_pids,
    }


def apply_mode_cooldown(
    desired_mode: str,
    current_mode: str,
    now: float,
    last_higher_ts: float,
    cooldown_sec: float = 12.0,
) -> tuple[str, float]:
    """
    Apply asymmetric upgrade/downgrade transition hysteresis:
      - Upgrading (e.g. light -> download -> heavy) happens immediately.
      - Downgrading holds the higher mode until ``cooldown_sec`` seconds elapse.
    """
    des = desired_mode if desired_mode in MODE_RANK else "light"
    cur = current_mode if current_mode in MODE_RANK else "light"
    des_rank = MODE_RANK[des]
    cur_rank = MODE_RANK[cur]

    if des_rank >= cur_rank:
        new_ts = now if des_rank > MODE_RANK["light"] else last_higher_ts
        return des, new_ts

    # Downgrade requested
    if (now - last_higher_ts) < max(0.0, float(cooldown_sec)):
        return cur, last_higher_ts

    new_ts = now if des_rank > MODE_RANK["light"] else 0.0
    return des, new_ts


def evaluate_charge_limit_alert(
    *,
    charge_limit_enabled: bool,
    charge_limit_hw_supported: bool,
    charge_limit_notify_fallback: bool,
    charge_limit_pct: int,
    ac_online: bool,
    battery_present: bool,
    battery_status: str,
    battery_pct: int | float | None,
    already_notified: bool,
) -> tuple[bool, bool]:
    """
    Evaluate once-per-charge-cycle charging threshold notification state on
    platforms without a hardware EC cutoff backend (alert-only fallback).

    Returns ``(should_notify_now, next_notified_flag)``:
      - Fires once when enabled, no HW limit, fallback notification is enabled,
        ``battery_status`` is ``"charging"`` on AC, and ``battery_pct >= charge_limit_pct``.
      - Resets ``already_notified`` once unplugged (``not ac_online``), disabled,
        or ``battery_pct < charge_limit_pct - 2``.
    """
    if (
        not charge_limit_enabled
        or charge_limit_hw_supported
        or not charge_limit_notify_fallback
        or not battery_present
    ):
        return False, False

    if not ac_online:
        return False, False

    if battery_pct is None:
        return False, already_notified

    try:
        pct = float(battery_pct)
    except (TypeError, ValueError):
        return False, already_notified
    if not math.isfinite(pct):
        return False, already_notified

    threshold = max(50.0, min(100.0, float(charge_limit_pct)))
    if pct < (threshold - 2.0):
        return False, False

    is_charging = str(battery_status or "").strip().lower() == "charging"
    if is_charging and pct >= threshold and not already_notified:
        return True, True

    return False, already_notified


# ---------------------------------------------------------------------------
# Direct 3-Tier Refresh Helpers (Delegated to Shared display_refresh Module)
# ---------------------------------------------------------------------------


def _choose_3tier_modes(
    info: PanelDisplayInfo,
    ac_hz: float = 0.0,
    battery_hz: float = 0.0,
    low_hz: float = 60.0,
) -> dict[str, DisplayMode | None]:
    """Delegate 3-tier refresh mode resolution directly to shared display_refresh."""
    return choose_refresh_modes(
        info,
        ac_hz=ac_hz,
        battery_hz=battery_hz,
        low_hz=low_hz,
    )


def select_windows_target_mode(
    info: PanelDisplayInfo,
    profile: str,
    ac_hz: float = 0.0,
    battery_hz: float = 0.0,
    low_hz: float = 60.0,
) -> DisplayMode | None:
    """
    Select target ``DisplayMode`` for ``profile`` ("high"|"mid"|"low") via shared
    ``display_refresh.select_target_mode``. Returns None if already at target Hz.
    """
    return select_target_mode(
        info,
        profile=profile,
        ac_hz=ac_hz,
        battery_hz=battery_hz,
        low_hz=low_hz,
    )


# ---------------------------------------------------------------------------
# Subprocess Helper (Zero Console Window Flash on Windows)
# ---------------------------------------------------------------------------


def _run_hidden(cmd: list[str], timeout: float = 4.0) -> tuple[int, str, str]:
    """Run a subprocess cleanly without flashing a console window on Windows."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "timeout": timeout,
        "check": False,
    }
    if IS_WINDOWS and CREATE_NO_WINDOW:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(cmd, **kwargs)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _run_powershell(script: str, timeout: float = 4.0) -> tuple[int, str, str]:
    """Run a short PowerShell snippet silently with CREATE_NO_WINDOW."""
    if not IS_WINDOWS:
        return 1, "", "not-windows"
    ps_bin = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or "powershell.exe"
    return _run_hidden(
        [
            ps_bin,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Windows Telemetry & Control Backends (Guarded Behind IS_WINDOWS)
# ---------------------------------------------------------------------------


class WindowsBatteryTelemetry:
    """Reads AC/battery state via Win32 GetSystemPowerStatus + optional WMI wattage."""

    def __init__(self, wmi_interval_sec: float = 7.5) -> None:
        self._wmi_interval_sec = float(wmi_interval_sec)
        self._last_wmi_ts: float = 0.0
        self._wmi_failures: int = 0
        self._wmi_backoff_until: float = 0.0
        self._cached_watts: float = 0.0

    def _read_wmi_watts(self, now: float) -> float:
        if not IS_WINDOWS or now < self._wmi_backoff_until:
            return self._cached_watts
        if (now - self._last_wmi_ts) < self._wmi_interval_sec:
            return self._cached_watts

        self._last_wmi_ts = now
        script = (
            "$b = Get-CimInstance -Namespace root/WMI -ClassName BatteryStatus "
            "-ErrorAction SilentlyContinue | Select-Object -First 1; "
            "if ($b) { "
            "$r = if ($b.DischargeRate -gt 0) { $b.DischargeRate } "
            "elseif ($b.ChargeRate -gt 0) { $b.ChargeRate } else { 0 }; "
            "Write-Output $r }"
        )
        rc, out, _ = _run_powershell(script, timeout=3.0)
        if rc == 0 and out.strip():
            try:
                mw = float(out.strip().splitlines()[-1])
                if math.isfinite(mw) and mw >= 0.0:
                    self._cached_watts = round(mw / 1000.0, 2)
                    self._wmi_failures = 0
                    return self._cached_watts
            except ValueError:
                pass

        self._wmi_failures += 1
        if self._wmi_failures >= 3:
            self._wmi_backoff_until = now + 120.0
        return self._cached_watts

    def read(self, now: float | None = None) -> dict[str, Any]:
        """Return dict with ac_online, battery_present, battery_pct, battery_status, battery_watts."""
        ts = time.time() if now is None else float(now)
        if not IS_WINDOWS:
            return {
                "ac_online": True,
                "battery_present": False,
                "battery_pct": None,
                "battery_status": "Unknown",
                "battery_watts": 0.0,
            }

        status = SYSTEM_POWER_STATUS()
        try:
            ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
        except Exception:
            ok = 0

        if not ok:
            return {
                "ac_online": True,
                "battery_present": False,
                "battery_pct": None,
                "battery_status": "Unknown",
                "battery_watts": 0.0,
            }

        ac_flag = int(status.ACLineStatus)
        bat_flag = int(status.BatteryFlag)
        pct_raw = int(status.BatteryLifePercent)

        ac_online = ac_flag != 0
        battery_present = not bool(bat_flag & 128) and not (bat_flag == 255 and pct_raw == 255)
        battery_pct = pct_raw if (battery_present and 0 <= pct_raw <= 100) else None

        if not battery_present:
            bat_status = "None"
        elif bat_flag & 8:
            bat_status = "Charging"
        elif ac_online and battery_pct == 100:
            bat_status = "Full"
        elif ac_online:
            bat_status = "Not charging"
        else:
            bat_status = "Discharging"

        watts = self._read_wmi_watts(ts) if battery_present else 0.0
        return {
            "ac_online": ac_online,
            "battery_present": battery_present,
            "battery_pct": battery_pct,
            "battery_status": bat_status,
            "battery_watts": watts,
        }


class Win32SleepInhibitor:
    """Inhibits system idle sleep during active downloads via SetThreadExecutionState."""

    def __init__(self) -> None:
        self.active: bool = False

    def acquire(self) -> bool:
        if self.active:
            return True
        if IS_WINDOWS:
            try:
                res = ctypes.windll.kernel32.SetThreadExecutionState(
                    ctypes.c_uint32(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                )
                if res != 0:
                    self.active = True
                    return True
                return False
            except Exception:
                return False
        self.active = True
        return True

    def release(self) -> bool:
        if not self.active:
            return True
        if IS_WINDOWS:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint32(ES_CONTINUOUS))
            except Exception:
                pass
        self.active = False
        return True


class WindowsPowerBackend:
    """
    Controls Windows Power Overlay (powrprof.dll), powercfg scheme fallback,
    Wireless Adapter Power Saving, PCIe ASPM, and USB Selective Suspend,
    recording original baseline settings to ``baseline.json`` for clean restore.
    """

    def __init__(
        self,
        baseline_path: Path,
        runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    ) -> None:
        self.baseline_path = baseline_path
        self._runner = runner if runner is not None else _run_hidden
        self.baseline: dict[str, Any] = self._load_or_capture_baseline()
        self.current_overlay: str = OVERLAY_NAMES[OVERLAY_BALANCED]
        self.current_wifi_ps: str = "Unknown"
        self.current_pcie_aspm: str = "Unknown"
        self.current_usb_suspend: str = "Unknown"

    def _query_powercfg_indices(self, subgroup: str, setting: str) -> tuple[int | None, int | None]:
        if not IS_WINDOWS:
            return None, None
        rc, out, _ = self._runner(["powercfg", "/query", "SCHEME_CURRENT", subgroup, setting], 4.0)
        if rc != 0:
            return None, None
        return parse_powercfg_ac_dc_indices(out)

    def _set_powercfg_indices(
        self,
        subgroup: str,
        setting: str,
        ac_val: int | None,
        dc_val: int | None,
        activate: bool = False,
    ) -> bool:
        if not IS_WINDOWS:
            return True
        ok = True
        if ac_val is not None:
            rc, _, _ = self._runner(
                [
                    "powercfg",
                    "/setacvalueindex",
                    "SCHEME_CURRENT",
                    subgroup,
                    setting,
                    str(int(ac_val)),
                ],
                4.0,
            )
            ok = ok and (rc == 0)
        if dc_val is not None:
            rc, _, _ = self._runner(
                [
                    "powercfg",
                    "/setdcvalueindex",
                    "SCHEME_CURRENT",
                    subgroup,
                    setting,
                    str(int(dc_val)),
                ],
                4.0,
            )
            ok = ok and (rc == 0)
        if activate:
            self._runner(["powercfg", "/setactive", "SCHEME_CURRENT"], 4.0)
        return ok

    def _get_active_overlay_guid(self) -> str | None:
        if not IS_WINDOWS:
            return None
        try:
            powrprof = ctypes.windll.powrprof
            fn = getattr(powrprof, "PowerGetActualOverlayScheme", None) or getattr(
                powrprof, "PowerGetEffectiveOverlayScheme", None
            )
            if fn is None:
                return None
            out_guid = GUID()
            res = fn(ctypes.byref(out_guid))
            if res == 0:
                return guid_to_string(out_guid)
        except Exception:
            pass
        return None

    def _set_overlay_guid(self, overlay_guid: str) -> bool:
        if not IS_WINDOWS:
            self.current_overlay = OVERLAY_NAMES.get(overlay_guid, overlay_guid)
            return True
        try:
            powrprof = ctypes.windll.powrprof
            fn = getattr(powrprof, "PowerSetActiveOverlayScheme", None)
            if fn is not None:
                g = guid_from_string(overlay_guid)
                res = fn(ctypes.byref(g))
                if res == 0:
                    self.current_overlay = OVERLAY_NAMES.get(overlay_guid, overlay_guid)
                    return True
        except Exception:
            pass

        # Safe fallback to classic powercfg /setactive scheme
        scheme = (
            SCHEME_HIGH_PERFORMANCE
            if overlay_guid == OVERLAY_BEST_PERFORMANCE
            else SCHEME_BALANCED
        )
        rc, _, _ = self._runner(["powercfg", "/setactive", scheme], 4.0)
        if rc != 0 and scheme != SCHEME_BALANCED:
            rc, _, _ = self._runner(["powercfg", "/setactive", SCHEME_BALANCED], 4.0)
            scheme = SCHEME_BALANCED
        self.current_overlay = (
            "High Performance (scheme)"
            if scheme == SCHEME_HIGH_PERFORMANCE
            else "Balanced (scheme)"
        )
        return rc == 0

    def _get_active_scheme_guid(self) -> str | None:
        if not IS_WINDOWS:
            return None
        rc, out, _ = self._runner(["powercfg", "/getactivescheme"], 4.0)
        if rc != 0:
            return None
        m = re.search(
            r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
            out,
        )
        return m.group(1).lower() if m else None

    def _load_or_capture_baseline(self) -> dict[str, Any]:
        existing = read_json_file(self.baseline_path)
        if existing is not None:
            return existing

        baseline: dict[str, Any] = {
            "overlay_guid": OVERLAY_BALANCED,
            "scheme_guid": SCHEME_BALANCED,
            "wifi_ps": {"ac": 0, "dc": 1},
            "pcie_aspm": {"ac": 1, "dc": 2},
            "usb_suspend": {"ac": 1, "dc": 1},
        }
        if IS_WINDOWS:
            ov = self._get_active_overlay_guid()
            if ov:
                baseline["overlay_guid"] = ov
            sc = self._get_active_scheme_guid()
            if sc:
                baseline["scheme_guid"] = sc
            w_ac, w_dc = self._query_powercfg_indices(SUBGROUP_WIFI, SETTING_WIFI_PS)
            if w_ac is not None or w_dc is not None:
                baseline["wifi_ps"] = {"ac": w_ac, "dc": w_dc}
            p_ac, p_dc = self._query_powercfg_indices(SUBGROUP_PCIE, SETTING_PCIE_ASPM)
            if p_ac is not None or p_dc is not None:
                baseline["pcie_aspm"] = {"ac": p_ac, "dc": p_dc}
            u_ac, u_dc = self._query_powercfg_indices(SUBGROUP_USB, SETTING_USB_SUSPEND)
            if u_ac is not None or u_dc is not None:
                baseline["usb_suspend"] = {"ac": u_ac, "dc": u_dc}
            atomic_write_json(self.baseline_path, baseline)
        return baseline

    def apply_mode(self, mode: str, config: dict[str, Any]) -> None:
        """Apply overlay + Wi-Fi PS + PCIe ASPM + USB selective suspend for ``mode``."""
        bt = config.get("battery_tricks", {}) if isinstance(config, dict) else {}
        aspm_cfg = str(bt.get("aspm_light", "powersave")).strip().lower()
        usb_cfg = bool(bt.get("usb_autosuspend_non_hid", True))

        if mode == "light":
            self._set_overlay_guid(OVERLAY_BETTER_BATTERY)
            wifi_val = 2  # Medium Power Saving
            aspm_val = 2 if aspm_cfg in ("powersave", "powersupersave") else 1
            usb_val = 1 if usb_cfg else 0
            self._set_powercfg_indices(SUBGROUP_WIFI, SETTING_WIFI_PS, wifi_val, wifi_val)
            self._set_powercfg_indices(SUBGROUP_PCIE, SETTING_PCIE_ASPM, aspm_val, aspm_val)
            self._set_powercfg_indices(
                SUBGROUP_USB, SETTING_USB_SUSPEND, usb_val, usb_val, activate=True
            )
            self.current_wifi_ps = WIFI_PS_LABELS[wifi_val]
            self.current_pcie_aspm = "Maximum" if aspm_val == 2 else "Moderate"
            self.current_usb_suspend = "Enabled" if usb_val == 1 else "Disabled"

        elif mode == "download":
            self._set_overlay_guid(OVERLAY_BALANCED)
            self._set_powercfg_indices(SUBGROUP_WIFI, SETTING_WIFI_PS, 0, 0)
            self._set_powercfg_indices(SUBGROUP_PCIE, SETTING_PCIE_ASPM, 0, 0)
            self._set_powercfg_indices(SUBGROUP_USB, SETTING_USB_SUSPEND, 0, 0, activate=True)
            self.current_wifi_ps = WIFI_PS_LABELS[0]
            self.current_pcie_aspm = "Off"
            self.current_usb_suspend = "Disabled"

        elif mode == "heavy":
            self._set_overlay_guid(OVERLAY_BEST_PERFORMANCE)
            self._set_powercfg_indices(SUBGROUP_WIFI, SETTING_WIFI_PS, 0, 0)
            self._set_powercfg_indices(SUBGROUP_PCIE, SETTING_PCIE_ASPM, 0, 0)
            self._set_powercfg_indices(SUBGROUP_USB, SETTING_USB_SUSPEND, 0, 0, activate=True)
            self.current_wifi_ps = WIFI_PS_LABELS[0]
            self.current_pcie_aspm = "Off"
            self.current_usb_suspend = "Disabled"

        else:
            # OFF / unrestricted: restore baseline values cleanly
            self.restore_baseline()

    def restore_baseline(self) -> None:
        """Restore saved baseline overlay, Wi-Fi PS, PCIe ASPM, and USB selective suspend."""
        base_overlay = str(self.baseline.get("overlay_guid") or OVERLAY_BALANCED)
        # Never leave the laptop stuck in Better Battery on OFF/uninstall
        if base_overlay == OVERLAY_BETTER_BATTERY:
            base_overlay = OVERLAY_BALANCED
        self._set_overlay_guid(base_overlay)

        wifi_b = self.baseline.get("wifi_ps") or {}
        w_ac = wifi_b.get("ac", 0)
        w_dc = wifi_b.get("dc", 0)
        self._set_powercfg_indices(SUBGROUP_WIFI, SETTING_WIFI_PS, w_ac, w_dc)

        pcie_b = self.baseline.get("pcie_aspm") or {}
        self._set_powercfg_indices(
            SUBGROUP_PCIE, SETTING_PCIE_ASPM, pcie_b.get("ac", 1), pcie_b.get("dc", 1)
        )

        usb_b = self.baseline.get("usb_suspend") or {}
        self._set_powercfg_indices(
            SUBGROUP_USB,
            SETTING_USB_SUSPEND,
            usb_b.get("ac", 1),
            usb_b.get("dc", 1),
            activate=True,
        )

        self.current_wifi_ps = WIFI_PS_LABELS.get(int(w_ac or 0), "Baseline")
        self.current_pcie_aspm = "Baseline"
        self.current_usb_suspend = "Baseline"


class WindowsDisplayRefreshController:
    """
    3-tier adaptive refresh rate controller for Windows via Win32 user32.dll
    (EnumDisplayDevicesW, EnumDisplaySettingsW, ChangeDisplaySettingsExW).
    """

    def __init__(
        self,
        probe_interval_sec: float = 15.0,
        min_switch_interval_sec: float = 5.0,
    ) -> None:
        self._probe_interval_sec = float(probe_interval_sec)
        self._min_switch_interval_sec = float(min_switch_interval_sec)

        self.device_name: str | None = None
        self.output_name: str | None = None
        self.last_info: PanelDisplayInfo | None = None
        self.current_hz: float | None = None
        self.native_hz: float | None = None
        self.active_profile: str = "high"

        self._last_probe_ts: float = 0.0
        self._last_switch_ts: float = 0.0
        self._last_desired_profile: str | None = None
        self._we_changed_refresh: bool = False

        if IS_WINDOWS:
            self.probe(force=True)

    @staticmethod
    def _find_primary_display_device() -> str | None:
        if not IS_WINDOWS:
            return None
        user32 = ctypes.windll.user32
        dd = DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        first_attached: str | None = None
        idx = 0
        while user32.EnumDisplayDevicesW(None, idx, ctypes.byref(dd), 0):
            flags = int(dd.StateFlags)
            if flags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
                name = str(dd.DeviceName)
                if first_attached is None:
                    first_attached = name
                if flags & DISPLAY_DEVICE_PRIMARY_DEVICE:
                    return name
            dd = DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            idx += 1
        return first_attached

    def _enumerate_win32_panel(self) -> PanelDisplayInfo | None:
        if not IS_WINDOWS:
            return None
        user32 = ctypes.windll.user32
        dev_name = self._find_primary_display_device()
        self.device_name = dev_name

        cur_dm = DEVMODEW()
        cur_dm.dmSize = ctypes.sizeof(DEVMODEW)
        has_cur = bool(
            user32.EnumDisplaySettingsW(
                ctypes.c_wchar_p(dev_name) if dev_name else None,
                ctypes.c_uint32(ENUM_CURRENT_SETTINGS),
                ctypes.byref(cur_dm),
            )
        )

        raw_modes: list[tuple[int, int, int, int]] = []
        mode_idx = 0
        while True:
            dm = DEVMODEW()
            dm.dmSize = ctypes.sizeof(DEVMODEW)
            if not user32.EnumDisplaySettingsW(
                ctypes.c_wchar_p(dev_name) if dev_name else None,
                ctypes.c_uint32(mode_idx),
                ctypes.byref(dm),
            ):
                break
            raw_modes.append(
                (
                    int(dm.dmPelsWidth),
                    int(dm.dmPelsHeight),
                    int(dm.dmDisplayFrequency),
                    int(dm.dmBitsPerPel),
                )
            )
            mode_idx += 1
            if mode_idx > 4096:
                break

        cur_tuple = (
            (
                int(cur_dm.dmPelsWidth),
                int(cur_dm.dmPelsHeight),
                int(cur_dm.dmDisplayFrequency),
                int(cur_dm.dmBitsPerPel),
            )
            if has_cur
            else None
        )
        info = win_devmodes_to_modes(
            raw_modes,
            current_mode=cur_tuple,
            output_name=dev_name or "DISPLAY1",
        )
        return info if info.modes else None

    def _switch_win32_refresh(self, target: DisplayMode) -> bool:
        if not IS_WINDOWS:
            return False
        user32 = ctypes.windll.user32
        user32.ChangeDisplaySettingsExW.restype = ctypes.c_long
        dev_name = self.device_name
        dev_arg = ctypes.c_wchar_p(dev_name) if dev_name else None
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if not user32.EnumDisplaySettingsW(
            dev_arg, ctypes.c_uint32(ENUM_CURRENT_SETTINGS), ctypes.byref(dm)
        ):
            return False

        # Never change resolution; update refresh frequency only
        dm.dmPelsWidth = int(target.width)
        dm.dmPelsHeight = int(target.height)
        dm.dmDisplayFrequency = int(round(target.refresh_hz))
        dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL | DM_DISPLAYFREQUENCY

        # Test first so an unsupported mode never glitches the panel
        test_res = int(
            user32.ChangeDisplaySettingsExW(
                dev_arg, ctypes.byref(dm), None, ctypes.c_uint32(CDS_TEST), None
            )
        )
        if test_res != DISP_CHANGE_SUCCESSFUL:
            return False

        # Apply transiently (flags=0) so an unclean reboot always recovers native registry Hz
        apply_res = int(
            user32.ChangeDisplaySettingsExW(
                dev_arg, ctypes.byref(dm), None, ctypes.c_uint32(0), None
            )
        )
        return apply_res == DISP_CHANGE_SUCCESSFUL

    def probe(self, force: bool = False, now: float | None = None) -> PanelDisplayInfo | None:
        ts = time.time() if now is None else float(now)
        if (
            not force
            and self.last_info is not None
            and (ts - self._last_probe_ts) < self._probe_interval_sec
        ):
            return self.last_info

        self._last_probe_ts = ts
        info = self._enumerate_win32_panel()
        if info is not None:
            self.last_info = info
            self.output_name = info.output_name
            if info.current_mode is not None:
                self.current_hz = round(info.current_mode.refresh_hz, 2)
            tiers = _choose_3tier_modes(info)
            if tiers["high"] is not None:
                self.native_hz = round(tiers["high"].refresh_hz, 2)
        return self.last_info

    def apply_profile(
        self,
        profile: str,
        ac_hz: float = 0.0,
        battery_hz: float = 0.0,
        low_hz: float = 60.0,
        now: float | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        ts = time.time() if now is None else float(now)
        norm = (profile or "high").strip().lower()
        if norm not in ("high", "mid", "low") or not enabled:
            norm = "high"

        profile_changed = norm != self._last_desired_profile
        info = self.probe(force=profile_changed, now=ts)
        self._last_desired_profile = norm
        self.active_profile = norm

        if info is None:
            return self.get_telemetry()

        tiers = _choose_3tier_modes(info, ac_hz=ac_hz, battery_hz=battery_hz, low_hz=low_hz)
        if tiers["high"] is not None:
            self.native_hz = round(tiers["high"].refresh_hz, 2)

        target = select_windows_target_mode(
            info,
            profile=norm,
            ac_hz=ac_hz,
            battery_hz=battery_hz,
            low_hz=low_hz,
        )
        if target is None:
            if norm == "high":
                self._we_changed_refresh = False
            return self.get_telemetry()

        if not enabled and not self._we_changed_refresh:
            return self.get_telemetry()

        if (ts - self._last_switch_ts) < self._min_switch_interval_sec:
            return self.get_telemetry()

        self._last_switch_ts = ts
        if self._switch_win32_refresh(target):
            self.current_hz = round(target.refresh_hz, 2)
            self._we_changed_refresh = norm != "high"
            updated_modes = tuple(
                DisplayMode(
                    mode_id=m.mode_id,
                    width=m.width,
                    height=m.height,
                    refresh_hz=m.refresh_hz,
                    is_current=(m.mode_id == target.mode_id),
                    is_preferred=m.is_preferred,
                    rate_str=m.rate_str,
                )
                for m in info.modes
            )
            cur = next((m for m in updated_modes if m.is_current), target)
            self.last_info = PanelDisplayInfo(
                output_name=info.output_name,
                current_mode=cur,
                modes=updated_modes,
                backend="win32",
            )
        return self.get_telemetry()

    def restore_native(self) -> bool:
        """Restore the primary display to its highest native refresh rate."""
        if not IS_WINDOWS:
            self._we_changed_refresh = False
            return True
        info = self.probe(force=True)
        if info is None:
            return False
        target = select_windows_target_mode(info, profile="high")
        if target is None:
            self._we_changed_refresh = False
            return True
        if self._switch_win32_refresh(target):
            self.current_hz = round(target.refresh_hz, 2)
            self.active_profile = "high"
            self._we_changed_refresh = False
            return True
        return False

    def get_telemetry(self) -> dict[str, Any]:
        return {
            "refresh_output": self.output_name,
            "refresh_hz": self.current_hz,
            "native_refresh_hz": self.native_hz,
            "refresh_profile": self.active_profile,
            "refresh_backend": "win32" if IS_WINDOWS and self.last_info is not None else "none",
        }


class WindowsBacklightBackend:
    """
    Windows laptop internal backlight controller via WMI
    ``WmiMonitorBrightness`` / ``WmiMonitorBrightnessMethods`` (non-windowed PowerShell).
    Automatically backs off on desktops / external monitors without WMI brightness.
    """

    def __init__(self) -> None:
        self._failures: int = 0
        self._backoff_until: float = 0.0
        self._backend_name: str = "wmi" if IS_WINDOWS else "none"

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def get_pct(self) -> int | None:
        if not IS_WINDOWS or time.time() < self._backoff_until:
            return None
        script = (
            "$b = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
            "-ErrorAction SilentlyContinue | Select-Object -First 1; "
            "if ($b) { Write-Output $b.CurrentBrightness }"
        )
        rc, out, _ = _run_powershell(script, timeout=3.0)
        if rc == 0 and out.strip():
            try:
                val = int(round(float(out.strip().splitlines()[-1])))
                if 0 <= val <= 100:
                    self._failures = 0
                    return val
            except ValueError:
                pass
        self._failures += 1
        if self._failures >= 2:
            self._backoff_until = time.time() + 60.0
        return None

    def set_pct(self, pct: int) -> bool:
        if not IS_WINDOWS or time.time() < self._backoff_until:
            return False
        target = max(1, min(100, int(round(pct))))
        script = (
            "$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
            "-ErrorAction SilentlyContinue | Select-Object -First 1; "
            f"if ($m) {{ Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
            f"-Arguments @{{ Timeout = [uint32]1; Brightness = [byte]{target} }} | Out-Null; "
            "Write-Output 'OK' }"
        )
        rc, out, _ = _run_powershell(script, timeout=3.0)
        if rc == 0 and "OK" in out:
            self._failures = 0
            return True
        self._failures += 1
        if self._failures >= 2:
            self._backoff_until = time.time() + 60.0
        return False


# ---------------------------------------------------------------------------
# Adaptive Windows Power Engine
# ---------------------------------------------------------------------------


class WindowsPowerEngine:
    """
    Core stateful Windows adaptive power engine combining:
      - Workload detection (psutil) + 12s downgrade cooldown + Heavy priority boost
      - Power overlay / Wi-Fi PS / PCIe ASPM / USB Selective Suspend
      - Sleep inhibition during active downloads
      - 3-tier adaptive display refresh rate
      - Pure day/night circadian adaptive display brightness (WMI)
      - Persistent state.json, baseline.json, and live status.json telemetry
    """

    def __init__(
        self,
        config_path: str | Path | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.data_dir: Path = data_dir if data_dir is not None else get_windows_data_dir()
        self.config_path: Path = resolve_windows_config_path(config_path)
        self.state_path: Path = self.data_dir / "state.json"
        self.baseline_path: Path = self.data_dir / "baseline.json"
        self.status_path: Path = self.data_dir / "status.json"

        self.config: dict[str, Any] = load_windows_config(self.config_path)
        self.state: dict[str, Any] = self._load_state()

        self.battery_reader = WindowsBatteryTelemetry()
        self.inhibitor = Win32SleepInhibitor()
        self.power_backend = WindowsPowerBackend(baseline_path=self.baseline_path)
        self.refresh_ctrl = WindowsDisplayRefreshController()
        self.brightness_ctrl = AdaptiveBrightnessController(
            backend=WindowsBacklightBackend(),
        )

        self.current_mode: str = "light" if self.enabled else "off"
        self._applied_mode: str | None = None
        self._last_higher_ts: float = 0.0
        self._last_sample_ts: float | None = None
        self._prev_rx_bytes: int | None = None
        self._rx_streak: int = 0
        self._cpu_streak: int = 0
        self._prev_refresh_profile: str = "high"
        self._own_pid: int = os.getpid()
        self.last_status: dict[str, Any] = {}

        # Prime psutil CPU counters so the first tick has meaningful deltas
        if psutil is not None:
            try:
                psutil.cpu_percent(interval=None, percpu=True)
            except Exception:
                pass

    @property
    def enabled(self) -> bool:
        return bool(self.state.get("enabled", True))

    def _load_state(self) -> dict[str, Any]:
        default_state: dict[str, Any] = {
            "enabled": True,
            "brightness_enabled": None,
            "charge_limit_enabled": None,
        }
        raw = read_json_file(self.state_path)
        if raw is None:
            return default_state
        for key in default_state:
            if key in raw:
                default_state[key] = raw[key]
        default_state["enabled"] = bool(default_state["enabled"])
        return default_state

    def _save_state(self) -> None:
        atomic_write_json(self.state_path, self.state)

    def effective_brightness_cfg(self) -> dict[str, Any]:
        return normalize_brightness_config(effective_brightness_config(self.config, self.state))

    def effective_charge_limit_cfg(self) -> dict[str, Any]:
        return resolve_charge_limit_config(self.config, self.state)

    def reload_config(self) -> dict[str, Any]:
        self.config = load_windows_config(self.config_path)
        return self.config

    def set_enabled(self, enabled: bool) -> bool:
        self.state["enabled"] = bool(enabled)
        self._save_state()
        if not self.enabled:
            self.inhibitor.release()
            self.power_backend.apply_mode("off", self.config)
            self.refresh_ctrl.apply_profile("high", enabled=False)
            self._applied_mode = "off"
            self.current_mode = "off"
        return self.enabled

    def toggle_enabled(self) -> bool:
        return self.set_enabled(not self.enabled)

    def toggle_brightness(self) -> bool:
        cur = bool(self.effective_brightness_cfg().get("enabled", True))
        self.state["brightness_enabled"] = not cur
        self._save_state()
        if not cur:
            self.brightness_ctrl.clear_manual_override()
        return not cur

    def set_charge_limit_enabled(self, enabled: bool) -> bool:
        self.state["charge_limit_enabled"] = bool(enabled)
        self._save_state()
        return bool(enabled)

    def toggle_charge_limit(self) -> bool:
        cur = bool(self.effective_charge_limit_cfg().get("limit_enabled", True))
        return self.set_charge_limit_enabled(not cur)

    def _sample_system_metrics(self, now: float) -> tuple[list[dict[str, Any]], float, float, float]:
        """Collect process list, total CPU %, max core CPU %, and network RX MiB/s via psutil."""
        if psutil is None:
            return [], 0.0, 0.0, 0.0

        elapsed = (
            max(0.2, now - self._last_sample_ts)
            if self._last_sample_ts is not None
            else float(self.config.get("general", {}).get("poll_interval_sec", 2.5))
        )
        self._last_sample_ts = now

        # CPU %
        total_cpu = 0.0
        max_core = 0.0
        try:
            per_cpu = psutil.cpu_percent(interval=None, percpu=True)
            if per_cpu:
                total_cpu = sum(per_cpu) / float(len(per_cpu))
                max_core = max(per_cpu)
        except Exception:
            pass

        # Network RX MiB/s (sum all non-loopback interfaces)
        rx_mib_s = 0.0
        try:
            per_nic = psutil.net_io_counters(pernic=True)
            cur_rx = 0
            for nic, stats in per_nic.items():
                low = nic.lower()
                if "loopback" in low or low == "lo":
                    continue
                cur_rx += int(stats.bytes_recv)
            if self._prev_rx_bytes is not None and cur_rx >= self._prev_rx_bytes:
                rx_mib_s = ((cur_rx - self._prev_rx_bytes) / elapsed) / (1024.0 * 1024.0)
            self._prev_rx_bytes = cur_rx
        except Exception:
            pass

        # Process snapshots
        procs: list[dict[str, Any]] = []
        try:
            for p in psutil.process_iter(
                attrs=["pid", "ppid", "name", "cmdline", "cpu_percent", "exe"]
            ):
                try:
                    info = p.info
                    raw_pcpu = float(info.get("cpu_percent") or 0.0)
                    procs.append(
                        {
                            "pid": int(info.get("pid") or 0),
                            "ppid": int(info.get("ppid") or 0),
                            "name": info.get("name") or "",
                            "cmdline": info.get("cmdline") or [],
                            "cpu_pct": raw_pcpu,
                            "exe": info.get("exe") or "",
                        }
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            pass

        return procs, total_cpu, max_core, rx_mib_s

    def _boost_heavy_pids(self, pids: Sequence[int]) -> None:
        """Boost active compile/build/game processes to ABOVE_NORMAL_PRIORITY_CLASS."""
        if not IS_WINDOWS or psutil is None:
            return
        above_normal = getattr(psutil, "ABOVE_NORMAL_PRIORITY_CLASS", None)
        normal_cls = getattr(psutil, "NORMAL_PRIORITY_CLASS", None)
        if above_normal is None or normal_cls is None:
            return
        for pid in pids:
            if pid <= 4 or pid == self._own_pid:
                continue
            try:
                proc = psutil.Process(pid)
                name_low = (proc.name() or "").lower()
                if name_low in WINDOWS_SYSTEM_EXCLUDED:
                    continue
                if proc.nice() == normal_cls:
                    proc.nice(above_normal)
            except Exception:
                continue

    def tick(self, now: float | None = None) -> dict[str, Any]:
        """Run one adaptive evaluation cycle, apply changes, and publish status.json."""
        ts = time.time() if now is None else float(now)
        gen_cfg = self.config.get("general", {})
        bt_cfg = self.config.get("battery_tricks", {})
        w_cfg = self.config.get("workloads", {})
        bcfg = self.effective_brightness_cfg()

        bat = self.battery_reader.read(now=ts)
        ac_online = bool(bat["ac_online"])
        battery_present = bool(bat["battery_present"])
        battery_pct = bat["battery_pct"]

        procs, total_cpu, max_core, rx_mib_s = self._sample_system_metrics(ts)

        rx_thresh = float(w_cfg.get("rx_threshold_mib_s", 1.5))
        cpu_thresh = float(w_cfg.get("cpu_heavy_pct", 25.0))
        core_thresh = float(w_cfg.get("single_core_heavy_pct", 85.0))

        self._rx_streak = (self._rx_streak + 1) if rx_mib_s >= rx_thresh else 0
        self._cpu_streak = (
            (self._cpu_streak + 1)
            if (total_cpu >= cpu_thresh or max_core >= core_thresh)
            else 0
        )

        cls = classify_windows_workload(
            procs,
            total_cpu_pct=total_cpu,
            max_core_cpu_pct=max_core,
            rx_mib_s=rx_mib_s,
            rx_streak=self._rx_streak,
            cpu_streak=self._cpu_streak,
            config=self.config,
            own_pid=self._own_pid,
        )

        if not self.enabled:
            self.current_mode = "off"
            reason = "Smart Optimize paused (unrestricted)"
            self.inhibitor.release()
            if self._applied_mode != "off":
                self.power_backend.apply_mode("off", self.config)
                self._applied_mode = "off"
        else:
            cooldown_sec = float(gen_cfg.get("cooldown_sec", 12.0))
            prev_mode = (
                self.current_mode
                if self.current_mode in ("light", "download", "heavy")
                else "light"
            )
            eff_mode, self._last_higher_ts = apply_mode_cooldown(
                desired_mode=cls["mode"],
                current_mode=prev_mode,
                now=ts,
                last_higher_ts=self._last_higher_ts,
                cooldown_sec=cooldown_sec,
            )
            self.current_mode = eff_mode
            if eff_mode != cls["mode"]:
                reason = f"cooldown ({eff_mode})"
            else:
                reason = cls["reason"]

            if eff_mode == "download" or (eff_mode == "heavy" and cls["has_download"]):
                self.inhibitor.acquire()
            else:
                self.inhibitor.release()

            if self._applied_mode != eff_mode:
                self.power_backend.apply_mode(eff_mode, self.config)
                self._applied_mode = eff_mode

            if eff_mode == "heavy" and cls["boost_pids"]:
                self._boost_heavy_pids(cls["boost_pids"])

        # 3-tier refresh rate policy
        refresh_enabled = bool(bt_cfg.get("refresh_policy_enabled", True))
        ac_hz = float(bt_cfg.get("ac_refresh_hz", 0) or 0)
        battery_hz = float(bt_cfg.get("battery_refresh_hz", 0) or 0)
        low_bat_pct = int(bt_cfg.get("low_battery_refresh_pct", 20) or 20)
        low_hz = float(bt_cfg.get("low_refresh_hz", 60) or 60)

        desired_refresh = compute_windows_refresh_profile(
            enabled=self.enabled,
            ac_online=ac_online,
            battery_pct=battery_pct,
            refresh_policy_enabled=refresh_enabled,
            low_battery_refresh_pct=low_bat_pct,
            prev_profile=self._prev_refresh_profile,
            hysteresis_pct=2,
            battery_present=battery_present,
        )
        self._prev_refresh_profile = desired_refresh
        refresh_tel = self.refresh_ctrl.apply_profile(
            desired_refresh,
            ac_hz=ac_hz,
            battery_hz=battery_hz,
            low_hz=low_hz,
            now=ts,
            enabled=self.enabled and refresh_enabled,
        )

        # Pure day/night circadian adaptive brightness
        bright_tel = self.brightness_ctrl.update(config=bcfg, ac_online=ac_online, now=ts)

        # Battery charge limit telemetry (independent of Smart Optimize ON/OFF)
        cl_cfg = self.effective_charge_limit_cfg()

        status: dict[str, Any] = {
            "platform": "windows",
            "version": VERSION,
            "timestamp": round(ts, 2),
            "enabled": self.enabled,
            "mode": self.current_mode,
            "reason": reason,
            "ac_online": ac_online,
            "battery_present": battery_present,
            "battery_pct": battery_pct,
            "battery_status": bat["battery_status"],
            "battery_watts": bat["battery_watts"],
            "charge_limit_enabled": bool(cl_cfg["limit_enabled"]),
            "charge_limit_pct": int(cl_cfg["stop_threshold"]),
            "charge_limit_hw_supported": False,
            "charge_limit_backend": "alert-only",
            "charge_limit_active_value": None,
            "charge_limit_notify_fallback": bool(cl_cfg["notify_when_no_hw_limit"]),
            "cpu_pct": round(total_cpu, 1),
            "max_core_cpu_pct": round(max_core, 1),
            "rx_mib_s": round(rx_mib_s, 2),
            "power_overlay": self.power_backend.current_overlay,
            "wifi_power_save": self.power_backend.current_wifi_ps,
            "pcie_aspm": self.power_backend.current_pcie_aspm,
            "usb_selective_suspend": self.power_backend.current_usb_suspend,
            "sleep_inhibit": self.inhibitor.active,
            "desired_refresh_profile": desired_refresh,
            "ac_refresh_hz": int(round(ac_hz)),
            "battery_refresh_hz": int(round(battery_hz)),
            "low_battery_refresh_pct": low_bat_pct,
            "low_refresh_hz": int(round(low_hz)),
            "brightness_config": bcfg,
        }
        status.update(refresh_tel)
        status.update(bright_tel)

        self.last_status = status
        atomic_write_json(self.status_path, status)
        return status

    def restore_all(self) -> None:
        """Restore native display refresh rate, baseline power settings, and clear sleep inhibit."""
        self.inhibitor.release()
        self.refresh_ctrl.restore_native()
        self.power_backend.restore_baseline()
        self.brightness_ctrl.cleanup()


# ---------------------------------------------------------------------------
# Formatting Helpers (Shared Between CLI --status and Tray Tooltip)
# ---------------------------------------------------------------------------


def _format_hz(hz: float | None) -> str:
    if hz is None:
        return "--"
    if abs(float(hz) - round(float(hz))) < 0.15:
        return f"{int(round(float(hz)))} Hz"
    return f"{float(hz):.1f} Hz"


def _refresh_tier_label(profile: str, ac_online: bool) -> str:
    norm = (profile or "high").strip().lower()
    if norm == "low":
        return "Low Battery"
    if norm == "mid":
        return "Battery"
    return "AC" if ac_online else "Native"


def format_refresh_line(status: dict[str, Any]) -> str:
    cur_hz = status.get("refresh_hz")
    if cur_hz is None:
        return "Unavailable"
    profile = str(status.get("desired_refresh_profile") or status.get("refresh_profile") or "high")
    ac_online = bool(status.get("ac_online", True))
    tier = _refresh_tier_label(profile, ac_online)
    return f"{_format_hz(cur_hz)} ({tier})"


def format_brightness_line(status: dict[str, Any]) -> str:
    pct = status.get("brightness_pct")
    source = str(status.get("brightness_source") or "Unavailable")
    override_until = float(status.get("manual_override_until") or 0.0)
    if pct is None:
        return f"Unavailable ({source})"
    if source == "Manual Pause":
        rem_min = max(1, int(round((override_until - time.time()) / 60.0)))
        return f"{pct}% (Manual Pause - {rem_min}m)"
    return f"{pct}% ({source})"


def format_battery_line(status: dict[str, Any]) -> str:
    if not status.get("battery_present", True):
        return "No battery (AC)"
    watts = float(status.get("battery_watts") or 0.0)
    bstat = str(status.get("battery_status") or "Unknown").strip()
    bstat_display = bstat.title() if bstat else "Unknown"
    bpct = status.get("battery_pct")
    pct_str = f"{int(round(float(bpct)))}%" if bpct is not None else "?%"
    if watts > 0.0:
        return f"{pct_str} — {bstat_display} ({watts:.2f} W)"
    return f"{pct_str} — {bstat_display}"


def format_charge_limit_line(status: dict[str, Any]) -> str:
    cl_enabled = bool(status.get("charge_limit_enabled", True))
    limit_pct = int(status.get("charge_limit_pct", 80) or 80)
    hw_sup = bool(status.get("charge_limit_hw_supported", False))
    active_val = status.get("charge_limit_active_value")
    if not cl_enabled:
        return "OFF (100%)"
    if hw_sup:
        return f"{limit_pct}% (ON — {active_val})" if active_val else f"{limit_pct}% (ON)"
    return f"{limit_pct}% (Alert Only)"


def format_status_report(status: dict[str, Any]) -> str:
    """Format status.json as a clean human-readable CLI report."""
    enabled = bool(status.get("enabled", True))
    mode = str(status.get("mode", "light" if enabled else "off")).upper()
    state_str = f"ON ({mode})" if enabled else "OFF (UNRESTRICTED)"
    lines = [
        f"Smart Optimize : {state_str}",
        f"Trigger        : {status.get('reason', '--')}",
        f"Battery        : {format_battery_line(status)}",
        f"Charge Limit   : {format_charge_limit_line(status)}",
        f"Power Overlay  : {status.get('power_overlay', 'Unknown')}",
        f"Wi-Fi PS       : {status.get('wifi_power_save', 'Unknown')}",
        f"PCIe ASPM      : {status.get('pcie_aspm', 'Unknown')}",
        f"Sleep Inhibit  : {bool(status.get('sleep_inhibit', False))}",
        f"CPU / RX       : {status.get('cpu_pct', 0.0):.1f}% CPU | {status.get('rx_mib_s', 0.0):.2f} MiB/s RX",
        f"Refresh Rate   : {format_refresh_line(status)} [{status.get('desired_refresh_profile', 'high')}]",
        f"Brightness     : {format_brightness_line(status)}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PyQt6 System Tray UI (Imported Lazily so CLI / CI Tests Never Require PyQt6)
# ---------------------------------------------------------------------------


def _run_tray_app(engine: WindowsPowerEngine) -> int:
    try:
        from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
        from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF
        from PyQt6.QtNetwork import QLocalServer, QLocalSocket
        from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    except ImportError as exc:
        print(
            f"smart-powerd: PyQt6 is required for --tray mode ({exc}). "
            "Install requirements-windows.txt or run with --headless.",
            file=sys.stderr,
        )
        return 1

    def render_tray_icon(
        mode: str,
        enabled: bool,
        battery_pct: int | None = None,
        charging: bool = False,
    ) -> QIcon:
        key = mode if enabled else "off"
        bg_hex = MODE_COLORS.get(key, MODE_COLORS["off"])

        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        if battery_pct is not None:
            pct_val = max(0, min(100, int(round(battery_pct))))
            text = str(pct_val)

            # High-contrast rounded badge so percentage digits remain crisp at 16-24px tray scale
            painter.setPen(QPen(QColor("#1c242b"), 3.0))
            painter.setBrush(QColor(bg_hex))
            painter.drawRoundedRect(QRectF(3.0, 3.0, 58.0, 58.0), 12.0, 12.0)

            font = QFont()
            font.setStyleHint(QFont.StyleHint.SansSerif)
            font.setBold(True)
            if len(text) >= 3:
                font.setPixelSize(26)
            elif len(text) == 2:
                font.setPixelSize(34)
            else:
                font.setPixelSize(38)
            painter.setFont(font)

            text_rect = QRectF(2.0, 1.0 if charging else 2.0, 60.0, 56.0 if charging else 60.0)
            # Subtle dark shadow for crisp contrast on all mode colors
            painter.setPen(QColor("#1c242b"))
            painter.drawText(text_rect.translated(1.5, 1.5), Qt.AlignmentFlag.AlignCenter, text)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)

            if charging:
                # Subtle bright yellow/gold charging accent bar + top-right dot
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#fff176"))
                painter.drawRoundedRect(QRectF(14.0, 52.0, 36.0, 5.0), 2.5, 2.5)
                painter.setPen(QPen(QColor("#1c242b"), 1.5))
                painter.drawEllipse(QRectF(47.0, 7.0, 10.0, 10.0))

            painter.end()
            return QIcon(pixmap)

        # Desktop / no-battery fallback: classic circular mode badge
        painter.setPen(QPen(QColor("#1c242b"), 3.0))
        painter.setBrush(QColor(bg_hex))
        painter.drawEllipse(4, 4, 56, 56)

        painter.setPen(
            QPen(
                QColor("#ffffff"),
                3.5,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(QColor("#ffffff"))

        if not enabled or key == "off":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(18, 18, 28, 28, 130 * 16, 280 * 16)
            painter.drawLine(32, 13, 32, 29)
            painter.setPen(
                QPen(QColor("#e74c3c"), 4.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            )
            painter.drawLine(16, 48, 48, 16)
        elif key == "download":
            painter.drawLine(32, 16, 32, 39)
            arrow = QPolygonF([QPointF(21, 31), QPointF(32, 43), QPointF(43, 31)])
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(arrow)
            painter.drawLine(19, 48, 45, 48)
        elif key == "heavy":
            bolt = QPolygonF(
                [
                    QPointF(35, 13),
                    QPointF(20, 34),
                    QPointF(31, 34),
                    QPointF(27, 51),
                    QPointF(44, 29),
                    QPointF(33, 29),
                ]
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(bolt)
        else:
            bolt = QPolygonF(
                [
                    QPointF(34, 15),
                    QPointF(22, 34),
                    QPointF(31, 34),
                    QPointF(28, 49),
                    QPointF(42, 30),
                    QPointF(33, 30),
                ]
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(bolt)

        painter.end()
        return QIcon(pixmap)

    def acquire_lock(server_name: str) -> QLocalServer | None:
        probe = QLocalSocket()
        probe.connectToServer(server_name)
        if probe.waitForConnected(200):
            probe.disconnectFromServer()
            return None
        QLocalServer.removeServer(server_name)
        srv = QLocalServer()
        if not srv.listen(server_name):
            return None
        return srv

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Smart Power")

    lock_server = acquire_lock("smart-powerd-win-tray")
    if lock_server is None:
        return 0

    class SmartPowerWinTray:
        def __init__(self) -> None:
            self._icon_cache: dict[tuple[str, int | None, bool], QIcon] = {}
            self._last_icon_key: tuple[str, int | None, bool] | None = None
            self._charge_limit_notified: bool = False
            self._cleaned_up = False

            self.tray = QSystemTrayIcon()
            self.tray.activated.connect(self._on_activated)

            self.menu = QMenu()
            self.act_state = self._make_info("Smart Optimize: Starting...")
            self.act_reason = self._make_info("Trigger: --")
            self.act_battery = self._make_info("Battery: --")
            self.act_charge_limit = self._make_info("Charge Limit: --")
            self.act_overlay = self._make_info("Overlay: -- | Wi-Fi PS: --")
            self.act_refresh = self._make_info("Refresh: --")
            self.act_brightness = self._make_info("Brightness: --")

            for act in (
                self.act_state,
                self.act_reason,
                self.act_battery,
                self.act_charge_limit,
                self.act_overlay,
                self.act_refresh,
                self.act_brightness,
            ):
                self.menu.addAction(act)

            self.menu.addSeparator()

            self.act_toggle = QAction("Turn Smart Optimize OFF", self.menu)
            self.act_toggle.triggered.connect(self._on_toggle)
            self.menu.addAction(self.act_toggle)

            self.act_charge_limit_toggle = QAction("80% Charge Limit: ON", self.menu)
            self.act_charge_limit_toggle.triggered.connect(self._on_charge_limit_toggle)
            self.menu.addAction(self.act_charge_limit_toggle)

            self.act_brightness_toggle = QAction("Auto Brightness: ON", self.menu)
            self.act_brightness_toggle.triggered.connect(self._on_brightness_toggle)
            self.menu.addAction(self.act_brightness_toggle)

            self.act_open_config = QAction("Open Config", self.menu)
            self.act_open_config.triggered.connect(self._on_open_config)
            self.menu.addAction(self.act_open_config)

            self.menu.addSeparator()

            self.act_reload = QAction("Reload Config", self.menu)
            self.act_reload.triggered.connect(self._on_reload)
            self.menu.addAction(self.act_reload)

            self.act_quit = QAction("Quit", self.menu)
            self.act_quit.triggered.connect(self._on_quit)
            self.menu.addAction(self.act_quit)

            self.tray.setContextMenu(self.menu)

            poll_ms = max(
                1000,
                int(
                    round(
                        float(engine.config.get("general", {}).get("poll_interval_sec", 2.5)) * 1000
                    )
                ),
            )
            self.poll_timer = QTimer(app)
            self.poll_timer.timeout.connect(self._on_poll)

            app.aboutToQuit.connect(self.cleanup)

            self._on_poll()
            self.tray.show()
            self.poll_timer.start(poll_ms)

        def _make_info(self, text: str) -> QAction:
            act = QAction(text, self.menu)
            act.setEnabled(False)
            return act

        def _get_icon(
            self,
            mode: str,
            enabled: bool,
            battery_pct: int | None,
            charging: bool,
        ) -> QIcon:
            mode_key = mode if enabled else "off"
            pct_key = (
                max(0, min(100, int(round(battery_pct))))
                if battery_pct is not None
                else None
            )
            cache_key = (mode_key, pct_key, bool(charging))
            if cache_key not in self._icon_cache:
                if len(self._icon_cache) >= 64:
                    oldest = next(iter(self._icon_cache))
                    self._icon_cache.pop(oldest, None)
                self._icon_cache[cache_key] = render_tray_icon(
                    mode, enabled, battery_pct=pct_key, charging=bool(charging)
                )
            return self._icon_cache[cache_key]

        def _on_poll(self) -> None:
            st = engine.tick(now=time.time())
            self._refresh_ui(st)

        def _refresh_ui(self, st: dict[str, Any]) -> None:
            enabled = bool(st.get("enabled", True))
            mode = str(st.get("mode", "light" if enabled else "off"))
            bat_present = bool(st.get("battery_present", False))
            raw_pct = st.get("battery_pct") if bat_present else None
            pct_int = (
                max(0, min(100, int(round(float(raw_pct)))))
                if isinstance(raw_pct, (int, float))
                else None
            )
            bat_status = str(st.get("battery_status") or "")
            is_charging = bat_present and bat_status.strip().lower() == "charging"

            icon_key = (mode if enabled else "off", pct_int, is_charging)
            if icon_key != self._last_icon_key:
                self.tray.setIcon(self._get_icon(mode, enabled, pct_int, is_charging))
                self._last_icon_key = icon_key

            toggle_label = "ON" if enabled else "OFF"
            mode_label = MODE_LABELS.get(mode, mode.title())
            reason = str(st.get("reason") or "--")
            bat_line = format_battery_line(st)
            charge_limit_line = format_charge_limit_line(st)
            overlay = str(st.get("power_overlay") or "Unknown")
            wifi_ps = str(st.get("wifi_power_save") or "Unknown")
            refresh_line = format_refresh_line(st)
            bright_line = format_brightness_line(st)
            bcfg = engine.effective_brightness_cfg()
            cl_enabled = bool(st.get("charge_limit_enabled", True))
            cl_pct = int(st.get("charge_limit_pct", 80) or 80)

            tooltip = (
                f"Smart Optimize: {toggle_label} | Mode: {mode_label}\n"
                f"Trigger: {reason}\n"
                f"Battery: {bat_line}\n"
                f"Charge Limit: {charge_limit_line}\n"
                f"Overlay: {overlay} | Wi-Fi PS: {wifi_ps}\n"
                f"Refresh: {refresh_line}\n"
                f"Brightness: {bright_line}"
            )
            self.tray.setToolTip(tooltip)

            self.act_state.setText(f"Smart Optimize: {toggle_label} ({mode_label})")
            self.act_reason.setText(f"Trigger: {reason[:52]}")
            self.act_battery.setText(f"Battery: {bat_line}")
            self.act_charge_limit.setText(f"Charge Limit: {charge_limit_line}")
            self.act_overlay.setText(f"Overlay: {overlay} | Wi-Fi PS: {wifi_ps}")
            self.act_refresh.setText(f"Refresh: {refresh_line}")
            self.act_brightness.setText(f"Brightness: {bright_line}")

            self.act_toggle.setText(
                "Turn Smart Optimize OFF" if enabled else "Turn Smart Optimize ON"
            )
            self.act_charge_limit_toggle.setText(
                f"{cl_pct}% Charge Limit: {'ON' if cl_enabled else 'OFF'}"
            )
            self.act_brightness_toggle.setText(
                f"Auto Brightness: {'ON' if bcfg.get('enabled', True) else 'OFF'}"
            )

            # Once-per-charge-cycle Windows notification when charging reaches stop_threshold
            should_notify, next_notified = evaluate_charge_limit_alert(
                charge_limit_enabled=cl_enabled,
                charge_limit_hw_supported=bool(st.get("charge_limit_hw_supported", False)),
                charge_limit_notify_fallback=bool(st.get("charge_limit_notify_fallback", True)),
                charge_limit_pct=cl_pct,
                ac_online=bool(st.get("ac_online", True)),
                battery_present=bat_present,
                battery_status=bat_status,
                battery_pct=pct_int,
                already_notified=self._charge_limit_notified,
            )
            self._charge_limit_notified = next_notified
            if should_notify and pct_int is not None:
                self.tray.showMessage(
                    f"Battery reached {cl_pct}%",
                    (
                        f"Battery is at {pct_int}% while charging. "
                        "Unplug AC (or enable your OEM battery conservation limit) "
                        "to preserve long-term battery health."
                    ),
                    QSystemTrayIcon.MessageIcon.Information,
                    8000,
                )

        def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                self._on_toggle()

        def _on_toggle(self) -> None:
            engine.toggle_enabled()
            self._on_poll()

        def _on_charge_limit_toggle(self) -> None:
            engine.toggle_charge_limit()
            self._charge_limit_notified = False
            self._on_poll()

        def _on_brightness_toggle(self) -> None:
            engine.toggle_brightness()
            self._on_poll()

        def _on_open_config(self) -> None:
            target = engine.data_dir / "config.toml"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file():
                bundled = find_bundled_config_example()
                if bundled is not None:
                    shutil.copy2(bundled, target)
            if IS_WINDOWS and hasattr(os, "startfile"):
                try:
                    os.startfile(str(target))  # type: ignore[attr-defined]
                except OSError:
                    pass

        def _on_reload(self) -> None:
            engine.reload_config()
            self._on_poll()

        def _on_quit(self) -> None:
            self.cleanup()
            app.quit()

        def cleanup(self) -> None:
            if self._cleaned_up:
                return
            self._cleaned_up = True
            self.poll_timer.stop()
            engine.restore_all()
            lock_server.close()

    _tray = SmartPowerWinTray()
    return app.exec()


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smart-powerd",
        description="Adaptive Windows 10/11 laptop power, refresh-rate, and circadian brightness optimizer.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--tray",
        action="store_true",
        help="Run with the PyQt6 system tray UI (default).",
    )
    mode_group.add_argument(
        "--headless",
        action="store_true",
        help="Run the adaptive optimization loop without a GUI tray icon.",
    )
    mode_group.add_argument(
        "--once",
        action="store_true",
        help="Run a single evaluation tick, update status.json, and print status.",
    )
    mode_group.add_argument(
        "--status",
        action="store_true",
        help="Print current live status from %%LOCALAPPDATA%%\\smart-powerd\\status.json.",
    )
    mode_group.add_argument(
        "--restore",
        action="store_true",
        help="Restore native refresh rate, Balanced overlay, and baseline power settings.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON when used with --status or --once.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Optional explicit path to config.toml.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print version and exit 0.",
    )
    return parser


def _ensure_std_streams() -> None:
    """Guard against None stdout/stderr in PyInstaller --windowed executables."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_std_streams()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"smart-powerd {VERSION}", flush=True)
        return 0

    if args.status:
        status_file = get_windows_data_dir() / "status.json"
        data = read_json_file(status_file)
        if data is None:
            print(
                f"smart-powerd status unavailable ({status_file} not found). "
                "Start smart-powerd first.",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(data, indent=2), flush=True)
        else:
            print(format_status_report(data), flush=True)
        return 0

    engine = WindowsPowerEngine(config_path=args.config)

    if args.restore:
        engine.restore_all()
        print(
            "Restored baseline Windows power scheme, Wi-Fi/PCIe settings, and native refresh rate.",
            flush=True,
        )
        return 0

    if args.once:
        st = engine.tick()
        if args.json:
            print(json.dumps(st, indent=2), flush=True)
        else:
            print(format_status_report(st), flush=True)
        return 0

    if args.headless:
        running = True

        def _handle_stop(_signum: int, _frame: Any) -> None:
            nonlocal running
            running = False

        signal.signal(signal.SIGINT, _handle_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handle_stop)

        try:
            while running:
                engine.tick()
                interval = max(
                    0.5,
                    float(engine.config.get("general", {}).get("poll_interval_sec", 2.5)),
                )
                deadline = time.monotonic() + interval
                while running and time.monotonic() < deadline:
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        finally:
            engine.restore_all()
        return 0

    # Default: --tray
    return _run_tray_app(engine)


if __name__ == "__main__":
    raise SystemExit(main())
