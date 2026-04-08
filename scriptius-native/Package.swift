// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ScriptiusAudio",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "ScriptiusAudio",
            path: "Sources/ScriptiusAudio",
            swiftSettings: [
                .swiftLanguageMode(.v5)
            ],
            linkerSettings: [
                .linkedFramework("CoreAudio"),
                .linkedFramework("AudioToolbox")
            ]
        )
    ]
)
