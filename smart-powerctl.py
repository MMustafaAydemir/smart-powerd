#!/usr/bin/env python3
"""
User CLI wrapper for smart-powerd and smart-power-tray.

Supports:
  smart-powerctl status [--json]
  smart-powerctl on|off|toggle|reload
  smart-powerctl brightness on|off|toggle
"""

from __future__ import annotations

import json
import os
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

try:
    from smart_power_config import VALID_CONTROL_COMMANDS
except ImportError:
    VALID_CONTROL_COMMANDS = (
        "on",
        "off",
        "toggle",
        "reload",
        "brightness-on",
        "brightness-off",
        "brightness-toggle",
    )

STATUS_FILE = Path("/run/smart-powerd/status.json")
HELPER_BIN = "/usr/local/libexec/smart-powerd-helper"
VALID_HELPER_SET = frozenset(VALID_CONTROL_COMMANDS)


def get_display_status_path() -> Path:
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg:
        return Path(xdg) / "smart-power-display.json"
    return Path(f"/tmp/smart-power-display-{os.getuid()}.json")


def _read_display_telemetry() -> dict[str, Any] | None:
    path = get_display_status_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _format_brightness_line(st: dict[str, Any], disp: dict[str, Any] | None) -> str:
    bcfg = st.get("brightness_config") if isinstance(st.get("brightness_config"), dict) else {}
    auto_on = bool(bcfg.get("enabled", True))

    parts: list[str] = []
    if disp and disp.get("brightness_pct") is not None:
        pct = disp.get("brightness_pct")
        target = disp.get("brightness_target_pct")
        if target is not None and target != pct:
            parts.append(f"{pct}% (target {target}%)")
        else:
            parts.append(f"{pct}%")
        source = disp.get("brightness_source") or "Schedule"
        parts.append(f"source={source}")
        pause_until = disp.get("manual_override_until") or 0
        if isinstance(pause_until, (int, float)) and pause_until > time.time():
            mins = max(1, int((pause_until - time.time()) / 60))
            parts.append(f"manual-pause ~{mins}m")
    else:
        parts.append("tray inactive")

    parts.append(f"auto={'ON' if auto_on else 'OFF'}")
    return " | ".join(parts)


def _format_refresh_line(st: dict[str, Any], disp: dict[str, Any] | None) -> str:
    desired = str(st.get("desired_refresh_profile", "high"))
    profile_labels = {
        "high": "AC / High",
        "mid": "Battery / Mid",
        "low": "Low Battery",
    }
    tier_label = profile_labels.get(desired, desired)
    low_pct = st.get("low_battery_refresh_pct", 20)
    low_hz = st.get("low_refresh_hz", 60)

    if disp and disp.get("refresh_hz") is not None:
        cur_hz = disp.get("refresh_hz")
        nat_hz = disp.get("native_refresh_hz")
        out_name = disp.get("refresh_output") or "panel"
        backend = disp.get("refresh_backend") or "unknown"
        hz_str = f"{cur_hz:.0f} Hz" if isinstance(cur_hz, (int, float)) else f"{cur_hz} Hz"
        if isinstance(nat_hz, (int, float)) and nat_hz > 0:
            hz_str += f" (native {nat_hz:.0f} Hz)"
        return (
            f"{hz_str} [{desired}: {tier_label}] on {out_name} ({backend})"
            f" | low<{low_pct}% -> {low_hz} Hz"
        )
    return f"profile={desired} ({tier_label}) | low<{low_pct}% -> {low_hz} Hz | tray inactive"


def print_status(as_json: bool) -> int:
    if not STATUS_FILE.is_file():
        if as_json:
            print(json.dumps({"ok": False, "error": "smart-powerd status not found"}))
        else:
            print("smart-powerd is not running (status file missing).", file=sys.stderr)
        return 1

    raw = STATUS_FILE.read_text(encoding="utf-8", errors="ignore")
    try:
        st = json.loads(raw)
    except ValueError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": f"Failed to parse status.json: {exc}"}))
        else:
            print(f"Failed to parse status.json: {exc}", file=sys.stderr)
        return 1

    disp = _read_display_telemetry()
    if as_json:
        out = dict(st)
        out["display"] = disp
        print(json.dumps(out, indent=2))
        return 0

    enabled = "ON" if st.get("enabled") else "OFF"
    mode = str(st.get("mode", "unknown")).upper()
    print(f"Smart Optimize : {enabled} ({mode})")
    print(f"Reason         : {st.get('reason', 'unknown')}")

    cpu_driver = st.get("cpu_driver", "unknown")
    ppd = st.get("ppd_profile", "unknown")
    pprof = st.get("platform_profile")
    ppd_str = f"PPD={ppd}"
    if pprof and pprof != "none":
        ppd_str += f" (platform={pprof})"
    print(
        f"CPU / PPD      : driver={cpu_driver} | EPP={st.get('epp')} | {ppd_str} | CPU={st.get('cpu_pct')}%"
    )
    print(
        f"Wi-Fi / RX     : {st.get('wifi_iface')} power_save={st.get('wifi_power_save')} | RX={st.get('rx_mbps')} MiB/s"
    )

    tricks_parts = [
        f"ASPM={st.get('aspm')}",
        f"audio_ps={st.get('audio_power_save')}",
    ]
    if st.get("dirty_writeback_cs") is not None:
        tricks_parts.append(f"dirty_wb={st.get('dirty_writeback_cs')}cs")
    if st.get("sata_alpm") and st.get("sata_alpm") != "none":
        tricks_parts.append(f"SATA_ALPM={st.get('sata_alpm')}")
    if st.get("amd_abm_level") is not None:
        tricks_parts.append(f"AMD_ABM={st.get('amd_abm_level')}")
    print(f"Power Tricks   : {' | '.join(tricks_parts)}")

    print(f"Brightness     : {_format_brightness_line(st, disp)}")
    print(f"Refresh Rate   : {_format_refresh_line(st, disp)}")

    print(
        f"dGPU           : {st.get('dgpu_pci')} status={st.get('dgpu_status')} ({st.get('dgpu_power_state')})"
    )

    bat_line = (
        f"{st.get('battery_watts')} W {st.get('battery_status')} "
        f"({st.get('battery_pct')}%) | AC={st.get('ac_online')}"
    )
    thresholds = st.get("charge_thresholds")
    if isinstance(thresholds, dict) and thresholds:
        th_str = ", ".join(f"{k}={v}" for k, v in sorted(thresholds.items()))
        bat_line += f" | thresholds: {th_str}"
    print(f"Battery        : {bat_line}")
    print(f"Sleep Inhibit  : {st.get('inhibit_active')}")
    return 0


def run_control(cmd: str) -> int:
    if cmd not in VALID_HELPER_SET:
        print(f"Invalid control command: {cmd}", file=sys.stderr)
        return 2
    argv = [HELPER_BIN, cmd] if os.geteuid() == 0 else ["sudo", "-n", HELPER_BIN, cmd]
    res = subprocess.run(argv, check=False)
    return res.returncode


def resolve_control_command(args: list[str]) -> str | None:
    """Map CLI arguments to a validated helper command string, or None."""
    if len(args) == 1 and args[0] in VALID_HELPER_SET:
        return args[0]
    if len(args) == 2 and args[0] == "brightness" and args[1] in {"on", "off", "toggle"}:
        return f"brightness-{args[1]}"
    return None


def print_usage() -> None:
    print(
        "Usage:\n"
        "  smart-powerctl status [--json]\n"
        "  smart-powerctl {on|off|toggle|reload}\n"
        "  smart-powerctl brightness {on|off|toggle}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] == "status":
        extra = args[1:]
        if extra and extra != ["--json"]:
            print_usage()
            return 2
        return print_status(as_json=("--json" in extra))

    cmd = resolve_control_command(args)
    if cmd is not None:
        return run_control(cmd)

    print_usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
