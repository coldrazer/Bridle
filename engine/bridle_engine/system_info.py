"""Static hardware facts and live resource usage for this machine."""

from __future__ import annotations

import json
import platform
import re
import shutil
import socket
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

_CHIP_RE = re.compile(r"Apple M(\d+)(?:\s+(Pro|Max|Ultra))?", re.IGNORECASE)


@dataclass(frozen=True)
class MachineInfo:
    hostname: str
    model_name: str          # "MacBook Pro", "Mac mini", ...
    kind: str                # laptop | mini | studio | imac | pro | other
    chip: str                # "Apple M5 Max"
    memory_total_bytes: int
    disk_total_bytes: int
    os_version: str
    thunderbolt_generation: int | None
    thunderbolt_ports: int | None
    rdma: str                # enabled | disabled | unavailable | unknown

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LiveStats:
    memory_used_bytes: int
    memory_total_bytes: int
    cpu_percent: float
    disk_free_bytes: int

    def to_json(self) -> dict:
        return asdict(self)


def thunderbolt_generation_for_chip(chip: str) -> int | None:
    """Thunderbolt generation from the chip name. TB5 (needed for RDMA clustering) ships on
    M4 Pro/Max and later Pro/Max parts, plus M3 Ultra and later Ultras."""
    m = _CHIP_RE.search(chip or "")
    if not m:
        return None
    gen, tier = int(m.group(1)), (m.group(2) or "").lower()
    if tier in ("pro", "max") and gen >= 4:
        return 5
    if tier == "ultra" and gen >= 3:
        return 5
    return 4


def machine_kind(model_name: str) -> str:
    name = model_name.lower()
    if "macbook" in name:
        return "laptop"
    if "mac mini" in name:
        return "mini"
    if "mac studio" in name:
        return "studio"
    if "imac" in name:
        return "imac"
    if "mac pro" in name:
        return "pro"
    return "other"


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def _profiler(data_type: str) -> list[dict]:
    raw = _run(["system_profiler", data_type, "-json"], timeout=15.0)
    if not raw:
        return []
    try:
        items = json.loads(raw).get(data_type, [])
    except json.JSONDecodeError:
        return []
    return [i for i in items if isinstance(i, dict)]


def count_thunderbolt_ports(items: list[dict]) -> int | None:
    """Each Thunderbolt/USB4 bus reported by system_profiler is one physical receptacle."""
    if not items:
        return None
    buses = [i for i in items if "receptacle_1_tag" in i or str(i.get("_name", "")).startswith("thunderbolt")]
    return len(buses) or None


def parse_rdma_status(output: str | None) -> str:
    if output is None:
        return "unavailable"
    text = output.strip().lower()
    if "disabled" in text or "not enabled" in text:
        return "disabled"
    if "enabled" in text:
        return "enabled"
    return "unknown"


def _hostname() -> str:
    local = _run(["scutil", "--get", "LocalHostName"])
    if local and local.strip():
        return local.strip()
    return socket.gethostname().split(".")[0]


def collect_machine_info(models_dir: Path) -> MachineInfo:
    system = platform.system()
    model_name, chip = platform.node(), platform.processor() or platform.machine()
    tb_ports: int | None = None
    if system == "Darwin":
        hw = _profiler("SPHardwareDataType")
        if hw:
            model_name = hw[0].get("machine_name", model_name)
            chip = hw[0].get("chip_type", chip)
        brand = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if brand and brand.strip().startswith("Apple"):
            chip = brand.strip()
        tb_ports = count_thunderbolt_ports(_profiler("SPThunderboltDataType"))
        os_version = f"macOS {platform.mac_ver()[0]}"
        rdma = parse_rdma_status(_run(["rdma_ctl", "status"]))
    else:
        model_name = f"{system} host"
        os_version = f"{system} {platform.release()}"
        rdma = "unavailable"
    disk_root = models_dir if models_dir.exists() else Path.home()
    return MachineInfo(
        hostname=_hostname(),
        model_name=model_name,
        kind=machine_kind(model_name),
        chip=chip,
        memory_total_bytes=psutil.virtual_memory().total,
        disk_total_bytes=shutil.disk_usage(disk_root).total,
        os_version=os_version,
        thunderbolt_generation=thunderbolt_generation_for_chip(chip) if system == "Darwin" else None,
        thunderbolt_ports=tb_ports,
        rdma=rdma,
    )


class StatsSampler:
    def __init__(self, models_dir: Path):
        self._models_dir = models_dir
        psutil.cpu_percent(interval=None)  # prime: the first call always returns 0.0

    def sample(self) -> LiveStats:
        vm = psutil.virtual_memory()
        root = self._models_dir if self._models_dir.exists() else Path.home()
        return LiveStats(
            memory_used_bytes=vm.total - vm.available,
            memory_total_bytes=vm.total,
            cpu_percent=round(psutil.cpu_percent(interval=None), 1),
            disk_free_bytes=shutil.disk_usage(root).free,
        )


def lan_address() -> str | None:
    """Primary LAN IPv4 address. UDP connect sends no packets; it only selects a route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        addr = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if addr.startswith("127.") else addr
