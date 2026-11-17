import SwiftUI

struct StudioView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var selection: String? = "Projects"
    private let intelligence = ["Projects", "Documents", "Jobs", "Workflows", "Prompts", "Evaluations"]
    private let operations = ["Recommendations", "Queue", "Performance", "Power", "Health", "Connect Apps", "Tools", "MCP", "Network", "Mesh", "Remote Connections"]
    var body: some View {
        VStack(spacing: 0) {
            HStack { Text("Bridle Studio").font(.title2.bold()); Spacer(); Button("Done") { dismiss() } }.padding(16)
            Divider()
            NavigationSplitView {
                List(selection: $selection) {
                    Section("Your work") { ForEach(intelligence, id: \.self) { Text($0).tag($0) } }
                    Section("Advanced") { ForEach(operations, id: \.self) { Text($0).tag($0) } }
                }.navigationSplitViewColumnWidth(180)
            } detail: {
                if let selection {
                    if intelligence.contains(selection) { IntelligenceStudioView(section: selection).id(selection) }
                    else if selection == "Remote Connections" { RemoteConnectionWizard() }
                    else if selection == "Mesh" { MeshView() }
                    else if selection == "Recommendations" { RecommendationView() }
                    else { OperationsStudioView(section: selection).id(selection) }
                }
            }
        }
        .frame(minWidth: 940, idealWidth: 1120, minHeight: 650, idealHeight: 780)
        .onAppear { selection = model.studioSection }
        .onChange(of: model.studioSection) { _, section in selection = section }
    }
}
