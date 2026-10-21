import Foundation
import Testing

@testable import EarshotCore

/// C4: every page oldest to newest reaches the spool, and msg 7 never goes out for a page whose record has not
/// been fsynced.
@Suite struct DrainOrderTests {
  /// For every ACK in the log: the page it names was appended, and an fsync came after that append and before
  /// the ACK.
  private func assertEveryAckFollowsFsync(_ events: [String]) {
    var acks = 0
    for (i, e) in events.enumerated() where e.hasPrefix("write:msg7(") {
      acks += 1
      let n = Int(e.dropFirst("write:msg7(".count).dropLast())!
      let appendAt = events.firstIndex(of: "append:\(n)")
      #expect(appendAt != nil, "ACK \(n) names a page that was never appended")
      guard let appendAt else { continue }
      let syncBetween = events[appendAt..<i].contains("sync")
      #expect(syncBetween, "ACK \(n) was written with no fsync after append:\(n): \(events)")
    }
    #expect(acks > 0, "the drain wrote no ACK at all")
  }

  @Test func ackIsWrittenOnlyAfterFsyncOfTheAckedPages() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 100
    pendant.newest = 159
    pendant.pagesToSend = makePages(100...159)
    let result = try await runDrain(pendant, log: log)

    #expect(result.end == .caughtUp)
    #expect(result.pagesSaved == 60)
    #expect(result.ackedThrough == 159)
    let events = log.all
    assertEveryAckFollowsFsync(events)
    // 60 pages at 25 per ACK: ACKs at 124, 149 and the final 159.
    let acks = pendant.acks
    #expect(acks == [124, 149, 159])
    // Pages reach the sink oldest to newest.
    let appended = events.compactMap { $0.hasPrefix("append:") ? Int($0.dropFirst(7)) : nil }
    #expect(appended == Array(100...159))
  }

  @Test func aStatusReplyAmongThePagesChangesNeitherWhatIsSavedNorACKed() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 100
    pendant.newest = 159
    let pages = makePages(100...159)
    let push = try DeviceStatusTests.notification("status-idle")
    pendant.pagesToSend = Array(pages[..<30]) + [push] + Array(pages[30...])
    let seen = StatusBox()
    let engine = DrainEngine(
      transport: pendant, sink: RecordingSink(log: log), timing: fastTiming,
      onStatus: { seen.add($0) })
    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(result.end == .caughtUp)
    #expect(result.pagesSaved == 60)
    #expect(pendant.acks == [124, 149, 159])
    #expect(result.status?.storage.newestPage == 159)
    #expect(seen.all.map(\.mode) == [.unknown, .off], "the polled reply, then the push")
  }

  @Test func ackCoversPagesOnDiskInTheRealSpool() async throws {
    let dir = try tempDirectory("order")
    defer { try? FileManager.default.removeItem(at: dir) }
    let spool = try Spool(directory: dir)
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 100
    pendant.newest = 129
    pendant.pagesToSend = makePages(100...129)
    // At the moment each ACK is written, the spool files on disk must already hold the acked page.
    let missing = LockedBox<[Int]>([])
    pendant.onWrite = { cmd in
      guard case .ack(let n) = cmd else { return }
      let files =
        (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil))
        ?? []
      let onDisk = files.compactMap { try? Data(contentsOf: $0) }
        .flatMap { RawRecord.parse($0).records.compactMap(RawPage.pageIndex(of:)) }
      if !onDisk.contains(n) { missing.mutate { $0.append(n) } }
    }
    let engine = DrainEngine(transport: pendant, sink: spool, timing: fastTiming)

    try await engine.beginSession()
    let result = await engine.runCycle()
    try spool.rotate()

    #expect(result.end == .caughtUp)
    #expect(
      missing.value.isEmpty, "ACKs went out before their page was in the spool: \(missing.value)")
    let all = spool.closedSegments().flatMap { RawRecord.parse(try! Data(contentsOf: $0)).records }
    #expect(all.compactMap(RawPage.pageIndex(of:)) == Array(100...129))
    #expect(spool.closedSegments().allSatisfy { $0.lastPathComponent.hasSuffix(".pages.v2") })
  }

  @Test func aSpoolFsyncFailureInsideRotationMeansNoAckEvenAtTheEnd() async throws {
    let dir = try tempDirectory("order-fsync")
    defer { try? FileManager.default.removeItem(at: dir) }
    let spool = try Spool(
      directory: dir, maxSegmentBytes: 3000, fullSync: { _ in throw InjectedFailure() })
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 100
    pendant.newest = 129
    pendant.pagesToSend = makePages(100...129)
    let engine = DrainEngine(transport: pendant, sink: spool, timing: fastTiming)

    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(result.end == .appendFailed)
    #expect(pendant.acks.isEmpty, "ACKed pages whose fsync failed: \(pendant.acks)")
    #expect(pendant.commands.last == .setMode(batch: false, realTime: false))
  }

  @Test func failedFsyncMeansNoAckAtAll() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 100
    pendant.newest = 129
    pendant.pagesToSend = makePages(100...129)
    let sink = RecordingSink(log: log)
    sink.failSync = true
    let engine = DrainEngine(transport: pendant, sink: sink, timing: fastTiming)

    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(result.end == .syncFailed)
    #expect(result.ackedThrough == nil)
    #expect(!pendant.commands.contains { if case .ack = $0 { return true } else { return false } })
  }

  @Test func aPageThatNeverSurfacedIsNeverAckedPast() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 100
    pendant.newest = 109
    // Page 105's notification is lost.
    pendant.pagesToSend = makePages(100...104) + makePages(106...109)
    let result = try await runDrain(pendant, log: log)

    #expect(result.end == .caughtUp)
    #expect(result.ackedThrough == 104)
    #expect(result.blockedAt == 105)
    let acks = pendant.acks
    #expect(acks.allSatisfy { $0 <= 104 }, "ACKed past the missing page: \(acks)")
  }

  /// The six real StorageBuffers, re-indexed to consecutive pages 3935...3940: a marker, two audio pages, a
  /// telemetry page, a CRC-failed page (no field 6) and an audio page.
  private func realRun() throws -> [[UInt8]] {
    try [3935, 3936, 3937, 4891, 49052, 49123].enumerated().map { n, page in
      try reindexed(realStorageBuffer(page), to: 3935 + n)
    }
  }

  @Test func everyRealPageIsSavedRawAndAckedAfterTheFsync() async throws {
    let dir = try tempDirectory("order-real")
    defer { try? FileManager.default.removeItem(at: dir) }
    let log = EventLog()
    let spool = try Spool(
      directory: dir,
      fullSync: { fd in
        log.add("fsync")
        try Spool.fullSync(fd)
      })
    let buffers = try realRun()
    let pendant = FakePendant(log: log)
    pendant.oldest = 3935
    pendant.newest = 3940
    pendant.pagesToSend = buffers.enumerated().map { storageBufferPacket(msgIndex: 10 + $0, $1) }
    let engine = DrainEngine(transport: pendant, sink: spool, timing: fastTiming)

    try await engine.beginSession()
    let result = await engine.runCycle()
    let events = log.all
    try spool.rotate()

    #expect(result.end == .caughtUp)
    #expect(result.pagesSaved == 6)
    #expect(result.ackedThrough == 3940)
    #expect(result.blockedAt == nil)
    #expect(pendant.acks == [3940])
    let ackAt = try #require(events.firstIndex(of: "write:msg7(3940)"))
    #expect(events[..<ackAt].contains("fsync"), "ACK 3940 written before any fsync: \(events)")
    let segments = spool.closedSegments()
    #expect(segments.count == 1)
    let records = RawRecord.parse(try Data(contentsOf: segments[0])).records
    #expect(records.map { [UInt8]($0) } == buffers, "the spool must hold the raw bytes unchanged")
  }

  @Test func aPageWithoutAReadableIndexIsSavedButNeverAdvancesTheRun() async throws {
    let log = EventLog()
    let sink = RecordingSink(log: log)
    let notices = LockedBox<[String]>([])
    let pendant = FakePendant(log: log)
    pendant.oldest = 3935
    pendant.newest = 3937
    // Field 5 of the CRC-failed page is cut off mid-varint, so its index cannot be read.
    var unreadable = try reindexed(realStorageBuffer(49052), to: 3936)
    unreadable = Array(unreadable.dropLast(2)) + [0x28]
    #expect(RawPage.pageIndex(of: Data(unreadable)) == nil)
    pendant.pagesToSend = [
      storageBufferPacket(msgIndex: 10, try reindexed(realStorageBuffer(3935), to: 3935)),
      storageBufferPacket(msgIndex: 11, unreadable),
      storageBufferPacket(msgIndex: 12, try reindexed(realStorageBuffer(3937), to: 3937)),
    ]
    let engine = DrainEngine(
      transport: pendant, sink: sink, timing: fastTiming,
      notice: { line in notices.mutate { $0.append(line) } })

    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(result.pagesSaved == 3)
    #expect(sink.appended.contains(Data(unreadable)), "the raw bytes are saved")
    #expect(result.ackedThrough == 3935, "an unreadable page does not extend the contiguous run")
    #expect(pendant.acks.allSatisfy { $0 <= 3935 })
    #expect(notices.value.contains { $0.contains("page without a readable index") })
  }

  @Test func setCurrentTimeIsTheFirstWrite() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    let engine = DrainEngine(
      transport: pendant, sink: RecordingSink(log: log), timing: fastTiming,
      now: { Date(timeIntervalSince1970: 1_750_000_000) })

    try await engine.beginSession()
    #expect(pendant.commands == [.setTime])
    // Message index 0, request id 1, clock 1750000000000: byte-identical to omi's msg6 encoder vector.
    let first = try #require(pendant.writes.first)
    let hex = first.map { String(format: "%02x", $0) }.joined()
    #expect(hex == "080010001801221032070880b883a1f732f2010408011000")
  }
}

final class LockedBox<T>: @unchecked Sendable {
  private let lock = NSLock()
  private var v: T
  init(_ v: T) { self.v = v }
  var value: T { lock.withLock { v } }
  func mutate(_ f: (inout T) -> Void) { lock.withLock { f(&v) } }
}
