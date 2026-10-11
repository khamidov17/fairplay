// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "ProctoraLockdown",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "ProctoraLockdown", path: "Sources/ProctoraLockdown")
    ]
)
