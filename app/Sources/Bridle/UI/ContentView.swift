import SwiftUI

struct AdvancedCanvasView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        VStack(spacing: 0) {
            if let crash = model.crashNotice {
                Banner(icon: "exclamationmark.octagon.fill", tint: .red,
                       text: crash.restarting
                           ? "The engine stopped unexpectedly (exit \(crash.exitStatus)) and is restarting with your model."
                           : "The engine stopped unexpectedly (exit \(crash.exitStatus)).") {
                    if crash.report != nil { Button("View Report") { model.openCrashReport() } }
                    Button("Dismiss") { model.dismissCrashNotice() }
                }
            }
            if let notice = model.notice {
                Banner(icon: "info.circle.fill", tint: .accentColor, text: notice) {
                    if notice.contains("Restart the engine") { Button("Restart Now") { model.restartEngine(); model.dismissNotice() } }
                    Button("Dismiss") { model.dismissNotice() }
                }
            }
            VSplitView {
                CanvasView()
                    .frame(minHeight: 280, maxHeight: .infinity)
                    .overlay {
                        switch model.engineState {
                        case .needsSetup, .settingUp, .failed:
                            EngineSetupOverlay()
                        default:
                            EmptyView()
                        }
                    }
                if let panel = model.bottomPanel {
                    BottomPanelView(panel: panel)
                        .frame(minHeight: 180, idealHeight: 280, maxHeight: .infinity)
                }
            }
            StatusBar()
        }
        .inspector(isPresented: $model.showInspector) {
            InspectorView()
                .inspectorColumnWidth(min: 250, ideal: 280, max: 380)
        }
        .toolbar {
            ToolbarItem(placement: .navigation) {
                BridleMark()
                    .frame(width: 34, height: 22)
                    .opacity(0.85)
                    .help("Bridle")
            }
            ToolbarItem(placement: .principal) {
                ModelBar()
            }
            ToolbarItem(placement: .primaryAction) {
                PlayButton()
            }
            ToolbarItem(placement: .primaryAction) {
                HarnessButton()
            }
            ToolbarItem(placement: .primaryAction) {
                Button {
                    model.showCompare = true
                } label: {
                    Image(systemName: "rectangle.split.2x1")
                }
                .help("Compare models side by side")
                .disabled(!model.isRunning)
            }
            ToolbarItem(placement: .primaryAction) {
                Button {
                    model.showInspector.toggle()
                } label: {
                    Image(systemName: "sidebar.right")
                }
                .help("Toggle inspector")
            }
        }
        .sheet(isPresented: $model.showCompare) {
            CompareView()
                .environment(model)
        }
        .alert("Load anyway?", isPresented: Binding(
            get: { model.pendingForceLoad != nil },
            set: { if !$0 { model.pendingForceLoad = nil } }
        )) {
            Button("Load Anyway", role: .destructive) { model.confirmForceLoad() }
            Button("Cancel", role: .cancel) { model.pendingForceLoad = nil }
        } message: {
            Text("\(Format.shortName(model.pendingForceLoad?.repo ?? "")) \(model.pendingForceLoad?.reason ?? ""). Loading it may make your Mac swap heavily or fail.")
        }
        .sheet(isPresented: Binding(
            get: { model.focusedDownloadRepo != nil && !model.showModelPicker },
            set: { if !$0 { model.focusedDownloadRepo = nil } }
        )) {
            DownloadSheet()
                .environment(model)
        }
        .alert("Something went wrong", isPresented: Binding(
            get: { model.lastError != nil },
            set: { if !$0 { model.dismissError() } }
        )) {
            Button("OK") { model.dismissError() }
        } message: {
            Text(model.lastError ?? "")
        }
    }
}

/// The capsule in the middle of the toolbar: model picker · status · endpoint.
struct ModelBar: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 10) {
            Menu {
                if model.downloadedModels.isEmpty {
                    Text("No models downloaded yet")
                } else {
                    ForEach(model.downloadedModels) { entry in
                        Button {
                            model.choose(entry.repo)
                        } label: {
                            if entry.repo == model.loadedRepo {
                                Label(entry.name, systemImage: "checkmark")
                            } else {
                                Text(entry.name)
                            }
                        }
                    }
                }
                Divider()
                Button("Browse Models…") { model.showModelPicker = true }
            } label: {
                Text(currentTitle)
                    .fontWeight(.medium)
                    .lineLimit(1)
            }
            .menuStyle(.borderlessButton)
            .fixedSize()

            Divider().frame(height: 16)

            StatusDot(label: model.statusLabel)

            if let endpoint = model.endpoint {
                Text(endpoint)
                    .font(.system(.callout, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Button {
                    model.copyEndpoint()
                } label: {
                    Image(systemName: "doc.on.doc")
                }
                .buttonStyle(.borderless)
                .help("Copy API base URL")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 5)
        .frame(minWidth: 420)
        .background(.quaternary.opacity(0.6), in: Capsule())
    }

    private var currentTitle: String {
        if let loading = model.status?.model.loadingRepo, !loading.isEmpty { return Format.shortName(loading) }
        if let repo = model.loadedRepo ?? model.selectedRepo { return Format.shortName(repo) }
        return "Select a model"
    }
}

struct StatusDot: View {
    let label: String

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
                .overlay(Circle().stroke(.secondary.opacity(label == "Idle" ? 0.6 : 0), lineWidth: 1))
            Text(label)
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    private var color: Color {
        switch label {
        case "Running", "Generating": return .green
        case "Loading", "Starting", "Setup": return .orange
        case "Sleeping": return .blue
        case "Error": return .red
        default: return .clear
        }
    }
}

struct PlayButton: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let serving = model.isModelReady || model.modelState == "loading"
        Button {
            model.togglePlay()
        } label: {
            Image(systemName: serving ? "stop.fill" : "play.fill")
        }
        .help(serving ? "Unload model" : "Load the selected model and serve it")
        .disabled({
            if case .running = model.engineState { return false }
            return true
        }())
    }
}

/// Picks the harness that drives the loaded model. Selecting one writes its
/// Bridle-owned wiring files; Launch opens the harness through them. Locked
/// until a model is ready.
struct HarnessButton: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Menu {
            Section("Harness") {
                ForEach(Harness.allCases) { option in
                    Button {
                        model.selectedHarness = option
                    } label: {
                        if option == model.selectedHarness {
                            Label(option.displayName, systemImage: "checkmark")
                        } else {
                            Text(option.displayName)
                        }
                    }
                }
            }
            Divider()
            Section(model.selectedHarness.displayName) {
                if model.harnessInstalled {
                    Button {
                        model.launchHarness()
                    } label: {
                        Label("Launch \(model.selectedHarness.displayName)…", systemImage: "play")
                    }
                    Button("Copy Launch Command") { model.copyHarnessLaunchCommand() }
                    Button("Reveal Wiring in Finder") { model.revealHarnessWiring() }
                } else {
                    Text("Not installed. \(model.selectedHarness.installHint)")
                }
            }
            if let wiring = model.harnessWiring {
                Section {
                    Text("Wired: \(Format.shortName(wiring.model)) at \(wiring.endpoint)")
                }
            }
        } label: {
            Label("Harness", systemImage: "curlybraces")
        }
        .help(model.canChat ? "Harness: \(model.selectedHarness.displayName). \(model.selectedHarness.wiringSummary)"
                                 : "Load a model to unlock the Harness picker")
        .disabled(!model.canChat)
        .accessibilityLabel("Harness")
        .accessibilityValue(model.canChat ? model.selectedHarness.displayName : "Locked until a model is loaded")
    }
}

struct StatusBar: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 14) {
            Button {
                model.bottomPanel = model.bottomPanel == .devTools ? nil : .devTools
            } label: {
                Label("Dev Tools", systemImage: "hammer")
                    .fontWeight(.medium)
            }
            .buttonStyle(.borderless)

            Button {
                model.bottomPanel = model.bottomPanel == .chat ? nil : .chat
            } label: {
                Label("Sandbox Chat", systemImage: "bubble.left.and.text.bubble.right")
            }
            .buttonStyle(.borderless)

            Button {
                model.bottomPanel = model.bottomPanel == .requests ? nil : .requests
            } label: {
                Label("Requests", systemImage: "list.bullet.rectangle")
            }
            .buttonStyle(.borderless)

            if let usage = model.status?.usage, usage.requests > 0 {
                Text("\(usage.requests) requests today · \(usage.completionTokens.formatted()) tokens")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            if let job = model.activeDownloads.first {
                Button {
                    model.focusedDownloadRepo = job.repo
                } label: {
                    HStack(spacing: 6) {
                        ProgressView(value: job.fraction)
                            .frame(width: 80)
                        Text("\(Format.shortName(job.repo)) \(Format.percent(job.fraction))")
                            .font(.caption)
                    }
                }
                .buttonStyle(.borderless)
            }

            Button {
                model.bottomPanel = model.bottomPanel == nil ? .chat : nil
            } label: {
                Image(systemName: model.bottomPanel == nil ? "rectangle.bottomthird.inset.filled" : "rectangle")
            }
            .buttonStyle(.borderless)
            .help("Toggle bottom panel")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
        .background(.bar)
        .overlay(alignment: .top) { Divider() }
    }
}


/// A thin strip across the top of the window for crash and restart notices.
struct Banner<Actions: View>: View {
    let icon: String
    let tint: Color
    let text: String
    @ViewBuilder let actions: () -> Actions

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon).foregroundStyle(tint)
            Text(text).font(.callout)
            Spacer()
            actions()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(tint.opacity(0.12))
        .overlay(alignment: .bottom) { Divider() }
    }
}
