import Foundation

/// Creates the engine virtualenv and installs its Python dependencies.
enum EngineSetup {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// Returns the first interpreter that is Python 3.11 or newer. When a
    /// lockfile is in use it was generated on Python 3.11, so prefer that.
    static func findPython(prefer311: Bool = false) async -> URL? {
        var candidates = EngineLocator.systemPythonCandidates()
        if prefer311 {
            candidates.sort { $0.lastPathComponent.contains("3.11") && !$1.lastPathComponent.contains("3.11") }
        }
        for candidate in candidates {
            let ok = await run(candidate, ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"],
                               onLine: { _ in })
            if ok == 0 { return candidate }
        }
        return nil
    }

    static func install(engineDirectory: URL, onLine: @escaping @Sendable (String) -> Void) async throws {
        let hasLock = FileManager.default.fileExists(atPath: engineDirectory.appendingPathComponent("requirements-lock.txt").path)
        guard let python = await findPython(prefer311: hasLock) else {
            throw Failure(message: "Python 3.11 or newer was not found. Install it with `brew install python@3.12`, then try again.")
        }
        onLine("Using \(python.path)")
        try FileManager.default.createDirectory(at: EngineLocator.supportDirectory, withIntermediateDirectories: true)
        let venv = EngineLocator.venvDirectory
        if !FileManager.default.fileExists(atPath: venv.appendingPathComponent("bin/python3").path) {
            onLine("Creating virtual environment at \(venv.path)")
            guard await run(python, ["-m", "venv", venv.path], onLine: onLine) == 0 else {
                throw Failure(message: "Could not create the virtual environment.")
            }
        }
        let venvPython = venv.appendingPathComponent("bin/python3")
        // No unpinned pip self-upgrade: the venv's bundled pip installs the locked wheels.
        let lock = engineDirectory.appendingPathComponent("requirements-lock.txt")
        let requirements = FileManager.default.fileExists(atPath: lock.path) ? lock
            : engineDirectory.appendingPathComponent("requirements.txt")
        onLine("Installing MLX and server dependencies (this can take a few minutes)")
        guard await run(venvPython, ["-m", "pip", "install", "-r", requirements.path], onLine: onLine) == 0 else {
            throw Failure(message: "Installing dependencies failed. See Dev Tools for the pip output.")
        }
        let speechLock = engineDirectory.appendingPathComponent("requirements-speech-lock.txt")
        if FileManager.default.fileExists(atPath: speechLock.path) {
            onLine("Installing speech support for Aria's voice input")
            guard await run(venvPython, ["-m", "pip", "install", "-r", speechLock.path], onLine: onLine) == 0 else {
                throw Failure(message: "Installing speech support failed. See Dev Tools for the pip output.")
            }
        }
        // Full extras (vision + embeddings + speech) ship with setup so no
        // feature fails later for a missing runtime. Failure here does not
        // block the engine: per-feature Install actions remain available.
        let extrasLock = engineDirectory.appendingPathComponent("requirements-extras-lock.txt")
        if FileManager.default.fileExists(atPath: extrasLock.path) {
            onLine("Installing vision, speech, and embedding support (this can take a few minutes)")
            if await run(venvPython, ["-m", "pip", "install", "-r", extrasLock.path], onLine: onLine) != 0 {
                onLine("Some optional support did not install; Bridle will offer it again when a feature needs it.")
            }
        }
        onLine("Checking MLX")
        guard await run(venvPython, ["-c", "import mlx.core as mx; print('MLX', mx.__version__, 'Metal:', mx.metal.is_available())"],
                        onLine: onLine) == 0 else {
            throw Failure(message: "MLX was installed but failed to import.")
        }
        onLine("Engine ready.")
    }

    /// Runs a process to completion, streaming merged stdout/stderr lines. Returns the exit status.
    static func run(_ executable: URL, _ arguments: [String],
                    onLine: @escaping @Sendable (String) -> Void) async -> Int32 {
        await withCheckedContinuation { continuation in
            let proc = Process()
            proc.executableURL = executable
            proc.arguments = arguments
            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = pipe
            let splitter = LineSplitter()
            pipe.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                guard !data.isEmpty else { handle.readabilityHandler = nil; return }
                splitter.append(data).forEach(onLine)
            }
            proc.terminationHandler = { p in
                pipe.fileHandleForReading.readabilityHandler = nil
                if let rest = splitter.flush(), !rest.isEmpty { onLine(rest) }
                continuation.resume(returning: p.terminationStatus)
            }
            do {
                try proc.run()
            } catch {
                onLine("Failed to launch \(executable.path): \(error.localizedDescription)")
                continuation.resume(returning: -1)
            }
        }
    }
}
