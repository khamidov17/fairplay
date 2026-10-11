// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Proctora",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "Proctora",
            path: "Sources/Proctora",
            linkerSettings: [.linkedFramework("WebKit"), .linkedFramework("Carbon")]
        )
    ]
)
