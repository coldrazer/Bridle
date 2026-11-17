"""Vision-language models (Qwen2.5-VL, Gemma 3, SmolVLM, …) via the optional mlx-vlm package."""

from __future__ import annotations

import base64
import binascii
import http.client
import ipaddress
import logging
import socket
import ssl
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from . import __version__
from .runtime_errors import FeatureUnavailable

if TYPE_CHECKING:
    from .runtime import GenJob, InferenceRuntime, ModelHandle

log = logging.getLogger("bridle.vision")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
INSTALL_HINT = ("vision support isn't installed; in Bridle, open Models and choose a Photo (vision) "
                "assistant to install it, or run `scripts/bridle extras vision` (installs mlx-vlm)")


def _mlx_vlm():
    try:
        import mlx_vlm  # noqa: F401
        return mlx_vlm
    except ImportError as exc:
        raise FeatureUnavailable(INSTALL_HINT) from exc


def load_vision_model(path: Path) -> tuple[Any, Any, Any]:
    mlx_vlm = _mlx_vlm()
    model, processor = mlx_vlm.load(str(path))
    tokenizer = getattr(processor, "tokenizer", processor)
    return model, processor, tokenizer


def render_vision_prompt(lm: "ModelHandle", messages: list[dict], num_images: int) -> str:
    from mlx_vlm.prompt_utils import apply_chat_template

    plain = [{"role": m["role"], "content": m["content"]} for m in messages]
    return apply_chat_template(lm.processor, lm.config, plain, num_images=num_images)


def _resolve_public_ip(url: str) -> tuple[str, str, int, str]:
    """Validate a URL and return (scheme, hostname, port, pinned_ip).

    Every resolved address must be publicly routable; the caller connects to
    the pinned IP so DNS cannot change between validation and connection.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("image URL must be an http(s) URL")
    if parts.username or parts.password:
        raise ValueError("image URL must not contain credentials")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("image URL has an invalid port") from exc
    try:
        infos = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(f"image host {parts.hostname!r} could not be resolved") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError("image URL must resolve to a public address")
    return parts.scheme, parts.hostname, port, sorted(addresses)[0]


FETCH_DEADLINE_SECONDS = 60.0


def _fetch_http(url: str, index: int) -> bytes:
    # One deadline for the whole fetch (all redirects): the per-read socket
    # timeout alone would let a drip-feeding server hold the MLX thread forever.
    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    from . import egress
    egress.record(url, "vision image fetch")
    current = url
    for _ in range(6):
        scheme, hostname, port, ip = _resolve_public_ip(current)
        parts = urlsplit(current)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        display_host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        host_header = display_host if port == (443 if scheme == "https" else 80) else f"{display_host}:{port}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(f"image {index} download timed out after {FETCH_DEADLINE_SECONDS:.0f}s")
        try:
            raw = socket.create_connection((ip, port), timeout=min(30.0, remaining))
            sock = raw
            if scheme == "https":
                sock = ssl.create_default_context().wrap_socket(raw, server_hostname=hostname)
            with sock:
                request = (f"GET {target} HTTP/1.1\r\nHost: {host_header}\r\n"
                           f"User-Agent: bridle-engine/{__version__}\r\nAccept: */*\r\nConnection: close\r\n\r\n")
                sock.sendall(request.encode("utf-8", "surrogateescape"))
                resp = http.client.HTTPResponse(sock, method="GET")
                resp.begin()
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    resp.read()
                    if not location:
                        raise ValueError(f"image {index} redirect has no location")
                    current = urljoin(current, location)
                    continue
                if resp.status >= 300:
                    raise ValueError(f"image {index} download failed with HTTP {resp.status}")
                data = bytearray()
                while chunk := resp.read(1 << 20):
                    if time.monotonic() > deadline:
                        raise ValueError(f"image {index} download timed out after {FETCH_DEADLINE_SECONDS:.0f}s")
                    sock.settimeout(max(0.1, min(30.0, deadline - time.monotonic())))
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise ValueError(f"image {index} is larger than 20 MB")
                return bytes(data)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise ValueError(f"image {index} download failed: {type(exc).__name__}") from exc
    raise ValueError(f"image {index} redirected too many times")


def fetch_images(urls: tuple[str, ...], workdir: Path) -> list[str]:
    """Materialize image URLs (data: or http(s)) as local files for the processor."""
    paths = []
    for i, url in enumerate(urls):
        if url.startswith("data:"):
            header, _, payload = url.partition(",")
            if ";base64" not in header:
                raise ValueError("data URLs must be base64-encoded")
            try:
                data = base64.b64decode(payload, validate=True)
            except binascii.Error as exc:
                raise ValueError(f"image {i} is not valid base64") from exc
        else:
            data = _fetch_http(url, i)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"image {i} is larger than 20 MB")
        p = workdir / f"image_{i}"
        p.write_bytes(data)
        paths.append(str(p))
    return paths


def run_vision(rt: "InferenceRuntime", job: "GenJob") -> None:
    from .runtime import GenEvent, OutputPipeline

    if not rt.settings.allow_remote_images and any(not u.startswith("data:") for u in job.images):
        raise ValueError("remote image URLs are turned off in Settings (allow_remote_images)")
    mlx_vlm = _mlx_vlm()
    lm = job.lm
    pipeline = OutputPipeline(job, lm.tokenizer)
    last, finish = None, None
    with tempfile.TemporaryDirectory(prefix="bridle-img-") as tmp:
        images = fetch_images(job.images, Path(tmp))
        kw: dict[str, Any] = {"max_tokens": job.params.max_tokens, "temperature": job.params.temperature}
        if job.params.top_p < 1.0:
            kw["top_p"] = job.params.top_p
        for resp in mlx_vlm.stream_generate(lm.model, lm.processor, job.prompt, image=images or None, **kw):
            last = resp
            if job.cancel.is_set():
                finish = "cancelled"
                break
            for ev in pipeline.push(resp.text):
                job.emit(ev)
            if pipeline.stopped:
                finish = "stop"
                break
            if resp.finish_reason:
                finish = resp.finish_reason
    if finish != "cancelled":
        for ev in pipeline.finish():
            job.emit(ev)
    job.emit(rt._finalize(job, pipeline, finish or "stop", GenEvent(
        prompt_tokens=last.prompt_tokens if last else 0,
        completion_tokens=last.generation_tokens if last else 0,
        prompt_tps=round(last.prompt_tps, 1) if last else 0.0,
        generation_tps=round(last.generation_tps, 1) if last else 0.0,
        peak_memory_gb=round(last.peak_memory, 2) if last else 0.0)))
