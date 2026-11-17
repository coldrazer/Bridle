import SwiftUI

struct ContentView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("advancedMode") private var advanced = false
    @State private var hasChosenMode = false
    @State private var showAria = false
    var body: some View {
        @Bindable var model = model
        VStack(spacing: 0) {
            if hasChosenMode {
                HStack {
                    Text(advanced ? "Advanced workspace" : "Your personal AI").font(.headline)
                    Spacer()
                    Button("Quick Assistant", systemImage: "sparkles") { QuickAssistantController.shared.toggle() }
                    Button("Projects & Tools", systemImage: "square.grid.2x2") { model.showStudio = true }
                    Button("Change mode") { hasChosenMode = false }
                }.padding(.horizontal, 16).padding(.vertical, 8)
                Divider()
                if advanced { AdvancedCanvasView() } else { BeginnerHomeView() }
            } else {
                VStack(spacing: 24) {
                    BridleMark(full: true).frame(width: 240, height: 100)
                    Text("How would you like to use Bridle?").font(.largeTitle.bold())
                    Text("Both modes run your AI on this Mac. Switch whenever you like.")
                        .foregroundStyle(.secondary)
                    HStack(alignment: .top, spacing: 20) {
                        mode("Beginner", icon: "sparkles", description: "Get a recommended assistant, follow a simple setup, and start chatting.", advanced: false)
                        mode("Advanced", icon: "slider.horizontal.3", description: "Manage models, API access, performance, tools, projects, and connected Macs.", advanced: true)
                        ariaCard()
                    }
                }.padding(40).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .sheet(isPresented: $model.showLocalImport) { LocalModelWizard().environment(model) }
        .sheet(isPresented: $showAria) { AriaView() }
        .sheet(isPresented: $model.showModelPicker) { ModelPickerView().environment(model) }
        .sheet(isPresented: $model.showStudio) { StudioView().environment(model) }
        .alert("Load anyway?", isPresented: Binding(get: { !advanced && model.pendingForceLoad != nil }, set: { if !$0 && !advanced { model.pendingForceLoad = nil } })) {
            Button("Cancel", role: .cancel) { model.pendingForceLoad = nil }
            Button("Load Anyway", role: .destructive) { model.confirmForceLoad() }
        } message: { Text(model.pendingForceLoad?.reason ?? "This assistant may use too much memory.") }
    }
    /// Chooser shortcut: open Aria's chat window right away. She runs on the
    /// small model bundled with the app on this Mac - no setup, no mode choice.
    private func ariaCard() -> some View {
        Button {
            showAria = true
        } label: {
            VStack(alignment: .leading, spacing: 16) {
                Image(systemName: "bubble.left.and.text.bubble.right").font(.largeTitle)
                Text("Chat with Aria").font(.title.bold())
                Text("Skip setup and talk to Aria now. She is Bridle's built-in guide, running live on this Mac.")
                    .foregroundStyle(.secondary).multilineTextAlignment(.leading)
                Label("Chat now", systemImage: "arrow.right")
            }.padding(24).frame(width: 270, height: 220, alignment: .topLeading)
                .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 20))
                .contentShape(RoundedRectangle(cornerRadius: 20))
        }.buttonStyle(.plain)
    }
    private func mode(_ title: String, icon: String, description: String, advanced: Bool) -> some View {
        Button {
            self.advanced = advanced
            hasChosenMode = true
            if !advanced { model.bottomPanel = .chat }
        } label: {
            VStack(alignment: .leading, spacing: 16) {
                Image(systemName: icon).font(.largeTitle)
                Text(title).font(.title.bold())
                Text(description).foregroundStyle(.secondary).multilineTextAlignment(.leading)
                Label("Continue with \(title)", systemImage: "arrow.right")
            }.padding(24).frame(width: 270, height: 220, alignment: .topLeading)
                .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 20))
                .contentShape(RoundedRectangle(cornerRadius: 20))
        }.buttonStyle(.plain)
    }
}
