import Foundation
import Testing

@testable import EarshotCore

/// C1: omi's golden fixtures (limitless_golden/*.json, copied unchanged from BasedHardware/omi at 056936e0) replayed
/// through the vendored codec with the expectations of omi's limitless_golden_test.dart.
@Suite struct CodecGoldenTests {
  static let dir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
    .appendingPathComponent("Fixtures/limitless_golden")

  struct Fixture {
    let name: String
    let packets: [Data]
    let expectedPages: [[String: Any]]
    let expectedState: [String: Int]?
  }

  static func load(_ name: String) throws -> Fixture {
    let data = try Data(contentsOf: dir.appendingPathComponent("\(name).json"))
    let json = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
    return Fixture(
      name: name,
      packets: (json["packets"] as? [String] ?? []).map { unhex($0) },
      expectedPages: json["expectedPages"] as? [[String: Any]] ?? [],
      expectedState: json["expectedStorageState"] as? [String: Int])
  }

  static func unhex(_ s: String) -> Data {
    var out = Data()
    var i = s.startIndex
    while i < s.endIndex {
      let j = s.index(i, offsetBy: 2)
      out.append(UInt8(s[i..<j], radix: 16)!)
      i = j
    }
    return out
  }

  static func hex(_ d: Data) -> String { d.map { String(format: "%02x", $0) }.joined() }

  /// What the drain engine does with RX packets: wrapper parse, reassembly by index and seq, message parse.
  /// Returns the pages surfaced after each packet, so tests can see when a page appears.
  static func surfaced(_ packets: [Data]) -> [[LimitlessProtocol.FlashPage]] {
    var buffer: [Int: [Int: Data]] = [:]
    var perPacket: [[LimitlessProtocol.FlashPage]] = []
    for raw in packets {
      var out: [LimitlessProtocol.FlashPage] = []
      if let p = LimitlessProtocol.parseBlePacket(Data(raw)) {
        buffer[p.index, default: [:]][p.seq] = p.payload
        if let parts = buffer[p.index], parts.count == p.numFrags {
          var whole = Data()
          for i in 0..<p.numFrags { if let part = parts[i] { whole.append(part) } }
          buffer.removeValue(forKey: p.index)
          out = LimitlessProtocol.parsePendantMessage(whole)
        }
      }
      perPacket.append(out)
    }
    return perPacket
  }

  static let pageFixtures = [
    "single_fragment_audio_page", "three_fragments_out_of_order", "diagnostic_page_no_audio",
    "nested_audio_containers", "invalid_toc_interleaved", "truncated_trailing_field",
    "audio_page_zero_frames", "overflow_varint_length", "fixed64_fields_page",
    "unknown_wiretype_page",
  ]

  @Test(arguments: pageFixtures)
  func pagesMatchGolden(name: String) throws {
    let f = try Self.load(name)
    let pages = Self.surfaced(f.packets).flatMap { $0 }
    #expect(pages.count == f.expectedPages.count, "\(name): surfaced \(pages.count) pages")
    for (page, expected) in zip(pages, f.expectedPages) {
      #expect(page.index == expected["index"] as? Int)
      #expect(page.session == expected["session"] as? Int)
      #expect(page.timestampMs == Int64(expected["timestampMs"] as! Int))
      #expect(page.opusFrames.map(Self.hex) == expected["frames"] as? [String])
    }
    // No page fixture may parse as a device-status packet.
    #expect(f.packets.allSatisfy { LimitlessProtocol.parseDeviceStatus($0) == nil })
    #expect(f.expectedState == nil)
  }

  @Test func fragmentedPageSurfacesOnlyWhenAllThreeFragmentsArrived() throws {
    let f = try Self.load("three_fragments_out_of_order")
    let perPacket = Self.surfaced(f.packets)
    #expect(f.packets.count == 3)
    #expect(
      perPacket.map(\.count) == [0, 0, 1], "page must not surface before all 3 fragments arrive")
  }

  @Test(arguments: ["audio_page_zero_frames", "fixed64_fields_page", "unknown_wiretype_page"])
  func audioPageWithZeroExtractedFramesIsNeverSurfaced(name: String) throws {
    let f = try Self.load(name)
    #expect(f.expectedPages.isEmpty)
    #expect(
      Self.surfaced(f.packets).flatMap { $0 }.isEmpty,
      "\(name) surfaced a page the drain would ACK past")
  }

  @Test func diagnosticPageWithNoAudioSurfacesWithZeroFrames() throws {
    let pages = Self.surfaced(try Self.load("diagnostic_page_no_audio").packets).flatMap { $0 }
    #expect(pages.count == 1)
    #expect(pages.first?.opusFrames.isEmpty == true)
    #expect(pages.first?.index == 102)
  }

  @Test func deviceStatusMatchesGolden() throws {
    let f = try Self.load("device_status")
    let state = try #require(LimitlessProtocol.parseDeviceStatus(f.packets[0]))
    let expected = try #require(f.expectedState)
    #expect(state.oldestFlashPage == expected["oldest_flash_page"])
    #expect(state.newestFlashPage == expected["newest_flash_page"])
    #expect(state.currentStorageSession == expected["current_storage_session"])
    #expect(state.freeCapturePages == expected["free_capture_pages"])
    #expect(state.totalCapturePages == expected["total_capture_pages"])
    #expect(
      Self.surfaced(f.packets).flatMap { $0 }.isEmpty, "a status packet must not emit a flash page")
  }

  @Test func encodersAreByteExact() throws {
    let data = try Data(contentsOf: Self.dir.appendingPathComponent("encoders.json"))
    let json = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
    let vectors = try #require(json["vectors"] as? [[String: Any]])
    #expect(vectors.count == 7)
    for v in vectors {
      let name = v["name"] as! String
      let idx = v["messageIndex"] as! Int
      let rid = Int64(v["requestId"] as! Int)
      let actual: Data
      switch name {
      case "msg6_set_current_time":
        actual = LimitlessProtocol.encodeSetCurrentTime(
          messageIndex: idx, requestId: rid, timestampMs: Int64(v["timestampMs"] as! Int))
      case "msg7_ack_12345":
        actual = LimitlessProtocol.encodeAcknowledgeProcessedData(
          messageIndex: idx, requestId: rid, upToIndex: v["upToIndex"] as! Int)
      case "msg21_get_device_status":
        actual = LimitlessProtocol.encodeGetDeviceStatus(messageIndex: idx, requestId: rid)
      default:
        actual = LimitlessProtocol.encodeDownloadFlashPages(
          messageIndex: idx, requestId: rid, batchMode: v["batchMode"] as! Bool,
          realTime: v["realTime"] as! Bool)
      }
      #expect(Self.hex(actual) == v["hex"] as? String, "\(name)")
    }
  }
}
