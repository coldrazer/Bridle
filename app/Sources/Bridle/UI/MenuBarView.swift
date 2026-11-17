import AppKit
import SwiftUI

/// Menu bar icon: the horse mark, with a dot while a model is serving.
struct MenuBarLabel: View {
    let status: String

    var body: some View {
        if let image = BrandAssets.mark {
            let icon: NSImage = {
                let copy = image.copy() as? NSImage ?? image
                copy.size = NSSize(width: 22, height: 15)
                copy.isTemplate = true
                return copy
            }()
            Image(nsImage: icon)
                .accessibilityLabel("Bridle – \(status)")
        } else {
            Image(systemName: "cpu")
        }
    }
}

/// Quick actions from the menu bar, so Bridle can run as a background server.
struct MenuBarContent: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Text("Bridle · \(model.statusLabel)")
        if let repo = model.loadedRepo {
            Text(Format.shortName(repo))
        }
        if let endpoint = model.endpoint {
            Button("Copy API URL (\(endpoint)/v1)") { model.copyEndpoint() }
        }
        Divider()
        if !model.downloadedChatModels.isEmpty {
            Menu("Serve Model") {
                ForEach(model.downloadedChatModels) { entry in
                    Button {
                        model.choose(entry.repo)
                    } label: {
                        if entry.repo == model.loadedRepo {
                            Label(entry.name, systemImage: "checkmark")
                        } else {
                            Text(entry.name)
                        }
                    }
                }
            }
            .disabled(!model.isRunning)
        }
        Button(model.isModelReady ? "Unload Model" : "Load Selected Model") { model.togglePlay() }
            .disabled(!model.isRunning)
        Divider()
        Button("Open Bridle") {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        SettingsLink {
            Text("Settings…")
        }
        if Updater.shared.isAvailable {
            Button("Check for Updates…") { Updater.shared.checkForUpdates() }
        }
        Divider()
        Button("Quit Bridle") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }
}
