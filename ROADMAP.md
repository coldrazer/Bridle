# Roadmap

## Phase 1 — single Mac ✅
Engine, OpenAI-compatible API, model browser/downloads, sandbox chat, inspector, dev tools, offline console.

## Phase 1.5 — production add-ons ✅
Speculative decoding, prompt caching, continuous batching, memory guard, idle unload, tool calling,
guaranteed JSON, embeddings, vision, speech-to-text, Ollama API, named API keys, HTTPS + Bonjour,
request history, menu bar mode + launch at login, benchmarks, compare, presets, Sparkle updates +
notarized releases, crash recovery. See README for details.

## Phase 2 — Thunderbolt 5 cluster ("Mesh Mode") — core shipped
Goal: connect Macs with Thunderbolt 5 cables. Their unified memory is pooled so they can run models no single Mac can hold.

**Requirements:** macOS 26.2+, Thunderbolt 5 Macs (M4 Pro/Max and later Pro/Max chips, M3 Ultra and later Ultras), and RDMA enabled on each Mac from Recovery. The inspector already detects and shows the Thunderbolt generation and RDMA status.

1. **Discovery** ✅ (done in 1.5). Each engine advertises `_bridle._tcp` over Bonjour and lists peers at `/control/peers`; the canvas shows them. Remaining: detect which peers are reachable over the Thunderbolt Bridge interface and draw "TB5" edges for those links (screenshot 4).
2. **Pairing.** The user confirms a peer from the canvas, and the two engines exchange a shared cluster key. Unpaired peers are never trusted.
3. **Placement.** The coordinator gets total memory from every node and plans a split:
   * *Pipeline parallel*: contiguous layer ranges sized to each node's memory. This works over TCP too.
   * *Tensor parallel*: split within layers. This is only worth it with RDMA, because without it each hop adds roughly 300 µs.
4. **Execution.** Use `mlx.core.distributed`: the `ring` backend over TCP, or the `jaccl` (RDMA) backend when it's enabled. Each node loads only its own shard of the safetensors. The coordinator alone serves `/v1`.
5. **UI.** The topology section shows Configuration (Pipeline/Tensor), Network (RDMA/TCP) and Distribution (layers per node). Cluster Stats shows pooled memory. Switching models re-plans the split.
6. **Downloads.** Each node fetches only the shard files it needs, with the same resumable, verified downloader.

**Shipped in Mesh Mode (2026-11-16):** readiness probes (macOS, TB5, RDMA status, `ibv_devinfo` ports, Thunderbolt Bridge), cabling discovery and the JACCL device matrix (full mesh and `jaccl-ring`), planning with per-Mac memory checks and fixes, pairing with a mesh grant, per-Mac model download, tensor and pipeline splits via `mlx_lm.server` in distributed mode, a TCP-ring fallback, and serving the pooled model from the coordinator's `/v1`.

**Still open:** memory-proportional pipeline splits (mlx-lm splits layers evenly), shard-only downloads, Ollama `/api` routing to the mesh, a TB5 edge view on the canvas, and validation on real multi-Mac RDMA hardware.

## Later
* SemanticSpec-style semantic-aware speculative decoding on top of the draft-model path
* Prompt caches persisted to disk (mlx-lm `save_prompt_cache`) for long system prompts
* Batched vision and structured-output requests
* Per-key model allow-lists
* Text-to-speech (`/v1/audio/speech`) via mlx-audio
