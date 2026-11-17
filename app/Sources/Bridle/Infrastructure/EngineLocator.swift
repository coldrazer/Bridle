import Foundation

/// Finds the Python engine sources and the virtualenv interpreter that runs them.
enum EngineLocator {
    static let bundleID = "com.bridle.app"

    static var supportDirectory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent(bundleID, isDirectory: true)
    }

    static var venvDirectory: URL { supportDirectory.appendingPathComponent("engine-venv", isDirectory: true) }

    static var modelsDirectory: URL { supportDirectory.appendingPathComponent("models", isDirectory: true) }

    /// Interpreter inside the app's virtualenv (or BRIDLE_PYTHON for development).
    static var venvPython: URL {
        if let override = ProcessInfo.processInfo.environment["BRIDLE_PYTHON"], !override.isEmpty {
            return URL(fileURLWithPath: override)
        }
        return venvDirectory.appendingPathComponent("bin/python3")
    }

    static var isEngineInstalled: Bool {
        FileManager.default.isExecutableFile(atPath: venvPython.path)
    }

    /// The directory containing the `bridle_engine` package:
    /// 1. BRIDLE_ENGINE_DIR, 2. the app bundle's Resources/engine, 3. the repo checkout (dev builds).
    static func engineDirectory() -> URL? {
        var candidates: [URL] = []
        if let override = ProcessInfo.processInfo.environment["BRIDLE_ENGINE_DIR"], !override.isEmpty {
            candidates.append(URL(fileURLWithPath: override))
        }
        if let resources = Bundle.main.resourceURL {
            candidates.append(resources.appendingPathComponent("engine"))
        }
        // <repo>/app/Sources/Bridle/Infrastructure/EngineLocator.swift -> <repo>/engine
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { url.deleteLastPathComponent() }
        candidates.append(url.appendingPathComponent("engine"))

        return candidates.first { dir in
            FileManager.default.fileExists(atPath: dir.appendingPathComponent("bridle_engine/__init__.py").path)
        }
    }

    /// Aria's bundled model directory: BRIDLE_ARIA_DIR, the app bundle's
    /// Resources/Aria, or the repo checkout's assets/Aria (dev builds).
    static func ariaModelDirectory() -> URL? {
        var candidates: [URL] = []
        if let override = ProcessInfo.processInfo.environment["BRIDLE_ARIA_DIR"], !override.isEmpty {
            candidates.append(URL(fileURLWithPath: override))
        }
        if let resources = Bundle.main.resourceURL {
            candidates.append(resources.appendingPathComponent("Aria"))
        }
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { url.deleteLastPathComponent() }
        candidates.append(url.appendingPathComponent("assets/Aria"))
        return candidates.first { dir in
            FileManager.default.fileExists(atPath: dir.appendingPathComponent("config.json").path)
        }
    }

    /// The bundled Onyx Whisper speech directory (bundle, env override, or repo assets).
    static func onyxModelDirectory() -> URL? {
        var candidates: [URL] = []
        if let override = ProcessInfo.processInfo.environment["BRIDLE_ONYX_DIR"], !override.isEmpty {
            candidates.append(URL(fileURLWithPath: override))
        }
        if let resources = Bundle.main.resourceURL {
            candidates.append(resources.appendingPathComponent("OnyxWhisper"))
        }
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { url.deleteLastPathComponent() }
        candidates.append(url.appendingPathComponent("assets/OnyxWhisper"))
        return candidates.first { dir in
            FileManager.default.fileExists(atPath: dir.appendingPathComponent("config.json").path)
        }
    }

    /// Python 3.11+ interpreters to build the virtualenv from, best first.
    static func systemPythonCandidates() -> [URL] {
        let names = ["python3.13", "python3.12", "python3.11", "python3"]
        let dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/Library/Frameworks/Python.framework/Versions/Current/bin", "/usr/bin"]
        var seen = Set<String>()
        var result: [URL] = []
        for dir in dirs {
            for name in names {
                let path = "\(dir)/\(name)"
                if FileManager.default.isExecutableFile(atPath: path), seen.insert(path).inserted {
                    result.append(URL(fileURLWithPath: path))
                }
            }
        }
        return result
    }
}
