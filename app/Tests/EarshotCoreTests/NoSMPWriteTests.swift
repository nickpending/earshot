import Foundation
import Testing

@testable import EarshotCore

/// C9: nothing writes to the SMP service, and server_url and token come only from
/// ~/.config/earshot/config.toml. Both are searches of the app's own source.
@Suite struct NoSMPWriteTests {
  static let sources = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("Sources")

  /// Every .swift file under Sources/, as (relative path, text).
  static func swiftFiles() -> [(String, String)] {
    let e = FileManager.default.enumerator(at: sources, includingPropertiesForKeys: nil)!
    return (e.allObjects as! [URL]).filter { $0.pathExtension == "swift" }.map {
      (
        String($0.path.dropFirst(sources.path.count + 1)),
        (try? String(contentsOf: $0, encoding: .utf8)) ?? ""
      )
    }
  }

  @Test func sourcesAreFound() {
    let names = Self.swiftFiles().map(\.0)
    #expect(names.contains("EarshotCore/DrainEngine.swift"))
    #expect(names.contains("Earshot/CoreBluetoothTransport.swift"))
  }

  @Test func noSourceNamesTheSMPServiceOrItsCharacteristic() {
    for (path, text) in Self.swiftFiles() {
      let t = text.lowercased()
      #expect(!t.contains("8d53dc1d"), "\(path) names the SMP service")
      #expect(!t.contains("da2e7828"), "\(path) names the SMP characteristic")
    }
  }

  @Test func theOnlyCharacteristicWriteIsToTX() {
    var writes: [(String, String)] = []
    for (path, text) in Self.swiftFiles() {
      for line in text.split(separator: "\n") where line.contains("writeValue(") {
        writes.append((path, String(line)))
      }
    }
    #expect(writes.count == 1, "expected exactly one writeValue call, found \(writes)")
    #expect(writes.first?.0 == "Earshot/CoreBluetoothTransport.swift")
    #expect(writes.first?.1.contains("for: tx") == true)
  }

  @Test func discoveryNeverAsksForAnythingButTheLimitlessAndBatteryServices() throws {
    let text = try #require(
      Self.swiftFiles().first { $0.0 == "Earshot/CoreBluetoothTransport.swift" }?.1)
    #expect(text.contains("discoverServices([Self.limitless, Self.battery])"))
    #expect(!text.contains("discoverServices(nil)"))
  }

  @Test func noSourceHardCodesAServerOrToken() {
    for (path, text) in Self.swiftFiles() {
      #expect(
        text.range(of: #""https?://"#, options: .regularExpression) == nil,
        "\(path) has a URL literal")
      if path != "EarshotCore/Config.swift" {
        #expect(!text.contains("server_url"), "\(path) reads server_url outside Config.swift")
        #expect(!text.contains("config.toml"), "\(path) names the config file outside Config.swift")
      }
    }
  }

  @Test func configPathIsTheDotConfigFile() {
    #expect(EarshotConfig.defaultPath.path.hasSuffix("/.config/earshot/config.toml"))
  }
}
