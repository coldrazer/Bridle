import SwiftUI

/// Runs one prompt through 2–4 models (loaded one after another, so memory never has to hold
/// more than the largest) and shows answers, speed and memory side by side.
struct CompareView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var selected: [String] = []
    @State private var prompt = ""
    @State private var system = ""
    @State private var maxTokens = 256
    @State private var temperature = 0.0

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Compare Models").font(.title2.weight(.semibold))
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(16)
            Divider()
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Models (2–4)").font(.headline)
                    ForEach(model.downloadedModels.filter { $0.kind == "text" }) { m in
                        Toggle(isOn: Binding(
                            get: { selected.contains(m.repo) },
                            set: { on in
                                if on, !selected.contains(m.repo), selected.count < 4 { selected.append(m.repo) }
                                if !on { selected.removeAll { $0 == m.repo } }
                            })) {
                            Text(m.name)
                        }
                        .toggleStyle(.checkbox)
                    }
                    if model.downloadedModels.filter({ $0.kind == "text" }).count < 2 {
                        Text("Download at least two text models to compare.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(width: 220, alignment: .leading)
                VStack(alignment: .leading, spacing: 8) {
                    TextField("System prompt (optional)", text: $system)
                        .textFieldStyle(.roundedBorder)
                    TextField("Prompt", text: $prompt, axis: .vertical)
                        .lineLimit(3...6)
                        .textFieldStyle(.roundedBorder)
                    HStack {
                        Stepper("Max tokens: \(maxTokens)", value: $maxTokens, in: 16...4096, step: 64)
                        Text("Temperature")
                        Slider(value: $temperature, in: 0...1.5, step: 0.05).frame(width: 120)
                        Text(String(format: "%.2f", temperature)).monospacedDigit()
                        Spacer()
                        if model.compareJob?.state == "running" {
                            Button("Stop") { model.cancelCompare() }
                        } else {
                            Button("Run") {
                                model.startCompare(models: selected, prompt: prompt, system: system,
                                                   maxTokens: maxTokens, temperature: temperature)
                            }
                            .keyboardShortcut(.defaultAction)
                            .disabled(selected.count < 2 || prompt.trimmingCharacters(in: .whitespaces).isEmpty)
                        }
                    }
                    Text("Models are loaded one at a time, then your current model is restored.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(16)
            Divider()
            if let job = model.compareJob {
                ScrollView(.horizontal) {
                    HStack(alignment: .top, spacing: 12) {
                        ForEach(Array(job.runs.enumerated()), id: \.offset) { _, run in
                            CompareColumn(run: run)
                        }
                    }
                    .padding(16)
                }
            } else {
                Spacer()
                Text("Pick models and a prompt, then Run.").foregroundStyle(.secondary)
                Spacer()
            }
        }
        .frame(width: 980, height: 680)
        .onAppear {
            if selected.isEmpty, let loaded = model.loadedRepo { selected = [loaded] }
        }
    }
}

private struct CompareColumn: View {
    let run: CompareRun

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(Format.shortName(run.repo)).font(.headline).lineLimit(1)
                Spacer()
                stateBadge
            }
            if run.state == "done" {
                HStack(spacing: 10) {
                    Text(String(format: "%.1f tok/s", run.generationTps)).bold()
                    Text("\(run.completionTokens) tok")
                    Text(String(format: "%.2f GB", run.peakMemoryGb))
                    Text(String(format: "%.1fs", run.seconds))
                }
                .font(.caption)
                .monospacedDigit()
                .foregroundStyle(.secondary)
            }
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    if !run.reasoning.isEmpty {
                        Text(run.reasoning).font(.callout).foregroundStyle(.secondary)
                    }
                    Text(run.error.isEmpty ? run.text : run.error)
                        .foregroundStyle(run.error.isEmpty ? Color.primary : Color.red)
                }
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(12)
        .frame(width: 300, height: 420, alignment: .top)
        .background(.background, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(.quaternary))
    }

    @ViewBuilder private var stateBadge: some View {
        switch run.state {
        case "loading": HStack(spacing: 4) { ProgressView().controlSize(.mini); Text("Loading").font(.caption) }
        case "generating": HStack(spacing: 4) { ProgressView().controlSize(.mini); Text("Generating").font(.caption) }
        case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case "failed": Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
        default: Text("Waiting").font(.caption).foregroundStyle(.secondary)
        }
    }
}
