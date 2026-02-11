from __future__ import annotations

import webbrowser
import os
import platform
import statistics
import socket
import subprocess
import time
import ipaddress
from typing import Any, Callable, Dict, List, Optional

import psutil
import requests
from PySide6 import QtCore, QtGui, QtWidgets

# Compatibility shim:
# The project historically used PyQt-style signal names (pyqtSignal).
# When running on PySide6, signals are exposed as QtCore.Signal.
if not hasattr(QtCore, "pyqtSignal") and hasattr(QtCore, "Signal"):
    QtCore.pyqtSignal = QtCore.Signal  # type: ignore

try:
    import pyqtgraph as pg
except Exception:
    pg = None  # optional

from core.engine_manager import EngineManager
from core.settings_manager import SettingsManager
from core.scanner import NetworkScanner
from core.updater import CoreUpdater


# ---------------------
# Utilities
# ---------------------

def human_mbps(v: float) -> str:
    if v is None:
        return "-"
    return f"{v:.2f} Mbps"


def _ping_cmd(host: str) -> List[str]:
    sys = platform.system().lower()
    if sys.startswith("win"):
        return ["ping", "-n", "1", "-w", "1000", host]
    return ["ping", "-c", "1", "-W", "1", host]


def _parse_ping_ms(output: str) -> Optional[float]:
    out = output.lower()
    # windows: time=12ms / time<1ms
    # linux: time=12.3 ms
    import re

    m = re.search(r"time[=<]\s*([0-9]+(?:\.[0-9]+)?)\s*ms", out)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None

    m = re.search(r"time\s*=\s*([0-9]+)\s*ms", out)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None

    return None


def _split_args(raw: str) -> List[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        import shlex

        return shlex.split(raw)
    except Exception:
        return raw.split()


# ---------------------
# Graph widgets (no zoom)
# ---------------------

class _NoZoomPlotWidget(pg.PlotWidget if pg else QtWidgets.QWidget):
    def __init__(self, *a, **kw):
        if pg:
            super().__init__(*a, **kw)
            self.setMouseEnabled(x=False, y=False)
            self.hideButtons()
            self.setMenuEnabled(False)
        else:
            super().__init__()

    def wheelEvent(self, ev):  # type: ignore
        # Disable zoom on mouse wheel
        ev.ignore()


# ---------------------
# Workers
# ---------------------

class PingWorker(QtCore.QThread):
    result = QtCore.pyqtSignal(float, bool)  # ms, success

    def __init__(self, host: str, parent=None):
        super().__init__(parent)
        self.host = host

    def run(self):
        try:
            p = subprocess.run(_ping_cmd(self.host), capture_output=True, text=True)
            out = (p.stdout or "") + "\n" + (p.stderr or "")
            ms = _parse_ping_ms(out)
            if ms is None or p.returncode != 0:
                self.result.emit(0.0, False)
            else:
                self.result.emit(ms, True)
        except Exception:
            self.result.emit(0.0, False)


class AdvancedSpeedtestWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(str, int)  # phase, percent
    finished = QtCore.pyqtSignal(dict)  # results dict
    failed = QtCore.pyqtSignal(str)

    def __init__(self, download_url: str, upload_url: str, download_bytes: int, upload_bytes: int, parent=None):
        super().__init__(parent)
        self.download_url = download_url
        self.upload_url = upload_url
        self.download_bytes = int(download_bytes)
        self.upload_bytes = int(upload_bytes)

    def run(self):
        res: Dict[str, Any] = {
            "download_mbps": None,
            "upload_mbps": None,
            "download_bytes": self.download_bytes,
            "upload_bytes": self.upload_bytes,
        }
        try:
            # ---- download (Range)
            self.progress.emit("download", 0)
            headers = {"Range": f"bytes=0-{self.download_bytes-1}"}
            t0 = time.time()
            with requests.get(self.download_url, headers=headers, stream=True, timeout=40) as r:
                r.raise_for_status()
                got = 0
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    got += len(chunk)
                    pc = min(100, int(got * 100 / max(1, self.download_bytes)))
                    self.progress.emit("download", pc)
                    if got >= self.download_bytes:
                        break
            dt = max(0.001, time.time() - t0)
            res["download_mbps"] = (got * 8.0 / dt) / 1e6

            # ---- upload (POST)
            self.progress.emit("upload", 0)
            payload = os.urandom(self.upload_bytes)
            t1 = time.time()
            # Upload endpoint must accept POST; result ignored.
            r2 = requests.post(self.upload_url, data=payload, timeout=40)
            _ = r2.status_code
            dt2 = max(0.001, time.time() - t1)
            res["upload_mbps"] = (self.upload_bytes * 8.0 / dt2) / 1e6

            self.progress.emit("upload", 100)
            self.finished.emit(res)
        except Exception as e:
            self.failed.emit(str(e))


class SubscriptionUpdateWorker(QtCore.QThread):
    done = QtCore.pyqtSignal(int)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, settings: SettingsManager, url: str, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.url = url

    def run(self):
        try:
            n = self.settings.update_subscription(self.url, timeout=30)
            self.done.emit(n)
        except Exception as e:
            self.failed.emit(str(e))


class OptimizeDnsWorker(QtCore.QThread):
    done = QtCore.pyqtSignal(dict)  # {"ranked": [(idx, name, server, ping, loss, jitter), ...]}
    failed = QtCore.pyqtSignal(str)

    def __init__(self, dns_servers: List[Dict[str, Any]], parent=None):
        super().__init__(parent)
        self.dns_servers = dns_servers

    def run(self):
        try:
            ranked = []
            for i, s in enumerate(self.dns_servers):
                host = (s.get("server") or "").strip()
                if not host:
                    continue
                samples = []
                lost = 0
                for _ in range(4):
                    p = subprocess.run(_ping_cmd(host), capture_output=True, text=True)
                    out = (p.stdout or "") + "\n" + (p.stderr or "")
                    ms = _parse_ping_ms(out)
                    if ms is None or p.returncode != 0:
                        lost += 1
                    else:
                        samples.append(ms)
                    time.sleep(0.08)

                loss = lost / 4.0
                ping = statistics.mean(samples) if samples else 999.0
                jitter = statistics.pstdev(samples) if len(samples) >= 2 else 0.0
                score = ping + jitter * 2.0 + loss * 2000.0
                ranked.append((score, i, s.get("name", ""), host, ping, loss, jitter))

            ranked.sort(key=lambda x: x[0])
            out = {
                "ranked": [(i, name, host, ping, loss, jitter) for _, i, name, host, ping, loss, jitter in ranked]
            }
            self.done.emit(out)
        except Exception as e:
            self.failed.emit(str(e))


# ---------------------
# Dashboard Page
# ---------------------

class DashboardPage(QtWidgets.QWidget):
    def __init__(
        self,
        engine: EngineManager,
        settings: SettingsManager,
        go_to_settings_cb: Optional[Callable[[], None]] = None,
        is_refresh_paused_cb: Optional[Callable[[], bool]] = None,
    ):
        super().__init__()
        self.engine = engine
        self.settings = settings
        self.go_to_settings_cb = go_to_settings_cb
        self.is_refresh_paused_cb = is_refresh_paused_cb

        self.scanner = NetworkScanner()

        self._ping_host = "1.1.1.1"
        self._ping_history: List[Optional[float]] = []  # 60 items; None=lost
        self._last_io = psutil.net_io_counters()
        self._last_ts = time.time()
        self._adv_last_results: Dict[str, Any] = {}
        self._last_ports_ts = 0.0
        self._ports_cache = "-"
        self._load_port_override()

        self._build()
        self._wire()

        # terminal callback
        self.engine.set_log_callback(self._append_terminal)

        self.btn_set_port_override.clicked.connect(self._set_port_override)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._ping_timer = QtCore.QTimer(self)
        self._ping_timer.setInterval(1000)
        self._ping_timer.timeout.connect(self._ping_once_if_engine_on)
        self._ping_timer.start()

    def shutdown(self):
        try:
            self._timer.stop()
            self._ping_timer.stop()
        except Exception:
            pass

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(12)

        # ----- Profiles card
        gb_profiles = QtWidgets.QGroupBox("Profiles")
        gbp = QtWidgets.QVBoxLayout(gb_profiles)
        self.lbl_profile = QtWidgets.QLabel("Active: -")
        self.cmb_profile = QtWidgets.QComboBox()
        self.cmb_profile.addItems(["Gaming", "Streaming", "Work", "Custom"])
        self.btn_apply_profile = QtWidgets.QPushButton("Apply Profile")
        self.btn_apply_profile.setMinimumHeight(44)
        gbp.addWidget(self.lbl_profile)
        gbp.addWidget(self.cmb_profile)
        gbp.addWidget(self.btn_apply_profile)
        gbp.addStretch(1)

        # bitrate block (streaming)
        self.bit_platform = QtWidgets.QComboBox()
        self.bit_platform.addItems(["Kick", "Aparat", "Twitch", "YouTube"])
        self.spin_upload_mbps = QtWidgets.QDoubleSpinBox()
        self.spin_upload_mbps.setRange(0, 1000)
        self.spin_upload_mbps.setValue(10.0)
        self.spin_upload_mbps.setSuffix(" Mbps")
        self.lbl_bitrate = QtWidgets.QLabel("Recommended bitrate: -")
        gbp.addSpacing(6)
        gbp.addWidget(QtWidgets.QLabel("Streaming helper (optional):"))
        gbp.addWidget(self.bit_platform)
        gbp.addWidget(self.spin_upload_mbps)
        gbp.addWidget(self.lbl_bitrate)

        # ----- Speed test card
        gb_speed = QtWidgets.QGroupBox("Speed Test")
        gbs = QtWidgets.QVBoxLayout(gb_speed)
        self.cmb_speed_target = QtWidgets.QComboBox()
        for t in (self.settings.data.get("speedtest", {}) or {}).get("targets", []):
            self.cmb_speed_target.addItem(f"{t.get('name','Target')} ({t.get('loc','')})")
        self.btn_pingtest = QtWidgets.QPushButton("Ping / Jitter / Loss (Safe)")
        self.btn_pingtest.setMinimumHeight(44)
        self.lbl_pingtest = QtWidgets.QLabel("Result: -")
        self.chk_advanced = QtWidgets.QCheckBox("Advanced: Download + Upload (may affect internet)")
        self.btn_advtest = QtWidgets.QPushButton("Run Advanced Speed Test")
        self.btn_advtest.setMinimumHeight(44)
        self.btn_advtest.setEnabled(False)

        # mini gauges
        self.pb_down = QtWidgets.QProgressBar()
        self.pb_up = QtWidgets.QProgressBar()
        for pb in (self.pb_down, self.pb_up):
            pb.setRange(0, 100)
            pb.setValue(0)
            pb.setTextVisible(True)
            pb.setMinimumHeight(18)

        gbs.addWidget(self.cmb_speed_target)
        gbs.addWidget(self.btn_pingtest)
        gbs.addWidget(self.lbl_pingtest)
        gbs.addSpacing(6)
        gbs.addWidget(self.chk_advanced)
        gbs.addWidget(self.btn_advtest)
        gbs.addWidget(QtWidgets.QLabel("Download gauge"))
        gbs.addWidget(self.pb_down)
        gbs.addWidget(QtWidgets.QLabel("Upload gauge"))
        gbs.addWidget(self.pb_up)
        gbs.addStretch(1)

        # ----- Live network card
        gb_live = QtWidgets.QGroupBox("Live Network")
        gbl = QtWidgets.QVBoxLayout(gb_live)
        self.cmb_metric = QtWidgets.QComboBox()
        self.cmb_metric.addItems(["Bandwidth (Down/Up)", "Packets (recv/sent)", "Errors (in/out)"])
        self.lbl_live = QtWidgets.QLabel("Down: - | Up: -")
        self.lbl_live2 = QtWidgets.QLabel("Ping60s: -   Loss60s: -   Jitter60s: -")
        self.lbl_ports = QtWidgets.QLabel("Listening ports: -")
        self.in_port_override = QtWidgets.QLineEdit()
        self.in_port_override.setPlaceholderText("Manual port override (comma-separated)")
        self.btn_set_port_override = QtWidgets.QPushButton("Set Port Override")
        self.btn_set_port_override.setMinimumHeight(34)

        # graph
        if pg:
            self.plot = _NoZoomPlotWidget()
            self.plot.setFixedHeight(160)
            self.plot.setBackground(None)
            self.plot.showGrid(x=False, y=True, alpha=0.15)
            self.curve_down = self.plot.plot([1], [1], pen=pg.mkPen(width=2))
            self.curve_up = self.plot.plot([1], [1], pen=pg.mkPen(width=2))
            self._x = list(range(60))
            self._down_hist = [0.0] * 60
            self._up_hist = [0.0] * 60
        else:
            self.plot = QtWidgets.QLabel("pyqtgraph not installed (graph disabled).")
            self.plot.setFixedHeight(160)
            self.plot.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self._x = []
            self._down_hist = []
            self._up_hist = []

        gbl.addWidget(self.cmb_metric)
        gbl.addWidget(self.lbl_live)
        gbl.addWidget(self.plot)
        gbl.addWidget(self.lbl_live2)
        gbl.addWidget(self.lbl_ports)
        gbl.addWidget(self.in_port_override)
        gbl.addWidget(self.btn_set_port_override)
        gbl.addStretch(1)

        top.addWidget(gb_profiles, 1)
        top.addWidget(gb_speed, 1)
        top.addWidget(gb_live, 1)

        # ----- Engine strip
        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(12)

        self.btn_engine = QtWidgets.QPushButton("Start Engine")
        self.btn_engine.setMinimumHeight(46)
        self.terminal = QtWidgets.QTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.setFixedHeight(150)

        bottom.addWidget(self.btn_engine, 0)
        bottom.addWidget(self.terminal, 1)

        # ----- Copilot card (rule-based, lightweight)
        gb_copilot = QtWidgets.QGroupBox("Copilot")
        gbc = QtWidgets.QVBoxLayout(gb_copilot)
        rowc = QtWidgets.QHBoxLayout()
        self.lbl_copilot_mode = QtWidgets.QLabel(f"Mode: {self.settings.get_copilot_mode()}")
        self.btn_copilot_run = QtWidgets.QPushButton("Run Analysis (Manual)")
        self.btn_copilot_run.setMinimumHeight(36)
        self.btn_copilot_rollback = QtWidgets.QPushButton("Rollback last Apply")
        self.btn_copilot_rollback.setMinimumHeight(36)
        rowc.addWidget(self.lbl_copilot_mode, 0)
        rowc.addStretch(1)
        rowc.addWidget(self.btn_copilot_run, 0)
        rowc.addWidget(self.btn_copilot_rollback, 0)
        gbc.addLayout(rowc)

        self.copilot_scroll = QtWidgets.QScrollArea()
        self.copilot_scroll.setWidgetResizable(True)
        self.copilot_scroll.setFixedHeight(190)

        self._copilot_host = QtWidgets.QWidget()
        self._copilot_v = QtWidgets.QVBoxLayout(self._copilot_host)
        self._copilot_v.setContentsMargins(6, 6, 6, 6)
        self._copilot_v.setSpacing(8)
        self._copilot_v.addStretch(1)
        self.copilot_scroll.setWidget(self._copilot_host)

        gbc.addWidget(self.copilot_scroll)

        # recipe strip (Expert mode)
        self.recipe_row = QtWidgets.QHBoxLayout()
        self.cmb_recipe = QtWidgets.QComboBox()
        self.cmb_recipe.addItems([
            "Recipe: Prepare for Streaming (Aparat/Kick)",
            "Recipe: Prepare for Gaming",
            "Recipe: Balanced (Work)",
        ])
        self.btn_recipe_apply = QtWidgets.QPushButton("Apply Recipe")
        self.btn_recipe_apply.setMinimumHeight(36)
        self.recipe_row.addWidget(self.cmb_recipe, 1)
        self.recipe_row.addWidget(self.btn_recipe_apply, 0)
        gbc.addLayout(self.recipe_row)

        layout.addLayout(top, 1)
        layout.addWidget(gb_copilot, 0)
        layout.addLayout(bottom, 0)

        self._refresh_profile_ui()
        # initial terminal status
        if not self.engine.is_running():
            self._append_terminal("[INFO] Engine is not running.")
        else:
            self._append_terminal("[INFO] Engine is running.")

    def _wire(self):
        self.btn_engine.clicked.connect(self._toggle_engine)
        self.btn_apply_profile.clicked.connect(self._apply_profile)
        self.cmb_profile.currentTextChanged.connect(lambda _: self._refresh_profile_ui())
        self.spin_upload_mbps.valueChanged.connect(lambda _: self._update_bitrate_label())
        self.bit_platform.currentTextChanged.connect(lambda _: self._update_bitrate_label())
        self.chk_advanced.toggled.connect(self.btn_advtest.setEnabled)
        self.btn_pingtest.clicked.connect(self._run_safe_pingtest)
        self.btn_advtest.clicked.connect(self._run_advanced_speedtest)

    def _append_terminal(self, line: str):
        # color engine state messages
        st = self.engine.status()
        color = "#6ee7b7" if st.running else "#fb7185"
        self.terminal.setTextColor(QtGui.QColor(color))
        self.terminal.append(line)
        self.terminal.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _toggle_engine(self):
        if self.engine.is_running():
            self.engine.stop_engine()
        else:
            self.engine.start_engine()
        self._update_engine_button()

    def _update_engine_button(self):
        st = self.engine.status()
        self.btn_engine.setText("Stop Engine" if st.running else "Start Engine")

    def _refresh_profile_ui(self):
        active = self.settings.get_active_profile()
        self.lbl_profile.setText(f"Active: {active}")
        # bitrate helper shown always but dashboard toggle respected for displaying label
        self._update_bitrate_label()

    def _apply_profile(self):
        chosen = self.cmb_profile.currentText()
        current = self.settings.get_active_profile()
        items = (self.settings.data.get("profiles", {}) or {}).get("items", {}) or {}
        readonly = bool((items.get(chosen, {}) or {}).get("readonly", False))

        if chosen != current and readonly:
            # ok, switching active profile is allowed
            self.settings.set_active_profile(chosen)
            self._append_terminal(f"[INFO] Profile switched to '{chosen}'")
            self._refresh_profile_ui()
            return

        # if editing readonly profile, treat as custom change with confirm
        if readonly and chosen == "Streaming":
            mb = QtWidgets.QMessageBox(self)
            mb.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            mb.setWindowTitle("Confirm")
            mb.setText("Streaming profile is predefined. Changing it will create/update Custom profile. Continue?")
            mb.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
            if mb.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
                return

        self.settings.set_active_profile(chosen)
        self._refresh_profile_ui()

    def _update_bitrate_label(self):
        ui = (self.settings.data.get("ui", {}) or {})
        show_on_dash = bool(ui.get("show_stream_bitrate_on_dashboard", True))
        if not show_on_dash:
            self.lbl_bitrate.setText("Recommended bitrate: (hidden)")
            return

        platform_name = self.bit_platform.currentText().strip()
        upload = float(self.spin_upload_mbps.value())
        # safe headroom factor
        safe = max(0.0, upload * 0.70)

        # soft caps (not hard rules; user can override in OBS)
        cap_kbps = {
            "Kick": 8000,
            "Aparat": 6000,
            "Twitch": 6000,
            "YouTube": 9000,
        }.get(platform_name, 8000)

        rec_kbps = int(min(cap_kbps, safe * 1000))
        self.lbl_bitrate.setText(f"Recommended bitrate ({platform_name}): ~{rec_kbps} kbps (based on upload headroom)")

    def _ping_once_if_engine_on(self):
        # lightweight ICMP monitoring: only when engine is ON
        if not self.engine.is_running():
            return
        # ping target comes from first speedtest target (cloudflare)
        targets = (self.settings.data.get("speedtest", {}) or {}).get("targets", [])
        if targets:
            self._ping_host = targets[1].get("host", "1.1.1.1") if len(targets) > 1 else targets[0].get("host", "1.1.1.1")

        w = PingWorker(self._ping_host, self)
        w.result.connect(self._on_ping_sample)
        w.start()

    def _on_ping_sample(self, ms: float, ok: bool):
        self._ping_history.append(ms if ok else None)
        self._ping_history = self._ping_history[-60:]
        self._update_ping_stats_label()

    def _update_ping_stats_label(self):
        hist = self._ping_history[-60:]
        if not hist:
            self.lbl_live2.setText("Ping60s: -   Loss60s: -   Jitter60s: -")
            return
        oks = [x for x in hist if x is not None]
        loss = 1.0 - (len(oks) / len(hist))
        ping = statistics.mean(oks) if oks else 0.0
        jitter = statistics.pstdev(oks) if len(oks) >= 2 else 0.0
        self.lbl_live2.setText(f"Ping60s: {ping:.0f} ms   Loss60s: {loss*100:.0f}%   Jitter60s: {jitter:.1f} ms")

    def _load_port_override(self):
        override = (self.settings.data.get("engine", {}) or {}).get("port_override", "")
        self.in_port_override.setText(str(override))

    def _set_port_override(self):
        raw = self.in_port_override.text().strip()
        if raw:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            cleaned = []
            for p in parts:
                if p.isdigit():
                    cleaned.append(str(int(p)))
            raw = ", ".join(cleaned)
        self.settings.data.setdefault("engine", {})["port_override"] = raw
        self.settings.save()
        QtWidgets.QMessageBox.information(self, "Port Override", "Manual port override saved.")

    def _tick(self):
        if self.is_refresh_paused_cb and self.is_refresh_paused_cb():
            return
        # engine state visuals
        self._update_engine_button()

        # net stats
        c = psutil.net_io_counters()
        t = time.time()
        dt = max(0.001, t - self._last_ts)
        down_mbps = ((c.bytes_recv - self._last_io.bytes_recv) * 8.0 / dt) / 1e6
        up_mbps = ((c.bytes_sent - self._last_io.bytes_sent) * 8.0 / dt) / 1e6
        self._last_io = c
        self._last_ts = t

        metric = self.cmb_metric.currentText()
        if metric.startswith("Bandwidth"):
            self.lbl_live.setText(f"Down: {human_mbps(down_mbps)} | Up: {human_mbps(up_mbps)}")
            if pg:
                self._down_hist = (self._down_hist + [down_mbps])[-60:]
                self._up_hist = (self._up_hist + [up_mbps])[-60:]
                self.curve_down.setData(self._x[-len(self._down_hist):], self._down_hist)
                self.curve_up.setData(self._x[-len(self._up_hist):], self._up_hist)
        elif metric.startswith("Packets"):
            self.lbl_live.setText(f"Packets recv: {c.packets_recv} | sent: {c.packets_sent}")
        else:
            self.lbl_live.setText(f"Errors in: {c.errin} | out: {c.errout}")

        # listening ports (lightweight, throttled)
        now_ports = time.time()
        if now_ports - self._last_ports_ts >= 5:
            ports = self.engine.detect_listening_ports(limit=6)
            if ports:
                port_text = ", ".join(f"{p['port']}:{p['name']}" for p in ports)
            else:
                port_text = "-"
            self._ports_cache = port_text
            self._last_ports_ts = now_ports
        override = (self.settings.data.get("engine", {}) or {}).get("port_override", "")
        if override:
            self.lbl_ports.setText(f"Listening ports: {self._ports_cache} | Override: {override}")
        else:
            self.lbl_ports.setText(f"Listening ports: {self._ports_cache}")

        # Terminal color hint (engine running)
        st = self.engine.status()
        bg = "#0b0d12"
        self.terminal.setStyleSheet(f"background:{bg}; color:{'#6ee7b7' if st.running else '#fb7185'};")

    # ---------------------
    # Speed tests
    # ---------------------

    def _run_safe_pingtest(self):
        # safe test: ping/jitter/loss using selected target host
        mb = QtWidgets.QMessageBox(self)
        mb.setWindowTitle("Confirm Safe Ping Test")
        mb.setIcon(QtWidgets.QMessageBox.Icon.Question)
        mb.setText(
            "This action sends 10 lightweight ping requests to the selected target and does not run download/upload tests.\n"
            "Do you want to continue?"
        )
        mb.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        if mb.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        targets = (self.settings.data.get("speedtest", {}) or {}).get("targets", [])
        idx = max(0, self.cmb_speed_target.currentIndex())
        host = targets[idx].get("host") if idx < len(targets) else "1.1.1.1" or "1.1.1.1"

        samples: List[Optional[float]] = []
        lost = 0
        for _ in range(10):
            p = subprocess.run(_ping_cmd(host), capture_output=True, text=True)
            out = (p.stdout or "") + "\n" + (p.stderr or "")
            ms = _parse_ping_ms(out)
            if ms is None or p.returncode != 0:
                lost += 1
                samples.append(None)
            else:
                samples.append(ms)
            QtWidgets.QApplication.processEvents()
            time.sleep(0.08)

        oks = [x for x in samples if x is not None]
        loss = lost / 10.0
        ping = statistics.mean(oks) if oks else 0.0
        jitter = statistics.pstdev(oks) if len(oks) >= 2 else 0.0
        self.lbl_pingtest.setText(f"Result: Ping {ping:.0f} ms | Jitter {jitter:.1f} ms | Loss {loss*100:.0f}% (10 pings)")

    def _run_advanced_speedtest(self):
        # Confirm (mandatory) - network impacting
        mb = QtWidgets.QMessageBox(self)
        mb.setWindowTitle("Confirm Advanced Speed Test")
        mb.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        mb.setText(
            "This test will download and upload data and may affect your internet, latency, or data usage.\n"
            "Only run if you explicitly want a bandwidth test. Continue?"
        )
        mb.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        if mb.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        targets = (self.settings.data.get("speedtest", {}) or {}).get("targets", [])
        idx = max(0, self.cmb_speed_target.currentIndex())
        target = targets[idx] if idx < len(targets) else (targets[0] if targets else {})
        download_url = target.get("download_url") or "https://cachefly.cachefly.net/10mb.test"
        upload_eps = (self.settings.data.get("speedtest", {}) or {}).get("upload_endpoints", [])
        upload_url = upload_eps[0].get("url") if upload_eps else "https://httpbin.org/post"

        bytes_down = int((self.settings.data.get("speedtest", {}) or {}).get("advanced_download_bytes", 10_000_000))
        bytes_up = int((self.settings.data.get("speedtest", {}) or {}).get("advanced_upload_bytes", 2_000_000))

        self.pb_down.setValue(0)
        self.pb_up.setValue(0)

        self._adv_worker = AdvancedSpeedtestWorker(download_url, upload_url, bytes_down, bytes_up, self)
        self._adv_worker.progress.connect(self._on_adv_progress)
        self._adv_worker.finished.connect(self._on_adv_finished)
        self._adv_worker.failed.connect(self._on_adv_failed)
        self.btn_advtest.setEnabled(False)
        self._adv_worker.start()

    def _on_adv_progress(self, phase: str, pc: int):
        if phase == "download":
            self.pb_down.setValue(pc)
            self.pb_down.setFormat(f"{pc}%")
        else:
            self.pb_up.setValue(pc)
            self.pb_up.setFormat(f"{pc}%")

    def _on_adv_finished(self, res: dict):
        self._adv_last_results = res
        d = res.get("download_mbps")
        u = res.get("upload_mbps")
        self.lbl_pingtest.setText(f"Advanced: Down {human_mbps(d)} | Up {human_mbps(u)}")
        # update bitrate helper based on measured upload
        if u:
            self.spin_upload_mbps.setValue(float(u))
        self.btn_advtest.setEnabled(self.chk_advanced.isChecked())

    def _on_adv_failed(self, err: str):
        self.lbl_pingtest.setText(f"Advanced failed: {err}")
        self.btn_advtest.setEnabled(self.chk_advanced.isChecked())


# ---------------------
# VPN Manager Page
# ---------------------

class VPNManagerPage(QtWidgets.QWidget):
    def __init__(self, engine: EngineManager, settings: SettingsManager):
        super().__init__()
        self.engine = engine
        self.settings = settings

        self._build()
        self._wire()
        self._refresh()

    def _build(self):
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(12)

        # left: configs
        gb_cfg = QtWidgets.QGroupBox("Configs")
        cfg_layout = QtWidgets.QVBoxLayout(gb_cfg)

        self.txt_import = QtWidgets.QTextEdit()
        self.txt_import.setPlaceholderText("Paste configs here (links, sing-box JSON, WireGuard ini)...")
        self.txt_import.setFixedHeight(120)

        self.btn_import = QtWidgets.QPushButton("Import")
        self.btn_import.setMinimumHeight(44)

        self.tbl_cfg = QtWidgets.QTableWidget(0, 4)
        self.tbl_cfg.setHorizontalHeaderLabels(["Name", "Type", "Core", "Added"])
        self.tbl_cfg.horizontalHeader().setStretchLastSection(True)
        self.tbl_cfg.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_cfg.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

        proto_row = QtWidgets.QHBoxLayout()
        self.cmb_proto_filter = QtWidgets.QComboBox()
        self.cmb_proto_filter.addItems(["All protocols", "SOCKS", "HTTP", "WireGuard", "Hysteria2"])
        self.lbl_proto_status = QtWidgets.QLabel("Protocol support: -")
        proto_row.addWidget(QtWidgets.QLabel("Filter:"))
        proto_row.addWidget(self.cmb_proto_filter)
        proto_row.addWidget(self.lbl_proto_status, 1)

        self.btn_set_active = QtWidgets.QPushButton("Use Selected Config for Active Profile")
        self.btn_set_active.setMinimumHeight(44)

        cfg_layout.addWidget(self.txt_import)
        cfg_layout.addWidget(self.btn_import)
        cfg_layout.addLayout(proto_row)
        cfg_layout.addWidget(self.tbl_cfg, 1)
        cfg_layout.addWidget(self.btn_set_active)

        # right: subscriptions + clipboard
        gb_sub = QtWidgets.QGroupBox("Subscriptions & Clipboard")
        r = QtWidgets.QVBoxLayout(gb_sub)

        self.txt_sub = QtWidgets.QLineEdit()
        self.txt_sub.setPlaceholderText("Subscription URL (v2rayN style)")
        self.btn_add_sub = QtWidgets.QPushButton("Add Subscription")
        self.btn_add_sub.setMinimumHeight(44)

        self.lst_subs = QtWidgets.QListWidget()
        self.btn_update_sub = QtWidgets.QPushButton("Update Selected Subscription")
        self.btn_update_sub.setMinimumHeight(44)

        self.btn_clip_import = QtWidgets.QPushButton("Import from Clipboard")
        self.btn_clip_import.setMinimumHeight(44)
        self.btn_clip_export = QtWidgets.QPushButton("Export Selected Config to Clipboard")
        self.btn_clip_export.setMinimumHeight(44)

        r.addWidget(self.txt_sub)
        r.addWidget(self.btn_add_sub)
        r.addWidget(self.lst_subs, 1)
        r.addWidget(self.btn_update_sub)
        r.addSpacing(10)
        r.addWidget(self.btn_clip_import)
        r.addWidget(self.btn_clip_export)

        top.addWidget(gb_cfg, 2)
        top.addWidget(gb_sub, 1)

        layout.addLayout(top, 1)

        self.lbl_status = QtWidgets.QLabel("Auto-detect: ON (core + config type).")
        layout.addWidget(self.lbl_status)

        core_actions = QtWidgets.QHBoxLayout()
        self.lbl_active_core = QtWidgets.QLabel("Active config: -")
        self.cmb_profile_select = QtWidgets.QComboBox()
        self.cmb_profile_select.addItems(self.settings.get_profile_names())
        self.btn_set_profile = QtWidgets.QPushButton("Set Profile")
        self.btn_start_core = QtWidgets.QPushButton("Start Core")
        self.btn_stop_core = QtWidgets.QPushButton("Stop Core")
        for b in (self.btn_set_profile, self.btn_start_core, self.btn_stop_core):
            b.setMinimumHeight(40)
        core_actions.addWidget(self.lbl_active_core, 1)
        core_actions.addWidget(self.cmb_profile_select)
        core_actions.addWidget(self.btn_set_profile)
        core_actions.addWidget(self.btn_start_core)
        core_actions.addWidget(self.btn_stop_core)
        layout.addLayout(core_actions)

    def _wire(self):
        self.btn_import.clicked.connect(self._import_text)
        self.btn_clip_import.clicked.connect(self._import_clipboard)
        self.btn_clip_export.clicked.connect(self._export_clipboard)
        self.btn_add_sub.clicked.connect(self._add_sub)
        self.btn_update_sub.clicked.connect(self._update_sub)
        self.btn_set_active.clicked.connect(self._set_active_config)
        self.btn_set_profile.clicked.connect(self._set_profile_for_vpn)
        self.cmb_proto_filter.currentTextChanged.connect(self._refresh)
        self.btn_start_core.clicked.connect(self._start_core)
        self.btn_stop_core.clicked.connect(self._stop_core)

    def _refresh(self):
        # configs table
        cfgs = self.settings.data.get("configs", []) or []
        filter_text = self.cmb_proto_filter.currentText() if hasattr(self, "cmb_proto_filter") else "All protocols"
        selected_type = {
            "SOCKS": "socks",
            "HTTP": "http",
            "WireGuard": "wireguard",
            "Hysteria2": "hysteria2",
        }.get(filter_text)

        shown_cfgs = [c for c in cfgs if (not selected_type or str(c.get("type", "")).lower() == selected_type)]

        self.tbl_cfg.setRowCount(0)
        for c in shown_cfgs:
            row = self.tbl_cfg.rowCount()
            self.tbl_cfg.insertRow(row)
            self.tbl_cfg.setItem(row, 0, QtWidgets.QTableWidgetItem(str(c.get("name", ""))))
            self.tbl_cfg.setItem(row, 1, QtWidgets.QTableWidgetItem(str(c.get("type", ""))))
            self.tbl_cfg.setItem(row, 2, QtWidgets.QTableWidgetItem(str(c.get("core", "auto"))))
            self.tbl_cfg.setItem(row, 3, QtWidgets.QTableWidgetItem(str(c.get("added_at", ""))))
        self.tbl_cfg.resizeColumnsToContents()

        counts = {"socks": 0, "http": 0, "wireguard": 0, "hysteria2": 0}
        for c in cfgs:
            t = str(c.get("type", "")).lower()
            if t in counts:
                counts[t] += 1
        self.lbl_proto_status.setText(
            f"Protocol support: SOCKS {counts['socks']} | HTTP {counts['http']} | WireGuard {counts['wireguard']} | Hysteria2 {counts['hysteria2']}"
        )

        # subs
        self.lst_subs.clear()
        for u in self.settings.data.get("subscriptions", []) or []:
            self.lst_subs.addItem(u)

        # active config label
        active_profile = self.settings.get_active_profile()
        if hasattr(self, "cmb_profile_select"):
            self.cmb_profile_select.setCurrentText(active_profile)
        active_idx = (self.settings.data.get("profiles", {}) or {}).get("items", {}).get(active_profile, {}).get("active_config_idx")
        if active_idx is not None and 0 <= active_idx < len(cfgs):
            self.lbl_active_core.setText(f"Active config: {cfgs[active_idx].get('name','')}")
        else:
            self.lbl_active_core.setText("Active config: -")

    def _import_text(self):
        txt = self.txt_import.toPlainText().strip()
        n = self.settings.process_smart_input(txt)
        self.txt_import.clear()
        self._refresh()
        QtWidgets.QMessageBox.information(self, "Import", f"Imported {n} configs.")

    def _import_clipboard(self):
        txt = QtWidgets.QApplication.clipboard().text()
        n = self.settings.process_smart_input(txt)
        self._refresh()
        QtWidgets.QMessageBox.information(self, "Clipboard Import", f"Imported {n} configs from clipboard.")

    def _export_clipboard(self):
        row = self.tbl_cfg.currentRow()
        cfgs = self.settings.data.get("configs", []) or []
        if row < 0 or row >= len(cfgs):
            return
        QtWidgets.QApplication.clipboard().setText(cfgs[row].get("raw", ""))
        QtWidgets.QMessageBox.information(self, "Clipboard Export", "Selected config copied to clipboard.")

    def _add_sub(self):
        url = self.txt_sub.text().strip()
        if not url:
            return
        if self.settings.add_subscription(url):
            self.txt_sub.clear()
            self._refresh()

    def _update_sub(self):
        item = self.lst_subs.currentItem()
        if not item:
            return
        url = item.text()
        self.btn_update_sub.setEnabled(False)
        self._w = SubscriptionUpdateWorker(self.settings, url, self)
        self._w.done.connect(self._on_sub_done)
        self._w.failed.connect(self._on_sub_failed)
        self._w.start()

    def _on_sub_done(self, n: int):
        self.btn_update_sub.setEnabled(True)
        self._refresh()
        QtWidgets.QMessageBox.information(self, "Subscription", f"Added {n} configs from subscription.")

    def _on_sub_failed(self, err: str):
        self.btn_update_sub.setEnabled(True)
        QtWidgets.QMessageBox.warning(self, "Subscription", f"Update failed:\n{err}")

    def _visible_configs(self) -> List[Dict[str, Any]]:
        cfgs = self.settings.data.get("configs", []) or []
        filter_text = self.cmb_proto_filter.currentText() if hasattr(self, "cmb_proto_filter") else "All protocols"
        selected_type = {
            "SOCKS": "socks",
            "HTTP": "http",
            "WireGuard": "wireguard",
            "Hysteria2": "hysteria2",
        }.get(filter_text)
        return [c for c in cfgs if (not selected_type or str(c.get("type", "")).lower() == selected_type)]

    def _set_active_config(self):
        row = self.tbl_cfg.currentRow()
        visible = self._visible_configs()
        all_cfgs = self.settings.data.get("configs", []) or []
        if row < 0 or row >= len(visible):
            return
        selected = visible[row]
        target_raw = (selected.get("raw") or "").strip()
        target_idx = -1
        for i, c in enumerate(all_cfgs):
            if (c.get("raw") or "").strip() == target_raw:
                target_idx = i
                break
        if target_idx < 0:
            return
        active_profile = self.settings.get_active_profile()
        self.settings.data.setdefault("profiles", {}).setdefault("items", {}).setdefault(active_profile, {})["active_config_idx"] = target_idx
        self.settings.save()
        QtWidgets.QMessageBox.information(self, "Active Config", f"Selected config set for profile: {active_profile}")
        self._refresh()

    def _start_core(self):
        cfgs = self.settings.data.get("configs", []) or []
        active_profile = self.settings.get_active_profile()
        active_idx = (self.settings.data.get("profiles", {}) or {}).get("items", {}).get(active_profile, {}).get("active_config_idx")
        if active_idx is None or not (0 <= active_idx < len(cfgs)):
            QtWidgets.QMessageBox.warning(self, "Start Core", "No active config selected for current profile.")
            return
        ok = self.engine.start_core_with_config(cfgs[active_idx])
        if not ok:
            QtWidgets.QMessageBox.warning(self, "Start Core", "Failed to start core. Check logs for details.")

    def _stop_core(self):
        self.engine.stop_core()
        QtWidgets.QMessageBox.information(self, "Stop Core", "Core stop requested.")

    def _set_profile_for_vpn(self):
        profile = self.cmb_profile_select.currentText() if hasattr(self, "cmb_profile_select") else ""
        if not profile:
            return
        self.settings.set_active_profile(profile)
        self._refresh()


# ---------------------
# App Launcher Page
# ---------------------

class AppLauncherPage(QtWidgets.QWidget):
    def __init__(self, engine: EngineManager, settings: SettingsManager, is_refresh_paused_cb: Optional[Callable[[], bool]] = None):
        super().__init__()
        self.engine = engine
        self.settings = settings
        self.is_refresh_paused_cb = is_refresh_paused_cb
        self._build()
        self._wire()
        self._refresh()

        self._auto_timer = QtCore.QTimer(self)
        self._auto_timer.timeout.connect(self._maybe_auto_refresh)
        self._auto_timer.start(1000)

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(12)

        gb_apps = QtWidgets.QGroupBox("App Launcher")
        g = QtWidgets.QVBoxLayout(gb_apps)

        form = QtWidgets.QHBoxLayout()
        self.in_app_name = QtWidgets.QLineEdit()
        self.in_app_name.setPlaceholderText("App name")
        self.in_app_path = QtWidgets.QLineEdit()
        self.in_app_path.setPlaceholderText("Path to EXE")
        self.btn_browse = QtWidgets.QPushButton("Browse")
        self.in_app_args = QtWidgets.QLineEdit()
        self.in_app_args.setPlaceholderText("Optional arguments")
        self.in_app_group = QtWidgets.QLineEdit()
        self.in_app_group.setPlaceholderText("Group (optional)")
        self.btn_add_app = QtWidgets.QPushButton("Add App")

        form.addWidget(self.in_app_name, 1)
        form.addWidget(self.in_app_path, 2)
        form.addWidget(self.btn_browse, 0)
        form.addWidget(self.in_app_args, 1)
        form.addWidget(self.in_app_group, 1)
        form.addWidget(self.btn_add_app, 0)

        self.tbl_apps = QtWidgets.QTableWidget(0, 9)
        self.tbl_apps.setHorizontalHeaderLabels(["Enabled", "Name", "Path", "Args", "Group", "Profile", "Last Launch", "Running", "Type"])
        self.tbl_apps.horizontalHeader().setStretchLastSection(True)
        self.tbl_apps.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_apps.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl_apps.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

        group_actions = QtWidgets.QHBoxLayout()
        self.cmb_group = QtWidgets.QComboBox()
        self.cmb_group.addItem("All groups")
        self.chk_filter_group = QtWidgets.QCheckBox("Filter list by group")
        self.btn_launch_group = QtWidgets.QPushButton("Launch Group")
        self.btn_stop_group = QtWidgets.QPushButton("Stop Group")
        for b in (self.btn_launch_group, self.btn_stop_group):
            b.setMinimumHeight(36)
        self.cmb_app_profile = QtWidgets.QComboBox()
        self.cmb_app_profile.addItems(["Auto", "Gaming", "Streaming", "Work", "Custom"])
        self.btn_set_profile = QtWidgets.QPushButton("Set Profile")
        self.btn_set_profile.setMinimumHeight(36)
        group_actions.addWidget(QtWidgets.QLabel("Group:"))
        group_actions.addWidget(self.cmb_group, 1)
        group_actions.addWidget(self.chk_filter_group)
        group_actions.addWidget(self.btn_launch_group)
        group_actions.addWidget(self.btn_stop_group)
        group_actions.addWidget(self.cmb_app_profile)
        group_actions.addWidget(self.btn_set_profile)

        actions = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("Refresh")
        self.btn_add_running = QtWidgets.QPushButton("Add Running Apps")
        self.btn_launch = QtWidgets.QPushButton("Launch Selected")
        self.btn_stop = QtWidgets.QPushButton("Stop Selected")
        self.btn_launch_enabled = QtWidgets.QPushButton("Launch Enabled")
        self.btn_stop_enabled = QtWidgets.QPushButton("Stop Enabled")
        self.chk_relaunch = QtWidgets.QCheckBox("Relaunch if running")
        self.btn_remove = QtWidgets.QPushButton("Remove Selected")
        self.btn_move_group = QtWidgets.QPushButton("Move to Group")
        for b in (
            self.btn_refresh,
            self.btn_add_running,
            self.btn_launch,
            self.btn_stop,
            self.btn_launch_enabled,
            self.btn_stop_enabled,
            self.btn_move_group,
            self.btn_remove,
        ):
            b.setMinimumHeight(40)

        actions.addWidget(self.btn_refresh)
        actions.addWidget(self.btn_add_running)
        actions.addWidget(self.btn_launch)
        actions.addWidget(self.btn_stop)
        actions.addWidget(self.btn_launch_enabled)
        actions.addWidget(self.btn_stop_enabled)
        actions.addWidget(self.chk_relaunch)
        actions.addWidget(self.btn_move_group)
        actions.addStretch(1)
        actions.addWidget(self.btn_remove)

        g.addLayout(form)
        g.addLayout(group_actions)
        g.addWidget(self.tbl_apps, 1)
        g.addLayout(actions)

        top.addWidget(gb_apps, 1)
        layout.addLayout(top, 1)

    def _wire(self):
        self.btn_browse.clicked.connect(self._browse_exe)
        self.btn_add_app.clicked.connect(self._add_app)
        self.btn_refresh.clicked.connect(self._refresh)
        self.btn_add_running.clicked.connect(self._add_running_apps)
        self.btn_launch.clicked.connect(self._launch_selected)
        self.btn_stop.clicked.connect(self._stop_selected)
        self.btn_launch_enabled.clicked.connect(self._launch_enabled)
        self.btn_stop_enabled.clicked.connect(self._stop_enabled)
        self.btn_launch_group.clicked.connect(self._launch_group)
        self.btn_stop_group.clicked.connect(self._stop_group)
        self.btn_move_group.clicked.connect(self._move_selected_to_group)
        self.btn_set_profile.clicked.connect(self._set_profile_for_selected)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.tbl_apps.itemChanged.connect(self._on_item_changed)
        self.tbl_apps.itemSelectionChanged.connect(self._save_selected_cache)
        self.cmb_group.currentTextChanged.connect(self._on_group_changed)
        self.chk_filter_group.toggled.connect(self._refresh)

    def _maybe_auto_refresh(self):
        if self.is_refresh_paused_cb and self.is_refresh_paused_cb():
            return
        ui = (self.settings.data.get("ui", {}) or {})
        if not bool(ui.get("refresh_enabled", True)):
            return
        interval_s = int(ui.get("refresh_interval_s", 60))
        if interval_s <= 0:
            return
        now = time.time()
        last = getattr(self, "_last_auto_refresh", 0.0)
        if now - last < interval_s:
            return
        self._last_auto_refresh = now
        self._refresh()

    def _all_apps(self) -> List[Dict[str, Any]]:
        apps = self.settings.data.get("apps", {}) or {}
        important = apps.get("important", []) or []
        custom = apps.get("custom", []) or []
        return [*important, *custom]

    def _refresh(self):
        running = self._find_running()
        rows = self._all_apps()
        group_filter = self._selected_group() if self.chk_filter_group.isChecked() else None
        self.tbl_apps.blockSignals(True)
        self.tbl_apps.setRowCount(0)
        groups = set()
        for app in rows:
            row = self.tbl_apps.rowCount()
            self.tbl_apps.insertRow(row)
            name = app.get("name", "")
            path = app.get("path", "")
            args = app.get("args", "")
            app_type = app.get("type", "important")
            enabled = bool(app.get("enabled", True))
            group = app.get("group", "Default") or "Default"
            profile = app.get("profile", "Auto")
            last_launch = (self.settings.data.get("apps", {}) or {}).get("last_launch", {}).get(name, "-")
            run_state = "Yes" if running.get(name.lower()) else "No"
            groups.add(group)
            if group_filter and group != group_filter:
                continue

            enabled_item = QtWidgets.QTableWidgetItem("")
            enabled_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            enabled_item.setCheckState(QtCore.Qt.CheckState.Checked if enabled else QtCore.Qt.CheckState.Unchecked)
            self.tbl_apps.setItem(row, 0, enabled_item)
            self.tbl_apps.setItem(row, 1, QtWidgets.QTableWidgetItem(str(name)))
            self.tbl_apps.setItem(row, 2, QtWidgets.QTableWidgetItem(str(path)))
            self.tbl_apps.setItem(row, 3, QtWidgets.QTableWidgetItem(str(args)))
            self.tbl_apps.setItem(row, 4, QtWidgets.QTableWidgetItem(str(group)))
            self.tbl_apps.setItem(row, 5, QtWidgets.QTableWidgetItem(str(profile)))
            self.tbl_apps.setItem(row, 6, QtWidgets.QTableWidgetItem(str(last_launch)))
            self.tbl_apps.setItem(row, 7, QtWidgets.QTableWidgetItem(run_state))
            self.tbl_apps.setItem(row, 8, QtWidgets.QTableWidgetItem(app_type))
        self.tbl_apps.blockSignals(False)
        self._restore_selected_cache()
        self.tbl_apps.resizeColumnsToContents()
        self._refresh_groups(sorted(groups))

    def _browse_exe(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select application", "", "Executable (*.exe)")
        if path:
            self.in_app_path.setText(path)
            if not self.in_app_name.text().strip():
                self.in_app_name.setText(os.path.splitext(os.path.basename(path))[0])

    def _add_app(self):
        name = self.in_app_name.text().strip()
        path = self.in_app_path.text().strip()
        args = self.in_app_args.text().strip()
        group = self.in_app_group.text().strip() or "Default"
        if not name:
            return
        if path and not os.path.exists(path):
            QtWidgets.QMessageBox.warning(self, "Invalid Path", "The selected executable does not exist.")
            return

        app = {"name": name, "path": path, "args": args, "enabled": True, "type": "custom", "group": group, "profile": "Auto"}
        apps = self.settings.data.setdefault("apps", {}).setdefault("custom", [])
        apps.append(app)
        self.settings.save()

        self.in_app_name.clear()
        self.in_app_path.clear()
        self.in_app_args.clear()
        self.in_app_group.clear()
        self._refresh()

    def _add_running_apps(self):
        apps = self.settings.data.setdefault("apps", {})
        custom = apps.setdefault("custom", [])
        existing_names = {str(a.get("name", "")).lower() for a in custom}

        added = 0
        for p in psutil.process_iter(attrs=["name", "exe"]):
            try:
                name = (p.info.get("name") or "").strip()
                exe = (p.info.get("exe") or "").strip()
                if not name or not exe:
                    continue
                lower_exe = exe.lower()
                if "\\windows\\system32" in lower_exe or "\\windows\\syswow64" in lower_exe:
                    continue
                if name.lower() in existing_names:
                    continue
                custom.append(
                    {
                        "name": name,
                        "path": exe,
                        "args": "",
                        "enabled": True,
                        "type": "custom",
                        "group": "Detected",
                        "profile": "Auto",
                    }
                )
                existing_names.add(name.lower())
                added += 1
            except Exception:
                continue

        if added:
            self.settings.save()
        self._refresh()
        QtWidgets.QMessageBox.information(self, "Detected Apps", f"Added {added} running app(s).")

    def _selected_app_names(self) -> List[str]:
        items = self.tbl_apps.selectedItems()
        rows = sorted({it.row() for it in items})
        names = []
        for r in rows:
            name_item = self.tbl_apps.item(r, 1)
            if name_item:
                names.append(name_item.text())
        return names

    def _selected_apps(self) -> List[Dict[str, Any]]:
        names = set(self._selected_app_names())
        if not names:
            return []
        return [app for app in self._all_apps() if app.get("name") in names]

    def _save_selected_cache(self):
        apps = self.settings.data.setdefault("apps", {})
        current = self._selected_app_names()
        prev = apps.get("last_selected", []) or []
        if prev == current:
            return
        apps["last_selected"] = current
        self.settings.save()

    def _restore_selected_cache(self):
        saved = (self.settings.data.get("apps", {}) or {}).get("last_selected", []) or []
        if not saved:
            return
        wanted = set(str(x) for x in saved)
        self.tbl_apps.blockSignals(True)
        for row in range(self.tbl_apps.rowCount()):
            name_item = self.tbl_apps.item(row, 1)
            if name_item and name_item.text() in wanted:
                self.tbl_apps.selectRow(row)
        self.tbl_apps.blockSignals(False)

    def _find_running(self) -> Dict[str, bool]:
        running = {}
        for p in psutil.process_iter(attrs=["name", "exe"]):
            try:
                name = (p.info.get("name") or "").lower()
                exe = (p.info.get("exe") or "").lower()
                if name:
                    running[name] = True
                if exe:
                    running[exe] = True
            except Exception:
                continue
        return running

    def _match_processes(self, app: Dict[str, Any]) -> List[psutil.Process]:
        matches = []
        name = (app.get("name") or "").lower()
        path = (app.get("path") or "").lower()
        for p in psutil.process_iter(attrs=["name", "exe"]):
            try:
                pname = (p.info.get("name") or "").lower()
                pexe = (p.info.get("exe") or "").lower()
                if (path and pexe == path) or (name and pname == name):
                    matches.append(p)
            except Exception:
                continue
        return matches

    def _set_active_profile_runtime(self, profile: str):
        profile = str(profile or "").strip()
        if not profile or profile == "Auto":
            return
        self.settings.data.setdefault("profiles", {})["active"] = profile

    def _launch_apps_batch(self, apps: List[Dict[str, Any]], relaunch: bool):
        prev_profile = self.settings.get_active_profile()
        try:
            for app in apps:
                self._set_active_profile_runtime(app.get("profile", "Auto"))
                self._launch_app(app, relaunch)
        finally:
            self._set_active_profile_runtime(prev_profile)

    def _launch_app(self, app: Dict[str, Any], relaunch: bool):
        matches = self._match_processes(app)
        if matches and not relaunch:
            return
        if matches and relaunch:
            for p in matches:
                try:
                    p.terminate()
                except Exception:
                    continue
        path = app.get("path") or ""
        if not path:
            return
        args = _split_args(app.get("args", ""))
        try:
            subprocess.Popen([path, *args])
            self._mark_last_launch(app.get("name", ""))
        except Exception:
            pass

    def _launch_selected(self):
        names = self._selected_app_names()
        apps = self._all_apps()
        relaunch = self.chk_relaunch.isChecked()
        selected = [app for app in apps if app.get("name") in names]
        self._launch_apps_batch(selected, relaunch)
        self._refresh()

    def _launch_enabled(self):
        apps = self._all_apps()
        relaunch = self.chk_relaunch.isChecked()
        enabled = [app for app in apps if app.get("enabled", True)]
        self._launch_apps_batch(enabled, relaunch)
        self._refresh()

    def _stop_selected(self):
        names = self._selected_app_names()
        apps = self._all_apps()
        for app in apps:
            if app.get("name") in names:
                for p in self._match_processes(app):
                    try:
                        p.terminate()
                    except Exception:
                        continue
        self._refresh()

    def _stop_enabled(self):
        apps = self._all_apps()
        for app in apps:
            if app.get("enabled", True):
                for p in self._match_processes(app):
                    try:
                        p.terminate()
                    except Exception:
                        continue
        self._refresh()

    def _move_selected_to_group(self):
        apps = self._selected_apps()
        if not apps:
            return
        group, ok = QtWidgets.QInputDialog.getText(self, "Move to Group", "Group name:")
        if not ok:
            return
        group = group.strip() or "Default"
        store = self.settings.data.get("apps", {}) or {}
        for app in store.get("important", []):
            if app.get("name") in {a.get("name") for a in apps}:
                app["group"] = group
        for app in store.get("custom", []):
            if app.get("name") in {a.get("name") for a in apps}:
                app["group"] = group
        self.settings.data["apps"] = store
        self.settings.save()
        self._refresh()

    def _set_profile_for_selected(self):
        apps = self._selected_apps()
        if not apps:
            return
        profile = self.cmb_app_profile.currentText()
        store = self.settings.data.get("apps", {}) or {}
        names = {a.get("name") for a in apps}
        for app in store.get("important", []):
            if app.get("name") in names:
                app["profile"] = profile
        for app in store.get("custom", []):
            if app.get("name") in names:
                app["profile"] = profile
        self.settings.data["apps"] = store
        self.settings.save()
        self._refresh()

    def _remove_selected(self):
        names = set(self._selected_app_names())
        apps = self.settings.data.get("apps", {}) or {}
        custom = apps.get("custom", []) or []
        apps["custom"] = [a for a in custom if a.get("name") not in names]
        self.settings.data["apps"] = apps
        self.settings.save()
        self._refresh()

    def _on_item_changed(self, item: QtWidgets.QTableWidgetItem):
        if item.column() != 0:
            return
        row = item.row()
        name_item = self.tbl_apps.item(row, 1)
        type_item = self.tbl_apps.item(row, 8)
        if not name_item or not type_item:
            return
        name = name_item.text()
        app_type = type_item.text()
        enabled = item.checkState() == QtCore.Qt.CheckState.Checked

        apps = self.settings.data.get("apps", {}) or {}
        bucket = apps.get("custom", []) if app_type == "custom" else apps.get("important", [])
        for app in bucket:
            if app.get("name") == name:
                app["enabled"] = enabled
                break
        self.settings.data["apps"] = apps
        self.settings.save()

    def _mark_last_launch(self, name: str):
        name = (name or "").strip()
        if not name:
            return
        apps = self.settings.data.setdefault("apps", {})
        last = apps.setdefault("last_launch", {})
        last[name] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.settings.save()

    def _refresh_groups(self, groups: List[str]):
        current = self.cmb_group.currentText()
        saved = (self.settings.data.get("apps", {}) or {}).get("last_group", "All groups")
        self.cmb_group.blockSignals(True)
        self.cmb_group.clear()
        self.cmb_group.addItem("All groups")
        for group in groups:
            self.cmb_group.addItem(group)
        if current and (current in groups or current == "All groups"):
            self.cmb_group.setCurrentText(current)
        elif saved and (saved in groups or saved == "All groups"):
            self.cmb_group.setCurrentText(saved)
        self.cmb_group.blockSignals(False)

    def _selected_group(self) -> Optional[str]:
        group = self.cmb_group.currentText()
        if group == "All groups":
            return None
        return group

    def _on_group_changed(self, group: str):
        apps = self.settings.data.setdefault("apps", {})
        apps["last_group"] = group
        self.settings.save()

    def _launch_group(self):
        group = self._selected_group()
        if not group:
            return
        relaunch = self.chk_relaunch.isChecked()
        grouped = [app for app in self._all_apps() if (app.get("group") or "Default") == group]
        self._launch_apps_batch(grouped, relaunch)
        self._refresh()

    def _stop_group(self):
        group = self._selected_group()
        if not group:
            return
        for app in self._all_apps():
            if (app.get("group") or "Default") == group:
                for p in self._match_processes(app):
                    try:
                        p.terminate()
                    except Exception:
                        continue
        self._refresh()


# ---------------------
# App Routing Page
# ---------------------

class AppRoutingPage(QtWidgets.QWidget):
    def __init__(
        self,
        engine: EngineManager,
        settings: SettingsManager,
        is_refresh_paused_cb: Optional[Callable[[], bool]] = None,
    ):
        super().__init__()
        self.engine = engine
        self.settings = settings
        self.is_refresh_paused_cb = is_refresh_paused_cb
        self.scanner = NetworkScanner()

        self._build()
        self._wire()
        self._refresh()

        self._auto_timer = QtCore.QTimer(self)
        self._auto_timer.timeout.connect(self._maybe_auto_refresh)
        self._auto_timer.start(1000)

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(12)

        gb_list = QtWidgets.QGroupBox("Applications")
        gl = QtWidgets.QVBoxLayout(gb_list)

        self.chk_active_only = QtWidgets.QCheckBox("Show only apps using network now")
        self.chk_active_only.setChecked(True)

        self.cmb_app_filter = QtWidgets.QComboBox()
        self.cmb_app_filter.addItems([
            "Apps only (recommended)",
            "Apps + background",
            "Include Windows services",
        ])

        self.lst_apps = QtWidgets.QListWidget()
        self.lst_apps.setMinimumWidth(330)

        self.btn_refresh = QtWidgets.QPushButton("Refresh")
        self.btn_refresh.setMinimumHeight(44)

        gl.addWidget(self.chk_active_only)
        gl.addWidget(self.cmb_app_filter)
        gl.addWidget(self.lst_apps, 1)
        gl.addWidget(self.btn_refresh)

        gb_route = QtWidgets.QGroupBox("Routing Rules (per-app)")
        gr = QtWidgets.QFormLayout(gb_route)
        gr.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        gr.setFormAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        gr.setVerticalSpacing(12)

        # combos (bigger heights so text is readable)
        self.cmb_dns = QtWidgets.QComboBox()
        self.cmb_vpn = QtWidgets.QComboBox()
        self.cmb_iface = QtWidgets.QComboBox()
        self.cmb_prio = QtWidgets.QComboBox()

        for cb in (self.cmb_dns, self.cmb_vpn, self.cmb_iface, self.cmb_prio):
            cb.setMinimumHeight(52)
            cb.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)

        self.cmb_prio.addItems(["Auto", "High", "Normal", "Low"])

        self.lbl_current = QtWidgets.QLabel("Current: -")
        self.lbl_current.setWordWrap(True)

        self.lbl_iface_info = QtWidgets.QLabel("Interface info: -")
        self.lbl_iface_info.setWordWrap(True)

        self.btn_apply = QtWidgets.QPushButton("Apply")
        self.btn_apply.setMinimumHeight(46)

        self.btn_reset_dns = QtWidgets.QPushButton("Reset DNS for App")
        self.btn_reset_dns.setMinimumHeight(40)

        self.btn_obs_quick = QtWidgets.QPushButton("Apply OBS Quick Profile")
        self.btn_obs_quick.setMinimumHeight(40)

        gr.addRow("DNS", self.cmb_dns)
        gr.addRow("VPN Route", self.cmb_vpn)
        gr.addRow("Interface", self.cmb_iface)
        gr.addRow("Priority", self.cmb_prio)
        gr.addRow("Selected App", self.lbl_current)
        gr.addRow("Interface info", self.lbl_iface_info)
        gr.addRow("", self.btn_apply)
        gr.addRow("", self.btn_reset_dns)
        gr.addRow("", self.btn_obs_quick)

        top.addWidget(gb_list, 1)
        top.addWidget(gb_route, 2)

        layout.addLayout(top, 1)

    def _wire(self):
        self.btn_refresh.clicked.connect(self._refresh)
        self.chk_active_only.toggled.connect(self._refresh)
        self.cmb_app_filter.currentIndexChanged.connect(self._refresh)
        self.lst_apps.currentItemChanged.connect(lambda *_: self._load_app_rule())
        self.cmb_iface.currentTextChanged.connect(lambda *_: self._update_iface_info())
        self.btn_apply.clicked.connect(self._apply)
        self.btn_reset_dns.clicked.connect(self._reset_dns_for_app)
        self.btn_obs_quick.clicked.connect(self._apply_obs_quick_profile)

    def refresh_now(self):
        self._refresh()

    def _maybe_auto_refresh(self):
        if self.is_refresh_paused_cb and self.is_refresh_paused_cb():
            return
        ui = (self.settings.data.get("ui", {}) or {})
        if not bool(ui.get("refresh_enabled", True)):
            return
        interval_s = int(ui.get("refresh_interval_s", 60))
        if interval_s <= 0:
            return
        now = time.time()
        last = getattr(self, "_last_auto_refresh", 0.0)
        if now - last < interval_s:
            return
        self._last_auto_refresh = now
        self._refresh()

    def _refresh(self):
        self.lst_apps.clear()

        active_only = self.chk_active_only.isChecked()
        procs = self.scanner.list_processes(only_network_active=active_only)

        # Filter processes: Apps only / Apps+background / Include services
        mode = self.cmb_app_filter.currentText() if hasattr(self, "cmb_app_filter") else "Apps only (recommended)"
        filtered = []
        my_pid = os.getpid()

        for p in procs:
            if p.pid == my_pid:
                continue
            nm = (p.name or "").lower()
            if "umbra" in nm:
                continue

            # crude classification
            is_system = nm in ("system", "system idle process", "idle", "services.exe", "wininit.exe", "csrss.exe", "lsass.exe", "smss.exe", "fontdrvhost.exe", "dwm.exe", "spoolsv.exe")
            exe_is_system = False
            user_is_system = False
            try:
                pr = psutil.Process(p.pid)
                exe = (pr.exe() or "").lower()
                exe_is_system = ("\\windows\\system32" in exe) or ("\\windows\\syswow64" in exe)
                user = (pr.username() or "").lower()
                user_is_system = user in ("nt authority\\system", "system", "local service", "nt authority\\local service", "nt authority\\network service")
            except Exception:
                pass

            if mode.startswith("Apps only"):
                if is_system or exe_is_system or user_is_system:
                    continue
            elif mode.startswith("Apps + background"):
                # hide core Windows services, but allow non-system background apps
                if is_system and (exe_is_system or user_is_system):
                    continue
            # else Include services => no filtering

            filtered.append(p)

        for p in filtered[:250]:
            self.lst_apps.addItem(f"{p.name} (PID {p.pid})")

        # dns
        self.cmb_dns.clear()
        self.cmb_dns.addItem("AUTO")
        servers = (self.settings.data.get("dns", {}) or {}).get("servers", [])
        for s in servers:
            self.cmb_dns.addItem(f"{s.get('name','')} - {s.get('server','')} [{s.get('loc','')}]")

        # vpn configs
        self.cmb_vpn.clear()
        self.cmb_vpn.addItem("AUTO")
        cfgs = self.settings.data.get("configs", []) or []
        for i, c in enumerate(cfgs):
            self.cmb_vpn.addItem(f"{i}: {c.get('name','')}")

        # interfaces
        self._iface_meta = self._collect_interface_meta()
        self.cmb_iface.clear()
        self.cmb_iface.addItem("AUTO")
        for name in self._iface_meta:
            self.cmb_iface.addItem(name)

        self._load_app_rule()
        self._update_iface_info()

    def _selected_app_key(self) -> Optional[str]:
        item = self.lst_apps.currentItem()
        if not item:
            return None
        # keep key as app name (without pid) for stable routing
        text = item.text()
        name = text.split("(PID")[0].strip()
        return name

    def _load_app_rule(self):
        key = self._selected_app_key()
        if not key:
            self.lbl_current.setText("Current: -")
            self._update_iface_info()
            return

        self.lbl_current.setText(f"Current: {key}")

        dns_map = self.settings.data.get("app_dns_routes", {}) or {}
        vpn_map = self.settings.data.get("app_vpn_routes", {}) or {}
        if_map = self.settings.data.get("app_interfaces", {}) or {}
        pr_map = self.settings.data.get("app_priorities", {}) or {}

        dns_val = dns_map.get(key, "AUTO")
        vpn_val = vpn_map.get(key, "AUTO")
        if_val = if_map.get(key, "AUTO")
        pr_val = pr_map.get(key, "Auto")

        # set comboboxes
        self._select_combo_by_prefix(self.cmb_dns, dns_val)
        self._select_combo_by_prefix(self.cmb_vpn, str(vpn_val))
        self._select_combo_exact(self.cmb_iface, if_val)
        self._select_combo_exact(self.cmb_prio, pr_val)
        self._update_iface_info()

    def _collect_interface_meta(self) -> Dict[str, Dict[str, str]]:
        iface_addrs = psutil.net_if_addrs() or {}
        gw_by_iface = self._default_gateways_by_iface()
        meta: Dict[str, Dict[str, str]] = {}
        for iface, entries in iface_addrs.items():
            ipv4 = "-"
            subnet = "-"
            for a in entries:
                if getattr(a, "family", None) != socket.AF_INET:
                    continue
                if a.address:
                    ipv4 = str(a.address)
                if a.netmask:
                    try:
                        subnet = str(ipaddress.IPv4Network(f"0.0.0.0/{a.netmask}").prefixlen)
                        subnet = f"/{subnet}"
                    except Exception:
                        subnet = str(a.netmask)
                break
            meta[iface] = {
                "ip": ipv4,
                "subnet": subnet,
                "gateway": gw_by_iface.get(iface, "-"),
            }
        return dict(sorted(meta.items(), key=lambda kv: kv[0].lower()))

    def _default_gateways_by_iface(self) -> Dict[str, str]:
        gw: Dict[str, str] = {}
        try:
            sys_name = platform.system().lower()
            if sys_name.startswith("linux"):
                out = subprocess.check_output(["ip", "route", "show", "default"], text=True, stderr=subprocess.DEVNULL)
                for line in out.splitlines():
                    parts = line.split()
                    if "via" in parts and "dev" in parts:
                        gw_ip = parts[parts.index("via") + 1]
                        iface = parts[parts.index("dev") + 1]
                        if iface and gw_ip and iface not in gw:
                            gw[iface] = gw_ip
            elif sys_name.startswith("win"):
                out = subprocess.check_output(["route", "print", "-4"], text=True, stderr=subprocess.DEVNULL)
                in_defaults = False
                for line in out.splitlines():
                    t = line.strip()
                    if t.startswith("Active Routes:"):
                        in_defaults = True
                        continue
                    if in_defaults and t.startswith("===="):
                        break
                    if not in_defaults or not t or not t[0].isdigit():
                        continue
                    cols = t.split()
                    if len(cols) >= 4 and cols[0] == "0.0.0.0" and cols[1] == "0.0.0.0":
                        gw.setdefault(cols[3], cols[2])
            elif sys_name.startswith("darwin"):
                out = subprocess.check_output(["route", "-n", "get", "default"], text=True, stderr=subprocess.DEVNULL)
                default_gw = ""
                for line in out.splitlines():
                    line = line.strip()
                    if line.startswith("gateway:"):
                        default_gw = line.split(":", 1)[1].strip()
                    elif line.startswith("interface:"):
                        iface = line.split(":", 1)[1].strip()
                        if iface and default_gw:
                            gw[iface] = default_gw
        except Exception:
            return gw
        return gw

    def _update_iface_info(self):
        iface = self.cmb_iface.currentText() if hasattr(self, "cmb_iface") else "AUTO"
        if iface == "AUTO":
            self.lbl_iface_info.setText("Interface info: AUTO (system default route)")
            return
        meta = getattr(self, "_iface_meta", {}) or {}
        info = meta.get(iface)
        if not info:
            self.lbl_iface_info.setText("Interface info: -")
            return
        self.lbl_iface_info.setText(
            f"Interface info: IP {info.get('ip','-')} | Subnet {info.get('subnet','-')} | Gateway {info.get('gateway','-')}"
        )

    def _apply(self):
        key = self._selected_app_key()
        if not key:
            return

        dns_map = self.settings.data.setdefault("app_dns_routes", {})
        vpn_map = self.settings.data.setdefault("app_vpn_routes", {})
        if_map = self.settings.data.setdefault("app_interfaces", {})
        pr_map = self.settings.data.setdefault("app_priorities", {})

        dns_map[key] = self._extract_setting(self.cmb_dns)
        vpn_map[key] = self._extract_setting(self.cmb_vpn)
        if_map[key] = self.cmb_iface.currentText()
        pr_map[key] = self.cmb_prio.currentText()

        self.settings.save()
        QtWidgets.QMessageBox.information(self, "Applied", "Routing saved (Apply).")

    def _reset_dns_for_app(self):
        key = self._selected_app_key()
        if not key:
            return
        dns_map = self.settings.data.setdefault("app_dns_routes", {})
        if key in dns_map:
            del dns_map[key]
            self.settings.save()
        self._select_combo_by_prefix(self.cmb_dns, "AUTO")
        QtWidgets.QMessageBox.information(self, "DNS Reset", f"DNS reset to AUTO for {key}.")

    def _apply_obs_quick_profile(self):
        dns_val = self._extract_setting(self.cmb_dns)
        vpn_val = self._extract_setting(self.cmb_vpn)
        iface_val = self.cmb_iface.currentText()
        prio_val = self.cmb_prio.currentText() or "High"
        if prio_val == "Auto":
            prio_val = "High"

        dns_map = self.settings.data.setdefault("app_dns_routes", {})
        vpn_map = self.settings.data.setdefault("app_vpn_routes", {})
        if_map = self.settings.data.setdefault("app_interfaces", {})
        pr_map = self.settings.data.setdefault("app_priorities", {})

        obs_keys = ("obs64.exe", "obs.exe")
        for key in obs_keys:
            dns_map[key] = dns_val
            vpn_map[key] = vpn_val
            if_map[key] = iface_val
            pr_map[key] = prio_val

        self.settings.save()
        QtWidgets.QMessageBox.information(
            self,
            "OBS Quick Profile",
            "Quick profile saved for OBS (obs64.exe / obs.exe).",
        )

    def _extract_setting(self, cmb: QtWidgets.QComboBox) -> str:
        text = cmb.currentText()
        if text.startswith("AUTO"):
            return "AUTO"
        return text

    def _select_combo_by_prefix(self, cmb: QtWidgets.QComboBox, pref: str):
        pref = str(pref or "")
        for i in range(cmb.count()):
            if cmb.itemText(i).startswith(pref):
                cmb.setCurrentIndex(i)
                return
        cmb.setCurrentIndex(0)

    def _select_combo_exact(self, cmb: QtWidgets.QComboBox, val: str):
        val = str(val or "")
        idx = cmb.findText(val)
        cmb.setCurrentIndex(idx if idx >= 0 else 0)


# ---------------------
# Settings Page (Collapsed sections)
# ---------------------

class SettingsPage(QtWidgets.QWidget):
    def __init__(self, engine: EngineManager, settings: SettingsManager):
        super().__init__()
        self.engine = engine
        self.settings = settings
        self.updater = CoreUpdater(log=self._append_log)

        self._build()
        self._wire()
        self._refresh()

    def _build(self):
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.nav = QtWidgets.QListWidget()
        self.nav.setFixedWidth(250)
        self.nav.addItems([
            "DNS",
            "Load Balancer",
            "Updates",
            "Behavior",
            "License (disabled)",
        ])

        self.stack = QtWidgets.QStackedWidget()

        # DNS page
        self.page_dns = QtWidgets.QWidget()
        d = QtWidgets.QVBoxLayout(self.page_dns)
        self.tbl_dns = QtWidgets.QTableWidget(0, 3)
        self.tbl_dns.setHorizontalHeaderLabels(["Name", "Server", "Location"])
        self.tbl_dns.horizontalHeader().setStretchLastSection(True)

        row = QtWidgets.QHBoxLayout()
        self.in_dns_name = QtWidgets.QLineEdit()
        self.in_dns_name.setPlaceholderText("Name (optional)")
        self.in_dns_server = QtWidgets.QLineEdit()
        self.in_dns_server.setPlaceholderText("DNS server (IP or domain)")
        self.btn_add_dns = QtWidgets.QPushButton("Add DNS")
        self.btn_add_dns.setMinimumHeight(44)
        self.btn_del_dns = QtWidgets.QPushButton("Delete Selected")
        self.btn_del_dns.setMinimumHeight(44)

        preset_row = QtWidgets.QHBoxLayout()
        self.cmb_dns_preset = QtWidgets.QComboBox()
        self.btn_apply_dns_preset = QtWidgets.QPushButton("Apply Preset")
        self.btn_apply_dns_preset.setMinimumHeight(40)
        preset_row.addWidget(QtWidgets.QLabel("Preset:"))
        preset_row.addWidget(self.cmb_dns_preset, 1)
        preset_row.addWidget(self.btn_apply_dns_preset)

        row.addWidget(self.in_dns_name, 1)
        row.addWidget(self.in_dns_server, 1)
        row.addWidget(self.btn_add_dns, 0)

        d.addWidget(self.tbl_dns, 1)
        d.addLayout(row)
        d.addLayout(preset_row)
        d.addWidget(self.btn_del_dns)

        # Load balancer page
        self.page_lb = QtWidgets.QWidget()
        lb = QtWidgets.QVBoxLayout(self.page_lb)
        self.chk_auto_suggest = QtWidgets.QCheckBox("Enable Auto Suggestions (Accept/Deny)")
        self.btn_optimize = QtWidgets.QPushButton("Optimize DNS Now (Ping-based, safe)")
        self.btn_optimize.setMinimumHeight(46)
        self.lbl_opt = QtWidgets.QLabel("Status: -")

        self.tbl_opt = QtWidgets.QTableWidget(0, 5)
        self.tbl_opt.setHorizontalHeaderLabels(["Rank", "Server", "Ping", "Loss", "Jitter"])
        self.tbl_opt.horizontalHeader().setStretchLastSection(True)

        lb.addWidget(self.chk_auto_suggest)
        lb.addWidget(self.btn_optimize)
        lb.addWidget(self.lbl_opt)
        lb.addWidget(self.tbl_opt, 1)

        # Updates page
        self.page_upd = QtWidgets.QWidget()
        u = QtWidgets.QVBoxLayout(self.page_upd)
        self.btn_upd_sing = QtWidgets.QPushButton("Update sing-box")
        self.btn_upd_mihomo = QtWidgets.QPushButton("Update mihomo (Clash)")
        self.btn_upd_all = QtWidgets.QPushButton("Update All")
        self.btn_openvpn = QtWidgets.QPushButton("OpenVPN: Download/Install")
        self.btn_openconnect = QtWidgets.QPushButton("OpenConnect/Cisco: Download")

        for b in (self.btn_upd_sing, self.btn_upd_mihomo, self.btn_upd_all, self.btn_openvpn, self.btn_openconnect):
            b.setMinimumHeight(44)

        self.term = QtWidgets.QTextEdit()
        self.term.setReadOnly(True)
        self.term.setFixedHeight(230)

        u.addWidget(self.btn_upd_sing)
        u.addWidget(self.btn_upd_mihomo)
        u.addWidget(self.btn_upd_all)
        u.addWidget(self.btn_openvpn)
        u.addWidget(self.btn_openconnect)
        u.addWidget(self.term, 1)

        # Behavior page
        self.page_beh = QtWidgets.QWidget()
        b = QtWidgets.QFormLayout(self.page_beh)
        self.chk_tray = QtWidgets.QCheckBox("Enable system tray")
        self.cmb_close = QtWidgets.QComboBox()
        self.cmb_close.addItems(["minimize_to_tray", "exit"])
        self.chk_show_bitrate = QtWidgets.QCheckBox("Show bitrate helper on dashboard")
        self.chk_refresh = QtWidgets.QCheckBox("Auto refresh app list")
        self.spin_refresh = QtWidgets.QSpinBox()
        self.spin_refresh.setRange(5, 3600)
        self.spin_refresh.setSuffix(" s")
        self.chk_pause_min = QtWidgets.QCheckBox("Pause refresh when minimized")
        b.addRow("Tray", self.chk_tray)
        b.addRow("Close button action", self.cmb_close)
        b.addRow("Dashboard", self.chk_show_bitrate)
        b.addRow("Auto refresh", self.chk_refresh)
        b.addRow("Refresh interval", self.spin_refresh)
        b.addRow("Minimized", self.chk_pause_min)

        # Copilot (manual suggestions) - choose intensity
        self.cmb_copilot_mode = QtWidgets.QComboBox()
        self.cmb_copilot_mode.addItems(["Basic", "Helpful", "Expert"])
        self.btn_rerun_wizard = QtWidgets.QPushButton("Re-run First-Run Setup")
        self.btn_rerun_wizard.setMinimumHeight(40)
        self.btn_rollback = QtWidgets.QPushButton("Rollback last Apply")
        self.btn_rollback.setMinimumHeight(40)
        b.addRow("Copilot mode", self.cmb_copilot_mode)
        b.addRow("", self.btn_rerun_wizard)
        b.addRow("", self.btn_rollback)

        # License page
        self.page_lic = QtWidgets.QWidget()
        lic_layout = QtWidgets.QVBoxLayout(self.page_lic)
        lab = QtWidgets.QLabel("License system is prepared but DISABLED in this build.\n(Reserved for future update)")
        lab.setWordWrap(True)
        lab.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        lic_layout.addWidget(lab)

        self.stack.addWidget(self.page_dns)
        self.stack.addWidget(self.page_lb)
        self.stack.addWidget(self.page_upd)
        self.stack.addWidget(self.page_beh)
        self.stack.addWidget(self.page_lic)

        layout.addWidget(self.nav, 0)
        layout.addWidget(self.stack, 1)

        self.nav.setCurrentRow(0)
        self.stack.setCurrentIndex(0)

    def _wire(self):
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.btn_add_dns.clicked.connect(self._add_dns)
        self.btn_del_dns.clicked.connect(self._del_dns)
        self.btn_apply_dns_preset.clicked.connect(self._apply_dns_preset)

        self.btn_optimize.clicked.connect(self._optimize_dns)

        self.btn_upd_sing.clicked.connect(lambda: self._update_core("singbox"))
        self.btn_upd_mihomo.clicked.connect(lambda: self._update_core("clash"))
        self.btn_upd_all.clicked.connect(self._update_all)
        self.btn_openvpn.clicked.connect(self._open_openvpn)
        self.btn_openconnect.clicked.connect(self._open_openconnect)

        self.chk_tray.toggled.connect(self._save_behavior)
        self.cmb_close.currentTextChanged.connect(self._save_behavior)
        self.chk_show_bitrate.toggled.connect(self._save_behavior)
        self.chk_refresh.toggled.connect(self._save_behavior)
        self.spin_refresh.valueChanged.connect(self._save_behavior)
        self.chk_pause_min.toggled.connect(self._save_behavior)

    def _open_openvpn(self):
        # OpenVPN Community Downloads (installer/manual)
        try:
            webbrowser.open("https://openvpn.net/community-downloads/")
            self._append_log(f"[{_now_hms()}] [INFO] Opened OpenVPN download page.")
        except Exception as e:
            self._append_log(f"[{_now_hms()}] [ERROR] Failed to open OpenVPN page: {e}")

    def _open_openconnect(self):
        # OpenConnect GUI info/download page (Windows)
        try:
            webbrowser.open("https://openconnect.github.io/openconnect-gui/")
            self._append_log(f"[{_now_hms()}] [INFO] Opened OpenConnect page.")
        except Exception as e:
            self._append_log(f"[{_now_hms()}] [ERROR] Failed to open OpenConnect page: {e}")

    def _refresh(self):
        # behavior
        ui = (self.settings.data.get("ui", {}) or {})
        self.chk_tray.setChecked(bool(ui.get("tray_enabled", True)))
        self.cmb_close.setCurrentText(str(ui.get("close_action", "minimize_to_tray")))
        self.chk_show_bitrate.setChecked(bool(ui.get("show_stream_bitrate_on_dashboard", True)))
        self.chk_refresh.setChecked(bool(ui.get("refresh_enabled", True)))
        self.spin_refresh.setValue(int(ui.get("refresh_interval_s", 60)))
        self.chk_pause_min.setChecked(bool(ui.get("pause_refresh_when_minimized", True)))
        if hasattr(self, "cmb_copilot_mode"):
            self.cmb_copilot_mode.setCurrentText(self.settings.get_copilot_mode())

        # auto suggestions
        beh = (self.settings.data.get("behavior", {}) or {})
        self.chk_auto_suggest.setChecked(bool(beh.get("auto_suggestions", True)))

        # dns
        self._refresh_dns_table()
        self.cmb_dns_preset.clear()
        self.cmb_dns_preset.addItems(self.settings.dns_preset_names())

    def _save_behavior(self):
        self.settings.create_snapshot("Apply: Behavior/Copilot")
        self.settings.data.setdefault("ui", {})["tray_enabled"] = self.chk_tray.isChecked()
        self.settings.data.setdefault("ui", {})["close_action"] = self.cmb_close.currentText()
        self.settings.data.setdefault("ui", {})["show_stream_bitrate_on_dashboard"] = self.chk_show_bitrate.isChecked()
        self.settings.data.setdefault("ui", {})["refresh_enabled"] = self.chk_refresh.isChecked()
        self.settings.data.setdefault("ui", {})["refresh_interval_s"] = int(self.spin_refresh.value())
        self.settings.data.setdefault("ui", {})["pause_refresh_when_minimized"] = self.chk_pause_min.isChecked()
        # copilot mode
        if hasattr(self, "cmb_copilot_mode"):
            self.settings.set_copilot_mode(self.cmb_copilot_mode.currentText())
        self.settings.save()

    def _refresh_dns_table(self):
        servers = (self.settings.data.get("dns", {}) or {}).get("servers", []) or []
        self.tbl_dns.setRowCount(0)
        for s in servers:
            r = self.tbl_dns.rowCount()
            self.tbl_dns.insertRow(r)
            self.tbl_dns.setItem(r, 0, QtWidgets.QTableWidgetItem(str(s.get("name", ""))))
            self.tbl_dns.setItem(r, 1, QtWidgets.QTableWidgetItem(str(s.get("server", ""))))
            self.tbl_dns.setItem(r, 2, QtWidgets.QTableWidgetItem(str(s.get("loc", ""))))
        self.tbl_dns.resizeColumnsToContents()

    def _add_dns(self):
        name = self.in_dns_name.text().strip()
        server = self.in_dns_server.text().strip()
        ok = self.settings.add_dns(name, server, loc="AUTO")
        if ok:
            self.in_dns_name.clear()
            self.in_dns_server.clear()
            self._refresh_dns_table()

    def _del_dns(self):
        row = self.tbl_dns.currentRow()
        if row < 0:
            return
        self.settings.remove_dns_by_index(row)
        self._refresh_dns_table()

    def _apply_dns_preset(self):
        preset = self.cmb_dns_preset.currentText() if hasattr(self, "cmb_dns_preset") else ""
        if not preset:
            return
        if self.settings.apply_dns_preset(preset):
            self._refresh_dns_table()
            QtWidgets.QMessageBox.information(self, "DNS Preset", f"Applied preset: {preset}")

    def _optimize_dns(self):
        # user-triggered, ping-based (safe)
        servers = (self.settings.data.get("dns", {}) or {}).get("servers", []) or []
        if not servers:
            return

        self.btn_optimize.setEnabled(False)
        self.lbl_opt.setText("Status: testing (ping)...")
        self.tbl_opt.setRowCount(0)

        self._wopt = OptimizeDnsWorker(servers, self)
        self._wopt.done.connect(self._on_opt_done)
        self._wopt.failed.connect(self._on_opt_fail)
        self._wopt.start()

    def _on_opt_done(self, out: dict):
        self.btn_optimize.setEnabled(True)
        ranked = out.get("ranked", [])
        self.lbl_opt.setText(f"Status: done (top: {ranked[0][1] if ranked else '-'})")
        self.tbl_opt.setRowCount(0)

        for _, name, host, ping, loss, jitter in ranked[:10]:
            r = self.tbl_opt.rowCount()
            self.tbl_opt.insertRow(r)
            self.tbl_opt.setItem(r, 0, QtWidgets.QTableWidgetItem(str(name)))
            self.tbl_opt.setItem(r, 1, QtWidgets.QTableWidgetItem(str(host)))
            self.tbl_opt.setItem(r, 2, QtWidgets.QTableWidgetItem(f"{ping:.0f} ms" if ping < 9000 else "-"))
            self.tbl_opt.setItem(r, 3, QtWidgets.QTableWidgetItem(f"{loss*100:.0f}%"))
            self.tbl_opt.setItem(r, 4, QtWidgets.QTableWidgetItem(f"{jitter:.1f} ms"))
        self.tbl_opt.resizeColumnsToContents()

        # Suggest applying best DNS (Accept/Deny)
        if not ranked:
            return

        beh = (self.settings.data.get("behavior", {}) or {})
        if not bool(beh.get("auto_suggestions", True)):
            return

        best_idx = ranked[0][0]
        servers = (self.settings.data.get("dns", {}) or {}).get("servers", []) or []
        best = servers[best_idx] if best_idx < len(servers) else None
        if not best:
            return

        mb = QtWidgets.QMessageBox(self)
        mb.setWindowTitle("Apply suggestion?")
        mb.setIcon(QtWidgets.QMessageBox.Icon.Question)
        mb.setText(f"Suggested DNS: {best.get('name')} - {best.get('server')}\nApply as top suggestion for current profile?")
        mb.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        if mb.exec() == QtWidgets.QMessageBox.StandardButton.Yes:
            # store as profile override (does not force OS dns)
            prof = self.settings.get_active_profile()
            self.settings.data.setdefault("profiles", {}).setdefault("items", {}).setdefault(prof, {})["suggested_dns"] = best.get("server")
            self.settings.save()

    def _on_opt_fail(self, err: str):
        self.btn_optimize.setEnabled(True)
        self.lbl_opt.setText(f"Status: failed: {err}")

    # ---------------------
    # Updates terminal
    # ---------------------

    def _append_log(self, line: str):
        self.term.append(line)
        self.term.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _update_core(self, name: str):
        repos = (self.settings.data.get("core_updates", {}) or {}).get("repos", {}) or {}
        if name == "singbox":
            repo = repos.get("singbox", "SagerNet/sing-box")
            self._append_log(f"[INFO] Checking latest release: {repo}")
            try:
                tag = self.updater.update_singbox(repo)
                self._append_log(f"[INFO] sing-box updated to {tag}")
            except Exception as e:
                self._append_log(f"[ERROR] sing-box update failed: {e}")
            finally:
                self.updater.cleanup_tmp()

        if name == "clash":
            repo = repos.get("clash", "MetaCubeX/mihomo")
            self._append_log(f"[INFO] Checking latest release: {repo}")
            try:
                tag = self.updater.update_mihomo(repo)
                self._append_log(f"[INFO] mihomo updated to {tag}")
            except Exception as e:
                self._append_log(f"[ERROR] mihomo update failed: {e}")
            finally:
                self.updater.cleanup_tmp()

    def _update_all(self):
        self._update_core("singbox")
        self._update_core("clash")


# ---------------------
# First run wizard
# ---------------------

class FirstRunWizard(QtWidgets.QDialog):
    def __init__(self, settings: SettingsManager, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Welcome to Umbra (First Run)")
        self.setMinimumWidth(540)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QtWidgets.QLabel("<b>Quick setup</b><br><span style='color:#9aa4b2'>Nothing will run automatically without your approval. No speed tests are started here.</span>")
        title.setWordWrap(True)
        root.addWidget(title)

        gb_mode = QtWidgets.QGroupBox("Copilot intensity")
        v = QtWidgets.QVBoxLayout(gb_mode)
        self.rb_basic = QtWidgets.QRadioButton("Basic (minimal tips)")
        self.rb_helpful = QtWidgets.QRadioButton("Helpful (recommended)")
        self.rb_expert = QtWidgets.QRadioButton("Expert (more controls & recipes)")
        mode = self.settings.get_copilot_mode()
        if mode == "Basic":
            self.rb_basic.setChecked(True)
        elif mode == "Expert":
            self.rb_expert.setChecked(True)
        else:
            self.rb_helpful.setChecked(True)

        v.addWidget(self.rb_basic)
        v.addWidget(self.rb_helpful)
        v.addWidget(self.rb_expert)
        root.addWidget(gb_mode)

        gb = QtWidgets.QGroupBox("Defaults (you can change later in Settings)")
        f = QtWidgets.QFormLayout(gb)
        f.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        self.chk_default_dns = QtWidgets.QCheckBox("Enable curated DNS list (Iran + Global)")
        self.chk_default_dns.setChecked(bool(self.settings.data.get("assist", {}) or {}).get("default_dns_packs_enabled", True))
        self.chk_bitrate = QtWidgets.QCheckBox("Show bitrate helper on Dashboard (Streaming profile)")
        self.chk_bitrate.setChecked(bool(self.settings.data.get("ui", {}) or {}).get("show_stream_bitrate_on_dashboard", True))
        self.chk_ping_check = QtWidgets.QCheckBox("Run one-time safe checks now (DNS ping only)")
        self.chk_ping_check.setChecked(False)

        f.addRow("DNS", self.chk_default_dns)
        f.addRow("Dashboard", self.chk_bitrate)
        f.addRow("Checks", self.chk_ping_check)
        root.addWidget(gb)

        self.lbl_log = QtWidgets.QLabel("")
        self.lbl_log.setWordWrap(True)
        root.addWidget(self.lbl_log)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel | QtWidgets.QDialogButtonBox.StandardButton.Ok)
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Apply")
        btns.accepted.connect(self._apply)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _selected_mode(self) -> str:
        if self.rb_basic.isChecked():
            return "Basic"
        if self.rb_expert.isChecked():
            return "Expert"
        return "Helpful"

    def _apply(self):
        # snapshot, then apply
        self.settings.create_snapshot("First Run Setup")

        # copilot
        self.settings.set_copilot_mode(self._selected_mode())

        # defaults
        self.settings.data.setdefault("assist", {})["default_dns_packs_enabled"] = self.chk_default_dns.isChecked()
        self.settings.data.setdefault("ui", {})["show_stream_bitrate_on_dashboard"] = self.chk_bitrate.isChecked()

        if self.chk_ping_check.isChecked():
            self._run_safe_dns_ping_check()

        self.settings.mark_first_run_completed()
        self.accept()

    def _run_safe_dns_ping_check(self):
        # user-approved: only ICMP pings (no downloads/uploads)
        self.lbl_log.setText("Running safe DNS ping checks...")
        QtWidgets.QApplication.processEvents()

        servers = (self.settings.data.get("dns", {}) or {}).get("servers", []) or []
        # cap for speed
        servers = servers[:8]
        results = []
        for s in servers:
            ip = str(s.get("ip", "")).strip()
            if not ip:
                continue
            ms = None
            try:
                out = subprocess.check_output(_ping_cmd(ip), stderr=subprocess.STDOUT, text=True, timeout=3)
                ms = _parse_ping_ms(out)
            except Exception:
                ms = None
            results.append((ip, ms))

        cache = self.settings.data.setdefault("dns", {}).setdefault("rank_cache", {})
        cache["safe_ping"] = {ip: {"ping_ms": ms, "at": _now_hms()} for ip, ms in results}
        self.settings.save()

        ok = [r for r in results if r[1] is not None]
        self.lbl_log.setText(f"Safe checks complete. ({len(ok)}/{len(results)}) DNS targets responded.")


# ---------------------
# Helpers
# ---------------------

def _now_hms() -> str:
    return time.strftime("%H:%M:%S")
