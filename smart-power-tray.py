#!/usr/bin/env python3
"""
smart-power-tray: Lightweight PyQt6 system tray controller for smart-powerd.

Runs inside the user's graphical session (Wayland or X11) to coordinate:
  - Passwordless Smart Optimize toggle (left-click & right-click menu)
  - Live telemetry tooltip & menu (mode, trigger, battery wattage, EPP, Wi-Fi PS, dGPU)
  - Portable dGPU-safe adaptive brightness & 3-tier ambient light sensing
  - Multi-DE laptop display refresh rate switching (60 Hz on Battery+Light, native max otherwise)
  - Per-user QLocalServer IPC + SIGUSR1 handler for `smart-powerctl measure-light`
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
for _path_entry in (str(_SCRIPT_DIR), str(_LIB_DIR)):
    if _path_entry not in sys.path:
        sys.path.insert(0, _path_entry)

from PyQt6.QtCore import QPointF, QProcess, QSocketNotifier, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

try:
    from smart_power_config import load_config as _shared_load_config
except ImportError:
    _shared_load_config = None

from adaptive_brightness import (
    AdaptiveBrightnessController,
    measure_webcam_luma,
    normalize_brightness_config,
)
from display_refresh import DisplayRefreshController

STATUS_FILE = Path("/run/smart-powerd/status.json")
CTL_BIN = "/usr/local/bin/smart-powerctl"
POLL_MS = 2500
RAMP_STEP_MS = 140

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
            "low_refresh_on_battery_light": True,
            "low_refresh_hz": 60,
        },
        "brightness": normalize_brightness_config(None),
    }


def render_tray_icon(mode: str, enabled: bool) -> QIcon:
    """Render a crisp 64x64 vector badge icon directly with QPainter."""
    key = mode if enabled else "off"
    bg_hex = MODE_COLORS.get(key, MODE_COLORS["off"])

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Outer badge circle
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


class WebcamMeasureWorker(QThread):
    """Background QThread performing RAM-only single-frame webcam ambient sensing."""

    measured = pyqtSignal(object, str, bool)

    def __init__(self, device: str, manual: bool, parent: Any = None) -> None:
        super().__init__(parent)
        self._device = device
        self._manual = manual

    def run(self) -> None:
        luma, status = measure_webcam_luma(device=self._device, timeout_sec=4.0)
        self.measured.emit(luma, status, self._manual)


class SmartPowerTray:
    """System tray controller integrating daemon status, brightness, and refresh rate."""

    def __init__(self, app: QApplication, lock_server: QLocalServer) -> None:
        self.app = app
        self.lock_server = lock_server
        self._active_procs: list[QProcess] = []
        self._webcam_worker: WebcamMeasureWorker | None = None
        self._cleaned_up = False

        self.icon_cache: dict[str, QIcon] = {}
        self.last_icon_key: str | None = None
        self.last_enabled: bool = True
        self.last_status: dict[str, Any] | None = None
        self._last_telemetry_written: dict[str, Any] | None = None
        self._last_measure_note: str = ""
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
        self.act_watt = self._make_info_action("Battery: -- W")
        self.act_cpu = self._make_info_action("CPU EPP: -- | Wi-Fi PS: --")
        self.act_dgpu = self._make_info_action("dGPU: --")
        self.act_refresh = self._make_info_action("Display: --")
        self.act_brightness = self._make_info_action("Brightness: --")

        for info_act in (
            self.act_state,
            self.act_reason,
            self.act_watt,
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

        self.act_brightness_toggle = QAction("Auto Brightness: ON", self.menu)
        self.act_brightness_toggle.triggered.connect(self.trigger_brightness_toggle)
        self.menu.addAction(self.act_brightness_toggle)

        self.act_webcam_toggle = QAction("Webcam Ambient Sensor: ON", self.menu)
        self.act_webcam_toggle.triggered.connect(self.trigger_webcam_als_toggle)
        self.menu.addAction(self.act_webcam_toggle)

        self.act_measure_light = QAction("Measure Room Light Now", self.menu)
        self.act_measure_light.triggered.connect(self.trigger_measure_light)
        self.menu.addAction(self.act_measure_light)

        self.menu.addSeparator()

        self.act_refresh_now = QAction("Refresh Status", self.menu)
        self.act_refresh_now.triggered.connect(self.trigger_refresh_now)
        self.menu.addAction(self.act_refresh_now)

        self.act_quit = QAction("Quit Tray", self.menu)
        self.act_quit.triggered.connect(self.request_quit)
        self.menu.addAction(self.act_quit)

        self.tray.setContextMenu(self.menu)

        # Hook QLocalServer IPC for `smart-powerctl measure-light`
        self.lock_server.newConnection.connect(self._on_ipc_connection)

        # Smooth mini-step timer for immediate manual ambient measurements
        self.ramp_timer = QTimer(self.app)
        self.ramp_timer.setInterval(RAMP_STEP_MS)
        self.ramp_timer.timeout.connect(self._on_ramp_tick)

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

    def _get_icon(self, mode: str, enabled: bool) -> QIcon:
        key = mode if enabled else "off"
        if key not in self.icon_cache:
            self.icon_cache[key] = render_tray_icon(mode, enabled)
        return self.icon_cache[key]

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

    def _launch_webcam_worker(self, device: str, manual: bool) -> bool:
        if self._webcam_worker is not None and self._webcam_worker.isRunning():
            return False
        worker = WebcamMeasureWorker(device=device, manual=manual, parent=self.app)
        worker.measured.connect(self._on_webcam_measured)
        worker.finished.connect(self._on_webcam_worker_finished)
        self._webcam_worker = worker
        worker.start()
        return True

    def _on_webcam_worker_finished(self) -> None:
        worker = self._webcam_worker
        self._webcam_worker = None
        if worker is not None:
            worker.deleteLater()

    def _on_webcam_measured(self, luma: Any, status: str, manual: bool) -> None:
        st = self._read_status() or self.last_status
        bcfg = self._effective_brightness_cfg(st)
        ac_online = self._is_ac_online(st)
        als_lux = st.get("als_lux") if isinstance(st, dict) else None
        now = time.time()

        luma_val = float(luma) if isinstance(luma, (int, float)) else None
        self.brightness_ctrl.ingest_webcam_luma(
            luma_val,
            status=status,
            now=now,
            manual=manual,
            manual_ttl_sec=max(float(bcfg["webcam_interval_sec"]), 300.0),
        )
        if status == "webcam-busy":
            self._last_measure_note = "webcam busy"
        elif status != "ok" and manual:
            self._last_measure_note = status.replace("-", " ")
        else:
            self._last_measure_note = ""

        self.brightness_ctrl.update(
            config=bcfg,
            ac_online=ac_online,
            als_lux=als_lux,
            now=now,
            force_measure=manual,
        )

        if manual and self.brightness_ctrl.last_target_pct is not None:
            cur = self.brightness_ctrl.last_current_pct
            if cur is not None and cur != self.brightness_ctrl.last_target_pct:
                self.ramp_timer.start()

        self._update_ui_and_telemetry(st, bcfg)

    def _on_ramp_tick(self) -> None:
        st = self.last_status
        bcfg = self._effective_brightness_cfg(st)
        step = max(2, int(bcfg.get("smooth_step_pct", 3)))
        more = self.brightness_ctrl.step_ramp(step_size=step)
        if not more:
            self.ramp_timer.stop()
        self._update_ui_and_telemetry(st, bcfg)

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
        if cmd_norm == "measure-light":
            self.trigger_measure_light()
            reply = {"ok": True, "message": "ambient light measurement triggered"}
        elif cmd_norm == "refresh":
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

    def _format_brightness_summary(self, telemetry: dict[str, Any], bcfg: dict[str, Any]) -> str:
        pct = telemetry.get("brightness_pct")
        source = str(telemetry.get("brightness_source") or "Unavailable")
        override_until = float(telemetry.get("manual_override_until") or 0.0)
        lux = telemetry.get("ambient_lux")

        if pct is None:
            return f"Unavailable ({source})"

        detail = source
        if source == "Manual Pause":
            rem_min = max(1, int(round((override_until - time.time()) / 60.0)))
            detail = f"Manual Pause ({rem_min}m)"
        elif source == "ALS" and isinstance(lux, (int, float)):
            detail = f"ALS ({int(round(lux))} lux)"
        elif source == "Schedule" and self._last_measure_note:
            detail = f"Schedule ({self._last_measure_note})"
        elif not bcfg.get("enabled", True) and source == "Disabled":
            detail = "Disabled"

        return f"{pct}% — {detail}"

    @staticmethod
    def _format_refresh_summary(telemetry: dict[str, Any]) -> str:
        out = telemetry.get("refresh_output")
        cur_hz = telemetry.get("refresh_hz")
        nat_hz = telemetry.get("native_refresh_hz")
        if not out or cur_hz is None:
            return "Unavailable"
        cur_str = _format_hz(cur_hz)
        if nat_hz is not None and abs(float(nat_hz) - float(cur_hz)) >= 1.0:
            return f"{cur_str} / {_format_hz(nat_hz)} ({out})"
        return f"{cur_str} ({out})"

    def _update_ui_and_telemetry(
        self,
        st: dict[str, Any] | None,
        bcfg: dict[str, Any],
        force_write: bool = False,
    ) -> None:
        telemetry = self._publish_display_telemetry(force=force_write)
        refresh_summary = self._format_refresh_summary(telemetry)
        bright_summary = self._format_brightness_summary(telemetry, bcfg)

        self.act_refresh.setText(f"Display: {refresh_summary}")
        self.act_brightness.setText(f"Brightness: {bright_summary}")
        self.act_brightness_toggle.setText(
            f"Auto Brightness: {'ON' if bcfg.get('enabled', True) else 'OFF'}"
        )
        self.act_webcam_toggle.setText(
            f"Webcam Ambient Sensor: {'ON' if bcfg.get('webcam_als_enabled', True) else 'OFF'}"
        )

        if not st:
            icon = self._get_icon("off", False)
            if self.last_icon_key != "offline":
                self.tray.setIcon(icon)
                self.last_icon_key = "offline"
            tooltip = (
                "Smart Power: Daemon offline\n"
                f"Display: {refresh_summary}\n"
                f"Brightness: {bright_summary}"
            )
            self.tray.setToolTip(tooltip)
            self.act_state.setText("Smart Optimize: Daemon offline")
            self.act_reason.setText("Trigger: unavailable")
            self.act_watt.setText("Battery: unavailable")
            self.act_cpu.setText("CPU EPP: -- | Wi-Fi PS: --")
            self.act_dgpu.setText("dGPU: --")
            return

        enabled = bool(st.get("enabled", True))
        mode = str(st.get("mode", "light" if enabled else "off"))
        icon_key = mode if enabled else "off"
        if icon_key != self.last_icon_key:
            self.tray.setIcon(self._get_icon(mode, enabled))
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

        if st.get("battery_present", True):
            watts = float(st.get("battery_watts", 0.0) or 0.0)
            bat_status = str(st.get("battery_status", "Unknown")).lower()
            bat_pct = st.get("battery_pct", 0)
            bat_line = f"{watts:.2f} W {bat_status} ({bat_pct}%)"
        else:
            bat_line = "No battery"

        tooltip = (
            f"Smart Optimize: {toggle_label} | Mode: {mode_label}\n"
            f"Trigger: {reason}\n"
            f"Battery: {bat_line}\n"
            f"CPU EPP: {epp} | Wi-Fi PS: {wifi_ps}\n"
            f"dGPU: {dgpu_line}\n"
            f"Display: {refresh_summary}\n"
            f"Brightness: {bright_summary}"
        )
        self.tray.setToolTip(tooltip)

        self.act_state.setText(f"Smart Optimize: {toggle_label} ({mode_label})")
        self.act_reason.setText(f"Trigger: {_truncate(reason or '--')}")
        self.act_watt.setText(f"Battery: {bat_line}")
        self.act_cpu.setText(f"CPU EPP: {epp} | Wi-Fi PS: {wifi_ps}")
        self.act_dgpu.setText(f"dGPU: {dgpu_line}")
        self.act_toggle.setText(
            "Turn Smart Optimize OFF" if enabled else "Turn Smart Optimize ON"
        )

    def refresh_status(self, force_probe: bool = False) -> None:
        now = time.time()
        st = self._read_status()
        self.last_status = st
        bcfg = self._effective_brightness_cfg(st)
        ac_online = self._is_ac_online(st)
        als_lux = st.get("als_lux") if isinstance(st, dict) else None

        # 1. Display refresh rate management
        if force_probe:
            self.refresh_ctrl.probe(force=True, now=now)

        if st:
            desired_refresh = str(st.get("desired_refresh_profile", "high"))
            low_hz = float(st.get("low_refresh_hz", 60) or 60)
        else:
            # Daemon offline: never leave panel forced into low refresh mode
            desired_refresh = "high"
            low_hz = float(
                self._local_config.get("battery_tricks", {}).get("low_refresh_hz", 60) or 60
            )
        self.refresh_ctrl.apply_profile(desired_refresh, low_refresh_hz=low_hz, now=now)

        # 2. Adaptive brightness + periodic non-blocking webcam ambient sampling
        if self.brightness_ctrl.should_sample_webcam(bcfg, daemon_als_lux=als_lux, now=now):
            self._launch_webcam_worker(str(bcfg.get("webcam_device", "auto")), manual=False)

        self.brightness_ctrl.update(
            config=bcfg,
            ac_online=ac_online,
            als_lux=als_lux,
            now=now,
        )

        # 3. Update tooltip, context menu, and user-session display telemetry JSON
        self._update_ui_and_telemetry(st, bcfg, force_write=force_probe)

    def trigger_toggle(self) -> None:
        self._run_ctl(["toggle"])

    def trigger_brightness_toggle(self) -> None:
        self.brightness_ctrl.clear_manual_override()
        self._run_ctl(["brightness", "toggle"])

    def trigger_webcam_als_toggle(self) -> None:
        self._run_ctl(["webcam-als", "toggle"])

    def trigger_measure_light(self) -> None:
        """
        Immediately measure ambient room light (hardware ALS first; otherwise
        non-blocking single-frame webcam probe even when periodic webcam ALS is off),
        clear any manual override pause, and smoothly update brightness.
        """
        self.ramp_timer.stop()
        self.brightness_ctrl.clear_manual_override()
        self._last_measure_note = ""

        st = self._read_status() or self.last_status
        bcfg = self._effective_brightness_cfg(st)
        ac_online = self._is_ac_online(st)
        als_lux = st.get("als_lux") if isinstance(st, dict) else None
        now = time.time()

        if self.brightness_ctrl.should_sample_webcam(
            bcfg, daemon_als_lux=als_lux, now=now, force=True
        ):
            self.act_brightness.setText("Brightness: Measuring room light...")
            self._launch_webcam_worker(str(bcfg.get("webcam_device", "auto")), manual=True)
            return

        # Hardware ALS or schedule-only mode: apply immediately with smooth mini-stepping
        self.brightness_ctrl.update(
            config=bcfg,
            ac_online=ac_online,
            als_lux=als_lux,
            now=now,
            force_measure=True,
        )
        if (
            self.brightness_ctrl.last_target_pct is not None
            and self.brightness_ctrl.last_current_pct is not None
            and self.brightness_ctrl.last_current_pct != self.brightness_ctrl.last_target_pct
        ):
            self.ramp_timer.start()
        self._update_ui_and_telemetry(st, bcfg, force_write=True)

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
        self.ramp_timer.stop()

        if self._webcam_worker is not None and self._webcam_worker.isRunning():
            self._webcam_worker.wait(1500)

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
    """Bridge Unix SIGUSR1/SIGTERM/SIGINT safely into Qt's event loop."""
    rsock, wsock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    rsock.setblocking(False)
    wsock.setblocking(False)

    def _sig_handler(signum: int, _frame: Any) -> None:
        try:
            wsock.send(bytes([signum & 0xFF]))
        except OSError:
            pass

    signal.signal(signal.SIGUSR1, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    notifier = QSocketNotifier(rsock.fileno(), QSocketNotifier.Type.Read, app)

    def _on_signal_byte() -> None:
        try:
            data = rsock.recv(64)
        except OSError:
            return
        for sig_byte in data:
            if sig_byte == signal.SIGUSR1:
                tray.trigger_measure_light()
            elif sig_byte in (signal.SIGTERM, signal.SIGINT):
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
