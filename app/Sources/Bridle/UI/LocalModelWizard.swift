import AppKit
import SwiftUI

struct LocalModelWizard: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var folder = ""
    @State private var candidates: [[String: Any]] = []
    @State private var unsupported: [[String: Any]] = []
    @State private var warnings: [String] = []
    @State private var selectedPath = ""
    @State private var addAll = true
    @State private var busy = false
    @State private var scanned = false
    @State private var importedRepo = ""
    @State private var error = ""
    @State private var registeredOnly = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack { Text("Bring your own AI").font(.largeTitle.bold()); Spacer(); Button("Done") { dismiss() }.disabled(busy) }
            Text("Choose the folder where your models are already downloaded. Bridle checks it locally and keeps your original files in place.").foregroundStyle(.secondary)
            HStack { TextField("Your AI folder", text: $folder).textFieldStyle(.roundedBorder); Button("Choose Folder…") { chooseFolder() } }
            Button("Scan Folder") { scan() }.disabled(folder.isEmpty || busy || !model.isRunning)
            if !model.isRunning { Text("Prepare Bridle's engine first, then import your models.").foregroundStyle(.orange) }
            if busy { ProgressView("Checking local files…") }
            if !error.isEmpty { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            if !importedRepo.isEmpty, let issue = model.lastError {
                Text(issue).foregroundStyle(.red).textSelection(.enabled)
                if issue.contains("vision support isn't installed") {
                    if model.installingExtra != nil {
                        HStack { ProgressView().controlSize(.small); Text("Installing vision support, then restarting the engine…") }
                    } else {
                        Button("Install Vision Support") {
                            model.pendingExtraRetryRepo = importedRepo
                            model.installExtra("vision")
                        }
                        .buttonStyle(.borderedProminent)
                        Text("This is a one-time software download. Your model files stay where they are.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                Button("Retry loading selected model") { model.dismissError(); model.choose(importedRepo) }
            }
            if scanned {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("\(candidates.count) model candidates found").font(.headline)
                        Text("Bridle hosts MLX-compatible Safetensors models. Folder checks cannot guarantee every architecture will load. Only one chat model is resident at a time.").font(.caption).foregroundStyle(.secondary)
                        ForEach(candidates.indices, id: \.self) { i in
                            let item = candidates[i]
                            let path = item["path"] as? String ?? ""
                            Button { selectedPath = path } label: {
                                HStack {
                                    Image(systemName: selectedPath == path ? "largecircle.fill.circle" : "circle")
                                    VStack(alignment: .leading) {
                                        Text(item["name"] as? String ?? "Local model").fontWeight(.semibold)
                                        Text(path).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                                    }
                                    Spacer()
                                    Text(Format.bytes((item["size_bytes"] as? NSNumber)?.int64Value ?? 0)).font(.caption)
                                }.padding(10).frame(maxWidth: .infinity).background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 10))
                            }.buttonStyle(.plain).disabled(busy)
                        }
                        if candidates.isEmpty { Text("No supported model folder found. Choose a folder containing config.json, tokenizer files, and .safetensors weights—or its parent folder.") }
                        if !unsupported.isEmpty {
                            DisclosureGroup("\(unsupported.count) unsupported or incomplete items") {
                                ForEach(unsupported.indices, id: \.self) { i in
                                    VStack(alignment: .leading) {
                                        Text(unsupported[i]["path"] as? String ?? "").font(.caption.monospaced())
                                        Text(unsupported[i]["detail"] as? String ?? "").font(.caption).foregroundStyle(.secondary)
                                    }.padding(.vertical, 4)
                                }
                            }
                        }
                        ForEach(warnings, id: \.self) { Text($0).font(.caption).foregroundStyle(.orange) }
                    }
                }
                Toggle("Add all discovered candidates to my model library", isOn: $addAll).disabled(busy)
                Button("Add & Host Selected Model") { host() }.buttonStyle(.borderedProminent)
                    .disabled(selectedPath.isEmpty || busy || model.isGenerating || model.modelState == "loading")
            }
            if !importedRepo.isEmpty {
                Divider()
                if model.loadedRepo == importedRepo {
                    Label("Your model is hosted", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                    Text("Open chat or connect an app using the endpoint below.")
                    Button("Start Chatting") { model.bottomPanel = .chat; dismiss() }
                } else if registeredOnly {
                    Label("Available in your model library", systemImage: "checkmark.circle")
                    Text("Speech and embedding models load automatically when their API is used.")
                } else if model.modelState == "error" {
                    Text(model.status?.model.error ?? "This model could not be loaded. Try another compatible model.").foregroundStyle(.red)
                } else { ProgressView("Loading your selected model…") }
                Text(importedRepo).font(.caption.monospaced()).textSelection(.enabled)
                if let endpoint = model.endpoint { Text(endpoint + "/v1").font(.callout.monospaced()).textSelection(.enabled) }
                Button("Copy API endpoint") { model.copyEndpoint() }
                if let pending = model.pendingForceLoad {
                    Text(pending.reason).foregroundStyle(.orange)
                    HStack { Button("Choose a smaller model") { model.pendingForceLoad = nil; importedRepo = "" }; Button("Load Anyway") { model.confirmForceLoad() } }
                }
            }
        }.padding(26).frame(width: 720, height: 680)
    }
    private func chooseFolder() {
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.prompt = "Scan This Folder"
        if panel.runModal() == .OK, let path = panel.url?.path { folder = path; scan() }
    }
    private func scan() {
        busy = true; error = ""; importedRepo = ""; scanned = false
        Task {
            defer { busy = false }
            do {
                let result = try await model.featureRequest("POST", "/control/imports/scan", body: ["path": folder])
                candidates = result["models"] as? [[String: Any]] ?? []
                unsupported = result["unsupported"] as? [[String: Any]] ?? []
                warnings = result["warnings"] as? [String] ?? []
                selectedPath = candidates.first?["path"] as? String ?? ""
                scanned = true
            } catch { self.error = error.localizedDescription }
        }
    }
    private func host() {
        busy = true; error = ""; registeredOnly = false
        model.dismissError()
        let chosen = selectedPath
        Task {
            defer { busy = false }
            do {
                let paths = addAll ? candidates.compactMap { $0["path"] as? String } : [chosen]
                for path in paths {
                    let item = try await model.featureRequest("POST", "/control/imports/register", body: ["path": path])
                    if path == chosen { importedRepo = item["repo"] as? String ?? "" }
                }
                await model.refreshCatalog()
                guard !importedRepo.isEmpty else { throw EngineError(status: 400, message: "Choose a model and try again.") }
                let kind = model.catalog.first { $0.repo == importedRepo }?.kind ?? "text"
                registeredOnly = kind == "speech" || kind == "embedding"
                if !registeredOnly { model.choose(importedRepo) }
            } catch { self.error = error.localizedDescription }
        }
    }
}
