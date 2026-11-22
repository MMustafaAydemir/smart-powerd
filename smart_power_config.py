#!/usr/bin/env python3
"""
Shared configuration loader and schema defaults for smart-powerd.

Loads /etc/smart-powerd/config.toml using Python's stdlib tomllib and
deep-merges user overrides on top of safe, hardware-agnostic defaults.
Never raises on missing, partial, or malformed configuration files.
"""

from __future__ import annotations

import copy
import sys
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "/etc/smart-powerd/config.toml"

VALID_CONTROL_COMMANDS: tuple[str, ...] = (
    "on",
    "off",
    "toggle",
    "reload",
    "brightness-on",
    "brightness-off",
    "brightness-toggle",
    "webcam-als-on",
    "webcam-als-off",
    "webcam-als-toggle",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "general": {
        "poll_interval_sec": 2.5,
        "cooldown_sec": 12.0,
        "wifi_recheck_ticks": 6,
    },
    "cpu": {
        "light_epp_ac": "balance_power",
        "light_epp_battery": "balance_power",
        "download_epp": "balance_performance",
        "heavy_epp": "performance",
        "uncap_boost_in_light": True,
    },
    "battery_tricks": {
        "aspm_light": "powersave",
        "audio_power_save_light": 5,
        "dirty_writeback_battery": 1500,
        "disable_nmi_watchdog": True,
        "sata_alpm_light": "med_power_with_dipm",
        "amd_abm_battery_level": 2,
        "usb_autosuspend_non_hid": True,
        "pci_runtime_pm_unused": True,
        "kbd_backlight_dim_battery": True,
        "refresh_policy_enabled": True,
        "ac_refresh_hz": 0,
        "battery_refresh_hz": 0,
        "low_battery_refresh_pct": 20,
        "low_refresh_hz": 60,
    },
    "brightness": {
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
        "schedule": [
            [0, 22],
            [6, 25],
            [8, 55],
            [12, 78],
            [17, 65],
            [20, 42],
            [22, 28],
        ],
    },
    "workloads": {
        "extra_heavy_processes": [],
        "extra_download_processes": [],
        "rx_threshold_mib_s": 1.5,
        "rx_immediate_mib_s": 3.0,
        "cpu_heavy_pct": 25.0,
        "single_core_heavy_pct": 85.0,
        "heavy_nice": -5,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into a deep copy of base."""
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result

    for key, val in override.items():
        if key in result and isinstance(result[key], dict):
            if isinstance(val, dict):
                result[key] = deep_merge(result[key], val)
            # Ignore non-dict override for a section dict to prevent schema breakage
        else:
            result[key] = copy.deepcopy(val)
    return result


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """
    Load TOML configuration from ``path`` and deep-merge over DEFAULT_CONFIG.

    Returns a fresh dict with all default sections and keys populated even if
    the file is missing, partially specified, unreadable, or malformed.
    """
    defaults = copy.deepcopy(DEFAULT_CONFIG)
    if not path:
        return defaults

    cfg_path = Path(path)
    if not cfg_path.is_file():
        return defaults

    try:
        with cfg_path.open("rb") as fh:
            parsed = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(
            f"smart-power: failed to load config '{cfg_path}' ({exc}); using defaults.",
            file=sys.stderr,
        )
        return defaults

    if not isinstance(parsed, dict):
        return defaults

    merged = deep_merge(defaults, parsed)

    # Backward compatibility: honor legacy `low_refresh_on_battery_light` when
    # an existing v2.0 config.toml sets it without setting `refresh_policy_enabled`.
    raw_bt = parsed.get("battery_tricks")
    if (
        isinstance(raw_bt, dict)
        and "low_refresh_on_battery_light" in raw_bt
        and "refresh_policy_enabled" not in raw_bt
    ):
        merged["battery_tricks"]["refresh_policy_enabled"] = bool(
            raw_bt["low_refresh_on_battery_light"]
        )

    return merged


def effective_brightness_config(
    config: dict[str, Any], state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """
    Return the effective brightness configuration with runtime state.json
    boolean overrides (`brightness_enabled`, `webcam_als_enabled`) applied.
    """
    base = copy.deepcopy(DEFAULT_CONFIG["brightness"])
    cfg_section = config.get("brightness") if isinstance(config, dict) else None
    if isinstance(cfg_section, dict):
        base = deep_merge(base, cfg_section)

    if isinstance(state, dict):
        if isinstance(state.get("brightness_enabled"), bool):
            base["enabled"] = state["brightness_enabled"]
        if isinstance(state.get("webcam_als_enabled"), bool):
            base["webcam_als_enabled"] = state["webcam_als_enabled"]

    return base
