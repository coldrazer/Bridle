import SwiftUI

struct RecommendationView: View {
    @Environment(AppModel.self) private var model
    @State private var task: BeginnerAssistant = .everyday
    @State private var preferSpeed = false
    private var candidates: [CatalogModel] {
        let kind = task == .photo ? "vision" : "text"
        return model.catalog.filter { $0.kind == kind && $0.fit == "fits" }
    }
    private var recommendation: CatalogModel? {
        if preferSpeed {
            let measured = model.benchmarks.filter { result in candidates.contains { $0.repo == result.repo } }
                .sorted { $0.generationTps > $1.generationTps }
            if let best = measured.first, let entry = candidates.first(where: { $0.repo == best.repo }) { return entry }
        }
        return task.recommendation(in: model.catalog, memoryBytes: model.status?.machine.memoryTotalBytes ?? 0)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Find the right assistant").font(.largeTitle.bold())
            Picker("What do you want to do?", selection: $task) {
                ForEach(BeginnerAssistant.allCases) { Text($0.rawValue).tag($0) }
            }.pickerStyle(.segmented)
            Toggle("Prefer measured speed on this Mac", isOn: $preferSpeed)
            if let entry = recommendation {
                GroupBox {
                    VStack(alignment: .leading, spacing: 12) {
                        Label(entry.name, systemImage: task.symbol).font(.title2.bold())
                        Text(task.detail)
                        Text("Fits the engine's memory budget. Estimated working memory: \(Format.bytes(entry.requiredBytes)).")
                        if let result = model.benchmarks.filter({ $0.repo == entry.repo }).max(by: { $0.createdAt < $1.createdAt }) {
                            Text(String(format: "Measured here: %.1f tok/s · first token %d ms", result.generationTps, result.timeToFirstTokenMs))
                        } else { Text("No local benchmark yet; this recommendation uses task suitability and memory fit.").foregroundStyle(.secondary) }
                        Button(entry.downloaded ? "Load assistant" : "Download assistant") { model.choose(entry.repo) }
                    }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
                }
            } else { ContentUnavailableView("No safe recommendation yet", systemImage: "memorychip", description: Text("Start the engine and check available memory or browse models manually.")) }
            Button("Run a benchmark on the loaded model") { model.runBenchmark(promptTokens: 128, generationTokens: 64) }.disabled(!model.isModelReady || model.isBenchmarking)
            Spacer()
        }.padding(24).task { await model.refreshBenchmarks(); await model.refreshCatalog() }
    }
}
