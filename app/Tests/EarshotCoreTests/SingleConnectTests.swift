import Foundation
import Testing

/// C4: the app has one place that connects to a pendant, and it is reached only through the choice. A search of
/// the app's own source, read the way NoSMPWriteTests reads it.
@Suite struct SingleConnectTests {
  static let transportPath = "Earshot/CoreBluetoothTransport.swift"

  @Test func thereIsExactlyOneConnectCall() {
    var calls: [(String, String)] = []
    for (path, text) in NoSMPWriteTests.swiftFiles() {
      for line in text.split(separator: "\n") where line.contains("connect(") {
        calls.append((path, String(line)))
      }
    }
    #expect(calls.count == 1, "expected exactly one connect( call, found \(calls)")
    #expect(calls.first?.0 == Self.transportPath)
  }

  @Test func theConnectIsTheConnectCaseOfTheChoiceInDidDiscover() throws {
    let text = try #require(
      NoSMPWriteTests.swiftFiles().first { $0.0 == Self.transportPath }?.1)
    let lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
    let didDiscover = try #require(lines.firstIndex { $0.contains("didDiscover peripheral") })
    let decide = try #require(
      lines.indices.first { $0 > didDiscover && lines[$0].contains("switch PendantChoice.decide(") }
    )
    let connectCase = try #require(
      lines.indices.first { $0 > decide && lines[$0].contains("case .connect:") })
    let nextCase = try #require(
      lines.indices.first {
        $0 > connectCase && lines[$0].trimmingCharacters(in: .whitespaces).hasPrefix("case .")
      })
    let connectCall = try #require(lines.firstIndex { $0.contains("connect(") })
    #expect(connectCall > connectCase && connectCall < nextCase)
    // The decision is made with the transport's own choice, nothing else.
    #expect(lines[decide].contains("chosen: chosen"))
  }
}
