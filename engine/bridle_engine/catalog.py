"""Curated model catalog, on-disk model store, and memory-fit estimates."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .domain import ParseError, RepoId

GB = 1_000_000_000
MANIFEST = ".bridle.json"
PARTIAL_SUFFIX = ".partial"


@dataclass(frozen=True)
class CatalogEntry:
    repo: str
    name: str
    family: str
    params: str
    quant: str
    weights_bytes: int  # safetensors total, measured from the Hub
    summary: str
    kind: str = "text"  # text | vision | embedding | speech


# Repo ids and sizes verified against huggingface.co (Nov 2026).
CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry("mlx-community/SmolLM2-135M-Instruct-8bit", "SmolLM2 135M Instruct", "SmolLM2", "135M", "8-bit", 143_030_401, "Very small instruct model. (Bridle's built-in Aria guide is bundled separately and needs no download.)"),
    CatalogEntry("mlx-community/Qwen2.5-0.5B-Instruct-4bit", "Qwen2.5 0.5B Instruct", "Qwen2.5", "0.5B", "4-bit", 278_064_920, "Tiny and fast; good for smoke tests."),
    CatalogEntry("mlx-community/Qwen3-0.6B-4bit", "Qwen3 0.6B", "Qwen3", "0.6B", "4-bit", 335_000_000, "Tiny reasoning model; ideal draft for larger Qwen3."),
    CatalogEntry("mlx-community/Qwen3-1.7B-4bit", "Qwen3 1.7B", "Qwen3", "1.7B", "4-bit", 968_000_000, "Small reasoning model; draft for Qwen3 14B/32B."),
    CatalogEntry("mlx-community/Llama-3.2-1B-Instruct-4bit", "Llama 3.2 1B Instruct", "Llama", "1B", "4-bit", 700_000_000, "Small general chat model."),
    CatalogEntry("mlx-community/Llama-3.2-3B-Instruct-4bit", "Llama 3.2 3B Instruct", "Llama", "3B", "4-bit", 1_810_000_000, "Solid everyday assistant on any Mac."),
    CatalogEntry("mlx-community/Qwen3-4B-4bit", "Qwen3 4B", "Qwen3", "4B", "4-bit", 2_260_000_000, "Reasoning-capable small model."),
    CatalogEntry("mlx-community/Mistral-7B-Instruct-v0.3-4bit", "Mistral 7B Instruct v0.3", "Mistral", "7B", "4-bit", 4_080_000_000, "Reliable 7B instruct model."),
    CatalogEntry("mlx-community/Qwen3-8B-4bit", "Qwen3 8B", "Qwen3", "8B", "4-bit", 4_610_000_000, "Strong 8B generalist."),
    CatalogEntry("mlx-community/gemma-3-12b-it-4bit", "Gemma 3 12B Instruct", "Gemma", "12B", "4-bit", 8_030_000_000, "Google's 12B instruct model."),
    CatalogEntry("mlx-community/Qwen3-14B-4bit", "Qwen3 14B", "Qwen3", "14B", "4-bit", 8_310_000_000, "High quality for 24 GB+ Macs."),
    CatalogEntry("mlx-community/gpt-oss-20b-MXFP4-Q4", "gpt-oss 20B", "gpt-oss", "20B", "MXFP4", 11_180_000_000, "OpenAI open-weight MoE; needs 24 GB+."),
    CatalogEntry("mlx-community/Qwen3-30B-A3B-4bit", "Qwen3 30B A3B", "Qwen3", "30B MoE", "4-bit", 17_170_000_000, "Fast MoE; 3B active parameters."),
    CatalogEntry("mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit", "Qwen3 Coder 30B A3B", "Qwen3", "30B MoE", "4-bit", 17_180_000_000, "Coding-tuned MoE."),
    CatalogEntry("mlx-community/Qwen3-32B-4bit", "Qwen3 32B", "Qwen3", "32B", "4-bit", 18_430_000_000, "Dense 32B; 36 GB+ recommended."),
    CatalogEntry("mlx-community/Llama-3.3-70B-Instruct-4bit", "Llama 3.3 70B Instruct", "Llama", "70B", "4-bit", 39_690_000_000, "Needs 64 GB+ (or a cluster)."),
    CatalogEntry("mlx-community/gpt-oss-120b-MXFP4-Q4", "gpt-oss 120B", "gpt-oss", "120B", "MXFP4", 62_330_000_000, "Needs 96 GB+ (or a cluster)."),
    # Vision (requires the vision extra: mlx-vlm)
    CatalogEntry("mlx-community/SmolVLM-256M-Instruct-bf16", "SmolVLM 256M", "SmolVLM", "256M", "bf16", 513_000_000, "Tiny image-understanding model.", "vision"),
    CatalogEntry("mlx-community/gemma-3-4b-it-4bit", "Gemma 3 4B (vision)", "Gemma", "4B", "4-bit", 3_401_000_000, "Chat about images and text.", "vision"),
    CatalogEntry("mlx-community/Qwen2.5-VL-3B-Instruct-4bit", "Qwen2.5-VL 3B", "Qwen2.5-VL", "3B", "4-bit", 3_074_000_000, "Strong at documents, charts and screenshots.", "vision"),
    CatalogEntry("mlx-community/Qwen2.5-VL-7B-Instruct-4bit", "Qwen2.5-VL 7B", "Qwen2.5-VL", "7B", "4-bit", 5_638_000_000, "Best local vision quality under 16 GB.", "vision"),
    # Embeddings (requires the embeddings extra: mlx-embeddings)
    CatalogEntry("mlx-community/all-MiniLM-L6-v2-bf16", "MiniLM L6 v2", "Sentence-Transformers", "22M", "bf16", 45_000_000, "Fast 384-dim embeddings for search.", "embedding"),
    CatalogEntry("mlx-community/bge-small-en-v1.5-bf16", "BGE small en v1.5", "BGE", "33M", "bf16", 67_000_000, "Accurate small English embeddings.", "embedding"),
    CatalogEntry("mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ", "Qwen3 Embedding 0.6B", "Qwen3", "0.6B", "4-bit", 335_000_000, "Multilingual, high-quality embeddings.", "embedding"),
    # Speech to text (requires the speech extra: mlx-audio)
    CatalogEntry("mlx-community/whisper-tiny-asr-fp16", "Whisper tiny", "Whisper", "39M", "fp16", 74_000_000, "Fastest transcription; rough accuracy.", "speech"),
    CatalogEntry("mlx-community/whisper-small-asr-fp16", "Whisper small", "Whisper", "244M", "fp16", 481_000_000, "Good balance of speed and accuracy.", "speech"),
    CatalogEntry("mlx-community/whisper-large-v3-turbo-asr-fp16", "Whisper large v3 turbo", "Whisper", "809M", "fp16", 1_614_000_000, "Best transcription accuracy.", "speech"),
    CatalogEntry("wabibito/Onyx-Whisper-Tiny", "Onyx Whisper Tiny (bundled)", "Whisper", "39M", "mixed 4-bit", 65_813_293, "Bundled with the app for Aria's voice input; quick dictation.", "speech"),
)


def gpu_memory_budget(total_memory: int) -> int:
    """macOS lets the GPU wire roughly 2/3 of RAM on smaller Macs and 3/4 on larger ones."""
    return int(total_memory * (0.75 if total_memory > 36 * GB else 0.67))


def estimate_required_bytes(weights_bytes: int) -> int:
    # Weights + ~10% runtime overhead + ~1.5 GB for KV cache at moderate context lengths.
    return int(weights_bytes * 1.10 + 1.5 * GB)


def fit_status(weights_bytes: int, total_memory: int) -> str:
    need, budget = estimate_required_bytes(weights_bytes), gpu_memory_budget(total_memory)
    if need <= budget * 0.85:
        return "fits"
    if need <= budget:
        return "tight"
    return "too_large"


@dataclass(frozen=True)
class LocalModel:
    repo: str
    path: str
    size_bytes: int
    revision: str
    completed_at: str
    model_type: str
    quant_bits: int | None
    kind: str = "text"

    def to_json(self) -> dict:
        return asdict(self)


class ModelStore:
    """Downloaded models live at <models_dir>/<owner>/<name>/ with a manifest written last,
    so a directory without a manifest is never treated as usable."""

    def __init__(self, root: Path):
        self.root = root

    def path_for(self, repo: RepoId) -> Path:
        return self.root / repo.owner / repo.name

    def partial_path_for(self, repo: RepoId) -> Path:
        return self.root / ".partials" / repo.owner / repo.name

    def get(self, repo: RepoId) -> LocalModel | None:
        from .imports import registry
        imported = registry(self).get(str(repo))
        path = Path(imported["path"]) if imported else self.path_for(repo)
        manifest = path / MANIFEST
        if not imported and not manifest.is_file():
            return None
        try:
            meta = imported if imported else json.loads(manifest.read_text())
            if not isinstance(meta, dict):
                return None
            int(meta.get("size_bytes", 0))
        except (OSError, ValueError, TypeError):
            return None
        model_type, bits = "unknown", None
        cfg = path / "config.json"
        if cfg.is_file():
            try:
                config = json.loads(cfg.read_text())
                if not isinstance(config, dict):
                    return None
                model_type = str(config.get("model_type", "unknown"))
                q = config.get("quantization") or config.get("quantization_config") or {}
                bits = q.get("bits") if isinstance(q, dict) and isinstance(q.get("bits"), int) else None
            except (OSError, ValueError, TypeError):
                pass
        from .runtime import model_kind

        return LocalModel(
            repo=str(repo),
            path=str(path),
            size_bytes=int(meta.get("size_bytes", 0)),
            revision=str(meta.get("revision", "")),
            completed_at=str(meta.get("completed_at", "")),
            model_type=model_type,
            quant_bits=bits,
            kind=model_kind(path),
        )

    def list(self) -> list[LocalModel]:
        found: list[LocalModel] = []
        if not self.root.is_dir():
            return found
        for owner in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for model_dir in sorted(p for p in owner.iterdir() if p.is_dir()):
                if owner.name.startswith("."):
                    continue
                try:
                    repo = RepoId.parse(f"{owner.name}/{model_dir.name}")
                except ParseError:
                    continue
                local = self.get(repo)
                if local:
                    found.append(local)
        from .imports import registry
        existing = {m.repo for m in found}
        for repo in registry(self):
            if repo not in existing:
                local = self.get(RepoId.parse(repo))
                if local and Path(local.path).is_dir(): found.append(local)
        return found

    def delete(self, repo: RepoId) -> bool:
        from .imports import registry, save_registry, _LOCK
        with _LOCK:
            imports = registry(self)
            if str(repo) in imports:
                del imports[str(repo)]
                save_registry(self, imports)
                return True
        removed = False
        for p in (self.path_for(repo), self.partial_path_for(repo)):
            if p.exists():
                shutil.rmtree(p)
                removed = True
        owner_dir = self.root / repo.owner
        if owner_dir.is_dir() and not any(owner_dir.iterdir()):
            owner_dir.rmdir()
        return removed


def catalog_view(store: ModelStore, total_memory: int) -> list[dict]:
    """Catalog entries joined with local state and a fit estimate for this machine."""
    local = {m.repo: m for m in store.list()}
    rows: list[dict] = []
    for e in CATALOG:
        row = asdict(e)
        row["fit"] = fit_status(e.weights_bytes, total_memory)
        row["required_bytes"] = estimate_required_bytes(e.weights_bytes)
        row["downloaded"] = e.repo in local
        rows.append(row)
    # Speculative-decoding pairs: smaller downloaded text models of the same family.
    for row in rows:
        row["draft_candidates"] = [] if row["kind"] != "text" else [
            r["repo"] for r in rows
            if r["kind"] == "text" and r["family"] == row["family"] and r["downloaded"]
            and r["weights_bytes"] * 4 <= row["weights_bytes"]]
    # Downloaded models that are not in the curated list still show up.
    for repo, m in local.items():
        if any(r["repo"] == repo for r in rows):
            continue
        rows.append({
            "repo": repo, "name": repo.split("/", 1)[1], "family": m.model_type, "params": "",
            "quant": f"{m.quant_bits}-bit" if m.quant_bits else "", "weights_bytes": m.size_bytes,
            "summary": "Downloaded from Hugging Face.", "fit": fit_status(m.size_bytes, total_memory),
            "required_bytes": estimate_required_bytes(m.size_bytes), "downloaded": True, "kind": m.kind,
            "draft_candidates": [],
        })
    return rows
