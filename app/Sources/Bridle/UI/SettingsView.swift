import SwiftUI
import UniformTypeIdentifiers

struct SettingsView: View {
    var body: some View {
        TabView {
            GeneralSettings().tabItem { Label("General", systemImage: "gearshape") }
            PerformanceSettings().tabItem { Label("Performance", systemImage: "speedometer") }
            KeysSettings().tabItem { Label("API Keys", systemImage: "key") }
            PresetsSettings().tabItem { Label("Presets", systemImage: "slider.horizontal.3") }
            SystemSettings().tabItem { Label("System", systemImage: "stethoscope") }
            AppSettings().tabItem { Label("App", systemImage: "macwindow") }
        }
        .frame(width: 620, height: 520)
    }
}

// ---- General: network & server -----------------------------------------------------------------------
private struct GeneralSettings: View {
    @Environment(AppModel.self) private var model
    @State private var portText = ""

    var body: some View {
        @Bindable var model = model
        Form {
            Section("Server") {
                Toggle("Share the API on my local network", isOn: $model.shareOnNetwork)
                Text("Off: only apps on this Mac can connect. On: enable HTTPS below and create API keys for devices on your network. Remote HTTP requests are rejected.")
                    .font(.caption).foregroundStyle(.secondary)
                TextField("Port", text: $portText).onSubmit(applyPort)
                SecureField("Single shared API key (optional; prefer named keys)", text: $model.apiKey)
            }
            if let s = model.engineSettings {
                Section("Protocols") {
                    Toggle("Ollama-compatible API (/api/*)", isOn: binding(s.ollamaApi) { $0.ollamaApi = $1 })
                    Toggle("Advertise on the network with Bonjour", isOn: binding(s.bonjour) { $0.bonjour = $1 })
                    Toggle("Serve HTTPS (required for network clients)", isOn: binding(s.tls) { $0.tls = $1 })
                    if s.tls {
                        Text("HTTPS runs on port \(s.tlsPort == 0 ? "HTTP + 1" : String(s.tlsPort)). Clients should pin the certificate fingerprint shown in the inspector.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Toggle("Require API keys from this Mac too", isOn: binding(s.localRequiresKey) { $0.localRequiresKey = $1 })
                    Toggle("Allow vision models to fetch remote image URLs", isOn: binding(s.allowRemoteImages) { $0.allowRemoteImages = $1 })
                    Text("Remote images must resolve to public addresses either way; data URLs always work.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section {
                Button("Restart Engine to Apply") {
                    applyPort()
                    model.restartEngine()
                }
            }
            Section("Storage") {
                LabeledContent("Models folder", value: EngineLocator.modelsDirectory.path)
                Button("Show in Finder") { model.revealModelsFolder() }
            }
        }
        .formStyle(.grouped)
        .onAppear { portText = String(model.preferredPort) }
    }

    private func binding(_ value: Bool, _ set: @escaping (inout EngineSettings, Bool) -> Void) -> Binding<Bool> {
        Binding(get: { value }, set: { new in model.updateEngineSettings { set(&$0, new) } })
    }

    private func applyPort() {
        if let port = Int(portText.trimmingCharacters(in: .whitespaces)), (1024...65535).contains(port) {
            model.preferredPort = port
        } else {
            portText = String(model.preferredPort)
        }
    }
}

// ---- Performance -------------------------------------------------------------------------------------
private struct PerformanceSettings: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        Form {
            if let s = model.engineSettings {
                Section("Throughput") {
                    Toggle("Continuous batching (serve concurrent requests together)", isOn: bool(s.batching) { $0.batching = $1 })
                    Stepper("Max batch size: \(s.maxBatchSize)", value: int(s.maxBatchSize) { $0.maxBatchSize = $1 }, in: 1...64)
                    Stepper("Concurrent requests: \(s.maxConcurrentRequests)", value: int(s.maxConcurrentRequests) { $0.maxConcurrentRequests = $1 }, in: 1...64)
                    Stepper("Queue wait before 429: \(s.queueWaitSeconds)s", value: int(s.queueWaitSeconds) { $0.queueWaitSeconds = $1 }, in: 0...600, step: 5)
                    Toggle("Prompt caching (reuse work from earlier turns)", isOn: bool(s.promptCache) { $0.promptCache = $1 })
                    Toggle("Persist prompt cache to disk", isOn: bool(s.promptCacheDisk) { $0.promptCacheDisk = $1 })
                    if s.promptCacheDisk {
                        Stepper("Disk prompt cache cap: \(s.promptCacheDiskMb) MB", value: int(s.promptCacheDiskMb) { $0.promptCacheDiskMb = $1 }, in: 64...16384, step: 256)
                    }
                    Picker("Fast model (second slot)", selection: Binding(
                        get: { s.fastModel },
                        set: { new in model.updateEngineSettings { $0.fastModel = new } })) {
                        Text("None").tag("")
                        ForEach(model.downloadedChatModels) { entry in
                            Text(entry.name).tag(entry.repo)
                        }
                    }
                    Text("The fast model stays resident beside the main model for small/background calls (e.g. a harness's fast model).")
                        .font(.caption).foregroundStyle(.secondary)
                    Stepper("Speculative look-ahead: \(s.numDraftTokens) tokens", value: int(s.numDraftTokens) { $0.numDraftTokens = $1 }, in: 1...16)
                }
                Section("Memory") {
                    Toggle("Memory guard (warn before loading models that won't fit)", isOn: bool(s.memoryGuard) { $0.memoryGuard = $1 })
                    Picker("Unload idle models after", selection: int(s.idleUnloadMinutes) { $0.idleUnloadMinutes = $1 }) {
                        Text("Never").tag(0)
                        ForEach([5, 15, 30, 60, 120, 240], id: \.self) { Text("\($0) minutes").tag($0) }
                    }
                    Text("Unloaded models reload automatically on the next request.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Request history") {
                    Toggle("Store prompt and response text", isOn: bool(s.logContent) { $0.logContent = $1 })
                    Text("Off by default: history keeps only timing, token counts and errors.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Text("Start the engine to change these settings.").foregroundStyle(.secondary)
            }
            Section("Sandbox Chat") {
                TextField("System prompt", text: $model.systemPrompt, axis: .vertical).lineLimit(2...5)
                Stepper("Max tokens: \(model.maxTokens)", value: $model.maxTokens, in: 64...32768, step: 256)
                HStack {
                    Text("Temperature")
                    Slider(value: $model.temperature, in: 0...2, step: 0.05)
                    Text(String(format: "%.2f", model.temperature)).monospacedDigit().frame(width: 40)
                }
            }
        }
        .formStyle(.grouped)
    }

    private func bool(_ value: Bool, _ set: @escaping (inout EngineSettings, Bool) -> Void) -> Binding<Bool> {
        Binding(get: { value }, set: { new in model.updateEngineSettings { set(&$0, new) } })
    }

    private func int(_ value: Int, _ set: @escaping (inout EngineSettings, Int) -> Void) -> Binding<Int> {
        Binding(get: { value }, set: { new in model.updateEngineSettings { set(&$0, new) } })
    }
}

// ---- API keys -------------------------------------------------------------------------------------------
private struct KeysSettings: View {
    @Environment(AppModel.self) private var model
    @State private var name = ""
    @State private var rpm = ""
    @State private var daily = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Give each app or device its own key, with optional limits. Keys are required from other devices once any key exists.")
                .font(.callout).foregroundStyle(.secondary)
            HStack {
                TextField("Name (e.g. Gio's laptop)", text: $name)
                TextField("Requests/min", text: $rpm).frame(width: 100)
                TextField("Tokens/day", text: $daily).frame(width: 100)
                Button("Create Key") {
                    model.createKey(name: name, rpmLimit: Int(rpm), dailyTokenLimit: Int(daily))
                    name = ""; rpm = ""; daily = ""
                }
                .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            Table(model.keys) {
                TableColumn("Name") { k in Text(k.name).foregroundStyle(k.revoked ? Color.secondary : Color.primary) }
                TableColumn("Key") { k in Text(k.hint).font(.system(.caption, design: .monospaced)) }.width(100)
                TableColumn("Limits") { k in
                    Text([k.rpmLimit.map { "\($0)/min" }, k.dailyTokenLimit.map { "\($0.formatted()) tok/day" }]
                        .compactMap { $0 }.joined(separator: ", ").ifEmpty("None"))
                }
                TableColumn("Usage") { k in Text("\(k.requestsTotal) req · \(k.tokensTotal.formatted()) tok").monospacedDigit() }
                TableColumn("") { k in
                    HStack {
                        if !k.revoked {
                            Button("Revoke") { model.revokeKey(k) }
                        } else {
                            Text("Revoked").foregroundStyle(.secondary)
                        }
                        Button(role: .destructive) { model.deleteKey(k) } label: { Image(systemName: "trash") }
                            .buttonStyle(.borderless)
                    }
                }
                .width(120)
            }
        }
        .padding(16)
        .task { await model.refreshKeys() }
        .sheet(item: Binding(get: { model.createdKey.map(CreatedKeySheetItem.init) },
                             set: { if $0 == nil { model.createdKey = nil } })) { item in
            VStack(alignment: .leading, spacing: 12) {
                Text("Key for \(item.created.key.name)").font(.headline)
                Text("Copy it now. Bridle only stores a hash, so it can't be shown again.")
                    .font(.callout).foregroundStyle(.secondary)
                Text(item.created.secret)
                    .font(.system(.body, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(8)
                    .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
                HStack {
                    Button("Copy") { model.copyToPasteboard(item.created.secret) }
                    Spacer()
                    Button("Done") { model.createdKey = nil }.keyboardShortcut(.defaultAction)
                }
            }
            .padding(20)
            .frame(width: 520)
        }
    }
}

private struct CreatedKeySheetItem: Identifiable {
    let created: CreatedKey
    var id: String { created.key.id }
}

private extension String {
    func ifEmpty(_ fallback: String) -> String { isEmpty ? fallback : self }
}

// ---- Presets ----------------------------------------------------------------------------------------------
private struct PresetsSettings: View {
    @Environment(AppModel.self) private var model
    @State private var editing = Preset(name: "", model: "", systemPrompt: "", temperature: nil, topP: nil, maxTokens: nil)
    @State private var useTemperature = false
    @State private var useMaxTokens = false

    var body: some View {
        HSplitView {
            List(selection: Binding<String?>(get: { editing.name }, set: { name in
                if let name, let p = model.presets.first(where: { $0.name == name }) { load(p) }
            })) {
                ForEach(model.presets) { p in Text(p.name).tag(p.name) }
            }
            .frame(minWidth: 160, maxWidth: 200)
            Form {
                TextField("Name", text: $editing.name)
                Picker("Model", selection: $editing.model) {
                    Text("Whatever is loaded").tag("")
                    ForEach(model.downloadedChatModels) { Text($0.name).tag($0.repo) }
                }
                TextField("System prompt", text: $editing.systemPrompt, axis: .vertical).lineLimit(3...8)
                Toggle("Temperature", isOn: $useTemperature)
                if useTemperature {
                    Slider(value: Binding(get: { editing.temperature ?? 0.7 }, set: { editing.temperature = $0 }), in: 0...2, step: 0.05)
                }
                Toggle("Max tokens", isOn: $useMaxTokens)
                if useMaxTokens {
                    Stepper("\(editing.maxTokens ?? 1024)", value: Binding(get: { editing.maxTokens ?? 1024 },
                                                                         set: { editing.maxTokens = $0 }), in: 16...32768, step: 128)
                }
                Text("Use from any app with model \"preset:\(editing.name.isEmpty ? "Name" : editing.name)\". Values a request sends explicitly win.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("New") { load(Preset(name: "", model: "", systemPrompt: "", temperature: nil, topP: nil, maxTokens: nil)) }
                    Spacer()
                    if model.presets.contains(where: { $0.name == editing.name }) {
                        Button("Delete", role: .destructive) {
                            if let p = model.presets.first(where: { $0.name == editing.name }) { model.deletePreset(p) }
                        }
                    }
                    Button("Save") {
                        var p = editing
                        if !useTemperature { p.temperature = nil }
                        if !useMaxTokens { p.maxTokens = nil }
                        model.savePreset(p)
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(editing.name.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .formStyle(.grouped)
        }
        .task { await model.refreshPresets() }
    }

    private func load(_ p: Preset) {
        editing = p
        useTemperature = p.temperature != nil
        useMaxTokens = p.maxTokens != nil
    }
}

// ---- System: doctor, privacy/egress, storage, support, backup --------------------------------------------
private struct SystemSettings: View {
    @Environment(AppModel.self) private var model
    @State private var passphrase = ""
    @State private var showImporter = false

    var body: some View {
        Form {
            Section("Setup Doctor") {
                Button("Run Checks") { model.runDoctor() }
                    .disabled(!model.isRunning)
                if let report = model.doctorReport {
                    ForEach(report.checks) { check in
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: check.ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                                .foregroundStyle(check.ok ? .green : .orange)
                            VStack(alignment: .leading) {
                                Text(check.detail).font(.callout)
                                if !check.ok && !check.fix.isEmpty {
                                    Text(check.fix).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                } else {
                    Text("Checks this Mac, the engine, bundled models and installed harnesses.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Network Activity (recorded outbound calls)") {
                if model.egressEvents.isEmpty {
                    Text("Nothing recorded yet. Downloads, image fetches and peer calls appear here.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                ForEach(Array(model.egressEvents.prefix(10).enumerated()), id: \.offset) { _, event in
                    HStack {
                        Text(event.host).font(.system(.callout, design: .monospaced))
                        Spacer()
                        Text(event.purpose).font(.caption).foregroundStyle(.secondary)
                    }
                }
                HStack {
                    Button("Refresh") { model.refreshEgress() }
                    Button("Clear") { model.clearEgress() }
                }
                .disabled(!model.isRunning)
            }
            Section("Model Storage") {
                if let storage = model.storageReport {
                    LabeledContent("Models on disk", value: Format.memory(Int64(storage.totalBytes)))
                    LabeledContent("Partial downloads", value: Format.memory(Int64(storage.partialBytes)))
                    LabeledContent("Free space", value: Format.memory(Int64(storage.freeBytes)))
                    if storage.overQuota {
                        Text("Over the storage quota. Prune removes least-recently-used models first (never the loaded one).")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
                if let s = model.engineSettings {
                    Stepper("Storage quota: \(s.storageQuotaGb == 0 ? "off" : "\(s.storageQuotaGb) GB")",
                            value: Binding(get: { s.storageQuotaGb },
                                           set: { new in model.updateEngineSettings { $0.storageQuotaGb = new } }),
                            in: 0...2000, step: 10)
                }
                HStack {
                    Button("Refresh") { model.refreshStorage() }
                    Button("Prune to Quota") { model.pruneStorage() }
                    Button("Check for Updates") { model.checkModelUpdates() }
                }
                .disabled(!model.isRunning)
            }
            Section("Support & Backup") {
                Button("Save Support Bundle…") { model.saveSupportBundle() }
                    .disabled(!model.isRunning)
                SecureField("Backup passphrase (optional)", text: $passphrase)
                HStack {
                    Button("Export Backup…") { model.exportBackup(passphrase: passphrase) }
                    Button("Import Backup…") { showImporter = true }
                }
                .disabled(!model.isRunning)
                Text("Backups include settings, presets, hashed keys, projects and conversations. TLS private keys and peer credentials are never exported; restart the engine after importing.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Release") {
                Text("Signed releases: run `scripts/release.sh --check` in Terminal to see what this Mac still needs (Developer ID, notary profile, Sparkle keys).")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .onAppear { model.refreshStorage(); model.refreshEgress() }
        .fileImporter(isPresented: $showImporter,
                      allowedContentTypes: [UTType(filenameExtension: "bridlebackup") ?? .data]) { result in
            if case .success(let url) = result {
                model.importBackup(path: url.path, passphrase: passphrase)
            }
        }
    }
}

// ---- App: login item, menu bar, updates, crash recovery -----------------------------------------------------
private struct AppSettings: View {
    @Environment(AppModel.self) private var model
    @State private var launchAtLogin = LoginItem.isEnabled
    @State private var loginError: String?
    @State private var autoUpdate = Updater.shared.automaticallyChecks
    @AppStorage(AppAppearance.key) private var appearance = AppAppearance.system.rawValue

    var body: some View {
        @Bindable var model = model
        Form {
            Section("Appearance") {
                Picker("Appearance", selection: $appearance) {
                    ForEach(AppAppearance.allCases) { Text($0.title).tag($0.rawValue) }
                }
                .pickerStyle(.segmented)
                .onChange(of: appearance) { _, value in AppAppearance.apply(AppAppearance(rawValue: value) ?? .system) }
                Text("Changes only Bridle's windows, not your Mac's appearance.").font(.caption).foregroundStyle(.secondary)
            }
            Section("Startup") {
                Toggle("Launch Bridle at login", isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin) { _, on in
                        do {
                            try LoginItem.set(on)
                            loginError = nil
                        } catch {
                            loginError = "macOS refused: \(error.localizedDescription). This works for the installed Bridle.app, not `swift run`."
                            launchAtLogin = LoginItem.isEnabled
                        }
                    }
                if LoginItem.requiresApproval {
                    Button("Approve in System Settings…") { LoginItem.openSystemSettings() }
                }
                if let loginError { Text(loginError).font(.caption).foregroundStyle(.red) }
            }
            Section("Menu bar") {
                Toggle("Show Bridle in the menu bar", isOn: $model.showMenuBarIcon)
                Toggle("Keep serving when the window is closed", isOn: $model.keepRunningInMenuBar)
                    .disabled(!model.showMenuBarIcon)
            }
            Section("Reliability") {
                Toggle("Restart the engine automatically if it crashes", isOn: $model.autoRestartEngine)
                Text("Bridle saves a crash report, restarts the engine and reloads your model. It stops retrying after 3 crashes in 5 minutes.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Open Crash Reports") {
                    try? FileManager.default.createDirectory(at: CrashReporter.directory, withIntermediateDirectories: true)
                    NSWorkspace.shared.open(CrashReporter.directory)
                }
            }
            Section("Updates") {
                if Updater.shared.isAvailable {
                    Toggle("Check for updates automatically", isOn: $autoUpdate)
                        .onChange(of: autoUpdate) { _, on in Updater.shared.automaticallyChecks = on }
                    Button("Check Now") { Updater.shared.checkForUpdates() }
                } else {
                    Text("Updates are available in signed release builds (scripts/release.sh).")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
    }
}
