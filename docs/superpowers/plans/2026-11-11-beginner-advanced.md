# Beginner and Advanced Bridle Implementation Plan

Goal: Implement the 10 beginner and 20 advanced add-ons requested in this chat as functional MVP surfaces.
Architecture: Keep SwiftUI desktop and local Python engine; extend through focused route modules, native views, and persisted local stores. Beginner mode guides setup, recommendations, installation and chat. Advanced mode exposes explicit operational controls.
Tech stack: SwiftUI/AppKit, FastAPI, SQLite/JSON, existing MLX runtime.

Global constraints: macOS 14+, local processing by default, authenticated control routes, bounded inputs, no automatic privileged actions or credential disclosure. Existing features remain usable. Do not claim untested model/hardware behavior.

Review focus: malformed documents, interrupted jobs, revoked peers/tools, absent optional models, offline/failed setup, insufficient memory.

Tasks and acceptance:
- [x] Beginner experience: setup wizard, recommended assistant, friendly assistant shop, install-and-load, chat home, starters, readiness, attachments, recovery, personal shortcut. Build native UI on current AppModel and add durable preferences.
- [x] Local intelligence: persisted projects/chat, document retrieval with citations, versioned prompt library, evaluations, background jobs, typed workflows. Add API storage tests including malformed input, isolation, restart recovery/cancel.
- [x] Advanced services: bounded live queue/cancel, diagnostics/history, power profiles, health checks, client setup, safe tool execution/MCP, secure pairing/revoke, whole-request remote routing. Add auth and failure tests.
- [x] Desktop integration: beginner/advanced mode, workspaces/document surfaces, performance and operations dashboard, shortcut panel, screenshot and voice controls, interactive topology. Expose every backend feature through usable controls.
- [x] Verify full Python and Swift suites, release app build, feature coverage report with honest limitations.

Feature mapping (advanced): 1 documents, 2 projects, 3 quick panel, 4 client setup, 5 recommendations, 6 queue, 7 performance, 8 power profiles, 9 health checks, 10 jobs, 11 workflows, 12 permissioned tools, 13 MCP, 14 voice, 15 screenshots, 16 evaluation, 17 prompt versions, 18 pairing, 19 routing, 20 topology.

Implementation coverage and explicit MVP limits: [FEATURES-30.md](../../../FEATURES-30.md). Real-model/hardware verification remains an integration limitation, not an automated-test claim.
