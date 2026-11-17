import XCTest
@testable import Bridle

/// Audit F11/F14: multipart header construction must survive hostile filenames.
final class MultipartFilenameTests: XCTestCase {
    func testStripsQuotesAndCRLF() {
        XCTAssertEqual(EngineClient.multipartFilename("a\"b.wav"), "a_b.wav")
        XCTAssertEqual(EngineClient.multipartFilename("a\r\nb.wav"), "a__b.wav")
        XCTAssertFalse(EngineClient.multipartFilename("x\"\r\nInjected: 1").contains("\""))
    }

    func testEmptyFallsBack() {
        XCTAssertEqual(EngineClient.multipartFilename(""), "audio.wav")
        XCTAssertEqual(EngineClient.multipartFilename("   "), "audio.wav")
    }

    func testNormalNamePassesThrough() {
        XCTAssertEqual(EngineClient.multipartFilename("meeting notes.m4a"), "meeting notes.m4a")
    }
}
