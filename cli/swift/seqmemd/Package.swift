// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "seqmemd",
    platforms: [
        .macOS(.v26),
    ],
    dependencies: [
        // Local dev dependency. If/when this needs to be shareable, vendor Wax into this repo.
        .package(path: "/Users/nikiv/repos/christopherkarani/Wax"),
    ],
    targets: [
        .executableTarget(
            name: "seqmemd",
            dependencies: [
                .product(name: "WaxCore", package: "Wax"),
            ],
            swiftSettings: [
                .enableExperimentalFeature("StrictConcurrency"),
            ]
        ),
    ]
)

