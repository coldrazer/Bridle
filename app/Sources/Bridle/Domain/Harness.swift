import Foundation

/// An external agent harness that can drive the model Bridle is serving.
/// Only harnesses with a supported way to reach a local endpoint are listed:
/// Kiro and Antigravity were removed (neither accepts a custom model endpoint).
enum Harness: String, CaseIterable, Identifiable {
    case deepseek
    case claudeCode
    case codex
    case opencode
    case lemonade

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .deepseek: return "Deepseek harness"
        case .claudeCode: return "Claude Code"
        case .codex: return "Codex"
        case .opencode: return "OpenCode"
        case .lemonade: return "Lemonade"
        }
    }

    /// Migrates values persisted by the old string-based picker (including the
    /// since-removed Antigravity/Kiro entries, which fall back to Deepseek).
    static func fromPersisted(_ raw: String?) -> Harness {
        switch raw {
        case "Deepseek harness", "deepseek": return .deepseek
        case "Claude Code", "claudeCode": return .claudeCode
        case "Codex", "codex": return .codex
        case "OpenCode", "opencode": return .opencode
        case "Lemonade", "lemonade": return .lemonade
        default: return .deepseek
        }
    }

    /// Executables that prove the harness is installed, best first.
    var binaryCandidates: [String] {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        switch self {
        case .deepseek:
            return ["\(home)/.local/bin/dshctl", "/opt/homebrew/bin/dshctl", "/usr/local/bin/dshctl",
                    "/opt/homebrew/bin/dsh", "/usr/local/bin/dsh"]
        case .claudeCode:
            return ["\(home)/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
        case .codex:
            return ["\(home)/.local/bin/codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex"]
        case .opencode:
            return ["\(home)/.opencode/bin/opencode", "\(home)/.local/bin/opencode",
                    "/opt/homebrew/bin/opencode", "/usr/local/bin/opencode"]
        case .lemonade:
            return ["/usr/local/bin/lemonade", "/opt/homebrew/bin/lemonade", "\(home)/.local/bin/lemonade"]
        }
    }

    var installHint: String {
        switch self {
        case .deepseek: return "Install DeepSeek Harness (the desktop app or `dsh`), then relaunch Bridle."
        case .claudeCode: return "Install Claude Code, then relaunch Bridle."
        case .codex: return "Install the Muse (npm i -g @openai/codex), then relaunch Bridle."
        case .opencode: return "Install OpenCode (opencode.ai), then relaunch Bridle."
        case .lemonade: return "Install Lemonade and start its server, then relaunch Bridle."
        }
    }

    /// One-line description of how selection wires this harness to Bridle.
    var wiringSummary: String {
        switch self {
        case .deepseek:
            return "Launch registers Bridle as a provider in DeepSeek Harness (dshctl add-model) for the loaded model."
        case .claudeCode:
            return "Launch opens Claude Code pointed at Bridle's Anthropic API (ANTHROPIC_BASE_URL) with the loaded model."
        case .codex:
            return "Launch opens Codex with a Bridle provider override (Responses API) for the loaded model; ~/.codex/config.toml is untouched."
        case .opencode:
            return "Launch opens OpenCode with a Bridle provider config (OPENCODE_CONFIG) for the loaded model."
        case .lemonade:
            return "Launch registers Bridle as an OpenAI-compatible provider in your Lemonade server (lemonade cloud install)."
        }
    }
}
