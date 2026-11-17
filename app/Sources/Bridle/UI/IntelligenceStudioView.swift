import SwiftUI
import AppKit
import PDFKit
import UniformTypeIdentifiers

/// Project-scoped tools backed by the engine's durable local workspace.
struct IntelligenceStudioView: View {
    @Environment(AppModel.self) private var model
    let section: String
    @State private var projects: [[String: Any]] = []
    @State private var rows: [[String: Any]] = []
    @State private var projectID = ""
    @State private var title = ""
    @State private var input = ""
    @State private var promptTemplate = "Answer {input} using the context below. Cite sources when available.\n{context}"
    @State private var editingID = ""
    @State private var selectedPrompt: [String: Any] = [:]
    @State private var steps: [IntelligenceStepDraft] = [.init(kind: "input"), .init(kind: "retrieve"), .init(kind: "prompt")]
    @State private var cases: [IntelligenceCaseDraft] = [.init()]
    @State private var citations: [[String: Any]] = []
    @State private var trackedJobID = ""
    @State private var trackedJob: [String: Any] = [:]
    @State private var jobWorkspace = "local"
    @State private var error = ""
    @State private var notice = ""
    @State private var busy = false
    private let base = "/control/intelligence"

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                if !error.isEmpty { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red).textSelection(.enabled) }
                if !notice.isEmpty { Text(notice).foregroundStyle(.secondary) }
                if section == "Projects" { projectsContent }
                else if projectID.isEmpty {
                    ContentUnavailableView("Create a project first", systemImage: "folder.badge.plus", description: Text("Projects keep your chats, documents, prompts, and results together. Open Projects to create one."))
                } else {
                    switch section {
                    case "Documents": documentsContent
                    case "Jobs": jobsContent
                    case "Prompts": promptsContent
                    case "Workflows": workflowsContent
                    case "Evaluations": evaluationsContent
                    default: Text("Choose a workspace tool.").foregroundStyle(.secondary)
                    }
                }
                if !trackedJob.isEmpty { IntelligenceJobResult(job: trackedJob) }
            }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
        }
        .task(id: section) { resetEditor(); await initialize() }
        .task(id: section + projectID) {
            await refresh()
            guard section == "Jobs" else { return }
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
                await refresh()
            }
        }
        .task(id: trackedJobID) {
            guard !trackedJobID.isEmpty else { return }
            let id = trackedJobID
            while !Task.isCancelled {
                do {
                    let job = try await model.featureRequest("GET", base + "/jobs/" + id)
                    guard id == trackedJobID else { return }
                    trackedJob = job
                    let status = job["status"] as? String ?? ""
                    if !["pending", "running"].contains(status) { await refresh(); return }
                    try await Task.sleep(for: .seconds(1))
                } catch {
                    if !Task.isCancelled { self.error = error.localizedDescription }
                    return
                }
            }
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 5) {
                Text(section).font(.largeTitle.bold())
                Text(subtitle).font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            if section != "Projects", !projects.isEmpty {
                Picker("Project", selection: $projectID) {
                    ForEach(projects.indices, id: \.self) { i in
                        Text(projects[i]["name"] as? String ?? "Project").tag(projects[i]["id"] as? String ?? "")
                    }
                }.frame(maxWidth: 260).disabled(model.isGenerating)
                    .onChange(of: projectID) { _, value in
                        openProject(value)
                        resetEditor()
                    }
            }
            Button { Task { await initialize() } } label: { Image(systemName: "arrow.clockwise") }.help("Refresh workspace")
            if busy { ProgressView().controlSize(.small) }
        }
    }

    private var subtitle: String {
        switch section {
        case "Projects": return "Keep your local chats and tools organized by project."
        case "Documents": return "Search extracted text locally and ask your loaded model with source citations. Retrieval uses lexical matching."
        case "Jobs": return "Run prompts in the background. Results survive restarting Bridle; interrupted work requires an explicit retry."
        case "Prompts": return "Save reusable templates with a history of every version."
        case "Workflows": return "Arrange typed steps to retrieve, generate, validate, and export locally."
        case "Evaluations": return "Check actual model answers against expected text. A loaded model is required."
        default: return ""
        }
    }

    private var projectsContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                TextField("New project name", text: $title).textFieldStyle(.roundedBorder)
                Button("Create Project") {
                    perform {
                        let p = try await model.featureRequest("POST", base + "/projects", body: ["name": title])
                        projectID = p["id"] as? String ?? ""
                        openProject(projectID)
                        title = ""
                        await initialize()
                    }
                }.disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || busy || model.isGenerating)
            }
            ForEach(projects.indices, id: \.self) { i in
                let p = projects[i]
                VStack(alignment: .leading, spacing: 8) {
                  HStack {
                    Label(p["name"] as? String ?? "Project", systemImage: "folder")
                    Spacer()
                    if model.activeProjectID == p["id"] as? String {
                        Label("Active", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                    } else {
                        Button("Open Project") {
                            projectID = p["id"] as? String ?? ""
                            openProject(projectID)
                            notice = "Project opened. Its chat is available in the main workspace."
                        }.disabled(model.isGenerating)
                    }
                  }
                  IntelligenceProjectSettings(project: p) { Task { await initialize() } }
                }.padding(12).background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 10))
            }
            if projects.isEmpty { Text("Create your first project to begin.").foregroundStyle(.secondary) }
        }
    }

    private var documentsContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Button("Import TXT, Markdown, or PDF", systemImage: "doc.badge.plus") { importDocument() }.disabled(busy)
                Text("Text stays in this project's local database.").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(rows.indices, id: \.self) { i in
                HStack {
                    Label(rows[i]["name"] as? String ?? "Document", systemImage: "doc.text")
                    Text("\(rows[i]["page_count"] as? Int ?? 1) pages · \(rows[i]["characters"] as? Int ?? 0) characters").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Remove", role: .destructive) {
                        let id = rows[i]["id"] as? String ?? ""
                        perform { _ = try await model.featureRequest("DELETE", projectPath + "/documents/" + id); await refresh() }
                    }.disabled(busy)
                }
            }
            Divider()
            TextField("Question or search words", text: $input, axis: .vertical).textFieldStyle(.roundedBorder).lineLimit(2...5)
            HStack {
                Button("Find Passages") {
                    perform {
                        let result = try await model.featureRequest("POST", projectPath + "/search", body: ["query": input])
                        citations = result["citations"] as? [[String: Any]] ?? []
                        notice = citations.isEmpty ? "No lexical matches. Try words from your documents." : "Matching source passages"
                    }
                }
                Button("Ask with Sources", systemImage: "sparkles") {
                    perform {
                        let job = try await model.featureRequest("POST", projectPath + "/ask", body: ["query": input])
                        track(job)
                    }
                }.buttonStyle(.borderedProminent)
                Text("Load this project's saved model, or use the currently loaded model when none is saved.").font(.caption).foregroundStyle(.secondary)
            }.disabled(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || busy)
            IntelligenceCitations(citations: citations)
        }
    }

    private var jobsContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            TextField("Prompt to run in background", text: $input, axis: .vertical).textFieldStyle(.roundedBorder).lineLimit(3...8)
            TaskWorkspacePicker(selection: $jobWorkspace)
            HStack {
                Button("Run Prompt", systemImage: "play.fill") {
                    perform {
                        let binding: [String: Any] = jobWorkspace == "local" ? ["location": "local"]
                            : ["location": "remote", "workspace_id": jobWorkspace]
                        track(try await model.featureRequest("POST", base + "/jobs",
                                                             body: ["project_id": projectID, "prompt": input,
                                                                    "workspace_binding": binding]))
                        await refresh()
                    }
                }.disabled(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || busy)
                Button("Set as Project Default") {
                    perform {
                        let ref: [String: Any] = jobWorkspace == "local" ? ["location": "local"]
                            : ["location": "remote", "workspace_id": jobWorkspace]
                        _ = try await model.featureRequest("PATCH", base + "/projects/" + projectID,
                                                           body: ["workspace_ref": ref])
                        notice = "Project default workspace updated. Existing tasks keep their original workspace."
                    }
                }.disabled(busy)
            }
            Text("The workspace is fixed when the task starts and shown on the task. "
                 + "Choosing a remote folder does not upload it or move the model.")
                .font(.caption).foregroundStyle(.secondary)
            Divider()
            if rows.isEmpty { Text("Your background jobs will appear here.").foregroundStyle(.secondary) }
            ForEach(Array(rows.indices.reversed()), id: \.self) { i in
                let job = rows[i]
                DisclosureGroup {
                    IntelligenceJobResult(job: job)
                    HStack {
                        let status = job["status"] as? String ?? ""
                        if ["failed", "cancelled", "interrupted"].contains(status) {
                            Button("Retry") { perform { track(try await model.featureRequest("POST", base + "/jobs/" + (job["id"] as? String ?? "") + "/retry")); await refresh() } }
                        }
                        Button(["pending", "running"].contains(status) ? "Cancel Job" : "Delete Result", role: .destructive) {
                            perform { _ = try await model.featureRequest("DELETE", base + "/jobs/" + (job["id"] as? String ?? "")); await refresh() }
                        }
                    }.disabled(busy)
                } label: {
                    HStack {
                        Text(jobTitle(job)).lineLimit(1)
                        if let ws = job["workspace"] as? [String: Any], (ws["location"] as? String) == "remote" {
                            Text(ws["canonical_root"] as? String ?? "Remote")
                                .font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer()
                        Text((job["status"] as? String ?? "").capitalized).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private var promptsContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            savedDefinitions { row in
                selectedPrompt = row
                editingID = row["id"] as? String ?? ""
                title = row["name"] as? String ?? ""
                promptTemplate = row["template"] as? String ?? ""
            }
            Text(editingID.isEmpty ? "New prompt" : "Edit prompt · next version").font(.headline)
            TextField("Prompt name", text: $title).textFieldStyle(.roundedBorder)
            TextEditor(text: $promptTemplate).font(.body.monospaced()).frame(minHeight: 150).border(.quaternary)
            Text("Variables: {input}, {context}, {output}. Use double braces for literal braces.").font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Save Version") {
                    perform {
                        var data: [String: Any] = ["project_id": projectID, "name": title, "template": promptTemplate]
                        if !editingID.isEmpty { data["id"] = editingID }
                        selectedPrompt = try await model.featureRequest("POST", base + "/prompts", body: data)
                        editingID = selectedPrompt["id"] as? String ?? ""
                        notice = "Prompt version saved."
                        await refresh()
                    }
                }.disabled(title.isEmpty || promptTemplate.isEmpty || busy)
                Button("New Prompt") { resetEditor() }
            }
            if let versions = selectedPrompt["versions"] as? [[String: Any]], !versions.isEmpty {
                Text("Version history").font(.headline)
                ForEach(Array(versions.indices.reversed()), id: \.self) { i in
                    DisclosureGroup("Version \(versions[i]["version"] as? Int ?? 0)") {
                        Text(versions[i]["template"] as? String ?? "").textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                        Button("Restore in Editor") { promptTemplate = versions[i]["template"] as? String ?? "" }
                    }
                }
            }
        }
    }

    private var workflowsContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            savedDefinitions { row in
                title = row["name"] as? String ?? ""
                editingID = row["id"] as? String ?? ""
                steps = (row["steps"] as? [[String: Any]] ?? []).map(IntelligenceStepDraft.init)
            }
            TextField("Workflow name", text: $title).textFieldStyle(.roundedBorder)
            ForEach(steps.indices, id: \.self) { i in
                IntelligenceStepEditor(step: $steps[i], index: i, canMoveUp: i > 0,
                    moveUp: { steps.swapAt(i, i - 1) }, remove: { steps.remove(at: i) })
            }
            HStack {
                Menu("Add Step", systemImage: "plus") {
                    ForEach(["input", "retrieve", "prompt", "validate", "export"], id: \.self) { kind in
                        Button(kind.capitalized) { steps.append(.init(kind: kind)) }
                    }
                }.disabled(steps.count >= 10)
                Button("Save Workflow") { saveWorkflow(run: false) }.disabled(title.isEmpty || busy)
                Button("New Workflow") { resetEditor() }
            }
            TextField("Workflow input", text: $input, axis: .vertical).textFieldStyle(.roundedBorder).lineLimit(2...5)
            Button("Save and Run", systemImage: "play.fill") { saveWorkflow(run: true) }
                .disabled(title.isEmpty || input.isEmpty || busy)
            Text("Exports are written inside Bridle's local data folder. Steps cannot run shell commands.").font(.caption).foregroundStyle(.secondary)
        }
    }

    private var evaluationsContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            savedDefinitions { row in
                title = row["name"] as? String ?? ""
                editingID = row["id"] as? String ?? ""
                cases = (row["cases"] as? [[String: Any]] ?? []).map { IntelligenceCaseDraft(prompt: $0["prompt"] as? String ?? "", expected: $0["expected"] as? String ?? "") }
            }
            TextField("Evaluation name", text: $title).textFieldStyle(.roundedBorder)
            ForEach(cases.indices, id: \.self) { i in
                GroupBox("Case \(i + 1)") {
                    VStack(alignment: .leading, spacing: 8) {
                        TextField("Prompt", text: $cases[i].prompt, axis: .vertical).textFieldStyle(.roundedBorder)
                        TextField("Expected text in the answer", text: $cases[i].expected).textFieldStyle(.roundedBorder)
                        if cases.count > 1 { Button("Remove Case", role: .destructive) { cases.remove(at: i) } }
                    }.padding(4)
                }
            }
            HStack {
                Button("Add Case", systemImage: "plus") { cases.append(.init()) }.disabled(cases.count >= 25)
                Button("Save Suite") { saveEvaluation(run: false) }.disabled(title.isEmpty || busy)
                Button("Save and Run", systemImage: "play.fill") { saveEvaluation(run: true) }.disabled(title.isEmpty || busy)
                Button("New Suite") { resetEditor() }
            }
            Text("Each case generates an actual model answer. A case passes when its expected text occurs in that answer, ignoring case.").font(.caption).foregroundStyle(.secondary)
        }
    }

    private func savedDefinitions(select: @escaping ([String: Any]) -> Void) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !rows.isEmpty { Text("Saved \(section.lowercased())").font(.headline) }
            ForEach(rows.indices, id: \.self) { i in
                Button(rows[i]["name"] as? String ?? "Untitled") { select(rows[i]) }.buttonStyle(.link)
            }
            Divider()
        }
    }

    private var projectPath: String { base + "/projects/" + projectID }

    private func initialize() async {
        do {
            let response = try await model.featureRequest("GET", base + "/projects")
            projects = response["projects"] as? [[String: Any]] ?? []
            if !projects.contains(where: { $0["id"] as? String == projectID }) {
                projectID = projects.first(where: { $0["id"] as? String == model.activeProjectID })?["id"] as? String ?? projects.first?["id"] as? String ?? ""
            }
            await refresh()
        } catch { self.error = error.localizedDescription }
    }

    private func refresh() async {
        guard !projectID.isEmpty, section != "Projects" else { return }
        let requestedProject = projectID
        let key = section.lowercased()
        let path = section == "Documents" ? projectPath + "/documents" : base + "/" + key + "?project_id=" + projectID
        do {
            let response = try await model.featureRequest("GET", path)
            guard requestedProject == projectID, key == section.lowercased(), !Task.isCancelled else { return }
            rows = response[key] as? [[String: Any]] ?? []
        } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
    }

    private func perform(_ action: @escaping @MainActor () async throws -> Void) {
        busy = true; error = ""; notice = ""
        Task { @MainActor in
            defer { busy = false }
            do { try await action() } catch { self.error = error.localizedDescription }
        }
    }

    private func track(_ job: [String: Any]) {
        trackedJob = job
        trackedJobID = job["id"] as? String ?? ""
        notice = "Job queued. You can leave this tool; its result will remain in Jobs."
    }

    private func resetEditor() {
        editingID = ""; title = ""; selectedPrompt = [:]; citations = []; trackedJob = [:]; trackedJobID = ""
        input = ""; error = ""; notice = ""
        promptTemplate = "Answer {input} using the context below. Cite sources when available.\n{context}"
        steps = [.init(kind: "input"), .init(kind: "retrieve"), .init(kind: "prompt")]
        cases = [.init()]
    }

    private func saveWorkflow(run: Bool) {
        perform {
            var data: [String: Any] = ["project_id": projectID, "name": title, "steps": steps.map(\.payload)]
            if !editingID.isEmpty { data["id"] = editingID }
            let saved = try await model.featureRequest("POST", base + "/workflows", body: data)
            editingID = saved["id"] as? String ?? ""
            if run { track(try await model.featureRequest("POST", base + "/workflows/" + editingID + "/run", body: ["input": input])) }
            else { notice = "Workflow saved." }
            await refresh()
        }
    }

    private func saveEvaluation(run: Bool) {
        perform {
            var data: [String: Any] = ["project_id": projectID, "name": title, "cases": cases.map { ["prompt": $0.prompt, "expected": $0.expected] }]
            if !editingID.isEmpty { data["id"] = editingID }
            let saved = try await model.featureRequest("POST", base + "/evaluations", body: data)
            editingID = saved["id"] as? String ?? ""
            if run { track(try await model.featureRequest("POST", base + "/evaluations/" + editingID + "/run", body: [:])) }
            else { notice = "Evaluation suite saved." }
            await refresh()
        }
    }

    private func importDocument() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.plainText, .pdf, UTType(filenameExtension: "md") ?? .plainText]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.begin { result in
            guard result == .OK, let url = panel.url else { return }
            perform {
                let data = try IntelligenceDocumentImport.payload(url)
                _ = try await model.featureRequest("POST", projectPath + "/documents", body: data)
                notice = "Document imported into this project."
                await refresh()
            }
        }
    }

    private func jobTitle(_ job: [String: Any]) -> String {
        let definition = job["definition"] as? [String: Any] ?? [:]
        return definition["name"] as? String ?? (job["prompt"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? (job["kind"] as? String ?? "Job").capitalized
    }

    private func openProject(_ id: String) {
        guard !model.isGenerating else { return }
        model.switchProject(id)
        guard let project = projects.first(where: { $0["id"] as? String == id }) else { return }
        model.systemPrompt = project["system_prompt"] as? String ?? ""
        let savedModel = project["model"] as? String ?? ""
        if !savedModel.isEmpty { model.selectedRepo = savedModel }
    }
}
