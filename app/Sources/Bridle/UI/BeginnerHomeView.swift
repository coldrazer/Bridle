import SwiftUI

/// A guided, task-first entrance to the same engine and chat used by the workspace.
struct BeginnerHomeView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("advancedMode") private var advancedMode = false
    @State private var assistant: BeginnerAssistant = .everyday
    @State private var showShop = false
    @State private var showGuide = false
    @State private var showAria = false
    @State private var pendingAssistantRepo: String?
    @State private var loadAttempted = false
    @State private var installingPhotoSupport = false

    private var memoryBytes: Int64 {
        model.status?.machine.memoryTotalBytes ?? Int64(ProcessInfo.processInfo.physicalMemory)
    }
    private var recommendation: CatalogModel? {
        assistant.recommendation(in: model.catalog, memoryBytes: memoryBytes)
    }
    private var pendingDownload: DownloadJob? {
        pendingAssistantRepo.flatMap { model.download(for: $0) }
    }
    private var autoLoadState: String {
        [pendingAssistantRepo ?? "", pendingDownload?.state ?? "", String(model.catalog.contains { $0.repo == pendingAssistantRepo && $0.downloaded }),
         String(model.isRunning), String(model.features?.vision == true), model.installingExtra ?? "", String(model.isGenerating)].joined(separator: "|")
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if model.canChat {
                readyHeader
                if let repo = pendingAssistantRepo, pendingDownload?.state != "completed", pendingDownload != nil {
                    BeginnerDownloadProgress(repo: repo, job: pendingDownload,
                        cancel: { if let job = pendingDownload { model.cancelDownload(job) } },
                        retry: { loadAttempted = false; model.startDownload(repo); model.focusedDownloadRepo = nil })
                        .padding(.horizontal, 16)
                }
                if model.installingExtra != nil {
                    HStack { ProgressView().controlSize(.small); Text("Adding photo support…").foregroundStyle(.secondary); Spacer() }.padding(.horizontal, 16)
                }
                if model.chat.isEmpty { starters }
                ChatPanel()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        welcome
                        setupSteps
                        if model.isRunning {
                            if model.modelState == "loading" {
                                Label("Getting your assistant ready…", systemImage: "sparkles")
                                ProgressView().controlSize(.small)
                            } else {
                                assistantShop
                            }
                        }
                        if let repo = pendingAssistantRepo {
                            BeginnerDownloadProgress(repo: repo, job: pendingDownload,
                                cancel: { if let job = pendingDownload { model.cancelDownload(job) } },
                                retry: { loadAttempted = false; model.startDownload(repo); model.focusedDownloadRepo = nil })
                        }
                        recovery
                    }
                    .padding(32)
                    .frame(maxWidth: 840, alignment: .leading)
                    .frame(maxWidth: .infinity)
                }
            }
            if model.canChat, model.lastError != nil || model.modelState == "error" {
                recovery.padding(12)
            }
        }
        .frame(minWidth: 640, minHeight: 480)
        .task { model.bootstrap() }
        .task(id: autoLoadState) { finishAssistantPreparation() }
        .sheet(isPresented: $showShop) {
            ScrollView { assistantShop.padding(28) }.frame(width: 620, height: 550)
                .safeAreaInset(edge: .bottom) {
                    HStack { Spacer(); Button("Done") { showShop = false }.keyboardShortcut(.cancelAction) }.padding()
                }
        }
        .sheet(isPresented: $showGuide) { BeginnerGuideView() }
        .sheet(isPresented: $showAria) { AriaView() }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Label("Bridle", systemImage: "sparkles").font(.title3.weight(.semibold))
            Spacer()
            Button("Use My Models") { model.showLocalImport = true }.disabled(!model.isRunning)
            Button("Choose Assistant") { showShop = true }.disabled(!model.isRunning)
            Button("Ask Aria", systemImage: "sparkles") { showAria = true }
                .help("Ask Aria, Bridle's built-in guide, for help")
            Button("Guide", systemImage: "questionmark.circle") { showGuide = true }
            Button("Advanced Workspace") { advancedMode = true }
        }.padding(16)
    }

    private var welcome: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Your own AI, right on your Mac.").font(.largeTitle.bold())
            Text("Choose an assistant, download it once, and start a conversation. Your chat is processed on this Mac.")
                .font(.title3).foregroundStyle(.secondary)
            Label("Internet is needed for setup and new downloads. Downloaded assistants can chat offline.", systemImage: "arrow.down.circle")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    private var setupSteps: some View {
        VStack(alignment: .leading, spacing: 14) {
            step("1", title: "Prepare your Mac", done: model.isRunning, detail: setupDetail)
            switch model.engineState {
            case .needsSetup:
                Button("Set Up Bridle") { model.runSetup() }.buttonStyle(.borderedProminent)
                Text("This installs the software Bridle needs. Keep your Mac connected to the internet.").font(.caption).foregroundStyle(.secondary)
            case .settingUp, .starting, .locating:
                ProgressView().controlSize(.small)
                DisclosureGroup("Show setup details") {
                    Text(model.devLog.suffix(8).map(\.text).joined(separator: "\n"))
                        .font(.caption.monospaced()).textSelection(.enabled)
                }
            case .failed:
                Button("Try Starting Again") { model.restartEngine() }
            case .running: EmptyView()
            }
            step("2", title: "Choose your assistant", done: model.canChat,
                 detail: "We suggest a comfortable fit for your Mac’s \(Format.memory(memoryBytes)) of memory.")
            step("3", title: "Start a conversation", done: !model.chat.isEmpty,
                 detail: "Ask a question, or try one of the starter ideas.")
        }
        .padding(20).background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 14))
    }

    private func step(_ number: String, title: String, done: Bool, detail: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: done ? "checkmark.circle.fill" : "\(number).circle")
                .font(.title2).foregroundStyle(done ? Color.green : Color.secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.headline)
                Text(detail).font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    private var setupDetail: String {
        switch model.engineState {
        case .running: return "Bridle is ready on this Mac."
        case .settingUp: return "Installing the essentials. This can take a few minutes."
        case .starting, .locating: return "Checking your Mac and starting Bridle…"
        case .needsSetup: return "A one-time setup gets everything ready."
        case .failed: return "Bridle needs a little help starting. See the recovery options below."
        }
    }

    private var assistantShop: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("What would you like help with?").font(.title2.bold())
            Picker("Assistant", selection: $assistant) {
                ForEach(BeginnerAssistant.allCases) { Text($0.rawValue).tag($0) }
            }.pickerStyle(.segmented)
            Label(assistant.rawValue, systemImage: assistant.symbol).font(.title3.bold())
            Text(assistant.detail).foregroundStyle(.secondary)
            if let suggested = recommendation {
                Text("Recommended for your Mac").font(.headline)
                Text("\(Format.bytes(suggested.weightsBytes)) download · \(suggested.downloaded ? "Already on your Mac" : "Download once, use offline")")
                    .foregroundStyle(.secondary)
                if assistant == .photo, model.features?.vision != true {
                    Text("Photo help also needs a one-time support download. Bridle will install it before opening your assistant.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Button(suggested.downloaded ? "Use This Assistant" : "Download This Assistant") {
                    prepare(suggested)
                    showShop = false
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.modelState == "loading" || model.isGenerating || model.installingExtra != nil || pendingDownload?.isActive == true)
                DisclosureGroup("Assistant details") {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(suggested.name)
                        Text(suggested.repo).textSelection(.enabled)
                        Text("Estimated memory: \(Format.bytes(suggested.requiredBytes)). Bridle checks available memory again before opening it.")
                    }.font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Text(model.catalog.isEmpty ? "The assistant list is still loading." : "We couldn’t find a comfortable fit for this task on your Mac.")
                    .foregroundStyle(.secondary)
                Button("Refresh Assistant List") { Task { await model.refreshCatalog() } }
                Button("Browse All Models in Advanced Workspace") { advancedMode = true }
            }
            if let repo = model.loadedRepo, model.canChat {
                Text("Currently open: \(Format.shortName(repo))").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var readyHeader: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Label("Ready to chat", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                Text("Runs on your Mac · No internet needed for this chat").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("New Conversation") { model.clearChat() }.disabled(model.isGenerating || model.chat.isEmpty)
        }.padding(16)
    }

    private var starters: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Try a conversation starter").font(.headline)
            ForEach(assistant.prompts, id: \.self) { prompt in
                Button(prompt) { model.send(prompt) }.buttonStyle(.link).disabled(!model.canChat || model.isGenerating)
            }
        }.frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 20).padding(.bottom, 8)
    }

    @ViewBuilder private var recovery: some View {
        if let message = recoveryMessage {
            VStack(alignment: .leading, spacing: 10) {
                Label("Let’s get you going again", systemImage: "exclamationmark.triangle").font(.headline)
                Text("Check your internet connection for downloads. If an assistant won’t open, close other memory-heavy apps or choose Everyday for a smaller option.")
                    .font(.callout).foregroundStyle(.secondary)
                HStack {
                    Button("Choose Another Assistant") { showShop = true }.disabled(!model.isRunning)
                    Button("Restart Bridle’s Engine") { model.dismissError(); model.restartEngine() }
                    if model.crashNotice?.report != nil { Button("Open Report") { model.openCrashReport() } }
                }
                DisclosureGroup("Show error details") { Text(message).font(.caption).textSelection(.enabled) }
                if model.lastError != nil { Button("Dismiss") { model.dismissError() }.buttonStyle(.link) }
            }.padding(16).background(.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
        }
    }

    private var recoveryMessage: String? {
        if let error = model.lastError { return error }
        if case .failed(let error) = model.engineState { return error }
        if case .needsSetup(let reason) = model.engineState { return reason }
        if model.modelState == "error" { return model.status?.model.error }
        return nil
    }

    private func prepare(_ choice: CatalogModel) {
        pendingAssistantRepo = choice.repo
        model.selectedRepo = choice.repo
        loadAttempted = false
        installingPhotoSupport = false
        if !choice.downloaded {
            model.startDownload(choice.repo)
            model.focusedDownloadRepo = nil
        }
        finishAssistantPreparation()
    }

    private func finishAssistantPreparation() {
        guard let repo = pendingAssistantRepo, !loadAttempted, model.isRunning, !model.isGenerating,
              let choice = model.catalog.first(where: { $0.repo == repo && $0.downloaded }) else { return }
        if choice.kind == "vision", model.features?.vision != true {
            guard !installingPhotoSupport, model.installingExtra == nil else { return }
            installingPhotoSupport = true
            model.installExtra("vision")
            return
        }
        guard model.modelState != "loading" else { return }
        loadAttempted = true
        model.requestLoad(repo, draft: nil)
    }
}
