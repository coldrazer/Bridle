# SSH remote connections and task workspaces

Status: MVP milestones 1-5 implemented (connection wizard, host trust, folder browsing, task bindings, bounded reads/writes/transfers). Remote command execution (milestone 6, versioned helper) is not implemented and no UI exposes it. Inference remains local; connecting a workspace never starts a model on the remote device.

## Objective

Let a Bridle user connect to a remote device over SSH, browse its folders, and attach a chosen remote workspace to a task. Make the execution location and model location visible throughout the task.

A remote workspace is a directory on another device. It is distinct from Bridle's existing HTTPS model peer: connecting to a workspace does not automatically start inference there, pair a model server, or pool memory.

In this blueprint, a **task** is a Bridle job or workflow run. This does not imply control of tasks in the Codex desktop application.

## MVP experience

1. Open **Projects & Tools → Remote Connections → Add Connection**.
2. Choose an existing SSH host alias or enter a hostname, username, and port.
3. Select an authentication method: existing SSH agent or an existing private-key file. Never import or copy the private key into Bridle's database.
4. Click **Test Connection**. Show the resolved destination and the server's host-key fingerprint.
5. For an unknown host, require explicit fingerprint verification and trust confirmation before continuing. A changed host key blocks the connection.
6. Browse the remote home directory or enter an absolute remote folder path. Show readable/writable status and an optional Git repository summary.
7. Choose **Use This Workspace**. Store a remote workspace reference; do not download the directory.
8. In a task's workspace picker, choose **Local** or **Remote → connection → workspace**.
9. Review a summary showing device, user, folder, execution location, inference location, and access level before starting.
10. Stream task activity into the existing task view. Allow cancellation, connection diagnostics, and safe recovery after interruption.

Beginner mode uses plain labels such as **Another computer** and **Choose a folder**. Advanced mode exposes SSH aliases, identity selection, connection diagnostics, and workspace permissions.

## Primary screens

### Connection wizard

- Display name, SSH host alias or hostname, username, port; port defaults to 22.
- Existing SSH agent or key-file reference.
- Effective host/user/port review before connection.
- Connection progress, host fingerprint, authentication result, and actionable failure messages.
- Test, Save, Disconnect, and Forget Connection actions.
- No password, passphrase, private-key material, or agent socket contents in logs or exports.

For MVP, encrypted keys are unlocked through the user's existing agent or trusted system SSH interaction. If an authentication flow is unavailable, explain how to load the key into the agent. Do not hang on invisible password prompts. In-app password and keyboard-interactive authentication are a later milestone.

### Remote folder picker

- Breadcrumbs, parent navigation, folders, path entry, Refresh, and **Use This Workspace**.
- Hidden folders are off by default and can be shown explicitly.
- Read-only selection is the default. Request write access explicitly when a task needs it.
- Paginate large directories. Display permission failures without dropping the connection.
- Remote symlinks are labeled. Resolve and revalidate the chosen canonical root.
- Do not run repository hooks, build scripts, or shell startup customizations merely to inspect a folder.

### Task workspace picker

Example:

```text
Workspace
  Local: This Mac / Personal
  Remote: Development Mac / Users/alex/Projects/website

Runs on: Development Mac as alex
Model runs on: This Mac
Access: Read only
```

- Pick a workspace before starting. An active task cannot silently change hosts or roots.
- Each task stores an immutable connection identity and canonical workspace snapshot.
- A task may override the project's default workspace without changing other tasks.
- Previously used remote workspaces remain visible while offline, with an offline badge.
- Beginner tasks request only the access their feature needs.

## Supported scope

| Capability | MVP behavior |
|---|---|
| Remote hosts | macOS and Linux SSH servers |
| Authentication | Existing agent and key-file references |
| Host trust | Known-host verification; explicit first-use trust; changed-key blocking |
| Folder access | Browse, inspect, bounded file reads, explicitly approved writes |
| Remote commands | Optional advanced task capability; separate from folder access |
| File transfer | Explicit single-file download/upload with size limits and overwrite approval |
| Task history | Connection/workspace metadata, timestamps, sanitized status and results |
| Reconnection | Retry connection and reconcile status; never blindly replay mutations |
| Inference | Existing local or separately configured HTTPS model provider |
| Deferred | Windows-native shells, WSL discovery, SSH server provisioning, multi-hop UI, automatic folder sync, remote IDE, distributed model execution |

An existing trusted SSH configuration may already supply a jump host. A dedicated jump-host editor is outside the first milestone.

## Architecture

```text
SwiftUI connection wizard / remote folder picker / task composer
                          |
                  RemoteWorkspaceCoordinator
                          |
             authenticated loopback control API
                          |
               RemoteConnectionManager
                 /                   \
       SSH transport adapter      Workspace provider
                 |                   |
          remote SSH server -- SFTP or versioned helper
                                     |
                              selected directory
```

Keep transport separate from task logic. The task runner uses a workspace-provider interface for local and remote files, rather than embedding SSH calls throughout existing features.

### Proposed modules

| File | Responsibility |
|---|---|
| `engine/bridle_engine/remote/domain.py` | Validated connection, workspace, capability, task-binding types |
| `engine/bridle_engine/remote/store.py` | Versioned metadata persistence; no authentication secrets |
| `engine/bridle_engine/remote/ssh.py` | System OpenSSH process lifecycle, effective configuration review, trust and authentication errors |
| `engine/bridle_engine/remote/files.py` | Bounded SFTP operations and canonical-path enforcement |
| `engine/bridle_engine/remote/helper.py` | Optional versioned remote helper protocol and command task lifecycle |
| `engine/bridle_engine/remote/tasks.py` | Task bindings, remote job IDs, events, cancellation, reconnect reconciliation |
| `engine/bridle_engine/remote/routes.py` | Authenticated control routes |
| `app/Sources/Bridle/Domain/RemoteModels.swift` | Typed API models and connection state |
| `app/Sources/Bridle/Infrastructure/RemoteClient.swift` | Remote control API client |
| `app/Sources/Bridle/UI/RemoteConnectionWizard.swift` | Connection setup and host-trust review |
| `app/Sources/Bridle/UI/RemoteWorkspacePicker.swift` | Folder navigation and selection |
| `app/Sources/Bridle/UI/TaskWorkspacePicker.swift` | Per-task location and capability selection |

Integrate routes through `create_app` in `api.py`; integrate task bindings with `intelligence.py` jobs/workflows. Extend Studio navigation and project settings. Preserve existing local project IDs, jobs, and conversation archives.

### Transport decisions

- Prefer the user's system OpenSSH for authentication and known-host behavior; use a structured SFTP adapter for file operations.
- Spike the SFTP adapter before choosing its dependency: confirm large directories, filenames containing spaces/newlines, cancellation, and host-key checks. Do not parse human-oriented `ls` or `sftp` output as a file protocol.
- Launch local processes with executable/argument arrays. Reject control characters and option-like destinations; remote paths never become local command-line options.
- Never concatenate a user path or model output into a remote shell command. Command tasks use a fixed helper entry point and length-bounded structured input over stdin.
- Review effective SSH configuration. User-owned SSH configuration may intentionally execute local proxy commands; label it as trusted user configuration, not a safe document import. Repository files and model output cannot select SSH options or configuration files.
- Disable agent forwarding and unsolicited port forwarding. Do not weaken host-key verification or copy keys to a remote machine.
- Bound concurrent sessions and avoid multiplexing sockets shared with unrelated tasks. Use private, short-lived local socket paths if multiplexing is introduced.

## Data model

Persist identifiers and safe metadata, with schema versions:

```text
RemoteConnection
  id, name, host_alias?, hostname?, username?, port
  auth_kind: agent | identity_file
  identity_file_reference?
  effective_destination, approved_host_key_identity
  created_at, last_success_at

RemoteWorkspace
  id, connection_id, canonical_root, display_name
  verified_host_identity, verified_user_identity
  capabilities: read | write | command
  verified_at

TaskWorkspaceBinding
  task_id, location: local | remote
  connection_id?, workspace_id?, canonical_root?
  host_identity?, user_identity?, approved_capabilities
  inference_location, binding_version
```

Store passphrases only in an appropriate OS credential mechanism if a later authentication feature requires them. Store neither passwords nor raw private keys in SQLite, settings JSON, task records, crash reports, or connection URLs.

Connection references must be invalidated or require review when destination identity changes. A host alias being edited to point elsewhere does not automatically authorize the new host.

## Proposed API

All routes below are under the existing loopback-and-token-protected control boundary:

```text
GET    /control/remote/connections
POST   /control/remote/connections
POST   /control/remote/connections/{id}/test
POST   /control/remote/connections/{id}/trust
POST   /control/remote/connections/{id}/disconnect
DELETE /control/remote/connections/{id}

GET    /control/remote/connections/{id}/folders?path=...
POST   /control/remote/workspaces
GET    /control/remote/workspaces
DELETE /control/remote/workspaces/{id}

POST   /control/remote/workspaces/{id}/read
POST   /control/remote/workspaces/{id}/writes/preview
POST   /control/remote/workspaces/{id}/writes/apply
POST   /control/remote/workspaces/{id}/transfer/preview
POST   /control/remote/workspaces/{id}/transfer/apply

PATCH  /control/intelligence/projects/{id}       # default workspace reference
POST   /control/intelligence/jobs               # optional immutable workspace binding
GET    /control/intelligence/jobs/{id}          # location and remote execution status
DELETE /control/intelligence/jobs/{id}          # cancellation request
```

A trust response is bound to the exact pending connection attempt and observed fingerprint. A write approval is bound to host identity, workspace root, relative path, previous content digest, proposed bytes, task, and expiry. Changing any of these invalidates the approval.

Folder paths are sent as properly encoded query values or structured request fields. Never log request bodies containing file contents or authentication data by default.

## Remote execution contract

Folder browsing should work without installing a helper. If command execution needs a helper:

1. Detect remote OS, architecture and available runtime using fixed probes.
2. Show what will be installed, its version, digest, installation location, and permissions.
3. Require explicit installation approval; no `curl | sh`, package-manager mutation, or privilege escalation.
4. Launch a versioned helper over an authenticated SSH channel with bounded JSON messages and task IDs.
5. Store stdout/stderr events separately with sequence numbers and bounded retention.
6. Run each command job in its own process group. Cancellation requests target only that job.
7. Persist remote job state for reconnect reconciliation. Do not infer success from a disconnected channel.

**An SSH account is not a workspace sandbox.** File-provider operations enforce root confinement. Arbitrary command execution under that account can access anything the account can access; a working directory alone does not restrict it. Disclose this when granting command capability. Strong confinement requires a separately configured restricted account, container, or OS sandbox.

## Safety and reliability requirements

- Default to read-only; enable writes and commands separately and explicitly.
- Canonicalize paths and enforce root confinement on the remote host. Local path checks cannot establish remote filesystem safety.
- Reject traversal, symlink escapes, path swaps and stale approval digests. Use handle-based remote operations where available; otherwise reject operations whose confinement cannot be established.
- Folder selection grants no permission to upload its contents to an inference provider. Explicitly identify which selected files will be sent to the selected model location.
- No recursive folder upload, background sync, automatic repository execution, or automatic model installation.
- Redact secrets from output diagnostics. Keep content logging opt-in with retention controls.
- A disconnect cancels pending transport operations. Show remote command cancellation as unconfirmed until acknowledged or reconciled.
- Connection retries are bounded with backoff. Read-only operations may retry; writes and commands require idempotency or reconciliation before retry.
- Forgetting a connection removes its local references and invalidates future task use; it does not delete remote files or remove remote SSH authorization.
- Shut down owned SSH processes/sockets on app exit. Offer explicit detach only for helper-backed durable jobs with documented behavior.
- Host-key changes, auth failures, missing folders, permission denial and helper incompatibility each get a distinct actionable error.

## Initial limits

Proposed defaults, configurable after measurement:

- Connection handshake deadline: 15 seconds.
- File-operation deadline: 30 seconds; cancellable.
- Maximum active connections: 4.
- Maximum folder page: 200 entries.
- Maximum text read: 1 MiB; transfers require explicit size review.
- Maximum retained live output: 1 MiB per stream per task, with truncation markers.
- Approval expiry: 5 minutes; single use.
- No automatic retry of command execution or mutations.

## Implementation sequence

1. **Transport feasibility and domain:** verify OpenSSH/SFTP integration on macOS and Linux fixtures. Define typed errors, limits, and secret-free persistence.
2. **Connection wizard:** alias/manual entry, agent/key references, strict host trust, test/disconnect/forget. Test unknown and changed keys before UI polish.
3. **Workspace picker:** remote folder browsing, canonical-root verification, read-only selection, paginated results. Add a provider interface with equivalent local behavior.
4. **Task integration:** project default and per-task override, immutable bindings, visible execution/inference location, migration of old tasks to `local`.
5. **File operations:** bounded reads, preview-bound writes, explicit transfers, conflict handling, and content-disclosure review.
6. **Optional command tasks:** approved helper installation, capability checks, process-group cancellation, durable task IDs and reconnect reconciliation.
7. **Ship gate:** integration tests against disposable SSH servers, native wizard checks, security review, documentation, signed app build.

Each milestone must be independently usable. Do not expose a command button before its remote lifecycle and cancellation behavior are implemented.

## Acceptance tests

- A user can connect with a trusted agent identity and select a directory containing spaces or Unicode.
- First-use trust requires explicit approval; changed keys remain blocked.
- An unavailable agent or locked key produces an actionable error rather than an invisible prompt.
- Two tasks bound to different hosts cannot exchange roots, file handles, credentials, or results.
- Task UI always identifies the device, user, root and inference location.
- Existing local tasks continue without configuration changes.
- Traversal, symlink escapes, hostile filenames, option injection, stale digests and path swaps are rejected.
- A write preview cannot be replayed against another task, host, root, path or content version.
- Reconnection does not duplicate writes or command execution.
- Cancelling one job does not kill unrelated remote processes; disconnect cancellation uncertainty is visible.
- Restarting Bridle restores workspace metadata and marks unconfirmed remote work as needing reconciliation.
- Permission denial, missing folders, full disks, huge directories, non-UTF-8 data and slow links have bounded failure behavior.
- Logs, saved tasks, crash reports and exports contain no passwords, private keys or passphrases.
- Selecting a folder does not download it, execute repository code, start a model, or modify SSH settings.

## Completion definition

Ship when a user can add a verified SSH connection, choose a remote folder, bind it to a Bridle task, perform only approved workspace operations, and understand where their data and computation go. Report later capabilities separately; remote workspace access is not evidence of remote inference or multi-machine model execution.
