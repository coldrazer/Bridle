import Foundation

/// A task-first vocabulary for the curated engine catalog.
enum BeginnerAssistant: String, CaseIterable, Identifiable {
    case everyday = "Everyday", writing = "Writing", coding = "Coding", photo = "Photo"
    var id: String { rawValue }

    var symbol: String {
        switch self {
        case .everyday: return "sparkles"
        case .writing: return "pencil.and.outline"
        case .coding: return "chevron.left.forwardslash.chevron.right"
        case .photo: return "photo"
        }
    }

    var detail: String {
        switch self {
        case .everyday: return "Ask questions, learn something, or plan your day."
        case .writing: return "Draft, rewrite, and find the right words."
        case .coding: return "Explain code and work through programming problems."
        case .photo: return "Attach a photo and ask about what’s in it."
        }
    }

    var prompts: [String] {
        switch self {
        case .everyday: return ["Help me plan a relaxed weekend.", "Explain a difficult idea in simple terms.", "Give me three quick dinner ideas."]
        case .writing: return ["Help me write a friendly thank-you note.", "Give me five opening lines for a short story.", "Help me make my writing clearer. Ask me for a draft."]
        case .coding: return ["Explain how a function works with a simple example.", "Help me debug my code. Ask me what is going wrong.", "Teach me the basics of Python step by step."]
        case .photo: return ["What should I look for when describing a photo?", "Help me write a caption for a photo. Ask me to attach it.", "Help me understand an image. Ask me to attach it first."]
        }
    }

    /// The engine's fit result remains authoritative. Leave room for macOS and other apps,
    /// and never silently offer a model the engine already describes as a tight fit.
    func recommendation(in catalog: [CatalogModel], memoryBytes: Int64) -> CatalogModel? {
        guard memoryBytes > 0 else { return nil }
        let budget = Double(memoryBytes) * 0.6
        let eligible = catalog.filter {
            $0.kind == (self == .photo ? "vision" : "text") && $0.fit == "fits"
                && $0.requiredBytes > 0 && Double($0.requiredBytes) <= budget
        }
        let preferred: [String]
        switch self {
        case .everyday: preferred = ["Qwen3-4B-4bit", "Qwen3-1.7B-4bit", "Llama-3.2-3B-Instruct-4bit", "Qwen3-0.6B-4bit"]
        case .writing: preferred = ["Qwen3-8B-4bit", "Qwen3-4B-4bit", "Llama-3.2-3B-Instruct-4bit", "Qwen3-1.7B-4bit"]
        case .coding: preferred = ["Qwen3-Coder-30B-A3B-Instruct-4bit", "Qwen3-8B-4bit", "Qwen3-4B-4bit", "Qwen3-1.7B-4bit"]
        case .photo: preferred = ["Qwen2.5-VL-3B-Instruct-4bit", "gemma-3-4b-it-4bit", "SmolVLM-256M-Instruct-bf16"]
        }
        return eligible.sorted { lhs, rhs in
            let left = preferred.firstIndex(of: String(lhs.repo.split(separator: "/").last ?? "")) ?? Int.max
            let right = preferred.firstIndex(of: String(rhs.repo.split(separator: "/").last ?? "")) ?? Int.max
            if left != right { return left < right }
            if lhs.downloaded != rhs.downloaded { return lhs.downloaded }
            if lhs.requiredBytes != rhs.requiredBytes { return lhs.requiredBytes < rhs.requiredBytes }
            return lhs.repo < rhs.repo
        }.first
    }
}
