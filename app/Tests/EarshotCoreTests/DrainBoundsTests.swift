import Foundation
import Testing

@testable import EarshotCore

/// C7: a message that breaks a reassembly bound holds the ACK at its page. C8: the inbox is bounded. C9: a
/// cycle ends at the page limit and the next starts at once.
@Suite struct DrainBoundsTests {
  @Test func aMessageThatBreaksABoundHoldsTheAckAtItsPage() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 10
    pendant.newest = 12
    // Page 11's whole message arrives as fragment 0 of 2, then a second fragment whose seq is past numFrags.
    // Counting fragments alone (the old reassembly) takes that pair for a complete message and saves page 11.
    let page11 = bytesField(2, try reindexed(try realStorageBuffer(66064), to: 11))
    pendant.pagesToSend = [
      pagePacket(msgIndex: 1, index: 10),
      bleWrapper(2, 0, 2, page11),
      bleWrapper(2, 5, 2, [1, 2, 3]),
      pagePacket(msgIndex: 3, index: 12),
    ]
    let sink = RecordingSink(log: log)
    let engine = DrainEngine(transport: pendant, sink: sink, timing: fastTiming)
    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(pendant.acks.allSatisfy { $0 < 11 })
    #expect(result.ackedThrough == 10)
    #expect(result.blockedAt == 11)
    #expect(sink.appended.compactMap { RawPage.pageIndex(of: $0) } == [10, 12])
  }

  @Test func theInboxKeepsTheFirst4096PacketsInOrder() async {
    func packet(_ i: Int) -> Data { Data([UInt8(i % 256), UInt8(i / 256)]) }
    let queue = PacketQueue()
    for i in 0..<5000 { queue.push(packet(i)) }
    var got: [Data] = []
    while let next = await queue.next(timeout: .milliseconds(20)) { got.append(next) }
    let expected: [Data] = (0..<4096).map { packet($0) }

    #expect(got.count == 4096)
    #expect(got == expected)
    #expect(queue.takeDropped() == 904)
  }

  @Test func aCycleThatSawInboxDropsSendsOneNotice() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 100
    pendant.newest = 100 + 4999
    pendant.pagesToSend = makePages(100...(100 + 4999))
    let notices = EventLog()
    let engine = DrainEngine(
      transport: pendant, sink: RecordingSink(log: log), timing: fastTiming,
      notice: { notices.add($0) })
    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(result.end == .pageLimit)
    #expect(result.pagesSaved == 4096)
    #expect(notices.all.filter { $0.contains("inbox") }.count == 1, "\(notices.all)")
  }

  // MARK: Page limit

  private func backlog(pages: Int) -> (FakePendant, RecordingSink, EventLog) {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.ackAware = true
    pendant.oldest = 100
    pendant.newest = 100 + pages - 1
    pendant.pagesToSend = makePages(100...(100 + pages - 1))
    return (pendant, RecordingSink(log: log), log)
  }

  private func timing(limit: Int) -> DrainTiming {
    DrainTiming(
      statusTimeout: .seconds(2), stall: .milliseconds(300), ackEveryPages: 25, pageLimit: limit)
  }

  @Test func aCycleEndsAtThePageLimitAckingAfterFsyncAndTheNextContinuesToCaughtUp() async throws {
    let (pendant, sink, log) = backlog(pages: 70)
    let engine = DrainEngine(transport: pendant, sink: sink, timing: timing(limit: 40))
    try await engine.beginSession()

    let first = await engine.runCycle()
    #expect(first.end == .pageLimit)
    #expect(first.pagesSaved == 40)
    #expect(first.ackedThrough == 139)
    #expect(pendant.commands.last == .setMode(batch: false, realTime: false))
    let events = log.all
    let ack = try #require(events.lastIndex(of: "write:msg7(139)"))
    let sync = try #require(events.lastIndex(of: "sync"))
    #expect(sync < ack, "ACK only after fsync")

    let second = await engine.runCycle()
    #expect(second.end == .caughtUp)
    #expect(second.pagesSaved == 30)
    #expect(second.ackedThrough == 169)
    let appended = sink.appended.compactMap { RawPage.pageIndex(of: $0) }
    #expect(appended == Array(100...169))
  }

  @Test func runLoopStartsTheNextCycleAtOnceAfterAPageLimitAndWaitsAfterCaughtUp() async throws {
    let (pendant, sink, _) = backlog(pages: 70)
    let engine = DrainEngine(transport: pendant, sink: sink, timing: timing(limit: 40))
    try await engine.beginSession()
    let ends = EndsBox()
    let loop = Task {
      await engine.runLoop(
        firstDelay: .milliseconds(10), interval: .seconds(3600),
        willCycle: {}, didCycle: { ends.add($0.end) })
    }
    for _ in 0..<100 where ends.all.count < 2 { try await Task.sleep(for: .milliseconds(50)) }
    try await Task.sleep(for: .milliseconds(300))
    loop.cancel()
    await loop.value

    #expect(ends.all == [.pageLimit, .caughtUp], "no third cycle inside the hour")
  }
}

private final class EndsBox: @unchecked Sendable {
  private let lock = NSLock()
  private var items: [DrainEnd] = []
  func add(_ e: DrainEnd) { lock.withLock { items.append(e) } }
  var all: [DrainEnd] { lock.withLock { items } }
}
