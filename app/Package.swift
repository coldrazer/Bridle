// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Bridle",
    platforms: [.macOS(.v14)],
    dependencies: [
        // Automatic updates (active only in release builds that carry SUFeedURL + SUPublicEDKey).
        .package(url: "https://github.com/sparkle-project/Sparkle", from: "2.6.0"),
    ],
    targets: [
        .executableTarget(
            name: "Bridle",
            dependencies: [.product(name: "Sparkle", package: "Sparkle")],
            path: "Sources/Bridle",
            exclude: ["Resources"],   // logo + icon; copied into the .app by scripts/build_app.sh
            linkerSettings: [.unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"])]
        ),
        .testTarget(
            name: "BridleTests",
            dependencies: ["Bridle"],
            path: "Tests/BridleTests",
            resources: [.copy("Fixtures")]
        ),
    ]
)
