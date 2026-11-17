import AppKit
import SwiftUI
import Charts

struct OperationsStudioView: View {
    @Environment(AppModel.self) private var model
    let section: String
    @State private var snapshot: [String: Any] = [:]
    @State private var output = ""
    @State private var error: String?
    @State private var busy = false
    @State private var name = ""
    @State private var url = ""
    @State private var selected = ""
    @State private var path = ""
    @State private var content = ""
    @State private var code = ""
    @State private var apiKey = ""
    @State private var caPEM = ""
    @State private var writeAccess = false
    @State private var meshGrant = false
    @State private var approval = ""
    @State private var profile = "balanced"
    @State private var proposedAction = ""
    @State private var approvedProposal: [String: Any] = [:]
    @State private var toolName = ""
    @State private var arguments = "{}"
    @State private var clientKind = "openai"
    @State private var samples: [(Date, Double)] = []
    @AppStorage("followPowerConditions") private var followPower = true

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Text(section).font(.largeTitle.bold())
                    Spacer()
                    if busy { ProgressView().controlSize(.small) }
                    Button("Refresh") { Task { await refresh() } }
                }
                if let error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
                switch section {
                case "Performance", "Queue": monitor
                case "Power": power
                case "Health": health
                case "Connect Apps": clients
                case "Tools": tools
                case "MCP": mcp
                default: network
                }
                if !output.isEmpty {
                    GroupBox("Result") {
                        VStack(alignment: .leading) {
                            Text(output).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Button("Copy result") { model.copyToPasteboard(output) }
                        }.padding(8)
                    }
                }
            }.padding(24)
        }
        .textFieldStyle(.roundedBorder)
        .task(id: section) {
            selected = ""; output = ""; error = nil
            if section == "Network" {
                name = model.peerNameDraft; url = model.peerURLDraft; toolName = model.peerModelDraft
            }
            while !Task.isCancelled {
                await refresh()
                if section != "Performance" && section != "Queue" { break }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private var monitor: some View {
        VStack(alignment: .leading, spacing: 14) {
            let metrics = model.status?.model.metrics
            HStack(spacing: 24) {
                metric("Requests", "\(metrics?.requestsTotal ?? 0)")
                metric("Queued", "\(metrics?.queueDepth ?? 0)")
                metric("Last speed", String(format: "%.1f tok/s", metrics?.lastGenerationTps ?? 0))
                metric("Memory", model.status.map { Format.memory($0.stats.memoryUsedBytes) } ?? "—")
            }
            Text("Live engine activity. Cancelling a request asks the model worker to stop; prefill may finish before cancellation is observed.").foregroundStyle(.secondary)
            let rows = records("requests")
            if rows.isEmpty { ContentUnavailableView("No requests yet", systemImage: "tray", description: Text("Requests appear here when an app or a background job uses your model.")) }
            ForEach(rows.indices, id: \.self) { i in
                let row = rows[i]
                HStack {
                    VStack(alignment: .leading) {
                        Text(row["model"] as? String ?? "Model request").fontWeight(.medium)
                        Text("\(row["status"] as? String ?? "") · \(row["tokens"] as? Int ?? 0) tokens").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if ["queued", "running", "generating", "cancelling"].contains(row["status"] as? String ?? "") {
                        Button("Cancel") { action("POST", "/requests/\(row["id"] as? String ?? "")/cancel") }
                    }
                }.padding(10).background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
            }
            if section == "Performance" {
                Chart(Array(samples.enumerated()), id: \.offset) { sample in
                    LineMark(x: .value("Time", sample.element.0), y: .value("Tokens per second", sample.element.1))
                }.frame(height: 160).chartYAxisLabel("Last generation tok/s")
                GroupBox("History") {
                    VStack(alignment: .leading) {
                        Text("Per-request timing, cached tokens, failures and benchmark comparisons are available in the main Requests and Benchmarks panels.")
                        HStack {
                            Button("Requests") { model.bottomPanel = .requests }
                            Button("Benchmarks") { model.bottomPanel = .benchmarks }
                        }
                    }.padding(8)
                }
            }
        }
    }
    private func metric(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading) { Text(title).font(.caption).foregroundStyle(.secondary); Text(value).font(.title2.monospacedDigit()) }
    }
    private var power: some View {
        GroupBox("Resource profile") {
            VStack(alignment: .leading, spacing: 12) {
                Picker("Mode", selection: $profile) {
                    Text("Quiet").tag("quiet"); Text("Balanced").tag("balanced"); Text("Maximum throughput").tag("performance")
                }.pickerStyle(.segmented)
                Text("Quiet reduces batch size and unloads sooner. Maximum throughput allows larger batches and retains models longer. These are inference settings, not fan or CPU frequency controls.")
                Toggle("Use Quiet when macOS reports Low Power Mode or serious thermal pressure", isOn: $followPower)
                Text("macOS Low Power Mode: \(ProcessInfo.processInfo.isLowPowerModeEnabled ? "On" : "Off") · Thermal state: \(thermalLabel)")
                    .font(.caption).foregroundStyle(.secondary)
                Text("Watts and fan speeds are not measured.").font(.caption).foregroundStyle(.secondary)
                Button("Apply profile") { action("PUT", "/profile", ["name": profile]) }
            }.padding(10)
        }
    }
    private var health: some View {
        GroupBox("Check an installed model") {
            VStack(alignment: .leading, spacing: 12) {
                Picker("Model", selection: $selected) {
                    Text("Choose model").tag("")
                    ForEach(model.downloadedModels) { Text($0.name).tag($0.repo) }
                }
                Text("File checks inspect the manifest, configuration and weight files. Load & test also changes the resident model and runs a small generation; it can take time.").foregroundStyle(.secondary)
                HStack {
                    Button("Check files") { action("POST", "/health", ["repo": selected]) }
                    Button("Load & test") { action("POST", "/health", ["repo": selected, "generate": true]) }
                }.disabled(selected.isEmpty || busy)
            }.padding(10)
        }
    }
    private var clients: some View {
        GroupBox("Connect an app") {
            VStack(alignment: .leading, spacing: 12) {
                TextField("App or device name", text: $name)
                Picker("Configuration", selection: $clientKind) {
                    Text("OpenAI-compatible app").tag("openai"); Text("Terminal curl").tag("curl"); Text("Continue editor").tag("continue")
                }
                Text("Creates a dedicated revocable key and a connection configuration. Copy it into your app. Credentials are shown once; keep them private.").foregroundStyle(.secondary)
                Button("Create configuration") { action("POST", "/clients", ["name": name, "kind": clientKind]) }.disabled(name.isEmpty || busy)
                Button("Test local engine connection") { Task { await refresh(); output = error == nil ? "Connected to your local engine." : "Connection failed." } }
            }.padding(10)
        }
    }
    private var tools: some View {
        VStack(alignment: .leading, spacing: 16) {
            GroupBox("Explicit folder access") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Only selected text files inside a granted folder are available. Tools never run shell commands.").foregroundStyle(.secondary)
                    HStack { TextField("Folder", text: $path); Button("Choose…") { chooseFolder() } }
                    Toggle("Allow approved writes", isOn: $writeAccess)
                    Button("Grant folder") { action("POST", "/grants", ["path": path, "write": writeAccess]) }.disabled(path.isEmpty || busy)
                    Picker("Granted folder", selection: $selected) {
                        Text("Choose grant").tag("")
                        ForEach(records("grants").indices, id: \.self) { i in
                            let row = records("grants")[i]
                            Text(row["path"] as? String ?? "Folder").tag(row["id"] as? String ?? "")
                        }
                    }
                    Button("Revoke selected grant", role: .destructive) { action("DELETE", "/grants/\(selected)") }.disabled(selected.isEmpty)
                }.padding(10)
            }
            GroupBox("Read and preview") {
                VStack(alignment: .leading, spacing: 10) {
                    TextField("Relative file path (use . for the granted folder)", text: $name)
                    TextField("Describe a file task for the loaded model", text: $toolName)
                    Button("Ask model to propose a tool action") { action("POST", "/tools/propose", ["grant_id": selected, "task": toolName]) }
                        .disabled(selected.isEmpty || toolName.isEmpty || busy)
                    if !proposedAction.isEmpty {
                        Text("Proposed: \(proposedAction) \(approvedProposal["path"] as? String ?? "") in grant \(approvedProposal["grant_id"] as? String ?? ""). Review the result before approving.").foregroundStyle(.secondary)
                        Button("Approve exact proposed action") {
                            var payload = approvedProposal
                            payload.removeValue(forKey: "action")
                            action("POST", "/tools/" + proposedAction, payload)
                            proposedAction = ""; approvedProposal = [:]
                        }
                    }
                    HStack {
                        Button("List folder") { action("POST", "/tools/list", ["grant_id": selected, "path": name]) }
                        Button("Read file") { action("POST", "/tools/read", ["grant_id": selected, "path": name]) }
                    }.disabled(selected.isEmpty)
                    TextEditor(text: $content).font(.system(.body, design: .monospaced)).frame(height: 150).border(.quaternary)
                    Button("Preview proposed write") { action("POST", "/tools/preview-write", ["grant_id": selected, "path": name, "content": content]) }
                        .disabled(selected.isEmpty)
                    Text("Review the preview result, then paste its approval token to apply that exact change. A changed file requires a new preview.").font(.caption).foregroundStyle(.secondary)
                    TextField("Approval token from preview", text: $approval)
                    Button("Approve and write this file", role: .destructive) { action("POST", "/tools/commit-write", ["grant_id": selected, "path": name, "content": content, "approval": approval]); approval = "" }.disabled(approval.isEmpty)
                }.padding(10)
            }
        }
    }
    private var mcp: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Connect a Streamable HTTP MCP server over HTTPS or localhost. Every call here is explicit; adding a server does not run its tools.").foregroundStyle(.secondary)
            TextField("Connection name", text: $name)
            TextField("https://server.example/mcp or http://127.0.0.1:port/mcp", text: $url)
            Button("Add connection") { action("POST", "/mcp", ["name": name, "url": url]) }.disabled(name.isEmpty || url.isEmpty)
            Picker("Connection", selection: $selected) {
                Text("Choose connection").tag("")
                ForEach(records("mcp").indices, id: \.self) { i in
                    let row = records("mcp")[i]
                    Text(row["name"] as? String ?? "MCP server").tag(row["id"] as? String ?? "")
                }
            }
            HStack {
                Button("Initialize") { action("POST", "/mcp/\(selected)/initialize") }
                Button("List tools") { action("POST", "/mcp/\(selected)/tools") }
                Button("Disable") { action("PUT", "/mcp/\(selected)", ["enabled": false]) }
                Button("Enable") { action("PUT", "/mcp/\(selected)", ["enabled": true]) }
                Button("Remove", role: .destructive) { action("DELETE", "/mcp/\(selected)") }
            }.disabled(selected.isEmpty)
            TextField("Tool name (from tool list)", text: $toolName)
            Text("Arguments (JSON object)").font(.caption)
            TextEditor(text: $arguments).font(.system(.body, design: .monospaced)).frame(height: 140).border(.quaternary)
            Button("Run this tool") {
                guard let data = arguments.data(using: .utf8), let args = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { error = "Arguments must be a JSON object."; return }
                action("POST", "/mcp/\(selected)/call", ["name": toolName, "arguments": args])
            }.disabled(selected.isEmpty || toolName.isEmpty)
        }
    }
    private var network: some View {
        VStack(alignment: .leading, spacing: 16) {
            GroupBox("Pair a device with this Mac") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Generate a short-lived code here, then confirm it on this Mac to issue a device key. Transfer the key and certificate fingerprint privately. Bonjour discovery alone grants no access.").foregroundStyle(.secondary)
                    Button("Generate pairing code") { action("POST", "/pairing") }
                    TextField("Device name", text: $name)
                    TextField("Pairing code", text: $code)
                    Toggle("Allow mesh — powerful: this device may start/stop this Mac's mesh rank, trigger model downloads and hold this Mac's memory. Only grant it to Macs you control.", isOn: $meshGrant)
                    Button("Approve device") { action("POST", "/pairing/confirm", ["code": code, "name": name, "allow_mesh": meshGrant]); code = ""; meshGrant = false }.disabled(code.isEmpty || name.isEmpty)
                    ForEach(records("devices").indices, id: \.self) { i in
                        let row = records("devices")[i]
                        HStack {
                            Text(row["name"] as? String ?? "Device"); Spacer()
                            Button("Revoke", role: .destructive) { action("DELETE", "/devices/\(row["id"] as? String ?? "")") }
                        }
                    }
                }.padding(10)
            }
            GroupBox("Route to a trusted Mac") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Whole requests run on one selected Mac. Memory is not pooled. Add only a Mac you approve, using its HTTPS endpoint, API key and trusted PEM certificate.").foregroundStyle(.secondary)
                    TextField("Peer name", text: $name)
                    TextField("Peer HTTPS URL", text: $url)
                    SecureField("Peer API key", text: $apiKey)
                    Text("Trusted certificate (PEM)").font(.caption)
                    TextEditor(text: $caPEM).font(.system(.caption, design: .monospaced)).frame(height: 90).border(.quaternary)
                    Button("Approve peer") { action("POST", "/peers", ["name": name, "url": url, "api_key": apiKey, "ca_pem": caPEM]); apiKey = "" }.disabled(url.isEmpty || name.isEmpty || apiKey.isEmpty || caPEM.isEmpty)
                    Picker("Approved peer", selection: $selected) {
                        Text("Choose peer").tag("")
                        ForEach(records("peers").indices, id: \.self) { i in
                            let row = records("peers")[i]
                            Text(row["name"] as? String ?? "Peer").tag(row["id"] as? String ?? "")
                        }
                    }
                    TextField("Model on that Mac (optional)", text: $toolName)
                    TextField("Ask the remote assistant", text: $content, axis: .vertical)
                    HStack {
                        Button("Send to selected Mac") {
                            var req: [String: Any] = ["messages": [["role": "user", "content": content]], "max_tokens": 512, "stream": false]
                            if !toolName.isEmpty { req["model"] = toolName }
                            action("POST", "/route", ["peer_id": selected, "request": req])
                        }.disabled(selected.isEmpty || content.isEmpty)
                        Button("Remove peer", role: .destructive) { action("DELETE", "/peers/\(selected)") }.disabled(selected.isEmpty)
                    }
                }.padding(10)
            }
        }
    }
    private var thermalLabel: String {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: return "Normal"
        case .fair: return "Warm"
        case .serious: return "High"
        case .critical: return "Critical"
        @unknown default: return "Unknown"
        }
    }
    private func records(_ key: String) -> [[String: Any]] { snapshot[key] as? [[String: Any]] ?? [] }
    private func refresh() async {
        do {
            snapshot = try await model.featureRequest("GET", "/control/operations")
            error = nil
            samples.append((Date(), model.status?.model.metrics.lastGenerationTps ?? 0))
            if samples.count > 60 { samples.removeFirst(samples.count - 60) }
        } catch { self.error = error.localizedDescription }
    }
    private func action(_ method: String, _ path: String, _ body: [String: Any]? = nil) {
        busy = true; error = nil
        Task {
            defer { busy = false }
            do {
                let result = try await model.featureRequest(method, "/control/operations" + path, body: body)
                output = Self.pretty(result)
                if let proposal = result["proposal"] as? [String: Any] {
                    approvedProposal = proposal
                    proposedAction = proposal["action"] as? String ?? ""
                    name = proposal["path"] as? String ?? ""
                    content = proposal["content"] as? String ?? ""
                }
                await refresh()
            } catch { self.error = error.localizedDescription }
        }
    }
    static func pretty(_ result: [String: Any]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]) else { return "Completed" }
        return String(decoding: data, as: UTF8.self)
    }
    private func chooseFolder() {
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false
        if panel.runModal() == .OK { path = panel.url?.path ?? "" }
    }
}
