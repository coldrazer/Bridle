"""Mesh mode: probes, topology, planning, the worker environment, the member API's auth, the
session lifecycle and a full coordinator run (two engines, fake MLX workers, proxied /v1)."""

import asyncio
import json
import os
import shlex
import stat
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bridle_engine import mesh_session, mesh_worker
from bridle_engine.api import create_app
from bridle_engine.config import ServerConfig
from bridle_engine.logbuffer import LogBuffer
from bridle_engine.mesh import (find_ring, link_matrix, macos_ok, parse_bridge, parse_hardware_ports,
                                parse_ibv_devices, parse_ibv_devinfo, parse_thunderbolt_ports, plan)
from bridle_engine.service import Engine

REPO = "mlx-community/Big-Model-4bit"
GB = 1_000_000_000

# ---- probes ------------------------------------------------------------------------------------------
IBV_DEVINFO = """hca_id:\trdma_en3
\ttransport:\t\t\tThunderbolt (100)
\tphys_port_cnt:\t\t\t1
\t\tport:\t1
\t\t\tstate:\t\t\tPORT_ACTIVE (4)

hca_id:\trdma_en4
\ttransport:\t\t\tThunderbolt (100)
\t\tport:\t1
\t\t\tstate:\t\t\tPORT_DOWN (1)
"""

IBV_DEVICES = """    device          \t   node GUID
    ------          \t----------------
    rdma_en2        \t0000000000000000
    rdma_en3        \t0000000000000000
"""

HARDWARE_PORTS = """
Hardware Port: Ethernet
Device: en0
Ethernet Address: aa:bb:cc:dd:ee:ff

Hardware Port: Thunderbolt 1
Device: en2
Ethernet Address: 36:00:00:00:00:01

Hardware Port: Thunderbolt 2
Device: en3
Ethernet Address: 36:00:00:00:00:02

Hardware Port: Thunderbolt Bridge
Device: bridge0
"""

PROFILER = {"SPThunderboltDataType": [
    {"_name": "thunderboltusb4_bus_0", "device_name_key": "Mac Studio", "domain_uuid_key": "UUID-A1",
     "receptacle_1_tag": {"receptacle_id_key": "1"},
     "_items": [{"device_name_key": "Mac mini", "domain_uuid_key": "UUID-B1"}]},
    {"_name": "thunderboltusb4_bus_1", "device_name_key": "Mac Studio", "domain_uuid_key": "UUID-A2",
     "receptacle_1_tag": {"receptacle_id_key": "2"}},
    {"_name": "something_else"},
]}


def test_probe_parsers():
    assert parse_ibv_devinfo(IBV_DEVINFO) == {"rdma_en3": "PORT_ACTIVE", "rdma_en4": "PORT_DOWN"}
    assert parse_ibv_devinfo(None) == {}
    assert parse_ibv_devices(IBV_DEVICES) == ["rdma_en2", "rdma_en3"]
    assert parse_hardware_ports(HARDWARE_PORTS) == {"Ethernet": "en0", "Thunderbolt 1": "en2",
                                                    "Thunderbolt 2": "en3", "Thunderbolt Bridge": "bridge0"}
    ports = parse_thunderbolt_ports(PROFILER, parse_hardware_ports(HARDWARE_PORTS))
    assert [(p["iface"], p["uuid"], p["connected_to"]) for p in ports] == [
        ("en2", "UUID-A1", "UUID-B1"), ("en3", "UUID-A2", None)]
    assert ports[0]["connected_name"] == "Mac mini"
    active = parse_bridge("bridge0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
                          "\tmember: en2 flags=3<LEARNING,DISCOVER>\n\tstatus: active\n")
    assert active == {"present": True, "active": True, "members": ["en2"]}
    assert parse_bridge("bridge0: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500\n")["active"] is False
    assert parse_bridge(None)["present"] is False
    assert macos_ok("26.2") and macos_ok("27.0") and macos_ok("26.10")
    assert not macos_ok("26.1") and not macos_ok("15.6") and not macos_ok("")


# ---- planning ----------------------------------------------------------------------------------------
def node(name, links=(), *, mem=128, avail=None, rdma="enabled", tb=5, macos="26.2", bridge=False,
         active=None, models=((REPO, "rev1", 200),), mlx="0.32.4", ip=None, loaded=None):
    """links: [(iface, my_uuid, peer_uuid)]. Every linked iface gets an RDMA device (active unless listed)."""
    devices = [{"device": "rdma_" + i, "state": "PORT_ACTIVE" if (active is None or i in active) else "PORT_DOWN",
                "active": active is None or i in active} for i, _, _ in links]
    return {"name": name, "chip": "Apple M3 Ultra", "memory_total_bytes": mem * GB,
            "memory_available_bytes": (avail if avail is not None else mem - 8) * GB,
            "macos": macos, "macos_ok": macos_ok(macos), "thunderbolt_generation": tb, "rdma": rdma,
            "rdma_devices": devices,
            "links": [{"iface": i, "uuid": u, "connected_to": c, "tag": "1"} for i, u, c in links],
            "bridge": {"present": True, "active": bridge, "members": []}, "lan_ip": ip or f"10.0.0.{len(name)}",
            "engine_version": "0.2.0", "mlx_version": mlx, "mlx_lm_version": "0.32.2",
            "models": [{"repo": r, "revision": v, "size_bytes": s * GB} for r, v, s in models],
            "loaded_model": loaded, "mesh": {"state": "idle"}}


def pair():
    return [node("studio", [("en3", "A1", "B1")]), node("mini", [("en5", "B1", "A1")])]


def full_mesh(n):
    """n Macs, every pair cabled once. Node i's port to node j is en{10+j} with uuid f"{i}-{j}"."""
    return [node(f"mac{i}", [(f"en{10 + j}", f"{i}-{j}", f"{j}-{i}") for j in range(n) if j != i]) for i in range(n)]


def test_two_macs_one_cable_is_a_jaccl_tensor_mesh():
    p = plan(pair(), REPO)
    assert p.ok, [b.message for b in p.blockers]
    assert (p.backend, p.strategy, p.topology) == ("jaccl", "tensor", "full-mesh")
    assert p.devices == [[None, "rdma_en3"], ["rdma_en5", None]]
    assert p.coordinator == "10.0.0.6:32323"            # coordinator's LAN IP, fixed bootstrap port
    assert [r["share_bytes"] for r in p.nodes] == [100 * GB, 100 * GB]
    assert p.pooled_memory_bytes == 256 * GB and p.ring_hosts is None


def test_device_matrix_matches_mlx_launch_validation_for_a_full_mesh():
    p = plan(full_mesh(4), REPO)
    assert p.ok and p.backend == "jaccl"
    for i, row in enumerate(p.devices):
        assert len(row) == 4 and row[i] is None                       # mlx.launch: self is null
        assert all(row[j] == f"rdma_en{10 + j}" for j in range(4) if j != i)  # every pair listed


def test_two_cables_between_a_pair_list_both_devices():
    reports = [node("a", [("en3", "A1", "B1"), ("en4", "A2", "B2")]),
               node("b", [("en5", "B1", "A1"), ("en6", "B2", "A2")])]
    count, _ = link_matrix(reports)
    assert count == [[0, 2], [2, 0]]
    assert plan(reports, REPO).devices == [[None, ["rdma_en3", "rdma_en4"]], [["rdma_en5", "rdma_en6"], None]]


def test_ring_of_five_uses_jaccl_ring_in_ring_order():
    # 0-2-4-1-3-0: a ring that is not in index order, so ranks must be reordered along it.
    cycle = [0, 2, 4, 1, 3]
    links = {i: [] for i in range(5)}
    for k in range(5):
        a, b = cycle[k], cycle[(k + 1) % 5]
        links[a].append((f"en{10 + b}", f"{a}-{b}", f"{b}-{a}"))
        links[b].append((f"en{10 + a}", f"{b}-{a}", f"{a}-{b}"))
    reports = [node(f"mac{i}", links[i]) for i in range(5)]
    p = plan(reports, REPO)
    assert p.ok and (p.backend, p.topology, p.strategy) == ("jaccl-ring", "ring", "pipeline")
    order = [r["index"] for r in p.nodes]
    assert order[0] == 0 and find_ring(link_matrix(reports)[0]) == order
    for a, row in enumerate(p.devices):
        left, right = (a - 1) % 5, (a + 1) % 5
        for b, entry in enumerate(row):
            if b in (left, right):
                assert entry == f"rdma_en{10 + order[b]}"
            else:
                assert entry is None                                 # jaccl-ring: neighbours only


def test_port_limits_shape_the_advice():
    from bridle_engine.mesh import max_full_mesh, mesh_port_shortfall
    # 8 Macs with 6 ports each (M5 Ultra Mac Studio): a full mesh needs 7 ports, so advise a ring.
    reports = [node(f"u{i}", [(f"en{10 + ((i + d) % 8)}", f"{i}-{(i + d) % 8}", f"{(i + d) % 8}-{i}") for d in (1, 7)])
               for i in range(8)]
    for r in reports:
        r["thunderbolt_ports"] = 6
    assert max_full_mesh(reports) == 7 and len(mesh_port_shortfall(reports)) == 8
    p = plan(reports, REPO)
    assert p.ok and p.backend == "jaccl-ring" and p.max_full_mesh == 7     # cabled as a ring: fine
    assert any(w.code == "large_mesh" for w in p.warnings)
    for r in reports:                                                    # break the ring: now advise
        r["links"] = r["links"][:1]
    blocked = plan(reports, REPO)
    fix = next(b.fix for b in blocked.blockers if b.code == "not_cabled")
    assert "needs 7 Thunderbolt 5 ports" in fix and "ring" in fix
    seven = full_mesh(7)
    for r in seven:
        r["thunderbolt_ports"] = 6
    p7 = plan(seven, REPO)
    assert p7.ok and p7.backend == "jaccl" and not mesh_port_shortfall(seven)   # 7 is the full-mesh maximum
    assert not any(w.code == "large_mesh" for w in plan(full_mesh(4), REPO).warnings)


def test_partial_cabling_blocks_unless_tcp_is_allowed():
    reports = [node("a", [("en3", "A1", "B1")]), node("b", [("en5", "B1", "A1"), ("en6", "B2", "C1")]),
               node("c", [("en7", "C1", "B2")])]
    p = plan(reports, REPO)
    assert not p.ok and any(b.code == "not_cabled" for b in p.blockers)
    assert "a and c" in next(b.message for b in p.blockers if b.code == "not_cabled")
    tcp = plan(reports, REPO, allow_tcp=True)
    assert tcp.ok and (tcp.backend, tcp.strategy) == ("ring", "pipeline")
    assert tcp.ring_hosts == [["10.0.0.1:32400"], ["10.0.0.1:32401"], ["10.0.0.1:32402"]]
    assert any(w.code == "tcp_fallback" for w in tcp.warnings)


@pytest.mark.parametrize("change, code", [
    ({"macos": "26.1"}, "macos_old"),
    ({"tb": 4}, "no_tb5"),
    ({"rdma": "disabled"}, "rdma_disabled"),
    ({"bridge": True}, "bridge_on"),
    ({"active": set()}, "rdma_inactive"),
    ({"models": ()}, "model_missing"),
    ({"models": ((REPO, "rev2", 200),)}, "revision_mismatch"),
    ({"mlx": "0.31.0"}, "mlx_mismatch"),
    ({"avail": 50}, "memory_short"),
    ({"loaded": "some/model"}, "model_loaded"),
])
def test_every_readiness_problem_is_a_blocker_with_a_fix(change, code):
    reports = [node("studio", [("en3", "A1", "B1")]), node("mini", [("en5", "B1", "A1")], **change)]
    p = plan(reports, REPO)
    assert not p.ok
    issue = next(b for b in p.blockers if b.code == code)
    assert issue.message and (issue.fix or code in ("revision_mismatch",))
    if code == "rdma_disabled":
        assert "rdma_ctl enable" in issue.fix and "Recovery" in issue.fix


def test_link_down_on_a_cabled_port_is_reported():
    reports = [node("a", [("en3", "A1", "B1"), ("en4", "A2", "B2")], active={"en3"}),
               node("b", [("en5", "B1", "A1"), ("en6", "B2", "A2")])]
    p = plan(reports, REPO)
    assert any(b.code == "link_down" and "rdma_en4" in b.message for b in p.blockers)


def test_model_support_drives_the_split():
    assert plan(pair(), REPO, model_support={"tensor": False, "pipeline": True}).strategy == "pipeline"
    p = plan(pair(), REPO, strategy="tensor", model_support={"tensor": False, "pipeline": True})
    assert any(b.code == "unsupported_split" for b in p.blockers)
    with pytest.raises(ValueError):
        plan(pair(), REPO, strategy="sideways")


def test_single_mac_and_small_models():
    assert any(b.code == "too_few_nodes" for b in plan(pair()[:1], REPO).blockers)
    small = [node("a", [("en3", "A1", "B1")], models=((REPO, "r", 10),)), node("b", [("en5", "B1", "A1")], models=((REPO, "r", 10),))]
    p = plan(small, REPO)
    assert p.ok and any(w.code == "fits_one_mac" for w in p.warnings)


# ---- worker environment ------------------------------------------------------------------------------
def test_worker_env_matches_mlx_launch(tmp_path):
    cfg = {"rank": 1, "world": 2, "backend": "jaccl", "coordinator": "10.0.0.6:32323",
           "devices": [[None, "rdma_en3"], ["rdma_en5", None]], "strategy": "tensor", "model_path": "/m", "port": 5}
    env = mesh_worker.build_env(cfg, str(tmp_path))
    assert env["MLX_RANK"] == "1" and env["MLX_JACCL_COORDINATOR"] == "10.0.0.6:32323"
    assert json.loads(Path(env["MLX_IBV_DEVICES"]).read_text()) == cfg["devices"]
    assert "MLX_JACCL_RING" not in env and env["MLX_METAL_FAST_SYNCH"] == "1"
    assert mesh_worker.build_env({**cfg, "backend": "jaccl-ring"}, str(tmp_path))["MLX_JACCL_RING"] == "1"
    ring = mesh_worker.build_env({"rank": 0, "backend": "ring", "ring_hosts": [["1.2.3.4:32400"], ["1.2.3.5:32401"]]}, str(tmp_path))
    assert json.loads(Path(ring["MLX_HOSTFILE"]).read_text()) == [["1.2.3.4:32400"], ["1.2.3.5:32401"]]
    with pytest.raises(ValueError):
        mesh_worker.build_env({**cfg, "backend": "carrier-pigeon"}, str(tmp_path))
    assert mesh_worker.server_argv(cfg) == ["mlx_lm.server", "--model", "/m", "--host", "127.0.0.1", "--port", "5"]
    assert mesh_worker.server_argv({**cfg, "strategy": "pipeline"})[-1] == "--pipeline"


# ---- engines, fake models, fake workers -----------------------------------------------------------------
def make_model(models_dir: Path, repo=REPO, revision="rev1"):
    d = models_dir / repo
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"model_type": "qwen2"}))
    (d / ".bridle.json").write_text(json.dumps({"repo": repo, "revision": revision, "size_bytes": 200 * GB,
                                                "files": [], "completed_at": "now"}))


FAKE_WORKER = textwrap.dedent('''
    import json, os, sys, time, threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    path = os.environ["BRIDLE_MESH_CONFIG"]
    cfg = json.load(open(path)); os.unlink(path)
    with open(os.environ["FAKE_MESH_LOG"] + f".{cfg['rank']}", "w") as fh:
        json.dump({"cfg": cfg, "argv": sys.argv}, fh)
    if cfg.get("fail"):
        print("BRIDLE_MESH_ERROR could not join the jaccl group: Couldn't connect", flush=True); sys.exit(2)
    print(f"BRIDLE_MESH_JOINED {cfg['rank']}/{cfg['world']}", flush=True)
    if cfg["rank"] != 0:
        while True: time.sleep(0.2)
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            if body.get("stream"):
                self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
                for word in ("pooled", " memory"):
                    chunk = {"model": body["model"], "choices": [{"index": 0, "delta": {"content": word}}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\\n\\n").encode())
                self.wfile.write(b"data: [DONE]\\n\\n"); return
            out = {"model": body["model"], "echo": body,
                   "choices": [{"index": 0, "message": {"role": "assistant", "content": "pooled memory"}}]}
            data = json.dumps(out).encode()
            self.send_response(200); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)
    ThreadingHTTPServer(("127.0.0.1", cfg["port"]), H).serve_forever()
''')


@pytest.fixture
def fake_python(tmp_path, monkeypatch):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    wrapper = tmp_path / "fake-python"
    wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))}\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_MESH_LOG", str(tmp_path / "worker"))
    return str(wrapper)


def engine_app(root: Path, name: str, python: str, with_model=True):
    root.mkdir(parents=True, exist_ok=True)
    if with_model:
        make_model(root / "m")
    eng = Engine.build(ServerConfig(models_dir=root / "m", data_dir=root, control_token="secret"), LogBuffer())
    object.__setattr__(eng, "machine", eng.machine.__class__(**{**eng.machine.to_json(), "hostname": name}))
    eng.mesh = mesh_session.MeshSession(eng, python=python)
    return eng, create_app(eng)


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_session_lifecycle_and_private_launch_file(tmp_path, fake_python):
    eng, _ = engine_app(tmp_path / "a", "studio", fake_python)
    cfg = {"rank": 1, "world": 2, "backend": "jaccl", "strategy": "tensor", "model": REPO, "revision": "rev1",
           "coordinator": "10.0.0.6:32323", "devices": [[None, "rdma_en3"], ["rdma_en5", None]]}
    status = eng.mesh.start(cfg)
    assert status["state"] == "joining" and eng.mesh.active
    assert wait_for(lambda: eng.mesh.state == "loading")
    recorded = json.loads(Path(str(tmp_path / "worker") + ".1").read_text())
    assert recorded["cfg"]["model_path"].endswith(REPO)                 # resolved from this Mac's store
    assert not list((tmp_path / "a" / "mesh").glob("launch-*.json"))   # secrets file consumed
    with pytest.raises(mesh_session.MeshError):                       # one mesh at a time
        eng.mesh.start(cfg)
    assert eng.mesh.stop()["state"] == "stopped" and not eng.mesh.active
    failing = eng.mesh.start({**cfg, "fail": True})
    assert failing["state"] == "joining"
    assert wait_for(lambda: eng.mesh.state == "failed")
    assert "Couldn't connect" in eng.mesh.status()["error"]


def test_session_refuses_bad_configs(tmp_path, fake_python):
    eng, _ = engine_app(tmp_path / "a", "studio", fake_python)
    base = {"rank": 1, "world": 2, "backend": "jaccl", "strategy": "tensor", "model": REPO,
            "coordinator": "10.0.0.6:32323", "devices": [[None, "x"], ["y", None]]}
    for bad in ({**base, "backend": "smoke"}, {**base, "rank": 2}, {**base, "devices": None},
                {k: v for k, v in base.items() if k != "model"}, {**base, "model": "other/model"},
                {**base, "revision": "different"}):
        with pytest.raises(mesh_session.MeshError):
            eng.mesh.start(bad)
    launch = tmp_path / "a" / "mesh"
    assert not launch.exists() or not list(launch.iterdir())


def test_launch_file_is_private(tmp_path):
    target = tmp_path / "mesh" / "launch-1.json"
    mesh_session._write_private(target, b"{}")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# ---- member API auth -------------------------------------------------------------------------------------
def test_member_api_needs_a_mesh_key_and_https(tmp_path, fake_python):
    eng, app = engine_app(tmp_path / "a", "mini", fake_python)
    local = TestClient(create_app(eng, loopback_hosts=frozenset({"testclient"})), base_url="http://127.0.0.1:49813")
    assert local.get("/mesh/status").status_code == 401
    _, plain = eng.keys.create("app key")
    assert local.get("/mesh/status", headers={"authorization": f"Bearer {plain}"}).status_code == 403
    _, mesh_key = eng.keys.create("Device: studio", allow_mesh=True)
    ok = local.get("/mesh/status", headers={"authorization": f"Bearer {mesh_key}"})
    assert ok.status_code == 200 and ok.json()["state"] == "idle"
    assert local.post("/mesh/join", json={"rank": 0}, headers={"authorization": f"Bearer {mesh_key}"}).status_code == 400
    remote = TestClient(create_app(eng, loopback_hosts=frozenset()), base_url="http://127.0.0.1:49813")
    assert remote.get("/mesh/status", headers={"authorization": f"Bearer {mesh_key}"}).status_code == 426


def test_pairing_can_grant_mesh_and_keys_default_to_no_mesh(tmp_path, fake_python):
    eng, _ = engine_app(tmp_path / "a", "mini", fake_python)
    key, _ = eng.keys.create("plain")
    assert key.allow_mesh is False and "allow_mesh" in key.public()


# ---- coordinator end to end ---------------------------------------------------------------------------------
def test_coordinator_plans_starts_serves_and_stops(tmp_path, fake_python, monkeypatch):
    studio, studio_app = engine_app(tmp_path / "studio", "studio", fake_python)
    mini, mini_app = engine_app(tmp_path / "mini", "mini", fake_python)
    _, mini_secret = mini.keys.create("Device: studio", allow_mesh=True)

    def fake_report(engine, runner=None):
        name = engine.machine.hostname
        links = [("en3", "A1", "B1")] if name == "studio" else [("en5", "B1", "A1")]
        r = node(name, links, ip="127.0.0.1")
        r["models"] = [{"repo": m.repo, "revision": m.revision, "size_bytes": m.size_bytes} for m in engine.store.list()]
        r["mesh"] = engine.mesh.status()
        r["loaded_model"] = None
        return r
    monkeypatch.setattr(mesh_session, "node_report", fake_report)

    async def call_member(peer, secret, method, path, body=None, timeout=20.0):
        assert secret == mini_secret and peer["name"] == "mini"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mini_app), base_url="http://127.0.0.1:49813") as c:
            resp = await c.request(method, path, json=body, headers={"authorization": f"Bearer {secret}"})
        if resp.status_code >= 300:
            raise mesh_session.MeshError(f"mini: HTTP {resp.status_code} {resp.text}", resp.status_code)
        return resp.json()

    coordinator = studio_app.state.mesh
    coordinator.call = call_member
    coordinator.operations.state["peers"]["p1"] = {"id": "p1", "name": "mini", "url": "https://mini.local:49814",
                                                    "credential_id": "p1", "ca_pem": "x"}
    coordinator.operations.vault.put("p1", mini_secret)
    ctl = {"authorization": "Bearer secret"}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=studio_app), base_url="http://127.0.0.1:49813",
                                     timeout=30) as c:
            overview = (await c.get("/control/mesh", headers=ctl)).json()
            assert overview["setup"]["command"] == "rdma_ctl enable" and overview["peers"][0]["id"] == "p1"
            planned = (await c.post("/control/mesh/plan", headers=ctl, json={"model": REPO, "peers": ["p1"]})).json()
            assert planned["ok"] and planned["backend"] == "jaccl" and [n["peer_id"] for n in planned["nodes"]] == ["", "p1"]
            started = await c.post("/control/mesh/start", headers=ctl, json={"model": REPO, "peers": ["p1"]})
            assert started.status_code == 202
            for _ in range(200):
                state = (await c.get("/control/mesh", headers=ctl)).json()["coordinator"]["state"]
                if state in ("ready", "failed"):
                    break
                await asyncio.sleep(0.05)
            status = (await c.get("/control/mesh", headers=ctl)).json()["coordinator"]
            assert status["state"] == "ready", status
            assert mini.mesh.state == "loading" and mini.mesh.rank == 1          # member joined as rank 1
            member_cfg = json.loads(Path(str(tmp_path / "worker") + ".1").read_text())["cfg"]
            assert member_cfg["devices"] == [[None, "rdma_en3"], ["rdma_en5", None]]
            assert member_cfg["coordinator"] == "127.0.0.1:32323" and member_cfg["world"] == 2

            # The pooled model is served by this engine's /v1, routed to rank 0.
            models = (await c.get("/v1/models")).json()["data"]
            assert models[0]["id"] == REPO and models[0]["kind"] == "mesh"
            reply = (await c.post("/v1/chat/completions", json={
                "model": REPO, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5,
                "draft_model": "evil/model", "adapters": "x"})).json()
            assert reply["model"] == REPO and reply["choices"][0]["message"]["content"] == "pooled memory"
            assert reply["echo"]["model"] == "default_model"                     # never another path
            assert "draft_model" not in reply["echo"] and "adapters" not in reply["echo"]
            assert reply["bridle"]["mesh"] == {"world": 2, "backend": "jaccl", "strategy": "tensor"}
            streamed = await c.post("/v1/chat/completions", json={"model": REPO, "stream": True,
                                                                   "messages": [{"role": "user", "content": "hi"}]})
            chunks = [json.loads(line[6:]) for line in streamed.text.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            assert "".join(ch["choices"][0]["delta"]["content"] for ch in chunks) == "pooled memory"
            assert all(ch["model"] == REPO for ch in chunks) and "data: [DONE]" in streamed.text

            # While the mesh holds the memory, nothing loads next to it.
            assert (await c.post("/control/models/load", headers=ctl, json={"repo": REPO})).status_code == 409
            assert (await c.post("/control/mesh/start", headers=ctl, json={"model": REPO, "peers": ["p1"]})).status_code == 409

            stopped = (await c.post("/control/mesh/stop", headers=ctl)).json()
            assert stopped["state"] == "idle"
            assert studio.mesh.state == "stopped" and mini.mesh.state == "stopped"
            assert (await c.get("/v1/models")).json()["data"] == [] or \
                (await c.get("/v1/models")).json()["data"][0].get("kind") != "mesh"

    asyncio.run(scenario())


def test_start_refuses_with_the_plan_when_blocked(tmp_path, fake_python, monkeypatch):
    studio, studio_app = engine_app(tmp_path / "studio", "studio", fake_python)
    monkeypatch.setattr(mesh_session, "node_report", lambda engine, runner=None: node("studio", rdma="disabled"))
    c = TestClient(create_app(studio, loopback_hosts=frozenset({"testclient"}), control_hosts=frozenset({"testclient"})),
                   base_url="http://127.0.0.1:49813")
    resp = c.post("/control/mesh/start", headers={"authorization": "Bearer secret"}, json={"model": REPO, "peers": []})
    assert resp.status_code == 409
    codes = {b["code"] for b in resp.json()["error"]["plan"]["blockers"]}
    assert {"too_few_nodes", "rdma_disabled"} <= codes
    unknown = c.post("/control/mesh/plan", headers={"authorization": "Bearer secret"}, json={"model": REPO, "peers": ["nope"]})
    assert unknown.status_code == 404


def test_member_failure_tears_the_mesh_down(tmp_path, fake_python, monkeypatch):
    studio, studio_app = engine_app(tmp_path / "studio", "studio", fake_python)
    monkeypatch.setattr(mesh_session, "node_report",
                        lambda engine, runner=None: node(engine.machine.hostname,
                                                         [("en3", "A1", "B1")] if engine.machine.hostname == "studio"
                                                         else [("en5", "B1", "A1")], ip="127.0.0.1"))
    calls = []

    async def flaky_member(peer, secret, method, path, body=None, timeout=20.0):
        calls.append(path)
        if path == "/mesh/node":
            return node("mini", [("en5", "B1", "A1")], ip="127.0.0.1")
        if path == "/mesh/status":
            return {"state": "failed", "error": "[jaccl] Couldn't connect (error: 60)"}
        return {"state": "joining"}

    coordinator = studio_app.state.mesh
    coordinator.call = flaky_member
    coordinator.operations.state["peers"]["p1"] = {"id": "p1", "name": "mini", "url": "https://x", "credential_id": "p1", "ca_pem": "x"}
    coordinator.operations.vault.put("p1", "k")

    async def scenario():
        await coordinator.start(REPO, ["p1"])
        await asyncio.wait_for(coordinator.task, 20)
    asyncio.run(scenario())
    assert coordinator.state == "failed" and "Couldn't connect" in coordinator.error
    assert "/mesh/leave" in calls and studio.mesh.state == "failed" and not studio.mesh.active


# ---- real MLX: two ranks on this Mac, sharding a real model, served through /v1 -----------------------
REAL_MODEL = "mlx-community/Qwen2.5-0.5B-Instruct-4bit"
MODELS_DIR = os.environ.get("BRIDLE_TEST_MODELS_DIR")


def _have_real_stack() -> bool:
    if sys.platform != "darwin" or not MODELS_DIR or not (Path(MODELS_DIR) / REAL_MODEL / ".bridle.json").exists():
        return False
    try:
        import mlx.core  # noqa: F401
        import mlx_lm.server  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _have_real_stack(), reason="needs Apple silicon, MLX and the Qwen2.5-0.5B fixture model")
@pytest.mark.parametrize("strategy", ["pipeline", "tensor"])
def test_real_two_rank_mesh_generates_through_v1(tmp_path, monkeypatch, strategy):
    """The real path end to end: mesh_worker joins an MLX distributed group (TCP ring over
    loopback; RDMA needs two Macs), mlx_lm.server loads one shard per rank, the coordinator
    waits for the collective load and proxies a chat request to rank 0."""
    models = Path(MODELS_DIR)

    def build(name):
        root = tmp_path / name
        eng = Engine.build(ServerConfig(models_dir=models, data_dir=root, control_token="secret"), LogBuffer())
        object.__setattr__(eng, "machine", eng.machine.__class__(**{**eng.machine.to_json(), "hostname": name}))
        eng.mesh = mesh_session.MeshSession(eng)       # the real worker: python -m bridle_engine.mesh_worker
        return eng, create_app(eng)

    studio, studio_app = build("studio")
    mini, mini_app = build("mini")
    _, secret = mini.keys.create("Device: studio", allow_mesh=True)
    real_report = mesh_session.node_report

    def report(engine, runner=None):
        r = real_report(engine)
        r.update(lan_ip="127.0.0.1", loaded_model=None)  # both "Macs" are this Mac
        return r
    monkeypatch.setattr(mesh_session, "node_report", report)

    async def call_member(peer, key, method, path, body=None, timeout=20.0):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mini_app), base_url="http://127.0.0.1:49813",
                                     timeout=60) as c:
            resp = await c.request(method, path, json=body, headers={"authorization": f"Bearer {key}"})
        if resp.status_code >= 300:
            raise mesh_session.MeshError(f"mini: HTTP {resp.status_code} {resp.text}", resp.status_code)
        return resp.json()

    coordinator = studio_app.state.mesh
    coordinator.call = call_member
    coordinator.operations.state["peers"]["p1"] = {"id": "p1", "name": "mini", "url": "https://x",
                                                    "credential_id": "p1", "ca_pem": "x"}
    coordinator.operations.vault.put("p1", secret)
    ctl = {"authorization": "Bearer secret"}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=studio_app), base_url="http://127.0.0.1:49813",
                                     timeout=300) as c:
            body = {"model": REAL_MODEL, "peers": ["p1"], "strategy": strategy, "allow_tcp": True}
            planned = (await c.post("/control/mesh/plan", headers=ctl, json=body)).json()
            assert planned["ok"] and planned["backend"] == "ring", planned
            assert (await c.post("/control/mesh/start", headers=ctl, json=body)).status_code == 202
            for _ in range(600):
                st = (await c.get("/control/mesh", headers=ctl)).json()["coordinator"]
                if st["state"] in ("ready", "failed"):
                    break
                await asyncio.sleep(0.5)
            assert st["state"] == "ready", (st, mini.mesh.status())
            try:
                reply = (await c.post("/v1/chat/completions", json={
                    "model": REAL_MODEL, "max_tokens": 12, "temperature": 0,
                    "messages": [{"role": "user", "content": "Reply with the word: hello"}]})).json()
                text = reply["choices"][0]["message"]["content"]
                assert reply["model"] == REAL_MODEL and text.strip(), reply
                assert reply["bridle"]["mesh"]["world"] == 2 and reply["bridle"]["mesh"]["strategy"] == strategy
                assert any("BRIDLE_MESH_JOINED 1/2" in ln for ln in mini.mesh.status()["log"])
                assert any("BRIDLE_MESH_JOINED 0/2" in ln for ln in studio.mesh.status()["log"])
            finally:
                await c.post("/control/mesh/stop", headers=ctl)
            assert studio.mesh.state == "stopped" and mini.mesh.state == "stopped"

    asyncio.run(scenario())


# ---- adding a Mac from the canvas node: certificate check + connect ---------------------------------
def test_fetch_certificate_reports_the_fingerprint_bridle_shows(tmp_path):
    import socket
    import ssl
    import threading
    from bridle_engine import tls
    cert, key, fingerprint = tls.ensure_certificate(tmp_path / "tls", "mini.local", ["127.0.0.1"])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        try:
            with ctx.wrap_socket(conn, server_side=True):
                pass
        except (ssl.SSLError, OSError):
            pass
    threading.Thread(target=serve, daemon=True).start()
    got = mesh_session.fetch_certificate(f"https://127.0.0.1:{port}")
    server.close()
    assert got["fingerprint"] == fingerprint and "BEGIN CERTIFICATE" in got["pem"]
    for bad in ("http://127.0.0.1:1", "https://user:pw@host:1", "nonsense"):
        with pytest.raises(mesh_session.MeshError):
            mesh_session.fetch_certificate(bad)
    with pytest.raises(mesh_session.MeshError) as unreachable:
        mesh_session.fetch_certificate(f"https://127.0.0.1:{port}", timeout=1)
    assert "Share on network" in str(unreachable.value)


def _peer_cert(tmp_path):
    from bridle_engine import tls
    cert, _, fingerprint = tls.ensure_certificate(tmp_path / "peer-tls", "mini.local", ["127.0.0.1"])
    return Path(cert).read_text(), fingerprint


def test_connect_verifies_fingerprint_and_mesh_grant_before_saving(tmp_path, fake_python):
    studio, _ = engine_app(tmp_path / "studio", "studio", fake_python)
    pem, fingerprint = _peer_cert(tmp_path)
    app = create_app(studio, loopback_hosts=frozenset({"testclient"}), control_hosts=frozenset({"testclient"}))
    coordinator = app.state.mesh
    answers = {"good": node("mini", [("en5", "B1", "A1")]), "plain": 403, "wrong": 401}

    async def fake_call(peer, secret, method, path, body=None, timeout=20.0):
        assert path == "/mesh/node" and peer["ca_pem"].strip() == pem.strip()  # trust is pinned to the confirmed cert
        answer = answers[secret]
        if isinstance(answer, int):
            raise mesh_session.MeshError("mini: HTTP %d" % answer, answer)
        return answer
    coordinator.call = fake_call
    c = TestClient(app, base_url="http://127.0.0.1:49813")
    ctl = {"authorization": "Bearer secret"}
    base = {"name": "mini", "url": "https://mini.local:49814", "ca_pem": pem, "fingerprint": fingerprint}

    mismatch = c.post("/control/mesh/connect", headers=ctl, json={**base, "api_key": "good", "fingerprint": "AB:CD"})
    assert mismatch.status_code == 400 and "does not match" in mismatch.json()["error"]["message"]
    no_grant = c.post("/control/mesh/connect", headers=ctl, json={**base, "api_key": "plain"})
    assert no_grant.status_code == 403 and "Allow mesh" in no_grant.json()["error"]["message"]
    wrong = c.post("/control/mesh/connect", headers=ctl, json={**base, "api_key": "wrong"})
    assert wrong.status_code == 401
    assert c.get("/control/mesh/peers", headers=ctl).json()["peers"] == []      # nothing saved on failure

    ok = c.post("/control/mesh/connect", headers=ctl, json={**base, "api_key": "good",
                                                            "fingerprint": fingerprint.replace(":", " ").lower()})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["node"]["name"] == "mini" and body["node"]["rdma"] == "enabled"
    peers = c.get("/control/mesh/peers", headers=ctl).json()["peers"]
    assert len(peers) == 1 and peers[0]["mesh"] is True and peers[0]["fingerprint"] == fingerprint
    assert coordinator.operations.vault.get(peers[0]["id"]) == "good"           # key lives in the private vault
    again = c.post("/control/mesh/connect", headers=ctl, json={**base, "api_key": "good"})
    assert again.json()["peer"]["id"] == peers[0]["id"]                          # reconnecting updates, no duplicate
    assert len(c.get("/control/mesh/peers", headers=ctl).json()["peers"]) == 1
    assert c.post("/control/mesh/connect", headers=ctl, json={**base, "api_key": ""}).status_code == 400
