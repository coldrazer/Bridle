import SwiftUI

/// Remote folder navigation for one trusted connection, ending in
/// "Use This Workspace" (read-only unless write access is explicitly requested).
struct RemoteWorkspacePicker: View {
    @Environment(AppModel.self) private var model
    let connectionID: String
    var onUse: () -> Void = {}

    @State private var path = ""
    @State private var parent: String?
    @State private var entries: [RemoteFolderEntry] = []
    @State private var currentPath = ""
    @State private var allowWrite = false
    @State private var showHidden = false
    @State private var busy = false
    @State private var error = ""
    @State private var notice = ""

    private var client: RemoteClient { RemoteClient(model: model) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                TextField("Remote folder path (absolute)", text: $path)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { browse(path.isEmpty ? nil : path) }
                Button("Browse") { browse(path.isEmpty ? nil : path) }.disabled(busy)
                Toggle("Hidden", isOn: $showHidden).toggleStyle(.checkbox)
                    .onChange(of: showHidden) { _, _ in browse(currentPath.isEmpty ? nil : currentPath) }
            }
            if !currentPath.isEmpty {
                HStack(spacing: 8) {
                    Text(currentPath).font(.callout.monospaced()).textSelection(.enabled)
                    if let parent {
                        Button("Parent") { browse(parent) }.disabled(busy)
                    }
                    Spacer()
                    Toggle("Write access", isOn: $allowWrite).toggleStyle(.checkbox)
                    Button("Use This Workspace", systemImage: "checkmark.circle") { useWorkspace() }
                        .buttonStyle(.borderedProminent).disabled(busy)
                }
            }
            if !error.isEmpty { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
            if !notice.isEmpty { Text(notice).foregroundStyle(.secondary) }
            if entries.isEmpty && !currentPath.isEmpty {
                Text("This folder is empty.").foregroundStyle(.secondary)
            }
            ForEach(entries) { entry in
                HStack {
                    Label(entry.name, systemImage: entry.icon)
                    if entry.symlink { Text("symlink").font(.caption).foregroundStyle(.secondary) }
                    Spacer()
                    if entry.directory {
                        Button("Open") { browse(join(currentPath, entry.name)) }.disabled(busy)
                    }
                }
            }
        }
        .task { browse(nil) }
    }

    private func join(_ base: String, _ name: String) -> String {
        base.hasSuffix("/") ? base + name : base + "/" + name
    }

    private func browse(_ target: String?) {
        busy = true
        error = ""
        Task { @MainActor in
            defer { busy = false }
            do {
                let result = try await client.browse(connectionID: connectionID, path: target,
                                                     showHidden: showHidden)
                currentPath = result.path
                path = result.path
                parent = result.parent
                entries = result.entries
            } catch { self.error = error.localizedDescription }
        }
    }

    private func useWorkspace() {
        busy = true
        error = ""
        Task { @MainActor in
            defer { busy = false }
            do {
                let ws = try await client.createWorkspace(
                    connectionID: connectionID, path: currentPath,
                    capabilities: allowWrite ? ["read", "write"] : ["read"])
                notice = "Workspace saved: \(ws.root) (\(ws.accessLabel.lowercased())). "
                    + "Nothing was downloaded, and no model was started."
                onUse()
            } catch { self.error = error.localizedDescription }
        }
    }
}
