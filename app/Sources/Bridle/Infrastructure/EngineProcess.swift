import Foundation

/// Runs `python -m bridle_engine serve` as a child process and reports its lifecycle.
final class EngineProcess: @unchecked Sendable {
    enum Event {
        case ready(ReadyLine)
        case output(String, isError: Bool)
        case exited(status: Int32, beforeReady: Bool)
    }

    static let readyPrefix = "BRIDLE_READY "

    private var process: Process?
    private var sawReady = false
    private let lock = NSLock()

    var isRunning: Bool {
        lock.lock()
        defer { lock.unlock() }
        return process?.isRunning ?? false
    }

    /// Writes the launch secrets with 0600 permissions from the first byte (O_CREAT|O_EXCL), so
    /// there is no window where the file exists at the default umask.
    static func writeLaunchFile(_ data: Data, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if unlink(url.path) != 0 && errno != ENOENT {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        let fd = open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(fd) }
        let written = data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
        guard written == data.count else {
            unlink(url.path)
            throw POSIXError(.EIO)
        }
    }

    func start(python: URL, engineDirectory: URL, host: String, port: Int, apiKey: String,
               controlToken: String, preload: (repo: String, draft: String?)? = nil,
               launchDirectory: URL = EngineLocator.supportDirectory,
               onEvent: @escaping @Sendable (Event) -> Void) throws {
        stop()
        let proc = Process()
        proc.executableURL = python
        var args = ["-m", "bridle_engine",
                    "--data-dir", EngineLocator.supportDirectory.path,
                    "--models-dir", EngineLocator.modelsDirectory.path,
                    "serve", "--host", host, "--port", String(port)]
        if let aria = EngineLocator.ariaModelDirectory() {
            args += ["--aria-model-dir", aria.path]
        }
        if let onyx = EngineLocator.onyxModelDirectory() {
            args += ["--onyx-model-dir", onyx.path]
        }
        if let preload {
            args += ["--preload", preload.repo]
            if let draft = preload.draft { args += ["--preload-draft", draft] }
        }
        proc.arguments = args
        // Secrets travel in a 0600 launch file the engine deletes after reading,
        // never in the child environment (visible to same-user process inspection).
        let launchFile = launchDirectory.appendingPathComponent("engine-launch.json")
        let launchData = try JSONSerialization.data(withJSONObject: ["control_token": controlToken, "api_key": apiKey])
        try Self.writeLaunchFile(launchData, to: launchFile)
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = engineDirectory.path
        env["PYTHONUNBUFFERED"] = "1"
        // The bundled engine lives inside the signed app bundle; bytecode written
        // next to it on first run would break the bundle's code-signature seal.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["BRIDLE_LAUNCH_FILE"] = launchFile.path
        env["BRIDLE_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        env.removeValue(forKey: "BRIDLE_CONTROL_TOKEN")
        env.removeValue(forKey: "BRIDLE_API_KEY")
        proc.environment = env
        proc.currentDirectoryURL = engineDirectory

        let out = Pipe(), err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        let outLines = LineSplitter(), errLines = LineSplitter()

        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            for line in outLines.append(data) {
                if line.hasPrefix(Self.readyPrefix),
                   let json = line.dropFirst(Self.readyPrefix.count).data(using: .utf8),
                   let ready = try? JSONDecoder().decode(ReadyLine.self, from: json) {
                    self?.markReady()
                    onEvent(.ready(ready))
                } else {
                    onEvent(.output(line, isError: false))
                }
            }
        }
        err.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            for line in errLines.append(data) {
                onEvent(.output(line, isError: true))
            }
        }
        proc.terminationHandler = { [weak self] p in
            let before = !(self?.consumeReady() ?? false)
            onEvent(.exited(status: p.terminationStatus, beforeReady: before))
        }

        lock.lock()
        sawReady = false
        process = proc
        lock.unlock()
        do {
            try proc.run()
        } catch {
            // The engine never started, so nothing will read (and delete) the secrets file.
            try? FileManager.default.removeItem(at: launchFile)
            lock.lock()
            if process === proc { process = nil }
            lock.unlock()
            throw error
        }
    }

    /// SIGTERM, then SIGKILL if the engine hasn't exited within two seconds.
    func stop() {
        lock.lock()
        let proc = process
        process = nil
        lock.unlock()
        guard let proc, proc.isRunning else { return }
        proc.terminate()
        let pid = proc.processIdentifier
        DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
            if proc.isRunning { kill(pid, SIGKILL) }
        }
    }

    /// Synchronous variant for app termination.
    func stopAndWait(timeout: TimeInterval = 3) {
        lock.lock()
        let proc = process
        process = nil
        lock.unlock()
        guard let proc, proc.isRunning else { return }
        proc.terminate()
        let deadline = Date().addingTimeInterval(timeout)
        while proc.isRunning && Date() < deadline { usleep(50_000) }
        if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
    }

    private func markReady() {
        lock.lock()
        sawReady = true
        lock.unlock()
    }

    private func consumeReady() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return sawReady
    }
}
