// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "Earshot",
  platforms: [.macOS(.v14)],
  products: [
    .executable(name: "Earshot", targets: ["Earshot"])
  ],
  targets: [
    // Everything testable: codec, drain engine, spool, uploader, config, status.
    // No UI and no CoreBluetooth import, so the whole drain path runs under `swift test`.
    .target(
      name: "EarshotCore",
      exclude: ["OMI-LICENSE-NOTICE.md"]
    ),
    // Menu-bar app: SwiftUI, CoreBluetooth transport, login item.
    .executableTarget(
      name: "Earshot",
      dependencies: ["EarshotCore"]
    ),
    .testTarget(
      name: "EarshotCoreTests",
      dependencies: ["EarshotCore"],
      exclude: ["Fixtures"]
    ),
  ]
)
