"""Command line: run the server, or do any of its jobs offline from a terminal.

    python -m bridle_engine serve [--host 0.0.0.0] [--port 49813]
    python -m bridle_engine download mlx-community/Qwen3-8B-4bit
    python -m bridle_engine models | info | delete <repo>
    python -m bridle_engine chat <repo> [--draft <repo>]
    python -m bridle_engine bench <repo> [--prompt-tokens 512 --generation-tokens 128]
    python -m bridle_engine embed <repo> "text" ...
    python -m bridle_engine transcribe <repo> audio.wav [--format srt]
    python -m bridle_engine keys list | create NAME [--rpm N] [--daily-tokens N] | revoke ID | delete ID
    python -m bridle_engine presets list | set NAME [--model R --system S --temperature T ...] | delete NAME
    python -m bridle_engine requests [--limit 50]
    python -m bridle_engine settings [key=value ...]
    python -m bridle_engine extras vision|embeddings|speech|all
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .catalog import ModelStore, catalog_view
from .config import DEFAULT_PORT, ServerConfig, support_dir
from .domain import ChatMessage, ParseError, RepoId, SamplingParams
from .downloads import DownloadManager
from .logbuffer import LogBuffer
from .system_info import collect_machine_info, lan_address

READY_PREFIX = "BRIDLE_READY "
EXTRAS = {
    "vision": ["mlx-vlm"],
    "embeddings": ["mlx-embeddings"],
    "speech": ["mlx-audio"],
}


def _setup_logging(level: str) -> LogBuffer:
    buffer = LogBuffer()
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    for name in ("bridle", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [buffer, stderr]
        lg.setLevel(level.upper())
        lg.propagate = False
    return buffer


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def _dirs(args: argparse.Namespace) -> tuple[Path, Path]:
    data = Path(args.data_dir).expanduser()
    models = Path(args.models_dir).expanduser() if args.models_dir else data / "models"
    return data, models


# ---- serve ------------------------------------------------------------------------------------
def _exit_with_parent(pid: int) -> None:
    """When launched by the desktop app, exit if the app goes away (even if it crashed)."""
    def watch() -> None:
        while True:
            time.sleep(2)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                os._exit(0)
            except PermissionError:
                pass

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    return sock


def _launch_secrets() -> dict:
    """Secrets handed over by the desktop app in a 0600 file; deleted after reading."""
    path = os.environ.get("BRIDLE_LAUNCH_FILE", "")
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
        Path(path).unlink(missing_ok=True)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app
    from .service import Engine

    data_dir, models_dir = _dirs(args)
    crash_dir = data_dir / "crash"
    crash_dir.mkdir(parents=True, exist_ok=True)
    fault_log = (crash_dir / "faults.log").open("a")
    fault_log.write(f"\n--- engine start {datetime.now().isoformat(timespec='seconds')} pid {os.getpid()}\n")
    fault_log.flush()
    faulthandler.enable(file=fault_log, all_threads=True)  # native crashes (e.g. Metal) leave a trace

    logs = _setup_logging(args.log_level)
    launch = _launch_secrets()
    config = ServerConfig(
        host=args.host, port=args.port, models_dir=models_dir, data_dir=data_dir,
        api_key=args.api_key or str(launch.get("api_key") or "") or os.environ.get("BRIDLE_API_KEY", ""),
        control_token=str(launch.get("control_token") or "") or os.environ.get("BRIDLE_CONTROL_TOKEN")
        or ServerConfig().control_token,
        auto_load=not args.no_auto_load, preload=args.preload or "",
        aria_model_path=args.aria_model_dir or os.environ.get("BRIDLE_ARIA_MODEL_DIR", ""),
        onyx_model_path=args.onyx_model_dir or os.environ.get("BRIDLE_ONYX_MODEL_DIR", ""),
        allowed_origins=tuple(o.strip().rstrip("/").lower()
                              for o in os.environ.get("BRIDLE_ALLOWED_ORIGINS", "").split(",") if o.strip()),
    )
    try:
        sock = _bind(config.host, config.port)
    except OSError as exc:
        print(f"error: cannot bind {config.host}:{config.port}: {exc}", file=sys.stderr)
        return 2
    parent = os.environ.get("BRIDLE_PARENT_PID", "")
    if parent.isdigit():
        _exit_with_parent(int(parent))
    engine = Engine.build(config, logs)
    engine.bound_port = sock.getsockname()[1]
    log = logging.getLogger("bridle")

    tls_sock = None
    tls_files: tuple[Path, Path] | None = None
    if engine.settings.tls:
        try:
            from .tls import ensure_certificate

            lan = lan_address()
            cert, key, fingerprint = ensure_certificate(data_dir / "tls", engine.machine.hostname,
                                                        [lan] if lan else [])
            tls_port = engine.settings.tls_port or engine.bound_port + 1
            tls_sock = _bind(config.host, tls_port)
            engine.tls_port, engine.tls_fingerprint = tls_sock.getsockname()[1], fingerprint
            tls_files = (cert, key)
            log.info("HTTPS on port %d (certificate SHA-256 %s)", engine.tls_port, fingerprint)
        except Exception as exc:  # noqa: BLE001 - HTTPS is optional; HTTP keeps working
            log.error("could not start HTTPS: %s", exc)

    app = create_app(engine)
    engine.start_discovery()

    if config.preload:
        try:
            draft = RepoId.parse(args.preload_draft) if args.preload_draft else None
            engine.runtime.load_in_background(RepoId.parse(config.preload), draft, force=True)
        except Exception as exc:  # noqa: BLE001 - a bad preload should not stop the server
            log.error("preload failed: %s", exc)

    # The desktop app waits for this line to learn the ports.
    print(READY_PREFIX + json.dumps({"port": engine.bound_port, "endpoint": engine.endpoint(),
                                     "tls_port": engine.tls_port, "version": __version__}), flush=True)
    if "BRIDLE_CONTROL_TOKEN" not in os.environ and not launch.get("control_token"):
        print("control token: generated for this launch (set BRIDLE_CONTROL_TOKEN to choose your own)",
              file=sys.stderr, flush=True)
    log.info("serving OpenAI-compatible API at %s/v1%s", engine.endpoint(),
             " and Ollama API at /api" if engine.settings.ollama_api else "")

    servers = [uvicorn.Server(uvicorn.Config(app, proxy_headers=False, log_config=None, access_log=True, timeout_graceful_shutdown=5))]
    socks = [[sock]]
    if tls_sock is not None and tls_files is not None:
        servers.append(uvicorn.Server(uvicorn.Config(app, proxy_headers=False, log_config=None, access_log=True, lifespan="off",
                                                     ssl_certfile=str(tls_files[0]), ssl_keyfile=str(tls_files[1]),
                                                     timeout_graceful_shutdown=5)))
        socks.append([tls_sock])

    async def run_all() -> None:
        await asyncio.gather(*(s.serve(sockets=k) for s, k in zip(servers, socks)))

    asyncio.run(run_all())
    return 0


# ---- download / models / info / delete -----------------------------------------------------------
def cmd_download(args: argparse.Namespace) -> int:
    _setup_logging("warning")
    _, models_dir = _dirs(args)
    repo = RepoId.parse(args.repo)
    store = ModelStore(models_dir)
    store.root.mkdir(parents=True, exist_ok=True)
    manager = DownloadManager(store)
    job = manager.start(repo)
    try:
        while job.state in ("queued", "resolving", "downloading", "verifying"):
            time.sleep(0.5)
            if job.total_bytes:
                frac = job.done_bytes / job.total_bytes
                bar = "#" * int(frac * 30)
                rate = f"{job.bytes_per_second / 1e6:6.1f} MB/s" if job.bytes_per_second else ""
                print(f"\r{repo} [{bar:<30}] {frac * 100:5.1f}%  {_gb(job.done_bytes)}/{_gb(job.total_bytes)} {rate}",
                      end="", flush=True)
    except KeyboardInterrupt:
        manager.cancel(job.id)
        while job.state not in ("cancelled", "failed", "completed"):
            time.sleep(0.1)
        print("\ncancelled (partial files kept; run again to resume)")
        return 130
    print()
    if job.state != "completed":
        print(f"download {job.state}: {job.error}", file=sys.stderr)
        return 1
    print(f"saved to {store.path_for(repo)}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    _, models_dir = _dirs(args)
    store = ModelStore(models_dir)
    machine = collect_machine_info(store.root)
    rows = catalog_view(store, machine.memory_total_bytes)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{machine.chip} · {_gb(machine.memory_total_bytes)} memory\n")
    print(f"{'':2}{'MODEL':52} {'KIND':9} {'SIZE':>9}  {'FIT':9}")
    for r in rows:
        mark = "●" if r["downloaded"] else " "
        print(f"{mark:2}{r['repo']:52} {r['kind']:9} {_gb(r['weights_bytes']):>9}  {r['fit']:9}")
    print("\n● downloaded   fit = estimate for this Mac's GPU memory budget")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    _, models_dir = _dirs(args)
    print(json.dumps(collect_machine_info(models_dir).to_json(), indent=2))
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    _, models_dir = _dirs(args)
    if not ModelStore(models_dir).delete(RepoId.parse(args.repo)):
        print(f"{args.repo} is not downloaded", file=sys.stderr)
        return 1
    print(f"deleted {args.repo}")
    return 0


def _runtime(args: argparse.Namespace):
    from .runtime import InferenceRuntime
    from .settings import SettingsStore

    data_dir, models_dir = _dirs(args)
    store = ModelStore(models_dir)
    return InferenceRuntime(store, SettingsStore(data_dir / "settings.json").current), store


# ---- chat (offline REPL, no server) ----------------------------------------------------------------
def cmd_chat(args: argparse.Namespace) -> int:
    from .runtime import ModelUnavailable

    _setup_logging("warning")
    runtime, store = _runtime(args)
    repo = RepoId.parse(args.repo)
    if store.get(repo) is None:
        print(f"{repo} is not downloaded. Run: bridle download {repo}", file=sys.stderr)
        return 1
    print(f"loading {repo} ...", flush=True)
    try:
        lm = runtime.load(repo, RepoId.parse(args.draft) if args.draft else None, force=True)
    except ModelUnavailable as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"ready in {lm.load_seconds}s. /reset clears history, /exit quits.\n")
    history: list[ChatMessage] = []
    if args.system:
        history.append(ChatMessage("system", args.system))
    params = SamplingParams.parse({"max_tokens": args.max_tokens, "temperature": args.temperature})
    while True:
        try:
            line = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit"):
            return 0
        if line == "/reset":
            history = history[:1] if args.system else []
            print("(history cleared)")
            continue
        history.append(ChatMessage("user", line))
        prompt = runtime.chat_prompt(lm, tuple(history))
        cancel = threading.Event()
        reply: list[str] = []
        print("ai  › ", end="", flush=True)
        try:
            for ev in runtime.generate(lm, prompt, params, cancel):
                if ev.reasoning:
                    print(f"\033[2m{ev.reasoning}\033[0m", end="", flush=True)
                if ev.text:
                    reply.append(ev.text)
                    print(ev.text, end="", flush=True)
                if ev.finish_reason:
                    extra = f" · {ev.cached_tokens} cached" if ev.cached_tokens else ""
                    extra += f" · {ev.draft_accepted} drafted" if ev.draft_accepted else ""
                    print(f"\n      [{ev.completion_tokens} tokens · {ev.generation_tps} tok/s{extra}]\n")
        except KeyboardInterrupt:
            cancel.set()
            print("\n(stopped)\n")
        history.append(ChatMessage("assistant", "".join(reply)))


# ---- bench / embed / transcribe ---------------------------------------------------------------------
def cmd_bench(args: argparse.Namespace) -> int:
    from .jobs import BenchmarkStore, run_benchmark

    _setup_logging("warning")
    runtime, _ = _runtime(args)
    data_dir, models_dir = _dirs(args)
    draft = RepoId.parse(args.draft) if args.draft else None
    lm = runtime.load(RepoId.parse(args.repo), draft, force=True)
    chip = collect_machine_info(models_dir).chip
    print(f"benchmarking {lm.repo}{' + draft ' + str(draft) if draft else ''} "
          f"({args.prompt_tokens} prompt / {args.generation_tokens} generated tokens) …")
    result = run_benchmark(runtime, lm, chip, args.prompt_tokens, args.generation_tokens)
    BenchmarkStore(data_dir / "benchmarks.json").add(result)
    print(f"  prompt     {result.prompt_tps:8.1f} tok/s\n  generation {result.generation_tps:8.1f} tok/s\n"
          f"  first tok  {result.time_to_first_token_ms:8d} ms\n  peak mem   {result.peak_memory_gb:8.2f} GB\n"
          f"  load       {result.load_seconds:8.1f} s")
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    _setup_logging("warning")
    runtime, _ = _runtime(args)
    vectors, tokens = runtime.embed(RepoId.parse(args.repo), args.texts)
    for text, v in zip(args.texts, vectors):
        print(f"{len(v)} dims  [{', '.join(f'{x:.4f}' for x in v[:4])}, …]  {text[:60]}")
    print(f"({tokens} tokens)")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    from .audio import to_srt, to_vtt

    _setup_logging("warning")
    runtime, _ = _runtime(args)
    result = runtime.transcribe(RepoId.parse(args.repo), str(Path(args.audio).expanduser()),
                                language=args.language or None)
    print({"text": result["text"], "srt": to_srt(result), "vtt": to_vtt(result),
           "json": json.dumps(result, indent=2)}[args.format])
    return 0


# ---- keys / presets / requests / settings ---------------------------------------------------------
def cmd_keys(args: argparse.Namespace) -> int:
    from .keys import KeyStore

    data_dir, _ = _dirs(args)
    store = KeyStore(data_dir / "api_keys.json")
    if args.action == "create":
        if not args.name:
            print("usage: keys create NAME", file=sys.stderr)
            return 2
        key, secret = store.create(args.name, args.rpm, args.daily_tokens, args.allow_model_load)
        print(f"created '{key.name}' ({key.id}). Copy this key now; it is not shown again:\n\n  {secret}\n")
        print("Restart a running server to pick up keys created from the terminal.")
        return 0
    if args.action in ("revoke", "delete"):
        ok = store.revoke(args.name) if args.action == "revoke" else store.delete(args.name)
        print(f"{args.action}d {args.name}" if ok else f"no key with id {args.name}")
        return 0 if ok else 1
    for k in store.list():
        state = "revoked" if k.revoked else "active"
        limits = ", ".join(x for x in (f"{k.rpm_limit}/min" if k.rpm_limit else "",
                                       f"{k.daily_token_limit} tok/day" if k.daily_token_limit else "") if x)
        print(f"{k.id}  {k.name:24} {k.hint:14} {state:8} {k.requests_total:6} req {k.tokens_total:9} tok  {limits}")
    return 0


def cmd_presets(args: argparse.Namespace) -> int:
    from .presets import PresetStore, parse_preset

    data_dir, _ = _dirs(args)
    store = PresetStore(data_dir / "presets.json")
    if args.action == "set":
        body = {"name": args.name, "model": args.model, "system_prompt": args.system,
                "temperature": args.temperature, "top_p": args.top_p, "max_tokens": args.max_tokens}
        preset = parse_preset({k: v for k, v in body.items() if v not in (None, "")})
        store.put(preset)
        print(f"saved preset '{preset.name}'. Use it with model=\"preset:{preset.name}\"")
        return 0
    if args.action == "delete":
        ok = store.delete(args.name or "")
        print("deleted" if ok else "no such preset")
        return 0 if ok else 1
    for p in store.list():
        print(f"{p.name:20} model={p.model or '(loaded)'} temp={p.temperature} max={p.max_tokens} "
              f"system={p.system_prompt[:50]!r}")
    return 0


def cmd_requests(args: argparse.Namespace) -> int:
    from .history import RequestHistory

    data_dir, _ = _dirs(args)
    history = RequestHistory(data_dir / "requests.sqlite3")
    print(json.dumps(history.summary(), indent=None))
    for r in reversed(history.list(args.limit)):
        when = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S")
        print(f"{when} {r['status']} {r['endpoint']:26} {r['client']:14} {r['model'][-36:]:36} "
              f"{r['prompt_tokens']:>6}+{r['completion_tokens']:<6} {r['tps']:6.1f} tok/s {r['error'][:60]}")
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    from .settings import SettingsStore, parse_settings_patch

    data_dir, _ = _dirs(args)
    store = SettingsStore(data_dir / "settings.json")
    if args.assignments:
        patch = {}
        for a in args.assignments:
            key, _, raw = a.partition("=")
            patch[key] = json.loads(raw) if raw not in ("",) else raw
        _, restart = store.update(parse_settings_patch(patch))
        if restart:
            print(f"restart the server to apply: {', '.join(restart)}")
    print(json.dumps(store.current.to_json(), indent=2))
    return 0


def _pinned_extras(names: list[str]) -> list[str]:
    """Pin extras to requirements-extras-lock.txt when it is available."""
    lock = Path(__file__).resolve().parent.parent / "requirements-extras-lock.txt"
    if not lock.is_file():
        return names
    pins = {}
    for line in lock.read_text().splitlines():
        name, _, version = line.partition("==")
        if version:
            pins[name.strip().lower().replace("_", "-")] = version.strip()
    return [f"{n}=={pins[n.lower()]}" if n.lower() in pins else n for n in names]


def cmd_extras(args: argparse.Namespace) -> int:
    names = sum(EXTRAS.values(), []) if args.feature == "all" else EXTRAS[args.feature]
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", *_pinned_extras(names)]
    print("running:", " ".join(cmd))
    return subprocess.call(cmd)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bridle", description="Private local inference on Apple silicon.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--data-dir", default=str(support_dir()), help="settings, keys, history, presets")
    p.add_argument("--models-dir", default=None, help="where models are stored (default: DATA_DIR/models)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the OpenAI- and Ollama-compatible server")
    s.add_argument("--host", default="127.0.0.1", help="0.0.0.0 shares the API on your network")
    s.add_argument("--port", type=int, default=DEFAULT_PORT, help="0 picks a free port")
    s.add_argument("--api-key", default="", help="require this bearer key on /v1 (or set BRIDLE_API_KEY)")
    s.add_argument("--no-auto-load", action="store_true", help="don't load models named in requests")
    s.add_argument("--preload", default="", help="model to load at startup")
    s.add_argument("--preload-draft", default="", help="draft model for speculative decoding with --preload")
    s.add_argument("--aria-model-dir", default="", help="directory of Aria's bundled helper model")
    s.add_argument("--onyx-model-dir", default="", help="directory of the bundled Onyx Whisper speech model")
    s.add_argument("--log-level", default="info")
    s.set_defaults(func=cmd_serve)

    d = sub.add_parser("download", help="download a model from Hugging Face (resumable)")
    d.add_argument("repo")
    d.set_defaults(func=cmd_download)

    m = sub.add_parser("models", help="list the catalog and downloaded models")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_models)

    sub.add_parser("info", help="show this machine's hardware").set_defaults(func=cmd_info)

    x = sub.add_parser("delete", help="delete a downloaded model")
    x.add_argument("repo")
    x.set_defaults(func=cmd_delete)

    c = sub.add_parser("chat", help="chat with a downloaded model in the terminal")
    c.add_argument("repo")
    c.add_argument("--draft", default="", help="small model of the same family for speculative decoding")
    c.add_argument("--system", default="")
    c.add_argument("--max-tokens", type=int, default=1024)
    c.add_argument("--temperature", type=float, default=0.7)
    c.set_defaults(func=cmd_chat)

    b = sub.add_parser("bench", help="measure prompt and generation speed")
    b.add_argument("repo")
    b.add_argument("--draft", default="")
    b.add_argument("--prompt-tokens", type=int, default=512)
    b.add_argument("--generation-tokens", type=int, default=128)
    b.set_defaults(func=cmd_bench)

    e = sub.add_parser("embed", help="embed text with an embedding model")
    e.add_argument("repo")
    e.add_argument("texts", nargs="+")
    e.set_defaults(func=cmd_embed)

    t = sub.add_parser("transcribe", help="transcribe an audio file")
    t.add_argument("repo")
    t.add_argument("audio")
    t.add_argument("--language", default="")
    t.add_argument("--format", choices=["text", "srt", "vtt", "json"], default="text")
    t.set_defaults(func=cmd_transcribe)

    k = sub.add_parser("keys", help="manage API keys")
    k.add_argument("action", choices=["list", "create", "revoke", "delete"], nargs="?", default="list")
    k.add_argument("name", nargs="?", default="", help="key name (create) or key id (revoke/delete)")
    k.add_argument("--rpm", type=int, default=None, help="requests per minute limit")
    k.add_argument("--daily-tokens", type=int, default=None, help="tokens per day limit")
    k.add_argument("--allow-model-load", action="store_true", help="let this key load/swap models remotely")
    k.set_defaults(func=cmd_keys)

    pr = sub.add_parser("presets", help="manage generation presets")
    pr.add_argument("action", choices=["list", "set", "delete"], nargs="?", default="list")
    pr.add_argument("name", nargs="?", default="")
    pr.add_argument("--model", default="")
    pr.add_argument("--system", default="")
    pr.add_argument("--temperature", type=float, default=None)
    pr.add_argument("--top-p", type=float, default=None)
    pr.add_argument("--max-tokens", type=int, default=None)
    pr.set_defaults(func=cmd_presets)

    r = sub.add_parser("requests", help="show recent request history")
    r.add_argument("--limit", type=int, default=50)
    r.set_defaults(func=cmd_requests)

    st = sub.add_parser("settings", help="show or change settings: settings batching=false idle_unload_minutes=10")
    st.add_argument("assignments", nargs="*")
    st.set_defaults(func=cmd_settings)

    ex = sub.add_parser("extras", help="install optional features")
    ex.add_argument("feature", choices=[*EXTRAS, "all"])
    ex.set_defaults(func=cmd_extras)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
