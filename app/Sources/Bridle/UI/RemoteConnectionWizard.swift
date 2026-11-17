import SwiftUI

/// Add/test/trust SSH connections and manage their remote workspaces.
/// No password or key material is ever entered or stored: authentication is
/// the user's existing SSH agent or a key file referenced by path.
struct RemoteConnectionWizard: View {
    @Environment(AppModel.self) private var model

    @State private var connections: [RemoteConnectionInfo] = []
    @State private var workspaces: [RemoteWorkspaceInfo] = []
    @State private var selectedConnectionID = ""
    @State private var name = ""
    @State private var useAlias = true
    @State private var alias = ""
    @State private var hostname = ""
    @State private var username = ""
    @State private var port = "22"
    @State private var authKind = "agent"
    @State private var identityFile = ""
    @State private var pendingAttempt = ""
    @State private var pendingFingerprint = ""
    @State private var pendingDestination = ""
    @State private var pendingConnectionID = ""
    @State private var busy = false
    @State private var error = ""
    @State private var notice = ""

    private var client: RemoteClient { RemoteClient(model: model) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Remote Connections").font(.title3.bold())
                    Text("Connect to another computer over SSH to use its folders as task workspaces. "
                         + "This does not start a model there or pool memory.")
                        .foregroundStyle(.secondary)
                }
                if !error.isEmpty { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red).textSelection(.enabled) }
                if !notice.isEmpty { Text(notice).foregroundStyle(.secondary) }

                if connections.isEmpty {
                    Text("No connections yet. Add one below; you will review its host-key fingerprint before anything is trusted.")
                        .foregroundStyle(.secondary)
                }
                ForEach(connections) { conn in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(conn.name).font(.headline)
                                Text(conn.detail).font(.caption.monospaced()).foregroundStyle(.secondary)
                                Text(conn.summary).font(.caption).foregroundStyle(.secondary)
                                if conn.trusted {
                                    Text(conn.fingerprint).font(.caption2.monospaced())
                                        .foregroundStyle(.secondary).textSelection(.enabled)
                                }
                            }
                            Spacer()
                            Button("Test") { test(conn.id) }.disabled(busy)
                            Button("Browse Folders") { selectedConnectionID = conn.id }.disabled(busy || !conn.trusted)
                            Button("Disconnect") { act { try await client.disconnect(connectionID: conn.id) } }.disabled(busy)
                            Button("Forget", role: .destructive) {
                                act { try await client.forget(connectionID: conn.id); await refresh() }
                            }.disabled(busy)
                        }
                        if selectedConnectionID == conn.id && conn.trusted {
                            Divider()
                            RemoteWorkspacePicker(connectionID: conn.id) { Task { await refresh() } }
                        }
                    }
                    .padding(12)
                    .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
                }

                if !workspaces.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Saved workspaces").font(.headline)
                        ForEach(workspaces) { ws in
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(ws.displayName)
                                    Text(ws.root).font(.caption.monospaced()).foregroundStyle(.secondary)
                                    Text("\(ws.accessLabel) · as \(ws.user)").font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("Remove", role: .destructive) {
                                    act { try await client.deleteWorkspace(id: ws.id); await refresh() }
                                }.disabled(busy)
                            }
                        }
                    }
                }

                Divider()
                addForm
                if !pendingFingerprint.isEmpty { trustReview }
                Text("Bridle never copies or stores private keys, passwords, or passphrases. "
                     + "Encrypted keys must already be loaded in your SSH agent (ssh-add); "
                     + "if authentication fails, add the key there and test again.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task { await refresh() }
    }

    private var addForm: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Add Connection").font(.headline)
            TextField("Display name (e.g. Development Mac)", text: $name).textFieldStyle(.roundedBorder)
            Picker("Destination", selection: $useAlias) {
                Text("SSH host alias").tag(true)
                Text("Hostname").tag(false)
            }.pickerStyle(.segmented)
            if useAlias {
                TextField("Host alias from ~/.ssh/config (e.g. devbox)", text: $alias).textFieldStyle(.roundedBorder)
            } else {
                HStack {
                    TextField("Hostname", text: $hostname).textFieldStyle(.roundedBorder)
                    TextField("Username", text: $username).textFieldStyle(.roundedBorder)
                    TextField("Port", text: $port).textFieldStyle(.roundedBorder).frame(width: 70)
                }
            }
            Picker("Authentication", selection: $authKind) {
                Text("SSH agent").tag("agent")
                Text("Key file").tag("identity_file")
            }.pickerStyle(.segmented)
            if authKind == "identity_file" {
                TextField("Private key file path (referenced, never copied)", text: $identityFile)
                    .textFieldStyle(.roundedBorder)
            }
            Button("Add & Test Connection", systemImage: "network") { addAndTest() }
                .buttonStyle(.borderedProminent)
                .disabled(busy || name.trimmingCharacters(in: .whitespaces).isEmpty
                         || (useAlias ? alias.isEmpty : (hostname.isEmpty || username.isEmpty)))
        }
    }

    private var trustReview: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Verify this host before trusting it", systemImage: "lock.shield")
                .font(.headline)
            Text("Destination: \(pendingDestination)").font(.callout.monospaced())
            Text("SHA256 fingerprint:").font(.caption).foregroundStyle(.secondary)
            Text(pendingFingerprint).font(.body.monospaced()).textSelection(.enabled)
            Text("Compare this fingerprint with the server (for example, ask its owner or check its console). "
                 + "Trusting stores it for Bridle only; your SSH settings are not changed. A changed key will block the connection.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Trust This Host", systemImage: "checkmark.shield") { trust() }
                    .buttonStyle(.borderedProminent).disabled(busy)
                Button("Cancel") { pendingFingerprint = ""; pendingAttempt = "" }.disabled(busy)
            }
        }
        .padding(12)
        .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
    }

    // ---- actions ---------------------------------------------------------------
    private func refresh() async {
        connections = (try? await client.connections()) ?? []
        workspaces = (try? await client.workspaces()) ?? []
    }

    private func act(_ action: @escaping () async throws -> Void) {
        busy = true
        error = ""
        Task { @MainActor in
            defer { busy = false }
            do { try await action() } catch { self.error = error.localizedDescription }
        }
    }

    private func addAndTest() {
        var body: [String: Any] = ["name": name, "auth_kind": authKind]
        if useAlias {
            body["host_alias"] = alias
        } else {
            body["hostname"] = hostname
            body["username"] = username
            body["port"] = Int(port) ?? 22
        }
        if authKind == "identity_file" { body["identity_file"] = identityFile }
        act {
            let conn = try await client.createConnection(body)
            await refresh()
            test(conn.id)
        }
    }

    private func test(_ connectionID: String) {
        act {
            let result = try await client.test(connectionID: connectionID)
            if (result["status"] as? String) == "trust_required" {
                pendingAttempt = result["attempt_id"] as? String ?? ""
                pendingFingerprint = result["fingerprint"] as? String ?? ""
                pendingDestination = result["effective_destination"] as? String ?? ""
                pendingConnectionID = connectionID
                notice = "Review the fingerprint below before trusting this host."
            } else {
                pendingFingerprint = ""
                notice = "Connected."
                await refresh()
            }
        }
    }

    private func trust() {
        let connectionID = pendingConnectionID
        let attempt = pendingAttempt
        let fingerprint = pendingFingerprint
        act {
            _ = try await client.trust(connectionID: connectionID, attemptID: attempt, fingerprint: fingerprint)
            pendingFingerprint = ""
            pendingAttempt = ""
            notice = "Host trusted and connected. You can now browse its folders."
            selectedConnectionID = connectionID
            await refresh()
        }
    }
}
