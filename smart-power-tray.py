#!/usr/bin/env python3
"""
smart-power-tray: Lightweight PyQt6 system tray controller for smart-powerd.

Runs inside the user's graphical session (Wayland or X11) to coordinate:
  - Passwordless Smart Optimize toggle (left-click & right-click menu)
  - Live battery percentage tray badge + 80% hardware charge-limit control & fallback alert
  - Live telemetry tooltip & menu (mode, trigger, battery wattage, charge limit, EPP, Wi-Fi PS, dGPU)
  - Portable dGPU-safe day/night circadian adaptive brightness
  - Multi-DE 3-tier laptop display refresh rate switching (AC max Hz, Battery mid Hz, Low Battery 60 Hz)
  - Per-user QLocalServer single-instance lock & refresh IPC
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_LIB_DIR = Path("/usr/local/lib/smart-power")
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
if str(_LIB_DIR) not in sys.path:
    sys.path.append(str(_LIB_DIR))

from PyQt6.QtCore import QPointF, QProcess, QRectF, QSocketNotifier, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

try:
    from smart_power_config import load_config as _shared_load_config
except ImportError:
    _shared_load_config = None

from adaptive_brightness import (
    AdaptiveBrightnessController,
    normalize_brightness_config,
)
from display_refresh import DisplayRefreshController

STATUS_FILE = Path("/run/smart-powerd/status.json")
CTL_BIN = "/usr/local/bin/smart-powerctl"
POLL_MS = 2500
ICON_CACHE_MAX = 64

MODE_COLORS = {
    "light": "#2ecc71",     # Emerald green
    "download": "#3498db",  # Sky blue
    "heavy": "#f39c12",     # Amber/orange
    "off": "#7f8c8d",       # Muted gray
}

MODE_LABELS = {
    "light": "Light",
    "download": "Download",
    "heavy": "Heavy",
    "off": "Unrestricted (OFF)",
}


def get_runtime_dir() -> Path | None:
    """Return user's $XDG_RUNTIME_DIR if present and writable."""
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg:
        path = Path(xdg)
        if path.is_dir():
            return path
    return None


def get_display_telemetry_path() -> Path:
    """Return path for user-session display telemetry JSON."""
    rdir = get_runtime_dir()
    if rdir is not None:
        return rdir / "smart-power-display.json"
    return Path(f"/tmp/smart-power-display-{os.getuid()}.json")


def get_tray_socket_path() -> str:
    """Return per-user QLocalServer Unix socket path for single-instance lock + IPC."""
    rdir = get_runtime_dir()
    if rdir is not None:
        return str(rdir / "smart-power-tray.sock")
    return f"/tmp/smart-power-tray-{os.getuid()}.sock"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON telemetry to ``path`` using temp-file + os.replace."""
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
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        pass


def load_local_config() -> dict[str, Any]:
    """Load config via smart_power_config with safe fallback defaults."""
    if _shared_load_config is not None:
        try:
            cfg = _shared_load_config()
            if isinstance(cfg, dict):
                return cfg
        except Exception:
            pass
    return {
        "battery_tricks": {
            "refresh_policy_enabled": True,
            "ac_refresh_hz": 0,
            "battery_refresh_hz": 0,
            "low_battery_refresh_pct": 20,
            "low_refresh_hz": 60,
        },
        "battery_charge": {
            "limit_enabled": True,
            "stop_threshold": 80,
            "start_threshold": 75,
            "notify_when_no_hw_limit": True,
        },
        "brightness": normalize_brightness_config(None),
    }


def render_tray_icon(
    mode: str,
    enabled: bool,
    battery_pct: int | None = None,
    charging: bool = False,
) -> QIcon:
    """
    Render a crisp 64x64 tray badge icon directly with QPainter.

    When ``battery_pct`` is provided, renders a high-contrast mode-colored rounded
    badge displaying the bold battery percentage number (plus a bright charging
    accent bar when ``charging=True``). Falls back to the vector bolt/arrow/off
    circle badge when no battery is present or the daemon is offline.
    """
    key = mode if enabled else "off"
    bg_hex = MODE_COLORS.get(key, MODE_COLORS["off"])

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    if battery_pct is not None:
        pct_val = max(0, min(100, int(round(battery_pct))))
        badge_rect = QRectF(3.0, 3.0, 58.0, 58.0)

        painter.setPen(QPen(QColor("#1c242b"), 3.0))
        painter.setBrush(QColor(bg_hex))
        painter.drawRoundedRect(badge_rect, 14.0, 14.0)

        font = QFont("Sans Serif")
        font.setStyleHint(QFont.StyleHint.SansSerif)
        font.setBold(True)
        font.setWeight(QFont.Weight.ExtraBold)
        font.setPixelSize(25 if pct_val >= 100 else 35)
        painter.setFont(font)

        text = str(pct_val)
        text_rect = QRectF(3.0, 2.0, 58.0, 48.0) if charging else QRectF(3.0, 3.0, 58.0, 58.0)

        # Subtle dark drop shadow for sharp legibility at 22px panel scale
        painter.setPen(QColor(0, 0, 0, 155))
        painter.drawText(
            text_rect.translated(1.2, 1.8),
            int(Qt.AlignmentFlag.AlignCenter),
            text,
        )
        painter.setPen(QColor("#ffffff"))
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignCenter), text)

        if charging:
            # High-contrast golden charging indicator bar along bottom edge
            painter.setPen(QPen(QColor("#1c242b"), 1.6))
            painter.setBrush(QColor("#ffe066"))
            painter.drawRoundedRect(QRectF(14.0, 50.0, 36.0, 6.5), 3.0, 3.0)

        painter.end()
        return QIcon(pixmap)

    # Fallback vector circle badge (no battery / daemon offline)
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
        # Power symbol with diagonal slash
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(18, 18, 28, 28, 130 * 16, 280 * 16)
        painter.drawLine(32, 13, 32, 29)
        painter.setPen(QPen(QColor("#e74c3c"), 4.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(16, 48, 48, 16)
    elif key == "download":
        # Crisp downward arrow + tray bar
        painter.drawLine(32, 16, 32, 39)
        arrow = QPolygonF([QPointF(21, 31), QPointF(32, 43), QPointF(43, 31)])
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolyline(arrow)
        painter.drawLine(19, 48, 45, 48)
    elif key == "heavy":
        # Bold performance lightning bolt
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
        # Light mode: sleek eco-bolt
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


def _format_hz(hz: float | None) -> str:
    if hz is None:
        return "--"
    if abs(hz - round(hz)) < 0.15:
        return f"{int(round(hz))} Hz"
    return f"{hz:.1f} Hz"


def _truncate(text: str, max_len: int = 52) -> str:
    clean = (text or "").strip()
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"


def evaluate_charge_limit_alert(
    st: dict[str, Any],
    already_sent: bool,
) -> tuple[bool, bool]:
    """
    Evaluate once-per-charge-cycle alert-only notification state when no hardware
    EC charge cutoff is available. Returns ``(should_fire_now, next_already_sent)``.
    """
    bat_present = bool(st.get("battery_present", True))
    ac_online = SmartPowerTray._is_ac_online(st)
    bat_status = str(st.get("battery_status", "")).strip().lower()
    bat_pct = int(round(float(st.get("battery_pct", 0) or 0)))
    limit_on = bool(st.get("charge_limit_enabled", True))
    limit_pct = int(st.get("charge_limit_pct", 80) or 80)
    hw_sup = bool(st.get("charge_limit_hw_supported", False))
    notify_fb = bool(st.get("charge_limit_notify_fallback", True))

    if not bat_present or not ac_online or not limit_on or bat_pct < (limit_pct - 2):
        return False, False

    if (
        not already_sent
        and not hw_sup
        and notify_fb
        and bat_status == "charging"
        and bat_pct >= limit_pct
    ):
        return True, True

    return False, already_sent


class SmartPowerTray:
    """System tray controller integrating daemon status, circadian brightness, and refresh rate."""

    def __init__(self, app: QApplication, lock_server: QLocalServer) -> None:
        self.app = app
        self.lock_server = lock_server
        self._active_procs: list[QProcess] = []
        self._cleaned_up = False

        self.icon_cache: dict[tuple[str, int | None, bool], QIcon] = {}
        self.last_icon_key: tuple[str, int | None, bool] | None = None
        self.last_enabled: bool = True
        self.last_status: dict[str, Any] | None = None
        self._charge_alert_sent: bool = False
        self._last_telemetry_written: dict[str, Any] | None = None
        self._local_config: dict[str, Any] = load_local_config()
        self._telemetry_path = get_display_telemetry_path()

        self.brightness_ctrl = AdaptiveBrightnessController()
        self.refresh_ctrl = DisplayRefreshController()

        self.tray = QSystemTrayIcon()
        self.tray.activated.connect(self.on_activated)

        # Build right-click context menu
        self.menu = QMenu()
        self.act_state = self._make_info_action("Smart Optimize: Loading...")
        self.act_reason = self._make_info_action("Trigger: --")
        self.act_watt = self._make_info_action("Battery: --")
        self.act_charge_limit = self._make_info_action("Charge Limit: --")
        self.act_cpu = self._make_info_action("CPU EPP: -- | Wi-Fi PS: --")
        self.act_dgpu = self._make_info_action("dGPU: --")
        self.act_refresh = self._make_info_action("Refresh: --")
        self.act_brightness = self._make_info_action("Brightness: --")

        for info_act in (
            self.act_state,
            self.act_reason,
            self.act_watt,
            self.act_charge_limit,
            self.act_cpu,
            self.act_dgpu,
            self.act_refresh,
            self.act_brightness,
        ):
            self.menu.addAction(info_act)

        self.menu.addSeparator()

        self.act_toggle = QAction("Turn Smart Optimize OFF", self.menu)
        self.act_toggle.triggered.connect(self.trigger_toggle)
        self.menu.addAction(self.act_toggle)

        self.act_charge_limit_toggle = QAction("80% Charge Limit: ON", self.menu)
        self.act_charge_limit_toggle.triggered.connect(self.trigger_charge_limit_toggle)
        self.menu.addAction(self.act_charge_limit_toggle)

        self.act_brightness_toggle = QAction("Auto Brightness: ON", self.menu)
        self.act_brightness_toggle.triggered.connect(self.trigger_brightness_toggle)
        self.menu.addAction(self.act_brightness_toggle)

        self.menu.addSeparator()

        self.act_refresh_now = QAction("Refresh Status", self.menu)
        self.act_refresh_now.triggered.connect(self.trigger_refresh_now)
        self.menu.addAction(self.act_refresh_now)

        self.act_quit = QAction("Quit Tray", self.menu)
        self.act_quit.triggered.connect(self.request_quit)
        self.menu.addAction(self.act_quit)

        self.tray.setContextMenu(self.menu)

        # Hook QLocalServer IPC for `refresh` requests
        self.lock_server.newConnection.connect(self._on_ipc_connection)

        self.app.aboutToQuit.connect(self.cleanup)

        self.refresh_status()
        self.tray.show()

        self.timer = QTimer(self.app)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(POLL_MS)

    def _make_info_action(self, text: str) -> QAction:
        act = QAction(text, self.menu)
        act.setEnabled(False)
        return act

    def _get_icon(
        self,
        mode: str,
        enabled: bool,
        battery_pct: int | None = None,
        charging: bool = False,
    ) -> QIcon:
        icon_mode = mode if enabled else "off"
        norm_pct = max(0, min(100, int(round(battery_pct)))) if battery_pct is not None else None
        cache_key = (icon_mode, norm_pct, bool(charging) if norm_pct is not None else False)
        if cache_key not in self.icon_cache:
            if len(self.icon_cache) >= ICON_CACHE_MAX:
                self.icon_cache.pop(next(iter(self.icon_cache)))
            self.icon_cache[cache_key] = render_tray_icon(
                mode,
                enabled,
                battery_pct=norm_pct,
                charging=cache_key[2],
            )
        return self.icon_cache[cache_key]

    @staticmethod
    def _read_status() -> dict[str, Any] | None:
        try:
            raw = STATUS_FILE.read_text(encoding="utf-8", errors="ignore")
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _effective_brightness_cfg(self, st: dict[str, Any] | None) -> dict[str, Any]:
        if isinstance(st, dict) and isinstance(st.get("brightness_config"), dict):
            return normalize_brightness_config(st["brightness_config"])
        return normalize_brightness_config(self._local_config.get("brightness"))

    @staticmethod
    def _is_ac_online(st: dict[str, Any] | None) -> bool:
        if not isinstance(st, dict):
            return True
        if isinstance(st.get("ac_online"), bool):
            return st["ac_online"]
        if not st.get("battery_present", True):
            return True
        bat_status = str(st.get("battery_status", "")).strip().lower()
        return bat_status != "discharging"

    def _resolve_ctl_command(self, args: list[str]) -> tuple[str, list[str]]:
        if os.access(CTL_BIN, os.X_OK):
            return CTL_BIN, args
        local_ctl = _SCRIPT_DIR / "smart-powerctl.py"
        if local_ctl.is_file():
            return sys.executable, [str(local_ctl), *args]
        return CTL_BIN, args

    def _run_ctl(self, args: list[str], on_done: Any = None) -> None:
        program, full_args = self._resolve_ctl_command(args)
        proc = QProcess(self.app)
        self._active_procs.append(proc)

        def _finished(_code: int, _status: QProcess.ExitStatus) -> None:
            if proc in self._active_procs:
                self._active_procs.remove(proc)
            proc.deleteLater()
            if callable(on_done):
                on_done()
            self.refresh_status()

        proc.finished.connect(_finished)
        proc.start(program, full_args)

    def _on_ipc_connection(self) -> None:
        while self.lock_server.hasPendingConnections():
            sock = self.lock_server.nextPendingConnection()
            if sock is None:
                continue
            sock.readyRead.connect(lambda s=sock: self._handle_ipc_payload(s))
            sock.disconnected.connect(sock.deleteLater)
            if sock.bytesAvailable() > 0:
                self._handle_ipc_payload(sock)

    def _handle_ipc_payload(self, sock: QLocalSocket) -> None:
        raw = bytes(sock.readAll()).decode("utf-8", errors="ignore").strip()
        if not raw:
            return
        cmd = raw
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    cmd = str(parsed.get("cmd", "")).strip()
            except ValueError:
                cmd = ""

        cmd_norm = cmd.lower().strip()
        if cmd_norm == "refresh":
            self.refresh_status(force_probe=True)
            reply = {"ok": True, "message": "status refreshed"}
        else:
            reply = {"ok": False, "error": f"unknown command: {cmd_norm}"}

        try:
            sock.write((json.dumps(reply) + "\n").encode("utf-8"))
            sock.flush()
            sock.disconnectFromServer()
        except Exception:
            pass

    def _build_display_telemetry(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        payload.update(self.brightness_ctrl.get_telemetry())
        payload.update(self.refresh_ctrl.get_telemetry())
        return payload

    def _publish_display_telemetry(self, force: bool = False) -> dict[str, Any]:
        telemetry = self._build_display_telemetry()
        if force or telemetry != self._last_telemetry_written:
            atomic_write_json(self._telemetry_path, telemetry)
            self._last_telemetry_written = dict(telemetry)
        return telemetry

    @staticmethod
    def _format_brightness_summary(telemetry: dict[str, Any], bcfg: dict[str, Any]) -> str:
        pct = telemetry.get("brightness_pct")
        source = str(telemetry.get("brightness_source") or "Unavailable")
        override_until = float(telemetry.get("manual_override_until") or 0.0)

        if pct is None:
            return f"Unavailable ({source})"

        if source == "Manual Pause":
            rem_min = max(1, int(round((override_until - time.time()) / 60.0)))
            return f"{pct}% (Manual Pause, {rem_min}m)"
        if not bcfg.get("enabled", True) or source == "Disabled":
            return f"{pct}% (Disabled)"
        return f"{pct}% ({source})"

    @staticmethod
    def _format_refresh_summary(
        telemetry: dict[str, Any],
        st: dict[str, Any] | None = None,
    ) -> str:
        out = telemetry.get("refresh_output")
        cur_hz = telemetry.get("refresh_hz")
        if not out or cur_hz is None:
            return "Unavailable"
        cur_str = _format_hz(cur_hz)

        if not isinstance(st, dict):
            return f"{cur_str} ({out})"

        enabled = bool(st.get("enabled", True))
        if not enabled:
            return f"{cur_str} (OFF)"

        profile = str(
            st.get("desired_refresh_profile")
            or telemetry.get("refresh_profile")
            or "high"
        ).strip().lower()
        if profile == "low":
            tier_label = "Low Battery"
        elif profile == "mid":
            tier_label = "Battery"
        else:
            ac_online = SmartPowerTray._is_ac_online(st)
            tier_label = "AC" if ac_online else "Battery"
        return f"{cur_str} ({tier_label})"

    @staticmethod
    def _format_battery_summary(st: dict[str, Any]) -> str:
        if not st.get("battery_present", True):
            return "No battery"
        pct = int(round(float(st.get("battery_pct", 0) or 0)))
        watts = float(st.get("battery_watts", 0.0) or 0.0)
        status_raw = str(st.get("battery_status", "Unknown")).strip() or "Unknown"
        status_label = status_raw[:1].upper() + status_raw[1:]
        return f"{pct}% — {status_label} ({watts:.1f} W)"

    @staticmethod
    def _format_charge_limit_summary(st: dict[str, Any]) -> str:
        if not st.get("battery_present", True):
            return "No battery"
        limit_on = bool(st.get("charge_limit_enabled", True))
        limit_pct = int(st.get("charge_limit_pct", 80) or 80)
        hw_sup = bool(st.get("charge_limit_hw_supported", False))
        active_val = st.get("charge_limit_active_value")

        if not limit_on:
            return "OFF (100%)"
        if hw_sup:
            val_str = str(active_val).strip() if active_val is not None else ""
            if (
                val_str
                and not val_str.isdigit()
                and val_str not in (f"{limit_pct}%", str(limit_pct))
            ):
                return f"{limit_pct}% (ON — {val_str})"
            return f"{limit_pct}% (ON — Hardware)"
        return f"{limit_pct}% (Alert Only)"

    def _update_ui_and_telemetry(
        self,
        st: dict[str, Any] | None,
        bcfg: dict[str, Any],
        force_write: bool = False,
    ) -> None:
        telemetry = self._publish_display_telemetry(force=force_write)
        refresh_summary = self._format_refresh_summary(telemetry, st)
        bright_summary = self._format_brightness_summary(telemetry, bcfg)

        self.act_refresh.setText(f"Refresh: {refresh_summary}")
        self.act_brightness.setText(f"Brightness: {bright_summary}")
        self.act_brightness_toggle.setText(
            f"Auto Brightness: {'ON' if bcfg.get('enabled', True) else 'OFF'}"
        )

        if not st:
            offline_key = ("offline", None, False)
            if self.last_icon_key != offline_key:
                self.tray.setIcon(self._get_icon("off", False, battery_pct=None, charging=False))
                self.last_icon_key = offline_key
            tooltip = (
                "Smart Power: Daemon offline\n"
                f"Refresh: {refresh_summary}\n"
                f"Brightness: {bright_summary}"
            )
            self.tray.setToolTip(tooltip)
            self.act_state.setText("Smart Optimize: Daemon offline")
            self.act_reason.setText("Trigger: unavailable")
            self.act_watt.setText("Battery: unavailable")
            self.act_charge_limit.setText("Charge Limit: unavailable")
            self.act_cpu.setText("CPU EPP: -- | Wi-Fi PS: --")
            self.act_dgpu.setText("dGPU: --")
            return

        enabled = bool(st.get("enabled", True))
        mode = str(st.get("mode", "light" if enabled else "off"))
        icon_mode = mode if enabled else "off"

        bat_present = bool(st.get("battery_present", True))
        bat_status_raw = str(st.get("battery_status", "Unknown")).strip()
        bat_pct: int | None = None
        charging = False
        if bat_present and st.get("battery_pct") is not None:
            try:
                bat_pct = max(0, min(100, int(round(float(st.get("battery_pct", 0))))))
            except (TypeError, ValueError):
                bat_pct = None
            charging = bat_status_raw.lower() == "charging"

        icon_key = (icon_mode, bat_pct, charging if bat_pct is not None else False)
        if icon_key != self.last_icon_key:
            self.tray.setIcon(
                self._get_icon(mode, enabled, battery_pct=bat_pct, charging=charging)
            )
            self.last_icon_key = icon_key

        self.last_enabled = enabled
        toggle_label = "ON" if enabled else "OFF"
        mode_label = MODE_LABELS.get(mode, mode.title())
        reason = str(st.get("reason", ""))
        epp = str(st.get("epp", "unknown"))
        wifi_ps = str(st.get("wifi_power_save", "unknown"))

        dgpu_pci = st.get("dgpu_pci")
        dgpu_status = str(st.get("dgpu_status", "unknown"))
        dgpu_state = str(st.get("dgpu_power_state", "unknown"))
        if dgpu_pci in (None, "", "none") and dgpu_status in ("none", "absent", "unknown", ""):
            dgpu_line = "none (iGPU only)"
        else:
            dgpu_line = f"{dgpu_status} ({dgpu_state})"

        bat_line = self._format_battery_summary(st)
        charge_limit_line = self._format_charge_limit_summary(st)
        limit_on = bool(st.get("charge_limit_enabled", True))
        limit_pct = int(st.get("charge_limit_pct", 80) or 80)

        # Once-per-charge-cycle fallback alert when laptop lacks hardware EC charge cutoff
        should_fire, self._charge_alert_sent = evaluate_charge_limit_alert(
            st, self._charge_alert_sent
        )
        if should_fire:
            pct_disp = bat_pct if bat_pct is not None else limit_pct
            self.tray.showMessage(
                f"Battery reached {limit_pct}%",
                f"Battery is at {pct_disp}% while charging. Unplug the charger to preserve "
                "long-term battery health (no hardware charge cutoff detected).",
                QSystemTrayIcon.MessageIcon.Information,
                8000,
            )

        tooltip = (
            f"Smart Optimize: {toggle_label} | Mode: {mode_label}\n"
            f"Trigger: {reason}\n"
            f"Battery: {bat_line}\n"
            f"Charge Limit: {charge_limit_line}\n"
            f"CPU EPP: {epp} | Wi-Fi PS: {wifi_ps}\n"
            f"dGPU: {dgpu_line}\n"
            f"Refresh: {refresh_summary}\n"
            f"Brightness: {bright_summary}"
        )
        self.tray.setToolTip(tooltip)

        self.act_state.setText(f"Smart Optimize: {toggle_label} ({mode_label})")
        self.act_reason.setText(f"Trigger: {_truncate(reason or '--')}")
        self.act_watt.setText(f"Battery: {bat_line}")
        self.act_charge_limit.setText(f"Charge Limit: {charge_limit_line}")
        self.act_cpu.setText(f"CPU EPP: {epp} | Wi-Fi PS: {wifi_ps}")
        self.act_dgpu.setText(f"dGPU: {dgpu_line}")
        self.act_toggle.setText(
            "Turn Smart Optimize OFF" if enabled else "Turn Smart Optimize ON"
        )
        self.act_charge_limit_toggle.setText(
            f"{limit_pct}% Charge Limit: {'ON' if limit_on else 'OFF'}"
        )
        self.act_charge_limit_toggle.setEnabled(bat_present)

    def refresh_status(self, force_probe: bool = False) -> None:
        now = time.time()
        st = self._read_status()
        self.last_status = st
        bcfg = self._effective_brightness_cfg(st)
        ac_online = self._is_ac_online(st)

        # 1. Display refresh rate management (3-tier: high / mid / low)
        if force_probe:
            self.refresh_ctrl.probe(force=True, now=now)

        bt_cfg = self._local_config.get("battery_tricks", {})
        if st:
            desired_refresh = str(st.get("desired_refresh_profile", "high"))
            ac_hz = float(st.get("ac_refresh_hz", bt_cfg.get("ac_refresh_hz", 0)) or 0)
            battery_hz = float(
                st.get("battery_refresh_hz", bt_cfg.get("battery_refresh_hz", 0)) or 0
            )
            low_hz = float(st.get("low_refresh_hz", bt_cfg.get("low_refresh_hz", 60)) or 60)
        else:
            # Daemon offline: never leave panel forced into stepped-down refresh mode
            desired_refresh = "high"
            ac_hz = float(bt_cfg.get("ac_refresh_hz", 0) or 0)
            battery_hz = float(bt_cfg.get("battery_refresh_hz", 0) or 0)
            low_hz = float(bt_cfg.get("low_refresh_hz", 60) or 60)
        self.refresh_ctrl.apply_profile(
            desired_refresh,
            ac_hz=ac_hz,
            battery_hz=battery_hz,
            low_hz=low_hz,
            now=now,
        )

        # 2. Pure day/night circadian adaptive brightness
        self.brightness_ctrl.update(
            config=bcfg,
            ac_online=ac_online,
            now=now,
        )

        # 3. Update tooltip, context menu, and user-session display telemetry JSON
        self._update_ui_and_telemetry(st, bcfg, force_write=force_probe)

    def trigger_toggle(self) -> None:
        self._run_ctl(["toggle"])

    def trigger_charge_limit_toggle(self) -> None:
        self._charge_alert_sent = False
        self._run_ctl(["charge-limit", "toggle"])

    def trigger_brightness_toggle(self) -> None:
        self.brightness_ctrl.clear_manual_override()
        self._run_ctl(["brightness", "toggle"])

    def trigger_refresh_now(self) -> None:
        self._local_config = load_local_config()
        self.refresh_status(force_probe=True)

    def on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.trigger_toggle()

    def request_quit(self) -> None:
        self.cleanup()
        self.app.quit()

    def cleanup(self) -> None:
        """Restore native display refresh rate and release resources cleanly before exit."""
        if self._cleaned_up:
            return
        self._cleaned_up = True

        self.timer.stop()
        self.refresh_ctrl.restore_native()
        self.brightness_ctrl.cleanup()
        self._publish_display_telemetry(force=True)
        self.lock_server.close()


def acquire_single_instance(socket_path: str) -> QLocalServer | None:
    """Acquire per-user QLocalServer lock & IPC socket."""
    probe = QLocalSocket()
    probe.connectToServer(socket_path)
    if probe.waitForConnected(200):
        probe.disconnectFromServer()
        return None

    QLocalServer.removeServer(socket_path)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(socket_path):
        return None
    return server


def setup_unix_signal_bridge(
    app: QApplication,
    tray: SmartPowerTray,
) -> tuple[socket.socket, socket.socket, QSocketNotifier]:
    """Bridge Unix SIGTERM/SIGINT safely into Qt's event loop."""
    rsock, wsock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    rsock.setblocking(False)
    wsock.setblocking(False)

    def _sig_handler(signum: int, _frame: Any) -> None:
        try:
            wsock.send(bytes([signum & 0xFF]))
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    notifier = QSocketNotifier(rsock.fileno(), QSocketNotifier.Type.Read, app)

    def _on_signal_byte() -> None:
        try:
            data = rsock.recv(64)
        except OSError:
            return
        for sig_byte in data:
            if sig_byte in (signal.SIGTERM, signal.SIGINT):
                tray.request_quit()
                break

    notifier.activated.connect(_on_signal_byte)
    return rsock, wsock, notifier


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("smart-power-tray")
    app.setApplicationDisplayName("Smart Power")
    app.setDesktopFileName("smart-power-tray")
    app.setQuitOnLastWindowClosed(False)

    socket_path = get_tray_socket_path()
    lock_server = acquire_single_instance(socket_path)
    if lock_server is None:
        return 0

    tray = SmartPowerTray(app, lock_server)
    rsock, wsock, _notifier = setup_unix_signal_bridge(app, tray)
    try:
        rc = app.exec()
    finally:
        tray.cleanup()
        for sock in (rsock, wsock):
            try:
                sock.close()
            except OSError:
                pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
