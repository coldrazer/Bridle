import SwiftUI

/// Per-task workspace choice: Local (this Mac) or a saved remote workspace.
/// The pick is stored as an immutable binding when the task is submitted.
struct TaskWorkspacePicker: View {
    @Environment(AppModel.self) private var model
    @Binding var selection: String   // "local" or a workspace id
    @State private var workspaces: [RemoteWorkspaceInfo] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("Workspace", selection: $selection) {
                Text("Local: This Mac").tag("local")
                ForEach(workspaces) { ws in Text(ws.label).tag(ws.id) }
            }
            Text(summary).font(.caption).foregroundStyle(.secondary)
        }
        .task {
            workspaces = (try? await RemoteClient(model: model).workspaces()) ?? []
            if selection != "local" && !workspaces.contains(where: { $0.id == selection }) {
                selection = "local"
            }
        }
    }

    private var summary: String {
        guard selection != "local", let ws = workspaces.first(where: { $0.id == selection }) else {
            return "Runs on: This Mac · Model runs on: This Mac · Access: Local files"
        }
        return "Runs on: \(ws.displayName) as \(ws.user) · Model runs on: This Mac · Access: \(ws.accessLabel)"
    }

    /// Engine body fragment for the current selection.
    var binding: [String: Any] {
        selection == "local" ? ["location": "local"]
            : ["location": "remote", "workspace_id": selection]
    }
}
