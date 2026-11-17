import Foundation

struct ChatTurn: Identifiable, Equatable, Codable {
    enum Role: String, Codable { case system, user, assistant }

    let id: UUID
    let role: Role
    var text: String
    var reasoning: String
    var images: [String]              // data: URLs
    var toolCalls: [String]           // "name(arguments)" for display
    var footnote: String?
    var failed: Bool

    init(role: Role, text: String, images: [String] = [], footnote: String? = nil, failed: Bool = false) {
        self.id = UUID()
        self.role = role
        self.text = text
        self.reasoning = ""
        self.images = images
        self.toolCalls = []
        self.footnote = footnote
        self.failed = failed
    }
}

/// Selects the most recent turns that fit an approximate token budget, so a
/// long conversation degrades by forgetting the oldest turns instead of
/// failing every send once it outgrows the model's context window.
enum ChatHistory {
    static func windowed(_ turns: [ChatTurn], contextLength: Int?, maxTokens: Int) -> [ChatTurn] {
        let budget = max(256, (contextLength ?? 8192) - maxTokens - 256)
        var kept: [ChatTurn] = []
        var used = 0
        for turn in turns.reversed() {
            // Rough estimate: ~4 characters per token, plus a flat cost per image.
            let cost = max(1, turn.text.count / 4) + turn.images.count * 256
            if !kept.isEmpty && used + cost > budget { break }
            kept.append(turn)
            used += cost
        }
        return kept.reversed()
    }
}

struct ChatUsage: Equatable {
    let promptTokens: Int
    let cachedTokens: Int
    let completionTokens: Int
    let generationTps: Double
    let peakMemoryGB: Double
    let draftAccepted: Int
    let batched: Bool
}

enum ChatStreamEvent: Equatable {
    case delta(String)
    case reasoning(String)
    case toolCalls([String])
    case finished(reason: String)
    case usage(ChatUsage)
    case failure(String)
    case done
}

/// Parses OpenAI-style server-sent events, one `data:` line at a time.
enum SSEParser {
    static func parse(line: String) -> [ChatStreamEvent] {
        guard line.hasPrefix("data:") else { return [] }
        let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
        if payload == "[DONE]" { return [.done] }
        guard let data = payload.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [] }

        if let err = obj["error"] as? [String: Any] {
            return [.failure(err["message"] as? String ?? "generation failed")]
        }
        var events: [ChatStreamEvent] = []
        if let choices = obj["choices"] as? [[String: Any]], let first = choices.first {
            if let delta = first["delta"] as? [String: Any] {
                if let r = delta["reasoning_content"] as? String, !r.isEmpty { events.append(.reasoning(r)) }
                if let text = delta["content"] as? String, !text.isEmpty { events.append(.delta(text)) }
                if let calls = delta["tool_calls"] as? [[String: Any]], !calls.isEmpty {
                    events.append(.toolCalls(calls.compactMap { call in
                        guard let fn = call["function"] as? [String: Any], let name = fn["name"] as? String else { return nil }
                        return "\(name)(\(fn["arguments"] as? String ?? ""))"
                    }))
                }
            }
            if let reason = first["finish_reason"] as? String {
                events.append(.finished(reason: reason))
            }
        }
        if let usage = obj["usage"] as? [String: Any] {
            let perf = obj["bridle"] as? [String: Any] ?? [:]
            let details = usage["prompt_tokens_details"] as? [String: Any] ?? [:]
            events.append(.usage(ChatUsage(
                promptTokens: usage["prompt_tokens"] as? Int ?? 0,
                cachedTokens: details["cached_tokens"] as? Int ?? 0,
                completionTokens: usage["completion_tokens"] as? Int ?? 0,
                generationTps: (perf["generation_tps"] as? NSNumber)?.doubleValue ?? 0,
                peakMemoryGB: (perf["peak_memory_gb"] as? NSNumber)?.doubleValue ?? 0,
                draftAccepted: perf["draft_accepted_tokens"] as? Int ?? 0,
                batched: perf["batched"] as? Bool ?? false
            )))
        }
        return events
    }
}
