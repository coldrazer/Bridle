# Hardware guide — Bridle on Apple silicon

Bridle is built on [MLX](https://github.com/ml-explore/mlx), Apple's machine-learning framework for unified memory. This page collects the numbers that shape its design: what each M-series tier can serve, how fast, and how that compares with a dual-RTX-5090 workstation. Figures are community-measured/estimated MLX rates (4-bit unless noted); your exact numbers will vary by model build and context length — Bridle's own Benchmarks tab measures *your* Mac directly.

## The physics in one line

Single-stream generation is memory-bandwidth bound — every token reads the active weights once:

```
Generation speed (tok/s) ≈ effective bandwidth (GB/s) / active model weight (GB)
```

On Apple silicon that means ~920 GB/s effective (72–80% of the M5 Ultra's 1.2 TB/s theoretical peak) divided by the bytes actually read per token. Prompt processing (prefill) is compute-bound instead, where the M5's GPU-integrated Neural Accelerators deliver roughly **3.5–4× the prompt ingestion of an M3 Ultra**. MoE models are the cheat code: only their *active* parameters are read per token, which is why a 671B MoE can outrun a 70B dense model here.

## Mac Studio M5 Ultra vs dual RTX 5090

| Metric | Mac Studio M5 Ultra (96 GB) | Mac Studio M5 Ultra (256 GB) | Dual RTX 5090 workstation |
|---|---|---|---|
| Usable AI memory (VRAM) | ~75–80 GB (unified) | ~210–225 GB (unified) | 64 GB (2× 32 GB GDDR7) |
| Memory bandwidth | 1,228 GB/s (1.2 TB/s) | 1,228 GB/s (1.2 TB/s) | 3,584 GB/s (1,792 GB/s per GPU) |
| Peak AI compute (FP4/INT4) | High (80-core GPU w/ Neural Accel.) | High (80-core GPU w/ Neural Accel.) | ~6,700+ TOPS (3,352 × 2) |
| System cost (est.) | $5,499 (base 96 GB / 1 TB) | $10,799 (36/80-core, 256 GB, 1 TB) | ~$6,800–$8,500 (2× GPU + Threadripper/EPYC X870E + 1600 W PSU) |
| Peak / sustained wall draw | ~180–290 W (480 W PSU max) | ~200–310 W (480 W PSU max) | ~1,150–1,350 W (575 W TDP per GPU + CPU) |
| Idle power draw | ~10–15 W | ~12–18 W | ~75–120 W |
| Acoustics & footprint | Near-silent (7.7″ × 7.7″ × 3.7″, 8 lbs) | Near-silent (7.7″ × 7.7″ × 3.7″, 8 lbs) | Loud fan noise; full-tower chassis (~45–60 lbs); heavy heat output |

The trade-off in one sentence: **raw compute and bandwidth on models under 64 GB (dual 5090) versus massive unified-memory capacity and whisper-quiet efficiency for frontier models (M5 Ultra).**

## LLM inference by model (MLX, tokens/second)

Two RTX 5090s combine for 3.58 TB/s of GDDR7 — nearly 3× the M5 Ultra's bandwidth — plus far superior FP4/FP8 Tensor Core compute, so the dual-5090 rig dominates any model that fits inside 64 GB of VRAM. Once a model exceeds 64 GB, it falls off a cliff into PCIe CPU offloading, whereas the M5 Ultra keeps running at full memory speed.

| Model & quantization | VRAM needed | Dual RTX 5090 (vLLM / TensorRT-LLM) | Mac Studio M5 Ultra (MLX) |
|---|---|---|---|
| Llama-3.3 / Qwen 8B (4-bit) | ~5.5 GB | ~320+ tok/s (single GPU) | ~165 tok/s |
| Qwen3.8 27B Dense (4-bit) | ~16 GB | ~135–150 tok/s | ~45 tok/s |
| Qwen3.6 35B-A3B MoE (4-bit) | ~20 GB | ~240+ tok/s | ~109 tok/s |
| Llama-3.3 70B Dense (4-bit) | ~40 GB | ~62–72 tok/s (tensor-parallel) | ~23 tok/s |
| Qwen3.5 122B-A10B MoE (4-bit) | ~68 GB | Does not fit (~6–9 tok/s w/ CPU offload) | ~65 tok/s (fits in 96 GB+) |
| Qwen3.8-Flash Sparse (4-bit) | ~155 GB | Cannot run natively | ~54–73 tok/s (requires 256 GB+) |
| DeepSeek-V3 / R1 671B MoE (4-bit) | ~385 GB | Cannot run natively | ~28–34 tok/s (requires 512 GB) |

## What each M5 Ultra tier holds (prefill + generation)

| AI model & quantization | Est. RAM | Prompt reading (prefill) | Generation | Compatible tiers |
|---|---|---|---|---|
| Llama-3.3 / Qwen 8B (4-bit) | ~5.5 GB | ~4,200 tok/s | ~165 tok/s | 96 GB, 256 GB, 512 GB |
| Qwen3.8 27B Dense (4-bit) | ~16 GB | ~1,525 tok/s | ~43–48 tok/s | 96 GB, 256 GB, 512 GB |
| Qwen3.6 35B-A3B MoE (4-bit) | ~20 GB | ~6,000 tok/s | ~109 tok/s | 96 GB, 256 GB, 512 GB |
| Llama-3.3 70B Dense (4-bit) | ~40 GB | ~750 tok/s | ~22–24 tok/s | 96 GB, 256 GB, 512 GB |
| Llama-3.3 70B Dense (8-bit) | ~75 GB | ~680 tok/s | ~11–12 tok/s | 96 GB, 256 GB, 512 GB |
| Qwen3.5 122B-A10B MoE (4-bit) | ~68 GB | ~2,300 tok/s | ~65 tok/s | 96 GB, 256 GB, 512 GB |
| Mistral Large 123B Dense (4-bit) | ~70 GB | ~420 tok/s | ~12–13 tok/s | 96 GB, 256 GB, 512 GB |
| Qwen3.8-Flash Sparse (4-bit) | ~155 GB | ~2,730 tok/s | ~54–73 tok/s | 256 GB, 512 GB |
| DeepSeek-V3 / R1 671B MoE (3-bit) | ~245 GB | ~1,150 tok/s | ~38–45 tok/s | 512 GB only |
| DeepSeek-V3 / R1 671B MoE (4-bit) | ~385 GB | ~1,100 tok/s | ~28–34 tok/s | 512 GB only |
| Llama-3.1 405B Dense (4-bit) | ~230 GB | ~160 tok/s | ~4.0–4.3 tok/s | 256 GB (tight), 512 GB |
| GLM-5.2 Dense/Hybrid (4-bit) | ~418 GB | ~310 tok/s | ~26 tok/s | 512 GB only |

- **96 GB (30-core CPU / 64-core GPU):** macOS allows roughly 72–80 GB of wired GPU allocation by default (expandable slightly via `sysctl`). Hits the full 1.2 TB/s ceiling — 70B 4-bit at ~23 tok/s with room for a 64K+ context, or 122B MoE 4-bit at ~65 tok/s. With 64 GPU cores instead of 80, prompt reading is roughly 20% slower than the higher tiers; generation is nearly identical.
- **256 GB (36-core CPU / 80-core GPU):** the sweet spot for unquantized/8-bit medium models and massive sparse MoE architectures — a 155 GB sparse model in 4-bit alongside a 128K context at 54–73 tok/s, or 70B models in FP16 (~140 GB) at ~6.5 tok/s with zero quantization loss.
- **512 GB (36-core CPU / 80-core GPU):** built for frontier-scale local weights (~440 GB usable). A 405B dense (4-bit) reads ~230 GB of weights per token, limiting output to ~4.1 tok/s — but frontier MoEs like DeepSeek 671B (4-bit, ~37B active parameters per token) keep all 385 GB resident while reading only ~22 GB of active weights per token, yielding a responsive 28–34 tok/s.

**And with [Mesh Mode](../README.md#mesh-mode-thunderbolt-5-memory-pooling), tiers add up:** two 96 GB Studios pool to roughly a 256 GB-class server; the planner checks the split before anything loads.

## Prompt processing (time to first token)

- **Dual RTX 5090:** 3–5× faster prefill on large contexts (32K–64K token prompts process in ~1–2 s on a 70B 4-bit), plus far better batched throughput serving multiple concurrent users or parallel agents.
- **M5 Ultra:** a huge leap over the M3 Ultra thanks to its GPU-integrated Neural Accelerators — interactive single-user RAG and coding agents feel snappy — though still noticeably slower than Blackwell Tensor Cores on 32K+ context ingestion. This is exactly the gap Bridle's disk-persisted prompt cache closes for harnesses, whose multi-thousand-token system prompts repeat every turn.

## Power, heat & total cost of ownership

- **Circuit & cooling:** two RTX 5090s pull ~1,110 W sustained under full AI load, pushing total wall draw near 1,300 W. On a standard 15 A/120 V North American residential circuit (1,440 W continuous safe limit), that leaves almost zero headroom — a dedicated 20 A circuit, or power-limiting the GPUs to ~400 W each (which retains ~90% of inference speed while cutting heat substantially). The M5 Ultra peaks under 310 W and plugs into any standard outlet without warming up your office.
- **Electricity (24/7 idle):** a dual-5090 workstation idling at ~95 W burns ~830 kWh/year (~$125–$250/year depending on rates); the M5 Ultra at ~12 W uses ~105 kWh/year (~$15–$30/year) — material for an always-on inference server like Bridle.
- **Electricity (heavy 8 hr/day load):** the dual-5090 system consumes roughly 3.2 kWh/day more than the Mac Studio (~$175–$350/year extra, plus additional AC load in warm climates).
- **Software ecosystem:** dual 5090s get full native CUDA, PyTorch, vLLM, SGLang, TensorRT-LLM, ComfyUI, and fine-tuning/LoRA training Apple silicon cannot match — 4–8× faster for local image/video generation (Wan, Flux, Qwen-Image) and training. The Mac Studio relies on MLX and llama.cpp (Metal): zero-copy unified memory and effortless setup, with high-concurrency batch serving and custom CUDA kernels unavailable.

## Which one fits which workload?

- **Buy the dual RTX 5090 workstation if:** your daily models are 70B or smaller (4-bit) or 30B–35B (8-bit), you want 2.5–3× faster token generation and prompt ingestion, you serve concurrent agent threads, or you run heavy ComfyUI video/image generation and local fine-tuning.
- **Buy the Mac Studio M5 Ultra (256 GB or 512 GB) if:** you want to run 100B–671B+ frontier Mixture-of-Experts models locally at interactive speeds without server-rack noise, you prioritize a silent, compact desktop under 300 W, or your workflow lives natively inside macOS/Xcode/Swift. (At the 96 GB tier specifically, the dual-5090 rig is faster for almost every model that fits in 64 GB, making the 96 GB M5 Ultra compelling mainly if you specifically need 65–80 GB of VRAM or macOS exclusivity.)
- **Run Bridle either way you land on Mac:** a single M5 Ultra covers everything in the tables above; Mesh Mode lets several smaller Macs pool into the next tier up instead of replacing hardware.

## Visual generation (ComfyUI) — the honest exception

Diffusion is compute-bound, not bandwidth-bound, and NVIDIA wins it decisively:

| Workload & settings | RTX 5090 (PyTorch/TensorRT FP8/FP4) | Mac Studio M5 Ultra (PyTorch MPS) | Mac Studio M5 Ultra (native MLX / mflux) |
|---|---|---|---|
| Flux.1 Dev (1024×1024, 20 steps) | ~3.5–4.5 s | ~18–24 s | ~9–12 s |
| Flux.1 Schnell (1024×1024, 4 steps) | ~0.8–1.2 s | ~4.5–6.0 s | ~2.2–3.0 s |
| Flux.1 Dev + ControlNet + LoRA | ~5.5–7.0 s | ~28–35 s | Limited ControlNet support |
| Wan 2.1/2.2 5B (480p, 81 frames/5 s) | ~45–65 s | ~3.5–4.5 min | ~2.0–2.5 min |
| Wan 2.1/2.2 14B (480p, 81 frames/5 s) | ~2.0–2.5 min (FP8/GGUF Q8) | ~9–12 min | ~6–8 min |
| Wan 2.1/2.2 14B (720p, 81 frames/5 s) | ~4.5–6.5 min (block swapping / FP8) | ~18–24 min (no offloading needed) | ~12–15 min |

Blackwell Tensor Cores execute FP8/FP4 natively (roughly half the memory at up to 40% more throughput vs FP16 in ComfyUI), and every new custom node — SageAttention, FlashAttention-3, Triton kernels, TensorRT engines — launches with CUDA support on day one. The M5 Ultra's one structural win is capacity: a full FP16 Wan 14B pipeline (~28 GB weights + ~10 GB T5 encoder + frame buffers) overflows a 32 GB card but sits entirely in unified memory with zero block-swapping or OOM crashes — slower per frame, but it finishes jobs a 5090 has to offload its way through. Bottom line: if ComfyUI image/video generation is your primary workload, buy the 5090; the M5 Ultra is viable for occasional Flux or overnight Wan renders when you're already buying it for large LLMs.
