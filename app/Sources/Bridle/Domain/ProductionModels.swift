import Foundation

// Mirrors the engine's production add-on endpoints (production.py).
// Decoded with .convertFromSnakeCase like the rest of EngineModels.

struct DoctorCheck: Decodable, Equatable, Identifiable {
    let name: String
    let ok: Bool
    let detail: String
    let fix: String
    var id: String { name }
}

struct DoctorReport: Decodable, Equatable {
    let ok: Bool
    let version: String
    let checks: [DoctorCheck]
}

struct ScorecardTask: Decodable, Equatable, Identifiable {
    let name: String
    let ok: Bool
    let detail: String
    var id: String { name }
}

struct ScorecardResult: Decodable, Equatable, Identifiable {
    let repo: String
    let at: Double
    let passed: Int
    let total: Int
    let harnessReady: Bool
    let tasks: [ScorecardTask]
    var id: String { repo }
}

struct ScorecardList: Decodable {
    let scorecards: [ScorecardResult]
}

struct StorageModel: Decodable, Equatable, Identifiable {
    let repo: String
    let sizeBytes: Int
    let revision: String
    let completedAt: String
    let lastUsed: Double?
    let loaded: Bool
    var id: String { repo }
}

struct StorageReport: Decodable, Equatable {
    let models: [StorageModel]
    let totalBytes: Int
    let partialBytes: Int
    let freeBytes: Int
    let quotaBytes: Int
    let overQuota: Bool
    let pruneCandidates: [String]
}

struct PruneResult: Decodable {
    let pruned: [String]
    let note: String?
}

struct UpdateEntry: Decodable, Equatable, Identifiable {
    let repo: String
    let current: String
    let latest: String?
    let updateAvailable: Bool
    let error: String?
    var id: String { repo }
}

struct UpdateReport: Decodable {
    let models: [UpdateEntry]
}

struct EgressEvent: Decodable, Equatable {
    let ts: Double
    let host: String
    let purpose: String
}

struct EgressList: Decodable {
    let events: [EgressEvent]
    let total: Int
}

struct FileResult: Decodable {
    let path: String
    let bytes: Int
}

struct BackupExportResult: Decodable {
    let path: String
    let bytes: Int
    let encrypted: Bool
    let files: [String]
}

struct BackupImportResult: Decodable {
    let restored: [String]
    let restartRequired: Bool
    let note: String
}

struct ReverifyResult: Decodable {
    let repo: String
    let ok: Bool
    let checked: Int
    let bad: [String]
    let note: String?
}
