"""Mesh mode: pool the unified memory of several Macs over Thunderbolt 5 RDMA.

macOS 26.2+ exposes RDMA over Thunderbolt 5 (TN3205) once `rdma_ctl enable` has been run from
macOS Recovery on each Mac. MLX's JACCL backend uses it to run one model across Macs (tensor or
pipeline parallel). This module is the planning half:

* `node_report()` describes this Mac: macOS version, Thunderbolt 5, RDMA status, the RDMA
  devices (`ibv_devinfo`), which Thunderbolt port connects to which neighbour
  (`system_profiler SPThunderboltDataType` + `networksetup`, the same facts
  `mlx.distributed_config` uses), the Thunderbolt Bridge state and the downloaded models.
* `plan()` takes the coordinator's report plus every member's, derives the cabling, picks the
  backend (`jaccl` for a full mesh, `jaccl-ring` for a ring, `ring` over TCP only when allowed),
  the parallelism and the per-Mac memory share, and lists every blocker with its fix.

Nothing here runs a model; `mesh_session.py` does that. Everything is a pure function of the
reports so it can be tested without the hardware.
"""

from __future__ import annotations

import itertools
import json
import platform
import re
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Callable

MIN_MACOS = (26, 2)
JACCL_PORT = 32323          # rank 0 listens here to bootstrap the RDMA connections
RING_BASE_PORT = 32400      # TCP ring: rank r listens on RING_BASE_PORT + r
STRATEGIES = ("auto", "tensor", "pipeline")
TESTED_MESH_SIZE = 4        # Apple's guidance and MLX/exo testing centre on up to 4 fully connected Macs
RECOVERY_STEPS = [
    "Shut down the Mac.",
    "Press and hold the power button until the startup options appear.",
    "Choose Options, then Continue.",
    "From the menu bar choose Utilities > Terminal.",
    "Run: rdma_ctl enable",
    "Restart into macOS as usual.",
    "Back in macOS, check with: ibv_devices  (and ibv_devinfo once the cables are connected; "
    "the rdma_en ports should show PORT_ACTIVE).",
]

Runner = Callable[[list[str]], "str | None"]


def _run(cmd: list[str], timeout: float = 15.0) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


# ---- parsers (pure) ------------------------------------------------------------------------------
def parse_macos_version(text: str) -> tuple[int, int] | None:
    m = re.match(r"\s*(\d+)(?:\.(\d+))?", text or "")
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def macos_ok(text: str) -> bool:
    v = parse_macos_version(text)
    return v is not None and v >= MIN_MACOS


def parse_ibv_devinfo(text: str | None) -> dict[str, str]:
    """`ibv_devinfo` -> {device: port state}. A device with several ports reports its best state."""
    devices: dict[str, str] = {}
    current = None
    for line in (text or "").splitlines():
        m = re.match(r"\s*hca_id:\s*(\S+)", line)
        if m:
            current = m.group(1)
            devices.setdefault(current, "UNKNOWN")
            continue
        m = re.match(r"\s*state:\s*(PORT_\w+)", line)
        if m and current:
            if devices[current] != "PORT_ACTIVE":
                devices[current] = m.group(1)
    return devices


def parse_ibv_devices(text: str | None) -> list[str]:
    """`ibv_devices` table -> device names (fallback when ibv_devinfo is unavailable)."""
    return [line.split()[0] for line in (text or "").splitlines()
            if line.strip().startswith("rdma_")]


def parse_hardware_ports(text: str | None) -> dict[str, str]:
    """`networksetup -listallhardwareports` -> {"Thunderbolt 1": "en2", ...}."""
    ports, name = {}, None
    for line in (text or "").splitlines():
        if line.startswith("Hardware Port:"):
            name = line[len("Hardware Port:"):].strip()
        elif line.startswith("Device:") and name:
            ports[name] = line[len("Device:"):].strip()
            name = None
    return ports


def parse_thunderbolt_ports(profiler: dict | None, hardware_ports: dict[str, str]) -> list[dict]:
    """Each Thunderbolt receptacle: its network interface, its own domain UUID and the domain
    UUID of the Mac on the other end of the cable (None when nothing is plugged in)."""
    ports = []
    for bus in (profiler or {}).get("SPThunderboltDataType", []) or []:
        if not isinstance(bus, dict) or not bus.get("domain_uuid_key"):
            continue
        tag = (bus.get("receptacle_1_tag") or {}).get("receptacle_id_key")
        iface = hardware_ports.get(f"Thunderbolt {tag}")
        if not iface:
            continue
        connected = [i.get("domain_uuid_key") for i in bus.get("_items", []) or []
                     if isinstance(i, dict) and i.get("domain_uuid_key")]
        ports.append({"iface": iface, "uuid": bus["domain_uuid_key"], "tag": str(tag),
                      "connected_to": connected[0] if connected else None,
                      "connected_name": next((i.get("device_name_key") for i in bus.get("_items", []) or []
                                              if isinstance(i, dict) and i.get("domain_uuid_key")), None)})
    return sorted(ports, key=lambda p: p["iface"])


def parse_bridge(text: str | None) -> dict:
    """`ifconfig bridge0` -> whether the Thunderbolt Bridge is up and which ports it holds."""
    if not text:
        return {"present": False, "active": False, "members": []}
    first = text.splitlines()[0] if text.splitlines() else ""
    up = "<UP" in first or ",UP" in first
    members = re.findall(r"member:\s*(\S+)", text)
    active = up and ("status: active" in text or bool(members))
    return {"present": True, "active": active, "members": members}


# ---- this Mac --------------------------------------------------------------------------------------
def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:  # noqa: BLE001 - not installed / not a distribution
        return ""


def node_report(engine, runner: Runner = _run) -> dict:
    """Everything a coordinator needs to decide whether this Mac can join a mesh."""
    from . import __version__
    from .system_info import lan_address
    import psutil

    m = engine.machine
    darwin = platform.system() == "Darwin"
    macos = platform.mac_ver()[0] if darwin else ""
    devinfo = parse_ibv_devinfo(runner(["ibv_devinfo"])) if darwin else {}
    if darwin and not devinfo:
        devinfo = {d: "UNKNOWN" for d in parse_ibv_devices(runner(["ibv_devices"]))}
    tb_ports: list[dict] = []
    bridge = {"present": False, "active": False, "members": []}
    if darwin:
        raw = runner(["system_profiler", "SPThunderboltDataType", "-json"])
        try:
            profiler = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            profiler = None
        tb_ports = parse_thunderbolt_ports(profiler, parse_hardware_ports(
            runner(["networksetup", "-listallhardwareports"])))
        bridge = parse_bridge(runner(["ifconfig", "bridge0"]))
    models = [{"repo": lm.repo, "revision": lm.revision, "size_bytes": lm.size_bytes, "kind": lm.kind}
              for lm in engine.store.list()]
    loaded = engine.runtime.loaded if engine.runtime is not None else None
    session = getattr(engine, "mesh", None)
    return {
        "name": m.hostname, "model_name": m.model_name, "chip": m.chip,
        "memory_total_bytes": m.memory_total_bytes,
        "memory_available_bytes": psutil.virtual_memory().available,
        "macos": macos, "macos_ok": macos_ok(macos),
        "thunderbolt_generation": m.thunderbolt_generation, "thunderbolt_ports": m.thunderbolt_ports,
        "rdma": m.rdma,
        "rdma_devices": [{"device": d, "state": s, "active": s == "PORT_ACTIVE"} for d, s in sorted(devinfo.items())],
        "links": tb_ports, "bridge": bridge, "lan_ip": lan_address() or "",
        "engine_version": __version__, "mlx_version": _package_version("mlx"),
        "mlx_lm_version": _package_version("mlx-lm"),
        "models": models, "loaded_model": loaded.repo if loaded else None,
        "mesh": session.status() if session is not None else {"state": "idle"},
    }


# ---- planning (pure) -------------------------------------------------------------------------------
@dataclass
class Issue:
    node: str          # node name, or "" for the whole mesh
    code: str
    message: str
    fix: str = ""

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    ok: bool
    model: str
    backend: str                  # jaccl | jaccl-ring | ring | ""
    strategy: str                 # tensor | pipeline | ""
    topology: str                 # full-mesh | ring | partial | none
    nodes: list[dict]             # in rank order (rank 0 = coordinator)
    devices: list[list] | None    # MLX_IBV_DEVICES matrix in rank order (JACCL backends)
    ring_hosts: list[list[str]] | None  # MLX_HOSTFILE content for the TCP ring backend
    coordinator: str              # "ip:port" for JACCL bootstrap
    pooled_memory_bytes: int
    model_size_bytes: int
    max_full_mesh: int | None = None   # the most Macs these ports can fully connect (ports + 1)
    blockers: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["blockers"] = [b.to_json() for b in self.blockers]
        d["warnings"] = [w.to_json() for w in self.warnings]
        return d


def link_matrix(reports: list[dict]) -> tuple[list[list[int]], list[list[list[str]]]]:
    """count[i][j] cables from node i to node j, and the RDMA device names on node i for them."""
    owner = {}
    for i, r in enumerate(reports):
        for p in r.get("links", []):
            owner[p["uuid"]] = i
    n = len(reports)
    count = [[0] * n for _ in range(n)]
    devices: list[list[list[str]]] = [[[] for _ in range(n)] for _ in range(n)]
    for i, r in enumerate(reports):
        for p in r.get("links", []):
            j = owner.get(p.get("connected_to"))
            if j is None or j == i:
                continue
            count[i][j] += 1
            devices[i][j].append("rdma_" + p["iface"])
    return count, devices


def find_ring(count: list[list[int]]) -> list[int] | None:
    """A Hamiltonian cycle starting at node 0 (the coordinator), or None."""
    n = len(count)
    if n < 3:
        return None
    for rest in itertools.permutations(range(1, n)):
        order = [0, *rest]
        if all(count[order[k]][order[(k + 1) % n]] > 0 for k in range(n)):
            return order
    return None


def _device_entry(devs: list[str]):
    return devs[0] if len(devs) == 1 else list(devs)


def jaccl_matrix(order: list[int], devices, neighbours_only: bool) -> list[list]:
    """MLX_IBV_DEVICES in rank order; null on the diagonal (and off the ring for jaccl-ring)."""
    n = len(order)
    matrix = []
    for a, node in enumerate(order):
        row = []
        for b, other in enumerate(order):
            ring_neighbour = b in ((a - 1) % n, (a + 1) % n)
            if node == other or (neighbours_only and not ring_neighbour):
                row.append(None)
            else:
                row.append(_device_entry(devices[node][other]))
        matrix.append(row)
    return matrix


def _active_devices(report: dict) -> set[str]:
    return {d["device"] for d in report.get("rdma_devices", []) if d.get("active")}


def plan(reports: list[dict], model: str, strategy: str = "auto", allow_tcp: bool = False,
         model_support: dict | None = None) -> Plan:
    """reports[0] is the coordinator. model_support: {"pipeline": bool, "tensor": bool} if known."""
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}")
    blockers: list[Issue] = []
    warnings: list[Issue] = []
    n = len(reports)
    names = [r.get("name") or f"node {i}" for i, r in enumerate(reports)]
    if n < 2:
        blockers.append(Issue("", "too_few_nodes", "A mesh needs this Mac and at least one other Mac.",
                              "Approve another Mac under Network, with a device key that allows mesh."))

    # -- the model must be on every Mac, same revision --
    size = 0
    revisions = set()
    for name, r in zip(names, reports):
        have = next((m for m in r.get("models", []) if m.get("repo") == model), None)
        if have is None:
            blockers.append(Issue(name, "model_missing", f"{model} is not downloaded on {name}.",
                                  "Use Prepare to download it there (every Mac needs its own copy)."))
        else:
            size = max(size, int(have.get("size_bytes") or 0))
            revisions.add(have.get("revision") or "")
    if len(revisions) > 1:
        blockers.append(Issue("", "revision_mismatch", f"The Macs have different revisions of {model}.",
                              "Delete it on the Macs with the older copy and download it again."))

    # -- software on every Mac --
    mlx_versions = {r.get("mlx_version") for r in reports}
    if len(mlx_versions) > 1:
        blockers.append(Issue("", "mlx_mismatch", "The Macs run different MLX versions: "
                              + ", ".join(sorted(v or "none" for v in mlx_versions)) + ".",
                              "Update Bridle (or its engine) on every Mac so they match."))
    if any(not r.get("mlx_version") for r in reports):
        blockers.append(Issue("", "mlx_missing", "MLX is not installed on one of the Macs.", "Finish engine setup there."))
    if len({r.get("engine_version") for r in reports}) > 1:
        warnings.append(Issue("", "engine_mismatch", "The Macs run different Bridle versions.",
                              "Update Bridle on every Mac."))
    for name, r in zip(names, reports):
        if r.get("loaded_model"):
            blockers.append(Issue(name, "model_loaded", f"{name} has {r['loaded_model']} loaded.",
                                  "Unload it so the mesh can use that memory."))
        state = (r.get("mesh") or {}).get("state", "idle")
        if state not in ("idle", "stopped", "failed"):
            blockers.append(Issue(name, "mesh_busy", f"{name} is already in a mesh ({state}).", "Stop that mesh first."))

    # -- RDMA readiness per Mac --
    rdma_ready = []
    for name, r in zip(names, reports):
        problems = []
        if not r.get("macos_ok"):
            problems.append(Issue(name, "macos_old", f"{name} runs macOS {r.get('macos') or 'unknown'}; RDMA needs 26.2 or later.",
                                  "Update macOS in System Settings > General > Software Update."))
        if (r.get("thunderbolt_generation") or 0) < 5:
            problems.append(Issue(name, "no_tb5", f"{name} has no Thunderbolt 5 ports, so it cannot use RDMA.",
                                  "Use a Mac with Thunderbolt 5 (M4 Pro/Max or later Pro/Max, M3 Ultra or later), "
                                  "or allow a slower TCP mesh."))
        if r.get("rdma") != "enabled":
            problems.append(Issue(name, "rdma_disabled", f"RDMA is {r.get('rdma') or 'unknown'} on {name}.",
                                  "Run `rdma_ctl enable` from macOS Recovery on that Mac (see Setup)."))
        elif not _active_devices(r):
            problems.append(Issue(name, "rdma_inactive", f"{name} has no RDMA port in PORT_ACTIVE.",
                                  "Connect Thunderbolt 5 cables directly between the Macs and check `ibv_devinfo`."))
        if (r.get("bridge") or {}).get("active"):
            problems.append(Issue(name, "bridge_on", f"The Thunderbolt Bridge is active on {name}.",
                                  "System Settings > Network > Thunderbolt Bridge > Inactivate (needed for RDMA)."))
        rdma_ready.append(not problems)
        (warnings if allow_tcp else blockers).extend(problems)

    # -- topology --
    count, devices = link_matrix(reports) if n else ([], [])
    full_mesh = n >= 2 and all(count[i][j] > 0 for i in range(n) for j in range(n) if i != j)
    ring = None if full_mesh else find_ring(count)
    topology = "full-mesh" if full_mesh else "ring" if ring else (
        "partial" if any(any(row) for row in count) else "none")
    order = list(range(n))
    backend = ""
    use_rdma = n >= 2 and all(rdma_ready)
    if use_rdma and full_mesh:
        backend = "jaccl"
    elif use_rdma and ring:
        backend, order = "jaccl-ring", ring
    elif n >= 2:
        if use_rdma:
            missing = [f"{names[i]} and {names[j]}" for i in range(n) for j in range(i + 1, n) if count[i][j] == 0]
            short = mesh_port_shortfall(reports)
            fix = ("Connect every pair directly (full mesh)." if not short else
                   f"A full mesh of {n} Macs needs {n - 1} Thunderbolt 5 ports on every Mac, but "
                   + ", ".join(f"{name} has {ports}" for name, ports in short)
                   + ". Cable them in a ring instead (2 ports per Mac); Bridle then uses JACCL's ring mode.")
            issue = Issue("", "not_cabled", "These Macs are not directly connected by Thunderbolt: "
                          + "; ".join(missing[:4]) + ("…" if len(missing) > 4 else "") + ".", fix)
            (warnings if allow_tcp else blockers).append(issue)
        if allow_tcp:
            if all(r.get("lan_ip") for r in reports):
                backend = "ring"
                warnings.append(Issue("", "tcp_fallback", "Using a TCP ring instead of RDMA (about 300 µs per hop "
                                      "instead of under 50 µs). Expect much slower generation.", ""))
            else:
                blockers.append(Issue("", "no_lan_ip", "A Mac has no network address for the TCP ring.", ""))

    if n > TESTED_MESH_SIZE and backend.startswith("jaccl"):
        warnings.append(Issue("", "large_mesh", f"{n} Macs is beyond the 2–4 Mac meshes Apple and MLX test most; "
                              "expect less proven behaviour and, in a ring, higher latency between non-adjacent Macs.",
                              "If it misbehaves, try the largest group of up to 4 Macs that fits the model."))

    # -- parallelism --
    support = model_support or {}
    chosen = strategy
    if chosen == "auto":
        chosen = "tensor" if backend == "jaccl" else "pipeline"
        if support.get(chosen) is False:
            chosen = "pipeline" if chosen == "tensor" else "tensor"
    if backend and support.get(chosen) is False:
        blockers.append(Issue("", "unsupported_split", f"{model} does not support {chosen} parallelism in MLX.",
                              "Pick the other split, or a model family MLX can shard (Qwen, Llama, DeepSeek, GLM, gpt-oss…)."))
    if backend == "ring" and chosen == "tensor":
        warnings.append(Issue("", "tensor_over_tcp", "Tensor parallelism over TCP is very slow; pipeline is recommended.", ""))

    # -- memory --
    pooled = sum(int(r.get("memory_total_bytes") or 0) for r in reports)
    node_rows = []
    share = size / n if n and size else 0
    for rank, idx in enumerate(order):
        r, name = reports[idx], names[idx]
        avail = int(r.get("memory_available_bytes") or 0)
        need = int(share * 1.1) + (1 << 30) if share else 0   # weights + ~10% KV/activations + 1 GiB
        fits = not share or need <= avail
        if share and not fits:
            blockers.append(Issue(name, "memory_short", f"{name} needs about {need / 1e9:.1f} GB free for its share "
                                  f"but has {avail / 1e9:.1f} GB.",
                                  "Close apps or unload models there, add another Mac, or pick a smaller model."))
        node_rows.append({"rank": rank, "name": name, "index": idx, "chip": r.get("chip"),
                          "memory_total_bytes": r.get("memory_total_bytes"), "memory_available_bytes": avail,
                          "share_bytes": int(share), "fits": fits, "lan_ip": r.get("lan_ip", ""),
                          "rdma_ready": rdma_ready[idx] if idx < len(rdma_ready) else False})
    if size and n >= 2 and size * 1.1 + (1 << 30) < int(reports[0].get("memory_available_bytes") or 0):
        warnings.append(Issue("", "fits_one_mac", f"{model} fits on this Mac alone; the mesh only adds communication "
                              "overhead for it.", "Use the mesh for models larger than one Mac's memory."))

    devices_matrix = None
    if backend in ("jaccl", "jaccl-ring"):
        devices_matrix = jaccl_matrix(order, devices, neighbours_only=backend == "jaccl-ring")
        for a, row in enumerate(devices_matrix):
            node = reports[order[a]]
            active = _active_devices(node)
            for entry in row:
                for dev in ([entry] if isinstance(entry, str) else entry or []):
                    if dev not in active:
                        blockers.append(Issue(names[order[a]], "link_down", f"{dev} on {names[order[a]]} is cabled but not PORT_ACTIVE.",
                                              "Reseat the cable, make sure RDMA is enabled on both ends, then check `ibv_devinfo`."))
    coordinator = f"{reports[0].get('lan_ip', '')}:{JACCL_PORT}" if reports else ""
    if backend.startswith("jaccl") and not (reports[0].get("lan_ip") if reports else ""):
        blockers.append(Issue(names[0], "no_coordinator_ip", "This Mac needs a network address the other Macs can reach "
                              "to set up the RDMA connections.", "Connect it to Wi-Fi or Ethernet."))
    ring_hosts = ([[f"{reports[i]['lan_ip']}:{RING_BASE_PORT + rank}"] for rank, i in enumerate(order)]
                  if backend == "ring" else None)
    if not backend and n >= 2 and not blockers:
        blockers.append(Issue("", "no_backend", "No usable connection between the Macs.", "See Setup."))
    return Plan(ok=not blockers and bool(backend), model=model, backend=backend,
                strategy=chosen if backend else "", topology=topology, nodes=node_rows,
                devices=devices_matrix, ring_hosts=ring_hosts, coordinator=coordinator,
                pooled_memory_bytes=pooled, model_size_bytes=size, max_full_mesh=max_full_mesh(reports),
                blockers=blockers, warnings=warnings)


def model_support(model_type: str) -> dict:
    """Which splits mlx-lm implements for a model family ({} = unknown; MLX decides at load)."""
    try:
        import importlib
        module = importlib.import_module(f"mlx_lm.models.{model_type}")
        from mlx_lm.models.pipeline import PipelineMixin
    except Exception:  # noqa: BLE001 - mlx-lm absent (tests) or unknown family
        return {}
    model_cls = getattr(module, "Model", None)
    pipeline = any(isinstance(c, type) and issubclass(c, PipelineMixin) and c is not PipelineMixin
                   for c in vars(module).values())
    return {"tensor": bool(model_cls is not None and hasattr(model_cls, "shard")), "pipeline": pipeline}


def mesh_port_shortfall(reports: list[dict]) -> list[tuple[str, int]]:
    """Macs with too few Thunderbolt ports for a full mesh of this size (n - 1 cables each).
    Links are point-to-point and there are no Thunderbolt 5 switches, so ports cap the mesh:
    a Mac with p ports can be in a full mesh of at most p + 1 Macs (6 ports -> 7 Macs)."""
    need = len(reports) - 1
    return [(r.get("name") or "a Mac", int(r["thunderbolt_ports"])) for r in reports
            if isinstance(r.get("thunderbolt_ports"), int) and r["thunderbolt_ports"] < need]


def max_full_mesh(reports: list[dict]) -> int | None:
    """Largest full mesh these Macs' ports allow (min ports + 1), or None if ports are unknown."""
    ports = [r.get("thunderbolt_ports") for r in reports if isinstance(r.get("thunderbolt_ports"), int)]
    return min(ports) + 1 if ports else None
