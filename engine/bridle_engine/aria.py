"""Aria, Bridle's bundled helper.

Aria runs Qwen3-0.6B (4-bit) in her own runtime slot and helps with Bridle itself:
explaining screens, recommending a model, and proposing downloads/loads.
She never executes an action; a proposal is returned to the app and runs only
after the user approves it there. Grounding is a bundled guide plus a live
summary of this machine and engine, built fresh per request.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading

from fastapi import HTTPException, Request

from .aria_answers import canonical_answer, select_answers
from .catalog import CATALOG
from .domain import ChatMessage, ParseError, RepoId, SamplingParams

GUIDE = """\
What Bridle is: a private AI app for this Mac. Models run on this Mac; nothing is sent to the cloud.
Getting started: Bridle needs its engine set up, then an assistant (model) downloaded, then loaded. Downloads resume and roughly 140 MB to several GB depending on the model.
Choosing an assistant: Everyday (Qwen3 4B or Llama 3.2 3B), Writing (Qwen3 8B), Coding (Qwen3 Coder 30B on big Macs, otherwise Qwen3 8B), Photo (Gemma 3 4B vision or Qwen2.5-VL). A model must fit this Mac's memory; the catalog shows fit.
Main screens: Beginner home (chat and setup), Models (browse/download/load), Projects & Tools (Studio: documents, jobs, workflows, evaluations, remote connections), Inspector (machine, server, certificate), Settings (sharing, API keys, updates).
Network sharing: off by default. Turning it on serves the API on the local network over HTTPS with named keys, one per device. Other apps connect with the endpoint and a key.
Troubleshooting: if chat will not start, check the engine is running, a model is downloaded and loaded, and no download is still in progress. The memory warning means the model is too big; load anyway only if other apps are closed. If the engine crashed, Bridle restarts it automatically and shows a crash notice.
Privacy: chats and documents stay on this Mac. Request history stores text only if that setting is on.
"""

PERSONA = (
    "You are Aria, Bridle's friendly built-in guide. Help with Bridle itself: setup, choosing and "
    "loading assistants, finding screens, and fixing problems. Answer warmly, plainly, and briefly: "
    "one clear next step per reply, short sentences, no jargon without a plain-words explanation. "
    "Call models 'assistants' for beginners. Only talk about Bridle; if asked about anything else, "
    "say kindly that you only help with Bridle and suggest asking the main assistant. Never invent a "
    "button, setting, or model that is not in the facts below.\n\n"
    "You may propose ONE action by ending your reply with a block exactly like:\n"
    "```aria-action\n{\"kind\": \"download\", \"repo\": \"mlx-community/Qwen3-4B-4bit\"}\n```\n"
    "Kinds: download (repo), load (repo), open (screen: models, studio, settings, chat). "
    "Propose an action only when it is clearly the next step. The user must approve it; say what will happen.\n\n"
    f"Facts about Bridle:\n{GUIDE}"
)

_ACTION_BLOCK = re.compile(r"```aria-action\s*(\{.*?\})\s*```", re.DOTALL)
SCREENS = ("models", "studio", "settings", "chat")


def state_summary(status: dict, catalog_rows: list[dict]) -> str:
    machine = status.get("machine", {})
    server = status.get("server", {})
    model = status.get("model", {})
    loaded = (model.get("loaded") or {}).get("repo")
    active = [d for d in status.get("downloads", []) if d.get("state") in
              ("queued", "resolving", "downloading", "verifying")]
    downloaded = [r["repo"] for r in catalog_rows if r.get("downloaded")]
    known = [r["repo"] for r in catalog_rows]
    lines = [
        f"This Mac: {machine.get('chip', 'unknown')}, "
        f"{machine.get('memory_total_bytes', 0) // 1_000_000_000} GB memory.",
        f"Engine: {server.get('state', 'unknown')}.",
        f"Assistant loaded: {loaded or 'none'}.",
        f"Downloaded assistants: {', '.join(downloaded) if downloaded else 'none'}.",
        f"Downloading now: {', '.join(d['repo'] for d in active) if active else 'nothing'}.",
        f"Known assistants include: {', '.join(known[:12])}.",
    ]
    return "\n".join(lines)


def parse_action(text: str, known_repos: set[str], downloaded_repos: set[str]) -> tuple[str, dict | None]:
    """Strip and validate an aria-action block. Invalid proposals are dropped, never executed."""
    match = _ACTION_BLOCK.search(text)
    if match is None:
        return text.strip(), None
    clean = (text[:match.start()] + text[match.end():]).strip()
    try:
        raw = json.loads(match.group(1))
    except ValueError:
        return clean, None
    if not isinstance(raw, dict):
        return clean, None
    kind = raw.get("kind")
    if kind in ("download", "load"):
        try:
            repo = str(RepoId.parse(raw.get("repo")))
        except ParseError:
            return clean, None
        pool = downloaded_repos if kind == "load" else known_repos | downloaded_repos
        if repo not in pool:
            return clean, None
        label = f"{'Download' if kind == 'download' else 'Load'} {repo.split('/', 1)[-1]}"
        return clean, {"kind": kind, "repo": repo, "label": label}
    if kind == "open" and raw.get("screen") in SCREENS:
        screen = raw["screen"]
        return clean, {"kind": "open", "screen": screen,
                       "label": {"models": "Open Models", "studio": "Open Projects & Tools",
                                 "settings": "Open Settings", "chat": "Open Chat"}[screen]}
    return clean, None


def strip_thinking(text: str) -> str:
    """Drop Qwen3's <think>...</think> section (and unterminated ones) from a reply."""
    import re
    text = re.sub(r"<think>.*?(?:</think>|$)", "", text, flags=re.DOTALL)
    return text.removeprefix("</think>").strip()


def tidy_reply(text: str) -> str:
    """Strip thinking tags, then collapse consecutive repeated lines/sentences."""
    text = strip_thinking(text)
    lines, seen_tail = [], ""
    for line in text.splitlines():
        key = line.strip().lower()
        if key and key == seen_tail:
            continue
        lines.append(line)
        if key:
            seen_tail = key
    return "\n".join(lines).strip()


def validate_messages(body) -> list[ChatMessage]:
    if not isinstance(body, dict):
        raise ParseError("request body must be a JSON object")
    raw = body.get("messages")
    if not isinstance(raw, list) or not raw or len(raw) > 24:
        raise ParseError("messages must be an array of 1-24 turns", "messages")
    messages = []
    for item in raw:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant") \
                or not isinstance(item.get("content"), str) or not item["content"].strip() \
                or len(item["content"]) > 4000:
            raise ParseError("each message needs a user/assistant role and text up to 4000 characters",
                             "messages")
        messages.append(ChatMessage(item["role"], item["content"]))
    if messages[-1].role != "user":
        raise ParseError("the last message must be from the user", "messages")
    return messages


def register_aria_routes(app, engine):
    """Aria lives on the control plane only: she helps the local user."""
    from .runtime import ModelUnavailable

    rt = engine.runtime
    prefix = "/control/aria"

    async def body_of(request: Request) -> dict:
        from .api import body_json
        value = await body_json(request)
        if not isinstance(value, dict):
            raise ParseError("request body must be a JSON object")
        return value

    def status() -> dict:
        return rt.aria_status()

    @app.get(prefix + "/status")
    async def aria_status():
        return status()

    @app.post(prefix + "/transcribe")
    async def aria_transcribe(request: Request):
        """Aria's ears: a downloaded speech assistant wins; bundled Onyx is the fallback."""
        import tempfile
        from pathlib import Path as _Path

        from .catalog import catalog_view as _catalog_view
        from .domain import RepoId as _RepoId

        declared = request.headers.get("content-length")
        if declared is not None and str(declared).isdigit() and int(declared) > 26 * 1024 * 1024:
            raise ParseError("recording is larger than 25 MB", "file")
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise ParseError("multipart field 'file' is required", "file")
        rows = _catalog_view(engine.store, engine.machine.memory_total_bytes)
        downloaded = [r["repo"] for r in rows if r.get("downloaded") and r.get("kind") == "speech"]
        raw_suffix = _Path(getattr(upload, "filename", "") or "audio.wav").suffix
        suffix = raw_suffix if raw_suffix[1:].isalnum() and len(raw_suffix) <= 10 else ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            size = 0
            while chunk := await upload.read(1 << 20):
                size += len(chunk)
                if size > 25 * 1024 * 1024:
                    raise ParseError("recording is larger than 25 MB", "file")
                tmp.write(chunk)
            tmp.flush()
            try:
                if downloaded:
                    result = await asyncio.to_thread(rt.transcribe, _RepoId.parse(downloaded[0]),
                                                     tmp.name)
                    used, source = downloaded[0], "downloaded"
                elif rt.onyx_available:
                    result = await asyncio.to_thread(rt.transcribe_bundled, tmp.name)
                    used, source = rt.onyx_status()["repo"], "bundled"
                else:
                    raise HTTPException(404, "No speech assistant is available: download one in "
                                             "Models → Speech, or use a build with bundled speech.")
            except ModelUnavailable as exc:
                raise HTTPException(exc.status, str(exc)) from exc
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 - speech extras may be absent
                if type(exc).__name__ == "FeatureUnavailable" or "isn't installed" in str(exc):
                    raise HTTPException(501, "Speech support isn't installed in the engine yet. "
                                             "Re-run Set Up Bridle (or install speech support from "
                                             "Models → Speech), then try Talk again.") from exc
                raise HTTPException(getattr(exc, "status", 500), str(exc)) from exc
        return {"text": result["text"], "model": used, "source": source}

    @app.post(prefix + "/repair")
    async def aria_repair():
        try:
            handle = await asyncio.to_thread(rt.repair_aria)
        except ModelUnavailable as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return {"available": True, "loaded": True, "repo": handle.repo}

    @app.post(prefix + "/chat")
    async def aria_chat(request: Request):
        messages = validate_messages(await body_of(request))
        sheet_entry = canonical_answer(messages[-1].content)
        if sheet_entry is not None:
            return {"reply": sheet_entry["answer"], "action": None,
                    "repo": rt.aria_status()["repo"], "source": "answer_sheet",
                    "sheet_entry": sheet_entry["id"]}
        try:
            handle = await asyncio.to_thread(rt.ensure_aria)
        except ModelUnavailable as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        from .catalog import catalog_view
        machine_status = {
            "machine": engine.machine.to_json(),
            "server": {"state": "running"},
            "model": rt.status(),
            "downloads": [j.to_json(engine.store) for j in engine.downloads.list()],
        }
        rows = catalog_view(engine.store, engine.machine.memory_total_bytes)
        known = {r["repo"] for r in rows} | {e.repo for e in CATALOG}
        downloaded = {r["repo"] for r in rows if r.get("downloaded")}
        matched = select_answers(messages[-1].content)
        sheet = ""
        if matched:
            sheet = ("\n\nAnswer-sheet matches (base your answer on these; keep their button names "
                     "and facts exactly):\n" + "\n".join(
                         f"- Q: {entry['questions'][0]}\n  A: {entry['answer']}" for entry in matched))
        system = PERSONA + "\n\nLive facts:\n" + state_summary(machine_status, rows) + sheet
        history = (ChatMessage("system", system),) + tuple(messages[-12:])
        prompt = rt.chat_prompt(handle, history)
        cancel = threading.Event()

        def collect() -> str:
            text = ""
            for event in rt.generate(handle, prompt,
                                     SamplingParams.parse({"max_tokens": 768, "temperature": 0.2, "repetition_penalty": 1.1}),
                                     cancel):
                text += event.text
            return text

        raw_text = await asyncio.to_thread(collect)
        reply, action = parse_action(raw_text, known, downloaded)
        reply = tidy_reply(reply)
        if not reply:
            reply = "Here is what I suggest:"
        return {"reply": reply, "action": action, "repo": handle.repo, "source": "model"}
