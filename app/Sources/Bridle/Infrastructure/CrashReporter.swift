import Foundation

/// Writes a report when the engine exits unexpectedly: exit status, recent log lines and the
/// Python faulthandler trace (native crashes such as a Metal fault land there).
enum CrashReporter {
    static var directory: URL { EngineLocator.supportDirectory.appendingPathComponent("CrashReports", isDirectory: true) }

    static func write(exitStatus: Int32, recentLog: [String], model: String?) -> URL? {
        let fm = FileManager.default
        try? fm.createDirectory(at: directory, withIntermediateDirectories: true)
        let stamp = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")
        let url = directory.appendingPathComponent("engine-\(stamp).log")
        var text = """
        Bridle engine crash report
        Date: \(Date())
        Exit status: \(exitStatus)
        Loaded model: \(model ?? "none")
        macOS: \(ProcessInfo.processInfo.operatingSystemVersionString)

        ---- last \(recentLog.count) log lines ----
        \(recentLog.joined(separator: "\n"))

        """
        let faults = EngineLocator.supportDirectory.appendingPathComponent("crash/faults.log")
        if let data = try? Data(contentsOf: faults) {
            text += "---- faulthandler (tail) ----\n" + String(decoding: data.suffix(16_000), as: UTF8.self)
        }
        do {
            try text.write(to: url, atomically: true, encoding: .utf8)
            prune(keep: 20)
            return url
        } catch {
            return nil
        }
    }

    private static func prune(keep: Int) {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.creationDateKey]) else { return }
        let sorted = files.sorted { $0.lastPathComponent > $1.lastPathComponent }
        for old in sorted.dropFirst(keep) { try? fm.removeItem(at: old) }
    }
}
