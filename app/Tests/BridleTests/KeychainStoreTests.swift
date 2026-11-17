import XCTest
@testable import Bridle

final class KeychainStoreTests: XCTestCase {
    func testRoundTripUpdateAndRemove() throws {
        let account = "test-\(UUID().uuidString)"
        defer { KeychainStore.remove(account) }
        guard KeychainStore.set("first", for: account) else {
            throw XCTSkip("Keychain unavailable in this test environment")
        }
        XCTAssertEqual(KeychainStore.string(for: account), "first")
        XCTAssertTrue(KeychainStore.set("second", for: account))
        XCTAssertEqual(KeychainStore.string(for: account), "second")
        XCTAssertTrue(KeychainStore.set("", for: account))   // empty removes
        XCTAssertNil(KeychainStore.string(for: account))
    }
}
