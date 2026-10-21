import Foundation
import Testing

@testable import EarshotCore

/// A fragment as `FragmentBuffer` receives it, through the real wrapper parser.
private func fragment(_ index: Int, _ seq: Int, of numFrags: Int, bytes: Int = 4)
  -> LimitlessProtocol
  .BlePacket
{
  LimitlessProtocol.parseBlePacket(
    bleWrapper(
      index, seq, numFrags, [UInt8](repeating: UInt8(truncatingIfNeeded: seq), count: bytes)))!
}

/// A whole page message (PendantMessage field 2) split into `count` fragments under one wrapper index.
private func splitPage(_ buffer: [UInt8], index: Int, into count: Int) -> [Data] {
  let message = bytesField(2, buffer)
  let size = (message.count + count - 1) / count
  return (0..<count).map { i in
    bleWrapper(index, i, count, Array(message[(i * size)..<min(message.count, (i + 1) * size)]))
  }
}

/// C5: a fragment that contradicts itself drops its message; a truncated page is never saved.
@Suite struct FragmentValidityTests {
  private func engine(
    pages: [Data], notices: EventLog, oldest: Int = 100, newest: Int = 100
  ) -> (FakePendant, RecordingSink, DrainEngine) {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = oldest
    pendant.newest = newest
    pendant.pagesToSend = pages
    let sink = RecordingSink(log: log)
    return (
      pendant, sink,
      DrainEngine(
        transport: pendant, sink: sink, timing: fastTiming, notice: { notices.add($0) })
    )
  }

  @Test func aSeqPastNumFragsDropsTheIndexAndTheLaterCompleteSetIsSavedOnce() async throws {
    let buffer = try reindexed(try realStorageBuffer(66064), to: 100)
    let set = splitPage(buffer, index: 7, into: 3)
    let bad = bleWrapper(7, 5, 3, [1, 2, 3])
    let notices = EventLog()
    let (_, sink, engine) = engine(pages: [set[0], set[1], bad] + set, notices: notices)
    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(sink.appended == [Data(buffer)], "saved once, intact, after the second complete set")
    #expect(result.pagesSaved == 1)
    #expect(notices.all.count == 1)
  }

  @Test func badNumFragsAndDisagreeingNumFragsEachDropTheirIndexWithOneNotice() async throws {
    let buffer = try reindexed(try realStorageBuffer(66064), to: 100)
    let bads = [
      bleWrapper(1, 0, 0, [1]),
      bleWrapper(2, 0, 65, [1]),
      bleWrapper(3, 0, 3, [1]),
      bleWrapper(3, 1, 4, [2]),
    ]
    let notices = EventLog()
    let (_, sink, engine) = engine(
      pages: bads + [storageBufferPacket(msgIndex: 50, buffer)], notices: notices)
    try await engine.beginSession()
    _ = await engine.runCycle()

    #expect(sink.appended == [Data(buffer)])
    #expect(notices.all.count == 3, "\(notices.all)")
  }

  @Test func noCompleteSetBeforeTheValidFragmentsAllArrive() {
    var buffer = FragmentBuffer()
    #expect(buffer.add(fragment(1, 0, of: 3)).whole == nil)
    #expect(buffer.add(fragment(1, 1, of: 3)).whole == nil)
    let bad = buffer.add(fragment(1, 5, of: 3))
    #expect(bad.whole == nil)
    #expect(bad.drops.count == 1)
    #expect(buffer.indexCount == 0)
    #expect(buffer.add(fragment(1, 2, of: 3)).whole == nil, "what was dropped is not completed")
    #expect(buffer.add(fragment(1, 0, of: 3)).whole == nil)
    #expect(buffer.add(fragment(1, 1, of: 3)).whole?.count == 12)
  }

  @Test func differingNumFragsDropsTheIndex() {
    var buffer = FragmentBuffer()
    _ = buffer.add(fragment(4, 0, of: 3))
    let mismatch = buffer.add(fragment(4, 1, of: 4))
    #expect(mismatch.drops.count == 1)
    #expect(buffer.indexCount == 0)
  }
}

/// C6: whatever a device sends, the buffer holds at most 8 messages of 64 KiB.
@Suite struct FragmentBoundsTests {
  @Test func tenThousandFragmentsThatNeverCompleteStayWithinTheBounds() {
    var buffer = FragmentBuffer()
    let perMessage = 64 * 1024
    // Many indices, each fragment near the per-message bound: only the eviction keeps the count down.
    for n in 0..<5000 {
      _ = buffer.add(fragment(n, 0, of: 2, bytes: 60_000))
      #expect(buffer.indexCount <= 8)
      #expect(buffer.totalBytes <= 8 * perMessage)
    }
    // Few indices, each growing a fragment at a time: only the byte bound keeps one message down.
    buffer = FragmentBuffer()
    for n in 0..<5000 {
      _ = buffer.add(fragment(n / 9, n % 9, of: 64, bytes: 8000))
      #expect(buffer.largestMessageBytes <= perMessage)
      #expect(buffer.indexCount <= 8)
      #expect(buffer.totalBytes <= 8 * perMessage)
    }
  }

  @Test func theNinthIndexEvictsTheFirstSeen() async throws {
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 1
    pendant.newest = 9
    let sets = try (1...9).map { i in
      splitPage(try reindexed(try realStorageBuffer(66064), to: i), index: i, into: 2)
    }
    pendant.pagesToSend = sets.map { $0[0] } + [sets[0][1], sets[8][1]]
    let sink = RecordingSink(log: log)
    let notices = EventLog()
    let engine = DrainEngine(
      transport: pendant, sink: sink, timing: fastTiming, notice: { notices.add($0) })
    try await engine.beginSession()
    _ = await engine.runCycle()

    let saved = sink.appended.compactMap { RawPage.pageIndex(of: $0) }
    #expect(saved == [9], "index 1 was evicted by the ninth; index 9 completed")
    #expect(notices.all.count >= 1)
  }

  @Test func theRealFourKilobytePageInNineFragmentsIsSavedByteForByte() async throws {
    let buffer = try realStorageBuffer(66064)
    let message = bytesField(2, buffer)
    #expect(message.count > 8 * 490 && message.count <= 9 * 490, "nine 490-byte fragments")
    let frags = (0..<9).map { i in
      bleWrapper(3, i, 9, Array(message[(i * 490)..<min(message.count, (i + 1) * 490)]))
    }
    let log = EventLog()
    let pendant = FakePendant(log: log)
    pendant.oldest = 66064
    pendant.newest = 66064
    pendant.pagesToSend = frags
    let sink = RecordingSink(log: log)
    let engine = DrainEngine(transport: pendant, sink: sink, timing: fastTiming)
    try await engine.beginSession()
    let result = await engine.runCycle()

    #expect(sink.appended == [Data(buffer)])
    #expect(result.ackedThrough == 66064)
  }
}
