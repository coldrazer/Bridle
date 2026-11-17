import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct BottomPanelView: View {
    @Environment(AppModel.self) private var model
    let panel: BottomPanel

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Picker("Panel", selection: Binding(get: { panel }, set: { model.bottomPanel = $0 })) {
                    ForEach(BottomPanel.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                Spacer()
                switch panel {
                case .chat:
                    Button("Clear") { model.clearChat() }
                        .buttonStyle(.borderless)
                        .disabled(model.chat.isEmpty)
                case .requests:
                    Button("Clear History") { model.clearRequests() }
                        .buttonStyle(.borderless)
                case .benchmarks:
                    Button("Clear") { model.clearBenchmarks() }
                        .buttonStyle(.borderless)
                        .disabled(model.benchmarks.isEmpty)
                case .devTools:
                    Button("Copy All") {
                        model.copyToPasteboard(model.devLog.map(\.text).joined(separator: "\n"))
                    }
                    .buttonStyle(.borderless)
                    Button("Clear") { model.clearLog() }
                        .buttonStyle(.borderless)
                }
                Button {
                    model.bottomPanel = nil
                } label: {
                    Image(systemName: "chevron.down")
                }
                .buttonStyle(.borderless)
                .help("Hide panel")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            Divider()
            switch panel {
            case .chat: ChatPanel()
            case .requests: RequestsPanel()
            case .benchmarks: BenchmarksPanel()
            case .devTools: DevToolsPanel()
            }
        }
        .background(Color(nsColor: .controlBackgroundColor))
    }
}

// ---- Sandbox Chat ----------------------------------------------------------------------------------
struct ChatPanel: View {
    @Environment(AppModel.self) private var model
    @State private var draft = ""
    @State private var transcribing = false
    @FocusState private var focused: Bool

    var body: some View {
        @Bindable var model = model
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if model.chat.isEmpty {
                            Text(model.canChat
                                 ? "Try the loaded model here. Apps on your network can use the same model through the API endpoint."
                                 : "Load a model with ▶ to start chatting.")
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity)
                                .padding(.top, 24)
                        }
                        ForEach(model.chat) { turn in
                            ChatBubble(turn: turn, streaming: model.isGenerating && turn.id == model.chat.last?.id)
                                .id(turn.id)
                        }
                    }
                    .padding(16)
                }
                .onChange(of: model.chat.last?.text) {
                    if let last = model.chat.last { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
            if !model.pendingImages.isEmpty {
                HStack {
                    ForEach(Array(model.pendingImages.enumerated()), id: \.offset) { index, url in
                        AttachmentThumb(dataURL: url) { model.pendingImages.remove(at: index) }
                    }
                    Spacer()
                }
                .padding(.horizontal, 14)
            }
            HStack(alignment: .bottom, spacing: 8) {
                Menu {
                    Button("Attach Image…") { pickImage() }
                        .disabled(model.loadedModel?.kind != "vision")
                    Button("Add Document…") { model.pickDocument() }
                    Menu("Transcribe Audio…") {
                        if model.downloadedSpeechModels.isEmpty {
                            Text("Download a speech model first (Models → Speech)")
                        }
                        ForEach(model.downloadedSpeechModels) { m in
                            Button(m.name) { pickAudio(model: m.repo) }
                        }
                    }
                    Divider()
                    Toggle("JSON Output", isOn: $model.jsonOutput)
                    Picker("Preset", selection: $model.chatPreset) {
                        Text("No Preset").tag(String?.none)
                        ForEach(model.presets) { p in Text(p.name).tag(Optional(p.name)) }
                    }
                } label: {
                    Image(systemName: transcribing ? "waveform" : "plus.circle")
                        .font(.title2)
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .help("Attach image, transcribe audio, JSON output, presets")

                TextField(placeholder, text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...6)
                    .focused($focused)
                    .onSubmit(send)
                    .disabled(!model.canChat)
                if model.isGenerating {
                    Button {
                        model.stopGenerating()
                    } label: {
                        Image(systemName: "stop.circle.fill").font(.title2)
                    }
                    .buttonStyle(.borderless)
                    .help("Stop generating")
                } else {
                    Button {
                        send()
                    } label: {
                        Image(systemName: "arrow.up.circle.fill").font(.title2)
                    }
                    .buttonStyle(.borderless)
                    .disabled((draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && model.pendingImages.isEmpty)
                              || !model.canChat)
                    .keyboardShortcut(.return, modifiers: [.command])
                }
            }
            .padding(10)
            .background(.background, in: RoundedRectangle(cornerRadius: 18))
            .overlay(RoundedRectangle(cornerRadius: 18).stroke(.quaternary))
            .padding(.horizontal, 12)
            .padding(.top, 6)
            HStack(spacing: 10) {
                if let preset = model.chatPreset { Label("Preset: \(preset)", systemImage: "slider.horizontal.3") }
                if model.jsonOutput { Label("JSON", systemImage: "curlybraces") }
                if transcribing { Label("Transcribing…", systemImage: "waveform") }
                Spacer()
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 22)
            .padding(.bottom, 8)
        }
        .onAppear { focused = true }
        .dropDestination(for: URL.self) { urls, _ in
            let images = urls.filter { ["png", "jpg", "jpeg", "gif", "webp", "heic"].contains($0.pathExtension.lowercased()) }
            for url in images { model.attachImage(from: url) }
            let documents = urls.filter { ["pdf", "txt", "md"].contains($0.pathExtension.lowercased()) }
            for url in documents.prefix(1) { model.importDocument(url) }
            return !images.isEmpty || !documents.isEmpty
        }
    }

    private var placeholder: String {
        model.loadedModel?.kind == "vision" ? "Ask about an image, or anything…" : "Ask me anything…"
    }

    private func send() {
        let text = draft
        guard !(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && model.pendingImages.isEmpty),
              !model.isGenerating else { return }
        draft = ""
        model.send(text)
    }

    private func pickImage() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.image]
        panel.allowsMultipleSelection = true
        if panel.runModal() == .OK {
            for url in panel.urls { model.attachImage(from: url) }
        }
    }

    private func pickAudio(model repo: String) {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.audio]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        transcribing = true
        Task {
            if let text = await model.transcribe(fileURL: url, model: repo) {
                draft += (draft.isEmpty ? "" : " ") + text
            }
            transcribing = false
        }
    }
}

private struct AttachmentThumb: View {
    let dataURL: String
    let onRemove: () -> Void

    var body: some View {
        ZStack(alignment: .topTrailing) {
            if let image = decode() {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .frame(width: 48, height: 48)
                    .clipShape(RoundedRectangle(cornerRadius: 6))
            }
            Button(action: onRemove) {
                Image(systemName: "xmark.circle.fill")
            }
            .buttonStyle(.borderless)
            .offset(x: 6, y: -6)
        }
    }

    private func decode() -> NSImage? {
        guard let comma = dataURL.firstIndex(of: ","),
              let data = Data(base64Encoded: String(dataURL[dataURL.index(after: comma)...])) else { return nil }
        return NSImage(data: data)
    }
}

private struct ChatBubble: View {
    let turn: ChatTurn
    let streaming: Bool
    @State private var showReasoning = false

    var body: some View {
        if turn.role == .user {
            HStack {
                Spacer(minLength: 80)
                VStack(alignment: .trailing, spacing: 6) {
                    if !turn.images.isEmpty {
                        HStack { ForEach(Array(turn.images.enumerated()), id: \.offset) { _, url in AttachmentThumb(dataURL: url) {} } }
                    }
                    if !turn.text.isEmpty {
                        Text(turn.text)
                            .textSelection(.enabled)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 8)
                            .background(.quaternary.opacity(0.7), in: RoundedRectangle(cornerRadius: 14))
                    }
                }
            }
        } else {
            VStack(alignment: .leading, spacing: 6) {
                if !turn.reasoning.isEmpty {
                    DisclosureGroup(isExpanded: $showReasoning) {
                        Text(turn.reasoning)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    } label: {
                        Label(streaming && turn.text.isEmpty ? "Thinking…" : "Reasoning", systemImage: "brain")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                if turn.text.isEmpty && turn.reasoning.isEmpty && turn.toolCalls.isEmpty && streaming {
                    ProgressView().controlSize(.small)
                } else if !turn.text.isEmpty {
                    Text(turn.text)
                        .textSelection(.enabled)
                        .foregroundStyle(turn.failed ? Color.red : Color.primary)
                }
                ForEach(turn.toolCalls, id: \.self) { call in
                    Label(call, systemImage: "wrench.and.screwdriver")
                        .font(.system(.callout, design: .monospaced))
                        .padding(6)
                        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
                }
                if let footnote = turn.footnote {
                    Text(footnote).font(.caption).foregroundStyle(.tertiary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

// ---- Requests ---------------------------------------------------------------------------------------
struct RequestsPanel: View {
    @Environment(AppModel.self) private var model
    @State private var selection: RequestRow.ID?

    var body: some View {
        VStack(spacing: 0) {
            if let s = model.requestSummary {
                HStack(spacing: 16) {
                    Text("Last 24 h: \(s.requests) requests")
                    Text("\(s.promptTokens.formatted()) in · \(s.completionTokens.formatted()) out")
                    Text(String(format: "avg %.1f tok/s", s.avgTps))
                    if s.errors > 0 { Text("\(s.errors) errors").foregroundStyle(.red) }
                    Spacer()
                    if model.engineSettings?.logContent != true {
                        Text("Prompt text isn't stored (Settings → Performance).")
                            .foregroundStyle(.tertiary)
                    }
                }
                .font(.caption)
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
            }
            Table(model.requests, selection: $selection) {
                TableColumn("Time") { r in Text(Date(timeIntervalSince1970: r.ts), format: .dateTime.hour().minute().second()) }
                    .width(70)
                TableColumn("Client") { r in Text(r.client) }.width(min: 60, ideal: 90)
                TableColumn("Endpoint") { r in Text(r.endpoint).font(.system(.caption, design: .monospaced)) }
                    .width(min: 120, ideal: 170)
                TableColumn("Model") { r in Text(Format.shortName(r.model)) }.width(min: 100, ideal: 180)
                TableColumn("Tokens") { r in
                    Text(r.cachedTokens > 0 ? "\(r.promptTokens) (\(r.cachedTokens) cached) → \(r.completionTokens)"
                                            : "\(r.promptTokens) → \(r.completionTokens)")
                        .monospacedDigit()
                }
                .width(min: 90, ideal: 150)
                TableColumn("tok/s") { r in Text(r.tps > 0 ? String(format: "%.1f", r.tps) : "–").monospacedDigit() }
                    .width(50)
                TableColumn("Status") { r in
                    Text(r.status == 200 ? (r.finishReason.isEmpty ? "ok" : r.finishReason) : "\(r.status)")
                        .foregroundStyle(r.status >= 400 ? Color.red : Color.secondary)
                        .help(r.error)
                }
                .width(70)
            }
            if let id = selection, let row = model.requests.first(where: { $0.id == id }),
               row.prompt != nil || !row.error.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: 6) {
                        if !row.error.isEmpty { Text(row.error).foregroundStyle(.red) }
                        if let p = row.prompt { Text(p).font(.system(.caption, design: .monospaced)) }
                        if let r = row.response {
                            Divider()
                            Text(r).font(.system(.caption, design: .monospaced))
                        }
                    }
                    .textSelection(.enabled)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(height: 120)
            }
        }
        .task { await model.refreshRequests() }
    }
}

// ---- Benchmarks ---------------------------------------------------------------------------------------
struct BenchmarksPanel: View {
    @Environment(AppModel.self) private var model
    @State private var promptTokens = 512
    @State private var generationTokens = 128

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Text(model.loadedRepo.map { "Model: \(Format.shortName($0))" } ?? "Load a text model to benchmark")
                    .foregroundStyle(.secondary)
                Spacer()
                Picker("Prompt", selection: $promptTokens) {
                    ForEach([128, 512, 2048, 8192], id: \.self) { Text("\($0) tok").tag($0) }
                }
                .frame(width: 150)
                Picker("Generate", selection: $generationTokens) {
                    ForEach([64, 128, 256, 512], id: \.self) { Text("\($0) tok").tag($0) }
                }
                .frame(width: 160)
                if model.isBenchmarking {
                    ProgressView().controlSize(.small)
                    Text("Running…").font(.caption)
                } else {
                    Button("Run Benchmark") { model.runBenchmark(promptTokens: promptTokens, generationTokens: generationTokens) }
                        .disabled(!model.isModelReady || model.loadedModel?.kind != "text")
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            Table(model.benchmarks) {
                TableColumn("Date") { b in Text(Date(timeIntervalSince1970: b.createdAt), format: .dateTime.month().day().hour().minute()) }
                    .width(110)
                TableColumn("Model") { b in
                    Text(Format.shortName(b.repo) + (b.draft.map { " + \(Format.shortName($0))" } ?? ""))
                }
                .width(min: 140, ideal: 240)
                TableColumn("Prompt tok/s") { b in Text(String(format: "%.0f", b.promptTps)).monospacedDigit() }.width(90)
                TableColumn("Gen tok/s") { b in Text(String(format: "%.1f", b.generationTps)).monospacedDigit().bold() }.width(80)
                TableColumn("First token") { b in Text("\(b.timeToFirstTokenMs) ms").monospacedDigit() }.width(85)
                TableColumn("Peak memory") { b in Text(String(format: "%.2f GB", b.peakMemoryGb)).monospacedDigit() }.width(90)
                TableColumn("Shape") { b in Text("\(b.promptTokens) → \(b.generatedTokens)").monospacedDigit() }.width(80)
            }
        }
        .task { await model.refreshBenchmarks() }
    }
}

// ---- Dev Tools ----------------------------------------------------------------------------------------
struct DevToolsPanel: View {
    @Environment(AppModel.self) private var model
    @State private var filter = ""
    @State private var errorsOnly = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                TextField("Filter", text: $filter)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 260)
                Toggle("Errors only", isOn: $errorsOnly)
                    .toggleStyle(.checkbox)
                Spacer()
                Button("Crash Reports") {
                    try? FileManager.default.createDirectory(at: CrashReporter.directory, withIntermediateDirectories: true)
                    NSWorkspace.shared.open(CrashReporter.directory)
                }
                .buttonStyle(.borderless)
                Text("\(model.devLog.count) lines")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 1) {
                        ForEach(lines) { entry in
                            Text(entry.text)
                                .font(.system(size: 11, design: .monospaced))
                                .foregroundStyle(isProblem(entry) ? Color.red : Color.primary)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id(entry.id)
                        }
                    }
                    .padding(.horizontal, 12)
                    .padding(.bottom, 8)
                }
                .onChange(of: model.devLog.last?.id) {
                    if let last = lines.last { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
        }
    }

    private var lines: [DevLogEntry] {
        let q = filter.lowercased()
        return model.devLog.filter { entry in
            (!errorsOnly || isProblem(entry)) && (q.isEmpty || entry.text.lowercased().contains(q))
        }
    }

    /// stderr carries all engine logging, so color by level rather than by stream.
    private func isProblem(_ entry: DevLogEntry) -> Bool {
        let t = entry.text
        return t.contains(" ERROR ") || t.contains(" CRITICAL ") || t.hasPrefix("Error:") || t.hasPrefix("Traceback")
            || t.hasPrefix("Engine exited")
    }
}
