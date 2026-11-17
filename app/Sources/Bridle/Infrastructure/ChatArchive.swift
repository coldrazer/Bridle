import Foundation

enum ChatArchive {
    private static func url(_ project: String, directory: URL) -> URL {
        let safe = project.filter { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }
        return directory
            .appendingPathComponent(String(safe.prefix(100)) + ".json")
    }
    static func load(project: String, directory: URL = EngineLocator.supportDirectory.appendingPathComponent("conversations")) -> [ChatTurn] {
        guard let data = try? Data(contentsOf: url(project, directory: directory)) else { return [] }
        return (try? JSONDecoder().decode([ChatTurn].self, from: data)) ?? []
    }
    static func save(_ turns: [ChatTurn], project: String, directory: URL = EngineLocator.supportDirectory.appendingPathComponent("conversations")) {
        let target = url(project, directory: directory)
        do {
            try FileManager.default.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: target.deletingLastPathComponent().path)
            try JSONEncoder().encode(turns).write(to: target, options: [.atomic, .completeFileProtectionUnlessOpen])
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
        } catch { NSLog("Could not save conversation: %@", error.localizedDescription) }
    }
}
