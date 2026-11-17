import SwiftUI

struct InspectorView: View {
    @Environment(AppModel.self) private var model
    @State private var showRDMAHelp = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                server
                topology
                hardware
                modelSection
                scorecard
                stats
            }
            .padding(16)
        }
    }

    // ---- sections -----------------------------------------------------------------------
    private var server: some View {
        InspectorSection("Server") {
            InspectorRow("Status") { StatusDot(label: model.statusLabel) }
            InspectorRow("Mode", model.status?.server.mode.capitalized ?? "–")
            InspectorRow("Uptime", model.status.map { Format.duration($0.server.uptimeSeconds) } ?? "–")
            InspectorRow("API Endpoint") {
                Text(model.endpoint.map { $0 + "/v1" } ?? "–")
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                    .multilineTextAlignment(.trailing)
            }
            InspectorRow("Network", model.shareOnNetwork ? "Shared on LAN" : "This Mac only")
            if let https = model.status?.server.httpsEndpoint {
                InspectorRow("HTTPS") {
                    Text(https + "/v1")
                        .font(.system(.callout, design: .monospaced))
                        .textSelection(.enabled)
                }
                if let fp = model.status?.server.tlsFingerprint {
                    Button("Copy Certificate Fingerprint") { model.copyToPasteboard(fp) }
                        .buttonStyle(.link)
                        .font(.caption)
                        .help(fp)
                }
            }
            if model.status?.server.ollamaApi == true {
                InspectorRow("Ollama API") {
                    Text(model.endpoint ?? "–")
                        .font(.system(.callout, design: .monospaced))
                        .textSelection(.enabled)
                }
            }
            InspectorRow("Auth", model.status?.server.apiKeyRequired == true ? "API key required" : "Open")
            Button("Copy API") { model.copyEndpoint() }
                .frame(maxWidth: .infinity)
                .disabled(model.endpoint == nil)
        }
    }

    private var topology: some View {
        InspectorSection("Topology") {
            InspectorRow("Configuration", "Single node")
            InspectorRow("Bonjour", model.status?.server.bonjour == true ? "Advertising" : "Off")
            InspectorRow("Bridle peers", "\(model.peers.count)")
            ForEach(model.peers) { peer in
                HStack {
                    Image(systemName: "network").foregroundStyle(.secondary)
                    VStack(alignment: .leading) {
                        Text(peer.name).font(.callout)
                        Text([peer.chip, peer.model.map(Format.shortName)].compactMap { $0 }.joined(separator: " · "))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private var hardware: some View {
        InspectorSection("Hardware") {
            let machine = model.status?.machine
            InspectorRow("Thunderbolt", thunderboltText(machine))
            InspectorRow("RDMA") {
                Text(rdmaText(machine?.rdma))
                    .foregroundStyle(machine?.rdma == "enabled" ? Color.green : Color.secondary)
            }
            if machine?.canCluster == true && machine?.rdma != "enabled" {
                Button("How to enable RDMA…") { showRDMAHelp = true }
                    .buttonStyle(.link)
                    .font(.callout)
                    .popover(isPresented: $showRDMAHelp, arrowEdge: .leading) { RDMAHelp() }
            }
            InspectorRow("OS", machine?.osVersion ?? "–")
        }
    }

    private var modelSection: some View {
        InspectorSection("Model") {
            let runtime = model.status?.model
            if let loading = runtime?.loadingRepo, !loading.isEmpty {
                InspectorRow("Loading") {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text(Format.shortName(loading)).lineLimit(1)
                    }
                }
            }
            InspectorRow("Loaded", runtime?.loaded.map { Format.shortName($0.repo) } ?? "–")
            if let loaded = runtime?.loaded {
                InspectorRow("Type", loaded.kind == "vision" ? "Vision + text" : "Text")
                if let draft = loaded.draft {
                    InspectorRow("Draft", Format.shortName(draft.repo))
                }
                InspectorRow("Weights", Format.bytes(loaded.sizeBytes + (loaded.draft?.sizeBytes ?? 0)))
                InspectorRow("Context", loaded.contextLength.map { "\($0.formatted()) tokens" } ?? "–")
                InspectorRow("Load time", String(format: "%.1f s", loaded.loadSeconds))
                HStack(spacing: 6) {
                    if loaded.supportsTools { CapabilityTag(text: "Tools") }
                    if loaded.supportsReasoning { CapabilityTag(text: "Reasoning") }
                    if model.features?.structuredOutput == true { CapabilityTag(text: "JSON") }
                }
            } else if runtime?.unloadedReason == "idle", let last = runtime?.lastUnloaded {
                Text("\(Format.shortName(last)) was unloaded after being idle. It reloads on the next request.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            let aux = model.downloadedModels.filter { !$0.isChatModel }
            if !aux.isEmpty {
                InspectorRow("Aux models", "\(aux.count) (embeddings/speech)")
            }
            if let error = runtime?.error, !error.isEmpty {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
            Button("Switch Model") { model.showModelPicker = true }
                .frame(maxWidth: .infinity)
                .disabled(model.status == nil)
        }
    }

    private var scorecard: some View {
        InspectorSection("Model Scorecard") {
            if let card = model.scorecardForLoaded {
                InspectorRow("Result", "\(card.passed)/\(card.total) passed")
                InspectorRow("Harness ready", card.harnessReady ? "Yes" : "Not yet")
                ForEach(card.tasks) { task in
                    HStack(spacing: 6) {
                        Image(systemName: task.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .foregroundStyle(task.ok ? .green : .red)
                        VStack(alignment: .leading) {
                            Text(task.name).font(.callout)
                            Text(task.detail).font(.caption).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            } else {
                Text("Four quick checks (instructions, JSON, tool calling, reasoning) on the loaded model — run one before pointing a harness at it.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Button(model.isScoring ? "Scoring…" : "Run Scorecard") { model.runScorecard() }
                .frame(maxWidth: .infinity)
                .disabled(!model.isModelReady || model.isScoring)
        }
    }

    private var stats: some View {
        InspectorSection("Stats") {
            let s = model.status
            InspectorRow("Memory Used", s.map { Format.memory($0.stats.memoryUsedBytes) } ?? "–")
            InspectorRow("Model Memory", s?.stats.mlx.map { Format.memory($0.activeBytes) } ?? "–")
            InspectorRow("Last tok/s", s.map { String(format: "%.1f", $0.model.metrics.lastGenerationTps) } ?? "–")
            InspectorRow("Requests", s.map { "\($0.model.metrics.requestsTotal)" } ?? "–")
            InspectorRow("Tokens Out", s.map { $0.model.metrics.tokensGenerated.formatted() } ?? "–")
            InspectorRow("Queue", s.map { "\($0.model.metrics.queueDepth)" } ?? "–")
            InspectorRow("In flight", s.map { "\($0.model.metrics.activeRequests)" } ?? "–")
            InspectorRow("Batched", s.map { $0.model.metrics.batchedRequests.formatted() } ?? "–")
            InspectorRow("Cached prompt", s.map { "\($0.model.metrics.cachedPromptTokens.formatted()) tok" } ?? "–")
            InspectorRow("Drafted", s.map { "\($0.model.metrics.draftAcceptedTokens.formatted()) tok" } ?? "–")
            if let usage = s?.usage {
                InspectorRow("Last 24 h", "\(usage.requests) req · \(usage.errors) errors")
            }
        }
    }

    private func thunderboltText(_ m: MachineInfo?) -> String {
        guard let m else { return "–" }
        let gen = m.thunderboltGeneration.map { "TB\($0)" } ?? "Unknown"
        if let ports = m.thunderboltPorts { return "\(gen) · \(ports) ports" }
        return gen
    }

    private func rdmaText(_ value: String?) -> String {
        switch value {
        case "enabled": return "Enabled"
        case "disabled": return "Not Enabled"
        case "unavailable": return "Unavailable"
        case nil: return "–"
        default: return "Unknown"
        }
    }
}

private struct CapabilityTag: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(.quaternary, in: Capsule())
    }
}

private struct RDMAHelp: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Enable RDMA over Thunderbolt").font(.headline)
            Text("RDMA can only be switched on from macOS Recovery (macOS 26.2 or later, Thunderbolt 5 Macs):")
            Text("1. Shut down, then press and hold the power button until startup options appear.\n2. Choose Options → Continue, then Utilities → Terminal.\n3. Run  rdma_ctl enable  and restart.\n4. Repeat on every Mac in the cluster.")
                .font(.callout)
            Text("Needed for multi-Mac clustering (phase 2). Single-Mac inference doesn't use it.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding()
        .frame(width: 340)
    }
}

struct InspectorSection<Content: View>: View {
    let title: String
    let content: Content

    init(_ title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.headline)
            content
        }
    }
}

struct InspectorRow<Value: View>: View {
    let label: String
    let value: Value

    init(_ label: String, @ViewBuilder value: () -> Value) {
        self.label = label
        self.value = value()
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).foregroundStyle(.secondary)
            Spacer(minLength: 12)
            value
        }
        .font(.callout)
    }
}

extension InspectorRow where Value == Text {
    init(_ label: String, _ text: String) {
        self.init(label) { Text(text) }
    }
}
