import AppKit
import SwiftUI

@main
struct BridleApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var model = AppModel()

    var body: some Scene {
        Window("Bridle", id: "main") {
            ContentView()
                .environment(model)
                .frame(minWidth: 960, minHeight: 600)
                .onAppear {
                    appDelegate.model = model
                    QuickAssistantController.shared.model = model
                    QuickAssistantController.shared.install()
                    model.bootstrap()
                }
        }
        .windowToolbarStyle(.unified(showsTitle: false))
        .commands {
            CommandGroup(replacing: .appInfo) {
                Button("About Bridle") {
                    var options: [NSApplication.AboutPanelOptionKey: Any] = [.applicationName: "Bridle"]
                    if let icon = BrandAssets.appIcon { options[.applicationIcon] = icon }
                    NSApp.orderFrontStandardAboutPanel(options: options)
                }
                if Updater.shared.isAvailable {
                    Button("Check for Updates…") { Updater.shared.checkForUpdates() }
                }
            }
            CommandGroup(after: .toolbar) {
                Button("Toggle Inspector") { model.showInspector.toggle() }
                    .keyboardShortcut("i", modifiers: [.command, .option])
                Divider()
                Button("Show Sandbox Chat") { model.bottomPanel = .chat }
                    .keyboardShortcut("1", modifiers: [.command])
                Button("Show Requests") { model.bottomPanel = .requests }
                    .keyboardShortcut("2", modifiers: [.command])
                Button("Show Benchmarks") { model.bottomPanel = .benchmarks }
                    .keyboardShortcut("3", modifiers: [.command])
                Button("Show Dev Tools") { model.bottomPanel = .devTools }
                    .keyboardShortcut("4", modifiers: [.command])
            }
            CommandMenu("Assistant") {
                Button("Quick Assistant") { QuickAssistantController.shared.toggle() }
                    .keyboardShortcut(.space, modifiers: [.control, .option])
                Button("Workspaces & Advanced Tools…") { model.showStudio = true }
                    .keyboardShortcut("j", modifiers: [.command, .shift])
            }
            CommandMenu("Models") {
                Button("Import Models from My Mac…") { model.showLocalImport = true }
                Button("Browse Models…") { model.showModelPicker = true }
                    .keyboardShortcut("m", modifiers: [.command, .shift])
                Button("Mesh Mode…") { model.studioSection = "Mesh"; model.showStudio = true }
                    .disabled(!model.isRunning)
                Button("Compare Models…") { model.showCompare = true }
                    .keyboardShortcut("k", modifiers: [.command, .shift])
                    .disabled(!model.isRunning)
                Divider()
                Button(model.isModelReady ? "Unload Model" : "Load Selected Model") { model.togglePlay() }
                    .keyboardShortcut("r", modifiers: [.command])
                    .disabled(!model.isRunning)
                Button("Restart Engine") { model.restartEngine() }
            }
        }

        MenuBarExtra(isInserted: Binding(get: { model.showMenuBarIcon }, set: { model.showMenuBarIcon = $0 })) {
            MenuBarContent()
                .environment(model)
        } label: {
            MenuBarLabel(status: model.statusLabel)
        }
        .menuBarExtraStyle(.menu)

        Settings {
            SettingsView()
                .environment(model)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var model: AppModel?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // `swift run` launches a bare executable; make it a regular foreground app with a Dock icon.
        NSApp.setActivationPolicy(.regular)
        if let icon = BrandAssets.appIcon { NSApp.applicationIconImage = icon }
        NSApp.activate(ignoringOtherApps: true)
        AppAppearance.apply()
        _ = Updater.shared
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        MainActor.assumeIsolated { !(model?.keepRunningInMenuBar ?? false) }
    }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { model?.shutdown() }
    }
}
