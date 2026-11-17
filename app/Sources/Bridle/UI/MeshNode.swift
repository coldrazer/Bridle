import SwiftUI

/// The glowing node on the "This machine" card: the way into memory pooling.
/// It pulses gently (still when Reduce Motion is on) and shows how many Macs are connected.
struct MeshNodeButton: View {
    let connected: Int
    let action: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var pulse = false
    @State private var hovering = false

    static let glow = AngularGradient(colors: [.indigo, .purple, .pink, .purple, .indigo], center: .center)

    var body: some View {
        HStack(spacing: 0) {
            // Connector stub from the card edge to the node.
            Capsule()
                .fill(LinearGradient(colors: [.indigo.opacity(0.2), .purple.opacity(0.9)], startPoint: .leading, endPoint: .trailing))
                .frame(width: 16, height: 3)
                .shadow(color: .purple.opacity(0.8), radius: 4)
            Button(action: action) {
                ZStack {
                    if !reduceMotion {
                        Circle()
                            .stroke(Self.glow, lineWidth: 2)
                            .frame(width: 40, height: 40)
                            .scaleEffect(pulse ? 1.7 : 1)
                            .opacity(pulse ? 0 : 0.9)
                    }
                    Circle()
                        .fill(RadialGradient(colors: [.pink.opacity(0.95), .purple, .indigo],
                                             center: .topLeading, startRadius: 2, endRadius: 40))
                        .frame(width: 36, height: 36)
                        .overlay(Circle().stroke(.white.opacity(0.55), lineWidth: 1))
                        .shadow(color: .purple.opacity(0.95), radius: hovering ? 16 : (pulse ? 12 : 7))
                        .shadow(color: .pink.opacity(0.6), radius: hovering ? 26 : 14)
                    Image(systemName: connected > 0 ? "link" : "plus")
                        .font(.system(size: 15, weight: .bold))
                        .foregroundStyle(.white)
                }
                .frame(width: 48, height: 48)
                .scaleEffect(hovering ? 1.08 : 1)
                .overlay(alignment: .topTrailing) {
                    if connected > 0 {
                        Text("\(connected)")
                            .font(.caption2.bold()).monospacedDigit()
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(.green, in: Capsule())
                            .foregroundStyle(.white)
                            .offset(x: 2, y: -2)
                    }
                }
                .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .onHover { inside in withAnimation(.easeOut(duration: 0.15)) { hovering = inside } }
        }
        .help(connected > 0 ? "\(connected) Mac\(connected == 1 ? "" : "s") connected for memory pooling — add another"
                            : "Add another Mac and pool its memory over Thunderbolt 5")
        .accessibilityElement(children: .combine)
        .accessibilityLabel(connected > 0 ? "Memory pooling, \(connected) connected. Add another Mac." : "Add a Mac for memory pooling")
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { action() }
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.easeOut(duration: 1.9).repeatForever(autoreverses: false)) { pulse = true }
        }
    }
}

extension Peer {
    /// "Bridle on Studio" -> "Studio".
    var displayName: String { name.replacingOccurrences(of: "Bridle on ", with: "") }
    var advertisedMemory: Int64? { properties["memory"].flatMap(Int64.init) }
    var thunderboltGeneration: Int? { properties["tb"].flatMap(Int.init) }
    var httpsURL: String? {
        guard let address = addresses.first, let port = properties["tls"].flatMap(Int.init), (1...65535).contains(port) else { return nil }
        return "https://" + (address.contains(":") ? "[\(address)]" : address) + ":\(port)"
    }
}

/// Add a Mac for memory pooling: choose it, authorize this Mac on it, verify its certificate, connect.
struct AddMacSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    var onConnected: () -> Void = {}

    @State private var step = 0
    @State private var name = ""
    @State private var url = ""
    @State private var apiKey = ""
    @State private var certificate: [String: Any]?
    @State private var fingerprintConfirmed = false
    @State private var result: [String: Any]?
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12) {
                MeshGlyph()
                VStack(alignment: .leading, spacing: 2) {
                    Text("Add a Mac to the memory pool").font(.title2.bold())
                    Text(["Choose the Mac", "Allow this Mac on \(name.isEmpty ? "it" : name)", "Connected"][step])
                        .foregroundStyle(.secondary)
                }
                Spacer()
                StepDots(current: step, count: 3)
            }
            .padding(20)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let error { Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.red).textSelection(.enabled) }
                    switch step {
                    case 0: chooseMac
                    case 1: authorize
                    default: connected
                    }
                }.padding(20)
            }
            Divider()
            HStack {
                if step == 1 { Button("Back") { step = 0; error = nil } }
                Spacer()
                if busy { ProgressView().controlSize(.small) }
                switch step {
                case 0:
                    Button("Cancel") { dismiss() }
                    Button("Next") { step = 1; error = nil; certificate = nil; fingerprintConfirmed = false }
                        .keyboardShortcut(.defaultAction)
                        .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || !url.lowercased().hasPrefix("https://"))
                case 1:
                    Button("Cancel") { dismiss() }
                    Button("Connect") { connect() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(busy || apiKey.isEmpty || certificate == nil || !fingerprintConfirmed)
                default:
                    Button("Add Another Mac") { reset() }
                    Button("Open Mesh Mode") { dismiss(); model.studioSection = "Mesh"; model.showStudio = true }
                    Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
                }
            }
            .padding(16)
        }
        .frame(width: 560, height: 600)
        .textFieldStyle(.roundedBorder)
    }

    // MARK: step 1 — choose

    private var chooseMac: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Pooling joins this Mac's memory with other Macs connected by Thunderbolt 5 cables, so together they can run "
                 + "models none of them could hold alone. Pick a Mac running Bridle nearby:")
                .fixedSize(horizontal: false, vertical: true)
            if model.peers.isEmpty {
                Label("No other Macs found yet. Make sure Bridle is running on the other Mac with Share on network turned on, "
                      + "or enter its address below.", systemImage: "antenna.radiowaves.left.and.right.slash")
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            ForEach(model.peers) { peer in
                Button { choose(peer) } label: { peerRow(peer) }.buttonStyle(.plain)
            }
            GroupBox("Or enter it yourself") {
                VStack(alignment: .leading, spacing: 8) {
                    TextField("Name (e.g. Studio)", text: $name)
                    TextField("HTTPS address (e.g. https://studio.local:49814)", text: $url)
                    Text("Use the HTTPS address shown in Bridle's settings on that Mac.").font(.caption).foregroundStyle(.secondary)
                }.padding(6)
            }
        }
    }

    private func peerRow(_ peer: Peer) -> some View {
        let selected = url == peer.httpsURL && name == peer.displayName
        let tb = peer.thunderboltGeneration
        return HStack(spacing: 12) {
            Image(systemName: "desktopcomputer").font(.title2).foregroundStyle(selected ? .white : .purple)
                .frame(width: 40, height: 40)
                .background(selected ? AnyShapeStyle(MeshNodeButton.glow) : AnyShapeStyle(.quaternary), in: Circle())
            VStack(alignment: .leading, spacing: 3) {
                Text(peer.displayName).font(.headline)
                HStack(spacing: 8) {
                    if let chip = peer.chip { Text(chip) }
                    if let mem = peer.advertisedMemory { Text(Format.memory(mem)) }
                }.font(.caption).foregroundStyle(.secondary)
                HStack(spacing: 6) {
                    badge(tb.map { "Thunderbolt \($0)" } ?? "Thunderbolt ?", ok: (tb ?? 0) >= 5,
                          help: (tb ?? 0) >= 5 ? "Supports RDMA" : "Needs Thunderbolt 5 for RDMA pooling")
                    badge(peer.httpsURL == nil ? "HTTPS off" : "HTTPS on", ok: peer.httpsURL != nil,
                          help: "Turn on Share on network and HTTPS in Bridle's settings on that Mac")
                }
            }
            Spacer()
            if selected { Image(systemName: "checkmark.circle.fill").foregroundStyle(.green).font(.title3) }
        }
        .padding(10)
        .background(selected ? Color.purple.opacity(0.12) : Color.clear, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(selected ? Color.purple : Color.secondary.opacity(0.3)))
        .contentShape(RoundedRectangle(cornerRadius: 10))
    }

    private func badge(_ text: String, ok: Bool, help: String) -> some View {
        Text(text).font(.caption2.weight(.semibold)).padding(.horizontal, 6).padding(.vertical, 2)
            .background((ok ? Color.green : Color.orange).opacity(0.18), in: Capsule())
            .foregroundStyle(ok ? .green : .orange).help(help)
    }

    private func choose(_ peer: Peer) {
        name = peer.displayName
        url = peer.httpsURL ?? ""
        error = peer.httpsURL == nil ? "\(peer.displayName) isn't sharing over HTTPS yet. On that Mac, open Bridle's settings and turn on Share on network and HTTPS." : nil
    }

    // MARK: step 2 — authorize + verify

    private var authorize: some View {
        let me = model.status?.machine.hostname ?? "this Mac"
        return VStack(alignment: .leading, spacing: 14) {
            Text("\(name) has to allow this Mac to use its memory. On **\(name)**:")
            VStack(alignment: .leading, spacing: 8) {
                numbered(1, "Open Bridle's settings and turn on **Share on network** and **HTTPS**.")
                numbered(2, "Choose **Assistant > Workspaces & Advanced Tools**, then **Network**, and click **Generate pairing code**.")
                numbered(3, "Enter **\(me)** as the device name, paste the code, turn on **Allow mesh**, and click **Approve device**.")
                numbered(4, "It shows a key and a certificate fingerprint. Paste the key here, then compare the fingerprint below.")
            }
            SecureField("Key from \(name) (starts with brdl_)", text: $apiKey)
            GroupBox {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("Certificate").font(.headline)
                        Spacer()
                        Button(certificate == nil ? "Get Certificate" : "Get Again") { fetchCertificate() }.disabled(busy)
                    }
                    if let fp = certificate?["fingerprint"] as? String {
                        Text(fp).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                            .padding(8).frame(maxWidth: .infinity, alignment: .leading)
                            .background(.quaternary, in: RoundedRectangle(cornerRadius: 6))
                        Toggle("This matches the fingerprint \(name) showed", isOn: $fingerprintConfirmed)
                        Text("Matching fingerprints prove you're talking to \(name) and not something pretending to be it.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text("Bridle fetches \(name)'s certificate from \(url) so you can check it.").font(.caption).foregroundStyle(.secondary)
                    }
                }.padding(6)
            }
        }
    }

    private func numbered(_ n: Int, _ text: LocalizedStringKey) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text("\(n)").font(.caption.bold()).foregroundStyle(.white).frame(width: 20, height: 20)
                .background(MeshNodeButton.glow, in: Circle())
            Text(text).fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: step 3 — connected

    private var connected: some View {
        let node = result?["node"] as? [String: Any] ?? [:]
        let tb = node["thunderbolt_generation"] as? Int ?? 0
        let rdma = node["rdma"] as? String ?? "unknown"
        let active = (node["rdma_devices"] as? [[String: Any]] ?? []).filter { $0["active"] as? Bool == true }.count
        let mem = (node["memory_total_bytes"] as? Int64) ?? (node["memory_total_bytes"] as? Int).map(Int64.init) ?? 0
        let mine = model.status?.machine.memoryTotalBytes ?? 0
        return VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 14) {
                Image(systemName: "checkmark.seal.fill").font(.largeTitle).foregroundStyle(.green)
                VStack(alignment: .leading) {
                    Text("\(node["name"] as? String ?? name) is connected").font(.title3.bold())
                    Text("\(node["chip"] as? String ?? "") · \(Format.memory(mem))").foregroundStyle(.secondary)
                }
            }
            if mem > 0 && mine > 0 {
                Text("Pooled with this Mac: **\(Format.memory(mem + mine))** of unified memory.")
            }
            VStack(alignment: .leading, spacing: 6) {
                ready("macOS 26.2 or later", node["macos_ok"] as? Bool == true, "Update macOS on \(name)")
                ready("Thunderbolt 5", tb >= 5, "\(name) has Thunderbolt \(tb); RDMA needs Thunderbolt 5")
                ready("RDMA enabled", rdma == "enabled", "On \(name), run rdma_ctl enable from macOS Recovery (see Mesh Mode > Set up RDMA)")
                ready("RDMA ports active", active > 0, "Connect a Thunderbolt 5 cable directly between the Macs")
                ready("Thunderbolt Bridge off", (node["bridge"] as? [String: Any])?["active"] as? Bool != true,
                      "On \(name): System Settings > Network > Thunderbolt Bridge > Inactivate")
            }
            Text("Next, open Mesh Mode to pick a model and start pooling.").foregroundStyle(.secondary)
        }
    }

    private func ready(_ title: String, _ ok: Bool, _ fix: String) -> some View {
        HStack(alignment: .top) {
            Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill").foregroundStyle(ok ? .green : .orange)
            VStack(alignment: .leading) {
                Text(title)
                if !ok { Text(fix).font(.caption).foregroundStyle(.secondary) }
            }
        }
    }

    // MARK: actions

    private func fetchCertificate() {
        busy = true; error = nil; fingerprintConfirmed = false
        Task {
            defer { busy = false }
            do { certificate = try await model.featureRequest("POST", "/control/mesh/certificate", body: ["url": url]) }
            catch { self.error = error.localizedDescription; certificate = nil }
        }
    }

    private func connect() {
        guard let pem = certificate?["pem"] as? String, let fp = certificate?["fingerprint"] as? String else { return }
        busy = true; error = nil
        Task {
            defer { busy = false }
            do {
                result = try await model.featureRequest("POST", "/control/mesh/connect", body: [
                    "name": name, "url": url, "api_key": apiKey, "ca_pem": pem, "fingerprint": fp])
                apiKey = ""
                step = 2
                onConnected()
            } catch { self.error = error.localizedDescription }
        }
    }

    private func reset() {
        step = 0; name = ""; url = ""; apiKey = ""; certificate = nil; fingerprintConfirmed = false; result = nil; error = nil
    }
}

private struct MeshGlyph: View {
    var body: some View {
        ZStack {
            Circle().fill(MeshNodeButton.glow).frame(width: 44, height: 44).shadow(color: .purple.opacity(0.7), radius: 10)
            Image(systemName: "point.3.connected.trianglepath.dotted").foregroundStyle(.white).font(.title3.bold())
        }
    }
}

private struct StepDots: View {
    let current: Int
    let count: Int
    var body: some View {
        HStack(spacing: 6) {
            ForEach(0..<count, id: \.self) { i in
                Capsule().fill(i <= current ? AnyShapeStyle(MeshNodeButton.glow) : AnyShapeStyle(.quaternary))
                    .frame(width: i == current ? 22 : 8, height: 8)
            }
        }
        .animation(.easeInOut(duration: 0.2), value: current)
        .accessibilityLabel("Step \(current + 1) of \(count)")
    }
}
