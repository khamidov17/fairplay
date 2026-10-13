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
        ),
        // Audit T-07: `swift test` (or double-click "Run Mac Tests.command").
        .testTarget(
            name: "ProctoraTests",
            dependencies: ["Proctora"],
            path: "Tests/ProctoraTests"
        )
    ]
)
