// swift-tools-version:6.0
// Pure protocol and ack logic shared with the app target, unit-tested with
// `swift test` on the Mac (no device or simulator needed).
import PackageDescription

let package = Package(
    name: "PendantCore",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [.library(name: "PendantCore", targets: ["PendantCore"])],
    targets: [
        .target(name: "PendantCore", path: "PendantDirect/Core",
                swiftSettings: [.swiftLanguageMode(.v5)]),
        .testTarget(name: "PendantCoreTests", dependencies: ["PendantCore"], path: "Tests/PendantCoreTests",
                    swiftSettings: [.swiftLanguageMode(.v5)]),
    ]
)
