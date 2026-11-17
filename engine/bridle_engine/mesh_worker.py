"""One rank of a Bridle mesh.

Started by MeshSession as `python -m bridle_engine.mesh_worker` with BRIDLE_MESH_CONFIG naming a
0600 JSON file (deleted as soon as it is read). The worker sets the same environment `mlx.launch`
gives each rank (MLX_RANK, MLX_JACCL_COORDINATOR, MLX_IBV_DEVICES / MLX_HOSTFILE), joins the MLX
distributed group with the backend named explicitly, then runs mlx_lm.server in distributed mode:
rank 0 serves an OpenAI-compatible API on loopback (the Bridle engine proxies to it) and the other
ranks follow rank 0's requests. Every rank loads only its share of the weights.

Status lines on stdout (parsed by MeshSession):
  BRIDLE_MESH_JOINING <rank>/<world> <backend>
  BRIDLE_MESH_JOINED <rank>/<world>
  BRIDLE_MESH_ERROR <message>
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time

BACKENDS = ("jaccl", "jaccl-ring", "ring")


def build_env(cfg: dict, workdir: str) -> dict[str, str]:
    """The per-rank environment, exactly as mlx.launch sets it (mlx/_distributed_utils/launch.py)."""
    backend = cfg["backend"]
    if backend not in BACKENDS:
        raise ValueError(f"unknown mesh backend {backend!r}")
    env = {"MLX_RANK": str(int(cfg["rank"])), "MLX_METAL_FAST_SYNCH": "1"}
    if backend in ("jaccl", "jaccl-ring"):
        devices = os.path.join(workdir, "ibv_devices.json")
        with open(devices, "w") as fh:
            json.dump(cfg["devices"], fh)
        env["MLX_IBV_DEVICES"] = devices
        env["MLX_JACCL_COORDINATOR"] = cfg["coordinator"]
        if backend == "jaccl-ring":
            env["MLX_JACCL_RING"] = "1"
    else:
        hostfile = os.path.join(workdir, "hostfile.json")
        with open(hostfile, "w") as fh:
            json.dump(cfg["ring_hosts"], fh)
        env["MLX_HOSTFILE"] = hostfile
    return env


def server_argv(cfg: dict) -> list[str]:
    argv = ["mlx_lm.server", "--model", cfg["model_path"], "--host", "127.0.0.1",
            "--port", str(int(cfg.get("port") or 0))]
    if cfg["strategy"] == "pipeline":
        argv.append("--pipeline")
    return argv


def _watch_parent(pid: int) -> None:
    """Exit when the engine that started us goes away, so no rank is ever orphaned."""
    while True:
        time.sleep(1)
        if os.getppid() != pid:
            os._exit(3)


def _fail(message: str) -> int:
    print(f"BRIDLE_MESH_ERROR {message}", flush=True)
    return 2


def main() -> int:
    path = os.environ.get("BRIDLE_MESH_CONFIG", "")
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        return _fail(f"cannot read mesh config: {exc}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    parent = int(os.environ.get("BRIDLE_PARENT_PID") or 0)
    if parent:
        threading.Thread(target=_watch_parent, args=(parent,), daemon=True).start()

    rank, world, backend = int(cfg["rank"]), int(cfg["world"]), cfg["backend"]
    os.environ.update(build_env(cfg, tempfile.mkdtemp(prefix="bridle-mesh-")))
    print(f"BRIDLE_MESH_JOINING {rank}/{world} {backend}", flush=True)
    try:
        import mlx.core as mx
        # Name the backend explicitly: init(backend="any") can settle on a size-1 ring group when
        # MLX_RANK is set without MLX_HOSTFILE (ml-explore/mlx#3442), silently running single-node.
        group = mx.distributed.init(strict=True, backend="ring" if backend == "ring" else "jaccl")
    except Exception as exc:  # noqa: BLE001 - surface every join failure to the coordinator
        return _fail(f"could not join the {backend} group: {exc}")
    if group.size() != world or group.rank() != rank:
        return _fail(f"joined as rank {group.rank()} of {group.size()}, expected {rank} of {world}")
    if mx.distributed.init().size() != world:
        return _fail("MLX's default distributed group is not the mesh group; update MLX on every Mac")
    print(f"BRIDLE_MESH_JOINED {rank}/{world}", flush=True)

    sys.argv = server_argv(cfg)
    from mlx_lm import server
    server.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
