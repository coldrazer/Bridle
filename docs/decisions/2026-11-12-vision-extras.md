# Optional feature runtimes (vision / speech / embeddings)

Status: **Draft** — decisions settled in interview on 2026-11-12; implementation
landed the same day. Becomes Final only on explicit owner acceptance.

## Settled decisions

1. **All extras at setup.** Fresh engine setup installs the full extras set
   (vision, speech, embeddings) from the lock file, so a model never fails to
   load for a missing runtime. Setup failure in the extras step is non-fatal:
   core setup completes and per-feature recovery remains (decision 3).
2. **Existing installs auto-install once.** On engine start, if any extra is
   missing and the one-time flag (`extrasAutoInstallV1`) is not set, Bridle
   installs the full set with progress, restarts, and records the attempt so it
   never loops. (Settled after the original wizard-only Install button; the
   button remains as manual recovery.)
3. **Recovery on every load surface.** When a load fails with
   "vision support isn't installed", the import wizard offers
   Install Vision Support, AppModel's status watcher triggers the same install
   for loads from any surface, and the engine retry of the pending model runs
   automatically once after restart — never in a loop.
4. **This model loads as vision chat.** The reported
   DeepSeek-V4-Pro-Qwen3.5-9B (vision) model loads through mlx-vlm as a vision
   assistant — text chat and image prompts — once vision support is present.
5. **Proof bar.** Verify extras, restart through the app path, load the model,
   run one text prompt and one image prompt, and leave the model loaded.

## Non-goals

- No change to model-kind detection or the memory guard.
- No bundling of mlx-vlm/mlx-embeddings Python wheels inside the app; runtimes
  remain pip-installed into the engine venv from the lock files.

## Risks

- First setup grows by several hundred MB and a few minutes.
- Architecture support is bounded by mlx-vlm's model coverage; folder checks
  still cannot guarantee every architecture loads.
