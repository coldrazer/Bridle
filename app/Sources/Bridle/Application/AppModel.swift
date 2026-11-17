import AppKit
import Foundation
import Observation

enum EngineState: Equatable {
    case locating
    case needsSetup(reason: String)
    case settingUp
    case starting
    case running(port: Int)
    case failed(String)
}

enum BottomPanel: String, CaseIterable, Identifiable {
    case chat = "Sandbox Chat"
    case requests = "Requests"
    case benchmarks = "Benchmarks"
    case devTools = "Dev Tools"
    var id: String { rawValue }
}

struct DevLogEntry: Identifiable, Equatable {
    let id: Int
    let date: Date
    let text: String
    let isError: Bool
}

struct CrashNotice: Equatable {
    let date: Date
    let exitStatus: Int32
    let report: URL?
    let restarting: Bool
}

struct PendingLoad: Equatable {
    let repo: String
    let draft: String?
    let reason: String
}

/// The app's single source of truth. All mutation happens on the main actor.
@MainActor
@Observable
final class AppModel {
    // Engine
    private(set) var engineState: EngineState = .locating
    private(set) var status: EngineStatus?
    private(set) var catalog: [CatalogModel] = []
    private(set) var engineSettings: EngineSettings?
    private(set) var lastError: String?
    private(set) var notice: String?
    private(set) var crashNotice: CrashNotice?
    var pendingForceLoad: PendingLoad?

    // Features
    private(set) var keys: [ApiKeyInfo] = []
    var createdKey: CreatedKey?
    private(set) var presets: [Preset] = []
    private(set) var requests: [RequestRow] = []
    private(set) var requestSummary: UsageSummary?
    private(set) var benchmarks: [BenchmarkResult] = []
    private(set) var isBenchmarking = false
    private(set) var compareJob: CompareJob?
    private(set) var peers: [Peer] = []
    private(set) var installingExtra: String?
    private(set) var doctorReport: DoctorReport?
    private(set) var scorecards: [ScorecardResult] = []
    private(set) var isScoring = false
    private(set) var storageReport: StorageReport?
    private(set) var egressEvents: [EgressEvent] = []
    var scorecardForLoaded: ScorecardResult? {
        guard let repo = loadedRepo else { return nil }
        return scorecards.first { $0.repo == repo }
    }
    var pendingExtraRetryRepo: String?
    private var extraRetryAttempted = Set<String>()

    // UI
    var showInspector = true
    var bottomPanel: BottomPanel? = nil
    var showModelPicker = false
    var showCompare = false
    var showStudio = false
    var showLocalImport = false
    var studioSection = "Projects"
    var peerNameDraft = ""
    var peerURLDraft = ""
    var peerModelDraft = ""
    /// Chosen harness. Selecting one (re)writes its Bridle-owned wiring files
    /// for the loaded model; Launch opens the harness through them.
    var selectedHarness: Harness = .deepseek {
        didSet {
            UserDefaults.standard.set(selectedHarness.rawValue, forKey: "selectedHarness")
            refreshHarnessWiring()
        }
    }
    private(set) var harnessWiring: HarnessWiring?
    private var harnessWiredFor: String?  // "<harness>|<port>|<repo>" the wiring was written for
    var harnessInstalled: Bool { HarnessSetup.detect(selectedHarness) != nil }
    var activeProjectID = UserDefaults.standard.string(forKey: "activeProjectID") ?? "personal"
    var focusedDownloadRepo: String?
    var selectedRepo: String? {
        didSet { UserDefaults.standard.set(selectedRepo, forKey: Keys.selectedRepo) }
    }

    // Chat
    private(set) var chat: [ChatTurn] = []
    private(set) var isGenerating = false
    var systemPrompt = ""
    var maxTokens = 1024
    var temperature = 0.7
    var chatPreset: String?            // preset name, nil = none
    var jsonOutput = false
    var pendingImages: [String] = []   // data: URLs attached to the next message

    // Dev Tools
    private(set) var devLog: [DevLogEntry] = []
    private var devLogCounter = 0

    // App preferences (applied on engine restart where noted)
    var shareOnNetwork: Bool { didSet { UserDefaults.standard.set(shareOnNetwork, forKey: Keys.share) } }
    var preferredPort: Int { didSet { UserDefaults.standard.set(preferredPort, forKey: Keys.port) } }
    /// Shared network API key. Kept in the Keychain, not UserDefaults (plain-text plist).
    var apiKey: String { didSet { KeychainStore.set(apiKey, for: Keys.apiKey) } }
    var showMenuBarIcon: Bool { didSet { UserDefaults.standard.set(showMenuBarIcon, forKey: Keys.menuBar) } }
    var keepRunningInMenuBar: Bool { didSet { UserDefaults.standard.set(keepRunningInMenuBar, forKey: Keys.keepRunning) } }
    var autoRestartEngine: Bool { didSet { UserDefaults.standard.set(autoRestartEngine, forKey: Keys.autoRestart) } }

    private let process = EngineProcess()
    private var client: EngineClient?
    private var controlToken = ""
    private var launchID = 0
    private var pollTask: Task<Void, Never>?
    private var slowPollTask: Task<Void, Never>?
    private var chatTask: Task<Void, Never>?
    private var compareTask: Task<Void, Never>?
    private var knownCompleted = Set<String>()
    private var retriedWithEphemeralPort = false
    private var crashTimes: [Date] = []
    private var powerConstrained = false
    private var lastServing: (repo: String, draft: String?)?

    private enum Keys {
        static let share = "shareOnNetwork", port = "port", apiKey = "apiKey", selectedRepo = "selectedRepo"
        static let menuBar = "showMenuBarIcon", keepRunning = "keepRunningInMenuBar", autoRestart = "autoRestartEngine"
    }

    init() {
        let d = UserDefaults.standard
        d.register(defaults: [Keys.menuBar: true, Keys.keepRunning: false, Keys.autoRestart: true, "followPowerConditions": true])
        shareOnNetwork = d.bool(forKey: Keys.share)
        let port = d.integer(forKey: Keys.port)
        preferredPort = port > 0 ? port : 49813
        if let legacy = d.string(forKey: Keys.apiKey) {
            // One-time migration from UserDefaults; the plist copy is removed only once the
            // Keychain has it, so a refused write never loses the key.
            apiKey = legacy
            if KeychainStore.set(legacy, for: Keys.apiKey) { d.removeObject(forKey: Keys.apiKey) }
        } else {
            apiKey = KeychainStore.string(for: Keys.apiKey) ?? ""
        }
        selectedRepo = d.string(forKey: Keys.selectedRepo)
        selectedHarness = Harness.fromPersisted(d.string(forKey: "selectedHarness"))
        showMenuBarIcon = d.bool(forKey: Keys.menuBar)
        keepRunningInMenuBar = d.bool(forKey: Keys.keepRunning)
        autoRestartEngine = d.bool(forKey: Keys.autoRestart)
        chat = ChatArchive.load(project: activeProjectID)
    }

    // ---- derived state ------------------------------------------------------------------
    var modelState: String { status?.model.state ?? "empty" }
    var loadedModel: LoadedModel? { status?.model.loaded }
    var loadedRepo: String? { loadedModel?.repo }
    var isModelReady: Bool { modelState == "ready" }
    var canChat: Bool { isModelReady || (status?.model.lastUnloaded != nil && status?.server.autoLoad == true) }
    var downloadedModels: [CatalogModel] { catalog.filter(\.downloaded) }
    var downloadedChatModels: [CatalogModel] { downloadedModels.filter(\.isChatModel) }
    var downloadedSpeechModels: [CatalogModel] { downloadedModels.filter { $0.kind == "speech" } }
    var activeDownloads: [DownloadJob] { status?.downloads.filter(\.isActive) ?? [] }
    var features: Features? { status?.features }
    var isRunning: Bool { if case .running = engineState { return true } else { return false } }

    var endpoint: String? {
        guard isRunning, let status else { return nil }
        return status.server.endpoint
    }

    var statusLabel: String {
        switch engineState {
        case .locating, .starting: return "Starting"
        case .needsSetup, .settingUp: return "Setup"
        case .failed: return "Error"
        case .running:
            switch modelState {
            case "loading": return "Loading"
            case "ready": return status?.model.metrics.active == true ? "Generating" : "Running"
            case "error": return "Error"
            default: return status?.model.unloadedReason == "idle" ? "Sleeping" : "Idle"
            }
        }
    }

    func download(for repo: String) -> DownloadJob? {
        status?.downloads.first { $0.repo == repo }
    }

    // ---- lifecycle ----------------------------------------------------------------------
    func bootstrap() {
        guard case .locating = engineState else { return }
        guard EngineLocator.engineDirectory() != nil else {
            engineState = .failed("Engine sources not found. Set BRIDLE_ENGINE_DIR or rebuild the app bundle.")
            return
        }
        if EngineLocator.isEngineInstalled {
            startEngine()
        } else {
            engineState = .needsSetup(reason: "The inference engine (Python + MLX) isn't installed yet.")
        }
    }

    func runSetup() {
        guard let dir = EngineLocator.engineDirectory() else { return }
        engineState = .settingUp
        bottomPanel = .devTools
        Task {
            do {
                try await EngineSetup.install(engineDirectory: dir) { line in
                    Task { @MainActor in self.appendLog(line, isError: false) }
                }
                startEngine()
            } catch {
                engineState = .needsSetup(reason: error.localizedDescription)
            }
        }
    }

    /// Installs an optional feature (vision, embeddings, speech) into the engine and restarts it.
    /// Versions come only from the engine's requirements files (single source of truth);
    /// installing the whole extras set is idempotent and already-fast when present.
    func installExtra(_ feature: String) {
        guard installingExtra == nil else { return }
        guard ["vision", "embeddings", "speech"].contains(feature),
              let dir = EngineLocator.engineDirectory() else { return }
        let lock = dir.appendingPathComponent("requirements-extras-lock.txt")
        let req = FileManager.default.fileExists(atPath: lock.path) ? lock
            : dir.appendingPathComponent("requirements-extras.txt")
        installingExtra = feature
        bottomPanel = .devTools
        appendLog("Installing \(feature) support from \(req.lastPathComponent)…", isError: false)
        Task {
            let status = await EngineSetup.run(EngineLocator.venvPython, ["-m", "pip", "install", "-r", req.path]) { line in
                Task { @MainActor in self.appendLog(line, isError: false) }
            }
            installingExtra = nil
            if status == 0 {
                notice = "Installed \(feature) support. Restarting the engine…"
                restartEngine()
            } else {
                report(EngineError(status: 0, message: "Installing \(feature) support failed; see Dev Tools."))
            }
        }
    }

    /// Existing installs get the full extras set once, automatically. Failure is
    /// recorded (so this never loops) and per-feature Install actions remain.
    func ensureAllExtrasOnce() {
        let flag = "extrasAutoInstallV1"
        guard !UserDefaults.standard.bool(forKey: flag), installingExtra == nil,
              let features else { return }
        let missing = [("vision", features.vision), ("embeddings", features.embeddings),
                       ("speech", features.speech)].filter { !$0.1 }.map(\.0)
        UserDefaults.standard.set(true, forKey: flag)
        guard !missing.isEmpty, let dir = EngineLocator.engineDirectory() else { return }
        installingExtra = "all"
        appendLog("Adding full support (vision, speech, embeddings) for this install…", isError: false)
        notice = "Adding vision, speech and embedding support — one-time, then restarting…"
        Task {
            let lock = dir.appendingPathComponent("requirements-extras-lock.txt")
            let req = FileManager.default.fileExists(atPath: lock.path) ? lock
                : dir.appendingPathComponent("requirements-extras.txt")
            let args: [String] = ["-m", "pip", "install", "-r", req.path]
            let status = await EngineSetup.run(EngineLocator.venvPython, args) { line in
                Task { @MainActor in self.appendLog(line, isError: false) }
            }
            installingExtra = nil
            if status == 0 {
                notice = "Full support installed. Restarting the engine…"
                restartEngine()
            } else {
                report(EngineError(status: 0, message: "Adding full support did not finish; "
                                   + "the Install buttons remain available when a feature needs it. See Dev Tools."))
            }
        }
    }

    func startEngine(port: Int? = nil, preload: (repo: String, draft: String?)? = nil) {
        guard let dir = EngineLocator.engineDirectory() else { return }
        pollTask?.cancel()
        slowPollTask?.cancel()
        launchID += 1
        let thisLaunch = launchID
        controlToken = UUID().uuidString + UUID().uuidString
        engineState = .starting
        let host = shareOnNetwork ? "0.0.0.0" : "127.0.0.1"
        let chosenPort = port ?? preferredPort
        appendLog("Starting engine on \(host):\(chosenPort)", isError: false)
        do {
            try process.start(python: EngineLocator.venvPython, engineDirectory: dir, host: host, port: chosenPort,
                              apiKey: apiKey, controlToken: controlToken, preload: preload) { [weak self] event in
                Task { @MainActor in self?.handle(event, launch: thisLaunch) }
            }
        } catch {
            engineState = .failed("Could not launch the engine: \(error.localizedDescription)")
        }
    }

    func restartEngine() {
        retriedWithEphemeralPort = false
        let serving = loadedRepo.map { ($0, loadedModel?.draft?.repo) }
        status = nil
        pollTask?.cancel()
        slowPollTask?.cancel()
        client = nil
        launchID += 1               // events from the process being replaced are now stale
        engineState = .starting
        let restartID = launchID
        let process = self.process
        // Wait for the old engine to exit (and free its port) before launching the new one;
        // otherwise the relaunch races the shutdown and falls back to a random port.
        Task {
            await Task.detached { process.stopAndWait() }.value
            guard restartID == self.launchID else { return }  // superseded by another restart/shutdown
            self.startEngine(preload: serving)
        }
    }

    func shutdown() {
        ChatArchive.save(chat, project: activeProjectID)
        pollTask?.cancel()
        slowPollTask?.cancel()
        chatTask?.cancel()
        launchID += 1
        process.stopAndWait()
    }

    private func handle(_ event: EngineProcess.Event, launch: Int) {
        guard launch == launchID else { return }  // stale event from a process we replaced
        switch event {
        case .ready(let ready):
            client = EngineClient(port: ready.port, controlToken: controlToken, apiKey: apiKey)
            engineState = .running(port: ready.port)
            appendLog("Engine \(ready.version) ready at \(ready.endpoint)", isError: false)
            startPolling()
            Task {
                await refreshCatalog()
                await refreshStatus()
                if let repo = pendingExtraRetryRepo {
                    pendingExtraRetryRepo = nil
                    choose(repo)
                } else {
                    ensureAllExtrasOnce()
                }
                if activeProjectID != "personal", let project = try? await featureRequest("GET", "/control/intelligence/projects/" + activeProjectID) {
                    systemPrompt = project["system_prompt"] as? String ?? ""
                    if let repo = project["model"] as? String, !repo.isEmpty { selectedRepo = repo }
                }
                await refreshEngineSettings()
                await refreshPresets()
                await refreshKeys()
                await refreshScorecards()
            }
        case .output(let line, let isError):
            appendLog(line, isError: isError)
        case .exited(let code, let beforeReady):
            pollTask?.cancel()
            slowPollTask?.cancel()
            client = nil
            status = nil
            if beforeReady && code == 2 && !retriedWithEphemeralPort {
                // Port in use: fall back to any free port once.
                retriedWithEphemeralPort = true
                appendLog("Port \(preferredPort) is busy; retrying on a free port", isError: true)
                startEngine(port: 0)
            } else if !beforeReady {
                recoverFromCrash(exitStatus: code)
            } else {
                engineState = .failed("The engine stopped while starting (exit code \(code)). See Dev Tools for details.")
            }
        }
    }

    /// The engine died while serving: save a report and restart it (with the same model),
    /// backing off and giving up after 3 crashes in 5 minutes.
    private func recoverFromCrash(exitStatus: Int32) {
        let recent = devLog.suffix(400).map(\.text)
        let report = CrashReporter.write(exitStatus: exitStatus, recentLog: recent, model: lastServing?.repo)
        let now = Date()
        crashTimes = crashTimes.filter { now.timeIntervalSince($0) < 300 } + [now]
        let canRestart = autoRestartEngine && crashTimes.count <= 3
        crashNotice = CrashNotice(date: now, exitStatus: exitStatus, report: report, restarting: canRestart)
        appendLog("Engine exited unexpectedly (status \(exitStatus)). Report: \(report?.path ?? "not written")", isError: true)
        guard canRestart else {
            engineState = .failed("The engine crashed \(crashTimes.count) times in 5 minutes (last exit code \(exitStatus)). "
                                  + "See the crash report, then restart it.")
            return
        }
        let delay = Double(crashTimes.count) * 2
        engineState = .starting
        let preload = lastServing
        let launch = launchID
        Task {
            try? await Task.sleep(for: .seconds(delay))
            guard launch == launchID else { return }  // a manual restart/shutdown superseded this
            startEngine(preload: preload)
        }
    }

    func dismissCrashNotice() { crashNotice = nil }

    func openCrashReport() {
        if let url = crashNotice?.report { NSWorkspace.shared.open(url) }
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshStatus()
                try? await Task.sleep(for: .seconds(1))
            }
        }
        slowPollTask?.cancel()
        slowPollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshPeers()
                if self?.bottomPanel == .requests { await self?.refreshRequests() }
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    func refreshStatus() async {
        guard let client else { return }
        do {
            let fresh = try await client.status()
            let processInfo = ProcessInfo.processInfo
            let constrained = processInfo.isLowPowerModeEnabled || processInfo.thermalState == .serious || processInfo.thermalState == .critical
            if constrained && !powerConstrained && UserDefaults.standard.bool(forKey: "followPowerConditions") {
                // Advise only: applying a profile persists engine settings, so it stays a manual choice.
                notice = "macOS reports reduced power or high thermal pressure. Switch to the Quiet profile in Projects & Tools → Power if you want slower, cooler inference."
            }
            powerConstrained = constrained
            if fresh != status { status = fresh }
            refreshHarnessWiring()  // cheap: stamp-guarded, rewrites only on harness/port/model change
            // Recovery on every load surface: a vision load that failed for the
            // missing runtime installs it once and retries this model.
            if fresh.model.state == "error", fresh.model.error.contains("vision support isn't installed"),
               let repo = selectedRepo, !extraRetryAttempted.contains(repo), installingExtra == nil {
                extraRetryAttempted.insert(repo)
                pendingExtraRetryRepo = repo
                installExtra("vision")
            }
            if let loaded = fresh.model.loaded { lastServing = (loaded.repo, loaded.draft?.repo) }
            let completed = Set(fresh.downloads.filter { $0.state == "completed" }.map(\.repo))
            if !completed.subtracting(knownCompleted).isEmpty {
                knownCompleted = completed
                await refreshCatalog()
            }
        } catch {
            // Transient while the engine is busy; the process monitor reports real exits.
        }
    }

    func refreshCatalog() async {
        guard let client else { return }
        do {
            catalog = try await client.catalog()
            if selectedRepo == nil { selectedRepo = downloadedChatModels.first?.repo }
        } catch {
            report(error)
        }
    }

    // ---- model actions ------------------------------------------------------------------
    /// Play button: load the selected model, or unload the running one.
    func togglePlay() {
        if isModelReady || modelState == "loading" {
            perform { try await $0.unload() }
            return
        }
        guard let repo = selectedRepo, catalog.contains(where: { $0.repo == repo && $0.downloaded }) else {
            showModelPicker = true
            return
        }
        requestLoad(repo, draft: nil)
    }

    func choose(_ repo: String, draft: String? = nil) {
        selectedRepo = repo
        if catalog.contains(where: { $0.repo == repo && $0.downloaded }) {
            requestLoad(repo, draft: draft)
        } else {
            startDownload(repo)
        }
    }

    /// Checks memory first; if the guard objects, asks before forcing the load.
    func requestLoad(_ repo: String, draft: String?) {
        guard let client else { return }
        Task {
            do {
                let check = try await client.checkFit(repo: repo, draft: draft)
                if check.verdict != "ok" && engineSettings?.memoryGuard != false {
                    pendingForceLoad = PendingLoad(repo: repo, draft: draft, reason: check.reason)
                    return
                }
                try await client.load(repo: repo, draft: draft, force: check.verdict != "ok")
                await refreshStatus()
            } catch {
                report(error)
            }
        }
    }

    func confirmForceLoad() {
        guard let pending = pendingForceLoad else { return }
        pendingForceLoad = nil
        perform { try await $0.load(repo: pending.repo, draft: pending.draft, force: true) }
    }

    func startDownload(_ repo: String) {
        let trimmed = repo.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        focusedDownloadRepo = trimmed
        perform { _ = try await $0.startDownload(repo: trimmed) }
    }

    func cancelDownload(_ job: DownloadJob) {
        perform { try await $0.cancelDownload(id: job.id) }
    }

    func deleteModel(_ repo: String) {
        perform { client in
            try await client.deleteModel(repo: repo)
            await self.refreshCatalog()
            if self.selectedRepo == repo { self.selectedRepo = nil }
        }
    }

    private func perform(_ action: @escaping @MainActor (EngineClient) async throws -> Void) {
        guard let client else { return }
        Task {
            do {
                try await action(client)
                await refreshStatus()
            } catch {
                report(error)
            }
        }
    }

    func report(_ error: Error) {
        lastError = error.localizedDescription
        appendLog("Error: \(error.localizedDescription)", isError: true)
    }

    func dismissError() { lastError = nil }
    func dismissNotice() { notice = nil }

    // ---- engine settings ------------------------------------------------------------------
    func refreshEngineSettings() async {
        guard let client else { return }
        engineSettings = try? await client.settings()
    }

    func updateEngineSettings(_ change: (inout EngineSettings) -> Void) {
        guard var next = engineSettings else { return }
        change(&next)
        engineSettings = next
        perform { client in
            let result = try await client.updateSettings(next)
            self.engineSettings = result.settings
            if !result.restartRequired.isEmpty {
                self.notice = "Restart the engine to apply: \(result.restartRequired.joined(separator: ", "))."
            }
        }
    }

    // ---- API keys ----------------------------------------------------------------------------
    func refreshKeys() async {
        guard let client else { return }
        if let list = try? await client.keys() { keys = list }
    }

    func createKey(name: String, rpmLimit: Int?, dailyTokenLimit: Int?) {
        perform { client in
            self.createdKey = try await client.createKey(name: name, rpmLimit: rpmLimit, dailyTokenLimit: dailyTokenLimit)
            await self.refreshKeys()
        }
    }

    func revokeKey(_ key: ApiKeyInfo) {
        perform { client in
            try await client.revokeKey(id: key.id)
            await self.refreshKeys()
        }
    }

    func deleteKey(_ key: ApiKeyInfo) {
        perform { client in
            try await client.deleteKey(id: key.id)
            await self.refreshKeys()
        }
    }

    // ---- presets ---------------------------------------------------------------------------------
    func refreshPresets() async {
        guard let client else { return }
        if let list = try? await client.presets() { presets = list }
    }

    func savePreset(_ preset: Preset) {
        perform { client in
            try await client.savePreset(preset)
            await self.refreshPresets()
        }
    }

    func deletePreset(_ preset: Preset) {
        perform { client in
            try await client.deletePreset(name: preset.name)
            if self.chatPreset == preset.name { self.chatPreset = nil }
            await self.refreshPresets()
        }
    }

    // ---- request history ------------------------------------------------------------------------
    func refreshRequests() async {
        guard let client else { return }
        if let list = try? await client.requests() {
            requests = list.requests
            requestSummary = list.summary
        }
    }

    func clearRequests() {
        perform { client in
            try await client.clearRequests()
            await self.refreshRequests()
        }
    }

    // ---- benchmarks ------------------------------------------------------------------------------
    func refreshBenchmarks() async {
        guard let client else { return }
        if let list = try? await client.benchmarks() { benchmarks = list }
    }

    func runBenchmark(promptTokens: Int, generationTokens: Int) {
        guard let client, !isBenchmarking else { return }
        isBenchmarking = true
        Task {
            defer { isBenchmarking = false }
            do {
                _ = try await client.runBenchmark(promptTokens: promptTokens, generationTokens: generationTokens)
                await refreshBenchmarks()
            } catch {
                report(error)
            }
        }
    }

    func clearBenchmarks() {
        perform { client in
            try await client.clearBenchmarks()
            await self.refreshBenchmarks()
        }
    }

    // ---- compare -----------------------------------------------------------------------------------
    func startCompare(models: [String], prompt: String, system: String, maxTokens: Int, temperature: Double) {
        guard let client else { return }
        compareTask?.cancel()
        compareTask = Task {
            do {
                var job = try await client.startCompare(models: models, prompt: prompt, system: system,
                                                         maxTokens: maxTokens, temperature: temperature)
                compareJob = job
                while job.state == "running" && !Task.isCancelled {
                    try await Task.sleep(for: .milliseconds(700))
                    job = try await client.compareJob(id: job.id)
                    compareJob = job
                }
            } catch is CancellationError {
            } catch {
                report(error)
            }
        }
    }

    func cancelCompare() {
        guard let job = compareJob, job.state == "running" else { return }
        perform { try await $0.cancelCompare(id: job.id) }
    }

    // ---- network peers ------------------------------------------------------------------------------
    func refreshPeers() async {
        guard let client else { return }
        if let list = try? await client.peers() { peers = list.peers }
    }

    // ---- chat ---------------------------------------------------------------------------------------
    func attachImage(from url: URL) {
        guard let data = try? Data(contentsOf: url), data.count <= 20 * 1024 * 1024 else {
            report(EngineError(status: 0, message: "Images must be 20 MB or smaller."))
            return
        }
        let ext = url.pathExtension.lowercased()
        let mime = ext == "jpg" || ext == "jpeg" ? "image/jpeg" : (ext == "gif" ? "image/gif" : (ext == "webp" ? "image/webp" : "image/png"))
        pendingImages.append("data:\(mime);base64,\(data.base64EncodedString())")
    }

    func transcribe(fileURL: URL, model: String) async -> String? {
        guard let client else { return nil }
        do {
            return try await client.transcribe(fileURL: fileURL, model: model)
        } catch {
            report(error)
            return nil
        }
    }

    /// Aria's voice input: downloaded speech assistant first, bundled Onyx fallback.
    func transcribeForAria(fileURL: URL) async throws -> (text: String, source: String) {
        guard let client else { throw EngineError(status: 503, message: "Start the engine first.") }
        return try await client.transcribeForAria(fileURL: fileURL)
    }

    func send(_ text: String) {
        let prompt = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty || !pendingImages.isEmpty, !isGenerating, let client, canChat else { return }
        chat.append(ChatTurn(role: .user, text: prompt, images: pendingImages))
        pendingImages = []
        var history = ChatHistory.windowed(chat.filter { !$0.failed },
                                           contextLength: loadedModel?.contextLength, maxTokens: maxTokens)
        if !systemPrompt.isEmpty && chatPreset == nil {
            history.insert(ChatTurn(role: .system, text: systemPrompt), at: 0)
        }
        let reply = ChatTurn(role: .assistant, text: "")
        chat.append(reply)
        isGenerating = true
        let started = Date()
        var options = ChatOptions(model: chatPreset.map { "preset:\($0)" }, maxTokens: maxTokens,
                                  temperature: temperature, jsonOutput: jsonOutput)
        if chatPreset != nil {  // let the preset supply its own defaults
            options.maxTokens = nil
            options.temperature = nil
        }
        chatTask = Task {
            do {
                for try await event in client.streamChat(messages: history, options: options) {
                    switch event {
                    case .delta(let piece):
                        updateTurn(reply.id) { $0.text += piece }
                    case .reasoning(let piece):
                        updateTurn(reply.id) { $0.reasoning += piece }
                    case .toolCalls(let calls):
                        updateTurn(reply.id) { $0.toolCalls += calls }
                    case .usage(let u):
                        let elapsed = Date().timeIntervalSince(started)
                        var parts = [String(format: "%d tokens · %.1f tok/s · %.1fs", u.completionTokens, u.generationTps, elapsed)]
                        if u.cachedTokens > 0 { parts.append("\(u.cachedTokens) cached") }
                        if u.draftAccepted > 0 { parts.append("\(u.draftAccepted) drafted") }
                        if u.batched { parts.append("batched") }
                        updateTurn(reply.id) { $0.footnote = parts.joined(separator: " · ") }
                    case .failure(let message):
                        updateTurn(reply.id) {
                            $0.text += $0.text.isEmpty ? message : "\n\n\(message)"
                            $0.failed = true
                        }
                    case .finished, .done:
                        break
                    }
                }
                if Task.isCancelled { updateTurn(reply.id) { $0.footnote = "stopped" } }
            } catch let urlError as URLError where urlError.code == .cancelled {
                updateTurn(reply.id) { $0.footnote = "stopped" }
            } catch is CancellationError {
                updateTurn(reply.id) { $0.footnote = "stopped" }
            } catch {
                updateTurn(reply.id) {
                    $0.text = error.localizedDescription
                    $0.failed = true
                }
            }
            isGenerating = false
            ChatArchive.save(chat, project: activeProjectID)
        }
    }

    private func updateTurn(_ id: UUID, _ change: (inout ChatTurn) -> Void) {
        guard let i = chat.firstIndex(where: { $0.id == id }) else { return }
        change(&chat[i])
    }

    func stopGenerating() {
        chatTask?.cancel()
    }

    func clearChat() {
        stopGenerating()
        chat.removeAll()
        pendingImages.removeAll()
        ChatArchive.save(chat, project: activeProjectID)
    }

    // ---- utilities ----------------------------------------------------------------------
    func appendLog(_ text: String, isError: Bool) {
        devLogCounter += 1
        devLog.append(DevLogEntry(id: devLogCounter, date: Date(), text: text, isError: isError))
        if devLog.count > 3000 { devLog.removeFirst(devLog.count - 3000) }
    }

    func clearLog() { devLog.removeAll() }

    func copyToPasteboard(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    func copyEndpoint() {
        if let endpoint { copyToPasteboard(endpoint + "/v1") }
    }

    // ---- harness wiring ---------------------------------------------------------------------
    /// (Re)writes the selected harness's Bridle-owned wiring for the loaded
    /// model on the actually-bound port. No-ops until a model is serving.
    func refreshHarnessWiring() {
        // After an idle unload the last model will auto-load on first request,
        // so wiring for it stays valid and the picker stays usable.
        let repo = loadedRepo ?? status?.model.lastUnloaded
        guard case .running(let port) = engineState, let repo else {
            harnessWiring = nil
            harnessWiredFor = nil
            return
        }
        let stamp = "\(selectedHarness.rawValue)|\(port)|\(repo)"
        guard stamp != harnessWiredFor else { return }
        do {
            harnessWiring = try HarnessSetup.writeWiring(
                harness: selectedHarness, port: port, model: repo,
                token: apiKey.isEmpty ? "bridle-local" : apiKey,
                contextTokens: loadedModel?.contextLength)
            harnessWiredFor = stamp
            appendLog("\(selectedHarness.displayName) wiring written for \(repo) at http://127.0.0.1:\(port)",
                      isError: false)
        } catch {
            harnessWiring = nil
            harnessWiredFor = nil
            appendLog("Could not write harness wiring: \(error.localizedDescription)", isError: true)
        }
    }

    /// Opens the generated .command file (Terminal), running the harness
    /// through Bridle's wiring.
    func launchHarness() {
        refreshHarnessWiring()
        guard let wiring = harnessWiring else { return }
        NSWorkspace.shared.open(wiring.launchFile)
    }

    func copyHarnessLaunchCommand() {
        refreshHarnessWiring()
        if let wiring = harnessWiring { copyToPasteboard(wiring.launchCommand) }
    }

    func revealHarnessWiring() {
        refreshHarnessWiring()
        guard let wiring = harnessWiring else { return }
        NSWorkspace.shared.activateFileViewerSelecting([wiring.launchFile])
    }

    // ---- production: doctor, scorecards, storage, egress, support, backup ---------------------
    func runDoctor() {
        guard let client else { return }
        Task {
            do { doctorReport = try await client.doctor() }
            catch { report(error) }
        }
    }

    func runScorecard() {
        guard let client, !isScoring else { return }
        isScoring = true
        Task {
            defer { isScoring = false }
            do {
                let result = try await client.runScorecard()
                scorecards = [result] + scorecards.filter { $0.repo != result.repo }
                notice = result.harnessReady
                    ? "\(Format.shortName(result.repo)) passed all \(result.total) scorecard checks — harness ready."
                    : "\(Format.shortName(result.repo)) passed \(result.passed)/\(result.total) scorecard checks; harness use may be unreliable."
            } catch { report(error) }
        }
    }

    func refreshScorecards() async {
        guard let client else { return }
        if let rows = try? await client.scorecards() { scorecards = rows }
    }

    func refreshStorage() {
        guard let client else { return }
        Task {
            do { storageReport = try await client.storage() }
            catch { report(error) }
        }
    }

    func pruneStorage() {
        guard let client else { return }
        Task {
            do {
                let result = try await client.pruneStorage()
                notice = result.pruned.isEmpty
                    ? (result.note ?? "Nothing to prune.")
                    : "Pruned \(result.pruned.count) model(s): \(result.pruned.joined(separator: ", "))"
                storageReport = try await client.storage()
                await refreshCatalog()
            } catch { report(error) }
        }
    }

    func checkModelUpdates() {
        guard let client else { return }
        Task {
            do {
                let report = try await client.checkModelUpdates()
                let stale = report.models.filter(\.updateAvailable)
                notice = stale.isEmpty
                    ? "All downloaded models match their pinned revision."
                    : "Updates available for: \(stale.map(\.repo).joined(separator: ", ")). Re-download to update."
            } catch { report(error) }
        }
    }

    func refreshEgress() {
        guard let client else { return }
        Task {
            do { egressEvents = try await client.egress().events }
            catch { report(error) }
        }
    }

    func clearEgress() {
        guard let client else { return }
        Task {
            do { try await client.clearEgress(); egressEvents = [] }
            catch { report(error) }
        }
    }

    func saveSupportBundle() {
        guard let client else { return }
        Task {
            do {
                let result = try await client.supportBundle()
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: result.path)])
            } catch { report(error) }
        }
    }

    func exportBackup(passphrase: String) {
        guard let client else { return }
        Task {
            do {
                let result = try await client.exportBackup(passphrase: passphrase)
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: result.path)])
            } catch { report(error) }
        }
    }

    func importBackup(path: String, passphrase: String) {
        guard let client else { return }
        Task {
            do {
                let result = try await client.importBackup(path: path, passphrase: passphrase)
                notice = "Restored \(result.restored.count) file(s). Restart the engine to apply."
            } catch { report(error) }
        }
    }

    func revealModelsFolder() {
        let url = EngineLocator.modelsDirectory
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }
    func featureRequest(_ method: String, _ path: String, body: [String: Any]? = nil) async throws -> [String: Any] {
        guard let client else { throw EngineError(status: 503, message: "Start the engine first.") }
        let data = try await client.featureRequest(method, path, body: body)
        return (try JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
    }

    func switchProject(_ id: String) {
        guard !isGenerating else { return }
        ChatArchive.save(chat, project: activeProjectID)
        activeProjectID = id
        UserDefaults.standard.set(id, forKey: "activeProjectID")
        chat = ChatArchive.load(project: id)
        pendingImages.removeAll()
    }

}
