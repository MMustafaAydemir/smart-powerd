#!/usr/bin/env python3
"""
Comprehensive unit and integration-contract test suite for smart-power.

Run with:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import adaptive_brightness as ab  # noqa: E402
import display_refresh as dr  # noqa: E402
import smart_power_config as spc  # noqa: E402


def _load_module_from_path(mod_name: str, file_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


smart_powerd = _load_module_from_path("smart_powerd_mod", REPO_ROOT / "smart-powerd.py")
smart_power_helper = _load_module_from_path(
    "smart_power_helper_mod", REPO_ROOT / "smart-powerd-helper.py"
)
smart_powerctl = _load_module_from_path(
    "smart_powerctl_mod", REPO_ROOT / "smart-powerctl.py"
)


class FakeBacklightBackend:
    """In-memory backlight backend for deterministic AdaptiveBrightnessController tests."""

    def __init__(self, initial_pct: int = 50) -> None:
        self.pct: int | None = initial_pct
        self.set_calls: list[int] = []
        self.fail_writes: bool = False

    def get_pct(self) -> int | None:
        return self.pct

    def set_pct(self, pct: int) -> bool:
        if self.fail_writes:
            return False
        self.pct = max(1, min(100, int(round(pct))))
        self.set_calls.append(self.pct)
        return True


class FakeALSReader:
    """In-memory hardware ALS reader for deterministic tests."""

    def __init__(self, lux: float | None = None) -> None:
        self.lux = lux
        self.cleaned_up = False

    def read_lux(self, daemon_als_lux: float | None = None) -> float | None:
        if daemon_als_lux is not None:
            return float(daemon_als_lux)
        return self.lux

    def cleanup(self) -> None:
        self.cleaned_up = True


class TestSmartPowerConfig(unittest.TestCase):
    """Tests for smart_power_config.py and config.example.toml schema parity."""

    def test_default_load_missing_file_returns_isolated_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.toml"
            cfg = spc.load_config(missing)
            self.assertEqual(cfg, spc.DEFAULT_CONFIG)
            self.assertTrue(cfg["brightness"]["webcam_als_enabled"])
            self.assertEqual(cfg["brightness"]["webcam_interval_sec"], 1800)
            # Mutating returned dict must not pollute DEFAULT_CONFIG
            cfg["general"]["cooldown_sec"] = 999.0
            cfg["workloads"]["extra_heavy_processes"].append("custom-sim")
            self.assertEqual(spc.DEFAULT_CONFIG["general"]["cooldown_sec"], 12.0)
            self.assertEqual(spc.DEFAULT_CONFIG["workloads"]["extra_heavy_processes"], [])

    def test_partial_toml_override_deep_merge(self) -> None:
        partial_toml = """
[general]
cooldown_sec = 20.0

[cpu]
light_epp_battery = "power"

[brightness]
min_pct = 18
webcam_als_enabled = false

[workloads]
extra_heavy_processes = ["my_solver", "sim_runner"]
"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.toml"
            cfg_file.write_text(partial_toml, encoding="utf-8")
            loaded = spc.load_config(cfg_file)

        # Overridden values applied
        self.assertEqual(loaded["general"]["cooldown_sec"], 20.0)
        self.assertEqual(loaded["cpu"]["light_epp_battery"], "power")
        self.assertEqual(loaded["brightness"]["min_pct"], 18)
        self.assertFalse(loaded["brightness"]["webcam_als_enabled"])
        self.assertEqual(
            loaded["workloads"]["extra_heavy_processes"], ["my_solver", "sim_runner"]
        )

        # Untouched sibling keys & sections preserved
        self.assertEqual(loaded["general"]["poll_interval_sec"], 2.5)
        self.assertEqual(loaded["general"]["wifi_recheck_ticks"], 6)
        self.assertEqual(loaded["cpu"]["light_epp_ac"], "balance_power")
        self.assertTrue(loaded["cpu"]["uncap_boost_in_light"])
        self.assertEqual(loaded["brightness"]["max_pct"], 95)
        self.assertEqual(loaded["brightness"]["webcam_interval_sec"], 1800)
        self.assertEqual(loaded["battery_tricks"], spc.DEFAULT_CONFIG["battery_tricks"])

    def test_invalid_malformed_and_directory_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            bad_file = tmp_dir / "broken.toml"
            bad_file.write_text("[general\npoll_interval_sec = ???\n", encoding="utf-8")

            self.assertEqual(spc.load_config(bad_file), spc.DEFAULT_CONFIG)
            self.assertEqual(spc.load_config(tmp_dir), spc.DEFAULT_CONFIG)
            self.assertEqual(spc.load_config(""), spc.DEFAULT_CONFIG)

    def test_deep_merge_ignores_non_dict_section_override(self) -> None:
        merged = spc.deep_merge(spc.DEFAULT_CONFIG, {"cpu": "invalid-string"})
        self.assertEqual(merged["cpu"], spc.DEFAULT_CONFIG["cpu"])

    def test_effective_brightness_config_applies_state_overrides(self) -> None:
        base_cfg = copy.deepcopy(spc.DEFAULT_CONFIG)
        base_cfg["brightness"]["enabled"] = True
        base_cfg["brightness"]["webcam_als_enabled"] = False

        # Null values defer to config.toml
        eff_defer = spc.effective_brightness_config(
            base_cfg, {"enabled": True, "brightness_enabled": None, "webcam_als_enabled": None}
        )
        self.assertTrue(eff_defer["enabled"])
        self.assertFalse(eff_defer["webcam_als_enabled"])

        # Boolean values override config.toml
        eff_override = spc.effective_brightness_config(
            base_cfg, {"enabled": True, "brightness_enabled": False, "webcam_als_enabled": True}
        )
        self.assertFalse(eff_override["enabled"])
        self.assertTrue(eff_override["webcam_als_enabled"])

    def test_config_example_toml_matches_default_config_exact(self) -> None:
        example_path = REPO_ROOT / "config.example.toml"
        with example_path.open("rb") as fh:
            parsed = tomllib.load(fh)
        self.assertEqual(parsed, spc.DEFAULT_CONFIG)

    def test_legacy_low_refresh_on_battery_light_migrates_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy_cfg = Path(tmp) / "legacy.toml"
            legacy_cfg.write_text(
                "[battery_tricks]\nlow_refresh_on_battery_light = false\n",
                encoding="utf-8",
            )
            loaded = spc.load_config(legacy_cfg)
            self.assertFalse(loaded["battery_tricks"]["refresh_policy_enabled"])

            # Explicit refresh_policy_enabled takes precedence over legacy key
            mixed_cfg = Path(tmp) / "mixed.toml"
            mixed_cfg.write_text(
                "[battery_tricks]\nlow_refresh_on_battery_light = false\nrefresh_policy_enabled = true\n",
                encoding="utf-8",
            )
            loaded_mixed = spc.load_config(mixed_cfg)
            self.assertTrue(loaded_mixed["battery_tricks"]["refresh_policy_enabled"])


class TestAdaptiveBrightness(unittest.TestCase):
    """Tests for circadian schedule, ambient mapping, clamping, override, and safe backlight."""

    def test_circadian_schedule_interpolation_across_midnight(self) -> None:
        sched = spc.DEFAULT_CONFIG["brightness"]["schedule"]
        # Exact anchor points
        self.assertAlmostEqual(ab.interpolate_schedule(0.0, sched), 22.0, places=3)
        self.assertAlmostEqual(ab.interpolate_schedule(12.0, sched), 78.0, places=3)
        self.assertAlmostEqual(ab.interpolate_schedule(22.0, sched), 28.0, places=3)
        # Midpoint between [0, 22] and [6, 25] -> 23.5
        self.assertAlmostEqual(ab.interpolate_schedule(3.0, sched), 23.5, places=3)
        # Midnight wrap-around: [22, 28] -> [24/0, 22], midpoint at 23:00 -> 25.0
        self.assertAlmostEqual(ab.interpolate_schedule(23.0, sched), 25.0, places=3)
        self.assertAlmostEqual(ab.interpolate_schedule(23.5, sched), 23.5, places=3)
        self.assertAlmostEqual(ab.interpolate_schedule(24.0, sched), 22.0, places=3)

        # Schedule without an explicit 00:00 anchor still wraps smoothly across midnight
        no_zero_sched = [[6, 20], [18, 80]]
        self.assertAlmostEqual(ab.interpolate_schedule(0.0, no_zero_sched), 50.0, places=3)
        self.assertAlmostEqual(ab.interpolate_schedule(3.0, no_zero_sched), 35.0, places=3)
        self.assertAlmostEqual(ab.interpolate_schedule(21.0, no_zero_sched), 65.0, places=3)

    def test_lux_to_brightness_pct_logarithmic_mapping(self) -> None:
        self.assertAlmostEqual(ab.lux_to_brightness_pct(0.0, 12, 95), 12.0, places=2)
        self.assertAlmostEqual(ab.lux_to_brightness_pct(-5.0, 12, 95), 12.0, places=2)
        self.assertAlmostEqual(ab.lux_to_brightness_pct(float("nan"), 12, 95), 12.0, places=2)

        p10 = ab.lux_to_brightness_pct(10.0, 12, 95)
        p100 = ab.lux_to_brightness_pct(100.0, 12, 95)
        p400 = ab.lux_to_brightness_pct(400.0, 12, 95)
        p1500 = ab.lux_to_brightness_pct(1500.0, 12, 95)
        p5000 = ab.lux_to_brightness_pct(5000.0, 12, 95)

        self.assertGreaterEqual(p10, 20.0)
        self.assertLessEqual(p10, 30.0)
        self.assertGreaterEqual(p100, 48.0)
        self.assertLessEqual(p100, 58.0)
        self.assertGreaterEqual(p400, 70.0)
        self.assertLessEqual(p400, 80.0)
        self.assertAlmostEqual(p1500, 95.0, places=2)
        self.assertAlmostEqual(p5000, 95.0, places=2)
        self.assertTrue(12.0 < p10 < p100 < p400 < p1500)

    def test_luma_to_brightness_pct_mapping(self) -> None:
        self.assertAlmostEqual(ab.luma_to_brightness_pct(0.0, 12, 95), 12.0, places=2)
        self.assertAlmostEqual(ab.luma_to_brightness_pct(-10.0, 12, 95), 12.0, places=2)
        self.assertAlmostEqual(ab.luma_to_brightness_pct(255.0, 12, 95), 95.0, places=2)
        self.assertAlmostEqual(ab.luma_to_brightness_pct(400.0, 12, 95), 95.0, places=2)

        samples = [ab.luma_to_brightness_pct(v, 12, 95) for v in (0, 32, 64, 128, 192, 255)]
        for a, b in zip(samples, samples[1:]):
            self.assertLess(a, b)

    def test_battery_multiplier_blend_and_min_max_clamping(self) -> None:
        # AC schedule-only
        self.assertEqual(
            ab.compute_target_brightness(80.0, None, ac_online=True, battery_multiplier=0.85),
            80,
        )
        # Battery multiplier (80 * 0.85 = 68)
        self.assertEqual(
            ab.compute_target_brightness(80.0, None, ac_online=False, battery_multiplier=0.85),
            68,
        )
        # Blend 70% ambient (90) + 30% schedule (30) = 72 on AC; 72 * 0.85 = 61.2 -> 61 on battery
        self.assertEqual(
            ab.compute_target_brightness(30.0, 90.0, ac_online=True, battery_multiplier=0.85),
            72,
        )
        self.assertEqual(
            ab.compute_target_brightness(30.0, 90.0, ac_online=False, battery_multiplier=0.85),
            61,
        )
        # Lower clamp at min_pct
        self.assertEqual(
            ab.compute_target_brightness(
                14.0, None, ac_online=False, battery_multiplier=0.85, min_pct=12, max_pct=95
            ),
            12,
        )
        # Upper clamp at max_pct
        self.assertEqual(
            ab.compute_target_brightness(
                100.0, 100.0, ac_online=True, battery_multiplier=0.85, min_pct=12, max_pct=95
            ),
            95,
        )

    def test_manual_override_detection_pause_and_resume(self) -> None:
        self.assertFalse(ab.detect_manual_override(50, None))
        self.assertFalse(ab.detect_manual_override(53, 50))
        self.assertTrue(ab.detect_manual_override(54, 50))
        self.assertTrue(ab.detect_manual_override(46, 50))

        backend = FakeBacklightBackend(initial_pct=40)
        als = FakeALSReader(lux=None)
        ctrl = ab.AdaptiveBrightnessController(backend=backend, als_reader=als)
        cfg = copy.deepcopy(spc.DEFAULT_CONFIG["brightness"])
        cfg["smooth_step_pct"] = 5
        cfg["manual_override_pause_sec"] = 1800

        # Tick 1 at 12:00 (target 78% on AC): steps smoothly 40 -> 45
        t0 = 10_000.0
        tel1 = ctrl.update(cfg, ac_online=True, now=t0, local_hour=12.0)
        self.assertEqual(backend.pct, 45)
        self.assertEqual(tel1["brightness_target_pct"], 78)
        self.assertEqual(tel1["brightness_source"], "Schedule")
        self.assertEqual(tel1["manual_override_until"], 0.0)

        # User manually hits brightness Fn key down to 25% (delta=20 >= 4)
        backend.pct = 25
        writes_before = len(backend.set_calls)

        # Tick 2 detects manual override and pauses for 1800s without writing
        tel2 = ctrl.update(cfg, ac_online=True, now=t0 + 2.5, local_hour=12.0)
        self.assertEqual(tel2["brightness_source"], "Manual Pause")
        self.assertAlmostEqual(tel2["manual_override_until"], t0 + 2.5 + 1800.0, places=2)
        self.assertEqual(len(backend.set_calls), writes_before)

        # Tick 3 mid-pause still does not write
        tel3 = ctrl.update(cfg, ac_online=True, now=t0 + 900.0, local_hour=12.0)
        self.assertEqual(tel3["brightness_source"], "Manual Pause")
        self.assertEqual(len(backend.set_calls), writes_before)

        # Tick 4 after pause expires resumes smoothly from 25 -> 30
        tel4 = ctrl.update(cfg, ac_online=True, now=t0 + 1805.0, local_hour=12.0)
        self.assertEqual(tel4["brightness_source"], "Schedule")
        self.assertEqual(tel4["manual_override_until"], 0.0)
        self.assertEqual(backend.pct, 30)

        # Manual override again, then cleared immediately by force_measure=True
        backend.pct = 15
        ctrl.update(cfg, ac_online=True, now=t0 + 1810.0, local_hour=12.0)
        self.assertGreater(ctrl.manual_override_until, 0.0)

        tel5 = ctrl.update(cfg, ac_online=True, now=t0 + 1815.0, local_hour=12.0, force_measure=True)
        self.assertEqual(tel5["manual_override_until"], 0.0)
        self.assertEqual(backend.pct, 20)

    def test_should_sample_webcam_respects_ambient_source_even_on_force(self) -> None:
        backend = FakeBacklightBackend(initial_pct=50)
        als = FakeALSReader(lux=None)
        ctrl = ab.AdaptiveBrightnessController(backend=backend, als_reader=als)

        # Explicit schedule or als source must never fire webcam even when force=True
        sched_cfg = copy.deepcopy(spc.DEFAULT_CONFIG["brightness"])
        sched_cfg["ambient_source"] = "schedule"
        self.assertFalse(ctrl.should_sample_webcam(sched_cfg, now=1000.0, force=True))

        als_cfg = copy.deepcopy(spc.DEFAULT_CONFIG["brightness"])
        als_cfg["ambient_source"] = "als"
        self.assertFalse(ctrl.should_sample_webcam(als_cfg, now=1000.0, force=True))

        # Auto source with webcam_als_enabled=False skips periodic, allows explicit force=True
        auto_cfg = copy.deepcopy(spc.DEFAULT_CONFIG["brightness"])
        auto_cfg["ambient_source"] = "auto"
        auto_cfg["webcam_als_enabled"] = False
        self.assertFalse(ctrl.should_sample_webcam(auto_cfg, now=1000.0, force=False))
        self.assertTrue(ctrl.should_sample_webcam(auto_cfg, now=1000.0, force=True))

        # Hardware ALS present suppresses webcam in auto mode even on force=True
        self.assertFalse(
            ctrl.should_sample_webcam(auto_cfg, daemon_als_lux=120.0, now=1000.0, force=True)
        )

        # Default config enables periodic webcam ALS on 30-minute (1800s) cadence
        default_bcfg = ab.normalize_brightness_config(None)
        self.assertTrue(default_bcfg["webcam_als_enabled"])
        self.assertEqual(default_bcfg["webcam_interval_sec"], 1800)
        self.assertTrue(ctrl.should_sample_webcam(default_bcfg, now=1000.0, force=False))
        ctrl.ingest_webcam_luma(128.0, status="ok", now=1000.0)
        self.assertFalse(ctrl.should_sample_webcam(default_bcfg, now=2000.0, force=False))
        self.assertTrue(ctrl.should_sample_webcam(default_bcfg, now=2800.0, force=False))

    def test_select_safe_backlight_prefers_connected_igpu_and_rejects_nvidia(self) -> None:
        nvidia_unconnected = ab.BacklightEntry(
            name="nvidia_0",
            path="/sys/class/backlight/nvidia_0",
            device_path="/sys/devices/pci0000:00/0000:00:01.1/0000:0a:00.0",
            backlight_type="raw",
            connector_status="",
            max_brightness=0,
        )
        amdgpu_edp = ab.BacklightEntry(
            name="amdgpu_bl2",
            path="/sys/class/backlight/amdgpu_bl2",
            device_path="/sys/devices/pci0000:00/0000:00:08.1/0000:0b:00.0/drm/card2/card2-eDP-2",
            backlight_type="raw",
            connector_status="connected",
            max_brightness=65535,
        )
        intel_edp = ab.BacklightEntry(
            name="intel_backlight",
            path="/sys/class/backlight/intel_backlight",
            device_path="/sys/devices/pci0000:00/0000:00:02.0/drm/card0/card0-eDP-1",
            backlight_type="raw",
            connector_status="connected",
            max_brightness=96000,
        )
        disconnected_edp = ab.BacklightEntry(
            name="amdgpu_bl0",
            path="/sys/class/backlight/amdgpu_bl0",
            device_path="/sys/devices/pci0000:00/0000:00:08.1/0000:0b:00.0/drm/card1/card1-eDP-1",
            backlight_type="raw",
            connector_status="disconnected",
            max_brightness=255,
        )
        acpi_fw = ab.BacklightEntry(
            name="acpi_video0",
            path="/sys/class/backlight/acpi_video0",
            device_path="/sys/devices/LNXSYSTM:00/LNXVIDEO:00",
            backlight_type="firmware",
            connector_status="",
            max_brightness=100,
        )

        # Prefers amdgpu_bl2 over nvidia_0
        picked_amd = ab.select_safe_backlight([nvidia_unconnected, amdgpu_edp])
        self.assertIsNotNone(picked_amd)
        self.assertEqual(picked_amd.name, "amdgpu_bl2")

        # Prefers intel_backlight over nvidia_0 and acpi_video0
        picked_intel = ab.select_safe_backlight([nvidia_unconnected, acpi_fw, intel_edp])
        self.assertIsNotNone(picked_intel)
        self.assertEqual(picked_intel.name, "intel_backlight")

        # Skips disconnected eDP connector
        picked_conn = ab.select_safe_backlight([disconnected_edp, nvidia_unconnected, amdgpu_edp])
        self.assertIsNotNone(picked_conn)
        self.assertEqual(picked_conn.name, "amdgpu_bl2")

        # Rejects unconnected nvidia_0 when it is the only entry
        self.assertIsNone(ab.select_safe_backlight([nvidia_unconnected]))

        # Also works with dict-formatted entries
        dict_pick = ab.select_safe_backlight(
            [
                {"name": "nvidia_0", "device_path": "/sys/devices/pci0000:00/0000:0a:00.0", "type": "raw"},
                {
                    "name": "intel_backlight",
                    "device_path": "/sys/devices/pci0000:00/0000:00:02.0/drm/card0/card0-eDP-1",
                    "type": "raw",
                    "status": "connected",
                },
            ]
        )
        self.assertIsNotNone(dict_pick)
        self.assertEqual(dict_pick["name"], "intel_backlight")

    @unittest.skipIf(sys.platform == "win32", "POSIX /proc/<pid>/fd symlink check")
    def test_is_video_device_busy_detects_open_fd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            fake_dev = Path(tmp) / "video0"
            fake_dev.write_text("", encoding="utf-8")

            # Another PID holding fake_dev open
            fd_dir = proc_root / "999999" / "fd"
            fd_dir.mkdir(parents=True)
            os.symlink(str(fake_dev), fd_dir / "4")
            self.assertTrue(ab.is_video_device_busy(str(fake_dev), proc_root=proc_root))

            # Once closed, no longer busy
            (fd_dir / "4").unlink()
            self.assertFalse(ab.is_video_device_busy(str(fake_dev), proc_root=proc_root))

    def test_webcam_warmup_frame_extraction_and_shutter_closed_fallback(self) -> None:
        # 4 concatenated frames: frames 1..3 dark warmup (0), frame 4 exposed (120)
        raw = bytes([0] * 100 + [0] * 100 + [5] * 100 + [120] * 100)
        last = ab.extract_last_raw_frame(raw, frame_count=4)
        self.assertEqual(len(last), 100)
        luma, status = ab.analyze_webcam_frame(last)
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(luma or 0.0, 120.0, places=2)

        # Closed physical privacy shutter (all pixels 0 or <= 1) must be rejected
        shutter_luma, shutter_status = ab.analyze_webcam_frame(bytes([0] * 19200))
        self.assertIsNone(shutter_luma)
        self.assertEqual(shutter_status, "webcam-shutter-closed")

        shutter_luma_1, shutter_status_1 = ab.analyze_webcam_frame(bytes([1] * 100))
        self.assertIsNone(shutter_luma_1)
        self.assertEqual(shutter_status_1, "webcam-shutter-closed")

        # Isolated hot-pixel noise on a closed shutter (mean < 0.5, max <= 4) also rejected
        hot_pixel_frame = bytes([0] * 999 + [3])
        hp_luma, hp_status = ab.analyze_webcam_frame(hot_pixel_frame)
        self.assertIsNone(hp_luma)
        self.assertEqual(hp_status, "webcam-shutter-closed")

        # Controller clears cached webcam luma on shutter-closed and falls back to Schedule
        backend = FakeBacklightBackend(initial_pct=50)
        als = FakeALSReader(lux=None)
        ctrl = ab.AdaptiveBrightnessController(backend=backend, als_reader=als)
        cfg = copy.deepcopy(spc.DEFAULT_CONFIG["brightness"])

        ctrl.ingest_webcam_luma(200.0, status="ok", now=1000.0)
        tel_cam = ctrl.update(cfg, ac_online=True, now=1001.0, local_hour=12.0)
        self.assertEqual(tel_cam["brightness_source"], "Webcam")

        ctrl.ingest_webcam_luma(None, status="webcam-shutter-closed", now=2000.0)
        self.assertIsNone(ctrl.cached_webcam_luma)
        self.assertIsNone(ctrl.last_ambient_luma)
        tel_sched = ctrl.update(cfg, ac_online=True, now=2001.0, local_hour=12.0)
        self.assertEqual(tel_sched["brightness_source"], "Schedule")
        self.assertEqual(tel_sched["brightness_target_pct"], 78)


class TestDisplayRefresh(unittest.TestCase):
    """Tests for kscreen-doctor, wlr-randr, xrandr parsing and mode switching."""

    KSCREEN_SAMPLE_ANSI = (
        "\x1b[01;32mOutput: \x1b[0;0m1 eDP-2 9f553a58-b582-470b-87f1-7bd9e17e86b1\n"
        "\t\x1b[01;32menabled\x1b[0;0m\n"
        "\t\x1b[01;32mconnected\x1b[0;0m\n"
        "\t\x1b[01;32mpriority 1\x1b[0;0m\n"
        "\t\x1b[01;33mPanel\x1b[0;0m\n"
        "\t\x1b[01;34mModes: \x1b[0;0m "
        "1:\x1b[01;32m2560x1600@300.00*\x1b[0;0m!  "
        "2:2560x1600@240.00  "
        "3:2560x1600@60.00  "
        "4:1920x1200@300.00  "
        "5:1920x1200@60.00\n"
        "\tGeometry: 0,0 1897x1186\n"
        "\x1b[01;32mOutput: \x1b[0;0m2 HDMI-A-1 aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n"
        "\t\x1b[01;32menabled\x1b[0;0m\n"
        "\t\x1b[01;32mconnected\x1b[0;0m\n"
        "\t\x1b[01;34mModes: \x1b[0;0m 1:3840x2160@60.00*!  2:1920x1080@120.00\n"
    )

    WLR_RANDR_SAMPLE = """DP-1 "Dell Inc. DELL U2723QE"
  Enabled: yes
  Modes:
    3840x2160 px, 60.000000 Hz (preferred, current)
eDP-1 "BOE Display Panel"
  Enabled: yes
  Modes:
    2560x1600 px, 165.000000 Hz (preferred, current)
    2560x1600 px, 60.000000 Hz
    1920x1200 px, 60.000000 Hz
"""

    XRANDR_SAMPLE = """Screen 0: minimum 320 x 200, current 2560 x 1600, maximum 16384 x 16384
HDMI-1 connected 1920x1080+2560+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+  50.00
eDP-1 connected primary 2560x1600+0+0 (normal left inverted right x axis y axis) 344mm x 215mm
   2560x1600    240.00*+ 120.00   60.00
   1920x1200    240.00   60.00
DP-1 disconnected (normal left inverted right x axis y axis)
"""

    def test_parse_kscreen_doctor_with_ansi_and_300_240_60_modes(self) -> None:
        info = dr.parse_kscreen_output(self.KSCREEN_SAMPLE_ANSI)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.output_name, "eDP-2")
        self.assertEqual(info.backend, "kscreen-doctor")
        self.assertIsNotNone(info.current_mode)
        assert info.current_mode is not None
        self.assertEqual(info.current_mode.mode_id, "1")
        self.assertEqual((info.current_mode.width, info.current_mode.height), (2560, 1600))
        self.assertAlmostEqual(info.current_mode.refresh_hz, 300.0, places=2)

        tiers = dr.choose_refresh_modes(info, ac_hz=0, battery_hz=0, low_hz=60.0)
        high_mode, mid_mode, low_mode = tiers["high"], tiers["mid"], tiers["low"]
        assert high_mode is not None and mid_mode is not None and low_mode is not None
        self.assertEqual(high_mode.mode_id, "1")
        self.assertAlmostEqual(high_mode.refresh_hz, 300.0, places=2)
        self.assertEqual(mid_mode.mode_id, "2")
        self.assertAlmostEqual(mid_mode.refresh_hz, 240.0, places=2)
        self.assertEqual(low_mode.mode_id, "3")
        self.assertAlmostEqual(low_mode.refresh_hz, 60.0, places=2)
        self.assertEqual((low_mode.width, low_mode.height), (2560, 1600))

        self.assertEqual(dr.select_target_mode(info, "mid", low_hz=60.0), mid_mode)
        self.assertEqual(dr.select_target_mode(info, "low", low_hz=60.0), low_mode)
        # Already at 300 Hz -> high returns None (no-op)
        self.assertIsNone(dr.select_target_mode(info, "high", low_hz=60.0))
        self.assertEqual(
            dr.build_switch_command(info, mid_mode),
            ["kscreen-doctor", "output.eDP-2.mode.2"],
        )
        self.assertEqual(
            dr.build_switch_command(info, low_mode),
            ["kscreen-doctor", "output.eDP-2.mode.3"],
        )

    def test_3tier_panel_matrix_and_explicit_hz_overrides(self) -> None:
        matrix = [
            ([300.0, 240.0, 60.0], 300, 240, 60),
            ([240.0, 165.0, 60.0], 240, 165, 60),
            ([165.0, 120.0, 60.0], 165, 120, 60),
            ([144.0, 90.0, 60.0], 144, 90, 60),
            ([144.0, 60.0], 144, 144, 60),
            ([120.0, 60.0], 120, 120, 60),
            ([60.0], 60, 60, 60),
        ]
        for rates, exp_high, exp_mid, exp_low in matrix:
            with self.subTest(rates=rates):
                modes = [
                    dr.DisplayMode(
                        mode_id=str(i + 1),
                        width=2560,
                        height=1600,
                        refresh_hz=r,
                        is_current=(i == 0),
                        is_preferred=(i == 0),
                    )
                    for i, r in enumerate(rates)
                ]
                tiers = dr.choose_refresh_modes(modes, ac_hz=0, battery_hz=0, low_hz=60)
                assert tiers["high"] and tiers["mid"] and tiers["low"]
                self.assertEqual(int(round(tiers["high"].refresh_hz)), exp_high)
                self.assertEqual(int(round(tiers["mid"].refresh_hz)), exp_mid)
                self.assertEqual(int(round(tiers["low"].refresh_hz)), exp_low)

        # Explicit Hz overrides on a 300/240/120/60 panel
        quad_modes = [
            dr.DisplayMode(mode_id="1", width=2560, height=1600, refresh_hz=300.0, is_current=True),
            dr.DisplayMode(mode_id="2", width=2560, height=1600, refresh_hz=240.0),
            dr.DisplayMode(mode_id="3", width=2560, height=1600, refresh_hz=120.0),
            dr.DisplayMode(mode_id="4", width=2560, height=1600, refresh_hz=60.0),
        ]
        custom = dr.choose_refresh_modes(quad_modes, ac_hz=240, battery_hz=120, low_hz=60)
        assert custom["high"] and custom["mid"] and custom["low"]
        self.assertEqual(int(round(custom["high"].refresh_hz)), 240)
        self.assertEqual(int(round(custom["mid"].refresh_hz)), 120)
        self.assertEqual(int(round(custom["low"].refresh_hz)), 60)

    def test_display_refresh_win_devmodes_to_modes(self) -> None:
        raw = [
            (2560, 1600, 300, 32),
            (2560, 1600, 240, 32),
            (2560, 1600, 60, 32),
            (2560, 1600, 60, 16),
        ]
        info = dr.win_devmodes_to_modes(raw, current_mode=(2560, 1600, 300, 32))
        self.assertEqual(info.backend, "win32")
        tiers = dr.choose_refresh_modes(info)
        assert tiers["high"] and tiers["mid"] and tiers["low"]
        self.assertAlmostEqual(tiers["high"].refresh_hz, 300.0, places=1)
        self.assertAlmostEqual(tiers["mid"].refresh_hz, 240.0, places=1)
        self.assertAlmostEqual(tiers["low"].refresh_hz, 60.0, places=1)

    def test_parse_wlr_randr_output_and_mode_selection(self) -> None:
        info = dr.parse_wlr_randr_output(self.WLR_RANDR_SAMPLE)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.output_name, "eDP-1")
        self.assertEqual(info.backend, "wlr-randr")
        assert info.current_mode is not None
        self.assertAlmostEqual(info.current_mode.refresh_hz, 165.0, places=2)

        tiers = dr.choose_refresh_modes(info, low_hz=60.0)
        high_mode, mid_mode, low_mode = tiers["high"], tiers["mid"], tiers["low"]
        assert high_mode is not None and mid_mode is not None and low_mode is not None
        self.assertAlmostEqual(low_mode.refresh_hz, 60.0, places=2)
        self.assertEqual((low_mode.width, low_mode.height), (2560, 1600))
        self.assertAlmostEqual(high_mode.refresh_hz, 165.0, places=2)
        # 2-rate panel (165/60) keeps mid == high (165 Hz)
        self.assertAlmostEqual(mid_mode.refresh_hz, 165.0, places=2)
        self.assertEqual(
            dr.build_switch_command(info, low_mode),
            ["wlr-randr", "--output", "eDP-1", "--mode", "2560x1600@60.000000Hz"],
        )

    def test_parse_xrandr_output_and_mode_selection(self) -> None:
        info = dr.parse_xrandr_output(self.XRANDR_SAMPLE)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.output_name, "eDP-1")
        self.assertEqual(info.backend, "xrandr")
        assert info.current_mode is not None
        self.assertAlmostEqual(info.current_mode.refresh_hz, 240.0, places=2)

        tiers = dr.choose_refresh_modes(info, low_hz=60.0)
        high_mode, mid_mode, low_mode = tiers["high"], tiers["mid"], tiers["low"]
        assert high_mode is not None and mid_mode is not None and low_mode is not None
        self.assertAlmostEqual(high_mode.refresh_hz, 240.0, places=2)
        # 3-rate panel (240/120/60) picks 120 Hz for mid
        self.assertAlmostEqual(mid_mode.refresh_hz, 120.0, places=2)
        self.assertAlmostEqual(low_mode.refresh_hz, 60.0, places=2)
        self.assertEqual(
            dr.build_switch_command(info, low_mode),
            ["xrandr", "--output", "eDP-1", "--mode", "2560x1600", "--rate", "60.00"],
        )

    def test_single_refresh_rate_panel_never_switches(self) -> None:
        single_hz = (
            "Output: 1 eDP-1 uuid-1\n"
            "\tenabled\n"
            "\tconnected\n"
            "\tPanel\n"
            "\tModes: 1:1920x1080@60.00*! 2:1280x720@60.00\n"
        )
        info = dr.parse_kscreen_output(single_hz)
        assert info is not None
        tiers = dr.choose_refresh_modes(info, low_hz=60.0)
        assert tiers["high"] and tiers["mid"] and tiers["low"]
        self.assertAlmostEqual(tiers["high"].refresh_hz, 60.0, places=2)
        self.assertAlmostEqual(tiers["mid"].refresh_hz, 60.0, places=2)
        self.assertAlmostEqual(tiers["low"].refresh_hz, 60.0, places=2)
        self.assertIsNone(dr.select_target_mode(info, "mid", low_hz=60.0))
        self.assertIsNone(dr.select_target_mode(info, "low", low_hz=60.0))

    def test_controller_switches_3tier_without_thrashing_and_restores(self) -> None:
        state = {"mode_id": "1"}
        switch_cmds: list[list[str]] = []

        kscreen_high = self.KSCREEN_SAMPLE_ANSI
        kscreen_mid = self.KSCREEN_SAMPLE_ANSI.replace(
            "1:\x1b[01;32m2560x1600@300.00*\x1b[0;0m!  2:2560x1600@240.00  3:2560x1600@60.00",
            "1:2560x1600@300.00!  2:2560x1600@240.00*  3:2560x1600@60.00",
        )
        kscreen_low = self.KSCREEN_SAMPLE_ANSI.replace(
            "1:\x1b[01;32m2560x1600@300.00*\x1b[0;0m!  2:2560x1600@240.00  3:2560x1600@60.00",
            "1:2560x1600@300.00!  2:2560x1600@240.00  3:2560x1600@60.00*",
        )

        def fake_runner(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
            if cmd == ["kscreen-doctor", "-o"]:
                out = {"1": kscreen_high, "2": kscreen_mid, "3": kscreen_low}[state["mode_id"]]
                return 0, out, ""
            if cmd[0] == "kscreen-doctor" and cmd[1].startswith("output.eDP-2.mode."):
                switch_cmds.append(cmd)
                state["mode_id"] = cmd[1].rsplit(".", 1)[-1]
                return 0, "", ""
            return 1, "", "unsupported"

        ctrl = dr.DisplayRefreshController(
            runner=fake_runner, probe_interval_sec=60.0, min_switch_interval_sec=5.0
        )
        ctrl.backend = "kscreen-doctor"
        ctrl.probe(force=True, now=100.0)

        # Already high (300 Hz) -> no switch
        ctrl.apply_profile("high", ac_hz=0, battery_hz=0, low_hz=60.0, now=101.0)
        self.assertEqual(switch_cmds, [])

        # Unplugged >= 20% -> mid (240 Hz, mode.2)
        tel_mid = ctrl.apply_profile("mid", ac_hz=0, battery_hz=0, low_hz=60.0, now=110.0)
        self.assertEqual(switch_cmds, [["kscreen-doctor", "output.eDP-2.mode.2"]])
        self.assertAlmostEqual(tel_mid["refresh_hz"], 240.0, places=1)
        self.assertEqual(tel_mid["refresh_profile"], "mid")

        # Repeated mid tick -> no thrashing
        ctrl.apply_profile("mid", ac_hz=0, battery_hz=0, low_hz=60.0, now=120.0)
        self.assertEqual(len(switch_cmds), 1)

        # Unplugged < 20% -> low (60 Hz, mode.3)
        tel_low = ctrl.apply_profile("low", ac_hz=0, battery_hz=0, low_hz=60.0, now=130.0)
        self.assertEqual(switch_cmds[-1], ["kscreen-doctor", "output.eDP-2.mode.3"])
        self.assertAlmostEqual(tel_low["refresh_hz"], 60.0, places=1)

        # Plug AC back in -> high (300 Hz, mode.1)
        tel_high = ctrl.apply_profile("high", ac_hz=0, battery_hz=0, low_hz=60.0, now=140.0)
        self.assertEqual(switch_cmds[-1], ["kscreen-doctor", "output.eDP-2.mode.1"])
        self.assertAlmostEqual(tel_high["refresh_hz"], 300.0, places=1)

        # Step down to mid again and verify restore_native() restores 300 Hz cleanly
        ctrl.apply_profile("mid", ac_hz=0, battery_hz=0, low_hz=60.0, now=150.0)
        self.assertTrue(ctrl.restore_native())
        self.assertEqual(switch_cmds[-1], ["kscreen-doctor", "output.eDP-2.mode.1"])


class TestSmartPowerDaemon(unittest.TestCase):
    """Tests for smart-powerd.py workload hysteresis, filters, wattage, USB & dGPU helpers."""

    @staticmethod
    def _make_stub_daemon(cooldown_sec: float = 12.0) -> Any:
        d = smart_powerd.SmartPowerDaemon.__new__(smart_powerd.SmartPowerDaemon)
        d.enabled = True
        d.cooldown_sec = cooldown_sec
        d.current_mode = None
        d.current_reason = "initializing"
        d.last_mode_activity_ts = {}
        return d

    def test_mode_selection_and_12s_downgrade_hysteresis_without_repeated_cooldown(self) -> None:
        d = self._make_stub_daemon(cooldown_sec=12.0)

        # Initial idle -> light
        mode, reason = d._select_mode_with_hysteresis(100.0, None, None)
        d.current_mode, d.current_reason = mode, reason
        self.assertEqual(mode, "light")

        # Immediate upgrade light -> heavy
        mode, reason = d._select_mode_with_hysteresis(102.0, "process:gcc", None)
        d.current_mode, d.current_reason = mode, reason
        self.assertEqual(mode, "heavy")
        self.assertEqual(reason, "process:gcc")

        # Workload drops at t=105 (3s < 12s): stays heavy with single "(cooldown)"
        mode, reason = d._select_mode_with_hysteresis(105.0, None, None)
        d.current_mode, d.current_reason = mode, reason
        self.assertEqual(mode, "heavy")
        self.assertEqual(reason, "process:gcc (cooldown)")

        # Repeated cooldown ticks at t=108 and t=111.5 MUST NOT duplicate "(cooldown)"
        for ts in (108.0, 111.5, 113.9):
            mode, reason = d._select_mode_with_hysteresis(ts, None, None)
            d.current_mode, d.current_reason = mode, reason
            self.assertEqual(mode, "heavy")
            self.assertEqual(reason, "process:gcc (cooldown)")
            self.assertEqual(reason.count("(cooldown)"), 1)

        # At t=114.0 (12.0s elapsed since last heavy activity at t=102.0): downgrades to light
        mode, reason = d._select_mode_with_hysteresis(114.0, None, None)
        d.current_mode, d.current_reason = mode, reason
        self.assertEqual(mode, "light")
        self.assertEqual(reason, "idle / light workload")

        # Upgrade to download, then immediate upgrade to heavy (no cooldown on upgrade)
        mode, reason = d._select_mode_with_hysteresis(120.0, None, "pkg/dl:pacman")
        d.current_mode, d.current_reason = mode, reason
        self.assertEqual(mode, "download")

        mode, reason = d._select_mode_with_hysteresis(121.0, "process:cargo", None)
        d.current_mode, d.current_reason = mode, reason
        self.assertEqual(mode, "heavy")

        # Disabled daemon always selects off
        d.enabled = False
        mode, reason = d._select_mode_with_hysteresis(122.0, "process:cargo", None)
        self.assertEqual(mode, "off")
        self.assertIn("paused", reason)

    def test_chromium_electron_ui_helper_exclusion(self) -> None:
        self.assertTrue(
            smart_powerd.is_electron_ui_helper(["/usr/lib/electron/electron", "--type=renderer"])
        )
        self.assertTrue(
            smart_powerd.is_electron_ui_helper(["/usr/bin/chromium", "--type=gpu-process", "--gpu"])
        )
        self.assertTrue(
            smart_powerd.is_electron_ui_helper(["/opt/code/code", "--type=utility"])
        )
        self.assertTrue(
            smart_powerd.is_electron_ui_helper(["/opt/app/app", "--type=zygote"])
        )
        self.assertTrue(
            smart_powerd.is_electron_ui_helper(["/opt/app/app", "--type=crashpad-handler"])
        )

        # Legitimate workloads must NOT be classified as UI helpers
        self.assertFalse(smart_powerd.is_electron_ui_helper(["/usr/bin/python3", "train.py"]))
        self.assertFalse(smart_powerd.is_electron_ui_helper(["/usr/bin/node", "server.js"]))
        self.assertFalse(smart_powerd.is_electron_ui_helper(["/usr/bin/gcc", "-O2", "main.c"]))
        self.assertFalse(smart_powerd.is_electron_ui_helper([]))

    def test_battery_watts_calculation(self) -> None:
        # Direct power_now in microwatts
        self.assertAlmostEqual(
            smart_powerd.compute_battery_watts("14500000", None, None), 14.5, places=2
        )
        # Fallback BAT0 current_now * voltage_now / 1e12
        self.assertAlmostEqual(
            smart_powerd.compute_battery_watts(None, "1250000", "16000000"), 20.0, places=2
        )
        # Handles negative discharging current_now cleanly via abs()
        self.assertAlmostEqual(
            smart_powerd.compute_battery_watts(None, "-850000", "15400000"), 13.09, places=2
        )
        # Missing or malformed values -> 0.0
        self.assertEqual(smart_powerd.compute_battery_watts(None, None, None), 0.0)
        self.assertEqual(smart_powerd.compute_battery_watts(None, "bad", "16000000"), 0.0)

    def test_usb_hid_bluetooth_audio_exclusion(self) -> None:
        # Hubs excluded
        self.assertTrue(smart_powerd.should_skip_usb_device("09", set()))
        # HID (03) excluded
        self.assertTrue(smart_powerd.should_skip_usb_device("00", {"03"}))
        # Wireless/Bluetooth (e0) excluded via interface OR device class
        self.assertTrue(smart_powerd.should_skip_usb_device("ef", {"e0"}))
        self.assertTrue(smart_powerd.should_skip_usb_device("e0", set()))
        # Audio (01) excluded
        self.assertTrue(smart_powerd.should_skip_usb_device("00", {"01", "03"}))
        # Case-insensitive
        self.assertTrue(smart_powerd.should_skip_usb_device("00", {"E0"}))
        # Non-HID webcam / vendor-specific devices allowed for autosuspend
        self.assertFalse(smart_powerd.should_skip_usb_device("ef", {"0e", "fe"}))
        self.assertFalse(smart_powerd.should_skip_usb_device("ff", {"ff"}))

    def test_desired_refresh_profile_3tier_and_hysteresis(self) -> None:
        # AC online -> always high (even if previously in low at 15%)
        self.assertEqual(
            smart_powerd.compute_desired_refresh_profile(
                enabled=True, ac_online=True, battery_pct=100, refresh_policy_enabled=True
            ),
            "high",
        )
        self.assertEqual(
            smart_powerd.compute_desired_refresh_profile(
                enabled=True,
                ac_online=True,
                battery_pct=15,
                refresh_policy_enabled=True,
                previous_profile="low",
            ),
            "high",
        )
        # Smart Optimize OFF or policy disabled -> high
        self.assertEqual(
            smart_powerd.compute_desired_refresh_profile(
                enabled=False, ac_online=False, battery_pct=10, refresh_policy_enabled=True
            ),
            "high",
        )
        self.assertEqual(
            smart_powerd.compute_desired_refresh_profile(
                enabled=True, ac_online=False, battery_pct=10, refresh_policy_enabled=False
            ),
            "high",
        )
        # Desktop without battery -> high
        self.assertEqual(
            smart_powerd.compute_desired_refresh_profile(
                enabled=True, ac_online=False, battery_pct=0, battery_present=False
            ),
            "high",
        )
        # Unplugged >= 20% -> mid
        self.assertEqual(
            smart_powerd.compute_desired_refresh_profile(
                enabled=True, ac_online=False, battery_pct=80, previous_profile="high"
            ),
            "mid",
        )
        self.assertEqual(
            smart_powerd.compute_desired_refresh_profile(
                enabled=True, ac_online=False, battery_pct=20, previous_profile="mid"
            ),
            "mid",
        )
        # Unplugged < 20% -> low
        self.assertEqual(
            smart_powerd.compute_desired_refresh_profile(
                enabled=True, ac_online=False, battery_pct=19, previous_profile="mid"
            ),
            "low",
        )
        # +2% exit hysteresis on battery: 20% and 21% stay low, 22% exits to mid
        self.assertEqual(
            smart_powerd.compute_desired_refresh_profile(
                enabled=True, ac_online=False, battery_pct=20, previous_profile="low"
            ),
            "low",
        )
        self.assertEqual(
            smart_powerd.compute_desired_refresh_profile(
                enabled=True, ac_online=False, battery_pct=21, previous_profile="low"
            ),
            "low",
        )
        self.assertEqual(
            smart_powerd.compute_desired_refresh_profile(
                enabled=True, ac_online=False, battery_pct=22, previous_profile="low"
            ),
            "mid",
        )

    @unittest.skipIf(
        sys.platform == "win32", "Linux PCI BDF directory names contain ':' illegal on NTFS"
    )
    def test_discover_dgpu_in_synthetic_sysfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pci = Path(tmp)
            # Integrated AMD GPU (boot_vga=1)
            igpu = pci / "0000:0b:00.0"
            igpu.mkdir()
            (igpu / "class").write_text("0x030000\n", encoding="utf-8")
            (igpu / "vendor").write_text("0x1002\n", encoding="utf-8")
            (igpu / "boot_vga").write_text("1\n", encoding="utf-8")

            # Discrete NVIDIA GPU (boot_vga=0) + sibling HDA audio (0x0403)
            dgpu = pci / "0000:0a:00.0"
            (dgpu / "drm" / "renderD129").mkdir(parents=True)
            (dgpu / "drm" / "card1").mkdir(parents=True)
            (dgpu / "class").write_text("0x030000\n", encoding="utf-8")
            (dgpu / "vendor").write_text("0x10de\n", encoding="utf-8")
            (dgpu / "boot_vga").write_text("0\n", encoding="utf-8")

            dgpu_audio = pci / "0000:0a:00.1"
            dgpu_audio.mkdir()
            (dgpu_audio / "class").write_text("0x040300\n", encoding="utf-8")
            (dgpu_audio / "vendor").write_text("0x10de\n", encoding="utf-8")

            found_pci, render, card, audio, vendor = smart_powerd.discover_dgpu_in(pci)
            self.assertEqual(found_pci, dgpu)
            self.assertEqual(render, "renderD129")
            self.assertEqual(card, "card1")
            self.assertEqual(audio, dgpu_audio)
            self.assertEqual(vendor, "0x10de")

    def test_rclone_mount_excluded_but_active_transfers_detected(self) -> None:
        # Persistent background daemons must NOT be in unconditional DOWNLOAD_IMMEDIATE_NAMES
        self.assertNotIn("rclone", smart_powerd.DOWNLOAD_IMMEDIATE_NAMES)
        self.assertNotIn("snapd", smart_powerd.DOWNLOAD_IMMEDIATE_NAMES)

        # FUSE mounts and servers are NOT active download triggers
        self.assertFalse(
            smart_powerd.is_rclone_download(
                ["/usr/bin/rclone", "mount", "gdrive:", "/mnt/drive", "--vfs-cache-mode", "writes"]
            )
        )
        self.assertFalse(
            smart_powerd.is_rclone_download(
                ["rclone", "--config", "/etc/rclone.conf", "mount", "remote:", "/mnt/r"]
            )
        )
        self.assertFalse(smart_powerd.is_rclone_download(["rclone", "cmount", "remote:", "/mnt/r"]))
        self.assertFalse(smart_powerd.is_rclone_download(["rclone", "serve", "http", "remote:"]))
        self.assertFalse(smart_powerd.is_rclone_download(["rclone", "rcd"]))
        self.assertFalse(smart_powerd.is_rclone_download(["rclone"]))

        # Active transfer subcommands ARE download triggers
        self.assertTrue(smart_powerd.is_rclone_download(["rclone", "copy", "remote:a", "/tmp/a"]))
        self.assertTrue(smart_powerd.is_rclone_download(["rclone", "sync", "remote:a", "/tmp/a"]))
        self.assertTrue(
            smart_powerd.is_rclone_download(
                ["rclone", "--transfers", "8", "move", "remote:a", "/tmp/a"]
            )
        )

    def test_dynamic_epp_resolution_and_governor_unlock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp) / "cpu0" / "cpufreq"
            cdir.mkdir(parents=True)
            gov_path = cdir / "scaling_governor"
            avail_gov_path = cdir / "scaling_available_governors"
            epp_path = cdir / "energy_performance_preference"
            avail_epp_path = cdir / "energy_performance_available_preferences"

            # Simulate amd-pstate-epp starting locked in performance governor
            gov_path.write_text("performance\n", encoding="utf-8")
            avail_gov_path.write_text("performance powersave\n", encoding="utf-8")
            epp_path.write_text("performance\n", encoding="utf-8")
            avail_epp_path.write_text("performance\n", encoding="utf-8")

            d = self._make_stub_daemon()
            d.cpufreq_dirs = [cdir]
            d.epp_paths = [epp_path]
            d.available_epps = {"performance"}
            d.available_governors = {"performance", "powersave"}

            # Simulate kernel unlocking full EPP list once scaling_governor becomes powersave
            orig_detect = d._detect_available_epps

            def dynamic_detect() -> set[str]:
                if smart_powerd.read_text(gov_path) == "powersave":
                    avail_epp_path.write_text(
                        "default performance balance_performance balance_power power\n",
                        encoding="utf-8",
                    )
                return orig_detect()

            d._detect_available_epps = dynamic_detect  # type: ignore[method-assign]

            d._set_cpu_performance("light", "balance_power")
            self.assertEqual(smart_powerd.read_text(gov_path), "powersave")
            self.assertEqual(smart_powerd.read_text(epp_path), "balance_power")
            self.assertIn("balance_power", d.available_epps)

            d._set_cpu_performance("download", "balance_performance")
            self.assertEqual(smart_powerd.read_text(epp_path), "balance_performance")


class TestCrossWorkerContracts(unittest.TestCase):
    """Integration-contract checks across helper, ctl, daemon, sudoers, and install.sh."""

    def test_control_command_allowlist_identical_across_all_components(self) -> None:
        expected = set(spc.VALID_CONTROL_COMMANDS)
        self.assertEqual(
            expected,
            {
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
            },
        )

        # 1. smart-powerd-helper.py
        self.assertEqual(set(smart_power_helper.VALID_COMMANDS), expected)

        # 2. smart-powerctl.py
        self.assertEqual(set(smart_powerctl.VALID_HELPER_SET), expected)
        for direct in ("on", "off", "toggle", "reload"):
            self.assertEqual(smart_powerctl.resolve_control_command([direct]), direct)
        for sub in ("on", "off", "toggle"):
            self.assertEqual(
                smart_powerctl.resolve_control_command(["brightness", sub]),
                f"brightness-{sub}",
            )
            self.assertEqual(
                smart_powerctl.resolve_control_command(["webcam-als", sub]),
                f"webcam-als-{sub}",
            )
        self.assertIsNone(smart_powerctl.resolve_control_command(["--arbitrary-flag"]))

        # 3. smart-powerd.sudoers template
        sudoers_text = (REPO_ROOT / "smart-powerd.sudoers").read_text(encoding="utf-8")
        sudoers_cmds = set(
            re.findall(r"/usr/local/libexec/smart-powerd-helper\s+([a-z-]+)", sudoers_text)
        )
        self.assertEqual(sudoers_cmds, expected)

        # 4. install.sh HELPER_CMDS array
        install_text = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
        match = re.search(r"HELPER_CMDS=\((.*?)\)", install_text, re.S)
        self.assertIsNotNone(match, "HELPER_CMDS array not found in install.sh")
        assert match is not None
        install_cmds = set(match.group(1).split())
        self.assertEqual(install_cmds, expected)

        # 5. smart-powerd.py handle_command accepts all 10 and rejects unknown
        d = smart_powerd.SmartPowerDaemon.__new__(smart_powerd.SmartPowerDaemon)
        d.enabled = True
        d.current_mode = "light"
        d.current_reason = "idle"
        d.config = copy.deepcopy(spc.DEFAULT_CONFIG)
        d.state = {"enabled": True, "brightness_enabled": None, "webcam_als_enabled": None}
        d.last_mode_activity_ts = {}
        d.last_download_pids = []
        d.last_heavy_pids = []
        d._force_reapply = False
        d.config_path = Path("/nonexistent/config.toml")
        d._save_state = lambda: None
        d._read_battery_telemetry = lambda: {
            "battery_present": True,
            "ac_online": True,
            "battery_status": "Full",
            "battery_pct": 100,
            "battery_watts": 0.0,
        }
        d._apply_mode = lambda mode, *_a, **_kw: setattr(d, "current_mode", mode)
        d._publish_status = lambda *_a, **_kw: None

        for cmd in sorted(expected):
            resp = d.handle_command(cmd)
            self.assertTrue(resp.get("ok"), f"Daemon rejected valid command '{cmd}': {resp}")

        bad_resp = d.handle_command("rm -rf /")
        self.assertFalse(bad_resp.get("ok"))

    @unittest.skipIf(sys.platform == "win32", "POSIX bash syntax check")
    def test_shell_scripts_syntax_valid(self) -> None:
        for script in ("install.sh", "uninstall.sh", "scripts/build-release.sh"):
            res = subprocess.run(
                ["bash", "-n", str(REPO_ROOT / script)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, f"bash -n failed for {script}: {res.stderr}")

    def test_python_files_parse_cleanly(self) -> None:
        for py_file in sorted(
            p for p in REPO_ROOT.rglob("*.py") if ".git" not in p.parts
        ):
            src = py_file.read_text(encoding="utf-8")
            ast.parse(src, filename=str(py_file))

    def test_no_charge_threshold_writes_in_codebase(self) -> None:
        write_re = re.compile(
            r"(write_text|write_bytes|open\([^)]*['\"]w).*(charge_control|charge_start|charge_stop|charge_behaviour|conservation_mode)",
            re.IGNORECASE,
        )
        for path in sorted(REPO_ROOT.iterdir()):
            if path.suffix in {".py", ".sh"}:
                txt = path.read_text(encoding="utf-8", errors="ignore")
                self.assertIsNone(
                    write_re.search(txt),
                    f"Forbidden write to battery charge threshold found in {path.name}",
                )


if __name__ == "__main__":
    unittest.main()
