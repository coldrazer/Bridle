import AppKit

/// Bridle's own appearance (System, Light or Dark). It changes only Bridle's windows, never the Mac's
/// system-wide setting. Stored in UserDefaults under "appearance": "system" | "light" | "dark".
enum AppAppearance: String, CaseIterable, Identifiable {
    case system, light, dark
    static let key = "appearance"
    var id: String { rawValue }
    var title: String { self == .system ? "Match System" : rawValue.capitalized }

    static var current: AppAppearance {
        AppAppearance(rawValue: UserDefaults.standard.string(forKey: key) ?? "") ?? .system
    }

    static func apply(_ value: AppAppearance = current) {
        switch value {
        case .system: NSApp.appearance = nil
        case .light: NSApp.appearance = NSAppearance(named: .aqua)
        case .dark: NSApp.appearance = NSAppearance(named: .darkAqua)
        }
    }
}
