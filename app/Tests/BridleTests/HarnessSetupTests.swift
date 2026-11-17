import XCTest
@testable import Bridle

/// Harness wiring generation: per-harness launch files must carry the exact
/// endpoint/model/auth the harness needs, and stay executable shell.
final class HarnessSetupTests: XCTestCase {
    private let endpoint = "http://127.0.0.1:49813"
    private let model = "mlx-community/Qwen3-8B-4bit"

    func testClaudeScriptWiresAnthropicEnv() {
        let script = HarnessSetup.claudeScript(endpoint: endpoint, token: "tok-1", model: model,
                                               binary: "/opt/homebrew/bin/claude")
        XCTAssertTrue(script.contains("ANTHROPIC_BASE_URL=\"\(endpoint)\""))
        XCTAssertTrue(script.contains("ANTHROPIC_AUTH_TOKEN=\"tok-1\""))
        XCTAssertTrue(script.contains("ANTHROPIC_MODEL=\"\(model)\""))
        XCTAssertTrue(script.contains("ANTHROPIC_SMALL_FAST_MODEL=\"\(model)\""))
        XCTAssertTrue(script.contains("exec \"/opt/homebrew/bin/claude\""))
        XCTAssertTrue(script.hasPrefix("#!/bin/zsh"))
    }

    func testDeepseekScriptRegistersProvider() {
        let script = HarnessSetup.deepseekScript(endpoint: endpoint, token: "tok-1", model: model,
                                                 dshctl: "/Users/x/.local/bin/dshctl")
        XCTAssertTrue(script.contains("add-model --id bridle"))
        XCTAssertTrue(script.contains("--base-url \"\(endpoint)/v1\""))
        XCTAssertTrue(script.contains("--model \"\(model)\""))
        XCTAssertTrue(script.contains("DSH_MODEL_API_KEY=\"tok-1\""))
        XCTAssertTrue(script.contains("--force"))
    }

    func testLemonadeScriptRegistersProvider() {
        let script = HarnessSetup.lemonadeScript(endpoint: endpoint, token: "tok-1",
                                                 binary: "/usr/local/bin/lemonade")
        XCTAssertTrue(script.contains("cloud install bridle --base-url \"\(endpoint)/v1\" --allow-insecure-http"))
        XCTAssertTrue(script.contains("cloud uninstall bridle"))
        XCTAssertTrue(script.contains("cloud auth bridle --api-key \"tok-1\""))
    }

    func testClaudeScriptCarriesContextWindow() {
        let script = HarnessSetup.claudeScript(endpoint: endpoint, token: "t", model: model,
                                               binary: "/bin/claude", contextTokens: 32768)
        XCTAssertTrue(script.contains("CLAUDE_CODE_MAX_CONTEXT_TOKENS=\"32768\""))
        let deep = HarnessSetup.deepseekScript(endpoint: endpoint, token: "t", model: model,
                                               dshctl: "/bin/dshctl", contextTokens: 131072)
        XCTAssertTrue(deep.contains("--context 131072"))
        XCTAssertTrue(deep.contains("curl -s -m 2"))  // direct port probe, not dshctl's exit code
    }

    func testScriptValuesEscapeShellQuotes() {
        let script = HarnessSetup.claudeScript(endpoint: endpoint, token: "a\"b$c", model: model,
                                               binary: "/bin/claude")
        XCTAssertTrue(script.contains("ANTHROPIC_AUTH_TOKEN=\"a\\\"b\\$c\""))
        XCTAssertEqual(HarnessSetup.dq("a\\b\"c$d`e"), "a\\\\b\\\"c\\$d\\`e")
    }

    func testWriteWiringCreatesExecutableLaunchFile() throws {
        let base = FileManager.default.temporaryDirectory
            .appendingPathComponent("bridle-harness-tests-\(UUID().uuidString)")
        let wiring = try HarnessSetup.writeWiring(harness: .claudeCode, port: 49813, model: model,
                                                  token: "bridle-local", baseDirectory: base)
        XCTAssertEqual(wiring.endpoint, endpoint)
        XCTAssertTrue(FileManager.default.isExecutableFile(atPath: wiring.launchFile.path))
        let text = try String(contentsOf: wiring.launchFile, encoding: .utf8)
        XCTAssertTrue(text.contains("ANTHROPIC_MODEL=\"\(model)\""))
        XCTAssertTrue(FileManager.default.fileExists(
            atPath: wiring.directory.appendingPathComponent("README.md").path))
    }

    func testCodexScriptUsesResponsesProviderOverride() {
        let script = HarnessSetup.codexScript(endpoint: endpoint, token: "tok-1", model: model,
                                              binary: "/opt/homebrew/bin/codex")
        XCTAssertTrue(script.contains("BRIDLE_API_KEY=\"tok-1\""))
        XCTAssertTrue(script.contains("model_provider=\"bridle\""))
        XCTAssertTrue(script.contains("base_url = \"\(endpoint)/v1\""))
        XCTAssertTrue(script.contains("wire_api = \"responses\""))
        XCTAssertTrue(script.hasPrefix("#!/bin/zsh"))
    }

    func testOpencodeConfigAndScript() throws {
        let config = HarnessSetup.opencodeConfig(endpoint: endpoint, token: "tok-1", model: model)
        let parsed = try JSONSerialization.jsonObject(with: Data(config.utf8)) as? [String: Any]
        XCTAssertEqual(parsed?["model"] as? String, "bridle/\(model)")
        let provider = (parsed?["provider"] as? [String: Any])?["bridle"] as? [String: Any]
        XCTAssertEqual(provider?["npm"] as? String, "@ai-sdk/openai-compatible")
        let script = HarnessSetup.opencodeScript(configPath: "/tmp/opencode.json", binary: "/usr/local/bin/opencode")
        XCTAssertTrue(script.contains("OPENCODE_CONFIG=\"/tmp/opencode.json\""))
    }

    func testPersistedMigration() {
        XCTAssertEqual(Harness.fromPersisted("Deepseek harness"), .deepseek)
        XCTAssertEqual(Harness.fromPersisted("Claude Code"), .claudeCode)
        XCTAssertEqual(Harness.fromPersisted("Lemonade"), .lemonade)
        XCTAssertEqual(Harness.fromPersisted("Kiro"), .deepseek)        // removed entries fall back
        XCTAssertEqual(Harness.fromPersisted("Antigravity"), .deepseek) // removed entries fall back
        XCTAssertEqual(Harness.fromPersisted("claudeCode"), .claudeCode)
        XCTAssertEqual(Harness.fromPersisted(nil), .deepseek)
    }
}
