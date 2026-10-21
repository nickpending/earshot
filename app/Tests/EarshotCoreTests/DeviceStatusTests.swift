import Foundation
import Testing

@testable import EarshotCore

/// C1, C6: the status reader over the real replies, and what it leaves alone.
@Suite struct DeviceStatusTests {
  static func notification(_ name: String) throws -> Data {
    let hex = try String(
      contentsOf: pendantPagesDirectory.appendingPathComponent("\(name).notification.hex"),
      encoding: .utf8
    ).trimmingCharacters(in: .whitespacesAndNewlines)
    var out = Data()
    var i = hex.startIndex
    while i < hex.endIndex {
      let j = hex.index(i, offsetBy: 2)
      out.append(UInt8(hex[i..<j], radix: 16)!)
      i = j
    }
    return out
  }

  static let storage =
    intField(1, 100) + intField(2, 159) + intField(3, 3) + intField(4, 9) + intField(5, 20)

  @Test func theIdleReplyReadsModeOffWithBatteryAndNoCurrent() throws {
    let data = try Self.notification("status-idle")
    let status = try #require(DeviceStatus.parse(data))
    #expect(status.mode == .off)
    #expect(
      status.battery == .init(millivolts: 3657, milliamps: nil, tempC: 28, percent: 14))
    #expect(status.battery?.charging == false)
  }

  @Test func theRecordingReplyReadsModeOnWithADischargingCurrent() throws {
    let data = try Self.notification("status-recording")
    let status = try #require(DeviceStatus.parse(data))
    #expect(status.mode == .on)
    #expect(
      status.battery == .init(millivolts: 3644, milliamps: -11, tempC: 28, percent: 14))
    #expect(status.battery?.charging == false)
  }

  @Test(arguments: ["status-idle", "status-recording"])
  func storageIsWhatOmiReads(name: String) throws {
    let data = try Self.notification(name)
    let omi = try #require(LimitlessProtocol.parseDeviceStatus(data))
    let status = try #require(DeviceStatus.parse(data))
    #expect(status.storage == StorageStatus(omi))
  }

  @Test func aReplyWithNoModeFieldReadsUnknownNotOff() {
    let status = intField(1, 85) + bytesField(5, Self.storage)
    let data = bleWrapper(15, 0, 1, intField(1, 85) + bytesField(5, status))
    #expect(DeviceStatus.parse(data)?.mode == .unknown)
    #expect(DeviceStatus.parse(data)?.battery == nil)
  }

  @Test func aChargingCurrentReadsCharging() {
    let battery = intField(2, 4100) + intField(3, 150) + intField(4, 30) + intField(5, 88)
    let status =
      bytesField(5, Self.storage) + bytesField(7, intField(1, 1)) + bytesField(9, battery)
    let data = bleWrapper(15, 0, 1, bytesField(5, status))
    #expect(DeviceStatus.parse(data)?.battery?.charging == true)
    #expect(DeviceStatus.parse(data)?.battery?.percent == 88)
  }

  @Test func aPageIsNotAStatus() {
    #expect(DeviceStatus.parse(pagePacket(msgIndex: 1, index: 5)) == nil)
  }

  @Test func theEngineHandsOnAPushedStatusWithoutSendingAnything() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    let seen = StatusBox()
    let engine = DrainEngine(
      transport: pendant, sink: RecordingSink(log: log), timing: fastTiming,
      onStatus: { seen.add($0) })
    try await engine.beginSession()
    let before = pendant.writes.count
    pendant.push(try Self.notification("status-idle"))
    #expect(seen.all.map(\.mode) == [.off])
    #expect(pendant.writes.count == before, "a push is read, not asked for")
  }
}

final class StatusBox: @unchecked Sendable {
  private let lock = NSLock()
  private var items: [DeviceStatus] = []
  func add(_ s: DeviceStatus) { lock.withLock { items.append(s) } }
  var all: [DeviceStatus] { lock.withLock { items } }
}

/// A parsed status reply with the fields the menu reads; storage is a pendant with 100 pages.
func deviceStatus(
  _ mode: DeviceStatus.Mode, percent: Int = 50, milliamps: Int? = -12
) -> DeviceStatus {
  DeviceStatus(
    storage: StorageStatus(
      LimitlessProtocol.StorageState(
        oldestFlashPage: 1, newestFlashPage: 100, currentStorageSession: 1, freeCapturePages: 900,
        totalCapturePages: 1000)),
    mode: mode,
    battery: .init(millivolts: 3700, milliamps: milliamps, tempC: 28, percent: percent))
}
