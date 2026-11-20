#!/usr/bin/env python3
"""
Standalone multi-DE laptop display refresh rate switching module.

Runs in the user's graphical session (Wayland or X11) without root privileges.
Supports:
  - KDE Plasma 6 Wayland/X11 via ``kscreen-doctor``
  - wlroots compositors (Hyprland, Sway, River, etc.) via ``wlr-randr``
  - GNOME via ``gnome-randr`` or ``gdctl``
  - X11 via ``xrandr``

Switches the built-in laptop panel (eDP/LVDS/DSI) to a low refresh rate (~60 Hz)
only on Battery + Light Mode, and restores native max refresh rate on AC, Heavy,
Download, OFF, and tray exit.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_LIB_DIR = Path("/usr/local/lib/smart-power")
_SCRIPT_DIR = Path(__file__).resolve().parent
for _path_entry in (str(_SCRIPT_DIR), str(_LIB_DIR)):
    if _path_entry not in sys.path:
        sys.path.insert(0, _path_entry)

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


def choose_refresh_modes(
    info: PanelDisplayInfo,
    low_refresh_hz: float = 60.0,
) -> tuple[DisplayMode | None, DisplayMode | None]:
    """
    Determine ``(low_mode, high_mode)`` at the panel's current resolution.

    - Never changes resolution.
    - ``high_mode`` is the highest native refresh rate at the current resolution.
    - ``low_mode`` is the lowest refresh rate ``>= 59 Hz`` (respecting
      ``low_refresh_hz``) strictly below ``high_mode``, or ``None`` if only one
      refresh rate exists at this resolution.
    """
    if not info.modes:
        return None, None

    ref_mode = info.current_mode
    if ref_mode is None:
        ref_mode = next((m for m in info.modes if m.is_preferred), info.modes[0])

    same_res = [
        m for m in info.modes if m.width == ref_mode.width and m.height == ref_mode.height
    ]
    if not same_res:
        return None, None

    # Highest native refresh rate at current resolution (preferring preferred/current on tie)
    high_mode = max(
        same_res,
        key=lambda m: (round(m.refresh_hz, 2), 1 if m.is_preferred else 0, 1 if m.is_current else 0),
    )

    # Lower refresh modes must be strictly lower than high_mode and >= 59 Hz
    lower_candidates = [
        m
        for m in same_res
        if m.refresh_hz >= MIN_SAFE_REFRESH_HZ and m.refresh_hz < (high_mode.refresh_hz - 1.0)
    ]
    if not lower_candidates:
        return None, high_mode

    min_desired = max(MIN_SAFE_REFRESH_HZ, float(low_refresh_hz) - 1.5)
    matching_floor = [m for m in lower_candidates if m.refresh_hz >= min_desired]
    pool = matching_floor if matching_floor else lower_candidates
    low_mode = min(pool, key=lambda m: (m.refresh_hz, 0 if m.is_current else 1))
    return low_mode, high_mode


def select_target_mode(
    info: PanelDisplayInfo,
    profile: str,
    low_refresh_hz: float = 60.0,
) -> DisplayMode | None:
    """
    Select the target ``DisplayMode`` for ``profile`` ("low" | "high"), or None
    if no lower mode exists or the panel is already at the desired refresh rate.
    """
    low_mode, high_mode = choose_refresh_modes(info, low_refresh_hz=low_refresh_hz)
    if high_mode is None:
        return None

    normalized = (profile or "high").strip().lower()
    target = low_mode if normalized == "low" else high_mode
    if target is None:
        return None

    if info.current_mode is not None and abs(info.current_mode.refresh_hz - target.refresh_hz) < 1.0:
        return None
    return target


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
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


class DisplayRefreshController:
    """
    Manages automatic display refresh rate switching on the internal laptop panel
    with anti-thrashing guards and clean restoration on exit.
    """

    def __init__(
        self,
        runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
        probe_interval_sec: float = 15.0,
        min_switch_interval_sec: float = 5.0,
        backend: str | None = None,
    ) -> None:
        self._runner = runner if runner is not None else _default_runner
        self._probe_interval_sec = float(probe_interval_sec)
        self._min_switch_interval_sec = float(min_switch_interval_sec)

        self.backend: str = backend if backend else "none"
        self.last_info: PanelDisplayInfo | None = None
        self.output_name: str | None = None
        self.current_hz: float | None = None
        self.native_hz: float | None = None

        self._last_probe_ts: float = 0.0
        self._last_switch_ts: float = 0.0
        self._last_desired_profile: str | None = None
        self._we_lowered_refresh: bool = False
        self._gdctl_single_monitor: bool = False

        self._detect_and_probe(now=time.time())

    def _candidate_backends(self) -> list[str]:
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
        _, high_mode = choose_refresh_modes(info)
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

    def apply_profile(
        self,
        desired_profile: str,
        low_refresh_hz: float = 60.0,
        now: float | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """
        Apply desired refresh rate profile ("low" | "high") to the internal panel.

        Never thrashes, never switches if already at target Hz, and never switches
        if the display only advertises a single refresh rate at its current resolution.
        """
        ts = time.time() if now is None else float(now)
        profile = "low" if (enabled and str(desired_profile).strip().lower() == "low") else "high"
        profile_changed = profile != self._last_desired_profile

        info = self.probe(force=profile_changed, now=ts)
        self._last_desired_profile = profile

        if info is None:
            return self.get_telemetry()

        # Recompute native_hz with current low_refresh_hz context
        low_mode, high_mode = choose_refresh_modes(info, low_refresh_hz=low_refresh_hz)
        if high_mode is not None:
            self.native_hz = round(high_mode.refresh_hz, 2)

        # If only one refresh rate exists at current resolution, never attempt to switch
        if low_mode is None:
            return self.get_telemetry()

        # Avoid modifying multi-monitor GNOME layouts with gdctl
        if info.backend == "gdctl" and not self._gdctl_single_monitor:
            return self.get_telemetry()

        target = select_target_mode(info, profile=profile, low_refresh_hz=low_refresh_hz)
        if target is None:
            if profile == "high":
                self._we_lowered_refresh = False
            return self.get_telemetry()

        # If disabled, only restore if we previously lowered refresh ourselves.
        if not enabled and not self._we_lowered_refresh:
            return self.get_telemetry()

        # When restoring "high", restore if we lowered it OR if panel is sitting at low_mode
        # (e.g. recovering after an unclean shutdown); preserve custom mid-tier Hz (e.g. 240Hz).
        if profile == "high" and not self._we_lowered_refresh:
            cur_is_low = (
                info.current_mode is not None
                and abs(info.current_mode.refresh_hz - low_mode.refresh_hz) < 1.5
            )
            if not cur_is_low:
                return self.get_telemetry()

        if (ts - self._last_switch_ts) < self._min_switch_interval_sec:
            return self.get_telemetry()

        cmd = build_switch_command(info, target)
        if not cmd:
            return self.get_telemetry()

        self._last_switch_ts = ts
        rc, _, _ = self._runner(cmd, 4.0)
        if rc == 0:
            self.current_hz = round(target.refresh_hz, 2)
            self._we_lowered_refresh = profile == "low"
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
            return True
        cmd = build_switch_command(info, target)
        if not cmd:
            return False
        rc, _, _ = self._runner(cmd, 4.0)
        if rc == 0:
            self.current_hz = round(target.refresh_hz, 2)
            self._we_lowered_refresh = False
            return True
        return False

    def get_telemetry(self) -> dict[str, Any]:
        """Return refresh rate telemetry fields for smart-power-display.json."""
        return {
            "refresh_output": self.output_name,
            "refresh_hz": self.current_hz,
            "native_refresh_hz": self.native_hz,
            "refresh_backend": self.backend,
        }
