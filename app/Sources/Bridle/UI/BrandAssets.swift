import AppKit
import SwiftUI

/// Logo artwork. In the built .app it lives in Contents/Resources; in `swift run` / Xcode
/// development builds it is read straight from Sources/Bridle/Resources.
enum BrandAssets {
    static let logo = load("BridleLogo", "png")       // horse + wordmark, transparent
    static let mark = load("BridleMark", "png")       // horse only, transparent
    static let appIcon = load("AppIcon", "icns")

    private static func load(_ name: String, _ ext: String) -> NSImage? {
        if let url = Bundle.main.url(forResource: name, withExtension: ext), let image = NSImage(contentsOf: url) {
            return image
        }
        // <repo>/app/Sources/Bridle/UI/BrandAssets.swift -> <repo>/app/Sources/Bridle/Resources
        let dev = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Resources/\(name).\(ext)")
        return NSImage(contentsOf: dev)
    }
}

/// The line-art horse, tinted to the current text color so it reads in light and dark mode.
struct BridleMark: View {
    var full = false

    var body: some View {
        if let image = full ? BrandAssets.logo : BrandAssets.mark {
            Image(nsImage: image)
                .renderingMode(.template)
                .resizable()
                .interpolation(.high)
                .aspectRatio(contentMode: .fit)
                .foregroundStyle(.primary)
        } else {
            Text("BRIDLE")
                .font(.system(.title, design: .default).weight(.light))
                .tracking(6)
        }
    }
}
