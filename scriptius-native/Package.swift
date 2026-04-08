// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ScriptiusAudio",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "ScriptiusAudio",
            path: "Sources/ScriptiusAudio",
            linkerSettings: [
                .linkedFramework("CoreAudio"),
                .linkedFramework("AudioToolbox")
            ]
        )
    ]
)
