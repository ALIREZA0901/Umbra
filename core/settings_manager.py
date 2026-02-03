from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, unquote

import requests

DEFAULT_SETTINGS_PATH = os.path.join("configs", "settings.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_json_load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _safe_json_save(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------
# Detection helpers
# ---------------------

_RE_SSH_INLINE = re.compile(r"^\s*ssh\s+(.*)$", re.IGNORECASE)
_RE_WG = re.compile(r"^\s*\[Interface\]\s*", re.IGNORECASE | re.MULTILINE)


def _looks_base64(s: str) -> bool:
    s2 = s.strip()
    if len(s2) < 16:
        return False
    if any(ch.isspace() for ch in s2):
        return False
    return re.fullmatch(r"[A-Za-z0-9+/=]+", s2) is not None


def _b64_decode_maybe(s: str) -> Optional[str]:
    try:
        pad = "=" * ((4 - len(s) % 4) % 4)
        raw = base64.urlsafe_b64decode((s + pad).encode("utf-8"))
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return None


def _detect_type(raw: str) -> str:
    t = raw.strip()
    if not t:
        return "unknown"
    low = t.lower()
    if low.startswith("vmess://"):
        return "vmess"
    if low.startswith("vless://"):
        return "vless"
    if low.startswith("trojan://"):
        return "trojan"
    if low.startswith("ss://"):
        return "shadowsocks"
    if low.startswith("socks://") or low.startswith("socks5://"):
        return "socks"
    if low.startswith("http://") or low.startswith("https://"):
        # could be subscription or http proxy; we treat as subscription when used in subscription UI
        return "http"
    if low.startswith("hysteria2://") or low.startswith("hy2://"):
        return "hysteria2"
    if "openvpn" in low or low.startswith("ovpn://") or low.endswith(".ovpn"):
        return "openvpn"
    if "openconnect" in low or "anyconnect" in low:
        return "openconnect"
    if _RE_WG.search(t):
        return "wireguard"
    if _RE_SSH_INLINE.match(t) or low.startswith("ssh://"):
        return "ssh"
    if t.lstrip().startswith("{") and ("outbounds" in t or "inbounds" in t):
        return "singbox_json"
    return "unknown"


def _suggest_core(conf_type: str) -> str:
    # sing-box is our primary for modern protocols
    if conf_type in {"vmess", "vless", "trojan", "shadowsocks", "socks", "http", "hysteria2", "wireguard", "singbox_json"}:
        return "singbox"
    if conf_type == "openvpn":
        return "openvpn"
    if conf_type == "openconnect":
        return "openconnect"
    if conf_type == "ssh":
        return "ssh"
    return "auto"


def _infer_loc_from_host(host: str) -> str:
    h = (host or "").lower()
    if h.endswith(".ir") or ".ir/" in h:
        return "IR"
    return "GLOBAL"


# ---------------------
# Defaults
# ---------------------

def _default_dns_servers() -> List[Dict[str, Any]]:
    # A practical set for Iran + global. Users can add/remove.
    # (Names are friendly; loc used for grouping.)
    return [
        {"name": "Shecan 1", "server": "178.22.122.100", "loc": "IR", "tags": ["ir", "public"]},
        {"name": "Shecan 2", "server": "185.51.200.2", "loc": "IR", "tags": ["ir", "public"]},
        {"name": "Radar (IR)", "server": "10.202.10.10", "loc": "IR", "tags": ["ir", "local"]},
        {"name": "Cloudflare", "server": "1.1.1.1", "loc": "GLOBAL", "tags": ["global", "public"]},
        {"name": "Google", "server": "8.8.8.8", "loc": "GLOBAL", "tags": ["global", "public"]},
        {"name": "Quad9", "server": "9.9.9.9", "loc": "GLOBAL", "tags": ["global", "security"]},
        {"name": "AdGuard", "server": "94.140.14.14", "loc": "GLOBAL", "tags": ["global", "adblock"]},
        {"name": "OpenDNS", "server": "208.67.222.222", "loc": "GLOBAL", "tags": ["global", "public"]},
    ]


def _default_speedtest_targets() -> List[Dict[str, str]]:
    # Ping targets (safe) + download URLs for optional advanced tests.
    # IR: ArvanCloud edge download (supports big files; app uses HTTP Range to limit bytes)
    return [
        {"name": "ArvanCloud Edge (IR)", "host": "simin.iperf3.ir", "loc": "IR", "download_url": "http://simin.iperf3.ir/files/100mb.bin"},
        {"name": "Cloudflare DNS", "host": "1.1.1.1", "loc": "GLOBAL", "download_url": "https://speed.cloudflare.com/__down?bytes=10000000"},
        {"name": "Google DNS", "host": "8.8.8.8", "loc": "GLOBAL", "download_url": "https://cachefly.cachefly.net/10mb.test"},
        {"name": "Hetzner (FSN1)", "host": "fsn1-speed.hetzner.com", "loc": "GLOBAL", "download_url": "https://fsn1-speed.hetzner.com/100MB.bin"},
    ]


def _default_profiles() -> Dict[str, Any]:
    # Predefined profiles are read-only; modifying creates/uses Custom.
    return {
        "active": "Gaming",
        "items": {
            "Gaming": {
                "readonly": True,
                "dns_mode": "AUTO",
                "notes": "Lower latency preference.",
            },
            "Streaming": {
                "readonly": True,
                "dns_mode": "AUTO",
                "platform": "Kick",
                "notes": "Stability preference (loss/jitter-aware).",
            },
            "Work": {
                "readonly": True,
                "dns_mode": "AUTO",
                "notes": "Balanced.",
            },
            "Custom": {
                "readonly": False,
                "dns_mode": "AUTO",
                "notes": "User customized.",
            },
        },
    }


def _default_settings() -> Dict[str, Any]:
    return {
        "meta": {
            "created_at": _now(),
            "updated_at": _now(),
            "app": "Umbra",
            "version": "2.0.0-phase1",
            "first_run_completed": False,
        },
        "ui": {
            "tray_enabled": True,
            "close_action": "minimize_to_tray",  # minimize_to_tray | exit
            "show_stream_bitrate_on_dashboard": True,
            "refresh_enabled": True,
            "refresh_interval_s": 60,
            "pause_refresh_when_minimized": True,
        },
        "behavior": {
            "auto_suggestions": True,  # Accept/Deny flow; never forced.
        },
        "copilot": {
            "mode": "Helpful",
            "suppressed": [],
            "last_analysis_at": None,
        },
        "history": {
            "snapshots": [],
            "last_snapshot_id": None,
        },
        "assist": {
            "default_dns_packs_enabled": True,
            "streaming_auto_vpn_fallback": False,
            "ask_once_per_session": True,
            "last_checks": {},
        },
        "dns": {
            "servers": _default_dns_servers(),
            "rank_cache": {},
            "unreliable": [],
        },
        "speedtest": {
            "targets": _default_speedtest_targets(),
            "advanced_download_bytes": 10_000_000,  # 10MB range request
            "advanced_upload_bytes": 2_000_000,  # 2MB upload payload
            "upload_endpoints": [
                {"name": "HTTPBin", "url": "https://httpbin.org/post"},
            ],
        },
        "profiles": _default_profiles(),
        "configs": [],  # list of config dicts
        "subscriptions": [],  # list of subscription URLs
        "app_dns_routes": {},
        "app_vpn_routes": {},
        "app_interfaces": {},
        "app_priorities": {},
        "core_updates": {
            "repos": {
                "singbox": "SagerNet/sing-box",
                "clash": "MetaCubeX/mihomo",
            },
        },
    }


# ---------------------
# Settings Manager
# ---------------------

class SettingsManager:
    def __init__(self, path: str = DEFAULT_SETTINGS_PATH):
        self.path = path
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self):
        self.data = _safe_json_load(self.path)
        if not self.data:
            self.data = _default_settings()
            self.save()
            return

        # ensure defaults / missing keys
        defaults = _default_settings()
        self._deep_merge_missing(self.data, defaults)

        self.data.setdefault("meta", {})["updated_at"] = _now()
        self.save()

    def save(self):
        self.data.setdefault("meta", {})["updated_at"] = _now()
        _safe_json_save(self.path, self.data)

    def _deep_merge_missing(self, dst: Dict[str, Any], src: Dict[str, Any]):
        for k, v in src.items():
            if k not in dst:
                dst[k] = v
            else:
                if isinstance(dst[k], dict) and isinstance(v, dict):
                    self._deep_merge_missing(dst[k], v)

    # ---------------------
    # DNS
    # ---------------------

    def add_dns(self, name: str, server: str, loc: str = "AUTO") -> bool:
        server = (server or "").strip()
        name = (name or "").strip()
        if not server:
            return False
        servers = (self.data.get("dns", {}) or {}).get("servers", []) or []
        if any((s.get("server") or "").strip() == server for s in servers):
            return False
        if loc == "AUTO":
            loc = _infer_loc_from_host(server)
        entry = {
            "name": name or server,
            "server": server,
            "loc": loc,
            "tags": [loc.lower()],
        }
        self.data.setdefault("dns", {}).setdefault("servers", []).append(entry)
        self.save()
        return True

    def remove_dns_by_index(self, idx: int) -> bool:
        servers = (self.data.get("dns", {}) or {}).get("servers", []) or []
        if idx < 0 or idx >= len(servers):
            return False
        servers.pop(idx)
        self.data["dns"]["servers"] = servers
        self.save()
        return True

    # ---------------------
    # Configs / Subscription
    # ---------------------

    def process_smart_input(self, text: str) -> int:
        """
        Import from clipboard or subscription content. Accepts:
        - multiple lines of share links
        - base64 subscriptions (decoded into lines)
        - raw sing-box JSON
        - wireguard ini
        Returns number of configs added.
        """
        if not text:
            return 0
        raw = text.strip()

        # try decode if looks like base64 subscription
        if _looks_base64(raw) and "://" not in raw:
            decoded = _b64_decode_maybe(raw)
            if decoded and "://" in decoded:
                raw = decoded

        # split lines or handle json/ini as single
        items: List[str]
        if raw.lstrip().startswith("{") and raw.rstrip().endswith("}"):
            items = [raw]
        elif _RE_WG.search(raw):
            items = [raw]
        else:
            items = [ln.strip() for ln in raw.splitlines() if ln.strip()]

        added = 0
        for it in items:
            added += 1 if self._add_config(it, source="clipboard") else 0

        if added:
            self.save()
        return added

    def add_subscription(self, url: str) -> bool:
        url = (url or "").strip()
        if not url:
            return False
        subs = self.data.get("subscriptions", []) or []
        if url in subs:
            return False
        subs.append(url)
        self.data["subscriptions"] = subs
        self.save()
        return True

    def update_subscription(self, url: str, timeout: int = 20) -> int:
        url = (url or "").strip()
        if not url:
            return 0
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        text = r.text.strip()

        # Many subs are base64; decode if needed
        if _looks_base64(text) and "://" not in text:
            dec = _b64_decode_maybe(text)
            if dec:
                text = dec

        return self.process_smart_input(text)

    def _add_config(self, raw: str, source: str = "manual") -> bool:
        raw = raw.strip()
        if not raw:
            return False

        conf_type = _detect_type(raw)
        core = _suggest_core(conf_type)

        # Dedup by raw (simple but effective)
        for c in self.data.get("configs", []) or []:
            if (c.get("raw") or "").strip() == raw:
                return False

        name = self._auto_name(raw, conf_type)
        cfg = {
            "name": name,
            "type": conf_type,
            "core": core,
            "raw": raw,
            "source": source,
            "added_at": _now(),
            "tags": [],
        }
        self.data.setdefault("configs", []).append(cfg)
        return True

    def _auto_name(self, raw: str, conf_type: str) -> str:
        # Lightweight: try to extract host and loc.
        host = ""
        try:
            if "://" in raw and not raw.lstrip().startswith("{"):
                u = urlparse(raw.strip())
                host = u.hostname or ""
        except Exception:
            host = ""

        loc = _infer_loc_from_host(host) if host else "AUTO"
        short = host if host else conf_type.upper()
        if len(short) > 22:
            short = short[:22] + "…"

        return f"{short} [{loc}]"

    # ---------------------
    # Profiles helpers
    # ---------------------

    def get_active_profile(self) -> str:
        return str((self.data.get("profiles", {}) or {}).get("active", "Gaming"))

    def set_active_profile(self, name: str):
        if not name:
            return
        self.data.setdefault("profiles", {})["active"] = name
        self.save()

    # ---------------------
    # First-run / Copilot
    # ---------------------

    def is_first_run_pending(self) -> bool:
        meta = self.data.get("meta", {}) or {}
        return not bool(meta.get("first_run_completed", False))

    def mark_first_run_completed(self) -> None:
        self.data.setdefault("meta", {})["first_run_completed"] = True
        self.save()

    def get_copilot_mode(self) -> str:
        mode = (self.data.get("copilot", {}) or {}).get("mode", "Helpful") or "Helpful"
        mode = str(mode)
        if mode not in ("Basic", "Helpful", "Expert"):
            mode = "Helpful"
        return mode

    def set_copilot_mode(self, mode: str) -> None:
        mode = str(mode or "Helpful")
        if mode not in ("Basic", "Helpful", "Expert"):
            mode = "Helpful"
        self.data.setdefault("copilot", {})["mode"] = mode
        self.save()

    def suppress_suggestion(self, sid: str) -> None:
        sid = str(sid or "").strip()
        if not sid:
            return
        cop = self.data.setdefault("copilot", {})
        lst = cop.setdefault("suppressed", [])
        if sid not in lst:
            lst.append(sid)
            self.save()

    def is_suggestion_suppressed(self, sid: str) -> bool:
        sid = str(sid or "").strip()
        if not sid:
            return False
        lst = (self.data.get("copilot", {}) or {}).get("suppressed", []) or []
        return sid in lst

    # ---------------------
    # Snapshots / Rollback
    # ---------------------

    def create_snapshot(self, label: str) -> str:
        """Create a lightweight snapshot before applying changes."""
        label = str(label or "Snapshot")
        snap_id = str(int(time.time() * 1000))
        payload = json.loads(json.dumps(self.data))  # deep copy, JSON-safe
        # Prevent infinite growth
        payload.pop("history", None)

        snap = {
            "id": snap_id,
            "at": _now(),
            "label": label,
            "data": payload,
        }
        hist = self.data.setdefault("history", {})
        snaps = hist.setdefault("snapshots", [])
        snaps.insert(0, snap)
        # keep last 10
        if len(snaps) > 10:
            del snaps[10:]
        hist["last_snapshot_id"] = snap_id
        self.save()
        return snap_id

    def rollback_last_snapshot(self) -> bool:
        hist = self.data.get("history", {}) or {}
        snaps = hist.get("snapshots", []) or []
        if not snaps:
            return False
        return self.rollback_snapshot(str(snaps[0].get("id", "")))

    def rollback_snapshot(self, snap_id: str) -> bool:
        snap_id = str(snap_id or "")
        hist = self.data.get("history", {}) or {}
        snaps = hist.get("snapshots", []) or []
        target = None
        for s in snaps:
            if str(s.get("id", "")) == snap_id:
                target = s
                break
        if not target:
            return False

        payload = target.get("data")
        if not isinstance(payload, dict):
            return False

        # Preserve history and critical meta fields
        old = self.data
        new_data = json.loads(json.dumps(payload))

        # keep app/version/created_at from current
        old_meta = old.get("meta", {}) or {}
        new_meta = new_data.get("meta", {}) or {}
        for k in ("app", "version", "created_at"):
            if k in old_meta:
                new_meta[k] = old_meta[k]
        new_data["meta"] = new_meta
        new_data["history"] = old.get("history", {})

        self.data = new_data
        self.save()
        return True
