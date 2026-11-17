import XCTest
@testable import Bridle

/// Fixtures were captured from a live engine (`python -m bridle_engine serve`) running real models.
final class EngineDecodingTests: XCTestCase {
    private func fixture(_ name: String) throws -> Data {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "Fixtures/\(name)", withExtension: nil))
        return try Data(contentsOf: url)
    }

    private func decode<T: Decodable>(_ type: T.Type, _ name: String) throws -> T {
        try EngineJSON.decoder.decode(T.self, from: fixture(name))
    }

    func testDecodesLiveStatus() throws {
        let status = try decode(EngineStatus.self, "status.json")
        XCTAssertEqual(status.server.mode, "standalone")
        XCTAssertEqual(status.model.state, "ready")
        let loaded = try XCTUnwrap(status.model.loaded)
        XCTAssertEqual(loaded.repo, "mlx-community/Qwen2.5-0.5B-Instruct-4bit")
        XCTAssertEqual(loaded.kind, "text")
        XCTAssertEqual(loaded.contextLength, 32768)
        XCTAssertTrue(loaded.supportsTools)
        XCTAssertNotNil(status.stats.mlx)
        XCTAssertTrue(status.features.structuredOutput)
        XCTAssertGreaterThanOrEqual(status.usage.requests, 1)
        XCTAssertGreaterThanOrEqual(status.model.metrics.cachedPromptTokens, 0)
        let job = try XCTUnwrap(status.downloads.first)
        XCTAssertEqual(job.state, "cancelled")
        XCTAssertFalse(job.isActive)
    }

    func testDecodesCatalogWithKindsAndDrafts() throws {
        let catalog = try decode(CatalogResponse.self, "catalog.json").models
        XCTAssertGreaterThanOrEqual(catalog.count, 25)
        XCTAssertEqual(Set(catalog.map(\.kind)), ["text", "vision", "embedding", "speech"])
        let qwen8 = try XCTUnwrap(catalog.first { $0.repo == "mlx-community/Qwen3-8B-4bit" })
        XCTAssertTrue(qwen8.draftCandidates.contains("mlx-community/Qwen3-0.6B-4bit"))
        XCTAssertTrue(catalog.allSatisfy { ["fits", "tight", "too_large"].contains($0.fit) })
    }

    func testParsesLiveToolCallStream() throws {
        let text = String(decoding: try fixture("stream.txt"), as: UTF8.self)
        let events = text.split(separator: "\n").flatMap { SSEParser.parse(line: String($0)) }
        let calls = events.compactMap { if case .toolCalls(let c) = $0 { return c } else { return nil } }.flatMap { $0 }
        XCTAssertTrue(calls.first?.hasPrefix("get_weather(") ?? false, "\(events)")
        XCTAssertTrue(events.contains(.finished(reason: "tool_calls")))
        XCTAssertTrue(events.contains { if case .usage(let u) = $0 { return u.completionTokens > 0 } else { return false } })
        XCTAssertEqual(events.last, .done)
    }

    func testDecodesSettingsAndRoundTripsPatch() throws {
        let settings = try decode(EngineSettings.self, "settings.json")
        XCTAssertTrue(settings.batching)
        let encoded = try JSONSerialization.jsonObject(with: EngineJSON.encoder.encode(settings)) as? [String: Any]
        XCTAssertEqual(encoded?["idle_unload_minutes"] as? Int, settings.idleUnloadMinutes)
        XCTAssertNotNil(encoded?["ollama_api"])
    }

    func testDecodesFeatureEndpoints() throws {
        let requests = try decode(RequestList.self, "requests.json")
        XCTAssertFalse(requests.requests.isEmpty)
        XCTAssertTrue(requests.requests.contains { $0.status == 200 })
        let presets = try decode(PresetList.self, "presets.json").presets
        XCTAssertEqual(presets.first?.name, "Coder")
        let bench = try decode(BenchmarkList.self, "benchmarks.json").benchmarks
        XCTAssertGreaterThan(bench.first?.generationTps ?? 0, 0)
        let compare = try decode(CompareJob.self, "compare.json")
        XCTAssertEqual(compare.runs.count, 2)
        XCTAssertEqual(compare.state, "done")
        let keys = try decode(ApiKeyList.self, "keys.json").keys
        XCTAssertEqual(keys.first?.name, "Laptop")
        let created = try decode(CreatedKey.self, "created_key.json")
        XCTAssertTrue(created.secret.hasPrefix("brdl_"))
        let check = try decode(FitCheck.self, "check.json")
        XCTAssertEqual(check.verdict, "ok")
        let peers = try decode(PeerList.self, "peers.json")
        XCTAssertTrue(peers.advertising || !peers.error.isEmpty || peers.peers.isEmpty)
    }

    func testPresetEncodesSnakeCase() throws {
        let preset = Preset(name: "Coder", model: "", systemPrompt: "Write code.", temperature: 0.2, topP: nil, maxTokens: 512)
        let obj = try JSONSerialization.jsonObject(with: EngineJSON.encoder.encode(preset)) as? [String: Any]
        XCTAssertEqual(obj?["system_prompt"] as? String, "Write code.")
        XCTAssertEqual(obj?["max_tokens"] as? Int, 512)
        XCTAssertNil(obj?["top_p"])
    }

    func testEscapePathEncodesSegmentsButKeepsSlashes() {
        XCTAssertEqual(EngineClient.escapePath("mlx-community/Qwen3-8B-4bit"), "mlx-community/Qwen3-8B-4bit")
        XCTAssertEqual(EngineClient.escapePath("a b/c?d#e"), "a%20b/c%3Fd%23e")
        XCTAssertEqual(EngineClient.escapePath("preset name"), "preset%20name")
    }

    func testEngineErrorBody() {
        let body = Data(#"{"error":{"message":"no model is loaded","type":"x","param":null,"code":"y"}}"#.utf8)
        XCTAssertEqual(EngineClient.error(status: 503, data: body), EngineError(status: 503, message: "no model is loaded"))
        XCTAssertEqual(EngineClient.error(status: 500, data: Data()).message, "HTTP 500")
    }
}

final class SSEParserTests: XCTestCase {
    func testDeltaReasoningAndFinish() {
        let line = #"data: {"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}"#
        XCTAssertEqual(SSEParser.parse(line: line), [.delta("Hi")])
        let thinking = #"data: {"choices":[{"index":0,"delta":{"reasoning_content":"hmm"},"finish_reason":null}]}"#
        XCTAssertEqual(SSEParser.parse(line: thinking), [.reasoning("hmm")])
        let fin = #"data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}"#
        XCTAssertEqual(SSEParser.parse(line: fin), [.finished(reason: "length")])
    }

    func testUsageWithCacheDraftAndBatching() {
        let line = #"data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"prompt_tokens_details":{"cached_tokens":90}},"bridle":{"generation_tps":42.5,"peak_memory_gb":1.2,"draft_accepted_tokens":12,"batched":true}}"#
        XCTAssertEqual(SSEParser.parse(line: line), [.usage(ChatUsage(promptTokens: 100, cachedTokens: 90, completionTokens: 20,
                                                                    generationTps: 42.5, peakMemoryGB: 1.2, draftAccepted: 12,
                                                                    batched: true))])
    }

    func testErrorsAndNoise() {
        XCTAssertEqual(SSEParser.parse(line: #"data: {"error":{"message":"boom"}}"#), [.failure("boom")])
        XCTAssertEqual(SSEParser.parse(line: "data: [DONE]"), [.done])
        XCTAssertEqual(SSEParser.parse(line: ": keep-alive"), [])
        XCTAssertEqual(SSEParser.parse(line: "data: not json"), [])
    }

    func testWireMessageWithImages() {
        var turn = ChatTurn(role: .user, text: "what is this", images: ["data:image/png;base64,AAAA"])
        let wire = EngineClient.wireMessage(turn)
        let parts = wire["content"] as? [[String: Any]]
        XCTAssertEqual(parts?.count, 2)
        XCTAssertEqual(parts?[1]["type"] as? String, "image_url")
        turn.images = []
        XCTAssertEqual(EngineClient.wireMessage(turn)["content"] as? String, "what is this")
    }
}

final class LineSplitterTests: XCTestCase {
    func testSplitsAcrossChunks() {
        let s = LineSplitter()
        XCTAssertEqual(s.append(Data("BRIDLE_READY {\"po".utf8)), [])
        XCTAssertEqual(s.append(Data("rt\": 1}\r\nnext\npar".utf8)), ["BRIDLE_READY {\"port\": 1}", "next"])
        XCTAssertEqual(s.flush(), "par")
        XCTAssertNil(s.flush())
    }
}

final class FormatTests: XCTestCase {
    func testFormats() {
        XCTAssertEqual(Format.bytes(11_400_000_000), "11.4 GB")
        XCTAssertEqual(Format.bytes(512_000_000_000), "512 GB")
        XCTAssertEqual(Format.bytes(1_000_000_000_000), "1 TB")
        XCTAssertEqual(Format.memory(17_179_869_184), "16 GB")
        XCTAssertEqual(Format.memory(51_539_607_552), "48 GB")
        XCTAssertEqual(Format.duration(122_103), "33:55:03")
        XCTAssertEqual(Format.percent(0.65), "65%")
        XCTAssertEqual(Format.shortName("mlx-community/gpt-oss-20b-MXFP4-Q4"), "gpt-oss-20b-MXFP4-Q4")
    }

    func testThunderboltClusterEligibility() throws {
        let json = #"{"hostname":"h","model_name":"Mac mini","kind":"mini","chip":"Apple M4 Pro","memory_total_bytes":1,"disk_total_bytes":1,"os_version":"macOS 26.2","thunderbolt_generation":5,"thunderbolt_ports":3,"rdma":"disabled"}"#
        let m = try EngineJSON.decoder.decode(MachineInfo.self, from: Data(json.utf8))
        XCTAssertTrue(m.canCluster)
        XCTAssertEqual(m.symbolName, "macmini")
    }
}
