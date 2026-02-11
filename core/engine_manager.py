from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import psutil

from logger import Logger
from core.settings_manager import SettingsManager


@dataclass
class EngineStatus:
    running: bool
    core_process: bool
    message: str


@dataclass
class CoreSpec:
    name: str
    binary: Optional[str]
    kind: str


class EngineManager:
    """
    Engine Manager is responsible for:
    - lifecycle start/stop (no orphan threads)
    - (future) launching VPN cores
    - providing a stable "Engine Running" state for UI

    IMPORTANT:
    - Umbra will NEVER run bandwidth tests unless user explicitly clicks a test action.
    - Engine start does not perform speedtests; it only enables monitoring and routing features.
    """

    def __init__(self, settings: SettingsManager, logger: Optional[Logger] = None):
        self.settings = settings
        self.logger = logger or Logger()
        self._running = False

        self._stop_evt = threading.Event()
        self._worker: Optional[threading.Thread] = None

        self._proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()

        self._status_msg = "Engine is OFF"

    def set_log_callback(self, cb: Optional[Callable[[str], None]]):
        self.logger.set_callback(cb)

    def status(self) -> EngineStatus:
        with self._proc_lock:
            core_on = self._proc is not None and self._proc.poll() is None
        return EngineStatus(running=self._running, core_process=core_on, message=self._status_msg)

    def is_running(self) -> bool:
        return self._running

    def start_engine(self) -> bool:
        if self._running:
            self.logger.warn("Engine already running.")
            return False

        self._stop_evt.clear()
        self._running = True
        self._status_msg = "Engine is ON"

        # Lightweight worker to keep state consistent and detect crashed core
        self._worker = threading.Thread(target=self._run_loop, name="UmbraEngineLoop", daemon=True)
        self._worker.start()

        self.logger.info("Engine started.")
        return True

    def stop_engine(self) -> bool:
        if not self._running:
            return False

        self.logger.info("Stopping engine...")
        self._stop_evt.set()

        # stop core process first (if any)
        self._stop_core_process()

        # stop worker thread
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=3.0)

        self._worker = None
        self._running = False
        self._status_msg = "Engine is OFF"
        self.logger.info("Engine stopped.")
        return True

    def detect_listening_ports(self, limit: int = 8):
        ports = []
        try:
            conns = psutil.net_connections(kind="inet")
        except Exception:
            return ports
        for c in conns:
            try:
                if c.status != psutil.CONN_LISTEN:
                    continue
                laddr = c.laddr
                port = getattr(laddr, "port", None)
                if not port:
                    continue
                pid = c.pid or 0
                name = "-"
                if pid:
                    try:
                        name = psutil.Process(pid).name()
                    except Exception:
                        name = "-"
                ports.append({"port": int(port), "pid": int(pid), "name": name})
            except Exception:
                continue
        ports = sorted(ports, key=lambda x: x["port"])
        return ports[: max(0, limit)]

    def _core_spec(self, core_name: str) -> CoreSpec:
        paths = ((self.settings.data.get("core_updates", {}) or {}).get("paths", {}) or {})
        core_name = str(core_name or "").lower()
        if core_name in {"auto", "singbox", "sing-box"}:
            return CoreSpec(name="singbox", binary=self._find_singbox_binary(), kind="json")
        if core_name in {"clash", "mihomo"}:
            return CoreSpec(name="clash", binary=None, kind="yaml")
        if core_name == "openvpn":
            return CoreSpec(name="openvpn", binary=paths.get("openvpn") or None, kind="ovpn")
        if core_name == "openconnect":
            return CoreSpec(name="openconnect", binary=paths.get("openconnect") or None, kind="url")
        return CoreSpec(name=core_name, binary=None, kind="unknown")

    def _start_unsupported_core(self, spec: CoreSpec) -> bool:
        if not spec.binary:
            self.logger.error(f"{spec.name} binary path is not configured.")
            return False
        self.logger.error(f"Compatibility layer ready, but runtime for '{spec.name}' is not implemented yet.")
        return False

    def start_core_with_config(self, cfg: dict) -> bool:
        if not cfg:
            self.logger.error("No config provided for core start.")
            return False
        with self._proc_lock:
            if self._proc is not None and self._proc.poll() is None:
                self.logger.warn("Core already running.")
                return False

        raw = (cfg.get("raw") or "").strip()
        if not raw:
            self.logger.error("Config is empty; cannot start core.")
            return False

        spec = self._core_spec(str(cfg.get("core", "auto") or "auto"))
        if spec.name == "singbox":
            return self._start_singbox(raw)
        if spec.name in {"clash", "openvpn", "openconnect"}:
            return self._start_unsupported_core(spec)

        self.logger.error(f"Unsupported core: {spec.name}")
        return False

    def stop_core(self) -> bool:
        self._stop_core_process()
        return True

    def _start_singbox(self, raw: str) -> bool:
        if not raw.lstrip().startswith("{"):
            self.logger.error("Sing-box requires JSON config; provided config is not JSON.")
            return False

        core_path = self._find_singbox_binary()
        if not core_path:
            self.logger.error("Sing-box binary not found. Please install via Updates page.")
            return False

        cfg_path = self._write_active_config("singbox", raw)
        if not cfg_path:
            self.logger.error("Failed to write sing-box config.")
            return False

        try:
            proc = subprocess.Popen([core_path, "run", "-c", cfg_path])
            with self._proc_lock:
                self._proc = proc
            self._status_msg = "Core running: sing-box"
            self.logger.info("Sing-box core started.")
            return True
        except Exception as exc:
            self.logger.error(f"Failed to start sing-box: {exc}")
            return False

    def _find_singbox_binary(self) -> Optional[str]:
        candidates = [
            "cores/sing-box/sing-box",
            "cores/sing-box/sing-box.exe",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def _write_active_config(self, core_name: str, raw: str) -> Optional[str]:
        try:
            os.makedirs("configs", exist_ok=True)
            path = os.path.join("configs", f"active_{core_name}.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(raw)
            return path
        except Exception:
            return None

    def shutdown(self):
        # Called on app exit
        try:
            self.stop_engine()
        except Exception:
            pass

    # ---------------------
    # Internal
    # ---------------------

    def _run_loop(self):
        # No active bandwidth tests here. Only health checks.
        while not self._stop_evt.is_set():
            # if a core is running and it exited, notify
            with self._proc_lock:
                if self._proc is not None and self._proc.poll() is not None:
                    self.logger.error("VPN core process exited unexpectedly.")
                    self._proc = None
            time.sleep(0.5)

    def _stop_core_process(self):
        with self._proc_lock:
            proc = self._proc
            self._proc = None

        if not proc:
            return

        try:
            self.logger.info("Stopping VPN core process...")
            self._terminate_process_tree(proc.pid, timeout=3.0)
        except Exception as e:
            self.logger.error(f"Failed to stop core process: {e}")

    def _terminate_process_tree(self, pid: int, timeout: float = 3.0):
        try:
            parent = psutil.Process(pid)
        except Exception:
            return

        children = []
        try:
            children = parent.children(recursive=True)
        except Exception:
            children = []

        # terminate children
        for ch in children:
            try:
                ch.terminate()
            except Exception:
                pass

        try:
            parent.terminate()
        except Exception:
            pass

        gone, alive = psutil.wait_procs([parent] + children, timeout=timeout)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
