import Foundation
import XCTest
@testable import Bridle

/// Lifecycle tests for EngineProcess using a fake "python" shell script, so they need no engine.
final class EngineProcessTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("bridle-proc-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func fakeEngine(_ body: String) throws -> URL {
        let url = dir.appendingPathComponent("fake-python.sh")
        try ("#!/bin/sh\n" + body + "\n").write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
        return url
    }

    private final class Recorder: @unchecked Sendable {
        private let lock = NSLock()
        private var events: [EngineProcess.Event] = []
        func add(_ e: EngineProcess.Event) { lock.lock(); events.append(e); lock.unlock() }
        var all: [EngineProcess.Event] { lock.lock(); defer { lock.unlock() }; return events }
    }

    private func start(_ proc: EngineProcess, script: URL, into recorder: Recorder,
                       exited: XCTestExpectation) throws {
        try proc.start(python: script, engineDirectory: dir, host: "127.0.0.1", port: 0,
                       apiKey: "k", controlToken: "t", launchDirectory: dir) { event in
            recorder.add(event)
            if case .exited = event { exited.fulfill() }
        }
    }

    func testLaunchFileIsPrivateFromCreationAndReplacesOldOne() throws {
        let url = dir.appendingPathComponent("engine-launch.json")
        try Data("old".utf8).write(to: url)
        try EngineProcess.writeLaunchFile(Data("{\"control_token\":\"t\"}".utf8), to: url)
        let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
        XCTAssertEqual((attrs[.posixPermissions] as? NSNumber)?.intValue, 0o600)
        XCTAssertEqual(try String(contentsOf: url, encoding: .utf8), "{\"control_token\":\"t\"}")
    }

    func testReadyLineThenCleanExitIsNotAStartupFailure() throws {
        let script = try fakeEngine("""
        echo 'BRIDLE_READY {"port": 4242, "endpoint": "http://127.0.0.1:4242", "version": "0.2.0"}'
        sleep 0.2
        exit 0
        """)
        let recorder = Recorder(), exited = expectation(description: "exited")
        let proc = EngineProcess()
        try start(proc, script: script, into: recorder, exited: exited)
        wait(for: [exited], timeout: 10)
        let events = recorder.all
        XCTAssertTrue(events.contains { if case .ready(let r) = $0 { return r.port == 4242 && r.version == "0.2.0" }; return false })
        guard case .exited(let status, let beforeReady)? = events.first(where: { if case .exited = $0 { return true }; return false }) else { return XCTFail("no exit event") }
        XCTAssertEqual(status, 0)
        XCTAssertFalse(beforeReady)
    }

    func testExitBeforeReadyIsReportedAsStartupFailure() throws {
        let script = try fakeEngine("echo 'port in use' >&2\nexit 2")
        let recorder = Recorder(), exited = expectation(description: "exited")
        try start(EngineProcess(), script: script, into: recorder, exited: exited)
        wait(for: [exited], timeout: 10)
        guard case .exited(let status, let beforeReady)? = recorder.all.first(where: { if case .exited = $0 { return true }; return false }) else { return XCTFail("no exit event") }
        XCTAssertEqual(status, 2)
        XCTAssertTrue(beforeReady, "an exit with no READY line must count as a failed start")
    }

    func testStopAndWaitTerminatesARunningEngine() throws {
        let script = try fakeEngine("echo 'BRIDLE_READY {\"port\": 1, \"endpoint\": \"x\", \"version\": \"v\"}'\nexec sleep 30")
        let recorder = Recorder(), exited = expectation(description: "exited")
        let proc = EngineProcess()
        try start(proc, script: script, into: recorder, exited: exited)
        let deadline = Date().addingTimeInterval(5)
        while !proc.isRunning && Date() < deadline { usleep(20_000) }
        XCTAssertTrue(proc.isRunning)
        let began = Date()
        proc.stopAndWait(timeout: 3)
        XCTAssertLessThan(Date().timeIntervalSince(began), 4)
        wait(for: [exited], timeout: 5)
        XCTAssertFalse(proc.isRunning)
    }

    func testFailedLaunchRemovesSecretsFile() throws {
        let missing = dir.appendingPathComponent("no-such-python")
        XCTAssertThrowsError(try EngineProcess().start(python: missing, engineDirectory: dir, host: "127.0.0.1",
                                                       port: 0, apiKey: "k", controlToken: "t",
                                                       launchDirectory: dir) { _ in })
        XCTAssertFalse(FileManager.default.fileExists(atPath: dir.appendingPathComponent("engine-launch.json").path))
    }
}
