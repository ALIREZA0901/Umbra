from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass
from typing import Callable, Optional

_LOG_LOCK = threading.Lock()


def _ts() -> str:
    return time.strftime("%H:%M:%S")


@dataclass
class LogRecord:
    level: str
    message: str


class Logger:
    """
    Simple logger that:
    - writes to logs/umbra.log
    - optionally forwards to UI via callback
    """

    def __init__(self, log_path: str = "logs/umbra.log", callback: Optional[Callable[[str], None]] = None):
        self.log_path = log_path
        self.callback = callback
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

    def set_callback(self, cb: Optional[Callable[[str], None]]):
        self.callback = cb

    def _write(self, level: str, msg: str):
        line = f"[{_ts()}] [{level}] {msg}"
        with _LOG_LOCK:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
        if self.callback:
            try:
                self.callback(line)
            except Exception:
                pass

    def info(self, msg: str):
        self._write("INFO", msg)

    def warn(self, msg: str):
        self._write("WARN", msg)

    def error(self, msg: str):
        self._write("ERROR", msg)
