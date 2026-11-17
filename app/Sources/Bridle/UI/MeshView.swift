import SwiftUI

/// Mesh Mode: pool the unified memory of several Macs over Thunderbolt 5 RDMA and run one model
/// across them. This Mac coordinates (rank 0) and serves the pooled model through Bridle's API;
/// the other Macs are approved peers whose device keys allow mesh control.
struct MeshView: View {
    @Environment(AppModel.self) private var model
    @State private var overview: [String: Any] = [:]
    @State private var plan: [String: Any]?
    @State private var selectedPeers: Set<String> = []
    @State private var repo = ""
    @State private var strategy = "auto"
    @State private var allowTCP = false
    @State private var busy = false
    @State private var error: String?
    @State private var notice: String?
    @State private var showSetup = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                if let error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
                if let notice { Text(notice).foregroundStyle(.secondary) }
                thisMac
                setupGuide
                members
                modelAndSplit
                if let plan { planView(plan) }
                running
            }.padding(24)
        }
        .textFieldStyle(.roundedBorder)
        .task {
            if repo.isEmpty { repo = model.selectedRepo ?? model.downloadedChatModels.first?.repo ?? "" }
            while !Task.isCancelled {
                await refresh()
                try? await Task.sleep(for: .seconds(coordinatorState == "starting" ? 1 : 4))
            }
        }
    }

    // MARK: sections

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Mesh Mode").font(.largeTitle.bold())
                Text("Pool the memory of Macs connected by Thunderbolt 5 to run models no single Mac can hold. "
                     + "RDMA over Thunderbolt (macOS 26.2+) moves data between Macs in under 50 µs, versus about 300 µs over TCP.")
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            if busy { ProgressView().controlSize(.small) }
            Button("Refresh") { Task { await refresh() } }
        }
    }

    private var thisMac: some View {
        let node = overview["node"] as? [String: Any] ?? [:]
        let devices = node["rdma_devices"] as? [[String: Any]] ?? []
        let active = devices.filter { $0["active"] as? Bool == true }
        let links = (node["links"] as? [[String: Any]] ?? []).filter { $0["connected_to"] is String }
        let bridge = node["bridge"] as? [String: Any] ?? [:]
        let macos = node["macos"] as? String ?? ""
        return GroupBox("This Mac — \(node["name"] as? String ?? "…")") {
            VStack(alignment: .leading, spacing: 6) {
                check("macOS 26.2 or later", ok: node["macos_ok"] as? Bool == true, detail: macos.isEmpty ? "not macOS" : "macOS \(macos)")
                check("Thunderbolt 5", ok: (node["thunderbolt_generation"] as? Int ?? 0) >= 5,
                      detail: (node["thunderbolt_generation"] as? Int).map { "Thunderbolt \($0)" } ?? "unknown")
                check("RDMA enabled", ok: node["rdma"] as? String == "enabled", detail: node["rdma"] as? String ?? "unknown")
                check("RDMA ports active", ok: !active.isEmpty,
                      detail: devices.isEmpty ? "no rdma_en devices" : devices.map { "\($0["device"] as? String ?? "?") \(($0["active"] as? Bool == true) ? "active" : "down")" }.joined(separator: ", "))
                check("Thunderbolt cables to other Macs", ok: !links.isEmpty,
                      detail: links.isEmpty ? "none detected" : links.map { "\($0["iface"] as? String ?? "?") → \($0["connected_name"] as? String ?? "Mac")" }.joined(separator: ", "))
                check("Thunderbolt Bridge off", ok: bridge["active"] as? Bool != true,
                      detail: bridge["active"] as? Bool == true ? "active — turn it off in Network settings" : "off")
                if let total = node["memory_total_bytes"] as? Int64 ?? (node["memory_total_bytes"] as? Int).map(Int64.init) {
                    Text("Memory: \(Format.memory(total)) · MLX \(node["mlx_version"] as? String ?? "?")").font(.caption).foregroundStyle(.secondary)
                }
            }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var setupGuide: some View {
        let setup = overview["setup"] as? [String: Any] ?? [:]
        let steps = setup["recovery_steps"] as? [String] ?? []
        let requirements = setup["requirements"] as? [String] ?? []
        return GroupBox {
            DisclosureGroup("Set up RDMA on each Mac", isExpanded: $showSetup) {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Do this once on every Mac in the mesh. It can only be done from macOS Recovery, at the Mac itself.")
                        .foregroundStyle(.secondary)
                    ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                        HStack(alignment: .top) { Text("\(index + 1).").monospacedDigit(); Text(step) }
                    }
                    HStack {
                        Text("rdma_ctl enable").font(.system(.body, design: .monospaced)).padding(6)
                            .background(.quaternary, in: RoundedRectangle(cornerRadius: 6))
                        Button("Copy") { model.copyToPasteboard("rdma_ctl enable") }
                    }
                    Divider()
                    Text("Requirements").font(.headline)
                    ForEach(requirements, id: \.self) { Label($0, systemImage: "checkmark.circle").font(.callout) }
                    Text("Two Macs need one direct cable. 2–4 Macs fully connected (every pair cabled) is the recommended, best-tested setup. "
                         + "Links are point-to-point and there are no Thunderbolt 5 switches, so a full mesh of N Macs needs N−1 ports "
                         + "on every Mac: a 6-port M5 Ultra Mac Studio tops out at 7. Beyond that, cable a ring (2 ports per Mac; "
                         + "higher latency, less proven). Base M4 Mac minis have Thunderbolt 4 and cannot use RDMA.")
                        .font(.caption).foregroundStyle(.secondary)
                }.padding(.top, 6)
            }.padding(8)
        }
    }

    private var members: some View {
        let peers = overview["peers"] as? [[String: Any]] ?? []
        return GroupBox("Macs in the mesh") {
            VStack(alignment: .leading, spacing: 8) {
                Text(LocalizedStringKey("This Mac coordinates and serves the model. Add other Macs with the glowing + on This machine, "
                     + "or under Network (Approve peer). On each of them, pair this Mac with **Allow mesh** turned on so its key "
                     + "may start and stop that Mac's share."))
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if peers.isEmpty {
                    Button("Open Network to add a Mac") { model.studioSection = "Network" }
                } else {
                    ForEach(peers.indices, id: \.self) { i in
                        let id = peers[i]["id"] as? String ?? ""
                        Toggle(isOn: Binding(get: { selectedPeers.contains(id) },
                                             set: { if $0 { selectedPeers.insert(id) } else { selectedPeers.remove(id) } })) {
                            VStack(alignment: .leading) {
                                Text(peers[i]["name"] as? String ?? "Mac")
                                Text(peers[i]["url"] as? String ?? "").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var modelAndSplit: some View {
        GroupBox("Model") {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Picker("Model", selection: $repo) {
                        if repo.isEmpty { Text("Choose a model").tag("") }
                        ForEach(model.downloadedChatModels) { Text($0.name).tag($0.repo) }
                        if !repo.isEmpty && !model.downloadedChatModels.contains(where: { $0.repo == repo }) { Text(repo).tag(repo) }
                    }
                    TextField("or a Hugging Face id", text: $repo).frame(maxWidth: 320)
                }
                Picker("Split", selection: $strategy) {
                    Text("Automatic").tag("auto")
                    Text("Tensor (best with RDMA)").tag("tensor")
                    Text("Pipeline (layers per Mac)").tag("pipeline")
                }.pickerStyle(.segmented)
                Toggle("Allow a slower TCP mesh when RDMA isn't available", isOn: $allowTCP)
                Text("The model must be on every Mac (each Mac loads only its share into memory). Download fetches it where it's missing.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Check") { run("/plan") }.disabled(repo.isEmpty || busy)
                    Button("Download on all Macs") { run("/prepare") }.disabled(repo.isEmpty || busy)
                    Spacer()
                    if isRunning {
                        Button("Stop Mesh", role: .destructive) { run("/stop", withBody: false) }
                    } else {
                        Button("Start Mesh") { run("/start") }.buttonStyle(.borderedProminent)
                            .disabled(repo.isEmpty || busy || selectedPeers.isEmpty)
                    }
                }
            }.padding(8)
        }
    }

    private func planView(_ plan: [String: Any]) -> some View {
        let blockers = plan["blockers"] as? [[String: Any]] ?? []
        let warnings = plan["warnings"] as? [[String: Any]] ?? []
        let nodes = plan["nodes"] as? [[String: Any]] ?? []
        let backend = plan["backend"] as? String ?? ""
        return GroupBox(plan["ok"] as? Bool == true ? "Ready to start" : "Not ready yet") {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 18) {
                    stat("Network", backend == "jaccl" ? "RDMA (full mesh)" : backend == "jaccl-ring" ? "RDMA (ring)"
                         : backend == "ring" ? "TCP ring" : "—")
                    stat("Split", (plan["strategy"] as? String ?? "").capitalized.isEmpty ? "—" : (plan["strategy"] as? String ?? "").capitalized)
                    stat("Pooled memory", Format.memory(int64(plan["pooled_memory_bytes"])))
                    stat("Model", int64(plan["model_size_bytes"]) > 0 ? Format.memory(int64(plan["model_size_bytes"])) : "—")
                    if let maxMesh = plan["max_full_mesh"] as? Int { stat("Full-mesh limit", "\(maxMesh) Macs") }
                }
                if !nodes.isEmpty {
                    Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 4) {
                        GridRow { Text("Rank").bold(); Text("Mac").bold(); Text("Share").bold(); Text("Free").bold(); Text("") }
                        ForEach(nodes.indices, id: \.self) { i in
                            let n = nodes[i]
                            GridRow {
                                Text("\(n["rank"] as? Int ?? i)").monospacedDigit()
                                Text((n["name"] as? String ?? "Mac") + ((n["rank"] as? Int) == 0 ? " (this Mac)" : ""))
                                Text(Format.memory(int64(n["share_bytes"])))
                                Text(Format.memory(int64(n["memory_available_bytes"])))
                                Image(systemName: n["fits"] as? Bool == true ? "checkmark.circle.fill" : "xmark.octagon.fill")
                                    .foregroundStyle(n["fits"] as? Bool == true ? .green : .red)
                            }
                        }
                    }
                }
                ForEach(blockers.indices, id: \.self) { i in issue(blockers[i], color: .red, icon: "xmark.octagon.fill") }
                ForEach(warnings.indices, id: \.self) { i in issue(warnings[i], color: .orange, icon: "exclamationmark.triangle.fill") }
            }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var running: some View {
        let coordinator = overview["coordinator"] as? [String: Any] ?? [:]
        let rank0 = coordinator["rank0"] as? [String: Any] ?? [:]
        let log = rank0["log"] as? [String] ?? []
        return GroupBox("Status") {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Circle().fill(stateColor).frame(width: 9, height: 9)
                    Text(stateText).bold()
                }
                if let err = coordinator["error"] as? String, !err.isEmpty { Text(err).foregroundStyle(.red).textSelection(.enabled) }
                if coordinatorState == "ready", let served = rank0["model"] as? String {
                    Text("Serving \(served) on \(rank0["world"] as? Int ?? 0) Macs. Use it like any model: "
                         + "model \"\(served)\" (or \"mesh\") at \(model.status?.server.endpoint ?? "this Mac")/v1.")
                        .textSelection(.enabled)
                    Button("Copy model id") { model.copyToPasteboard(served) }
                }
                if !log.isEmpty {
                    DisclosureGroup("Rank 0 log") {
                        Text(log.joined(separator: "\n")).font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: helpers

    private var coordinatorState: String { (overview["coordinator"] as? [String: Any])?["state"] as? String ?? "idle" }
    private var isRunning: Bool { ["starting", "ready"].contains(coordinatorState) }
    private var stateText: String {
        switch coordinatorState {
        case "starting": return "Starting — each Mac is joining and loading its share…"
        case "ready": return "Mesh running"
        case "failed": return "Mesh failed"
        case "stopping": return "Stopping…"
        default: return "Not running"
        }
    }
    private var stateColor: Color {
        switch coordinatorState { case "ready": return .green; case "starting", "stopping": return .orange; case "failed": return .red; default: return .secondary }
    }

    private func check(_ title: String, ok: Bool, detail: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Image(systemName: ok ? "checkmark.circle.fill" : "xmark.circle.fill").foregroundStyle(ok ? .green : .red)
            Text(title)
            Spacer()
            Text(detail).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.trailing)
        }
    }

    private func stat(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading) { Text(title).font(.caption).foregroundStyle(.secondary); Text(value).bold() }
    }

    private func issue(_ item: [String: Any], color: Color, icon: String) -> some View {
        HStack(alignment: .top) {
            Image(systemName: icon).foregroundStyle(color)
            VStack(alignment: .leading, spacing: 2) {
                Text(item["message"] as? String ?? "")
                if let fix = item["fix"] as? String, !fix.isEmpty { Text(fix).font(.caption).foregroundStyle(.secondary) }
            }
        }
    }

    private func int64(_ value: Any?) -> Int64 {
        if let v = value as? Int64 { return v }
        if let v = value as? Int { return Int64(v) }
        if let v = value as? Double { return Int64(v) }
        return 0
    }

    private var requestBody: [String: Any] {
        ["model": repo, "peers": Array(selectedPeers).sorted(), "strategy": strategy, "allow_tcp": allowTCP]
    }

    private func refresh() async {
        do {
            overview = try await model.featureRequest("GET", "/control/mesh")
            let known = Set((overview["peers"] as? [[String: Any]] ?? []).compactMap { $0["id"] as? String })
            selectedPeers.formIntersection(known)
            if plan == nil, let saved = (overview["coordinator"] as? [String: Any])?["plan"] as? [String: Any] { plan = saved }
            error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func run(_ path: String, withBody: Bool = true) {
        busy = true; error = nil; notice = nil
        Task {
            defer { busy = false }
            do {
                let result = try await model.featureRequest("POST", "/control/mesh" + path, body: withBody ? requestBody : nil)
                switch path {
                case "/plan": plan = result
                case "/prepare":
                    let on = result["downloading_on"] as? [String] ?? []
                    notice = on.isEmpty ? "The model is already on every Mac." : "Downloading on: \(on.joined(separator: ", ")). Check again when they finish."
                case "/start": notice = "Starting the mesh. Large models take a few minutes to load on every Mac."
                default: break
                }
                await refresh()
            } catch {
                self.error = error.localizedDescription
                if path == "/start", let fresh = try? await model.featureRequest("POST", "/control/mesh/plan", body: requestBody) {
                    plan = fresh   // show exactly what is blocking
                }
            }
        }
    }
}
