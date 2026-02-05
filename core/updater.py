from __future__ import annotations

import os
import platform
import re
import shutil
import zipfile
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import requests


@dataclass
class ReleaseAsset:
    name: str
    url: str
    size: Optional[int] = None


class CoreUpdater:
    """
    GitHub release updater for VPN cores.
    - sing-box (SagerNet/sing-box)
    - mihomo (MetaCubeX/mihomo) (Clash compatible)
    """

    def __init__(self, cores_dir: str = "cores", log: Optional[Callable[[str], None]] = None):
        self.cores_dir = cores_dir
        self.log = log or (lambda s: None)
        os.makedirs(self.cores_dir, exist_ok=True)

    def _log(self, msg: str):
        self.log(msg)

    def _backup_existing(self, out_bin: str, core_name: str) -> Optional[str]:
        """Back up existing core binary before replacing it. Returns backup path or None."""
        try:
            if not os.path.exists(out_bin):
                return None
            ts = re.sub(r"[^0-9]", "", str(int(time.time())))
            bdir = os.path.join(self.cores_dir, "_backups", core_name, ts)
            os.makedirs(bdir, exist_ok=True)
            bpath = os.path.join(bdir, os.path.basename(out_bin))
            shutil.copy2(out_bin, bpath)
            return bpath
        except Exception:
            return None

    def _restore_backup(self, backup_path: Optional[str], out_bin: str) -> None:
        try:
            if backup_path and os.path.exists(backup_path):
                os.makedirs(os.path.dirname(out_bin), exist_ok=True)
                shutil.copy2(backup_path, out_bin)
        except Exception:
            pass

    def _gh_json(self, url: str, timeout: int = 20) -> dict:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Umbra-Updater"}
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def latest_release(self, repo: str) -> dict:
        return self._gh_json(f"https://api.github.com/repos/{repo}/releases/latest")

    def find_asset(self, release: dict, patterns: Tuple[str, ...]) -> Optional[ReleaseAsset]:
        assets = release.get("assets", []) or []
        for a in assets:
            name = a.get("name", "")
            if any(re.search(p, name, re.IGNORECASE) for p in patterns):
                return ReleaseAsset(
                    name=name,
                    url=a.get("browser_download_url", ""),
                    size=a.get("size", None),
                )
        return None

    def _arch_and_os(self) -> Tuple[str, str]:
        sys = platform.system().lower()
        arch = platform.machine().lower()

        if sys.startswith("win"):
            os_name = "windows"
        elif sys.startswith("linux"):
            os_name = "linux"
        elif sys.startswith("darwin"):
            os_name = "darwin"
        else:
            os_name = "windows"

        if arch in {"amd64", "x86_64"}:
            arch_name = "amd64"
        elif arch in {"arm64", "aarch64"}:
            arch_name = "arm64"
        else:
            arch_name = "amd64"

        return os_name, arch_name

    def download(self, url: str, out_path: str, timeout: int = 60):
        self._log(f"Downloading: {url}")
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        f.write(chunk)

    def update_singbox(self, repo: str) -> str:
        """
        Downloads and installs sing-box binary into cores/sing-box/
        """
        os_name, arch = self._arch_and_os()
        rel = self.latest_release(repo)
        tag = rel.get("tag_name", "latest")

        # sing-box assets are usually zip containing binary
        # Example patterns:
        # sing-box-1.10.0-windows-amd64.zip
        patt = (rf"sing-box-.*-{os_name}-{arch}\\.zip$", rf"sing-box-.*-{os_name}-{arch}\\.tar\\.gz$")
        asset = self.find_asset(rel, patt)
        if not asset:
            raise RuntimeError("No matching sing-box release asset found for your OS/arch.")

        tmp_dir = os.path.join(self.cores_dir, "_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        archive_path = os.path.join(tmp_dir, asset.name)
        self.download(asset.url, archive_path)

        dest_dir = os.path.join(self.cores_dir, "sing-box")
        os.makedirs(dest_dir, exist_ok=True)

        if archive_path.lower().endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as z:
                z.extractall(tmp_dir)
        else:
            raise RuntimeError("Unsupported archive format for sing-box (expected zip).")

        # find binary
        bin_name = "sing-box.exe" if os_name == "windows" else "sing-box"
        found = None
        for root, _, files in os.walk(tmp_dir):
            if bin_name in files:
                found = os.path.join(root, bin_name)
                break
        if not found:
            raise RuntimeError("sing-box binary not found in archive.")

        out_bin = os.path.join(dest_dir, bin_name)
        backup = self._backup_existing(out_bin, "sing-box")
        try:
            shutil.copy2(found, out_bin)
            if os.path.getsize(out_bin) <= 0:
                raise RuntimeError("Downloaded binary is empty.")
        except Exception:
            self._restore_backup(backup, out_bin)
            raise

        self.log(f"sing-box updated: {tag}")
        return tag

    def update_mihomo(self, repo: str) -> str:
        """
        Downloads and installs mihomo (Clash) binary into cores/mihomo/
        """
        os_name, arch = self._arch_and_os()
        rel = self.latest_release(repo)
        tag = rel.get("tag_name", "latest")

        # mihomo naming varies; use broad patterns.
        # common: mihomo-windows-amd64.zip or mihomo-windows-amd64.exe
        patt = (
            rf"mihomo.*{os_name}.*{arch}.*\\.zip$",
            rf"mihomo.*{os_name}.*{arch}.*\\.exe$",
        )
        asset = self.find_asset(rel, patt)
        if not asset:
            raise RuntimeError("No matching mihomo release asset found for your OS/arch.")

        tmp_dir = os.path.join(self.cores_dir, "_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        archive_path = os.path.join(tmp_dir, asset.name)
        self.download(asset.url, archive_path)

        dest_dir = os.path.join(self.cores_dir, "mihomo")
        os.makedirs(dest_dir, exist_ok=True)

        if archive_path.lower().endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as z:
                z.extractall(tmp_dir)
            # find exe
            found = None
            for root, _, files in os.walk(tmp_dir):
                for fn in files:
                    if fn.lower().startswith("mihomo") and fn.lower().endswith(".exe"):
                        found = os.path.join(root, fn)
                        break
                if found:
                    break
            if not found:
                raise RuntimeError("mihomo executable not found in zip.")
            shutil.copy2(found, os.path.join(dest_dir, "mihomo.exe"))
        elif archive_path.lower().endswith(".exe"):
            shutil.copy2(archive_path, os.path.join(dest_dir, "mihomo.exe"))
        else:
            raise RuntimeError("Unsupported archive format for mihomo.")

        self._log(f"mihomo updated: {tag}")
        return tag

    def cleanup_tmp(self):
        tmp_dir = os.path.join(self.cores_dir, "_tmp")
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass
