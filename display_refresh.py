#!/usr/bin/env python3
"""
Standalone cross-platform 3-tier laptop display refresh rate switching module.

Runs in the user's graphical session without elevated privileges.
Supports:
  - KDE Plasma 6 Wayland/X11 via ``kscreen-doctor``
  - wlroots compositors (Hyprland, Sway, River, etc.) via ``wlr-randr``
  - GNOME via ``gnome-randr`` or ``gdctl``
  - X11 via ``xrandr``
  - Windows 10/11 via native Win32 ``user32.dll`` (``ctypes`` — zero external tools)

3-tier adaptive refresh policy (automatically adapts to any panel's modes):
  - ``"high"`` (AC / OFF): highest native Hz (e.g. 300 Hz, 144 Hz) or ``ac_hz`` override
  - ``"mid"``  (Battery >= threshold): second-highest Hz when 3+ rates exist
               (e.g. 300/240/60 -> 240 Hz, 165/120/60 -> 120 Hz), or ``high`` on
               2-rate panels (e.g. 144/60 -> 144 Hz), or ``battery_hz`` override
  - ``"low"``  (Battery < threshold): lowest safe Hz >= 59 Hz (e.g. 60 Hz) or ``low_hz``
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

_LIB_DIR = Path("/usr/local/lib/smart-power")
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
if (sys.platform != "win32" or _LIB_DIR.is_dir()) and str(_LIB_DIR) not in sys.path:
    sys.path.append(str(_LIB_DIR))

try:
    from smart_power_config import load_config as _shared_load_config
except ImportError:
    _shared_load_config = None

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
INTERNAL_PANEL_RE = re.compile(r"^(?:eDP|LVDS|DSI)(?:[-_]?\d+)*$", re.IGNORECASE)

KSCREEN_OUTPUT_RE = re.compile(r"^\s*Output:\s+\d+\s+(\S+)", re.IGNORECASE)
KSCREEN_MODE_RE = re.compile(r"([A-Za-z0-9_-]+):(\d+)x(\d+)@(\d+(?:\.\d+)?)([*!]*)")

WLR_MODE_RE = re.compile(
    r"^\s+(\d+)x(\d+)\s+px,\s*(\d+(?:\.\d+)?)\s*Hz(?:\s*\(([^)]*)\))?",
    re.IGNORECASE,
)

XRANDR_OUTPUT_RE = re.compile(r"^(\S+)\s+(connected|disconnected)\b", re.IGNORECASE)
XRANDR_MODE_LINE_RE = re.compile(r"^\s+(\d+)x(\d+)\s+(.+)$")
XRANDR_RATE_TOKEN_RE = re.compile(r"(?<!\S)(\d+(?:\.\d+)?)([*+]*)")

GNOME_MONITOR_RE = re.compile(r"(?:Monitor\s+|^)(\S+)", re.IGNORECASE)
GNOME_MODE_RE = re.compile(
    r"(\d+)x(\d+)@(\d+(?:\.\d+)?)([*+]*)(?:\s*\(([^)]*)\))?",
    re.IGNORECASE,
)

MIN_SAFE_REFRESH_HZ = 59.0


@dataclass(frozen=True)
class DisplayMode:
    """Single display resolution + refresh rate mode."""

    mode_id: str
    width: int
    height: int
    refresh_hz: float
    is_current: bool = False
    is_preferred: bool = False
    rate_str: str = ""


@dataclass(frozen=True)
class PanelDisplayInfo:
    """Parsed state and available modes of the built-in laptop panel."""

    output_name: str
    current_mode: DisplayMode | None
    modes: tuple[DisplayMode, ...]
    backend: str = ""


def load_default_low_refresh_hz(path: str | Path | None = None) -> float:
    """Read configured low_refresh_hz via smart_power_config with safe default 60.0."""
    if _shared_load_config is not None:
        cfg = _shared_load_config(path) if path is not None else _shared_load_config()
        if isinstance(cfg, dict):
            bt = cfg.get("battery_tricks")
            if isinstance(bt, dict) and bt.get("low_refresh_hz") is not None:
                try:
                    return max(MIN_SAFE_REFRESH_HZ, float(bt["low_refresh_hz"]))
                except (TypeError, ValueError):
                    pass
    return 60.0


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI color/formatting escape sequences from CLI output."""
    return ANSI_ESCAPE_RE.sub("", text or "")


def is_internal_panel_name(name: str) -> bool:
    """Return True if connector name looks like a built-in laptop panel (eDP/LVDS/DSI)."""
    return bool(INTERNAL_PANEL_RE.match((name or "").strip()))


def parse_kscreen_output(raw_text: str) -> PanelDisplayInfo | None:
    """
    Parse ``kscreen-doctor -o`` output (including ANSI color escape sequences)
    and return the active internal laptop panel's modes.
    """
    clean = strip_ansi(raw_text)
    if not clean.strip():
        return None

    blocks: list[tuple[str, list[str]]] = []
    cur_name: str | None = None
    cur_lines: list[str] = []

    for line in clean.splitlines():
        m = KSCREEN_OUTPUT_RE.match(line)
        if m:
            if cur_name is not None:
                blocks.append((cur_name, cur_lines))
            cur_name = m.group(1).strip()
            cur_lines = []
        elif cur_name is not None:
            cur_lines.append(line)

    if cur_name is not None:
        blocks.append((cur_name, cur_lines))

    candidates: list[tuple[int, PanelDisplayInfo]] = []

    for output_name, lines in blocks:
        stripped_lines = [ln.strip() for ln in lines]
        lower_lines = [ln.lower() for ln in stripped_lines]

        if "disabled" in lower_lines or "disconnected" in lower_lines:
            continue

        is_internal = is_internal_panel_name(output_name)
        is_panel_type = "panel" in lower_lines
        if not is_internal and not is_panel_type:
            continue

        modes_chunks: list[str] = []
        in_modes = False
        for ln in stripped_lines:
            if ln.startswith("Modes:"):
                modes_chunks.append(ln[len("Modes:") :].strip())
                in_modes = True
            elif in_modes:
                # Continuation lines only if another mode token appears without a new field header
                if ":" in ln and not KSCREEN_MODE_RE.match(ln):
                    in_modes = False
                elif KSCREEN_MODE_RE.search(ln):
                    modes_chunks.append(ln)
                else:
                    in_modes = False

        modes_text = " ".join(modes_chunks)
        modes: list[DisplayMode] = []
        current_mode: DisplayMode | None = None

        for mm in KSCREEN_MODE_RE.finditer(modes_text):
            mode_id = mm.group(1)
            width = int(mm.group(2))
            height = int(mm.group(3))
            rate_str = mm.group(4)
            hz = float(rate_str)
            flags = mm.group(5) or ""
            is_cur = "*" in flags
            is_pref = "!" in flags
            dm = DisplayMode(
                mode_id=mode_id,
                width=width,
                height=height,
                refresh_hz=hz,
                is_current=is_cur,
                is_preferred=is_pref,
                rate_str=rate_str,
            )
            modes.append(dm)
            if is_cur:
                current_mode = dm

        if not modes:
            continue

        priority = 0 if is_internal else 1
        candidates.append(
            (
                priority,
                PanelDisplayInfo(
                    output_name=output_name,
                    current_mode=current_mode,
                    modes=tuple(modes),
                    backend="kscreen-doctor",
                ),
            )
        )

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def parse_wlr_randr_output(raw_text: str) -> PanelDisplayInfo | None:
    """Parse ``wlr-randr`` output for the built-in internal laptop panel."""
    clean = strip_ansi(raw_text)
    if not clean.strip():
        return None

    blocks: list[tuple[str, list[str]]] = []
    cur_name: str | None = None
    cur_lines: list[str] = []

    for line in clean.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            if cur_name is not None:
                blocks.append((cur_name, cur_lines))
            cur_name = line.strip().split()[0]
            cur_lines = []
        elif cur_name is not None:
            cur_lines.append(line)

    if cur_name is not None:
        blocks.append((cur_name, cur_lines))

    for output_name, lines in blocks:
        if not is_internal_panel_name(output_name):
            continue

        disabled = any(ln.strip().lower() == "enabled: no" for ln in lines)
        if disabled:
            continue

        modes: list[DisplayMode] = []
        current_mode: DisplayMode | None = None
        for ln in lines:
            mm = WLR_MODE_RE.match(ln)
            if not mm:
                continue
            width = int(mm.group(1))
            height = int(mm.group(2))
            rate_str = mm.group(3)
            hz = float(rate_str)
            flags = (mm.group(4) or "").lower()
            is_cur = "current" in flags
            is_pref = "preferred" in flags
            dm = DisplayMode(
                mode_id=f"{width}x{height}@{rate_str}Hz",
                width=width,
                height=height,
                refresh_hz=hz,
                is_current=is_cur,
                is_preferred=is_pref,
                rate_str=rate_str,
            )
            modes.append(dm)
            if is_cur:
                current_mode = dm

        if modes:
            return PanelDisplayInfo(
                output_name=output_name,
                current_mode=current_mode,
                modes=tuple(modes),
                backend="wlr-randr",
            )
    return None


def parse_xrandr_output(raw_text: str) -> PanelDisplayInfo | None:
    """Parse ``xrandr`` output for the built-in internal laptop panel."""
    clean = strip_ansi(raw_text)
    if not clean.strip():
        return None

    blocks: list[tuple[str, list[str]]] = []
    cur_name: str | None = None
    cur_lines: list[str] = []

    for line in clean.splitlines():
        hm = XRANDR_OUTPUT_RE.match(line)
        if hm:
            if cur_name is not None:
                blocks.append((cur_name, cur_lines))
            name = hm.group(1).strip()
            state = hm.group(2).lower()
            if state == "connected":
                cur_name = name
                cur_lines = []
            else:
                cur_name = None
                cur_lines = []
        elif cur_name is not None and line.startswith((" ", "\t")):
            cur_lines.append(line)
        elif line and not line[0].isspace():
            if cur_name is not None:
                blocks.append((cur_name, cur_lines))
            cur_name = None
            cur_lines = []

    if cur_name is not None:
        blocks.append((cur_name, cur_lines))

    for output_name, lines in blocks:
        if not is_internal_panel_name(output_name):
            continue

        modes: list[DisplayMode] = []
        current_mode: DisplayMode | None = None

        for ln in lines:
            mm = XRANDR_MODE_LINE_RE.match(ln)
            if not mm:
                continue
            width = int(mm.group(1))
            height = int(mm.group(2))
            rates_part = re.sub(r"\([^)]*\)", "", mm.group(3))

            for rm in XRANDR_RATE_TOKEN_RE.finditer(rates_part):
                rate_str = rm.group(1)
                flags = rm.group(2) or ""
                hz = float(rate_str)
                is_cur = "*" in flags
                is_pref = "+" in flags
                dm = DisplayMode(
                    mode_id=f"{width}x{height}@{rate_str}",
                    width=width,
                    height=height,
                    refresh_hz=hz,
                    is_current=is_cur,
                    is_preferred=is_pref,
                    rate_str=rate_str,
                )
                modes.append(dm)
                if is_cur:
                    current_mode = dm

        if modes:
            return PanelDisplayInfo(
                output_name=output_name,
                current_mode=current_mode,
                modes=tuple(modes),
                backend="xrandr",
            )
    return None


def parse_gnome_output(raw_text: str, backend: str = "gdctl") -> PanelDisplayInfo | None:
    """Parse GNOME ``gdctl show`` or ``gnome-randr`` output for the built-in panel."""
    clean = strip_ansi(raw_text)
    if not clean.strip():
        return None

    blocks: list[tuple[str, list[str]]] = []
    cur_name: str | None = None
    cur_lines: list[str] = []

    for line in clean.splitlines():
        stripped = line.strip().lstrip("├└│─ ")
        mon_match = re.match(r"^(?:Monitor\s+)?((?:eDP|LVDS|DSI|HDMI|DP|VGA|DVI)\S*)\b", stripped, re.IGNORECASE)
        if mon_match and (
            "Monitor " in line or not line[0].isspace()
        ):
            if cur_name is not None:
                blocks.append((cur_name, cur_lines))
            cur_name = mon_match.group(1).rstrip(":")
            cur_lines = []
        elif cur_name is not None:
            cur_lines.append(stripped)

    if cur_name is not None:
        blocks.append((cur_name, cur_lines))

    for output_name, lines in blocks:
        if not is_internal_panel_name(output_name):
            continue

        modes: list[DisplayMode] = []
        current_mode: DisplayMode | None = None
        for ln in lines:
            mm = GNOME_MODE_RE.search(ln)
            if not mm:
                continue
            width = int(mm.group(1))
            height = int(mm.group(2))
            rate_str = mm.group(3)
            hz = float(rate_str)
            sym_flags = mm.group(4) or ""
            paren_flags = (mm.group(5) or "").lower()
            is_cur = ("*" in sym_flags) or ("current" in paren_flags)
            is_pref = ("+" in sym_flags) or ("preferred" in paren_flags)
            dm = DisplayMode(
                mode_id=f"{width}x{height}@{rate_str}",
                width=width,
                height=height,
                refresh_hz=hz,
                is_current=is_cur,
                is_preferred=is_pref,
                rate_str=rate_str,
            )
            modes.append(dm)
            if is_cur:
                current_mode = dm

        if modes:
            return PanelDisplayInfo(
                output_name=output_name,
                current_mode=current_mode,
                modes=tuple(modes),
                backend=backend,
            )
    return None


def _extract_modes_and_reference(
    source: PanelDisplayInfo | Sequence[DisplayMode],
) -> tuple[list[DisplayMode], DisplayMode | None]:
    """Normalize a PanelDisplayInfo or DisplayMode sequence into (same_res_modes, current_mode)."""
    if isinstance(source, PanelDisplayInfo):
        all_modes = list(source.modes)
        current_mode = source.current_mode
    else:
        all_modes = list(source)
        current_mode = next((m for m in all_modes if m.is_current), None)

    if not all_modes:
        return [], None

    ref_mode = current_mode
    if ref_mode is None:
        ref_mode = next((m for m in all_modes if m.is_preferred), all_modes[0])

    same_res = [
        m for m in all_modes if m.width == ref_mode.width and m.height == ref_mode.height
    ]
    return same_res, current_mode


def _pick_mode_at_or_below(
    candidates: Sequence[DisplayMode],
    target_hz: float,
) -> DisplayMode | None:
    """Pick the highest mode <= target_hz (+0.5 Hz tolerance), else closest overall."""
    if not candidates:
        return None
    at_or_below = [m for m in candidates if m.refresh_hz <= (target_hz + 0.5)]
    if at_or_below:
        return max(
            at_or_below,
            key=lambda m: (
                round(m.refresh_hz, 2),
                1 if m.is_preferred else 0,
                1 if m.is_current else 0,
            ),
        )
    return min(
        candidates,
        key=lambda m: (
            abs(m.refresh_hz - target_hz),
            m.refresh_hz,
            0 if m.is_preferred else 1,
        ),
    )


def choose_refresh_modes(
    modes: PanelDisplayInfo | Sequence[DisplayMode],
    ac_hz: float = 0.0,
    battery_hz: float = 0.0,
    low_hz: float = 60.0,
    *,
    low_refresh_hz: float | None = None,
) -> dict[str, DisplayMode | None]:
    """
    Determine 3-tier refresh modes ``{"high": Mode, "mid": Mode, "low": Mode}``
    at the panel's active resolution (never changes resolution):

    - ``high``: if ``ac_hz > 0``, closest mode ``<= ac_hz`` (fallback closest);
      otherwise highest Hz at active resolution.
    - ``low``: closest mode to ``low_hz`` preferring ``>= 59 Hz``.
    - ``mid``: if ``battery_hz > 0``, closest mode ``<= battery_hz`` (fallback closest);
      otherwise if ``>= 3`` distinct rounded Hz values exist at the active resolution
      and the second-highest Hz is strictly ``> low.refresh_hz + 1.0``, pick that
      second-highest Hz (e.g. 300/240/60 -> 240, 240/165/60 -> 165, 165/120/60 -> 120,
      144/90/60 -> 90); otherwise pick ``high`` (e.g. 144/60 -> 144).
    """
    effective_low_hz = float(low_refresh_hz) if low_refresh_hz is not None else float(low_hz)
    same_res, _ = _extract_modes_and_reference(modes)
    if not same_res:
        return {"high": None, "mid": None, "low": None}

    # 1. High tier (AC / unrestricted)
    if float(ac_hz) > 0.0:
        high_mode = _pick_mode_at_or_below(same_res, float(ac_hz))
    else:
        high_mode = max(
            same_res,
            key=lambda m: (
                round(m.refresh_hz, 2),
                1 if m.is_preferred else 0,
                1 if m.is_current else 0,
            ),
        )

    # 2. Low tier (low battery < 20%): closest mode to low_hz preferring >= 59 Hz
    safe_low_pool = [m for m in same_res if m.refresh_hz >= MIN_SAFE_REFRESH_HZ]
    low_pool = safe_low_pool if safe_low_pool else same_res
    low_mode = min(
        low_pool,
        key=lambda m: (
            abs(m.refresh_hz - effective_low_hz),
            m.refresh_hz,
            0 if m.is_current else 1,
        ),
    )

    # 3. Mid tier (battery >= 20%)
    if float(battery_hz) > 0.0:
        mid_mode = _pick_mode_at_or_below(same_res, float(battery_hz))
    else:
        # Deduplicate by rounded integer Hz, preferring preferred/current representative
        by_rounded_hz: dict[int, DisplayMode] = {}
        for mode in same_res:
            key_hz = int(round(mode.refresh_hz))
            prev = by_rounded_hz.get(key_hz)
            if prev is None or (
                (1 if mode.is_preferred else 0, 1 if mode.is_current else 0, mode.refresh_hz)
                > (1 if prev.is_preferred else 0, 1 if prev.is_current else 0, prev.refresh_hz)
            ):
                by_rounded_hz[key_hz] = mode

        desc_modes = [by_rounded_hz[k] for k in sorted(by_rounded_hz.keys(), reverse=True)]
        if (
            len(desc_modes) >= 3
            and low_mode is not None
            and desc_modes[1].refresh_hz > (low_mode.refresh_hz + 1.0)
        ):
            mid_mode = desc_modes[1]
        else:
            mid_mode = high_mode

    return {"high": high_mode, "mid": mid_mode, "low": low_mode}


def select_target_mode(
    info: PanelDisplayInfo | Sequence[DisplayMode],
    profile: str,
    ac_hz: float = 0.0,
    battery_hz: float = 0.0,
    low_hz: float = 60.0,
    *,
    low_refresh_hz: float | None = None,
) -> DisplayMode | None:
    """
    Select the target ``DisplayMode`` for ``profile`` ("high" | "mid" | "low"),
    or ``None`` if no switch is needed (already within 1 Hz of target or no modes).
    """
    same_res, current_mode = _extract_modes_and_reference(info)
    if not same_res:
        return None

    tiers = choose_refresh_modes(
        info,
        ac_hz=ac_hz,
        battery_hz=battery_hz,
        low_hz=low_hz,
        low_refresh_hz=low_refresh_hz,
    )
    normalized = (profile or "high").strip().lower()
    if normalized not in ("high", "mid", "low"):
        normalized = "high"

    target = tiers.get(normalized)
    if target is None:
        return None

    if current_mode is not None and abs(current_mode.refresh_hz - target.refresh_hz) < 1.0:
        return None
    return target


def _parse_win_mode_item(item: Any) -> tuple[int, int, float, int, bool] | None:
    """Normalize a raw Win32 DEVMODE tuple/dict/object into (w, h, hz, bpp, is_current)."""
    if isinstance(item, DisplayMode):
        return (item.width, item.height, float(item.refresh_hz), 32, item.is_current)
    if isinstance(item, (tuple, list)):
        if len(item) < 3:
            return None
        try:
            w = int(item[0])
            h = int(item[1])
            hz = float(item[2])
            bpp = int(item[3]) if len(item) >= 4 else 32
            is_cur = bool(item[4]) if len(item) >= 5 else False
            return (w, h, hz, bpp, is_cur)
        except (TypeError, ValueError):
            return None
    if isinstance(item, dict):
        try:
            w = int(item.get("width", item.get("dmPelsWidth", 0)))
            h = int(item.get("height", item.get("dmPelsHeight", 0)))
            hz = float(
                item.get(
                    "refresh_hz",
                    item.get("hz", item.get("dmDisplayFrequency", 0)),
                )
            )
            bpp = int(item.get("bits_per_pel", item.get("dmBitsPerPel", 32)))
            is_cur = bool(item.get("is_current", False))
            return (w, h, hz, bpp, is_cur)
        except (TypeError, ValueError):
            return None
    # ctypes DEVMODEW or duck-typed object
    try:
        w = int(getattr(item, "dmPelsWidth", getattr(item, "width", 0)))
        h = int(getattr(item, "dmPelsHeight", getattr(item, "height", 0)))
        hz = float(
            getattr(
                item,
                "dmDisplayFrequency",
                getattr(item, "refresh_hz", getattr(item, "hz", 0)),
            )
        )
        bpp = int(getattr(item, "dmBitsPerPel", getattr(item, "bits_per_pel", 32)))
        is_cur = bool(getattr(item, "is_current", False))
        return (w, h, hz, bpp, is_cur)
    except (TypeError, ValueError):
        return None


def win_devmodes_to_modes(
    raw_modes: Sequence[Any],
    current_mode: Any | None = None,
    output_name: str = r"\\.\DISPLAY1",
) -> PanelDisplayInfo:
    """
    Convert Win32 DEVMODE records (tuples, dicts, or DEVMODEW structs) into a
    deduplicated ``PanelDisplayInfo`` compatible with ``choose_refresh_modes`` and
    ``select_target_mode``. Pure Python — runnable on Linux and Windows.
    """
    cur_parsed = _parse_win_mode_item(current_mode) if current_mode is not None else None
    cur_bpp = cur_parsed[3] if cur_parsed is not None and cur_parsed[3] > 0 else None

    # Deduplicate by (width, height, rounded_hz), preferring matching color depth
    dedup: dict[tuple[int, int, int], tuple[int, int, float, int, bool]] = {}
    for raw in raw_modes:
        parsed = _parse_win_mode_item(raw)
        if parsed is None:
            continue
        w, h, hz, bpp, is_cur = parsed
        if w <= 0 or h <= 0 or hz <= 1.0:
            continue
        if cur_bpp is not None and bpp > 0 and bpp != cur_bpp:
            continue
        key = (w, h, int(round(hz)))
        prev = dedup.get(key)
        if prev is None or (bpp > prev[3]) or (is_cur and not prev[4]):
            dedup[key] = (w, h, hz, bpp, is_cur or (prev[4] if prev else False))

    # Ensure current_mode itself is represented even if omitted from raw_modes
    if cur_parsed is not None and cur_parsed[0] > 0 and cur_parsed[1] > 0 and cur_parsed[2] > 1.0:
        cw, ch, chz, cbpp, _ = cur_parsed
        ckey = (cw, ch, int(round(chz)))
        if ckey not in dedup:
            dedup[ckey] = (cw, ch, chz, cbpp, True)

    sorted_entries = sorted(
        dedup.values(),
        key=lambda t: (-t[0], -t[1], -t[2]),
    )

    # Determine highest Hz per resolution for is_preferred tagging
    max_hz_by_res: dict[tuple[int, int], float] = {}
    for w, h, hz, _bpp, _is_cur in sorted_entries:
        rk = (w, h)
        if rk not in max_hz_by_res or hz > max_hz_by_res[rk]:
            max_hz_by_res[rk] = hz

    modes: list[DisplayMode] = []
    matched_current: DisplayMode | None = None
    for w, h, hz, _bpp, item_cur in sorted_entries:
        is_cur = item_cur
        if cur_parsed is not None:
            is_cur = (
                w == cur_parsed[0]
                and h == cur_parsed[1]
                and abs(hz - cur_parsed[2]) < 1.0
            )
        rate_str = str(int(round(hz))) if abs(hz - round(hz)) < 0.05 else f"{hz:.2f}"
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
        if is_cur and matched_current is None:
            matched_current = dm

    return PanelDisplayInfo(
        output_name=output_name,
        current_mode=matched_current,
        modes=tuple(modes),
        backend="win32",
    )


# Win32 display API constants (safe to define on all platforms)
_WIN_CCHDEVICENAME = 32
_WIN_CCHFORMNAME = 32
_WIN_ENUM_CURRENT_SETTINGS = -1
_WIN_DM_PELSWIDTH = 0x00080000
_WIN_DM_PELSHEIGHT = 0x00100000
_WIN_DM_DISPLAYFREQUENCY = 0x00400000
_WIN_DISP_CHANGE_SUCCESSFUL = 0
_WIN_DISPLAY_DEVICE_ACTIVE = 0x00000001
_WIN_DISPLAY_DEVICE_PRIMARY = 0x00000004


class _WinDISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_uint32),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


class _WinDEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * _WIN_CCHDEVICENAME),
        ("dmSpecVersion", ctypes.c_uint16),
        ("dmDriverVersion", ctypes.c_uint16),
        ("dmSize", ctypes.c_uint16),
        ("dmDriverExtra", ctypes.c_uint16),
        ("dmFields", ctypes.c_uint32),
        ("dmPosition_x", ctypes.c_int32),
        ("dmPosition_y", ctypes.c_int32),
        ("dmDisplayOrientation", ctypes.c_uint32),
        ("dmDisplayFixedOutput", ctypes.c_uint32),
        ("dmColor", ctypes.c_int16),
        ("dmDuplex", ctypes.c_int16),
        ("dmYResolution", ctypes.c_int16),
        ("dmTTOption", ctypes.c_int16),
        ("dmCollate", ctypes.c_int16),
        ("dmFormName", ctypes.c_wchar * _WIN_CCHFORMNAME),
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


class WindowsDisplayRefreshBackend:
    """
    Native Win32 display refresh backend using ``user32.dll`` via ``ctypes``.

    All Win32 calls are strictly guarded behind ``sys.platform == "win32"`` so
    this module remains 100% importable and testable on Linux.
    """

    def __init__(self, device_name: str | None = None) -> None:
        self._preferred_device = device_name

    def _get_user32(self) -> Any | None:
        if sys.platform != "win32":
            return None
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        return getattr(windll, "user32", None)

    def _find_primary_device(self, user32: Any) -> str:
        if self._preferred_device:
            return self._preferred_device
        first_active: str | None = None
        idx = 0
        while idx < 32:
            dev = _WinDISPLAY_DEVICEW()
            dev.cb = ctypes.sizeof(_WinDISPLAY_DEVICEW)
            ok = user32.EnumDisplayDevicesW(None, idx, ctypes.byref(dev), 0)
            if not ok:
                break
            flags = int(dev.StateFlags)
            if flags & _WIN_DISPLAY_DEVICE_PRIMARY:
                return str(dev.DeviceName)
            if first_active is None and (flags & _WIN_DISPLAY_DEVICE_ACTIVE):
                first_active = str(dev.DeviceName)
            idx += 1
        return first_active or r"\\.\DISPLAY1"

    def probe(self) -> PanelDisplayInfo | None:
        user32 = self._get_user32()
        if user32 is None:
            return None

        dev_name = self._find_primary_device(user32)
        cur_dm = _WinDEVMODEW()
        cur_dm.dmSize = ctypes.sizeof(_WinDEVMODEW)
        if not user32.EnumDisplaySettingsW(
            dev_name, _WIN_ENUM_CURRENT_SETTINGS, ctypes.byref(cur_dm)
        ):
            return None

        current_tuple = (
            int(cur_dm.dmPelsWidth),
            int(cur_dm.dmPelsHeight),
            float(cur_dm.dmDisplayFrequency),
            int(cur_dm.dmBitsPerPel),
            True,
        )

        raw_modes: list[tuple[int, int, float, int]] = []
        idx = 0
        while idx < 4096:
            dm = _WinDEVMODEW()
            dm.dmSize = ctypes.sizeof(_WinDEVMODEW)
            if not user32.EnumDisplaySettingsW(dev_name, idx, ctypes.byref(dm)):
                break
            raw_modes.append(
                (
                    int(dm.dmPelsWidth),
                    int(dm.dmPelsHeight),
                    float(dm.dmDisplayFrequency),
                    int(dm.dmBitsPerPel),
                )
            )
            idx += 1

        info = win_devmodes_to_modes(raw_modes, current_mode=current_tuple, output_name=dev_name)
        return info if info.modes else None

    def apply_mode(self, info: PanelDisplayInfo, target: DisplayMode) -> bool:
        user32 = self._get_user32()
        if user32 is None:
            return False

        dev_name = info.output_name or self._find_primary_device(user32)
        dm = _WinDEVMODEW()
        dm.dmSize = ctypes.sizeof(_WinDEVMODEW)
        if not user32.EnumDisplaySettingsW(
            dev_name, _WIN_ENUM_CURRENT_SETTINGS, ctypes.byref(dm)
        ):
            return False

        dm.dmPelsWidth = int(target.width)
        dm.dmPelsHeight = int(target.height)
        dm.dmDisplayFrequency = int(round(target.refresh_hz))
        dm.dmFields = _WIN_DM_PELSWIDTH | _WIN_DM_PELSHEIGHT | _WIN_DM_DISPLAYFREQUENCY

        res = int(user32.ChangeDisplaySettingsExW(dev_name, ctypes.byref(dm), None, 0, None))
        return res == _WIN_DISP_CHANGE_SUCCESSFUL


def build_switch_command(info: PanelDisplayInfo, target: DisplayMode) -> list[str] | None:
    """Build the backend CLI command list to switch ``info.output_name`` to ``target``."""
    backend = info.backend
    if backend == "kscreen-doctor":
        return ["kscreen-doctor", f"output.{info.output_name}.mode.{target.mode_id}"]
    if backend == "wlr-randr":
        rate = target.rate_str or f"{target.refresh_hz:.2f}"
        return [
            "wlr-randr",
            "--output",
            info.output_name,
            "--mode",
            f"{target.width}x{target.height}@{rate}Hz",
        ]
    if backend == "xrandr":
        rate = target.rate_str or f"{target.refresh_hz:.2f}"
        return [
            "xrandr",
            "--output",
            info.output_name,
            "--mode",
            f"{target.width}x{target.height}",
            "--rate",
            rate,
        ]
    if backend == "gnome-randr":
        return ["gnome-randr", "modify", info.output_name, "--mode", target.mode_id]
    if backend == "gdctl":
        return ["gdctl", "set", "-L", "-M", info.output_name, "-m", target.mode_id]
    return None


def _default_runner(cmd: list[str], timeout: float = 4.0) -> tuple[int, str, str]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "timeout": timeout,
        "check": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(cmd, **kwargs)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


class DisplayRefreshController:
    """
    Manages automatic 3-tier display refresh rate switching ("high" | "mid" | "low")
    on the internal laptop panel with anti-thrashing guards and clean restoration on exit.
    """

    def __init__(
        self,
        runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
        probe_interval_sec: float = 15.0,
        min_switch_interval_sec: float = 5.0,
        backend: str | None = None,
        win_backend: WindowsDisplayRefreshBackend | Any | None = None,
    ) -> None:
        self._runner = runner if runner is not None else _default_runner
        self._probe_interval_sec = float(probe_interval_sec)
        self._min_switch_interval_sec = float(min_switch_interval_sec)
        self._win_backend = (
            win_backend
            if win_backend is not None
            else (WindowsDisplayRefreshBackend() if sys.platform == "win32" else None)
        )

        self.backend: str = backend if backend else ("win32" if self._win_backend is not None else "none")
        self.last_info: PanelDisplayInfo | None = None
        self.output_name: str | None = None
        self.current_hz: float | None = None
        self.native_hz: float | None = None
        self.active_profile: str = "high"

        self._last_probe_ts: float = 0.0
        self._last_switch_ts: float = 0.0
        self._last_desired_profile: str | None = None
        self._we_lowered_refresh: bool = False
        self._gdctl_single_monitor: bool = False

        self._detect_and_probe(now=time.time())

    def _candidate_backends(self) -> list[str]:
        if sys.platform == "win32" or self._win_backend is not None:
            return ["win32"]

        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        session = os.environ.get("DESKTOP_SESSION", "").lower()
        is_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        has_x11 = bool(os.environ.get("DISPLAY"))

        ordered: list[str] = []
        if "kde" in desktop or "plasma" in desktop or "plasma" in session:
            ordered.append("kscreen-doctor")
        if is_wayland:
            for name in ("kscreen-doctor", "wlr-randr", "gnome-randr", "gdctl"):
                if name not in ordered:
                    ordered.append(name)
        if has_x11:
            for name in ("kscreen-doctor", "xrandr"):
                if name not in ordered:
                    ordered.append(name)
        for name in ("kscreen-doctor", "wlr-randr", "gnome-randr", "gdctl", "xrandr"):
            if name not in ordered:
                ordered.append(name)

        return [name for name in ordered if shutil.which(name)]

    def _probe_backend(self, backend: str) -> PanelDisplayInfo | None:
        if backend == "win32":
            if self._win_backend is None:
                self._win_backend = WindowsDisplayRefreshBackend()
            return self._win_backend.probe()
        if backend == "kscreen-doctor":
            rc, out, _ = self._runner(["kscreen-doctor", "-o"], 4.0)
            return parse_kscreen_output(out) if rc == 0 else None
        if backend == "wlr-randr":
            rc, out, _ = self._runner(["wlr-randr"], 4.0)
            return parse_wlr_randr_output(out) if rc == 0 else None
        if backend == "xrandr":
            rc, out, _ = self._runner(["xrandr", "--query"], 4.0)
            return parse_xrandr_output(out) if rc == 0 else None
        if backend == "gnome-randr":
            rc, out, _ = self._runner(["gnome-randr", "query"], 4.0)
            return parse_gnome_output(out, backend="gnome-randr") if rc == 0 else None
        if backend == "gdctl":
            rc, out, _ = self._runner(["gdctl", "show"], 4.0)
            if rc != 0:
                return None
            # Only allow gdctl set when a single monitor is active so multi-monitor layouts stay intact
            clean = strip_ansi(out)
            monitor_count = len(re.findall(r"\bMonitor\s+\S+", clean))
            self._gdctl_single_monitor = monitor_count <= 1
            return parse_gnome_output(clean, backend="gdctl")
        return None

    def _update_cached_info(self, info: PanelDisplayInfo | None) -> None:
        self.last_info = info
        if info is None:
            return
        self.backend = info.backend
        self.output_name = info.output_name
        if info.current_mode is not None:
            self.current_hz = round(info.current_mode.refresh_hz, 2)
        tiers = choose_refresh_modes(info)
        high_mode = tiers.get("high")
        if high_mode is not None:
            self.native_hz = round(high_mode.refresh_hz, 2)

    def _detect_and_probe(self, now: float) -> PanelDisplayInfo | None:
        self._last_probe_ts = now
        if self.backend != "none":
            info = self._probe_backend(self.backend)
            if info is not None:
                self._update_cached_info(info)
                return info

        for candidate in self._candidate_backends():
            info = self._probe_backend(candidate)
            if info is not None:
                self._update_cached_info(info)
                return info

        if self.last_info is None:
            self.backend = "none"
        return self.last_info

    def probe(self, force: bool = False, now: float | None = None) -> PanelDisplayInfo | None:
        """Probe current display state (throttled unless ``force=True``)."""
        ts = time.time() if now is None else float(now)
        if force or self.last_info is None or (ts - self._last_probe_ts) >= self._probe_interval_sec:
            return self._detect_and_probe(ts)
        return self.last_info

    def _execute_switch(self, info: PanelDisplayInfo, target: DisplayMode) -> bool:
        if info.backend == "win32":
            if self._win_backend is None:
                return False
            return bool(self._win_backend.apply_mode(info, target))
        cmd = build_switch_command(info, target)
        if not cmd:
            return False
        rc, _, _ = self._runner(cmd, 4.0)
        return rc == 0

    def apply_profile(
        self,
        desired_profile: str,
        ac_hz: float = 0.0,
        battery_hz: float = 0.0,
        low_hz: float = 60.0,
        *,
        now: float | None = None,
        enabled: bool = True,
        low_refresh_hz: float | None = None,
    ) -> dict[str, Any]:
        """
        Apply desired 3-tier refresh rate profile ("high" | "mid" | "low") to the
        internal panel.

        Never thrashes, never switches if already at target Hz, and never switches
        if the display only advertises a single refresh rate at its current resolution.
        """
        ts = time.time() if now is None else float(now)
        effective_low_hz = float(low_refresh_hz) if low_refresh_hz is not None else float(low_hz)

        norm_prof = str(desired_profile or "high").strip().lower()
        if not enabled or norm_prof not in ("high", "mid", "low"):
            profile = "high"
        else:
            profile = norm_prof

        profile_changed = profile != self._last_desired_profile
        info = self.probe(force=profile_changed, now=ts)
        self._last_desired_profile = profile
        self.active_profile = profile

        if info is None:
            return self.get_telemetry()

        # Always compute panel's true native max Hz at active resolution
        native_tiers = choose_refresh_modes(info)
        native_high = native_tiers.get("high")
        if native_high is not None:
            self.native_hz = round(native_high.refresh_hz, 2)

        tiers = choose_refresh_modes(
            info,
            ac_hz=ac_hz,
            battery_hz=battery_hz,
            low_hz=effective_low_hz,
        )
        high_m = tiers.get("high")
        mid_m = tiers.get("mid")
        low_m = tiers.get("low")

        # If only a single refresh rate exists across all tiers, never switch
        distinct_hz = {
            int(round(m.refresh_hz))
            for m in (high_m, mid_m, low_m)
            if m is not None
        }
        if len(distinct_hz) <= 1:
            return self.get_telemetry()

        # Avoid modifying multi-monitor GNOME layouts with gdctl
        if info.backend == "gdctl" and not self._gdctl_single_monitor:
            return self.get_telemetry()

        target = select_target_mode(
            info,
            profile=profile,
            ac_hz=ac_hz,
            battery_hz=battery_hz,
            low_hz=effective_low_hz,
        )
        if target is None:
            if profile == "high":
                self._we_lowered_refresh = False
            return self.get_telemetry()

        # If policy is disabled, only restore if we previously lowered refresh ourselves.
        if not enabled and not self._we_lowered_refresh:
            return self.get_telemetry()

        # When restoring "high" with auto ac_hz, restore if we lowered it OR if the panel
        # is sitting at a managed step-down tier (mid/low, e.g. after unclean shutdown).
        if profile == "high" and not self._we_lowered_refresh and float(ac_hz) <= 0.0:
            cur_hz = info.current_mode.refresh_hz if info.current_mode is not None else None
            cur_is_managed_tier = cur_hz is not None and (
                (low_m is not None and abs(cur_hz - low_m.refresh_hz) < 1.5)
                or (mid_m is not None and abs(cur_hz - mid_m.refresh_hz) < 1.5)
            )
            if not cur_is_managed_tier:
                return self.get_telemetry()

        if (ts - self._last_switch_ts) < self._min_switch_interval_sec:
            return self.get_telemetry()

        self._last_switch_ts = ts
        if self._execute_switch(info, target):
            self.current_hz = round(target.refresh_hz, 2)
            self._we_lowered_refresh = profile in ("mid", "low")
            # Update cached current_mode optimistically so consecutive ticks don't re-switch
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
                backend=info.backend,
            )
        return self.get_telemetry()

    def restore_native(self) -> bool:
        """Restore native max refresh rate cleanly (called on tray shutdown)."""
        if not self._we_lowered_refresh:
            return False
        info = self.probe(force=True)
        if info is None:
            return False
        if info.backend == "gdctl" and not self._gdctl_single_monitor:
            return False
        target = select_target_mode(info, profile="high")
        if target is None:
            self._we_lowered_refresh = False
            self.active_profile = "high"
            return True
        if self._execute_switch(info, target):
            self.current_hz = round(target.refresh_hz, 2)
            self._we_lowered_refresh = False
            self.active_profile = "high"
            return True
        return False

    def get_telemetry(self) -> dict[str, Any]:
        """Return refresh rate telemetry fields for smart-power-display.json."""
        return {
            "refresh_output": self.output_name,
            "refresh_hz": self.current_hz,
            "native_refresh_hz": self.native_hz,
            "refresh_backend": self.backend,
            "refresh_profile": self.active_profile,
        }
