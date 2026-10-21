import Foundation
import Testing

@testable import EarshotCore

/// C5: every way a drain cycle ends (completed, stalled, append failed, fsync failed) finishes with
/// SetMode {0,0}, after ACKing only what is durable.
@Suite struct DrainEndTests {
  private func commands(_ p: FakePendant) -> [Command] { p.commands }

  private func acks(_ p: FakePendant) -> [Int] {
    p.commands.compactMap { if case .ack(let n) = $0 { return n } else { return nil } }
  }

  private func make(
    range: ClosedRange<Int>, send: [Data]? = nil, timing: DrainTiming = fastTiming,
    configure: (RecordingSink) -> Void = { _ in }
  ) -> (FakePendant, RecordingSink, DrainEngine) {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = range.lowerBound
    pendant.newest = range.upperBound
    pendant.pagesToSend = send ?? makePages(range)
    let sink = RecordingSink(log: log)
    configure(sink)
    return (pendant, sink, DrainEngine(transport: pendant, sink: sink, timing: timing))
  }

  @Test func completedDrainEndsWithRecordToFlash() async throws {
    let (pendant, _, engine) = make(range: 100...109)
    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(result.end == .caughtUp)
    #expect(commands(pendant).last == .setMode(batch: false, realTime: false))
    // Download started with {1,0} before pages and ended with {0,0}; nothing else switched modes.
    let modes = commands(pendant).filter {
      if case .setMode = $0 { return true } else { return false }
    }
    #expect(
      modes == [.setMode(batch: true, realTime: false), .setMode(batch: false, realTime: false)])
    #expect(acks(pendant) == [109])
  }

  @Test func stalledDrainAcksWhatIsDurableThenRecordToFlash() async throws {
    // Pendant announces 100...159 but goes silent after 30 pages.
    let (pendant, _, engine) = make(
      range: 100...159, send: makePages(100...129), timing: stallTiming)
    try await engine.beginSession()
    let start = ContinuousClock.now
    let result = await engine.runCycle()

    #expect(result.end == .stall)
    #expect(ContinuousClock.now - start >= .milliseconds(300))
    #expect(acks(pendant) == [124, 129])
    #expect(commands(pendant).last == .setMode(batch: false, realTime: false))
  }

  /// C8: the pendant never finishes the page it is still writing, so every page below the newest in hand and
  /// 5 s of quiet (scaled down here) ends the cycle as caught up instead of waiting out the 30 s stall.
  @Test func quietAfterEveryPageBelowTheNewestEndsCaughtUpAndAcksAfterFsync() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 100
    pendant.newest = 109
    pendant.pagesToSend = makePages(100...108)
    let sink = RecordingSink(log: log)
    let engine = DrainEngine(
      transport: pendant, sink: sink,
      timing: DrainTiming(
        statusTimeout: .seconds(2), stall: .seconds(3), newestQuiet: .milliseconds(200),
        ackEveryPages: 25))
    try await engine.beginSession()
    let start = ContinuousClock.now
    let result = await engine.runCycle()
    let elapsed = ContinuousClock.now - start

    #expect(result.end == .caughtUp)
    #expect(elapsed >= .milliseconds(200), "waits the quiet window for the newest")
    #expect(elapsed < .seconds(2), "does not wait out the stall")
    #expect(result.pagesSaved == 9)
    #expect(result.ackedThrough == 108)
    #expect(acks(pendant) == [108], "the newest page is left on the pendant")
    #expect(commands(pendant).last == .setMode(batch: false, realTime: false))
    let events = log.all
    let ack = try #require(events.lastIndex(of: "write:msg7(108)"))
    let lastSync = try #require(events.lastIndex(of: "sync"))
    #expect(lastSync < ack, "ACK only after fsync")
    #expect(events[..<ack].contains("append:108"))
  }

  /// The early end needs every page below the newest: a hole stalls the old way.
  @Test func aHoleBelowTheNewestKeepsWaitingForTheStall() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 100
    pendant.newest = 109
    pendant.pagesToSend = makePages(100...104) + makePages(106...108)
    let engine = DrainEngine(
      transport: pendant, sink: RecordingSink(log: log),
      timing: DrainTiming(
        statusTimeout: .seconds(2), stall: .milliseconds(600), newestQuiet: .milliseconds(100),
        ackEveryPages: 25))
    try await engine.beginSession()
    let start = ContinuousClock.now
    let result = await engine.runCycle()

    #expect(result.end == .stall)
    #expect(ContinuousClock.now - start >= .milliseconds(600))
    #expect(acks(pendant) == [104])
    #expect(result.blockedAt == 105)
  }

  /// C9: the session loop the app runs sends the pendant nothing between cycles: every command falls
  /// inside a cycle, and no battery read happens at all.
  @Test func sessionLoopSendsNothingBetweenCycles() async throws {
    let (pendant, _, engine) = make(range: 100...109)
    try await engine.beginSession()
    let marks = Marks()
    let loop = Task {
      await engine.runLoop(
        firstDelay: .milliseconds(50), interval: .milliseconds(150),
        willCycle: { marks.add(.will, pendant.commands.count) },
        didCycle: { _ in marks.add(.did, pendant.commands.count) })
    }
    // Wait for the cycles, not for the clock: three have run when the third didCycle mark lands.
    let deadline = ContinuousClock.now.advanced(by: .seconds(10))
    while marks.all.filter({ $0.kind == .did }).count < 3, ContinuousClock.now < deadline {
      try await Task.sleep(for: .milliseconds(10))
    }
    loop.cancel()
    await loop.value

    let all = marks.all
    #expect(all.filter { $0.kind == .did }.count >= 3, "several cycles ran")
    // Command count at the end of one cycle equals the count at the start of the next.
    for (done, next) in zip(all, all.dropFirst()) where done.kind == .did && next.kind == .will {
      #expect(done.count == next.count)
    }
    #expect(all.first?.count == 1, "only SetCurrentTime before the first cycle")
    #expect(pendant.batteryReadCount == 0)
    let cycles = all.filter { $0.kind == .did }.count
    #expect(commands(pendant).filter { $0 == .getStatus }.count == cycles)
  }

  @Test func failedAppendAcksOnlyEarlierPagesThenRecordToFlash() async throws {
    let (pendant, sink, engine) = make(range: 100...139) { $0.failAppendAtIndex = 130 }
    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(result.end == .appendFailed)
    // Page 130 never reached the spool, so nothing at or past it is ACKed; 100...129 are.
    #expect(acks(pendant).allSatisfy { $0 < 130 })
    #expect(acks(pendant).last == 129)
    #expect(commands(pendant).last == .setMode(batch: false, realTime: false))
    let events = sink.log.all
    let failAt = try #require(events.firstIndex(of: "append-failed:130"))
    let lastAck = try #require(events.lastIndex(of: "write:msg7(129)"))
    #expect(events[..<lastAck].contains("sync"))
    #expect(!events[failAt...].contains { $0.hasPrefix("append:") })
  }

  @Test func failedFsyncStillRestoresRecordToFlash() async throws {
    let (pendant, _, engine) = make(range: 100...109) { $0.failSync = true }
    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(result.end == .syncFailed)
    #expect(acks(pendant).isEmpty)
    #expect(commands(pendant).last == .setMode(batch: false, realTime: false))
  }

  @Test func emptyPendantStartsNoDownload() async throws {
    let (pendant, _, engine) = make(range: 100...100, send: [])
    pendant.newest = 99
    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(result.end == .nothingToDrain)
    #expect(!commands(pendant).contains(.setMode(batch: true, realTime: false)))
  }

  @Test func silentPendantTimesOutWithoutStartingDownload() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 1
    pendant.newest = 2
    let engine = DrainEngine(
      transport: SilentTransport(pendant), sink: RecordingSink(log: log),
      timing: DrainTiming(
        statusTimeout: .milliseconds(200), stall: .milliseconds(200), ackEveryPages: 25))
    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(result.end == .noStatus)
    #expect(!pendant.commands.contains(.setMode(batch: true, realTime: false)))
  }
}

/// Forwards writes but swallows the pendant's replies.
final class SilentTransport: PendantTransport, @unchecked Sendable {
  private let inner: FakePendant
  init(_ inner: FakePendant) { self.inner = inner }
  func write(_ data: Data) async throws { try await inner.write(data) }
  func setNotificationHandler(_ handler: @escaping @Sendable (Data) -> Void) async {}
  func readBattery() async throws -> Int { 0 }

}

/// A4: the drain-end log line, built in the core so its wording is pinned.
@Suite struct DrainSummaryTests {
  /// The line names what was saved, ACKed and blocked, in the words the log shows.
  @Test func drainEndSummaryNamesSavedAckedAndBlocked() {
    let acked = DrainResult(
      end: .caughtUp, status: nil, pagesSaved: 146, ackedThrough: 54991, blockedAt: nil)
    #expect(
      acked.summary
        == "drain ended caughtUp: saved 146 pages, acked through 54991, blocked at none, pendant pages unknown"
    )
    let blocked = DrainResult(
      end: .stall, status: nil, pagesSaved: 0, ackedThrough: nil, blockedAt: 49052)
    #expect(blocked.summary.contains("acked through none, blocked at 49052"))
  }
}

private final class Marks: @unchecked Sendable {
  enum Kind { case will, did }
  struct Mark {
    let kind: Kind
    let count: Int
  }
  private let lock = NSLock()
  private var items: [Mark] = []
  func add(_ kind: Kind, _ count: Int) {
    lock.withLock { items.append(Mark(kind: kind, count: count)) }
  }
  var all: [Mark] { lock.withLock { items } }
}
