# Beginner + Advanced feature guide

Bridle now asks **Beginner** or **Advanced** at startup. Change mode returns to that choice without restarting. Both modes use the same local engine and models. Projects & Tools opens Studio from either mode.

## Beginner (10)

| Add-on | Entry point and behavior |
|---|---|
| Setup wizard | Beginner home: checks engine readiness, runs setup, explains downloads |
| Automatic recommendation | Hardware-aware safe-fit recommendation for Everyday, Writing, Coding, Photo |
| Assistant shop | Friendly task categories with optional technical details |
| One-button installation | Download/retry/cancel and guarded load when completed |
| Chat home | Existing streaming chat becomes beginner home after preparation |
| Starter prompts | Task-specific suggestions start a real conversation |
| Readiness indicator | Distinguishes setup, download, loading, ready and offline capability |
| Attachments | Add/drop text, Markdown or text PDFs into a project; images into compatible chat |
| Recovery | Actionable setup/restart/change-assistant controls and permission messages |
| Personal shortcut | Menu bar, Quick Assistant and Control–Option–Space |

## Advanced (20)

| Add-on | Entry point and behavior |
|---|---|
| Chat with files | Studio → Documents: local extraction, lexical retrieval, page/chunk passages and cited answer jobs |
| Project workspaces | Studio → Projects: saved model/instructions, native conversation archives, project-scoped sources/jobs/prompts |
| Global panel | Control–Option–Space, Assistant menu, plus macOS Services → Ask Bridle for selected text |
| Client setup | Connect Apps: creates dedicated key and copyable OpenAI/curl/Continue configuration; health test |
| Smart model selection | Recommendations: task/memory fit, optional measured local benchmark speed |
| Live queue | Queue: tracked queued/generating requests and cancellation |
| Performance cockpit | Performance: real runtime counters, recent speed chart, request history and benchmarks |
| Resource profiles | Power: Quiet/Balanced/Maximum settings; optional native Low Power/thermal Quiet policy |
| Model health | Health: static file/header checks or explicit load + tiny text generation with deadline |
| Background jobs | Jobs: durable queued prompts, results, cancel/retry; interrupted state after engine restart |
| Workflow builder | Ordered input/retrieve/prompt/validate/export steps with persisted execution |
| Permissioned tools | Tools: folder grants, bounded text reads, model proposals, exact human approval, preview-token writes |
| MCP | HTTPS/localhost Streamable HTTP connections, initialize/list/call, enable/disable/remove |
| Voice | Quick Assistant: push-to-talk local transcription through downloaded speech model, system spoken replies, Stop |
| Screenshot assistant | Quick Assistant: explicit region capture and attachment; compatible vision model required |
| Personal evaluations | Evaluations: saved prompt/expected cases, real answers, transparent substring scoring |
| Prompt versions | Prompts: editable templates with variable substitution and retained revisions |
| Device pairing | Network: short-lived host code, explicit host confirmation, one-time device key, revocation |
| Multi-Mac routing | Network: explicitly approved HTTPS peer, trusted certificate, private-file credential, whole nonstream request routing |
| Machine canvas | Persistent draggable local/peer cards, reset, details, live local counts; drop loaded model label onto peer to prepare routing |

## Storage and permissions

- Project source text, prompt revisions, definitions and job results: `intelligence.sqlite3` in Application Support.
- Conversation archives: private `conversations/` files. Documents are only imported after file selection/drop.
- Tool grants and peers: private operations settings. Peer API credentials are stored in a private 0600 file in Bridle's data folder on every platform (no Keychain). Peers registered while credentials were kept in the macOS Keychain must be registered again.
- Writes need an explicit preview and single-use approval token. Model proposals never execute themselves.
- MCP and remote peer requests happen only after explicit user action. Remote inference requires HTTPS and a valid key, including after revoking the last key.
- Microphone and screen capture permissions are requested only when using those features. Cancelled audio is removed.

## MVP limits and verification boundaries

- Retrieval is lexical, not an embedding/vector index. PDFs need readable text; scanned documents need OCR first. Import limits are shown in errors.
- Evaluation scoring checks expected text; it is not an independent correctness judge.
- Voice records a bounded utterance then transcribes after Send; it is not a simultaneous full-duplex audio stream. Speech model and optional speech dependencies must be installed. Spoken output uses macOS voices.
- Model health generation currently tests text models only. Loading cannot be interrupted; generation has a cancellation deadline.
- Network routing runs one whole request on one approved machine. It does not pool memory or shard a model. Pairing requires manual private transfer of the issued key/certificate information.
- The app does not infer trustworthy peer identity from Bonjour. Advertised peer details remain explicitly unverified.
- Power profiles change software scheduling/idle unload, not fan or hardware clock controls. The native app reads macOS Low Power and thermal state; watts are not fabricated.
- Client setup provides copyable configuration; it does not rewrite external applications.
- Full automated engine/native suites and local startup/navigation checks are run. Real-model, microphone, screen permission, external MCP and second-Mac routing need hardware/integration verification before release distribution.
