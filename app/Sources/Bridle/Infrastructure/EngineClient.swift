import Foundation

struct EngineError: LocalizedError, Equatable {
    let status: Int
    let message: String
    var errorDescription: String? { message }
}

/// Options for one Sandbox Chat request.
struct ChatOptions: Equatable {
    var model: String? = nil          // nil = loaded model; "preset:<name>" selects a preset
    var maxTokens: Int? = 1024
    var temperature: Double? = 0.7
    var jsonOutput = false
}

/// HTTP client for the local engine. Control calls always go to loopback.
struct EngineClient: Sendable {
    let port: Int
    let controlToken: String
    let apiKey: String

    private var base: URL { URL(string: "http://127.0.0.1:\(port)")! }

    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 60 * 60
        config.connectionProxyDictionary = [:]   // never route loopback through a proxy
        return URLSession(configuration: config)
    }()

    // ---- status, models, downloads --------------------------------------------------------------
    func status() async throws -> EngineStatus { try await control("GET", "/control/status") }

    func catalog() async throws -> [CatalogModel] {
        let response: CatalogResponse = try await control("GET", "/control/catalog")
        return response.models
    }

    func checkFit(repo: String, draft: String?) async throws -> FitCheck {
        var path = "/control/models/check?repo=\(Self.escape(repo))"
        if let draft { path += "&draft=\(Self.escape(draft))" }
        return try await control("GET", path)
    }

    func load(repo: String, draft: String? = nil, force: Bool = false) async throws {
        var body: [String: Any] = ["repo": repo, "force": force]
        if let draft { body["draft"] = draft }
        try await controlNoBody("POST", "/control/models/load", body: body)
    }

    func unload() async throws { try await controlNoBody("POST", "/control/models/unload", body: nil) }

    func deleteModel(repo: String) async throws { try await controlNoBody("DELETE", "/control/models/\(Self.escapePath(repo))", body: nil) }

    func startDownload(repo: String) async throws -> DownloadJob {
        try await control("POST", "/control/downloads", body: ["repo": repo])
    }

    func cancelDownload(id: String) async throws { try await controlNoBody("DELETE", "/control/downloads/\(id)", body: nil) }

    func clearFinishedDownloads() async throws { try await controlNoBody("POST", "/control/downloads/clear", body: nil) }

    // ---- settings ----------------------------------------------------------------------------------
    func settings() async throws -> EngineSettings { try await control("GET", "/control/settings") }

    func updateSettings(_ settings: EngineSettings) async throws -> SettingsPatchResponse {
        let data = try EngineJSON.encoder.encode(settings)
        return try await control("PATCH", "/control/settings", rawBody: data)
    }

    // ---- API keys ------------------------------------------------------------------------------------
    func keys() async throws -> [ApiKeyInfo] {
        let list: ApiKeyList = try await control("GET", "/control/keys")
        return list.keys
    }

    func createKey(name: String, rpmLimit: Int?, dailyTokenLimit: Int?) async throws -> CreatedKey {
        var body: [String: Any] = ["name": name]
        if let rpmLimit { body["rpm_limit"] = rpmLimit }
        if let dailyTokenLimit { body["daily_token_limit"] = dailyTokenLimit }
        return try await control("POST", "/control/keys", body: body)
    }

    func revokeKey(id: String) async throws { try await controlNoBody("POST", "/control/keys/\(id)/revoke", body: nil) }

    func deleteKey(id: String) async throws { try await controlNoBody("DELETE", "/control/keys/\(id)", body: nil) }

    // ---- history, presets, benchmarks, compare, peers ----------------------------------------------------
    func requests(limit: Int = 200) async throws -> RequestList { try await control("GET", "/control/requests?limit=\(limit)") }

    func clearRequests() async throws { try await controlNoBody("DELETE", "/control/requests", body: nil) }

    func presets() async throws -> [Preset] {
        let list: PresetList = try await control("GET", "/control/presets")
        return list.presets
    }

    func savePreset(_ preset: Preset) async throws {
        _ = try await controlRaw("PUT", "/control/presets", rawBody: try EngineJSON.encoder.encode(preset))
    }

    func deletePreset(name: String) async throws {
        try await controlNoBody("DELETE", "/control/presets/\(Self.escapePath(name))", body: nil)
    }

    func benchmarks() async throws -> [BenchmarkResult] {
        let list: BenchmarkList = try await control("GET", "/control/benchmarks")
        return list.benchmarks
    }

    func runBenchmark(promptTokens: Int, generationTokens: Int) async throws -> BenchmarkResult {
        try await control("POST", "/control/benchmarks",
                          body: ["prompt_tokens": promptTokens, "generation_tokens": generationTokens], timeout: 1800)
    }

    func clearBenchmarks() async throws { try await controlNoBody("DELETE", "/control/benchmarks", body: nil) }

    func startCompare(models: [String], prompt: String, system: String, maxTokens: Int, temperature: Double) async throws -> CompareJob {
        try await control("POST", "/control/compare", body: [
            "models": models, "prompt": prompt, "system": system, "max_tokens": maxTokens, "temperature": temperature,
        ])
    }

    func compareJob(id: String) async throws -> CompareJob { try await control("GET", "/control/compare/\(id)") }

    func cancelCompare(id: String) async throws { try await controlNoBody("DELETE", "/control/compare/\(id)", body: nil) }

    func peers() async throws -> PeerList { try await control("GET", "/control/peers") }

    // ---- production: doctor, egress, scorecards, storage, support, backup ----------------------
    func doctor() async throws -> DoctorReport { try await control("GET", "/control/doctor", timeout: 120) }

    func egress(limit: Int = 100) async throws -> EgressList { try await control("GET", "/control/egress?limit=\(limit)") }

    func clearEgress() async throws { try await controlNoBody("DELETE", "/control/egress", body: nil) }

    func scorecards() async throws -> [ScorecardResult] {
        let list: ScorecardList = try await control("GET", "/control/scorecards")
        return list.scorecards
    }

    func runScorecard() async throws -> ScorecardResult {
        try await control("POST", "/control/scorecards/run", body: [:], timeout: 900)
    }

    func storage() async throws -> StorageReport { try await control("GET", "/control/storage", timeout: 120) }

    func pruneStorage() async throws -> PruneResult { try await control("POST", "/control/storage/prune", body: [:], timeout: 300) }

    func reverify(repo: String) async throws -> ReverifyResult {
        try await control("POST", "/control/storage/reverify", body: ["repo": repo], timeout: 1800)
    }

    func checkModelUpdates() async throws -> UpdateReport {
        try await control("POST", "/control/storage/check-updates", body: [:], timeout: 300)
    }

    func supportBundle() async throws -> FileResult {
        try await control("POST", "/control/support-bundle", body: [:], timeout: 300)
    }

    func exportBackup(passphrase: String) async throws -> BackupExportResult {
        try await control("POST", "/control/backup/export", body: ["passphrase": passphrase], timeout: 900)
    }

    func importBackup(path: String, passphrase: String) async throws -> BackupImportResult {
        try await control("POST", "/control/backup/import", body: ["path": path, "passphrase": passphrase], timeout: 900)
    }

    /// A multipart-safe filename: quotes/CRLF in a real filename must not break the header.
    static func multipartFilename(_ raw: String) -> String {
        let cleaned = raw.unicodeScalars.map { scalar -> Character in
            (scalar == "\"" || scalar == "\r" || scalar == "\n" || scalar.value < 32) ? "_" : Character(scalar)
        }
        let name = String(cleaned).trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? "audio.wav" : String(name.prefix(120))
    }

    /// The engine caps uploads at 200 MB; refuse before loading the file into memory.
    static func checkUploadSize(_ fileURL: URL) throws {
        let size = (try? FileManager.default.attributesOfItem(atPath: fileURL.path)[.size] as? NSNumber)?.intValue ?? 0
        if size > 200 * 1024 * 1024 {
            throw EngineError(status: 413, message: "Audio file is larger than 200 MB.")
        }
    }

    // ---- speech to text (multipart upload) ---------------------------------------------------------------
    func transcribe(fileURL: URL, model: String) async throws -> String {
        try Self.checkUploadSize(fileURL)
        let boundary = "bridle-\(UUID().uuidString)"
        var body = Data()
        func field(_ name: String, _ value: String) {
            body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".utf8))
        }
        field("model", model)
        field("response_format", "json")
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(Self.multipartFilename(fileURL.lastPathComponent))\"\r\nContent-Type: application/octet-stream\r\n\r\n".utf8))
        body.append(try Data(contentsOf: fileURL))
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var request = URLRequest(url: base.appendingPathComponent("v1/audio/transcriptions"))
        request.httpMethod = "POST"
        request.timeoutInterval = 1800
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        if !apiKey.isEmpty { request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization") }
        request.httpBody = body
        let (data, response) = try await Self.session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200 else { throw Self.error(status: status, data: data) }
        return try EngineJSON.decoder.decode(Transcription.self, from: data).text
    }

    /// Aria's ears: the engine picks a downloaded speech assistant first and
    /// falls back to the bundled Onyx model. Returns (text, source).
    func transcribeForAria(fileURL: URL) async throws -> (text: String, source: String) {
        try Self.checkUploadSize(fileURL)
        let boundary = "bridle-\(UUID().uuidString)"
        var body = Data()
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(Self.multipartFilename(fileURL.lastPathComponent))\"\r\nContent-Type: application/octet-stream\r\n\r\n".utf8))
        body.append(try Data(contentsOf: fileURL))
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var request = URLRequest(url: base.appendingPathComponent("control/aria/transcribe"))
        request.httpMethod = "POST"
        request.timeoutInterval = 300
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(controlToken)", forHTTPHeaderField: "Authorization")
        request.httpBody = body
        let (data, response) = try await Self.session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200 else { throw Self.error(status: status, data: data) }
        let decoded = try EngineJSON.decoder.decode(AriaTranscription.self, from: data)
        return (decoded.text, decoded.source)
    }

    // ---- OpenAI-compatible chat (streaming) -----------------------------------------------------------
    func streamChat(messages: [ChatTurn], options: ChatOptions) -> AsyncThrowingStream<ChatStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = URLRequest(url: base.appendingPathComponent("v1/chat/completions"))
                    request.httpMethod = "POST"
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    if !apiKey.isEmpty { request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization") }
                    request.timeoutInterval = 900
                    var body: [String: Any] = [
                        "messages": messages.map(Self.wireMessage),
                        "stream": true,
                        "stream_options": ["include_usage": true],
                    ]
                    if let model = options.model { body["model"] = model }
                    if let maxTokens = options.maxTokens { body["max_tokens"] = maxTokens }
                    if let temperature = options.temperature { body["temperature"] = temperature }
                    if options.jsonOutput { body["response_format"] = ["type": "json_object"] }
                    request.httpBody = try JSONSerialization.data(withJSONObject: body)
                    let (bytes, response) = try await Self.session.bytes(for: request)
                    let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                    if status != 200 {
                        var data = Data()
                        for try await byte in bytes { data.append(byte) }
                        throw Self.error(status: status, data: data)
                    }
                    for try await line in bytes.lines {
                        for event in SSEParser.parse(line: line) {
                            continuation.yield(event)
                            if event == .done {
                                continuation.finish()
                                return
                            }
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            // Cancelling the consumer closes the connection; the engine then stops generating.
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    static func wireMessage(_ turn: ChatTurn) -> [String: Any] {
        guard !turn.images.isEmpty else { return ["role": turn.role.rawValue, "content": turn.text] }
        var parts: [[String: Any]] = [["type": "text", "text": turn.text]]
        for url in turn.images { parts.append(["type": "image_url", "image_url": ["url": url]]) }
        return ["role": turn.role.rawValue, "content": parts]
    }

    // ---- plumbing ---------------------------------------------------------------------------------------
    static func escape(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed.subtracting(CharacterSet(charactersIn: "&=+"))) ?? s
    }

    /// Percent-encode each path segment, preserving the `/` separators in owner/name ids.
    static func escapePath(_ s: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))
        return s.split(separator: "/", omittingEmptySubsequences: false)
            .map { String($0).addingPercentEncoding(withAllowedCharacters: allowed) ?? String($0) }
            .joined(separator: "/")
    }

    private func request(_ method: String, _ path: String, body: Data?, timeout: TimeInterval?) throws -> URLRequest {
        guard let url = URL(string: path, relativeTo: base) else {
            throw EngineError(status: 0, message: "Invalid engine request path: \(path)")
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("Bearer \(controlToken)", forHTTPHeaderField: "Authorization")
        if let timeout { req.timeoutInterval = timeout }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = body
        }
        return req
    }

    private func controlRaw(_ method: String, _ path: String, rawBody: Data?, timeout: TimeInterval? = nil) async throws -> Data {
        let (data, response) = try await Self.session.data(for: try request(method, path, body: rawBody, timeout: timeout))
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else { throw Self.error(status: status, data: data) }
        return data
    }

    private func control<T: Decodable>(_ method: String, _ path: String, body: [String: Any]? = nil,
                                        timeout: TimeInterval? = nil) async throws -> T {
        let raw = try body.map { try JSONSerialization.data(withJSONObject: $0) }
        return try EngineJSON.decoder.decode(T.self, from: try await controlRaw(method, path, rawBody: raw, timeout: timeout))
    }

    private func control<T: Decodable>(_ method: String, _ path: String, rawBody: Data) async throws -> T {
        try EngineJSON.decoder.decode(T.self, from: try await controlRaw(method, path, rawBody: rawBody))
    }

    private func controlNoBody(_ method: String, _ path: String, body: [String: Any]?) async throws {
        let raw = try body.map { try JSONSerialization.data(withJSONObject: $0) }
        _ = try await controlRaw(method, path, rawBody: raw)
    }

    static func error(status: Int, data: Data) -> EngineError {
        if let body = try? JSONDecoder().decode(APIErrorBody.self, from: data) {
            return EngineError(status: status, message: body.error.message)
        }
        let text = String(decoding: data.prefix(300), as: UTF8.self)
        return EngineError(status: status, message: text.isEmpty ? "HTTP \(status)" : text)
    }
    func featureRequest(_ method: String, _ path: String, body: [String: Any]? = nil) async throws -> Data {
        guard path.hasPrefix("/control/") else { throw EngineError(status: 0, message: "Invalid control route") }
        let raw = try body.map { try JSONSerialization.data(withJSONObject: $0) }
        return try await controlRaw(method, path, rawBody: raw, timeout: 1800)
    }

}
