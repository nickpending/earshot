import Foundation
import Testing

@testable import EarshotCore

/// C1: the display decoder reads the real pendant pages; it is never on the save/ACK path.
@Suite struct PageDigestTests {
  private func digest(_ page: Int) throws -> PageDigest {
    PageDigest(storageBuffer: Data(try realStorageBuffer(page)))
  }

  private func date(_ ms: Int) -> Date { Date(timeIntervalSince1970: Double(ms) / 1000) }

  @Test func batteryPageYieldsItsFiveRecords() throws {
    let d = try digest(4891)
    #expect(d.pageIndex == 4891)
    #expect(d.opusFrames == 0)
    #expect(d.batteries.count == 5)
    let first = try #require(d.batteries.first)
    #expect(first.millivolts == 3898)
    #expect(first.milliamps == -13)
    #expect(first.percent == 69)
    // Record time is the page timestamp plus the wrapper's offset (55,962 ms).
    #expect(first.at == date(1_790_916_729_739 + 55_962))
    #expect(d.batteries.map(\.percent) == [69, 69, 69, 69, 69])
    #expect(d.batteries.map(\.milliamps) == [-13, -12, -12, -12, -12])
    #expect(d.modeEvents.isEmpty)
  }

  @Test func audioPagesCountTheirOpusFrames() throws {
    #expect(try digest(3937).opusFrames == 72)
    #expect(try digest(3936).opusFrames == 1)
    #expect(try digest(49123).opusFrames == 72)
    #expect(try digest(3937).pageTime == date(1_790_911_718_910))
  }

  @Test func markerPageHasNoFrames() throws {
    #expect(try digest(3935).opusFrames == 0)
  }

  @Test func idlePageReportsTheButtonLineAndNoFrames() throws {
    let d = try digest(61432)
    #expect(d.opusFrames == 0)
    #expect(d.modeEvents == [.init(at: date(1_791_306_003_828 + 9763), kind: .idle)])
    let b = try #require(d.batteries.first)
    #expect(b.percent == 40)
    #expect(b.millivolts == 3763)
    #expect(b.milliamps == -12)
  }

  @Test func wakePageReportsTheWakeLineAndItsFrames() throws {
    let d = try digest(61433)
    #expect(d.modeEvents == [.init(at: date(1_791_306_021_596 + 40), kind: .wake)])
    #expect(d.opusFrames == 66)
  }

  @Test func headerOnlyPageDecodesToNothing() throws {
    let d = try digest(49052)
    #expect(d.pageIndex == 49052)
    #expect(d.pageTime == nil)
    #expect(d.opusFrames == 0)
    #expect(d.batteries.isEmpty)
    #expect(d.modeEvents.isEmpty)
  }

  @Test func garbageDecodesToNothingWithoutTrapping() {
    for bytes: [UInt8] in [[], [0xff], [0x32, 0x7f, 0x01], [0x32, 0x02, 0x1a, 0x05], [0x08]] {
      let d = PageDigest(storageBuffer: Data(bytes))
      #expect(d.opusFrames == 0)
      #expect(d.batteries.isEmpty)
    }
  }

  /// The drain path must not call the display decoder: the decoder lives beside the engine, not inside it.
  @Test func drainSourcesNeverReferenceTheDecoder() throws {
    let core = URL(fileURLWithPath: #filePath)
      .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
      .appendingPathComponent("Sources/EarshotCore")
    for name in ["DrainEngine.swift", "Spool.swift", "PendantTransport.swift"] {
      let text = try String(contentsOf: core.appendingPathComponent(name), encoding: .utf8)
      #expect(!text.contains("PageDigest"), "\(name) must not call the display decoder")
    }
  }
}
