// swift-tools-version: 6.2
import Foundation
import PackageDescription

let env = ProcessInfo.processInfo.environment
let waxPath = env["WAX_PATH"] ?? "/Users/nikiv/repos/christopherkarani/Wax"

let package = Package(
    name: "seqmem",
    platforms: [
        .macOS(.v26),
    ],
    products: [
        .library(name: "seqmem", type: .dynamic, targets: ["seqmem"]),
    ],
    dependencies: [
        // Local dev dependency. For portability, set WAX_PATH or vendor Wax into this repo.
        .package(path: waxPath),
    ],
    targets: [
        .target(
            name: "seqmem",
            dependencies: [
                .product(name: "WaxCore", package: "Wax"),
            ],
            swiftSettings: [
                .enableExperimentalFeature("StrictConcurrency"),
            ]
        ),
    ]
)

