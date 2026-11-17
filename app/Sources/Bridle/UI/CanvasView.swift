import AppKit
import SwiftUI

struct CanvasView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("topologyLayoutRevision") private var layoutRevision = 0
    @State private var localDetails = false
    @State private var selectedPeer: Peer?
    @State private var showAddMac = false
    @State private var pooledNames: Set<String> = []

    var body: some View {
        ZStack {
            DotGrid()
            if let machine = model.status?.machine {
                FloatingTopologyCard(storageKey: "machineCardPosition", defaultX: 0.5, defaultY: 0.5,
                                     layoutRevision: layoutRevision) {
                  HStack(alignment: .center, spacing: 0) {
                    VStack(spacing: 0) {
                        MachineCard(machine: machine, stats: model.status?.stats, highlighted: model.isModelReady)
                        if let repo = model.loadedRepo {
                            Label(Format.shortName(repo), systemImage: "shippingbox")
                                .font(.caption).padding(6).draggable(repo)
                                .help("Drag this model onto a discovered Mac to prepare a routed request.")
                        }
                        if let metrics = model.status?.model.metrics {
                            HStack(spacing: 14) {
                                Label("\(metrics.activeRequests) active", systemImage: "bolt")
                                Text("\(metrics.queueDepth) queued")
                            }
                            .font(.caption).foregroundStyle(.secondary).padding(.vertical, 8)
                        }
                    }
                    .background(.background, in: RoundedRectangle(cornerRadius: 16))
                    .onTapGesture { localDetails = true }
                    .popover(isPresented: $localDetails) { localInspector(machine) }
                    .accessibilityAction(named: Text("Show machine details")) { localDetails = true }
                    // Glowing node on the card's edge: add and connect another Mac for memory pooling.
                    MeshNodeButton(connected: pooledNames.count) { showAddMac = true }
                  }
                }
                ForEach(Array(model.peers.enumerated()), id: \.element.id) { index, peer in
                    FloatingTopologyCard(storageKey: peerStorageKey(peer), defaultX: index / 4 % 2 == 0 ? 0.8 : 0.2,
                                         defaultY: min(0.82, 0.22 + Double(index % 4) * 0.2),
                                         layoutRevision: layoutRevision) {
                        PeerCard(peer: peer, pooled: pooledNames.contains(peer.displayName))
                            .dropDestination(for: String.self) { values, _ in
                                guard let repo = values.first, repo.contains("/"), repo.count < 200 else { return false }
                                model.peerNameDraft = peer.name
                                model.peerModelDraft = repo
                                if let address = peer.addresses.first, let portText = peer.properties["tls"], let port = Int(portText), (1...65535).contains(port) {
                                    model.peerURLDraft = "https://" + displayAddress(address, port: port)
                                } else { model.peerURLDraft = "" }
                                openNetwork()
                                return true
                            }
                            .onTapGesture { selectedPeer = peer }
                            .popover(isPresented: Binding(get: { selectedPeer?.id == peer.id },
                                                         set: { if !$0 { selectedPeer = nil } })) {
                                peerInspector(model.peers.first { $0.id == peer.id } ?? peer)
                            }
                            .accessibilityAction(named: Text("Show discovered server details")) { selectedPeer = peer }
                    }
                }
                VStack {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Network topology").font(.headline)
                            Text("\(model.peers.count) discovered · \(pooledNames.count) connected for pooling · Drag cards to arrange")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Reset Layout", systemImage: "arrow.counterclockwise") { layoutRevision += 1 }
                        Button("Network Controls", systemImage: "network") { openNetwork() }
                    }
                    .padding(16)
                    Spacer()
                    Text(pooledNames.isEmpty
                         ? "This Mac runs independently. Use the glowing + on This machine to add another Mac and pool memory. Discovered servers are unverified until you connect them."
                         : "Connected Macs can pool memory in Mesh Mode. Discovered servers stay unverified until you connect them with the glowing node.")
                        .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
                        .padding(12).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10)).padding(16)
                }
                .allowsHitTesting(true)
            } else {
                VStack(spacing: 16) {
                    BridleMark().frame(width: 220).opacity(0.25)
                    ProgressView().controlSize(.small)
                    Text("Waiting for the engine…").font(.callout).foregroundStyle(.secondary)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .sheet(isPresented: $showAddMac, onDismiss: { Task { await refreshPooled() } }) {
            AddMacSheet { Task { await refreshPooled() } }.environment(model)
        }
        .task(id: model.isRunning) {
            while !Task.isCancelled && model.isRunning {
                await refreshPooled()
                try? await Task.sleep(for: .seconds(15))
            }
        }
        .onChange(of: model.peers.map(\.id)) {
            if let selectedPeer, !model.peers.contains(where: { $0.id == selectedPeer.id }) { self.selectedPeer = nil }
        }
    }

    private func refreshPooled() async {
        guard let result = try? await model.featureRequest("GET", "/control/mesh/peers") else { return }
        let names = (result["peers"] as? [[String: Any]] ?? []).filter { $0["mesh"] as? Bool == true }
            .compactMap { $0["name"] as? String }
        pooledNames = Set(names)
    }

    private func peerStorageKey(_ peer: Peer) -> String {
        "peerCardPosition." + Data(peer.id.utf8).base64EncodedString()
    }

    private func openNetwork() {
        selectedPeer = nil
        localDetails = false
        model.studioSection = "Network"
        model.showStudio = true
    }

    private func localInspector(_ machine: MachineInfo) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("This Mac", systemImage: machine.symbolName).font(.title3.bold())
            Text(machine.hostname).font(.headline).textSelection(.enabled)
            SpecRow(label: "Hardware", value: machine.modelName)
            SpecRow(label: "Chip", value: machine.chip)
            SpecRow(label: "Memory", value: Format.memory(machine.memoryTotalBytes))
            SpecRow(label: "System", value: machine.osVersion)
            SpecRow(label: "Assistant", value: model.loadedRepo.map(Format.shortName) ?? "None loaded")
            if let endpoint = model.endpoint {
                Text(endpoint + "/v1").font(.caption.monospaced()).textSelection(.enabled)
                Button("Copy Local Endpoint") { model.copyEndpoint() }
            }
            Divider()
            Text("Request counts on this card are reported by your local engine. No remote traffic is implied.")
                .font(.caption).foregroundStyle(.secondary)
            Button("Open Network Controls") { openNetwork() }
        }.padding(20).frame(width: 340)
    }

    private func peerInspector(_ peer: Peer) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Discovered · Unpaired", systemImage: "network").font(.title3.bold())
            Text(peer.name).font(.headline).textSelection(.enabled)
            Text("These details are advertised by a nearby server. Its identity and capabilities have not been verified.")
                .font(.callout).foregroundStyle(.secondary)
            if let chip = peer.chip { SpecRow(label: "Advertised chip", value: chip) }
            SpecRow(label: "Advertised assistant", value: peer.model.map(Format.shortName) ?? "Not advertised")
            Text("Addresses").font(.headline)
            ForEach(peer.addresses, id: \.self) { address in
                Text(displayAddress(address, port: peer.port)).font(.caption.monospaced()).textSelection(.enabled)
            }
            if peer.addresses.isEmpty { Text("No address advertised").foregroundStyle(.secondary) }
            Text("Last discovered \(Date(timeIntervalSince1970: peer.seenAt).formatted(date: .omitted, time: .shortened))")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Pair or Route in Network Controls") { openNetwork() }
                if let address = peer.addresses.first {
                    Button("Copy Address") { model.copyToPasteboard(displayAddress(address, port: peer.port)) }
                }
            }
            Text("Review the server and credentials in Network Controls before sending requests. Discovery does not establish a connection or pool memory.")
                .font(.caption).foregroundStyle(.secondary)
        }.padding(20).frame(width: 400)
    }

    private func displayAddress(_ address: String, port: Int) -> String {
        "\(address.contains(":") ? "[\(address)]" : address):\(port)"
    }
}

/// Card centers are stored as canvas fractions, using separate keys for each discovery identity.
/// Drag translation remains transient so updates do not accumulate or drift.
private struct FloatingTopologyCard<Content: View>: View {
    @AppStorage private var savedX: Double
    @AppStorage private var savedY: Double
    let defaultX: Double
    let defaultY: Double
    let layoutRevision: Int
    @ViewBuilder let content: () -> Content
    @GestureState private var translation = CGSize.zero
    @State private var cardSize = CGSize(width: 290, height: 300)

    init(storageKey: String, defaultX: Double, defaultY: Double, layoutRevision: Int,
         @ViewBuilder content: @escaping () -> Content) {
        _savedX = AppStorage(wrappedValue: defaultX, storageKey + "X")
        _savedY = AppStorage(wrappedValue: defaultY, storageKey + "Y")
        self.defaultX = defaultX
        self.defaultY = defaultY
        self.layoutRevision = layoutRevision
        self.content = content
    }

    var body: some View {
        GeometryReader { canvas in
            let origin = bounded(CGPoint(x: canvas.size.width * savedX,
                                         y: canvas.size.height * savedY), in: canvas.size)
            let center = bounded(CGPoint(x: origin.x + translation.width,
                                         y: origin.y + translation.height), in: canvas.size)
            content()
                .fixedSize()
                .background {
                    GeometryReader { card in Color.clear.preference(key: MachineCardSize.self, value: card.size) }
                }
                .onPreferenceChange(MachineCardSize.self) { cardSize = $0 }
                .contentShape(Rectangle())
                .help("Drag to move. Click for details. Double-click to reset position.")
                .onTapGesture(count: 2) { reset() }
                .simultaneousGesture(
                    DragGesture(minimumDistance: 3, coordinateSpace: .named("machineCanvas"))
                        .updating($translation) { value, state, _ in state = value.translation }
                        .onEnded { value in
                            let destination = bounded(CGPoint(x: origin.x + value.translation.width,
                                                              y: origin.y + value.translation.height), in: canvas.size)
                            savedX = destination.x / max(canvas.size.width, 1)
                            savedY = destination.y / max(canvas.size.height, 1)
                        }
                )
                .position(center)
                .accessibilityHint("Drag to reposition. Select to inspect. Double-click to reset position.")
                .accessibilityAction(named: Text("Reset card position")) { reset() }
        }
        .coordinateSpace(name: "machineCanvas")
        .onChange(of: layoutRevision) { reset() }
    }

    private func reset() { savedX = defaultX; savedY = defaultY }

    private func bounded(_ point: CGPoint, in size: CGSize) -> CGPoint {
        let insetX = min(cardSize.width / 2 + 12, size.width / 2)
        let insetY = min(cardSize.height / 2 + 56, size.height / 2)
        return CGPoint(x: min(max(point.x, insetX), size.width - insetX),
                       y: min(max(point.y, insetY), size.height - insetY))
    }
}

private struct MachineCardSize: PreferenceKey {
    static var defaultValue = CGSize(width: 290, height: 300)
    static func reduce(value: inout CGSize, nextValue: () -> CGSize) { value = nextValue() }
}

struct DotGrid: View {
    var spacing: CGFloat = 18

    var body: some View {
        Canvas { context, size in
            var path = Path()
            var y: CGFloat = spacing / 2
            while y < size.height {
                var x: CGFloat = spacing / 2
                while x < size.width {
                    path.addEllipse(in: CGRect(x: x - 0.9, y: y - 0.9, width: 1.8, height: 1.8))
                    x += spacing
                }
                y += spacing
            }
            context.fill(path, with: .color(Color.gray.opacity(0.35)))
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

struct MachineCard: View {
    let machine: MachineInfo
    let stats: LiveStats?
    let highlighted: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("This machine")
                .font(.callout.weight(.semibold))
                .frame(maxWidth: .infinity)
                .padding(.vertical, 7)
                .background(
                    LinearGradient(colors: [.indigo.opacity(0.35), .purple.opacity(0.25), .pink.opacity(0.3)],
                                   startPoint: .leading, endPoint: .trailing)
                )

            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    Image(systemName: machine.symbolName)
                        .font(.title3)
                        .foregroundStyle(.secondary)
                    Text(machine.modelName)
                        .foregroundStyle(.secondary)
                }
                Text(machine.hostname)
                    .font(.title3.weight(.bold))
                    .lineLimit(1)
                    .truncationMode(.middle)

                VStack(spacing: 5) {
                    SpecRow(label: "Chip", value: machine.chip)
                    SpecRow(label: "Memory", value: Format.memory(machine.memoryTotalBytes))
                    SpecRow(label: "SSD", value: Format.bytes(machine.diskTotalBytes))
                }

                if let stats {
                    Divider()
                    UsageRow(label: "Memory Usage", value: Format.memory(stats.memoryUsedBytes),
                             fraction: stats.memoryFraction)
                    UsageRow(label: "CPU", value: String(format: "%.0f%%", stats.cpuPercent),
                             fraction: stats.cpuPercent / 100)
                }
            }
            .padding(16)
        }
        .frame(width: 290)
        .background(.background, in: RoundedRectangle(cornerRadius: 16))
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .overlay(
            RoundedRectangle(cornerRadius: 16)
                .stroke(highlighted ? Color.accentColor : Color.indigo.opacity(0.7), lineWidth: 2)
        )
        .shadow(color: .black.opacity(0.18), radius: 14, y: 6)
    }
}

private struct SpecRow: View {
    let label: String
    let value: String

    var body: some View {
        HStack {
            Text(label).foregroundStyle(.secondary)
            Spacer()
            Text(value).fontWeight(.medium)
        }
        .font(.callout)
    }
}

private struct UsageRow: View {
    let label: String
    let value: String
    let fraction: Double

    var body: some View {
        VStack(spacing: 4) {
            HStack {
                Text(label).foregroundStyle(.secondary)
                Spacer()
                Text(value).monospacedDigit()
            }
            .font(.callout)
            ProgressView(value: min(max(fraction, 0), 1))
                .progressViewStyle(.linear)
                .tint(fraction > 0.85 ? Color.red : (fraction > 0.65 ? Color.orange : Color.accentColor))
        }
    }
}


/// Discovery metadata only. This card makes no identity, capability, or connection claim.
struct PeerCard: View {
    let peer: Peer
    var pooled = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: "network")
                Text(peer.name.replacingOccurrences(of: "Bridle on ", with: "")).fontWeight(.semibold).lineLimit(1)
            }
            Text(pooled ? "Connected for pooling" : "Discovered · Unpaired").font(.caption.weight(.medium))
                .foregroundStyle(pooled ? .green : .orange)
            if let chip = peer.chip { Text("Advertised: " + chip).font(.caption).foregroundStyle(.secondary) }
            Text(peer.model.map { "Advertised: " + Format.shortName($0) } ?? "Assistant not advertised")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("\(peer.addresses.first.map { $0.contains(":") ? "[\($0)]" : $0 } ?? "?"):\(peer.port)")
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.tertiary)
        }
        .padding(10)
        .frame(width: 210, alignment: .leading)
        .background(.background.opacity(0.8), in: RoundedRectangle(cornerRadius: 10))
        .overlay {
            if pooled {
                RoundedRectangle(cornerRadius: 10).strokeBorder(MeshNodeButton.glow, lineWidth: 2)
                    .shadow(color: .purple.opacity(0.6), radius: 8)
            } else {
                RoundedRectangle(cornerRadius: 10).strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
                    .foregroundStyle(.secondary)
            }
        }
    }
}
