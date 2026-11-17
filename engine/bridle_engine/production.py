"""Production add-ons (control plane): setup doctor, egress evidence, model
scorecards, model-store lifecycle, Prometheus metrics, support bundles and
backup/restore. All routes live under /control (loopback + control token);
metrics is also served at /metrics for loopback scrapers."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from fastapi import Request
from fastapi.responses import PlainTextResponse

from . import __version__, egress
from .domain import ChatMessage, ParseError, RepoId

log = logging.getLogger("bridle.production")
GB = 1_000_000_000


def _check(name: str, ok: bool, detail: str, fix: str = "") -> dict:
    return {"name": name, "ok": ok, "detail": detail, "fix": fix}


def run_doctor(engine) -> dict:
    checks: list[dict] = []
    checks.append(_check("python", sys.version_info >= (3, 11),
                         f"Python {platform.python_version()}",
                         "Install Python 3.11+ (brew install python@3.12)"))
    checks.append(_check("platform", platform.system() == "Darwin" and platform.machine() == "arm64",
                         f"{platform.system()} {platform.machine()}",
                         "Bridle's MLX engine needs an Apple silicon Mac"))
    try:
        import mlx.core as mx
        metal = bool(mx.metal.is_available())
        checks.append(_check("metal", metal, f"MLX {mx.__version__}, Metal {'available' if metal else 'unavailable (CPU fallback)'}",
                             "" if metal else "Run on Apple silicon for GPU inference"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("metal", False, f"MLX unavailable: {exc}", "Re-run Set Up Engine"))
    for module, label, fix in (("mlx_lm", "mlx-lm", "Re-run Set Up Engine"),
                               ("mlx_vlm", "vision support", "Install from Models → Photo (vision)"),
                               ("mlx_embeddings", "embeddings support", "Install from Models → Embeddings"),
                               ("mlx_audio", "speech support", "Install from Models → Speech")):
        import importlib.util
        present = importlib.util.find_spec(module) is not None
        checks.append(_check(f"extra:{module}", present, f"{label} {'installed' if present else 'not installed'}",
                             "" if present else fix))
    models_dir = engine.config.models_dir
    try:
        free = shutil.disk_usage(models_dir if models_dir.exists() else Path.home()).free
        checks.append(_check("disk", free > 5 * GB, f"{free / GB:.1f} GB free",
                             "Free disk space or prune models (Settings → Storage)"))
    except OSError as exc:
        checks.append(_check("disk", False, str(exc)))
    for name, path, label in (("data_dir", engine.config.data_dir, "data folder"),
                              ("models_dir", models_dir, "models folder")):
        writable = os.access(path, os.W_OK) if path.exists() else False
        checks.append(_check(name, writable, f"{label} {path}", f"Check permissions on {path}"))
    rt = engine.runtime
    checks.append(_check("model", rt.loaded is not None,
                         f"loaded: {rt.loaded.repo}" if rt.loaded else "no model loaded",
                         "Load a model from the model picker"))
    fast_raw = engine.settings.fast_model
    fast_ok = True
    if fast_raw:
        try:
            fast_ok = rt.fast_status()["loaded"] or engine.store.get(RepoId.parse(fast_raw)) is not None
        except Exception:  # noqa: BLE001 - a hand-edited bad value is exactly what doctor reports
            fast_ok = False
    checks.append(_check("fast_model", fast_ok,
                         f"fast model: {fast_raw or 'not configured'}",
                         "Download the fast model or clear Settings → Fast model"))
    checks.append(_check("aria", rt.aria_available, "Aria helper model present" if rt.aria_available
                         else "Aria not in this build", ""))
    checks.append(_check("tls", not engine.settings.tls or bool(engine.tls_fingerprint),
                         "HTTPS " + (engine.tls_fingerprint or "off"),
                         "Enable HTTPS for network clients"))
    for binary, label in (("claude", "Claude Code"), ("dshctl", "DeepSeek Harness"),
                          ("lemonade", "Lemonade"), ("codex", "Codex"), ("opencode", "OpenCode")):
        found = shutil.which(binary) or next(
            (str(p) for p in (Path.home() / ".local" / "bin" / binary,
                              Path("/opt/homebrew/bin") / binary,
                              Path("/usr/local/bin") / binary) if p.exists()), None)
        checks.append(_check(f"harness:{binary}", found is not None,
                             f"{label}: {found or 'not found'}",
                             f"Install {label} to use it as a harness"))
    return {"ok": all(c["ok"] for c in checks if c["name"] not in ("aria",) and not c["name"].startswith("harness:")),
            "version": __version__, "checks": checks}


# ---- scorecards ----------------------------------------------------------------------------
class ScorecardStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            rows = json.loads(self.path.read_text())
            return rows if isinstance(rows, list) else []
        except (OSError, ValueError):
            return []

    def add(self, result: dict) -> None:
        rows = [r for r in self.list() if r.get("repo") != result.get("repo")]
        rows.insert(0, result)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".scorecards-", dir=self.path.parent)
            tmp = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(rows[:100], indent=2))
                tmp.replace(self.path)
            finally:
                tmp.unlink(missing_ok=True)


def run_scorecard(engine) -> dict:
    """Four quick behavioural checks on the loaded text model."""
    from .domain import SamplingParams
    from .runtime import ModelUnavailable

    rt = engine.runtime
    lm = rt.loaded
    if lm is None or lm.kind != "text":
        raise ModelUnavailable("load a text model to score it", 409)
    def ask(prompt: str, max_tokens: int = 96, tools: tuple = ()) -> tuple[str, tuple]:
        rendered = rt.chat_prompt(lm, (ChatMessage("user", prompt),), tools)
        text, calls = "", ()
        # generate() sets its cancel event when done: a fresh one per task.
        for event in rt.generate(lm, rendered,
                                 SamplingParams.parse({"max_tokens": max_tokens, "temperature": 0}),
                                 threading.Event(), tools=tools):
            text += event.text
            if event.tool_calls:
                calls = event.tool_calls
        return text.strip(), calls

    tasks: list[dict] = []

    def record(name: str, ok: bool, detail: str) -> None:
        tasks.append({"name": name, "ok": bool(ok), "detail": detail[:240]})

    try:
        out, _ = ask("Reply with exactly the word: ready")
        record("instruction_following", out.lower().strip(" .!\"'") == "ready", f"replied {out[:60]!r}")
    except Exception as exc:  # noqa: BLE001
        record("instruction_following", False, f"failed: {exc}")
    try:
        out, _ = ask('Reply with only a JSON object: {"city": "Paris", "population": 2} - output that object with your best value.')
        try:
            parsed = json.loads(out[out.index("{"):out.rindex("}") + 1])
            record("json_output", isinstance(parsed, dict) and "city" in parsed, f"parsed keys {list(parsed)[:4]}")
        except Exception:  # noqa: BLE001
            record("json_output", False, f"unparseable: {out[:80]!r}")
    except Exception as exc:  # noqa: BLE001
        record("json_output", False, f"failed: {exc}")
    try:
        tools = ({"type": "function", "function": {
            "name": "get_weather", "description": "Get the weather for a city",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}}, "required": ["city"]}}},)
        _out, calls = ask("What is the weather in Paris? Use the get_weather tool.", tools=tools)
        ok = bool(calls) and calls[0].name == "get_weather" and "paris" in str(calls[0].arguments).lower()
        record("tool_calling", ok, f"calls: {[c.name for c in calls]}")
    except Exception as exc:  # noqa: BLE001
        record("tool_calling", False, f"failed: {exc}")
    try:
        out, _ = ask("What is 17 + 25? Reply with only the number.")
        record("basic_reasoning", "42" in out, f"replied {out[:60]!r}")
    except Exception as exc:  # noqa: BLE001
        record("basic_reasoning", False, f"failed: {exc}")
    passed = sum(1 for t in tasks if t["ok"])
    return {"repo": lm.repo, "at": time.time(), "passed": passed, "total": len(tasks),
            "harness_ready": passed == len(tasks), "tasks": tasks}


# ---- storage / lifecycle ---------------------------------------------------------------------
def storage_report(engine) -> dict:
    store, config, settings = engine.store, engine.config, engine.settings
    last_used: dict[str, float] = {}
    for row in engine.history.list(1000):
        if row.get("model"):
            last_used[row["model"]] = max(last_used.get(row["model"], 0.0), float(row.get("ts") or 0))
    from .imports import registry
    imported_repos = set(registry(store))
    rt = engine.runtime
    resident = set()
    if rt.loaded:
        resident.add(rt.loaded.repo)
        if rt.loaded.draft:
            resident.add(rt.loaded.draft.repo)
    if rt.fast_loaded:
        resident.add(rt.fast_loaded.repo)
    models = []
    for model in store.list():
        path = Path(model.path)
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else model.size_bytes
        imported = model.repo in imported_repos
        models.append({"repo": model.repo, "size_bytes": size, "revision": model.revision,
                       "completed_at": model.completed_at,
                       "last_used": last_used.get(model.repo),
                       "imported": imported,
                       "loaded": model.repo in resident})
    partials = config.models_dir / ".partials"
    partial_bytes = sum(f.stat().st_size for f in partials.rglob("*") if f.is_file()) if partials.is_dir() else 0
    managed = [m for m in models if not m["imported"]]
    total = sum(m["size_bytes"] for m in managed)  # quota governs Bridle-managed storage only
    quota = settings.storage_quota_gb * GB
    candidates = sorted((m for m in managed if not m["loaded"]),
                        key=lambda m: (m["last_used"] or 0.0))
    return {"models": models, "total_bytes": total, "partial_bytes": partial_bytes,
            "free_bytes": shutil.disk_usage(config.models_dir if config.models_dir.exists() else Path.home()).free,
            "quota_bytes": quota, "over_quota": bool(quota and total > quota),
            "prune_candidates": [m["repo"] for m in candidates]}


def reverify_model(engine, repo: RepoId) -> dict:
    local = engine.store.get(repo)
    if local is None:
        from .runtime import ModelUnavailable
        raise ModelUnavailable(f"{repo} is not downloaded")
    root = Path(local.path)
    manifest_path = root / ".bridle.json"
    bad: list[str] = []
    if not manifest_path.is_file():
        return {"repo": str(repo), "ok": True, "checked": 0, "bad": [],
                "note": "imported model has no Bridle manifest to verify against"}
    manifest = json.loads(manifest_path.read_text())
    checked = 0
    for name, digest in (manifest.get("file_hashes") or {}).items():
        target = root / name
        if not target.is_file():
            bad.append(f"missing {name}")
            continue
        hasher = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
        checked += 1
        if hasher.hexdigest() != digest:
            bad.append(f"hash mismatch {name}")
    return {"repo": str(repo), "ok": not bad, "checked": checked, "bad": bad}


def check_updates(engine) -> dict:
    from huggingface_hub import HfApi

    egress.record(engine.downloads.endpoint, "model update check")
    out = []
    for model in engine.store.list():
        if model.revision in ("", "local"):
            continue
        try:
            info = HfApi(endpoint=engine.downloads.endpoint,
                         token=engine.downloads.token or None).model_info(model.repo)
            latest = info.sha or ""
            out.append({"repo": model.repo, "current": model.revision, "latest": latest,
                        "update_available": bool(latest and latest != model.revision)})
        except Exception as exc:  # noqa: BLE001 - report per model, keep going
            out.append({"repo": model.repo, "current": model.revision, "latest": None,
                        "update_available": False, "error": type(exc).__name__})
    return {"models": out}


# ---- metrics + support bundle ------------------------------------------------------------------
def metrics_text(engine) -> str:
    summary = engine.history.summary()
    rt = engine.runtime
    lines = [
        "# HELP bridle_requests_total Requests in the last 24h window",
        "# TYPE bridle_requests_total counter",
        f"bridle_requests_total {summary['requests']}",
        f"bridle_request_errors_total {summary['errors']}",
        f"bridle_completion_tokens_total {summary['completion_tokens']}",
        f"bridle_prompt_tokens_total {summary['prompt_tokens']}",
        f"bridle_avg_generation_tps {summary['avg_tps']}",
        f"bridle_model_loaded {1 if rt.loaded else 0}",
        f"bridle_fast_model_loaded {1 if rt.fast_loaded else 0}",
        f"bridle_generation_active {rt.metrics.active}",
        f"bridle_tokens_generated_total {rt.metrics.tokens_generated}",
        f"bridle_cached_prompt_tokens_total {rt.metrics.cached_prompt_tokens}",
        f"bridle_uptime_seconds {int(time.time() - engine.started_at)}",
        f"bridle_egress_events {egress.LOG.count()}",
        f"bridle_prompt_cache_disk_bytes {rt._pcache.disk_usage()['bytes']}",
    ]
    if rt.loaded:
        lines.append(f'bridle_model_info{{repo="{rt.loaded.repo}"}} 1')
    return "\n".join(lines) + "\n"


def support_bundle(engine) -> dict:
    data_dir = engine.config.data_dir
    out_dir = data_dir / "support"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"bridle-support-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.zip"
    status = {"version": __version__, "platform": platform.platform(),
              "machine": engine.machine.to_json(), "model": engine.runtime.status(),
              "settings": engine.settings.to_json(), "doctor": run_doctor(engine),
              "egress": egress.LOG.list(100)}
    logs, _ = engine.logs.since(0)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("status.json", json.dumps(status, indent=2))
        bundle.writestr("engine.log", "\n".join(
            f"{line.get('level', '')} {line.get('logger', '')}: {line.get('message', '')}"
            if isinstance(line, dict) else str(line) for line in logs[-2000:]))
        faults = data_dir / "crash" / "faults.log"
        if faults.is_file():
            bundle.writestr("faults.log", faults.read_bytes()[-200_000:])
        for report in sorted((data_dir / "CrashReports").glob("*"))[-5:] if (data_dir / "CrashReports").is_dir() else []:
            if report.is_file() and report.stat().st_size < 1_000_000:
                bundle.write(report, f"CrashReports/{report.name}")
        bundle.writestr("README.txt",
                        "Bridle support bundle. Prompt/response text is included only if "
                        "'log content' was on. No API key plaintext is stored by Bridle.\n")
    return {"path": str(out), "bytes": out.stat().st_size}


# ---- backup / restore ----------------------------------------------------------------------------
_BACKUP_FILES = ("settings.json", "presets.json", "api_keys.json", "benchmarks.json",
                 "operations.json", "scorecards.json", "requests.sqlite3",
                 "intelligence.sqlite3")



def _derive_key(passphrase: str, salt: bytes) -> bytes:
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def export_backup(engine, passphrase: str = "") -> dict:
    data_dir = engine.config.data_dir
    raw = io.BytesIO()
    included: list[str] = []
    meta = {"version": __version__, "at": time.time(),
            "note": "Peer credentials (vault) are excluded: re-pair peers after restore."}
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("backup.json", json.dumps(meta, indent=2))
        for name in _BACKUP_FILES:
            path = data_dir / name
            if not path.is_file():
                continue
            if name.endswith(".sqlite3"):
                # Consistent snapshot of a live WAL database, not a torn file copy.
                import sqlite3
                snapshot = io.BytesIO()
                try:
                    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                    dest = sqlite3.connect(":memory:")
                    with dest:
                        source.backup(dest)
                    snapshot.write(dest.serialize())
                    dest.close()
                    source.close()
                    bundle.writestr(name, snapshot.getvalue())
                except Exception:  # noqa: BLE001 - fall back to a plain copy
                    bundle.write(path, name)
            else:
                bundle.write(path, name)
            included.append(name)
        conversations = data_dir / "conversations"
        if conversations.is_dir():
            for path in conversations.rglob("*"):
                if path.is_file():
                    bundle.write(path, str(path.relative_to(data_dir)))
                    included.append(str(path.relative_to(data_dir)))
        tls_dir = data_dir / "tls"
        if tls_dir.is_dir():
            for path in tls_dir.glob("*.pem"):
                if "key" not in path.name:  # never export the TLS private key
                    bundle.write(path, f"tls/{path.name}")
                    included.append(f"tls/{path.name}")
        imports = engine.config.models_dir / ".imports.json"
        if imports.is_file():
            bundle.write(imports, "imports.json")
            included.append("imports.json")
    payload = raw.getvalue()
    encrypted = bool(passphrase)
    if encrypted:
        from cryptography.fernet import Fernet

        salt = os.urandom(16)
        payload = salt + Fernet(_derive_key(passphrase, salt)).encrypt(payload)
    out_dir = data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"bridle-backup-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.bridlebackup"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    return {"path": str(out), "bytes": out.stat().st_size, "encrypted": encrypted, "files": included}


def import_backup(engine, path_raw: str, passphrase: str = "") -> dict:
    source = Path(path_raw).expanduser()
    if not source.is_file() or source.stat().st_size > 500 * 1024 * 1024:
        raise ParseError("choose an existing .bridlebackup file (max 500 MB)", "path")
    payload = source.read_bytes()
    if passphrase:
        from cryptography.fernet import Fernet, InvalidToken

        try:
            payload = Fernet(_derive_key(passphrase, payload[:16])).decrypt(payload[16:])
        except (InvalidToken, ValueError) as exc:
            raise ParseError("wrong passphrase or not an encrypted Bridle backup", "passphrase") from exc
    data_dir = engine.config.data_dir
    restored: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ParseError("not a Bridle backup (or it is encrypted and needs its passphrase)",
                         "path") from exc
    with archive as bundle:
        names = set(bundle.namelist())
        if "backup.json" not in names:
            raise ParseError("not a Bridle backup (backup.json missing)", "path")
        for name in sorted(names):
            if name == "backup.json":
                continue
            if name.startswith("/") or ".." in Path(name).parts:
                raise ParseError(f"backup contains an unsafe path: {name}", "path")
            target = (engine.config.models_dir / ".imports.json") if name == "imports.json" \
                else data_dir / name
            if "key" in Path(name).name and name.startswith("tls/"):
                continue  # never import a TLS private key
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".restore-", dir=target.parent)
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(bundle.read(name))
                tmp.chmod(0o600)
                tmp.replace(target)
            finally:
                tmp.unlink(missing_ok=True)
            if name.endswith(".sqlite3"):
                # Stale sidecars of the live DB must not shadow the restored one.
                for suffix in ("-wal", "-shm"):
                    Path(str(target) + suffix).unlink(missing_ok=True)
            restored.append(name)
    return {"restored": restored, "restart_required": True,
            "note": "restart the engine to load restored settings, keys and projects"}


# ---- routes --------------------------------------------------------------------------------------
def register_production_routes(app, engine) -> ScorecardStore:
    from .api import body_json

    scorecards = ScorecardStore(engine.config.data_dir / "scorecards.json")
    app.state.scorecards = scorecards

    async def body_dict(request: Request) -> dict:
        value = await body_json(request)
        if not isinstance(value, dict):
            raise ParseError("request body must be a JSON object")
        return value

    def string_field(body: dict, key: str, default: str = "") -> str:
        value = body.get(key, default)
        if not isinstance(value, str) or len(value) > 4096:
            raise ParseError(f"{key} must be a string (max 4096 chars)", key)
        return value

    @app.get("/control/doctor")
    async def doctor():
        return await asyncio.to_thread(run_doctor, engine)

    @app.get("/control/egress")
    async def egress_list(limit: int = 100):
        return {"events": egress.LOG.list(limit), "total": egress.LOG.count()}

    @app.delete("/control/egress")
    async def egress_clear():
        egress.LOG.clear()
        return {"ok": True}

    @app.get("/control/scorecards")
    async def scorecard_list():
        return {"scorecards": scorecards.list()}

    @app.post("/control/scorecards/run")
    async def scorecard_run():
        result = await asyncio.to_thread(run_scorecard, engine)
        await asyncio.to_thread(scorecards.add, result)
        return result

    @app.get("/control/storage")
    async def storage():
        return await asyncio.to_thread(storage_report, engine)

    @app.post("/control/storage/prune")
    async def storage_prune():
        report = await asyncio.to_thread(storage_report, engine)
        quota = report["quota_bytes"]
        if not quota:
            return {"pruned": [], "note": "no storage quota set (Settings → Storage quota)"}
        total, pruned = report["total_bytes"], []
        sizes = {m["repo"]: m["size_bytes"] for m in report["models"]}
        for repo_raw in report["prune_candidates"]:
            if total <= quota:
                break
            if await asyncio.to_thread(engine.store.delete, RepoId.parse(repo_raw)):
                pruned.append(repo_raw)
                total -= sizes.get(repo_raw, 0)
        return {"pruned": pruned, "total_bytes": total, "quota_bytes": quota}

    @app.post("/control/storage/reverify")
    async def storage_reverify(request: Request):
        repo = RepoId.parse(string_field(await body_dict(request), "repo"))
        return await asyncio.to_thread(reverify_model, engine, repo)

    @app.post("/control/storage/check-updates")
    async def storage_updates():
        return await asyncio.to_thread(check_updates, engine)

    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse(metrics_text(engine), media_type="text/plain; version=0.0.4")

    @app.post("/control/support-bundle")
    async def make_support_bundle():
        return await asyncio.to_thread(support_bundle, engine)

    @app.post("/control/backup/export")
    async def backup_export(request: Request):
        body = await body_dict(request)
        return await asyncio.to_thread(export_backup, engine, string_field(body, "passphrase"))

    @app.post("/control/backup/import")
    async def backup_import(request: Request):
        body = await body_dict(request)
        return await asyncio.to_thread(import_backup, engine,
                                        string_field(body, "path"), string_field(body, "passphrase"))

    return scorecards
