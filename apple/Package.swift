// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "StreamlyApple",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "StreamlyKit", targets: ["StreamlyKit"]),
        .library(name: "StreamlyPlayer", targets: ["StreamlyPlayer"]),
        .executable(name: "streamly-smoke", targets: ["streamly-smoke"]),
    ],
    dependencies: [
        // LGPL product only: never depend on "MPVKit-GPL", the app must stay closed-source friendly.
        .package(url: "https://github.com/mpvkit/MPVKit.git", exact: "1.0.0"),
    ],
    targets: [
        .target(name: "StreamlyKit"),
        .target(
            name: "StreamlyPlayer",
            dependencies: [.product(name: "MPVKit", package: "MPVKit")]
        ),
        .executableTarget(
            name: "streamly-smoke",
            dependencies: ["StreamlyPlayer", "StreamlyKit"]
        ),
        .testTarget(name: "StreamlyKitTests", dependencies: ["StreamlyKit"]),
    ]
)
