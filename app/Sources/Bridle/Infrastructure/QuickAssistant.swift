import AppKit
import AVFoundation
import Carbon
import Observation
import SwiftUI

@MainActor
final class QuickAssistantController: NSObject {
    static let shared = QuickAssistantController()
    weak var model: AppModel?
    private var panel: NSPanel?
    private var hotKey: EventHotKeyRef?
    private var eventHandler: EventHandlerRef?

    func install() {
        NSApp.servicesProvider = self
        NSUpdateDynamicServices()
        guard hotKey == nil else { return }
        var event = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
            Task { @MainActor in QuickAssistantController.shared.toggle() }
            return noErr
        }, 1, &event, nil, &eventHandler)
        RegisterEventHotKey(UInt32(kVK_Space), UInt32(controlKey | optionKey),
                           EventHotKeyID(signature: 0x4252444c, id: 1), GetApplicationEventTarget(), 0, &hotKey)
    }

    @objc func askBridle(_ pasteboard: NSPasteboard, userData: String?, error: AutoreleasingUnsafeMutablePointer<NSString?>) {
        guard let text = pasteboard.string(forType: .string), !text.isEmpty else { return }
        guard let model, model.canChat else { error.pointee = "Open Bridle and choose an assistant first."; return }
        if panel?.isVisible != true { toggle() }
        model.send("Explain or summarize the following selected text:\n" + String(text.prefix(32000)))
    }

    func toggle() {
        if panel?.isVisible == true { panel?.orderOut(nil); return }
        guard let model else { return }
        if panel == nil {
            let window = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 650, height: 580),
                                 styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            window.title = "Your Bridle assistant · Control–Option–Space"
            window.level = .floating
            window.isReleasedWhenClosed = false
            window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
            window.contentView = NSHostingView(rootView: QuickAssistantView().environment(model))
            window.center()
            panel = window
        }
        NSApp.activate(ignoringOtherApps: true)
        panel?.makeKeyAndOrderFront(nil)
    }
}

@MainActor @Observable
final class VoiceSession {
    var recording = false
    var working = false
    var error: String?
    var speakReplies = false
    private var recorder: AVAudioRecorder?
    private let speaker = AVSpeechSynthesizer()
    private var file: URL?

    func toggle(model: AppModel) async {
        if recording {
            recorder?.stop()
            recording = false
            guard let file else { return }
            defer { try? FileManager.default.removeItem(at: file); self.file = nil; recorder = nil }
            guard let speech = model.downloadedSpeechModels.first else { error = "Get a speech assistant in Models first."; return }
            working = true
            defer { working = false }
            if let text = await model.transcribe(fileURL: file, model: speech.repo) {
                model.send(text)
            }
            return
        }
        guard model.canChat else { error = "Load a chat assistant before starting a voice conversation."; return }
        guard !model.downloadedSpeechModels.isEmpty else { error = "Download a speech assistant from Models first."; return }
        guard await AVCaptureDevice.requestAccess(for: .audio) else { error = "Allow microphone access in System Settings → Privacy & Security."; return }
        do {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".wav")
            file = url
            recorder = try AVAudioRecorder(url: url, settings: [AVFormatIDKey: kAudioFormatLinearPCM,
                AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false])
            speaker.stopSpeaking(at: .immediate)
            guard let started = recorder, started.record(forDuration: 120) else { throw EngineError(status: 0, message: "Could not start microphone.") }
            recording = true
            // Capture ends at 120 s; send the recording then rather than showing a stale "recording" state.
            Task { @MainActor [weak self] in
                try? await Task.sleep(for: .seconds(120))
                guard let self, self.recording, self.recorder === started else { return }
                await self.toggle(model: model)
            }
        } catch { stop(); self.error = error.localizedDescription }
    }
    func speak(_ text: String) {
        guard speakReplies, !text.isEmpty else { return }
        speaker.speak(AVSpeechUtterance(string: String(text.prefix(20000))))
    }
    func stop() {
        recorder?.stop(); recorder = nil; recording = false
        speaker.stopSpeaking(at: .immediate)
        if let file { try? FileManager.default.removeItem(at: file) }
        file = nil
    }
}

struct QuickAssistantView: View {
    @Environment(AppModel.self) private var model
    @State private var voice = VoiceSession()
    @State private var captureError: String?
    @State private var showAria = false
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label(model.canChat ? "Ready · processed on this Mac" : "Choose an assistant to begin", systemImage: "lock.shield")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Ask Aria", systemImage: "sparkles") { showAria = true }
                    .help("Ask Aria, Bridle's built-in guide, for help")
                Button("Screenshot", systemImage: "viewfinder") { capture() }
                Button(voice.recording ? "Send recording" : "Talk", systemImage: voice.recording ? "stop.circle.fill" : "mic") {
                    Task { await voice.toggle(model: model) }
                }.disabled(voice.working || model.isGenerating)
                Toggle("Read replies", isOn: $voice.speakReplies).toggleStyle(.checkbox)
                Button("Stop") { voice.stop(); model.stopGenerating() }
            }.padding(12)
            Divider()
            ChatPanel()
        }
        .onChange(of: model.isGenerating) { _, generating in
            if !generating { voice.speak(model.chat.last(where: { $0.role == .assistant })?.text ?? "") }
        }
        .onDisappear { voice.stop() }
        .sheet(isPresented: $showAria) { AriaView() }
        .alert("Assistant needs attention", isPresented: Binding(get: { voice.error != nil || captureError != nil }, set: { if !$0 { voice.error = nil; captureError = nil } })) {
            Button("OK") { voice.error = nil; captureError = nil }
        } message: { Text(voice.error ?? captureError ?? "") }
    }
    private func capture() {
        guard CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess() else {
            captureError = "Allow Screen Recording for Bridle in System Settings → Privacy & Security, then reopen Bridle and try again."
            return
        }
        let target = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".png")
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        task.arguments = ["-i", "-x", "-t", "png", target.path]
        task.terminationHandler = { _ in
            Task { @MainActor in
                defer { try? FileManager.default.removeItem(at: target) }
                if FileManager.default.fileExists(atPath: target.path) {
                    model.attachImage(from: target)
                    if model.loadedModel?.kind != "vision" { captureError = "Screenshot attached. Choose a Photo Assistant before asking about it." }
                }
            }
        }
        do { try task.run() } catch { captureError = error.localizedDescription }
    }
}
