#!/usr/bin/env python3
"""
Cross-platform unit tests for windows/smart_power_win.py.

Runs cleanly on BOTH Linux and Windows CI without requiring PyQt6, psutil, or
Win32 DLL access.

Run with:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_DIR = REPO_ROOT / "windows"
for _p in (str(REPO_ROOT), str(WINDOWS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import display_refresh as dr  # noqa: E402
import smart_power_win as spw  # noqa: E402


class TestWindowsRefreshPolicy(unittest.TestCase):
    """Tests for win_devmodes_to_modes, 3-tier mode selection, and battery hysteresis."""

    def test_win_devmodes_to_modes_deduplicates_and_filters_bpp(self) -> None:
        raw = [
            # Legacy 16-bit modes should be dropped when 32-bit modes exist
            (2560, 1600, 60, 16),
            # 32-bit panel modes (300/240/60 at native res + lower res)
            (2560, 1600, 300, 32),
            (2560, 1600, 300, 32),  # duplicate
            (2560, 1600, 240, 32),
            (2560, 1600, 60, 32),
            (1920, 1200, 300, 32),
            (1920, 1200, 60, 32),
            # Dict format & ctypes DEVMODEW struct input
            {"width": 2560, "height": 1600, "refresh_hz": 240.0, "bpp": 32},
        ]
        cur_dm = spw.DEVMODEW()
        cur_dm.dmPelsWidth = 2560
        cur_dm.dmPelsHeight = 1600
        cur_dm.dmBitsPerPel = 32
        cur_dm.dmDisplayFrequency = 300

        info = spw.win_devmodes_to_modes(raw, current_mode=cur_dm, output_name=r"\\.\DISPLAY1")
        self.assertEqual(info.output_name, "DISPLAY1")
        self.assertEqual(info.backend, "win32")
        self.assertIsNotNone(info.current_mode)
        assert info.current_mode is not None
        self.assertEqual((info.current_mode.width, info.current_mode.height), (2560, 1600))
        self.assertAlmostEqual(info.current_mode.refresh_hz, 300.0, places=2)

        # Native resolution has exactly 3 deduplicated modes: 300, 240, 60
        native_rates = sorted(
            int(round(m.refresh_hz))
            for m in info.modes
            if (m.width, m.height) == (2560, 1600)
        )
        self.assertEqual(native_rates, [60, 240, 300])

        # Verify 3-tier target selection via shared display_refresh logic
        self.assertIsNone(spw.select_windows_target_mode(info, "high"))
        mid_target = spw.select_windows_target_mode(info, "mid")
        low_target = spw.select_windows_target_mode(info, "low")
        self.assertIsNotNone(mid_target)
        self.assertIsNotNone(low_target)
        assert mid_target is not None and low_target is not None
        self.assertAlmostEqual(mid_target.refresh_hz, 240.0, places=2)
        self.assertAlmostEqual(low_target.refresh_hz, 60.0, places=2)

    def test_win_3tier_panel_matrix(self) -> None:
        cases = [
            ([300, 240, 60], 300, 240, 60),
            ([240, 165, 60], 240, 165, 60),
            ([165, 120, 60], 165, 120, 60),
            ([144, 90, 60], 144, 90, 60),
            ([144, 60], 144, 144, 60),
            ([120, 60], 120, 120, 60),
            ([60], 60, 60, 60),
        ]
        for rates, exp_high, exp_mid, exp_low in cases:
            with self.subTest(rates=rates):
                raw = [(2560, 1600, r, 32) for r in rates]
                info = spw.win_devmodes_to_modes(raw, current_mode=(2560, 1600, rates[0], 32))
                tiers = dr.choose_refresh_modes(info, ac_hz=0, battery_hz=0, low_hz=60)
                self.assertEqual(int(round(tiers["high"].refresh_hz)), exp_high)
                self.assertEqual(int(round(tiers["mid"].refresh_hz)), exp_mid)
                self.assertEqual(int(round(tiers["low"].refresh_hz)), exp_low)

    def test_compute_windows_refresh_profile_and_hysteresis(self) -> None:
        # Charging / AC always -> high (even at 10% battery and prev='low')
        self.assertEqual(
            spw.compute_windows_refresh_profile(True, ac_online=True, battery_pct=100), "high"
        )
        self.assertEqual(
            spw.compute_windows_refresh_profile(
                True, ac_online=True, battery_pct=10, prev_profile="low"
            ),
            "high",
        )

        # Smart Optimize OFF or policy disabled -> high
        self.assertEqual(
            spw.compute_windows_refresh_profile(False, ac_online=False, battery_pct=10), "high"
        )
        self.assertEqual(
            spw.compute_windows_refresh_profile(
                True, ac_online=False, battery_pct=10, refresh_policy_enabled=False
            ),
            "high",
        )

        # Desktop (no battery) -> high
        self.assertEqual(
            spw.compute_windows_refresh_profile(
                True, ac_online=False, battery_pct=None, battery_present=False
            ),
            "high",
        )

        # Unplugged >= 20% -> mid
        self.assertEqual(
            spw.compute_windows_refresh_profile(
                True, ac_online=False, battery_pct=85, prev_profile="high"
            ),
            "mid",
        )
        self.assertEqual(
            spw.compute_windows_refresh_profile(
                True, ac_online=False, battery_pct=20, prev_profile="mid"
            ),
            "mid",
        )

        # Unplugged < 20% -> low
        self.assertEqual(
            spw.compute_windows_refresh_profile(
                True, ac_online=False, battery_pct=19, prev_profile="mid"
            ),
            "low",
        )

        # +2% hysteresis while unplugged: 20% and 21% remain 'low', 22% exits to 'mid'
        self.assertEqual(
            spw.compute_windows_refresh_profile(
                True, ac_online=False, battery_pct=20, prev_profile="low"
            ),
            "low",
        )
        self.assertEqual(
            spw.compute_windows_refresh_profile(
                True, ac_online=False, battery_pct=21, prev_profile="low"
            ),
            "low",
        )
        self.assertEqual(
            spw.compute_windows_refresh_profile(
                True, ac_online=False, battery_pct=22, prev_profile="low"
            ),
            "mid",
        )


class TestWindowsWorkloadClassifier(unittest.TestCase):
    """Tests for classify_windows_workload and 12s downgrade cooldown."""

    def test_idle_and_system_processes_stay_light(self) -> None:
        procs = [
            {"pid": 4, "ppid": 0, "name": "System", "cmdline": [], "cpu_pct": 1.0, "exe": ""},
            {"pid": 500, "ppid": 4, "name": "svchost.exe", "cmdline": [], "cpu_pct": 5.0, "exe": ""},
            {"pid": 600, "ppid": 4, "name": "dwm.exe", "cmdline": [], "cpu_pct": 8.0, "exe": ""},
            {"pid": 700, "ppid": 600, "name": "explorer.exe", "cmdline": [], "cpu_pct": 2.0, "exe": ""},
            # Idle Windows Installer service (/V) must NOT trigger Download mode
            {"pid": 800, "ppid": 500, "name": "msiexec.exe", "cmdline": ["msiexec.exe", "/V"], "cpu_pct": 0.0, "exe": ""},
            # Idle rclone mount must NOT trigger Download mode
            {"pid": 900, "ppid": 700, "name": "rclone.exe", "cmdline": ["rclone.exe", "mount", "gdrive:", "G:"], "cpu_pct": 0.0, "exe": ""},
            # Steam web helper and Electron renderer must NOT trigger Heavy mode
            {"pid": 1000, "ppid": 700, "name": "steam.exe", "cmdline": ["steam.exe"], "cpu_pct": 1.0, "exe": ""},
            {"pid": 1001, "ppid": 1000, "name": "steamwebhelper.exe", "cmdline": ["steamwebhelper.exe"], "cpu_pct": 4.0, "exe": ""},
            {"pid": 1100, "ppid": 700, "name": "code.exe", "cmdline": ["code.exe", "--type=renderer"], "cpu_pct": 40.0, "exe": ""},
        ]
        res = spw.classify_windows_workload(procs, total_cpu_pct=5.0, max_core_cpu_pct=15.0, rx_mib_s=0.1)
        self.assertEqual(res["mode"], "light")
        self.assertFalse(res["has_download"])
        self.assertEqual(res["heavy_pids"], [])
        self.assertEqual(res["download_pids"], [])

    def test_compilers_games_and_heavy_priority(self) -> None:
        # Immediate compiler trigger + boostable PID
        res_cl = spw.classify_windows_workload(
            [{"pid": 2001, "ppid": 100, "name": "cl.exe", "cmdline": ["cl.exe", "/O2"], "cpu_pct": 10.0, "exe": ""}]
        )
        self.assertEqual(res_cl["mode"], "heavy")
        self.assertIn(2001, res_cl["heavy_pids"])
        self.assertIn(2001, res_cl["boost_pids"])

        # Steam child game detected via parent PID table
        res_game = spw.classify_windows_workload(
            [
                {"pid": 3000, "ppid": 1, "name": "steam.exe", "cmdline": ["steam.exe"], "cpu_pct": 1.0, "exe": ""},
                {
                    "pid": 3001,
                    "ppid": 3000,
                    "name": "CyberGame.exe",
                    "cmdline": ["CyberGame.exe"],
                    "cpu_pct": 20.0,
                    "exe": r"C:\Games\SteamApps\common\CyberGame\CyberGame.exe",
                },
            ]
        )
        self.assertEqual(res_game["mode"], "heavy")
        self.assertIn("game:", res_game["reason"])
        self.assertIn(3001, res_game["boost_pids"])

        # Heavy takes #1 priority over Download when both are active, while keeping has_download=True
        res_both = spw.classify_windows_workload(
            [
                {"pid": 4001, "ppid": 1, "name": "rustc.exe", "cmdline": ["rustc.exe"], "cpu_pct": 30.0, "exe": ""},
                {"pid": 4002, "ppid": 1, "name": "winget.exe", "cmdline": ["winget.exe", "upgrade"], "cpu_pct": 1.0, "exe": ""},
            ]
        )
        self.assertEqual(res_both["mode"], "heavy")
        self.assertTrue(res_both["has_download"])
        self.assertIn(4001, res_both["heavy_pids"])
        self.assertIn(4002, res_both["download_pids"])

        # Cargo build -> heavy, cargo fetch -> download
        res_cb = spw.classify_windows_workload(
            [{"pid": 4100, "ppid": 1, "name": "cargo.exe", "cmdline": ["cargo.exe", "build"], "cpu_pct": 5.0, "exe": ""}]
        )
        self.assertEqual(res_cb["mode"], "heavy")
        res_cf = spw.classify_windows_workload(
            [{"pid": 4101, "ppid": 1, "name": "cargo.exe", "cmdline": ["cargo.exe", "fetch"], "cpu_pct": 1.0, "exe": ""}]
        )
        self.assertEqual(res_cf["mode"], "download")

        # CPU-gated python.exe >= 25% triggers heavy; below 25% stays light
        self.assertEqual(
            spw.classify_windows_workload(
                [{"pid": 4200, "ppid": 1, "name": "python.exe", "cmdline": ["python.exe", "train.py"], "cpu_pct": 30.0, "exe": ""}]
            )["mode"],
            "heavy",
        )
        self.assertEqual(
            spw.classify_windows_workload(
                [{"pid": 4200, "ppid": 1, "name": "python.exe", "cmdline": ["python.exe", "idle.py"], "cpu_pct": 4.0, "exe": ""}]
            )["mode"],
            "light",
        )

    def test_download_triggers_and_network_rx_thresholds(self) -> None:
        # Active msiexec /i triggers download
        res_msi = spw.classify_windows_workload(
            [{"pid": 5001, "ppid": 1, "name": "msiexec.exe", "cmdline": ["msiexec.exe", "/i", "app.msi"], "cpu_pct": 1.0, "exe": ""}]
        )
        self.assertEqual(res_msi["mode"], "download")

        # Active rclone copy triggers download
        res_rc = spw.classify_windows_workload(
            [{"pid": 5002, "ppid": 1, "name": "rclone.exe", "cmdline": ["rclone.exe", "copy", "remote:a", "D:\\a"], "cpu_pct": 1.0, "exe": ""}]
        )
        self.assertEqual(res_rc["mode"], "download")

        # Docker pull triggers download
        res_dk = spw.classify_windows_workload(
            [{"pid": 5003, "ppid": 1, "name": "docker.exe", "cmdline": ["docker.exe", "pull", "python:3.12"], "cpu_pct": 1.0, "exe": ""}]
        )
        self.assertEqual(res_dk["mode"], "download")

        # Network RX: 2.0 MiB/s single tick stays light, 2 consecutive ticks triggers download
        self.assertEqual(spw.classify_windows_workload([], rx_mib_s=2.0, rx_streak=1)["mode"], "light")
        self.assertEqual(spw.classify_windows_workload([], rx_mib_s=2.0, rx_streak=2)["mode"], "download")
        # Immediate spike >= 3.0 MiB/s triggers download on tick 1
        self.assertEqual(spw.classify_windows_workload([], rx_mib_s=3.5, rx_streak=1)["mode"], "download")

        # Extra configured process names (with or without .exe)
        cfg = {"workloads": {"extra_heavy_processes": ["custom_solver"], "extra_download_processes": ["my_dl"]}}
        self.assertEqual(
            spw.classify_windows_workload(
                [{"pid": 6001, "ppid": 1, "name": "custom_solver.exe", "cmdline": [], "cpu_pct": 1.0, "exe": ""}],
                config=cfg,
            )["mode"],
            "heavy",
        )
        self.assertEqual(
            spw.classify_windows_workload(
                [{"pid": 6002, "ppid": 1, "name": "my_dl.exe", "cmdline": [], "cpu_pct": 1.0, "exe": ""}],
                config=cfg,
            )["mode"],
            "download",
        )

    def test_apply_mode_cooldown_12s_hysteresis(self) -> None:
        # Immediate upgrade light -> heavy at t=100
        mode, ts = spw.apply_mode_cooldown("heavy", "light", now=100.0, last_higher_ts=0.0, cooldown_sec=12.0)
        self.assertEqual(mode, "heavy")
        self.assertEqual(ts, 100.0)

        # Downgrade held at t=105 and t=111.9
        mode, ts = spw.apply_mode_cooldown("light", "heavy", now=105.0, last_higher_ts=ts, cooldown_sec=12.0)
        self.assertEqual(mode, "heavy")
        mode, ts = spw.apply_mode_cooldown("light", "heavy", now=111.9, last_higher_ts=ts, cooldown_sec=12.0)
        self.assertEqual(mode, "heavy")

        # Clean downgrade at t=112.0
        mode, ts = spw.apply_mode_cooldown("light", "heavy", now=112.0, last_higher_ts=ts, cooldown_sec=12.0)
        self.assertEqual(mode, "light")
        self.assertEqual(ts, 0.0)


class TestWindowsUtilities(unittest.TestCase):
    """Tests for GUID round-trips, powercfg parsing, frozen bundle lookup, and config loading."""

    def test_guid_round_trip(self) -> None:
        for g_str in (
            spw.OVERLAY_BETTER_BATTERY,
            spw.OVERLAY_BALANCED,
            spw.OVERLAY_BEST_PERFORMANCE,
            spw.SCHEME_BALANCED,
            spw.SUBGROUP_WIFI,
            spw.SETTING_WIFI_PS,
        ):
            g_struct = spw.guid_from_string(g_str)
            self.assertEqual(spw.guid_to_string(g_struct), g_str.lower())

    def test_parse_powercfg_ac_dc_indices_locale_independent(self) -> None:
        # Non-English localized output still exposes hex AC/DC indices as last two 0xXXXXXXXX values
        sample = """
    Alt Grup GUID: 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1  (Kablosuz Bagdastirici Ayarlari)
      Guc Ayari GUID: 12bbebe6-58d6-4636-95bb-3217ef867c1a  (Guc Tasarrufu Modu)
        Olasi Ayar Dizini: 000
        Olasi Ayar Dizini: 002
      Gecerli AC Guc Ayari Dizini: 0x00000000
      Gecerli DC Guc Ayari Dizini: 0x00000002
"""
        ac, dc = spw.parse_powercfg_ac_dc_indices(sample)
        self.assertEqual((ac, dc), (0, 2))
        self.assertEqual(spw.parse_powercfg_ac_dc_indices(""), (None, None))

    def test_load_windows_config_with_partial_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.toml"
            cfg_path.write_text(
                "[battery_tricks]\nlow_battery_refresh_pct = 25\nbattery_refresh_hz = 120\n",
                encoding="utf-8",
            )
            loaded = spw.load_windows_config(cfg_path)
            self.assertTrue(loaded["battery_tricks"]["refresh_policy_enabled"])
            self.assertEqual(loaded["battery_tricks"]["low_battery_refresh_pct"], 25)
            self.assertEqual(loaded["battery_tricks"]["battery_refresh_hz"], 120)
            self.assertEqual(loaded["battery_tricks"]["low_refresh_hz"], 60)
            self.assertEqual(
                set(loaded["brightness"].keys()),
                {
                    "enabled",
                    "battery_multiplier",
                    "min_pct",
                    "max_pct",
                    "smooth_step_pct",
                    "manual_override_pause_sec",
                    "schedule",
                },
            )

    def test_version_flag_and_bundled_config_lookup(self) -> None:
        self.assertEqual(spw.VERSION, "2.3.0")
        self.assertEqual(spw.main(["--version"]), 0)
        bundled = spw.find_bundled_config_example()
        self.assertIsNotNone(bundled)
        assert bundled is not None
        self.assertTrue(bundled.is_file())
        self.assertEqual(bundled.name, "config.example.toml")

    def test_charge_limit_alert_once_per_charge_cycle(self) -> None:
        # Below 80% while charging -> do not notify
        fire, flag = spw.evaluate_charge_limit_alert(
            charge_limit_enabled=True,
            charge_limit_hw_supported=False,
            charge_limit_notify_fallback=True,
            charge_limit_pct=80,
            ac_online=True,
            battery_present=True,
            battery_status="Charging",
            battery_pct=79,
            already_notified=False,
        )
        self.assertEqual((fire, flag), (False, False))

        # Reaches 80% while charging -> fires once and latches flag
        fire, flag = spw.evaluate_charge_limit_alert(
            charge_limit_enabled=True,
            charge_limit_hw_supported=False,
            charge_limit_notify_fallback=True,
            charge_limit_pct=80,
            ac_online=True,
            battery_present=True,
            battery_status="Charging",
            battery_pct=80,
            already_notified=False,
        )
        self.assertEqual((fire, flag), (True, True))

        # Subsequent tick at 81% does not spam notification
        fire, flag = spw.evaluate_charge_limit_alert(
            charge_limit_enabled=True,
            charge_limit_hw_supported=False,
            charge_limit_notify_fallback=True,
            charge_limit_pct=80,
            ac_online=True,
            battery_present=True,
            battery_status="Charging",
            battery_pct=81,
            already_notified=True,
        )
        self.assertEqual((fire, flag), (False, True))

        # Unplugging resets flag for the next charge cycle
        fire, flag = spw.evaluate_charge_limit_alert(
            charge_limit_enabled=True,
            charge_limit_hw_supported=False,
            charge_limit_notify_fallback=True,
            charge_limit_pct=80,
            ac_online=False,
            battery_present=True,
            battery_status="Discharging",
            battery_pct=81,
            already_notified=True,
        )
        self.assertEqual((fire, flag), (False, False))

        # Hardware-enforced backend suppresses alert-only notification
        fire, flag = spw.evaluate_charge_limit_alert(
            charge_limit_enabled=True,
            charge_limit_hw_supported=True,
            charge_limit_notify_fallback=True,
            charge_limit_pct=80,
            ac_online=True,
            battery_present=True,
            battery_status="Charging",
            battery_pct=85,
            already_notified=False,
        )
        self.assertEqual((fire, flag), (False, False))


if __name__ == "__main__":
    unittest.main()
