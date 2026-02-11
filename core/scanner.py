from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Tuple

import psutil


@dataclass
class ProcNetInfo:
    pid: int
    name: str
    connections: int


class NetworkScanner:
    """
    Lightweight process/network scanner.
    NOTE: This does NOT run any bandwidth tests. It only reads OS counters (psutil).
    """

    def list_processes(self, only_network_active: bool = False) -> List[ProcNetInfo]:
        out: List[ProcNetInfo] = []
        for p in psutil.process_iter(attrs=["pid", "name"]):
            try:
                conns = p.net_connections(kind="inet")
                c = len([x for x in conns if x.status])
                if only_network_active and c == 0:
                    continue
                out.append(ProcNetInfo(pid=p.info["pid"], name=p.info.get("name") or f"PID {p.info['pid']}", connections=c))
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
            except Exception:
                continue
        out.sort(key=lambda x: (x.connections, x.name.lower()), reverse=True)
        return out

    def get_total_net_mbps(self, interval: float = 0.0) -> Tuple[float, float]:
        """
        Returns (download_mbps, upload_mbps) using system IO counters.
        If interval > 0, waits interval seconds and measures delta.
        """
        c1 = psutil.net_io_counters()
        t1 = time.time()
        if interval > 0:
            time.sleep(interval)
        c2 = psutil.net_io_counters()
        t2 = time.time()
        dt = max(0.001, t2 - t1)
        down_bps = (c2.bytes_recv - c1.bytes_recv) * 8.0 / dt
        up_bps = (c2.bytes_sent - c1.bytes_sent) * 8.0 / dt
        return down_bps / 1e6, up_bps / 1e6
