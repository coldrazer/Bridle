import AppKit
import PDFKit
import UniformTypeIdentifiers

@MainActor
extension AppModel {
    func pickDocument() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.pdf, .plainText]
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url { importDocument(url) }
    }
    func importDocument(_ url: URL) {
        guard !isGenerating else { report(EngineError(status: 409, message: "Wait for the reply or stop it before importing a document.")); return }
        Task {
            do {
                let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
                guard size <= 20_000_000 else { throw EngineError(status: 413, message: "Documents must be under 20 MB.") }
                var pages: [String] = []
                if url.pathExtension.lowercased() == "pdf" {
                    guard let pdf = PDFDocument(url: url), !pdf.isLocked else { throw EngineError(status: 400, message: "This PDF cannot be read. Unlock it first.") }
                    guard pdf.pageCount <= 500 else { throw EngineError(status: 413, message: "Import a section of this PDF with 500 pages or fewer.") }
                    var bytes = 0
                    for i in 0..<min(pdf.pageCount, 500) {
                        let page = pdf.page(at: i)?.string ?? ""
                        bytes += page.utf8.count
                        guard bytes <= 1_000_000 else { throw EngineError(status: 413, message: "This document has too much text. Import a smaller section.") }
                        pages.append(page)
                    }
                } else {
                    let data = try Data(contentsOf: url)
                    guard data.count <= 1_000_000, let text = String(data: data, encoding: .utf8) else { throw EngineError(status: 400, message: "Choose a UTF-8 text or Markdown file under 1 MB.") }
                    pages = [text]
                }
                guard pages.contains(where: { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else { throw EngineError(status: 400, message: "No readable text found. Scanned PDFs need OCR first.") }
                var destinationID = activeProjectID
                if destinationID == "personal" {
                    let project = try await featureRequest("POST", "/control/intelligence/projects", body: ["name": "Personal"])
                    if let id = project["id"] as? String { destinationID = id; switchProject(id) }
                }
                _ = try await featureRequest("POST", "/control/intelligence/projects/\(destinationID)/documents", body: ["name": url.lastPathComponent, "pages": pages])
                studioSection = "Documents"
                showStudio = true
            } catch { report(error) }
        }
    }
}
