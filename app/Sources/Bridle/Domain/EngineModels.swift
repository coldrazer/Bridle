import Foundation

// Mirrors the engine's /control/* JSON. Decoded with .convertFromSnakeCase.

struct EngineStatus: Decodable, Equatable {
    let version: String
    let server: ServerInfo
    let machine: MachineInfo
    let stats: LiveStats
    let model: ModelRuntime
    let downloads: [DownloadJob]
    let features: Features
    let peers: Int
    let usage: UsageSummary     // last 24 hours
    let aria: HelperStatus?
    let onyx: HelperStatus?
}

struct HelperStatus: Decodable, Equatable {
    let available: Bool
    let loaded: Bool
    let repo: String
}

struct ServerInfo: Decodable, Equatable {
    let state: String
    let mode: String
    let host: String
    let port: Int
    let endpoint: String
    let httpsEndpoint: String?
    let tlsFingerprint: String?
    let lanAddress: String?
    let apiKeyRequired: Bool
    let autoLoad: Bool
    let startedAt: Double
    let uptimeSeconds: Int
    let modelsDir: String
    let ollamaApi: Bool
    let bonjour: Bool
}

struct Features: Decodable, Equatable {
    let vision: Bool
    let embeddings: Bool
    let speech: Bool
    let structuredOutput: Bool
    let bonjour: Bool
    let tls: Bool
}

struct UsageSummary: Decodable, Equatable {
    let requests: Int
    let completionTokens: Int
    let promptTokens: Int
    let errors: Int
    let avgTps: Double
    let windowSeconds: Int
}

struct MachineInfo: Decodable, Equatable {
    let hostname: String
    let modelName: String
    let kind: String
    let chip: String
    let memoryTotalBytes: Int64
    let diskTotalBytes: Int64
    let osVersion: String
    let thunderboltGeneration: Int?
    let thunderboltPorts: Int?
    let rdma: String

    var symbolName: String {
        switch kind {
        case "laptop": return "laptopcomputer"
        case "mini": return "macmini"
        case "studio": return "macstudio"
        case "pro": return "macpro.gen3"
        default: return "desktopcomputer"
        }
    }

    var canCluster: Bool { thunderboltGeneration == 5 }
}

struct LiveStats: Decodable, Equatable {
    let memoryUsedBytes: Int64
    let memoryTotalBytes: Int64
    let cpuPercent: Double
    let diskFreeBytes: Int64
    let mlx: MLXMemory?

    var memoryFraction: Double {
        memoryTotalBytes > 0 ? Double(memoryUsedBytes) / Double(memoryTotalBytes) : 0
    }
}

struct MLXMemory: Decodable, Equatable {
    let activeBytes: Int64
    let peakBytes: Int64
}

struct ModelRuntime: Decodable, Equatable {
    let state: String          // empty | loading | ready | error
    let loadingRepo: String
    let error: String
    let loaded: LoadedModel?
    let unloadedReason: String
    let lastUnloaded: String?
    let metrics: RuntimeMetrics
}

struct DraftInfo: Decodable, Equatable {
    let repo: String
    let sizeBytes: Int64
}

struct LoadedModel: Decodable, Equatable {
    let repo: String
    let kind: String
    let sizeBytes: Int64
    let contextLength: Int?
    let loadSeconds: Double
    let loadedAt: Double
    let lastUsed: Double
    let draft: DraftInfo?
    let supportsTools: Bool
    let supportsReasoning: Bool
}

struct RuntimeMetrics: Decodable, Equatable {
    let requestsTotal: Int
    let requestsFailed: Int
    let tokensGenerated: Int
    let lastGenerationTps: Double
    let lastPromptTps: Double
    let queueDepth: Int
    let active: Bool
    let activeRequests: Int
    let batchedRequests: Int
    let cachedPromptTokens: Int
    let draftAcceptedTokens: Int
}

struct DownloadJob: Decodable, Equatable, Identifiable {
    let id: String
    let repo: String
    let state: String          // queued | resolving | downloading | verifying | completed | failed | cancelled
    let totalBytes: Int64
    let doneBytes: Int64
    let fraction: Double
    let currentFile: String
    let revision: String
    let error: String
    let bytesPerSecond: Double
    let destination: String
    let startedAt: Double
    let finishedAt: Double?

    var isActive: Bool { ["queued", "resolving", "downloading", "verifying"].contains(state) }
}

struct CatalogModel: Decodable, Equatable, Identifiable {
    let repo: String
    let name: String
    let family: String
    let params: String
    let quant: String
    let weightsBytes: Int64
    let requiredBytes: Int64
    let summary: String
    let fit: String            // fits | tight | too_large
    let downloaded: Bool
    let kind: String           // text | vision | embedding | speech
    let draftCandidates: [String]

    var id: String { repo }
    var isChatModel: Bool { kind == "text" || kind == "vision" }
}

struct CatalogResponse: Decodable {
    let models: [CatalogModel]
}

struct FitCheck: Decodable, Equatable {
    let repo: String
    let draft: String?
    let requiredBytes: Int64
    let availableBytes: Int64
    let budgetBytes: Int64
    let verdict: String        // ok | too_large | insufficient_free
    let reason: String
}

struct ReadyLine: Decodable, Equatable {
    let port: Int
    let endpoint: String
    let version: String
}

struct APIErrorBody: Decodable {
    struct Detail: Decodable { let message: String }
    let error: Detail
}

// ---- settings ---------------------------------------------------------------------------------
struct EngineSettings: Codable, Equatable {
    var idleUnloadMinutes: Int
    var batching: Bool
    var maxBatchSize: Int
    var promptCache: Bool
    var memoryGuard: Bool
    var numDraftTokens: Int
    var logContent: Bool
    var localRequiresKey: Bool
    var bonjour: Bool
    var tls: Bool
    var tlsPort: Int
    var ollamaApi: Bool
    var promptCacheDisk: Bool
    var promptCacheDiskMb: Int
    var fastModel: String
    var maxConcurrentRequests: Int
    var queueWaitSeconds: Int
    var allowRemoteImages: Bool
    var storageQuotaGb: Int
}

struct SettingsPatchResponse: Decodable {
    let settings: EngineSettings
    let restartRequired: [String]
}

// ---- API keys -------------------------------------------------------------------------------------
struct ApiKeyInfo: Decodable, Equatable, Identifiable {
    let id: String
    let name: String
    let hint: String
    let createdAt: Double
    let revoked: Bool
    let rpmLimit: Int?
    let dailyTokenLimit: Int?
    let requestsTotal: Int
    let tokensTotal: Int
    let tokensToday: Int
    let lastUsedAt: Double?
    let allowModelLoad: Bool?
}

struct ApiKeyList: Decodable { let keys: [ApiKeyInfo] }

struct CreatedKey: Decodable, Equatable {
    let key: ApiKeyInfo
    let secret: String
}

// ---- request history ---------------------------------------------------------------------------------
struct RequestRow: Decodable, Equatable, Identifiable {
    let id: Int
    let ts: Double
    let endpoint: String
    let model: String
    let client: String
    let remote: String
    let status: Int
    let promptTokens: Int
    let cachedTokens: Int
    let completionTokens: Int
    let durationMs: Int
    let tps: Double
    let finishReason: String
    let error: String
    let prompt: String?
    let response: String?
}

struct RequestList: Decodable {
    let requests: [RequestRow]
    let summary: UsageSummary
}

// ---- presets ----------------------------------------------------------------------------------------
struct Preset: Codable, Equatable, Identifiable, Hashable {
    var name: String
    var model: String
    var systemPrompt: String
    var temperature: Double?
    var topP: Double?
    var maxTokens: Int?

    var id: String { name }
}

struct PresetList: Decodable { let presets: [Preset] }

// ---- benchmarks & compare ---------------------------------------------------------------------------
struct BenchmarkResult: Decodable, Equatable, Identifiable {
    let id: String
    let repo: String
    let draft: String?
    let chip: String
    let promptTokens: Int
    let generatedTokens: Int
    let promptTps: Double
    let generationTps: Double
    let timeToFirstTokenMs: Int
    let peakMemoryGb: Double
    let loadSeconds: Double
    let createdAt: Double
}

struct BenchmarkList: Decodable { let benchmarks: [BenchmarkResult] }

struct CompareRun: Decodable, Equatable, Identifiable {
    let repo: String
    let state: String          // pending | loading | generating | done | failed
    let text: String
    let reasoning: String
    let error: String
    let completionTokens: Int
    let generationTps: Double
    let promptTps: Double
    let peakMemoryGb: Double
    let seconds: Double

    var id: String { repo + state }
}

struct CompareJob: Decodable, Equatable, Identifiable {
    let id: String
    let prompt: String
    let system: String
    let state: String          // running | done | cancelled
    let createdAt: Double
    let runs: [CompareRun]
}

// ---- network peers ------------------------------------------------------------------------------------
struct Peer: Decodable, Equatable, Identifiable {
    let name: String
    let addresses: [String]
    let port: Int
    let properties: [String: String]
    let seenAt: Double

    var id: String { name }
    var model: String? { properties["model"].flatMap { $0.isEmpty ? nil : $0 } }
    var chip: String? { properties["chip"] }
}

struct PeerList: Decodable {
    let peers: [Peer]
    let advertising: Bool
    let error: String
}

struct Transcription: Decodable, Equatable {
    let text: String
}

struct AriaTranscription: Decodable, Equatable {
    let text: String
    let model: String
    let source: String      // downloaded | bundled
}

enum EngineJSON {
    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        return e
    }()
}
