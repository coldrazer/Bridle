import SwiftUI

/// Shown until the Python engine is installed and running, or when it has failed.
struct EngineSetupOverlay: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        ZStack {
            Rectangle().fill(.ultraThinMaterial)
            VStack(spacing: 14) {
                if case .failed = model.engineState {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.system(size: 40))
                        .foregroundStyle(.secondary)
                } else {
                    BridleMark(full: true)
                        .frame(width: 200)
                        .padding(.bottom, 6)
                }
                Text(title)
                    .font(.title2.weight(.semibold))
                Text(message)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .textSelection(.enabled)
                    .frame(maxWidth: 440)
                actions
            }
            .padding(32)
            .background(.background, in: RoundedRectangle(cornerRadius: 18))
            .shadow(color: .black.opacity(0.15), radius: 20, y: 8)
        }
    }

    private var title: String {
        switch model.engineState {
        case .failed: return "Engine stopped"
        case .settingUp: return "Installing the engine…"
        default: return "Set up local inference"
        }
    }

    private var message: String {
        switch model.engineState {
        case .needsSetup(let reason):
            return reason + "\n\nSetup creates a private Python environment and installs MLX (about 300 MB). Models are downloaded separately."
        case .settingUp:
            return "Installing MLX and the API server. Progress is shown in Dev Tools."
        case .failed(let message):
            return message
        default:
            return ""
        }
    }

    @ViewBuilder private var actions: some View {
        switch model.engineState {
        case .needsSetup:
            Button("Set Up Engine") { model.runSetup() }
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)
        case .settingUp:
            ProgressView().controlSize(.small)
        case .failed:
            HStack {
                Button("Show Dev Tools") { model.bottomPanel = .devTools }
                Button("Reinstall Engine") { model.runSetup() }
                Button("Restart Engine") { model.restartEngine() }
                    .keyboardShortcut(.defaultAction)
            }
        default:
            EmptyView()
        }
    }
}
