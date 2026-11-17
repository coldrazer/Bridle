import AVFoundation
import SwiftUI

/// Aria, Bridle's built-in guide. She answers from a bundled guide plus live
/// app state, and may propose one action at a time — it runs only when the
/// user taps the approval button, and approval never carries over.
struct AriaView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    @State private var turns: [ChatTurn] = []
    @State private var actions: [UUID: [String: Any]] = [:]
    @State private var approved: Set<UUID> = []
    @State private var input = ""
    @State private var thinking = false
    @State private var available: Bool?
    @State private var error = ""
    @State private var showGuide = false
    @State private var recorder: AVAudioRecorder?
    @State private var recording = false
    @State private var voiceWorking = false
    @State private var voiceFile: URL?
    @State private var speakReplies = false
    @State private var speaker = AVSpeechSynthesizer()

    private let starters = ["How do I get started?", "Which assistant should I choose?",
                            "Why won't chat start?", "How do I share Bridle with my phone?"]

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if available == false {
                unavailableState
            } else {
                messages
                if !error.isEmpty { errorBar }
                inputBar
            }
        }
        .frame(minWidth: 480, minHeight: 520)
        .task { await load() }
        .onDisappear {
            recorder?.stop()
            speaker.stopSpeaking(at: .immediate)
            if let voiceFile { try? FileManager.default.removeItem(at: voiceFile) }
        }
        .sheet(isPresented: $showGuide) { BeginnerGuideView() }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "sparkles").foregroundStyle(.tint)
            VStack(alignment: .leading, spacing: 1) {
                Text("Aria").font(.headline)
                Text("Bridle's built-in guide · runs on this Mac").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if !turns.isEmpty {
                Button("Clear Chat") { turns = []; actions = [:]; approved = []; save() }
                    .help("Clear Aria's saved conversation")
            }
            Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
        }.padding(14)
    }

    private var unavailableState: some View {
        VStack(spacing: 12) {
            Image(systemName: "sparkles").font(.largeTitle).foregroundStyle(.secondary)
            Text("Aria isn't in this build").font(.headline)
            Text("This copy of Bridle was built without Aria's bundled helper. "
                 + "The Beginner Guide covers the same setup steps.")
                .multilineTextAlignment(.center).foregroundStyle(.secondary)
            Button("Open Beginner Guide") { showGuide = true }.buttonStyle(.borderedProminent)
            Button("Try Repair") { Task { await repair() } }
        }
        .padding(32).frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var messages: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if turns.isEmpty {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Hi, I'm Aria. I can explain Bridle, help you choose an assistant, "
                             + "and suggest the next step. What would you like to do?")
                        ForEach(starters, id: \.self) { starter in
                            Button(starter) { input = starter; send() }
                                .buttonStyle(.bordered)
                        }
                    }
                    .padding(14)
                    .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
                }
                ForEach(turns) { turn in
                    VStack(alignment: turn.role == .user ? .trailing : .leading, spacing: 8) {
                        HStack {
                            if turn.role == .user { Spacer() }
                            Text(turn.text)
                                .padding(10)
                                .background(turn.role == .user ? Color.accentColor.opacity(0.18) : Color.secondary.opacity(0.12),
                                            in: RoundedRectangle(cornerRadius: 10))
                                .textSelection(.enabled)
                            if turn.role != .user { Spacer() }
                        }
                        if let action = actions[turn.id] {
                            approvalCard(turn: turn, action: action)
                        }
                    }
                }
                if thinking {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Aria is thinking…").foregroundStyle(.secondary)
                    }
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func approvalCard(turn: ChatTurn, action: [String: Any]) -> some View {
        let label = action["label"] as? String ?? "Do it"
        let done = approved.contains(turn.id)
        return HStack(spacing: 10) {
            Image(systemName: done ? "checkmark.circle.fill" : "hand.raised")
                .foregroundStyle(done ? .green : .orange)
            VStack(alignment: .leading, spacing: 2) {
                Text(done ? "Done — you approved this:" : "Aria suggests:").font(.caption).foregroundStyle(.secondary)
                Text(label).font(.callout.bold())
            }
            Spacer()
            if !done {
                Button(label) { approve(turn: turn, action: action) }
                    .buttonStyle(.borderedProminent)
                    .help("Run this one action now. Aria will ask again next time.")
            }
        }
        .padding(10)
        .background(.orange.opacity(done ? 0.06 : 0.12), in: RoundedRectangle(cornerRadius: 10))
        .accessibilityElement(children: .contain)
        .accessibilityLabel(done ? "Approved action, \(label)" : "Suggested action, \(label)")
    }

    private var errorBar: some View {
        HStack(spacing: 8) {
            Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red)
            Spacer()
            Button("Repair Aria") { Task { await repair() } }
            Button("Open Guide") { showGuide = true }
        }.padding(.horizontal, 14).padding(.vertical, 8)
    }

    private var inputBar: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .bottom, spacing: 8) {
                TextField("Ask Aria about Bridle…", text: $input, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .onSubmit { send() }
                    .accessibilityLabel("Ask Aria about Bridle")
                Button(recording ? "Send Recording" : "Talk",
                       systemImage: recording ? "stop.circle.fill" : "mic") {
                    Task { await toggleTalk() }
                }
                .disabled(voiceWorking || thinking)
                .help(recording ? "Stop and send your recording to Aria" : "Talk to Aria using your voice")
                Button("Ask Aria") { send() }
                    .buttonStyle(.borderedProminent)
                    .disabled(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || thinking)
                    .help("Send your question to Aria")
            }
            HStack(spacing: 10) {
                Text(recording ? "Listening… tap Send Recording when you're done."
                     : "Quick built-in listening — for better accuracy, download a speech assistant in Models.")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Toggle("Read Replies", isOn: $speakReplies).toggleStyle(.checkbox).font(.caption)
                if speakReplies {
                    Button("Stop") { speaker.stopSpeaking(at: .immediate) }.font(.caption)
                }
            }
        }.padding(14)
    }

    // ---- data -------------------------------------------------------------------
    private func load() async {
        turns = ChatArchive.load(project: "aria")
        do {
            let status = try await model.featureRequest("GET", "/control/aria/status")
            available = status["available"] as? Bool ?? false
        } catch {
            available = false
            self.error = "Aria could not start. \(error.localizedDescription)"
        }
    }

    private func save() { ChatArchive.save(turns, project: "aria") }

    private func send() {
        let question = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty, !thinking else { return }
        input = ""
        ask(question)
    }

    private func ask(_ question: String) {
        guard !thinking else { return }
        turns.append(ChatTurn(role: .user, text: question))
        save()
        thinking = true
        error = ""
        Task { @MainActor in
            defer { thinking = false }
            do {
                let history = turns.suffix(12).map { ["role": $0.role.rawValue, "content": $0.text] }
                let reply = try await model.featureRequest("POST", "/control/aria/chat",
                                                           body: ["messages": history])
                let turn = ChatTurn(role: .assistant, text: reply["reply"] as? String ?? "")
                turns.append(turn)
                if let action = reply["action"] as? [String: Any] { actions[turn.id] = action }
                save()
                if speakReplies, !turn.text.isEmpty {
                    speaker.speak(AVSpeechUtterance(string: String(turn.text.prefix(2000))))
                }
            } catch {
                self.error = "Aria could not answer. \(error.localizedDescription)"
            }
        }
    }

    private func toggleTalk() async {
        if recording {
            recorder?.stop()
            recording = false
            recorder = nil
            guard let file = voiceFile else { return }
            voiceFile = nil
            defer { try? FileManager.default.removeItem(at: file) }
            voiceWorking = true
            defer { voiceWorking = false }
            do {
                let result = try await model.transcribeForAria(fileURL: file)
                let heard = result.text.trimmingCharacters(in: .whitespacesAndNewlines)
                if heard.count < 2 {
                    error = "I couldn't make that out. Try again closer to the microphone, "
                        + "or download a speech assistant in Models for better accuracy."
                    return
                }
                error = ""
                ask(heard)
            } catch {
                self.error = "Listening did not work. \(error.localizedDescription) "
                    + "For better accuracy, download a speech assistant in Models."
            }
            return
        }
        guard await AVCaptureDevice.requestAccess(for: .audio) else {
            error = "Allow microphone access in System Settings → Privacy & Security → Microphone, then try Talk again."
            return
        }
        do {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".wav")
            voiceFile = url
            let rec = try AVAudioRecorder(url: url, settings: [AVFormatIDKey: kAudioFormatLinearPCM,
                AVSampleRateKey: 16000, AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false])
            speaker.stopSpeaking(at: .immediate)
            guard rec.record(forDuration: 120) else { throw EngineError(status: 0, message: "Could not start the microphone.") }
            recorder = rec
            recording = true
            error = ""
            // record(forDuration:) stops capturing at 120 s; send it then instead of leaving the
            // UI saying "Listening…" over a stopped recorder.
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(120))
                if recording, recorder === rec { await toggleTalk() }
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func repair() async {
        do {
            _ = try await model.featureRequest("POST", "/control/aria/repair", body: [:])
            available = true
            error = ""
        } catch {
            self.error = "Repair did not work. \(error.localizedDescription) "
                + "The Beginner Guide can walk you through setup instead."
        }
    }

    private func approve(turn: ChatTurn, action: [String: Any]) {
        approved.insert(turn.id)
        save()
        switch action["kind"] as? String {
        case "download":
            if let repo = action["repo"] as? String { model.startDownload(repo) }
        case "load":
            if let repo = action["repo"] as? String { model.requestLoad(repo, draft: nil) }
        case "open":
            switch action["screen"] as? String {
            case "models": model.showModelPicker = true
            case "studio": model.showStudio = true
            case "chat": model.bottomPanel = .chat; dismiss()
            case "settings":
                NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
            default: break
            }
            if (action["screen"] as? String) != "chat" { dismiss() }
        default: break
        }
    }
}
