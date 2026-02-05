from __future__ import annotations

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
