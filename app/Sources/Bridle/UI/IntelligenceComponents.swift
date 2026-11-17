import SwiftUI
import AppKit
import PDFKit
import Foundation

struct IntelligenceStepDraft {
    var kind: String
    var template = "Answer {input} using these passages. Cite source numbers when available.\n{context}"
    var contains = ""
    var validationFormat = "contains"
    var schema: Any?
    var limit = 6
    init(kind: String) { self.kind = kind }
    init(_ row: [String: Any]) {
        kind = row["type"] as? String ?? "input"
        template = row["template"] as? String ?? template
        contains = row["contains"] as? String ?? ""
        validationFormat = row["format"] as? String ?? "contains"
        schema = row["schema"]
        limit = row["limit"] as? Int ?? 6
    }
    var payload: [String: Any] {
        switch kind {
        case "retrieve": return ["type": kind, "limit": limit]
        case "prompt": return ["type": kind, "template": template]
        case "validate":
            if validationFormat == "json" {
                var result: [String: Any] = ["type": kind, "format": "json"]
                if let schema { result["schema"] = schema }
                return result
            }
            return ["type": kind, "contains": contains]
        default: return ["type": kind]
        }
    }
}

struct IntelligenceCaseDraft {
    var prompt = ""
    var expected = ""
}

struct IntelligenceStepEditor: View {
    @Binding var step: IntelligenceStepDraft
    let index: Int
    let canMoveUp: Bool
    let moveUp: () -> Void
    let remove: () -> Void
    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("\(index + 1). \(step.kind.capitalized)").font(.headline)
                    Spacer()
                    Button(action: moveUp) { Image(systemName: "arrow.up") }.help("Move step earlier").disabled(!canMoveUp)
                    Button(role: .destructive, action: remove) { Image(systemName: "trash") }.help("Remove step")
                }
                switch step.kind {
                case "input": Text("Accept the text entered when this workflow runs.").foregroundStyle(.secondary)
                case "retrieve": Stepper("Source passages: \(step.limit)", value: $step.limit, in: 1...12)
                case "prompt":
                    TextField("Template", text: $step.template, axis: .vertical).textFieldStyle(.roundedBorder).lineLimit(3...8)
                    Text("Use {input}, {context}, and {output}.").font(.caption).foregroundStyle(.secondary)
                case "validate":
                    Picker("Validation", selection: $step.validationFormat) {
                        Text("Contains text").tag("contains")
                        Text("Valid JSON").tag("json")
                    }
                    if step.validationFormat == "json" {
                        Text(step.schema == nil ? "The answer must parse as valid JSON." : "The answer must be valid JSON and match this workflow's saved JSON Schema.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        TextField("Answer must contain this text", text: $step.contains).textFieldStyle(.roundedBorder)
                    }
                case "export": Text("Write the generated answer as a text file inside Bridle's data folder.").foregroundStyle(.secondary)
                default: EmptyView()
                }
            }.padding(4)
        }
    }
}

struct IntelligenceCitations: View {
    let citations: [[String: Any]]
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(citations.indices, id: \.self) { i in
                DisclosureGroup("[\(i + 1)] \(citations[i]["filename"] as? String ?? "Source") · page \(citations[i]["page"] as? Int ?? 1) · chunk \(citations[i]["chunk"] as? Int ?? 1)") {
                    Text(citations[i]["text"] as? String ?? "")
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
                }
            }
        }
    }
}

struct IntelligenceJobResult: View {
    let job: [String: Any]
    private var result: [String: Any] { job["result"] as? [String: Any] ?? [:] }
    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("Result").font(.headline)
                    Text((job["status"] as? String ?? "").capitalized).foregroundStyle(.secondary)
                    if ["pending", "running"].contains(job["status"] as? String ?? "") { ProgressView().controlSize(.small) }
                    Spacer()
                }
                if let error = job["error"] as? String, !error.isEmpty { Text(error).foregroundStyle(.red).textSelection(.enabled) }
                if let text = result["text"] as? String {
                    Text(text).textSelection(.enabled)
                    Button("Copy Answer", systemImage: "doc.on.doc") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(text, forType: .string)
                    }
                }
                if let passed = result["passed"] as? Int, let total = result["total"] as? Int {
                    Text("\(passed) of \(total) cases passed").font(.headline)
                }
                if let cases = result["cases"] as? [[String: Any]] {
                    ForEach(cases.indices, id: \.self) { i in
                        DisclosureGroup {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("Expected: \(cases[i]["expected"] as? String ?? "")").foregroundStyle(.secondary)
                                Text(cases[i]["actual"] as? String ?? "").textSelection(.enabled)
                            }.frame(maxWidth: .infinity, alignment: .leading)
                        } label: {
                            Label(cases[i]["prompt"] as? String ?? "Case", systemImage: (cases[i]["passed"] as? Bool ?? false) ? "checkmark.circle" : "xmark.circle")
                        }
                    }
                }
                if result["validated"] as? Bool == true { Label("Output validation passed", systemImage: "checkmark.seal") }
                if let exportedURL {
                    Button("Show Exported File in Finder", systemImage: "folder") {
                        NSWorkspace.shared.activateFileViewerSelecting([exportedURL])
                    }
                }
                IntelligenceCitations(citations: result["citations"] as? [[String: Any]] ?? [])
            }.frame(maxWidth: .infinity, alignment: .leading).padding(6)
        }
    }

    private var exportedURL: URL? {
        guard let relative = result["export"] as? String,
              relative.hasPrefix("intelligence-exports/"),
              !relative.split(separator: "/").contains(".."),
              !relative.contains("\\") else { return nil }
        let support = EngineLocator.supportDirectory.standardizedFileURL.resolvingSymlinksInPath()
        let root = support.appendingPathComponent("intelligence-exports", isDirectory: true)
            .standardizedFileURL.resolvingSymlinksInPath()
        let url = support.appendingPathComponent(relative)
            .standardizedFileURL.resolvingSymlinksInPath()
        guard root.path.hasPrefix(support.path + "/"), url.path.hasPrefix(root.path + "/"),
              FileManager.default.fileExists(atPath: url.path) else { return nil }
        return url
    }
}

enum IntelligenceDocumentImport {
    static func payload(_ url: URL) throws -> [String: Any] {
        let scope = url.startAccessingSecurityScopedResource()
        defer { if scope { url.stopAccessingSecurityScopedResource() } }
        let bytes = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
        guard bytes <= 20_000_000 else { throw importError("Choose a file smaller than 20 MB.") }
        let payload: [String: Any]
        if url.pathExtension.lowercased() == "pdf" {
            guard let pdf = PDFDocument(url: url), pdf.pageCount <= 1000 else { throw importError("This PDF cannot be read or has more than 1,000 pages.") }
            var pages: [String] = []
            var count = 0
            for index in 0..<pdf.pageCount {
                let text = pdf.page(at: index)?.string ?? ""
                count += text.utf8.count
                guard count <= 1_000_000 else { throw importError("Extracted text is too large. Import a smaller PDF.") }
                pages.append(text)
            }
            guard pages.contains(where: { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else {
                throw importError("This PDF has no extractable text. OCR scanned pages before importing.")
            }
            payload = ["name": url.lastPathComponent, "pages": pages]
        } else {
            guard bytes <= 1_000_000 else { throw importError("Import text files smaller than 1 MB.") }
            let text = try String(contentsOf: url, encoding: .utf8)
            payload = ["name": url.lastPathComponent, "text": text]
        }
        guard try JSONSerialization.data(withJSONObject: payload).count <= 2_000_000 else {
            throw importError("The text request exceeds 2 MB. Import a smaller document.")
        }
        return payload
    }
    private static func importError(_ message: String) -> NSError { NSError(domain: "BridleImport", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
}
