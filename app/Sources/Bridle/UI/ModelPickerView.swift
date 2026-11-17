import SwiftUI

enum ModelFilter: String, CaseIterable, Identifiable {
    case chat = "Chat"
    case vision = "Vision"
    case embedding = "Embeddings"
    case speech = "Speech"
    case downloaded = "Downloaded"
    var id: String { rawValue }

    func matches(_ m: CatalogModel) -> Bool {
        switch self {
        case .chat: return m.kind == "text"
        case .vision: return m.kind == "vision"
        case .embedding: return m.kind == "embedding"
        case .speech: return m.kind == "speech"
        case .downloaded: return m.downloaded
        }
    }

    /// The optional engine extra this category needs, if any.
    var extra: String? {
        switch self {
        case .vision: return "vision"
        case .embedding: return "embeddings"
        case .speech: return "speech"
        default: return nil
        }
    }
}

struct ModelPickerView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var filter: ModelFilter = .chat
    @State private var customRepo = ""
    @State private var showImport = false
    @State private var confirmDelete: CatalogModel?

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if let extra = filter.extra, !featureInstalled(extra) {
                extraBanner(extra)
            }
            List(filtered) { entry in
                ModelRow(entry: entry, onDelete: { confirmDelete = entry })
            }
            .listStyle(.inset)
            Divider()
            footer
        }
        .frame(width: 760, height: 600)
        .sheet(isPresented: $showImport) { LocalModelWizard().environment(model) }
        .task { await model.refreshCatalog() }
        .confirmationDialog("Delete \(confirmDelete?.name ?? "model")?",
                            isPresented: Binding(get: { confirmDelete != nil }, set: { if !$0 { confirmDelete = nil } }),
                            presenting: confirmDelete) { entry in
            Button("Delete \(Format.bytes(entry.weightsBytes))", role: .destructive) { model.deleteModel(entry.repo) }
        } message: { _ in
            Text(confirmDelete?.repo.hasPrefix("local/") == true ? "This removes the reference from Bridle. Your original model files stay in place." : "The files are removed from disk. You can download the model again later.")
        }
    }

    private func featureInstalled(_ extra: String) -> Bool {
        guard let f = model.features else { return true }
        switch extra {
        case "vision": return f.vision
        case "embeddings": return f.embeddings
        case "speech": return f.speech
        default: return true
        }
    }

    private func extraBanner(_ extra: String) -> some View {
        HStack {
            Image(systemName: "puzzlepiece.extension")
            Text("\(extra.capitalized) support isn't installed in the engine yet.")
            Spacer()
            if model.installingExtra == extra {
                ProgressView().controlSize(.small)
                Text("Installing…").font(.caption)
            } else {
                Button("Install") { model.installExtra(extra) }
                    .disabled(model.installingExtra != nil)
            }
        }
        .font(.callout)
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(.orange.opacity(0.1))
    }

    private var filtered: [CatalogModel] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        return model.catalog.filter { filter.matches($0) }
            .filter { q.isEmpty || $0.repo.lowercased().contains(q) || $0.name.lowercased().contains(q) }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    HStack { Text("Models").font(.title2.weight(.semibold)); Button("Import Folder…") { showImport = true } }
                    if let machine = model.status?.machine {
                        Text("\(machine.chip) · \(Format.memory(machine.memoryTotalBytes)) unified memory")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                TextField("Search", text: $query)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 200)
            }
            Picker("Kind", selection: $filter) {
                ForEach(ModelFilter.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
        }
        .padding(16)
    }

    private var footer: some View {
        HStack {
            TextField("Any MLX model on Hugging Face, e.g. mlx-community/Qwen3-8B-4bit", text: $customRepo)
                .textFieldStyle(.roundedBorder)
                .onSubmit(downloadCustom)
            Button("Download", action: downloadCustom)
                .disabled(!customRepo.contains("/"))
            Spacer(minLength: 16)
            Button("Done") { dismiss() }
                .keyboardShortcut(.defaultAction)
        }
        .padding(16)
    }

    private func downloadCustom() {
        guard customRepo.contains("/") else { return }
        model.startDownload(customRepo)
        customRepo = ""
        dismiss()
    }
}

private struct ModelRow: View {
    @Environment(AppModel.self) private var model
    let entry: CatalogModel
    let onDelete: () -> Void
    @State private var draft: String = ""

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon)
                .foregroundStyle(.secondary)
                .frame(width: 20)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(entry.name).fontWeight(.medium)
                    FitBadge(fit: entry.fit)
                    if entry.repo == model.loadedRepo {
                        Text("LOADED").font(.caption2.weight(.bold)).foregroundStyle(.green)
                    }
                    if model.loadedModel?.draft?.repo == entry.repo {
                        Text("DRAFT").font(.caption2.weight(.bold)).foregroundStyle(.blue)
                    }
                }
                Text(details).font(.caption).foregroundStyle(.secondary)
                Text(entry.summary).font(.caption).foregroundStyle(.tertiary)
                if entry.downloaded && entry.kind == "text" && !entry.draftCandidates.isEmpty {
                    Picker("Speed up with", selection: $draft) {
                        Text("No draft model").tag("")
                        ForEach(entry.draftCandidates, id: \.self) { Text("Draft: \(Format.shortName($0))").tag($0) }
                    }
                    .pickerStyle(.menu)
                    .controlSize(.small)
                    .frame(maxWidth: 300)
                    .help("Speculative decoding: a small model of the same family drafts tokens that the big model verifies in one pass. Same output, often 1.5–2.5× faster.")
                }
            }
            Spacer()
            actions
        }
        .padding(.vertical, 4)
    }

    private var icon: String {
        switch entry.kind {
        case "vision": return "eye"
        case "embedding": return "point.3.connected.trianglepath.dotted"
        case "speech": return "waveform"
        default: return "text.bubble"
        }
    }

    private var details: String {
        var parts = [entry.repo]
        if !entry.params.isEmpty { parts.append(entry.params) }
        if !entry.quant.isEmpty { parts.append(entry.quant) }
        parts.append(Format.bytes(entry.weightsBytes))
        return parts.joined(separator: " · ")
    }

    @ViewBuilder private var actions: some View {
        if let job = model.download(for: entry.repo), job.isActive {
            HStack(spacing: 8) {
                ProgressView(value: job.fraction).frame(width: 90)
                Text(Format.percent(job.fraction)).font(.caption).monospacedDigit()
                Button("Cancel") { model.cancelDownload(job) }
            }
        } else if entry.downloaded {
            HStack(spacing: 6) {
                if entry.isChatModel {
                    let isLoaded = entry.repo == model.loadedRepo && (model.loadedModel?.draft?.repo ?? "") == draft
                    Button(isLoaded ? "Loaded" : "Load") {
                        model.choose(entry.repo, draft: draft.isEmpty ? nil : draft)
                    }
                    .disabled(isLoaded)
                    .help(entry.fit == "too_large" ? "Likely too large for this Mac's memory." : "Load into memory and serve")
                } else {
                    Text(entry.kind == "embedding" ? "Used by /v1/embeddings" : "Used by /v1/audio/transcriptions")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Button(role: .destructive, action: onDelete) {
                    Image(systemName: "trash")
                }
                .buttonStyle(.borderless)
                .disabled(entry.repo == model.loadedRepo)
            }
        } else {
            Button("Download") { model.startDownload(entry.repo) }
                .help("Needs about \(Format.bytes(entry.requiredBytes)) of memory to run")
        }
    }
}

struct FitBadge: View {
    let fit: String

    var body: some View {
        Text(label)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .foregroundStyle(color)
            .background(color.opacity(0.15), in: Capsule())
    }

    private var label: String {
        switch fit {
        case "fits": return "Fits"
        case "tight": return "Tight fit"
        default: return "Too large"
        }
    }

    private var color: Color {
        switch fit {
        case "fits": return .green
        case "tight": return .orange
        default: return .red
        }
    }
}
