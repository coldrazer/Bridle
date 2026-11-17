import Foundation
import XCTest
@testable import Bridle

final class ChatArchiveTests: XCTestCase {
    func testConversationsSurviveReloadAndStaySeparate() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = [ChatTurn(role: .user, text: "A private question"), ChatTurn(role: .assistant, text: "An answer")]
        ChatArchive.save(first, project: "one", directory: directory)
        ChatArchive.save([ChatTurn(role: .user, text: "Other project")], project: "two", directory: directory)
        XCTAssertEqual(ChatArchive.load(project: "one", directory: directory), first)
        XCTAssertEqual(ChatArchive.load(project: "two", directory: directory).first?.text, "Other project")
        XCTAssertTrue(ChatArchive.load(project: "missing", directory: directory).isEmpty)
        let permissions = try FileManager.default.attributesOfItem(atPath: directory.appendingPathComponent("one.json").path)[.posixPermissions] as? Int
        XCTAssertEqual(permissions, 0o600)
    }

    func testHistoryWindowingKeepsNewestTurnsWithinBudget() {
        let long = String(repeating: "x", count: 4000)   // ~1000 estimated tokens each
        let turns = (0..<10).map { ChatTurn(role: $0.isMultiple(of: 2) ? .user : .assistant, text: long) }
        let kept = ChatHistory.windowed(turns, contextLength: 4096, maxTokens: 1024)
        XCTAssertEqual(kept.last?.text, long)
        XCTAssertLessThan(kept.count, turns.count)
        XCTAssertEqual(kept, Array(turns.suffix(kept.count)))
        // A single oversized newest turn is always kept; the engine reports the real limit.
        let huge = [ChatTurn(role: .user, text: String(repeating: "y", count: 100_000))]
        XCTAssertEqual(ChatHistory.windowed(huge, contextLength: 2048, maxTokens: 512).count, 1)
        // Short histories pass through untouched.
        let short = [ChatTurn(role: .user, text: "hi")]
        XCTAssertEqual(ChatHistory.windowed(short, contextLength: nil, maxTokens: 1024), short)
    }
}
