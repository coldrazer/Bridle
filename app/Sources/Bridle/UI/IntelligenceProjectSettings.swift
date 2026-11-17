import SwiftUI

struct IntelligenceProjectSettings: View {
    @Environment(AppModel.self) private var model
    let project: [String: Any]
    let onSaved: () -> Void
    @State private var name = ""
    @State private var repo = ""
    @State private var system = ""
    @State private var message = ""
    @State private var saving = false

    var body: some View {
        DisclosureGroup("Project Settings") {
            VStack(alignment: .leading, spacing: 10) {
                TextField("Project name", text: $name).textFieldStyle(.roundedBorder)
                TextField("Model repository (optional)", text: $repo).textFieldStyle(.roundedBorder)
                TextField("System instructions for this project's chat", text: $system, axis: .vertical)
                    .textFieldStyle(.roundedBorder).lineLimit(3...8)
                HStack {
                    Button("Save Settings") { save() }.disabled(saving || name.isEmpty)
                    if !repo.isEmpty {
                        Button(model.downloadedModels.contains(where: { $0.repo == repo }) ? "Load Saved Model" : "Download Saved Model") {
                            model.choose(repo)
                        }.disabled(saving || model.isGenerating)
                    }
                    if saving { ProgressView().controlSize(.small) }
                }
                Text("Opening a project restores its chat instructions and model selection. Load or download the model when you are ready.")
                    .font(.caption).foregroundStyle(.secondary)
                if !message.isEmpty { Text(message).font(.caption).textSelection(.enabled) }
            }.padding(.top, 8)
        }.onAppear {
            name = project["name"] as? String ?? ""
            repo = project["model"] as? String ?? ""
            system = project["system_prompt"] as? String ?? ""
        }
    }

    private func save() {
        let id = project["id"] as? String ?? ""
        saving = true; message = ""
        Task { @MainActor in
            defer { saving = false }
            do {
                _ = try await model.featureRequest("PATCH", "/control/intelligence/projects/" + id,
                    body: ["name": name, "model": repo, "system_prompt": system])
                if model.activeProjectID == id, !model.isGenerating {
                    model.systemPrompt = system
                    if !repo.isEmpty { model.selectedRepo = repo }
                }
                message = "Settings saved."
                onSaved()
            } catch { message = error.localizedDescription }
        }
    }
}
