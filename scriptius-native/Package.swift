// swift-tools-version: 5.9
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
